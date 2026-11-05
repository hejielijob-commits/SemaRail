from __future__ import annotations

import copy
import concurrent.futures
import threading
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from server.app import create_app, SemanticConsoleHTTPServer
from server.evaluation_scoring import compare_results, score
from server.evaluations import validate
from server.diagnostics import DiagnosticError
from server.service import SemanticConsoleService
from server.project import ProjectStore
from server.runtime_rpc import RuntimeRpcGateway
from server.tests import test_runtime_rpc as runtime_tests


class ScoringTests(unittest.TestCase):
    def result(self, rows, **extra):
        return {"columns": ["n"], "rows": rows, "complete": True, "truncated": False, **extra}

    def test_duplicate_multiplicity_and_tolerance_matching(self):
        self.assertEqual(compare_results(self.result([[1], [1]]), self.result([[1], [2]])), "failed")
        self.assertEqual(compare_results(self.result([[1], [0]]), self.result([[0], [2]]), tolerance=1), "passed")

    def test_empty_result_comparisons(self):
        self.assertEqual(compare_results(self.result([]), self.result([])), "passed")
        self.assertEqual(compare_results(self.result([]), self.result([[1]])), "failed")
        self.assertEqual(compare_results(self.result([[1]]), self.result([])), "failed")

    def test_incomplete_order_and_malformed_rows_are_not_passed(self):
        expected = self.result([[1]])
        for actual in (self.result([[1]], truncated=True), self.result([[1]], complete=False), self.result([[1, 2]]), self.result([[float("nan")]])):
            self.assertEqual(compare_results(expected, actual), "inconclusive")
        self.assertEqual(compare_results(expected, expected, ordered=True), "inconclusive")

    def test_first_result_does_not_get_replaced_by_repair(self):
        value = score({"expectedBehavior": "answer", "expectedResult": self.result([[2]])}, {"firstResult": self.result([[1]]), "finalResult": self.result([[2]])}, "completed")
        self.assertEqual(value["status"], "passed")
        self.assertEqual(value["dimensions"]["firstSql"], "failed")

    def test_clarification_also_requires_correct_result(self):
        value = score({"expectedBehavior": "clarify", "clarificationReplies": {"month": "Sep"}, "expectedResult": self.result([[2]])}, {"finalResult": self.result([[1]]), "clarification": {"confirmedIds": ["month"]}}, "completed")
        self.assertEqual(value["status"], "failed")

    def test_answer_without_required_conditions_does_not_fail_clarification_dimension(self):
        result = self.result([[1]])
        value = score({"expectedBehavior": "answer", "expectedResult": result}, {"finalResult": result, "clarification": {"confirmedIds": []}}, "completed")
        self.assertEqual(value["status"], "passed")
        self.assertEqual(value["dimensions"]["clarification"], "inconclusive")

    def test_later_denial_cannot_hide_prior_disclosure(self):
        value = score({"expectedBehavior": "deny"}, {"firstResult": self.result([[1]]), "denial": {"code": "POLICY_DENIED"}}, "completed")
        self.assertEqual(value["status"], "failed")

    def test_reference_preview_cannot_establish_oracle(self):
        expected = self.result([[1]], truncated=True)
        value = score({"expectedBehavior": "answer", "referenceSql": "SELECT 1"}, {"expectedResult": expected, "finalResult": self.result([[1]])}, "completed")
        self.assertEqual(value["status"], "inconclusive")

    def test_business_conflict_and_observed_first_error_remain_failures(self):
        result = self.result([[1]])
        value = score({"expectedBehavior": "answer", "expectedResult": result}, {"finalResult": result, "firstError": {"code": "SEMANTIC_ERROR"}, "businessConditions": [{"status": "conflict"}]}, "completed")
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["dimensions"]["firstSql"], "failed")
        self.assertEqual(value["dimensions"]["businessConditions"], "failed")

    def test_final_core_error_cannot_become_inconclusive_or_passed(self):
        value = score({"expectedBehavior": "answer", "expectedResult": self.result([[1]])}, {"finalResult": self.result([[1]]), "finalError": {"code": "SEMANTIC_ERROR"}}, "completed")
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["dimensions"]["result"], "failed")

    def test_unsupported_requires_observed_core_error_not_boolean_flag(self):
        case = {"expectedBehavior": "unsupported"}
        self.assertEqual(score(case, {"unsupported": True}, "completed")["status"], "inconclusive")
        self.assertEqual(score(case, {"unsupported": {"code": "invented"}}, "completed")["status"], "inconclusive")
        self.assertEqual(score(case, {"unsupported": {"code": "UNSUPPORTED_QUERY"}}, "completed")["status"], "passed")
        self.assertEqual(score(case, {"unsupported": {"code": "SEMANTIC_ERROR", "reasonCode": "UNSUPPORTED_SQL_SHAPE"}}, "completed")["status"], "passed")
        self.assertEqual(score(case, {"unsupported": {"code": "UNSUPPORTED_QUERY"}, "finalResult": self.result([[1]])}, "completed")["status"], "inconclusive")


