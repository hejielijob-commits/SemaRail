from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from server.project import ProjectStore
from server.service import SemanticConsoleService
from server.view_preview import ViewPreviewError, ViewPreviewService


class FakeDispatcher:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []
        self.stage_paths: list[Path] = []

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        stage = Path(request["params"]["projectDir"])
        self.stage_paths.append(stage)
        assert stage.is_dir()
        assert (stage / "views" / "daily_orders" / "sql.yml").is_file()
        return self.response


class FakePreview:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, payload))
        return {
            "schemaVersion": 1,
            "queryId": "preview-1",
            "status": "success",
            "semanticSql": 'SELECT * FROM "daily_orders"',
            "nativeSql": "SELECT 1 AS count",
            "columns": [{"name": "count", "type": "BIGINT", "semanticRole": "measure"}],
            "previewRows": [{"count": 1}],
            "stats": {"returnedRows": 1, "durationMs": 2.0, "truncated": False},
        }


class ViewPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semantic-console-view-preview-")
        root = Path(self.temp.name)
        self.project_dir = root / "project"
        self.state_dir = root / "state"
        self.project_dir.mkdir()
        (self.project_dir / "wren_project.yml").write_text(
            "schema_version: 5\nname: preview\ndata_source: postgres\n",
            encoding="utf-8",
        )
        view_dir = self.project_dir / "views" / "daily_orders"
        view_dir.mkdir(parents=True)
        (view_dir / "metadata.yml").write_text("name: daily_orders\n", encoding="utf-8")
        (view_dir / "sql.yml").write_text("statement: SELECT * FROM orders\n", encoding="utf-8")
        self.store = ProjectStore(self.project_dir, state_dir=self.state_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preview_uses_draft_stage_and_sidecar_query_run(self) -> None:
        response = {
            "protocolVersion": "1",
            "id": "rpc",
            "ok": True,
            "result": {
                "schemaVersion": 1,
                "queryId": "preview",
                "status": "success",
                "semanticSql": 'SELECT * FROM "daily_orders"',
                "nativeSql": "SELECT 1",
                "columns": [{"name": "value", "type": "BIGINT", "semanticRole": "measure"}],
                "previewRows": [{"value": 1}],
                "stats": {"returnedRows": 1, "durationMs": 1.0, "truncated": False},
            },
        }
        dispatcher = FakeDispatcher(response)
        service = ViewPreviewService(self.store, dispatcher)
        self.store.put_file(
            "views/daily_orders/sql.yml",
            "statement: SELECT id FROM orders\n",
        )

        result = service.run("daily_orders", {"limit": 25, "maxBytes": 4096, "timeoutMs": 1000})

        self.assertEqual(result["status"], "success")
        self.assertIn("projectRevision", result)
        self.assertFalse(result["stale"])
        request = dispatcher.requests[0]
        self.assertEqual(request["method"], "query.run")
        self.assertEqual(request["params"]["semanticSql"], 'SELECT * FROM "daily_orders"')
        self.assertEqual(request["params"]["maxRows"], 25)
        self.assertEqual(request["params"]["previewRows"], 25)
        self.assertEqual(request["params"]["maxPreviewBytes"], 4096)
        self.assertEqual(request["params"]["timeoutMs"], 1000)
        self.assertFalse(dispatcher.stage_paths[0].exists())

    def test_preview_maps_stable_sidecar_error_without_internal_details(self) -> None:
        dispatcher = FakeDispatcher(
            {
                "protocolVersion": "1",
                "id": "rpc",
                "ok": False,
                "error": {
                    "code": "POLICY_DENIED",
                    "phase": "policy",
                    "message": "query denied by read-only SQL policy",
                    "retryable": False,
                },
            }
        )
        service = ViewPreviewService(self.store, dispatcher)

        with self.assertRaises(ViewPreviewError) as raised:
            service.run("daily_orders")

        self.assertEqual(raised.exception.code, "POLICY_DENIED")
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(raised.exception.details, {"phase": "policy", "retryable": False})
        self.assertFalse(dispatcher.stage_paths[0].exists())

    def test_preview_rejects_unbounded_or_unknown_requests(self) -> None:
        service = ViewPreviewService(self.store, FakeDispatcher({}))
        for payload in ({"limit": 201}, {"maxBytes": 1_048_577}, {"timeoutMs": 30_001}, {"sql": "SELECT 1"}):
            with self.subTest(payload=payload), self.assertRaises(ViewPreviewError) as raised:
                service.run("daily_orders", payload)
            self.assertEqual(raised.exception.code, "INVALID_PARAMS")
        with self.assertRaises(ViewPreviewError):
            service.run("bad name")

    def test_missing_sidecar_runtime_degrades_explicitly(self) -> None:
        service = ViewPreviewService(self.store, dispatcher=None)
        service.dispatcher = None
        with self.assertRaises(ViewPreviewError) as raised:
            service.run("daily_orders")
        self.assertEqual(raised.exception.code, "WREN_UNAVAILABLE")
        self.assertEqual(raised.exception.status, 503)

    def test_rest_route_checks_view_and_returns_preview(self) -> None:
        preview = FakePreview()
        service = SemanticConsoleService(self.store, view_preview=preview)  # type: ignore[arg-type]

        status, result = service.dispatch("POST", "/api/views/daily_orders/preview", body={"limit": 20})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "success")
        self.assertEqual(preview.calls, [("daily_orders", {"limit": 20})])

        status, result = service.dispatch("POST", "/api/views/missing/preview", body={})
        self.assertEqual(status, 404)
        self.assertEqual(result["__error__"]["code"], "VIEW_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
