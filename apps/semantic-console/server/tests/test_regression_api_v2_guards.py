from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import UTC, datetime

from server.tests import test_regression as regression_test_support


class RegressionApiV2AndDatasetGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the real runtime/API fixture without inheriting its test suite.
        regression_test_support.RegressionTests.setUp(self)

    def call(
        self,
        method: str,
        path: str,
        body=None,
        authorization: str | None = None,
    ):
        return self.app.request(
            method,
            "/api/v1/diagnostics/" + path,
            body,
            authorization=authorization or self.authorization,
        )

    def test_dataset_mutations_conflict_during_a_live_run_without_registry_changes(self) -> None:
        case = regression_test_support.RegressionTests.create_case(self)
        entered, release = threading.Event(), threading.Event()
        original = self.dispatcher.dispatch

        def block_core_run(request):
            if request["method"] == "query.run":
                entered.set()
                release.wait(5)
            return original(request)

        self.dispatcher.dispatch = block_core_run
        status, run = self.call("POST", "regression-runs", {"caseIds": [case["id"]]})
        self.assertEqual(status, 202, run)
        self.assertTrue(entered.wait(3), "regression worker did not enter Core query.run")

        try:
            before_status, before = self.call("GET", "test-datasets")
            self.assertEqual(before_status, 200)
            self.assertEqual(len(before["items"]), 1)
            dataset_before = before["items"][0]
            self.assertEqual(
                (dataset_before["id"], dataset_before["version"], dataset_before["datasourceId"]),
                ("fixture", "v1", self.datasource_id),
            )
            self.assertIn("updatedAt", dataset_before)

            attempts = (
                (
                    "POST",
                    "test-datasets",
                    {"id": "late-fixture", "version": "v1", "datasourceId": self.datasource_id},
                ),
                (
                    "PUT",
                    "test-datasets/fixture",
                    {"version": "v2", "datasourceId": self.datasource_id},
                ),
                ("DELETE", "test-datasets/fixture", None),
            )
            for method, path, body in attempts:
                with self.subTest(method=method):
                    mutation_status, response = self.call(method, path, body)
                    self.assertEqual(
                        (mutation_status, response.get("code")),
                        (409, "REGRESSION_ENVIRONMENT_BUSY"),
                        response,
                    )
                    current_status, current = self.call("GET", "test-datasets")
                    self.assertEqual(current_status, 200)
                    self.assertEqual(current, before)
        finally:
            release.set()

        for _ in range(200):
            _, detail = self.call("GET", "regression-runs/" + run["id"])
            if detail["status"] in {"completed", "cancelled", "interrupted"}:
                break
            time.sleep(0.01)
        self.assertEqual(detail["status"], "completed", detail)

    def test_schema_version_two_export_contains_reviewed_and_legacy_cases(self) -> None:
        reviewed = regression_test_support.RegressionTests.create_case(self)
        legacy = self.gateway.diagnostics.create_regression_case(
            auth=self.auth,
            project_id="runtime-test",
            feedback_id=self.feedback["feedbackId"],
            case={"kind": "agent_evidence", "question": "Legacy agent evidence"},
        )

        status, exported = self.app.request(
            "GET",
            "/api/v1/diagnostics/regression-cases/export?schemaVersion=2",
            authorization=self.authorization,
        )

        self.assertEqual(status, 200, exported)
        self.assertEqual(exported["schemaVersion"], 2)
        self.assertEqual(exported["projectId"], "runtime-test")
        cases = {record["id"]: record for record in exported["cases"]}
        self.assertEqual(cases[reviewed["id"]]["status"], "enabled")
        self.assertEqual(cases[reviewed["id"]]["revision"], 2)
        self.assertEqual(cases[legacy["id"]]["case"]["kind"], "agent_evidence")
        self.assertEqual(
            cases[legacy["id"]]["case"]["question"], "Legacy agent evidence"
        )

    def test_source_trace_from_another_organization_is_not_accessible(self) -> None:
        foreign_organization = "org-source-trace-foreign"
        with self.gateway.access_control._connect() as connection:
            connection.execute(
                "INSERT INTO organizations(id,name,created_at) VALUES(?,?,?)",
                (foreign_organization, "Foreign source organization", "2026-10-01T00:00:00Z"),
            )
        subject = self.gateway.access_control.create_service_account(
            "Foreign trace writer", organization_id=foreign_organization
        )
        key = self.gateway.access_control.issue_api_key(subject.id)
        foreign_auth = self.gateway.access_control.authenticate(f"Bearer {key['apiKey']}")
        occurred_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        foreign_trace = self.gateway.diagnostics.record_trace_events(
            auth=foreign_auth,
            project_id="runtime-test",
            source_session_id="foreign-session",
            source_turn_id="foreign-turn",
            events=[
                {"eventId": "foreign-start", "occurredAt": occurred_at, "type": "turn_started"}
            ],
        )

        status, response = self.call(
            "POST",
            "regression-cases",
            {
                "feedbackId": self.feedback["feedbackId"],
                "sourceTraceId": foreign_trace["id"],
                "case": self.case_input,
            },
        )

        self.assertEqual((status, response.get("code")), (404, "TRACE_NOT_FOUND"))
        _, listing = self.call("GET", "regression-cases")
        self.assertFalse(
            any(item["feedbackId"] == self.feedback["feedbackId"] for item in listing["items"])
        )
        self.assertNotIn(foreign_trace["id"], json.dumps(response))


if __name__ == "__main__":
    unittest.main()
