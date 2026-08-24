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
