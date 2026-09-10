from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.access_control import AccessControlStore
from server.diagnostics import DiagnosticError, DiagnosticStore


class DiagnosticStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-diagnostics-")
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
        self.store = AccessControlStore(
            Path(self.temp.name) / "control.sqlite3",
            bootstrap_token="bootstrap-token-that-is-at-least-thirty-two-characters",
            clock=lambda: self.now,
        )
        subject = self.store.create_service_account("Diagnostic Agent")
        key = self.store.issue_api_key(subject.id)
        self.auth = self.store.authenticate(f"Bearer {key['apiKey']}")
        self.diagnostics = DiagnosticStore(self.store)

    def _record(self, *, status: str = "failure", trace_id: str = "trace-owned") -> str:
        return self.diagnostics.record_execution(
            auth=self.auth,
            project_id="hr-project",
            trace_id=trace_id,
            query_id="query-owned",
            datasource_id="hr-datasource",
            transport="core-http",
            method="query.run",
            status=status,
            stage="authorization",
            semantic_version="semantic-v3",
            policy_versions=["policy-1:4"],
            error={"reasonCode": "TABLE_PERMISSION_REQUIRED"},
            question="show salary Bearer top-secret-token",
            semantic_sql="SELECT salary FROM hr.compensation",
            native_sql="SELECT salary FROM hr.compensation WHERE password=unsafe",
            duration_ms=12.5,
        )

    def test_failure_is_bounded_redacted_and_separate_from_audit(self) -> None:
        diagnostic_id = self._record()

        result = self.diagnostics.list_diagnostics(
            organization_id=self.auth.subject.organization_id,
            project_id="hr-project",
        )

        self.assertEqual(result["items"][0]["id"], diagnostic_id)
        self.assertEqual(result["items"][0]["error"]["reasonCode"], "TABLE_PERMISSION_REQUIRED")
        self.assertEqual(result["items"][0]["durationMs"], 12.5)
        self.assertIn("[REDACTED]", result["items"][0]["question"])
        self.assertNotIn("top-secret-token", str(result))
        self.assertEqual(self.store.list_audit(), [])

        issue = self.diagnostics.list_feedback(
            organization_id=self.auth.subject.organization_id,
            project_id="hr-project",
        )["items"][0]
        self.assertEqual(issue["diagnosticId"], diagnostic_id)
        self.assertEqual(issue["category"], "runtime_failure")
        self.assertEqual(issue["evidence"]["source"], "server")

    def test_structured_error_secret_redaction_preserves_valid_json(self) -> None:
        self.diagnostics.record_execution(
            auth=self.auth,
            project_id="hr-project",
            trace_id="trace-json-redaction",
            query_id="query-json-redaction",
            datasource_id="hr-datasource",
            transport="core-http",
            method="query.run",
            status="failure",
            stage="execution",
            error={
                "reasonCode": "DATABASE_PERMISSION_REQUIRED",
                "message": "database rejected Bearer secret-value",
                "nested": {"dsn": "postgresql://admin:password@db.example/hr"},
            },
        )

        item = self.diagnostics.list_diagnostics(
            organization_id=self.auth.subject.organization_id,
            project_id="hr-project",
        )["items"][0]

        self.assertEqual(item["error"]["message"], "database rejected [REDACTED]")
        self.assertEqual(item["error"]["nested"]["dsn"], "[REDACTED]")

    def test_feedback_time_filter_is_server_side_and_validated(self) -> None:
        self._record(trace_id="trace-before")
        boundary = self.now + timedelta(hours=1)
        self.now = boundary + timedelta(hours=1)
        self._record(trace_id="trace-after")

        result = self.diagnostics.list_feedback(
            organization_id=self.auth.subject.organization_id,
            project_id="hr-project",
            created_after=boundary.isoformat(),
        )

        self.assertEqual([item["traceId"] for item in result["items"]], ["trace-after"])
        with self.assertRaises(DiagnosticError) as invalid:
            self.diagnostics.list_feedback(
                organization_id=self.auth.subject.organization_id,
                project_id="hr-project",
                created_after="yesterday",
            )
        self.assertEqual(invalid.exception.code, "INVALID_FILTER")

    def test_success_retains_only_ownership_metadata_until_feedback(self) -> None:
        self._record(status="success", trace_id="trace-success")

        item = self.diagnostics.list_diagnostics(
            organization_id=self.auth.subject.organization_id,
            project_id="hr-project",
        )["items"][0]

        self.assertEqual(item["status"], "success")
        self.assertIsNone(item["question"])
        self.assertIsNone(item["semanticSql"])
        self.assertIsNone(item["nativeSql"])
        self.assertIsNone(item["error"])

    def test_feedback_is_owner_scoped_and_idempotent(self) -> None:
        diagnostic_id = self._record()
        first = self.diagnostics.submit_feedback(
            auth=self.auth,
            project_id="hr-project",
            reference="query-owned",
            idempotency_key="feedback-attempt-1",
            category="permission_configuration",
            description="The policy is wrong",
            expected_behavior="Allow this aggregate",
            question="What is salary?",
            semantic_sql="SELECT salary FROM hr.compensation",
        )
        repeated = self.diagnostics.submit_feedback(
            auth=self.auth,
            project_id="hr-project",
            reference="query-owned",
            idempotency_key="feedback-attempt-1",
            category="permission_configuration",
            description="ignored on retry",
        )

        self.assertEqual(first["diagnosticId"], diagnostic_id)
        self.assertEqual(repeated["feedbackId"], first["feedbackId"])
        self.assertTrue(repeated["duplicate"])
        detail = self.diagnostics.feedback_detail(
            first["feedbackId"],
            organization_id=self.auth.subject.organization_id,
            project_id="hr-project",
        )
        self.assertEqual(detail["traceId"], "trace-owned")
        self.assertEqual(detail["queryId"], "query-owned")
        self.assertEqual(detail["evidence"]["source"], "client")
        self.assertEqual(detail["evidence"]["question"], "What is salary?")

        self.diagnostics.record_execution(
            auth=self.auth,
            project_id="other-project",
            trace_id="trace-other-project",
            query_id="query-other-project",
            datasource_id="hr-datasource",
            transport="core-http",
            method="query.run",
            status="success",
            stage="complete",
        )
        with self.assertRaises(DiagnosticError) as conflict:
            self.diagnostics.submit_feedback(
                auth=self.auth,
                project_id="other-project",
                reference="query-other-project",
                idempotency_key="feedback-attempt-1",
                category="other",
                description="same key in another project",
            )
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_KEY_CONFLICT")
        self.assertNotIn(first["feedbackId"], conflict.exception.safe_message)

        other = self.store.create_service_account("Other Agent")
        other_key = self.store.issue_api_key(other.id)
        other_auth = self.store.authenticate(f"Bearer {other_key['apiKey']}")
        with self.assertRaises(DiagnosticError) as denied:
            self.diagnostics.submit_feedback(
                auth=other_auth,
                project_id="hr-project",
                reference="query-owned",
                idempotency_key="other-feedback",
                category="other",
                description="try to claim another query",
            )
        self.assertEqual(denied.exception.code, "DIAGNOSTIC_NOT_FOUND")

    def test_diagnostic_cursor_follows_time_and_id_order_without_skips(self) -> None:
        identifiers = [
            SimpleNamespace(hex="f" * 32),
            SimpleNamespace(hex="0" * 32),
            SimpleNamespace(hex="8" * 32),
        ]
        with patch("server.diagnostics.uuid.uuid4", side_effect=identifiers):
            for index in range(3):
                self.diagnostics.record_execution(
                    auth=self.auth,
                    project_id="hr-project",
                    trace_id=f"trace-page-{index + 1}",
                    query_id=f"query-page-{index + 1}",
                    datasource_id="hr-datasource",
                    transport="core-http",
                    method="query.run",
                    status="success",
                    stage="complete",
                )
                self.now += timedelta(minutes=1)

        cursor = None
        traces = []
        for _ in range(3):
            page = self.diagnostics.list_diagnostics(
                organization_id=self.auth.subject.organization_id,
                project_id="hr-project",
                limit=1,
                cursor=cursor,
            )
            traces.append(page["items"][0]["traceId"])
            cursor = page["nextCursor"]
        self.assertEqual(traces, ["trace-page-3", "trace-page-2", "trace-page-1"])
        self.assertIsNone(cursor)

    def test_retry_reference_is_canonical_and_owner_scoped(self) -> None:
        self._record(trace_id="trace-original")
        original = self.diagnostics.resolve_owned_retry_reference(
            auth=self.auth, project_id="hr-project", reference="query-owned"
        )
        self.assertEqual(original, "query-owned")
        self.diagnostics.record_execution(
            auth=self.auth,
            project_id="hr-project",
            trace_id="trace-retry",
            query_id="query-retry",
            original_query_id=original,
            datasource_id="hr-datasource",
            transport="core-http",
            method="query.run",
            status="failure",
            stage="authorization",
            question="show salary",
        )
        self.assertEqual(
            self.diagnostics.resolve_owned_retry_reference(
                auth=self.auth, project_id="hr-project", reference="query-retry"
            ),
            "query-owned",
        )

        other = self.store.create_service_account("Retry reference attacker")
        other_key = self.store.issue_api_key(other.id)
        other_auth = self.store.authenticate(f"Bearer {other_key['apiKey']}")
        with self.assertRaises(DiagnosticError) as denied:
            self.diagnostics.resolve_owned_retry_reference(
                auth=other_auth, project_id="hr-project", reference="query-owned"
            )
        self.assertEqual(denied.exception.code, "DIAGNOSTIC_NOT_FOUND")

    def test_v3_storage_is_migrated_to_retry_linkage_schema(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy-control.sqlite3"
        legacy_access = AccessControlStore(legacy_path, clock=lambda: self.now)
        with legacy_access._connect() as connection:
            connection.execute(
                "CREATE TABLE diagnostic_schema_migrations "
                "(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                [(1, "2026-09-01T00:00:00Z"), (2, "2026-09-02T00:00:00Z"), (3, "2026-09-03T00:00:00Z")],
            )
            connection.execute(
                "CREATE TABLE query_diagnostics ("
                "id TEXT PRIMARY KEY,trace_id TEXT NOT NULL UNIQUE,query_id TEXT,"
                "organization_id TEXT NOT NULL,project_id TEXT NOT NULL,datasource_id TEXT,"
                "subject_id TEXT NOT NULL,credential_id TEXT,transport TEXT NOT NULL,method TEXT NOT NULL,"
                "status TEXT NOT NULL,stage TEXT NOT NULL,semantic_version TEXT,policy_versions_json TEXT NOT NULL,"
                "error_json TEXT,question TEXT,semantic_sql TEXT,native_sql TEXT,evidence_source TEXT NOT NULL,"
                "created_at TEXT NOT NULL,expires_at TEXT NOT NULL,content_purged_at TEXT,duration_ms DOUBLE PRECISION)"
            )

        DiagnosticStore(legacy_access)

        with legacy_access._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(query_diagnostics)").fetchall()}
            versions = [row["version"] for row in connection.execute(
                "SELECT version FROM diagnostic_schema_migrations ORDER BY version"
            ).fetchall()]
        self.assertIn("original_query_id", columns)
        self.assertEqual(versions, [1, 2, 3, 4])

    def test_cleanup_removes_content_but_preserves_metadata(self) -> None:
        self._record()
        self.now += timedelta(days=31)

        self.assertEqual(self.diagnostics.cleanup_expired(), 1)
        item = self.diagnostics.list_diagnostics(
            organization_id=self.auth.subject.organization_id,
            project_id="hr-project",
        )["items"][0]
        self.assertEqual(item["status"], "failure")
        self.assertEqual(item["queryId"], "query-owned")
        self.assertIsNone(item["question"])
        self.assertIsNotNone(item["contentPurgedAt"])


if __name__ == "__main__":
    unittest.main()
