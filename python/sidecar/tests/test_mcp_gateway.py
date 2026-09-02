from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from sidecar.errors import POLICY_DENIED, RpcFault
from sidecar.mcp_gateway import _default_query_service, create_governed_mcp_server


class FakeQueryService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    def run(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        return {
            "schemaVersion": 1,
            "queryId": params["queryId"],
            "status": "success",
            "semanticSql": params["semanticSql"],
            "nativeSql": "SELECT 1",
            "columns": [],
            "previewRows": [],
            "stats": {"returnedRows": 0, "durationMs": 1, "truncated": False},
            "question": params["question"],
        }

    def cancel(self, params: Mapping[str, Any]) -> dict[str, Any]:
        query_id = str(params["queryId"])
        self.cancelled.append(query_id)
        return {"queryId": query_id, "cancelled": True}


class BlockingQueryService(FakeQueryService):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.released = threading.Event()

    def run(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        self.started.set()
        self.released.wait(2)
        return {"queryId": params["queryId"], "status": "success"}

    def cancel(self, params: Mapping[str, Any]) -> dict[str, Any]:
        result = super().cancel(params)
        self.released.set()
        return result


class GovernedMcpGatewayTests(unittest.TestCase):
    def test_default_service_warms_advisory_dependencies_before_worker_threads(self) -> None:
        class WarmService(FakeQueryService):
            def __init__(self) -> None:
                super().__init__()
                self.warmed = False

            def prepare_for_worker_threads(self) -> None:
                self.warmed = True

        service = WarmService()
        with patch(
            "sidecar.wren_adapter.default_dependencies",
            return_value=SimpleNamespace(query_service=service),
        ):
            selected = _default_query_service()

        self.assertIs(selected, service)
        self.assertTrue(service.warmed)

    def test_tool_pins_project_and_connection_policy_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            service = FakeQueryService()
            server = create_governed_mcp_server(
                project=project,
                database_dsn_env="ANALYTICS_DATABASE_URL",
                query_service=service,
            )

            tools = asyncio.run(server.list_tools())
            self.assertEqual([tool.name for tool in tools], ["semarail_governed_query"])
            schema = tools[0].inputSchema
            self.assertNotIn("project_dir", schema.get("properties", {}))
            self.assertNotIn("database_dsn_env", schema.get("properties", {}))

            _content, result = asyncio.run(
                server.call_tool(
                    "semarail_governed_query",
                    {
                        "question": "How many orders?",
                        "semantic_sql": "SELECT COUNT(*) FROM orders",
                        "chart_intent": "table",
                        "timeout_ms": 1234,
                        "max_rows": 12,
                        "preview_rows": 7,
                        "max_preview_bytes": 4096,
                    },
                )
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(len(service.calls), 1)
            params = service.calls[0]
            self.assertEqual(params["projectDir"], str(project.resolve()))
            self.assertEqual(params["databaseDsnEnv"], "ANALYTICS_DATABASE_URL")
            self.assertEqual(params["semanticSql"], "SELECT COUNT(*) FROM orders")
            self.assertEqual(params["timeoutMs"], 1234)
            self.assertEqual(params["maxRows"], 12)
            self.assertEqual(params["previewRows"], 7)
            self.assertEqual(params["maxPreviewBytes"], 4096)
            self.assertRegex(params["queryId"], r"^semarail-mcp-[0-9a-f]{32}$")

    def test_rpc_fault_becomes_stable_mcp_tool_error_without_secret(self) -> None:
        class DeniedService(FakeQueryService):
            def run(self, params: Mapping[str, Any]) -> dict[str, Any]:
                del params
                raise RpcFault(
                    POLICY_DENIED,
                    "policy",
                    "query denied by read-only SQL policy",
                    retryable=False,
                )

        with tempfile.TemporaryDirectory() as directory:
            server = create_governed_mcp_server(
                project=Path(directory),
                query_service=DeniedService(),
            )
            with self.assertRaises(ToolError) as caught:
                asyncio.run(
                    server.call_tool(
                        "semarail_governed_query",
                        {"question": "secret", "semantic_sql": "DROP TABLE orders"},
                    )
                )
        message = str(caught.exception)
        payload = json.loads(message[message.index("{") :])
        self.assertEqual(payload["code"], POLICY_DENIED)
        self.assertEqual(payload["phase"], "policy")
        self.assertNotIn("secret", message)

    def test_unexpected_service_error_is_redacted(self) -> None:
        class BrokenService(FakeQueryService):
            def run(self, params: Mapping[str, Any]) -> dict[str, Any]:
                del params
                raise RuntimeError(
                    "postgresql://alice:super-secret@db.internal/analytics"
                )

        with tempfile.TemporaryDirectory() as directory:
            server = create_governed_mcp_server(
                project=Path(directory),
                query_service=BrokenService(),
            )
            with self.assertRaises(ToolError) as caught:
                asyncio.run(
                    server.call_tool(
                        "semarail_governed_query",
                        {"question": "q", "semantic_sql": "SELECT 1"},
                    )
                )
        message = str(caught.exception)
        self.assertIn("INTERNAL_ERROR", message)
        self.assertNotIn("super-secret", message)
        self.assertNotIn("db.internal", message)

    def test_mcp_task_cancellation_calls_query_service_cancel(self) -> None:
        async def exercise() -> tuple[list[str], str]:
            with tempfile.TemporaryDirectory() as directory:
                service = BlockingQueryService()
                server = create_governed_mcp_server(
                    project=Path(directory),
                    query_service=service,
                    cancellation_grace_seconds=1.0,
                )
                task = asyncio.create_task(
                    server.call_tool(
                        "semarail_governed_query",
                        {"question": "slow", "semantic_sql": "SELECT 1"},
                    )
                )
                started = await asyncio.to_thread(service.started.wait, 1)
                self.assertTrue(started)
                query_id = str(service.calls[0]["queryId"])
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                return service.cancelled, query_id

        cancelled, query_id = asyncio.run(exercise())
        self.assertEqual(cancelled, [query_id])


if __name__ == "__main__":
    unittest.main()
