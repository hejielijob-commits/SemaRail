from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from server.project import ProjectStore
from server.service import SemanticConsoleService


class FakeValidator:
    def health(self):
        return {"available": False, "version": None}

    def validate(self, project_dir: Path):
        return {"valid": True, "errors": [], "warnings": [], "errorCount": 0, "warningCount": 0}

    def build(self, project_dir: Path):
        return {"models": [], "views": []}


class ViewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="semantic-console-view-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        project = root / "project"
        project.mkdir()
        (project / "wren_project.yml").write_text(
            "schema_version: 5\nname: demo\ndata_source: postgres\n", encoding="utf-8"
        )
        model = project / "models" / "orders"
        model.mkdir(parents=True)
        (model / "metadata.yml").write_text(
            "name: orders\ncolumns:\n  - name: id\n    type: INTEGER\n", encoding="utf-8"
        )
        inline = project / "views" / "revenue"
        inline.mkdir(parents=True)
        (inline / "metadata.yml").write_text(
            "name: revenue\n"
            "statement: SELECT id FROM orders\n"
            "properties:\n"
            "  description: Revenue\n"
            "  custom_flag: true\n"
            "custom_root: retain-me\n",
            encoding="utf-8",
        )
        sidecar = project / "views" / "customer_orders"
        sidecar.mkdir(parents=True)
        (sidecar / "metadata.yml").write_text(
            "name: customer_orders\n"
            "statement: SELECT stale FROM orders\n"
            "dialect: postgres\n"
            "properties:\n"
            "  owner: analytics\n"
            "custom_root: retain-sidecar\n",
            encoding="utf-8",
        )
        (sidecar / "sql.yml").write_text(
            "statement: SELECT id FROM orders\ncustom_sql: retain-sidecar\n", encoding="utf-8"
        )
        self.store = ProjectStore(project, state_dir=root / "state", validator=FakeValidator())
        self.service = SemanticConsoleService(self.store)

    def test_snapshot_projects_inline_and_sql_precedence(self):
        snapshot = self.service.views_snapshot()
        by_name = {view["name"]: view for view in snapshot["views"]}
        self.assertEqual(by_name["revenue"]["storage"], "metadata")
        self.assertIsNone(by_name["revenue"]["sqlPath"])
        self.assertEqual(by_name["revenue"]["statement"], "SELECT id FROM orders")
        self.assertEqual(by_name["revenue"]["properties"]["custom_flag"], True)
        self.assertEqual(by_name["customer_orders"]["storage"], "sql")
        self.assertEqual(by_name["customer_orders"]["statement"], "SELECT id FROM orders")
        self.assertEqual(by_name["customer_orders"]["sqlPath"], "views/customer_orders/sql.yml")
        self.assertEqual(
            [item["path"] for item in snapshot["sourceFiles"]],
            [
                "views/customer_orders/metadata.yml",
                "views/customer_orders/sql.yml",
                "views/revenue/metadata.yml",
            ],
        )

    def test_create_defaults_to_sql_and_preserves_properties_on_save(self):
        status, snapshot = self.service.dispatch(
            "POST",
            "/api/views",
            body={
                "name": "new_view",
                "statement": "SELECT id FROM orders",
                "properties": {"description": "New", "ui_color": "blue"},
            },
        )
        self.assertEqual(status, 201)
        view = next(item for item in snapshot["views"] if item["name"] == "new_view")
        self.assertEqual(view["storage"], "sql")
        self.assertEqual(view["sqlPath"], "views/new_view/sql.yml")
        metadata = yaml.safe_load(self.store.read_file("views/new_view/metadata.yml")["content"])
        self.assertNotIn("statement", metadata)
        self.assertEqual(yaml.safe_load(self.store.read_file("views/new_view/sql.yml")["content"])["statement"], "SELECT id FROM orders")

        current = self.service.get_view("revenue")
        updated = self.service.save_view(
            "revenue",
            {
                "statement": "SELECT id FROM orders WHERE id > 1",
                "properties": {"description": "Updated"},
                "expectedRevision": snapshot["revision"],
            },
        )
        self.assertTrue(next(item for item in updated["views"] if item["name"] == "revenue")["draft"])
        raw = yaml.safe_load(self.store.read_file(current["sourcePath"])["content"])
        self.assertEqual(raw["properties"]["description"], "Updated")
        self.assertTrue(raw["properties"]["custom_flag"])
        self.assertEqual(raw["custom_root"], "retain-me")

    def test_storage_switches_are_atomic_and_remove_stale_sidecar(self):
        current = self.service.get_view("revenue")
        updated = self.service.save_view(
            "revenue",
            {
                "statement": "SELECT id FROM orders",
                "storage": "sql",
                "expectedRevision": current.get("revision", self.service.views_snapshot()["revision"]),
            },
        )
        switched = next(item for item in updated["views"] if item["name"] == "revenue")
        self.assertEqual(switched["storage"], "sql")
        self.assertEqual(yaml.safe_load(self.store.read_file("views/revenue/sql.yml")["content"])["statement"], "SELECT id FROM orders")
        revision = updated["revision"]
        updated = self.service.save_view(
            "revenue",
            {"statement": "SELECT id FROM orders", "storage": "metadata", "expectedRevision": revision},
        )
        switched = next(item for item in updated["views"] if item["name"] == "revenue")
        self.assertEqual(switched["storage"], "metadata")
        with self.assertRaises(Exception):
            self.store.read_file("views/revenue/sql.yml")
        self.assertEqual(yaml.safe_load(self.store.read_file("views/revenue/metadata.yml")["content"])["statement"], "SELECT id FROM orders")

    def test_revision_conflict_and_global_name_conflict(self):
        current = self.service.views_snapshot()
        status, error = self.service.dispatch(
            "PUT",
            "/api/views/revenue",
            body={"statement": "SELECT id FROM orders", "expectedRevision": "sha256:stale"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["__error__"]["code"], "REVISION_CONFLICT")
        status, error = self.service.dispatch(
            "POST",
            "/api/views",
            body={"name": "orders", "statement": "SELECT id FROM orders", "expectedRevision": current["revision"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["__error__"]["code"], "NAME_CONFLICT")

    def test_validation_rejects_sql_dialect_and_statement_size(self):
        result = self.service.validate_view(
            "revenue",
            {"statement": "UPDATE orders SET id = 1", "dialect": "not-a-dialect"},
        )
        self.assertFalse(result["valid"])
        messages = " ".join(item["message"] for item in result["errors"])
        self.assertIn("unknown dialect", messages)
        self.assertIn("read-only", messages)
        result = self.service.validate_view("revenue", {"statement": "x" * (64 * 1024 + 1)})
        self.assertFalse(result["valid"])
        self.assertTrue(any(item["code"] == "STATEMENT_TOO_LARGE" for item in result["errors"]))

    def test_dispatch_routes_and_delete_remove_both_files(self):
        status, detail = self.service.dispatch("GET", "/api/views/customer_orders")
        self.assertEqual(status, 200)
        self.assertEqual(detail["storage"], "sql")
        status, validation = self.service.dispatch(
            "POST", "/api/views/customer_orders/validate", body={"statement": "SELECT 1"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(validation["valid"])
        status, snapshot = self.service.dispatch(
            "DELETE", "/api/views/customer_orders", body={"expectedRevision": self.service.views_snapshot()["revision"]}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("customer_orders", {item["name"] for item in snapshot["views"]})
        with self.assertRaises(Exception):
            self.store.read_file("views/customer_orders/metadata.yml")
        with self.assertRaises(Exception):
            self.store.read_file("views/customer_orders/sql.yml")

    def test_delete_blocks_downstream_view_and_cube_dependencies(self):
        dependent = self.store.project_dir / "views" / "revenue_detail"
        dependent.mkdir(parents=True)
        (dependent / "metadata.yml").write_text(
            "name: revenue_detail\nstatement: SELECT * FROM revenue\n",
            encoding="utf-8",
        )
        cube = self.store.project_dir / "cubes" / "revenue_cube"
        cube.mkdir(parents=True)
        (cube / "metadata.yml").write_text(
            "name: revenue_cube\nbase_object: revenue\nmeasures: []\ndimensions: []\n",
            encoding="utf-8",
        )

        status, error = self.service.dispatch(
            "DELETE",
            "/api/views/revenue",
            body={"expectedRevision": self.service.views_snapshot()["revision"]},
        )

        self.assertEqual(status, 409)
        self.assertEqual(error["__error__"]["code"], "VIEW_IN_USE")
        self.assertEqual(
            {(item["kind"], item["name"]) for item in error["__error__"]["details"]["dependents"]},
            {("view", "revenue_detail"), ("cube", "revenue_cube")},
        )
        self.assertEqual(self.service.get_view("revenue")["name"], "revenue")

    def test_fallback_validation_checks_missing_view_statement(self):
        bad = self.store.project_dir / "views" / "broken"
        bad.mkdir(parents=True)
        (bad / "metadata.yml").write_text("name: broken\n", encoding="utf-8")
        # Use the real adapter so the no-Wren structural path (rather than
        # this test module's permissive fake validator) is exercised.
        fallback_store = ProjectStore(self.store.project_dir, state_dir=self.store.state_dir / "fallback")
        result = fallback_store.validate()
        self.assertFalse(result["valid"])
        self.assertTrue(any("statement" in item["message"] for item in result["errors"]))


if __name__ == "__main__":
    unittest.main()
