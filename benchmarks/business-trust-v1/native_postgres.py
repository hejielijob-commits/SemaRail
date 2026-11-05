#!/usr/bin/env python3
"""Owned native PostgreSQL 17 runtime for business-trust-v1 acceptance.

Run with the repository .venv Python (psycopg required). `init` only initializes
an owned cluster; `serve` stays foreground and loads verified data once. No
command accesses the machine's existing port 5432 or removes cluster data.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CLUSTER = REPO / ".benchmark-data" / "business-trust-v1-postgres"
GENERATED = Path(os.environ.get("BUSINESS_TRUST_GENERATED", str(REPO.parent / "dsh-wren-data-agent" / ".benchmark-data" / "hr-enterprise" / "generated"))).resolve()
PORT = 55433
OWNER = "business_trust_owner"
DATABASE = "hr_enterprise"
TABLES = ("regions", "departments", "employees", "compensation_history", "performance_reviews", "attendance_monthly")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

class NativeError(RuntimeError):
    """Safe diagnostic without credentials or backend query values."""


def pg_bin() -> Path:
    path = Path(os.environ.get("BUSINESS_TRUST_PG_BIN", "C:/Program Files/PostgreSQL/17/bin"))
    if not (path / "postgres.exe").is_file():
        raise NativeError("PostgreSQL binaries missing; set BUSINESS_TRUST_PG_BIN")
    version = subprocess.run([str(path / "postgres.exe"), "--version"], capture_output=True, text=True, creationflags=NO_WINDOW)
    if version.returncode or " 17." not in version.stdout:
        raise NativeError("Native fallback requires PostgreSQL 17")
    return path


def expected_identity() -> dict:
    return {"schemaVersion": 1, "owner": "semarail-business-trust-v1", "cluster": str(CLUSTER.resolve()), "port": PORT}


def owned() -> None:
    marker = CLUSTER / "ownership.json"
    if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != expected_identity():
        raise NativeError("Cluster ownership sentinel missing or does not match; refusing access")


def occupied() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def verify_data() -> dict:
    source = REPO / "benchmarks/hr-enterprise/prepare.py"
    spec = importlib.util.spec_from_file_location("hr_ab_prepare", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.verify(GENERATED)
    manifest = json.loads((GENERATED / "manifest.json").read_text(encoding="utf-8"))
    facts = sum(manifest["outputs"][name]["rows"] for name in TABLES[2:])
    if facts != 1_900_000:
        raise NativeError("Expected 1,900,000 employee and fact rows")
    return manifest


def initialize() -> dict:
    binaries = pg_bin()
    verify_data()
    if CLUSTER.exists():
        owned()
        if not (CLUSTER / "data/PG_VERSION").is_file():
            raise NativeError("Owned but incomplete cluster; no automatic destructive recovery")
        return {"status": "already_initialized", "port": PORT}
    if occupied():
        raise NativeError("Port 55433 is occupied; refusing to initialize against an unknown service")
    CLUSTER.mkdir(parents=True)
    (CLUSTER / "ownership.json").write_text(json.dumps(expected_identity()), encoding="utf-8")
    password = secrets.token_urlsafe(40)
    secret_file = CLUSTER / ".owner-password"
    secret_file.write_text(password + "\n", encoding="utf-8")
    (CLUSTER / ".reader-password").write_text(secrets.token_urlsafe(40) + "\n", encoding="utf-8")
    if os.name != "nt":
        secret_file.chmod(0o600)
    result = subprocess.run([str(binaries / "initdb.exe"), "-D", str(CLUSTER / "data"), "-U", OWNER,
                             "--pwfile", str(secret_file), "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C"],
                            capture_output=True, creationflags=NO_WINDOW)
    if result.returncode:
        raise NativeError("initdb failed; owned files retained for inspection")
    config = CLUSTER / "data/postgresql.conf"
    with config.open("a", encoding="utf-8") as f:
        f.write("\nlisten_addresses = '127.0.0.1'\nport = 55433\nlog_statement = 'none'\nlog_min_error_statement = 'panic'\n")
    return {"status": "initialized", "port": PORT}


def connect(database: str = "postgres"):
    import psycopg
    owned()
    return psycopg.connect(host="127.0.0.1", port=PORT, dbname=database, user=OWNER,
                           password=(CLUSTER / ".owner-password").read_text(encoding="utf-8").strip(), connect_timeout=3)


def check_server(connection) -> None:
    owned()
    pid = CLUSTER / "data/postmaster.pid"
    if not pid.is_file():
        raise NativeError("Owned postmaster PID file missing")
    lines = pid.read_text(encoding="utf-8").splitlines()
    if len(lines) < 4 or Path(lines[1]).resolve() != (CLUSTER / "data").resolve() or int(lines[3]) != PORT:
        raise NativeError("Owned postmaster directory or port mismatch")
    with connection.cursor() as cursor:
        cursor.execute("SHOW data_directory")
        if Path(cursor.fetchone()[0]).resolve() != (CLUSTER / "data").resolve():
            raise NativeError("Connected server does not own the benchmark directory")
        cursor.execute("SHOW server_version_num")
        if not 170000 <= int(cursor.fetchone()[0]) < 180000:
            raise NativeError("Connected server must be PostgreSQL 17")


def load_data() -> dict:
    from psycopg import sql
    manifest = verify_data()
    report_file = CLUSTER / "load-report.json"
    with connect() as connection:
        check_server(connection)
        connection.commit()
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,))
            if cursor.fetchone() is None:
                cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(DATABASE), sql.Identifier(OWNER)))
    with connect(DATABASE) as connection:
        check_server(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('hr.employees')")
            existing = cursor.fetchone()[0] is not None
            if existing:
                if not report_file.is_file():
                    raise NativeError("Existing HR data lacks a completed load report; refusing overwrite")
                report = json.loads(report_file.read_text(encoding="utf-8"))
                if report["dataManifestSha256"] != hashlib.sha256((GENERATED / "manifest.json").read_bytes()).hexdigest():
                    raise NativeError("Completed cluster data fingerprint differs")
                counts = {}
                for table in TABLES:
                    cursor.execute(sql.SQL("SELECT COUNT(*) FROM hr.{}").format(sql.Identifier(table)))
                    counts[table] = cursor.fetchone()[0]
                if counts != report["rowCounts"]:
                    raise NativeError("Loaded row counts changed since load")
                return {**report, "status": "already_loaded"}
            schema = (REPO / "benchmarks/hr-enterprise/sql/001_schema.sql").read_text(encoding="utf-8")
            schema = "\n".join(line for line in schema.splitlines() if not line.startswith("\\"))
            cursor.execute(schema)
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            counts = {}
            for table in TABLES:
                with cursor.copy(sql.SQL("COPY hr.{} FROM STDIN WITH (FORMAT csv, HEADER true)").format(sql.Identifier(table))) as copy:
                    with (GENERATED / f"{table}.csv").open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            copy.write(chunk)
                cursor.execute(sql.SQL("SELECT COUNT(*) FROM hr.{}").format(sql.Identifier(table)))
                counts[table] = cursor.fetchone()[0]
                if counts[table] != manifest["outputs"][table]["rows"]:
                    raise NativeError("Loaded row count mismatch")
            # Same isolated synthetic fixture identity expected by the existing bootstrap.
            cursor.execute(sql.SQL("CREATE ROLE hr_benchmark_reader LOGIN PASSWORD {}").format(sql.Literal((CLUSTER / ".reader-password").read_text(encoding="utf-8").strip())))
            cursor.execute("ALTER ROLE hr_benchmark_reader SET default_transaction_read_only = on")
            cursor.execute("GRANT CONNECT ON DATABASE hr_enterprise TO hr_benchmark_reader")
            cursor.execute("GRANT USAGE ON SCHEMA hr TO hr_benchmark_reader")
            cursor.execute("GRANT SELECT ON ALL TABLES IN SCHEMA hr TO hr_benchmark_reader")
            cursor.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA hr GRANT SELECT ON TABLES TO hr_benchmark_reader")
            for table in TABLES:
                cursor.execute(sql.SQL("ANALYZE hr.{}").format(sql.Identifier(table)))
    report = {"schemaVersion": 1, "status": "loaded", "serverMajor": 17, "port": PORT,
              "rowCounts": counts, "employeeAndFactRows": sum(counts[t] for t in TABLES[2:]),
              "dataManifestSha256": hashlib.sha256((GENERATED / "manifest.json").read_bytes()).hexdigest(),
              "outputs": manifest["outputs"]}
    report_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def status() -> dict:
    owned()
    if not occupied():
        return {"status": "stopped", "port": PORT}
    with connect() as connection:
        check_server(connection)
    return {"status": "running", "port": PORT, "loaded": (CLUSTER / "load-report.json").is_file()}


def stop() -> dict:
    owned()
    if not occupied():
        return {"status": "stopped", "port": PORT}
    with connect() as connection:
        check_server(connection)
    result = subprocess.run([str(pg_bin() / "pg_ctl.exe"), "-D", str(CLUSTER / "data"), "stop", "-m", "fast", "-w", "-t", "30"], capture_output=True, creationflags=NO_WINDOW)
    if result.returncode:
        raise NativeError("Owned cluster stop failed")
    return {"status": "stopped", "port": PORT}


def serve() -> None:
    owned()
    if occupied():
        raise NativeError("Port occupied; use status to verify existing owned instance")
    process = subprocess.Popen([str(pg_bin() / "postgres.exe"), "-D", str(CLUSTER / "data")],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=NO_WINDOW)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise NativeError("Owned PostgreSQL process exited before readiness")
            if occupied():
                try:
                    with connect() as connection:
                        check_server(connection)
                    break
                except Exception:
                    pass
            time.sleep(0.25)
        else:
            raise NativeError("Owned PostgreSQL readiness timed out")
        print(json.dumps(load_data()), flush=True)
        process.wait()
    finally:
        if process.poll() is None:
            try:
                stop()
            except Exception:
                # This Popen handle is exclusively the process we created.
                process.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "serve", "status", "stop"))
    args = parser.parse_args()
    try:
        if args.command == "serve":
            serve()
        else:
            print(json.dumps({"init": initialize, "status": status, "stop": stop}[args.command]()))
        return 0
    except KeyboardInterrupt:
        return 130
    except NativeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # Backend exceptions may embed connection/query secrets: show type only.
        print(f"Native fallback failed ({type(error).__name__}); credentials suppressed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
