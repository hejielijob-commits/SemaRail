from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError

from sidecar.errors import POLICY_DENIED, RpcFault
from sidecar.semantic_mcp import create_semantic_mcp_server, main


class FakeSemanticService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def validate_project(self) -> dict[str, Any]:
        self.calls.append(("validate", None))
        return {"schemaVersion": 1, "valid": True, "errorCount": 0, "warningCount": 0}

    def list_models(self) -> dict[str, Any]:
        self.calls.append(("list", None))
        return {"schemaVersion": 1, "models": [{"name": "orders"}]}

    def get_context(self, question: str) -> dict[str, Any]:
        self.calls.append(("context", question))
        return {"schemaVersion": 1, "models": [{"name": "orders"}]}

    def plan_query(self, semantic_sql: str) -> dict[str, Any]:
        self.calls.append(("plan", semantic_sql))
        return {
            "schemaVersion": 1,
            "semanticSql": semantic_sql,
            "nativeSql": "SELECT * FROM public.orders",
        }


class SemanticMcpTests(unittest.TestCase):
    def test_exposes_only_stable_semarail_semantic_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = FakeSemanticService()
            server = create_semantic_mcp_server(
                project=directory,
                semantic_service=service,
            )
            tools = asyncio.run(server.list_tools())
            self.assertEqual(
                [tool.name for tool in tools],
                [
                    "semarail_validate_project",
                    "semarail_list_models",
                    "semarail_get_context",
                    "semarail_plan_query",
                ],
            )
            for tool in tools:
                properties = tool.inputSchema.get("properties", {})
                self.assertNotIn("project", properties)
                self.assertNotIn("project_dir", properties)
                self.assertNotIn("database_dsn_env", properties)
                self.assertTrue(tool.annotations.readOnlyHint)
                self.assertFalse(tool.annotations.destructiveHint)
                self.assertTrue(tool.annotations.idempotentHint)

            _content, validated = asyncio.run(
                server.call_tool("semarail_validate_project", {})
            )
            _content, listed = asyncio.run(
                server.call_tool("semarail_list_models", {})
            )

            _content, context = asyncio.run(
                server.call_tool(
                    "semarail_get_context",
                    {"question": "What is revenue?"},
                )
            )
            _content, plan = asyncio.run(
                server.call_tool(
                    "semarail_plan_query",
                    {"semantic_sql": "SELECT * FROM orders"},
                )
            )
            self.assertTrue(validated["valid"])
            self.assertEqual(listed["models"][0]["name"], "orders")
            self.assertEqual(context["models"][0]["name"], "orders")
            self.assertEqual(plan["nativeSql"], "SELECT * FROM public.orders")
            self.assertEqual(
                service.calls,
                [
                    ("validate", None),
                    ("list", None),
                    ("context", "What is revenue?"),
                    ("plan", "SELECT * FROM orders"),
                ],
            )

    def test_unexpected_runtime_error_is_redacted(self) -> None:
        secret = "postgres://alice:password@db.internal/analytics"

        class FailingService(FakeSemanticService):
            def get_context(self, question: str) -> dict[str, Any]:
                raise RuntimeError(f"{question} {secret} C:/private/project")

        with tempfile.TemporaryDirectory() as directory:
            server = create_semantic_mcp_server(
                project=directory,
                semantic_service=FailingService(),
            )
            with self.assertRaises(ToolError) as caught:
                asyncio.run(
                    server.call_tool(
                        "semarail_get_context",
                        {"question": "secret business question"},
                    )
                )
        message = str(caught.exception)
        self.assertIn("INTERNAL_ERROR", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("secret business question", message)
        self.assertNotIn("C:/private/project", message)

    def test_rpc_fault_is_a_stable_redacted_tool_error(self) -> None:
        class DeniedService(FakeSemanticService):
            def plan_query(self, semantic_sql: str) -> dict[str, Any]:
                del semantic_sql
                raise RpcFault(
                    POLICY_DENIED,
                    "policy",
                    "semantic SQL must be one read-only query",
                    retryable=False,
                )

        with tempfile.TemporaryDirectory() as directory:
            server = create_semantic_mcp_server(
                project=directory,
                semantic_service=DeniedService(),
            )
            with self.assertRaises(ToolError) as caught:
                asyncio.run(
                    server.call_tool(
                        "semarail_plan_query",
                        {"semantic_sql": "DROP TABLE secret_table"},
                    )
                )
        message = str(caught.exception)
        payload = json.loads(message[message.index("{") :])
        self.assertEqual(payload["code"], POLICY_DENIED)
        self.assertNotIn("secret_table", message)

    def test_main_starts_the_semarail_server_over_stdio(self) -> None:
        class FakeServer:
            def __init__(self) -> None:
                self.transports: list[str] = []

            def run(self, *, transport: str) -> None:
                self.transports.append(transport)

        server = FakeServer()
        semantic_service = FakeSemanticService()
        semantic_service.prepare = Mock()  # type: ignore[attr-defined]
        with patch(
            "sidecar.semantic_mcp.create_semantic_mcp_server",
            return_value=server,
        ) as create, patch(
            "sidecar.semantic_mcp.SemanticService",
            return_value=semantic_service,
        ):
            result = main(["--project", "C:/semantic/project"])
        self.assertEqual(result, 0)
        semantic_service.prepare.assert_called_once_with()  # type: ignore[attr-defined]
        create.assert_called_once_with(
            project="C:/semantic/project",
            semantic_service=semantic_service,
        )
        self.assertEqual(server.transports, ["stdio"])


if __name__ == "__main__":
    unittest.main()
