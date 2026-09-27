from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from server.app import create_app
from server.project import ProjectError, ProjectStore, _file_digest
from server.runtime_rpc import RuntimeRpcGateway
from server.service import ApiServiceError, SemanticConsoleService
from sidecar.semantic_index import project_revision
from sidecar.semantic_retrieval import HybridSemanticRetriever
from sidecar.wren_adapter import _project_revision


class FakeValidator:
    def health(self):
        return {"available": True, "version": "0.13.2"}

    def validate(self, project_dir: Path):
        return {"valid": (project_dir / "wren_project.yml").is_file(), "errors": [], "warnings": [], "errorCount": 0, "warningCount": 0}

    def build(self, project_dir: Path):
        return {"models": []}


class FakeCursor:
    def __init__(self):
        self.description = []
        self.rows = []

    def execute(self, sql, params=()):
        if "SELECT 1" in sql:
            self.description = [("?column?",)]
            self.rows = [(1,)]
        elif "table_constraints" in sql:
            self.description = [("column_name",)]
            self.rows = [("id",)]
        elif "information_schema.columns" in sql:
            self.description = [("column_name",), ("data_type",), ("udt_name",), ("is_nullable",), ("ordinal_position",)]
            self.rows = [("id", "integer", "int4", "NO", 1), ("display_name", "character varying", "varchar", "YES", 2)]

    def fetchall(self):
        return self.rows

    def close(self):
        return None


class FakeConnection:
    autocommit = False

    def cursor(self):
        return FakeCursor()

    def close(self):
        return None


class SemanticConsoleServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="semantic-console-test-")
        self.addCleanup(self.temp.cleanup)
        self.tmp_path = Path(self.temp.name)

    def make_project(self, validator=None) -> ProjectStore:
        project = self.tmp_path / "project"
        project.mkdir()
        (project / "wren_project.yml").write_text("schema_version: 5\nname: demo\ndata_source: postgres\n", encoding="utf-8")
        return ProjectStore(project, state_dir=self.tmp_path / "state", validator=validator or FakeValidator())

    def test_publisher_and_runtime_share_project_revision_algorithm(self):
        store = self.make_project()
        project = store.project_dir
        source = project / "models" / "large" / "metadata.yml"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"name: large\n# " + b"x" * (1024 * 1024 + 17))
        target = project / "target" / "mdl.json"
        target.parent.mkdir()
        target.write_text('{"models": []}', encoding="utf-8")

        published_revision = _file_digest(project, exclude=store.state_dir)
        self.assertEqual(_project_revision(project), published_revision)
        self.assertEqual(project_revision(project), published_revision)

    def test_published_index_is_readable_at_runtime_revision(self):
        with patch.dict("os.environ", {"SEMARAIL_EMBEDDING_PROVIDER": "none"}):
            store = self.make_project()
            published = store.publish(label="revisioned-index")

        revision = published["version"]["revision"]
        reopened = HybridSemanticRetriever(
            embedder=None, storage_path=store.state_dir / "semantic-index"
        )
        self.assertEqual(_project_revision(store.project_dir), revision)
        self.assertEqual(reopened.active_revision, revision)
        self.assertEqual(reopened.status(revision).state, "degraded")
        self.assertEqual(reopened.search("demo", revision=revision).status.revision, revision)

    def test_datasource_is_redacted_and_survives_restart(self):
        store = self.make_project()
        service = SemanticConsoleService(store)
        public = service.create_datasource(
            {"name": "local", "type": "postgres", "connection": {"host": "localhost", "password": "never-return"}}
        )
        self.assertNotIn("never-return", json.dumps(public))
        self.assertTrue(public["hasPassword"])
        self.assertEqual(store.active_datasource_id, public["id"])
        secret_path = self.tmp_path / "state" / "datasources.secrets.json"
        self.assertIn("never-return", secret_path.read_text(encoding="utf-8"))

        restarted = SemanticConsoleService(ProjectStore(self.tmp_path / "project", state_dir=self.tmp_path / "state", validator=FakeValidator()))
        self.assertTrue(restarted.list_datasources()[0]["hasPassword"])
        self.assertNotIn("never-return", json.dumps(restarted.list_datasources()))
        self.assertEqual(restarted.project_overview()["activeDatasource"]["id"], public["id"])

    def test_create_and_update_reject_unavailable_datasource_drivers(self):
        service = SemanticConsoleService(self.make_project())

        with patch("server.drivers._module_available", return_value=False):
            with self.assertRaises(ApiServiceError) as create_error:
                service.create_datasource({"name": "missing", "type": "mysql", "connection": {}})
        self.assertEqual(create_error.exception.code, "UNSUPPORTED_DATASOURCE")

        with patch("server.drivers._module_available", return_value=True):
            datasource = service.create_datasource({"name": "available", "type": "postgres", "connection": {}})
        with patch("server.drivers._module_available", return_value=False):
            with self.assertRaises(ApiServiceError) as update_error:
                service.update_datasource(datasource["id"], {"type": "mysql"})
        self.assertEqual(update_error.exception.code, "UNSUPPORTED_DATASOURCE")

    def test_drafts_publish_without_touching_git_and_rollback(self):
        store = self.make_project()
        git = self.tmp_path / "project" / ".git"
        git.mkdir()
        (git / "HEAD").write_text("main", encoding="utf-8")
        service = SemanticConsoleService(store)
        result = service.put_file(
            "models/orders/metadata.yml",
            {"content": "name: orders\ntable_reference:\n  schema: public\n  table: orders\ncolumns:\n- name: id\n  type: INTEGER\n"},
        )
        self.assertTrue(result["draft"])
        self.assertFalse((self.tmp_path / "project/models/orders/metadata.yml").exists())
        published = service.publish_project({"label": "first"})
        self.assertTrue((self.tmp_path / "project/models/orders/metadata.yml").is_file())
        self.assertEqual((git / "HEAD").read_text(encoding="utf-8"), "main")
        self.assertEqual(published["version"]["revision"], published["project"]["revision"])

        service.put_file("models/orders/metadata.yml", {"content": "name: changed\n"})
        service.publish_project({"label": "second"})
        self.assertIn("changed", (self.tmp_path / "project/models/orders/metadata.yml").read_text(encoding="utf-8"))
        versions = service.versions()
        service.rollback(versions[-1]["id"])
        self.assertIn("orders", (self.tmp_path / "project/models/orders/metadata.yml").read_text(encoding="utf-8"))

    def test_publish_stages_index_before_source_swap_and_activates_revision(self):
        events: list[tuple[str, str]] = []

        class Lifecycle:
            def stage(self, manifest, project_dir, revision):
                self.assert_stage = (project_dir / "wren_project.yml").is_file()
                events.append(("stage", revision))

            def activate(self, revision):
                events.append(("activate", revision))

        project = self.tmp_path / "indexed-project"
        project.mkdir()
        (project / "wren_project.yml").write_text(
            "schema_version: 5\nname: demo\ndata_source: postgres\n", encoding="utf-8"
        )
        lifecycle = Lifecycle()
        store = ProjectStore(
            project,
            state_dir=self.tmp_path / "indexed-state",
            validator=FakeValidator(),
            semantic_index_lifecycle=lifecycle,
        )
        published = store.publish(label="indexed")
        self.assertTrue(lifecycle.assert_stage)
        self.assertEqual([name for name, _ in events], ["stage", "activate"])
        self.assertEqual(events[0][1], published["version"]["revision"])
        self.assertEqual(events[1][1], published["version"]["revision"])

    def test_rollback_rebuilds_and_activates_the_snapshot_index_revision(self):
        events: list[tuple[str, str]] = []

        class Lifecycle:
            def stage(self, _manifest, _project_dir, revision):
                events.append(("stage", revision))

            def activate(self, revision):
                events.append(("activate", revision))

        project = self.tmp_path / "rollback-index-project"
        project.mkdir()
        (project / "wren_project.yml").write_text(
            "schema_version: 5\nname: first\ndata_source: postgres\n",
            encoding="utf-8",
        )
        store = ProjectStore(
            project,
            state_dir=self.tmp_path / "rollback-index-state",
            validator=FakeValidator(),
            semantic_index_lifecycle=Lifecycle(),
        )
        first = store.publish(label="first")
        store.put_file(
            "wren_project.yml",
            "schema_version: 5\nname: second\ndata_source: postgres\n",
        )
        store.publish(label="second")
        events.clear()

        rolled_back = store.rollback(first["version"]["id"])

        self.assertEqual(
            events,
            [
                ("stage", first["version"]["revision"]),
                ("activate", first["version"]["revision"]),
            ],
        )
        self.assertEqual(
            rolled_back["version"]["revision"],
            first["version"]["revision"],
        )

    def test_index_activation_failure_restores_source_and_keeps_draft(self):
        class Lifecycle:
            def stage(self, _manifest, _project_dir, _revision):
                return None

            def activate(self, _revision):
                raise RuntimeError("simulated atomic pointer failure")

        project = self.tmp_path / "failed-index-project"
        project.mkdir()
        original = "schema_version: 5\nname: before\ndata_source: postgres\n"
        changed = "schema_version: 5\nname: after\ndata_source: postgres\n"
        (project / "wren_project.yml").write_text(original, encoding="utf-8")
        store = ProjectStore(
            project,
            state_dir=self.tmp_path / "failed-index-state",
            validator=FakeValidator(),
            semantic_index_lifecycle=Lifecycle(),
        )
        store.put_file("wren_project.yml", changed)

        with self.assertRaisesRegex(ProjectError, "project could not be published") as caught:
            store.publish(label="must-fail")

        self.assertEqual(caught.exception.code, "PUBLISH_FAILED")
        self.assertEqual((project / "wren_project.yml").read_text(encoding="utf-8"), original)
        self.assertEqual(store.read_file("wren_project.yml")["content"], changed)
        self.assertEqual(store.versions(), [])

    def test_admin_can_view_and_rebuild_published_semantic_index(self):
        events: list[tuple[str, str, str]] = []

        class Lifecycle:
            active: str | None = None

            def stage(self, _manifest, project_dir, revision):
                name = yaml.safe_load((project_dir / "wren_project.yml").read_text(encoding="utf-8"))["name"]
                events.append(("stage", revision, name))

            def activate(self, revision):
                self.active = revision
                events.append(("activate", revision, ""))

            def status(self, revision=None):
                return {
                    "status": "ready" if self.active == revision else "stale",
                    "activeRevision": self.active,
                    "indexedRevision": revision,
                    "documentCount": 7,
                    "backend": "hybrid",
                    "embeddingModelId": "multilingual-test",
                    "embeddingModelVersion": "v1",
                    "embeddingDimension": 384,
                    "indexBuildVersion": 1,
                    "lastBuildAt": "2026-09-18T00:00:00Z",
                    "buildDurationMs": 12.5,
                    "storagePath": "must-not-leak",
                }

        project = self.tmp_path / "rebuild-index-project"
        project.mkdir()
        (project / "wren_project.yml").write_text(
            "schema_version: 5\nname: published\ndata_source: postgres\n", encoding="utf-8"
        )
        lifecycle = Lifecycle()
        store = ProjectStore(
            project,
            state_dir=self.tmp_path / "rebuild-index-state",
            validator=FakeValidator(),
            semantic_index_lifecycle=lifecycle,
        )
        store.put_file(
            "wren_project.yml",
            "schema_version: 5\nname: draft\ndata_source: postgres\n",
        )
        service = SemanticConsoleService(store)

        status_code, rebuilt = service.dispatch(
            "POST", "/api/project/semantic-index/rebuild"
        )
        read_code, status = service.dispatch("GET", "/api/project/semantic-index")

        self.assertEqual((status_code, read_code), (200, 200))
        self.assertEqual(events[0][2], "published")
        self.assertEqual(rebuilt["embeddingDimension"], 384)
        self.assertEqual(status["documentCount"], 7)
        self.assertNotIn("storagePath", status)
        self.assertEqual(store.read_file("wren_project.yml")["content"].splitlines()[1], "name: draft")

    def test_secret_content_and_traversal_are_rejected(self):
        service = SemanticConsoleService(self.make_project())
        with self.assertRaises(ApiServiceError):
            service.put_file("../credentials.yml", {"content": "password: bad"})
        with self.assertRaises(ApiServiceError):
            service.put_file("knowledge/rules/rule.md", {"content": "dsn: postgres://u:p@host/db"})
        with self.assertRaises(ApiServiceError):
            service.put_file("knowledge/rules/clickhouse.md", {"content": "clickhouse://u:p@host/analytics"})

    def test_postgres_schema_columns_and_model_generation(self):
        service = SemanticConsoleService(self.make_project(), connection_factory=lambda _values: FakeConnection())
        datasource = service.create_datasource({"name": "pg", "type": "postgres", "connection": {"host": "localhost"}})
        columns = service.datasource_columns(datasource["id"], "public", "customers")
        self.assertTrue(columns[0]["primaryKey"])
        generated = service.generate_model(datasource["id"], "public", "customers")
        self.assertTrue(generated["draft"])
        self.assertEqual(generated["model"]["columns"][0]["type"], "INTEGER")

    def test_embedded_app_returns_direct_shapes_and_safe_errors(self):
        project = self.make_project()
        token = "admin-token-that-is-at-least-thirty-two-characters"
        app = create_app(
            SemanticConsoleService(project),
            runtime_rpc=RuntimeRpcGateway(project, dispatcher=None, auth_token=token),
        )
        status, health = app.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["service"], "semantic-console")
        status, error = app.request(
            "GET", "/api/project/file?path=../secret", authorization=f"Bearer {token}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "INVALID_PATH")

    def test_mcp_integration_defaults_to_authenticated_remote_configuration(self):
        service = SemanticConsoleService(self.make_project())
        datasource = service.create_datasource(
            {"name": "pg", "type": "postgres", "connection": {"host": "localhost", "password": "never-return"}}
        )

        status, integration = service.dispatch("GET", "/api/mcp-integration")

        self.assertEqual(status, 200)
        self.assertEqual(integration["schemaVersion"], 2)
        self.assertEqual(integration["transport"], "streamable-http")
        self.assertEqual(integration["endpoint"]["authentication"], "bearer")
        self.assertEqual(integration["authentication"]["acceptedCredentials"], ["service_account_key", "employee_session"])
        self.assertEqual(integration["readiness"]["semanticContext"], "ready")
        self.assertEqual(integration["readiness"]["governedQuery"], "ready")
        self.assertEqual(integration["readiness"]["datasourceType"], "postgres")
        self.assertEqual(integration["readiness"]["endpointConfiguration"], "defaulted")
        remote = integration["clientConfig"]["mcpServers"]["semarail"]
        self.assertEqual(remote["transport"], "streamable-http")
        self.assertEqual(remote["headers"]["Authorization"], "Bearer ${SEMARAIL_TOKEN}")
        self.assertFalse(integration["trustedLocalOperator"]["userIsolation"])
        serialized = json.dumps(integration)
        self.assertNotIn("never-return", serialized)
        self.assertNotIn(str(self.tmp_path), serialized)
        self.assertNotIn("DATABASE_URL", serialized)
        self.assertNotIn(datasource.get("connection", {}).get("password", "never-return"), serialized)

    def test_mcp_integration_marks_mysql_governed_execution_unsupported(self):
        service = SemanticConsoleService(self.make_project())
        with patch("server.drivers._module_available", return_value=True):
            service.create_datasource({"name": "mysql", "type": "mysql", "connection": {"host": "localhost"}})

        integration = service.mcp_integration()

        self.assertEqual(integration["readiness"]["semanticContext"], "ready")
        self.assertEqual(integration["readiness"]["governedQuery"], "setup_required")
        self.assertEqual(integration["readiness"]["datasourceType"], "mysql")

    def test_mcp_integration_accepts_only_credential_free_remote_endpoint(self):
        service = SemanticConsoleService(self.make_project())
        with patch.dict(
            "os.environ", {"SEMARAIL_REMOTE_MCP_URL": "https://mcp.example.test/mcp"}
        ):
            configured = service.mcp_integration()
        self.assertEqual(configured["endpoint"]["url"], "https://mcp.example.test/mcp")
        self.assertEqual(configured["readiness"]["endpointConfiguration"], "ready")

        with patch.dict(
            "os.environ",
            {"SEMARAIL_REMOTE_MCP_URL": "https://user:secret@mcp.example.test/mcp?token=bad"},
        ):
            rejected = service.mcp_integration()
        self.assertEqual(rejected["endpoint"]["url"], "http://127.0.0.1:48764/mcp")
        self.assertEqual(rejected["readiness"]["endpointConfiguration"], "defaulted")
        self.assertNotIn("secret", json.dumps(rejected))

        for unsafe in (
            "http://mcp.example.test/mcp",
            "https://mcp.example.test:not-a-port/mcp",
            "http://[invalid",
        ):
            with self.subTest(unsafe=unsafe):
                with patch.dict("os.environ", {"SEMARAIL_REMOTE_MCP_URL": unsafe}):
                    rejected = service.mcp_integration()
                self.assertEqual(rejected["endpoint"]["url"], "http://127.0.0.1:48764/mcp")
                self.assertEqual(rejected["readiness"]["endpointConfiguration"], "defaulted")

    def test_business_model_projection_updates_wren_and_locales_as_drafts(self):
        store = self.make_project()
        model_dir = self.tmp_path / "project" / "models" / "orders"
        model_dir.mkdir(parents=True)
        model_dir.joinpath("metadata.yml").write_text(
            "name: orders\ntable_reference:\n  schema: public\n  table: orders\nprimary_key: id\ncolumns:\n  - name: id\n    type: INTEGER\n    is_primary_key: true\n  - name: amount\n    type: DECIMAL\n  - name: customer_id\n    type: INTEGER\n",
            encoding="utf-8",
        )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        model = snapshot["models"][0]
        self.assertEqual(model["columns"][0]["semanticRole"], "key")
        self.assertEqual(model["columns"][2]["semanticRole"], "key")
        model["displayName"] = {"zh-CN": "订单", "en-US": "Orders"}
        model["description"] = {"zh-CN": "销售订单", "en-US": "Sales orders"}
        model["columns"][1]["displayName"] = {"zh-CN": "销售额", "en-US": "Revenue"}
        model["columns"][1]["semanticRole"] = "measure"
        updated = service.save_semantic_model("orders", {**model, "expectedRevision": snapshot["revision"]})
        self.assertEqual(updated["models"][0]["displayName"]["zh-CN"], "订单")
        self.assertTrue(store.read_file("models/orders/metadata.yml")["draft"])
        self.assertIn("Sales orders", store.read_file("models/orders/metadata.yml")["content"])
        self.assertIn("销售额", store.read_file("semantic-console/locales.yml")["content"])
        diff = service.diff_file("models/orders/metadata.yml")
        self.assertTrue(diff["changed"])
        self.assertIn("draft/models/orders/metadata.yml", diff["diff"])

    def test_relationship_projection_validates_models_and_updates_graph_source(self):
        store = self.make_project()
        for name in ("orders", "customers"):
            model_dir = self.tmp_path / "project" / "models" / name
            model_dir.mkdir(parents=True)
            model_dir.joinpath("metadata.yml").write_text(
                f"name: {name}\ntable_reference:\n  schema: public\n  table: {name}\ncolumns:\n  - name: id\n    type: INTEGER\n",
                encoding="utf-8",
            )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        updated = service.save_relationships({
            "expectedRevision": snapshot["revision"],
            "relationships": [{
                "name": "orders_customer",
                "models": ["orders", "customers"],
                "joinType": "MANY_TO_ONE",
                "condition": "orders.customer_id = customers.id",
                "displayName": {"zh-CN": "订单客户", "en-US": "Order customer"},
                "description": {"zh-CN": "", "en-US": ""},
            }],
        })
        self.assertEqual(updated["relationships"][0]["joinType"], "MANY_TO_ONE")
        self.assertIn("orders_customer", store.read_file("relationships.yml")["content"])
        with self.assertRaises(ApiServiceError):
            service.save_relationships({
                "expectedRevision": updated["revision"],
                "relationships": [{"name": "bad", "models": ["orders", "missing"], "joinType": "MANY_TO_ONE", "condition": "x = y"}],
            })

    def test_semantic_dispatch_exposes_snapshot_save_relationships_and_diff(self):
        store = self.make_project()
        model_dir = self.tmp_path / "project" / "models" / "orders"
        model_dir.mkdir(parents=True)
        model_dir.joinpath("metadata.yml").write_text(
            "name: orders\ntable_reference:\n  schema: public\n  table: orders\ncolumns:\n  - name: id\n    type: INTEGER\n",
            encoding="utf-8",
        )
        service = SemanticConsoleService(store)
        status, snapshot = service.dispatch("GET", "/api/semantic-project")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["models"][0]["name"], "orders")
        status, updated = service.dispatch(
            "PUT",
            "/api/semantic-models/orders",
            body={**snapshot["models"][0], "expectedRevision": snapshot["revision"]},
        )
        self.assertEqual(status, 200)
        status, diff = service.dispatch("GET", "/api/project/diff", {"path": "models/orders/metadata.yml"})
        self.assertEqual(status, 200)
        self.assertTrue(diff["changed"])
        status, relationships = service.dispatch(
            "PUT",
            "/api/semantic-relationships",
            body={"expectedRevision": updated["revision"], "relationships": []},
        )
        self.assertEqual(status, 200)
        self.assertEqual(relationships["relationships"], [])

    def test_semantic_save_preserves_unknown_wren_and_locale_fields_and_composite_pk(self):
        store = self.make_project()
        model_dir = self.tmp_path / "project" / "models" / "orders"
        model_dir.mkdir(parents=True)
        model_dir.joinpath("metadata.yml").write_text(
            "name: orders\n"
            "table_reference:\n"
            "  catalog: analytics\n"
            "  schema: public\n"
            "  table: orders\n"
            "primary_key: [tenant_id, order_id]\n"
            "custom_model_field: keep-me\n"
            "columns:\n"
            "  - name: tenant_id\n"
            "    type: INTEGER\n"
            "    custom_column_field: keep-column\n"
            "  - name: order_id\n"
            "    type: INTEGER\n"
            "  - name: total\n"
            "    type: DECIMAL\n"
            "    is_calculated: true\n"
            "    expression: amount * quantity\n",
            encoding="utf-8",
        )
        customers_dir = self.tmp_path / "project" / "models" / "customers"
        customers_dir.mkdir(parents=True)
        customers_dir.joinpath("metadata.yml").write_text(
            "name: customers\ntable_reference:\n  table: customers\ncolumns:\n  - name: id\n    type: INTEGER\n",
            encoding="utf-8",
        )
        (self.tmp_path / "project" / "relationships.yml").write_text(
            "custom_relationship_root: keep-root\n"
            "relationships:\n"
            "  - name: orders_customer\n"
            "    models: [orders, customers]\n"
            "    join_type: MANY_TO_ONE\n"
            "    condition: orders.id = customers.id\n"
            "    custom_relationship_field: keep-relationship\n",
            encoding="utf-8",
        )
        (self.tmp_path / "project" / "semantic-console").mkdir()
        (self.tmp_path / "project" / "semantic-console" / "locales.yml").write_text(
            "custom_locale_root: keep-locale-root\n"
            "models:\n"
            "  orders:\n"
            "    custom_model_locale: keep-model-locale\n"
            "    columns:\n"
            "      tenant_id:\n"
            "        custom_column_locale: keep-column-locale\n",
            encoding="utf-8",
        )
        locales_path = self.tmp_path / "project" / "semantic-console" / "locales.yml"
        locales_path.write_text(
            locales_path.read_text(encoding="utf-8")
            + "relationships:\n"
            + "  orders_customer:\n"
            + "    custom_relationship_locale: keep-relationship-locale\n",
            encoding="utf-8",
        )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        model = next(item for item in snapshot["models"] if item["name"] == "orders")
        self.assertEqual(model["primaryKey"], ["tenant_id", "order_id"])
        relationship = snapshot["relationships"][0]
        relationship["displayName"] = {"zh-CN": "订单客户", "en-US": "Order customer"}
        relationship["description"] = {"zh-CN": "", "en-US": ""}
        updated = service.save_semantic_model("orders", {**model, "expectedRevision": snapshot["revision"]})
        updated = service.save_relationships({
            "expectedRevision": updated["revision"],
            "relationships": [relationship],
        })
        metadata = store.read_file("models/orders/metadata.yml")["content"]
        locales = store.read_file("semantic-console/locales.yml")["content"]
        relationships = store.read_file("relationships.yml")["content"]
        self.assertIn("custom_model_field: keep-me", metadata)
        self.assertIn("catalog: analytics", metadata)
        self.assertIn("custom_column_field: keep-column", metadata)
        self.assertIn("custom_locale_root: keep-locale-root", locales)
        self.assertIn("custom_model_locale: keep-model-locale", locales)
        self.assertIn("custom_column_locale: keep-column-locale", locales)
        self.assertIn("custom_relationship_root: keep-root", relationships)
        self.assertIn("custom_relationship_field: keep-relationship", relationships)
        self.assertIn("custom_relationship_locale: keep-relationship-locale", locales)
        self.assertEqual(next(item for item in updated["models"] if item["name"] == "orders")["primaryKey"], ["tenant_id", "order_id"])

    def test_semantic_revision_conflict_is_atomic_and_condition_must_name_models(self):
        store = self.make_project()
        for name in ("orders", "customers"):
            model_dir = self.tmp_path / "project" / "models" / name
            model_dir.mkdir(parents=True)
            model_dir.joinpath("metadata.yml").write_text(
                f"name: {name}\ntable_reference:\n  table: {name}\ncolumns:\n  - name: id\n    type: INTEGER\n",
                encoding="utf-8",
            )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        with self.assertRaises(ApiServiceError) as conflict:
            service.save_semantic_model(
                "orders",
                {**snapshot["models"][0], "expectedRevision": "stale-revision"},
            )
        self.assertEqual(conflict.exception.code, "REVISION_CONFLICT")
        self.assertEqual(store.files(), [item for item in store.files() if item["draft"] is False])
        with self.assertRaises(ApiServiceError) as condition:
            service.save_relationships({
                "expectedRevision": snapshot["revision"],
                "relationships": [{
                    "name": "orders_customer",
                    "models": ["orders", "customers"],
                    "joinType": "MANY_TO_ONE",
                    "condition": "orders.customer_id = missing.id",
                }],
            })
        self.assertEqual(condition.exception.code, "INVALID_RELATIONSHIP")

    def test_semantic_locales_malformed_shape_fails_closed(self):
        store = self.make_project()
        model_dir = self.tmp_path / "project" / "models" / "orders"
        model_dir.mkdir(parents=True)
        model_dir.joinpath("metadata.yml").write_text(
            "name: orders\ntable_reference:\n  table: orders\ncolumns:\n  - name: id\n    type: INTEGER\n",
            encoding="utf-8",
        )
        locale_dir = self.tmp_path / "project" / "semantic-console"
        locale_dir.mkdir()
        locale_dir.joinpath("locales.yml").write_text("models: []\n", encoding="utf-8")
        service = SemanticConsoleService(store)
        with self.assertRaises(ApiServiceError) as error:
            service.semantic_project()
        self.assertEqual(error.exception.code, "INVALID_LOCALES")

    def test_semantic_noop_save_preserves_ref_sql_model_mode(self):
        store = self.make_project()
        model_dir = self.tmp_path / "project" / "models" / "monthly"
        model_dir.mkdir(parents=True)
        model_dir.joinpath("metadata.yml").write_text(
            "name: monthly\nref_sql: SELECT 1 AS id\ncolumns:\n  - name: id\n    type: INTEGER\n",
            encoding="utf-8",
        )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        service.save_semantic_model("monthly", {**snapshot["models"][0], "expectedRevision": snapshot["revision"]})
        metadata = store.read_file("models/monthly/metadata.yml")["content"]
        self.assertIn("ref_sql: SELECT 1 AS id", metadata)
        self.assertNotIn("table_reference:", metadata)

    def test_semantic_scalar_text_patch_preserves_unedited_locale_and_unknown_fields(self):
        store = self.make_project()
        model_dir = self.tmp_path / "project" / "models" / "orders"
        model_dir.mkdir(parents=True)
        model_dir.joinpath("metadata.yml").write_text(
            "name: orders\n"
            "properties:\n"
            "  description: Legacy Wren description\n"
            "custom_model_field: keep-me\n"
            "table_reference:\n"
            "  schema: public\n"
            "  table: orders\n"
            "columns:\n"
            "  - name: id\n"
            "    type: INTEGER\n",
            encoding="utf-8",
        )
        locale_dir = self.tmp_path / "project" / "semantic-console"
        locale_dir.mkdir(parents=True)
        locale_dir.joinpath("locales.yml").write_text(
            "models:\n"
            "  orders:\n"
            "    custom_model_locale: keep-model-locale\n"
            "    display_name:\n"
            "      zh-CN: 旧订单\n"
            "      en-US: Legacy orders\n"
            "    description:\n"
            "      zh-CN: 旧中文描述\n"
            "      en-US: Legacy English description\n"
            "    columns:\n"
            "      id:\n"
            "        display_name:\n"
            "          zh-CN: 编号\n"
            "          en-US: Identifier\n"
            "        custom_column_locale: keep-column-locale\n",
            encoding="utf-8",
        )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        model = snapshot["models"][0]
        updated = service.save_semantic_model(
            "orders",
            {
                **model,
                "displayName": "新订单",
                "description": "新的中文描述",
                "locale": "zh-CN",
                "columns": [{**model["columns"][0], "displayName": "编号（新）", "locale": "zh-CN"}],
                "expectedRevision": snapshot["revision"],
            },
        )
        saved_model = updated["models"][0]
        self.assertEqual(saved_model["displayName"], {"zh-CN": "新订单", "en-US": "Legacy orders"})
        self.assertEqual(saved_model["columns"][0]["displayName"], {"zh-CN": "编号（新）", "en-US": "Identifier"})
        locales = store.read_file("semantic-console/locales.yml")["content"]
        metadata = store.read_file("models/orders/metadata.yml")["content"]
        self.assertIn("Legacy orders", locales)
        self.assertIn("custom_model_locale: keep-model-locale", locales)
        self.assertIn("custom_column_locale: keep-column-locale", locales)
        self.assertIn("custom_model_field: keep-me", metadata)
        # The Wren-level description keeps its English/default value when only
        # the Chinese companion text was edited.
        self.assertIn("description: Legacy Wren description", metadata)

    def test_semantic_noop_save_keeps_partial_locale_records_and_revision_is_atomic(self):
        store = self.make_project()
        model_dir = self.tmp_path / "project" / "models" / "orders"
        model_dir.mkdir(parents=True)
        model_dir.joinpath("metadata.yml").write_text(
            "name: orders\ncustom_model_field: keep-me\ncolumns:\n  - name: id\n    type: INTEGER\n",
            encoding="utf-8",
        )
        locale_dir = self.tmp_path / "project" / "semantic-console"
        locale_dir.mkdir(parents=True)
        locale_dir.joinpath("locales.yml").write_text(
            "models:\n  orders:\n    display_name:\n      en-US: Orders\n    custom_model_locale: keep-me\n",
            encoding="utf-8",
        )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        model = snapshot["models"][0]
        updated = service.save_semantic_model(
            "orders",
            {"columns": model["columns"], "expectedRevision": snapshot["revision"]},
        )
        self.assertEqual(updated["models"][0]["displayName"]["en-US"], "Orders")
        self.assertIn("custom_model_locale: keep-me", store.read_file("semantic-console/locales.yml")["content"])
        self.assertIn("custom_model_field: keep-me", store.read_file("models/orders/metadata.yml")["content"])
        after_locale = store.read_file("semantic-console/locales.yml")["content"]
        after_metadata = store.read_file("models/orders/metadata.yml")["content"]
        stale = {"columns": model["columns"], "displayName": "stale", "expectedRevision": snapshot["revision"]}
        with self.assertRaises(ApiServiceError) as conflict:
            service.save_semantic_model("orders", stale)
        self.assertEqual(conflict.exception.code, "REVISION_CONFLICT")
        self.assertEqual(store.read_file("semantic-console/locales.yml")["content"], after_locale)
        self.assertEqual(store.read_file("models/orders/metadata.yml")["content"], after_metadata)

    def test_relationship_field_pairs_are_derived_without_rewriting_composite_condition(self):
        store = self.make_project()
        for name in ("orders", "customers"):
            model_dir = self.tmp_path / "project" / "models" / name
            model_dir.mkdir(parents=True)
            model_dir.joinpath("metadata.yml").write_text(
                f"name: {name}\ncolumns:\n  - name: id\n    type: INTEGER\n  - name: tenant_id\n    type: INTEGER\n",
                encoding="utf-8",
            )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        condition = "orders.tenant_id = customers.tenant_id AND customers.id = orders.id"
        updated = service.save_relationships(
            {
                "expectedRevision": snapshot["revision"],
                "relationships": [{
                    "name": "orders_customers",
                    "models": ["orders", "customers"],
                    "joinType": "MANY_TO_ONE",
                    "condition": condition,
                }],
            }
        )
        relationship = updated["relationships"][0]
        self.assertEqual(relationship["fieldPairs"], [
            {"sourceModel": "orders", "sourceField": "tenant_id", "targetModel": "customers", "targetField": "tenant_id"},
            {"sourceModel": "orders", "sourceField": "id", "targetModel": "customers", "targetField": "id"},
        ])
        self.assertIn(condition, store.read_file("relationships.yml")["content"])

    def test_relationship_scalar_locale_patch_preserves_complex_condition_and_does_not_persist_field_pairs(self):
        store = self.make_project()
        for name in ("orders", "customers"):
            model_dir = self.tmp_path / "project" / "models" / name
            model_dir.mkdir(parents=True)
            model_dir.joinpath("metadata.yml").write_text(
                f"name: {name}\ncolumns:\n  - name: customer_id\n    type: INTEGER\n  - name: id\n    type: INTEGER\n",
                encoding="utf-8",
            )
        condition = "orders.customer_id = customers.id AND (orders.deleted_at IS NULL OR customers.deleted_at IS NULL)"
        (self.tmp_path / "project" / "relationships.yml").write_text(
            "custom_relationship_root: keep-root\n"
            "relationships:\n"
            "  - name: orders_customer\n"
            "    models: [orders, customers]\n"
            "    join_type: MANY_TO_ONE\n"
            f"    condition: {condition}\n"
            "    custom_relationship_field: keep-entry\n",
            encoding="utf-8",
        )
        locale_dir = self.tmp_path / "project" / "semantic-console"
        locale_dir.mkdir(parents=True)
        locale_dir.joinpath("locales.yml").write_text(
            "custom_locale_root: keep-locale-root\n"
            "relationships:\n"
            "  orders_customer:\n"
            "    display_name:\n"
            "      zh-CN: 旧订单客户\n"
            "      en-US: Legacy order customer\n"
            "    description:\n"
            "      zh-CN: 旧中文关系\n"
            "      en-US: Legacy English relationship\n"
            "    custom_relationship_locale: keep-locale-entry\n",
            encoding="utf-8",
        )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        relationship = snapshot["relationships"][0]
        self.assertEqual(relationship["fieldPairs"], [])
        updated = service.save_relationships({
            "expectedRevision": snapshot["revision"],
            "locale": "zh-CN",
            "relationships": [{
                **relationship,
                "displayName": "新订单客户",
                "description": "新的中文关系",
                # This is a read-only projection and must be ignored on write.
                "fieldPairs": [{"sourceModel": "evil", "sourceField": "x", "targetModel": "evil", "targetField": "y"}],
            }],
        })
        saved = updated["relationships"][0]
        self.assertEqual(saved["displayName"], {"zh-CN": "新订单客户", "en-US": "Legacy order customer"})
        self.assertEqual(saved["description"], {"zh-CN": "新的中文关系", "en-US": "Legacy English relationship"})
        self.assertEqual(saved["condition"], condition)
        relationships = store.read_file("relationships.yml")["content"]
        locales = store.read_file("semantic-console/locales.yml")["content"]
        self.assertEqual(yaml.safe_load(relationships)["relationships"][0]["condition"], condition)
        self.assertIn("custom_relationship_root: keep-root", relationships)
        self.assertIn("custom_relationship_field: keep-entry", relationships)
        self.assertNotIn("fieldPairs", relationships)
        self.assertIn("Legacy order customer", locales)
        self.assertIn("custom_relationship_locale: keep-locale-entry", locales)
        self.assertNotIn("fieldPairs", locales)

    def test_relationship_save_preserves_unknown_model_records_and_locale_extensions(self):
        store = self.make_project()
        for name in ("orders", "customers"):
            model_dir = self.tmp_path / "project" / "models" / name
            model_dir.mkdir(parents=True)
            model_dir.joinpath("metadata.yml").write_text(
                f"name: {name}\ncolumns:\n  - name: id\n    type: INTEGER\n  - name: customer_id\n    type: INTEGER\n",
                encoding="utf-8",
            )
        (self.tmp_path / "project" / "relationships.yml").write_text(
            "custom_relationship_root: keep-root\n"
            "relationships:\n"
            "  - name: orders_customer\n"
            "    models: [orders, customers]\n"
            "    join_type: MANY_TO_ONE\n"
            "    condition: orders.customer_id = customers.id\n"
            "    custom_visible_field: keep-visible\n"
            "  - name: orders_archived_customer\n"
            "    models: [orders, archived_customers]\n"
            "    join_type: MANY_TO_ONE\n"
            "    condition: orders.customer_id = archived_customers.id\n"
            "    custom_unknown_field: keep-unknown\n",
            encoding="utf-8",
        )
        locale_dir = self.tmp_path / "project" / "semantic-console"
        locale_dir.mkdir(parents=True)
        locale_dir.joinpath("locales.yml").write_text(
            "custom_locale_root: keep-locale-root\n"
            "relationships:\n"
            "  orders_archived_customer:\n"
            "    display_name:\n"
            "      zh-CN: 历史订单客户\n"
            "      en-US: Archived order customer\n"
            "      x-acme: Keep this translation extension\n"
            "    custom_unknown_locale: keep-unknown-locale\n",
            encoding="utf-8",
        )
        service = SemanticConsoleService(store)
        snapshot = service.semantic_project()
        self.assertEqual(snapshot["relationships"][0]["name"], "orders_customer")
        self.assertEqual(snapshot["relationshipErrors"][0]["name"], "orders_archived_customer")

        visible = snapshot["relationships"][0]
        updated = service.save_relationships({
            "expectedRevision": snapshot["revision"],
            "relationships": [{**visible, "condition": "orders.id = customers.id"}],
        })
        saved_document = yaml.safe_load(store.read_file("relationships.yml")["content"])
        saved_by_name = {entry["name"]: entry for entry in saved_document["relationships"]}
        self.assertEqual(saved_by_name["orders_archived_customer"]["models"], ["orders", "archived_customers"])
        self.assertEqual(saved_by_name["orders_archived_customer"]["custom_unknown_field"], "keep-unknown")
        self.assertEqual(saved_by_name["orders_customer"]["condition"], "orders.id = customers.id")
        saved_locales = yaml.safe_load(store.read_file("semantic-console/locales.yml")["content"])
        unknown_locale = saved_locales["relationships"]["orders_archived_customer"]
        self.assertEqual(unknown_locale["custom_unknown_locale"], "keep-unknown-locale")
        self.assertEqual(unknown_locale["display_name"]["x-acme"], "Keep this translation extension")

        # Removing a visible relationship still has its normal semantics; it
        # must not turn the preserved unknown-model entry into a deletion.
        service.save_relationships({"expectedRevision": updated["revision"], "relationships": []})
        after_delete = yaml.safe_load(store.read_file("relationships.yml")["content"])
        self.assertEqual(
            [entry["name"] for entry in after_delete["relationships"]],
            ["orders_archived_customer"],
        )
        after_delete_locales = yaml.safe_load(store.read_file("semantic-console/locales.yml")["content"])
        self.assertEqual(list(after_delete_locales["relationships"]), ["orders_archived_customer"])

        # The graph cannot silently create a second entry with the same name
        # as a hidden source relationship; the source record must be repaired
        # explicitly first.
        latest = service.semantic_project()
        with self.assertRaises(ApiServiceError) as collision:
            service.save_relationships({
                "expectedRevision": latest["revision"],
                "relationships": [{
                    "name": "orders_archived_customer",
                    "models": ["orders", "customers"],
                    "joinType": "MANY_TO_ONE",
                    "condition": "orders.customer_id = customers.id",
                }],
            })
        self.assertEqual(collision.exception.code, "INVALID_RELATIONSHIP")

    def test_relationship_field_pairs_require_simple_and_terms_and_existing_fields(self):
        store = self.make_project()
        for name in ("orders", "customers"):
            model_dir = self.tmp_path / "project" / "models" / name
            model_dir.mkdir(parents=True)
            model_dir.joinpath("metadata.yml").write_text(
                f"name: {name}\ncolumns:\n  - name: id\n    type: INTEGER\n  - name: customer_id\n    type: INTEGER\n",
                encoding="utf-8",
            )
        service = SemanticConsoleService(store)
        conditions = {
            "relationship_or": " orders.customer_id = customers.id OR orders.id = customers.id ",
            "relationship_function": "LOWER(orders.customer_id) = customers.id",
            "relationship_cast": "CAST(orders.customer_id AS TEXT) = customers.id",
            "relationship_parenthesized": "orders.customer_id = customers.id AND (orders.id = customers.id)",
            "relationship_expression": "orders.customer_id + 1 = customers.id",
            "relationship_missing_field": "orders.missing_field = customers.id",
        }
        snapshot = service.semantic_project()
        updated = service.save_relationships({
            "expectedRevision": snapshot["revision"],
            "relationships": [
                {
                    "name": name,
                    "models": ["orders", "customers"],
                    "joinType": "MANY_TO_ONE",
                    "condition": condition,
                }
                for name, condition in conditions.items()
            ],
        })
        saved = {relationship["name"]: relationship for relationship in updated["relationships"]}
        for name, condition in conditions.items():
            self.assertEqual(saved[name]["fieldPairs"], [], name)
            self.assertEqual(saved[name]["condition"], condition)
        persisted = yaml.safe_load(store.read_file("relationships.yml")["content"])
        persisted_by_name = {entry["name"]: entry for entry in persisted["relationships"]}
        for name, condition in conditions.items():
            self.assertEqual(persisted_by_name[name]["condition"], condition)
