from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests = []

    def dispatch(self, request):
        self.requests.append(request)
        if request["method"] == "health":
            result = {"status": "ok", "protocolVersion": "1", "wrenAvailable": True}
        elif request["method"] == "project.validate":
            result = {"valid": True, "projectRevision": "sha256:test"}
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
        self.dispatcher = RecordingDispatcher()
        self.token = "test-token-that-is-at-least-thirty-two-characters"
        self.authorization = f"Bearer {self.token}"
        self.gateway = RuntimeRpcGateway(self.project, self.dispatcher, auth_token=self.token)

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
        self.assertFalse(response["result"]["capabilities"]["governedQuery"])
        self.assertEqual(response["result"]["readiness"]["governedQuery"], "setup_required")

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

    def test_runtime_rpc_requires_a_bearer_token(self) -> None:
        status, response = self.gateway.dispatch(
            {"protocolVersion": "1", "id": "health-2", "method": "health", "params": {}},
            "Bearer wrong-token-that-is-at-least-thirty-two-characters",
        )

        self.assertEqual(status, 401)
        self.assertEqual(response["error"]["code"], "UNAUTHENTICATED")
        self.assertEqual(self.dispatcher.requests, [])


if __name__ == "__main__":
    unittest.main()
