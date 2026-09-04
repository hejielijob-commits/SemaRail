from __future__ import annotations

import hashlib
import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from server.app import SemanticConsoleHTTPServer, create_app
from server.artifact_store import (
    ARTIFACT_TTL_SECONDS,
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    ArtifactStore,
)
from server.models import DatasourceRecord
from server.project import ProjectStore
from server.remote_mcp import create_remote_mcp_server
from server.runtime_rpc import RuntimeRpcGateway
from server.service import SemanticConsoleService


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class _Validator:
    def health(self) -> dict[str, Any]:
        return {"available": True}

    def validate(self, _project_dir: Path) -> dict[str, Any]:
        return {"valid": True, "errors": [], "warnings": [], "errorCount": 0, "warningCount": 0}

    def build(self, _project_dir: Path) -> dict[str, Any]:
        return {"models": []}


class _Dispatcher:
    def __init__(self, gateway: RuntimeRpcGateway | None = None) -> None:
        self.gateway = gateway
        self.requests: list[dict[str, Any]] = []

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        artifact_request = request["params"]["artifactRequest"]
        body = b"order_id,total\n1,12\n"
        # The gateway's shared sink is represented by the dispatcher writing
        # through its injected store in this test seam. Production sidecar
        # wiring uses the same trusted artifact id and Core-generated name.
        assert self.gateway is not None
        metadata = self.gateway.artifacts.metadata(artifact_request["id"])
        digest = hashlib.sha256(body).hexdigest()
        self.gateway.artifacts.finalize(
            artifact_request["id"],
            body,
            sidecar_metadata={
                "id": artifact_request["id"],
                "format": "csv",
                "fileName": metadata.filename,
                "rowCount": 1,
                "sizeBytes": len(body),
                "sha256": digest,
                "expiresAt": metadata.expires_at,
            },
        )
        return {
            "protocolVersion": "1",
            "id": request["id"],
            "ok": True,
            "result": {
                "schemaVersion": 1,
                "queryId": request["params"]["queryId"],
                "status": "success",
                "semanticSql": request["params"]["semanticSql"],
                "columns": [],
                "previewRows": [],
                "stats": {"returnedRows": 1, "durationMs": 1, "truncated": True},
                "artifact": {
                    "id": artifact_request["id"],
                    "format": "csv",
                    "fileName": metadata.filename,
                    "rowCount": 1,
                    "sizeBytes": len(body),
                    "sha256": digest,
                    "expiresAt": metadata.expires_at,
                },
            },
        }


