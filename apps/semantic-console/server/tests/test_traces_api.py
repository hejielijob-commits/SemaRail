from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.access_control import AccessControlStore
from server.authorization import PolicyEngine
from server.diagnostics import DiagnosticStore
from server.diagnostics_api import DiagnosticsApi
from server.app import SemanticConsoleHTTPServer


class TraceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-traces-")
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
        self.bootstrap = "bootstrap-token-that-is-at-least-thirty-two-characters"
        self.access = AccessControlStore(
            Path(self.temp.name) / "control.sqlite3", bootstrap_token=self.bootstrap,
            clock=lambda: self.now,
        )
        self.store = DiagnosticStore(self.access)
        self.api = DiagnosticsApi(self.store, self.access, PolicyEngine(), project_id="project-a")
        subject = self.access.create_service_account("Codex Hook")
        policy = self.access.create_policy(
            "Trace writer", {"schemaVersion": 1, "projects": ["project-a"], "tools": ["trace:write"]},
        )
        self.access.bind_policy(subject.id, policy["id"])
        key = self.access.issue_api_key(subject.id)
        self.token = f"Bearer {key['apiKey']}"
        self.auth = self.access.authenticate(self.token)

    def _post(self, events: list[dict], **overrides: object) -> tuple[int, dict]:
        payload = {
            "schemaVersion": 1, "source": "codex", "sourceSessionId": "session-1",
            "sourceTurnId": "turn-1", "events": events,
        }
        payload.update(overrides)
        return self.api.dispatch("POST", "/api/v1/traces/events", {}, payload, self.token)

    def test_out_of_order_retry_and_admin_read(self) -> None:
        later = {"eventId": "e-2", "occurredAt": "2026-09-30T08:00:02Z", "type": "turn_completed", "status": "success"}
        earlier = {"eventId": "e-1", "occurredAt": "2026-09-30T08:00:01Z", "type": "turn_started", "model": "gpt-6"}
        status, first = self._post([later])
        retry_status, retry = self._post([later, earlier])
        denied_status, denied = self.api.dispatch("GET", "/api/v1/traces", {}, None, self.token)
        list_status, listing = self.api.dispatch("GET", "/api/v1/traces", {}, None, f"Bearer {self.bootstrap}")
        detail_status, detail = self.api.dispatch("GET", f"/api/v1/traces/{first['id']}", {}, None, f"Bearer {self.bootstrap}")

        self.assertEqual((status, first["accepted"], first["duplicates"]), (201, 1, 0))
        self.assertEqual((retry_status, retry["id"], retry["accepted"], retry["duplicates"]), (201, first["id"], 1, 1))
        self.assertEqual((denied_status, denied["code"]), (403, "FORBIDDEN"))
        self.assertEqual(list_status, 200)
        self.assertEqual(listing["items"][0]["status"], "success")
        self.assertEqual(listing["items"][0]["model"], "gpt-6")
        self.assertIsNone(listing["items"][0]["tokenUsage"])
        self.assertEqual(detail_status, 200)
        self.assertEqual([item["eventId"] for item in detail["events"]], ["e-1", "e-2"])
        turn_span = next(span for span in detail["spans"] if span["id"] == "turn")
        self.assertEqual((turn_span["startedAt"], turn_span["endedAt"], turn_span["status"]),
                         ("2026-09-30T08:00:01Z", "2026-09-30T08:00:02Z", "success"))
        reopened = DiagnosticStore(self.access)
        self.assertEqual(
            reopened.trace_detail(first["id"], organization_id=self.auth.subject.organization_id,
                                  project_id="project-a")["eventCount"], 2,
        )
        late_terminal = {
            "eventId": "e-0", "occurredAt": "2026-09-30T08:00:01.500Z",
            "type": "turn_interrupted", "status": "cancelled",
        }
        self._post([late_terminal])
        _, detail_after_late_terminal = self.api.dispatch(
            "GET", f"/api/v1/traces/{first['id']}", {}, None, f"Bearer {self.bootstrap}",
        )
        turn_span = next(span for span in detail_after_late_terminal["spans"] if span["id"] == "turn")
        self.assertEqual((turn_span["endedAt"], turn_span["status"]), ("2026-09-30T08:00:02Z", "success"))

    def test_equal_time_terminal_events_use_event_id_order(self) -> None:
        later_id = {
            "eventId": "z-terminal", "occurredAt": "2026-09-30T08:00:02Z",
            "type": "turn_interrupted", "status": "cancelled",
        }
        earlier_id = {
            "eventId": "a-terminal", "occurredAt": "2026-09-30T08:00:02Z",
            "type": "turn_completed", "status": "success",
        }
        _, accepted = self._post([later_id])
        self._post([earlier_id])
        _, detail = self.api.dispatch(
            "GET", f"/api/v1/traces/{accepted['id']}", {}, None, f"Bearer {self.bootstrap}",
        )
        turn_span = next(span for span in detail["spans"] if span["id"] == "turn")
        self.assertEqual(turn_span["status"], "cancelled")
        self.assertEqual(detail["status"], "cancelled")

    def test_statusless_subagent_stop_keeps_unknown_outcome(self) -> None:
        _, accepted = self._post([{
            "eventId": "subagent-stop", "occurredAt": "2026-09-30T08:00:03Z",
            "type": "subagent_completed", "agentId": "agent-1",
        }])
        _, detail = self.api.dispatch(
            "GET", f"/api/v1/traces/{accepted['id']}", {}, None, f"Bearer {self.bootstrap}",
        )
        span = next(span for span in detail["spans"] if span["id"] == "agent:agent-1")
        self.assertEqual((span["endedAt"], span["status"]), ("2026-09-30T08:00:03Z", "unknown"))

    def test_child_model_does_not_replace_turn_model(self) -> None:
        _, accepted = self._post([
            {"eventId": "start", "occurredAt": "2026-09-30T08:00:00Z", "type": "turn_started", "model": "main-model"},
            {"eventId": "child", "occurredAt": "2026-09-30T08:00:01Z", "type": "subagent_started", "agentId": "child-1", "model": "child-model", "tokenUsage": {"input": 5}},
        ])
        detail = self.store.trace_detail(accepted["id"], organization_id=self.auth.subject.organization_id, project_id="project-a")
        self.assertEqual(detail["model"], "main-model")
        self.assertIsNone(detail["tokenUsage"])

    def test_semantically_identical_token_usage_retry_is_idempotent(self) -> None:
        first_event = {
            "eventId": "usage", "occurredAt": "2026-09-30T08:00:00Z", "type": "turn_started",
            "tokenUsage": {"input": 1, "output": 2, "total": 3},
        }
        retry_event = {
            "eventId": "usage", "occurredAt": "2026-09-30T08:00:00+00:00", "type": "turn_started",
            "tokenUsage": {"total": 3, "output": 2, "input": 1},
        }
        _, first = self._post([first_event])
        status, retry = self._post([retry_event])
        self.assertEqual((status, retry["id"], retry["accepted"], retry["duplicates"]),
                         (201, first["id"], 0, 1))

    def test_rejects_payload_content_and_conflicting_event_id(self) -> None:
        event = {"eventId": "e-1", "occurredAt": "2026-09-30T08:00:00Z", "type": "output"}
        invalid_status, invalid = self._post([{**event, "text": "secret output"}])
        self.assertEqual((invalid_status, invalid["code"]), (400, "INVALID_TRACE"))
        self._post([event])
        conflict_status, conflict = self._post([{**event, "type": "tool_started", "toolUseId": "tool-1"}])
        self.assertEqual((conflict_status, conflict["code"]), (409, "TRACE_EVENT_CONFLICT"))

    def test_core_link_requires_same_subject_and_project(self) -> None:
        owned_diagnostic_id = self.store.record_execution(
            auth=self.auth, project_id="project-a", trace_id="core-owned", query_id="query-owned",
            datasource_id="warehouse", transport="core-http", method="query.run",
            status="failure", stage="execution", error={"reasonCode": "TEST_FAILURE"},
            phase_spans=[
                {"name": "authentication", "status": "success", "durationMs": 1.0},
                {"name": "policy", "status": "success", "durationMs": 2.0},
                {"name": "runtime", "status": "failure", "durationMs": 3.0},
            ],
        )
        second_diagnostic_id = self.store.record_execution(
            auth=self.auth, project_id="project-a", trace_id="core-second", query_id="query-second",
            datasource_id="warehouse", transport="core-http", method="query.run",
            status="failure", stage="execution", error={"reasonCode": "SECOND_FAILURE"},
        )
        with self.access._connect() as connection:
            expected_core_issues = {
                "core-owned": [row["id"] for row in connection.execute(
                    "SELECT id FROM query_feedback WHERE diagnostic_id=? ORDER BY created_at,id",
                    (owned_diagnostic_id,),
                ).fetchall()],
                "core-second": [row["id"] for row in connection.execute(
                    "SELECT id FROM query_feedback WHERE diagnostic_id=? ORDER BY created_at,id",
                    (second_diagnostic_id,),
                ).fetchall()],
            }
        other = self.access.create_service_account("Other")
        other_key = self.access.issue_api_key(other.id)
        other_auth = self.access.authenticate(f"Bearer {other_key['apiKey']}")
        self.store.record_execution(
            auth=other_auth, project_id="project-a", trace_id="core-foreign", query_id="query-foreign",
            datasource_id="warehouse", transport="core-http", method="query.run",
            status="success", stage="complete",
        )
        event = {"eventId": "e-1", "occurredAt": "2026-09-30T08:00:00Z", "type": "tool_completed", "toolUseId": "tool-1"}
        forbidden_status, forbidden = self._post([{**event, "coreTraceId": "core-foreign"}])
        self.assertEqual((forbidden_status, forbidden["code"]), (403, "TRACE_LINK_FORBIDDEN"))
        status, accepted = self._post([
            {**event, "coreTraceId": "core-owned"},
            {**event, "eventId": "e-2", "toolUseId": "tool-2", "coreTraceId": "core-second"},
        ])
        self.assertEqual(status, 201)
        _, detail = self.api.dispatch("GET", f"/api/v1/traces/{accepted['id']}", {}, None, f"Bearer {self.bootstrap}")
        self.assertEqual(detail["coreTraceIds"], ["core-owned", "core-second"])
        self.assertEqual(detail["issueCount"], 2)
        self.assertEqual(detail["events"][0]["coreTraceId"], "core-owned")
        self.assertEqual([span["name"] for span in detail["coreDiagnostics"][0]["phaseSpans"]], ["authentication", "policy", "runtime"])
        self.assertEqual(
            {item["traceId"]: item["issueIds"] for item in detail["coreDiagnostics"]},
            expected_core_issues,
        )
        self.assertEqual(detail["issueIds"], expected_core_issues["core-owned"] + expected_core_issues["core-second"])
        lookup_status, lookup = self.api.dispatch(
            "GET", "/api/v1/traces/by-core/core-owned", {}, None, f"Bearer {self.bootstrap}",
        )
        self.assertEqual((lookup_status, lookup["id"]), (200, accepted["id"]))
        foreign_status, foreign = self.api.dispatch(
            "GET", "/api/v1/traces/by-core/core-foreign", {}, None, f"Bearer {self.bootstrap}",
        )
        self.assertEqual((foreign_status, foreign["code"]), (404, "TRACE_NOT_FOUND"))
        duplicate_status, duplicate = self._post(
            [{**event, "eventId": "e-2", "toolUseId": "tool-2", "coreTraceId": "core-owned"}],
            sourceTurnId="turn-2",
        )
        self.assertEqual((duplicate_status, duplicate["code"]), (409, "TRACE_LINK_CONFLICT"))

    def test_legacy_ambiguous_core_claims_return_conflict_and_stay_hidden(self) -> None:
        self.store.record_execution(
            auth=self.auth, project_id="project-a", trace_id="core-shared", query_id="query-shared",
            datasource_id="warehouse", transport="core-http", method="query.run",
            status="success", stage="complete",
        )
        event_one = {
            "eventId": "e-1", "occurredAt": "2026-09-30T08:00:00Z", "type": "tool_completed",
            "toolUseId": "tool-1", "coreTraceId": "core-shared",
        }
        event_two = {
            "eventId": "e-2", "occurredAt": "2026-09-30T08:00:01Z", "type": "tool_completed",
            "toolUseId": "tool-2",
        }
        _, first = self._post([event_one])
        _, second = self._post([event_two], sourceTurnId="turn-2")
        with self.access._connect() as connection:
            connection.execute(
                "INSERT INTO agent_trace_core_links(trace_id,event_id,core_trace_id) VALUES(?,?,?)",
                (second["id"], "e-2", "core-shared"),
            )
            connection.execute(
                "UPDATE agent_trace_core_claims SET trace_id=NULL,status='ambiguous' WHERE core_trace_id=?",
                ("core-shared",),
            )
        first_status, first_detail = self.api.dispatch(
            "GET", f"/api/v1/traces/{first['id']}", {}, None, f"Bearer {self.bootstrap}",
        )
        lookup_status, lookup = self.api.dispatch(
            "GET", "/api/v1/traces/by-core/core-shared", {}, None, f"Bearer {self.bootstrap}",
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(first_detail["coreTraceIds"], [])
        self.assertNotIn("coreTraceId", first_detail["events"][0])
        self.assertEqual((lookup_status, lookup["code"]), (409, "TRACE_LINK_AMBIGUOUS"))

    def test_pending_core_link_only_appears_after_owned_diagnostic(self) -> None:
        event = {"eventId": "e-1", "occurredAt": "2026-09-30T08:00:00Z", "type": "tool_completed", "toolUseId": "tool-1", "coreTraceId": "core-late"}
        _, accepted = self._post([event])
        admin = f"Bearer {self.bootstrap}"
        _, before = self.api.dispatch("GET", f"/api/v1/traces/{accepted['id']}", {}, None, admin)
        self.assertNotIn("coreTraceId", before["events"][0])
        self.store.record_execution(
            auth=self.auth, project_id="project-a", trace_id="core-late", query_id="query-late",
            datasource_id="warehouse", transport="core-http", method="query.run",
            status="success", stage="complete",
        )
        _, after = self.api.dispatch("GET", f"/api/v1/traces/{accepted['id']}", {}, None, admin)
        self.assertEqual(after["coreTraceIds"], ["core-late"])
        lookup_status, lookup = self.api.dispatch(
            "GET", "/api/v1/traces/by-core/core-late", {}, None, admin,
        )
        self.assertEqual((lookup_status, lookup["id"]), (200, accepted["id"]))

    def test_list_pagination_and_project_scope(self) -> None:
        event = {"eventId": "e-1", "occurredAt": "2026-09-30T08:00:00Z", "type": "turn_started"}
        _, first = self._post([event], sourceTurnId="turn-a")
        _, second = self._post([event], sourceTurnId="turn-b")
        foreign = self.store.record_trace_events(
            auth=self.auth, project_id="project-b", source_session_id="session-1",
            source_turn_id="turn-c", events=[event],
        )
        with self.access._connect() as connection:
            connection.execute(
                "INSERT INTO organizations(id,name,created_at) VALUES(?,?,?)",
                ("org-foreign", "Foreign organization", "2026-09-01T00:00:00Z"),
            )
        foreign_subject = self.access.create_service_account("Foreign writer", organization_id="org-foreign")
        foreign_key = self.access.issue_api_key(foreign_subject.id)
        foreign_auth = self.access.authenticate(f"Bearer {foreign_key['apiKey']}")
        foreign_org_trace = self.store.record_trace_events(
            auth=foreign_auth, project_id="project-a", source_session_id="session-1",
            source_turn_id="turn-foreign-org", events=[event],
        )
        admin = f"Bearer {self.bootstrap}"
        _, page = self.api.dispatch("GET", "/api/v1/traces", {"limit": "1"}, None, admin)
        _, next_page = self.api.dispatch(
            "GET", "/api/v1/traces", {"limit": "1", "cursor": page["nextCursor"]}, None, admin,
        )
        self.assertEqual({page["items"][0]["id"], next_page["items"][0]["id"]}, {first["id"], second["id"]})
        self.assertIsNone(next_page["nextCursor"])
        admin = f"Bearer {self.bootstrap}"
        diagnosis_status, diagnosis = self.api.dispatch(
            "GET", f"/api/v1/traces/{foreign['id']}/diagnosis", {}, None, admin,
        )
        compare_status, comparison = self.api.dispatch(
            "GET", "/api/v1/traces/compare",
            {"leftTraceId": first["id"], "rightTraceId": foreign["id"]}, None, admin,
        )
        foreign_org_status, foreign_org = self.api.dispatch(
            "GET", f"/api/v1/traces/{foreign_org_trace['id']}", {}, None, admin,
        )
        self.assertEqual((diagnosis_status, diagnosis["code"]), (404, "TRACE_NOT_FOUND"))
        self.assertEqual((compare_status, comparison["code"]), (404, "TRACE_NOT_FOUND"))
        self.assertEqual((foreign_org_status, foreign_org["code"]), (404, "TRACE_NOT_FOUND"))

    def test_trace_list_filters_are_scoped_and_use_persisted_issue_evidence(self) -> None:
        admin = f"Bearer {self.bootstrap}"
        started = "2026-09-30T08:00:00Z"
        self._post([
            {"eventId": "success-start", "occurredAt": started, "type": "turn_started"},
            {"eventId": "success-end", "occurredAt": "2026-09-30T08:00:01Z", "type": "turn_completed", "status": "success"},
        ], sourceSessionId="session-success", sourceTurnId="turn-success")
        self.store.record_execution(
            auth=self.auth, project_id="project-a", trace_id="core-filtered", query_id="query-filtered",
            datasource_id="warehouse", transport="core-http", method="query.run",
            status="failure", stage="datasource", error={"code": "DATASOURCE_UNAVAILABLE"},
        )
        _, failed = self._post([
            {"eventId": "failed-start", "occurredAt": started, "type": "turn_started"},
            {"eventId": "failed-end", "occurredAt": "2026-09-30T08:00:02Z", "type": "turn_completed", "status": "failure"},
            {"eventId": "failed-tool", "occurredAt": "2026-09-30T08:00:01Z", "type": "tool_completed", "toolUseId": "tool-failed", "coreTraceId": "core-filtered"},
        ], sourceSessionId="session-failure", sourceTurnId="turn-failure")

        status, filtered = self.api.dispatch(
            "GET", "/api/v1/traces",
            {
                "createdAfter": started, "createdBefore": started, "sourceSessionId": "session-failure",
                "subjectId": self.auth.subject.id, "status": "failure", "hasIssues": "true",
            },
            None, admin,
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in filtered["items"]], [failed["id"]])
        self.assertEqual(filtered["items"][0]["issueCount"], 1)

        no_issues_status, no_issues = self.api.dispatch(
            "GET", "/api/v1/traces", {"hasIssues": "false", "sourceSessionId": "session-success"}, None, admin,
        )
        self.assertEqual(no_issues_status, 200)
        self.assertEqual([item["sourceSessionId"] for item in no_issues["items"]], ["session-success"])

        invalid_status, invalid = self.api.dispatch(
            "GET", "/api/v1/traces", {"hasIssues": "yes"}, None, admin,
        )
        self.assertEqual((invalid_status, invalid["code"]), (400, "INVALID_FILTER"))

    def test_diagnosis_uses_only_safe_verified_core_metadata_and_marks_gaps_incomplete(self) -> None:
        self.store.record_execution(
            auth=self.auth, project_id="project-a", trace_id="core-diagnosis", query_id="query-diagnosis",
            datasource_id="warehouse", transport="core-http", method="query.run",
            status="failure", stage="sql_validation", semantic_version="semantic-v4",
            policy_versions=["policy-a:3"],
            error={
                "code": "SQL_VALIDATION_FAILED", "reasonCode": "UNSAFE_QUERY",
                "phase": "sqlValidation", "message": "private message sentinel",
                "body": "private body sentinel",
            },
            phase_spans=[{"name": "runtime", "status": "success", "durationMs": 5_001}],
        )
        _, accepted = self._post([
            {"eventId": "diag-start", "occurredAt": "2026-09-30T08:00:00Z", "type": "turn_started"},
            {"eventId": "diag-tool", "occurredAt": "2026-09-30T08:00:01Z", "type": "tool_completed", "toolUseId": "diag-tool-use", "coreTraceId": "core-diagnosis"},
            {"eventId": "diag-end", "occurredAt": "2026-09-30T08:00:02Z", "type": "turn_completed", "status": "success"},
        ], sourceTurnId="turn-diagnosis")
        admin = f"Bearer {self.bootstrap}"
        detail_status, detail = self.api.dispatch(
            "GET", f"/api/v1/traces/{accepted['id']}", {}, None, admin,
        )
        diagnosis_status, diagnosis = self.api.dispatch(
            "GET", f"/api/v1/traces/{accepted['id']}/diagnosis", {}, None, admin,
        )
        self.assertEqual((detail_status, diagnosis_status), (200, 200))
        core = detail["coreDiagnostics"][0]
        self.assertEqual(
            (core["code"], core["reasonCode"], core["phase"], core["semanticVersion"], core["policyVersions"]),
            ("SQL_VALIDATION_FAILED", "UNSAFE_QUERY", "sqlValidation", "semantic-v4", ["policy-a:3"]),
        )
        self.assertNotIn("private message sentinel", repr(detail))
        self.assertNotIn("private body sentinel", repr(detail))
        self.assertEqual(diagnosis["status"], "issues")
        sql_failure = next(item for item in diagnosis["findings"] if item["code"] == "SQL_VALIDATION_FAILED")
        slow = next(item for item in diagnosis["findings"] if item["code"] == "SLOW_CORE_PHASE")
        self.assertEqual(sql_failure["category"], "sqlValidation")
        self.assertEqual(sql_failure["evidenceIds"], ["core-diagnosis"] + core["issueIds"])
        self.assertEqual((slow["severity"], slow["phase"]), ("info", "runtime"))
        self.assertNotIn("private message sentinel", repr(diagnosis))

        _, incomplete_trace = self._post([
            {"eventId": "gap-start", "occurredAt": "2026-09-30T08:00:00Z", "type": "turn_started"},
            {"eventId": "gap-tool", "occurredAt": "2026-09-30T08:00:01Z", "type": "tool_completed", "toolUseId": "gap-tool-use"},
        ], sourceTurnId="turn-incomplete")
        incomplete_status, incomplete = self.api.dispatch(
            "GET", f"/api/v1/traces/{incomplete_trace['id']}/diagnosis", {}, None, admin,
        )
        self.assertEqual((incomplete_status, incomplete["status"]), (200, "incomplete"))
        self.assertEqual({item["severity"] for item in incomplete["findings"]}, {"info"})
        self.assertEqual(
            {item["code"] for item in incomplete["findings"]},
            {"MISSING_TURN_END", "MISSING_TOOL_START"},
        )

    def test_core_error_phases_remain_visible_without_error_bodies(self) -> None:
        for phase in ("database", "authorization", "validation", "cancel"):
            with self.subTest(phase=phase):
                core_id = f"core-phase-{phase}"
                self.store.record_execution(
                    auth=self.auth, project_id="project-a", trace_id=core_id,
                    query_id=f"query-phase-{phase}", datasource_id="warehouse",
                    transport="core-http", method="query.run", status="failure",
                    stage=phase, error={"code": "DATABASE_ERROR", "phase": phase,
                                        "message": "private phase detail sentinel"},
                )
                _, accepted = self._post([
                    {"eventId": f"phase-event-{phase}", "occurredAt": "2026-09-30T08:00:01Z",
                     "type": "tool_completed", "toolUseId": f"phase-tool-{phase}", "coreTraceId": core_id},
                ], sourceTurnId=f"turn-phase-{phase}")
                for path in (f"/api/v1/traces/{accepted['id']}", f"/api/v1/diagnostics/core/{core_id}"):
                    status, detail = self.api.dispatch("GET", path, {}, None, f"Bearer {self.bootstrap}")
                    self.assertEqual(status, 200)
                    metadata = detail["coreDiagnostics"][0] if "coreDiagnostics" in detail else detail
                    self.assertEqual(metadata["phase"], phase)
                    self.assertNotIn("private phase detail sentinel", repr(detail))

    def test_diagnosis_distinguishes_non_core_tools_and_late_span_events(self) -> None:
        from server.trace_analysis import diagnose_trace
        events = [
            {"eventId": "start", "type": "turn_started"},
            {"eventId": "tool-start", "type": "tool_started", "toolUseId": "shell", "toolName": "exec_command"},
            {"eventId": "tool-end", "type": "tool_completed", "toolUseId": "shell", "status": "success"},
            {"eventId": "agent-start", "type": "subagent_started", "agentId": "child"},
            {"eventId": "end", "type": "turn_completed", "status": "success"},
        ]
        trace = {"id": "sample", "events": events, "coreDiagnostics": []}
        diagnosis = diagnose_trace(trace)
        self.assertEqual({item["code"] for item in diagnosis["findings"]}, {"MISSING_AGENT_END"})
        self.assertEqual(diagnosis["status"], "incomplete")
        events.append({"eventId": "agent-end", "type": "subagent_completed", "agentId": "child", "status": "success"})
        self.assertEqual(diagnose_trace(trace)["status"], "healthy")
        events[2]["coreTraceId"] = "expired-core"
        self.assertEqual({item["code"] for item in diagnose_trace(trace)["findings"]}, {"MISSING_CORE_EVIDENCE"})
        trace["coreDiagnostics"] = [{"traceId": "expired-core", "status": "failure", "stage": "runtime", "reasonCode": "CONNECTION_FAILED"}]
        failure = next(item for item in diagnose_trace(trace)["findings"] if item["severity"] == "error")
        self.assertEqual(failure["category"], "datasource")

    def test_comparison_does_not_infer_version_equality_from_missing_metadata(self) -> None:
        from server.trace_analysis import compare_traces
        empty = {"id": "empty", "events": [], "spans": [], "coreDiagnostics": []}
        present = {**empty, "id": "present", "model": "model-a", "coreDiagnostics": [
            {"semanticVersion": "revision-a", "policyVersions": ["policy-a:1"]},
            {"semanticVersion": "revision-b", "policyVersions": ["policy-a:1"]},
        ]}
        missing = compare_traces(empty, present)
        self.assertEqual(missing["versionChanges"], {"model": None, "semanticVersion": None, "policyVersions": None})
        self.assertEqual(missing["right"]["semanticVersions"], ["revision-a", "revision-b"])
        self.assertEqual(compare_traces(present, {**present, "id": "other"})["versionChanges"],
                         {"model": False, "semanticVersion": False, "policyVersions": False})

    def test_trace_comparison_aligns_structural_tool_identity_and_reports_versions(self) -> None:
        admin = f"Bearer {self.bootstrap}"
        self.store.record_execution(
            auth=self.auth, project_id="project-a", trace_id="core-left", query_id="query-left",
            datasource_id="warehouse", transport="core-http", method="query.run",
            status="success", stage="complete", semantic_version="semantic-v1",
            policy_versions=["policy-a:1"],
            phase_spans=[{"name": "runtime", "status": "success", "durationMs": 1_000}],
        )
        self.store.record_execution(
            auth=self.auth, project_id="project-a", trace_id="core-right", query_id="query-right",
            datasource_id="warehouse", transport="core-http", method="query.run",
            status="success", stage="complete", semantic_version="semantic-v2",
            policy_versions=["policy-a:2"],
            phase_spans=[{"name": "runtime", "status": "success", "durationMs": 1_800}],
        )
        left_events = [
            {"eventId": "l-turn-start", "occurredAt": "2026-09-30T08:00:00Z", "type": "turn_started", "model": "model-a"},
            {"eventId": "l-agent-start", "occurredAt": "2026-09-30T08:00:01Z", "type": "subagent_started", "agentId": "left-agent"},
            {"eventId": "l-tool-start", "occurredAt": "2026-09-30T08:00:10Z", "type": "tool_started", "toolUseId": "left-use", "toolName": "wren.query", "parentAgentId": "left-agent"},
            {"eventId": "l-tool-end", "occurredAt": "2026-09-30T08:00:11Z", "type": "tool_completed", "toolUseId": "left-use", "toolName": "wren.query", "parentAgentId": "left-agent", "status": "success", "coreTraceId": "core-left"},
            {"eventId": "l-agent-end", "occurredAt": "2026-09-30T08:00:12Z", "type": "subagent_completed", "agentId": "left-agent", "status": "success"},
            {"eventId": "l-turn-end", "occurredAt": "2026-09-30T08:00:13Z", "type": "turn_completed", "status": "success"},
        ]
        right_events = [
            {"eventId": "r-turn-start", "occurredAt": "2026-09-30T08:01:00Z", "type": "turn_started", "model": "model-b"},
            {"eventId": "r-agent-start", "occurredAt": "2026-09-30T08:01:01Z", "type": "subagent_started", "agentId": "right-agent"},
            {"eventId": "r-tool-start", "occurredAt": "2026-09-30T08:01:10Z", "type": "tool_started", "toolUseId": "right-use", "toolName": "wren.query", "parentAgentId": "right-agent"},
            {"eventId": "r-tool-end", "occurredAt": "2026-09-30T08:01:12Z", "type": "tool_completed", "toolUseId": "right-use", "toolName": "wren.query", "parentAgentId": "right-agent", "status": "failure", "coreTraceId": "core-right"},
            {"eventId": "r-other-start", "occurredAt": "2026-09-30T08:01:13Z", "type": "tool_started", "toolUseId": "right-other", "toolName": "wren.describe"},
            {"eventId": "r-other-end", "occurredAt": "2026-09-30T08:01:14Z", "type": "tool_completed", "toolUseId": "right-other", "toolName": "wren.describe", "status": "success"},
            {"eventId": "r-agent-end", "occurredAt": "2026-09-30T08:01:15Z", "type": "subagent_completed", "agentId": "right-agent", "status": "success"},
            {"eventId": "r-turn-end", "occurredAt": "2026-09-30T08:01:16Z", "type": "turn_completed", "status": "success"},
        ]
        _, left = self._post(left_events, sourceTurnId="turn-left")
        _, right = self._post(right_events, sourceTurnId="turn-right")
        status, comparison = self.api.dispatch(
            "GET", "/api/v1/traces/compare",
            {"leftTraceId": left["id"], "rightTraceId": right["id"]}, None, admin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(comparison["versionChanges"], {
            "model": True, "semanticVersion": True, "policyVersions": True,
        })
        aligned_tool = next(item for item in comparison["aligned"] if item["kind"] == "tool")
        self.assertEqual((aligned_tool["toolName"], aligned_tool["agentPath"]), ("wren.query", ["agent#1"]))
        self.assertEqual((aligned_tool["left"]["status"], aligned_tool["right"]["status"]), ("success", "failure"))
        self.assertEqual(aligned_tool["durationDeltaMs"], 1_000)
        self.assertEqual(aligned_tool["phaseDurations"], [{
            "name": "runtime", "leftMs": 1_000.0, "rightMs": 1_800.0, "deltaMs": 800.0,
        }])
        self.assertEqual([item["toolName"] for item in comparison["rightOnly"]], ["wren.describe"])

    def test_trace_comparison_keeps_parallel_indistinguishable_agents_unmatched(self) -> None:
        def events(prefix: str, agent_order: list[str], minute: str) -> list[dict]:
            values = [{
                "eventId": f"{prefix}-turn-start", "occurredAt": f"2026-09-30T08:{minute}:00Z",
                "type": "turn_started",
            }]
            for offset, agent_id in enumerate(agent_order, start=1):
                values.append({
                    "eventId": f"{prefix}-{agent_id}-start",
                    "occurredAt": f"2026-09-30T08:{minute}:{offset:02}Z",
                    "type": "subagent_started", "agentId": agent_id,
                })
            for agent_id in agent_order:
                values.extend([
                    {
                        "eventId": f"{prefix}-{agent_id}-tool-start",
                        "occurredAt": f"2026-09-30T08:{minute}:02Z", "type": "tool_started",
                        "toolUseId": f"{prefix}-{agent_id}-tool", "toolName": "wren.query",
                        "parentAgentId": agent_id,
                    },
                    {
                        "eventId": f"{prefix}-{agent_id}-tool-end",
                        "occurredAt": f"2026-09-30T08:{minute}:03Z", "type": "tool_completed",
                        "toolUseId": f"{prefix}-{agent_id}-tool", "toolName": "wren.query",
                        "parentAgentId": agent_id, "status": "success",
                    },
                ])
            for agent_id in ("agent-a", "agent-b"):
                values.append({
                    "eventId": f"{prefix}-{agent_id}-end",
                    "occurredAt": f"2026-09-30T08:{minute}:{'05' if agent_id == 'agent-a' else '04'}Z",
                    "type": "subagent_completed", "agentId": agent_id, "status": "success",
                })
            values.append({
                "eventId": f"{prefix}-turn-end", "occurredAt": f"2026-09-30T08:{minute}:06Z",
                "type": "turn_completed", "status": "success",
            })
            return values

        _, left = self._post(
            events("parallel-left", ["agent-a", "agent-b"], "00"),
            sourceTurnId="turn-parallel-left",
        )
        _, right = self._post(
            events("parallel-right", ["agent-b", "agent-a"], "01"),
            sourceTurnId="turn-parallel-right",
        )
        status, comparison = self.api.dispatch(
            "GET", "/api/v1/traces/compare",
            {"leftTraceId": left["id"], "rightTraceId": right["id"]}, None,
            f"Bearer {self.bootstrap}",
        )

        self.assertEqual(status, 200)
        self.assertEqual([node["kind"] for node in comparison["aligned"]], ["turn"])
        self.assertEqual(len(comparison["leftOnly"]), 4)
        self.assertEqual(len(comparison["rightOnly"]), 4)
        self.assertEqual({node["kind"] for node in comparison["leftOnly"]}, {"agent", "tool"})

    def test_collector_status_auth_strict_payload_staleness_and_retention(self) -> None:
        body = {
            "schemaVersion": 1, "collectorId": "collector-main", "observedAt": "2026-09-30T08:00:00Z",
            "pendingCount": 4, "retryCount": 2, "droppedCount": 1, "pausedCount": 0,
            "quarantinedCount": 3, "lastSuccessAt": "2026-09-30T07:59:00Z",
            "dropReasons": {"expired": 1, "capacity": 0, "invalid": 0, "conflict": 0},
        }
        admin = f"Bearer {self.bootstrap}"
        rejected_status, rejected = self.api.dispatch(
            "POST", "/api/v1/traces/collector-status", {},
            {**body, "rawMessages": ["sensitive payload"]}, self.token,
        )
        self.assertEqual((rejected_status, rejected["code"]), (400, "INVALID_REQUEST"))
        missing_status, missing = self.api.dispatch(
            "POST", "/api/v1/traces/collector-status", {},
            {key: value for key, value in body.items() if key != "lastSuccessAt"}, self.token,
        )
        self.assertEqual((missing_status, missing["code"]), (400, "INVALID_COLLECTOR_STATUS"))
        denied_status, denied = self.api.dispatch(
            "POST", "/api/v1/traces/collector-status", {}, body, admin,
        )
        self.assertEqual((denied_status, denied["code"]), (403, "FORBIDDEN"))
        unbound = self.access.create_service_account("Unbound collector")
        unbound_key = self.access.issue_api_key(unbound.id)
        unbound_status, unbound_response = self.api.dispatch(
            "POST", "/api/v1/traces/collector-status", {}, body,
            f"Bearer {unbound_key['apiKey']}",
        )
        self.assertEqual((unbound_status, unbound_response["code"]), (403, "FORBIDDEN"))

        accepted_status, accepted = self.api.dispatch(
            "POST", "/api/v1/traces/collector-status", {}, body, self.token,
        )
        self.assertEqual(accepted_status, 201)
        self.assertEqual(accepted["subjectId"], self.auth.subject.id)
        listed_status, listed = self.api.dispatch(
            "GET", "/api/v1/traces/collector-status", {}, None, admin,
        )
        self.assertEqual(listed_status, 200)
        self.assertEqual(len(listed["items"]), 1)
        item = listed["items"][0]
        self.assertEqual(item["dropReasons"], body["dropReasons"])
        update = {**body, "observedAt": "2026-09-30T08:00:30Z", "pendingCount": 5}
        updated_status, _ = self.api.dispatch(
            "POST", "/api/v1/traces/collector-status", {}, update, self.token,
        )
        updated_list_status, updated_list = self.api.dispatch(
            "GET", "/api/v1/traces/collector-status", {}, None, admin,
        )
        self.assertEqual((updated_status, updated_list_status), (201, 200))
        self.assertEqual(len(updated_list["items"]), 1)
        self.assertEqual(updated_list["items"][0]["pendingCount"], 5)
        self.assertFalse(item["stale"])
        self.assertNotIn("rawMessages", item)

        self.now += timedelta(minutes=6)
        _, stale = self.api.dispatch("GET", "/api/v1/traces/collector-status", {}, None, admin)
        self.assertTrue(stale["items"][0]["stale"])
        self.now += timedelta(days=31)
        self.store.cleanup_expired()
        _, expired = self.api.dispatch("GET", "/api/v1/traces/collector-status", {}, None, admin)
        self.assertEqual(expired["items"], [])

    def test_write_requires_project_scoped_trace_permission(self) -> None:
        event = {"eventId": "e-1", "occurredAt": "2026-09-30T08:00:00Z", "type": "turn_started"}
        unbound = self.access.create_service_account("Unbound")
        key = self.access.issue_api_key(unbound.id)
        denied_status, denied = self.api.dispatch(
            "POST", "/api/v1/traces/events", {},
            {"schemaVersion": 1, "source": "codex", "sourceSessionId": "s", "sourceTurnId": "t", "events": [event]},
            f"Bearer {key['apiKey']}",
        )
        self.assertEqual((denied_status, denied["code"]), (403, "FORBIDDEN"))
        other_project_api = DiagnosticsApi(self.store, self.access, PolicyEngine(), project_id="project-b")
        denied_status, denied = other_project_api.dispatch(
            "POST", "/api/v1/traces/events", {},
            {"schemaVersion": 1, "source": "codex", "sourceSessionId": "s", "sourceTurnId": "t", "events": [event]},
            self.token,
        )
        self.assertEqual((denied_status, denied["code"]), (403, "FORBIDDEN"))

    def test_expiration_removes_events_and_trace(self) -> None:
        _, accepted = self._post([{"eventId": "e-1", "occurredAt": "2026-09-30T08:00:00Z", "type": "turn_started"}])
        self.now += timedelta(days=31)
        self.store.cleanup_expired()
        status, result = self.api.dispatch("GET", f"/api/v1/traces/{accepted['id']}", {}, None, f"Bearer {self.bootstrap}")
        self.assertEqual((status, result["code"]), (404, "TRACE_NOT_FOUND"))

    def test_backfilled_event_uses_ingestion_time_for_span_retention(self) -> None:
        old_time = (self.now - timedelta(days=60)).isoformat().replace("+00:00", "Z")
        _, accepted = self._post([{
            "eventId": "old-start", "occurredAt": old_time, "type": "turn_started",
        }])
        _, detail = self.api.dispatch(
            "GET", f"/api/v1/traces/{accepted['id']}", {}, None, f"Bearer {self.bootstrap}",
        )
        self.assertEqual(detail["eventCount"], 1)
        self.assertEqual(detail["spans"][0]["startedAt"], old_time)
        self.now += timedelta(days=31)
        self.store.cleanup_expired()
        with self.access._connect() as connection:
            spans = connection.execute(
                "SELECT COUNT(*) AS count FROM agent_trace_spans WHERE trace_id=?", (accepted["id"],),
            ).fetchone()["count"]
        self.assertEqual(spans, 0)

    def test_http_server_schedules_cleanup_while_idle(self) -> None:
        class CleanupStore:
            def __init__(self) -> None:
                self.calls = 0

            def cleanup_expired(self) -> None:
                self.calls += 1

        cleanup = CleanupStore()
        application = type("App", (), {
            "runtime_rpc": type("Rpc", (), {"diagnostics": cleanup})(),
            "diagnostics_api": None,
        })()
        server = SemanticConsoleHTTPServer(("127.0.0.1", 0), application)
        try:
            server.service_actions()
            server.service_actions()
            self.assertEqual(cleanup.calls, 1)
            server._next_diagnostic_cleanup_at = 0
            server.service_actions()
            self.assertEqual(cleanup.calls, 2)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
