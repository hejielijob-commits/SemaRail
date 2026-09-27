from __future__ import annotations

import json
import unittest
from typing import Any

from sidecar.dispatch import Dispatcher
from sidecar.semantic_index import SemanticDocument
from sidecar.semantic_policy import semantic_document_visible


def request(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": "2",
        "id": "context-v2",
        "method": "context.ask",
        "params": params,
        "traceId": "trace-context-v2",
    }


class Provider:
    def ask_v2(self, _params: object) -> dict[str, Any]:
        return {
            "schemaVersion": 2,
            "projectRevision": "sha256:current",
            "schema": {
                "models": [
                    {
                        "name": "orders",
                        "table": "public.orders",
                        "properties": {"businessDomain": "Commerce", "visible": True},
                        "columns": [
                            {
                                "name": "order_id", "type": "INTEGER", "description": "safe",
                                "semanticRole": "dimension",
                                "properties": {"format": "integer", "grain": "order"},
                            },
                            {"name": "secret", "type": "TEXT"},
                        ],
                    },
                    {
                        "name": "payroll",
                        "table": "public.payroll",
                        "columns": [{"name": "salary", "type": "DECIMAL"}],
                    },
                ]
            },
            "relationships": [],
            "metrics": [{
                "name": "order_count", "kind": "measure", "expression": "COUNT(order_id)",
                "type": "BIGINT", "model": "orders", "cube": "orders_cube",
                "properties": {"unit": "orders", "visible": True},
                "referencedModels": ["orders"], "referencedColumns": ["orders.order_id"],
            }],
            "rules": [
                {
                    "id": "rule:safe",
                    "text": "Use order_id for order grain.",
                    "referencedModels": ["orders"],
                    "referencedColumns": ["orders.order_id"],
                    "effectiveFrom": "2026-01-01",
                    "allowedRoles": ["analyst"],
                },
                {
                    "id": "rule:unbound",
                    "text": "Use payroll salary.",
                    "referencedModels": [],
                    "referencedColumns": [],
                },
                {
                    "id": "rule:false-binding",
                    "text": "Use payroll.salary and orders.secret.",
                    "referencedModels": ["orders"],
                    "referencedColumns": ["orders.order_id"],
                },
            ],
            "sqlExamples": [
                {
                    "id": "sql:safe",
                    "question": "Order ids",
                    "sql": "SELECT order_id FROM orders",
                    "referencedModels": ["orders"],
                    "referencedColumns": ["orders.order_id"],
                    "dataSource": "warehouse",
                    "roles": ["analyst"],
                    "version": "v2",
                },
                {
                    "id": "sql:denied",
                    "question": "Payroll",
                    "sql": "SELECT salary FROM payroll",
                    "referencedModels": ["payroll"],
                    "referencedColumns": ["payroll.salary"],
                },
            ],
            "views": [],
            "indexStatus": {"status": "ready", "backend": "hybrid", "path": "C:/private/index"},
            "retrievalTrace": [
                {
                    "source": "schema",
                    "retrievalType": "vector",
                    "relevance": 0.8,
                    "reasonCode": "vectorMatch",
                    "projectRevision": "sha256:current",
                    "authorizationFiltered": False,
                    "query": "salary secret should not cross",
                }
            ],
        }


