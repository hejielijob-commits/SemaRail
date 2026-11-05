#!/usr/bin/env python3
"""Run a real, isolated HR snapshot feedback-to-regression acceptance.

The repository HR semantic project is copied into a fresh temporary workspace;
the tracked campaign project and its history are never edited. PostgreSQL's
reader password is loaded only in this process and is passed to the Sidecar's
connection resolver without being persisted or printed. The acceptance uses
the installed Wren 0.13.2 adapter, Core RPC, PostgreSQL, DiagnosticStore,
RegressionRunner and FeedbackResolutionStore. It deliberately imports no
external Agent evaluation, so the final resolution remains incomplete until a
real same-version Agent result is reviewed by an administrator.

Run with the repository virtualenv:

    .venv/Scripts/python.exe scripts/acceptance-business-trust-lifecycle.py

Use --work-dir with a new or empty directory to retain the isolated project
and control-plane state for a later authenticated MCP session. A normal run
uses an automatically removed temporary directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "apps" / "semantic-console"
SIDECAR_ROOT = ROOT / "python" / "sidecar"
PRIVATE_DATA = ROOT / ".benchmark-data" / "business-trust-v1-postgres"
SOURCE_PROJECT = ROOT / "benchmarks" / "hr-enterprise" / "project"
MANIFEST_PATH = PRIVATE_DATA / "load-report.json"
PASSWORD_PATH = PRIVATE_DATA / ".reader-password"

sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(SIDECAR_ROOT))

import psycopg  # noqa: E402

from sidecar import Dispatcher, default_dependencies  # noqa: E402
from server.access_control import AccessControlStore  # noqa: E402
from server.authorization import PolicyEngine, validate_policy_document  # noqa: E402
from server.diagnostics import DiagnosticStore  # noqa: E402
from server.feedback_resolution import FeedbackResolutionStore  # noqa: E402
from server.knowledge import RuleStore  # noqa: E402
from server.models import DatasourceRecord  # noqa: E402
from server.project import ProjectStore  # noqa: E402
from server.regression import RegressionRunner, RegressionStore  # noqa: E402
from server.runtime_rpc import RuntimeRpcGateway  # noqa: E402


DATASOURCE_ID = "hr_actual_snapshot"
DATASET_ID = "hr_enterprise_actual_snapshot"
SCENARIOS = (
    {
        "key": "salarySnapshot",
        "model": "compensation_history",
        "column": "monthly_salary",
        "date_column": "effective_date",
        "question": "What is the average monthly salary in the current salary snapshot?",
        "metric_check": "average-salary",
        "date_check": "salary-snapshot-date",
        "rule_slug": "acceptance-current-average-salary",
        "rule_title": "Acceptance: current average salary snapshot",
        "description": "The current-snapshot salary query used an earlier effective date.",
        "expected": "Use AVG(monthly_salary) at the latest effective_date in the published snapshot.",
    },
    {
        "key": "performanceSnapshot",
        "model": "performance_reviews",
        "column": "performance_score",
        "date_column": "review_date",
        "question": "What is the average performance score in the current review snapshot?",
        "metric_check": "average-performance",
        "date_check": "performance-snapshot-date",
        "rule_slug": "acceptance-current-performance-snapshot",
        "rule_title": "Acceptance: current performance snapshot",
        "description": "The current-snapshot performance query used an earlier review date.",
        "expected": "Use AVG(performance_score) at the latest review_date in the published snapshot.",
    },
)


class AcceptanceFailure(RuntimeError):
    """A sanitized acceptance assertion; never carries query or credential data."""


_CURRENT_PHASE = "startup"


def _phase(value: str) -> None:
    global _CURRENT_PHASE
    _CURRENT_PHASE = value


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise AcceptanceFailure(code)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if not 0 < len(raw) <= 1_048_576:
            raise ValueError("JSON file size is invalid")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise AcceptanceFailure(code) from exc
    if not isinstance(value, dict):
        raise AcceptanceFailure(code)
    return value


def _protect_secret_file(path: Path) -> None:
    """Restrict a short-lived local worker credential to the current user."""

    try:
        os.chmod(path, 0o600)
        if os.name == "nt":
            identity = subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"],
                check=True,
                capture_output=True,
                text=True,
            )
            rows = list(csv.reader(identity.stdout.splitlines()))
            sid = next(
                (
                    row[-1].strip()
                    for row in rows
                    if row and re.fullmatch(r"S-1-(?:[0-9]+-)+[0-9]+", row[-1].strip())
                ),
                None,
            )
            if sid is None:
                raise OSError("current user SID is unavailable")
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
                check=True,
                capture_output=True,
                text=True,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise AcceptanceFailure("PRIVATE_CREDENTIAL_PROTECTION_FAILED") from exc


def _write_private_secret(directory: Path, name: str, value: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{1,48}", name) or not isinstance(value, str) or not value:
        raise AcceptanceFailure("PRIVATE_CREDENTIAL_INVALID")
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        path = directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.write("\n")
        _protect_secret_file(path)
        return path
    except FileExistsError as exc:
        raise AcceptanceFailure("PRIVATE_CREDENTIAL_ALREADY_EXISTS") from exc
    except AcceptanceFailure:
        raise
    except OSError as exc:
        raise AcceptanceFailure("PRIVATE_CREDENTIAL_WRITE_FAILED") from exc


def _read_private_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AcceptanceFailure("PRIVATE_CREDENTIAL_UNAVAILABLE") from exc
    if not re.fullmatch(r"sr_live_[A-Za-z0-9_-]{20,}", value):
        raise AcceptanceFailure("PRIVATE_CREDENTIAL_INVALID")
    return value


def _write_report(path: Path, report: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        path.write_text(rendered + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        raise AcceptanceFailure("SAFE_REPORT_WRITE_FAILED") from exc


def _load_manifest() -> tuple[str, dict[str, str]]:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AcceptanceFailure("DATASET_MANIFEST_UNAVAILABLE") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 1
        or manifest.get("status") != "loaded"
        or not isinstance(manifest.get("dataManifestSha256"), str)
        or len(manifest["dataManifestSha256"]) != 64
    ):
        raise AcceptanceFailure("DATASET_MANIFEST_INVALID")
    expected_tables = {
        "compensation_history": "effective_date",
        "performance_reviews": "review_date",
    }
    return manifest["dataManifestSha256"], expected_tables


def _connection_url() -> str:
    """Construct the database URL in memory; never return it to a logger."""

    try:
        password = PASSWORD_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AcceptanceFailure("DATABASE_CREDENTIAL_UNAVAILABLE") from exc
    if not password or any(character in password for character in "\r\n\x00"):
        raise AcceptanceFailure("DATABASE_CREDENTIAL_INVALID")
    return (
        "postgresql://hr_benchmark_reader:"
        + quote(password, safe="")
        + "@127.0.0.1:55433/hr_enterprise?sslmode=disable"
    )


def _snapshot_dates(connection_url: str) -> dict[str, tuple[str, str]]:
    """Read only actual snapshot date metadata; never fetch metric values."""

    result: dict[str, tuple[str, str]] = {}
    try:
        with psycopg.connect(connection_url, connect_timeout=5) as connection:
            identity = connection.execute(
                "SELECT current_database(), current_setting('server_version_num')::int"
            ).fetchone()
            if identity is None or identity[0] != "hr_enterprise" or identity[1] // 10_000 != 17:
                raise AcceptanceFailure("DATABASE_IDENTITY_MISMATCH")
            for table, date_column in (
                ("compensation_history", "effective_date"),
                ("performance_reviews", "review_date"),
            ):
                # Identifiers are fixed constants above, not user input.
                rows = connection.execute(
                    f"SELECT DISTINCT {date_column}::text FROM hr.{table} "
                    f"WHERE {date_column} IS NOT NULL ORDER BY 1"
                ).fetchall()
                dates = [row[0] for row in rows if isinstance(row[0], str)]
                if len(dates) < 2:
                    raise AcceptanceFailure("SNAPSHOT_DATE_HISTORY_INCOMPLETE")
                result[table] = (dates[-2], dates[-1])
    except AcceptanceFailure:
        raise
    except Exception as exc:
        raise AcceptanceFailure("DATABASE_PREFLIGHT_FAILED") from exc
    return result


def _checks(scenario: dict[str, str], snapshot_date: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "checks": [
            {
                "id": scenario["metric_check"],
                "kind": "metric",
                "model": scenario["model"],
                "column": scenario["column"],
                "aggregation": "avg",
                "mandatory": True,
            },
            {
                "id": scenario["date_check"],
                "kind": "timeFilter",
                "model": scenario["model"],
                "column": scenario["date_column"],
                "conditionKey": "snapshotDate",
                "valueType": "date",
                "confirmation": "confirmedOrDefault",
                "defaultValue": snapshot_date,
                "mandatory": True,
            },
        ],
    }


def _rpc(
    gateway: RuntimeRpcGateway,
    authorization: str,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    status, response = gateway.dispatch(
        {
            "protocolVersion": "3",
            "id": f"accept-{uuid.uuid4().hex}",
            "method": method,
            "params": params,
            "deadlineMs": 30_000,
        },
        authorization,
        transport="runtime-rpc",
    )
    if status != 200 or response.get("ok") is not True:
        error = response.get("error")
        code = error.get("code") if isinstance(error, dict) else "CORE_RPC_FAILED"
        phase = error.get("phase") if isinstance(error, dict) else None
        reason = error.get("reasonCode") if isinstance(error, dict) else None
        safe_parts = [value for value in (code, phase, reason) if isinstance(value, str) and value.replace("_", "").replace("-", "").isalnum()]
        raise AcceptanceFailure(f"CORE_RPC_{method.upper().replace('.', '_')}_{'_'.join(safe_parts)}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise AcceptanceFailure("CORE_RPC_RESULT_INVALID")
    return {"result": result, "traceId": response.get("traceId")}


def _query(
    gateway: RuntimeRpcGateway,
    authorization: str,
    *,
    question: str,
    sql: str,
    query_id: str,
) -> dict[str, Any]:
    prepared_response = _rpc(
        gateway,
        authorization,
        "query.prepare",
        {"question": question, "semanticSql": sql, "conditions": {}, "confirmedConditions": []},
    )
    prepared = prepared_response["result"]
    if prepared.get("status") != "ready" or not isinstance(prepared.get("preparationId"), str):
        evidence = prepared.get("businessEvidence")
        codes = []
        if isinstance(evidence, dict) and isinstance(evidence.get("checks"), list):
            codes = [
                item.get("reasonCode")
                for item in evidence["checks"]
                if isinstance(item, dict) and isinstance(item.get("reasonCode"), str)
            ]
        reason = "_".join(codes[:4]) if codes else "PREPARATION_NOT_READY"
        raise AcceptanceFailure(f"QUERY_PREPARATION_{reason}")
    try:
        gateway.preparations.require_ready(
            auth=gateway.access_control.authenticate(authorization),
            project_id=str(gateway.project.overview().get("name") or ""),
            semantic_sql=sql,
            preparation_id=prepared["preparationId"],
        )
    except Exception as exc:
        evidence = getattr(exc, "business_evidence", None)
        codes = []
        if isinstance(evidence, dict) and isinstance(evidence.get("checks"), list):
            codes = [
                item.get("reasonCode")
                for item in evidence["checks"]
                if isinstance(item, dict) and isinstance(item.get("reasonCode"), str)
            ]
        code = getattr(exc, "code", "PREPARATION_REPLAY_FAILED")
        safe_code = code if isinstance(code, str) and code.replace("_", "").isalnum() else "PREPARATION_REPLAY_FAILED"
        raise AcceptanceFailure(f"PREPARATION_REPLAY_{safe_code}_{'_'.join(codes[:4])}") from exc
    run_response = _rpc(
        gateway,
        authorization,
        "query.run",
        {
            "question": question,
            "semanticSql": sql,
            "queryId": query_id,
            "preparationId": prepared["preparationId"],
            "chartIntent": "table",
        },
    )
    result = run_response["result"]
    if result.get("status") not in {"success", "completed", "ok"} and not result.get("success"):
        raise AcceptanceFailure("QUERY_EXECUTION_FAILED")
    if not isinstance(run_response.get("traceId"), str):
        raise AcceptanceFailure("QUERY_TRACE_MISSING")
    return {
        "queryId": query_id,
        "traceId": run_response["traceId"],
        "preparation": prepared,
        "result": result,
    }


def _policy(datasource_id: str, project_id: str, *, admin: bool = False) -> dict[str, Any]:
    tools = ["console:admin"] if admin else ["semantic:read", "query:plan", "query:execute"]
    tables = {
        "hr.compensation_history": {"effect": "allow", "rows": [], "columns": {"deny": []}},
        "hr.performance_reviews": {"effect": "allow", "rows": [], "columns": {"deny": []}},
    }
    return {
        "schemaVersion": 1,
        "datasourceId": datasource_id,
        "projects": [project_id],
        "tools": tools,
        "limits": {"maxRows": 20, "previewRows": 5, "maxPreviewBytes": 32_768, "timeoutMs": 30_000},
        "tables": tables,
    }


def _legacy_query_policy(datasource_id: str, project_id: str) -> dict[str, Any]:
    """The exact query policy document created before retained-session recovery."""

    document = _policy(datasource_id, project_id)
    document["tools"] = ["query:plan", "query:execute"]
    return document


def _scenario_sql(scenario: dict[str, str], snapshot_date: str) -> str:
    return (
        f"SELECT AVG({scenario['column']}) AS average_value "
        f"FROM {scenario['model']} "
        f"WHERE {scenario['date_column']} = DATE '{snapshot_date}'"
    )


def _wait_run(regression_store: RegressionStore, scope: tuple[str, str], run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = regression_store.run(scope, run_id)
        if run.get("status") in {"completed", "cancelled", "interrupted"}:
            return run
        time.sleep(0.2)
    raise AcceptanceFailure("REGRESSION_RUN_TIMEOUT")


def _scenario_result(
    resolution: dict[str, Any],
    run: dict[str, Any],
    case: dict[str, Any],
    *,
    feedback_id: str,
    source_query_id: str,
    source_trace_id: str,
    source_semantic_version: str,
    dataset_version: str,
    fixed_semantic_version: str,
) -> dict[str, Any]:
    verification = resolution.get("verification")
    checks = verification.get("checks") if isinstance(verification, dict) else None
    check_statuses = {
        item.get("key"): {"status": item.get("status"), "reasonCode": item.get("reasonCode")}
        for item in checks or []
        if isinstance(item, dict)
    }
    sql = next(
        (item for item in run.get("results", []) if isinstance(item, dict) and item.get("caseId") == case["id"]),
        None,
    )
    _require(run.get("status") == "completed", "REGRESSION_RUN_NOT_COMPLETED")
    _require(sql is not None and sql.get("status") == "passed", "SQL_REGRESSION_NOT_PASSED")
    _require(sql.get("reasonCode") == "ASSERTIONS_PASSED", "SQL_ASSERTIONS_NOT_PASSED")
    _require(sql.get("environment", {}).get("semanticVersion") == fixed_semantic_version, "SQL_SEMANTIC_VERSION_MISMATCH")
    _require(sql.get("environment", {}).get("datasetVersion") == dataset_version, "SQL_DATASET_VERSION_MISMATCH")
    _require(check_statuses.get("semanticAsset", {}).get("status") == "passed", "SEMANTIC_ASSET_LINK_NOT_PASSED")
    _require(check_statuses.get("feedbackEvidence", {}).get("status") == "passed", "SOURCE_FEEDBACK_VERSION_NOT_BASE")
    _require(check_statuses.get("regressionCase", {}).get("status") == "passed", "CASE_REVISION_NOT_LINKED")
    _require(check_statuses.get("sqlRun", {}).get("status") == "passed", "SQL_RUN_VERSION_CHAIN_NOT_PASSED")
    _require(check_statuses.get("agentEvaluation", {}).get("status") == "unverified", "AGENT_EVALUATION_MUST_REMAIN_UNVERIFIED")
    return {
        "feedbackId": feedback_id,
        "sourceQueryId": source_query_id,
        "sourceTraceId": source_trace_id,
        "sourceSemanticVersion": source_semantic_version,
        "fixSemanticVersion": fixed_semantic_version,
        "datasetVersion": dataset_version,
        "caseId": case["id"],
        "caseRevision": case["revision"],
        "runId": run["id"],
        "runStatus": run["status"],
        "sqlStatus": sql["status"],
        "resolutionStatus": verification.get("status"),
        "verificationChecks": check_statuses,
    }


def _run_acceptance(
    work_dir: Path,
    dataset_manifest_hash: str,
    connection_url: str,
    *,
    serve_mcp: bool = False,
    mcp_port: int = 48_764,
) -> tuple[dict[str, Any], Any | None, dict[str, str] | None]:
    _phase("copy_isolated_project")
    project_dir = work_dir / "semantic-project"
    state_dir = work_dir / "state"
    shutil.copytree(SOURCE_PROJECT, project_dir)

    # Give every run its own semantic project identity as well as its own
    # storage directory. The copied source project is never edited in place.
    isolated_project_id = f"semarail_hr_lifecycle_{uuid.uuid4().hex[:12]}"
    project_config = project_dir / "wren_project.yml"
    config_text = project_config.read_text(encoding="utf-8")
    _require("name: semarail_hr_enterprise" in config_text, "SOURCE_PROJECT_ID_UNEXPECTED")
    project_config.write_text(
        config_text.replace("name: semarail_hr_enterprise", f"name: {isolated_project_id}", 1),
        encoding="utf-8",
    )

    project = ProjectStore(project_dir, state_dir=state_dir)
    project_id = str(project.overview().get("name") or "")
    _require(project_id == isolated_project_id, "ISOLATED_PROJECT_ID_MISMATCH")

    project._datasources[DATASOURCE_ID] = DatasourceRecord(
        DATASOURCE_ID,
        "Actual HR snapshot acceptance",
        "postgres",
        {"host": "127.0.0.1", "port": 55433, "database": "hr_enterprise", "user": "hr_benchmark_reader"},
    )
    project.active_datasource_id = DATASOURCE_ID

    dates = _snapshot_dates(connection_url)
    manifest, _expected = _load_manifest()
    _require(manifest == dataset_manifest_hash, "DATASET_MANIFEST_CHANGED")

    rules = RuleStore(project)
    _phase("create_source_rules")
    scenario_records: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        previous_date, latest_date = dates[scenario["model"]]
        content = (
            f"Current {scenario['model'].replace('_', ' ')} snapshot averages {scenario['column']}. "
            f"The published SQL execution check binds the snapshot date."
        )
        created = rules.create(
            {
                "slug": scenario["rule_slug"],
                "title": scenario["rule_title"],
                "content": content,
                "executionChecks": _checks(scenario, previous_date),
            }
        )
        scenario_records[scenario["key"]] = {
            "record": created["rule"],
            "baseDate": previous_date,
            "latestDate": latest_date,
        }

    _phase("publish_source_snapshot")
    project.publish(label="isolated source snapshot before feedback repair")
    base_version_id = project.published_version_id
    base_semantic_version = project.published_revision
    _require(isinstance(base_version_id, str) and base_version_id, "BASE_PUBLICATION_MISSING")
    _require(not project.drafts, "BASE_DRAFTS_REMAIN")

    _phase("configure_runtime_and_auth")
    access = AccessControlStore.from_config(state_dir / "access-control.sqlite3", bootstrap_token="")
    query_subject = access.create_service_account("HR lifecycle query runner")
    token_expiry = (datetime.now(UTC) + timedelta(hours=24)).isoformat(timespec="seconds").replace("+00:00", "Z")
    query_credential = access.issue_api_key(query_subject.id, label="lifecycle-query", expires_at=token_expiry)
    query_authorization = f"Bearer {query_credential['apiKey']}"
    query_policy = access.create_policy("HR lifecycle query policy", _policy(DATASOURCE_ID, project_id))
    access.bind_policy(query_subject.id, query_policy["id"])

    admin_subject = access.create_service_account("HR lifecycle resolution administrator")
    admin_credential = access.issue_api_key(admin_subject.id, label="lifecycle-review", expires_at=token_expiry)
    admin_authorization = f"Bearer {admin_credential['apiKey']}"
    admin_policy = access.create_policy("HR lifecycle resolution administration", _policy(DATASOURCE_ID, project_id, admin=True))
    access.bind_policy(admin_subject.id, admin_policy["id"])
    query_auth = access.authenticate(query_authorization)
    admin_auth = access.authenticate(admin_authorization)

    dispatcher = Dispatcher(
        default_dependencies(
            connection_resolver=lambda _project, _environment: {
                "connectionUrl": connection_url,
                "datasource": "postgres",
            },
            semantic_index_dir=state_dir / "semantic-index",
        )
    )
    gateway = RuntimeRpcGateway(project, dispatcher, access_control=access)
    _require(gateway.diagnostics is not None, "DIAGNOSTIC_STORE_UNAVAILABLE")
    regression_store = RegressionStore(gateway.diagnostics)
    resolution_store = FeedbackResolutionStore(access, gateway.diagnostics, project, regression_store)

    feedback_records: dict[str, dict[str, Any]] = {}
    _phase("source_queries_and_feedback")
    for scenario in SCENARIOS:
        item = scenario_records[scenario["key"]]
        source_sql = _scenario_sql(scenario, item["baseDate"])
        query_id = f"hr-lifecycle-source-{scenario['key'].lower()}-{uuid.uuid4().hex[:10]}"
        source = _query(
            gateway,
            query_authorization,
            question=scenario["question"],
            sql=source_sql,
            query_id=query_id,
        )
        evidence = _rpc(
            gateway,
            query_authorization,
            "query.evidence",
            {"queryId": query_id},
        )["result"]
        _require(evidence.get("schemaVersion") == 1, "SOURCE_EVIDENCE_SCHEMA_INVALID")
        _require(evidence.get("projectRevision") == base_semantic_version, "SOURCE_EVIDENCE_VERSION_NOT_BASE")
        _require(evidence.get("outcome") == "success", "SOURCE_QUERY_EVIDENCE_NOT_SUCCESS")
        evidence_checks = evidence.get("checks")
        _require(
            isinstance(evidence_checks, list)
            and all(isinstance(check, dict) and check.get("status") == "passed" for check in evidence_checks),
            "SOURCE_QUERY_CHECKS_NOT_PASSED",
        )
        feedback_status, feedback = gateway.submit_feedback(
            {
                "reference": query_id,
                "idempotencyKey": f"hr-lifecycle-{scenario['key'].lower()}-{uuid.uuid4().hex}",
                "category": "sql_generation",
                "description": scenario["description"],
                "expectedBehavior": scenario["expected"],
                "question": scenario["question"],
                "semanticSql": source_sql,
            },
            query_authorization,
        )
        _require(feedback_status == 201 and isinstance(feedback.get("feedbackId"), str), "SOURCE_FEEDBACK_NOT_SAVED")
        feedback_records[scenario["key"]] = {
            "feedbackId": feedback["feedbackId"],
            "sourceQueryId": query_id,
            "sourceTraceId": source["traceId"],
            "sourceSemanticVersion": evidence["projectRevision"],
        }

    # Both fixes update the versioned RuleStore record and its effective rule
    # document in the isolated draft. Link the actual changed knowledge file
    # in the resolution ledger; executionChecks are versioned alongside it in
    # the semantic project index.
    _phase("repair_rules_and_publish")
    for scenario in SCENARIOS:
        record = scenario_records[scenario["key"]]["record"]
        latest_date = scenario_records[scenario["key"]]["latestDate"]
        rules.save(
            record["id"],
            {
                "content": (
                    f"Current {scenario['model'].replace('_', ' ')} snapshot averages "
                    f"{scenario['column']} as of {latest_date}. The published SQL "
                    "execution check binds the same snapshot date."
                ),
                "executionChecks": _checks(scenario, latest_date),
            },
        )

    project.publish(label="isolated current snapshot execution-check repair")
    fix_version_id = project.published_version_id
    fix_semantic_version = project.published_revision
    _require(isinstance(fix_version_id, str) and fix_version_id != base_version_id, "FIX_PUBLICATION_MISSING")
    _require(not project.drafts, "FIX_DRAFTS_REMAIN")
    _require(project.versions()[0]["id"] == fix_version_id, "CURRENT_PUBLICATION_MISMATCH")

    _phase("register_dataset_and_cases")
    dataset_version = f"hr-enterprise-{dataset_manifest_hash[:24]}"
    scope = regression_store.scope(query_auth, project_id)
    regression_store.save_dataset(
        scope,
        {"id": DATASET_ID, "version": dataset_version, "datasourceId": DATASOURCE_ID},
    )

    cases: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        item = scenario_records[scenario["key"]]
        feedback = feedback_records[scenario["key"]]
        latest_sql = _scenario_sql(scenario, item["latestDate"])
        case = {
            "schemaVersion": 2,
            "kind": "deterministic_sql",
            "question": scenario["question"],
            "semanticSql": latest_sql,
            "conditions": {},
            "confirmedConditions": [],
            "testDatasetId": DATASET_ID,
            # Keep actual compensation and performance values out of durable
            # run metadata. Query preparation checks the metric/date semantics;
            # the SQL assertion checks real execution and scalar result shape.
            "assertions": {"status": "success", "rowCount": 1},
        }
        cases[scenario["key"]] = regression_store.save_case(
            query_auth,
            project_id,
            {
                "feedbackId": feedback["feedbackId"],
                "case": case,
                "enable": True,
            },
        )

    _phase("execute_sql_regression")
    runner = RegressionRunner(regression_store, gateway)
    queued = runner.start(
        query_auth,
        project_id,
        query_authorization,
        [cases[scenario["key"]]["id"] for scenario in SCENARIOS],
    )
    run = _wait_run(regression_store, scope, queued["id"])
    _require(run.get("status") == "completed", "REGRESSION_BATCH_NOT_COMPLETED")

    _phase("link_resolution_evidence")
    scenario_reports: dict[str, Any] = {}
    for scenario in SCENARIOS:
        feedback = feedback_records[scenario["key"]]
        case = cases[scenario["key"]]
        resolution = resolution_store.save(
            admin_auth,
            project_id,
            feedback["feedbackId"],
            {
                "expectedRevision": 0,
                "assetPath": scenario_records[scenario["key"]]["record"]["sourcePath"],
                "baseVersionId": base_version_id,
                "publishedVersionId": fix_version_id,
                "caseId": case["id"],
                "caseRevision": case["revision"],
                "runId": run["id"],
            },
        )
        scenario_reports[scenario["key"]] = _scenario_result(
            resolution,
            run,
            case,
            feedback_id=feedback["feedbackId"],
            source_query_id=feedback["sourceQueryId"],
            source_trace_id=feedback["sourceTraceId"],
            source_semantic_version=feedback["sourceSemanticVersion"],
            dataset_version=dataset_version,
            fixed_semantic_version=fix_semantic_version,
        )

    report = {
        "schemaVersion": 1,
        "status": "sql_lifecycle_passed_agent_review_pending",
        "projectId": project_id,
        "datasetVersion": dataset_version,
        "datasourceId": DATASOURCE_ID,
        "basePublishedVersionId": base_version_id,
        "baseSemanticVersion": base_semantic_version,
        "fixPublishedVersionId": fix_version_id,
        "fixSemanticVersion": fix_semantic_version,
        "publishedVersionIsCurrent": project.versions()[0]["id"] == project.published_version_id,
        "scenarios": scenario_reports,
        "externalAgentEvaluationImported": False,
        "sourceFeedbackAndRegressionRecordsAreActual": True,
        "queryValuesIncluded": False,
        "readerPasswordPersisted": False,
        "formalCampaignProjectModified": False,
        "querySubjectId": query_subject.id,
        "reviewerSubjectId": admin_subject.id,
        "isolatedProjectDirectory": str(project_dir),
        "isolatedStateDirectory": str(state_dir),
    }
    if not serve_mcp:
        return report, None, None
    from server.remote_mcp import create_remote_mcp_server

    server = create_remote_mcp_server(
        project=project_dir,
        state_dir=state_dir,
        host="127.0.0.1",
        port=mcp_port,
        gateway=gateway,
    )
    return report, server, {
        "query": query_credential["apiKey"],
        "admin": admin_credential["apiKey"],
        "queryExpiresAt": query_credential["credential"]["expiresAt"],
    }


def _resume_retained_server(
    work_dir: Path,
    *,
    mcp_port: int,
) -> tuple[dict[str, Any], Any, dict[str, str], Path]:
    """Reopen a completed SQL lifecycle without rebuilding or republishing it."""

    _phase("load_retained_acceptance")
    work_dir = work_dir.expanduser().resolve()
    report_path = work_dir / "acceptance-report.json"
    report = _read_json(report_path, "ACCEPTANCE_REPORT_UNAVAILABLE")
    _require(report.get("schemaVersion") == 1, "ACCEPTANCE_REPORT_SCHEMA_UNSUPPORTED")
    _require(report.get("status") == "sql_lifecycle_passed_agent_review_pending", "ACCEPTANCE_NOT_PENDING_REVIEW")
    _require(report.get("sourceFeedbackAndRegressionRecordsAreActual") is True, "ACCEPTANCE_EVIDENCE_NOT_ACTUAL")
    _require(report.get("externalAgentEvaluationImported") is False, "ACCEPTANCE_ALREADY_FINALIZED")
    _require(report.get("queryValuesIncluded") is False, "ACCEPTANCE_REPORT_CONTAINS_QUERY_VALUES")
    _require(report.get("readerPasswordPersisted") is False, "ACCEPTANCE_READER_PASSWORD_PERSISTED")

    project_id = report.get("projectId")
    query_subject_id = report.get("querySubjectId")
    _require(isinstance(project_id, str) and project_id, "ACCEPTANCE_PROJECT_ID_MISSING")
    _require(isinstance(query_subject_id, str) and query_subject_id, "ACCEPTANCE_QUERY_SUBJECT_MISSING")
    scenarios = report.get("scenarios")
    _require(
        isinstance(scenarios, dict) and set(scenarios) == {item["key"] for item in SCENARIOS},
        "ACCEPTANCE_SCENARIOS_INVALID",
    )
    for item in scenarios.values():
        _require(
            isinstance(item, dict)
            and item.get("runStatus") == "completed"
            and item.get("sqlStatus") == "passed"
            and item.get("resolutionStatus") == "incomplete",
            "ACCEPTANCE_SQL_EVIDENCE_INVALID",
        )

    project_dir = Path(str(report.get("isolatedProjectDirectory", ""))).expanduser().resolve()
    state_dir = Path(str(report.get("isolatedStateDirectory", ""))).expanduser().resolve()
    try:
        project_dir.relative_to(work_dir)
        state_dir.relative_to(work_dir)
    except ValueError as exc:
        raise AcceptanceFailure("ACCEPTANCE_PATH_OUTSIDE_WORKSPACE") from exc
    _require(project_dir.is_dir() and state_dir.is_dir(), "ACCEPTANCE_STATE_UNAVAILABLE")

    _phase("verify_retained_publication_and_dataset")
    dataset_hash, _expected_tables = _load_manifest()
    expected_dataset_version = f"hr-enterprise-{dataset_hash[:24]}"
    _require(report.get("datasetVersion") == expected_dataset_version, "DATASET_VERSION_MISMATCH")
    connection_url = _connection_url()
    dates = _snapshot_dates(connection_url)
    _require(all(len(pair) == 2 for pair in dates.values()), "SNAPSHOT_PREFLIGHT_FAILED")

    project = ProjectStore(project_dir, state_dir=state_dir)
    overview = project.overview()
    _require(overview.get("name") == project_id, "ISOLATED_PROJECT_ID_MISMATCH")
    _require(project.published_version_id == report.get("fixPublishedVersionId"), "PUBLISHED_FIX_VERSION_CHANGED")
    _require(project.published_revision == report.get("fixSemanticVersion"), "PUBLISHED_FIX_SEMANTIC_VERSION_CHANGED")
    versions = project.versions()
    _require(bool(versions) and versions[0].get("id") == project.published_version_id, "CURRENT_PUBLICATION_MISMATCH")
    _require(not project.drafts, "FIX_DRAFTS_REMAIN")

    # The original acceptance wired the private Postgres reader into this
    # isolated process without persisting a password or URL. Restore the same
    # metadata-only datasource binding for the new in-process gateway.
    project._datasources[DATASOURCE_ID] = DatasourceRecord(
        DATASOURCE_ID,
        "Actual HR snapshot acceptance",
        "postgres",
        {"host": "127.0.0.1", "port": 55433, "database": "hr_enterprise", "user": "hr_benchmark_reader"},
    )
    project.active_datasource_id = DATASOURCE_ID

    _phase("refresh_retained_query_credential")
    access = AccessControlStore.from_config(state_dir / "access-control.sqlite3", bootstrap_token="")
    subject = access.subject(query_subject_id)
    _require(subject.kind == "service_account" and subject.status == "active", "QUERY_SUBJECT_UNAVAILABLE")
    expected_policy = _policy(DATASOURCE_ID, project_id)
    policies = access.policies_for_subject(query_subject_id)
    _require(
        len(policies) == 1
        and policies[0].get("name") == "HR lifecycle query policy"
        and policies[0].get("document") == expected_policy,
        "QUERY_POLICY_CHANGED",
    )

    dispatcher = Dispatcher(
        default_dependencies(
            connection_resolver=lambda _project, _environment: {
                "connectionUrl": connection_url,
                "datasource": "postgres",
            },
            semantic_index_dir=state_dir / "semantic-index",
        )
    )
    gateway = RuntimeRpcGateway(project, dispatcher, access_control=access)
    _require(gateway.diagnostics is not None, "DIAGNOSTIC_STORE_UNAVAILABLE")

    from server.remote_mcp import create_remote_mcp_server

    server = create_remote_mcp_server(
        project=project_dir,
        state_dir=state_dir,
        host="127.0.0.1",
        port=mcp_port,
        gateway=gateway,
    )
    private_dir = state_dir / "private"
    query_credential_file = private_dir / "agent-mcp-token"
    # Retire the previous private key before replacing its file. This handles
    # both the expired key from an interrupted server and a still-valid key
    # from an intentional restart, while keeping the same query subject.
    if query_credential_file.exists():
        previous_token = _read_private_secret(query_credential_file)
        pieces = previous_token.split("_")
        if len(pieces) == 4 and pieces[0] == "sr" and pieces[1] == "live":
            try:
                access.revoke_credential(pieces[2])
            except Exception:
                pass
        try:
            # The original acceptance grants read-only file access. Retain
            # user-only protection while allowing this process to retire the
            # stale token before installing its replacement.
            _protect_secret_file(query_credential_file)
            query_credential_file.unlink()
        except OSError as exc:
            raise AcceptanceFailure("PRIVATE_CREDENTIAL_REPLACE_FAILED") from exc

    token_expiry = (datetime.now(UTC) + timedelta(hours=24)).isoformat(timespec="seconds").replace("+00:00", "Z")
    credential = access.issue_api_key(query_subject_id, label="lifecycle-query-resume", expires_at=token_expiry)
    try:
        authenticated = access.authenticate(f"Bearer {credential['apiKey']}")
        _require(authenticated.subject.id == query_subject_id, "QUERY_CREDENTIAL_SUBJECT_MISMATCH")
    except Exception:
        try:
            access.revoke_credential(credential["credential"]["id"])
        except Exception:
            pass
        raise
    try:
        query_credential_file = _write_private_secret(private_dir, "agent-mcp-token", credential["apiKey"])
    except Exception:
        try:
            access.revoke_credential(credential["credential"]["id"])
        except Exception:
            pass
        raise

    report["mcpEndpoint"] = f"http://127.0.0.1:{mcp_port}/mcp"
    report["mcpCredentialFile"] = str(query_credential_file)
    report["mcpCredentialExpiresAfter"] = "24 hours"
    report["mcpCredentialExpiresAt"] = credential["credential"]["expiresAt"]
    try:
        _write_report(report_path, report)
    except Exception:
        try:
            access.revoke_credential(credential["credential"]["id"])
        except Exception:
            pass
        query_credential_file.unlink(missing_ok=True)
        raise
    return report, server, {"query": credential["apiKey"]}, query_credential_file


def _migrate_retained_query_context_policy(work_dir: Path) -> dict[str, Any]:
    """Add only semantic context-read scope to the exact retained query policy."""

    work_dir = work_dir.expanduser().resolve()
    report = _read_json(work_dir / "acceptance-report.json", "ACCEPTANCE_REPORT_UNAVAILABLE")
    _require(report.get("schemaVersion") == 1, "ACCEPTANCE_REPORT_SCHEMA_UNSUPPORTED")
    _require(report.get("status") == "sql_lifecycle_passed_agent_review_pending", "ACCEPTANCE_NOT_PENDING_REVIEW")
    _require(report.get("sourceFeedbackAndRegressionRecordsAreActual") is True, "ACCEPTANCE_EVIDENCE_NOT_ACTUAL")
    _require(report.get("externalAgentEvaluationImported") is False, "ACCEPTANCE_ALREADY_FINALIZED")
    _require(report.get("queryValuesIncluded") is False, "ACCEPTANCE_REPORT_CONTAINS_QUERY_VALUES")
    _require(report.get("readerPasswordPersisted") is False, "ACCEPTANCE_READER_PASSWORD_PERSISTED")

    project_id = report.get("projectId")
    query_subject_id = report.get("querySubjectId")
    reviewer_subject_id = report.get("reviewerSubjectId")
    _require(isinstance(project_id, str) and project_id, "ACCEPTANCE_PROJECT_ID_MISSING")
    _require(isinstance(query_subject_id, str) and query_subject_id, "ACCEPTANCE_QUERY_SUBJECT_MISSING")
    _require(isinstance(reviewer_subject_id, str) and reviewer_subject_id, "ACCEPTANCE_REVIEWER_SUBJECT_MISSING")
    scenarios = report.get("scenarios")
    _require(
        isinstance(scenarios, dict) and set(scenarios) == {item["key"] for item in SCENARIOS},
        "ACCEPTANCE_SCENARIOS_INVALID",
    )
    for item in scenarios.values():
        _require(
            isinstance(item, dict)
            and item.get("runStatus") == "completed"
            and item.get("sqlStatus") == "passed"
            and item.get("resolutionStatus") == "incomplete",
            "ACCEPTANCE_SQL_EVIDENCE_INVALID",
        )

    project_dir = Path(str(report.get("isolatedProjectDirectory", ""))).expanduser().resolve()
    state_dir = Path(str(report.get("isolatedStateDirectory", ""))).expanduser().resolve()
    try:
        project_dir.relative_to(work_dir)
        state_dir.relative_to(work_dir)
    except ValueError as exc:
        raise AcceptanceFailure("ACCEPTANCE_PATH_OUTSIDE_WORKSPACE") from exc
    _require(project_dir.is_dir() and state_dir.is_dir(), "ACCEPTANCE_STATE_UNAVAILABLE")

    dataset_hash, _expected_tables = _load_manifest()
    _require(
        report.get("datasetVersion") == f"hr-enterprise-{dataset_hash[:24]}",
        "DATASET_VERSION_MISMATCH",
    )
    project = ProjectStore(project_dir, state_dir=state_dir)
    _require(project.overview().get("name") == project_id, "ISOLATED_PROJECT_ID_MISMATCH")
    _require(project.published_version_id == report.get("fixPublishedVersionId"), "PUBLISHED_FIX_VERSION_CHANGED")
    _require(project.published_revision == report.get("fixSemanticVersion"), "PUBLISHED_FIX_SEMANTIC_VERSION_CHANGED")
    versions = project.versions()
    _require(bool(versions) and versions[0].get("id") == project.published_version_id, "CURRENT_PUBLICATION_MISMATCH")
    _require(not project.drafts, "FIX_DRAFTS_REMAIN")

    access = AccessControlStore.from_config(state_dir / "access-control.sqlite3", bootstrap_token="")
    query_subject = access.subject(query_subject_id)
    _require(query_subject.kind == "service_account" and query_subject.status == "active", "QUERY_SUBJECT_UNAVAILABLE")
    _require(query_subject.organization_id == "default", "QUERY_SUBJECT_ORGANIZATION_CHANGED")
    policies = access.policies_for_subject(query_subject_id)
    _require(len(policies) == 1, "QUERY_POLICY_SET_CHANGED")
    query_policy = policies[0]
    _require(query_policy.get("name") == "HR lifecycle query policy", "QUERY_POLICY_CHANGED")
    old_document = _legacy_query_policy(DATASOURCE_ID, project_id)
    new_document = _policy(DATASOURCE_ID, project_id)
    current_document = query_policy.get("document")

    if current_document == new_document:
        return {
            "status": "already_current",
            "addedToolScope": "semantic:read",
            "policyVersion": query_policy.get("version"),
            "allowedTables": sorted(new_document["tables"]),
            "tableAndColumnRulesUnchanged": True,
        }
    _require(current_document == old_document, "QUERY_POLICY_CHANGED")
    _require(
        query_policy.get("organizationId") == query_subject.organization_id,
        "QUERY_POLICY_ORGANIZATION_CHANGED",
    )
    _require(validate_policy_document(new_document) == new_document, "QUERY_POLICY_DOCUMENT_INVALID")

    reviewer_subject = access.subject(reviewer_subject_id)
    _require(
        reviewer_subject.kind == "service_account"
        and reviewer_subject.status == "active"
        and reviewer_subject.organization_id == query_subject.organization_id,
        "REVIEWER_SUBJECT_UNAVAILABLE",
    )
    reviewer_token = _read_private_secret(state_dir / "private" / "admin-review-token")
    reviewer_auth = access.authenticate(f"Bearer {reviewer_token}")
    _require(reviewer_auth.subject.id == reviewer_subject_id, "REVIEWER_SUBJECT_MISMATCH")
    reviewer_policies = access.policies_for_subject(reviewer_subject_id)
    _require(
        len(reviewer_policies) == 1
        and reviewer_policies[0].get("name") == "HR lifecycle resolution administration"
        and reviewer_policies[0].get("document") == _policy(DATASOURCE_ID, project_id, admin=True)
        and reviewer_policies[0].get("organizationId") == reviewer_subject.organization_id,
        "REVIEWER_POLICY_CHANGED",
    )
    decision = PolicyEngine().authorize_scope(
        reviewer_auth.subject,
        "console:admin",
        reviewer_policies,
        project_id=project_id,
    )
    _require(decision.allowed, "REVIEWER_ADMIN_SCOPE_REQUIRED")

    updated = access.update_policy(str(query_policy["id"]), new_document)
    _require(
        updated.get("document") == new_document
        and updated.get("version") == query_policy.get("version", 0) + 1,
        "QUERY_POLICY_UPDATE_NOT_CONFIRMED",
    )
    access.record_audit(
        action="access.policy.update",
        decision="allowed",
        auth=reviewer_auth,
        resource=f"policy:{query_policy['id']}",
        policy_version=str(updated["version"]),
        details={
            "migration": "lifecycle-query-semantic-read-v1",
            "addedToolScope": "semantic:read",
            "previousVersion": query_policy.get("version"),
        },
    )
    _require(
        access.policies_for_subject(query_subject_id)[0].get("document") == new_document,
        "QUERY_POLICY_UPDATE_NOT_CONFIRMED",
    )
    return {
        "status": "migrated",
        "addedToolScope": "semantic:read",
        "previousPolicyVersion": query_policy.get("version"),
        "policyVersion": updated.get("version"),
        "allowedTables": sorted(new_document["tables"]),
        "tableAndColumnRulesUnchanged": True,
        "auditRecorded": True,
    }


def _check_resume_readiness(work_dir: Path, *, mcp_port: int) -> dict[str, Any]:
    """Exercise retained-state reopening and clean up the temporary fresh key."""

    work_dir = work_dir.expanduser().resolve()
    report_path = work_dir / "acceptance-report.json"
    try:
        original_report = report_path.read_bytes()
    except OSError as exc:
        raise AcceptanceFailure("ACCEPTANCE_REPORT_UNAVAILABLE") from exc
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", mcp_port))
    except OSError as exc:
        raise AcceptanceFailure("MCP_PORT_UNAVAILABLE") from exc

    try:
        report, _server, credentials, credential_file = _resume_retained_server(work_dir, mcp_port=mcp_port)
        state_dir = Path(report["isolatedStateDirectory"])
        access = AccessControlStore.from_config(state_dir / "access-control.sqlite3", bootstrap_token="")
        credential_id = credentials["query"].split("_")[2]
        access.revoke_credential(credential_id)
        credential_file.unlink(missing_ok=True)
    except AcceptanceFailure:
        raise
    except Exception as exc:
        raise AcceptanceFailure("RESUME_CHECK_FAILED") from exc
    finally:
        try:
            report_path.write_bytes(original_report)
        except OSError as exc:
            raise AcceptanceFailure("RESUME_CHECK_CLEANUP_FAILED") from exc
    return {
        "status": "resume_ready",
        "projectId": report.get("projectId"),
        "datasetVersion": report.get("datasetVersion"),
        "fixSemanticVersion": report.get("fixSemanticVersion"),
        "mcpPortAvailable": True,
        "sameQuerySubjectCredentialRefresh": True,
        "privateCredentialRemovedAfterCheck": not credential_file.exists(),
        "retainedReportRestored": report_path.read_bytes() == original_report,
    }


def _validate_independent_oracle_reports(
    work_dir: Path,
    paths: list[Path],
    acceptance_report: dict[str, Any],
    covered_scenarios: list[str],
) -> dict[str, dict[str, Any]]:
    """Bind each covered evaluation to an observed query and private PG check."""

    _require(bool(paths), "INDEPENDENT_ORACLE_REPORT_REQUIRED")
    work_dir = work_dir.expanduser().resolve()
    private_dir = (work_dir / "private").resolve()
    acceptance_report_path = work_dir / "acceptance-report.json"
    try:
        acceptance_digest = hashlib.sha256(acceptance_report_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise AcceptanceFailure("ACCEPTANCE_REPORT_UNAVAILABLE") from exc
    scenario_by_key = {item["key"]: item for item in SCENARIOS}
    reviews: dict[str, dict[str, Any]] = {}

    for supplied_path in paths:
        path = supplied_path.expanduser().resolve()
        try:
            path.relative_to(private_dir)
        except ValueError as exc:
            raise AcceptanceFailure("ORACLE_REPORT_OUTSIDE_PRIVATE_DIRECTORY") from exc
        oracle = _read_json(path, "INDEPENDENT_ORACLE_REPORT_UNAVAILABLE")
        expected_oracle_fields = {
            "schemaVersion",
            "source",
            "status",
            "createdAt",
            "projectId",
            "datasetVersion",
            "semanticVersion",
            "retainedAcceptanceReportSha256",
            "scenario",
            "workerThreadId",
            "workerThreadIdsSeenInTranscript",
            "transcriptFile",
            "transcriptSha256",
            "recordSequences",
            "requestedQuestion",
            "submittedSemanticSql",
            "observedSemanticSql",
            "observedNativeSql",
            "observedResult",
            "physicalOracle",
            "matched",
            "agentReview",
        }
        _require(set(oracle) == expected_oracle_fields, "INDEPENDENT_ORACLE_REPORT_FIELDS_INVALID")
        scenario_key = oracle.get("scenario")
        _require(
            isinstance(scenario_key, str)
            and scenario_key in scenario_by_key
            and scenario_key in covered_scenarios,
            "ORACLE_SCENARIO_COVERAGE_MISMATCH",
        )
        _require(scenario_key not in reviews, "ORACLE_SCENARIO_REPORT_DUPLICATED")
        scenario = scenario_by_key[scenario_key]
        _require(
            oracle.get("schemaVersion") == 1
            and oracle.get("source") == "independent_postgresql_physical_oracle"
            and oracle.get("status") == "oracle_match_pending_root_agent_review"
            and oracle.get("matched") is True,
            "INDEPENDENT_ORACLE_DID_NOT_MATCH",
        )
        _require(
            oracle.get("projectId") == acceptance_report.get("projectId")
            and oracle.get("datasetVersion") == acceptance_report.get("datasetVersion")
            and oracle.get("semanticVersion") == acceptance_report.get("fixSemanticVersion")
            and oracle.get("retainedAcceptanceReportSha256") == acceptance_digest,
            "ORACLE_ACCEPTANCE_BINDING_MISMATCH",
        )
        _require(oracle.get("requestedQuestion") == scenario["question"], "ORACLE_QUESTION_MISMATCH")
        worker_thread_id = oracle.get("workerThreadId")
        _require(
            isinstance(worker_thread_id, str)
            and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", worker_thread_id),
            "ORACLE_WORKER_THREAD_ID_INVALID",
        )
        seen_thread_ids = oracle.get("workerThreadIdsSeenInTranscript")
        _require(
            isinstance(seen_thread_ids, list)
            and worker_thread_id in seen_thread_ids
            and all(
                isinstance(value, str)
                and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
                for value in seen_thread_ids
            ),
            "ORACLE_WORKER_THREAD_SET_INVALID",
        )
        transcript_name = oracle.get("transcriptFile")
        transcript_digest = oracle.get("transcriptSha256")
        _require(
            isinstance(transcript_name, str)
            and Path(transcript_name).name == transcript_name
            and isinstance(transcript_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", transcript_digest),
            "ORACLE_TRANSCRIPT_BINDING_INVALID",
        )
        transcript_path = (private_dir / transcript_name).resolve()
        try:
            transcript_path.relative_to(private_dir)
            observed_digest = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise AcceptanceFailure("ORACLE_TRANSCRIPT_UNAVAILABLE") from exc
        _require(observed_digest == transcript_digest, "ORACLE_TRANSCRIPT_DIGEST_MISMATCH")

        sequences = oracle.get("recordSequences")
        _require(
            isinstance(sequences, dict)
            and set(sequences) == {"context", "plan", "prepare", "governedQuery"}
            and all(type(value) is int and value > 0 for value in sequences.values())
            and sequences["context"] < sequences["plan"] < sequences["prepare"] < sequences["governedQuery"]
            and sequences["plan"] < sequences["prepare"],
            "ORACLE_TOOL_RECORD_BINDING_INVALID",
        )
        semantic_sql = oracle.get("submittedSemanticSql")
        observed_semantic_sql = oracle.get("observedSemanticSql")
        native_sql = oracle.get("observedNativeSql")
        _require(
            isinstance(semantic_sql, str)
            and bool(semantic_sql.strip())
            and observed_semantic_sql == semantic_sql
            and isinstance(native_sql, str)
            and bool(native_sql.strip()),
            "ORACLE_EXECUTED_SQL_BINDING_INVALID",
        )
        physical = oracle.get("physicalOracle")
        observed = oracle.get("observedResult")
        _require(
            isinstance(physical, dict)
            and set(physical) == {"snapshotDate", "average"}
            and isinstance(physical.get("snapshotDate"), str)
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", physical["snapshotDate"])
            and isinstance(observed, dict)
            and set(observed) == {"average"},
            "ORACLE_RESULT_BINDING_INVALID",
        )
        try:
            oracle_value = Decimal(str(physical.get("average")))
            observed_value = Decimal(str(observed.get("average")))
        except (InvalidOperation, ValueError) as exc:
            raise AcceptanceFailure("ORACLE_RESULT_NOT_NUMERIC") from exc
        _require(
            oracle_value.is_finite() and observed_value.is_finite() and oracle_value == observed_value,
            "ORACLE_RESULT_MISMATCH",
        )
        agent_review = oracle.get("agentReview")
        _require(
            isinstance(agent_review, dict)
            and set(agent_review) == {"decision"}
            and agent_review.get("decision") == "pending_root_review",
            "ORACLE_REVIEW_STATE_INVALID",
        )
        report_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        reviews[scenario_key] = {
            "scenario": scenario_key,
            "status": "matched",
            "workerThreadId": worker_thread_id,
            "transcriptSha256": transcript_digest,
            "reportSha256": report_digest,
            "governedQueryRecord": sequences["governedQuery"],
        }

    _require(set(reviews) == set(covered_scenarios), "ORACLE_SCENARIO_COVERAGE_INCOMPLETE")
    return reviews


def _finalize(
    work_dir: Path,
    summary_path: Path,
    agent_report_path: Path,
    oracle_report_paths: list[Path],
    *,
    review_decision: str,
    covered_scenarios: list[str],
) -> dict[str, Any]:
    _phase("load_external_agent_evaluation")
    work_dir = work_dir.expanduser().resolve()
    report_path = work_dir / "acceptance-report.json"
    report = _read_json(report_path, "ACCEPTANCE_REPORT_UNAVAILABLE")
    _require(report.get("schemaVersion") == 1, "ACCEPTANCE_REPORT_SCHEMA_UNSUPPORTED")
    _require(report.get("sourceFeedbackAndRegressionRecordsAreActual") is True, "ACCEPTANCE_EVIDENCE_NOT_ACTUAL")
    _require(report.get("externalAgentEvaluationImported") is False, "ACCEPTANCE_ALREADY_FINALIZED")
    project_id = report.get("projectId")
    scenarios = report.get("scenarios")
    _require(isinstance(project_id, str) and project_id, "ACCEPTANCE_PROJECT_ID_MISSING")
    _require(isinstance(scenarios, dict) and set(scenarios) == {item["key"] for item in SCENARIOS}, "ACCEPTANCE_SCENARIOS_INVALID")

    scenario_keys = {item["key"] for item in SCENARIOS}
    _require(
        isinstance(covered_scenarios, list)
        and 1 <= len(covered_scenarios) <= len(scenario_keys)
        and all(isinstance(key, str) and key in scenario_keys for key in covered_scenarios)
        and len(set(covered_scenarios)) == len(covered_scenarios),
        "AGENT_SCENARIO_COVERAGE_INVALID",
    )
    oracle_reviews = _validate_independent_oracle_reports(
        work_dir,
        oracle_report_paths,
        report,
        covered_scenarios,
    )

    project_dir = Path(str(report.get("isolatedProjectDirectory", ""))).expanduser().resolve()
    state_dir = Path(str(report.get("isolatedStateDirectory", ""))).expanduser().resolve()
    try:
        project_dir.relative_to(work_dir)
        state_dir.relative_to(work_dir)
    except ValueError as exc:
        raise AcceptanceFailure("ACCEPTANCE_PATH_OUTSIDE_WORKSPACE") from exc

    summary = _read_json(summary_path.expanduser().resolve(), "AGENT_SUMMARY_UNAVAILABLE")
    expected_fields = {
        "schemaVersion", "campaignId", "reportSha256", "source", "model", "reasoning",
        "datasetVersion", "semanticVersion", "verdictAggregates",
    }
    _require(set(summary) == expected_fields, "AGENT_SUMMARY_FIELDS_INVALID")
    _require(type(summary.get("schemaVersion")) is int and summary["schemaVersion"] == 1, "AGENT_SUMMARY_SCHEMA_UNSUPPORTED")
    try:
        artifact = agent_report_path.expanduser().resolve().read_bytes()
    except OSError as exc:
        raise AcceptanceFailure("AGENT_REPORT_UNAVAILABLE") from exc
    _require(0 < len(artifact) <= 10 * 1024 * 1024, "AGENT_REPORT_SIZE_INVALID")
    artifact_sha256 = hashlib.sha256(artifact).hexdigest()
    _require(summary.get("reportSha256") == artifact_sha256, "AGENT_REPORT_DIGEST_MISMATCH")
    _require(summary.get("datasetVersion") == report.get("datasetVersion"), "AGENT_DATASET_VERSION_MISMATCH")
    fixed_versions = {item.get("fixSemanticVersion") for item in scenarios.values() if isinstance(item, dict)}
    _require(len(fixed_versions) == 1 and summary.get("semanticVersion") in fixed_versions, "AGENT_SEMANTIC_VERSION_MISMATCH")
    aggregates = summary.get("verdictAggregates")
    _require(
        isinstance(aggregates, dict)
        and set(aggregates) == {"passed", "failed", "inconclusive", "total"}
        and all(type(aggregates.get(key)) is int and aggregates[key] >= 0 for key in aggregates)
        and aggregates.get("total") == len(covered_scenarios)
        and aggregates.get("passed") == aggregates.get("total")
        and aggregates.get("failed") == 0
        and aggregates.get("inconclusive") == 0,
        "AGENT_EVALUATION_NOT_ALL_PASS",
    )
    _require(review_decision == "approved", "ADMIN_REVIEW_NOT_APPROVED")

    _phase("authenticate_reviewer_and_load_stores")
    access = AccessControlStore.from_config(state_dir / "access-control.sqlite3", bootstrap_token="")
    reviewer_token = _read_private_secret(state_dir / "private" / "admin-review-token")
    reviewer_auth = access.authenticate(f"Bearer {reviewer_token}")
    _require(reviewer_auth.subject.id == report.get("reviewerSubjectId"), "REVIEWER_SUBJECT_MISMATCH")
    project = ProjectStore(project_dir, state_dir=state_dir)
    diagnostics = DiagnosticStore(access)
    regression_store = RegressionStore(diagnostics)
    resolution_store = FeedbackResolutionStore(access, diagnostics, project, regression_store)

    _phase("import_review_and_verify")
    finalized_scenarios: dict[str, Any] = {}
    for key, scenario_report in scenarios.items():
        _require(isinstance(scenario_report, dict), "ACCEPTANCE_SCENARIO_INVALID")
        if key not in covered_scenarios:
            updated = dict(scenario_report)
            updated["agentEvaluationCoverageStatus"] = "not_evaluated"
            finalized_scenarios[key] = updated
            continue
        feedback_id = scenario_report.get("feedbackId")
        _require(isinstance(feedback_id, str) and feedback_id, "ACCEPTANCE_FEEDBACK_ID_MISSING")
        evaluation = resolution_store.import_evaluation(
            reviewer_auth,
            project_id,
            feedback_id,
            summary,
        )
        review = evaluation.get("review")
        if review is None:
            evaluation = resolution_store.review_evaluation(
                reviewer_auth,
                project_id,
                feedback_id,
                evaluation["id"],
                {"expectedRevision": evaluation["revision"], "decision": review_decision},
            )
            review = evaluation.get("review")
        _require(
            isinstance(review, dict)
            and review.get("decision") == review_decision
            and review.get("reviewerSubjectId") == reviewer_auth.subject.id,
            "AGENT_EVALUATION_REVIEW_INVALID",
        )
        verified = resolution_store.verify(reviewer_auth, project_id, feedback_id)
        _require(verified.get("verification", {}).get("status") == "verified", "FULL_VERIFICATION_FAILED")
        updated = dict(scenario_report)
        updated["resolutionStatus"] = "verified"
        updated["agentEvaluationCoverageStatus"] = "evaluated"
        updated["independentOracleReview"] = dict(oracle_reviews[key])
        updated["agentEvaluationId"] = evaluation["id"]
        updated["agentEvaluationRevision"] = evaluation["revision"]
        updated["agentReview"] = review
        updated["verificationChecks"] = {
            item["key"]: {"status": item["status"], "reasonCode": item["reasonCode"]}
            for item in verified["verification"]["checks"]
            if isinstance(item, dict)
        }
        finalized_scenarios[key] = updated

    result = dict(report)
    result.update(
        {
            "status": "fully_verified" if set(covered_scenarios) == scenario_keys else "partially_verified",
            "externalAgentEvaluationImported": True,
            "agentEvaluation": {
                "campaignId": summary["campaignId"],
                "reportSha256": summary["reportSha256"],
                "source": summary["source"],
                "model": summary["model"],
                "reasoning": summary["reasoning"],
                "datasetVersion": summary["datasetVersion"],
                "semanticVersion": summary["semanticVersion"],
                "verdictAggregates": dict(aggregates),
                "coveredScenarios": list(covered_scenarios),
            },
            "independentOracleReports": [dict(oracle_reviews[key]) for key in covered_scenarios],
            "scenarios": finalized_scenarios,
        }
    )
    _phase("write_final_report")
    _write_report(work_dir / "agent-verified-report.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="retain project and state under a new or empty directory instead of using an auto-removed temp directory",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="write the safe metadata report to a JSON file (contains no SQL, query values or credentials)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="retain the isolated acceptance and serve its authenticated query-only MCP endpoint",
    )
    parser.add_argument(
        "--resume-serve",
        action="store_true",
        help="reopen an existing SQL-passed acceptance, refresh its query credential, and serve the same published fix",
    )
    parser.add_argument(
        "--resume-check",
        action="store_true",
        help="exercise retained-state and credential-refresh readiness, then revoke and remove the temporary credential",
    )
    parser.add_argument(
        "--migrate-query-context-policy",
        action="store_true",
        help="add semantic:read to the exact retained query policy after validating the isolated reviewer and publication",
    )
    parser.add_argument("--mcp-port", type=int, default=48_764)
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="import a real external Agent report, record administrator review, and verify the full chain",
    )
    parser.add_argument("--agent-summary", type=Path, help="strict summary JSON bound to --agent-report by SHA-256")
    parser.add_argument("--agent-report", type=Path, help="raw external Agent evaluation artifact; contents are never printed")
    parser.add_argument(
        "--oracle-report",
        type=Path,
        action="append",
        help="protected independent SQL/physical-PostgreSQL review report; repeat once for each covered scenario",
    )
    parser.add_argument(
        "--covered-scenario",
        action="append",
        choices=tuple(item["key"] for item in SCENARIOS),
        help="scenario explicitly present in the reviewed external Agent artifact; repeat for each covered scenario",
    )
    parser.add_argument("--review-decision", choices=("approved",), help="explicit administrator decision for --finalize")
    args = parser.parse_args(argv)

    try:
        if args.migrate_query_context_policy:
            _require(args.work_dir is not None, "MIGRATION_WORK_DIR_REQUIRED")
            _require(
                not args.finalize
                and not args.serve
                and not args.resume_serve
                and not args.resume_check
                and args.agent_summary is None
                and args.agent_report is None
                and args.oracle_report is None
                and args.covered_scenario is None
                and args.review_decision is None
                and args.report is None,
                "MIGRATION_MODE_ARGUMENTS_INVALID",
            )
            _phase("migrate_query_context_policy")
            migration = _migrate_retained_query_context_policy(args.work_dir)
            print(json.dumps(migration, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 0

        if args.finalize:
            _require(args.work_dir is not None, "FINALIZE_WORK_DIR_REQUIRED")
            _require(args.agent_summary is not None and args.agent_report is not None, "FINALIZE_AGENT_ARTIFACTS_REQUIRED")
            _require(args.oracle_report is not None, "FINALIZE_ORACLE_REPORTS_REQUIRED")
            _require(args.review_decision is not None, "FINALIZE_REVIEW_DECISION_REQUIRED")
            _require(args.covered_scenario is not None, "FINALIZE_COVERAGE_REQUIRED")
            _require(not args.serve and not args.resume_serve and not args.resume_check, "FINALIZE_AND_SERVE_ARE_MUTUALLY_EXCLUSIVE")
            final_report = _finalize(
                args.work_dir,
                args.agent_summary,
                args.agent_report,
                args.oracle_report,
                review_decision=args.review_decision,
                covered_scenarios=args.covered_scenario,
            )
            output_path = args.report.expanduser().resolve() if args.report else args.work_dir.expanduser().resolve() / "agent-verified-report.json"
            _write_report(output_path, final_report)
            print(json.dumps(final_report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 0

        _require(
            args.agent_summary is None
            and args.agent_report is None
            and args.oracle_report is None
            and args.review_decision is None
            and args.covered_scenario is None,
            "FINALIZE_ARGUMENTS_REQUIRE_FINALIZE",
        )
        _require(not (args.serve and args.resume_serve), "SERVE_MODES_ARE_MUTUALLY_EXCLUSIVE")
        _require(not (args.resume_check and (args.serve or args.resume_serve)), "RESUME_CHECK_AND_SERVE_ARE_MUTUALLY_EXCLUSIVE")
        if args.resume_check:
            _require(args.work_dir is not None, "RESUME_WORK_DIR_REQUIRED")
            _require(1 <= args.mcp_port <= 65_535, "MCP_PORT_INVALID")
            readiness = _check_resume_readiness(args.work_dir, mcp_port=args.mcp_port)
            print(json.dumps(readiness, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 0
        _require(not (args.serve or args.resume_serve) or args.work_dir is not None, "SERVE_WORK_DIR_REQUIRED")
        _require(1 <= args.mcp_port <= 65_535, "MCP_PORT_INVALID")
        server = None
        credentials = None
        query_credential_file: Path | None = None
        if args.resume_serve:
            _require(args.work_dir is not None, "RESUME_WORK_DIR_REQUIRED")
            report, server, credentials, query_credential_file = _resume_retained_server(
                args.work_dir,
                mcp_port=args.mcp_port,
            )
        else:
            _require(SOURCE_PROJECT.is_dir(), "SOURCE_PROJECT_UNAVAILABLE")
            dataset_hash, _ = _load_manifest()
            connection_url = _connection_url()
            # Prove the private reader login works before writing any project state.
            dates = _snapshot_dates(connection_url)
            _require(all(len(pair) == 2 for pair in dates.values()), "SNAPSHOT_PREFLIGHT_FAILED")

        if not args.resume_serve and args.work_dir is not None:
            work_dir = args.work_dir.expanduser().resolve()
            if work_dir.exists() and any(work_dir.iterdir()):
                raise AcceptanceFailure("WORK_DIR_MUST_BE_EMPTY")
            work_dir.mkdir(parents=True, exist_ok=True)
            report, server, credentials = _run_acceptance(
                work_dir,
                dataset_hash,
                connection_url,
                serve_mcp=args.serve,
                mcp_port=args.mcp_port,
            )
        elif not args.resume_serve:
            with tempfile.TemporaryDirectory(prefix="semarail-business-trust-lifecycle-") as temporary:
                report, server, credentials = _run_acceptance(Path(temporary), dataset_hash, connection_url)

        if server is not None:
            _require(args.work_dir is not None and credentials is not None, "MCP_SERVE_CONFIGURATION_INVALID")
            state_dir = Path(report["isolatedStateDirectory"])
            private_dir = state_dir / "private"
            if query_credential_file is None:
                query_credential_file = _write_private_secret(private_dir, "agent-mcp-token", credentials["query"])
            if not args.resume_serve:
                _write_private_secret(private_dir, "admin-review-token", credentials["admin"])
            report["mcpEndpoint"] = f"http://127.0.0.1:{args.mcp_port}/mcp"
            report["mcpCredentialFile"] = str(query_credential_file)
            report["mcpCredentialExpiresAfter"] = "24 hours"
            if not args.resume_serve:
                report["mcpCredentialExpiresAt"] = credentials["queryExpiresAt"]
            report_path = args.work_dir.expanduser().resolve() / "acceptance-report.json"
            _write_report(report_path, report)
            if args.report is not None:
                _write_report(args.report.expanduser().resolve(), report)
            rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            print(rendered, flush=True)
            try:
                server.run(transport="streamable-http")
            finally:
                try:
                    access = AccessControlStore.from_config(state_dir / "access-control.sqlite3", bootstrap_token="")
                    credential_id = credentials["query"].split("_")[2]
                    access.revoke_credential(credential_id)
                except Exception:
                    pass
                query_credential_file.unlink(missing_ok=True)
            return 0

        if args.work_dir is not None:
            _write_report(args.work_dir.expanduser().resolve() / "acceptance-report.json", report)
        rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        print(rendered)
        if args.report is not None:
            _write_report(args.report.expanduser().resolve(), report)
        return 0
    except AcceptanceFailure as exc:
        print(json.dumps({"status": "failed", "code": str(exc)}, separators=(",", ":")))
        return 1
    except Exception as exc:
        # Do not print exception text: adapters can include SQL, rows, paths or
        # connection details. The class name provides safe debugging metadata.
        exception_code = getattr(exc, "code", None)
        if not isinstance(exception_code, str) or not exception_code.replace("_", "").isalnum():
            exception_code = "UNEXPECTED_FAILURE"
        print(json.dumps({"status": "failed", "code": exception_code, "phase": _CURRENT_PHASE, "type": type(exc).__name__}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
