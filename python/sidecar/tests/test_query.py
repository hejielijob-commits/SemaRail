from __future__ import annotations

import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from sidecar.dispatch import Dispatcher
from sidecar.errors import CANCELLED, DATABASE_ERROR, POLICY_DENIED, TIMEOUT
from sidecar.query import (
    DatabaseSession,
    MAX_PREVIEW_BYTES,
    PostgresQueryExecutor,
    QueryLimits,
    WrenQueryService,
    validate_read_only_sql,
)


def request(method: str, params: dict[str, Any], request_id: str = "q-rpc") -> dict[str, Any]:
    return {
        "protocolVersion": "1",
        "id": request_id,
        "method": method,
        "params": params,
    }


class FakePlanner:
    def __init__(self, native_sql: str = "SELECT order_id, amount FROM orders") -> None:
        self.native_sql = native_sql
        self.calls: list[dict[str, Any]] = []

    def dry_plan(self, params: dict[str, Any]) -> dict[str, str]:
        self.calls.append(params)
        return {"nativeSql": self.native_sql}


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]], *, execute_error: BaseException | None = None) -> None:
        self.description = [
            SimpleNamespace(name="order_id", type_code=20),
            SimpleNamespace(name="amount", type_code=1700),
        ]
        self.rows = list(rows)
        self.execute_error = execute_error
        self.executed: list[tuple[str, Any | None]] = []
        self.closed = False

    def execute(self, sql: str, parameters: Any | None = None) -> None:
        self.executed.append((sql, parameters))
        if self.execute_error is not None and sql.startswith("SELECT"):
            raise self.execute_error

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows.pop(0) if self.rows else None

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, rows: list[tuple[Any, ...]], *, execute_error: BaseException | None = None) -> None:
        self.rows = rows
        self.execute_error = execute_error
        self.cursors: list[FakeCursor] = []
        self.cancelled = threading.Event()
        self.closed = False
        self.readonly: tuple[bool, bool] | None = None
        self.rolled_back = False

    def set_session(self, *, readonly: bool, autocommit: bool) -> None:
        self.readonly = (readonly, autocommit)

    def cursor(self) -> FakeCursor:
        # The first cursor is the SET LOCAL cursor; the second is the query.
        cursor = FakeCursor(
            self.rows if not self.cursors else self.rows,
            execute_error=self.execute_error,
        )
        self.cursors.append(cursor)
        return cursor

    def cancel(self) -> None:
        self.cancelled.set()

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class BlockingCursor(FakeCursor):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__([])
        self.started = started
        self.release = release

    def execute(self, sql: str, parameters: Any | None = None) -> None:
        self.executed.append((sql, parameters))
        if sql.startswith("SELECT"):
            self.started.set()
            self.release.wait(2)
            if self.release.is_set():
                raise RuntimeError("cancelled by fake database")


class BlockingConnection(FakeConnection):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__([])
        self.started = started
        self.release = release

    def cursor(self) -> BlockingCursor:
        cursor = BlockingCursor(self.started, self.release)
        self.cursors.append(cursor)
        return cursor


class PresentationExecutor:
    """Executor seam returning a chosen, already-bounded DB presentation."""

    def __init__(
        self,
        columns: list[dict[str, str]],
        rows: list[dict[str, Any]],
        *,
        returned_rows: int | None = None,
        truncated: bool = False,
    ) -> None:
        self.columns = columns
        self.rows = rows
        self.returned_rows = len(rows) if returned_rows is None else returned_rows
        self.truncated = truncated

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "queryId": kwargs["query_id"],
            "status": "success",
            "semanticSql": kwargs["semantic_sql"],
            "nativeSql": kwargs["native_sql"],
            "columns": self.columns,
            "previewRows": self.rows,
            # An executor is not trusted to supply executable/arbitrary chart
            # options; the service must replace this with ChartSpecV1.
            "chart": {"formatter": "arbitrary"},
            "stats": {
                "returnedRows": self.returned_rows,
                "durationMs": 1,
                "truncated": self.truncated,
            },
        }

    def cancel(self, _query_id: str) -> bool:
        return False


