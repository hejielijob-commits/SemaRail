from __future__ import annotations

import base64
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sidecar.dispatch import Dispatcher, SidecarDependencies
from sidecar.wren_adapter import LazyWrenAdapter


def rpc_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": "1",
        "id": "rpc-1",
        "method": method,
        "params": params,
    }


def manifest(project_path: Path) -> dict[str, Any]:
    return {
        "catalog": "analytics",
        "schema": "public",
        "dataSource": "postgres",
        "models": [
            {
                "name": "orders",
                "description": f"Orders from {project_path}",
                "tableReference": {"table": "orders"},
                "primaryKey": "order_id",
                "columns": [
                    {"name": "order_id", "type": "INTEGER", "notNull": True},
                    {
                        "name": "revenue",
                        "type": "DECIMAL",
                        "isCalculated": True,
                        "expression": "amount * quantity",
                    },
                ],
            }
        ],
        "relationships": [
            {
                "name": "orders_customer",
                "models": ["orders", "customers"],
                "joinType": "MANY_TO_ONE",
                "condition": "orders.customer_id = customers.customer_id",
            }
        ],
        "views": [
            {
                "name": "order_totals",
                "statement": "SELECT order_id, SUM(revenue) FROM orders GROUP BY 1",
            }
        ],
        "layoutVersion": 3,
    }


class FakeEngine:
    def __init__(self, native_sql: str) -> None:
        self.native_sql = native_sql
        self.seen_sql: list[str] = []
        self.closed = False

    def dry_plan(self, sql: str) -> str:
        self.seen_sql.append(sql)
        return self.native_sql

    def close(self) -> None:
        self.closed = True


