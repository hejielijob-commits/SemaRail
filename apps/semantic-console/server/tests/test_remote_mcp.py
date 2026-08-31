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
        self.gateway.access_control.bind_policy(account.id, policy["id"])
        issued = self.gateway.access_control.issue_api_key(account.id, label="remote-test")
        self.api_key = issued["apiKey"]
        self.credential_id = issued["credential"]["id"]

    def test_token_verifier_honors_revocation_immediately(self) -> None:
        verifier = SemaRailTokenVerifier(self.gateway.access_control)
        accepted = asyncio.run(verifier.verify_token(self.api_key))
        self.assertIsNotNone(accepted)
        self.gateway.access_control.revoke_credential(self.credential_id)
        self.assertIsNone(asyncio.run(verifier.verify_token(self.api_key)))

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
                            context = await session.call_tool(
                                "semarail_get_context", {"question": "Revenue?"}
                            )
                            self.assertFalse(context.isError)
                            query = await session.call_tool(
                                "semarail_governed_query",
                                {"question": "Revenue?", "semantic_sql": "SELECT amount FROM sales"},
                            )
                            self.assertFalse(query.isError)

        asyncio.run(exercise())
        by_method = {request["method"]: request for request in self.dispatcher.requests}
        self.assertEqual(by_method["context.ask"]["params"]["question"], "Revenue?")
        compiled = by_method["query.run"]["params"]["authorizationPolicy"]
        self.assertEqual(
            compiled["tables"]["public.sales"]["rowFilter"]["conditions"][0]["conditions"][0]["values"],
            ["CN-JIA"],
        )
        self.assertNotIn(self.api_key, str(self.dispatcher.requests))


if __name__ == "__main__":
    unittest.main()
