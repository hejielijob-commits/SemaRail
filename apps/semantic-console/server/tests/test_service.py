from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.app import create_app
from server.project import ProjectStore
from server.service import ApiServiceError, SemanticConsoleService


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

    def test_secret_content_and_traversal_are_rejected(self):
        service = SemanticConsoleService(self.make_project())
        with self.assertRaises(ApiServiceError):
            service.put_file("../credentials.yml", {"content": "password: bad"})
        with self.assertRaises(ApiServiceError):
            service.put_file("knowledge/rules/rule.md", {"content": "dsn: postgres://u:p@host/db"})

    def test_postgres_schema_columns_and_model_generation(self):
        service = SemanticConsoleService(self.make_project(), connection_factory=lambda _values: FakeConnection())
        datasource = service.create_datasource({"name": "pg", "type": "postgres", "connection": {"host": "localhost"}})
        columns = service.datasource_columns(datasource["id"], "public", "customers")
        self.assertTrue(columns[0]["primaryKey"])
        generated = service.generate_model(datasource["id"], "public", "customers")
        self.assertTrue(generated["draft"])
        self.assertEqual(generated["model"]["columns"][0]["type"], "INTEGER")

    def test_embedded_app_returns_direct_shapes_and_safe_errors(self):
        app = create_app(SemanticConsoleService(self.make_project()))
        status, health = app.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["service"], "semantic-console")
        status, error = app.request("GET", "/api/project/file?path=../secret")
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "INVALID_PATH")

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