class ContextV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = {
            "schemaVersion": 1,
            "defaultEffect": "deny",
            "tables": {
                "public.orders": {
                    "allowedColumns": ["order_id"],
                    "deniedColumns": ["secret"],
                },
            },
            "policyVersions": ["policy:current"],
        }

    def test_v2_is_versioned_partitioned_and_budgeted(self) -> None:
        response = Dispatcher(context_provider=Provider()).dispatch(request({
            "projectDir": "project",
            "question": "orders",
            "contextVersion": 2,
            "budgets": {"topK": {"rules": 1}},
        }))
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertEqual(result["schemaVersion"], 2)
        self.assertIn("schema", result)
        self.assertNotIn("table", result["schema"]["models"][0])
        self.assertNotIn("public.orders", json.dumps(result))
        self.assertEqual(len(result["rules"]), 1)
        self.assertEqual(result["budgets"]["topK"]["rules"], 1)
        self.assertEqual(result["indexStatus"], {"status": "ready", "backend": "hybrid"})
        self.assertNotIn("query", json.dumps(result))
        self.assertNotIn("path", result["indexStatus"])

    def test_v2_rejects_unknown_fields_and_versions(self) -> None:
        unknown = Dispatcher(context_provider=Provider()).dispatch(request({
            "projectDir": "project", "question": "orders", "contextVersion": 2, "unexpected": True,
        }))
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["error"]["code"], "INVALID_PARAMS")
        unsupported = Dispatcher(context_provider=Provider()).dispatch(request({
            "projectDir": "project", "question": "orders", "contextVersion": 3,
        }))
        self.assertFalse(unsupported["ok"])
        self.assertEqual(unsupported["error"]["code"], "UNSUPPORTED_PROTOCOL")

        class FutureProvider(Provider):
            def ask_v2(self, _params: object) -> dict[str, Any]:
                value = super().ask_v2(_params)
                value["schemaVersion"] = 3
                return value

        future = Dispatcher(context_provider=FutureProvider()).dispatch(request({
            "projectDir": "project", "question": "orders", "contextVersion": 2,
        }))
        self.assertFalse(future["ok"])
        self.assertEqual(future["error"]["code"], "UNSUPPORTED_PROTOCOL")

    def test_restricted_v2_retains_only_bound_safe_knowledge(self) -> None:
        params = {
            "projectDir": "project",
            "question": "orders",
            "contextVersion": 2,
            "authorizationPolicy": self.policy,
        }
        response = Dispatcher(context_provider=Provider()).dispatch(request(params))
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertEqual([item["id"] for item in result["rules"]], ["rule:safe"])
        self.assertEqual([item["id"] for item in result["sqlExamples"]], ["sql:safe"])
        self.assertEqual(result["rules"][0]["effectiveFrom"], "2026-01-01")
        self.assertEqual(result["rules"][0]["allowedRoles"], ["analyst"])
        self.assertEqual(result["sqlExamples"][0]["dataSource"], "warehouse")
        self.assertEqual(result["sqlExamples"][0]["roles"], ["analyst"])
        self.assertEqual(result["sqlExamples"][0]["version"], "v2")
        self.assertEqual([item["name"] for item in result["schema"]["models"]], ["orders"])
        self.assertNotIn("table", result["schema"]["models"][0])
        self.assertEqual(result["schema"]["models"][0]["properties"], {"businessDomain": "Commerce", "visible": True})
        self.assertEqual(result["schema"]["models"][0]["columns"], [{
            "name": "order_id", "type": "INTEGER", "semanticRole": "dimension",
            "properties": {"format": "integer", "grain": "order"},
        }])
        self.assertEqual(result["metrics"][0]["kind"], "measure")
        self.assertEqual(result["metrics"][0]["properties"], {"unit": "orders", "visible": True})
        self.assertTrue(result["retrievalTrace"][0]["authorizationFiltered"])
        self.assertNotIn("salary", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_restricted_v2_keeps_only_catalog_checked_descriptions(self) -> None:
        class CatalogProvider(Provider):
            def ask_v2(self, params: object) -> dict[str, Any]:
                value = super().ask_v2(params)
                value["_authorizationCatalog"] = [
                    {"name": "orders", "table": "physical_orders", "columns": ["order_id", "secret"]},
                    {"name": "payroll", "table": "payroll_private", "columns": ["salary"]},
                ]
                columns = value["schema"]["models"][0]["columns"]
                columns[0]["description"] = "Stored percentage points from 0 to 100."
                return value

        response = Dispatcher(context_provider=CatalogProvider()).dispatch(request({
            "projectDir": "project", "question": "orders", "contextVersion": 2,
            "authorizationPolicy": self.policy,
        }))
        self.assertTrue(response["ok"])
        self.assertEqual(
            response["result"]["schema"]["models"][0]["columns"][0]["description"],
            "Stored percentage points from 0 to 100.",
        )
        self.assertEqual([item["id"] for item in response["result"]["sqlExamples"]], ["sql:safe"])
        self.assertNotIn("_authorizationCatalog", response["result"])

        class LeakingProvider(CatalogProvider):
            def ask_v2(self, params: object) -> dict[str, Any]:
                value = super().ask_v2(params)
                model = value["schema"]["models"][0]
                model["description"] = "Use payroll_private for joins."
                model["columns"][0]["description"] = "Join to orders.secret or payroll.salary."
                return value

        leaked = Dispatcher(context_provider=LeakingProvider()).dispatch(request({
            "projectDir": "project", "question": "orders", "contextVersion": 2,
            "authorizationPolicy": self.policy,
        }))
        self.assertTrue(leaked["ok"])
        self.assertNotIn("description", leaked["result"]["schema"]["models"][0])
        self.assertNotIn("description", leaked["result"]["schema"]["models"][0]["columns"][0])
        self.assertNotIn("payroll_private", json.dumps(leaked["result"]))

        scoped_policy = {
            **self.policy,
            "tables": {
                "public.orders": {
                    **self.policy["tables"]["public.orders"],
                    "rowFilter": {
                        "field": "order_id", "operator": "permissionLookup",
                        "values": ["user-private"],
                        "lookup": {"table": "auth.order_permissions"},
                        "organizationValue": "org-private",
                    },
                },
            },
        }

        class RowLeakProvider(CatalogProvider):
            def ask_v2(self, params: object) -> dict[str, Any]:
                value = super().ask_v2(params)
                value["schema"]["models"][0]["columns"][0]["description"] = (
                    "Use org-private from auth.order_permissions."
                )
                return value

        row_leak = Dispatcher(context_provider=RowLeakProvider()).dispatch(request({
            "projectDir": "project", "question": "orders", "contextVersion": 2,
            "authorizationPolicy": scoped_policy,
        }))
        self.assertTrue(row_leak["ok"])
        self.assertNotIn("description", row_leak["result"]["schema"]["models"][0]["columns"][0])
        self.assertNotIn("org-private", json.dumps(row_leak["result"]))

    def test_restricted_documents_are_filtered_before_retrieval_scoring(self) -> None:
        safe = SemanticDocument(
            id="column:orders.order_id", kind="column", projectRevision="r1",
            model="orders", referencedModels=["orders"], referencedColumns=["orders.order_id"],
        )
        denied = SemanticDocument(
            id="column:orders.secret", kind="column", projectRevision="r1",
            model="orders", referencedModels=["orders"], referencedColumns=["orders.secret"],
        )
        aggregate = SemanticDocument(
            id="model:orders", kind="model", projectRevision="r1", model="orders",
            referencedModels=["orders"], referencedColumns=["orders.order_id", "orders.secret"],
        )
        unbound = SemanticDocument(id="rule:free-text", kind="rule", projectRevision="r1")
        self.assertTrue(semantic_document_visible(safe, self.policy))
        self.assertFalse(semantic_document_visible(denied, self.policy))
        self.assertFalse(semantic_document_visible(aggregate, self.policy))
        self.assertFalse(semantic_document_visible(unbound, self.policy))

    def test_compiled_policy_reaches_native_v2_retrieval(self) -> None:
        captured: dict[str, Any] = {}

        class CapturingProvider(Provider):
            def ask_v2(self, params: object) -> dict[str, Any]:
                assert isinstance(params, dict)
                captured.update(params)
                return super().ask_v2(params)

        response = Dispatcher(context_provider=CapturingProvider()).dispatch(request({
            "projectDir": "project", "question": "orders", "contextVersion": 2,
            "authorizationPolicy": self.policy,
        }))
        self.assertTrue(response["ok"])
        self.assertEqual(captured["authorizationPolicy"], self.policy)


if __name__ == "__main__":
    unittest.main()
