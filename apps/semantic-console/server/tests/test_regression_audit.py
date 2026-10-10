"""Focused audit regressions for metadata and lifecycle boundaries."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from server.tests import test_regression as regression_test_support
from server.tests import test_access_control_storage as postgres_test_support
from server import regression as regression_module
from server.diagnostics import DiagnosticError, DiagnosticStore
from server.access_control import AccessControlStore
from server.regression import (
    RegressionRunner,
    RegressionStore,
    _acquire_postgres_execution_lock,
    _acquire_storage_execution_lock,
)


def _try_start_regression_in_other_process(
    database_path: str,
    bootstrap_token: str,
    authorization: str,
    case_id: str,
    output,
) -> None:
    """Exercise the durable lock from an independently initialized process."""

    access = AccessControlStore(database_path, bootstrap_token=bootstrap_token)
    auth = access.authenticate(authorization)
    store = RegressionStore(DiagnosticStore(access))
    runner = RegressionRunner(store, object())
    try:
        runner.start(auth, "runtime-test", authorization, [case_id])
    except Exception as exc:
        output.put((getattr(exc, "code", type(exc).__name__), getattr(exc, "status", None)))
    else:
        output.put(("STARTED", 202))


def _try_acquire_configuration_guard_in_other_process(
    database_path: str,
    bootstrap_token: str,
    authorization: str,
    _case_id: str,
    output,
) -> None:
    """Check that a separate Console process sees the held project lock."""

    access = AccessControlStore(database_path, bootstrap_token=bootstrap_token)
    auth = access.authenticate(authorization)
    store = RegressionStore(DiagnosticStore(access))
    runner = RegressionRunner(store, object())
    try:
        guard = runner.acquire_configuration_guard(auth, "runtime-test")
    except Exception as exc:
        output.put((getattr(exc, "code", type(exc).__name__), getattr(exc, "status", None)))
    else:
        guard.release()
        output.put(("ACQUIRED", 200))


class _AdvisoryLockConnection:
    def __init__(self, *, acquired: bool, calls: list[tuple[str, tuple]]) -> None:
        self.acquired = acquired
        self.calls = calls
        self.closed = False

    def execute(self, statement: str, params: tuple = ()):
        self.calls.append((statement, params))
        if "pg_try_advisory_lock" in statement:
            return SimpleNamespace(fetchone=lambda: {"acquired": self.acquired})
        return SimpleNamespace(fetchone=lambda: {"released": True})

    def commit(self) -> None:
        self.calls.append(("COMMIT", ()))

    def close(self) -> None:
        self.closed = True


class RegressionCredentialAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the governed runtime, project, and admin route fixture without
        # inheriting its full test suite.
        regression_test_support.RegressionTests.setUp(self)

    def call(self, method: str, path: str, body=None):
        return self.app.request(
            method,
            "/api/v1/diagnostics/" + path,
            body,
            authorization=self.authorization,
        )

    def create_case(self, case=None):
        case = case or self.case_input
        status, draft = self.call(
            "POST", "regression-cases",
            {"feedbackId": self.feedback["feedbackId"], "case": case},
        )
        self.assertEqual(status, 201, draft)
        status, reviewed = self.call(
            "PUT", "regression-cases/" + draft["id"],
            {"expectedRevision": draft["revision"], "case": case, "enable": True},
        )
        self.assertEqual(status, 200, reviewed)
        return reviewed

    def test_dataset_metadata_rejects_managed_credential(self) -> None:
        credential = self.authorization.removeprefix("Bearer ")
        status, response = self.call(
            "POST",
            "test-datasets",
            {
                "id": "credential-audit",
                "version": credential,
                "datasourceId": self.datasource_id,
            },
        )

        self.assertEqual(status, 400)
        self.assertNotIn(credential, json.dumps(response))
        _, datasets = self.call("GET", "test-datasets")
        self.assertFalse(any(item["id"] == "credential-audit" for item in datasets["items"]))

    def test_nested_condition_secret_is_rejected_without_persistence(self) -> None:
        marker = "nested-condition-secret-value"
        payload = {
            "feedbackId": self.feedback["feedbackId"],
            "case": {**self.case_input, "conditions": {"filters": [{"credentials": marker}]}},
            "enable": False,
        }

        status, response = self.call("POST", "regression-cases", payload)

        self.assertEqual(status, 400)
        self.assertNotIn(marker, json.dumps(response))
        _, cases = self.call("GET", "regression-cases")
        self.assertNotIn(marker, json.dumps(cases))

    def test_condition_map_allows_at_most_one_hundred_top_level_properties(self) -> None:
        for count, expected_status in ((100, 201), (101, 400)):
            conditions = {f"condition_{index}": index for index in range(count)}
            status, response = self.call(
                "POST",
                "regression-cases",
                {
                    "feedbackId": self.feedback["feedbackId"],
                    "case": {**self.case_input, "conditions": conditions},
                    "enable": False,
                },
            )
            self.assertEqual(status, expected_status, response)

    def test_post_case_requires_draft_and_does_not_persist_reviewed_case(self) -> None:
        for index, invalid_enable in enumerate((True, "false", 0, None)):
            marker = f"review-gate-case-{index}"
            status, response = self.call(
                "POST",
                "regression-cases",
                {
                    "feedbackId": self.feedback["feedbackId"],
                    "case": {**self.case_input, "question": marker},
                    "enable": invalid_enable,
                },
            )
            self.assertEqual(status, 400, response)
            self.assertNotIn(marker, json.dumps(response))
            _, cases = self.call("GET", "regression-cases")
            self.assertNotIn(marker, json.dumps(cases))

    def test_source_trace_must_match_feedback_core_diagnostic(self) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        unrelated = self.gateway.diagnostics.record_trace_events(
            auth=self.auth,
            project_id="runtime-test",
            source_session_id="unrelated-session",
            source_turn_id="unrelated-turn",
            events=[{"eventId": "start", "occurredAt": now, "type": "turn_started"}],
        )
        status, response = self.call(
            "POST",
            "regression-cases",
            {
                "feedbackId": self.feedback["feedbackId"],
                "sourceTraceId": unrelated["id"],
                "case": self.case_input,
            },
        )
        self.assertEqual((status, response.get("code")), (400, "TRACE_FEEDBACK_MISMATCH"))
        _, cases = self.call("GET", "regression-cases")
        self.assertFalse(any(item["feedbackId"] == self.feedback["feedbackId"] for item in cases["items"]))

        feedback = self.gateway.diagnostics.feedback_detail(
            self.feedback["feedbackId"],
            organization_id=self.scope[0],
            project_id=self.scope[1],
        )
        linked = self.gateway.diagnostics.record_trace_events(
            auth=self.auth,
            project_id="runtime-test",
            source_session_id="linked-session",
            source_turn_id="linked-turn",
            events=[{
                "eventId": "linked-core",
                "occurredAt": now,
                "type": "tool_completed",
                "toolUseId": "query-tool",
                "coreTraceId": feedback["traceId"],
            }],
        )
        status, created = self.call(
            "POST",
            "regression-cases",
            {
                "feedbackId": self.feedback["feedbackId"],
                "sourceTraceId": linked["id"],
                "case": self.case_input,
            },
        )
        self.assertEqual(status, 201, created)
        self.assertEqual(created["sourceTraceId"], linked["id"])

        foreign_scope = self.gateway.diagnostics.record_trace_events(
            auth=self.auth,
            project_id="runtime-other",
            source_session_id="foreign-session",
            source_turn_id="foreign-turn",
            events=[{"eventId": "foreign-start", "occurredAt": now, "type": "turn_started"}],
        )
        status, response = self.call(
            "POST",
            "regression-cases",
            {
                "feedbackId": self.feedback["feedbackId"],
                "sourceTraceId": foreign_scope["id"],
                "case": self.case_input,
            },
        )
        self.assertEqual((status, response.get("code")), (404, "TRACE_NOT_FOUND"))

    def test_expired_source_trace_does_not_block_editing_an_existing_case(self) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        feedback = self.gateway.diagnostics.feedback_detail(
            self.feedback["feedbackId"],
            organization_id=self.scope[0],
            project_id=self.scope[1],
        )
        linked = self.gateway.diagnostics.record_trace_events(
            auth=self.auth,
            project_id="runtime-test",
            source_session_id="expiring-session",
            source_turn_id="expiring-turn",
            events=[{
                "eventId": "expiring-core",
                "occurredAt": now,
                "type": "tool_completed",
                "toolUseId": "query-tool",
                "coreTraceId": feedback["traceId"],
            }],
        )
        status, draft = self.call(
            "POST",
            "regression-cases",
            {
                "feedbackId": self.feedback["feedbackId"],
                "sourceTraceId": linked["id"],
                "case": self.case_input,
            },
        )
        self.assertEqual(status, 201, draft)
        with self.gateway.access_control._connect() as connection:
            connection.execute(
                "UPDATE agent_traces SET expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00Z", linked["id"]),
            )

        status, updated = self.call(
            "PUT",
            "regression-cases/" + draft["id"],
            {"expectedRevision": draft["revision"], "case": self.case_input, "enable": True},
        )
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["sourceTraceId"], linked["id"])

    def test_expired_run_lease_still_blocks_a_second_process_until_core_returns(self) -> None:
        entered, release = threading.Event(), threading.Event()
        query_run_calls = 0
        query_run_lock = threading.Lock()
        original = self.dispatcher.dispatch

        def blocked_query(request):
            nonlocal query_run_calls
            if request["method"] == "query.run":
                with query_run_lock:
                    query_run_calls += 1
                entered.set()
                release.wait(12)
            return original(request)

        self.dispatcher.dispatch = blocked_query
        case = self.create_case()
        status, run = self.call("POST", "regression-runs", {"caseIds": [case["id"]]})
        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(3))
        with self.gateway.access_control._connect() as connection:
            connection.execute(
                "UPDATE regression_run_leases SET expires_at=? WHERE run_id=?",
                ("2000-01-01T00:00:00Z", run["id"]),
            )

        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        process = context.Process(
            target=_try_start_regression_in_other_process,
            args=(
                str(self.gateway.access_control.path),
                self.token,
                self.authorization,
                case["id"],
                output,
            ),
        )
        try:
            process.start()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(3)
                self.fail("the independent runner process did not return")
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(output.get(timeout=2), ("REGRESSION_BUSY", 409))
            self.assertEqual(query_run_calls, 1)

            configuration_output = context.Queue()
            configuration_process = context.Process(
                target=_try_acquire_configuration_guard_in_other_process,
                args=(
                    str(self.gateway.access_control.path),
                    self.token,
                    self.authorization,
                    case["id"],
                    configuration_output,
                ),
            )
            configuration_process.start()
            configuration_process.join(10)
            if configuration_process.is_alive():
                configuration_process.terminate()
                configuration_process.join(3)
                self.fail("the independent configuration process did not return")
            self.assertEqual(configuration_process.exitcode, 0)
            self.assertEqual(
                configuration_output.get(timeout=2),
                ("REGRESSION_ENVIRONMENT_BUSY", 409),
            )
            self.assertEqual(query_run_calls, 1)
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not self.app.regression_api.runner._project_mutex(self.scope).locked():
                    break
                time.sleep(0.01)

    def test_postgres_control_store_migrates_and_persists_regression_metadata(self) -> None:
        postgres = postgres_test_support.PostgreSQLAccessControlStoreTests()
        postgres.setUp()
        try:
            access = postgres.store()
            diagnostics = DiagnosticStore(access)
            regression = RegressionStore(diagnostics)
            saved = regression.save_dataset(
                ("org-audit", "project-audit"),
                {"id": "dataset-audit", "version": "v1", "datasourceId": "ds-audit"},
            )
            self.assertEqual(saved["id"], "dataset-audit")
            with closing(sqlite3.connect(postgres.database)) as connection:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM regression_schema_migrations ORDER BY version"
                    )
                ]
                stored = connection.execute(
                    "SELECT version,datasource_id FROM regression_test_datasets WHERE organization_id=? AND project_id=? AND id=?",
                    ("org-audit", "project-audit", "dataset-audit"),
                ).fetchone()
            self.assertEqual(versions, [1, 2])
            self.assertEqual(stored, ("v1", "ds-audit"))
            self.assertTrue(
                any(
                    "pg_advisory_xact_lock" in statement
                    and params == (8_341_972_315_443_004,)
                    for statement, params in postgres.statements
                )
            )
        finally:
            postgres.doCleanups()

    def test_postgres_project_lock_uses_a_dedicated_session_advisory_lock(self) -> None:
        calls: list[tuple[str, tuple]] = []
        connections: list[_AdvisoryLockConnection] = []

        def factory(_database_url: str):
            connection = _AdvisoryLockConnection(acquired=True, calls=calls)
            connections.append(connection)
            return connection

        access = SimpleNamespace(
            path=None,
            database_url="postgresql://control.example/semarail",
            _connection_factory=factory,
        )
        lock = _acquire_postgres_execution_lock(access, self.scope)
        lock.release()

        statements = [statement for statement, _ in calls]
        self.assertTrue(any("pg_try_advisory_lock" in statement for statement in statements))
        self.assertTrue(any("pg_advisory_unlock" in statement for statement in statements))
        self.assertEqual(statements.count("COMMIT"), 2)
        self.assertLess(statements.index("COMMIT"), next(
            index for index, statement in enumerate(statements) if "pg_advisory_unlock" in statement
        ))
        self.assertTrue(connections[0].closed)

        busy_calls: list[tuple[str, tuple]] = []
        busy_connections: list[_AdvisoryLockConnection] = []

        def busy_factory(_database_url: str):
            connection = _AdvisoryLockConnection(acquired=False, calls=busy_calls)
            busy_connections.append(connection)
            return connection

        with self.assertRaises(DiagnosticError) as caught:
            _acquire_postgres_execution_lock(
                SimpleNamespace(
                    path=None,
                    database_url="postgresql://control.example/semarail",
                    _connection_factory=busy_factory,
                ),
                self.scope,
            )
        self.assertEqual((caught.exception.code, caught.exception.status), ("REGRESSION_BUSY", 409))
        self.assertTrue(busy_connections[0].closed)

    def test_unknown_lock_adapter_fails_closed_without_process_local_fallback(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            _acquire_storage_execution_lock(SimpleNamespace(path=None), self.scope)
        self.assertEqual(
            (caught.exception.code, caught.exception.status),
            ("REGRESSION_LOCK_UNAVAILABLE", 503),
        )

    def test_lost_lease_interrupts_before_core_execution(self) -> None:
        case = self.create_case()
        run = self.store.create_run(self.auth, "runtime-test", [case])
        with self.gateway.access_control._connect() as connection:
            connection.execute(
                "DELETE FROM regression_run_leases WHERE organization_id=? AND project_id=?",
                self.scope,
            )

        state = {
            "cancel": threading.Event(),
            "queryId": None,
            "subjectId": self.auth.subject.id,
            "runId": run["id"],
            "leaseLost": False,
        }
        self.app.regression_api.runner._execute(
            self.scope, run, [case], self.authorization, state
        )

        self.assertTrue(state["leaseLost"])
        self.assertTrue(state["cancel"].is_set())
        self.assertEqual(self.store.run(self.scope, run["id"])["status"], "interrupted")
        self.assertEqual(self.dispatcher.requests, [])

    def test_regression_avoids_artifacts_and_normal_core_keeps_them(self) -> None:
        case = self.create_case()

        original_dispatch = self.dispatcher.dispatch

        def bounded_but_incomplete(request):
            response = original_dispatch(request)
            if request["method"] == "query.run":
                response = dict(response)
                response["result"] = {
                    **response["result"],
                    # More than Core's 50-row inline boundary. The incomplete
                    # result must not be treated as a complete assertion pass.
                    "previewRows": [{"value": str(index)} for index in range(51)],
                    "stats": {"returnedRows": 500, "durationMs": 1, "truncated": True},
                }
            return response

        self.dispatcher.dispatch = bounded_but_incomplete
        with patch.object(
            self.gateway.artifacts,
            "reserve",
            side_effect=AssertionError("regression must not reserve a persistent artifact"),
        ) as reserve:
            run_status, run = self.call("POST", "regression-runs", {"caseIds": [case["id"]]})
            self.assertEqual(run_status, 202)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                _, run = self.call("GET", "regression-runs/" + run["id"])
                if run["status"] in {"completed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.01)

        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["results"][0]["status"], "inconclusive", run)
        self.assertEqual(run["results"][0]["reasonCode"], "RESULT_INCOMPLETE")
        self.assertEqual(run["results"][0]["assertions"][1]["reasonCode"], "RESULT_INCOMPLETE")
        reserve.assert_not_called()
        regression_query = next(
            request for request in self.dispatcher.requests
            if request["method"] == "query.run"
        )
        self.assertNotIn("artifactRequest", regression_query["params"])
        serialized = json.dumps(run)
        for forbidden in ('"expected"', '"actual"', '"previewRows"', '"value": "50"'):
            self.assertNotIn(forbidden, serialized)

        # The public Core RPC path still injects an artifact capability for a
        # normal query; this change is limited to trusted regression transport.
        self.dispatcher.dispatch = original_dispatch
        case_input = self.case_input
        _, prepared = self.gateway.dispatch(
            {
                "protocolVersion": "3",
                "id": "ordinary-core-prepare",
                "method": "query.prepare",
                "params": {
                    "question": case_input["question"],
                    "semanticSql": case_input["semanticSql"],
                    "conditions": case_input["conditions"],
                    "confirmedConditions": case_input["confirmedConditions"],
                },
            },
            authorization=self.authorization,
        )
        with patch.object(self.gateway.artifacts, "reserve", wraps=self.gateway.artifacts.reserve) as reserve:
            status, response = self.gateway.dispatch(
                {
                    "protocolVersion": "3",
                    "id": "ordinary-core-run",
                    "method": "query.run",
                    "params": {
                        "question": case_input["question"],
                        "semanticSql": case_input["semanticSql"],
                        "queryId": "ordinary-core-artifact-audit",
                        "preparationId": prepared["result"]["preparationId"],
                        "chartIntent": "table",
                    },
                },
                authorization=self.authorization,
            )
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        reserve.assert_called_once()
        ordinary_query = self.dispatcher.requests[-1]
        self.assertEqual(ordinary_query["method"], "query.run")
        self.assertIn("artifactRequest", ordinary_query["params"])

    def test_missing_core_result_status_is_not_a_success(self) -> None:
        case = self.create_case({**self.case_input, "assertions": {"status": "success"}})
        original_dispatch = self.dispatcher.dispatch

        def malformed_success(request):
            response = original_dispatch(request)
            if request["method"] == "query.run":
                return {
                    "protocolVersion": "1",
                    "id": request["id"],
                    "ok": True,
                    "result": {},
                }
            return response

        self.dispatcher.dispatch = malformed_success
        run_status, run = self.call("POST", "regression-runs", {"caseIds": [case["id"]]})
        self.assertEqual(run_status, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, run = self.call("GET", "regression-runs/" + run["id"])
            if run["status"] in {"completed", "cancelled", "interrupted"}:
                break
            time.sleep(0.01)

        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["results"][0]["status"], "inconclusive", run)
        self.assertNotEqual(run["results"][0]["reasonCode"], "ASSERTIONS_PASSED")

    def test_cancel_before_core_registration_retries_and_preserves_current_outcome(self) -> None:
        entered, release, retried = threading.Event(), threading.Event(), threading.Event()
        cancel_results: list[bool] = []
        cancel_trace_ids: list[str] = []
        cancel_results_lock = threading.Lock()
        original_dispatch = self.dispatcher.dispatch

        def blocked_query(request):
            if request["method"] == "query.run":
                entered.set()
                release.wait(5)
            response = original_dispatch(request)
            if request["method"] == "query.cancel":
                # The race under audit: Core has not registered query.run yet.
                response["result"] = {"queryId": request["params"]["queryId"], "cancelled": False}
                with cancel_results_lock:
                    cancel_results.append(False)
                    cancel_trace_ids.append(request["traceId"])
                    if len(cancel_results) >= 2:
                        retried.set()
            return response

        self.dispatcher.dispatch = blocked_query
        first = self.create_case()
        second = self.create_case()
        status, run = self.call("POST", "regression-runs", {"caseIds": [first["id"], second["id"]]})
        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(3))

        try:
            remote_runner = regression_test_support.RegressionRunner(self.store, self.gateway)
            result = remote_runner.cancel(self.auth, "runtime-test", run["id"], self.authorization)
            self.assertEqual(result["status"], "running")
            self.assertTrue(self.store.cancel_requested(self.scope, run["id"]))

            self.assertTrue(retried.wait(3), "a cancelled=false response must remain retryable while query.run is in flight")
            with cancel_results_lock:
                self.assertGreaterEqual(len(cancel_results), 2)

            busy_status, busy = self.call("POST", "regression-runs", {"caseIds": [first["id"]]})
            self.assertEqual((busy_status, busy["code"]), (409, "REGRESSION_BUSY"))
        finally:
            release.set()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, final = self.call("GET", "regression-runs/" + run["id"])
            if final["status"] in {"completed", "cancelled", "interrupted"}:
                break
            time.sleep(0.01)
        self.assertEqual(final["status"], "cancelled", final)
        self.assertEqual(final["results"][0]["status"], "passed", final)
        self.assertEqual(final["results"][0]["reasonCode"], "ASSERTIONS_PASSED", final)
        self.assertEqual(final["results"][1]["reasonCode"], "CANCELLED", final)
        self.assertEqual(sum(request["method"] == "query.run" for request in self.dispatcher.requests), 1)
        self.assertGreaterEqual(len(cancel_trace_ids), 2)
        self.assertTrue(set(cancel_trace_ids).issubset(set(final["results"][0]["coreTraceIds"])), final)

    def test_core_cancelled_response_marks_only_the_current_case_cancelled(self) -> None:
        entered, release = threading.Event(), threading.Event()
        cancel_entered, release_cancel = threading.Event(), threading.Event()
        cancel_trace_ids: list[str] = []
        original_dispatch = self.dispatcher.dispatch

        def cancelled_query(request):
            if request["method"] == "query.run":
                entered.set()
                release.wait(5)
                return {
                    "protocolVersion": "1",
                    "id": request["id"],
                    "ok": False,
                    "error": {"code": "CANCELLED", "phase": "cancel", "message": "query was cancelled"},
                }
            response = original_dispatch(request)
            if request["method"] == "query.cancel":
                cancel_trace_ids.append(request["traceId"])
                cancel_entered.set()
                release_cancel.wait(5)
                response["result"] = {"queryId": request["params"]["queryId"], "cancelled": True}
            return response

        self.dispatcher.dispatch = cancelled_query
        case = self.create_case()
        status, run = self.call("POST", "regression-runs", {"caseIds": [case["id"]]})
        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(3))

        cancel_response = []
        cancel_thread = threading.Thread(
            target=lambda: cancel_response.append(
                self.call("POST", "regression-runs/" + run["id"] + "/cancel", {})
            ),
            daemon=True,
        )
        cancel_thread.start()
        try:
            self.assertTrue(cancel_entered.wait(3))
            release.set()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                _, interim = self.call("GET", "regression-runs/" + run["id"])
                if interim["results"] or interim["status"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(interim["status"], "running", interim)
            self.assertEqual(interim["results"], [], interim)
        finally:
            release.set()
            release_cancel.set()
        cancel_thread.join(3)
        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual(cancel_response[0][0], 202)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, final = self.call("GET", "regression-runs/" + run["id"])
            if final["status"] in {"completed", "cancelled", "interrupted"}:
                break
            time.sleep(0.01)
        self.assertEqual(final["status"], "cancelled", final)
        self.assertEqual(final["results"][0]["status"], "cancelled", final)
        self.assertEqual(final["results"][0]["reasonCode"], "CANCELLED", final)
        self.assertEqual(len(cancel_trace_ids), 1)
        self.assertTrue(set(cancel_trace_ids).issubset(set(final["results"][0]["coreTraceIds"])), final)

    def test_late_cancel_trace_is_persisted_after_bounded_settle_wait(self) -> None:
        entered, release = threading.Event(), threading.Event()
        cancel_entered, release_cancel = threading.Event(), threading.Event()
        cancel_trace_ids: list[str] = []
        original_dispatch = self.dispatcher.dispatch

        def delayed_cancel(request):
            if request["method"] == "query.run":
                entered.set()
                release.wait(5)
            response = original_dispatch(request)
            if request["method"] == "query.cancel":
                cancel_trace_ids.append(request["traceId"])
                cancel_entered.set()
                release_cancel.wait(5)
                response["result"] = {"queryId": request["params"]["queryId"], "cancelled": True}
            return response

        self.dispatcher.dispatch = delayed_cancel
        case = self.create_case()
        with patch("server.regression._CANCEL_RPC_JOIN_SECONDS", 0.05):
            status, run = self.call("POST", "regression-runs", {"caseIds": [case["id"]]})
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(3))

            cancel_response = []
            cancel_thread = threading.Thread(
                target=lambda: cancel_response.append(
                    self.call("POST", "regression-runs/" + run["id"] + "/cancel", {})
                ),
                daemon=True,
            )
            cancel_thread.start()
            try:
                self.assertTrue(cancel_entered.wait(3))
                release.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    _, interim = self.call("GET", "regression-runs/" + run["id"])
                    if interim["results"] and interim["status"] in {"completed", "cancelled", "interrupted"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(interim["status"], "cancelled", interim)
                self.assertEqual(interim["results"][0]["status"], "passed", interim)
                self.assertNotIn(cancel_trace_ids[0], interim["results"][0]["coreTraceIds"])
            finally:
                release.set()
                release_cancel.set()
            cancel_thread.join(3)
            self.assertFalse(cancel_thread.is_alive())
            self.assertEqual(cancel_response[0][0], 202)

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                _, final = self.call("GET", "regression-runs/" + run["id"])
                if cancel_trace_ids[0] in final["results"][0]["coreTraceIds"]:
                    break
                time.sleep(0.01)
            self.assertIn(cancel_trace_ids[0], final["results"][0]["coreTraceIds"], final)

    def test_late_trace_cas_preserves_terminal_status_and_other_case_results(self) -> None:
        first, second = self.create_case(), self.create_case()
        record = self.store.create_run(self.auth, "runtime-test", [first, second])
        record["status"] = "running"
        record["results"] = [
            {"caseId": first["id"], "status": "passed", "coreTraceIds": ["trace-existing"]},
            {"caseId": second["id"], "status": "blocked", "coreTraceIds": ["trace-other-case"]},
        ]
        self.store.update_run(self.scope, record)

        read_snapshot, allow_write = threading.Event(), threading.Event()
        original_json = regression_module._json
        worker_errors: list[BaseException] = []

        def pause_late_patch(value):
            if threading.current_thread().name == "late-patch":
                read_snapshot.set()
                if not allow_write.wait(5):
                    raise RuntimeError("test did not release the CAS writer")
            return original_json(value)

        def append_late_trace():
            try:
                self.store.append_core_trace_id(
                    self.scope, record["id"], first["id"], "trace-cancel-late",
                )
            except BaseException as exc:
                worker_errors.append(exc)

        with patch.object(regression_module, "_json", side_effect=pause_late_patch):
            worker = threading.Thread(target=append_late_trace, name="late-patch")
            worker.start()
            self.assertTrue(read_snapshot.wait(3), "late patch must reach its stale snapshot before terminal update")
            record["status"] = "cancelled"
            record["results"][1]["status"] = "passed"
            self.store.update_run(self.scope, record)
            allow_write.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_errors, [])
        final = self.store.run(self.scope, record["id"])
        self.assertEqual(final["status"], "cancelled", final)
        self.assertEqual(final["results"][0]["coreTraceIds"], ["trace-existing", "trace-cancel-late"], final)
        self.assertEqual(final["results"][1]["status"], "passed", final)
        self.assertEqual(final["results"][1]["coreTraceIds"], ["trace-other-case"], final)

    def test_recovery_cas_preserves_concurrent_terminal_run_and_trace_ids(self) -> None:
        first, second = self.create_case(), self.create_case()
        record = self.store.create_run(self.auth, "runtime-test", [first, second])
        record["status"] = "running"
        record["results"] = [
            {"caseId": first["id"], "status": "passed", "coreTraceIds": ["trace-real"]},
            {"caseId": second["id"], "status": "blocked", "coreTraceIds": ["trace-other"]},
        ]
        self.store.update_run(self.scope, record)
        expired_at = regression_module._timestamp(datetime.now(UTC) - timedelta(seconds=10))
        with self.store.access._connect() as connection:
            connection.execute(
                "UPDATE regression_run_leases SET expires_at=? "
                "WHERE organization_id=? AND project_id=? AND run_id=?",
                (expired_at, *self.scope, record["id"]),
            )

        read_snapshot, allow_recover = threading.Event(), threading.Event()
        original_json = regression_module._json
        worker_errors: list[BaseException] = []

        def pause_recovery(value):
            if threading.current_thread().name == "recover":
                read_snapshot.set()
                if not allow_recover.wait(5):
                    raise RuntimeError("test did not release the recovery CAS writer")
            return original_json(value)

        def recover():
            try:
                self.store.recover()
            except BaseException as exc:
                worker_errors.append(exc)

        with patch.object(regression_module, "_json", side_effect=pause_recovery):
            worker = threading.Thread(target=recover, name="recover")
            worker.start()
            self.assertTrue(read_snapshot.wait(3), "recovery must reach its stale record snapshot")
            record["status"] = "completed"
            record["results"][0]["coreTraceIds"].append("trace-final")
            record["results"][1]["status"] = "passed"
            self.store.update_run(self.scope, record)
            allow_recover.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_errors, [])
        final = self.store.run(self.scope, record["id"])
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(final["results"][0]["coreTraceIds"], ["trace-real", "trace-final"], final)
        self.assertEqual(final["results"][1]["status"], "passed", final)
        self.assertEqual(final["results"][1]["coreTraceIds"], ["trace-other"], final)

    def test_recovery_stale_expired_snapshot_does_not_interrupt_renewed_lease(self) -> None:
        case = self.create_case()
        record = self.store.create_run(self.auth, "runtime-test", [case])
        heartbeat_at = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
        recovery_at = heartbeat_at + timedelta(seconds=10)
        observed_expiry = regression_module._timestamp(heartbeat_at + timedelta(seconds=5))
        renewed_expiry = regression_module._timestamp(heartbeat_at + timedelta(seconds=180))
        with self.store.access._connect() as connection:
            connection.execute(
                "UPDATE regression_run_leases SET expires_at=? "
                "WHERE organization_id=? AND project_id=? AND run_id=?",
                (observed_expiry, *self.scope, record["id"]),
            )

        self.store.clock = lambda: (
            recovery_at if threading.current_thread().name == "recover" else heartbeat_at
        )
        snapshot_taken, allow_claim = threading.Event(), threading.Event()
        original_recovery = self.store._recover_expired_lease
        worker_errors: list[BaseException] = []

        def pause_before_claim(scope, run_id, now, expiry):
            if threading.current_thread().name == "recover":
                snapshot_taken.set()
                if not allow_claim.wait(5):
                    raise RuntimeError("test did not release the recovery claim")
            return original_recovery(scope, run_id, now, expiry)

        def recover():
            try:
                self.store.recover()
            except BaseException as exc:
                worker_errors.append(exc)

        with patch.object(self.store, "_recover_expired_lease", side_effect=pause_before_claim):
            worker = threading.Thread(target=recover, name="recover")
            worker.start()
            self.assertTrue(snapshot_taken.wait(3), "recovery must select the stale expired lease")
            self.assertTrue(self.store.heartbeat(record["id"]))
            allow_claim.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_errors, [])
        with self.store.access._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM regression_run_leases "
                "WHERE organization_id=? AND project_id=? AND run_id=?",
                (*self.scope, record["id"]),
            ).fetchone()
            run = connection.execute(
                "SELECT status FROM regression_query_runs "
                "WHERE id=? AND organization_id=? AND project_id=?",
                (record["id"], *self.scope),
            ).fetchone()
        self.assertEqual(row["expires_at"], renewed_expiry)
        self.assertEqual(run["status"], "queued")


if __name__ == "__main__":
    unittest.main()
