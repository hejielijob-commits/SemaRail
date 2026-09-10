from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app import create_app
from server.diagnostics import DiagnosticError
from server.models import DatasourceRecord
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


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests = []

    def dispatch(self, request):
        self.requests.append(request)
        if request["method"] == "health":
            result = {"status": "ok", "protocolVersion": "1", "wrenAvailable": True}
        elif request["method"] == "project.validate":
            result = {"valid": True, "projectRevision": "sha256:test"}
        elif request["method"] == "query.run":
            result = {
                "schemaVersion": 1,
                "queryId": request["params"]["queryId"],
                "status": "success",
                "semanticSql": request["params"]["semanticSql"],
                "columns": [{"name": "value", "type": "BIGINT", "semanticRole": "measure"}],
                "previewRows": [{"value": "1"}],
                "stats": {"returnedRows": 1, "durationMs": 1, "truncated": False},
            }
        else:
            result = {"accepted": True}
        return {"protocolVersion": "1", "id": request["id"], "ok": True, "result": result}


class RuntimeRpcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-runtime-rpc-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        project_dir = root / "project"
        project_dir.mkdir()
        project_dir.joinpath("wren_project.yml").write_text(
            "schema_version: 5\nname: runtime-test\ndata_source: postgres\n",
            encoding="utf-8",
        )
        self.project = ProjectStore(project_dir, state_dir=root / "state", validator=FakeValidator())
        self.datasource_id = "runtime-datasource"
        self.project.datasource_records()[self.datasource_id] = DatasourceRecord(
            self.datasource_id, "Runtime warehouse", "postgres", {"database": "runtime"}
        )
        self.project.active_datasource_id = self.datasource_id
        self.project.save_datasources()
        self.dispatcher = RecordingDispatcher()
        self.token = "test-token-that-is-at-least-thirty-two-characters"
        self.admin_authorization = f"Bearer {self.token}"
        self.gateway = RuntimeRpcGateway(self.project, self.dispatcher, auth_token=self.token)
        runtime_account = self.gateway.access_control.create_service_account(
            "Runtime test Agent", attributes={"regionCodes": ["CN-JIA"]}
        )
        runtime_policy = self.gateway.access_control.create_policy(
            "Runtime test access",
            {
                "schemaVersion": 1,
                "datasourceId": self.datasource_id,
                "projects": ["runtime-test"],
                "tools": [
                    "runtime:health", "project:validate", "semantic:read",
                    "query:plan", "query:execute", "query:cancel",
                ],
                "tables": {
                    "public.orders": {"effect": "allow"},
                    "public.sales": {"effect": "allow"},
                },
            },
        )
        self.gateway.access_control.bind_policy(runtime_account.id, runtime_policy["id"])
        runtime_key = self.gateway.access_control.issue_api_key(runtime_account.id)
        self.authorization = f"Bearer {runtime_key['apiKey']}"

    def test_health_exposes_stable_core_handshake(self) -> None:
        status, response = self.gateway.dispatch(
            {"protocolVersion": "1", "id": "health-1", "method": "health", "params": {}},
            authorization=self.authorization,
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["service"], "semarail-core")
        self.assertEqual(response["result"]["apiVersion"], "1")
        self.assertTrue(response["result"]["capabilities"]["queryCancellation"])
        self.assertTrue(response["result"]["capabilities"]["governedQuery"])
        self.assertEqual(response["result"]["readiness"]["governedQuery"], "ready")

    def test_v2_rpc_adds_structured_errors_and_core_trace(self) -> None:
        status, response = self.gateway.dispatch(
            {"protocolVersion": "2", "id": "health-v2", "method": "health", "params": {}},
            authorization=self.authorization,
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["protocolVersion"], "2")
        self.assertEqual(response["result"]["protocolVersion"], "2")
        self.assertEqual(self.dispatcher.requests[-1]["protocolVersion"], "2")
        self.assertTrue(self.dispatcher.requests[-1]["traceId"].startswith("trace-"))

        denied_status, denied = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "bootstrap-v2",
                "method": "query.run",
                "params": {"question": "Revenue", "semanticSql": "SELECT * FROM orders", "queryId": "q-v2"},
            },
            authorization=f"Bearer {self.token}",
        )

        self.assertEqual(denied_status, 403)
        self.assertEqual(denied["protocolVersion"], "2")
        self.assertEqual(denied["error"]["code"], "POLICY_DENIED")
        self.assertEqual(denied["error"]["reasonCode"], "TOOL_PERMISSION_REQUIRED")
        self.assertEqual(denied["error"]["resources"], [{"kind": "tool", "name": "query.run"}])
        self.assertEqual(denied["error"]["requiredPermissions"], ["query.run"])
        self.assertEqual(denied["error"]["origin"], "semarail-policy")
        self.assertTrue(denied["error"]["traceId"].startswith("trace-"))

    def test_v2_rpc_preserves_sidecar_column_denial_and_owns_trace(self) -> None:
        class ColumnDeniedDispatcher:
            def dispatch(self, request):
                return {
                    "protocolVersion": "2",
                    "id": request["id"],
                    "ok": False,
                    "error": {
                        "code": "POLICY_DENIED",
                        "phase": "authorization",
                        "message": "query denied by data access policy",
                        "retryable": False,
                        "reasonCode": "COLUMN_PERMISSION_REQUIRED",
                        "resources": [{"kind": "column", "name": "hr.compensation.salary"}],
                        "requiredPermissions": ["column:read"],
                        "suggestion": "Remove the field or ask an administrator to update the column access policy.",
                        "origin": "semarail-policy",
                        "traceId": request["traceId"],
                    },
                }

        self.gateway.dispatcher = ColumnDeniedDispatcher()
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "column-denied-v2",
                "method": "query.run",
                "params": {
                    "question": "Salary",
                    "semanticSql": "SELECT salary FROM compensation",
                    "queryId": "q-column-denied",
                },
            },
            authorization=self.authorization,
        )

        self.assertEqual(status, 200)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["reasonCode"], "COLUMN_PERMISSION_REQUIRED")
        self.assertEqual(response["error"]["resources"], [{"kind": "column", "name": "hr.compensation.salary"}])
        self.assertTrue(response["error"]["traceId"].startswith("trace-"))

        captured = self.gateway.diagnostics.list_diagnostics(
            organization_id=self.gateway.access_control.authenticate(self.authorization).subject.organization_id,
            project_id="runtime-test",
        )
        item = captured["items"][0]
        caller = self.gateway.access_control.authenticate(self.authorization)
        self.assertEqual(item["queryId"], "q-column-denied")
        self.assertEqual(item["question"], "Salary")
        self.assertEqual(item["projectId"], "runtime-test")
        self.assertEqual(item["datasourceId"], self.datasource_id)
        self.assertEqual(item["subjectId"], caller.subject.id)
        self.assertEqual(item["credentialId"], caller.credential_id)
        self.assertEqual((item["transport"], item["method"], item["stage"]), ("runtime-rpc", "query.run", "authorization"))
        self.assertTrue(item["semanticVersion"].startswith("sha256:"))
        self.assertTrue(item["policyVersions"])
        self.assertEqual(item["error"]["reasonCode"], "COLUMN_PERMISSION_REQUIRED")
        self.assertEqual(item["error"]["traceId"], item["traceId"])
        self.assertGreaterEqual(item["durationMs"], 0)

    def test_missing_row_attribute_is_specific_and_never_reaches_sidecar(self) -> None:
        account = self.gateway.access_control.create_service_account("Missing region Agent")
        policy = self.gateway.access_control.create_policy(
            "Region-bound access",
            {
                "schemaVersion": 1,
                "datasourceId": self.datasource_id,
                "projects": ["runtime-test"],
                "tools": ["query:execute"],
                "tables": {
                    "public.sales": {
                        "effect": "allow",
                        "rows": [{
                            "field": "region_code",
                            "operator": "in",
                            "valueFrom": "subject.attributes.regionCodes",
                        }],
                    },
                },
            },
        )
        self.gateway.access_control.bind_policy(account.id, policy["id"])
        key = self.gateway.access_control.issue_api_key(account.id)
        before = len(self.dispatcher.requests)

        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "missing-region-attribute",
                "method": "query.run",
                "params": {
                    "question": "Revenue by region",
                    "semanticSql": "SELECT revenue FROM Sales",
                    "queryId": "q-missing-region",
                },
            },
            authorization=f"Bearer {key['apiKey']}",
        )

        self.assertEqual(status, 403)
        self.assertEqual(response["error"]["reasonCode"], "ROW_ATTRIBUTE_MISSING")
        self.assertEqual(response["error"]["resources"], [{"kind": "attribute", "name": "regionCodes"}])
        self.assertEqual(response["error"]["requiredPermissions"], ["subject.attribute:regionCodes"])
        self.assertEqual(response["error"]["origin"], "semarail-policy")
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual(len(self.dispatcher.requests), before)

    def test_success_records_metadata_without_query_content(self) -> None:
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "successful-diagnostic",
                "method": "query.run",
                "params": {
                    "question": "Revenue",
                    "semanticSql": "SELECT * FROM orders",
                    "queryId": "q-successful-diagnostic",
                },
            },
            authorization=self.authorization,
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        captured = self.gateway.diagnostics.list_diagnostics(
            organization_id=self.gateway.access_control.authenticate(self.authorization).subject.organization_id,
            project_id="runtime-test",
        )["items"][0]
        self.assertEqual(captured["status"], "success")
        self.assertIsNone(captured["question"])
        self.assertIsNone(captured["semanticSql"])

    def test_planning_failure_is_automatically_captured_with_available_sql(self) -> None:
        class PlanningFailureDispatcher:
            def dispatch(self, request):
                return {
                    "protocolVersion": "2",
                    "id": request["id"],
                    "ok": False,
                    "error": {
                        "code": "INVALID_QUERY",
                        "phase": "planning",
                        "message": "semantic query could not be planned",
                        "retryable": False,
                        "reasonCode": "SEMANTIC_PARSE_FAILED",
                        "resources": [],
                        "requiredPermissions": [],
                        "suggestion": "Check model and field names.",
                        "origin": "sidecar",
                        "traceId": request["traceId"],
                    },
                }

        self.gateway.dispatcher = PlanningFailureDispatcher()
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "planning-failure",
                "method": "query.dryPlan",
                "params": {"semanticSql": "SELECT missing_measure FROM orders"},
            },
            authorization=self.authorization,
        )

        self.assertEqual(status, 200)
        self.assertFalse(response["ok"])
        captured = self.gateway.diagnostics.list_diagnostics(
            organization_id=self.gateway.access_control.authenticate(self.authorization).subject.organization_id,
            project_id="runtime-test",
        )["items"][0]
        self.assertEqual((captured["method"], captured["stage"]), ("query.dryPlan", "planning"))
        self.assertIsNone(captured["question"])
        self.assertEqual(captured["semanticSql"], "SELECT missing_measure FROM orders")
        self.assertEqual(captured["error"]["reasonCode"], "SEMANTIC_PARSE_FAILED")
        self.assertEqual(captured["error"]["traceId"], captured["traceId"])

    def test_retry_creates_an_independent_trace_linked_to_the_original_question(self) -> None:
        class FailedQueryDispatcher:
            def __init__(self) -> None:
                self.requests = []

            def dispatch(self, request):
                self.requests.append(request)
                return {
                    "protocolVersion": "2",
                    "id": request["id"],
                    "ok": False,
                    "error": {
                        "code": "POLICY_DENIED",
                        "phase": "authorization",
                        "message": "query denied by data access policy",
                        "retryable": False,
                        "reasonCode": "TABLE_PERMISSION_REQUIRED",
                        "resources": [{"kind": "table", "name": "hr.compensation"}],
                        "requiredPermissions": ["table:read"],
                        "suggestion": "Ask an administrator to update the table access policy.",
                        "origin": "semarail-policy",
                        "traceId": request["traceId"],
                    },
                }

        dispatcher = FailedQueryDispatcher()
        self.gateway.dispatcher = dispatcher
        original_question = "What is the compensation total?"
        for attempt in (1, 2):
            params = {
                "question": original_question,
                "semanticSql": "SELECT SUM(salary) FROM compensation",
                "queryId": f"q-retry-{attempt}",
            }
            if attempt == 2:
                params["retryOfQueryId"] = "q-retry-1"
            status, response = self.gateway.dispatch(
                {
                    "protocolVersion": "2",
                    "id": f"retry-attempt-{attempt}",
                    "method": "query.run",
                    "params": params,
                },
                authorization=self.authorization,
            )
            self.assertEqual(status, 200)
            self.assertFalse(response["ok"])

        captured = self.gateway.diagnostics.list_diagnostics(
            organization_id=self.gateway.access_control.authenticate(self.authorization).subject.organization_id,
            project_id="runtime-test",
        )["items"]
        retry_records = [item for item in captured if item["queryId"] in {"q-retry-1", "q-retry-2"}]
        self.assertEqual(len(retry_records), 2)
        self.assertEqual({item["queryId"] for item in retry_records}, {"q-retry-1", "q-retry-2"})
        self.assertEqual({item["question"] for item in retry_records}, {original_question})
        self.assertEqual(len({item["traceId"] for item in retry_records}), 2)
        self.assertEqual(len({request["traceId"] for request in dispatcher.requests}), 2)
        by_query = {item["queryId"]: item for item in retry_records}
        self.assertIsNone(by_query["q-retry-1"]["originalQueryId"])
        self.assertEqual(by_query["q-retry-2"]["originalQueryId"], "q-retry-1")

        other = self.gateway.access_control.create_service_account("Other retry Agent")
        other_policy = self.gateway.access_control.create_policy(
            "Other retry access",
            {
                "schemaVersion": 1,
                "datasourceId": self.datasource_id,
                "projects": ["runtime-test"],
                "tools": ["query:execute"],
                "tables": {"public.orders": {"effect": "allow"}},
            },
        )
        self.gateway.access_control.bind_policy(other.id, other_policy["id"])
        other_key = self.gateway.access_control.issue_api_key(other.id)
        before = len(dispatcher.requests)
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "cross-owner-retry",
                "method": "query.run",
                "params": {
                    "question": original_question,
                    "semanticSql": "SELECT * FROM orders",
                    "queryId": "q-cross-owner-retry",
                    "retryOfQueryId": "q-retry-1",
                },
            },
            authorization=f"Bearer {other_key['apiKey']}",
        )
        self.assertEqual(status, 404)
        self.assertEqual(response["error"]["code"], "DIAGNOSTIC_NOT_FOUND")
        self.assertEqual(len(dispatcher.requests), before)

    def test_legacy_protocol_rejects_retry_linkage_as_an_unknown_query_field(self) -> None:
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "legacy-retry-link",
                "method": "query.run",
                "params": {
                    "question": "Retry revenue",
                    "semanticSql": "SELECT revenue FROM orders",
                    "queryId": "q-legacy-retry",
                    "retryOfQueryId": "q-original",
                },
            },
            authorization=self.authorization,
        )

        self.assertEqual(status, 400)
        self.assertEqual(response["protocolVersion"], "1")
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertNotIn("reasonCode", response["error"])

    def test_confirmation_rule_cannot_be_bypassed_by_direct_query_run(self) -> None:
        SemanticConsoleService(self.project).create_rule(
            {
                "title": "Sales period",
                "content": "Confirm the Sales reporting period.",
                "confirmationRule": {
                    "kind": "timeRange",
                    "models": ["orders"],
                    "conditionKey": "timeRange",
                    "required": True,
                    "valueType": "dateRange",
                    "requireConfirmation": True,
                    "prompt": "Which reporting period?",
                },
            }
        )
        direct_status, direct = self.gateway.dispatch(
            {
                "protocolVersion": "2", "id": "direct-without-preparation", "method": "query.run",
                "params": {"question": "Revenue", "semanticSql": "SELECT * FROM orders", "queryId": "q-direct"},
            },
            authorization=self.authorization,
        )
        self.assertEqual(direct_status, 409)
        self.assertEqual(direct["error"]["reasonCode"], "CLARIFICATION_REQUIRED")
        self.assertEqual(self.dispatcher.requests, [])

        _, pending = self.gateway.dispatch(
            {
                "protocolVersion": "2", "id": "prepare-pending", "method": "query.prepare",
                "params": {"question": "Revenue", "semanticSql": "SELECT * FROM orders", "conditions": {}},
            },
            authorization=self.authorization,
        )
        self.assertEqual(pending["result"]["status"], "needs_clarification")
        _, ready = self.gateway.dispatch(
            {
                "protocolVersion": "2", "id": "prepare-ready", "method": "query.prepare",
                "params": {
                    "question": "Revenue", "semanticSql": "SELECT * FROM orders",
                    "conditions": {"timeRange": {"start": "2026-01-01", "end": "2026-08-31"}},
                    "confirmedConditions": ["timeRange"],
                },
            },
            authorization=self.authorization,
        )
        prepared_id = ready["result"]["preparationId"]
        run_status, run = self.gateway.dispatch(
            {
                "protocolVersion": "2", "id": "run-prepared", "method": "query.run",
                "params": {
                    "question": "Revenue", "semanticSql": "SELECT * FROM orders", "queryId": "q-prepared",
                    "preparationId": prepared_id,
                },
            },
            authorization=self.authorization,
        )
        self.assertEqual(run_status, 200)
        self.assertTrue(run["ok"])
        self.assertNotIn("preparationId", self.dispatcher.requests[-1]["params"])

    def test_diagnostic_write_failure_never_replaces_query_result(self) -> None:
        class UnavailableDiagnostics:
            def record_execution(self, **_kwargs):
                raise DiagnosticError("DIAGNOSTIC_STORE_UNAVAILABLE", "unavailable", status=503)

        self.gateway.diagnostics = UnavailableDiagnostics()
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "diagnostics-down",
                "method": "query.run",
                "params": {
                    "question": "Revenue",
                    "semanticSql": "SELECT * FROM orders",
                    "queryId": "q-diagnostics-down",
                },
            },
            authorization=self.authorization,
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])

    def test_auth_failure_cancel_and_context_failure_follow_capture_boundaries(self) -> None:
        unauthenticated_status, unauthenticated = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "unauthenticated-diagnostic",
                "method": "query.run",
                "params": {
                    "question": "must-not-enter-security-events",
                    "semanticSql": "SELECT must_not_enter_security_events",
                    "queryId": "q-unauthenticated",
                },
            },
            authorization="Bearer invalid-credential-value-that-is-long-enough",
        )
        self.assertEqual(unauthenticated_status, 401)
        self.assertEqual(unauthenticated["error"]["reasonCode"], "AUTHENTICATION_EXPIRED")
        with self.gateway.access_control._connect() as connection:
            security = connection.execute("SELECT * FROM diagnostic_security_events").fetchall()
            diagnostic_count = connection.execute("SELECT COUNT(*) AS count FROM query_diagnostics").fetchone()
        self.assertEqual(len(security), 1)
        self.assertEqual(
            set(security[0].keys()),
            {"id", "trace_id", "transport", "method", "status", "created_at"},
        )
        self.assertNotIn("must-not-enter-security-events", str(dict(security[0])))
        self.assertEqual(diagnostic_count["count"], 0)

        cancel_status, cancel = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "cancel-is-not-a-failure",
                "method": "query.cancel",
                "params": {"queryId": "q-cancelled"},
            },
            authorization=self.authorization,
        )
        self.assertEqual(cancel_status, 200)
        self.assertTrue(cancel["ok"])
        with self.gateway.access_control._connect() as connection:
            diagnostic_count = connection.execute("SELECT COUNT(*) AS count FROM query_diagnostics").fetchone()
        self.assertEqual(diagnostic_count["count"], 0)

        class ContextFailureDispatcher:
            def dispatch(self, request):
                return {
                    "protocolVersion": "2",
                    "id": request["id"],
                    "ok": False,
                    "error": {
                        "code": "SEMANTIC_ERROR",
                        "phase": "context",
                        "message": "semantic context could not be resolved",
                        "retryable": False,
                        "reasonCode": "SEMANTIC_PARSE_FAILED",
                        "resources": [],
                        "requiredPermissions": [],
                        "suggestion": "Inspect the published semantic model.",
                        "origin": "semantic-runtime",
                        "traceId": request["traceId"],
                    },
                }

        self.gateway.dispatcher = ContextFailureDispatcher()
        context_status, context = self.gateway.dispatch(
            {
                "protocolVersion": "2",
                "id": "context-failure",
                "method": "context.ask",
                "params": {"question": "Where is revenue defined?"},
            },
            authorization=self.authorization,
        )
        self.assertEqual(context_status, 200)
        self.assertFalse(context["ok"])
        captured = self.gateway.diagnostics.list_diagnostics(
            organization_id=self.gateway.access_control.authenticate(self.authorization).subject.organization_id,
            project_id="runtime-test",
        )["items"]
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["question"], "Where is revenue defined?")
        self.assertIsNone(captured[0]["semanticSql"])
        self.assertIsNone(captured[0]["nativeSql"])
        self.assertEqual(captured[0]["error"]["reasonCode"], "SEMANTIC_PARSE_FAILED")

    def test_query_pins_project_credentials_and_limits_server_side(self) -> None:
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "query-1",
                "method": "query.run",
                "params": {
                    "question": "Daily revenue",
                    "semanticSql": "SELECT * FROM orders",
                    "queryId": "agent-query-1",
                    "chartIntent": "line",
                },
            },
            authorization=self.authorization,
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        internal = self.dispatcher.requests[-1]["params"]
        self.assertEqual(internal["projectDir"], str(self.project.project_dir))
        self.assertEqual(internal["databaseDsnEnv"], "SEMARAIL_DATABASE_URL")
        self.assertEqual(internal["maxRows"], 500)
        self.assertEqual(internal["previewRows"], 200)
        self.assertNotIn("connection", internal)

    def test_health_reports_legacy_postgres_environment_as_query_ready(self) -> None:
        with patch.dict("os.environ", {"SEMARAIL_DATABASE_URL": "postgresql://redacted"}):
            _, response = self.gateway.dispatch(
                {"protocolVersion": "1", "id": "health-env", "method": "health", "params": {}},
                authorization=self.authorization,
            )

        self.assertTrue(response["result"]["capabilities"]["governedQuery"])
        self.assertEqual(response["result"]["readiness"]["governedQuery"], "ready")

    def test_public_request_cannot_override_project_or_limits(self) -> None:
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "bad-1",
                "method": "query.run",
                "params": {
                    "question": "Q",
                    "semanticSql": "SELECT 1",
                    "queryId": "q-1",
                    "projectDir": "C:/private",
                    "maxRows": 999999,
                },
            },
            authorization=self.authorization,
        )

        self.assertEqual(status, 400)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(self.dispatcher.requests, [])

    def test_application_routes_runtime_rpc_separately_from_console_crud(self) -> None:
        app = create_app(
            SemanticConsoleService(self.project),
            runtime_rpc=self.gateway,
        )
        status, response = app.request(
            "POST",
            "/api/v1/runtime/rpc",
            {"protocolVersion": "1", "id": "validate-1", "method": "project.validate", "params": {}},
            authorization=self.authorization,
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(self.dispatcher.requests[-1]["params"], {"projectDir": str(self.project.project_dir)})

    def test_runtime_pins_describe_and_dry_plan_project_paths(self) -> None:
        for method, params in (
            ("project.describe", {}),
            ("query.dryPlan", {"semanticSql": "SELECT * FROM orders"}),
        ):
            with self.subTest(method=method):
                status, response = self.gateway.dispatch(
                    {"protocolVersion": "1", "id": method, "method": method, "params": params},
                    authorization=self.authorization,
                )
                self.assertEqual(status, 200)
                self.assertTrue(response["ok"])
                self.assertEqual(self.dispatcher.requests[-1]["params"]["projectDir"], str(self.project.project_dir))

    def test_runtime_compiles_policy_for_all_semantic_metadata_calls(self) -> None:
        for method, params in (
            ("project.describe", {}),
            ("context.ask", {"question": "orders"}),
            ("query.dryPlan", {"semanticSql": "SELECT * FROM orders"}),
        ):
            with self.subTest(method=method):
                status, response = self.gateway.dispatch(
                    {"protocolVersion": "1", "id": f"managed-{method}", "method": method, "params": params},
                    authorization=self.authorization,
                )
                self.assertEqual(status, 200)
                self.assertTrue(response["ok"])
                compiled = self.dispatcher.requests[-1]["params"]["authorizationPolicy"]
                self.assertEqual(compiled["defaultEffect"], "deny")

    def test_runtime_rejects_bootstrap_administrator_credential(self) -> None:
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "bootstrap-query",
                "method": "query.run",
                "params": {
                    "question": "Revenue",
                    "semanticSql": "SELECT * FROM orders",
                    "queryId": "bootstrap-query",
                },
            },
            authorization=self.admin_authorization,
        )

        self.assertEqual(status, 403)
        self.assertEqual(response["error"]["code"], "FORBIDDEN")
        self.assertEqual(response["error"]["message"], "managed Agent credentials are required")
        self.assertEqual(self.dispatcher.requests, [])
        event = self.gateway.access_control.list_audit()[0]
        self.assertEqual(event["subjectId"], "bootstrap-admin")
        self.assertEqual(event["decision"], "denied")

        health_status, health = self.gateway.dispatch(
            {"protocolVersion": "1", "id": "bootstrap-health", "method": "health", "params": {}},
            authorization=self.admin_authorization,
        )
        self.assertEqual(health_status, 200)
        self.assertTrue(health["ok"])

    def test_runtime_fails_closed_when_semantic_policy_cannot_be_compiled(self) -> None:
        with patch.object(self.gateway.policy_engine, "compile_data_policy", side_effect=RuntimeError("missing policy")):
            status, response = self.gateway.dispatch(
                {"protocolVersion": "1", "id": "metadata-policy-missing", "method": "context.ask", "params": {"question": "orders"}},
                authorization=self.authorization,
            )
        self.assertEqual(status, 403)
        self.assertEqual(response["error"]["code"], "FORBIDDEN")
        self.assertEqual(self.dispatcher.requests, [])

    def test_runtime_rpc_requires_a_bearer_token(self) -> None:
        status, response = self.gateway.dispatch(
            {"protocolVersion": "1", "id": "health-2", "method": "health", "params": {}},
            "Bearer wrong-token-that-is-at-least-thirty-two-characters",
        )

        self.assertEqual(status, 401)
        self.assertEqual(response["error"]["code"], "UNAUTHENTICATED")
        self.assertEqual(self.dispatcher.requests, [])

    def test_v2_disabled_account_is_not_misreported_as_expired_authentication(self) -> None:
        subject = self.gateway.access_control.authenticate(self.authorization).subject
        self.gateway.access_control.set_subject_status(subject.id, "disabled")

        status, response = self.gateway.dispatch(
            {"protocolVersion": "2", "id": "disabled-v2", "method": "health", "params": {}},
            authorization=self.authorization,
        )

        self.assertEqual(status, 403)
        self.assertEqual(response["error"]["code"], "SUBJECT_DISABLED")
        self.assertEqual(response["error"]["reasonCode"], "ACCOUNT_DISABLED")
        self.assertEqual(response["error"]["origin"], "authentication")

    def test_v2_method_denial_distinguishes_project_tool_and_explicit_deny(self) -> None:
        cases = (
            (
                "Wrong project",
                {
                    "schemaVersion": 1,
                    "datasourceId": self.datasource_id,
                    "projects": ["another-project"],
                    "tools": ["query:execute"],
                    "tables": {"public.sales": {"effect": "allow"}},
                },
                "PROJECT_PERMISSION_REQUIRED",
                {"kind": "project", "name": "runtime-test"},
            ),
            (
                "Missing tool",
                {
                    "schemaVersion": 1,
                    "datasourceId": self.datasource_id,
                    "projects": ["runtime-test"],
                    "tools": ["semantic:read"],
                    "tables": {"public.sales": {"effect": "allow"}},
                },
                "TOOL_PERMISSION_REQUIRED",
                {"kind": "tool", "name": "query.run"},
            ),
            (
                "Explicit tool deny",
                {
                    "schemaVersion": 1,
                    "datasourceId": self.datasource_id,
                    "projects": ["runtime-test"],
                    "tools": ["query:execute"],
                    "denyTools": ["query:execute"],
                    "tables": {"public.sales": {"effect": "allow"}},
                },
                "EXPLICIT_DENIAL",
                {"kind": "tool", "name": "query.run"},
            ),
        )
        for label, document, reason_code, resource in cases:
            with self.subTest(label=label):
                account = self.gateway.access_control.create_service_account(label)
                policy = self.gateway.access_control.create_policy(label, document)
                self.gateway.access_control.bind_policy(account.id, policy["id"])
                key = self.gateway.access_control.issue_api_key(account.id)
                status, response = self.gateway.dispatch(
                    {
                        "protocolVersion": "2",
                        "id": f"denial-{reason_code}",
                        "method": "query.run",
                        "params": {
                            "question": "Revenue",
                            "semanticSql": "SELECT revenue FROM Sales",
                            "queryId": f"q-{reason_code}",
                        },
                    },
                    authorization=f"Bearer {key['apiKey']}",
                )
                self.assertEqual(status, 403)
                self.assertEqual(response["error"]["reasonCode"], reason_code)
                self.assertEqual(response["error"]["resources"], [resource])
                self.assertFalse(response["error"]["retryable"])

    def test_service_account_policy_controls_runtime_scope_and_limits(self) -> None:
        account = self.gateway.access_control.create_service_account(
            "Sales Agent", attributes={"regionCodes": ["CN-JIA"]}
        )
        policy = self.gateway.access_control.create_policy(
            "Sales query",
            {
                "schemaVersion": 1,
                "datasourceId": self.datasource_id,
                "projects": ["runtime-test"],
                "tools": ["query:execute"],
                "tables": {},
                "limits": {"maxRows": 25, "timeoutMs": 5000},
            },
        )
        self.gateway.access_control.bind_policy(account.id, policy["id"])
        issued = self.gateway.access_control.issue_api_key(account.id)

        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "svc-query",
                "method": "query.run",
                "params": {"question": "Revenue", "semanticSql": "SELECT * FROM orders", "queryId": "svc-1"},
            },
            authorization=f"Bearer {issued['apiKey']}",
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        internal = self.dispatcher.requests[-1]["params"]
        self.assertEqual(internal["maxRows"], 25)
        self.assertEqual(internal["timeoutMs"], 5000)
        self.assertEqual(internal["authorizationPolicy"]["defaultEffect"], "deny")
        event = self.gateway.access_control.list_audit()[0]
        self.assertEqual(event["subjectId"], account.id)
        self.assertEqual(event["policyVersion"], f"{policy['id']}:1")

        self.gateway.access_control.update_policy(
            policy["id"],
            {
                "schemaVersion": 1,
                "datasourceId": self.datasource_id,
                "projects": ["runtime-test"],
                "tools": ["semantic:read"],
                "tables": {},
            },
        )
        denied_status, denied = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "svc-query-after-policy-change",
                "method": "query.run",
                "params": {"question": "Revenue", "semanticSql": "SELECT * FROM orders", "queryId": "svc-2"},
            },
            authorization=f"Bearer {issued['apiKey']}",
        )
        self.assertEqual(denied_status, 403)
        self.assertEqual(denied["error"]["code"], "FORBIDDEN")
        self.assertEqual(self.gateway.access_control.list_audit()[0]["policyVersion"], f"{policy['id']}:2")

    def test_unbound_service_account_is_forbidden(self) -> None:
        account = self.gateway.access_control.create_service_account("No policy")
        issued = self.gateway.access_control.issue_api_key(account.id)
        status, response = self.gateway.dispatch(
            {"protocolVersion": "1", "id": "denied", "method": "health", "params": {}},
            authorization=f"Bearer {issued['apiKey']}",
        )
        self.assertEqual(status, 403)
        self.assertEqual(response["error"]["code"], "FORBIDDEN")

    def test_employee_session_uses_the_same_current_row_policy_as_service_accounts(self) -> None:
        user = self.gateway.access_control.upsert_external_user(
            provider="dingtalk", external_subject="employee-a", name="Employee A"
        )
        self.gateway.access_control.update_user(
            user.id,
            attributes={"regionCodes": ["CN-JIA"], "privateProfile": "must-not-reach-sidecar"},
        )
        policy = self.gateway.access_control.create_policy(
            "Employee sales region",
            {
                "schemaVersion": 1,
                "datasourceId": self.datasource_id,
                "projects": ["runtime-test"],
                "tools": ["query:execute"],
                "tables": {
                    "public.sales": {
                        "effect": "allow",
                        "rows": [
                            {
                                "field": "region_code",
                                "operator": "in",
                                "valueFrom": "subject.attributes.regionCodes",
                            }
                        ],
                        "columns": {"allow": ["order_id", "region_code", "amount"], "deny": []},
                    }
                },
            },
        )
        self.gateway.access_control.bind_policy(user.id, policy["id"])
        session = self.gateway.access_control.issue_session(user.id)

        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "employee-query",
                "method": "query.run",
                "params": {"question": "Revenue", "semanticSql": "SELECT * FROM sales", "queryId": "employee-1"},
            },
            authorization=f"Bearer {session['accessToken']}",
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        compiled = self.dispatcher.requests[-1]["params"]["authorizationPolicy"]
        row_filter = compiled["tables"]["public.sales"]["rowFilter"]
        self.assertEqual(row_filter["conditions"][0]["conditions"][0]["values"], ["CN-JIA"])
        self.assertEqual(compiled["databaseSession"]["subjectId"], user.id)
        self.assertEqual(compiled["databaseSession"]["attributes"], {"regionCodes": ["CN-JIA"]})
        self.assertNotIn("privateProfile", str(compiled))
        self.assertEqual(self.gateway.access_control.list_audit()[0]["subjectId"], user.id)

    def test_client_cannot_supply_or_override_database_identity_context(self) -> None:
        dispatch_count = len(self.dispatcher.requests)
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "spoofed-session",
                "method": "query.run",
                "params": {
                    "question": "Revenue",
                    "semanticSql": "SELECT * FROM sales",
                    "queryId": "spoofed-session",
                    "authorizationPolicy": {
                        "databaseSession": {
                            "subjectId": "administrator",
                            "organizationId": "other-org",
                            "attributes": {"regionCodes": ["*"]},
                        }
                    },
                },
            },
            authorization=self.authorization,
        )

        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(len(self.dispatcher.requests), dispatch_count)

    def test_unbinding_policy_immediately_revokes_existing_service_account_credential(self) -> None:
        account = self.gateway.access_control.create_service_account("Sales Agent")
        policy = self.gateway.access_control.create_policy(
            "Sales query",
            {
                "schemaVersion": 1,
                "datasourceId": self.datasource_id,
                "projects": ["runtime-test"],
                "tools": ["query:execute"],
                "tables": {},
            },
        )
        self.gateway.access_control.bind_policy(account.id, policy["id"])
        issued = self.gateway.access_control.issue_api_key(account.id)
        credential_authorization = f"Bearer {issued['apiKey']}"
        request = {
            "protocolVersion": "1",
            "id": "svc-query-before-unbind",
            "method": "query.run",
            "params": {"question": "Revenue", "semanticSql": "SELECT * FROM orders", "queryId": "svc-1"},
        }

        status, response = self.gateway.dispatch(request, authorization=credential_authorization)

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        dispatch_count = len(self.dispatcher.requests)

        app = create_app(SemanticConsoleService(self.project), runtime_rpc=self.gateway)
        unbind_status, unbound = app.request(
            "DELETE",
            f"/api/v1/access/policy-bindings/{account.id}/{policy['id']}",
            authorization=self.admin_authorization,
        )

        self.assertEqual((unbind_status, unbound["status"]), (200, "unbound"))
        denied_status, denied = self.gateway.dispatch(
            {**request, "id": "svc-query-after-unbind", "params": {**request["params"], "queryId": "svc-2"}},
            authorization=credential_authorization,
        )

        self.assertEqual(denied_status, 403)
        self.assertEqual(denied["error"]["code"], "FORBIDDEN")
        self.assertEqual(len(self.dispatcher.requests), dispatch_count)

    def test_query_policy_is_rejected_when_the_active_datasource_changes(self) -> None:
        account = self.gateway.access_control.create_service_account("Source-bound agent")
        policy = self.gateway.access_control.create_policy(
            "Source A only",
            {
                "schemaVersion": 1,
                "datasourceId": self.datasource_id,
                "projects": ["runtime-test"],
                "tools": ["query:execute"],
                "tables": {"public.orders": {"effect": "allow"}},
            },
        )
        self.gateway.access_control.bind_policy(account.id, policy["id"])
        issued = self.gateway.access_control.issue_api_key(account.id)
        self.project.datasource_records()["other-datasource"] = DatasourceRecord(
            "other-datasource", "Other warehouse", "postgres", {"database": "other"}
        )
        self.project.active_datasource_id = "other-datasource"
        self.project.save_datasources()

        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "wrong-source",
                "method": "query.run",
                "params": {"question": "Orders", "semanticSql": "SELECT * FROM orders", "queryId": "source-2"},
            },
            authorization=f"Bearer {issued['apiKey']}",
        )

        self.assertEqual((status, response["error"]["code"]), (403, "FORBIDDEN"))
        self.assertEqual(self.dispatcher.requests, [])

    def test_legacy_unbound_policy_cannot_execute_a_query(self) -> None:
        account = self.gateway.access_control.create_service_account("Legacy agent")
        policy = self.gateway.access_control.create_policy(
            "Legacy policy",
            {
                "schemaVersion": 1,
                "projects": ["runtime-test"],
                "tools": ["query:execute"],
                "tables": {"public.orders": {"effect": "allow"}},
            },
        )
        self.gateway.access_control.bind_policy(account.id, policy["id"])
        issued = self.gateway.access_control.issue_api_key(account.id)

        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "legacy-source",
                "method": "query.run",
                "params": {"question": "Orders", "semanticSql": "SELECT * FROM orders", "queryId": "legacy-1"},
            },
            authorization=f"Bearer {issued['apiKey']}",
        )

        self.assertEqual((status, response["error"]["code"]), (403, "FORBIDDEN"))
        self.assertEqual(self.dispatcher.requests, [])


if __name__ == "__main__":
    unittest.main()
