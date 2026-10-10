from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from server.app import create_app
from server.diagnostics import DiagnosticError
from server.regression import RegressionRunner, RegressionStore
from server.regression_assertions import evaluate, validate_case
from server.service import SemanticConsoleService
from server.tests import test_runtime_rpc as runtime_tests


class RegressionTests(unittest.TestCase):
    def setUp(self):
        runtime_tests.RuntimeRpcTests.setUp(self)
        self.auth = self.gateway.access_control.authenticate(self.authorization)
        console_policy = self.gateway.access_control.create_policy("Regression admin", {
            "schemaVersion": 1, "projects": ["runtime-test"], "tools": ["console:admin"],
        })
        self.gateway.access_control.bind_policy(self.auth.subject.id, console_policy["id"])
        self.console_policy_id = console_policy["id"]
        self.app = create_app(SemanticConsoleService(self.project), runtime_rpc=self.gateway)
        self.store = self.app.regression_api.store
        self.scope = (self.auth.subject.organization_id, "runtime-test")
        self.gateway.diagnostics.record_execution(auth=self.auth, project_id="runtime-test", trace_id="trace-source",
            query_id="query-source", datasource_id=self.datasource_id, transport="core-http", method="query.run", status="success", stage="complete")
        self.feedback = self.gateway.diagnostics.submit_feedback(auth=self.auth, project_id="runtime-test", reference="trace-source",
            idempotency_key="regression-source", category="evaluation", description="Manually reviewed test")
        self.store.save_dataset(self.scope, {"id": "fixture", "version": "v1", "datasourceId": self.datasource_id})
        self.case_input = {"schemaVersion": 2, "kind": "deterministic_sql", "question": "Return one value", "semanticSql": "SELECT value FROM orders ORDER BY value",
            "conditions": {}, "confirmedConditions": [], "testDatasetId": "fixture", "assertions": {"status": "success", "rowCount": 1, "cells": [{"row": 0, "column": "value", "expected": "1"}]}}

    def call(self, method, path, body=None, authorization=None):
        return self.app.request(method, "/api/v1/diagnostics/" + path, body, authorization=authorization or self.authorization)

    def create_case(self, **changes):
        case = copy.deepcopy(self.case_input)
        case.update(changes)
        status, draft = self.call("POST", "regression-cases", {"feedbackId": self.feedback["feedbackId"], "case": case})
        self.assertEqual(status, 201, draft)
        status, reviewed = self.call(
            "PUT", "regression-cases/" + draft["id"],
            {"expectedRevision": draft["revision"], "case": case, "enable": True},
        )
        self.assertEqual(status, 200, reviewed)
        return reviewed

    def run_case(self, record):
        status, run = self.call("POST", "regression-runs", {"caseIds": [record["id"]]})
        self.assertEqual(status, 202, run)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, result = self.call("GET", "regression-runs/" + run["id"])
            self.assertEqual(status, 200, result)
            if result["status"] in {"completed", "cancelled", "interrupted"}:
                return result
            time.sleep(0.01)
        self.fail("regression did not finish")

    def test_governed_execution_retains_evidence_not_values(self):
        case = self.create_case()
        run = self.run_case(case)
        self.assertEqual(run["status"], "completed", run)
        outcome = run["results"][0]
        self.assertEqual(outcome["status"], "passed", run)
        self.assertEqual(len(outcome["coreTraceIds"]), 2)
        self.assertEqual(outcome["caseRevision"], 2)
        raw = json.dumps(run)
        for forbidden in ("expected", "actual", "semanticSql", "question", "previewRows", "accessToken", "apiKey"):
            self.assertNotIn(forbidden, raw)
        reopened = RegressionStore(self.gateway.diagnostics)
        self.assertEqual(reopened.run(self.scope, run["id"]), run)
        self.assertEqual(self.dispatcher.requests[-1]["method"], "query.run")

    def test_dataset_mismatch_blocks_before_core(self):
        case = self.create_case()
        self.project.active_datasource_id = None
        run = self.run_case(case)
        self.assertEqual(run["results"][0]["reasonCode"], "DATASET_MISMATCH")
        self.assertEqual(run["results"][0]["coreTraceIds"], [])

    def test_dataset_revision_requires_case_review(self):
        case = self.create_case()
        self.store.save_dataset(self.scope, {"version": "v2", "datasourceId": self.datasource_id}, dataset_id="fixture")
        self.assertEqual(self.run_case(case)["results"][0]["status"], "blocked")
        status, updated = self.call("PUT", "regression-cases/" + case["id"], {"expectedRevision": 2, "case": case["case"], "enable": True})
        self.assertEqual((status, updated["revision"], updated["datasetVersion"]), (200, 3, "v2"))
        self.assertEqual(self.run_case(updated)["results"][0]["status"], "passed")
        status, conflict = self.call("PUT", "regression-cases/" + case["id"], {"expectedRevision": 2, "case": case["case"], "enable": True})
        self.assertEqual((status, conflict["code"]), (409, "CASE_REVISION_CONFLICT"))

    def test_expected_core_denial_is_not_impersonated(self):
        case = self.create_case(assertions={"status": "failure", "error": {"code": "POLICY_DENIED", "phase": "authorization"}})
        for policy in self.gateway.access_control.policies_for_subject(self.auth.subject.id):
            if policy["id"] != self.console_policy_id:
                self.gateway.access_control.unbind_policy(self.auth.subject.id, policy["id"])
        result = self.run_case(case)["results"][0]
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["coreTraceIds"])
        self.assertFalse(any(x["method"] == "query.run" for x in self.dispatcher.requests))

    def test_external_semantic_change_is_inconclusive(self):
        original = self.dispatcher.dispatch
        def mutate(request):
            response = original(request)
            if request["method"] == "query.run":
                self.project.project_dir.joinpath("external.yml").write_text("changed: true\n")
            return response
        self.dispatcher.dispatch = mutate
        outcome = self.run_case(self.create_case())["results"][0]
        self.assertEqual((outcome["status"], outcome["reasonCode"]), ("inconclusive", "ENVIRONMENT_CHANGED"))

    def test_clarifications_stop_execution(self):
        original = self.gateway.dispatch
        def prepare(request, *args, **kwargs):
            if request["method"] == "query.prepare":
                return 200, {"ok": True, "traceId": "trace-preparation", "result": {"status": "needs_clarification"}}
            return original(request, *args, **kwargs)
        with patch.object(self.gateway, "dispatch", side_effect=prepare):
            outcome = self.run_case(self.create_case())["results"][0]
        self.assertEqual((outcome["status"], outcome["reasonCode"]), ("blocked", "CLARIFICATION_REQUIRED"))
        self.assertEqual(self.dispatcher.requests, [])

    def test_batch_serialization_cancel_and_configuration_guard(self):
        entered, release = threading.Event(), threading.Event()
        original = self.dispatcher.dispatch
        def block(request):
            if request["method"] == "query.run":
                entered.set()
                release.wait(5)
            return original(request)
        self.dispatcher.dispatch = block
        case = self.create_case()
        status, run = self.call("POST", "regression-runs", {"caseIds": [case["id"]]})
        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(3))
        try:
            status, busy = self.call("POST", "regression-runs", {"caseIds": [case["id"]]})
            self.assertEqual((status, busy["code"]), (409, "REGRESSION_BUSY"))
            status, busy = self.app.request("POST", f"/api/datasources/{self.datasource_id}/activate", {}, authorization=self.authorization)
            self.assertEqual((status, busy["code"]), (409, "REGRESSION_ENVIRONMENT_BUSY"))
            status, _ = self.call("POST", "regression-runs/" + run["id"] + "/cancel", {})
            self.assertEqual(status, 202)
            self.assertTrue(any(x["method"] == "query.cancel" for x in self.dispatcher.requests))
        finally:
            release.set()
        for _ in range(100):
            result = self.store.run(self.scope, run["id"])
            if result["status"] == "cancelled":
                break
            time.sleep(0.01)
        self.assertEqual(result["status"], "cancelled")

    def test_foreign_scope_and_bootstrap_execution_are_denied(self):
        record = self.create_case()
        with self.assertRaises(DiagnosticError):
            self.store.case(("foreign-org", self.scope[1]), record["id"])
        with self.assertRaises(DiagnosticError):
            self.store.case((self.scope[0], "foreign-project"), record["id"])
        status, result = self.call("POST", "regression-runs", {"caseIds": [record["id"]]}, self.admin_authorization)
        self.assertEqual((status, result["code"]), (403, "FORBIDDEN"))

    def test_expired_lease_cannot_admit_another_runner_while_core_is_live(self):
        entered, release = threading.Event(), threading.Event()
        original = self.dispatcher.dispatch
        def block(request):
            if request["method"] == "query.run":
                entered.set()
                release.wait(5)
            return original(request)
        self.dispatcher.dispatch = block
        now = datetime.now(UTC)
        self.store.clock = lambda: now
        case = self.create_case()
        status, run = self.call("POST", "regression-runs", {"caseIds": [case["id"]]})
        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(3))
        try:
            now += timedelta(seconds=181)
            second = RegressionRunner(self.store, self.gateway)
            self.assertEqual(self.store.run(self.scope, run["id"])["status"], "interrupted")
            with self.assertRaises(DiagnosticError) as caught:
                second.start(self.auth, "runtime-test", self.authorization, [case["id"]])
            self.assertEqual(caught.exception.code, "REGRESSION_BUSY")
        finally:
            release.set()
        for _ in range(100):
            if not self.app.regression_api.runner._project_mutex(self.scope).locked():
                break
            time.sleep(0.01)
        self.assertFalse(self.app.regression_api.runner._project_mutex(self.scope).locked())

    def test_recovery_and_thirty_day_cleanup_preserve_case_inputs(self):
        case = self.create_case()
        now = datetime(2026, 10, 10, tzinfo=UTC)
        self.store.clock = lambda: now
        run = self.store.create_run(self.auth, "runtime-test", [case])
        now += timedelta(seconds=181)
        reopened = RegressionStore(self.gateway.diagnostics)
        reopened.clock = lambda: now
        reopened.recover()
        self.assertEqual(reopened.run(self.scope, run["id"])["status"], "interrupted")
        now += timedelta(days=30)
        reopened.recover()
        with self.assertRaises(DiagnosticError):
            reopened.run(self.scope, run["id"])
        self.assertEqual(reopened.case(self.scope, case["id"])["case"]["question"], self.case_input["question"])

    def test_legacy_cases_require_upgrade_and_agent_evidence_stays_exportable(self):
        legacy = self.gateway.diagnostics.create_regression_case(auth=self.auth, project_id="runtime-test", feedback_id=self.feedback["feedbackId"], case={"kind": "agent_evidence", "question": "Legacy"})
        status, result = self.call("POST", "regression-runs", {"caseIds": [legacy["id"]]})
        self.assertEqual((status, result["code"]), (400, "CASE_NOT_EXECUTABLE"))
        self.assertEqual(self.call("GET", "regression-cases/export")[0], 200)
        status, upgraded = self.call("PUT", "regression-cases/" + legacy["id"], {"expectedRevision": 0, "case": self.case_input, "enable": True})
        self.assertEqual((status, upgraded["revision"]), (200, 1))

    def test_credentials_and_unknown_fields_are_rejected(self):
        for changes in ({"role": "administrator"}, {"policyTestConfig": {}}, {"question": "sr_live_topsecretcredential"}, {"schemaVersion": 7}):
            case = {**self.case_input, **changes}
            status, _ = self.call("POST", "regression-cases", {"feedbackId": self.feedback["feedbackId"], "case": case, "enable": False})
            self.assertEqual(status, 400)

    def test_regression_initialization_failure_does_not_change_core(self):
        with patch("server.regression_api.RegressionStore", side_effect=RuntimeError("private detail")):
            app = create_app(SemanticConsoleService(self.project), runtime_rpc=self.gateway)
        status, response = app.request("POST", "/api/v1/runtime/rpc", {"protocolVersion": "3", "id": "health", "method": "health", "params": {}}, authorization=self.authorization)
        self.assertEqual((status, response["ok"]), (200, True))
        status, response = app.request("GET", "/api/v1/diagnostics/regression-runs", authorization=self.authorization)
        self.assertEqual(status, 503)
        self.assertNotIn("private detail", json.dumps(response))

    def test_revocation_between_prepare_and_run_uses_current_policy(self):
        case = self.create_case(assertions={"status": "failure", "error": {"code": "POLICY_DENIED", "phase": "authorization"}})
        original = self.gateway.dispatch
        def revoke(request, *args, **kwargs):
            response = original(request, *args, **kwargs)
            if request["method"] == "query.prepare":
                for policy in self.gateway.access_control.policies_for_subject(self.auth.subject.id):
                    if policy["id"] != self.console_policy_id:
                        self.gateway.access_control.unbind_policy(self.auth.subject.id, policy["id"])
            return response
        with patch.object(self.gateway, "dispatch", side_effect=revoke):
            outcome = self.run_case(case)["results"][0]
        self.assertEqual(outcome["status"], "passed")
        self.assertEqual(len(outcome["coreTraceIds"]), 2)
        self.assertFalse(any(x["method"] == "query.run" for x in self.dispatcher.requests))


