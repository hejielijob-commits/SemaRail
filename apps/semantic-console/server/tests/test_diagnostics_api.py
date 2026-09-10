from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.access_control import AccessControlStore
from server.authorization import PolicyEngine
from server.diagnostics import DiagnosticStore
from server.diagnostics_api import DiagnosticsApi


class DiagnosticsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-diagnostics-api-")
        self.addCleanup(self.temp.cleanup)
        self.bootstrap = "bootstrap-token-that-is-at-least-thirty-two-characters"
        self.access = AccessControlStore(
            Path(self.temp.name) / "control.sqlite3", bootstrap_token=self.bootstrap
        )
        self.store = DiagnosticStore(self.access)
        self.api = DiagnosticsApi(
            self.store, self.access, PolicyEngine(), project_id="diagnostic-project"
        )
        subject = self.access.create_service_account("Feedback Agent")
        issued = self.access.issue_api_key(subject.id)
        self.authorization = f"Bearer {issued['apiKey']}"
        self.auth = self.access.authenticate(self.authorization)
        self.store.record_execution(
            auth=self.auth,
            project_id="diagnostic-project",
            trace_id="trace-feedback",
            query_id="query-feedback",
            datasource_id="warehouse",
            transport="core-http",
            method="query.run",
            status="success",
            stage="complete",
        )

    def test_authenticated_caller_submits_only_owned_query_and_retry_is_idempotent(self) -> None:
        body = {
            "reference": "query-feedback",
            "idempotencyKey": "attempt-one",
            "category": "sql_generation",
            "description": "口径错误",
            "expectedBehavior": "Use net revenue",
            "question": "Revenue?",
            "semanticSql": "SELECT revenue FROM orders",
        }
        status, first = self.api.dispatch(
            "POST", "/api/v1/feedback", {}, body, self.authorization
        )
        repeated_status, repeated = self.api.dispatch(
            "POST", "/api/v1/feedback", {}, {**body, "description": "retry"}, self.authorization
        )

        self.assertEqual(status, 201)
        self.assertEqual(repeated_status, 201)
        self.assertEqual(first["feedbackId"], repeated["feedbackId"])
        self.assertTrue(repeated["duplicate"])

        other = self.access.create_service_account("Other Feedback Agent")
        key = self.access.issue_api_key(other.id)
        denied_status, denied = self.api.dispatch(
            "POST",
            "/api/v1/feedback",
            {},
            {**body, "idempotencyKey": "other-attempt"},
            f"Bearer {key['apiKey']}",
        )
        self.assertEqual((denied_status, denied["code"]), (404, "DIAGNOSTIC_NOT_FOUND"))

    def test_admin_lists_updates_and_exports_reviewed_case(self) -> None:
        _, feedback = self.api.dispatch(
            "POST",
            "/api/v1/feedback",
            {},
            {
                "reference": "trace-feedback",
                "idempotencyKey": "workflow-one",
                "category": "evaluation",
                "description": "Expected a permission error",
            },
            self.authorization,
        )
        self.store.record_execution(
            auth=self.auth,
            project_id="diagnostic-project",
            trace_id="trace-canonical",
            query_id="query-canonical",
            datasource_id="warehouse",
            transport="core-http",
            method="query.run",
            status="success",
            stage="complete",
        )
        canonical_status, canonical = self.api.dispatch(
            "POST",
            "/api/v1/feedback",
            {},
            {
                "reference": "query-canonical",
                "idempotencyKey": "canonical-feedback",
                "category": "evaluation",
                "description": "Canonical issue",
            },
            self.authorization,
        )
        admin = f"Bearer {self.bootstrap}"
        list_status, listing = self.api.dispatch(
            "GET", "/api/v1/diagnostics/feedback", {"limit": "10"}, None, admin
        )
        update_status, updated = self.api.dispatch(
            "PUT",
            f"/api/v1/diagnostics/feedback/{feedback['feedbackId']}",
            {},
            {
                "status": "closed_no_fix",
                "category": "evaluation",
                "duplicateOf": canonical["feedbackId"],
                "note": "Duplicate; no separate fix",
            },
            admin,
        )
        case_status, case = self.api.dispatch(
            "POST",
            f"/api/v1/diagnostics/feedback/{feedback['feedbackId']}/regression-cases",
            {},
            {
                "enable": True,
                "case": {
                    "kind": "deterministic_sql",
                    "question": "Revenue?",
                    "semanticSql": "SELECT revenue FROM orders",
                    "testDatasetId": "fixture-v1",
                    "expectedError": {"reasonCode": "TABLE_PERMISSION_REQUIRED"},
                },
            },
            admin,
        )
        incomplete_status, incomplete = self.api.dispatch(
            "POST",
            f"/api/v1/diagnostics/feedback/{feedback['feedbackId']}/regression-cases",
            {},
            {
                "enable": True,
                "case": {
                    "kind": "agent_evidence",
                    "question": "Which revenue definition should be used?",
                },
            },
            admin,
        )
        agent_status, agent_case = self.api.dispatch(
            "POST",
            f"/api/v1/diagnostics/feedback/{feedback['feedbackId']}/regression-cases",
            {},
            {
                "enable": True,
                "case": {
                    "kind": "agent_evidence",
                    "question": "Which revenue definition should be used?",
                    "clarificationAnswers": {"business_definition": "net_revenue"},
                    "role": "finance-analyst",
                    "policyTestConfig": {"expectedReasonCode": None},
                    "semanticSnapshot": {"revision": "sha256:test"},
                    "testDatasetId": "external-agent-fixture-v1",
                    "expectedResult": {"clarificationAsked": True, "answerContains": "net revenue"},
                },
            },
            admin,
        )
        export_status, exported = self.api.dispatch(
            "GET", "/api/v1/diagnostics/regression-cases/export", {}, None, admin
        )

        self.assertEqual(list_status, 200)
        self.assertEqual(canonical_status, 201)
        self.assertIn("Expected a permission error", {item["description"] for item in listing["items"]})
        self.assertEqual(update_status, 200)
        self.assertEqual(updated["status"], "closed_no_fix")
        self.assertEqual(updated["duplicateOf"], canonical["feedbackId"])
        self.assertEqual(updated["history"][0]["note"], "Duplicate; no separate fix")
        self.assertEqual((case_status, case["status"]), (201, "enabled"))
        self.assertEqual(
            (incomplete_status, incomplete["code"]),
            (400, "REGRESSION_CASE_NOT_REPRODUCIBLE"),
        )
        self.assertEqual((agent_status, agent_case["status"]), (201, "enabled"))
        self.assertEqual(agent_case["case"]["kind"], "agent_evidence")
        self.assertEqual(export_status, 200)
        self.assertEqual(exported["schemaVersion"], 1)
        self.assertEqual(exported["cases"][0]["id"], case["id"])
        self.assertEqual({item["id"] for item in exported["cases"]}, {case["id"], agent_case["id"]})

    def test_non_admin_cannot_read_management_routes(self) -> None:
        status, body = self.api.dispatch(
            "GET", "/api/v1/diagnostics/feedback", {}, None, self.authorization
        )
        self.assertEqual((status, body["code"]), (403, "FORBIDDEN"))

    def test_query_and_admin_routes_cannot_cross_project_boundary(self) -> None:
        self.store.record_execution(
            auth=self.auth,
            project_id="other-project",
            trace_id="trace-other-project",
            query_id="query-other-project",
            datasource_id="warehouse",
            transport="core-http",
            method="query.run",
            status="success",
            stage="complete",
        )
        foreign = self.store.submit_feedback(
            auth=self.auth,
            project_id="other-project",
            reference="query-other-project",
            idempotency_key="foreign-project-feedback",
            category="other",
            description="Must remain in the other project",
        )

        submit_status, submit = self.api.dispatch(
            "POST",
            "/api/v1/feedback",
            {},
            {
                "reference": "query-other-project",
                "idempotencyKey": "cross-project-attempt",
                "category": "other",
                "description": "Should not attach",
            },
            self.authorization,
        )
        list_status, listing = self.api.dispatch(
            "GET",
            "/api/v1/diagnostics/feedback",
            {"limit": "100"},
            None,
            f"Bearer {self.bootstrap}",
        )
        detail_status, detail = self.api.dispatch(
            "GET",
            f"/api/v1/diagnostics/feedback/{foreign['feedbackId']}",
            {},
            None,
            f"Bearer {self.bootstrap}",
        )

        self.assertEqual((submit_status, submit["code"]), (404, "DIAGNOSTIC_NOT_FOUND"))
        self.assertEqual(list_status, 200)
        self.assertNotIn(foreign["feedbackId"], {item["id"] for item in listing["items"]})
        self.assertEqual((detail_status, detail["code"]), (404, "FEEDBACK_NOT_FOUND"))


if __name__ == "__main__":
    unittest.main()
