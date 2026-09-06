#!/usr/bin/env python3
"""Run the local PostgreSQL HR enterprise benchmark without leaking data or keys."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DATA_ROOT = REPO_ROOT / ".benchmark-data" / "hr-enterprise"
CORPUS = HERE / "golden-questions.json"
ENDPOINT = "http://127.0.0.1:48773"
ADMIN_TOKEN = os.environ.get("HR_BENCHMARK_ADMIN_TOKEN") or secrets.token_urlsafe(36)
PROJECT = "semarail_hr_enterprise"
TABLES = (
    "hr.regions", "hr.departments", "hr.employees", "hr.compensation_history",
    "hr.performance_reviews", "hr.attendance_monthly",
)
TOOLS = (
    "runtime:health", "project:validate", "semantic:read", "query:plan",
    "query:execute", "query:cancel",
)
LIMITS = {"maxRows": 500, "previewRows": 50, "maxPreviewBytes": 131072, "timeoutMs": 10000}


class BenchmarkError(RuntimeError):
    pass


def _rule(*, field: str | None = None, operator: str = "eq", attribute: str | None = None,
          deny: bool = False, denied_columns: tuple[str, ...] = ()) -> dict[str, Any]:
    if deny:
        return {"effect": "deny"}
    result: dict[str, Any] = {"effect": "allow", "rows": [], "columns": {"deny": []}}
    if field and attribute:
        result["rows"] = [{"field": field, "operator": operator, "valueFrom": f"subject.attributes.{attribute}"}]
    if denied_columns:
        result["columns"] = {"deny": list(denied_columns)}
    return result


def policy_document(actor: str, datasource_id: str) -> dict[str, Any]:
    dimensions = {"hr.regions": _rule(), "hr.departments": _rule()}
    if actor == "employee":
        tables = {
            **dimensions,
            "hr.employees": _rule(field="employee_id", attribute="employeeId", denied_columns=("gender", "age")),
            "hr.compensation_history": _rule(field="employee_id", attribute="employeeId"),
            "hr.performance_reviews": _rule(deny=True),
            "hr.attendance_monthly": _rule(field="employee_id", attribute="employeeId"),
        }
    elif actor == "department_manager":
        tables = {
            **dimensions,
            "hr.employees": _rule(field="manager_id", attribute="employeeId", denied_columns=("gender", "age")),
            "hr.compensation_history": _rule(deny=True),
            "hr.performance_reviews": _rule(field="manager_id", attribute="employeeId"),
            "hr.attendance_monthly": _rule(field="manager_id", attribute="employeeId"),
        }
    elif actor == "hrbp":
        tables = {
            "hr.regions": _rule(field="region_code", operator="in", attribute="regionCodes"),
            "hr.departments": _rule(field="region_code", operator="in", attribute="regionCodes"),
            "hr.employees": _rule(field="region_code", operator="in", attribute="regionCodes"),
            "hr.compensation_history": _rule(deny=True),
            "hr.performance_reviews": _rule(field="region_code", operator="in", attribute="regionCodes"),
            "hr.attendance_monthly": _rule(field="region_code", operator="in", attribute="regionCodes"),
        }
    elif actor == "compensation_admin":
        tables = {
            "hr.regions": _rule(field="region_code", operator="in", attribute="regionCodes"),
            "hr.departments": _rule(field="region_code", operator="in", attribute="regionCodes"),
            "hr.employees": _rule(field="region_code", operator="in", attribute="regionCodes"),
            "hr.compensation_history": _rule(field="region_code", operator="in", attribute="regionCodes"),
            "hr.performance_reviews": _rule(deny=True),
            "hr.attendance_monthly": _rule(deny=True),
        }
    elif actor == "hr_director":
        tables = {table: _rule() for table in TABLES}
    else:
        raise BenchmarkError(f"unknown actor: {actor}")
    return {
        "schemaVersion": 1, "datasourceId": datasource_id, "projects": [PROJECT],
        "tools": list(TOOLS), "limits": dict(LIMITS), "tables": tables,
    }


def _request(path: str, *, token: str | None = None, method: str = "GET",
             body: Mapping[str, Any] | None = None, raw: bool = False,
             allow_http_error: bool = False) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(path if path.startswith("http") else ENDPOINT + path,
                                     data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if allow_http_error:
            try:
                return json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        safe = payload.decode("utf-8", "replace")[:500]
        raise BenchmarkError(f"HTTP {exc.code} for {request.full_url}: {safe}") from exc


def _admin(path: str, *, method: str = "GET", body: Mapping[str, Any] | None = None) -> Any:
    return _request(path, token=ADMIN_TOKEN, method=method, body=body)


def _rpc(token: str, request_id: str, semantic_sql: str, question: str) -> dict[str, Any]:
    started = time.monotonic()
    response = _request("/api/v1/runtime/rpc", token=token, method="POST", allow_http_error=True, body={
        "protocolVersion": "1", "id": request_id, "method": "query.run",
        "params": {"question": question, "semanticSql": semantic_sql, "queryId": request_id},
    })
    if not isinstance(response, dict):
        raise BenchmarkError("runtime returned a non-object response")
    if response.get("ok") is True:
        result = response.get("result")
        if not isinstance(result, dict):
            raise BenchmarkError("runtime success omitted result")
        return result
    error = response.get("error")
    return {
        "schemaVersion": 2,
        "status": "error",
        "error": error or {},
        # A top-level RPC error has no result payload. In particular, the
        # sidecar's POLICY_DENIED path returns before database execution.
        "stats": {"returnedRows": 0, "durationMs": (time.monotonic() - started) * 1000, "truncated": False},
    }


def _scalar_equal(actual: Any, expected: Any, tolerance: float) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual == expected
    try:
        return abs(float(actual) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return actual == expected


def _rows_equal(
    actual: list[Any],
    expected: list[Any],
    tolerance: float,
    ordered: bool,
    aliases: Mapping[str, Any] | None = None,
    allowed_extras: list[str] | None = None,
) -> bool:
    expected_keys = set(expected[0]) if expected else set()
    alias_map = aliases or {}
    allowed_extra_keys = set(allowed_extras or [])

    def normalize(row: Any) -> Mapping[str, Any] | None:
        if not isinstance(row, Mapping):
            return None
        normalized: dict[str, Any] = {}
        consumed: set[str] = set()
        for key in expected_keys:
            candidates = [key, *alias_map.get(key, [])]
            matches = [candidate for candidate in candidates if candidate in row]
            if len(matches) != 1:
                return None
            consumed.add(matches[0])
            normalized[key] = row[matches[0]]
        if set(row) - consumed - allowed_extra_keys:
            return None
        return normalized

    def row_equal(left: Any, right: Any) -> bool:
        normalized = normalize(left)
        return normalized is not None and isinstance(right, Mapping) and set(normalized) == set(right) and all(
            _scalar_equal(normalized[key], right[key], tolerance) for key in normalized
        )
    if len(actual) != len(expected):
        return False
    if ordered:
        return all(row_equal(left, right) for left, right in zip(actual, expected))
    remaining = list(actual)
    for expected_row in expected:
        match = next((index for index, row in enumerate(remaining) if row_equal(row, expected_row)), None)
        if match is None:
            return False
        remaining.pop(match)
    return True


def _percentile(values: list[float], proportion: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * proportion + 0.999999)))
    return ordered[index]


def _python() -> Path:
    configured = os.environ.get("WREN_PYTHON")
    candidates = [Path(configured)] if configured else []
    candidates += [REPO_ROOT / ".venv" / "Scripts" / "python.exe", REPO_ROOT / ".venv" / "bin" / "python"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return Path(sys.executable).resolve()


def _start_core() -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    sidecar = REPO_ROOT / "python" / "sidecar"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(sidecar) + (os.pathsep + existing if existing else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SEMARAIL_API_TOKEN"] = ADMIN_TOKEN
    process = subprocess.Popen([
        str(_python()), "-m", "server", "--host", "127.0.0.1", "--port", "48773",
        "--project-dir", str(HERE / "project"), "--state-dir", str(DATA_ROOT / "state"),
    ], cwd=REPO_ROOT / "apps" / "semantic-console", env=env,
       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BenchmarkError("Core exited during startup; run the semantic-console tests for diagnostics")
        try:
            if _request("/api/health").get("status") == "ok":
                return process
        except Exception:
            time.sleep(0.25)
    process.terminate()
    raise BenchmarkError("Core did not become healthy within 30 seconds")


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if not shutil.which("docker"):
        raise BenchmarkError("Docker is required for benchmark:hr but was not found on PATH")
    return subprocess.run(["docker", "compose", "-f", str(HERE / "compose.yml"), *args],
                          cwd=REPO_ROOT, check=check, text=True)


def _wait_for_postgres_final(timeout: float = 120.0) -> None:
    """Wait past the entrypoint's temporary init server for the final postmaster.

    The official Postgres image starts a temporary server while it runs init
    scripts. A plain pg_isready healthcheck can therefore become healthy while
    the large COPY transaction is still in progress. Requiring two consecutive
    reads of loaded data also survives the brief shutdown between that temporary
    server and the final server.
    """
    command = [
        "docker", "compose", "-f", str(HERE / "compose.yml"), "exec", "-T",
        "-e", "PGPASSWORD=hr_benchmark_readonly", "postgres", "psql",
        "-U", "hr_benchmark_reader", "-d", "hr_enterprise", "-Atqc",
        "SELECT 1 FROM hr.attendance_monthly LIMIT 1",
    ]
    deadline = time.monotonic() + timeout
    consecutive = 0
    while time.monotonic() < deadline:
        result = subprocess.run(
            command, cwd=REPO_ROOT, text=True, capture_output=True, check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            consecutive += 1
            if consecutive == 2:
                return
        else:
            consecutive = 0
        time.sleep(1)
    raise BenchmarkError("PostgreSQL did not reach its final loaded state within 120 seconds")


def _bootstrap(corpus: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    datasource = _admin("/api/datasources", method="POST", body={
        "name": "HR enterprise benchmark", "type": "postgres",
        "connection": {"host": "127.0.0.1", "port": 55432, "database": "hr_enterprise",
                       "user": "hr_benchmark_reader", "password": "hr_benchmark_readonly"},
    })
    _admin(f"/api/datasources/{datasource['id']}/activate", method="POST", body={})
    keys: dict[str, str] = {}
    resources: dict[str, dict[str, str]] = {}
    for actor, attributes in corpus["actors"].items():
        account = _admin("/api/v1/access/service-accounts", method="POST", body={
            "name": f"HR benchmark {actor}", "attributes": attributes,
        })
        policy = _admin("/api/v1/access/policies", method="POST", body={
            "name": f"HR benchmark {actor}", "document": policy_document(actor, datasource["id"]),
        })
        _admin("/api/v1/access/policy-bindings", method="POST", body={
            "subjectId": account["id"], "policyId": policy["id"],
        })
        issued = _admin(f"/api/v1/access/service-accounts/{account['id']}/keys", method="POST", body={"label": "benchmark"})
        keys[actor] = issued["apiKey"]
        resources[actor] = {"account": account["id"], "policy": policy["id"], "credential": issued["credential"]["id"]}
    return keys, resources


def _assert_scenario(question: Mapping[str, Any], result: Mapping[str, Any]) -> tuple[bool, str, float]:
    oracle = question["oracle"]
    raw_duration = result.get("stats", {}).get("durationMs")
    duration = float(raw_duration) if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool) else -1.0
    if oracle["type"] == "denial":
        error = result.get("error", {})
        ok = (
            result.get("status") == "error" and error.get("code") == oracle["errorCode"] and
            result.get("stats", {}).get("returnedRows") == oracle["databaseRowsReturned"] and
            0 <= duration <= 10000
        )
        return ok, "PASS" if ok else "POLICY_NOT_DENIED", duration
    if result.get("status") != "success":
        return False, "QUERY_ERROR", duration
    if result.get("delivery", "inline") != "inline" or result.get("stats", {}).get("truncated"):
        return False, "INCOMPLETE_RESULT", duration
    rows = result.get("previewRows")
    if not isinstance(rows, list):
        return False, "ROWS", duration
    ok = _rows_equal(
        rows,
        oracle["rows"],
        float(oracle["numericTolerance"]),
        bool(oracle.get("orderSensitive")),
        oracle.get("columnAliases"),
        oracle.get("allowedExtraColumns"),
    )
    if not ok:
        return False, "ORACLE_MISMATCH", duration
    if not 0 <= duration <= 10000:
        return False, "RUNTIME_LIMIT", duration
    return True, "PASS", duration


def _security_probes(keys: Mapping[str, str], resources: Mapping[str, Mapping[str, str]], corpus: Mapping[str, Any]) -> dict[str, bool]:
    probes: dict[str, bool] = {}
    counts: dict[str, Any] = {}
    sql = "SELECT COUNT(DISTINCT employee_id) AS headcount FROM employees WHERE resigned = FALSE"
    for actor in ("employee", "department_manager", "hrbp", "hr_director"):
        result = _rpc(keys[actor], f"boundary-{actor}", sql, "visible active employee count")
        rows = result.get("previewRows", [])
        counts[actor] = rows[0].get("headcount") if rows else None
    probes["role_boundaries_differ"] = len({str(value) for value in counts.values()}) == 4
    isolation_queries = {
        "employee": "SELECT COUNT(DISTINCT employee_id) AS leaked FROM employees WHERE employee_id <> 1",
        "department_manager": "SELECT COUNT(DISTINCT employee_id) AS leaked FROM employees WHERE manager_id <> 627",
        "hrbp": "SELECT COUNT(DISTINCT employee_id) AS leaked FROM employees WHERE region_code <> 'APAC'",
    }
    isolation_ok = True
    for actor, isolation_sql in isolation_queries.items():
        result = _rpc(keys[actor], f"isolation-{actor}", isolation_sql, "count rows outside my assigned scope")
        rows = result.get("previewRows", [])
        isolation_ok = isolation_ok and bool(rows) and str(rows[0].get("leaked")) == "0"
    probes["no_out_of_scope_rows"] = isolation_ok

    employee = resources["employee"]
    _admin(f"/api/v1/access/service-accounts/{employee['account']}", method="PUT", body={
        "name": "HR benchmark employee", "attributes": {},
    })
    missing = _rpc(keys["employee"], "missing-attributes", sql, "missing attributes must fail closed")
    probes["missing_attributes_denied"] = missing.get("status") == "error" and missing.get("error", {}).get("code") in {"FORBIDDEN", "POLICY_DENIED"}
    _admin(f"/api/v1/access/service-accounts/{employee['account']}", method="PUT", body={
        "name": "HR benchmark employee", "attributes": corpus["actors"]["employee"],
    })
    _admin(f"/api/v1/access/policy-bindings/{employee['account']}/{employee['policy']}", method="DELETE")
    unbound = _rpc(keys["employee"], "unbound-policy", sql, "unbound policy must fail closed")
    probes["unbound_policy_denied"] = unbound.get("status") == "error" and unbound.get("error", {}).get("code") in {"FORBIDDEN", "POLICY_DENIED"}
    _admin("/api/v1/access/policy-bindings", method="POST", body={"subjectId": employee["account"], "policyId": employee["policy"]})

    large = _rpc(keys["hr_director"], "artifact-result", "SELECT employee_id FROM employees ORDER BY employee_id", "all employee identifiers")
    artifact = large.get("artifact", {})
    probes["large_result_is_bounded_artifact"] = (
        large.get("status") == "success" and large.get("delivery") == "artifact" and
        large.get("stats", {}).get("returnedRows") == 500 and len(large.get("previewRows", [])) <= 20 and
        artifact.get("rowCount") == 500 and "chart" not in large
    )
    if not probes["large_result_is_bounded_artifact"]:
        probes["artifact_download_and_revocation"] = False
        return probes
    payload = _request(artifact["downloadUrl"], raw=True)
    _admin(f"/api/v1/access/credentials/{resources['hr_director']['credential']}/revoke", method="POST", body={})
    revoked = False
    try:
        _request(artifact["downloadUrl"], raw=True)
    except BenchmarkError as exc:
        revoked = "HTTP 410" in str(exc)
    probes["artifact_download_and_revocation"] = bool(payload) and revoked
    return probes


def run() -> int:
    manifest = DATA_ROOT / "generated" / "manifest.json"
    if not manifest.is_file():
        raise BenchmarkError("generated data is missing; run benchmark:hr:prepare first")
    subprocess.run([str(_python()), str(HERE / "prepare.py"), "verify"], cwd=REPO_ROOT, check=True)
    state_dir = DATA_ROOT / "state"
    if state_dir.is_dir():
        resolved_state = state_dir.resolve()
        if resolved_state.parent != DATA_ROOT.resolve() or resolved_state.name != "state":
            raise BenchmarkError("refusing to reset an unexpected Core state path")
        shutil.rmtree(resolved_state)
    # A benchmark run always owns a fresh dedicated database volume so schema,
    # COPY time, and policy results cannot be inherited from an earlier run.
    _docker("down", "-v", "--remove-orphans", check=False)
    started = time.monotonic()
    _docker("up", "-d", "--wait")
    _wait_for_postgres_final()
    load_seconds = time.monotonic() - started
    core: subprocess.Popen[bytes] | None = None
    try:
        core = _start_core()
        corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        keys, resources = _bootstrap(corpus)
        outcomes: list[dict[str, Any]] = []
        durations: list[float] = []
        for question in corpus["questions"]:
            result = _rpc(keys[question["actor"]], question["id"], question["canonical"]["semanticSql"], question["question"])
            passed, reason, duration = _assert_scenario(question, result)
            durations.append(duration)
            outcomes.append({"id": question["id"], "passed": passed, "reason": reason, "durationMs": duration})
        probes = _security_probes(keys, resources, corpus)
        failures = [item for item in outcomes if not item["passed"]]
        report = {
            "schemaVersion": 1, "corpusId": corpus["corpusId"], "datasetManifest": str(manifest.relative_to(REPO_ROOT)).replace("\\", "/"),
            "counts": {"total": len(outcomes), "passed": len(outcomes) - len(failures), "failed": len(failures),
                       "authorization": sum(
                           1 for item, question in zip(outcomes, corpus["questions"])
                           if question["category"] == "authorization" and item["passed"]
                       )},
            "timing": {"loadSeconds": round(load_seconds, 3), "medianQueryMs": round(statistics.median(durations), 3),
                       "p95QueryMs": round(_percentile(durations, .95), 3), "maxQueryMs": round(max(durations), 3)},
            "securityProbes": probes, "failures": failures,
            "status": "pass" if not failures and all(probes.values()) else "fail",
        }
        report_dir = DATA_ROOT / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "deterministic.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"HR_BENCHMARK_{report['status'].upper()}: {report['counts']['passed']}/{report['counts']['total']}; report={report_path}")
        return 0 if report["status"] == "pass" else 1
    finally:
        if core is not None and core.poll() is None:
            core.terminate()
            try:
                core.wait(timeout=10)
            except subprocess.TimeoutExpired:
                core.kill()


def clean() -> int:
    if shutil.which("docker"):
        _docker("down", "-v", "--remove-orphans", check=False)
    if DATA_ROOT.is_dir():
        resolved = DATA_ROOT.resolve()
        expected_parent = (REPO_ROOT / ".benchmark-data").resolve()
        if resolved.parent != expected_parent or resolved.name != "hr-enterprise":
            raise BenchmarkError("refusing to remove an unexpected benchmark data path")
        shutil.rmtree(resolved)
    print("HR_BENCHMARK_CLEAN_OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "clean", "print-policies"))
    args = parser.parse_args()
    try:
        if args.command == "run":
            return run()
        if args.command == "clean":
            return clean()
        print(json.dumps({actor: policy_document(actor, "datasource_test") for actor in (
            "employee", "department_manager", "hrbp", "compensation_admin", "hr_director")}, indent=2))
        return 0
    except (BenchmarkError, OSError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        print(f"HR_BENCHMARK_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
