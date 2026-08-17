from __future__ import annotations

import io
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sidecar.dispatch import Dispatcher, SidecarDependencies
from sidecar.wren_adapter import LazyWrenAdapter


class WrenAdapterTests(unittest.TestCase):
    def test_import_is_lazy_and_health_reports_fake_wren(self) -> None:
        calls: list[str] = []

        def loader(name: str) -> object:
            calls.append(name)
            if name == "wren.context":
                return SimpleNamespace(
                    validate_project=lambda _: [],
                    build_json=lambda _: {"models": []},
                )
            if name == "wren":
                return SimpleNamespace(__version__="0.13.2")
            raise ModuleNotFoundError(name)

        adapter = LazyWrenAdapter(module_loader=loader)
        self.assertEqual(calls, [])
        health = adapter.health()
        self.assertEqual(health, {
            "status": "ok",
            "protocolVersion": "1",
            "wrenAvailable": True,
            "wrenVersion": "0.13.2",
        })
        self.assertIn("wren.context", calls)

    def test_validate_calls_context_validate_and_build_and_counts_issues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            (project / "models.yml").write_text("models: []\n", encoding="utf-8")
            (project / "target").mkdir()
            (project / "target" / "mdl.json").write_text("volatile", encoding="utf-8")
            calls: list[tuple[str, Path]] = []

            def validate_project(path: Path) -> list[object]:
                calls.append(("validate", path))
                return [SimpleNamespace(level="error"), SimpleNamespace(level="warning")]

            def build_json(path: Path) -> dict[str, object]:
                calls.append(("build", path))
                return {"models": []}

            context = SimpleNamespace(
                validate_project=validate_project,
                build_json=build_json,
            )
            adapter = LazyWrenAdapter(
                module_loader=lambda name: context,
                version_provider=lambda: "0.13.2",
            )
            result = adapter.validate({"projectDir": str(project)})
            self.assertFalse(result["valid"])
            self.assertEqual(result["errorCount"], 1)
            self.assertEqual(result["warningCount"], 1)
            self.assertTrue(str(result["projectRevision"]).startswith("sha256:"))
            self.assertEqual([name for name, _ in calls], ["validate", "build"])
            self.assertEqual(calls[0][1], project.resolve())

            # Generated target output is deliberately excluded from the
            # source revision; the result must remain deterministic.
            first_revision = result["projectRevision"]
            (project / "target" / "mdl.json").write_text("changed", encoding="utf-8")
            self.assertEqual(
                adapter.validate({"projectDir": str(project)})["projectRevision"],
                first_revision,
            )

    def test_project_dir_is_required_before_wren_is_called(self) -> None:
        calls: list[object] = []
        adapter = LazyWrenAdapter(
            module_loader=lambda _: calls.append(True) or SimpleNamespace(
                validate_project=lambda _: [], build_json=lambda _: {}
            )
        )
        response = Dispatcher(
            SidecarDependencies(project_validator=adapter)
        ).dispatch({
            "protocolVersion": "1",
            "id": "missing",
            "method": "project.validate",
            "params": {},
        })
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(calls, [])

    def test_unexpected_messages_and_tracebacks_never_reach_logs(self) -> None:
        secret = "postgres://alice:super-secret@db.internal/analytics SELECT password /private/project"
        log = io.StringIO()
        handler = logging.StreamHandler(log)
        logger = logging.getLogger("sidecar.test.no-leak")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            response = Dispatcher(
                project_validator=lambda _: (_ for _ in ()).throw(RuntimeError(secret)),
                logger=logger,
            ).dispatch({
                "protocolVersion": "1",
                "id": "leak-check",
                "method": "project.validate",
                "params": {"projectDir": "."},
            })
        finally:
            logger.removeHandler(handler)
        self.assertEqual(response["error"]["code"], "PROJECT_VALIDATION_FAILED")
        self.assertNotIn(secret, log.getvalue())
        self.assertNotIn("Traceback", log.getvalue())


if __name__ == "__main__":
    unittest.main()

