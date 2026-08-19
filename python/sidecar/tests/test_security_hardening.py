from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sidecar.dispatch import Dispatcher
from sidecar.errors import DATABASE_ERROR, INVALID_PARAMS, POLICY_DENIED
from sidecar.query import (
    MAX_QUERY_CONCURRENCY,
    MAX_PREVIEW_BYTES,
    MAX_TIMEOUT_MS,
    PostgresQueryExecutor,
    QueryLimits,
    validate_read_only_sql,
)
from sidecar.protocol import encode_frame, read_frame
from sidecar.server import JsonRpcServer
from sidecar.sql_policy import (
    PhysicalAllowlist,
    PhysicalTable,
    SqlPolicyError,
    validate_native_sql,
    validate_semantic_sql,
)
from sidecar.wren_adapter import (
    MAX_CONTEXT_KNOWLEDGE_BYTES,
    LazyWrenAdapter,
)


def rpc(method: str, params: dict[str, Any], request_id: str = "id") -> dict[str, Any]:
    return {
        "protocolVersion": "1",
        "id": request_id,
        "method": method,
        "params": params,
    }


class SecurityHardeningTests(unittest.TestCase):
    def test_ast_rejects_cte_dml_comments_dollar_quotes_and_multi_statement(self) -> None:
        self.assertEqual(validate_semantic_sql("SELECT 'DROP TABLE x' AS note"), "SELECT 'DROP TABLE x' AS note")
        self.assertEqual(validate_semantic_sql("SELECT $$UPDATE x SET a=1;$$ AS note"), "SELECT $$UPDATE x SET a=1;$$ AS note")
        for sql in (
            "WITH changed AS (DELETE FROM orders RETURNING *) SELECT * FROM changed",
            "SELECT 1; DROP TABLE orders",
            "COPY orders TO STDOUT",
            "CALL dangerous_proc()",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(SqlPolicyError):
                    validate_semantic_sql(sql)

        diagnostics = io.StringIO()
        with mock.patch("sys.stderr", diagnostics):
            with self.assertRaises(SqlPolicyError):
                validate_semantic_sql("CALL secret_proc('password=super-secret')")
        self.assertNotIn("super-secret", diagnostics.getvalue())

    def test_native_ast_enforces_physical_object_and_function_allowlists(self) -> None:
        allowlist = PhysicalAllowlist(frozenset({PhysicalTable("wren", "public", "orders")}))
        self.assertEqual(
            validate_native_sql(
                "WITH x AS (SELECT * FROM public.orders) SELECT COUNT(*) FROM x",
                allowed_physical=allowlist,
            ),
            "WITH x AS (SELECT * FROM public.orders) SELECT COUNT(*) FROM x",
        )
        for sql in (
            # A same-named object in an earlier search_path schema must not be
            # authorized by the MDL's public.orders entry.
            "SELECT * FROM orders",
            "SELECT * FROM analytics.orders",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(SqlPolicyError):
                    validate_native_sql(sql, allowed_physical=allowlist)
        for sql in (
            "SELECT * FROM pg_catalog.pg_tables",
            "SELECT * FROM unknown_table",
            "SELECT pg_sleep(1) FROM public.orders",
            "SELECT current_setting('search_path') FROM public.orders",
            "SELECT unknown_user_function() FROM public.orders",
            "WITH x AS (UPDATE public.orders SET id=1 RETURNING *) SELECT * FROM x",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(SqlPolicyError):
                    validate_native_sql(sql, allowed_physical=allowlist)

    def test_timeout_is_hard_capped_at_thirty_seconds(self) -> None:
        params = {
            "projectDir": ".",
            "question": "q",
            "semanticSql": "SELECT 1",
            "queryId": "q",
            "timeoutMs": MAX_TIMEOUT_MS + 1,
        }
        response = Dispatcher().dispatch(rpc("query.run", params))
        self.assertEqual(response["error"]["code"], INVALID_PARAMS)
        self.assertIn("timeoutMs", response["error"]["message"])

    def test_wren_dry_plan_passes_strict_config_and_returns_physical_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            built = {
                "catalog": "wren",
                "schema": "public",
                "dataSource": "postgres",
                "models": [
                    {"name": "orders", "tableReference": {"schema": "public", "table": "orders"}}
                ],
            }
            seen: dict[str, Any] = {}

            class Engine:
                def dry_plan(self, _sql: str) -> str:
                    return "SELECT * FROM public.orders"

                def close(self) -> None:
                    return None

            context = SimpleNamespace(build_json=lambda _path: built)

            def factory(**kwargs: Any) -> Engine:
                seen.update(kwargs)
                return Engine()

            adapter = LazyWrenAdapter(
                module_loader=lambda name: context,
                engine_factory=factory,
            )
            result = adapter.dry_plan(
                {"projectDir": str(project), "semanticSql": "SELECT * FROM orders"}
            )
            self.assertTrue(seen["config"].strict_mode)
            self.assertIn("pg_read_file", seen["config"].denied_functions)
            self.assertEqual(result["allowedPhysical"]["schemas"], ["public"])
            self.assertEqual(result["allowedPhysical"]["tables"][0]["table"], "orders")

    def test_context_rules_are_loaded_and_aggregate_utf8_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
            secret = "password=super-secret postgres://alice:p@db.local/x " + ("规则" * 50_000)
            context = SimpleNamespace(
                build_json=lambda _path: {"models": []},
                load_rules=lambda _path: (secret, False),
            )
            adapter = LazyWrenAdapter(module_loader=lambda _name: context)
            result = adapter.ask({"projectDir": str(project), "question": "rules?"})
            encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.assertLessEqual(
                sum(len(item.encode("utf-8")) for item in result.get("knowledge", [])),
                MAX_CONTEXT_KNOWLEDGE_BYTES,
            )
            self.assertNotIn("super-secret", encoded.decode("utf-8"))
            self.assertNotIn("postgres://alice:p@", encoded.decode("utf-8"))

    def test_query_executor_wraps_select_with_server_limit(self) -> None:
        class Cursor:
            description = [SimpleNamespace(name="value", type_code=23)]

            def __init__(self) -> None:
                self.sql: list[str] = []

            def execute(self, sql: str) -> None:
                self.sql.append(sql)

            def fetchone(self) -> tuple[int, ...] | None:
                return (1,)

            def close(self) -> None:
                return None

        class Connection:
            def __init__(self) -> None:
                self.cursors: list[Cursor] = []

            def set_session(self, **_kwargs: Any) -> None:
                return None

            def cursor(self) -> Cursor:
                cursor = Cursor()
                self.cursors.append(cursor)
                return cursor

            def rollback(self) -> None:
                return None

            def close(self) -> None:
                return None

        connection = Connection()
        executor = PostgresQueryExecutor(connection_factory=lambda _info: connection)
        result = executor.execute(
            query_id="bounded",
            semantic_sql="SELECT 1",
            native_sql="SELECT 1; -- trailing comment",
            project_dir=".",
            connection_info={},
            limits=QueryLimits(max_rows=3),
        )
        self.assertEqual(result["status"], "success")
        self.assertIn("LIMIT 4", connection.cursors[1].sql[0])
        self.assertNotIn("-- trailing comment", connection.cursors[1].sql[0])

    def test_oversize_utf8_row_is_not_retained_in_preview(self) -> None:
        class Cursor:
            description = [SimpleNamespace(name="note", type_code=25)]

            def __init__(self) -> None:
                self.first = True

            def execute(self, _sql: str) -> None:
                return None

            def fetchone(self) -> tuple[str, ...] | None:
                if self.first:
                    self.first = False
                    return ("界" * MAX_PREVIEW_BYTES,)
                return None

            def close(self) -> None:
                return None

        class Connection:
            def set_session(self, **_kwargs: Any) -> None:
                return None

            def cursor(self) -> Cursor:
                return Cursor()

            def rollback(self) -> None:
                return None

            def close(self) -> None:
                return None

        result = PostgresQueryExecutor(
            connection_factory=lambda _info: Connection()
        ).execute(
            query_id="oversize",
            semantic_sql="SELECT note FROM orders",
            native_sql="SELECT note FROM public.orders",
            project_dir=".",
            connection_info={},
            limits=QueryLimits(max_rows=1),
        )
        self.assertEqual(result["previewRows"], [])
        self.assertTrue(result["stats"]["truncated"])
        self.assertLessEqual(
            len(json.dumps(result["previewRows"], ensure_ascii=False).encode("utf-8")),
            MAX_PREVIEW_BYTES,
        )

    def test_direct_executor_rejects_third_active_query(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class Cursor:
            description = []

            def execute(self, sql: str) -> None:
                if sql.startswith("SELECT * FROM"):
                    started.set()
                    release.wait(2)

            def fetchone(self) -> None:
                return None

            def close(self) -> None:
                return None

        class Connection:
            def set_session(self, **_kwargs: Any) -> None:
                return None

            def cursor(self) -> Cursor:
                return Cursor()

            def rollback(self) -> None:
                return None

            def close(self) -> None:
                return None

            def cancel(self) -> None:
                release.set()

        executor = PostgresQueryExecutor(connection_factory=lambda _info: Connection())
        errors: list[BaseException] = []

        def run(number: int) -> None:
            try:
                executor.execute(
                    query_id=f"concurrent-{number}",
                    semantic_sql="SELECT * FROM orders",
                    native_sql="SELECT * FROM orders",
                    project_dir=".",
                    connection_info={},
                    limits=QueryLimits(timeout_ms=2_000),
                )
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=run, args=(index,)) for index in range(MAX_QUERY_CONCURRENCY)]
        for worker in workers:
            worker.start()
        self.assertTrue(started.wait(1))
        third = threading.Thread(target=run, args=(3,))
        third.start()
        third.join(1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(getattr(errors[0], "error").code, DATABASE_ERROR)
        release.set()
        for worker in workers:
            worker.join(2)

    def test_server_rejects_third_run_instead_of_queueing(self) -> None:
        started = 0
        started_lock = threading.Lock()
        started_event = threading.Event()
        release = threading.Event()

        class BlockingService:
            def run(self, _params: dict[str, Any]) -> dict[str, Any]:
                nonlocal started
                with started_lock:
                    started += 1
                    if started == MAX_QUERY_CONCURRENCY:
                        started_event.set()
                release.wait(2)
                return {"status": "success", "queryId": "run"}

            def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
                release.set()
                return {"queryId": params["queryId"], "cancelled": True}

        frames = []
        for index in range(MAX_QUERY_CONCURRENCY + 1):
            frames.append(
                encode_frame(
                    rpc(
                        "query.run",
                        {
                            "projectDir": ".",
                            "question": "q",
                            "semanticSql": "SELECT 1",
                            "queryId": f"run-{index}",
                        },
                        f"rpc-{index}",
                    )
                )
            )
        output = io.BytesIO()
        server_thread = threading.Thread(
            target=JsonRpcServer(
                Dispatcher(query_service=BlockingService())
            ).serve,
            args=(io.BytesIO(b"".join(frames)), output),
        )
        server_thread.start()
        self.assertTrue(started_event.wait(1))
        # The third response is emitted while the first two workers remain
        # blocked, proving there is no hidden queue of active executions.
        deadline = time.monotonic() + 1
        while output.tell() == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        release.set()
        server_thread.join(2)
        self.assertFalse(server_thread.is_alive())
        output.seek(0)
        responses = []
        while True:
            response = read_frame(output)
            if response is None:
                break
            responses.append(response)
        self.assertEqual(len(responses), MAX_QUERY_CONCURRENCY + 1)
        rejected = [item for item in responses if not item["ok"]]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error"]["code"], DATABASE_ERROR)


if __name__ == "__main__":
    unittest.main()
