"""Production storage backend for the access-control control plane.

SQLite remains the default implementation in :mod:`access_control`. This
module provides the PostgreSQL implementation selected by
``SEMARAIL_ACCESS_CONTROL_DATABASE_URL`` (or ``from_config(database_url=...)``).
The public store methods are inherited unchanged, which keeps credential
hashing, authorization behaviour, and API compatibility identical across
backends.  This layer owns only connection lifecycle, SQL parameter dialect,
and versioned schema migration.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

try:
    from .access_control import (
        DEFAULT_ORGANIZATION_ID,
        AccessControlError,
        AccessControlStore,
        _timestamp,
        _utc_now,
    )
except ImportError:  # pragma: no cover - direct module loading
    from access_control import (  # type: ignore[no-redef]
        DEFAULT_ORGANIZATION_ID,
        AccessControlError,
        AccessControlStore,
        _timestamp,
        _utc_now,
    )


_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})
_INSERT_IGNORE = re.compile(r"\A\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.IGNORECASE)
_LATEST_SCHEMA_VERSION = 2
# A stable, application-specific advisory-lock key. PostgreSQL transaction
# advisory locks serialize migrations without creating a permanent lock row.
_MIGRATION_LOCK_ID = 8_341_972_315_443_001


class _Cursor(Protocol):
    rowcount: int

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...


class _PsycopgConnection:
    """Expose SQLite's tiny ``execute`` surface over a psycopg connection."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, statement: str, params: tuple[Any, ...] = ()) -> _Cursor:
        sql = _postgres_sql(statement)
        return self._connection.execute(sql, params)


def _postgres_sql(statement: str) -> str:
    """Translate the repository's static DB-API SQLite statements safely.

    Statements live in server-owned source code and all values remain bound
    parameters. No user-controlled SQL is passed through this function.
    """

    sql = statement.replace("?", "%s")
    if _INSERT_IGNORE.match(sql):
        sql = _INSERT_IGNORE.sub("INSERT INTO ", sql)
        sql = f"{sql.rstrip().rstrip(';')} ON CONFLICT DO NOTHING"
    return sql


def _connect_postgres(database_url: str) -> Any:
    try:
        import psycopg  # type: ignore[import-not-found]
        from psycopg.rows import dict_row  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AccessControlError(
            "STORE_UNAVAILABLE", "PostgreSQL access-control storage driver is unavailable", status=503
        ) from exc
    try:
        return psycopg.connect(database_url, row_factory=dict_row)
    except Exception as exc:
        # DSNs can contain passwords; neither logs nor client errors receive
        # the underlying driver exception.
        raise AccessControlError(
            "STORE_UNAVAILABLE", "PostgreSQL access-control storage is unavailable", status=503
        ) from exc


class PostgreSQLAccessControlStore(AccessControlStore):
    """PostgreSQL-backed durable control plane with transactional migrations."""

    def __init__(
        self,
        database_url: str,
        *,
        bootstrap_token: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
        connection_factory: Callable[[str], Any] | None = None,
    ) -> None:
        if not isinstance(database_url, str):
            raise AccessControlError("INVALID_REQUEST", "access-control database URL must use PostgreSQL", status=400)
        normalized_url = database_url.strip()
        try:
            parsed = urlsplit(normalized_url)
        except ValueError as exc:
            raise AccessControlError(
                "INVALID_REQUEST", "access-control database URL must use PostgreSQL", status=400
            ) from exc
        if parsed.scheme.lower() not in _POSTGRES_SCHEMES or not normalized_url.lower().startswith(
            ("postgres://", "postgresql://")
        ) or parsed.fragment or (not parsed.netloc and parsed.path in {"", "/"}):
            raise AccessControlError("INVALID_REQUEST", "access-control database URL must use PostgreSQL", status=400)
        self.database_url = normalized_url
        self.path = None
        self.bootstrap_token = (bootstrap_token if bootstrap_token is not None else os.environ.get("SEMARAIL_API_TOKEN", "")).strip()
        self.clock = clock
        self._lock = threading.RLock()
        self._connection_factory = connection_factory or _connect_postgres
        try:
            self._initialize()
        except AccessControlError:
            raise
        except Exception as exc:
            raise AccessControlError(
                "STORE_UNAVAILABLE", "PostgreSQL access-control storage migration failed", status=503
            ) from exc

    @contextmanager
    def _connect(self) -> Iterator[_PsycopgConnection]:
        try:
            connection = self._connection_factory(self.database_url)
        except AccessControlError:
            raise
        except Exception as exc:
            raise AccessControlError(
                "STORE_UNAVAILABLE", "PostgreSQL access-control storage is unavailable", status=503
            ) from exc
        try:
            # psycopg starts a transaction on first statement. The explicit
            # transaction context gives all inherited multi-step operations
            # (credential rotation, identity consume, policy changes) atomic
            # commit/rollback behaviour.
            try:
                with connection.transaction():
                    yield _PsycopgConnection(connection)
            except AccessControlError:
                raise
            except Exception as exc:
                raise AccessControlError(
                    "STORE_UNAVAILABLE", "PostgreSQL access-control storage operation failed", status=503
                ) from exc
        except BaseException:
            raise
        finally:
            try:
                connection.close()
            except Exception:
                # Closing an already-failed connection must not replace the
                # stable, credential-free error raised for the real failure.
                pass

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(?)", (_MIGRATION_LOCK_ID,))
            connection.execute(
                "CREATE TABLE IF NOT EXISTS access_control_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            rows = connection.execute(
                "SELECT version FROM access_control_schema_migrations ORDER BY version"
            ).fetchall()
            applied = [int(row["version"]) for row in rows]
            expected = list(range(1, len(applied) + 1))
            if applied != expected or any(version > _LATEST_SCHEMA_VERSION for version in applied):
                raise AccessControlError(
                    "STORE_SCHEMA_INCOMPATIBLE",
                    "PostgreSQL access-control storage schema is incompatible",
                    status=503,
                )
            version = applied[-1] if applied else 0
            for migration, statements in _MIGRATIONS:
                if migration <= version:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO access_control_schema_migrations(version,applied_at) VALUES(?,?)",
                    (migration, _timestamp(self.clock())),
                )
            connection.execute(
                "INSERT OR IGNORE INTO organizations(id,name,created_at) VALUES(?,?,?)",
                (DEFAULT_ORGANIZATION_ID, "Default organization", _timestamp(self.clock())),
            )