class ContextAndDryPlanTests(unittest.TestCase):
    def test_context_ask_recalls_confirmed_sql_with_stable_safe_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            source = project / "knowledge" / "sql" / "revenue.md"

            class FakeIndex:
                def search(self, question: str, *, limit: int) -> list[dict[str, Any]]:
                    self.question = question
                    self.limit = limit
                    return [{
                        "nl_query": "Daily revenue",
                        "sql_query": "SELECT day, SUM(amount) FROM orders GROUP BY day",
                        "path": str(source),
                    }]

            fake_index = FakeIndex()
            context_module = SimpleNamespace(build_json=lambda path: manifest(path))
            index_module = SimpleNamespace(get_index=lambda *_: fake_index)

            def load(name: str) -> Any:
                return index_module if name == "wren.memory.index_backend" else context_module

            adapter = LazyWrenAdapter(
                module_loader=load,
                context_retriever=lambda *_: None,
                schema_describer=lambda _: "orders schema",
            )
            response = Dispatcher(context_provider=adapter).dispatch(rpc_request(
                "context.ask",
                {"projectDir": str(project), "question": "Show revenue"},
            ))

            reference = response["result"]["sqlHistory"][0]
            self.assertTrue(reference["id"].startswith("sql:"))
            self.assertEqual(reference["question"], "Daily revenue")
            self.assertEqual(reference["sourcePath"], "knowledge/sql/revenue.md")
            self.assertNotIn(str(project), json.dumps(reference))
            self.assertEqual(fake_index.question, "Show revenue")
            self.assertEqual(fake_index.limit, 3)

    def test_context_ask_builds_versioned_contract_and_uses_context_retriever(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            built = manifest(project.resolve())
            calls: list[tuple[dict[str, Any], str, Path]] = []
            context_module = SimpleNamespace(build_json=lambda path: built)

            def retrieve(
                value: dict[str, Any],
                question: str,
                project_path: Path,
            ) -> dict[str, Any]:
                calls.append((value, question, project_path))
                return {
                    "strategy": "search",
                    "results": [
                        {"text": f"Relevant orders at {project_path}"},
                        {"text": "password=do-not-leak postgres://u:p@db.local/x"},
                    ],
                }

            adapter = LazyWrenAdapter(
                module_loader=lambda _: context_module,
                version_provider=lambda: "0.13.2",
                context_retriever=retrieve,
            )
            response = Dispatcher(
                SidecarDependencies(context_provider=adapter)
            ).dispatch(rpc_request(
                "context.ask",
                {"projectDir": str(project), "question": "Which fields describe revenue?"},
            ))

            self.assertTrue(response["ok"])
            result = response["result"]
            self.assertEqual(result["schemaVersion"], 1)
            self.assertTrue(result["projectRevision"].startswith("sha256:"))
            self.assertEqual(result["models"][0]["name"], "orders")
            self.assertEqual(result["relationships"][0]["name"], "orders_customer")
            self.assertEqual(result["views"][0]["name"], "order_totals")
            self.assertEqual(calls[0][1], "Which fields describe revenue?")
            wire = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(str(project.resolve()), wire)
            self.assertNotIn("do-not-leak", wire)
            self.assertNotIn("postgres://u:p@", wire)

    def test_context_ask_falls_back_to_public_schema_description(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            context_module = SimpleNamespace(build_json=lambda path: manifest(path))
            adapter = LazyWrenAdapter(
                module_loader=lambda _: context_module,
                context_retriever=lambda *_: None,
                schema_describer=lambda _: "### Model: orders",
            )
            response = Dispatcher(context_provider=adapter).dispatch(rpc_request(
                "context.ask",
                {"projectDir": str(project), "question": "orders"},
            ))
            self.assertEqual(response["result"]["summary"], "### Model: orders")

    def test_dry_plan_builds_manifest_and_never_connects_to_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            built = manifest(project.resolve())
            created: list[dict[str, Any]] = []
            engine = FakeEngine("WITH orders AS (...) SELECT * FROM orders")

            def engine_factory(**kwargs: Any) -> FakeEngine:
                created.append(kwargs)
                return engine

            adapter = LazyWrenAdapter(
                module_loader=lambda _: SimpleNamespace(build_json=lambda path: built),
                engine_factory=engine_factory,
            )
            semantic_sql = "SELECT * FROM orders"
            response = Dispatcher(query_planner=adapter).dispatch(rpc_request(
                "query.dryPlan",
                {"projectDir": str(project), "semanticSql": semantic_sql},
            ))

            self.assertTrue(response["ok"])
            self.assertEqual(response["result"]["semanticSql"], semantic_sql)
            self.assertEqual(
                response["result"]["nativeSql"],
                "WITH orders AS (...) SELECT * FROM orders",
            )
            self.assertTrue(response["result"]["projectRevision"].startswith("sha256:"))
            self.assertEqual(created[0]["connection_info"], {})
            self.assertEqual(created[0]["data_source"], "postgres")
            decoded = json.loads(base64.b64decode(created[0]["manifest_str"]))
            self.assertEqual(decoded, built)
            self.assertEqual(engine.seen_sql, [semantic_sql])
            self.assertTrue(engine.closed)

    def test_dry_plan_error_and_logs_do_not_leak_sql_dsn_or_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            secret_sql = "SELECT secret_token FROM users"
            secret_dsn = "postgres://alice:password@db.internal/analytics"

            class FailingEngine:
                def dry_plan(self, sql: str) -> str:
                    raise RuntimeError(f"{sql} {secret_dsn} {project.resolve()}")

                def close(self) -> None:
                    return None

            adapter = LazyWrenAdapter(
                module_loader=lambda _: SimpleNamespace(
                    build_json=lambda path: manifest(path)
                ),
                engine_factory=lambda **_: FailingEngine(),
            )
            log = io.StringIO()
            logger = logging.getLogger("sidecar.test.dry-plan-no-leak")
            logger.handlers.clear()
            handler = logging.StreamHandler(log)
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            try:
                response = Dispatcher(query_planner=adapter, logger=logger).dispatch(
                    rpc_request(
                        "query.dryPlan",
                        {"projectDir": str(project), "semanticSql": secret_sql},
                    )
                )
            finally:
                logger.removeHandler(handler)
            self.assertEqual(response["error"]["code"], "SEMANTIC_ERROR")
            serialized = json.dumps(response)
            combined = serialized + log.getvalue()
            self.assertNotIn(secret_sql, combined)
            self.assertNotIn(secret_dsn, combined)
            self.assertNotIn(str(project.resolve()), combined)
            self.assertNotIn("Traceback", combined)


if __name__ == "__main__":
    unittest.main()
