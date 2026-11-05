from __future__ import annotations

import unittest

from server.business_trust_api import BusinessTrustApi
from server.regression import RegressionStore
from server.tests import test_business_evidence as fixtures


class BusinessTrustApiTests(unittest.TestCase):
    def setUp(self) -> None:
        fixtures.BusinessEvidenceTests.setUp(self)
        self.api = BusinessTrustApi(self.gateway, RegressionStore(self.gateway.diagnostics))
        self.store.record(auth=self.auth, project_id="runtime-test", query_id="q-evidence", datasource_id=self.datasource_id, template=self.template, outcome="success")

    def test_console_evidence_requires_admin_even_for_query_owner(self):
        route = "/api/v1/diagnostics/queries/q-evidence/evidence"
        status, response = self.api.dispatch("GET", route, None, self.authorization)
        self.assertEqual(status, 403)
        self.assertNotIn("checks", response)
        status, evidence = self.api.dispatch("GET", route, None, self.admin_authorization)
        self.assertEqual(status, 200)
        self.assertEqual(evidence["queryId"], "q-evidence")
        self.assertNotIn("semanticSql", evidence)
        self.assertNotIn("previewRows", evidence)

    def test_evidence_route_is_authenticated_read_only_and_separate(self):
        route = "/api/v1/diagnostics/queries/q-evidence/evidence"
        self.assertEqual(self.api.dispatch("GET", route, None, None)[0], 401)
        self.assertEqual(self.api.dispatch("POST", route, {"checks": []}, self.admin_authorization)[0], 405)
        self.assertIsNone(self.api.dispatch("GET", "/api/v1/diagnostics/queries/q-evidence", None, self.admin_authorization))

    def test_verification_cannot_receive_caller_supplied_passes(self):
        self.gateway.diagnostics.record_execution(auth=self.auth, project_id="runtime-test", trace_id="trace-feedback-api", query_id="q-feedback-api", datasource_id=self.datasource_id, semantic_version=self.project.published_revision, transport="core-http", method="query.run", status="success", stage="complete")
        feedback = self.gateway.diagnostics.submit_feedback(auth=self.auth, project_id="runtime-test", reference="q-feedback-api", idempotency_key="feedback-api", category="sql_generation", description="Incorrect business snapshot")
        route = "/api/v1/diagnostics/feedback/" + feedback["feedbackId"] + "/resolution/verify"
        status, response = self.api.dispatch("POST", route, {"status": "verified", "checks": [{"status": "passed"}]}, self.admin_authorization)
        self.assertEqual((status, response["code"]), (400, "INVALID_REQUEST"))
        status, response = self.api.dispatch("POST", route, {}, self.authorization)
        self.assertEqual(status, 403)
        status, response = self.api.dispatch("POST", route, {}, self.admin_authorization)
        self.assertEqual(status, 200)
        self.assertNotEqual(response.get("status"), "verified")


if __name__ == "__main__":
    unittest.main()
