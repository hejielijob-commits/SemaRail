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
        self.store.record_trace_events(
            auth=self.auth, project_id="project-b", source_session_id="session-1",
            source_turn_id="turn-c", events=[event],
        )
        admin = f"Bearer {self.bootstrap}"
        _, page = self.api.dispatch("GET", "/api/v1/traces", {"limit": "1"}, None, admin)
        _, next_page = self.api.dispatch(
            "GET", "/api/v1/traces", {"limit": "1", "cursor": page["nextCursor"]}, None, admin,
        )
        self.assertEqual({page["items"][0]["id"], next_page["items"][0]["id"]}, {first["id"], second["id"]})
        self.assertIsNone(next_page["nextCursor"])

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
