from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sidecar.errors import INTERNAL_ERROR, INVALID_PARAMS, RpcFault
from sidecar.semantic_service import SemanticService


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def validate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("validate", dict(params)))
        return {
            "valid": True,
            "errorCount": 0,
            "warningCount": 0,
            "projectRevision": "sha256:test",
        }

    def describe(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("describe", dict(params)))
        return {
            "schemaVersion": 1,
            "models": [{"name": "orders", "columns": []}],
            "relationships": [],
            "projectRevision": "sha256:test",
        }

    def ask(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("ask", dict(params)))
        return {
            "schemaVersion": 1,
            "models": [{"name": "orders", "columns": []}],
            "relationships": [],
            "summary": "orders semantic context",
            "projectRevision": "sha256:test",
        }

    def dry_plan(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("dry_plan", dict(params)))
        return {
            "semanticSql": params["semanticSql"],
            "nativeSql": "SELECT * FROM public.orders",
            "allowedPhysical": {"relations": ["public.orders"]},
            "projectRevision": "sha256:test",
        }


class SemanticServiceTests(unittest.TestCase):
    def test_pins_project_and_passes_existing_runtime_structures_through(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            runtime = FakeRuntime()
            service = SemanticService(project, runtime=runtime)

            validated = service.validate_project()
            listed = service.list_models()
            context = service.get_context("  What is revenue?  ")
            plan = service.plan_query("  SELECT * FROM orders  ")

        self.assertEqual(validated["schemaVersion"], 1)
        self.assertEqual(listed["models"][0]["name"], "orders")
        self.assertEqual(context["summary"], "orders semantic context")
        self.assertEqual(plan["semanticSql"], "SELECT * FROM orders")
        for _method, params in runtime.calls:
            self.assertEqual(params["projectDir"], str(project))
        self.assertEqual(runtime.calls[2][1]["question"], "What is revenue?")
        self.assertNotIn(str(project), json.dumps(validated))

    def test_rejects_invalid_agent_text_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            service = SemanticService(directory, runtime=runtime)
            for operation in (
                lambda: service.get_context(" "),
                lambda: service.get_context("x" * 16_001),
                lambda: service.plan_query(""),
                lambda: service.plan_query("x" * 64_001),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(RpcFault) as caught:
                        operation()
                    self.assertEqual(caught.exception.error.code, INVALID_PARAMS)
            self.assertEqual(runtime.calls, [])

    def test_requires_an_existing_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaisesRegex(ValueError, "project directory is unavailable"):
                SemanticService(missing, runtime=FakeRuntime())

    def test_rejects_non_json_safe_runtime_results_with_stable_phases(self) -> None:
        cases = (
            ("validate", lambda service: service.validate_project(), "project.validate", object()),
            ("describe", lambda service: service.list_models(), "project.describe", {"value": float("nan")}),
            ("ask", lambda service: service.get_context("orders"), "context.ask", {"value": object()}),
            ("dry_plan", lambda service: service.plan_query("SELECT * FROM orders"), "query.dryPlan", None),
        )
        for method, operation, phase, invalid in cases:
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                runtime = FakeRuntime()
                setattr(runtime, method, lambda _params, value=invalid: value)
                service = SemanticService(directory, runtime=runtime)
                with self.assertRaises(RpcFault) as caught:
                    operation(service)
                self.assertEqual(caught.exception.error.code, INTERNAL_ERROR)
                self.assertEqual(caught.exception.error.phase, phase)


if __name__ == "__main__":
    unittest.main()
