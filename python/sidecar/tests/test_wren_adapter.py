from __future__ import annotations

import io
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sidecar.dispatch import Dispatcher, SidecarDependencies
from sidecar.semantic_index import build_semantic_documents
from sidecar.semantic_retrieval import HybridSemanticRetriever
from sidecar.wren_adapter import LazyWrenAdapter, _semantic_question_type, default_dependencies


class WrenAdapterTests(unittest.TestCase):
    def test_v2_rejects_semantic_alias_with_unauthorized_physical_source_before_scoring(self) -> None:
        manifest = {
            "models": [{
                "name": "orders", "tableReference": {"table": "private_orders"},
                "columns": [{"name": "order_id", "type": "BIGINT"}],
            }],
            "relationships": [],
        }
        context = SimpleNamespace(build_json=lambda _: manifest)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            adapter = LazyWrenAdapter(
                module_loader=lambda _: context,
                version_provider=lambda: "0.13.2",
                semantic_retriever=HybridSemanticRetriever(embedder=None),
            )
            response = Dispatcher(context_provider=adapter).dispatch({
                "protocolVersion": "2", "id": "physical-source", "method": "context.ask",
                "params": {
                    "projectDir": str(project), "question": "orders", "contextVersion": 2,
                    "authorizationPolicy": {
                        "schemaVersion": 1, "defaultEffect": "deny",
                        "tables": {"public.orders": {"allowedColumns": ["order_id"], "deniedColumns": []}},
                    },
                },
                "traceId": "physical-source",
            })
            direct = adapter.ask_v2({
                "projectDir": str(project), "question": "orders", "contextVersion": 2,
                "authorizationPolicy": {
                    "schemaVersion": 1, "defaultEffect": "deny",
                    "tables": {"public.orders": {"allowedColumns": ["order_id"], "deniedColumns": []}},
                },
            })
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertEqual(result["schema"]["models"], [])
        self.assertGreater(result["retrievalSummary"]["filteredCount"], 0)
        self.assertNotIn("private_orders", str(result))
        self.assertEqual(direct["schema"]["models"], [])
        self.assertNotIn("_authorizationCatalog", direct)
        self.assertNotIn("private_orders", str(direct))

    def test_context_v2_keeps_all_composite_primary_key_columns(self) -> None:
        manifest = {
            "models": [{
                "name": "performance_reviews",
                "primaryKey": ["employee_id", "review_date"],
                "columns": [
                    {"name": "employee_id", "type": "BIGINT"},
                    {"name": "review_date", "type": "DATE"},
                    {"name": "performance_score", "type": "DECIMAL"},
                    {"name": "unrelated_note", "type": "TEXT"},
                ],
            }],
            "relationships": [],
        }
        documents = build_semantic_documents(manifest, project_revision="r1")
        by_id = {document.id: document for document in documents}

        class SearchResult(list):
            status = SimpleNamespace(
                state="active", active_revision="r1", revision="r1",
                document_count=len(documents), backend="lexical",
                embedding_model_id=None, embedding_model_version=None,
                embedding_dimension=None, index_build_version=None,
                last_build_at=None, build_duration_ms=None,
            )
            trace: list[object] = []

        hits = SearchResult([
            SimpleNamespace(document=by_id["model:performance_reviews"]),
            SimpleNamespace(document=by_id["column:performance_reviews.performance_score"]),
        ])
        adapter = LazyWrenAdapter(version_provider=lambda: "0.13.2")
        result = adapter._context_v2_result(manifest, Path("."), "r1", hits, documents)
        columns = {column["name"] for column in result["schema"]["models"][0]["columns"]}
        self.assertEqual(columns, {"employee_id", "review_date", "performance_score"})

    def test_v1_v2_shadow_logs_text_free_size_and_retrieval_comparison(self) -> None:
        manifest = {
            "models": [{"name": "employees", "columns": [{"name": "employee_id", "type": "BIGINT"}]}],
            "relationships": [],
        }
        context = SimpleNamespace(build_json=lambda _: manifest)
        logger = logging.getLogger(f"shadow-test-{id(self)}")
        logger.setLevel(logging.INFO)
        records: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = Capture()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            adapter = LazyWrenAdapter(
                module_loader=lambda _: context,
                version_provider=lambda: "0.13.2",
                context_retriever=lambda *_: None,
                schema_describer=lambda _: "public summary",
                semantic_retriever=HybridSemanticRetriever(embedder=None),
                logger=logger,
            )
            with patch.dict("os.environ", {"SEMARAIL_CONTEXT_V2_SHADOW": "1"}):
                adapter.ask({"projectDir": str(project), "question": "employee id"})

        record = next(item for item in records if item.getMessage() == "semantic context v2 shadow completed")
        self.assertEqual(len(record.questionHash), 64)
        self.assertGreater(record.v1ContextBytes, 0)
        self.assertGreater(record.v2ContextBytes, 0)
        self.assertGreaterEqual(record.candidateCount, record.selectedCount)
        self.assertFalse(hasattr(record, "question"))

    def test_question_type_drives_deterministic_context_budget_profile(self) -> None:
        self.assertEqual(_semantic_question_type("List employee names"), "singleTable")
        self.assertEqual(_semantic_question_type("各区域平均薪资"), "metric")
        self.assertEqual(_semantic_question_type("Compare salary and performance"), "crossModel")

    def test_context_v2_uses_revisioned_hybrid_retrieval(self) -> None:
        class Embedder:
            config = {"provider": "test", "model": "v1"}

            def embed(self, texts: list[str], **_: object) -> list[list[float]]:
                return [[1.0, 0.0] if "order" in text.casefold() else [0.0, 1.0] for text in texts]

        manifest = {
            "models": [{
                "name": "orders",
                "tableReference": {"table": "physical_orders"},
                "columns": [
                    {"name": "order_id", "type": "BIGINT"},
                    {"name": "ordered_at", "type": "TIMESTAMP"},
                    {"name": "unrelated_note", "type": "TEXT"},
                ],
            }],
            "relationships": [],
            "cubes": [{
                "name": "order_metrics",
                "baseObject": "orders",
                # Missing source type is valid MDL input; Context v2 still has
                # to satisfy its required metric contract deterministically.
                "measures": [{"name": "order_count", "expression": "COUNT(order_id)"}],
                "dimensions": [{"name": "order_key", "expression": "order_id"}],
                "timeDimensions": [{"name": "order_day", "expression": "ordered_at"}],
            }],
            "rules": [{
                "id": "order-grain", "text": "Use one row per order.",
                "referencedModels": ["orders"], "referencedColumns": ["orders.order_id"],
                "ruleType": "grain", "mandatory": True,
                "effectiveFrom": "2026-01-01", "allowedRoles": ["analyst"],
            }],
            "sqlExamples": [{
                "id": "orders", "question": "List orders", "sql": "SELECT order_id FROM orders",
                "referencedModels": ["orders"], "referencedColumns": ["orders.order_id"],
                "dataSource": "warehouse", "roles": ["analyst"], "version": "v2",
            }],
        }
        context = SimpleNamespace(build_json=lambda _: manifest)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            (project / "semantic-console").mkdir()
            (project / "semantic-console/locales.yml").write_text(
                """models:\n  orders:\n    displayName:\n      zh-CN: 订单\n    businessDomain: Commerce\n    columns:\n      order_id:\n        displayName:\n          zh-CN: 订单编号\n        semanticRole: dimension\n        acceptedValues: [web, store]\ncubes:\n  order_metrics:\n    displayName:\n      zh-CN: 订单指标\n    timeDimensions:\n      order_day:\n        displayName:\n          zh-CN: 下单日期\n        grain: day\n""",
                encoding="utf-8",
            )
            adapter = LazyWrenAdapter(
                module_loader=lambda _: context,
                version_provider=lambda: "0.13.2",
                semantic_retriever=HybridSemanticRetriever(embedder=Embedder()),
            )
            result = adapter.ask_v2({
                "projectDir": str(project), "question": "orders", "contextVersion": 2,
                "budgets": {"topK": {"schema": 1, "rules": 5, "sqlExamples": 5}},
            })

        self.assertEqual(result["schemaVersion"], 2)
        self.assertNotIn("_authorizationCatalog", result)
        self.assertNotIn("physical_orders", str(result))
        self.assertEqual(result["indexStatus"]["status"], "ready")
        self.assertEqual(result["indexStatus"]["backend"], "hybrid")
        self.assertEqual(result["indexStatus"]["embeddingModelId"], "v1")
        self.assertEqual(result["indexStatus"]["embeddingDimension"], 2)
        self.assertEqual(result["indexStatus"]["indexBuildVersion"], 1)
        self.assertGreaterEqual(result["indexStatus"]["buildDurationMs"], 0)
        self.assertTrue(result["indexStatus"]["lastBuildAt"].endswith("Z"))
        self.assertEqual([item["name"] for item in result["schema"]["models"]], ["orders"])
        returned_columns = {
            item["name"]: item for item in result["schema"]["models"][0]["columns"]
        }
        self.assertIn("order_id", returned_columns)
        self.assertNotIn("unrelated_note", returned_columns)
        self.assertEqual(result["schema"]["models"][0]["properties"]["displayName"]["zh-CN"], "订单")
        self.assertEqual(result["schema"]["models"][0]["properties"]["businessDomain"], "Commerce")
        self.assertEqual(returned_columns["order_id"]["semanticRole"], "dimension")
        self.assertEqual(returned_columns["order_id"]["properties"]["acceptedValues"], ["web", "store"])
        self.assertEqual(
            next(item for item in result["metrics"] if item["name"] == "order_count")["type"],
            "UNKNOWN",
        )
        self.assertEqual(
            {item["kind"] for item in result["metrics"]},
            {"cube", "measure", "dimension", "timeDimension"},
        )
        order_day = next(item for item in result["metrics"] if item["name"] == "order_day")
        self.assertEqual(order_day["properties"]["displayName"]["zh-CN"], "下单日期")
        self.assertEqual(order_day["properties"]["grain"], "day")
        self.assertTrue(result["retrievalTrace"])
        self.assertTrue(result["retrievalTrace"][0]["documentId"])
        self.assertGreaterEqual(result["retrievalSummary"]["candidateCount"], result["retrievalSummary"]["selectedCount"])
        self.assertEqual(result["retrievalSummary"]["filteredCount"], 0)
        self.assertGreaterEqual(result["retrievalSummary"]["latencyMs"], 0)
        self.assertEqual(result["rules"][0]["effectiveFrom"], "2026-01-01")
        self.assertEqual(result["rules"][0]["allowedRoles"], ["analyst"])
        self.assertEqual(result["sqlExamples"][0]["dataSource"], "warehouse")
        self.assertEqual(result["sqlExamples"][0]["roles"], ["analyst"])
        self.assertEqual(result["sqlExamples"][0]["version"], "v2")

    def test_context_v2_only_enables_graph_for_cross_model_questions(self) -> None:
        class CapturingRetriever(HybridSemanticRetriever):
            def __init__(self) -> None:
                super().__init__(embedder=None)
                self.channel_calls: list[tuple[str, ...]] = []

            def search(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
                self.channel_calls.append(tuple(kwargs.get("channels", ())))
                return super().search(*args, **kwargs)

        manifest = {
            "models": [{
                "name": "employees",
                "columns": [{"name": "salary", "type": "DOUBLE"}],
            }],
            "relationships": [],
        }
        context = SimpleNamespace(build_json=lambda _: manifest)
        retriever = CapturingRetriever()
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            adapter = LazyWrenAdapter(
                module_loader=lambda _: context,
                version_provider=lambda: "0.13.2",
                semantic_retriever=retriever,
            )
            adapter.ask_v2({
                "projectDir": str(project),
                "question": "List employee salary",
                "contextVersion": 2,
            })
            adapter.ask_v2({
                "projectDir": str(project),
                "question": "Compare employee salary and performance",
                "contextVersion": 2,
            })

        self.assertNotIn("graph", retriever.channel_calls[0])
        self.assertIn("graph", retriever.channel_calls[1])

    def test_default_dependencies_accepts_canonical_connection_resolver(self) -> None:
        def resolver(_project_dir: str, _env_name: str) -> dict[str, str]:
            return {"datasource": "postgres", "connectionUrl": "redacted-in-test"}

        dependencies = default_dependencies(connection_resolver=resolver)

        self.assertIsNotNone(dependencies.query_service)
        self.assertIs(dependencies.query_service.connection_resolver, resolver)

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
            "protocolVersion": "2",
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
