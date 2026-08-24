from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sidecar.datasource_state import (
    DatasourceStateError,
    load_active_connection,
    semantic_console_state_file,
)


class DatasourceStateTests(unittest.TestCase):
    def test_project_state_path_is_stable_and_outside_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            first = semantic_console_state_file(project, home=root / "home")
            second = semantic_console_state_file(project / ".", home=root / "home")
            self.assertEqual(first, second)
            self.assertNotIn(project, first.parents)
            self.assertEqual(first.name, "datasources.secrets.json")

    def test_active_console_profile_takes_precedence_over_legacy_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "datasources.secrets.json"
            state.write_text(
                json.dumps(
                    {
                        "activeDatasourceId": "primary",
                        "datasources": {
                            "primary": {
                                "id": "primary",
                                "type": "postgres",
                                "connection": {
                                    "host": "db.internal",
                                    "database": "analytics",
                                    "user": "analyst",
                                    "password": "console-secret",
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = load_active_connection(
                ".",
                "WREN_DATABASE_URL",
                state_file=state,
                environ={"WREN_DATABASE_URL": "postgresql://legacy:secret@example.invalid/db"},
            )
            self.assertEqual(result["host"], "db.internal")
            self.assertEqual(result["password"], "console-secret")
            self.assertNotIn("connectionUrl", result)

    def test_missing_selection_preserves_legacy_environment_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "datasources.secrets.json"
            state.write_text(
                json.dumps(
                    {
                        "datasources": {
                            "one": {"type": "postgres", "connection": {"host": "one"}},
                            "two": {"type": "postgres", "connection": {"host": "two"}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            dsn = "postgresql://legacy:secret@example.invalid/db"
            result = load_active_connection(
                ".",
                "WREN_DATABASE_URL",
                state_file=state,
                environ={"WREN_DATABASE_URL": dsn},
            )
            self.assertEqual(result, {"connectionUrl": dsn, "datasource": "postgres"})

    def test_selected_unsupported_or_invalid_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "datasources.secrets.json"
            state.write_text(
                json.dumps(
                    {
                        "activeDatasourceId": "mysql",
                        "datasources": {
                            "mysql": {"type": "mysql", "connection": {"host": "db"}}
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(DatasourceStateError):
                load_active_connection(
                    ".",
                    "WREN_DATABASE_URL",
                    state_file=state,
                    environ={"WREN_DATABASE_URL": "postgresql://fallback.invalid/db"},
                )
            state.write_text("not-json", encoding="utf-8")
            with self.assertRaises(DatasourceStateError):
                load_active_connection(".", "WREN_DATABASE_URL", state_file=state, environ={})


if __name__ == "__main__":
    unittest.main()
