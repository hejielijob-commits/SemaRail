"""Mandatory period checks at the authenticated Core query dispatch boundary."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from server.business_verification import RULES_VERSION
from server.service import SemanticConsoleService
from server.tests import test_runtime_rpc as fixtures
from server.tests.test_business_verification import FakeIndexLifecycle


class BusinessPeriodExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch("server.project._default_semantic_index_lifecycle", return_value=FakeIndexLifecycle()):
            fixtures.RuntimeRpcTests.setUp(self)
        model_dir = self.project.project_dir / "models" / "orders"
        model_dir.mkdir(parents=True)
        (model_dir / "metadata.yml").write_text(
            "name: orders\ntable_reference: {schema: public, table: orders}\ncolumns:\n"
            "  - {name: amount, type: DECIMAL}\n  - {name: review_date, type: DATE}\n",
            encoding="utf-8",
        )
        SemanticConsoleService(self.project).create_rule({
            "title": "Confirmed review period", "content": "Confirm the review period before execution.",
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "average", "kind": "metric", "model": "orders", "column": "amount", "aggregation": "avg"},
                {"id": "period", "kind": "timeFilter", "model": "orders", "column": "review_date",
                 "conditionKey": "reviewPeriod", "valueType": "dateRange", "confirmation": "confirmed"},
                {"id": "trend", "kind": "timeGrouping", "model": "orders", "column": "review_date",
                 "mode": "original", "whenGrouped": True, "conditionKey": "trend"},
            ]},
        })
        self.project.publish()
        self.sql = (
            "SELECT review_date, AVG(amount) FROM orders "
            "WHERE review_date BETWEEN DATE '2024-07-01' AND DATE '2024-09-30' GROUP BY review_date"
        )

    def rpc(self, method, params, version="3"):
        return self.gateway.dispatch({
            "protocolVersion": version, "id": "period-boundary", "method": method, "params": params,
        }, authorization=self.authorization)

    def prepare(self, confirmed=False):
        return self.rpc("query.prepare", {
            "question": "Average trend during the selected period?", "semanticSql": self.sql,
            "conditions": {"reviewPeriod": {"start": "2024-07-01", "end": "2024-09-30"}} if confirmed else {},
            "confirmedConditions": ["reviewPeriod"] if confirmed else [],
        })

    def test_missing_period_blocks_direct_and_prepared_execution_for_all_protocol_versions(self):
        status, prepared = self.prepare()
        self.assertEqual(status, 200)
        self.assertNotEqual(prepared["result"]["status"], "ready")
        evidence = prepared["result"]["businessEvidence"]
        self.assertEqual(evidence["confirmations"]["reviewPeriod"], "missing")
        self.assertEqual(next(c for c in evidence["checks"] if c["id"].endswith(":period"))["status"], "unverified")
        self.assertEqual(next(c for c in evidence["checks"] if c["id"].endswith(":trend"))["status"], "passed")
        before = len(self.dispatcher.requests)
        for version in ("1", "2", "3"):
            for preparation_id in (None, prepared["result"]["preparationId"]):
                with self.subTest(version=version, prepared=preparation_id is not None):
                    params = {"question": "Average trend?", "semanticSql": self.sql, "queryId": "q-blocked-period"}
                    if preparation_id is not None:
                        params["preparationId"] = preparation_id
                    status, response = self.rpc("query.run", params, version)
                    self.assertEqual(status, 409)
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["error"]["code"], "CLARIFICATION_REQUIRED")
                    self.assertEqual(len(self.dispatcher.requests), before)

    def test_confirmed_period_dispatches_and_returns_core_evidence(self):
        status, prepared = self.prepare(confirmed=True)
        self.assertEqual(status, 200)
        self.assertEqual(prepared["result"]["status"], "ready")
        status, response = self.rpc("query.run", {
            "question": "Average trend?", "semanticSql": self.sql, "queryId": "q-confirmed-period",
            "preparationId": prepared["result"]["preparationId"],
        })
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(sum(r["method"] == "query.run" for r in self.dispatcher.requests), 1)
        status, response = self.rpc("query.evidence", {"queryId": "q-confirmed-period"})
        self.assertEqual(status, 200)
        evidence = response["result"]
        self.assertEqual(evidence["rulesVersion"], RULES_VERSION)
        self.assertEqual(evidence["confirmations"]["reviewPeriod"], "confirmed")
        self.assertEqual(evidence["time"]["source"], "confirmed")
        self.assertTrue(all(c["status"] == "passed" for c in evidence["checks"] if c["mandatory"]))

    def test_old_verifier_preparation_cannot_dispatch_even_with_confirmed_period(self):
        _, prepared = self.prepare(confirmed=True)
        preparation_id = prepared["result"]["preparationId"]
        with self.gateway.access_control._connect() as connection:
            row = connection.execute("SELECT business_evidence_json FROM query_preparations WHERE id=?", (preparation_id,)).fetchone()
            template = json.loads(row["business_evidence_json"])
            template["rulesVersion"] = "business-trust-v1"
            connection.execute("UPDATE query_preparations SET business_evidence_json=? WHERE id=?", (json.dumps(template), preparation_id))
        before = len(self.dispatcher.requests)
        status, response = self.rpc("query.run", {
            "question": "Average trend?", "semanticSql": self.sql, "queryId": "q-old-period-preparation",
            "preparationId": preparation_id,
        })
        self.assertEqual(status, 409)
        self.assertEqual(response["error"]["code"], "CLARIFICATION_REQUIRED")
        self.assertEqual(len(self.dispatcher.requests), before)


if __name__ == "__main__":
    unittest.main()