class _InlineDispatcher:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "protocolVersion": "1",
            "id": request["id"],
            "ok": True,
            "result": {
                "schemaVersion": 1,
                "queryId": request["params"]["queryId"],
                "status": "success",
                "semanticSql": request["params"]["semanticSql"],
                "columns": [{"name": "total", "type": "BIGINT", "semanticRole": "measure"}],
                "previewRows": [{"total": "12"}],
                "stats": {"returnedRows": 1, "durationMs": 1, "truncated": False},
            },
        }


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-artifact-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = _Clock()
        self.store = ArtifactStore(self.root / "state", clock=self.clock)

    def _reserve(self):
        return self.store.reserve(
            subject_id="subject-a",
            organization_id="organization-a",
            credential_id="credential-a",
            query_id="query-a",
            datasource_id="datasource-a",
            policy_versions=("policy-a:1",),
        )

    def test_random_binding_hashes_and_atomic_finalize(self) -> None:
        reservation = self._reserve()
        self.assertTrue(reservation.id.startswith("art_"))
        self.assertNotEqual(reservation.id, reservation.filename)
        self.assertEqual(reservation.organization_id, "organization-a")
        self.assertEqual(reservation.query_id, "query-a")
        database = self.store.database_path.read_bytes()
        self.assertNotIn(reservation.token.encode(), database)
        body = b"id,total\n1,5\n"
        metadata = self.store.finalize(reservation, body)
        self.assertEqual(metadata.status, "ready")
        self.assertEqual(metadata.size, len(body))
        self.assertEqual(metadata.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(list(self.store.root.glob("*.tmp")), [])
        download = self.store.resolve_download(
            reservation.id,
            reservation.token,
            current_datasource_id="datasource-a",
            current_policy_versions=("policy-a:1",),
        )
        self.assertEqual(download.path.read_bytes(), body)

    def test_wrong_token_is_uniform_404_and_expiry_is_410(self) -> None:
        reservation = self._reserve()
        self.store.finalize(reservation, b"x")
        with self.assertRaises(ArtifactError) as wrong:
            self.store.resolve_download(
                reservation.id,
                "wrong-token",
                current_datasource_id="datasource-a",
                current_policy_versions=("policy-a:1",),
            )
        self.assertEqual(wrong.exception.status, 404)
        with self.assertRaises(ArtifactError) as missing:
            self.store.resolve_download(
                "art_" + "0" * 32,
                reservation.token,
                current_datasource_id="datasource-a",
                current_policy_versions=("policy-a:1",),
            )
        self.assertEqual(missing.exception.status, 404)
        self.clock.advance(ARTIFACT_TTL_SECONDS)
        with self.assertRaises(ArtifactError) as expired:
            self.store.resolve_download(
                reservation.id,
                reservation.token,
                current_datasource_id="datasource-a",
                current_policy_versions=("policy-a:1",),
            )
        self.assertEqual(expired.exception.status, 410)
        self.assertEqual(self.store.metadata(reservation.id).status, "expired")
        self.assertFalse((self.store.root / reservation.filename).exists())

    def test_server_can_configure_a_bounded_artifact_ttl(self) -> None:
        short = ArtifactStore(self.root / "short-state", clock=self.clock, ttl_seconds=60)
        reservation = short.reserve(
            subject_id="subject-a",
            organization_id="organization-a",
            credential_id="credential-a",
            query_id="query-a",
            datasource_id="datasource-a",
            policy_versions=("policy-a:1",),
        )
        self.assertEqual(
            datetime.fromisoformat(reservation.expires_at.replace("Z", "+00:00"))
            - datetime.fromisoformat(reservation.created_at.replace("Z", "+00:00")),
            timedelta(seconds=60),
        )
        for invalid in (0, 59, 86401, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ArtifactStore(self.root / f"invalid-{invalid}", ttl_seconds=invalid)

    def test_expired_and_orphan_tmp_files_are_cleaned(self) -> None:
        reservation = self._reserve()
        self.store.finalize(reservation, b"x")
        orphan = self.store.root / "orphan.tmp"
        orphan.write_bytes(b"orphan")
        self.clock.advance(ARTIFACT_TTL_SECONDS)
        self.store.cleanup()
        self.assertFalse(orphan.exists())
        self.assertFalse((self.store.root / reservation.filename).exists())

    def test_cleanup_preserves_temporary_files_for_live_pending_reservations(self) -> None:
        reservation = self._reserve()
        sidecar_temp = self.store.root / f".{reservation.id}.writer.csv.tmp"
        core_temp = self.store.root / f"{reservation.filename}.writer.tmp"
        orphan = self.store.root / "orphan.tmp"
        for candidate in (sidecar_temp, core_temp, orphan):
            candidate.write_bytes(b"partial")

        self.store.cleanup()

        self.assertTrue(sidecar_temp.exists())
        self.assertTrue(core_temp.exists())
        self.assertFalse(orphan.exists())

    def test_creation_cleanup_is_limited_to_one_hundred_records(self) -> None:
        for index in range(101):
            self.store.reserve(
                subject_id="subject-a",
                organization_id="organization-a",
                credential_id="credential-a",
                query_id=f"query-{index}",
                datasource_id="datasource-a",
                policy_versions=("policy-a:1",),
            )
        self.clock.advance(ARTIFACT_TTL_SECONDS)
        self._reserve()
        connection = sqlite3.connect(self.store.database_path)
        try:
            expired = connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE status='expired'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(expired, 100)
        self.store.cleanup()
        connection = sqlite3.connect(self.store.database_path)
        try:
            expired = connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE status='expired'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(expired, 101)

    def test_oversize_content_never_becomes_ready(self) -> None:
        reservation = self._reserve()
        with self.assertRaises(ArtifactError) as oversized:
            self.store.finalize(reservation, b"x" * (MAX_ARTIFACT_BYTES + 1))
        self.assertEqual(oversized.exception.status, 413)
        self.assertEqual(self.store.metadata(reservation.id).status, "failed")
        self.assertEqual(list(self.store.root.glob("*.tmp")), [])

    def test_download_rejects_changed_context(self) -> None:
        reservation = self._reserve()
        self.store.finalize(reservation, b"x")
        with self.assertRaises(ArtifactError) as source:
            self.store.resolve_download(
                reservation.id,
                reservation.token,
                current_datasource_id="datasource-b",
                current_policy_versions=("policy-a:1",),
            )
        self.assertEqual(source.exception.status, 410)
        with self.assertRaises(ArtifactError) as policy:
            self.store.resolve_download(
                reservation.id,
                reservation.token,
                current_datasource_id="datasource-a",
                current_policy_versions=("policy-a:2",),
            )
        self.assertEqual(policy.exception.status, 410)


class ArtifactGatewayHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-artifact-http-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        project_dir = root / "project"
        project_dir.mkdir()
        (project_dir / "wren_project.yml").write_text(
            "schema_version: 5\nname: artifact-http\ndata_source: postgres\n", encoding="utf-8"
        )
        project = ProjectStore(project_dir, state_dir=root / "state", validator=_Validator())
        project.datasource_records()["source-a"] = DatasourceRecord("source-a", "Warehouse", "postgres", {"database": "db"})
        project.active_datasource_id = "source-a"
        project.save_datasources()
        self.gateway = RuntimeRpcGateway(
            project,
            dispatcher=None,
            auth_token="bootstrap-token-that-is-at-least-thirty-two-characters",
        )
        account = self.gateway.access_control.create_service_account("Artifact Agent")
        self.account_id = account.id
        self.policy_document = {
            "schemaVersion": 1,
            "datasourceId": "source-a",
            "projects": ["artifact-http"],
            "tools": ["query:execute"],
            "tables": {"public.orders": {"effect": "allow"}},
        }
        policy = self.gateway.access_control.create_policy(
            "Artifact policy",
            self.policy_document,
        )
        self.policy_id = policy["id"]
        self.gateway.access_control.bind_policy(account.id, policy["id"])
        issued = self.gateway.access_control.issue_api_key(account.id)
        self.credential_id = issued["credential"]["id"]
        self.authorization = f"Bearer {issued['apiKey']}"
        self.gateway.dispatcher = _Dispatcher(self.gateway)

    def _run_query(self) -> dict[str, Any]:
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "artifact-query",
                "method": "query.run",
                "params": {"question": "Orders", "semanticSql": "SELECT * FROM orders", "queryId": "artifact-q"},
            },
            authorization=self.authorization,
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.last_query_result = response["result"]
        return response["result"]["artifact"]

    def test_gateway_pins_artifact_request_and_http_streams_attachment(self) -> None:
        application = create_app(SemanticConsoleService(self.gateway.project), runtime_rpc=self.gateway)
        server = SemanticConsoleHTTPServer(("127.0.0.1", 0), application)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            artifact = self._run_query()
            self.assertEqual(self.last_query_result["schemaVersion"], 2)
            self.assertEqual(self.last_query_result["delivery"], "artifact")
            self.assertEqual(self.last_query_result["stats"]["previewedRows"], 0)
            self.assertNotIn("chart", self.last_query_result)
            internal = self.gateway.dispatcher.requests[-1]["params"]
            self.assertEqual(internal["artifactRequest"]["id"], artifact["id"])
            self.assertEqual(internal["artifactRequest"]["maxBytes"], MAX_ARTIFACT_BYTES)
            self.assertEqual(internal["artifactRequest"]["directory"], str(self.gateway.artifacts.root))
            self.assertNotIn("token", internal["artifactRequest"])
            self.assertIn("downloadUrl", artifact)
            request = urllib.request.Request(
                artifact["downloadUrl"],
                headers={"Host": f"127.0.0.1:{server.server_port}"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
                self.assertEqual(response.read(), b"order_id,total\n1,12\n")
            bad = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/v1/artifacts/{artifact['id']}/download?token=wrong",
                headers={"Host": f"127.0.0.1:{server.server_port}"},
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(bad, timeout=3)
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_small_result_is_published_as_v2_inline_and_reservation_is_removed(self) -> None:
        dispatcher = _InlineDispatcher()
        self.gateway.dispatcher = dispatcher
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "inline-query",
                "method": "query.run",
                "params": {"question": "Total", "semanticSql": "SELECT total FROM orders", "queryId": "inline-q"},
            },
            authorization=self.authorization,
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(
            response["result"],
            {
                "schemaVersion": 2,
                "queryId": "inline-q",
                "status": "success",
                "semanticSql": "SELECT total FROM orders",
                "delivery": "inline",
                "columns": [{"name": "total", "type": "BIGINT", "semanticRole": "measure"}],
                "previewRows": [{"total": "12"}],
                "stats": {"returnedRows": 1, "previewedRows": 1, "durationMs": 1, "truncated": False},
            },
        )
        reservation_id = dispatcher.requests[-1]["params"]["artifactRequest"]["id"]
        self.assertEqual(self.gateway.artifacts.metadata(reservation_id).status, "failed")

    def test_public_query_cannot_supply_artifact_request_or_limits(self) -> None:
        status, response = self.gateway.dispatch(
            {
                "protocolVersion": "1",
                "id": "artifact-spoof",
                "method": "query.run",
                "params": {
                    "question": "Orders",
                    "semanticSql": "SELECT * FROM orders",
                    "queryId": "artifact-q",
                    "artifactRequest": {"path": "C:/secret", "maxBytes": 1},
                },
            },
            authorization=self.authorization,
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(self.gateway.dispatcher.requests, [])

    def test_download_is_revoked_with_issuing_credential(self) -> None:
        artifact = self._run_query()
        parsed = urlsplit(artifact["downloadUrl"])
        token = parsed.query.removeprefix("token=")
        self.gateway.access_control.revoke_credential(self.credential_id)
        with self.assertRaises(ArtifactError) as revoked:
            self.gateway.download_artifact(artifact["id"], token)
        self.assertEqual(revoked.exception.status, 410)
        self.assertEqual(self.gateway.artifacts.metadata(artifact["id"]).status, "expired")

    def test_download_is_revoked_when_issuing_subject_is_disabled(self) -> None:
        artifact = self._run_query()
        parsed = urlsplit(artifact["downloadUrl"])
        token = parsed.query.removeprefix("token=")
        self.gateway.access_control.set_subject_status(self.account_id, "disabled")
        with self.assertRaises(ArtifactError) as revoked:
            self.gateway.download_artifact(artifact["id"], token)
        self.assertEqual(revoked.exception.status, 410)
        self.assertEqual(self.gateway.artifacts.metadata(artifact["id"]).status, "expired")

    def test_download_is_revoked_when_policy_version_changes(self) -> None:
        artifact = self._run_query()
        parsed = urlsplit(artifact["downloadUrl"])
        token = parsed.query.removeprefix("token=")
        updated = dict(self.policy_document)
        updated["limits"] = {"maxRows": 100}
        self.gateway.access_control.update_policy(self.policy_id, updated)
        with self.assertRaises(ArtifactError) as changed:
            self.gateway.download_artifact(artifact["id"], token)
        self.assertEqual(changed.exception.status, 410)
        self.assertEqual(self.gateway.artifacts.metadata(artifact["id"]).status, "expired")

    def test_download_is_revoked_when_active_datasource_changes(self) -> None:
        artifact = self._run_query()
        parsed = urlsplit(artifact["downloadUrl"])
        token = parsed.query.removeprefix("token=")
        self.gateway.project.datasource_records()["source-b"] = DatasourceRecord(
            "source-b", "Archive", "postgres", {"database": "archive"}
        )
        self.gateway.project.active_datasource_id = "source-b"
        self.gateway.project.save_datasources()
        with self.assertRaises(ArtifactError) as changed:
            self.gateway.download_artifact(artifact["id"], token)
        self.assertEqual(changed.exception.status, 410)
        self.gateway.project.active_datasource_id = "source-a"
        self.gateway.project.save_datasources()
        with self.assertRaises(ArtifactError) as still_revoked:
            self.gateway.download_artifact(artifact["id"], token)
        self.assertEqual(still_revoked.exception.status, 410)

    def test_remote_mcp_custom_route_streams_the_same_artifact(self) -> None:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def exercise() -> None:
            server = create_remote_mcp_server(
                project=self.gateway.project.project_dir,
                gateway=self.gateway,
                host="127.0.0.1",
                port=48764,
                allowed_hosts=["testserver"],
            )
            app = server.streamable_http_app()
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                    headers={"Authorization": self.authorization},
                ) as client:
                    async with streamable_http_client(
                        "http://testserver/mcp", http_client=client, terminate_on_close=False
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            query = await session.call_tool(
                                "semarail_governed_query",
                                {"question": "Orders", "semantic_sql": "SELECT * FROM orders"},
                            )
                            self.assertFalse(query.isError)
                            self.assertIsInstance(query.structuredContent, dict)
                            payload = query.structuredContent
                            assert isinstance(payload, dict)
                            self.assertEqual(payload["schemaVersion"], 2)
                            self.assertEqual(payload["delivery"], "artifact")
                            artifact = payload["artifact"]
                    response = await client.get(
                        urlsplit(artifact["downloadUrl"]).path
                        + "?"
                        + urlsplit(artifact["downloadUrl"]).query,
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
                    self.assertEqual(response.content, b"order_id,total\n1,12\n")
                    download = self.root / "codex-agent-download.csv"
                    download.write_bytes(response.content)
                    import duckdb

                    with duckdb.connect(":memory:") as database:
                        total = database.execute(
                            "SELECT SUM(total) FROM read_csv_auto(?)", [str(download)]
                        ).fetchone()
                    self.assertEqual(total, (12,))

        asyncio.run(exercise())
        download_events = [
            event
            for event in self.gateway.access_control.list_audit()
            if event["action"] == "artifact.download"
        ]
        self.assertEqual(len(download_events), 1)
        self.assertEqual(download_events[0]["decision"], "allowed")
        self.assertEqual(set(download_events[0]["details"]), {"artifactId", "queryId", "transport"})
        self.assertEqual(download_events[0]["details"]["transport"], "remote-mcp")
        serialized = json.dumps(download_events, ensure_ascii=False)
        for sensitive in ("token=", "downloadUrl", "SELECT * FROM orders", "order_id,total", "1,12"):
            self.assertNotIn(sensitive, serialized)


if __name__ == "__main__":
    unittest.main()
