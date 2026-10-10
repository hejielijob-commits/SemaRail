"""Durable manual regression cases and metadata-only governed query runs."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

try:
    from .access_control import AccessControlError, AuthContext, BOOTSTRAP_SUBJECT_ID, _timestamp
    from .diagnostics import DiagnosticError, _SECRET
    from .regression_assertions import evaluate, validate_case
except ImportError:  # pragma: no cover
    from access_control import AccessControlError, AuthContext, BOOTSTRAP_SUBJECT_ID, _timestamp
    from diagnostics import DiagnosticError, _SECRET
    from regression_assertions import evaluate, validate_case


_TERMINAL = {"completed", "cancelled", "interrupted"}
_MIGRATION_LOCK = 8_341_972_315_443_004
_QUERY_DEADLINE_MS = 120_000
_CANCEL_RPC_JOIN_SECONDS = 5
# Renewal is fenced: an expired/deleted lease cannot resurrect. A separate
# process-local mutex stays held until Core actually returns, even if DB
# renewal fails. deadlineMs is forwarded, not assumed to bound connection setup.
_LEASE_SECONDS = 180
# A durable recovery claim prevents a delayed heartbeat from renewing a lease
# after recovery has selected it. This sorts before every valid lease timestamp.
_RECOVERY_CLAIM_EXPIRY = "0001-01-01T00:00:00.000000Z"
_LOCAL_RUN_MUTEXES: dict[tuple[str, str, str], threading.Lock] = {}
_LOCAL_RUN_MUTEXES_LOCK = threading.Lock()
_SENSITIVE_METADATA_KEY_PARTS = (
    "password", "passwd", "pwd", "token", "secret", "apikey",
    "credential", "authorization",
)


class _SQLiteExecutionLock:
    """An OS-owned project lock whose lifetime follows the open file handle."""

    def __init__(self, stream: Any, *, windows: bool) -> None:
        self._stream = stream
        self._windows = windows
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            try:
                if self._windows:
                    import msvcrt
                    self._stream.seek(0)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor also releases the OS lock.
                pass
            finally:
                self._stream.close()


class _PostgresExecutionLock:
    """A session advisory lock held by a dedicated PostgreSQL connection."""

    def __init__(self, connection: Any, key: int) -> None:
        self._connection = connection
        self._key = key
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            connection, self._connection = self._connection, None
            try:
                connection.execute("SELECT pg_advisory_unlock(%s)", (self._key,))
                connection.commit()
            except Exception:
                # Closing the dedicated session releases a session advisory lock.
                pass
            finally:
                try:
                    connection.close()
                except Exception:
                    pass


class _ProjectExecutionGuard:
    """Own the in-process and cross-process project lock until explicit release."""

    def __init__(self, local_lock: threading.Lock, storage_lock: Any) -> None:
        self._local_lock = local_lock
        self._storage_lock = storage_lock
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            try:
                self._storage_lock.release()
            finally:
                self._local_lock.release()


def _scope_lock_material(scope: tuple[str, str]) -> bytes:
    return json.dumps(
        ["semarail-regression-project-lock-v1", scope[0], scope[1]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _acquire_sqlite_execution_lock(database_path: Any, scope: tuple[str, str]) -> _SQLiteExecutionLock:
    try:
        resolved = os.path.realpath(os.fspath(database_path))
        directory = os.path.dirname(resolved)
        digest = hashlib.sha256(resolved.encode("utf-8") + b"\0" + _scope_lock_material(scope)).hexdigest()
        lock_path = os.path.join(directory, f".semarail-regression-{digest}.lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        descriptor = os.open(lock_path, flags, 0o600)
    except Exception as exc:
        raise DiagnosticError(
            "REGRESSION_LOCK_UNAVAILABLE", "project execution lock is unavailable", status=503
        ) from exc

    stream = None
    try:
        if os.name == "posix":
            os.chmod(lock_path, 0o600)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
        descriptor = -1
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(exc, "winerror", None) in {33, 36}:
                    raise DiagnosticError(
                        "REGRESSION_BUSY", "a regression query or configuration change is active for this project", status=409
                    ) from exc
                raise
            return _SQLiteExecutionLock(stream, windows=True)
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise DiagnosticError(
                        "REGRESSION_BUSY", "a regression query or configuration change is active for this project", status=409
                    ) from exc
                raise
            return _SQLiteExecutionLock(stream, windows=False)
        raise DiagnosticError(
            "REGRESSION_LOCK_UNAVAILABLE", "project execution lock is unavailable", status=503
        )
    except DiagnosticError:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)
        raise
    except Exception as exc:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)
        raise DiagnosticError(
            "REGRESSION_LOCK_UNAVAILABLE", "project execution lock is unavailable", status=503
        ) from exc


def _postgres_execution_lock_key(scope: tuple[str, str]) -> int:
    unsigned = int.from_bytes(hashlib.sha256(_scope_lock_material(scope)).digest()[:8], "big")
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def _acquire_postgres_execution_lock(access: Any, scope: tuple[str, str]) -> _PostgresExecutionLock:
    database_url = getattr(access, "database_url", None)
    connection_factory = getattr(access, "_connection_factory", None)
    if not isinstance(database_url, str) or not database_url or not callable(connection_factory):
        raise DiagnosticError(
            "REGRESSION_LOCK_UNAVAILABLE", "project execution lock is unavailable", status=503
        )
    key = _postgres_execution_lock_key(scope)
    try:
        connection = connection_factory(database_url)
        row = connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (key,)
        ).fetchone()
        if isinstance(row, Mapping):
            acquired = bool(row.get("acquired"))
        elif row is not None:
            acquired = bool(row[0])
        else:
            acquired = False
        if not acquired:
            connection.close()
            raise DiagnosticError(
                "REGRESSION_BUSY", "a regression query or configuration change is active for this project", status=409
            )
        commit = getattr(connection, "commit", None)
        if not callable(commit):
            raise RuntimeError("session advisory lock connection cannot commit")
        commit()
        return _PostgresExecutionLock(connection, key)
    except DiagnosticError:
        raise
    except Exception as exc:
        try:
            connection.close()
        except Exception:
            pass
        raise DiagnosticError(
            "REGRESSION_LOCK_UNAVAILABLE", "project execution lock is unavailable", status=503
        ) from exc


def _acquire_storage_execution_lock(access: Any, scope: tuple[str, str]) -> Any:
    database_path = getattr(access, "path", None)
    if database_path is not None:
        return _acquire_sqlite_execution_lock(database_path, scope)
    return _acquire_postgres_execution_lock(access, scope)


def _contains_sensitive_metadata_key(value: Any) -> bool:
    """Reject credential fields even when their names are JSON-quoted."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if any(part in normalized for part in _SENSITIVE_METADATA_KEY_PARTS):
                return True
            if _contains_sensitive_metadata_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_metadata_key(item) for item in value)
    return False


