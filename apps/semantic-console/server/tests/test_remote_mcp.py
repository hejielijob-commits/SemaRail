from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server.project import ProjectStore
from server.models import DatasourceRecord
from server.remote_mcp import SemaRailTokenVerifier, create_remote_mcp_server
from server.runtime_rpc import RuntimeRpcGateway


class FakeValidator:
    def health(self):
        return {"available": True}

    def validate(self, _project_dir):
        return {"valid": True, "errors": [], "warnings": [], "errorCount": 0, "warningCount": 0}

    def build(self, _project_dir):
        return {"models": []}


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        method = request["method"]
        if method == "context.ask":
            result = {"schemaVersion": 1, "projectRevision": "rev", "models": [], "relationships": []}
        elif method == "query.run":
            result = {
                "schemaVersion": 1,
                "queryId": request["params"]["queryId"],
                "status": "success",
                "semanticSql": request["params"]["semanticSql"],
                "columns": [],
                "previewRows": [],
                "stats": {"returnedRows": 0, "durationMs": 1, "truncated": False},
            }
        else:
            result = {"schemaVersion": 1, "projectRevision": "rev", "models": [], "relationships": []}
        return {"protocolVersion": "1", "id": request["id"], "ok": True, "result": result}


class RemoteMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-remote-mcp-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        project_dir = root / "project"
        project_dir.mkdir()
        project_dir.joinpath("wren_project.yml").write_text(
            "schema_version: 5\nname: remote-mcp-test\ndata_source: postgres\n", encoding="utf-8"
        )
        project = ProjectStore(project_dir, state_dir=root / "state", validator=FakeValidator())
        self.datasource_id = "remote-mcp-datasource"
        project.datasource_records()[self.datasource_id] = DatasourceRecord(
            self.datasource_id, "Remote warehouse", "postgres", {"database": "remote"}
        )
        project.active_datasource_id = self.datasource_id
        project.save_datasources()
        self.dispatcher = RecordingDispatcher()
        self.gateway = RuntimeRpcGateway(
            project,
            dispatcher=self.dispatcher,
            auth_token="bootstrap-token-that-is-at-least-thirty-two-characters",
        )
        account = self.gateway.access_control.create_service_account(
            "Remote Sales Agent", attributes={"regionCodes": ["CN-JIA"]}
        )
        self.account = account
        policy = self.gateway.access_control.create_policy(
            "Remote MCP",
            {
                "schemaVersion": 1,
                "datasourceId": self.datasource_id,
                "projects": ["remote-mcp-test"],
                "tools": ["project:validate", "semantic:read", "query:plan", "query:execute", "query:cancel"],
                "tables": {
                    "public.sales": {
                        "effect": "allow",
                        "rows": [{
                            "field": "region_code",
                            "operator": "in",
                            "valueFrom": "subject.attributes.regionCodes",
                        }],
                    }
                },
            },
        )
        self.policy = policy
        self.gateway.access_control.bind_policy(account.id, policy["id"])
        issued = self.gateway.access_control.issue_api_key(account.id, label="remote-test")
        self.api_key = issued["apiKey"]
        self.credential_id = issued["credential"]["id"]

    def test_token_verifier_rejects_bootstrap_but_accepts_managed_identities(self) -> None:
        verifier = SemaRailTokenVerifier(self.gateway.access_control)

        self.assertIsNone(
            asyncio.run(
                verifier.verify_token("bootstrap-token-that-is-at-least-thirty-two-characters")
            )
        )
        service_access = asyncio.run(verifier.verify_token(self.api_key))
        self.assertIsNotNone(service_access)
        self.assertEqual(service_access.claims["subjectType"], "service_account")

        employee = self.gateway.access_control.upsert_external_user(
            provider="dingtalk", external_subject="remote-employee", name="Remote employee"
        )
        self.gateway.access_control.bind_policy(employee.id, self.policy["id"])
        session = self.gateway.access_control.issue_session(employee.id)
        employee_access = asyncio.run(verifier.verify_token(session["accessToken"]))
        self.assertIsNotNone(employee_access)
        self.assertEqual(employee_access.claims["subjectType"], "user")

    def test_token_verifier_honors_revocation_immediately(self) -> None:
        verifier = SemaRailTokenVerifier(self.gateway.access_control)
        accepted = asyncio.run(verifier.verify_token(self.api_key))
        self.assertIsNotNone(accepted)
        self.gateway.access_control.revoke_credential(self.credential_id)
        self.assertIsNone(asyncio.run(verifier.verify_token(self.api_key)))

    def test_employee_session_can_call_the_authenticated_remote_mcp(self) -> None:
        employee = self.gateway.access_control.upsert_external_user(
            provider="dingtalk", external_subject="mcp-employee", name="MCP employee"
        )
        self.gateway.access_control.bind_policy(employee.id, self.policy["id"])
        employee_session = self.gateway.access_control.issue_session(employee.id)

        async def exercise() -> None:
            server = create_remote_mcp_server(
                project=self.gateway.project.project_dir,
                gateway=self.gateway,
                host="127.0.0.1",
                port=48764,
                allowed_hosts=["testserver"],
            )
            app = server.streamable_http_app()
            transport = httpx.ASGITransport(app=app)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    headers={"Authorization": f"Bearer {employee_session['accessToken']}"},
                ) as http_client:
                    async with streamable_http_client(
                        "http://testserver/mcp", http_client=http_client, terminate_on_close=False
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            result = await session.call_tool(
                                "semarail_get_context", {"question": "Employee revenue?"}
                            )
                            self.assertFalse(result.isError)

        asyncio.run(exercise())
        event = self.gateway.access_control.list_audit()[0]
        self.assertEqual(event["subjectId"], employee.id)
        self.assertEqual(event["details"]["authenticationMethod"], "oauth_session")
        self.assertEqual(event["details"]["transport"], "remote-mcp")
        self.assertNotIn("Employee revenue?", str(event))

    def test_official_mcp_client_uses_api_key_and_current_runtime_policy(self) -> None:
        async def exercise() -> None:
            server = create_remote_mcp_server(
                project=self.gateway.project.project_dir,
                gateway=self.gateway,
                host="127.0.0.1",
                port=48764,
                allowed_hosts=["testserver"],
            )
            app = server.streamable_http_app()
            transport = httpx.ASGITransport(app=app)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as http_client:
                    async with streamable_http_client(
                        "http://testserver/mcp", http_client=http_client, terminate_on_close=False
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            self.assertEqual(
                                [tool.name for tool in tools.tools],
                                [
                                    "semarail_validate_project",
                                    "semarail_list_models",
                                    "semarail_get_context",
                                    "semarail_plan_query",
                                    "semarail_governed_query",
                                ],
                            )
                            forbidden = {
                                "authorizationPolicy", "subject", "databaseDsn",
                                "databaseDsnEnv", "project", "projectDir",
                            }
                            for tool in tools.tools:
                                self.assertFalse(
                                    set(tool.inputSchema.get("properties", {})) & forbidden
                                )
                                self.assertFalse(tool.inputSchema.get("additionalProperties", True))
                            calls_before_spoof = len(self.dispatcher.requests)
                            spoofed = await session.call_tool(
                                "semarail_get_context",
                                {
                                    "question": "Revenue?",
                                    "authorizationPolicy": {"defaultEffect": "allow"},
                                },
                            )
                            self.assertTrue(spoofed.isError)
                            self.assertEqual(len(self.dispatcher.requests), calls_before_spoof)
                            context = await session.call_tool(
                                "semarail_get_context", {"question": "Revenue?"}
                            )
                            self.assertFalse(context.isError)
                            query = await session.call_tool(
                                "semarail_governed_query",
                                {"question": "Revenue?", "semantic_sql": "SELECT amount FROM sales"},
                            )
                            self.assertFalse(query.isError)
                            # The same authenticated MCP session must evaluate
                            # the latest policy on every tool call.
                            self.gateway.access_control.update_policy(
                                self.policy["id"],
                                {
                                    "schemaVersion": 1,
                                    "datasourceId": self.datasource_id,
                                    "projects": ["remote-mcp-test"],
                                    "tools": ["semantic:read"],
                                    "tables": {},
                                },
                            )
                            denied = await session.call_tool(
                                "semarail_governed_query",
                                {"question": "Revenue?", "semantic_sql": "SELECT amount FROM sales"},
                            )
                            self.assertTrue(denied.isError)

        asyncio.run(exercise())
        by_method = {request["method"]: request for request in self.dispatcher.requests}
        self.assertEqual(by_method["context.ask"]["params"]["question"], "Revenue?")
        compiled = by_method["query.run"]["params"]["authorizationPolicy"]
        self.assertEqual(
            compiled["tables"]["public.sales"]["rowFilter"]["conditions"][0]["conditions"][0]["values"],
            ["CN-JIA"],
        )
        self.assertNotIn(self.api_key, str(self.dispatcher.requests))
        query_events = [
            event for event in self.gateway.access_control.list_audit()
            if event["action"] == "query.run"
        ]
        self.assertEqual([event["decision"] for event in query_events], ["denied", "allowed"])
        allowed_details = query_events[1]["details"]
        self.assertEqual(allowed_details["transport"], "remote-mcp")
        self.assertEqual(allowed_details["datasourceId"], self.datasource_id)
        self.assertTrue(allowed_details["queryId"].startswith("remote-mcp-query-"))
        self.assertEqual(allowed_details["policyTables"], ["public.sales"])
        self.assertEqual(allowed_details["policyVersions"], [f"{self.policy['id']}:1"])
        self.assertEqual(allowed_details["authenticationMethod"], "api_key")
        serialized_audit = str(query_events)
        for sensitive in (
            self.api_key,
            "Revenue?",
            "SELECT amount FROM sales",
            "CN-JIA",
            "authorizationPolicy",
            "question",
            "semanticSql",
            "previewRows",
        ):
            self.assertNotIn(sensitive, serialized_audit)


if __name__ == "__main__":
    unittest.main()
