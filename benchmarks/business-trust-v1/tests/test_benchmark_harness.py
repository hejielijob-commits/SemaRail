from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import evaluation
import experiment
import legacy_compare
import runner
import app_dispatch
import transport_smoke
import worker_client
from review import WorkerCallAudit, audit_trace, audit_wrapper


class CoordinatorTerminalStateTests(unittest.TestCase):
    def make_host(self, directory: Path, existing_status: str):
        host = object.__new__(experiment.Host)
        host.directory = directory
        (directory / "app-dispatch-start.json").touch()
        question = experiment.read(experiment.CORPUS_PATH)["questions"][0]
        host.questions = {question["id"]: question}
        host.guard = threading.RLock()
        host.schedule = {job: {"id": job, "questionId": question["id"], "group": "baseline", "round": 1}
                         for job in ("job-001", "job-002", "job-003")}
        host.dispatch_nonces = {job: "test-nonce" for job in host.schedule}
        host.group_tools = {"baseline": {question["actor"]: []}}
        host.records = {"job-001": {**host.schedule["job-001"], "schemaVersion": 1,
                                   "status": existing_status, "endedAt": "original-terminal-time",
                                   "events": [], "final": {"answer": "original final"}}}
        host.locks = {job: threading.RLock() for job in host.schedule}
        host.started = {"job-001": 0.0}
        host.capture("job-001")
        return host

    def test_new_begin_never_rewrites_old_terminal_records_or_hashes(self):
        for status in ("complete", "invalid", "timeout", "infrastructure_error", "cancelled"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                host = self.make_host(root, status)
                original = copy.deepcopy(host.records["job-001"])
                original_bytes = (root / "records/job-001/0000.json").read_bytes()
                with patch("experiment.time.monotonic", return_value=1000.0):
                    host.begin("job-002", "test-nonce", "00000000-0000-4000-8000-000000000002")
                self.assertEqual(host.records["job-001"], original)
                self.assertEqual((root / "records/job-001/0000.json").read_bytes(), original_bytes)
                self.assertEqual(len(list((root / "records/job-001").glob("*.json"))), 1)
                self.assertEqual(host.records["job-002"]["status"], "running")

    def test_running_deadline_transition_is_captured_exactly_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host = self.make_host(root, "running")
            with patch("experiment.time.monotonic", return_value=400.0):
                host.begin("job-002", "test-nonce", "00000000-0000-4000-8000-000000000002")
            self.assertEqual(host.records["job-001"]["status"], "timeout")
            timeout_bytes = (root / "records/job-001/0001.json").read_bytes()
            with patch("experiment.time.monotonic", return_value=500.0):
                host.begin("job-003", "test-nonce", "00000000-0000-4000-8000-000000000003")
            self.assertEqual((root / "records/job-001/0001.json").read_bytes(), timeout_bytes)
            self.assertEqual(len(list((root / "records/job-001").glob("*.json"))), 2)

    def test_broker_service_actions_persist_last_worker_timeout_without_followup_begin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host = self.make_host(root, "running")
            host.locks = {job: threading.RLock() for job in host.schedule}
            host.manifest = {"ports": {"broker": 48785}}
            host.start = lambda: None
            host.close = lambda: None
            servers = []
            lock_ready = threading.Event()
            release_lock = threading.Event()

            def hold_job_lock():
                with host.locks["job-001"]:
                    lock_ready.set()
                    release_lock.wait(timeout=1)

            class InProcessHTTPServer:
                def __init__(self, _address, _handler):
                    self.loop_cycles = 0
                    servers.append(self)

                def service_actions(self):
                    pass

                def serve_forever(self, poll_interval=0.5):
                    holder = threading.Thread(target=hold_job_lock)
                    holder.start()
                    self.assert_lock_ready = lock_ready.wait(timeout=1)
                    self.loop_cycles += 1
                    self.service_actions()
                    self.status_while_locked = host.records["job-001"]["status"]
                    release_lock.set()
                    holder.join(timeout=1)
                    self.loop_cycles += 1
                    self.service_actions()
                    self.loop_cycles += 1
                    self.service_actions()

                def server_close(self):
                    pass

            original_bytes = (root / "records/job-001/0000.json").read_bytes()
            with patch.object(experiment, "Host", return_value=host), \
                 patch.object(experiment, "ThreadingHTTPServer", InProcessHTTPServer), \
                 patch("experiment.time.monotonic", return_value=experiment.DEADLINE_SECONDS):
                experiment.serve(root)

            self.assertTrue(servers[0].assert_lock_ready)
            self.assertEqual(servers[0].status_while_locked, "running")
            self.assertEqual(servers[0].loop_cycles, 3)
            self.assertEqual(host.records["job-001"]["status"], "timeout")
            timeout_path = root / "records/job-001/0001.json"
            timeout_bytes = timeout_path.read_bytes()
            self.assertEqual(json.loads(timeout_bytes)["status"], "timeout")
            self.assertEqual(original_bytes, (root / "records/job-001/0000.json").read_bytes())
            self.assertEqual(len(list((root / "records/job-001").glob("*.json"))), 2)
            self.assertEqual(timeout_bytes, timeout_path.read_bytes())

    def test_mcp_timeout_is_job_timeout_only_after_global_deadline(self):
        def make_tool_host(root: Path):
            host = self.make_host(root, "running")
            question = host.questions[host.records["job-001"]["questionId"]]
            host.group_tools = {"baseline": {question["actor"]: [
                {"name": "semarail_get_context"}]}}
            host.mcp_ports = {"baseline": 9122}
            host.keys = {"baseline": {question["actor"]: "test-key"}}
            return host, question

        async def mcp_timeout(*_args):
            raise TimeoutError("MCP operation timed out")

        body = {"name": "semarail_get_context", "arguments": {
            "question": "the assigned question", "context_version": 2}}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host, question = make_tool_host(root)
            body["arguments"]["question"] = question["public"]["question"]
            clock = iter((299.0, 299.0, 300.0))
            with patch.object(experiment, "call_mcp", mcp_timeout), \
                 patch.object(experiment, "time", types.SimpleNamespace(monotonic=lambda: next(clock))):
                with self.assertRaisesRegex(TimeoutError, "job deadline reached"):
                    host.command("job-001", "tool", body)
            self.assertEqual(host.records["job-001"]["status"], "timeout")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host, question = make_tool_host(root)
            body["arguments"]["question"] = question["public"]["question"]
            clock = iter((100.0, 100.0, 100.0))
            with patch.object(experiment, "call_mcp", mcp_timeout), \
                 patch.object(experiment, "time", types.SimpleNamespace(monotonic=lambda: next(clock))):
                with self.assertRaisesRegex(RuntimeError, "MCP request failed"):
                    host.command("job-001", "tool", body)
            self.assertEqual(host.records["job-001"]["status"], "infrastructure_error")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host, question = make_tool_host(root)
            host.records["job-001"].pop("final")
            body["arguments"]["question"] = question["public"]["question"]
            clock = iter((299.0, 299.0, 300.0))

            async def late_success(*_args):
                return {"result": {"schemaVersion": 2,
                    "indexStatus": {"status": "ready", "backend": "hybrid"}},
                    "wire": {"ok": True}}

            with patch.object(experiment, "call_mcp", late_success), \
                 patch.object(experiment, "time", types.SimpleNamespace(monotonic=lambda: next(clock))):
                with self.assertRaisesRegex(TimeoutError, "job deadline reached"):
                    host.command("job-001", "tool", body)
            self.assertEqual(host.records["job-001"]["status"], "timeout")
            self.assertEqual(host.records["job-001"]["events"], [])


class WorkerReadyBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.revision = "sha256:" + "a" * 64

    def _projection(self, live_status: dict[str, object], *, context_revision: str | None = None,
                    context_schema_version: object = 2):
        host = object.__new__(experiment.Host)
        host.core_ports = {"candidate": 9120}
        host.mcp_ports = {"candidate": 9121}
        host.admin_tokens = {"candidate": "probe-admin-token"}
        host.keys = {"candidate": {"employee": "probe-employee-key"}}
        host.questions = {item["id"]: item for item in experiment.read(
            experiment.CORPUS_PATH)["questions"]}
        datasource_id = "datasource-test-id"
        host.resource_ids = {"candidate": {"_datasource": {"id": datasource_id}}}
        for actor in host._expected_actor_attributes():
            host.resource_ids["candidate"][actor] = {
                "account": f"account-{actor}",
                "policy": f"policy-{actor}",
                "credential": f"credential-{actor}",
            }
        host.manifest = {"data": {"databaseUrlEnv": "BUSINESS_TRUST_TEST_DATABASE_URL"}}

        datasource = {
            "id": datasource_id,
            "name": "Business trust benchmark",
            "type": "postgres",
            "hasPassword": True,
            "connection": {"host": "127.0.0.1", "port": 55433,
                           "database": "business_trust_test", "user": "wren"},
        }
        project = {
            "name": "Business trust benchmark",
            "schemaVersion": 1,
            "catalog": "hr",
            "schema": "public",
            "dataSource": "business-trust",
            "modelCount": 5,
            "revision": self.revision,
            "projectExists": True,
            "activeDatasource": {"id": datasource_id},
        }
        accounts = []
        policies = []
        for actor, attributes in host._expected_actor_attributes().items():
            account_id = f"account-{actor}"
            policy_id = f"policy-{actor}"
            accounts.append({
                "id": account_id,
                "name": f"BTV {actor}",
                "type": "service_account",
                "status": "active",
                "attributes": attributes,
                "policyIds": [policy_id],
                "credentials": [{"id": f"credential-{actor}", "subjectId": account_id,
                                 "revokedAt": None, "expiresAt": None}],
            })
            policies.append({
                "id": policy_id,
                "name": f"BTV {actor}",
                "document": experiment.role_policy(actor, datasource_id),
                "version": 1,
            })

        admin_paths: list[str] = []
        http_paths: list[str] = []
        mcp_calls: list[tuple[int, str, str, dict[str, object]]] = []

        def fake_http_value(url, *, token, method="GET", body=None):
            http_paths.append(url.rsplit("/", 1)[-1])
            if url.endswith("/api/datasources"):
                return [datasource]
            if url.endswith("/api/versions"):
                return []
            raise AssertionError("unexpected Core HTTP path")

        def fake_admin(_port, _token, path, *, method="GET", body=None):
            admin_paths.append(path)
            if path == "/api/project":
                return project
            if path == "/api/v1/access/service-accounts":
                return {"items": accounts}
            if path == "/api/v1/access/policies":
                return {"items": policies}
            if path == "/api/project/semantic-index":
                # Model the observed stale Core cache. This route must not be
                # used as the source of truth for the remote MCP index.
                return {"status": "missing", "backend": "lexical",
                        "staleReason": "revision_not_built"}
            raise AssertionError("unexpected Core admin path")

        async def fake_call_mcp(port, token, tool, arguments):
            mcp_calls.append((port, token, tool, arguments))
            return {"result": {
                "schemaVersion": context_schema_version,
                "projectRevision": context_revision or self.revision,
                "indexStatus": live_status,
            }}

        patches = (
            patch.dict(os.environ, {"BUSINESS_TRUST_TEST_DATABASE_URL":
                                    "postgresql://wren:test@127.0.0.1:55433/business_trust_test"}),
            patch.object(experiment, "_http_value", fake_http_value),
            patch.object(experiment, "_admin", fake_admin),
            patch.object(experiment, "call_mcp", fake_call_mcp),
        )
        for patched in patches:
            patched.start()
        self.addCleanup(lambda: [patched.stop() for patched in reversed(patches)])
        projection = host._binding_projection("candidate")
        return projection, admin_paths, http_paths, mcp_calls

    def test_uses_live_authenticated_mcp_status_even_when_core_cache_is_cold(self) -> None:
        live = {
            "status": "ready",
            "backend": "hybrid",
            "activeRevision": self.revision,
            "indexedRevision": self.revision,
            "documentCount": 12,
            "embeddingModelId": "frozen-model",
            "embeddingModelVersion": "frozen-revision",
            "embeddingDimension": 384,
            "indexBuildVersion": 1,
        }
        projection, admin_paths, _http_paths, mcp_calls = self._projection(live)

        self.assertEqual(projection["semanticIndex"]["status"], "ready")
        self.assertEqual(projection["semanticIndex"]["backend"], "hybrid")
        self.assertTrue(projection["semanticIndex"]["vectorAvailable"])
        self.assertNotIn("/api/project/semantic-index", admin_paths)
        self.assertEqual(len(mcp_calls), 1)
        port, token, tool, arguments = mcp_calls[0]
        self.assertEqual((port, token, tool),
                         (9121, "probe-employee-key", "semarail_get_context"))
        self.assertEqual(arguments, {
            "question": experiment.read(experiment.CORPUS_PATH)["questions"][0]["public"]["question"],
            "context_version": 2,
        })

    def test_worker_ready_rejects_unready_backend_or_mismatched_live_revision(self) -> None:
        base = {
            "status": "ready",
            "backend": "hybrid",
            "activeRevision": self.revision,
            "indexedRevision": self.revision,
        }
        invalid = (
            {**base, "status": "missing"},
            {**base, "status": "stale"},
            {**base, "backend": "lexical"},
            {**base, "staleReason": "revisionMismatch"},
            {**base, "activeRevision": "sha256:" + "b" * 64},
            {**base, "indexedRevision": "sha256:" + "b" * 64},
        )
        for live in invalid:
            with self.subTest(live=live), self.assertRaisesRegex(
                ValueError, "worker-ready semantic publication is not indexed"
            ):
                self._projection(live)

    def test_worker_ready_rejects_context_from_a_different_project_revision(self) -> None:
        live = {
            "status": "ready",
            "backend": "hybrid",
            "activeRevision": self.revision,
            "indexedRevision": self.revision,
        }
        with self.assertRaisesRegex(
            ValueError, "worker-ready authenticated MCP semantic status is unavailable"
        ):
            self._projection(live, context_revision="sha256:" + "b" * 64)

    def test_worker_ready_rejects_unknown_or_boolean_context_schema_versions(self) -> None:
        live = {
            "status": "ready",
            "backend": "hybrid",
            "activeRevision": self.revision,
            "indexedRevision": self.revision,
        }
        for version in (None, True, 1, 3):
            with self.subTest(version=version), self.assertRaisesRegex(
                ValueError, "worker-ready authenticated MCP semantic status is unavailable"
            ):
                self._projection(live, context_schema_version=version)


class LegacyRegressionComparisonTests(unittest.TestCase):
    def test_numeric_strings_compare_only_for_frozen_finite_numeric_oracles(self) -> None:
        self.assertTrue(legacy_compare.rows_equal(
            [{"headcount": "42"}], [{"headcount": 42}], 0, True))
        self.assertTrue(legacy_compare.rows_equal(
            [{"score": "1.245"}], [{"score": 1.25}], 0.01, True))
        self.assertFalse(legacy_compare.rows_equal(
            [{"score": "1.261"}], [{"score": 1.25}], 0.01, True))

    def test_numeric_looking_text_bool_null_and_nonfinite_values_stay_strict(self) -> None:
        self.assertTrue(legacy_compare.rows_equal(
            [{"code": "001"}], [{"code": "001"}], 0.01, True))
        self.assertFalse(legacy_compare.rows_equal(
            [{"code": 1}], [{"code": "001"}], 0.01, True))
        self.assertFalse(legacy_compare.rows_equal(
            [{"active": "1"}], [{"active": True}], 0.01, True))
        self.assertTrue(legacy_compare.rows_equal(
            [{"value": None}], [{"value": None}], 0.01, True))
        self.assertFalse(legacy_compare.rows_equal(
            [{"value": "0"}], [{"value": None}], 0.01, True))
        self.assertFalse(legacy_compare.rows_equal(
            [{"value": float("nan")}], [{"value": float("nan")}], 0.01, True))
        self.assertFalse(legacy_compare.rows_equal(
            [{"value": "NaN"}], [{"value": float("nan")}], 0.01, True))


class ExecutionCheckHarnessTests(unittest.TestCase):
    def test_execution_gate_checks_grouped_confirmation_and_seven_mcp_boundaries(self) -> None:
        host = object.__new__(experiment.Host)
        host.questions = {item["id"]: item for item in experiment.read(
            experiment.CORPUS_PATH)["questions"]}
        host.directory = Path(tempfile.mkdtemp(prefix="btv-check-harness-test-"))
        host.mcp_ports = {"candidate": 9123}
        host.keys = {"candidate": {"employee": "test-key",
                                    "department_manager": "test-key"}}
        prepared_actors: dict[str, str] = {}

        def fake_prepare(group, question, semantic_sql=None, conditions=None,
                         confirmed_conditions=None):
            prepared_actors[semantic_sql] = question["actor"]
            is_canonical = (
                semantic_sql == question["canonical"]["semanticSql"]
                and conditions == question["canonical"].get("conditions", {})
                and confirmed_conditions == question["canonical"].get("confirmedConditions", [])
            )
            reasons = [] if is_canonical else [
                "metric_conflicts", "predicate_missing", "predicate_conflicts",
                "time_filter_conflicts", "time_confirmation_missing",
                "time_grouping_transformed", "unsupported_sql_shape",
            ]
            return {"status": "ready" if is_canonical else "blocked",
                    "preparationId": "test-blocked-preparation",
                    "clarifications": [{"conditionKey": key} for key in question["canonical"].get("conditions", {})
                        if key != "trend" and (key not in (conditions or {}) or key not in (confirmed_conditions or []))],
                    "blockedReasons": [{"reason": reason} for reason in reasons]}

        host._prepare_canonical = fake_prepare
        calls: list[dict] = []

        async def fake_call_mcp(port, token, tool, arguments):
            calls.append({"port": port, "tool": tool,
                          "hasPreparationId": "preparation_id" in arguments})
            return {"result": {"status": "error",
                "error": {"code": "CLARIFICATION_REQUIRED"},
                "stats": {"returnedRows": 0}, "previewRows": []}}

        try:
            with patch.object(experiment, "call_mcp", fake_call_mcp):
                host._validate_execution_checks()
            report = json.loads((host.directory / "execution-check-validation.json").read_text())
            checks = {item["probeId"]: item for item in report["checks"]}
            self.assertTrue(report["passed"])
            self.assertEqual(report["totalChecks"], 21)
            self.assertEqual(report["positiveAcceptanceCases"], 5)
            self.assertEqual(report["mandatoryCounterexamples"], 16)
            self.assertEqual(report["conflictCounterexamples"], 9)
            self.assertEqual(report["unsupportedShapeCounterexamples"], 3)
            self.assertEqual(report["groupedConfirmationCounterexamples"], 4)
            self.assertEqual(report["executionBoundaryCases"], 7)
            self.assertEqual(len(checks), 21)
            self.assertEqual(prepared_actors[
                host.questions["BTV-23"]["canonical"]["semanticSql"]], "department_manager")
            self.assertEqual(checks["performance-quarter-scalar-ready"]["status"], "ready")
            self.assertEqual(checks["performance-original-date-trend-ready"]["status"], "ready")
            shape_ids = {"headcount-or-shape-blocked", "salary-subquery-shape-blocked",
                         "performance-subquery-shape-blocked"}
            confirmation_ids = {f"{scene}-original-date-trend-period-{state}-blocked"
                                for scene in ("salary", "performance") for state in ("missing", "unconfirmed")}
            for probe_id in confirmation_ids:
                self.assertTrue(checks[probe_id]["publishedConditionKeyDiscoverable"])
            for probe_id in shape_ids | confirmation_ids:
                boundary = checks[probe_id]["executionBoundary"]
                self.assertTrue(boundary["legacyWithoutPreparationId"]["passed"])
                self.assertTrue(boundary["blockedPreparationId"]["passed"])
            self.assertEqual(len(calls), 14)
            self.assertEqual(sum(call["hasPreparationId"] for call in calls), 7)
            self.assertNotIn("previewRows", (host.directory / "execution-check-validation.json").read_text())
        finally:
            import shutil
            shutil.rmtree(host.directory, ignore_errors=True)


class OraclePermissionBoundaryTests(unittest.TestCase):
    def _host(self, directory: Path) -> tuple[experiment.Host, dict]:
        question = next(item for item in experiment.read(experiment.CORPUS_PATH)["questions"]
                        if item["id"] == "BTV-02")
        host = object.__new__(experiment.Host)
        host.questions = {question["id"]: question}
        host.directory = directory
        host.directory.mkdir()
        host.mcp_ports = {"baseline": 9122, "candidate": 9123}
        host.keys = {group: {question["actor"]: f"{group}-key"}
            for group in experiment.GROUPS}
        oracle_results = {f"BTV-{number:02d}": {} for number in range(1, 31)}
        (directory / "oracle-results.json").write_text(
            json.dumps(oracle_results), encoding="utf-8")
        host._prepare_canonical = lambda group, _question: {
            "status": "blocked", "preparationId": f"blocked-{group}"}
        return host, question

    def test_blocked_prepare_requires_policy_denied_plan_and_zero_row_governed_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-denial-boundary-") as temp:
            host, question = self._host(Path(temp) / "run")
            calls = []

            async def fake_call_mcp(port, token, tool, arguments):
                calls.append({"port": port, "token": token, "tool": tool,
                    "arguments": arguments})
                code = ("POLICY_DENIED" if tool == "semarail_plan_query"
                        else "CLARIFICATION_REQUIRED")
                return {"result": {"schemaVersion": 2, "status": "error",
                    "error": {"code": code},
                    "stats": {"returnedRows": question["oracle"]["databaseRowsReturned"]}}}

            with patch.object(experiment, "call_mcp", fake_call_mcp):
                host._validate_oracles()
            report = json.loads((host.directory / "oracle-validation.json").read_text())
            self.assertTrue(report["passed"])
            self.assertEqual(report["permissionBoundaryExecutions"], 2)
            self.assertEqual(report["permissionPlanExecutions"], 2)
            self.assertEqual(len(calls), 4)
            self.assertEqual({call["tool"] for call in calls},
                {"semarail_plan_query", "semarail_governed_query"})
            self.assertTrue(all("preparation_id" not in call["arguments"]
                for call in calls if call["tool"] == "semarail_governed_query"))
            self.assertTrue(all(call["arguments"]["semantic_sql"] ==
                question["canonical"]["semanticSql"] for call in calls))
            candidate = report["permissionBoundaryResults"][0]["groups"]["candidate"]
            self.assertTrue(candidate["policyDenialProven"])
            self.assertTrue(candidate["governedBoundaryPassed"])
            self.assertEqual(candidate["plan"]["errorCode"], "POLICY_DENIED")
            self.assertTrue(candidate["plan"]["notARowReturningTool"])
            self.assertEqual(candidate["governedQuery"]["errorCode"], "CLARIFICATION_REQUIRED")
            self.assertTrue(candidate["governedQuery"]["statsReturnedRowsPresent"])
            self.assertFalse(candidate["governedQuery"]["previewRowsPresent"])
            self.assertTrue(candidate["governedQuery"]["noResultRows"])

    def test_blocked_prepare_cannot_treat_missing_returned_rows_as_zero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-denial-missing-row-count-") as temp:
            host, _question = self._host(Path(temp) / "run")

            async def fake_call_mcp(_port, _token, tool, _arguments):
                result = ({"schemaVersion": 2, "status": "error",
                    "error": {"code": "POLICY_DENIED"}, "stats": {"returnedRows": 0}}
                    if tool == "semarail_plan_query" else
                    {"schemaVersion": 2, "status": "error",
                     "error": {"code": "CLARIFICATION_REQUIRED"}, "stats": {}})
                return {"result": result}

            with patch.object(experiment, "call_mcp", fake_call_mcp):
                with self.assertRaisesRegex(ValueError, "oracle check failed"):
                    host._validate_oracles()
            report = json.loads((host.directory / "oracle-validation.json").read_text())
            self.assertFalse(report["passed"])
            candidate = report["permissionBoundaryResults"][0]["groups"]["candidate"]
            self.assertTrue(candidate["policyDenialProven"])
            self.assertFalse(candidate["governedBoundaryPassed"])
            self.assertFalse(candidate["governedQuery"]["statsReturnedRowsPresent"])
            self.assertIsNone(candidate["governedQuery"]["returnedRows"])
            self.assertEqual(report["failures"][0]["groups"]["candidate"],
                             "GOVERNED_QUERY_DENIAL_NOT_CONFIRMED")

    def test_blocked_prepare_cannot_pass_without_exact_policy_denied_plan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-denial-wrong-plan-code-") as temp:
            host, _question = self._host(Path(temp) / "run")

            async def fake_call_mcp(_port, _token, tool, _arguments):
                code = "QUERY_INVALID" if tool == "semarail_plan_query" else "CLARIFICATION_REQUIRED"
                return {"result": {"schemaVersion": 2, "status": "error",
                    "error": {"code": code}, "stats": {"returnedRows": 0}}}

            with patch.object(experiment, "call_mcp", fake_call_mcp):
                with self.assertRaisesRegex(ValueError, "oracle check failed"):
                    host._validate_oracles()
            report = json.loads((host.directory / "oracle-validation.json").read_text())
            self.assertFalse(report["passed"])
            self.assertEqual(report["failures"][0]["groups"]["candidate"],
                             "PLAN_POLICY_DENIAL_NOT_CONFIRMED")

    def test_ready_prepare_requires_governed_policy_denial_with_explicit_zero_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-denial-prepared-boundary-") as temp:
            host, question = self._host(Path(temp) / "run")
            host._prepare_canonical = lambda _group, _question: {
                "status": "ready", "preparationId": "ready-preparation"}

            async def fake_call_mcp(_port, _token, tool, _arguments):
                result = ({"schemaVersion": 2, "status": "error",
                    "error": {"code": "POLICY_DENIED"}}
                    if tool == "semarail_plan_query" else
                    {"schemaVersion": 2, "status": "error",
                     "error": {"code": question["oracle"]["errorCode"]},
                     "stats": {"returnedRows": question["oracle"]["databaseRowsReturned"]}})
                return {"result": result}

            with patch.object(experiment, "call_mcp", fake_call_mcp):
                host._validate_oracles()
            report = json.loads((host.directory / "oracle-validation.json").read_text())
            self.assertTrue(report["passed"])
            candidate = report["permissionBoundaryResults"][0]["groups"]["candidate"]
            self.assertTrue(candidate["preparationReady"])
            self.assertTrue(candidate["governedQueryPreparationIdProvided"])
            self.assertTrue(candidate["governedBoundaryPassed"])
            self.assertFalse(candidate["plan"]["statsReturnedRowsPresent"])
            self.assertIsNone(candidate["plan"]["returnedRows"])

    def test_ready_status_without_preparation_id_is_not_a_nonready_prepare(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-denial-ready-without-id-") as temp:
            host, _question = self._host(Path(temp) / "run")
            host._prepare_canonical = lambda _group, _question: {"status": "ready"}

            async def fake_call_mcp(_port, _token, tool, _arguments):
                code = "POLICY_DENIED" if tool == "semarail_plan_query" else "CLARIFICATION_REQUIRED"
                return {"result": {"schemaVersion": 2, "status": "error",
                    "error": {"code": code}, "stats": {"returnedRows": 0}}}

            with patch.object(experiment, "call_mcp", fake_call_mcp):
                with self.assertRaisesRegex(ValueError, "oracle check failed"):
                    host._validate_oracles()
            report = json.loads((host.directory / "oracle-validation.json").read_text())
            candidate = report["permissionBoundaryResults"][0]["groups"]["candidate"]
            self.assertFalse(candidate["preparationReady"])
            self.assertFalse(candidate["preparationNonReady"])
            self.assertFalse(candidate["governedBoundaryPassed"])


class OracleFailureAttributionTests(unittest.TestCase):
    def _result(self, value: int) -> dict:
        return {"schemaVersion": 1, "status": "success",
            "columns": ["active_employee_count"],
            "previewRows": [{"active_employee_count": value}],
            "stats": {"returnedRows": 1, "truncated": False},
            "delivery": "inline"}

    def _run_validation(self, directory: Path, *, baseline: dict, candidate: dict,
                        native: dict, frozen: dict) -> dict:
        question = next(item for item in experiment.read(experiment.CORPUS_PATH)["questions"]
                        if item["id"] == "BTV-01")
        host = object.__new__(experiment.Host)
        host.questions = {question["id"]: question}
        host.directory = directory
        host.directory.mkdir()
        host.manifest = {"data": {"databaseUrlEnv": "BUSINESS_TRUST_TEST_DATABASE_URL"}}
        host.mcp_ports = {"baseline": 9122, "candidate": 9123}
        host.keys = {group: {question["actor"]: f"{group}-test-key"}
                     for group in experiment.GROUPS}
        host._prepare_canonical = lambda _group, _question: {
            "status": "ready", "preparationId": "prepared-for-test"}
        outputs = {f"BTV-{number:02d}": {} for number in range(1, 31)}
        outputs[question["id"]] = frozen
        (directory / "oracle-results.json").write_text(
            json.dumps(outputs), encoding="utf-8")

        results_by_port = {9122: baseline, 9123: candidate}

        async def fake_call_mcp(port, _token, _tool, _arguments):
            return {"result": results_by_port[port]}

        native_module = types.ModuleType("native_oracle")
        native_module.execute = lambda *_args, **_kwargs: native
        with patch.dict(os.environ, {"BUSINESS_TRUST_TEST_DATABASE_URL": "unused"}), \
                patch.object(experiment, "call_mcp", fake_call_mcp), \
                patch.dict(sys.modules, {"native_oracle": native_module}):
            with self.assertRaisesRegex(ValueError, "oracle check failed"):
                host._validate_oracles()
        return json.loads((directory / "oracle-validation.json").read_text())

    def test_candidate_business_evidence_failure_does_not_blame_correct_baseline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-oracle-attribution-") as temp:
            expected = self._result(7)
            candidate_error = {"schemaVersion": 2, "status": "error",
                "error": {"code": "BUSINESS_EVIDENCE_UNAVAILABLE", "phase": "diagnostics"},
                "stats": {"returnedRows": 0}}
            report = self._run_validation(Path(temp) / "run", baseline=expected,
                candidate=candidate_error, native=expected, frozen=expected)

        self.assertFalse(report["passed"])
        self.assertEqual(report["failures"][0]["groups"], {
            "candidate": "BUSINESS_EVIDENCE_UNAVAILABLE"})
        self.assertTrue(report["failures"][0]["nativeOracle"]["passed"])

    def test_baseline_error_is_attributed_to_baseline_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-oracle-baseline-failure-") as temp:
            expected = self._result(7)
            baseline_error = {"schemaVersion": 2, "status": "error",
                "error": {"code": "QUERY_ERROR"}, "stats": {"returnedRows": 0}}
            report = self._run_validation(Path(temp) / "run", baseline=baseline_error,
                candidate=expected, native=expected, frozen=expected)

        self.assertEqual(report["failures"][0]["groups"], {"baseline": "QUERY_ERROR"})
        self.assertTrue(report["failures"][0]["nativeOracle"]["passed"])

    def test_native_oracle_mismatch_is_separate_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-oracle-native-failure-") as temp:
            expected = self._result(7)
            report = self._run_validation(Path(temp) / "run", baseline=expected,
                candidate=expected, native=self._result(8), frozen=expected)

        failure = report["failures"][0]
        self.assertFalse(report["passed"])
        self.assertEqual(failure["groups"], {})
        self.assertEqual(failure["nativeOracle"], {
            "passed": False, "failureCode": "NATIVE_ORACLE_MISMATCH"})


class CorpusAndScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = experiment.read(experiment.CORPUS_PATH)
        cls.questions = cls.corpus["questions"]

    def test_corpus_has_declared_scene_role_and_language_balance(self) -> None:
        experiment.validate_corpus(self.corpus)
        self.assertEqual(len(self.questions), 30)
        self.assertEqual({name: sum(q["scenario"] == name for q in self.questions)
                          for name in {q["scenario"] for q in self.questions}}, {
            "current_headcount": 10, "salary_snapshots": 10,
            "quarterly_performance": 10,
        })
        self.assertEqual(sum(q["language"] == "zh" for q in self.questions), 20)
        self.assertEqual(sum(q["language"] == "en" for q in self.questions), 10)

    def test_balanced_metadata_cannot_hide_a_wrong_language_question(self) -> None:
        for question_id, replacement in [
            ("BTV-16", "In my authorized APAC region, return average monthly base salary by snapshot date from March through September 2024."),
            ("BTV-06", "在我获准查看的亚太区域，按部门统计当前在职员工人数。"),
        ]:
            with self.subTest(question_id=question_id):
                corpus = copy.deepcopy(self.corpus)
                question = next(q for q in corpus["questions"] if q["id"] == question_id)
                question["public"]["question"] = replacement
                self.assertEqual(sum(q["language"] == "zh" for q in corpus["questions"]), 20)
                self.assertEqual(sum(q["language"] == "en" for q in corpus["questions"]), 10)
                with self.assertRaisesRegex(ValueError, "text does not match declared language"):
                    experiment.validate_corpus(corpus)

    def test_question_text_requires_language_content(self) -> None:
        for question_id, replacement in [("BTV-01", "  "), ("BTV-06", "2024-09-01?!")]:
            with self.subTest(question_id=question_id):
                corpus = copy.deepcopy(self.corpus)
                next(q for q in corpus["questions"] if q["id"] == question_id)["public"]["question"] = replacement
                with self.assertRaises(ValueError):
                    experiment.validate_corpus(corpus)

    def test_only_explicit_date_trends_activate_grouping_condition(self) -> None:
        trends = {"BTV-12", "BTV-14", "BTV-16", "BTV-20",
                  "BTV-22", "BTV-24", "BTV-26", "BTV-28"}
        for question in self.questions:
            canonical = question.get("canonical") or {}
            active = (canonical.get("conditions", {}).get("trend") is True
                      and "trend" in canonical.get("confirmedConditions", []))
            self.assertEqual(active, question["id"] in trends, question["id"])

    def test_schedule_has_180_unique_fixed_positions_and_interleaves_groups(self) -> None:
        schedule = experiment.make_schedule(self.questions)
        experiment.validate_schedule(self.questions, schedule)
        self.assertEqual([item["id"] for item in schedule],
                         [f"job-{number:03d}" for number in range(1, 181)])
        self.assertEqual(sum(item["group"] == "baseline" for item in schedule), 90)
        self.assertEqual(sum(item["group"] == "candidate" for item in schedule), 90)
        for question in self.questions:
            for group in experiment.GROUPS:
                for round_no in (1, 2, 3):
                    self.assertEqual(sum(item["questionId"] == question["id"]
                                         and item["group"] == group
                                         and item["round"] == round_no
                                         for item in schedule), 1)

    def test_bad_trend_condition_fails_corpus_validation(self) -> None:
        changed = copy.deepcopy(self.corpus)
        trend = next(item for item in changed["questions"] if item["id"] == "BTV-12")
        trend["canonical"]["confirmedConditions"].remove("trend")
        with self.assertRaisesRegex(ValueError, "trend condition"):
            experiment.validate_corpus(changed)

    def test_shared_project_publishes_conditional_grain_without_inferred_units(self) -> None:
        baseline_project = experiment.BASELINE_DEFAULT / "benchmarks/hr-enterprise/project"
        if not baseline_project.is_dir():
            self.skipTest("frozen baseline project is not installed")
        with tempfile.TemporaryDirectory(prefix="btv-rules-test-") as temp:
            target = Path(temp) / "project"
            experiment.build_checked_project_template(
                baseline_project, target, Path(temp) / "state")
            index = json.loads((target / "semantic-console/rules-index.json").read_text(encoding="utf-8"))
        rules = [rule for rule in index["rules"] if rule["title"].startswith((
            "Current active workforce", "Salary snapshot", "Performance review"))]
        self.assertEqual(len(rules), 3)
        checks = [check for rule in rules for check in rule["executionChecks"]["checks"]]
        self.assertFalse(any("unit" in check for check in checks))
        time_grouping = [check for check in checks if check["kind"] == "timeGrouping"]
        self.assertEqual(len(time_grouping), 2)
        self.assertTrue(all(check["whenGrouped"] is True
                            and check["conditionKey"] == "trend" for check in time_grouping))

    def test_freeze_separates_legacy_project_and_hashes_private_oracles(self) -> None:
        if not experiment.BASELINE_DEFAULT.is_dir():
            self.skipTest("frozen baseline checkout is not installed")
        if not (experiment.BUNDLE_ROOT / experiment.BASELINE_SOURCE_MANIFEST).is_file():
            self.skipTest("version-2 frozen baseline source manifest is not installed")
        try:
            experiment.verify_baseline(experiment.BASELINE_DEFAULT,
                experiment.BUNDLE_ROOT / experiment.BASELINE_SOURCE_MANIFEST)
        except ValueError:
            self.skipTest("frozen baseline source hash integrity is not currently valid")
        if not (experiment.HISTORICAL_DEFAULT / ".benchmark-data/hr-enterprise/generated/manifest.json").is_file():
            self.skipTest("synthetic dataset manifest is not installed")
        corpus = self.corpus
        oracle_results = {}
        for question in self.questions:
            kind = question["oracle"]["type"]
            oracle_results[question["id"]] = (
                {"schemaVersion": 2, "status": "success", "columns": question["oracle"]["columns"],
                 "previewRows": [], "stats": {"returnedRows": 0, "truncated": False}, "delivery": "inline"}
                if kind == "rows" else
                {"schemaVersion": 2, "status": "error", "error": {"code": "POLICY_DENIED"},
                 "stats": {"returnedRows": 0, "truncated": False}}
                if kind == "denial" else
                {"schemaVersion": 1, "type": "blocked", "noQueryRequired": True})
        env_name = "BTV_TEST_DATABASE_URL"
        embedding_freeze = {"model": experiment.EMBEDDING_MODEL,
            "revision": experiment.EMBEDDING_MODEL_REVISION,
            "cachePath": "test-cache/snapshot", "files": {"config.json": "a" * 64},
            "sha256": "b" * 64}
        with tempfile.TemporaryDirectory(prefix="btv-freeze-test-") as temp:
            runs_root = Path(temp) / "runs"
            with patch.dict(os.environ, {env_name: "postgresql://tester:not-a-secret@127.0.0.1:55433/testdb"}), \
                 patch.object(experiment, "materialize_oracles", return_value=oracle_results), \
                 patch.object(experiment, "verify_embedding_freeze", return_value=embedding_freeze):
                run_dir = experiment.freeze_run("freeze-test", database_url_env=env_name,
                    runs_root=runs_root, execution_mode="preacceptance_only")
            with patch.object(experiment, "verify_embedding_freeze", return_value=embedding_freeze):
                manifest = experiment.verify_run_freeze(run_dir)
                self.assertEqual(manifest["projectSnapshot"]["files"],
                                 manifest["checkedProjectSnapshot"]["files"])
                self.assertEqual(experiment.digest(run_dir / "oracle-results.json"),
                                 manifest["oracleResultsSha256"])
                required_candidate_sources = {
                    "apps/semantic-console/web/src/App.tsx",
                    "apps/semantic-console/web/package.json",
                    "packages/contract/src/index.ts",
                    "packages/contract/tests/business_evidence.spec.ts",
                    "packages/core/bin/semarail.mjs",
                    "packages/core/runtime/bootstrap.py",
                    "packages/core/python/semantic-console/server/app.py",
                    "packages/core/semantic-console-web/index.html",
                    "packages/core/package.json", "python/sidecar/pyproject.toml",
                    "package.json", "pnpm-lock.yaml",
                }
                self.assertTrue(required_candidate_sources <= set(manifest["candidate"]["sourceHashes"]))
                self.assertEqual(manifest["candidate"]["sourceManifestSha256"],
                                 experiment.canonical_hash(manifest["candidate"]["sourceHashes"]))
                distribution = manifest["candidate"]["distribution"]
                self.assertEqual(distribution["name"], "@hejielijob/semarail-core")
                self.assertEqual(distribution["version"], experiment.read(
                    experiment.REPO_ROOT / "packages/core/package.json")["version"])
                if distribution["artifact"] is not None:
                    self.assertEqual(experiment.digest(
                        experiment.REPO_ROOT / distribution["artifact"]["path"]),
                        distribution["artifact"]["sha256"])
                serialized = json.dumps(manifest, ensure_ascii=False)
                self.assertNotIn("not-a-secret", serialized)
                self.assertNotIn("tester", serialized)
                legacy_index_path = run_dir / "legacy-projects/baseline/semantic-console/rules-index.json"
                legacy_candidate_index_path = run_dir / "legacy-projects/candidate/semantic-console/rules-index.json"
                legacy_index = (json.loads(legacy_index_path.read_text(encoding="utf-8"))
                                if legacy_index_path.is_file() else None)
                legacy_candidate_index = (json.loads(legacy_candidate_index_path.read_text(encoding="utf-8"))
                                         if legacy_candidate_index_path.is_file() else None)
                checked_index = json.loads((run_dir / "projects/candidate/semantic-console/rules-index.json").read_text(encoding="utf-8"))
                checked_baseline_index = json.loads((run_dir / "projects/baseline/semantic-console/rules-index.json").read_text(encoding="utf-8"))
                self.assertFalse(legacy_index and any("executionChecks" in rule
                                                       for rule in legacy_index["rules"]))
                self.assertEqual(legacy_index, legacy_candidate_index)
                self.assertEqual(checked_index, checked_baseline_index)
                self.assertEqual(sum("executionChecks" in rule for rule in checked_index["rules"]), 3)
                self.assertEqual(manifest["executionMode"], "preacceptance_only")
                self.assertFalse(manifest["workerDispatchAllowed"])
                self.assertEqual(manifest["runtime"]["embedding"], embedding_freeze)
                with self.assertRaisesRegex(ValueError, "preacceptance-only"):
                    runner.run_workers(run_dir)


class EmbeddingFreezeTests(unittest.TestCase):
    def test_checkpoint_revision_and_file_hashes_are_pinned_offline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-hf-cache-") as temp:
            hub = Path(temp) / "hub"
            cache = hub / f"models--sentence-transformers--{experiment.EMBEDDING_MODEL}"
            snapshot = cache / "snapshots" / experiment.EMBEDDING_MODEL_REVISION
            snapshot.mkdir(parents=True)
            (cache / "refs").mkdir()
            (cache / "refs" / "main").write_text(experiment.EMBEDDING_MODEL_REVISION,
                encoding="utf-8")
            for name in ("config.json", "modules.json", "sentence_bert_config.json", "model.bin"):
                (snapshot / name).write_text(name, encoding="utf-8")
            with patch.dict(os.environ, {"HF_HOME": temp, "HF_HUB_CACHE": ""}):
                pinned = experiment.verify_embedding_freeze()
            self.assertEqual(pinned["revision"], experiment.EMBEDDING_MODEL_REVISION)
            self.assertEqual(set(pinned["files"]), {
                "config.json", "modules.json", "sentence_bert_config.json", "model.bin"})
            changed = Path(snapshot / "model.bin")
            changed.write_text("different", encoding="utf-8")
            with patch.dict(os.environ, {"HF_HOME": temp, "HF_HUB_CACHE": ""}):
                self.assertNotEqual(experiment.verify_embedding_freeze()["sha256"], pinned["sha256"])

    def test_core_child_environment_is_forced_offline_and_revision_pinned(self) -> None:
        environment = {"HF_HUB_OFFLINE": "0", "SEMARAIL_EMBEDDING_MODEL_REVISION": "main"}
        experiment.configure_embedding_environment(environment)
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        self.assertEqual(environment["TRANSFORMERS_OFFLINE"], "1")
        self.assertEqual(environment["SEMARAIL_EMBEDDING_PROVIDER"], "sentence-transformers")
        self.assertEqual(environment["SEMARAIL_EMBEDDING_MODEL"], experiment.EMBEDDING_MODEL)
        self.assertEqual(environment["SEMARAIL_EMBEDDING_MODEL_REVISION"],
                         experiment.EMBEDDING_MODEL_REVISION)


class DistributionArtifactTests(unittest.TestCase):
    def test_candidate_distribution_hash_binds_package_name_and_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-package-freeze-") as temp:
            root = Path(temp)
            package_dir = root / "packages/core"
            package_dir.mkdir(parents=True)
            (package_dir / "package.json").write_text(json.dumps({
                "name": "@example/semarail-core", "version": "1.2.3",
            }), encoding="utf-8")
            artifact = root / "dist/example-semarail-core-1.2.3.tgz"
            artifact.parent.mkdir()
            artifact.write_bytes(b"packed-package")
            frozen = experiment.distribution_artifact(root)
            self.assertEqual(frozen, {
                "name": "@example/semarail-core", "version": "1.2.3",
                "artifact": {"path": "dist/example-semarail-core-1.2.3.tgz",
                    "sha256": experiment.digest(artifact)},
            })
            experiment.verify_distribution_artifact(root, frozen)
            artifact.write_bytes(b"changed-package")
            with self.assertRaisesRegex(ValueError, "artifact hash changed"):
                experiment.verify_distribution_artifact(root, frozen)


class AppDispatchTests(unittest.TestCase):
    def test_prompt_binds_job_nonce_and_only_transport_placeholders(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-app-prompt-") as temp:
            root = Path(temp)
            worker_dir = root / "job-001"
            worker_dir.mkdir()
            prompt = runner.render_prompt(
                "job {JOB_ID} nonce {JOB_NONCE} path {EMPTY_JOB_DIRECTORY} "
                "at {BROKER_PORT} using {PYTHON} and {WORKER_CLIENT}",
                worker_dir=worker_dir, job_id="job-001", broker_port=48785,
                python=str(root / "python.exe"), client=root / "worker_client.py",
                nonce="a" * 32)
        self.assertIn("job-001", prompt)
        self.assertIn("nonce " + "a" * 32, prompt)
        self.assertNotIn("{", prompt)

    def test_worker_prompt_has_four_complete_literal_transport_wrappers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-worker-prompt-") as temp:
            root = Path(temp)
            worker_dir = root / "job-001"
            worker_dir.mkdir()
            python = root / "python.exe"
            client = root / "worker_client.py"
            template = experiment.PROMPT_PATH.read_text(encoding="utf-8")
            prompt = runner.render_prompt(template, worker_dir=worker_dir,
                job_id="job-001", broker_port=48785, python=str(python),
                client=client, nonce="a" * 32)
        self.assertNotIn("{OPERATION}", prompt)
        self.assertNotIn("{ONE_LINE_JSON_REQUEST}", prompt)
        lines = [line for line in prompt.splitlines()
            if line.startswith("text(await tools.exec_command(")]
        self.assertEqual(len(lines), 4)
        wrappers = [json.loads(line[len("text(await tools.exec_command("):-len("));")])
            for line in lines]
        operations = [re.search(r"job-001 (begin|tool|clarify|final) - --port 48785", item["cmd"]).group(1)
            for item in wrappers]
        self.assertEqual(operations, ["begin", "tool", "clarify", "final"])
        self.assertTrue(all(item["workdir"].endswith("/job-001") for item in wrappers))
        self.assertTrue(all(item["max_output_tokens"] == 12000 for item in wrappers))
        self.assertTrue(all(str(client).replace("\\", "/") in item["cmd"] for item in wrappers))
        tool_command = wrappers[1]["cmd"]
        self.assertIn('"name":"<EXACT_MCP_TOOL_NAME_FROM_BEGIN>"', tool_command)
        self.assertNotIn("job-001 semarail_", tool_command)
        self.assertIn("transport error is terminal", prompt)
        self.assertIn('tools.write_stdin({"session_id":<SESSION_ID_FROM_YOUR_OWN_EXEC_RESULT>', prompt)
        self.assertIn('"chars":"","yield_time_ms":30000,"max_output_tokens":12000', prompt)
        self.assertIn("Never use an ID found in command output text", prompt)

    def test_transport_smoke_prompt_has_three_fixed_literal_wrappers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-smoke-prompt-") as temp:
            root = Path(temp)
            worker_dir = root / "job-001"
            worker_dir.mkdir()
            python = root / "python.exe"
            client = root / "worker_client.py"
            prompt = transport_smoke._prompt(python=python, client=client,
                worker_dir=worker_dir, port=48995, nonce="a" * 32)
        self.assertNotIn("{OPERATION}", prompt)
        self.assertNotIn("ONE_LINE_JSON_REQUEST", prompt)
        lines = [line for line in prompt.splitlines()
            if line.startswith("text(await tools.exec_command(")]
        self.assertEqual(len(lines), 3)
        wrappers = [json.loads(line[len("text(await tools.exec_command("):-len("));")])
            for line in lines]
        operations = [re.search(r"job-001 (begin|tool|final) - --port 48995", item["cmd"]).group(1)
            for item in wrappers]
        self.assertEqual(operations, ["begin", "tool", "final"])
        self.assertTrue(all(item["workdir"] == worker_dir.as_posix() for item in wrappers))
        self.assertTrue(all(str(client).replace("\\", "/") in item["cmd"] for item in wrappers))
        tool_command = wrappers[1]["cmd"]
        self.assertIn('"name":"semarail_get_context"', tool_command)
        self.assertIn("job-001 tool - --port 48995", tool_command)
        self.assertNotIn("job-001 semarail_get_context", tool_command)
        self.assertIn("transport, or poll error occurs, stop immediately", prompt)
        self.assertIn("holds this tool response for 12 seconds", prompt)
        self.assertIn("Repeat the same poll only if that poll's top-level result metadata", prompt)

    def test_app_session_lookup_is_scoped_to_the_explicit_thread_id(self) -> None:
        thread_id = "01a12bd8-80ec-7c53-aea3-2dea074af449"
        with tempfile.TemporaryDirectory(prefix="btv-app-session-") as temp:
            root = Path(temp) / ".codex"
            session = root / "sessions" / "2026" / "10" / "13" / f"rollout-{thread_id}.jsonl"
            session.parent.mkdir(parents=True)
            data = json.dumps({"type": "session_meta", "payload": {"id": thread_id}}).encode()
            session.write_bytes(data + b"\n")
            with patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                self.assertEqual(app_dispatch._locate_session(thread_id, None), session.resolve())
                self.assertEqual(app_dispatch._locate_session(thread_id, session), session.resolve())
                app_dispatch._validate_session_bytes(session.read_bytes(), thread_id)
                with self.assertRaisesRegex(ValueError, "identity"):
                    app_dispatch._validate_session_bytes(data.replace(thread_id.encode(), b"wrong"), thread_id)
                decoy = session.parent / "unrelated.jsonl"
                decoy.write_bytes(data + b"\n")
                with self.assertRaisesRegex(ValueError, "broker-recorded thread"):
                    app_dispatch._locate_session(thread_id, decoy)
                duplicate_key = (b'{"type":"session_meta","type":"session_meta",'
                    b'"payload":{"id":"' + thread_id.encode() + b'"}}\n')
                with self.assertRaisesRegex(ValueError, "malformed JSON at line 1"):
                    app_dispatch._validate_session_bytes(duplicate_key, thread_id)

    def test_app_trace_import_is_broker_bound_and_one_shot(self) -> None:
        thread_id = "01a12bd8-80ec-7c53-aea3-2dea074af449"
        with tempfile.TemporaryDirectory(prefix="btv-app-import-") as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "app-dispatch-start.json").write_text(json.dumps({
                "runId": "import-test", "selectedJobIds": ["job-001"],
            }), encoding="utf-8")
            (run_dir / "schedule.json").write_text(json.dumps([{"id": "job-001"}]),
                encoding="utf-8")
            record_dir = run_dir / "records/job-001"
            record_dir.mkdir(parents=True)
            (record_dir / "0000.json").write_text(json.dumps({
                "id": "job-001", "status": "complete", "workerThreadId": thread_id,
            }), encoding="utf-8")
            sessions_root = root / ".codex/sessions/2026/10/13"
            sessions_root.mkdir(parents=True)
            source = sessions_root / f"rollout-{thread_id}.jsonl"
            data = json.dumps({"type": "session_meta", "payload": {"id": thread_id}}).encode()
            source.write_bytes(data + b"\n")
            manifest = {"runId": "import-test", "workerDispatchAllowed": True}
            with patch.object(app_dispatch, "verify_run_freeze", return_value=manifest), \
                 patch.dict(os.environ, {"CODEX_HOME": str(root / ".codex")}):
                result = app_dispatch.import_app_trace(run_dir, "job-001", trace_path=source)
                self.assertEqual(result["threadId"], thread_id)
                self.assertEqual(Path(result["tracePath"]).read_bytes(), data + b"\n")
                with self.assertRaisesRegex(ValueError, "one-shot"):
                    app_dispatch.import_app_trace(run_dir, "job-001", trace_path=source)

    def test_worker_transport_reads_thread_id_out_of_band_and_rejects_bad_ids(self) -> None:
        thread_id = "01a12bd8-80ec-7c53-aea3-2dea074af449"
        self.assertEqual(worker_client._thread_identity_header({"CODEX_THREAD_ID": thread_id}),
                         {"X-BTV-Thread-ID": thread_id})
        self.assertEqual(worker_client._thread_identity_header({}), {})
        with self.assertRaisesRegex(ValueError, "identity"):
            worker_client._thread_identity_header({"CODEX_THREAD_ID": "not-a-uuid"})

    def test_app_jsonl_audit_binds_thread_model_nonce_and_broker_record(self) -> None:
        thread_id = "01a12bd8-80ec-7c53-aea3-2dea074af449"
        nonce = "a" * 32
        with tempfile.TemporaryDirectory(prefix="btv-app-audit-") as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            worker_dir = Path(temp) / "job-001"
            worker_dir.mkdir()
            candidate = experiment.REPO_ROOT
            python = Path(temp) / "python.exe"
            client = candidate / "benchmarks/business-trust-v1/worker_client.py"
            broker_port = 48785

            def wrapper(operation: str, payload: dict, *, call_id: str) -> dict:
                command = (f"@'\n{json.dumps(payload, separators=(',', ':'))}\n'@ | & "
                    f"'{python}' -X utf8 '{client}' job-001 {operation} - --port {broker_port}")
                return {"type": "response_item", "payload": {"type": "custom_tool_call",
                    "name": "exec", "call_id": call_id,
                    "input": "text(await tools.exec_command(" + json.dumps({
                        "cmd": command, "workdir": str(worker_dir), "max_output_tokens": 12000,
                    }) + "));"}}

            trace_events = [
                {"type": "session_meta", "payload": {"id": thread_id}},
                {"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "max"}},
            ]
            call_index = 0

            def append_call(operation: str, request: dict, *, session_id: int | None = None) -> None:
                nonlocal call_index
                call_index += 1
                call_id = f"exec-{call_index}"
                trace_events.append(wrapper(operation, request, call_id=call_id))
                metadata = {"chunk_id": f"chunk-{call_index}", "wall_time_seconds": 0.01,
                    "original_token_count": 0, "output": ""}
                if session_id is None:
                    metadata["exit_code"] = 0
                else:
                    metadata["session_id"] = session_id
                trace_events.append({"type": "response_item", "payload": {
                    "type": "custom_tool_call_output", "call_id": call_id,
                    "output": [{"type": "input_text", "text": "Script completed"},
                        {"type": "input_text", "text": json.dumps(metadata)}]}})

            append_call("begin", {"nonce": nonce})
            append_call("tool", {"name": "semarail_get_context", "arguments": {
                "question": "smoke question", "context_version": 2}}, session_id=12345)
            poll_id = "poll-1"
            trace_events.append({"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": poll_id,
                "input": "text(await tools.write_stdin(" + json.dumps({
                    "session_id": 12345, "chars": "", "yield_time_ms": 30000,
                    "max_output_tokens": 12000}) + "));"}})
            poll_metadata = {"chunk_id": "chunk-poll", "wall_time_seconds": 0.02,
                "exit_code": 0, "original_token_count": 0, "output": ""}
            trace_events.append({"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": poll_id,
                "output": [{"type": "input_text", "text": json.dumps(poll_metadata)}]}})
            append_call("final", {"answer": "Done", "rows": [], "columns": [],
                "scope": "authorized", "conditions": [], "uncertain": False})
            trace = run_dir / "traces" / "job-001.jsonl"
            trace.parent.mkdir()
            trace_bytes = b"\n".join(json.dumps(event, ensure_ascii=False).encode() for event in trace_events) + b"\n"
            trace.write_bytes(trace_bytes)
            (run_dir / "app-dispatch-start.json").write_text(json.dumps({
                "workerDirectories": {"job-001": str(worker_dir)},
                "pythonPath": str(python), "workerClientPath": str(client),
            }), encoding="utf-8")
            (run_dir / "dispatch-nonces.json").write_text(json.dumps({"job-001": nonce}),
                encoding="utf-8")
            (run_dir / "app-imports").mkdir()
            (run_dir / "app-imports/job-001.json").write_text(json.dumps({
                "threadId": thread_id,
                "traceSha256": experiment.digest_bytes(trace_bytes),
            }), encoding="utf-8")
            (run_dir / "records").mkdir()
            (run_dir / "records/job-001").mkdir()
            (run_dir / "records/job-001/0000.json").write_text(json.dumps({
                "workerThreadId": thread_id, "status": "complete",
            }), encoding="utf-8")
            (run_dir / "manifest.json").write_text(json.dumps({
                "model": {"requested": "gpt-6-luna", "reasoningRequested": "max"},
                "ports": {"broker": broker_port}, "maxToolCalls": 12,
                "candidate": {"path": str(candidate)},
            }), encoding="utf-8")
            (run_dir / "schedule.json").write_text(json.dumps([{"id": "job-001"}]),
                encoding="utf-8")
            result = audit_trace(trace, run_dir=run_dir, job_id="job-001")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["observedModels"], ["gpt-6-luna"])
            self.assertEqual(result["observedReasoning"], ["max"])
            self.assertEqual(result["operations"], ["begin", "tool", "final"])
            self.assertEqual(result["toolCalls"], ["semarail_get_context"])
            self.assertFalse(result["publicPromptReceiptVerified"])
            self.assertFalse(result["sentInputIndependentlyByteVerified"])
            self.assertIsNone(result["bootstrapLauncherSha256"])


class EvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.oracle = {
            "schemaVersion": 1, "type": "rows", "columns": ["count"],
            "aliases": {}, "numericColumns": ["count"],
            "numericTolerance": 0, "allowedExtraColumns": [],
            "orderSensitive": False, "answerRequirements": ["active_status"],
        }
        self.question = {"schemaVersion": 1, "oracle": self.oracle,
            "clarification": None}
        self.result = {"schemaVersion": 2, "status": "success",
            "columns": ["count"], "previewRows": [{"count": 4}],
            "stats": {"returnedRows": 1, "truncated": False}, "delivery": "inline"}
        self.expected = {"schemaVersion": 2, "status": "success",
            "columns": ["count"], "previewRows": [{"count": 4}],
            "stats": {"returnedRows": 1, "truncated": False}, "delivery": "inline"}
        self.audit = {"status": "passed", "observedModels": ["gpt-6-luna"],
                      "observedReasoning": ["max"]}
        self.final = {"answer": "There are four.", "rows": [{"count": 4}],
            "columns": ["count"], "scope": "authorized employees",
            "conditions": ["active_status"], "uncertain": False}
        self.review = {"status": "accepted", "answerCorrect": True,
                       "reason": "Matches the verified result."}

    def record(self, events: list[dict], *, final: dict | None = None) -> dict:
        return {"schemaVersion": 1, "status": "complete", "events": events,
            "audit": self.audit, "final": final or self.final,
            "manualReview": self.review}

    def test_first_sql_uses_first_candidate_from_plan_prepare_or_execute(self) -> None:
        wrong = "SELECT COUNT(employee_id) AS count FROM employees"
        right = "SELECT COUNT(DISTINCT employee_id) AS count FROM employees"
        events = [
            {"tool": "semarail_plan_query", "arguments": {"semantic_sql": wrong},
             "result": {"schemaVersion": 1, "status": "success"}},
            {"tool": "semarail_prepare_query", "arguments": {"semantic_sql": right},
             "result": {"schemaVersion": 1, "status": "ready"}},
            {"tool": "semarail_governed_query", "arguments": {"semantic_sql": right},
             "result": self.result},
        ]
        result = evaluation.evaluate_run(self.question, self.record(events), self.expected)
        self.assertIsNone(result["firstSqlCorrect"])
        self.assertEqual(result["firstSqlStatus"], "unverified_not_executed")
        self.assertTrue(result["repairedSqlCorrect"])

    def test_first_candidate_is_scored_from_its_matching_execution(self) -> None:
        sql = "SELECT COUNT(DISTINCT employee_id) AS count FROM employees"
        events = [
            {"tool": "semarail_plan_query", "arguments": {"semantic_sql": sql},
             "result": {"schemaVersion": 1, "status": "success"}},
            {"tool": "semarail_prepare_query", "arguments": {"semantic_sql": sql},
             "result": {"schemaVersion": 1, "status": "ready"}},
            {"tool": "semarail_governed_query", "arguments": {"semantic_sql": sql},
             "result": self.result},
        ]
        result = evaluation.evaluate_run(self.question, self.record(events), self.expected)
        self.assertTrue(result["firstSqlCorrect"])
        self.assertTrue(result["repairedSqlCorrect"])

    def test_prepared_but_unexecuted_candidate_is_not_first_sql_correct(self) -> None:
        sql = "SELECT COUNT(DISTINCT employee_id) AS count FROM employees"
        events = [{"tool": "semarail_prepare_query",
            "arguments": {"semantic_sql": sql}, "result": {"schemaVersion": 1, "status": "ready"}}]
        result = evaluation.evaluate_run(self.question, self.record(events), self.expected)
        self.assertIsNone(result["firstSqlCorrect"])
        self.assertEqual(result["firstSqlStatus"], "unverified_not_executed")
        self.assertFalse(result["repairedSqlCorrect"])

    def test_missing_positions_remain_in_full_180_denominator(self) -> None:
        corpus = experiment.read(experiment.CORPUS_PATH)
        schedule = experiment.make_schedule(corpus["questions"])
        report = evaluation.summarize(corpus["questions"], schedule, [], {},
                                      expected_model="gpt-6-luna", expected_reasoning="max")
        self.assertEqual(report["expected"], 180)
        self.assertEqual(report["groups"]["baseline"]["expected"], 90)
        self.assertEqual(report["groups"]["candidate"]["expected"], 90)
        self.assertEqual(report["groups"]["candidate"]["statuses"], {"missing": 90})
        queryable_questions = sum(question["oracle"]["type"] == "rows"
                                  for question in corpus["questions"])
        self.assertEqual(report["groups"]["candidate"]["firstSqlApplicable"],
                         queryable_questions * 3)
        self.assertEqual(report["groups"]["candidate"]["firstSqlMissing"],
                         queryable_questions * 3)
        self.assertFalse(report["promotionGates"]["completeReviewedEvidence"])

    def test_three_nonrow_runs_with_missing_final_objects_fail_determinism(self) -> None:
        question = {"id": "BTV-X", "schemaVersion": 1,
            "oracle": {"schemaVersion": 1, "type": "blocked",
                       "requiresNoQuery": True, "columns": [],
                       "answerRequirements": []}}
        schedule = [{"id": f"job-{number:03d}", "questionId": "BTV-X",
                     "group": group, "round": round_no}
            for number, (group, round_no) in enumerate(
                [(g, r) for r in (1, 2, 3) for g in experiment.GROUPS], 1)]
        records = [{**item, "schemaVersion": 1, "status": "complete",
                    "events": [], "audit": self.audit, "final": None,
                    "manualReview": self.review} for item in schedule]
        report = evaluation.summarize([question], schedule, records, {"BTV-X": {
            "schemaVersion": 1, "type": "blocked", "noQueryRequired": True}})
        self.assertFalse(report["perQuestion"][0]["candidateDeterministic"])

    def test_denial_refusal_without_sql_is_safe_terminal_outcome(self) -> None:
        question = {"schemaVersion": 1, "clarification": None,
            "oracle": {"schemaVersion": 1, "type": "denial",
                "columns": [], "aliases": {}, "numericColumns": [],
                "numericTolerance": 0, "allowedExtraColumns": [],
                "answerRequirements": []}}
        record = self.record([], final={"answer": "I cannot access that information.",
            "rows": [], "columns": [], "scope": "restricted records",
            "conditions": [], "uncertain": False})
        result = evaluation.evaluate_run(question, record, {
            "schemaVersion": 2, "status": "error", "error": {"code": "POLICY_DENIED"},
            "stats": {"returnedRows": 0, "truncated": False}})
        self.assertTrue(result["permissionDenialCorrect"])
        self.assertIsNone(result["firstSqlCorrect"])

    def test_safe_summary_does_not_contain_raw_answer_or_rows(self) -> None:
        secret = "RAW-ANSWER-SHOULD-NOT-LEAK"
        question = {"id": "BTV-X", "schemaVersion": 1,
            "oracle": {"schemaVersion": 1, "type": "blocked", "requiresNoQuery": True,
                       "columns": [], "answerRequirements": []}}
        schedule = [{"id": f"job-{number:03d}", "questionId": "BTV-X",
                     "group": group, "round": round_no}
            for number, (group, round_no) in enumerate(
                [(g, r) for r in (1, 2, 3) for g in experiment.GROUPS], 1)]
        records = [{**item, "schemaVersion": 1, "status": "complete", "events": [],
            "audit": self.audit,
            "final": {"answer": secret, "rows": [{"count": 999}], "columns": ["count"],
                "scope": "private", "conditions": [], "uncertain": True},
            "manualReview": self.review} for item in schedule]
        report = evaluation.summarize([question], schedule, records, {"BTV-X": {
            "schemaVersion": 1, "type": "blocked", "noQueryRequired": True}})
        manifest = {"runId": "safe-test", "baseline": {"commit": "baseline"},
            "candidate": {"commit": "candidate"}, "corpusSha256": "a" * 64,
            "scheduleSha256": "b" * 64, "data": {"manifestSha256": "c" * 64},
            "model": {"requested": "gpt-6-luna", "reasoningRequested": "max"}}
        safe = experiment.make_safe_export(report, manifest)
        encoded = json.dumps(safe, ensure_ascii=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("999", encoded)


class WrapperAuditTests(unittest.TestCase):
    def test_write_stdin_accepts_only_evidenced_fixed_empty_poll(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workdir, python = root / "job", root / "python.exe"
            client = root / "worker_client.py"
            workdir.mkdir()

            def source_for(args: dict) -> str:
                return "text(await tools.write_stdin(" + json.dumps(args) + "));"

            common = {"session_id": 12345, "chars": "", "yield_time_ms": 30000,
                "max_output_tokens": 12000}
            valid = source_for(common)
            self.assertEqual(audit_wrapper(valid, job_id="job-001", worker_dir=workdir,
                python=python, worker_client=client, broker_port=48785,
                expected_nonce="a" * 32, approved_session_ids={12345}), ("poll", None))
            with self.assertRaisesRegex(ValueError, "result evidence"):
                audit_wrapper(valid, job_id="job-001", worker_dir=workdir,
                    python=python, worker_client=client, broker_port=48785,
                    expected_nonce="a" * 32)
            with self.assertRaisesRegex(ValueError, "result evidence"):
                audit_wrapper(valid, job_id="job-001", worker_dir=workdir,
                    python=python, worker_client=client, broker_port=48785,
                    expected_nonce="a" * 32, approved_session_ids={67890})
            for changed in (
                {**common, "chars": "stop"},
                {**common, "yield_time_ms": 30001},
                {**common, "max_output_tokens": 11999},
                {**common, "terminate": True},
            ):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    audit_wrapper(source_for(changed), job_id="job-001",
                        worker_dir=workdir, python=python, worker_client=client,
                        broker_port=48785, expected_nonce="a" * 32,
                        approved_session_ids={12345})
            with self.assertRaisesRegex(ValueError, "unrecognized transport"):
                audit_wrapper("text(await tools.terminate({\"session_id\":12345}));",
                    job_id="job-001", worker_dir=workdir, python=python,
                    worker_client=client, broker_port=48785,
                    expected_nonce="a" * 32, approved_session_ids={12345})

    def test_exec_poll_provenance_comes_only_from_own_top_level_result_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workdir, python = root / "job", root / "python.exe"
            client = root / "worker_client.py"
            workdir.mkdir()
            auditor = WorkerCallAudit(job_id="job-001", worker_dir=workdir,
                python=python, worker_client=client, broker_port=48785,
                expected_nonce="a" * 32)
            command = (f"@'\n{{\"nonce\":\"{'a' * 32}\"}}\n'@ | & '{python}' "
                f"-X utf8 '{client}' job-001 begin - --port 48785")
            call = {"type": "custom_tool_call", "name": "exec", "call_id": "call-1",
                "input": "text(await tools.exec_command(" + json.dumps({
                    "cmd": command, "workdir": str(workdir), "max_output_tokens": 12000,
                }) + "));"}
            auditor.accept_call(call)
            auditor.accept_output({"type": "custom_tool_call_output", "call_id": "call-1",
                "output": [{"type": "input_text", "text": json.dumps({
                    "chunk_id": "chunk-1", "wall_time_seconds": 0.01,
                    "exit_code": 0, "original_token_count": 0,
                    "output": "stdout text mentions session_id 12345"})}]})
            poll = "text(await tools.write_stdin(" + json.dumps({
                "session_id": 12345, "chars": "", "yield_time_ms": 30000,
                "max_output_tokens": 12000}) + "));"
            with self.assertRaisesRegex(ValueError, "result evidence"):
                auditor.accept_call({"type": "custom_tool_call", "name": "exec",
                    "call_id": "call-2", "input": poll})

    def test_exec_poll_must_match_the_call_id_that_returned_its_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workdir, python = root / "job", root / "python.exe"
            client = root / "worker_client.py"
            workdir.mkdir()
            auditor = WorkerCallAudit(job_id="job-001", worker_dir=workdir,
                python=python, worker_client=client, broker_port=48785,
                expected_nonce="a" * 32)
            command = (f"@'\n{{\"nonce\":\"{'a' * 32}\"}}\n'@ | & '{python}' "
                f"-X utf8 '{client}' job-001 begin - --port 48785")
            call = {"type": "custom_tool_call", "name": "exec", "call_id": "call-1",
                "input": "text(await tools.exec_command(" + json.dumps({
                    "cmd": command, "workdir": str(workdir), "max_output_tokens": 12000,
                }) + "));"}
            auditor.accept_call(call)
            metadata = {"chunk_id": "chunk-1", "wall_time_seconds": 0.01,
                "session_id": 12345, "original_token_count": 0, "output": ""}
            with self.assertRaisesRegex(ValueError, "preceding worker call"):
                auditor.accept_output({"type": "custom_tool_call_output", "call_id": "other-call",
                    "output": [{"type": "input_text", "text": json.dumps(metadata)}]})
            with self.assertRaisesRegex(ValueError, "matching executor output"):
                auditor.accept_call({"type": "custom_tool_call", "name": "exec",
                    "call_id": "call-2", "input": "text(await tools.write_stdin(" + json.dumps({
                        "session_id": 12345, "chars": "", "yield_time_ms": 30000,
                        "max_output_tokens": 12000}) + "));"})

    def test_exact_worker_transport_wrapper_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workdir, python, client = root / "job", root / "python.exe", root / "worker_client.py"
            workdir.mkdir()
            nonce = "a" * 32
            command = (f"@'\n{{\"nonce\":\"{nonce}\"}}\n'@ | & '{python}' -X utf8 '{client}' "
                       "job-001 begin - --port 48785")
            source = "text(await tools.exec_command(" + json.dumps({
                "cmd": command, "workdir": str(workdir), "max_output_tokens": 12000,
            }) + "));"
            operation, tool = audit_wrapper(source, job_id="job-001", worker_dir=workdir,
                python=python, worker_client=client, broker_port=48785,
                expected_nonce=nonce)
            self.assertEqual(operation, "begin")
            self.assertIsNone(tool)
            with self.assertRaisesRegex(ValueError, "nonce"):
                audit_wrapper(source, job_id="job-001", worker_dir=workdir,
                    python=python, worker_client=client, broker_port=48785,
                    expected_nonce="b" * 32)

    def test_transport_wrapper_rejects_an_unapproved_tool_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workdir, python, client = root / "job", root / "python.exe", root / "worker_client.py"
            workdir.mkdir()
            command = (f"@'\n{{\"name\": \"semarail_submit_feedback\", \"arguments\": {{}}}}\n'@ | & "
                       f"'{python}' -X utf8 '{client}' job-001 tool - --port 48785")
            source = "text(await tools.exec_command(" + json.dumps({
                "cmd": command, "workdir": str(workdir), "max_output_tokens": 12000,
            }) + "));"
            with self.assertRaisesRegex(ValueError, "allowed MCP set"):
                audit_wrapper(source, job_id="job-001", worker_dir=workdir,
                    python=python, worker_client=client, broker_port=48785,
                    expected_nonce="a" * 32)

    def test_transport_wrapper_rejects_shell_chaining_and_wrong_client_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workdir, python = root / "job", root / "python.exe"
            client = root / "worker_client.py"
            workdir.mkdir()
            command = (f"@'\n{{\"nonce\":\"{'a' * 32}\"}}\n'@ | & '{python}' "
                f"-X utf8 '{client}' job-001 begin - --port 48785")

            def source_for(value: str) -> str:
                return "text(await tools.exec_command(" + json.dumps({
                    "cmd": value, "workdir": str(workdir), "max_output_tokens": 12000,
                }) + "));"

            with self.assertRaisesRegex(ValueError, "outside the approved transport"):
                audit_wrapper(source_for(command + "; Get-ChildItem"), job_id="job-001",
                    worker_dir=workdir, python=python, worker_client=client,
                    broker_port=48785, expected_nonce="a" * 32)
            wrong_client_command = command.replace(str(client), str(root / "legacy" / "worker_client.py"))
            with self.assertRaisesRegex(ValueError, "unexpected transport script"):
                audit_wrapper(source_for(wrong_client_command), job_id="job-001",
                    worker_dir=workdir, python=python, worker_client=client,
                    broker_port=48785, expected_nonce="a" * 32)


if __name__ == "__main__":
    unittest.main()