_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            "CREATE TABLE IF NOT EXISTS organizations (id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS subjects (id TEXT PRIMARY KEY,organization_id TEXT NOT NULL REFERENCES organizations(id),kind TEXT NOT NULL CHECK(kind IN ('user','service_account')),name TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('active','disabled')),attributes_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY,subject_id TEXT NOT NULL REFERENCES subjects(id),label TEXT NOT NULL,salt BYTEA NOT NULL,secret_hash BYTEA NOT NULL,created_at TEXT NOT NULL,expires_at TEXT,revoked_at TEXT,last_used_at TEXT)",
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY,subject_id TEXT NOT NULL REFERENCES subjects(id),salt BYTEA NOT NULL,secret_hash BYTEA NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,revoked_at TEXT,last_used_at TEXT)",
            "CREATE TABLE IF NOT EXISTS external_identities (provider_key TEXT NOT NULL,provider TEXT NOT NULL,external_subject TEXT NOT NULL,subject_id TEXT NOT NULL REFERENCES subjects(id),organization_external_id TEXT,profile_json TEXT NOT NULL,last_login_at TEXT NOT NULL,PRIMARY KEY(provider_key,external_subject),UNIQUE(provider_key,subject_id))",
            "CREATE TABLE IF NOT EXISTS identity_transactions (id TEXT PRIMARY KEY,provider TEXT NOT NULL,state_hash BYTEA NOT NULL,device_hash BYTEA NOT NULL,status TEXT NOT NULL CHECK(status IN ('pending','completed','consumed')),subject_id TEXT REFERENCES subjects(id),created_at TEXT NOT NULL,expires_at TEXT NOT NULL,completed_at TEXT,consumed_at TEXT,confirmation_hash BYTEA,confirmation_attempts INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS policies (id TEXT PRIMARY KEY,organization_id TEXT NOT NULL REFERENCES organizations(id),name TEXT NOT NULL,version INTEGER NOT NULL,document_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS policy_bindings (subject_id TEXT NOT NULL REFERENCES subjects(id),policy_id TEXT NOT NULL REFERENCES policies(id),created_at TEXT NOT NULL,PRIMARY KEY(subject_id,policy_id))",
            "CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY,occurred_at TEXT NOT NULL,organization_id TEXT,subject_id TEXT,credential_id TEXT,action TEXT NOT NULL,decision TEXT NOT NULL,resource TEXT,policy_version TEXT,details_json TEXT NOT NULL)",
        ),
    ),
    (
        2,
        (
            "CREATE INDEX IF NOT EXISTS credentials_subject_idx ON credentials(subject_id)",
            "CREATE INDEX IF NOT EXISTS sessions_subject_idx ON sessions(subject_id)",
            "CREATE INDEX IF NOT EXISTS identity_subject_idx ON external_identities(subject_id)",
            "CREATE INDEX IF NOT EXISTS identity_transaction_expiry_idx ON identity_transactions(expires_at)",
            "CREATE INDEX IF NOT EXISTS audit_occurred_idx ON audit_events(occurred_at)",
        ),
    ),
)


__all__ = ["PostgreSQLAccessControlStore"]