class QueryTests(unittest.TestCase):
    def test_query_prefers_dedicated_sql_history_recall_over_full_context(self) -> None:
        class DedicatedRecallPlanner(FakePlanner):
            def recall_sql_history(self, params: dict[str, Any]) -> list[dict[str, str]]:
                self.recall_params = params
                return [{
                    "id": "sql:revenue",
                    "question": "Daily revenue",
                    "sql": "SELECT day, SUM(amount) FROM orders GROUP BY day",
                }]

            def ask(self, _params: dict[str, Any]) -> dict[str, Any]:
                raise AssertionError("full semantic context must not be built for query history")

        planner = DedicatedRecallPlanner()
        result = WrenQueryService(
            planner,
            PresentationExecutor(
                [{"name": "amount", "type": "DECIMAL", "semanticRole": "measure"}],
                [{"amount": "12.50"}],
            ),
            connection_resolver=lambda *_: {"dsn": "process-local"},
        ).run({
            "projectDir": ".",
            "question": "Show revenue",
            "semanticSql": "SELECT amount FROM orders",
            "queryId": "q-dedicated-history",
        })

        self.assertEqual(result["sqlHistory"][0]["id"], "sql:revenue")
        self.assertEqual(planner.recall_params["question"], "Show revenue")

    def test_query_presentation_carries_question_and_actual_recalled_sql(self) -> None:
        class RecallPlanner(FakePlanner):
            def ask(self, params: dict[str, Any]) -> dict[str, Any]:
                self.context_params = params
                return {
                    "sqlHistory": [{
                        "id": "sql:revenue",
                        "question": "Daily revenue",
                        "sql": "SELECT day, SUM(amount) FROM orders GROUP BY day",
                        "sourcePath": "knowledge/sql/revenue.md",
                    }]
                }

        planner = RecallPlanner()
        executor = PresentationExecutor(
            [{"name": "amount", "type": "DECIMAL", "semanticRole": "measure"}],
            [{"amount": "12.50"}],
        )
        result = WrenQueryService(
            planner,
            executor,
            connection_resolver=lambda *_: {"dsn": "process-local"},
        ).run({
            "projectDir": ".",
            "question": "Show revenue",
            "semanticSql": "SELECT amount FROM orders",
            "queryId": "q-history",
        })
        self.assertEqual(result["question"], "Show revenue")
        self.assertEqual(result["sqlHistory"][0]["id"], "sql:revenue")
        self.assertEqual(planner.context_params["question"], "Show revenue")

    def _chart_query(
        self,
        columns: list[dict[str, str]],
        rows: list[dict[str, Any]],
        intent: str | None = None,
        *,
        include_intent: bool = True,
        returned_rows: int | None = None,
        truncated: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "projectDir": ".",
            "question": "Show sales",
            "semanticSql": "SELECT order_id, amount FROM orders",
            "queryId": "q-chart",
        }
        if include_intent:
            params["chartIntent"] = intent
        with patch.dict(
            os.environ,
            {"SEMARAIL_DATABASE_URL": "postgresql://user:secret@db.invalid/analytics"},
        ):
            return WrenQueryService(
                FakePlanner(),
                PresentationExecutor(
                    columns,
                    rows,
                    returned_rows=returned_rows,
                    truncated=truncated,
                ),
            ).run(params)

    def test_explicit_line_uses_temporal_dimension_and_only_drawable_measures(self) -> None:
        columns = [
            {"name": "region", "type": "TEXT", "semanticRole": "dimension"},
            {"name": "day", "type": "DATE", "semanticRole": "dimension"},
            {"name": "revenue", "type": "NUMERIC", "semanticRole": "measure"},
            {"name": "enabled", "type": "BOOLEAN", "semanticRole": "measure"},
            {"name": "label", "type": "TEXT", "semanticRole": "measure"},
        ]
        rows = [{"region": "East", "day": "2026-01-01", "revenue": "12.50", "enabled": True, "label": "n/a"}]
        result = self._chart_query(columns, rows, "line")
        self.assertEqual(
            result["chart"],
            {"version": 1, "type": "line", "x": "day", "y": ["revenue"], "tooltip": True},
        )
        self.assertNotIn("formatter", json.dumps(result["chart"]))

    def test_explicit_bar_uses_real_columns_and_caps_y_at_eight(self) -> None:
        columns = [{"name": "region", "type": "TEXT", "semanticRole": "dimension"}]
        columns.extend(
            {"name": f"metric_{index}", "type": "INTEGER", "semanticRole": "measure"}
            for index in range(10)
        )
        rows = [{"region": "East", **{f"metric_{index}": index for index in range(10)}}]
        result = self._chart_query(columns, rows, "bar")
        self.assertEqual(result["chart"]["type"], "bar")
        self.assertEqual(result["chart"]["x"], "region")
        self.assertEqual(result["chart"]["y"], [f"metric_{index}" for index in range(8)])

    def test_explicit_pie_uses_one_stable_measure(self) -> None:
        columns = [
            {"name": "region", "type": "TEXT", "semanticRole": "dimension"},
            {"name": "revenue", "type": "NUMERIC", "semanticRole": "measure"},
            {"name": "orders", "type": "BIGINT", "semanticRole": "measure"},
        ]
        rows = [{"region": "East", "revenue": "12.50", "orders": "2"}]
        result = self._chart_query(columns, rows, "pie")
        self.assertEqual(
            result["chart"],
            {"version": 1, "type": "pie", "x": "region", "y": ["revenue"], "tooltip": True},
        )

    def test_auto_is_deterministic_and_omitted_intent_means_auto(self) -> None:
        temporal_columns = [
            {"name": "day", "type": "TIMESTAMP", "semanticRole": "dimension"},
            {"name": "revenue", "type": "DOUBLE", "semanticRole": "measure"},
        ]
        temporal_rows = [{"day": "2026-01-01T00:00:00", "revenue": 12.5}]
        omitted = self._chart_query(temporal_columns, temporal_rows, include_intent=False)
        explicit = self._chart_query(temporal_columns, temporal_rows, "auto")
        self.assertEqual(omitted["chart"], explicit["chart"])
        self.assertEqual(omitted["chart"]["type"], "line")

        category_columns = [
            {"name": "region", "type": "TEXT", "semanticRole": "dimension"},
            {"name": "revenue", "type": "REAL", "semanticRole": "measure"},
        ]
        category_rows = [{"region": "East", "revenue": 12.5}]
        category = self._chart_query(category_columns, category_rows, "auto")
        self.assertEqual(category["chart"]["type"], "bar")

    def test_table_intent_explicitly_suppresses_chart(self) -> None:
        columns = [
            {"name": "region", "type": "TEXT", "semanticRole": "dimension"},
            {"name": "revenue", "type": "NUMERIC", "semanticRole": "measure"},
        ]
        result = self._chart_query(columns, [{"region": "East", "revenue": "12.50"}], "table")
        self.assertNotIn("chart", result)

    def test_truncated_preview_remains_drawable(self) -> None:
        columns = [
            {"name": "region", "type": "TEXT", "semanticRole": "dimension"},
            {"name": "revenue", "type": "NUMERIC", "semanticRole": "measure"},
        ]
        result = self._chart_query(
            columns,
            [{"region": "East", "revenue": "12.50"}],
            "bar",
            returned_rows=500,
            truncated=True,
        )
        self.assertEqual(result["chart"]["type"], "bar")
        self.assertTrue(result["stats"]["truncated"])

    def test_unrenderable_results_safely_omit_chart(self) -> None:
        cases = [
            (
                [{"name": "region", "type": "TEXT", "semanticRole": "dimension"}],
                [{"region": "East"}],
            ),
            (
                [{"name": "revenue", "type": "NUMERIC", "semanticRole": "measure"}],
                [{"revenue": "12.50"}],
            ),
            (
                [
                    {"name": "region", "type": "TEXT", "semanticRole": "dimension"},
                    {"name": "enabled", "type": "BOOLEAN", "semanticRole": "measure"},
                ],
                [{"region": "East", "enabled": True}],
            ),
            (
                [
                    {"name": "region", "type": "TEXT", "semanticRole": "dimension"},
                    {"name": "revenue", "type": "NUMERIC", "semanticRole": "measure"},
                ],
                [],
            ),
        ]
        for columns, rows in cases:
            with self.subTest(columns=columns, rows=rows):
                self.assertNotIn("chart", self._chart_query(columns, rows, "line"))

    def test_sql_policy_allows_select_and_cte_but_rejects_multi_statement_and_dml(self) -> None:
        self.assertEqual(validate_read_only_sql(" SELECT 1;"), "SELECT 1;")
        self.assertEqual(
            validate_read_only_sql("WITH x AS (SELECT 1) SELECT * FROM x"),
            "WITH x AS (SELECT 1) SELECT * FROM x",
        )
        for sql in (
            "SELECT 1; DROP TABLE orders",
            "UPDATE orders SET amount = 0",
            "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
            "SELECT pg_sleep(10)",
            "SELECT 1 /* unterminated",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    validate_read_only_sql(sql)

    def test_service_plans_then_executes_using_sidecar_only_dsn_env(self) -> None:
        planner = FakePlanner()
        seen: dict[str, Any] = {}

        class Executor:
            def execute(self, **kwargs: Any) -> dict[str, Any]:
                seen.update(kwargs)
                return {
                    "schemaVersion": 1,
                    "queryId": kwargs["query_id"],
                    "status": "success",
                    "semanticSql": kwargs["semantic_sql"],
                    "nativeSql": kwargs["native_sql"],
                    "columns": [],
                    "previewRows": [],
                    "stats": {"returnedRows": 0, "durationMs": 1, "truncated": False},
                }

            def cancel(self, _query_id: str) -> bool:
                return False

        old = os.environ.get("SEMARAIL_DATABASE_URL")
        os.environ["SEMARAIL_DATABASE_URL"] = "postgresql://user:secret@db.invalid/analytics"
        try:
            service = WrenQueryService(planner, Executor())
            dispatcher = Dispatcher(query_service=service)
            result = dispatcher.dispatch(
                request(
                    "query.run",
                    {
                        "projectDir": ".",
                        "question": "How many orders?",
                        "semanticSql": "SELECT COUNT(*) FROM orders",
                        "queryId": "q-1",
                    },
                )
            )
        finally:
            if old is None:
                os.environ.pop("SEMARAIL_DATABASE_URL", None)
            else:
                os.environ["SEMARAIL_DATABASE_URL"] = old
        self.assertTrue(result["ok"])
        self.assertEqual(planner.calls, [{"projectDir": ".", "semanticSql": "SELECT COUNT(*) FROM orders"}])
        self.assertEqual(seen["connection_info"]["connectionUrl"], "postgresql://user:secret@db.invalid/analytics")
        self.assertNotIn("secret", json.dumps(result))

    def test_service_applies_row_policy_with_bound_parameters_before_execution(self) -> None:
        original_sql = "SELECT sales.region_code, SUM(sales.amount) AS revenue FROM public.sales GROUP BY sales.region_code"
        seen: dict[str, Any] = {}

        class PolicyPlanner:
            def dry_plan(self, _params: dict[str, Any]) -> dict[str, Any]:
                return {
                    "nativeSql": original_sql,
                    "allowedPhysical": {
                        "tables": [{"schema": "public", "table": "sales"}],
                        "schemas": ["public"],
                        "catalogs": [],
                    },
                }

        class RecordingExecutor:
            def execute(self, **kwargs: Any) -> dict[str, Any]:
                seen.update(kwargs)
                return {
                    "schemaVersion": 1,
                    "queryId": kwargs["query_id"],
                    "status": "success",
                    "semanticSql": kwargs["semantic_sql"],
                    "nativeSql": kwargs["native_sql"],
                    "columns": [],
                    "previewRows": [],
                    "stats": {"returnedRows": 0, "durationMs": 1, "truncated": False},
                }

            def cancel(self, _query_id: str) -> bool:
                return False

        policy = {
            "schemaVersion": 1,
            "defaultEffect": "deny",
            "policyVersions": ["pol-sales:3"],
            "databaseSession": {
                "schemaVersion": 1,
                "subjectId": "user-a",
                "organizationId": "org-sales",
                "attributes": {"regionCodes": ["CN-JIA"]},
                "policyVersions": ["pol-sales:3"],
            },
            "tables": {
                "public.sales": {
                    "rowFilter": {
                        "op": "or",
                        "conditions": [{
                            "op": "and",
                            "conditions": [
                                {"field": "organization_id", "operator": "eq", "values": ["org-sales"]},
                                {"field": "region_code", "operator": "in", "values": ["CN-JIA"]},
                            ],
                        }],
                    },
                    "allowedColumns": ["region_code", "amount", "organization_id"],
                    "deniedColumns": [],
                }
            },
        }
        service = WrenQueryService(
            PolicyPlanner(),
            RecordingExecutor(),
            connection_resolver=lambda _project, _env: {"connectionUrl": "postgresql://local.invalid/db"},
        )

        result = service.run({
            "projectDir": ".",
            "question": "Revenue by region",
            "semanticSql": original_sql,
            "queryId": "q-row-policy",
            "authorizationPolicy": policy,
        })

        self.assertIn("FROM (SELECT * FROM public.sales WHERE", seen["native_sql"])
        self.assertLess(seen["native_sql"].index("WHERE"), seen["native_sql"].index("GROUP BY"))
        self.assertNotIn("CN-JIA", seen["native_sql"])
        self.assertEqual(set(seen["query_parameters"].values()), {"org-sales", "CN-JIA"})
        self.assertEqual(seen["database_session"].subject_id, "user-a")
        self.assertEqual(seen["database_session"].attributes_json, '{"regionCodes":["CN-JIA"]}')
        self.assertEqual(result["nativeSql"], original_sql)
        self.assertEqual(result["authorization"], {
            "rowPolicyApplied": True,
            "tableCount": 1,
            "policyVersions": ["pol-sales:3"],
        })
        self.assertNotIn("CN-JIA", json.dumps(result))

    def test_service_denies_a_physical_table_missing_from_data_policy(self) -> None:
        class PayrollPlanner:
            def dry_plan(self, _params: dict[str, Any]) -> dict[str, Any]:
                return {
                    "nativeSql": "SELECT employee_id FROM public.payroll",
                    "allowedPhysical": {
                        "tables": [{"schema": "public", "table": "payroll"}],
                        "schemas": ["public"],
                        "catalogs": [],
                    },
                }

        service = WrenQueryService(
            PayrollPlanner(),
            PresentationExecutor([], []),
            connection_resolver=lambda _project, _env: {"connectionUrl": "postgresql://local.invalid/db"},
        )
        with self.assertRaises(Exception) as caught:
            service.run({
                "projectDir": ".",
                "question": "Payroll",
                "semanticSql": "SELECT employee_id FROM public.payroll",
                "queryId": "q-policy-deny",
                "authorizationPolicy": {
                    "schemaVersion": 1,
                    "defaultEffect": "deny",
                    "tables": {},
                    "policyVersions": ["pol-sales:1"],
                    "databaseSession": {
                        "schemaVersion": 1,
                        "subjectId": "user-a",
                        "organizationId": "org-sales",
                        "attributes": {},
                        "policyVersions": ["pol-sales:1"],
                    },
                },
            })
        self.assertEqual(getattr(caught.exception, "error").code, POLICY_DENIED)
        self.assertEqual(getattr(caught.exception, "error").phase, "authorization")

    def test_service_rejects_missing_or_oversized_database_session_fail_closed(self) -> None:
        base_policy = {
            "schemaVersion": 1,
            "defaultEffect": "allow",
            "tables": {},
            "policyVersions": [],
        }
        service = WrenQueryService(
            FakePlanner(),
            PresentationExecutor([], []),
            connection_resolver=lambda *_: {"connectionUrl": "postgresql://local.invalid/db"},
        )
        params = {
            "projectDir": ".",
            "question": "Orders",
            "semanticSql": "SELECT order_id, amount FROM orders",
            "queryId": "q-session-invalid",
        }
        for database_session in (
            None,
            {
                "schemaVersion": 1,
                "subjectId": "user-a",
                "organizationId": "org-sales",
                "attributes": {"regionCodes": ["x" * 40_000]},
                "policyVersions": [],
            },
        ):
            policy = dict(base_policy)
            if database_session is not None:
                policy["databaseSession"] = database_session
            with self.subTest(database_session_present=database_session is not None):
                with self.assertRaises(Exception) as caught:
                    service.run({**params, "authorizationPolicy": policy})
                self.assertEqual(getattr(caught.exception, "error").code, POLICY_DENIED)
                self.assertEqual(getattr(caught.exception, "error").phase, "authorization")

    def test_rls_context_is_parameterized_and_set_before_user_sql(self) -> None:
        connection = FakeConnection([(1, "10.25")])
        executor = PostgresQueryExecutor(connection_factory=lambda _: connection)
        malicious = "甲'); SELECT pg_sleep(9); --"
        session = DatabaseSession(
            subject_id="user-a",
            organization_id="org-sales",
            attributes_json=json.dumps({"regionCodes": [malicious]}, ensure_ascii=False),
            policy_versions_json='["pol-sales:3"]',
        )

        executor.execute(
            query_id="q-rls",
            semantic_sql="SELECT order_id, amount FROM orders",
            native_sql="SELECT order_id, amount FROM orders",
            project_dir=".",
            connection_info={"connectionUrl": "postgresql://local.invalid/db"},
            limits=QueryLimits(),
            database_session=session,
        )

        self.assertEqual(connection.readonly, (True, False))
        self.assertEqual(len(connection.cursors), 3)
        timeout_sql, timeout_params = connection.cursors[0].executed[0]
        context_sql, context_params = connection.cursors[1].executed[0]
        user_sql, _ = connection.cursors[2].executed[0]
        self.assertTrue(timeout_sql.startswith("SET LOCAL statement_timeout"))
        self.assertIsNone(timeout_params)
        self.assertEqual(context_sql.count("set_config("), 4)
        self.assertIn("set_config('semarail.attributes', %s, true)", context_sql)
        self.assertNotIn(malicious, context_sql)
        self.assertIn(malicious, context_params[2])
        self.assertNotIn(malicious, user_sql)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_rls_context_failure_never_runs_user_sql_and_always_cleans_up(self) -> None:
        class ContextFailConnection(FakeConnection):
            def cursor(self) -> FakeCursor:
                cursor = FakeCursor(self.rows)
                if len(self.cursors) == 1:
                    cursor.execute_error = RuntimeError("set_config rejected")
                self.cursors.append(cursor)
                return cursor

        connection = ContextFailConnection([])
        executor = PostgresQueryExecutor(connection_factory=lambda _: connection)
        with self.assertRaises(Exception) as caught:
            executor.execute(
                query_id="q-rls-fail",
                semantic_sql="SELECT order_id, amount FROM orders",
                native_sql="SELECT order_id, amount FROM orders",
                project_dir=".",
                connection_info={"connectionUrl": "postgresql://local.invalid/db"},
                limits=QueryLimits(),
                database_session=DatabaseSession("user-a", "org-sales", "{}", "[]"),
            )

        self.assertEqual(getattr(caught.exception, "error").code, DATABASE_ERROR)
        self.assertEqual(len(connection.cursors), 2)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_query_result_is_bounded_and_uses_exact_strings_for_numeric_precision(self) -> None:
        connection = FakeConnection([(1, "10.25"), (2, "20.50"), (3, "30.75")])
        executor = PostgresQueryExecutor(connection_factory=lambda _: connection)
        result = executor.execute(
            query_id="q-2",
            semantic_sql="SELECT amount FROM orders",
            native_sql="SELECT order_id, amount FROM orders",
            project_dir=".",
            connection_info={"connectionUrl": "postgresql://user:secret@db.invalid/x"},
            limits=QueryLimits(max_rows=2, preview_rows=1, max_bytes=MAX_PREVIEW_BYTES),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["stats"]["returnedRows"], 2)
        self.assertTrue(result["stats"]["truncated"])
        self.assertEqual(result["previewRows"], [{"order_id": "1", "amount": "10.25"}])
        self.assertEqual(result["columns"][0]["semanticRole"], "measure")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(connection.readonly, (True, False))
        self.assertTrue(connection.closed)

    def test_database_errors_are_stable_and_do_not_include_driver_details(self) -> None:
        secret = "postgresql://alice:super-secret@db.internal/analytics"
        connection = FakeConnection([], execute_error=RuntimeError(f"dsn={secret}"))
        executor = PostgresQueryExecutor(connection_factory=lambda _: connection)
        with self.assertRaises(Exception) as caught:
            executor.execute(
                query_id="q-3",
                semantic_sql="SELECT 1",
                native_sql="SELECT 1",
                project_dir=".",
                connection_info={"connectionUrl": secret},
                limits=QueryLimits(),
            )
        error = caught.exception
        self.assertEqual(getattr(error, "error").code, DATABASE_ERROR)
        self.assertNotIn("super-secret", str(error))

    def test_statement_timeout_maps_to_timeout(self) -> None:
        QueryCanceled = type("QueryCanceled", (RuntimeError,), {})
        QueryCanceled.__module__ = "psycopg.errors"
        connection = FakeConnection([], execute_error=QueryCanceled("server detail"))
        executor = PostgresQueryExecutor(connection_factory=lambda _: connection)
        with self.assertRaises(Exception) as caught:
            executor.execute(
                query_id="q-4",
                semantic_sql="SELECT 1",
                native_sql="SELECT 1",
                project_dir=".",
                connection_info={"connectionUrl": "postgresql://u:p@db/x"},
                limits=QueryLimits(),
            )
        self.assertEqual(getattr(caught.exception, "error").code, TIMEOUT)

    def test_cancel_marks_active_connection_and_maps_run_to_cancelled(self) -> None:
        started = threading.Event()
        release = threading.Event()
        connection = BlockingConnection(started, release)
        executor = PostgresQueryExecutor(connection_factory=lambda _: connection)
        outcome: list[BaseException] = []

        def run() -> None:
            try:
                executor.execute(
                    query_id="q-cancel",
                    semantic_sql="SELECT 1",
                    native_sql="SELECT 1",
                    project_dir=".",
                    connection_info={"connectionUrl": "postgresql://u:p@db/x"},
                    limits=QueryLimits(),
                )
            except BaseException as exc:  # expected cancellation
                outcome.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(1))
        self.assertTrue(executor.cancel("q-cancel"))
        self.assertTrue(connection.cancelled.is_set())
        release.set()
        worker.join(2)
        self.assertEqual(len(outcome), 1)
        self.assertEqual(getattr(outcome[0], "error").code, CANCELLED)
        self.assertFalse(executor.cancel("q-cancel"))

    def test_dispatch_rejects_plaintext_connection_info(self) -> None:
        response = Dispatcher().dispatch(
            request(
                "query.run",
                {
                    "projectDir": ".",
                    "question": "q",
                    "semanticSql": "SELECT 1",
                    "queryId": "q-5",
                    "connectionInfo": {"password": "secret"},
                },
            )
        )
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertNotIn("secret", json.dumps(response))


if __name__ == "__main__":
    unittest.main()
