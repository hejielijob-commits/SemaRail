from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.app import create_app
from server.project import ProjectStore
from server.runtime_rpc import RuntimeRpcGateway
from server.service import SemanticConsoleService


class FakeValidator:
    def health(self):
        return {"available": True}

    def validate(self, _project_dir):
        return {"valid": True, "errors": [], "warnings": [], "errorCount": 0, "warningCount": 0}

    def build(self, _project_dir):
        return {"models": []}


class AccessControlAdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-access-api-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        project_dir = root / "project"
        project_dir.mkdir()
        project_dir.joinpath("wren_project.yml").write_text(
            "schema_version: 5\nname: access-api\ndata_source: postgres\n", encoding="utf-8"
        )
        project = ProjectStore(project_dir, state_dir=root / "state", validator=FakeValidator())
        self.token = "admin-token-that-is-at-least-thirty-two-characters"
        gateway = RuntimeRpcGateway(project, dispatcher=None, auth_token=self.token)
        self.app = create_app(SemanticConsoleService(project), runtime_rpc=gateway)
        self.authorization = f"Bearer {self.token}"

    def test_bootstrap_admin_can_create_account_policy_binding_and_key(self) -> None:
        status, account = self.app.request(
            "POST", "/api/v1/access/service-accounts",
            {"name": "Region A", "attributes": {"regionCodes": ["CN-JIA"]}},
            authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        status, policy = self.app.request(
            "POST", "/api/v1/access/policies",
            {
                "name": "Region A sales",
                "document": {
                    "schemaVersion": 1,
                    "projects": ["access-api"],
                    "tools": ["semantic:read", "query:execute"],
                    "tables": {
                        "sales.orders": {
                            "effect": "allow",
                            "rows": [{"field": "region_code", "operator": "in", "valueFrom": "subject.attributes.regionCodes"}],
                        }
                    },
                },
            },
            authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        status, _ = self.app.request(
            "POST", "/api/v1/access/policy-bindings",
            {"subjectId": account["id"], "policyId": policy["id"]},
            authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        status, issued = self.app.request(
            "POST", f"/api/v1/access/service-accounts/{account['id']}/keys",
            {"label": "codex"}, authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        self.assertTrue(issued["apiKey"].startswith("sr_live_"))

        status, accounts = self.app.request(
            "GET", "/api/v1/access/service-accounts", authorization=self.authorization
        )
        self.assertEqual(status, 200)
        self.assertEqual(accounts["items"][0]["attributes"]["regionCodes"], ["CN-JIA"])
        self.assertEqual(accounts["items"][0]["policyIds"], [policy["id"]])
        self.assertNotIn("apiKey", accounts["items"][0]["credentials"][0])

        status, updated = self.app.request(
            "PUT", f"/api/v1/access/policies/{policy['id']}",
            {"document": {"schemaVersion": 1, "projects": ["access-api"], "tools": ["query:execute"], "tables": {}}},
            authorization=self.authorization,
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["version"], 2)

        status, changed_account = self.app.request(
            "PUT", f"/api/v1/access/service-accounts/{account['id']}",
            {"attributes": {"regionCodes": ["CN-YI"]}}, authorization=self.authorization,
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed_account["attributes"]["regionCodes"], ["CN-YI"])
        status, policies = self.app.request(
            "GET", "/api/v1/access/policies", authorization=self.authorization,
        )
        self.assertEqual(status, 200)
        self.assertEqual(policies["items"][0]["version"], 2)

        old_key = issued["apiKey"]
        status, rotated = self.app.request(
            "POST", f"/api/v1/access/credentials/{issued['credential']['id']}/rotate",
            {"label": "codex-rotated"}, authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        self.assertNotEqual(rotated["apiKey"], old_key)
        with self.assertRaises(Exception):
            self.app.runtime_rpc.access_control.authenticate(f"Bearer {old_key}")
        self.assertEqual(
            self.app.runtime_rpc.access_control.authenticate(f"Bearer {rotated['apiKey']}").subject.id,
            account["id"],
        )

    def test_management_routes_require_admin_scope(self) -> None:
        status, response = self.app.request("GET", "/api/v1/access/service-accounts")
        self.assertEqual(status, 401)
        self.assertEqual(response["code"], "UNAUTHENTICATED")

    def test_management_api_rejects_invalid_policy_before_persistence(self) -> None:
        status, response = self.app.request(
            "POST", "/api/v1/access/policies",
            {
                "name": "unsafe",
                "document": {
                    "schemaVersion": 1,
                    "tools": ["query:execute"],
                    "tables": {"sales": {"rows": [{"field": "region", "operator": "sql", "valueFrom": "request.value"}]}},
                },
            },
            authorization=self.authorization,
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["code"], "INVALID_POLICY")

        status, response = self.app.request(
            "POST", "/api/v1/access/policies",
            {"name": "ambiguous", "document": {"schemaVersion": 1, "tables": {"sales": {"effect": "allow"}}}},
            authorization=self.authorization,
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["code"], "INVALID_POLICY")


if __name__ == "__main__":
    unittest.main()