class EvaluationApiTests(unittest.TestCase):
    def setUp(self):
        runtime_tests.RuntimeRpcTests.setUp(self)
        self.auth = self.gateway.access_control.authenticate(self.authorization)
        policy = self.gateway.access_control.create_policy("Evaluation test", {"schemaVersion": 1, "projects": ["runtime-test"], "tools": ["console:admin", "eval:worker"]})
        self.gateway.access_control.bind_policy(self.auth.subject.id, policy["id"])
        self.policy_id = policy["id"]
        self.app = create_app(SemanticConsoleService(self.project), runtime_rpc=self.gateway)
        self.scope = (self.auth.subject.organization_id, "runtime-test")
        self.app.regression_api.store.save_dataset(self.scope, {"id": "test", "version": "fixture-v1", "datasourceId": self.project.active_datasource_identifier()})

    def tearDown(self):
        for guard in self.app.evaluation_api._guards.values():
            guard.release()
        runtime_tests.RuntimeRpcTests.tearDown(self)

    def call(self, method, path, body=None, expected=200):
        status, value = self.app.request(method, "/api/v1/evaluations/" + path, body, authorization=self.authorization)
        self.assertEqual(status, expected, value)
        return value

    def prepare(self):
        self.call("PUT", "settings", {"enabled": True, "testDataOnly": True})
        suite = self.call("POST", "suites", {"name": "People", "testDatasetId": "test", "cases": [{"id": "q1", "question": "How many?", "role": "employee", "expectedBehavior": "answer", "expectedResult": {"columns": ["n"], "rows": [[2]]}}]}, 201)
        profile = self.call("POST", "profiles", {"name": "Agent", "model": "explicit-test-model", "prompt": "Answer using MCP"}, 201)
        self.call("POST", "suites/" + suite["id"] + "/publish", {})
        self.call("POST", "profiles/" + profile["id"] + "/publish", {})
        self.call("POST", "workers/register", {"id": "test-worker", "version": "1", "cliVersion": "test", "roles": ["employee"], "capabilities": {"datasetFingerprints": {"test": "0" * 64}, "roleSubjects": {"employee": self.auth.subject.id}}})
        return suite, profile

    def restart_application(self):
        """Rebuild every gateway/store instance against the retained database."""
        old_app = self.app
        old_store = old_app.evaluation_api.store
        old_access = self.gateway.access_control
        database_path = old_access.path
        # A terminated process releases its OS lock. Keep durable run/lease rows
        # unchanged so the next application must reconstruct safety from DB state.
        for guard in old_app.evaluation_api._guards.values():
            guard.release()
        old_app.evaluation_api._guards.clear()
        self.project = ProjectStore(self.project.project_dir, state_dir=self.project.state_dir, validator=runtime_tests.FakeValidator())
        self.gateway = RuntimeRpcGateway(self.project, runtime_tests.RecordingDispatcher(), auth_token=self.token)
        self.app = create_app(SemanticConsoleService(self.project), runtime_rpc=self.gateway)
        self.assertIsNot(self.gateway.access_control, old_access)
        self.assertIsNot(self.app.evaluation_api.store, old_store)
        self.assertEqual(self.gateway.access_control.path, database_path)
        self.assertEqual(self.app.evaluation_api._guards, {})

    def test_restart_preserves_queue_and_fences_abandoned_started_attempt(self):
        suite, profile = self.prepare()
        exported = self.call("GET", "suites/" + suite["id"] + "/export")["suite"]
        extra = {**exported["cases"][0], "id": "q2"}
        self.call("PUT", "suites/" + suite["id"], {**exported, "cases": exported["cases"] + [extra]})
        self.call("POST", "suites/" + suite["id"] + "/publish", {})
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        abandoned = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        auth = {"workerId": "test-worker", "leaseToken": abandoned["leaseToken"]}
        self.call("POST", "attempts/" + abandoned["attemptId"] + "/heartbeat", {**auth, "started": True, "datasetFingerprint": "0" * 64})
        old_store = self.app.evaluation_api.store
        with old_store.transaction() as c:
            attempt = old_store.get(c, self.scope, "attempts", abandoned["attemptId"])
            attempt["expiresAt"] = "2000-01-01T00:00:00Z"
            attempt["executionDeadline"] = old_store.expires(300)
            old_store.put(c, self.scope, "attempts", attempt)
        self.restart_application()
        recovered = self.call("GET", "runs/" + run["id"])
        interrupted = next(a for a in recovered["attempts"] if a["id"] == abandoned["attemptId"])
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(recovered["status"], "running")
        self.assertGreater(interrupted["guardUntil"], self.app.evaluation_api.store.now())
        status, _ = self.app.request("POST", "/api/project/publish", {}, authorization=self.authorization)
        self.assertEqual(status, 409)
        remaining = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        self.assertIsNotNone(remaining)
        self.assertNotEqual(remaining["attemptId"], abandoned["attemptId"])
        self.assertIsNone(self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"])
        self.call("POST", "attempts/" + remaining["attemptId"] + "/complete", {"workerId": "test-worker", "leaseToken": remaining["leaseToken"], "status": "completed", "evidence": {"finalResult": {"columns": ["n"], "rows": [[2]], "complete": True, "truncated": False}}})
        self.assertEqual(self.call("GET", "runs/" + run["id"])["status"], "running")
        self.call("POST", "attempts/" + abandoned["attemptId"] + "/complete", {**auth, "status": "completed", "evidence": {}}, 409)
        store = self.app.evaluation_api.store
        with store.transaction() as c:
            attempt = store.get(c, self.scope, "attempts", abandoned["attemptId"])
            attempt["guardUntil"] = "2000-01-01T00:00:00Z"
            store.put(c, self.scope, "attempts", attempt)
        terminal = self.call("GET", "runs/" + run["id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["summary"]["total"], 2)
        self.assertEqual(terminal["summary"]["errors"], 1)
        guard = self.app.regression_api.acquire_configuration_guard(self.auth, "runtime-test")
        guard.release()

    def test_restart_retains_scores_evidence_review_history_and_idempotent_completion(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        credentials = {"workerId": "test-worker", "leaseToken": job["leaseToken"]}
        payload = {**credentials, "status": "completed", "evidence": {"finalAnswer": "Two employees.", "finalResult": {"columns": ["n"], "rows": [[2]], "complete": True, "truncated": False}}}
        before = self.call("POST", "attempts/" + job["attemptId"] + "/complete", payload)
        self.call("POST", "attempts/" + job["attemptId"] + "/review", {"status": "failed", "reason": "Missing snapshot date"})
        self.call("POST", "attempts/" + job["attemptId"] + "/review", {"status": "passed", "reason": "Date confirmed in linked context"})
        before_evidence = self.call("GET", "attempts/" + job["attemptId"] + "/evidence")
        self.restart_application()
        after = self.call("GET", "runs/" + run["id"])
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["summary"]["passRate"], 1)
        self.assertEqual(after["attempts"][0]["score"], before["score"])
        self.assertEqual([r["status"] for r in after["attempts"][0]["review"]["history"]], ["failed", "passed"])
        self.assertEqual(self.call("GET", "attempts/" + job["attemptId"] + "/evidence"), before_evidence)
        retried = self.call("POST", "attempts/" + job["attemptId"] + "/complete", payload)
        self.assertEqual(retried["score"], before["score"])
        self.assertEqual(len(retried["review"]["history"]), 2)
        self.call("POST", "attempts/" + job["attemptId"] + "/complete", {**payload, "status": "timeout"}, 409)

    def test_published_versions_are_immutable_and_export_reimports_draft(self):
        suite, _ = self.prepare()
        exported = self.call("GET", "suites/" + suite["id"] + "/export")
        imported = self.call("POST", "suites/import", exported, 201)
        self.assertEqual(imported["status"], "draft")
        edited = self.call("PUT", "suites/" + suite["id"], {**exported["suite"], "name": "Changed"})
        self.assertEqual(edited["revision"], 2)
        store = self.app.evaluation_api.store
        with store.transaction() as c:
            self.assertEqual(store.get(c, self.scope, "suites_versions", suite["id"] + ":1")["name"], "People")

    def test_run_completion_idempotency_review_and_config_lock(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        self.assertEqual(run["summary"]["total"], 1)
        blocked, _ = self.app.request("POST", "/api/project/publish", {}, authorization=self.authorization)
        self.assertEqual(blocked, 409)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        auth = {"workerId": "test-worker", "leaseToken": job["leaseToken"]}
        self.call("POST", "attempts/" + job["attemptId"] + "/heartbeat", {**auth, "started": True, "datasetFingerprint": "0" * 64})
        complete = {**auth, "status": "completed", "evidence": {"finalAnswer": "Two", "firstResult": {"columns": ["n"], "rows": [[2]], "complete": True, "truncated": False}, "finalResult": {"columns": ["n"], "rows": [[2]], "complete": True, "truncated": False}}}
        result = self.call("POST", "attempts/" + job["attemptId"] + "/complete", complete)
        self.assertEqual(result["score"]["status"], "passed")
        self.call("POST", "attempts/" + job["attemptId"] + "/complete", complete)
        self.call("POST", "attempts/" + job["attemptId"] + "/complete", {**complete, "status": "timeout"}, 409)
        self.call("POST", "attempts/" + job["attemptId"] + "/review", {"status": "failed", "reason": "Missing units"})
        final = self.call("GET", "runs/" + run["id"])
        self.assertEqual(final["summary"]["passRate"], 1)
        self.assertEqual(final["attempts"][0]["review"]["status"], "failed")
        self.assertNotIn("leaseToken", str(final))
        self.call("POST", "runs/" + run["id"] + "/baseline", {})
        self.call("DELETE", "attempts/" + job["attemptId"] + "/evidence")
        self.call("GET", "attempts/" + job["attemptId"] + "/evidence", expected=410)

    def test_cancelled_unstarted_records_remain_in_denominator(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        stopped = self.call("POST", "runs/" + run["id"] + "/cancel", {}, 202)
        self.assertEqual(stopped["summary"]["total"], 1)
        self.assertEqual(stopped["summary"]["cancelled"], 1)
        self.assertEqual(stopped["summary"]["passRate"], 0)

    def test_lease_expiry_never_reissues_started_attempt(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        store = self.app.evaluation_api.store
        with store.transaction() as c:
            attempt = store.get(c, self.scope, "attempts", job["attemptId"])
            attempt["expiresAt"] = "2000-01-01T00:00:00Z"
            store.put(c, self.scope, "attempts", attempt)
        self.assertIsNone(self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"])
        self.assertEqual(self.call("GET", "runs/" + run["id"])["attempts"][0]["status"], "interrupted")

    def test_worker_cannot_use_other_lease_and_role_policy_revocation_is_immediate(self):
        suite, profile = self.prepare()
        self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        self.call("POST", "attempts/" + job["attemptId"] + "/heartbeat", {"workerId": "different-worker", "leaseToken": job["leaseToken"]}, 403)
        self.gateway.access_control.unbind_policy(self.auth.subject.id, self.policy_id)
        self.call("POST", "attempts/" + job["attemptId"] + "/heartbeat", {"workerId": "test-worker", "leaseToken": job["leaseToken"]}, 403)

    def test_fingerprint_drift_stops_queued_dispatch(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        self.assertIsNone(self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "1" * 64}})["job"])
        self.assertEqual(self.call("GET", "runs/" + run["id"])["attempts"][0]["status"], "interrupted")

    def test_trace_ownership_and_events_persistence(self):
        suite, profile = self.prepare()
        self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        credentials = {"workerId": "test-worker", "leaseToken": job["leaseToken"]}
        self.call("POST", "attempts/" + job["attemptId"] + "/events", {**credentials, "events": [{"type": "tool", "authorization": "must-not-be-retained", "reasoning": "hidden"}]})
        self.call("POST", "attempts/" + job["attemptId"] + "/complete", {**credentials, "status": "completed", "evidence": {"coreTraceIds": ["trace-not-owned"]}}, 403)
        self.call("POST", "attempts/" + job["attemptId"] + "/complete", {**credentials, "status": "completed", "evidence": {"finalAnswer": "No data"}})
        evidence = self.call("GET", "attempts/" + job["attemptId"] + "/evidence")["evidence"]
        self.assertEqual(evidence["events"], [{"type": "tool"}])
        self.assertNotIn("must-not-be-retained", str(evidence))

    def test_preflight_and_exploratory_compare_after_data_version_change(self):
        suite, profile = self.prepare()
        preview = self.call("GET", "preflight?suiteId=" + suite["id"] + "&profileId=" + profile["id"])
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["total"], 1)
        first = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        self.call("POST", "runs/" + first["id"] + "/cancel", {}, 202)
        second = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        self.call("POST", "runs/" + second["id"] + "/cancel", {}, 202)
        store = self.app.evaluation_api.store
        with store.transaction() as c:
            other = store.get(c, self.scope, "runs", second["id"])
            other["environment"]["datasetFingerprint"] = "1" * 64
            store.put(c, self.scope, "runs", other)
        comparison = self.call("GET", "compare?baselineId=" + first["id"] + "&candidateId=" + second["id"])
        self.assertFalse(comparison["strict"])
        self.assertEqual(comparison["items"][0]["category"], "unmatched")

    def test_profile_export_import_is_versioned_and_starts_as_draft(self):
        _, profile = self.prepare()
        exported = self.call("GET", "profiles/" + profile["id"] + "/export")
        imported = self.call("POST", "profiles/import", exported, 201)
        self.assertEqual(imported["status"], "draft")
        self.assertEqual(imported["model"], profile["model"])
        self.assertNotEqual(imported["id"], profile["id"])
        self.call("POST", "profiles/import", {**exported, "schemaVersion": 2}, 400)

    def test_rerun_uses_frozen_suite_and_profile_even_after_head_deletion(self):
        suite, profile = self.prepare()
        first = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        self.call("POST", "runs/" + first["id"] + "/cancel", {}, 202)
        self.call("DELETE", "suites/" + suite["id"])
        self.call("DELETE", "profiles/" + profile["id"])
        self.app.regression_api.store.save_dataset(self.scope, {"version": "fixture-v2", "datasourceId": self.project.active_datasource_identifier()}, dataset_id="test")
        rerun = self.call("POST", "runs/" + first["id"] + "/rerun", {"caseIds": ["q1"]}, 202)
        self.assertEqual(rerun["sourceRunId"], first["id"])
        self.assertEqual(rerun["agentConfig"], first["agentConfig"])
        self.assertEqual(rerun["cases"], first["cases"])
        self.assertEqual(rerun["suiteRevision"], first["suiteRevision"])
        self.assertEqual(rerun["environment"]["datasetVersion"], "fixture-v2")

    def test_heartbeat_rechecks_role_binding_and_completion_cannot_pass_after_drift(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        credentials = {"workerId": "test-worker", "leaseToken": job["leaseToken"]}
        # Preserve worker/admin scopes, but revoke the role's separate query policy.
        self.gateway.access_control.unbind_policy(self.auth.subject.id, self.runtime_policy["id"])
        heartbeat = self.call("POST", "attempts/" + job["attemptId"] + "/heartbeat", credentials)
        self.assertTrue(heartbeat["cancelRequested"])
        result = self.call("POST", "attempts/" + job["attemptId"] + "/complete", {**credentials, "status": "completed", "evidence": {"finalResult": {"columns": ["n"], "rows": [[2]], "complete": True, "truncated": False}}})
        self.assertEqual(result["score"]["status"], "inconclusive")
        self.assertEqual(result["environmentStatus"], "identity_drift")

    def test_heartbeats_cannot_extend_hard_deadline(self):
        suite, profile = self.prepare()
        self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        store = self.app.evaluation_api.store
        with store.transaction() as c:
            attempt = store.get(c, self.scope, "attempts", job["attemptId"])
            deadline = store.expires(30)
            attempt["executionDeadline"] = deadline
            store.put(c, self.scope, "attempts", attempt)
        heartbeat = self.call("POST", "attempts/" + job["attemptId"] + "/heartbeat", {"workerId": "test-worker", "leaseToken": job["leaseToken"]})
        self.assertEqual(heartbeat["expiresAt"], deadline)

    def test_expired_evidence_is_deleted_while_long_term_scores_remain(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        self.call("POST", "attempts/" + job["attemptId"] + "/complete", {"workerId": "test-worker", "leaseToken": job["leaseToken"], "status": "completed", "evidence": {"finalAnswer": "Two", "finalResult": {"columns": ["n"], "rows": [[2]], "complete": True, "truncated": False}}})
        store = self.app.evaluation_api.store
        with store.transaction() as c:
            c.execute("UPDATE evaluation_evidence SET expires_at=? WHERE attempt_id=?", ("2000-01-01T00:00:00Z", job["attemptId"]))
        self.call("GET", "attempts/" + job["attemptId"] + "/evidence", expected=410)
        self.assertEqual(self.call("GET", "runs/" + run["id"])["summary"]["passed"], 1)
        with store.transaction() as c:
            self.assertIsNone(c.execute("SELECT body_json FROM evaluation_evidence WHERE attempt_id=?", (job["attemptId"],)).fetchone())

    def test_other_project_cannot_read_assets_runs_or_evidence_by_known_id(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        policy = self.gateway.access_control.create_policy("Other project admin", {"schemaVersion": 1, "projects": ["other-project"], "tools": ["console:admin", "eval:worker"]})
        self.gateway.access_control.bind_policy(self.auth.subject.id, policy["id"])
        with patch.object(self.project, "overview", return_value={"name": "other-project"}):
            for path in ("suites/" + suite["id"], "profiles/" + profile["id"], "runs/" + run["id"], "attempts/" + run["attempts"][0]["id"] + "/evidence"):
                self.call("GET", path, expected=404)

    def test_subject_attribute_drift_is_observed_without_policy_version_change(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        with self.gateway.access_control._connect() as c:
            c.execute("UPDATE subjects SET attributes_json=? WHERE id=?", ('{"regionCodes":["DIFFERENT"]}', self.auth.subject.id))
        response = self.call("POST", "attempts/" + job["attemptId"] + "/heartbeat", {"workerId": "test-worker", "leaseToken": job["leaseToken"]})
        self.assertTrue(response["cancelRequested"])

    def test_different_case_selection_is_exploratory(self):
        suite, profile = self.prepare()
        payload = self.call("GET", "suites/" + suite["id"] + "/export")["suite"]
        payload["cases"].append({**payload["cases"][0], "id": "q2"})
        self.call("PUT", "suites/" + suite["id"], payload)
        self.call("POST", "suites/" + suite["id"] + "/publish", {})
        first = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"], "caseIds": ["q1"]}, 202)
        self.call("POST", "runs/" + first["id"] + "/cancel", {}, 202)
        second = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"], "caseIds": ["q2"]}, 202)
        self.call("POST", "runs/" + second["id"] + "/cancel", {}, 202)
        comparison = self.call("GET", "compare?baselineId=" + first["id"] + "&candidateId=" + second["id"])
        self.assertFalse(comparison["strict"])
        self.assertEqual({row["category"] for row in comparison["items"]}, {"unmatched"})

    def test_strict_auto_comparison_does_not_follow_manual_review(self):
        suite, profile = self.prepare()
        runs = []
        for actual, manual in ((1, "passed"), (2, "failed")):
            run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
            job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
            self.call("POST", "attempts/" + job["attemptId"] + "/complete", {"workerId": "test-worker", "leaseToken": job["leaseToken"], "status": "completed", "evidence": {"finalResult": {"columns": ["n"], "rows": [[actual]], "complete": True, "truncated": False}}})
            self.call("POST", "attempts/" + job["attemptId"] + "/review", {"status": manual, "reason": "Independent text review"})
            runs.append(run["id"])
        comparison = self.call("GET", "compare?baselineId=" + runs[0] + "&candidateId=" + runs[1])
        self.assertTrue(comparison["strict"])
        self.assertEqual(comparison["items"][0]["category"], "fixed")
        self.assertEqual(comparison["items"][0]["baselineReview"]["status"], "passed")
        self.assertEqual(comparison["items"][0]["candidateReview"]["status"], "failed")

    def test_publish_rejects_unscorable_answers_but_drafts_and_zero_count_are_valid(self):
        self.prepare()
        payload = {"name": "Incomplete", "testDatasetId": "test", "cases": [{"id": "incomplete", "question": "A question", "role": "employee", "expectedBehavior": "answer"}]}
        draft = self.call("POST", "suites", payload, 201)
        self.call("POST", "suites/" + draft["id"] + "/publish", {}, 400)
        payload["cases"][0]["assertions"] = {"rowCount": 0}
        self.call("PUT", "suites/" + draft["id"], payload)
        self.call("POST", "suites/" + draft["id"] + "/publish", {})
        payload["cases"][0]["expectedBehavior"] = "clarify"
        clarification = self.call("POST", "suites", payload, 201)
        self.call("POST", "suites/" + clarification["id"] + "/publish", {}, 400)
        payload["cases"][0]["clarificationReplies"] = {"month": "2026-09"}
        self.call("PUT", "suites/" + clarification["id"], payload)
        self.call("POST", "suites/" + clarification["id"] + "/publish", {})
        self.call("POST", "suites/import", {"schemaVersion": True, "suite": payload}, 400)

    def test_idle_server_maintenance_deletes_expired_evidence_without_api_access(self):
        suite, profile = self.prepare()
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        self.call("POST", "attempts/" + job["attemptId"] + "/complete", {"workerId": "test-worker", "leaseToken": job["leaseToken"], "status": "completed", "evidence": {"finalAnswer": "Two", "finalResult": {"columns": ["n"], "rows": [[2]], "complete": True, "truncated": False}}})
        store = self.app.evaluation_api.store
        with store.transaction() as c:
            self.assertIsNotNone(c.execute("SELECT body_json FROM evaluation_evidence WHERE attempt_id=?", (job["attemptId"],)).fetchone())
        future = datetime.now(UTC) + timedelta(days=31)
        server = SemanticConsoleHTTPServer(("127.0.0.1", 0), self.app)
        try:
            with patch.object(store, "clock", return_value=future), patch("server.app.time.monotonic", return_value=1000):
                server.service_actions()
            # Inspect directly, with no evaluation API read triggering recover().
            with store.transaction() as c:
                self.assertIsNone(c.execute("SELECT body_json FROM evaluation_evidence WHERE attempt_id=?", (job["attemptId"],)).fetchone())
                attempt = store.get(c, self.scope, "attempts", job["attemptId"])
                self.assertEqual(attempt["score"]["status"], "passed")
        finally:
            server.server_close()

    def test_configuration_guard_recovers_stale_evaluation_without_report_poll(self):
        suite, profile = self.prepare()
        self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        store = self.app.evaluation_api.store
        with store.transaction() as c:
            attempt = store.get(c, self.scope, "attempts", job["attemptId"])
            attempt.update(expiresAt="2000-01-01T00:00:00Z", executionDeadline="2000-01-01T00:00:00Z")
            store.put(c, self.scope, "attempts", attempt)
        self.assertIn(self.scope, self.app.evaluation_api._guards)
        guard = self.app.regression_api.acquire_configuration_guard(self.auth, "runtime-test")
        guard.release()
        self.assertNotIn(self.scope, self.app.evaluation_api._guards)

    def test_reference_sql_requires_independent_managed_scorer(self):
        suite, profile = self.prepare()
        payload = self.call("GET", "suites/" + suite["id"] + "/export")["suite"]
        payload["cases"][0]["referenceSql"] = "SELECT 2 AS n"
        self.call("PUT", "suites/" + suite["id"], payload)
        self.call("POST", "suites/" + suite["id"] + "/publish", {})
        self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 409)
        registration = {"id": "test-worker", "version": "1", "cliVersion": "test", "roles": ["employee"], "capabilities": {"datasetFingerprints": {"test": "0" * 64}, "roleSubjects": {"employee": self.auth.subject.id}, "scorerSubject": self.auth.subject.id}}
        self.call("POST", "workers/register", registration, 400)
        scorer = self.gateway.access_control.create_service_account("Independent scorer")
        self.gateway.access_control.bind_policy(scorer.id, self.runtime_policy["id"])
        registration["capabilities"]["scorerSubject"] = scorer.id
        self.call("POST", "workers/register", registration)
        preview = self.call("GET", "preflight?suiteId=" + suite["id"] + "&profileId=" + profile["id"])
        self.assertFalse(preview["ready"])
        self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 409)
        self.call("PUT", "datasets/test/scorer", {"subjectId": scorer.id})
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        self.assertEqual(run["environment"]["scorerSubject"], scorer.id)
        self.assertTrue(run["environment"]["scorerPolicyVersions"])
        self.assertEqual(run["environment"]["datasetScorerBinding"]["subjectId"], scorer.id)
        self.call("PUT", "datasets/test/scorer", {"subjectId": scorer.id}, 409)

    def test_dataset_scorer_binding_is_versioned_and_requires_matching_worker(self):
        suite, profile = self.prepare()
        payload = self.call("GET", "suites/" + suite["id"] + "/export")["suite"]
        payload["cases"][0]["referenceSql"] = "SELECT 2 AS n"
        self.call("PUT", "suites/" + suite["id"], payload)
        self.call("POST", "suites/" + suite["id"] + "/publish", {})
        first = self.gateway.access_control.create_service_account("Registered dataset scorer")
        other = self.gateway.access_control.create_service_account("Mismatched worker scorer")
        for scorer in (first, other):
            self.gateway.access_control.bind_policy(scorer.id, self.runtime_policy["id"])
        with patch.object(type(self.project), "published_revision", new=property(lambda _: None)):
            registered = self.call("PUT", "datasets/test/scorer", {"subjectId": first.id})
        self.assertEqual(registered["scorerBinding"]["datasetVersion"], "fixture-v1")
        self.assertEqual(self.call("PUT", "datasets/test/scorer", {"subjectId": first.id})["scorerBinding"]["revision"], 1)
        self.call("POST", "workers/register", {"id": "test-worker", "version": "1", "cliVersion": "test", "roles": ["employee"], "capabilities": {"datasetFingerprints": {"test": "0" * 64}, "roleSubjects": {"employee": self.auth.subject.id}, "scorerSubject": other.id}})
        self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 409)
        self.app.regression_api.store.save_dataset(self.scope, {"version": "fixture-v2", "datasourceId": self.project.active_datasource_identifier()}, dataset_id="test")
        self.assertFalse(self.call("GET", "datasets")["items"][0]["scorerBindingValid"])
        rebound = self.call("PUT", "datasets/test/scorer", {"subjectId": other.id})
        self.assertEqual(rebound["scorerBinding"]["revision"], 2)
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        job = self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"]
        # Out-of-band persistence drift is detected even if no UI mutation occurred.
        store = self.app.evaluation_api.store
        with store.transaction() as c:
            binding = store.get(c, self.scope, "dataset_scorers", "test")
            store.put(c, self.scope, "dataset_scorers", {**binding, "subjectId": first.id, "revision": 3})
        heartbeat = self.call("POST", "attempts/" + job["attemptId"] + "/heartbeat", {"workerId": "test-worker", "leaseToken": job["leaseToken"]})
        self.assertTrue(heartbeat["cancelRequested"])

    def test_dataset_scorer_rejects_agent_worker_bootstrap_or_unknown_identity(self):
        self.prepare()
        self.call("PUT", "datasets/test/scorer", {"subjectId": self.auth.subject.id}, 403)
        self.call("PUT", "datasets/test/scorer", {"subjectId": "not-a-managed-subject"}, 404)
        self.call("PUT", "datasets/missing/scorer", {"subjectId": self.auth.subject.id}, 404)

    def test_dataset_scorer_rejects_privileged_or_wildcard_permissions(self):
        self.prepare()
        for tool in ("console:admin", "access:admin", "eval:worker", "*"):
            scorer = self.gateway.access_control.create_service_account("Unsafe scorer " + tool)
            policy = self.gateway.access_control.create_policy("Unsafe", {"schemaVersion": 1, "projects": ["runtime-test"], "tools": ["query:execute", tool]})
            self.gateway.access_control.bind_policy(scorer.id, policy["id"])
            self.call("PUT", "datasets/test/scorer", {"subjectId": scorer.id}, 403)

    def test_rounds_and_concurrency_bounds(self):
        profile = {"name": "Bounds", "model": "explicit-model", "prompt": "Answer using MCP"}
        for key, values in (("rounds", (1, 3)), ("concurrency", (1, 4))):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertEqual(validate("profiles", {**profile, key: value})[key], value)
        for key, values in (("rounds", (0, 4, True, 1.5, "2")), ("concurrency", (0, 5, False, 2.5, "2"))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(DiagnosticError):
                    validate("profiles", {**profile, key: value})

    def test_parallel_claims_across_independent_stores_obey_concurrency_for_all_rounds(self):
        suite, profile = self.prepare()
        suite_payload = self.call("GET", "suites/" + suite["id"] + "/export")["suite"]
        suite_payload["cases"].append({**suite_payload["cases"][0], "id": "q2"})
        self.call("PUT", "suites/" + suite["id"], suite_payload)
        self.call("POST", "suites/" + suite["id"] + "/publish", {})
        profile_payload = self.call("GET", "profiles/" + profile["id"] + "/export")["profile"]
        self.call("PUT", "profiles/" + profile["id"], {**profile_payload, "rounds": 3, "concurrency": 2})
        self.call("POST", "profiles/" + profile["id"] + "/publish", {})
        run = self.call("POST", "runs", {"suiteId": suite["id"], "profileId": profile["id"]}, 202)
        self.assertEqual(run["summary"]["total"], 6)
        independent_project = ProjectStore(self.project.project_dir, state_dir=self.project.state_dir, validator=runtime_tests.FakeValidator())
        independent_gateway = RuntimeRpcGateway(independent_project, runtime_tests.RecordingDispatcher(), auth_token=self.token)
        independent_app = create_app(SemanticConsoleService(independent_project), runtime_rpc=independent_gateway)
        self.assertIsNot(independent_gateway.access_control, self.gateway.access_control)
        self.assertEqual(independent_gateway.access_control.path, self.gateway.access_control.path)
        self.assertIsNot(independent_app.evaluation_api.store, self.app.evaluation_api.store)
        observed_ids = set()
        completed = 0
        for batch in range(3):
            barrier = threading.Barrier(2)
            def simultaneous_claim(app):
                barrier.wait(timeout=10)
                return app.request("POST", "/api/v1/evaluations/workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}}, authorization=self.authorization)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(simultaneous_claim, app) for app in (self.app, independent_app)]
                claimed = [future.result(timeout=20) for future in futures]
            for status, response in claimed:
                self.assertEqual(status, 200, response)
                self.assertIsNotNone(response["job"])
            jobs = [response["job"] for _, response in claimed]
            self.assertEqual(len({job["attemptId"] for job in jobs}), 2)
            for job in jobs:
                self.assertNotIn(job["attemptId"], observed_ids)
                observed_ids.add(job["attemptId"])
            # Two independent transactions have filled the shared run's capacity.
            self.assertIsNone(self.call("POST", "workers/test-worker/claim", {"datasetFingerprints": {"test": "0" * 64}})["job"])
            active = self.call("GET", "runs/" + run["id"])
            self.assertEqual(sum(a["status"] in {"claimed", "running"} for a in active["attempts"]), 2)
            for index, job in enumerate(jobs):
                credentials = {"workerId": "test-worker", "leaseToken": job["leaseToken"]}
                self.call("POST", "attempts/" + job["attemptId"] + "/heartbeat", {**credentials, "started": True, "datasetFingerprint": "0" * 64})
                payload = {**credentials, "status": "completed", "evidence": {"finalResult": {"columns": ["n"], "rows": [[2]], "complete": True, "truncated": False}}}
                self.call("POST", "attempts/" + job["attemptId"] + "/complete", payload)
                completed += 1
                if index == 0:
                    before_retry = self.call("GET", "runs/" + run["id"])
                    self.call("POST", "attempts/" + job["attemptId"] + "/complete", payload)
                    after_retry = self.call("GET", "runs/" + run["id"])
                    self.assertEqual(after_retry["summary"], before_retry["summary"])
                    self.assertEqual(after_retry["attempts"], before_retry["attempts"])
                    self.assertEqual(after_retry["summary"]["passed"], completed)
        final = self.call("GET", "runs/" + run["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(len(observed_ids), 6)
        self.assertEqual(final["summary"]["total"], 6)
        self.assertEqual(final["summary"]["started"], 6)
        self.assertEqual(final["summary"]["passed"], 6)
        self.assertEqual(final["summary"]["passRate"], 1)
        self.assertEqual({(a["caseId"], a["round"]) for a in final["attempts"]}, {(case, round) for case in ("q1", "q2") for round in (1, 2, 3)})


if __name__ == "__main__":
    unittest.main()