def _json(value: Any) -> str:
    if _contains_sensitive_metadata_key(value):
        raise DiagnosticError("INVALID_REGRESSION_CASE", "case inputs cannot contain credential fields")
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DiagnosticError("INVALID_REGRESSION_CASE", "case must contain JSON-safe inputs") from exc
    if len(result.encode("utf-8")) > 256_000 or _SECRET.search(result):
        raise DiagnosticError("INVALID_REGRESSION_CASE", "case inputs are too large or contain credentials")
    return result


def _id(value: Any) -> str:
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) or _SECRET.search(value):
        raise DiagnosticError("INVALID_REQUEST", "identifier is invalid")
    return value


class RegressionStore:
    """Independent versioned regression storage, using the configured control DB."""

    def __init__(self, diagnostics: Any) -> None:
        self.diagnostics = diagnostics
        self.access = diagnostics.access_control
        self.clock = diagnostics.clock
        self._migrate()

    def _migrate(self) -> None:
        with self.access._lock, self.access._connect() as connection:
            if getattr(self.access, "path", None) is None:
                connection.execute("SELECT pg_advisory_xact_lock(?)", (_MIGRATION_LOCK,))
            else:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS regression_schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
            versions = [int(x["version"]) for x in connection.execute("SELECT version FROM regression_schema_migrations ORDER BY version").fetchall()]
            if versions != list(range(1, len(versions) + 1)) or any(version > 2 for version in versions):
                raise DiagnosticError("REGRESSION_STORE_UNAVAILABLE", "unsupported regression storage version", status=503)
            version = versions[-1] if versions else 0
            if version < 1:
                for statement in (
                    "CREATE TABLE regression_test_datasets(id TEXT NOT NULL,organization_id TEXT NOT NULL,project_id TEXT NOT NULL,version TEXT NOT NULL,datasource_id TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(organization_id,project_id,id))",
                    "CREATE TABLE regression_case_versions(id TEXT NOT NULL,organization_id TEXT NOT NULL,project_id TEXT NOT NULL,revision INTEGER NOT NULL,record_json TEXT NOT NULL,PRIMARY KEY(organization_id,project_id,id,revision))",
                    "CREATE TABLE regression_query_runs(id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,project_id TEXT NOT NULL,subject_id TEXT NOT NULL,status TEXT NOT NULL,record_json TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL)",
                    "CREATE INDEX regression_query_run_scope_idx ON regression_query_runs(organization_id,project_id,created_at)",
                    "CREATE TABLE regression_run_leases(organization_id TEXT NOT NULL,project_id TEXT NOT NULL,run_id TEXT NOT NULL,expires_at TEXT NOT NULL,PRIMARY KEY(organization_id,project_id))",
                ):
                    connection.execute(statement)
                connection.execute("INSERT INTO regression_schema_migrations(version,applied_at) VALUES(?,?)", (1, _timestamp(self.clock())))
                version = 1
            if version < 2:
                connection.execute("ALTER TABLE regression_query_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
                connection.execute("INSERT INTO regression_schema_migrations(version,applied_at) VALUES(?,?)", (2, _timestamp(self.clock())))

    @staticmethod
    def _merge_core_trace_ids(record: dict[str, Any], persisted: Mapping[str, Any]) -> None:
        """Carry concurrent metadata-only trace appends into a run update."""

        persisted_results = persisted.get("results")
        if not isinstance(persisted_results, list):
            return
        trace_ids_by_case = {
            item.get("caseId"): item.get("coreTraceIds")
            for item in persisted_results
            if isinstance(item, Mapping) and isinstance(item.get("coreTraceIds"), list)
        }
        for result in record.get("results", []):
            if not isinstance(result, dict):
                continue
            current = result.get("coreTraceIds")
            persisted_ids = trace_ids_by_case.get(result.get("caseId"))
            if not isinstance(current, list) or not isinstance(persisted_ids, list):
                continue
            for trace_id in persisted_ids:
                if trace_id not in current:
                    current.append(trace_id)

    @staticmethod
    def scope(auth: AuthContext, project_id: str) -> tuple[str, str]:
        return auth.subject.organization_id, project_id

    def datasets(self, scope: tuple[str, str]) -> dict[str, Any]:
        with self.access._connect() as connection:
            rows = connection.execute("SELECT * FROM regression_test_datasets WHERE organization_id=? AND project_id=? ORDER BY id", scope).fetchall()
        return {"schemaVersion": 1, "items": [{"id": r["id"], "version": r["version"], "datasourceId": r["datasource_id"], "updatedAt": r["updated_at"]} for r in rows]}

    def save_dataset(self, scope: tuple[str, str], payload: Any, *, dataset_id: str | None = None) -> dict[str, Any]:
        allowed = {"version", "datasourceId"} if dataset_id else {"id", "version", "datasourceId"}
        if not isinstance(payload, Mapping) or set(payload) != allowed:
            raise DiagnosticError("INVALID_REQUEST", "test dataset fields are invalid")
        identifier = _id(dataset_id or payload.get("id"))
        version, datasource = _id(payload.get("version")), _id(payload.get("datasourceId"))
        now = _timestamp(self.clock())
        with self.access._connect() as connection:
            row = connection.execute("SELECT id FROM regression_test_datasets WHERE organization_id=? AND project_id=? AND id=?", (*scope, identifier)).fetchone()
            if dataset_id and row is None:
                raise DiagnosticError("DATASET_NOT_FOUND", "test dataset was not found", status=404)
            if not dataset_id and row is not None:
                raise DiagnosticError("DATASET_CONFLICT", "test dataset already exists", status=409)
            connection.execute("INSERT INTO regression_test_datasets(id,organization_id,project_id,version,datasource_id,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(organization_id,project_id,id) DO UPDATE SET version=excluded.version,datasource_id=excluded.datasource_id,updated_at=excluded.updated_at", (identifier, *scope, version, datasource, now))
        return {"id": identifier, "version": version, "datasourceId": datasource, "updatedAt": now}

    def delete_dataset(self, scope: tuple[str, str], identifier: str) -> None:
        with self.access._connect() as connection:
            connection.execute("DELETE FROM regression_test_datasets WHERE organization_id=? AND project_id=? AND id=?", (*scope, _id(identifier)))

    def cases(self, scope: tuple[str, str]) -> dict[str, Any]:
        with self.access._connect() as connection:
            rows = connection.execute("SELECT record_json FROM regression_case_versions c WHERE organization_id=? AND project_id=? AND revision=(SELECT MAX(revision) FROM regression_case_versions x WHERE x.organization_id=c.organization_id AND x.project_id=c.project_id AND x.id=c.id) ORDER BY id", scope).fetchall()
        records = [json.loads(r["record_json"]) for r in rows]
        identifiers = {r["id"] for r in records}
        legacy = self.diagnostics.export_regression_cases(organization_id=scope[0], project_id=scope[1])["cases"]
        for old in legacy:
            if old["id"] not in identifiers:
                records.append({"id": old["id"], "feedbackId": old["feedbackId"], "sourceTraceId": None, "revision": 0,
                                "status": old["status"], "case": {k: v for k, v in old.items() if k not in {"id", "feedbackId", "status"}}, "datasetVersion": None})
        return {"schemaVersion": 2, "items": [r for r in records if r.get("status") != "deleted"]}

    def case(self, scope: tuple[str, str], identifier: str) -> dict[str, Any]:
        result = next((x for x in self.cases(scope)["items"] if x["id"] == _id(identifier)), None)
        if result is None:
            raise DiagnosticError("CASE_NOT_FOUND", "regression case was not found", status=404)
        return result

    def save_case(self, auth: AuthContext, project_id: str, payload: Any, *, identifier: str | None = None) -> dict[str, Any]:
        allowed = {"expectedRevision", "case", "enable"} if identifier else {"feedbackId", "sourceTraceId", "case", "enable"}
        if not isinstance(payload, Mapping) or set(payload) - allowed or type(payload.get("enable", False)) is not bool:
            raise DiagnosticError("INVALID_REQUEST", "regression case request is invalid")
        scope = self.scope(auth, project_id)
        previous = self.case(scope, identifier) if identifier else None
        expected = payload.get("expectedRevision")
        if previous and (type(expected) is not int or expected != previous["revision"]):
            raise DiagnosticError("CASE_REVISION_CONFLICT", "case changed since it was read", status=409)
        enabled = payload.get("enable", False)
        case = validate_case(payload.get("case"), enabled=enabled)
        _json(case)
        feedback_id = previous["feedbackId"] if previous else _id(payload.get("feedbackId"))
        feedback = None
        if previous is None:
            feedback = self.diagnostics.feedback_detail(
                feedback_id, organization_id=scope[0], project_id=scope[1]
            )
        source_trace = previous.get("sourceTraceId") if previous else payload.get("sourceTraceId")
        if source_trace is not None and previous is None:
            source = self.diagnostics.trace_detail(
                _id(source_trace), organization_id=scope[0], project_id=scope[1]
            )
            feedback_core_id = feedback.get("traceId") if isinstance(feedback, Mapping) else None
            if not isinstance(feedback_core_id, str) or feedback_core_id not in source.get("coreTraceIds", []):
                raise DiagnosticError(
                    "TRACE_FEEDBACK_MISMATCH",
                    "source trace is not linked to the feedback evidence",
                )
        dataset = next((x for x in self.datasets(scope)["items"] if x["id"] == case["testDatasetId"]), None)
        if enabled and dataset is None:
            raise DiagnosticError("DATASET_NOT_FOUND", "register the test dataset before enabling the case")
        now = _timestamp(self.clock())
        record = {"id": identifier or f"case_{uuid.uuid4().hex}", "feedbackId": feedback_id, "sourceTraceId": source_trace,
                  "revision": previous["revision"] + 1 if previous else 1, "status": "enabled" if enabled else "draft",
                  "case": case, "datasetVersion": dataset["version"] if dataset else None,
                  "createdAt": previous.get("createdAt", now) if previous else now, "updatedAt": now}
        with self.access._connect() as connection:
            if getattr(self.access, "path", None) is None:
                connection.execute("SELECT pg_advisory_xact_lock(?)", (_MIGRATION_LOCK,))
            else:
                connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute("SELECT MAX(revision) AS revision FROM regression_case_versions WHERE organization_id=? AND project_id=? AND id=?", (*scope, record["id"])).fetchone()
            if int(latest["revision"] or 0) != (previous["revision"] if previous else 0):
                raise DiagnosticError("CASE_REVISION_CONFLICT", "case changed since it was read", status=409)
            connection.execute("INSERT INTO regression_case_versions(id,organization_id,project_id,revision,record_json) VALUES(?,?,?,?,?)", (record["id"], *scope, record["revision"], _json(record)))
        return record

    def delete_case(self, scope: tuple[str, str], identifier: str) -> None:
        self.case(scope, identifier)
        # Removing reviewed inputs is explicit. Historical run metadata contains no input bodies.
        with self.access._connect() as connection:
            connection.execute("DELETE FROM regression_case_versions WHERE organization_id=? AND project_id=? AND id=?", (*scope, identifier))
            connection.execute("DELETE FROM regression_cases WHERE organization_id=? AND project_id=? AND id=?", (*scope, identifier))

    def recover(self) -> None:
        now = _timestamp(self.clock())
        with self.access._connect() as connection:
            expired = connection.execute(
                "SELECT organization_id,project_id,run_id,expires_at "
                "FROM regression_run_leases WHERE expires_at<=?",
                (now,),
            ).fetchall()
        for lease in expired:
            self._recover_expired_lease(
                (lease["organization_id"], lease["project_id"]),
                lease["run_id"],
                now,
                lease["expires_at"],
            )
        with self.access._connect() as connection:
            connection.execute("DELETE FROM regression_query_runs WHERE expires_at<=?", (now,))

    def _recover_expired_lease(
        self,
        scope: tuple[str, str],
        run_id: str,
        now: str,
        observed_expiry: str,
    ) -> None:
        # Claim in its own short transaction. The expected expiry fences a
        # heartbeat that renewed after the stale expired-lease snapshot, while
        # committing the claim before reading the run lets a concurrent
        # terminal update finish and win its run-record CAS.
        with self.access._connect() as connection:
            claimed = connection.execute(
                "UPDATE regression_run_leases SET expires_at=? "
                "WHERE organization_id=? AND project_id=? AND run_id=? "
                "AND expires_at=? AND expires_at<=?",
                (_RECOVERY_CLAIM_EXPIRY, *scope, run_id, observed_expiry, now),
            )
        if claimed.rowcount != 1:
            return

        for _ in range(64):
            with self.access._connect() as connection:
                lease = connection.execute(
                    "SELECT run_id FROM regression_run_leases "
                    "WHERE organization_id=? AND project_id=? AND run_id=? AND expires_at=?",
                    (*scope, run_id, _RECOVERY_CLAIM_EXPIRY),
                ).fetchone()
                if lease is None:
                    return
                row = connection.execute(
                    "SELECT status,record_json FROM regression_query_runs "
                    "WHERE id=? AND organization_id=? AND project_id=?",
                    (run_id, *scope),
                ).fetchone()
                if row is None or row["status"] in _TERMINAL:
                    connection.execute(
                        "DELETE FROM regression_run_leases "
                        "WHERE organization_id=? AND project_id=? AND run_id=? AND expires_at=?",
                        (*scope, run_id, _RECOVERY_CLAIM_EXPIRY),
                    )
                    return

                record = json.loads(row["record_json"])
                record.update(status="interrupted", updatedAt=now)
                updated = connection.execute(
                    "UPDATE regression_query_runs SET status=?,record_json=? "
                    "WHERE id=? AND organization_id=? AND project_id=? AND status=? "
                    "AND record_json=? AND status NOT IN ('completed','cancelled','interrupted') "
                    "AND EXISTS (SELECT 1 FROM regression_run_leases "
                    "WHERE organization_id=? AND project_id=? AND run_id=? AND expires_at=?)",
                    (
                        "interrupted", _json(record), run_id, *scope,
                        row["status"], row["record_json"], *scope, run_id,
                        _RECOVERY_CLAIM_EXPIRY,
                    ),
                )
                if updated.rowcount == 1:
                    connection.execute(
                        "DELETE FROM regression_run_leases "
                        "WHERE organization_id=? AND project_id=? AND run_id=? AND expires_at=?",
                        (*scope, run_id, _RECOVERY_CLAIM_EXPIRY),
                    )
                    return
            # A concurrent writer changed the run record. Re-read and either
            # retry against the new nonterminal state or leave its terminal
            # result untouched.
        raise DiagnosticError(
            "REGRESSION_STORE_UNAVAILABLE",
            "regression recovery could not commit a concurrent run update",
            status=503,
        )

    def create_run(self, auth: AuthContext, project_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        self.recover()
        scope = self.scope(auth, project_id)
        now = self.clock()
        record = {"schemaVersion": 1, "id": f"rr_{uuid.uuid4().hex}", "status": "queued", "subjectId": auth.subject.id,
                  "caseIds": [c["id"] for c in cases], "results": [], "createdAt": _timestamp(now), "updatedAt": _timestamp(now),
                  "expiresAt": _timestamp(now + timedelta(days=30))}
        with self.access._connect() as connection:
            connection.execute("INSERT INTO regression_run_leases(organization_id,project_id,run_id,expires_at) VALUES(?,?,?,?) ON CONFLICT(organization_id,project_id) DO NOTHING", (*scope, record["id"], _timestamp(now + timedelta(seconds=_LEASE_SECONDS))))
            lease = connection.execute("SELECT run_id FROM regression_run_leases WHERE organization_id=? AND project_id=?", scope).fetchone()
            if lease["run_id"] != record["id"]:
                raise DiagnosticError("REGRESSION_BUSY", "a regression batch is already running for this project", status=409)
            connection.execute("INSERT INTO regression_query_runs(id,organization_id,project_id,subject_id,status,record_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)", (record["id"], *scope, auth.subject.id, "queued", _json(record), record["createdAt"], record["expiresAt"]))
        return record

    def update_run(self, scope: tuple[str, str], record: dict[str, Any]) -> None:
        for _ in range(64):
            with self.access._connect() as connection:
                row = connection.execute(
                    "SELECT status,cancel_requested,record_json FROM regression_query_runs WHERE id=? AND organization_id=? AND project_id=?",
                    (record["id"], *scope),
                ).fetchone()
                if row is None or row["status"] in _TERMINAL:
                    return
                persisted = json.loads(row["record_json"])
                self._merge_core_trace_ids(record, persisted)
                if record["status"] == "completed" and row["cancel_requested"]:
                    record["status"] = "cancelled"
                record["updatedAt"] = _timestamp(self.clock())
                updated = connection.execute(
                    "UPDATE regression_query_runs SET status=?,record_json=? "
                    "WHERE id=? AND organization_id=? AND project_id=? AND status=? "
                    "AND cancel_requested=? AND record_json=?",
                    (
                        record["status"], _json(record), record["id"], *scope,
                        row["status"], row["cancel_requested"], row["record_json"],
                    ),
                )
                if updated.rowcount == 1:
                    if record["status"] in _TERMINAL:
                        connection.execute("DELETE FROM regression_run_leases WHERE run_id=?", (record["id"],))
                    return
        raise DiagnosticError("REGRESSION_STORE_UNAVAILABLE", "regression storage could not commit a concurrent run update", status=503)

    def append_core_trace_id(
        self, scope: tuple[str, str], run_id: str, case_id: str, trace_id: str,
    ) -> None:
        """Persist a late, metadata-only Core trace without changing run status."""

        if (
            not isinstance(trace_id, str) or not trace_id or len(trace_id) > 128
            or _SECRET.search(trace_id)
        ):
            return
        for _ in range(64):
            with self.access._connect() as connection:
                row = connection.execute(
                    "SELECT status,record_json FROM regression_query_runs WHERE id=? AND organization_id=? AND project_id=?",
                    (run_id, *scope),
                ).fetchone()
                if row is None:
                    return
                record = json.loads(row["record_json"])
                target = next(
                    (item for item in record.get("results", []) if item.get("caseId") == case_id),
                    None,
                )
                if target is None:
                    return
                trace_ids = target.get("coreTraceIds")
                if not isinstance(trace_ids, list):
                    return
                if trace_id in trace_ids:
                    return
                trace_ids.append(trace_id)
                record["updatedAt"] = _timestamp(self.clock())
                updated = connection.execute(
                    "UPDATE regression_query_runs SET record_json=? "
                    "WHERE id=? AND organization_id=? AND project_id=? AND status=? AND record_json=?",
                    (_json(record), run_id, *scope, row["status"], row["record_json"]),
                )
                if updated.rowcount == 1:
                    return
        raise DiagnosticError("REGRESSION_STORE_UNAVAILABLE", "regression trace evidence could not be committed concurrently", status=503)

    def request_cancel(self, scope: tuple[str, str], run_id: str) -> None:
        with self.access._connect() as connection:
            connection.execute(
                "UPDATE regression_query_runs SET cancel_requested=1 WHERE id=? AND organization_id=? AND project_id=? AND status NOT IN ('completed','cancelled','interrupted') AND expires_at>?",
                (run_id, *scope, _timestamp(self.clock())),
            )

    def cancel_requested(self, scope: tuple[str, str], run_id: str) -> bool:
        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM regression_query_runs WHERE id=? AND organization_id=? AND project_id=? AND expires_at>?",
                (run_id, *scope, _timestamp(self.clock())),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def heartbeat(self, run_id: str) -> bool:
        with self.access._connect() as connection:
            changed = connection.execute("UPDATE regression_run_leases SET expires_at=? WHERE run_id=? AND expires_at>?", (_timestamp(self.clock() + timedelta(seconds=_LEASE_SECONDS)), run_id, _timestamp(self.clock())))
            return changed.rowcount == 1

    def runs(self, scope: tuple[str, str]) -> dict[str, Any]:
        self.recover()
        with self.access._connect() as connection:
            rows = connection.execute("SELECT record_json FROM regression_query_runs WHERE organization_id=? AND project_id=? AND expires_at>? ORDER BY created_at DESC,id DESC LIMIT 100", (*scope, _timestamp(self.clock()))).fetchall()
        return {"schemaVersion": 1, "items": [json.loads(r["record_json"]) for r in rows]}

    def run(self, scope: tuple[str, str], identifier: str) -> dict[str, Any]:
        self.recover()
        with self.access._connect() as connection:
            row = connection.execute("SELECT record_json FROM regression_query_runs WHERE organization_id=? AND project_id=? AND id=? AND expires_at>?", (*scope, _id(identifier), _timestamp(self.clock()))).fetchone()
        if row is None:
            raise DiagnosticError("RUN_NOT_FOUND", "regression run was not found or has expired", status=404)
        return json.loads(row["record_json"])


class RegressionRunner:
    """Serialize project batches while retaining credentials only in worker memory."""

    def __init__(self, store: RegressionStore, gateway: Any) -> None:
        self.store, self.gateway = store, gateway
        self._lock = threading.Lock()
        self._active: dict[str, dict[str, Any]] = {}
        self.store.recover()

    def _project_mutex(self, scope: tuple[str, str]) -> threading.Lock:
        access = self.store.access
        database = str(getattr(access, "path", None) or getattr(access, "database_url", ""))
        key = (hashlib.sha256(database.encode()).hexdigest(), *scope)
        with _LOCAL_RUN_MUTEXES_LOCK:
            return _LOCAL_RUN_MUTEXES.setdefault(key, threading.Lock())

    def _acquire_project_guard(
        self,
        scope: tuple[str, str],
        *,
        busy_code: str,
        busy_message: str,
    ) -> _ProjectExecutionGuard:
        mutex = self._project_mutex(scope)
        if not mutex.acquire(blocking=False):
            raise DiagnosticError(busy_code, busy_message, status=409)
        try:
            storage_lock = _acquire_storage_execution_lock(self.store.access, scope)
        except DiagnosticError as exc:
            mutex.release()
            if exc.code == "REGRESSION_BUSY":
                raise DiagnosticError(busy_code, busy_message, status=409) from exc
            raise
        except Exception as exc:
            mutex.release()
            raise DiagnosticError(
                "REGRESSION_LOCK_UNAVAILABLE", "project execution lock is unavailable", status=503
            ) from exc
        return _ProjectExecutionGuard(mutex, storage_lock)

    def acquire_configuration_guard(self, auth: AuthContext, project_id: str) -> _ProjectExecutionGuard:
        """Serialize a Console environment mutation against project execution."""

        return self._acquire_project_guard(
            self.store.scope(auth, project_id),
            busy_code="REGRESSION_ENVIRONMENT_BUSY",
            busy_message="configuration cannot change during a regression batch",
        )

    def start(self, auth: AuthContext, project_id: str, authorization: str | None, identifiers: Any) -> dict[str, Any]:
        if auth.subject.id == BOOTSTRAP_SUBJECT_ID:
            raise AccessControlError("FORBIDDEN", "a managed query credential is required", status=403)
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 20 or len(set(_id(x) for x in identifiers)) != len(identifiers):
            raise DiagnosticError("INVALID_REQUEST", "select between one and twenty distinct cases")
        scope = self.store.scope(auth, project_id)
        cases = [self.store.case(scope, identifier) for identifier in identifiers]
        for record in cases:
            if record["status"] != "enabled" or record["case"].get("schemaVersion") != 2:
                raise DiagnosticError("CASE_NOT_EXECUTABLE", "case must be reviewed and enabled with the current schema")
            validate_case(record["case"], enabled=True)
        guard = self._acquire_project_guard(
            scope,
            busy_code="REGRESSION_BUSY",
            busy_message="a Core regression request is still executing for this project",
        )
        try:
            run = self.store.create_run(auth, project_id, cases)
        except Exception:
            guard.release()
            raise
        state = {"cancel": threading.Event(), "queryId": None, "subjectId": auth.subject.id,
                 "runId": run["id"], "leaseLost": False,
                 "cancelLock": threading.Lock(), "cancelInFlightQueryId": None,
                 "cancelAcceptedQueryId": None, "cancelSettled": threading.Event(),
                 "currentQueryId": None, "currentCaseId": None, "currentResult": None,
                 "persistedCaseIds": set(), "scope": scope, "executionGuard": guard}
        state["cancelSettled"].set()
        with self._lock:
            self._active[run["id"]] = state
        try:
            threading.Thread(target=self._execute, args=(scope, run, cases, authorization, state), daemon=True, name="semarail-regression").start()
        except Exception:
            with self._lock:
                self._active.pop(run["id"], None)
            run["status"] = "interrupted"
            try:
                self.store.update_run(scope, run)
            finally:
                guard.release()
            raise DiagnosticError("REGRESSION_UNAVAILABLE", "a regression worker could not start", status=503)
        return run

    def cancel(self, auth: AuthContext, project_id: str, identifier: str, authorization: str | None) -> dict[str, Any]:
        scope = self.store.scope(auth, project_id)
        run = self.store.run(scope, identifier)
        if run["subjectId"] != auth.subject.id:
            raise AccessControlError("FORBIDDEN", "only the running subject can cancel this query", status=403)
        self.store.request_cancel(scope, identifier)
        with self._lock:
            active = self._active.get(identifier)
        if active:
            self._request_cancel(active)
            self._cancel_core(active, authorization)
        return self.store.run(scope, identifier)

    @staticmethod
    def _request_cancel(state: dict[str, Any]) -> None:
        lock = state.setdefault("cancelLock", threading.Lock())
        with lock:
            state["cancel"].set()

    def _cancel_core(self, state: dict[str, Any], authorization: str | None) -> None:
        lock = state.setdefault("cancelLock", threading.Lock())
        with lock:
            query_id = state.get("queryId")
            if (
                not query_id
                or state.get("cancelAcceptedQueryId") == query_id
                or state.get("cancelInFlightQueryId") == query_id
            ):
                return
            state["cancelInFlightQueryId"] = query_id
            state.setdefault("cancelSettled", threading.Event()).clear()
        accepted = False
        trace_id = None
        try:
            status, response = self.gateway.dispatch(
                {"protocolVersion": "3", "id": f"cancel-{uuid.uuid4().hex}", "method": "query.cancel", "params": {"queryId": query_id}},
                authorization,
                transport="regression",
            )
        except Exception:
            pass
        else:
            candidate_trace_id = response.get("traceId")
            if isinstance(candidate_trace_id, str):
                trace_id = candidate_trace_id
            result = response.get("result")
            accepted = (
                status == 200
                and response.get("ok") is True
                and isinstance(result, Mapping)
                and result.get("queryId") == query_id
                and result.get("cancelled") is True
            )
        finally:
            late_persist = False
            case_id = None
            with lock:
                case_id = state.get("currentCaseId")
                current_result = state.get("currentResult")
                if trace_id and current_result is not None:
                    trace_ids = current_result.setdefault("coreTraceIds", [])
                    if trace_id not in trace_ids:
                        trace_ids.append(trace_id)
                    late_persist = case_id in state.get("persistedCaseIds", set())
                if state.get("cancelInFlightQueryId") == query_id:
                    state["cancelInFlightQueryId"] = None
                if accepted and state.get("queryId") == query_id:
                    state["cancelAcceptedQueryId"] = query_id
                if state.get("currentQueryId") == query_id and state.get("queryId") != query_id:
                    state["currentQueryId"] = None
                    state["currentCaseId"] = None
                    state["currentResult"] = None
                state.setdefault("cancelSettled", threading.Event()).set()
            if trace_id and late_persist and case_id:
                try:
                    self.store.append_core_trace_id(scope=state["scope"], run_id=state["runId"], case_id=case_id, trace_id=trace_id)
                except Exception:
                    pass

    def _settle_cancel_rpc(self, state: dict[str, Any], query_id: str) -> bool:
        """Join an in-flight cancel briefly before a case result is persisted."""

        lock = state.setdefault("cancelLock", threading.Lock())
        settled = state.setdefault("cancelSettled", threading.Event())
        deadline = time.monotonic() + _CANCEL_RPC_JOIN_SECONDS
        while True:
            with lock:
                if state.get("cancelInFlightQueryId") != query_id:
                    if state.get("currentQueryId") == query_id:
                        state["currentQueryId"] = None
                        state["currentCaseId"] = None
                        state["currentResult"] = None
                    return True
                settled.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not settled.wait(remaining):
                # Keep the result reference live so a late RPC response can
                # append its trace and patch the already persisted metadata.
                with lock:
                    if state.get("cancelInFlightQueryId") != query_id:
                        continue
                return False

    @contextmanager
    def _environment(self):
        project = self.gateway.project
        with project._lock:
            project._regression_readers = getattr(project, "_regression_readers", 0) + 1
        try:
            yield
        finally:
            with project._lock:
                project._regression_readers -= 1

    def _fingerprint(self) -> tuple[dict[str, Any], str]:
        project = self.gateway.project
        with project._lock:
            overview = project.overview()
            active = project.datasource_records().get(project.active_datasource_identifier())
            # Connection values are hashed in memory only, never retained or returned.
            material = {"revision": overview.get("revision"), "datasource": active.as_dict() if active and hasattr(active, "as_dict") else vars(active) if active else None}
            from pathlib import Path
            state_file = Path(project.datasource_state_file)
            private_state = state_file.read_bytes() if state_file.exists() else b""
            digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode() + private_state).hexdigest()
            return {"datasourceId": project.active_datasource_identifier(), "semanticVersion": overview.get("revision")}, digest

    def _execute(self, scope: tuple[str, str], run: dict[str, Any], cases: list[dict[str, Any]], authorization: str | None, state: dict[str, Any]) -> None:
        stop_heartbeat = threading.Event()

        def beat() -> None:
            last_heartbeat = time.monotonic()
            while not stop_heartbeat.wait(1):
                try:
                    if self.store.cancel_requested(scope, run["id"]):
                        self._request_cancel(state)
                        self._cancel_core(state, authorization)
                except Exception:
                    pass  # Retry a failed cancellation poll without losing the lease.
                if time.monotonic() - last_heartbeat < 10:
                    continue
                try:
                    if self.store.heartbeat(run["id"]):
                        last_heartbeat = time.monotonic()
                        continue
                except Exception:
                    pass
                state["leaseLost"] = True
                self._request_cancel(state)
                self._cancel_core(state, authorization)
                return

        try:
            threading.Thread(target=beat, daemon=True, name="regression-lease").start()
            run["status"] = "running"
            self.store.update_run(scope, run)
            with self._environment():
                for record in cases:
                    outcome = self._run_case(scope, record, authorization, state)
                    with state["cancelLock"]:
                        run["results"].append(outcome)
                        self.store.update_run(scope, run)
                        state["persistedCaseIds"].add(outcome["caseId"])
            run["status"] = "interrupted" if state["leaseLost"] else "cancelled" if state["cancel"].is_set() else "completed"
        except Exception:
            # Do not preserve exceptions: query adapters can include result or connection bodies.
            run["status"] = "interrupted"
        finally:
            stop_heartbeat.set()
            try:
                with state["cancelLock"]:
                    state["queryId"] = None
                    self.store.update_run(scope, run)
            except Exception:
                pass  # Lease expiry recovers the durable interrupted state.
            finally:
                with self._lock:
                    self._active.pop(run["id"], None)
                guard = state.get("executionGuard")
                if guard is not None:
                    guard.release()
                else:
                    # Compatibility for focused recovery calls that bypass start().
                    mutex = state.get("mutex")
                    if mutex is not None:
                        mutex.release()

    def _run_case(self, scope: tuple[str, str], record: dict[str, Any], authorization: str | None, state: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        environment, fingerprint = self._fingerprint()
        result = {"caseId": record["id"], "caseRevision": record["revision"], "status": "blocked", "reasonCode": "DATASET_MISMATCH",
                  "durationMs": 0, "coreTraceIds": [], "assertions": [], "environment": {**environment, "datasetVersion": record["datasetVersion"], "policyVersions": None},
                  "feedbackId": record["feedbackId"], "sourceTraceId": record.get("sourceTraceId")}

        def finish(status: str, reason: str) -> dict[str, Any]:
            result.update(status=status, reasonCode=reason, durationMs=max(0, (time.monotonic() - started) * 1000))
            return result

        if state["cancel"].is_set():
            return finish("cancelled", "CANCELLED")
        case = record["case"]
        dataset = next((x for x in self.store.datasets(scope)["items"] if x["id"] == case["testDatasetId"]), None)
        if dataset is None or dataset["datasourceId"] != environment["datasourceId"] or dataset["version"] != record["datasetVersion"]:
            return finish("blocked", "DATASET_MISMATCH")
        dataset_fingerprint = (dataset["version"], dataset["datasourceId"], dataset["updatedAt"])

        def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
            query_id = params.get("queryId") if method == "query.run" else None
            lock = state.setdefault("cancelLock", threading.Lock())
            if method == "query.run":
                # Publish the in-flight ID only when dispatch is imminent. The
                # same lock makes this check atomic with a local cancel request.
                with lock:
                    if state["cancel"].is_set() or self.store.cancel_requested(scope, state["runId"]):
                        state["cancel"].set()
                        return {"ok": False, "error": {"code": "REGRESSION_CANCELLED"}}
                    if not self.store.heartbeat(state["runId"]):
                        state["leaseLost"] = True
                        state["cancel"].set()
                        raise DiagnosticError("REGRESSION_LEASE_LOST", "the run no longer owns its execution lease")
                    state["queryId"] = query_id
                    state["cancelAcceptedQueryId"] = None
                    state["currentQueryId"] = query_id
                    state["currentCaseId"] = record["id"]
                    state["currentResult"] = result
            else:
                if state["cancel"].is_set() or self.store.cancel_requested(scope, state["runId"]):
                    self._request_cancel(state)
                    self._cancel_core(state, authorization)
                    return {"ok": False, "error": {"code": "REGRESSION_CANCELLED"}}
                if not self.store.heartbeat(state["runId"]):
                    state["leaseLost"] = True
                    self._request_cancel(state)
                    self._cancel_core(state, authorization)
                    raise DiagnosticError("REGRESSION_LEASE_LOST", "the run no longer owns its execution lease")
            try:
                _, response = self.gateway.dispatch({"protocolVersion": "3", "id": f"reg-{uuid.uuid4().hex}", "method": method, "params": params, "deadlineMs": _QUERY_DEADLINE_MS}, authorization, transport="regression")
                trace_id = response.get("traceId")
                if isinstance(trace_id, str):
                    with lock:
                        if trace_id not in result["coreTraceIds"]:
                            result["coreTraceIds"].append(trace_id)
                if isinstance(trace_id, str):
                    try:
                        evidence = self.gateway.diagnostics.core_diagnostic(trace_id, organization_id=scope[0], project_id=scope[1])
                        versions = evidence.get("policyVersions")
                        if isinstance(versions, list):
                            result["environment"]["policyVersions"] = sorted(set((result["environment"]["policyVersions"] or []) + versions))
                    except Exception:
                        pass  # Optional diagnostic failure cannot alter execution.
                return response
            finally:
                if query_id is not None:
                    with lock:
                        if state.get("queryId") == query_id:
                            state["queryId"] = None

        response = rpc("query.prepare", {"question": case["question"], "semanticSql": case["semanticSql"], "conditions": case["conditions"], "confirmedConditions": case["confirmedConditions"]})
        if state["cancel"].is_set():
            return finish("cancelled", "CANCELLED")
        if self._fingerprint()[1] != fingerprint:
            return finish("inconclusive", "ENVIRONMENT_CHANGED")
        if response.get("ok") is True:
            prepared = response.get("result", {})
            if prepared.get("status") != "ready":
                return finish("blocked", "CLARIFICATION_REQUIRED")
            query_id = f"regression-{uuid.uuid4().hex}"
            result["queryId"] = query_id
            response = rpc("query.run", {"question": case["question"], "semanticSql": case["semanticSql"], "queryId": query_id, "preparationId": prepared["preparationId"], "chartIntent": "table"})
            self._settle_cancel_rpc(state, query_id)
            error = response.get("error")
            if isinstance(error, Mapping) and error.get("code") in {"CANCELLED", "REGRESSION_CANCELLED"}:
                return finish("cancelled", "CANCELLED")
        current_dataset = next((x for x in self.store.datasets(scope)["items"] if x["id"] == case["testDatasetId"]), None)
        if self._fingerprint()[1] != fingerprint or current_dataset is None or (current_dataset["version"], current_dataset["datasourceId"], current_dataset["updatedAt"]) != dataset_fingerprint:
            return finish("inconclusive", "ENVIRONMENT_CHANGED")
        outcome, checks = evaluate(case["assertions"], response, semantic_sql=case["semanticSql"])
        result["assertions"] = checks
        return finish(outcome, "ASSERTIONS_PASSED" if outcome == "passed" else "ASSERTIONS_FAILED" if outcome == "failed" else "RESULT_INCOMPLETE")
