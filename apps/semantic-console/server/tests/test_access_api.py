from __future__ import annotations

import json
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

        status, unbound = self.app.request(
            "DELETE",
            f"/api/v1/access/policy-bindings/{account['id']}/{policy['id']}",
            authorization=self.authorization,
        )
        self.assertEqual((status, unbound["status"]), (200, "unbound"))
        status, accounts = self.app.request(
            "GET", "/api/v1/access/service-accounts", authorization=self.authorization
        )
        self.assertEqual(accounts["items"][0]["policyIds"], [])

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

    def test_audit_route_accepts_a_bounded_limit(self) -> None:
        for index in range(3):
            status, _ = self.app.request(
                "POST",
                "/api/v1/access/service-accounts",
                {"name": f"Audit account {index + 1}"},
                authorization=self.authorization,
            )
            self.assertEqual(status, 201)

        status, response = self.app.request(
            "GET", "/api/v1/access/audit?limit=2", authorization=self.authorization
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(response["items"]), 2)

        status, response = self.app.request(
            "GET", "/api/v1/access/audit?limit=not-a-number", authorization=self.authorization
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["code"], "INVALID_REQUEST")

    def test_legacy_console_routes_are_fail_closed_and_require_console_admin(self) -> None:
        status, response = self.app.request("GET", "/api/datasources")
        self.assertEqual((status, response["code"]), (401, "UNAUTHENTICATED"))
        status, response = self.app.request("POST", "/api/views/daily_sales/preview", {"limit": 10})
        self.assertEqual((status, response["code"]), (401, "UNAUTHENTICATED"))

        status, account = self.app.request(
            "POST",
            "/api/v1/access/service-accounts",
            {"name": "Query only"},
            authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        status, policy = self.app.request(
            "POST",
            "/api/v1/access/policies",
            {
                "name": "Query only",
                "document": {
                    "schemaVersion": 1,
                    "tools": ["query:execute"],
                    "tables": {"public.sales": {"effect": "allow", "columns": {"allow": ["id"]}}},
                },
            },
            authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        self.app.request(
            "POST",
            "/api/v1/access/policy-bindings",
            {"subjectId": account["id"], "policyId": policy["id"]},
            authorization=self.authorization,
        )
        status, issued = self.app.request(
            "POST",
            f"/api/v1/access/service-accounts/{account['id']}/keys",
            {"label": "query-only"},
            authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        status, response = self.app.request(
            "GET", "/api/datasources", authorization=f"Bearer {issued['apiKey']}"
        )
        self.assertEqual((status, response["code"]), (403, "FORBIDDEN"))

        status, response = self.app.request(
            "GET", "/api/datasources", authorization=self.authorization
        )
        self.assertEqual(status, 200)

    def test_console_admin_scope_is_restricted_to_the_current_project(self) -> None:
        status, account = self.app.request(
            "POST", "/api/v1/access/service-accounts", {"name": "Other project admin"},
            authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        status, policy = self.app.request(
            "POST",
            "/api/v1/access/policies",
            {
                "name": "Project console administrator",
                "document": {
                    "schemaVersion": 1,
                    "projects": ["different-project"],
                    "tools": ["console:admin"],
                    "tables": {},
                },
            },
            authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        self.app.request(
            "POST", "/api/v1/access/policy-bindings",
            {"subjectId": account["id"], "policyId": policy["id"]},
            authorization=self.authorization,
        )
        status, issued = self.app.request(
            "POST", f"/api/v1/access/service-accounts/{account['id']}/keys",
            {"label": "console"}, authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        delegated = f"Bearer {issued['apiKey']}"

        status, response = self.app.request("GET", "/api/datasources", authorization=delegated)
        self.assertEqual((status, response["code"]), (403, "FORBIDDEN"))

        status, _ = self.app.request(
            "PUT", f"/api/v1/access/policies/{policy['id']}",
            {
                "document": {
                    "schemaVersion": 1,
                    "projects": ["access-api"],
                    "tools": ["console:admin"],
                    "tables": {},
                }
            },
            authorization=self.authorization,
        )
        self.assertEqual(status, 200)
        status, _ = self.app.request("GET", "/api/datasources", authorization=delegated)
        self.assertEqual(status, 200)

    def test_console_and_access_administrator_capabilities_are_independent(self) -> None:
        issued_by_scope: dict[str, str] = {}
        for scope in ("console:admin", "access:admin"):
            status, account = self.app.request(
                "POST", "/api/v1/access/service-accounts", {"name": scope},
                authorization=self.authorization,
            )
            self.assertEqual(status, 201)
            status, policy = self.app.request(
                "POST", "/api/v1/access/policies",
                {
                    "name": scope,
                    "document": {
                        "schemaVersion": 1,
                        "projects": ["access-api"],
                        "tools": [scope],
                        "tables": {},
                    },
                },
                authorization=self.authorization,
            )
            self.assertEqual(status, 201)
            self.app.request(
                "POST", "/api/v1/access/policy-bindings",
                {"subjectId": account["id"], "policyId": policy["id"]},
                authorization=self.authorization,
            )
            status, issued = self.app.request(
                "POST", f"/api/v1/access/service-accounts/{account['id']}/keys",
                {"label": scope}, authorization=self.authorization,
            )
            self.assertEqual(status, 201)
            issued_by_scope[scope] = f"Bearer {issued['apiKey']}"

        status, console_capabilities = self.app.request(
            "GET", "/api/v1/auth/capabilities", authorization=issued_by_scope["console:admin"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            console_capabilities["capabilities"],
            {"console:admin": True, "access:admin": False},
        )
        status, _ = self.app.request(
            "GET", "/api/datasources", authorization=issued_by_scope["console:admin"]
        )
        self.assertEqual(status, 200)
        status, response = self.app.request(
            "GET", "/api/v1/access/users", authorization=issued_by_scope["console:admin"]
        )
        self.assertEqual((status, response["code"]), (403, "FORBIDDEN"))

        status, access_capabilities = self.app.request(
            "GET", "/api/v1/auth/capabilities", authorization=issued_by_scope["access:admin"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            access_capabilities["capabilities"],
            {"console:admin": False, "access:admin": True},
        )
        status, response = self.app.request(
            "GET", "/api/datasources", authorization=issued_by_scope["access:admin"]
        )
        self.assertEqual((status, response["code"]), (403, "FORBIDDEN"))
        status, _ = self.app.request(
            "GET", "/api/v1/access/users", authorization=issued_by_scope["access:admin"]
        )
        self.assertEqual(status, 200)

        status, response = self.app.request("GET", "/api/mcp-integration")
        self.assertEqual((status, response["code"]), (401, "UNAUTHENTICATED"))
        status, response = self.app.request(
            "GET", "/api/mcp-integration", authorization=issued_by_scope["access:admin"]
        )
        self.assertEqual((status, response["code"]), (403, "FORBIDDEN"))
        status, integration = self.app.request(
            "GET", "/api/mcp-integration", authorization=issued_by_scope["console:admin"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(integration["schemaVersion"], 2)

    def test_openapi_defaults_console_routes_to_bearer_and_marks_only_public_flows(self) -> None:
        specification = json.loads(
            (Path(__file__).resolve().parents[1] / "openapi.json").read_text(encoding="utf-8")
        )
        self.assertEqual(specification["security"], [{"RuntimeBearer": []}])
        public_operations = {
            (path, method)
            for path, item in specification["paths"].items()
            for method, operation in item.items()
            if method in {"get", "post", "put", "delete", "patch"}
            and operation.get("security") == []
        }
        self.assertEqual(
            public_operations,
            {
                ("/api/health", "get"),
                ("/api/v1/auth/providers", "get"),
                ("/api/v1/auth/device/start", "post"),
                ("/api/v1/auth/callback/{provider}", "get"),
                ("/api/v1/auth/device/token", "post"),
            },
        )

    def test_openapi_trace_v2_response_and_version_contracts(self) -> None:
        specification = json.loads(
            (Path(__file__).resolve().parents[1] / "openapi.json").read_text(encoding="utf-8")
        )
        self.assertEqual(specification["security"], [{"RuntimeBearer": []}])
        paths = specification["paths"]

        def response_schema(route: str, method: str, status: str) -> dict:
            return paths[route][method]["responses"][status]["content"]["application/json"]["schema"]

        expected = {
            ("/api/v1/traces/{id}/diagnosis", "get", "200"): "TraceDiagnosis",
            ("/api/v1/traces/compare", "get", "200"): "TraceComparison",
            ("/api/v1/traces/collector-status", "post", "201"): "TraceCollectorStatusReceipt",
            ("/api/v1/traces/collector-status", "get", "200"): "TraceCollectorStatusList",
            ("/api/v1/diagnostics/core/{coreTraceId}", "get", "200"): "CoreDiagnostic",
            ("/api/v1/diagnostics/test-datasets", "get", "200"): "RegressionTestDatasetList",
            ("/api/v1/diagnostics/test-datasets", "post", "201"): "RegressionTestDatasetRecord",
            ("/api/v1/diagnostics/test-datasets/{id}", "put", "200"): "RegressionTestDatasetRecord",
            ("/api/v1/diagnostics/test-datasets/{id}", "delete", "200"): "DeletedResource",
            ("/api/v1/diagnostics/regression-cases", "get", "200"): "RegressionCaseList",
            ("/api/v1/diagnostics/regression-cases", "post", "201"): "RegressionCaseRecord",
            ("/api/v1/diagnostics/regression-cases/{id}", "get", "200"): "RegressionCaseRecord",
            ("/api/v1/diagnostics/regression-cases/{id}", "put", "200"): "RegressionCaseRecord",
            ("/api/v1/diagnostics/regression-cases/{id}", "delete", "200"): "DeletedResource",
            ("/api/v1/diagnostics/regression-runs", "get", "200"): "RegressionRunList",
            ("/api/v1/diagnostics/regression-runs", "post", "202"): "RegressionRunRecord",
            ("/api/v1/diagnostics/regression-runs/{id}/cancel", "post", "202"): "RegressionRunRecord",
        }
        for (route, method, status), schema_name in expected.items():
            self.assertEqual(response_schema(route, method, status)["$ref"], f"#/components/schemas/{schema_name}")
            self.assertNotEqual(paths[route][method].get("security"), [])

        schemas = specification["components"]["schemas"]
        trace_filters = {item["name"] for item in paths["/api/v1/traces"]["get"]["parameters"]}
        self.assertTrue({"sourceSessionId", "subjectId", "createdAfter", "createdBefore", "status", "hasIssues"} <= trace_filters)
        comparison = schemas["TraceComparison"]
        side = comparison["properties"]["left"]
        self.assertEqual(side["properties"]["semanticVersions"]["type"], "array")
        self.assertTrue(side["properties"]["semanticVersion"]["nullable"])
        for field in ("model", "semanticVersion", "policyVersions"):
            self.assertTrue(comparison["properties"]["versionChanges"]["properties"][field]["nullable"])
        self.assertEqual(schemas["RegressionCaseV2"]["properties"]["conditions"]["maxProperties"], 100)
        self.assertEqual(paths["/api/v1/diagnostics/regression-cases"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["enable"]["enum"], [False])
        export_parameter = paths["/api/v1/diagnostics/regression-cases/export"]["get"]["parameters"][0]
        self.assertEqual(export_parameter["name"], "schemaVersion")
        self.assertEqual(export_parameter["schema"]["enum"], ["1", "2"])
        self.assertEqual(schemas["CoreDiagnostic"]["properties"]["status"]["enum"], ["success", "failure", "cancelled"])
        self.assertEqual(schemas["TraceCollectorStatusV1"]["properties"]["dropReasons"]["required"], ["expired", "capacity", "invalid", "conflict"])
        self.assertEqual(schemas["RegressionRunRecord"]["properties"]["status"]["enum"], ["queued", "running", "completed", "cancelled", "interrupted"])
        self.assertEqual(schemas["RegressionRunRecord"]["properties"]["results"]["items"]["properties"]["status"]["enum"], ["passed", "failed", "blocked", "inconclusive", "cancelled"])

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

    def test_management_api_creates_and_updates_version_two_permission_lookup_policy(self) -> None:
        document = {
            "schemaVersion": 2,
            "datasourceId": "source-a",
            "projects": ["access-api"],
            "tools": ["query:execute"],
            "tables": {
                "public.sales": {
                    "effect": "allow",
                    "tenantField": "organization_id",
                    "rows": [{
                        "field": "employee_id",
                        "operator": "permissionLookup",
                        "valueFrom": "subject.attributes.employeeId",
                        "lookup": {
                            "table": "auth.employee_permission",
                            "principalField": "employee",
                            "targetField": "subordinate",
                            "organizationField": "organization_id",
                        },
                        "includeSelf": False,
                    }],
                }
            },
        }

        status, created = self.app.request(
            "POST", "/api/v1/access/policies",
            {"name": "employee lookup", "document": document},
            authorization=self.authorization,
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["document"]["schemaVersion"], 2)

        document["tables"]["public.sales"]["rows"][0]["includeSelf"] = True
        status, updated = self.app.request(
            "PUT", f"/api/v1/access/policies/{created['id']}",
            {"document": document},
            authorization=self.authorization,
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["version"], 2)
        self.assertTrue(updated["document"]["tables"]["public.sales"]["rows"][0]["includeSelf"])

if __name__ == "__main__":
    unittest.main()
