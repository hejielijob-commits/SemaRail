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
        return {"models": [], "cubes": []}


class CubeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="semantic-console-cube-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project_dir = root / "project"
        self.project_dir.mkdir()
        (self.project_dir / "wren_project.yml").write_text(
            "schema_version: 5\nname: demo\ndata_source: postgres\n", encoding="utf-8"
        )
        model_dir = self.project_dir / "models" / "orders"
        model_dir.mkdir(parents=True)
        (model_dir / "metadata.yml").write_text(
            "name: orders\ncolumns:\n  - name: id\n    type: INTEGER\n", encoding="utf-8"
        )
        view_dir = self.project_dir / "views" / "revenue"
        view_dir.mkdir(parents=True)
        (view_dir / "metadata.yml").write_text(
            "name: revenue\nstatement: SELECT 1\ncolumns:\n  - name: value\n    type: DOUBLE\n", encoding="utf-8"
        )
        cube_dir = self.project_dir / "cubes" / "order_metrics"
        cube_dir.mkdir(parents=True)
        (cube_dir / "metadata.yml").write_text(
            "name: order_metrics\n"
            "base_object: orders\n"
            "measures:\n"
            "  - name: total_revenue\n"
            "    expression: SUM(amount)\n"
            "    type: DOUBLE\n"
            "    custom_measure_field: retain-me\n"
            "dimensions:\n"
            "  - name: status\n"
            "    expression: status\n"
            "    type: VARCHAR\n"
            "time_dimensions:\n"
            "  - name: ordered_at\n"
            "    expression: ordered_at\n"
            "    type: DATE\n"
            "hierarchies:\n"
            "  time: [ordered_at]\n"
            "refresh_time: 15 minutes\n"
            "custom_root: retain-root\n",
            encoding="utf-8",
        )
        self.store = ProjectStore(self.project_dir, state_dir=root / "state", validator=FakeValidator())
        self.service = SemanticConsoleService(self.store)

    def test_snapshot_projects_v5_cubes_and_base_objects(self):
        result = self.service.cubes_snapshot()
        self.assertEqual(result["cubes"][0]["name"], "order_metrics")
        cube = result["cubes"][0]
        self.assertEqual(cube["baseObject"], "orders")
        self.assertEqual(cube["measures"][0]["expression"], "SUM(amount)")
        self.assertEqual(cube["timeDimensions"][0]["name"], "ordered_at")
        self.assertEqual(cube["hierarchies"], {"time": ["ordered_at"]})
        self.assertEqual(cube["refreshTime"], "15 minutes")
        self.assertIn("orders", result["availableBaseObjects"])
        self.assertIn("revenue", result["availableBaseObjects"])
        self.assertEqual(result["sourceFiles"][0]["path"], "cubes/order_metrics/metadata.yml")

    def test_save_merges_known_values_without_dropping_extensions(self):
        snapshot = self.service.cubes_snapshot()
        cube = snapshot["cubes"][0]
        cube["measures"][0]["expression"] = "SUM(net_amount)"
        cube["measures"][0]["name"] = "net_revenue"
        cube["dimensions"] = []
        cube["hierarchies"] = {}
        updated = self.service.save_cube("order_metrics", {**cube, "expectedRevision": snapshot["revision"]})
        self.assertTrue(updated["cubes"][0]["draft"])
        raw = yaml.safe_load(self.store.read_file("cubes/order_metrics/metadata.yml")["content"])
        self.assertEqual(raw["custom_root"], "retain-root")
        self.assertNotIn("custom_measure_field", raw["measures"][0])
        self.assertEqual(raw["measures"][0]["name"], "net_revenue")
        self.assertEqual(raw["measures"][0]["expression"], "SUM(net_amount)")
        self.assertNotIn("dimensions", raw)
        self.assertNotIn("hierarchies", raw)
        self.assertEqual(raw["refresh_time"], "15 minutes")

    def test_validation_reports_reference_and_hierarchy_errors(self):
        result = self.service.validate_cube(
            "order_metrics",
            {
                "name": "order_metrics",
                "baseObject": "missing_model",
                "measures": [],
                "dimensions": [{"name": "status", "expression": "status", "type": "VARCHAR"}],
                "hierarchies": {"bad": ["missing_dimension"]},
            },
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["warningCount"], 1)
        messages = " ".join(item["message"] for item in result["errors"])
        self.assertIn("not a defined model or view", messages)
        self.assertIn("unknown dimension", messages)

    def test_dispatch_save_validate_and_delete(self):
        status, snapshot = self.service.dispatch("GET", "/api/cubes")
        self.assertEqual(status, 200)
        status, detail = self.service.dispatch("GET", "/api/cubes/order_metrics")
        self.assertEqual(status, 200)
        self.assertEqual(detail["name"], "order_metrics")
        status, validation = self.service.dispatch(
            "POST", "/api/cubes/order_metrics/validate", body=snapshot["cubes"][0]
        )
        self.assertEqual(status, 200)
        self.assertTrue(validation["valid"])
        status, updated = self.service.dispatch(
            "PUT",
            "/api/cubes/order_metrics",
            body={**snapshot["cubes"][0], "expectedRevision": snapshot["revision"]},
        )
        self.assertEqual(status, 200)
        status, deleted = self.service.dispatch(
            "DELETE", "/api/cubes/order_metrics", body={"expectedRevision": updated["revision"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(deleted["cubes"], [])

    def test_create_cube_is_a_draft_and_rejects_duplicate_names(self):
        status, created = self.service.dispatch(
            "POST",
            "/api/cubes",
            body={
                "name": "customer_metrics",
                "baseObject": "orders",
                "measures": [{"name": "orders_count", "expression": "COUNT(*)", "type": "BIGINT"}],
            },
        )
        self.assertEqual(status, 201)
        self.assertIn("customer_metrics", {item["name"] for item in created["cubes"]})
        status, error = self.service.dispatch(
            "POST",
            "/api/cubes",
            body={
                "name": "customer_metrics",
                "baseObject": "orders",
                "measures": [{"name": "orders_count", "expression": "COUNT(*)", "type": "BIGINT"}],
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["__error__"]["code"], "FILE_EXISTS")

    def test_unknown_cube_yaml_fails_closed(self):
        path = self.project_dir / "cubes" / "broken" / "metadata.yml"
        path.parent.mkdir(parents=True)
        path.write_text("name: broken\nbase_object: orders\nmeasures: nope\n", encoding="utf-8")
        with self.assertRaises(Exception) as raised:
            self.service.cubes_snapshot()
        self.assertEqual(getattr(raised.exception, "code", None), "INVALID_CUBE")

    def test_save_keeps_existing_metadata_yaml_extension(self):
        cube_dir = self.project_dir / "cubes" / "legacy_yaml"
        cube_dir.mkdir(parents=True)
        (cube_dir / "metadata.yaml").write_text(
            "name: legacy_yaml\nbase_object: orders\nmeasures:\n"
            "  - name: count\n    expression: COUNT(*)\n    type: BIGINT\n",
            encoding="utf-8",
        )
        snapshot = self.service.cubes_snapshot()
        cube = next(item for item in snapshot["cubes"] if item["name"] == "legacy_yaml")
        cube["measures"][0]["expression"] = "COUNT(id)"
        self.service.save_cube("legacy_yaml", {**cube, "expectedRevision": snapshot["revision"]})
        self.assertTrue(self.store.read_file("cubes/legacy_yaml/metadata.yaml")["draft"])
        with self.assertRaises(Exception):
            self.store.read_file("cubes/legacy_yaml/metadata.yml")


if __name__ == "__main__":
    unittest.main()