class AssertionTests(unittest.TestCase):
    def test_sql_row_windows_cannot_prove_order_from_the_returned_subset(self):
        assertions = {"status": "success", "rowCount": 1,
                      "cells": [{"row": 0, "column": "value", "expected": "wanted"}]}
        response = {"ok": True, "result": {"status": "success", "delivery": "inline",
            "columns": [{"name": "value"}, {"name": "sort_key"}],
            "previewRows": [{"value": "wanted", "sort_key": 1}],
            "stats": {"returnedRows": 1, "truncated": False}}}
        queries = (
            "SELECT value, sort_key FROM t ORDER BY sort_key LIMIT 1",
            "SELECT value, sort_key FROM t ORDER BY sort_key OFFSET 1",
            "SELECT value, sort_key FROM t ORDER BY sort_key FETCH FIRST 1 ROW ONLY",
            "SELECT TOP 1 value, sort_key FROM t ORDER BY sort_key",
            "SELECT value, sort_key FROM (SELECT value, sort_key FROM t LIMIT 1) s ORDER BY sort_key",
            "WITH s AS (SELECT value, sort_key FROM t LIMIT 1) SELECT value, sort_key FROM s ORDER BY sort_key",
        )
        for sql in queries:
            with self.subTest(sql=sql):
                state, checks = evaluate(assertions, response, semantic_sql=sql)
                self.assertEqual((state, checks[-1]["reasonCode"]), ("inconclusive", "ORDERING_UNPROVEN"))
                self.assertEqual(checks[1]["status"], "passed")  # Result count is still observed.
        self.assertEqual(evaluate(assertions, response,
            semantic_sql="SELECT value, sort_key FROM t ORDER BY sort_key")[0], "passed")

    def test_order_column_qualification_must_match_the_projected_source(self):
        assertions = {"status": "success", "cells": [{"row": 0, "column": "value", "expected": "wanted"}]}
        response = {"ok": True, "result": {"status": "success", "columns": [{"name": "sortkey"}, {"name": "value"}],
            "previewRows": [{"sortkey": 2, "value": "wanted"}, {"sortkey": 1, "value": "other"}],
            "stats": {"returnedRows": 2, "truncated": False}}}
        sql = "SELECT a.sortkey AS sortkey, a.value AS value FROM a JOIN b ON a.id=b.id ORDER BY b.sortkey"
        state, checks = evaluate(assertions, response, semantic_sql=sql)
        self.assertEqual((state, checks[-1]["reasonCode"]), ("inconclusive", "ORDERING_UNPROVEN"))
        self.assertEqual(evaluate(assertions, response, semantic_sql=sql.replace("ORDER BY b.sortkey", "ORDER BY a.sortkey DESC"))[0], "passed")
        self.assertEqual(evaluate(assertions, response, semantic_sql=sql.replace("ORDER BY b.sortkey", "ORDER BY 1 DESC"))[0], "passed")
        response["result"]["columns"].append({"name": "value"})
        self.assertEqual(evaluate(assertions, response, semantic_sql=sql.replace("ORDER BY b.sortkey", "ORDER BY 1 DESC"))[0], "inconclusive")

    def test_tied_order_keys_are_inconclusive_and_numeric_strings_use_tolerance(self):
        assertions = {"status": "success", "cells": [{"row": 0, "column": "n", "expected": 1, "tolerance": 0.01}]}
        response = {"ok": True, "result": {"status": "success", "delivery": "inline", "columns": [{"name": "n", "type": "BIGINT"}], "previewRows": [{"n": "1"}], "stats": {"returnedRows": 1, "truncated": False}}}
        self.assertEqual(evaluate(assertions, response, semantic_sql="SELECT n FROM orders ORDER BY n")[0], "passed")
        response["result"]["previewRows"].append({"n": "1"})
        response["result"]["stats"]["returnedRows"] = 2
        outcome, checks = evaluate(assertions, response, semantic_sql="SELECT n FROM orders ORDER BY n")
        self.assertEqual(outcome, "inconclusive")
        self.assertEqual(checks[-1]["reasonCode"], "ORDERING_UNPROVEN")
    def test_readonly_and_real_top_level_order(self):
        base = {"schemaVersion": 2, "kind": "deterministic_sql", "question": "manual", "testDatasetId": "fixture", "assertions": {"status": "success", "cells": [{"row": 0, "column": "n", "expected": 1}]}}
        for sql in ("DELETE FROM orders", "SELECT * INTO target FROM orders", "SELECT * FROM (SELECT * FROM orders ORDER BY n) a", "SELECT 1; SELECT 2", "SELECT 'ORDER BY' AS n"):
            with self.assertRaises(DiagnosticError):
                validate_case({**base, "semanticSql": sql}, enabled=True)
        validate_case({**base, "semanticSql": "SELECT n FROM orders ORDER BY n"}, enabled=True)

    def test_truncation_missing_rows_and_tolerance_never_expose_values(self):
        assertions = {"status": "success", "rowCount": 1, "cells": [{"row": 0, "column": "n", "expected": 1.1, "tolerance": 0.2}]}
        response = {"ok": True, "result": {"status": "success", "delivery": "inline", "columns": [{"name": "n"}], "previewRows": [{"n": 1}], "stats": {"returnedRows": 1, "truncated": False}}}
        outcome, checks = evaluate(assertions, response)
        self.assertEqual(outcome, "passed")
        self.assertNotIn("1.1", json.dumps(checks))
        response["result"]["stats"]["truncated"] = True
        self.assertEqual(evaluate(assertions, response)[0], "inconclusive")
        response["result"]["stats"]["truncated"] = False
        response["result"]["previewRows"] = []
        response["result"]["delivery"] = "artifact"
        self.assertEqual(evaluate(assertions, response)[0], "inconclusive")
