#!/usr/bin/env python3
"""Real PostgreSQL acceptance for SemaRail control, diagnostics, and preparation stores.

The administrator URL is read only from ``SEMARAIL_ACCEPTANCE_ADMIN_DATABASE_URL``.
The script provisions one randomly named database, exercises the production psycopg
backend, and removes that database in ``finally``. It never prints the URL.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import psycopg


ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = ROOT / "apps" / "semantic-console"
sys.path.insert(0, str(SERVER_ROOT))

from server.access_control import AccessControlStore  # noqa: E402
from server.diagnostics import DiagnosticError, DiagnosticStore  # noqa: E402
from server.project import ProjectStore  # noqa: E402
from server.query_preparation import PreparationError, QueryPreparationStore  # noqa: E402
from server.service import SemanticConsoleService  # noqa: E402


class FakeValidator:
    def health(self):
        return {"available": True}

    def validate(self, _path):
        return {"valid": True, "errors": [], "warnings": []}

    def build(self, _path):
        return {"models": []}


def _database_url(admin_url: str, database: str) -> str:
    parsed = urlsplit(admin_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
        raise RuntimeError("acceptance administrator URL must be a PostgreSQL URL")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, ""))


def main() -> int:
    admin_url = os.environ.get("SEMARAIL_ACCEPTANCE_ADMIN_DATABASE_URL", "").strip()
    if not admin_url:
        raise RuntimeError("SEMARAIL_ACCEPTANCE_ADMIN_DATABASE_URL is required")
    database = f"semarail_control_{uuid.uuid4().hex[:16]}"
    control_url = _database_url(admin_url, database)
    admin = psycopg.connect(admin_url, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{database}"')
        now = [datetime(2026, 9, 9, 12, 0, tzinfo=UTC)]
        access = AccessControlStore.from_config(
            ROOT / ".unused-control.sqlite3",
            database_url=control_url,
            clock=lambda: now[0],
        )
        if getattr(access, "path", "unexpected") is not None:
            raise AssertionError("control store did not select PostgreSQL")
        subject = access.create_service_account("PostgreSQL Acceptance Agent")
        key = access.issue_api_key(subject.id)
        auth = access.authenticate(f"Bearer {key['apiKey']}")
        other = access.create_service_account("Other PostgreSQL Agent")
        other_key = access.issue_api_key(other.id)
        other_auth = access.authenticate(f"Bearer {other_key['apiKey']}")

        diagnostics = DiagnosticStore(access)
        diagnostics.record_execution(
            auth=auth,
            project_id="acceptance-project",
            trace_id="trace-failure",
            query_id="query-failure",
            datasource_id="hr-datasource",
            transport="core-http",
            method="query.run",
            status="failure",
            stage="authorization",
            error={"reasonCode": "TABLE_PERMISSION_REQUIRED", "message": "Bearer secret-value"},
            question="Show compensation using password=unsafe",
            semantic_sql="SELECT salary FROM hr.compensation",
        )
        automatic = diagnostics.list_feedback(
            organization_id=auth.subject.organization_id,
            project_id="acceptance-project",
        )["items"][0]
        if automatic["evidence"]["source"] != "server" or "secret-value" in str(automatic):
            raise AssertionError("automatic failure capture was not server-owned and redacted")

        diagnostics.record_execution(
            auth=auth,
            project_id="acceptance-project",
            trace_id="trace-success",
            query_id="query-success",
            datasource_id="hr-datasource",
            transport="core-http",
            method="query.run",
            status="success",
            stage="complete",
            question="must not be retained before feedback",
            semantic_sql="SELECT 1",
        )
        all_diagnostics = diagnostics.list_diagnostics(
            organization_id=auth.subject.organization_id,
            project_id="acceptance-project",
        )["items"]
        before = next(item for item in all_diagnostics if item["traceId"] == "trace-success")
        if before["question"] is not None or before["semanticSql"] is not None:
            raise AssertionError("successful query content was retained without feedback")
        submitted = diagnostics.submit_feedback(
            auth=auth,
            project_id="acceptance-project",
            reference="query-success",
            idempotency_key="acceptance-feedback-1",
            category="ambiguity",
            description="Business definition is wrong",
            question="What is revenue?",
            semantic_sql="SELECT revenue FROM Sales",
        )
        duplicate = diagnostics.submit_feedback(
            auth=auth,
            project_id="acceptance-project",
            reference="query-success",
            idempotency_key="acceptance-feedback-1",
            category="other",
            description="retry",
        )
        if not duplicate["duplicate"] or duplicate["feedbackId"] != submitted["feedbackId"]:
            raise AssertionError("feedback retry was not idempotent")
        submitted_detail = diagnostics.feedback_detail(
            submitted["feedbackId"],
            organization_id=auth.subject.organization_id,
            project_id="acceptance-project",
        )
        if (
            submitted_detail["evidence"]["source"] != "client"
            or submitted_detail["traceId"] != "trace-success"
            or submitted_detail["queryId"] != "query-success"
        ):
            raise AssertionError("client evidence or trace reference was not preserved")
        original_query_id = diagnostics.resolve_owned_retry_reference(
            auth=auth,
            project_id="acceptance-project",
            reference="query-failure",
        )
        diagnostics.record_execution(
            auth=auth,
            project_id="acceptance-project",
            trace_id="trace-failure-retry",
            query_id="query-failure-retry",
            original_query_id=original_query_id,
            datasource_id="hr-datasource",
            transport="core-http",
            method="query.run",
            status="failure",
            stage="authorization",
            question="Show compensation using the corrected scope",
        )
        retry_item = next(
            item
            for item in diagnostics.list_diagnostics(
                organization_id=auth.subject.organization_id,
                project_id="acceptance-project",
            )["items"]
            if item["queryId"] == "query-failure-retry"
        )
        if retry_item["originalQueryId"] != "query-failure":
            raise AssertionError("retry did not retain the original query linkage")
        try:
            diagnostics.submit_feedback(
                auth=other_auth,
                project_id="acceptance-project",
                reference="query-success",
                idempotency_key="foreign-query",
                category="other",
                description="must fail",
            )
        except DiagnosticError as exc:
            if exc.code != "DIAGNOSTIC_NOT_FOUND":
                raise
        else:
            raise AssertionError("another subject linked the query")

        case = diagnostics.create_regression_case(
            auth=auth,
            project_id="acceptance-project",
            feedback_id=submitted["feedbackId"],
            enable=True,
            case={
                "kind": "deterministic_sql",
                "question": "What is revenue?",
                "semanticSql": "SELECT revenue FROM Sales",
                "testDatasetId": "acceptance-dataset",
                "expectedResult": {"columns": ["revenue"]},
            },
        )
        exported = diagnostics.export_regression_cases(
            organization_id=auth.subject.organization_id,
            project_id="acceptance-project",
        )
        if case["status"] != "enabled" or exported["schemaVersion"] != 1 or len(exported["cases"]) != 1:
            raise AssertionError("reviewed regression export failed")
        now[0] += timedelta(days=31)
        if diagnostics.cleanup_expired() != 3:
            raise AssertionError("30-day content cleanup did not purge all diagnostics")

        diagnostics.record_execution(
            auth=auth,
            project_id="other-project",
            trace_id="trace-other-project",
            query_id="query-other-project",
            datasource_id="hr-datasource",
            transport="core-http",
            method="query.run",
            status="success",
            stage="complete",
        )
        try:
            diagnostics.submit_feedback(
                auth=auth,
                project_id="other-project",
                reference="query-other-project",
                idempotency_key="acceptance-feedback-1",
                category="other",
                description="same key in a different project",
            )
        except DiagnosticError as exc:
            if exc.code != "IDEMPOTENCY_KEY_CONFLICT" or submitted["feedbackId"] in exc.safe_message:
                raise
        else:
            raise AssertionError("cross-project idempotency key reuse leaked or rebound feedback")

        page_ids = ["f" * 32, "0" * 32, "8" * 32]
        with patch(
            "server.diagnostics.uuid.uuid4",
            side_effect=[SimpleNamespace(hex=value) for value in page_ids],
        ):
            for index in range(3):
                diagnostics.record_execution(
                    auth=auth,
                    project_id="acceptance-project",
                    trace_id=f"trace-page-{index + 1}",
                    query_id=f"query-page-{index + 1}",
                    datasource_id="hr-datasource",
                    transport="core-http",
                    method="query.run",
                    status="success",
                    stage="complete",
                )
                now[0] += timedelta(minutes=1)
        cursor = None
        paged_queries = []
        for _ in range(6):
            page = diagnostics.list_diagnostics(
                organization_id=auth.subject.organization_id,
                project_id="acceptance-project",
                limit=1,
                cursor=cursor,
            )
            if not page["items"]:
                break
            paged_queries.append(page["items"][0]["queryId"])
            cursor = page["nextCursor"]
            if cursor is None:
                break
        if paged_queries[:3] != ["query-page-3", "query-page-2", "query-page-1"]:
            raise AssertionError("diagnostic cursor skipped or reordered records across timestamps")

        with tempfile.TemporaryDirectory(prefix="semarail-control-pg-") as temp:
            project_dir = Path(temp) / "project"
            project_dir.mkdir()
            (project_dir / "wren_project.yml").write_text(
                "schema_version: 5\nname: acceptance-project\ndata_source: postgres\n",
                encoding="utf-8",
            )
            project = ProjectStore(project_dir, state_dir=Path(temp) / "state", validator=FakeValidator())
            service = SemanticConsoleService(project)
            service.create_rule(
                {
                    "title": "Sales reporting period",
                    "content": "Confirm the reporting period",
                    "confirmationRule": {
                        "kind": "timeRange",
                        "models": ["Sales"],
                        "conditionKey": "timeRange",
                        "required": True,
                        "valueType": "dateRange",
                        "requireConfirmation": True,
                        "prompt": "Which reporting period?",
                    },
                }
            )
            preparations = QueryPreparationStore(access, project)
            pending = preparations.prepare(
                auth=auth,
                project_id="acceptance-project",
                question="Revenue?",
                semantic_sql="SELECT revenue FROM Sales",
                conditions={},
                confirmed_conditions=[],
            )
            if pending["status"] != "needs_clarification":
                raise AssertionError("required clarification was not requested")
            try:
                preparations.require_ready(
                    auth=auth,
                    project_id="acceptance-project",
                    semantic_sql="SELECT revenue FROM Sales",
                    preparation_id=None,
                )
            except PreparationError as exc:
                if exc.code != "CLARIFICATION_REQUIRED":
                    raise
            else:
                raise AssertionError("direct execution bypassed required clarification")
            ready = preparations.prepare(
                auth=auth,
                project_id="acceptance-project",
                question="Revenue?",
                semantic_sql="SELECT revenue FROM Sales",
                conditions={"timeRange": {"start": "2026-01-01", "end": "2026-06-30"}},
                confirmed_conditions=["timeRange"],
            )
            preparations.require_ready(
                auth=auth,
                project_id="acceptance-project",
                semantic_sql="SELECT revenue FROM Sales",
                preparation_id=ready["preparationId"],
            )

        with psycopg.connect(control_url) as connection:
            ledgers = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT 'access',max(version) FROM access_control_schema_migrations "
                    "UNION ALL SELECT 'diagnostics',max(version) FROM diagnostic_schema_migrations "
                    "UNION ALL SELECT 'preparation',max(version) FROM query_preparation_schema_migrations"
                ).fetchall()
            }
        if ledgers != {"access": 2, "diagnostics": 4, "preparation": 1}:
            raise AssertionError("PostgreSQL migration ledgers are incomplete")

        print("CONTROL_POSTGRES_E2E_PASS")
        print("  access/diagnostic/preparation migrations: passed")
        print("  automatic failure/redaction/success metadata-only: passed")
        print("  owner/project scope/idempotency/stable cursor/30-day cleanup/regression export: passed")
        print("  clarification/confirmation/direct-execution guard: passed")
        return 0
    finally:
        try:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()",
                (database,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        finally:
            admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
