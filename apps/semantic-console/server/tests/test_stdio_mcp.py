from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from server.stdio_mcp import (
    CoreHttpTransport,
    create_stdio_mcp_server,
    load_employee_session,
    normalize_core_endpoint,
    resolve_authentication,
)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        return {"schemaVersion": 1, "method": method}


class _CoreHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    response_status = 200
    response_error: dict[str, Any] | None = None
    response_result: dict[str, Any] | None = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).requests.append(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
        )
        if type(self).response_status == 200:
            payload = {
                "protocolVersion": "1",
                "id": body["id"],
                "ok": True,
                "result": type(self).response_result or {"schemaVersion": 1, "method": body["method"]},
            }
        else:
            payload = {
                "protocolVersion": "1",
                "id": body["id"],
                "ok": False,
                "error": type(self).response_error or {"code": "FORBIDDEN", "message": "denied"},
            }
        encoded = json.dumps(payload).encode()
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args: Any) -> None:
        return


class StdioMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        _CoreHandler.requests = []
        _CoreHandler.response_status = 200
        _CoreHandler.response_error = None
        _CoreHandler.response_result = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CoreHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_exposes_only_five_stable_policy_neutral_tools(self) -> None:
        transport = RecordingTransport()
        server = create_stdio_mcp_server(transport)
        tools = asyncio.run(server.list_tools())
        self.assertEqual(
            [tool.name for tool in tools],
            [
                "semarail_validate_project",
                "semarail_list_models",
                "semarail_get_context",
                "semarail_plan_query",
                "semarail_governed_query",
            ],
        )
        forbidden = {
            "authorizationPolicy",
            "authorization_policy",
            "subject",
            "databaseDsn",
            "database_dsn",
            "databaseDsnEnv",
            "project",
            "projectDir",
        }
        for tool in tools:
            properties = set(tool.inputSchema.get("properties", {}))
            self.assertFalse(properties & forbidden)
            self.assertFalse(tool.inputSchema.get("additionalProperties", True))

        asyncio.run(server.call_tool("semarail_get_context", {"question": "Revenue?"}))
        asyncio.run(
            server.call_tool(
                "semarail_governed_query",
                {"question": "Revenue?", "semantic_sql": "SELECT revenue FROM sales"},
            )
        )
        self.assertEqual(transport.calls[0], ("context.ask", {"question": "Revenue?"}))
        query_params = transport.calls[1][1]
        self.assertEqual(transport.calls[1][0], "query.run")
        self.assertEqual(set(query_params), {"question", "semanticSql", "chartIntent", "queryId"})
        self.assertNotIn("authorizationPolicy", str(transport.calls))
        self.assertNotIn("subject", str(transport.calls))
        with self.assertRaises(ToolError):
            asyncio.run(
                server.call_tool(
                    "semarail_get_context",
                    {"question": "Revenue?", "authorizationPolicy": {"effect": "allow"}},
                )
            )
        self.assertEqual(len(transport.calls), 2)

    def test_core_request_uses_fixed_rpc_path_and_bearer_header(self) -> None:
        token = "sr_key_" + "a" * 40
        transport = CoreHttpTransport(self.endpoint, token)
        result = asyncio.run(transport.call("context.ask", {"question": "Revenue?"}))

        self.assertEqual(result["method"], "context.ask")
        self.assertEqual(len(_CoreHandler.requests), 1)
        request = _CoreHandler.requests[0]
        self.assertEqual(request["path"], "/api/v1/runtime/rpc")
        self.assertEqual(request["authorization"], f"Bearer {token}")
        self.assertEqual(
            {key for key in request["body"] if key not in {"id"}},
            {"protocolVersion", "method", "params"},
        )
        self.assertNotIn(token, str(result))

    def test_official_mcp_client_talks_stdio_while_bridge_calls_core(self) -> None:
        token = "sr_live_" + "e" * 24 + "_" + "f" * 32

        async def exercise() -> tuple[list[str], Any]:
            environment = dict(os.environ)
            environment["SEMARAIL_MCP_TOKEN"] = token
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "server.stdio_mcp", "--endpoint", self.endpoint],
                cwd=str(Path(__file__).resolve().parents[2]),
                env=environment,
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    called = await session.call_tool(
                        "semarail_get_context", {"question": "Revenue?"}
                    )
                    return [tool.name for tool in tools.tools], called

        tools, called = asyncio.run(exercise())
        self.assertEqual(len(tools), 5)
        self.assertFalse(called.isError)
        self.assertEqual(_CoreHandler.requests[-1]["authorization"], f"Bearer {token}")
        self.assertEqual(_CoreHandler.requests[-1]["body"]["method"], "context.ask")
        self.assertNotIn(token, str(called))

    def test_unauthorized_and_forbidden_errors_are_stable_and_redacted(self) -> None:
        token = "sr_key_" + "b" * 40
        transport = CoreHttpTransport(self.endpoint, token)
        for status, expected in ((401, "UNAUTHENTICATED"), (403, "FORBIDDEN")):
            _CoreHandler.response_status = status
            _CoreHandler.response_error = {
                "code": expected,
                "message": f"Bearer {token} postgresql://user:secret@host/db",
            }
            with self.assertRaises(ToolError) as caught:
                asyncio.run(transport.call("project.describe", {}))
            message = str(caught.exception)
            self.assertIn(expected, message)
            self.assertNotIn(token, message)
            self.assertNotIn("secret", message)

    def test_core_cannot_reflect_bridge_credential_into_tool_result(self) -> None:
        token = "sr_key_" + "f" * 40
        _CoreHandler.response_result = {"schemaVersion": 1, "debug": token}
        with self.assertRaises(ToolError) as caught:
            asyncio.run(CoreHttpTransport(self.endpoint, token).call("project.validate", {}))
        self.assertNotIn(token, str(caught.exception))

    def test_reads_protected_employee_session_and_honors_its_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            token = "sr_session_" + "c" * 24 + "_" + "d" * 32
            path.write_text(
                json.dumps(
                    {
                        "endpoint": self.endpoint,
                        "accessToken": token,
                        "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            if os.name != "nt":
                path.chmod(0o600)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_employee_session(path), (self.endpoint, token))
            with patch.dict(os.environ, {"SEMARAIL_MCP_TOKEN": ""}, clear=False):
                self.assertEqual(
                    resolve_authentication(
                        endpoint=None,
                        session_file=path,
                        token_env="SEMARAIL_MCP_TOKEN",
                    ),
                    (self.endpoint, token),
                )
                with self.assertRaisesRegex(ValueError, "different Core endpoint"):
                    resolve_authentication(
                        endpoint="http://127.0.0.1:1",
                        session_file=path,
                        token_env="SEMARAIL_MCP_TOKEN",
                    )

    def test_service_account_token_is_read_only_from_named_environment_variable(self) -> None:
        token = "sr_live_" + "d" * 24 + "_" + "e" * 32
        with patch.dict(os.environ, {"MY_SEMARAIL_TOKEN": token}, clear=False):
            resolved = resolve_authentication(
                endpoint=self.endpoint,
                session_file="missing.json",
                token_env="MY_SEMARAIL_TOKEN",
            )
        self.assertEqual(resolved, (self.endpoint, token))

    def test_service_token_rejects_bootstrap_and_malformed_managed_credentials(self) -> None:
        for token in (
            "bootstrap-administrator-token-that-is-long-enough",
            "sr_key_" + "a" * 64,
            "sr_live_" + "a" * 23 + "_" + "b" * 32,
            "sr_session_" + "a" * 24 + "_" + "b" * 32,
        ):
            with self.subTest(token_prefix=token.split("_")[0]):
                with patch.dict(os.environ, {"SEMARAIL_MCP_TOKEN": token}, clear=False):
                    with self.assertRaisesRegex(ValueError, "service-account key"):
                        resolve_authentication(
                            endpoint=self.endpoint,
                            session_file="missing.json",
                            token_env="SEMARAIL_MCP_TOKEN",
                        )

    def test_endpoint_rejects_paths_credentials_and_non_tls_remote_hosts(self) -> None:
        self.assertEqual(normalize_core_endpoint(f"{self.endpoint}/"), self.endpoint)
        for value in (
            f"{self.endpoint}/api/v1/runtime/rpc",
            "http://example.com:48763",
            "https://user:password@example.com",
            "https://example.com/?token=secret",
        ):
            with self.assertRaises(ValueError):
                normalize_core_endpoint(value)


if __name__ == "__main__":
    unittest.main()
