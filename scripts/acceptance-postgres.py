#!/usr/bin/env python3
"""Real PostgreSQL acceptance gate for the Wren data-agent sidecar.

The normal mode provisions an isolated database and login using an existing
administrator connection, loads ``examples/wren-postgres/seed.sql``, and
then drives the real framed sidecar process as the generated read-only login.
No database server is downloaded or started by this script.  Credentials are
accepted only through environment variables, kept in memory, and never
written to reports, command lines, or diagnostics.

``--mode existing`` is useful for a managed PostgreSQL instance.  It performs
the same sidecar and read-only checks against a DSN supplied by an environment
variable, but does not create, alter, or drop database objects.

Examples (PowerShell):

    # Validate paths and environment-source configuration without connecting.
    .venv\\Scripts\\python.exe scripts\\acceptance-postgres.py --dry-run

    # PGHOST/PGPORT/PGUSER/PGPASSWORD (or a libpq service) must identify an
    # administrator.  The script creates and cleans its own database/role.
    .venv\\Scripts\\python.exe scripts\\acceptance-postgres.py

    # Use an existing DSN already held in WREN_DATABASE_URL; this mode never
    # provisions or cleans database objects.
    .venv\\Scripts\\python.exe scripts\\acceptance-postgres.py \\
        --mode existing --database-dsn-env WREN_DATABASE_URL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


PROTOCOL_VERSION = "1"
MAX_FRAME_BYTES = 16 * 1024 * 1024
SAFE_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
SAFE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
EXPECTED_TABLES = ("regions", "customers", "products", "orders", "order_items")
EXPECTED_FEATURES = {"aggregate", "date_grain", "join", "null"}


class E2EFailure(RuntimeError):
    """A safe, user-facing acceptance failure with no driver exception text."""


def _safe_env_name(value: str, label: str) -> str:
    if not SAFE_ENV_NAME.fullmatch(value):
        raise E2EFailure(f"{label} must be an uppercase environment-variable name")
    return value


def _safe_identifier(value: str, label: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise E2EFailure(f"{label} must be a lowercase PostgreSQL identifier")
    return value


def _quote_identifier(value: str) -> str:
    # All generated/provided identifiers have already passed the strict
    # identifier check.  Keep quoting explicit for PostgreSQL correctness.
    return '"' + value.replace('"', '""') + '"'


def _frame(message: Mapping[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            dict(message), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise E2EFailure("could not encode a sidecar request") from exc
    if len(payload) > MAX_FRAME_BYTES:
        raise E2EFailure("sidecar request exceeds the maximum frame size")
    return struct.pack(">I", len(payload)) + payload


def _read_exact(stream: Any, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(bytes(chunk))
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream: Any) -> dict[str, Any] | None:
    prefix = _read_exact(stream, 4)
    if not prefix:
        return None
    if len(prefix) != 4:
        raise E2EFailure("sidecar returned a truncated frame prefix")
    (length,) = struct.unpack(">I", prefix)
    if length > MAX_FRAME_BYTES:
        raise E2EFailure("sidecar returned an oversized frame")
    payload = _read_exact(stream, length)
    if len(payload) != length:
        raise E2EFailure("sidecar returned a truncated frame payload")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("sidecar returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise E2EFailure("sidecar returned a non-object response")
    return value


class SidecarClient:
    """Small framed-RPC client with concurrent response support for cancel."""

    def __init__(self, python_path: Path, *, env: Mapping[str, str], sidecar_root: Path) -> None:
        self._python_path = python_path
        self._env = dict(env)
        self._sidecar_root = sidecar_root
        self._process: subprocess.Popen[bytes] | None = None
        self._write_lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue()
        self._pending: dict[str, dict[str, Any]] = {}
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()

    @property
    def stderr_text(self) -> str:
        with self._stderr_lock:
            return bytes(self._stderr).decode("utf-8", errors="replace")

    def start(self) -> None:
        if self._process is not None:
            raise E2EFailure("sidecar client was started twice")
        process_env = dict(self._env)
        # The package is intentionally loaded from the repository's sidecar
        # source directory; no editable install or network access is required.
        existing_pythonpath = process_env.get("PYTHONPATH", "")
        process_env["PYTHONPATH"] = (
            str(self._sidecar_root)
            if not existing_pythonpath
            else str(self._sidecar_root) + os.pathsep + existing_pythonpath
        )
        try:
            self._process = subprocess.Popen(
                [str(self._python_path), "-m", "sidecar"],
                cwd=str(self._sidecar_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                bufsize=0,
            )
        except OSError as exc:
            raise E2EFailure("could not start the Python Sidecar") from exc
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

    def _read_loop(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            while True:
                response = _read_frame(self._process.stdout)
                if response is None:
                    break
                self._responses.put(response)
        except BaseException as exc:  # surfaced by request(), never logged raw
            self._responses.put(exc)
        finally:
            self._responses.put(None)

    def _read_stderr(self) -> None:
        assert self._process is not None
        assert self._process.stderr is not None
        try:
            while True:
                chunk = self._process.stderr.read(4096)
                if not chunk:
                    break
                with self._stderr_lock:
                    self._stderr.extend(chunk)
        except OSError:
            return

    def send(self, request: Mapping[str, Any]) -> str:
        process = self._process
        if process is None or process.stdin is None:
            raise E2EFailure("sidecar is not running")
        request_id = request.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise E2EFailure("sidecar request id is invalid")
        try:
            with self._write_lock:
                process.stdin.write(_frame(request))
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise E2EFailure("sidecar stdin closed unexpectedly") from exc
        return request_id

    def recv(self, request_id: str, *, timeout: float = 35.0) -> dict[str, Any]:
        cached = self._pending.pop(request_id, None)
        if cached is not None:
            return cached
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise E2EFailure(f"sidecar response timed out for request {request_id}")
            try:
                item = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise E2EFailure(f"sidecar response timed out for request {request_id}") from exc
            if item is None:
                raise E2EFailure("sidecar exited before returning a response")
            if isinstance(item, BaseException):
                raise E2EFailure("sidecar response reader failed") from item
            item_id = item.get("id")
            if item_id == request_id:
                return item
            if isinstance(item_id, str) and item_id:
                self._pending[item_id] = item

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float = 35.0,
    ) -> dict[str, Any]:
        request_id = "e2e-" + uuid.uuid4().hex
        self.send(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        return self.recv(request_id, timeout=timeout)

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self._stderr_reader is not None:
            self._stderr_reader.join(timeout=2)
        self._process = None


@dataclass
class ProvisionedTarget:
    database: str
    role: str
    password: str
    dsn: str
    admin_connection: Any
    cleanup_required: bool = True


def _load_psycopg() -> Any:
    try:
        import psycopg  # type: ignore[import-not-found]
        from psycopg import sql  # type: ignore[import-not-found]
    except ImportError as exc:
        raise E2EFailure(
            "psycopg is unavailable; run this gate with the project's Python environment"
        ) from exc
    return psycopg, sql


def _admin_connection(args: argparse.Namespace) -> Any:
    psycopg, _sql = _load_psycopg()
    dsn_env = args.admin_dsn_env
    if dsn_env:
        _safe_env_name(dsn_env, "--admin-dsn-env")
        dsn = os.environ.get(dsn_env)
        if not dsn:
            raise E2EFailure(f"required admin DSN environment variable is missing: {dsn_env}")
        try:
            return psycopg.connect(dsn, autocommit=True)
        except Exception as exc:
            raise E2EFailure("could not connect with the configured admin DSN") from exc

    kwargs: dict[str, Any] = {"autocommit": True}
    for option, key in (
        ("admin_host", "host"),
        ("admin_port", "port"),
        ("admin_user", "user"),
        ("admin_database", "dbname"),
        ("admin_sslmode", "sslmode"),
    ):
        value = getattr(args, option, None)
        if value:
            kwargs[key] = value
    if args.admin_password_env:
        password_env = _safe_env_name(args.admin_password_env, "--admin-password-env")
        password = os.environ.get(password_env)
        if not password:
            raise E2EFailure(f"required admin password environment variable is missing: {password_env}")
        kwargs["password"] = password
    # If no explicit option is supplied psycopg/libpq reads PGHOST, PGPORT,
    # PGUSER, PGDATABASE, PGPASSWORD, etc. directly from the current process.
    try:
        return psycopg.connect(**kwargs)
    except Exception as exc:
        raise E2EFailure(
            "could not connect to PostgreSQL administrator; configure libpq PG* variables or --admin-dsn-env"
        ) from exc


def _admin_dsn_for_database(args: argparse.Namespace, database: str) -> str:
    """Build a local-only admin connection string for the fixture database."""

    psycopg, _sql = _load_psycopg()
    if args.admin_dsn_env:
        env_name = _safe_env_name(args.admin_dsn_env, "--admin-dsn-env")
        raw = os.environ.get(env_name)
        if not raw:
            raise E2EFailure(f"required admin DSN environment variable is missing: {env_name}")
        try:
            values = dict(psycopg.conninfo.conninfo_to_dict(raw))
        except Exception as exc:
            raise E2EFailure("configured admin DSN could not be parsed") from exc
    else:
        values = {}
        for env_key, conn_key in (
            ("PGHOST", "host"),
            ("PGPORT", "port"),
            ("PGUSER", "user"),
            ("PGPASSWORD", "password"),
            ("PGSSLMODE", "sslmode"),
            ("PGSSLROOTCERT", "sslrootcert"),
            ("PGSSLCERT", "sslcert"),
            ("PGSSLKEY", "sslkey"),
            ("PGGSSENCMODE", "gssencmode"),
            ("PGCHANNELBINDING", "channel_binding"),
            ("PGSERVICE", "service"),
        ):
            if os.environ.get(env_key):
                values[conn_key] = os.environ[env_key]
        explicit = {
            "host": args.admin_host,
            "port": args.admin_port,
            "user": args.admin_user,
            "dbname": args.admin_database,
            "sslmode": args.admin_sslmode,
        }
        values.update({key: value for key, value in explicit.items() if value})
        if args.admin_password_env:
            password_env = _safe_env_name(args.admin_password_env, "--admin-password-env")
            password = os.environ.get(password_env)
            if not password:
                raise E2EFailure(f"required admin password environment variable is missing: {password_env}")
            values["password"] = password
    values["dbname"] = database
    try:
        return psycopg.conninfo.make_conninfo(**values)
    except Exception as exc:
        raise E2EFailure("could not construct the administrator fixture connection") from exc


def _dsn_for_role(admin_connection: Any, *, database: str, role: str, password: str) -> str:
    psycopg, _sql = _load_psycopg()
    info = getattr(admin_connection, "info", None)
    source: Mapping[str, Any] = {}
    if info is not None:
        raw = getattr(info, "dsn_parameters", None)
        if raw is None:
            get_parameters = getattr(info, "get_parameters", None)
            if callable(get_parameters):
                raw = get_parameters()
        if isinstance(raw, Mapping):
            source = raw
    allowed = {
        "host",
        "port",
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "sslpassword",
        "gssencmode",
        "channel_binding",
        "connect_timeout",
        "application_name",
    }
    kwargs: dict[str, Any] = {
        key: value
        for key, value in source.items()
        if key in allowed and isinstance(value, str) and value
    }
    # conn.info.dsn_parameters may omit a socket host or options; libpq env
    # fallbacks are copied only when they are explicitly present in the
    # current environment.  None of this is written to disk or displayed.
    for env_key, conn_key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGSSLMODE", "sslmode"),
        ("PGSSLROOTCERT", "sslrootcert"),
        ("PGSSLCERT", "sslcert"),
        ("PGSSLKEY", "sslkey"),
        ("PGGSSENCMODE", "gssencmode"),
        ("PGCHANNELBINDING", "channel_binding"),
    ):
        if conn_key not in kwargs and os.environ.get(env_key):
            kwargs[conn_key] = os.environ[env_key]
    kwargs.update({"dbname": database, "user": role, "password": password})
    try:
        return psycopg.conninfo.make_conninfo(**kwargs)
    except Exception as exc:
        raise E2EFailure("could not construct the temporary read-only connection") from exc


def _provision_target(args: argparse.Namespace) -> ProvisionedTarget:
    psycopg, sql = _load_psycopg()
    admin = _admin_connection(args)
    suffix = secrets.token_hex(8)
    database = _safe_identifier(f"dsh_e2e_{suffix}", "generated database")
    role = _safe_identifier(f"dsh_ro_{suffix}", "generated role")
    password = secrets.token_urlsafe(32)
    project_dir = Path(args.project_dir).resolve()
    seed_path = project_dir / "seed.sql"
    phase = "create_database"
    try:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
        )
        phase = "connect_fixture_database"
        target = psycopg.connect(_admin_dsn_for_database(args, database), autocommit=True)
        # The database owner/administrator connection above is only used for
        # loading.  Its password (if any) remains in process memory and is not
        # propagated to the Sidecar.
        try:
            if not seed_path.is_file():
                raise E2EFailure(f"seed file is missing: {seed_path}")
            seed_sql = seed_path.read_text(encoding="utf-8")
            phase = "load_seed"
            target.execute(seed_sql)
            grant_statements = [
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                ),
                sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(sql.Identifier(role)),
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)),
                sql.SQL("GRANT SELECT ON {}, {}, {}, {}, {} TO {}").format(
                    *(sql.Identifier(table) for table in EXPECTED_TABLES), sql.Identifier(role)
                ),
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                ),
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(role)),
                sql.SQL("ALTER ROLE {} SET statement_timeout = '30s'").format(sql.Identifier(role)),
            ]
            for index, statement in enumerate(grant_statements):
                phase = f"configure_readonly_role_{index + 1}"
                target.execute(statement)
        finally:
            target.close()
        dsn = _dsn_for_role(admin, database=database, role=role, password=password)
        return ProvisionedTarget(database, role, password, dsn, admin)
    except E2EFailure:
        # The caller has no target object to clean until provisioning returns;
        # clean the generated names here on every partial-provision failure.
        try:
            _drop_generated_objects(admin, database, role)
        except E2EFailure:
            pass
        raise
    except Exception as exc:
        # Do not expose psycopg's exception because it may contain a DSN.
        try:
            _drop_generated_objects(admin, database, role)
        except E2EFailure:
            pass
        sqlstate = getattr(exc, "sqlstate", None)
        state_suffix = f" (SQLSTATE {sqlstate})" if isinstance(sqlstate, str) else ""
        raise E2EFailure(
            f"PostgreSQL fixture provisioning failed during {phase}{state_suffix}"
        ) from exc


def _drop_generated_objects(admin: Any, database: str, role: str) -> None:
    """Best-effort cleanup for a fixture that failed before return."""

    _psycopg, sql = _load_psycopg()
    try:
        try:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
        except Exception:
            pass
        try:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
        except Exception:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    except Exception as exc:
        raise E2EFailure("partial PostgreSQL fixture cleanup failed") from exc


def _drop_database_and_role(target: ProvisionedTarget) -> None:
    """Drop only the generated database/role, best effort and secret-free."""

    admin = target.admin_connection
    _psycopg, sql = _load_psycopg()
    try:
        try:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (target.database,),
            )
        except Exception:
            # DROP ... WITH (FORCE) below is the authoritative cleanup path.
            pass
        _drop_generated_objects(admin, target.database, target.role)
    except Exception as exc:
        raise E2EFailure("temporary PostgreSQL cleanup failed") from exc
    finally:
        try:
            admin.close()
        except Exception:
            pass


def _validate_fixture(project_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not project_dir.is_dir():
        raise E2EFailure(f"project directory does not exist: {project_dir}")
    for name in ("wren_project.yml", "seed.sql", "golden-questions.json", "smoke-cases.json"):
        if not (project_dir / name).is_file():
            raise E2EFailure(f"fixture file is missing: {project_dir / name}")
    try:
        golden = json.loads((project_dir / "golden-questions.json").read_text(encoding="utf-8"))
        smoke = json.loads((project_dir / "smoke-cases.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("fixture JSON could not be loaded") from exc
    questions = golden.get("questions")
    if not isinstance(questions, list) or len(questions) != 20:
        raise E2EFailure("golden-questions.json must contain exactly 20 questions")
    cases = smoke.get("cases")
    if not isinstance(cases, list) or len(cases) < 4:
        raise E2EFailure("smoke-cases.json must contain at least four cases")
    features = {feature for case in cases for feature in (case.get("features") or [])}
    if not EXPECTED_FEATURES.issubset(features):
        missing = ", ".join(sorted(EXPECTED_FEATURES - features))
        raise E2EFailure(f"smoke corpus is missing required feature coverage: {missing}")
    return golden, smoke


def _resolve_python(args: argparse.Namespace, repo_dir: Path) -> Path:
    candidate = args.python or os.environ.get("WREN_PYTHON")
    if candidate:
        path = Path(candidate).expanduser().resolve()
    else:
        names = (
            repo_dir / ".venv" / "Scripts" / "python.exe",
            repo_dir / ".venv" / "bin" / "python",
            Path(sys.executable),
        )
        path = next((item.resolve() for item in names if item.is_file()), names[-1].resolve())
    if not path.is_file():
        raise E2EFailure(f"Python executable does not exist: {path}")
    return path


def _request_id() -> str:
    return "e2e-" + uuid.uuid4().hex


def _assert_ok(response: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if response.get("protocolVersion") != PROTOCOL_VERSION:
        raise E2EFailure(f"{label} returned an unexpected protocol version")
    if response.get("ok") is not True:
        error = response.get("error")
        code = error.get("code") if isinstance(error, Mapping) else "unknown"
        raise E2EFailure(f"{label} failed with sidecar error {code}")
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise E2EFailure(f"{label} returned no result object")
    return result


def _assert_rejected(response: Mapping[str, Any], label: str, allowed_codes: set[str] | None = None) -> None:
    if response.get("ok") is True:
        raise E2EFailure(f"{label} was unexpectedly accepted")
    error = response.get("error")
    if not isinstance(error, Mapping) or not isinstance(error.get("code"), str):
        raise E2EFailure(f"{label} returned an invalid error shape")
    if allowed_codes is not None and error["code"] not in allowed_codes:
        raise E2EFailure(f"{label} returned an unexpected rejection code")


def _run_smoke_cases(client: SidecarClient, project_dir: Path, smoke: Mapping[str, Any]) -> None:
    cases = smoke.get("cases")
    assert isinstance(cases, list)
    for case in cases:
        if not isinstance(case, Mapping):
            raise E2EFailure("smoke corpus contains a malformed case")
        case_id = case.get("id")
        question = case.get("question")
        semantic_sql = case.get("semanticSql")
        expected_columns = case.get("expectedColumns")
        if not all(isinstance(value, str) and value for value in (case_id, question, semantic_sql)):
            raise E2EFailure("smoke corpus contains a malformed query case")
        if not isinstance(expected_columns, list):
            raise E2EFailure(f"smoke case {case_id} has no expectedColumns")
        response = client.request(
            "query.run",
            {
                "projectDir": str(project_dir),
                "question": question,
                "semanticSql": semantic_sql,
                "queryId": _request_id(),
                "chartIntent": case.get("expectedChart") or "auto",
            },
            timeout=35,
        )
        result = _assert_ok(response, f"smoke case {case_id}")
        if result.get("schemaVersion") != 1 or result.get("status") != "success":
            raise E2EFailure(f"smoke case {case_id} returned an invalid presentation")
        for field in ("semanticSql", "nativeSql", "columns", "previewRows", "stats", "queryId"):
            if field not in result:
                raise E2EFailure(f"smoke case {case_id} omitted result metadata {field}")
        actual_columns = [
            column.get("name")
            for column in result.get("columns", [])
            if isinstance(column, Mapping)
        ]
        if actual_columns != expected_columns:
            raise E2EFailure(f"smoke case {case_id} returned unexpected columns")
        stats = result.get("stats")
        if (
            not isinstance(stats, Mapping)
            or not isinstance(stats.get("returnedRows"), int)
            or not isinstance(stats.get("durationMs"), (int, float))
            or not isinstance(stats.get("truncated"), bool)
        ):
            raise E2EFailure(f"smoke case {case_id} returned invalid row statistics")
        expected_chart = case.get("expectedChart")
        if expected_chart:
            chart = result.get("chart")
            if not isinstance(chart, Mapping) or chart.get("type") != expected_chart:
                raise E2EFailure(f"smoke case {case_id} returned the wrong chart type")


def _run_bounds_and_policy(client: SidecarClient, project_dir: Path) -> None:
    bounded = client.request(
        "query.run",
        {
            "projectDir": str(project_dir),
            "question": "列出订单编号",
            "semanticSql": "SELECT orders.id FROM orders ORDER BY orders.id",
            "queryId": _request_id(),
            "maxRows": 2,
            "previewRows": 1,
        },
    )
    result = _assert_ok(bounded, "row-bound query")
    stats = result.get("stats")
    if not isinstance(stats, Mapping) or stats.get("returnedRows") != 2 or stats.get("truncated") is not True:
        raise E2EFailure("row-bound query did not report maxRows truncation")
    if len(result.get("previewRows", [])) > 1:
        raise E2EFailure("row-bound query exceeded previewRows")

    policy_cases = (
        ("DML", "INSERT INTO orders (id, customer_id, ordered_at, status) VALUES (9001, 1, CURRENT_TIMESTAMP, 'paid')"),
        ("multi-statement", "SELECT orders.id FROM orders; SELECT orders.id FROM orders"),
        ("dangerous function", "SELECT pg_sleep(1) AS waited"),
        ("unauthorized object", "SELECT private_audit.id FROM private_audit"),
    )
    for label, semantic_sql in policy_cases:
        response = client.request(
            "query.run",
            {
                "projectDir": str(project_dir),
                "question": label,
                "semanticSql": semantic_sql,
                "queryId": _request_id(),
            },
        )
        _assert_rejected(
            response,
            label,
            {"POLICY_DENIED", "SEMANTIC_ERROR", "INVALID_PARAMS"},
        )


def _mcp_result_payload(result: Any, label: str) -> Mapping[str, Any]:
    if getattr(result, "isError", False):
        raise E2EFailure(f"{label} returned an MCP tool error")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, Mapping):
        return structured
    for content in getattr(result, "content", []):
        text = getattr(content, "text", None)
        if isinstance(text, str):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping):
                return payload
    raise E2EFailure(f"{label} returned no structured MCP result")


async def _run_governed_mcp_smoke_async(
    python_path: Path,
    project_dir: Path,
    process_env: Mapping[str, str],
    sidecar_root: Path,
) -> None:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise E2EFailure(
            "MCP client is unavailable; install python/sidecar with the mcp extra"
        ) from exc

    child_env = dict(process_env)
    existing_pythonpath = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = (
        str(sidecar_root)
        if not existing_pythonpath
        else str(sidecar_root) + os.pathsep + existing_pythonpath
    )
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    params = StdioServerParameters(
        command=str(python_path),
        args=[
            "-m",
            "sidecar.mcp_gateway",
            "--project",
            str(project_dir),
            "--database-dsn-env",
            "WREN_DATABASE_URL",
        ],
        cwd=str(sidecar_root),
        env=child_env,
    )

    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            if initialized.serverInfo.name != "dsh-governed-query":
                raise E2EFailure("governed MCP returned an unexpected server identity")
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if names != {"dsh_governed_query"}:
                raise E2EFailure("governed MCP exposed an unexpected tool surface")

            successful = await session.call_tool(
                "dsh_governed_query",
                {
                    "question": "List order identifiers",
                    "semantic_sql": "SELECT orders.id FROM orders ORDER BY orders.id",
                    "chart_intent": "table",
                    "max_rows": 2,
                    "preview_rows": 1,
                },
            )
            payload = _mcp_result_payload(successful, "governed MCP query")
            stats = payload.get("stats")
            if (
                payload.get("schemaVersion") != 1
                or payload.get("status") != "success"
                or not isinstance(stats, Mapping)
                or stats.get("returnedRows") != 2
                or stats.get("truncated") is not True
                or len(payload.get("previewRows", [])) > 1
            ):
                raise E2EFailure("governed MCP did not preserve DSH result bounds")

            denied = await session.call_tool(
                "dsh_governed_query",
                {
                    "question": "policy probe",
                    "semantic_sql": "SELECT pg_sleep(1) AS waited",
                },
            )
            if not getattr(denied, "isError", False):
                raise E2EFailure("governed MCP unexpectedly accepted dangerous SQL")
            diagnostic = " ".join(
                str(getattr(content, "text", ""))
                for content in getattr(denied, "content", [])
            )
            if "POLICY_DENIED" not in diagnostic:
                raise E2EFailure("governed MCP returned an unexpected policy error")
            dsn = child_env.get("WREN_DATABASE_URL", "")
            if dsn and dsn in diagnostic:
                raise E2EFailure("governed MCP diagnostics leaked the database DSN")


def _run_governed_mcp_smoke(
    python_path: Path,
    project_dir: Path,
    process_env: Mapping[str, str],
    sidecar_root: Path,
) -> None:
    asyncio.run(
        _run_governed_mcp_smoke_async(
            python_path,
            project_dir,
            process_env,
            sidecar_root,
        )
    )


def _long_running_sql() -> str:
    aliases = [f"orders AS o{index}" for index in range(1, 13)]
    return "SELECT COUNT(*) AS n FROM " + " CROSS JOIN ".join(aliases)


def _run_timeout_and_cancel(client: SidecarClient, project_dir: Path) -> None:
    long_sql = _long_running_sql()
    timeout = client.request(
        "query.run",
        {
            "projectDir": str(project_dir),
            "question": "timeout probe",
            "semanticSql": long_sql,
            "queryId": _request_id(),
            "timeoutMs": 250,
        },
        timeout=15,
    )
    _assert_rejected(timeout, "timeout probe", {"TIMEOUT", "CANCELLED"})

    query_id = _request_id()
    run_id = _request_id()
    client.send(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "id": run_id,
            "method": "query.run",
            "params": {
                "projectDir": str(project_dir),
                "question": "cancel probe",
                "semanticSql": long_sql,
                "queryId": query_id,
                "timeoutMs": 10_000,
            },
        }
    )
    # Wren is warmed by the smoke corpus.  Give the worker a brief window to
    # reach PostgreSQL, then deliver query.cancel over the same framed pipe.
    time.sleep(0.15)
    cancel_id = _request_id()
    client.send(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "id": cancel_id,
            "method": "query.cancel",
            "params": {"queryId": query_id},
        }
    )
    cancel = client.recv(cancel_id, timeout=10)
    cancel_result = _assert_ok(cancel, "cancel probe request")
    if cancel_result.get("cancelled") is not True:
        raise E2EFailure("query.cancel did not report an active query")
    run = client.recv(run_id, timeout=15)
    _assert_rejected(run, "cancelled query", {"CANCELLED", "TIMEOUT"})


def _run_read_only_probe(dsn: str) -> None:
    psycopg, _sql = _load_psycopg()
    try:
        connection = psycopg.connect(dsn, autocommit=False)
    except Exception as exc:
        raise E2EFailure("could not connect as the read-only PostgreSQL role") from exc
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, current_setting('transaction_read_only')")
            row = cursor.fetchone()
            if not row or row[1] != "on":
                raise E2EFailure("temporary role is not configured read-only")
            try:
                cursor.execute("UPDATE public.orders SET status = status WHERE false")
            except Exception:
                connection.rollback()
            else:
                connection.rollback()
                raise E2EFailure("read-only role unexpectedly accepted a write")
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _dry_run(args: argparse.Namespace, project_dir: Path, python_path: Path) -> int:
    _validate_fixture(project_dir)
    if args.mode == "existing":
        dsn_env = _safe_env_name(args.database_dsn_env, "--database-dsn-env")
        dsn_source = f"environment:{dsn_env}"
    else:
        dsn_source = (
            f"environment:{_safe_env_name(args.admin_dsn_env, '--admin-dsn-env')}"
            if args.admin_dsn_env
            else "libpq PG* environment / service configuration"
        )
    report = {
        "status": "dry-run-pass",
        "mode": args.mode,
        "projectDir": str(project_dir),
        "python": str(python_path),
        "dsnSource": dsn_source,
        "sidecar": "python -m sidecar",
        "databaseProvisioning": args.mode == "provision",
        "passwordHandling": "environment-only/in-memory",
        "networkProvisioning": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("POSTGRES_E2E_DRY_RUN_PASS")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("provision", "existing"), default="provision")
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[1] / "examples" / "wren-postgres"))
    parser.add_argument("--python", help="Python executable containing Wren 0.13.2 and psycopg")
    parser.add_argument("--admin-dsn-env", default="", help="environment name containing an admin DSN")
    parser.add_argument("--admin-password-env", default="", help="environment name containing an admin password")
    parser.add_argument("--admin-host", default="")
    parser.add_argument("--admin-port", default="")
    parser.add_argument("--admin-user", default="")
    parser.add_argument("--admin-database", default="")
    parser.add_argument("--admin-sslmode", default="")
    parser.add_argument("--database-dsn-env", default="WREN_DATABASE_URL")
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    repo_dir = Path(__file__).resolve().parents[1]
    project_dir = Path(args.project_dir).expanduser().resolve()
    python_path = _resolve_python(args, repo_dir)
    if args.admin_dsn_env:
        args.admin_dsn_env = _safe_env_name(args.admin_dsn_env, "--admin-dsn-env")
    if args.admin_password_env:
        args.admin_password_env = _safe_env_name(args.admin_password_env, "--admin-password-env")
    if args.mode == "existing":
        args.database_dsn_env = _safe_env_name(args.database_dsn_env, "--database-dsn-env")
        if not os.environ.get(args.database_dsn_env) and not args.dry_run:
            raise E2EFailure(f"required database DSN environment variable is missing: {args.database_dsn_env}")
    if args.dry_run:
        return _dry_run(args, project_dir, python_path)
    _validate_fixture(project_dir)
    run_dir = Path(tempfile.mkdtemp(prefix="dsh-wren-postgres-e2e-"))
    target: ProvisionedTarget | None = None
    client: SidecarClient | None = None
    succeeded = False
    try:
        if args.mode == "provision":
            target = _provision_target(args)
            dsn = target.dsn
        else:
            dsn = os.environ[args.database_dsn_env]
        if not dsn:
            raise E2EFailure("database DSN is empty")
        _run_read_only_probe(dsn)
        process_env = dict(os.environ)
        # The only credential crossing into the child environment is the
        # process-local DSN.  It is never sent in an RPC params object.
        # The sidecar defaults to WREN_DATABASE_URL.  Keep the caller's
        # source-name semantics for existing mode, but normalize the child
        # process to this one well-known, process-local key so every RPC test
        # uses the same contract.
        process_env["WREN_DATABASE_URL"] = dsn
        client = SidecarClient(
            python_path,
            env=process_env,
            sidecar_root=repo_dir / "python" / "sidecar",
        )
        client.start()
        health = client.request("health", {}, timeout=15)
        health_result = _assert_ok(health, "health")
        if health_result.get("wrenAvailable") is not True:
            raise E2EFailure("Wren 0.13.2 is not available in the selected Python environment")
        validation = client.request("project.validate", {"projectDir": str(project_dir)}, timeout=35)
        validation_result = _assert_ok(validation, "project.validate")
        if validation_result.get("valid") is not True or validation_result.get("errorCount") != 0:
            raise E2EFailure("Wren example project validation failed")
        _run_smoke_cases(client, project_dir, json.loads((project_dir / "smoke-cases.json").read_text(encoding="utf-8")))
        _run_bounds_and_policy(client, project_dir)
        _run_governed_mcp_smoke(
            python_path,
            project_dir,
            process_env,
            repo_dir / "python" / "sidecar",
        )
        _run_timeout_and_cancel(client, project_dir)
        stderr = client.stderr_text
        if target is not None and target.password in stderr:
            raise E2EFailure("sidecar diagnostics leaked the temporary role password")
        if target is not None and target.dsn in stderr:
            raise E2EFailure("sidecar diagnostics leaked the temporary DSN")
        succeeded = True
        print("POSTGRES_E2E_PASS")
        print(f"  mode={args.mode}")
        print("  health/project validation/smoke result metadata: passed")
        print("  framed sidecar and governed MCP query paths: passed")
        print("  row bounds/policy/read-only account/timeout/cancel: passed")
        return 0
    except E2EFailure as exc:
        print(f"POSTGRES_E2E_FAIL: {exc}", file=sys.stderr)
        print(f"POSTGRES_E2E_DIAGNOSTICS={run_dir}", file=sys.stderr)
        return 1
    except Exception:
        print("POSTGRES_E2E_FAIL: unexpected acceptance failure", file=sys.stderr)
        print(f"POSTGRES_E2E_DIAGNOSTICS={run_dir}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            try:
                client.stop()
                (run_dir / "sidecar.stderr.log").write_text(client.stderr_text, encoding="utf-8")
            except Exception:
                pass
        if target is not None and target.cleanup_required:
            try:
                _drop_database_and_role(target)
            except E2EFailure as exc:
                print(f"POSTGRES_E2E_CLEANUP_WARNING: {exc}", file=sys.stderr)
                succeeded = False
        if succeeded and not args.keep_artifacts:
            shutil.rmtree(run_dir, ignore_errors=True)
        elif run_dir.exists():
            print(f"POSTGRES_E2E_ARTIFACTS={run_dir}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except E2EFailure as exc:
        print(f"POSTGRES_E2E_FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
