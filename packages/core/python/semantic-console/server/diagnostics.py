"""Independent query diagnostics, feedback, and regression-case storage.

The store deliberately shares only the configured control-plane database
connection with access control.  Its tables, migration ledger, retention, and
payloads are separate from the audit log.
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

try:
    from .access_control import AccessControlError, AccessControlStore, AuthContext, _timestamp, _utc_now
except ImportError:  # pragma: no cover - direct module loading
    from access_control import (  # type: ignore[no-redef]
        AccessControlError,
        AccessControlStore,
        AuthContext,
        _timestamp,
        _utc_now,
    )


DIAGNOSTIC_SCHEMA_VERSION = 11
_DIAGNOSTIC_MIGRATION_LOCK_ID = 8_341_972_315_443_002
DEFAULT_RETENTION_DAYS = 30
TRACE_RETENTION_DAYS = 30
MAX_TEXT = 64_000
MAX_ERROR_JSON = 64_000
_SECRET = re.compile(
    r"(?i)(bearer\s+\S+|sr_(?:live|session|key)_[A-Za-z0-9._~-]+|"
    r"(?:postgres(?:ql)?|mysql|clickhouse)://[^\s\"']+|"
    r"(?:password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+)"
)
_CATEGORIES = frozenset(
    {
        "ambiguity",
        "knowledge_gap",
        "agent_understanding",
        "sql_generation",
        "permission_configuration",
        "runtime_failure",
        "evaluation",
        "other",
    }
)
_STATUSES = frozenset({"pending", "classified", "located", "fixed", "verified", "closed_no_fix"})
_RETRIEVAL_ANOMALIES = frozenset(
    {"ZERO_RECALL", "FULL_FALLBACK", "INDEX_NOT_READY", "PERMISSION_OVER_FILTERED"}
)
_TRACE_EVENT_TYPES = frozenset({
    "turn_started", "turn_completed", "turn_interrupted", "tool_started",
    "tool_completed", "subagent_started", "subagent_completed", "output",
})
_TRACE_STATUSES = frozenset({"running", "success", "failure", "cancelled"})
_TRACE_EVENT_FIELDS = frozenset({
    "eventId", "occurredAt", "type", "agentId", "parentAgentId", "toolUseId",
    "parentToolUseId", "toolName", "model", "status", "coreTraceId", "tokenUsage",
})
_TRACE_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_LOGGER = logging.getLogger("semarail-core.diagnostics")


class DiagnosticError(RuntimeError):
    def __init__(self, code: str, safe_message: str, *, status: int = 400) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status = status


def _safe_text(value: Any, *, required: bool = False, limit: int = MAX_TEXT) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise DiagnosticError("INVALID_FEEDBACK", "feedback fields must be text")
    text = value.strip()
    if required and not text:
        raise DiagnosticError("INVALID_FEEDBACK", "a required feedback field is empty")
    if len(text) > limit:
        raise DiagnosticError("INVALID_FEEDBACK", "feedback content is too large")
    return _SECRET.sub("[REDACTED]", text)


def _json(value: Any, *, limit: int = MAX_ERROR_JSON) -> str:
    def redact(item: Any) -> Any:
        if isinstance(item, str):
            return _SECRET.sub("[REDACTED]", item)
        if isinstance(item, Mapping):
            return {key: redact(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [redact(nested) for nested in item]
        return item

    try:
        # Redact values before encoding. Redacting serialized JSON can consume
        # closing quotes/braces (for example ``Bearer token"}``) and corrupt
        # the durable payload.
        encoded = json.dumps(redact(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise DiagnosticError("INVALID_DIAGNOSTIC", "diagnostic content is invalid") from exc
    if len(encoded) > limit:
        raise DiagnosticError("INVALID_DIAGNOSTIC", "diagnostic content is too large")
    return encoded


def _filter_timestamp(value: str) -> str:
    text = _safe_text(value, required=True, limit=64) or ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiagnosticError("INVALID_FILTER", "diagnostic time filter is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _timestamp(parsed.astimezone(UTC))


def _safe_retrieval_explanation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed unless a retrieval diagnostic is text-free and bounded."""

    allowed = {
        "schemaVersion", "projectRevision", "indexStatus", "candidateCount",
        "filteredCount", "selectedCount", "latencyMs", "fallbackReason",
        "traceCount", "authorizationFilteredCount", "anomalies", "retrievalTrace",
    }
    if set(value) - allowed or value.get("schemaVersion") != 1:
        raise DiagnosticError("INVALID_DIAGNOSTIC", "retrieval explanation is invalid")
    for key in ("candidateCount", "filteredCount", "selectedCount", "traceCount", "authorizationFilteredCount"):
        count = value.get(key, 0)
        if type(count) is not int or not 0 <= count <= 10_000_000:
            raise DiagnosticError("INVALID_DIAGNOSTIC", "retrieval explanation is invalid")
    latency = value.get("latencyMs", 0.0)
    if (
        isinstance(latency, bool) or not isinstance(latency, (int, float))
        or not 0 <= float(latency) <= 86_400_000
    ):
        raise DiagnosticError("INVALID_DIAGNOSTIC", "retrieval explanation is invalid")
    fallback = value.get("fallbackReason")
    if fallback is not None and fallback not in {
        "embeddingUnavailable", "vectorSearchFailed", "indexDegraded"
    }:
        raise DiagnosticError("INVALID_DIAGNOSTIC", "retrieval explanation is invalid")
    anomalies = value.get("anomalies", [])
    if (
        not isinstance(anomalies, list)
        or len(anomalies) > len(_RETRIEVAL_ANOMALIES)
        or any(item not in _RETRIEVAL_ANOMALIES for item in anomalies)
    ):
        raise DiagnosticError("INVALID_DIAGNOSTIC", "retrieval explanation is invalid")
    trace = value.get("retrievalTrace", [])
    if not isinstance(trace, list) or len(trace) > 2_000:
        raise DiagnosticError("INVALID_DIAGNOSTIC", "retrieval explanation is invalid")
    trace_keys = {
        "documentId", "source", "retrievalType", "relevance", "reasonCode",
        "projectRevision", "authorizationFiltered", "selected",
    }
    safe_trace: list[dict[str, Any]] = []
    for item in trace:
        if not isinstance(item, Mapping) or set(item) - trace_keys:
            raise DiagnosticError("INVALID_DIAGNOSTIC", "retrieval explanation is invalid")
        encoded = _json(dict(item), limit=4_096)
        safe_trace.append(json.loads(encoded))
    index_status = value.get("indexStatus", {})
    if not isinstance(index_status, Mapping) or set(index_status) - {
        "status", "activeRevision", "indexedRevision", "documentCount", "backend",
        "staleReason", "embeddingModelId", "embeddingModelVersion", "embeddingDimension",
        "indexBuildVersion", "lastBuildAt", "buildDurationMs",
    }:
        raise DiagnosticError("INVALID_DIAGNOSTIC", "retrieval explanation is invalid")
    revision = value.get("projectRevision")
    if revision is not None and (not isinstance(revision, str) or not 1 <= len(revision) <= 256):
        raise DiagnosticError("INVALID_DIAGNOSTIC", "retrieval explanation is invalid")
    return {
        "schemaVersion": 1,
        **({"projectRevision": revision} if revision is not None else {}),
        "indexStatus": json.loads(_json(dict(index_status), limit=16_000)),
        "candidateCount": value.get("candidateCount", 0),
        "filteredCount": value.get("filteredCount", 0),
        "selectedCount": value.get("selectedCount", 0),
        "latencyMs": float(latency),
        **({"fallbackReason": fallback} if fallback is not None else {}),
        "traceCount": value.get("traceCount", 0),
        "authorizationFilteredCount": value.get("authorizationFilteredCount", 0),
        "anomalies": list(anomalies),
        "retrievalTrace": safe_trace,
    }


def _trace_id(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value)
        or _SECRET.sub("[REDACTED]", value) != value
    ):
        raise DiagnosticError("INVALID_TRACE", f"{field} is invalid")
    return value


def _trace_time(value: str) -> datetime:
    """Parse a normalized event timestamp for ordering without string quirks."""

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _trace_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - _TRACE_EVENT_FIELDS:
        raise DiagnosticError("INVALID_TRACE", "trace event is invalid")
    event_id = _trace_id(value.get("eventId"), "eventId")
    kind = value.get("type")
    if not isinstance(kind, str) or kind not in _TRACE_EVENT_TYPES:
        raise DiagnosticError("INVALID_TRACE", "trace event type is invalid")
    occurred = value.get("occurredAt")
    if not isinstance(occurred, str) or len(occurred) > 64:
        raise DiagnosticError("INVALID_TRACE", "trace event time is invalid")
    try:
        parsed = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiagnosticError("INVALID_TRACE", "trace event time is invalid") from exc
    if parsed.tzinfo is None:
        raise DiagnosticError("INVALID_TRACE", "trace event time needs a timezone")
    event: dict[str, Any] = {
        "eventId": event_id, "occurredAt": _timestamp(parsed.astimezone(UTC)), "type": kind,
    }
    for field in ("agentId", "parentAgentId", "toolUseId", "parentToolUseId", "coreTraceId"):
        if field in value:
            event[field] = _trace_id(value[field], field)
    if kind in {"tool_started", "tool_completed"} and "toolUseId" not in event:
        raise DiagnosticError("INVALID_TRACE", "tool events require toolUseId")
    if kind in {"subagent_started", "subagent_completed"} and "agentId" not in event:
        raise DiagnosticError("INVALID_TRACE", "subagent events require agentId")
    if "coreTraceId" in event and kind != "tool_completed":
        raise DiagnosticError("INVALID_TRACE", "Core trace IDs belong to completed tool events")
    if "toolName" in value:
        name = value["toolName"]
        if not isinstance(name, str) or not _TRACE_TOOL_NAME.fullmatch(name) or _SECRET.sub("[REDACTED]", name) != name:
            raise DiagnosticError("INVALID_TRACE", "toolName is invalid")
        event["toolName"] = name
    if "model" in value:
        event["model"] = _trace_id(value["model"], "model")
    if "status" in value:
        if not isinstance(value["status"], str) or value["status"] not in _TRACE_STATUSES:
            raise DiagnosticError("INVALID_TRACE", "trace event status is invalid")
        event["status"] = value["status"]
    if "tokenUsage" in value:
        usage = value["tokenUsage"]
        if not isinstance(usage, Mapping) or not usage or set(usage) - {"input", "output", "total"}:
            raise DiagnosticError("INVALID_TRACE", "token usage is invalid")
        if any(type(count) is not int or not 0 <= count <= 1_000_000_000 for count in usage.values()):
            raise DiagnosticError("INVALID_TRACE", "token usage is invalid")
        event["tokenUsage"] = {
            key: usage[key] for key in ("input", "output", "total") if key in usage
        }
    return event


class DiagnosticStore:
    """Versioned SQLite/PostgreSQL diagnostics storage with 30-day bodies."""

    def __init__(
        self,
        access_control: AccessControlStore,
        *,
        clock: Callable[[], datetime] = _utc_now,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise ValueError("diagnostic retention must be between 1 and 365 days")
        self.access_control = access_control
        self.clock = getattr(access_control, "clock", clock)
        self.retention_days = retention_days
        self._initialize()

    def _initialize(self) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS diagnostic_schema_migrations (version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS query_diagnostics (id TEXT PRIMARY KEY,trace_id TEXT NOT NULL UNIQUE,query_id TEXT,organization_id TEXT NOT NULL,project_id TEXT NOT NULL,datasource_id TEXT,subject_id TEXT NOT NULL,credential_id TEXT,transport TEXT NOT NULL,method TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,semantic_version TEXT,policy_versions_json TEXT NOT NULL,error_json TEXT,question TEXT,semantic_sql TEXT,native_sql TEXT,evidence_source TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,content_purged_at TEXT)",
            "CREATE INDEX IF NOT EXISTS diagnostics_scope_time_idx ON query_diagnostics(organization_id,project_id,created_at)",
            "CREATE INDEX IF NOT EXISTS diagnostics_owner_query_idx ON query_diagnostics(subject_id,project_id,query_id)",
            "CREATE TABLE IF NOT EXISTS query_feedback (id TEXT PRIMARY KEY,diagnostic_id TEXT NOT NULL REFERENCES query_diagnostics(id),organization_id TEXT NOT NULL,project_id TEXT NOT NULL,subject_id TEXT NOT NULL,credential_id TEXT,idempotency_key TEXT NOT NULL,category TEXT NOT NULL,description TEXT NOT NULL,expected_behavior TEXT,question TEXT,semantic_sql TEXT,native_sql TEXT,evidence_source TEXT NOT NULL,status TEXT NOT NULL,duplicate_of TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(subject_id,idempotency_key))",
            "CREATE INDEX IF NOT EXISTS feedback_scope_time_idx ON query_feedback(organization_id,project_id,created_at)",
            "CREATE TABLE IF NOT EXISTS diagnostic_status_history (id TEXT PRIMARY KEY,feedback_id TEXT NOT NULL REFERENCES query_feedback(id),actor_subject_id TEXT NOT NULL,from_status TEXT,to_status TEXT NOT NULL,note TEXT,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS regression_cases (id TEXT PRIMARY KEY,feedback_id TEXT NOT NULL REFERENCES query_feedback(id),organization_id TEXT NOT NULL,project_id TEXT NOT NULL,schema_version INTEGER NOT NULL,case_json TEXT NOT NULL,status TEXT NOT NULL,created_by TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS regression_scope_time_idx ON regression_cases(organization_id,project_id,created_at)",
            "CREATE TABLE IF NOT EXISTS diagnostic_security_events (id TEXT PRIMARY KEY,trace_id TEXT NOT NULL,transport TEXT NOT NULL,method TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL)",
        )
        try:
            lock = getattr(self.access_control, "_lock")
            connect = getattr(self.access_control, "_connect")
            with lock, connect() as connection:
                if getattr(self.access_control, "path", None) is None:
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(?)", (_DIAGNOSTIC_MIGRATION_LOCK_ID,)
                    )
                connection.execute(statements[0])
                rows = connection.execute(
                    "SELECT version FROM diagnostic_schema_migrations ORDER BY version"
                ).fetchall()
                applied = [int(row["version"]) for row in rows]
                if applied != list(range(1, len(applied) + 1)) or any(
                    version > DIAGNOSTIC_SCHEMA_VERSION for version in applied
                ):
                    raise DiagnosticError(
                        "DIAGNOSTIC_SCHEMA_INCOMPATIBLE",
                        "diagnostic storage schema is incompatible",
                        status=503,
                    )
                if not applied:
                    for statement in statements[1:-1]:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (1, _timestamp(self.clock())),
                    )
                if len(applied) < 2:
                    connection.execute(statements[-1])
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (2, _timestamp(self.clock())),
                    )
                if len(applied) < 3:
                    connection.execute(
                        "ALTER TABLE query_diagnostics ADD COLUMN duration_ms DOUBLE PRECISION"
                    )
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (3, _timestamp(self.clock())),
                    )
                if len(applied) < 4:
                    connection.execute(
                        "ALTER TABLE query_diagnostics ADD COLUMN original_query_id TEXT"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS diagnostics_owner_original_query_idx "
                        "ON query_diagnostics(subject_id,project_id,original_query_id)"
                    )
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (4, _timestamp(self.clock())),
                    )
                if len(applied) < 5:
                    connection.execute(
                        "ALTER TABLE query_diagnostics ADD COLUMN question_hash TEXT"
                    )
                    connection.execute(
                        "ALTER TABLE query_diagnostics ADD COLUMN retrieval_trace_id TEXT"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS diagnostics_retrieval_link_idx "
                        "ON query_diagnostics(organization_id,project_id,subject_id,question_hash,created_at)"
                    )
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (5, _timestamp(self.clock())),
                    )
                if len(applied) < 6:
                    connection.execute(
                        "ALTER TABLE query_diagnostics ADD COLUMN retrieval_explanation_json TEXT"
                    )
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (6, _timestamp(self.clock())),
                    )
                if len(applied) < 7:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS agent_traces ("
                        "id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,project_id TEXT NOT NULL,"
                        "subject_id TEXT NOT NULL,source TEXT NOT NULL,source_session_id TEXT NOT NULL,"
                        "source_turn_id TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,"
                        "UNIQUE(organization_id,project_id,subject_id,source,source_session_id,source_turn_id))"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS agent_traces_scope_time_idx "
                        "ON agent_traces(organization_id,project_id,created_at,id)"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS agent_trace_events ("
                        "trace_id TEXT NOT NULL REFERENCES agent_traces(id),event_id TEXT NOT NULL,"
                        "occurred_at TEXT NOT NULL,event_type TEXT NOT NULL,event_json TEXT NOT NULL,"
                        "created_at TEXT NOT NULL,PRIMARY KEY(trace_id,event_id))"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS agent_trace_events_time_idx "
                        "ON agent_trace_events(trace_id,occurred_at,event_id)"
                    )
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (7, _timestamp(self.clock())),
                    )
                if len(applied) < 8:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS agent_trace_core_links ("
                        "trace_id TEXT NOT NULL REFERENCES agent_traces(id),"
                        "event_id TEXT NOT NULL,core_trace_id TEXT NOT NULL,"
                        "PRIMARY KEY(trace_id,event_id))"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS agent_trace_core_lookup_idx "
                        "ON agent_trace_core_links(core_trace_id,trace_id)"
                    )
                    for saved in connection.execute(
                        "SELECT trace_id,event_id,event_json FROM agent_trace_events"
                    ).fetchall():
                        core_id = json.loads(saved["event_json"]).get("coreTraceId")
                        if isinstance(core_id, str):
                            connection.execute(
                                "INSERT INTO agent_trace_core_links(trace_id,event_id,core_trace_id) "
                                "VALUES(?,?,?)",
                                (saved["trace_id"], saved["event_id"], core_id),
                            )
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (8, _timestamp(self.clock())),
                    )
                if len(applied) < 9:
                    connection.execute(
                        "ALTER TABLE query_diagnostics ADD COLUMN phase_spans_json TEXT"
                    )
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (9, _timestamp(self.clock())),
                    )
                if len(applied) < 10:
                    connection.execute("ALTER TABLE agent_traces ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
                    connection.execute("ALTER TABLE agent_trace_events ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS agent_trace_spans ("
                        "trace_id TEXT NOT NULL REFERENCES agent_traces(id),span_id TEXT NOT NULL,"
                        "schema_version INTEGER NOT NULL,kind TEXT NOT NULL,parent_span_id TEXT,"
                        "started_at TEXT,ended_at TEXT,status TEXT NOT NULL,"
                        "PRIMARY KEY(trace_id,span_id))"
                    )
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (10, _timestamp(self.clock())),
                    )
                if len(applied) < 11:
                    connection.execute(
                        "ALTER TABLE agent_trace_spans ADD COLUMN last_event_created_at TEXT"
                    )
                    connection.execute(
                        "ALTER TABLE agent_trace_spans ADD COLUMN terminal_event_at TEXT"
                    )
                    connection.execute(
                        "ALTER TABLE agent_trace_spans ADD COLUMN terminal_event_id TEXT"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS agent_trace_core_claims ("
                        "core_trace_id TEXT PRIMARY KEY,trace_id TEXT,status TEXT NOT NULL,"
                        "FOREIGN KEY(trace_id) REFERENCES agent_traces(id))"
                    )
                    for saved in connection.execute(
                        "SELECT trace_id,event_id,event_json,created_at FROM agent_trace_events "
                        "ORDER BY occurred_at,event_id"
                    ).fetchall():
                        event = json.loads(saved["event_json"])
                        self._upsert_trace_span(
                            connection, saved["trace_id"], event,
                            event_created_at=saved["created_at"],
                        )
                        core_id = event.get("coreTraceId")
                        if isinstance(core_id, str):
                            connection.execute(
                                "INSERT INTO agent_trace_core_claims(core_trace_id,trace_id,status) "
                                "VALUES(?,?,?) ON CONFLICT(core_trace_id) DO NOTHING",
                                (core_id, saved["trace_id"], "verified"),
                            )
                    duplicates = connection.execute(
                        "SELECT core_trace_id FROM agent_trace_core_links GROUP BY core_trace_id "
                        "HAVING COUNT(DISTINCT trace_id)>1"
                    ).fetchall()
                    for claim in duplicates:
                        # Historical duplicate claims are retained as an
                        # explicit conflict. Never pick one trace arbitrarily.
                        connection.execute(
                            "UPDATE agent_trace_core_claims SET trace_id=NULL,status='ambiguous' "
                            "WHERE core_trace_id=?", (claim["core_trace_id"],),
                        )
                    connection.execute(
                        "INSERT INTO diagnostic_schema_migrations(version,applied_at) VALUES(?,?)",
                        (11, _timestamp(self.clock())),
                    )
        except DiagnosticError:
            raise
        except Exception as exc:
            raise DiagnosticError(
                "DIAGNOSTIC_STORE_UNAVAILABLE", "diagnostic storage is unavailable", status=503
            ) from exc

    def record_execution(
        self,
        *,
        auth: AuthContext,
        project_id: str,
        trace_id: str,
        query_id: str | None,
        original_query_id: str | None = None,
        datasource_id: str | None,
        transport: str,
        method: str,
        status: str,
        stage: str,
        semantic_version: str | None = None,
        policy_versions: list[str] | tuple[str, ...] = (),
        error: Mapping[str, Any] | None = None,
        question: str | None = None,
        semantic_sql: str | None = None,
        native_sql: str | None = None,
        evidence_source: str = "server",
        duration_ms: float = 0.0,
        phase_spans: list[Mapping[str, Any]] | None = None,
        retrieval_explanation: Mapping[str, Any] | None = None,
    ) -> str:
        if status not in {"success", "failure", "cancelled"}:
            raise DiagnosticError("INVALID_DIAGNOSTIC", "diagnostic status is invalid")
        now = self.clock().astimezone(UTC)
        diagnostic_id = f"diag_{uuid.uuid4().hex}"
        # Successes retain ownership metadata only. Content is attached later
        # only when the caller explicitly submits feedback.
        keep_content = status == "failure"
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
            raise DiagnosticError("INVALID_DIAGNOSTIC", "diagnostic duration is invalid")
        bounded_duration = float(duration_ms)
        if not 0 <= bounded_duration <= 86_400_000:
            raise DiagnosticError("INVALID_DIAGNOSTIC", "diagnostic duration is invalid")
        safe_phases: list[dict[str, Any]] = []
        for phase in phase_spans or []:
            if not isinstance(phase, Mapping) or set(phase) != {"name", "status", "durationMs"}:
                raise DiagnosticError("INVALID_DIAGNOSTIC", "diagnostic phase is invalid")
            if phase["name"] not in {"authentication", "policy", "runtime"} or phase["status"] not in {"success", "failure"}:
                raise DiagnosticError("INVALID_DIAGNOSTIC", "diagnostic phase is invalid")
            elapsed = phase["durationMs"]
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not 0 <= elapsed <= 86_400_000:
                raise DiagnosticError("INVALID_DIAGNOSTIC", "diagnostic phase is invalid")
            safe_phases.append({"name": phase["name"], "status": phase["status"], "durationMs": float(elapsed)})
        if len(safe_phases) > 3 or len({phase["name"] for phase in safe_phases}) != len(safe_phases):
            raise DiagnosticError("INVALID_DIAGNOSTIC", "diagnostic phase is invalid")
        safe_question = _safe_text(question) if question is not None else None
        question_hash = (
            hashlib.sha256(safe_question.strip().encode("utf-8")).hexdigest()
            if isinstance(safe_question, str) and safe_question.strip()
            else None
        )
        retrieval_trace_id: str | None = None
        values = (
            diagnostic_id,
            _safe_text(trace_id, required=True, limit=128),
            _safe_text(query_id, limit=128),
            _safe_text(original_query_id, limit=128),
            auth.subject.organization_id,
            _safe_text(project_id, required=True, limit=256),
            _safe_text(datasource_id, limit=512),
            auth.subject.id,
            auth.credential_id,
            _safe_text(transport, required=True, limit=64),
            _safe_text(method, required=True, limit=64),
            status,
            _safe_text(stage, required=True, limit=64),
            _safe_text(semantic_version, limit=256),
            _json(list(policy_versions)[:64], limit=16_000),
            _json(dict(error)) if keep_content and error is not None else None,
            safe_question if keep_content else None,
            _safe_text(semantic_sql) if keep_content else None,
            _safe_text(native_sql) if keep_content else None,
            evidence_source,
            _timestamp(now),
            _timestamp(now + timedelta(days=self.retention_days)),
            bounded_duration,
            question_hash,
            retrieval_trace_id,
            _json(_safe_retrieval_explanation(retrieval_explanation), limit=256_000)
            if method == "context.ask" and retrieval_explanation is not None
            else None,
            _json(safe_phases, limit=2_048),
        )
        try:
            connect = getattr(self.access_control, "_connect")
            with connect() as connection:
                if method != "context.ask" and question_hash is not None:
                    retrieval = connection.execute(
                        "SELECT trace_id FROM query_diagnostics "
                        "WHERE organization_id=? AND project_id=? AND subject_id=? "
                        "AND method='context.ask' AND question_hash=? "
                        "ORDER BY created_at DESC,id DESC LIMIT 1",
                        (
                            auth.subject.organization_id,
                            project_id,
                            auth.subject.id,
                            question_hash,
                        ),
                    ).fetchone()
                    if retrieval is not None and isinstance(retrieval["trace_id"], str):
                        retrieval_trace_id = retrieval["trace_id"]
                        values = (*values[:-3], retrieval_trace_id, *values[-2:])
                connection.execute(
                    "INSERT OR IGNORE INTO query_diagnostics(id,trace_id,query_id,original_query_id,organization_id,project_id,datasource_id,subject_id,credential_id,transport,method,status,stage,semantic_version,policy_versions_json,error_json,question,semantic_sql,native_sql,evidence_source,created_at,expires_at,duration_ms,question_hash,retrieval_trace_id,retrieval_explanation_json,phase_spans_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                row = connection.execute(
                    "SELECT id FROM query_diagnostics WHERE trace_id=?", (values[1],)
                ).fetchone()
                if row is not None and status == "failure":
                    feedback_id = f"fb_{uuid.uuid4().hex}"
                    error_message = error.get("message") if isinstance(error, Mapping) else None
                    description = (
                        _safe_text(error_message, limit=8_000)
                        if isinstance(error_message, str) and error_message.strip()
                        else "Automatically captured query failure"
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO query_feedback(id,diagnostic_id,organization_id,project_id,subject_id,credential_id,idempotency_key,category,description,expected_behavior,question,semantic_sql,native_sql,evidence_source,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            feedback_id,
                            row["id"],
                            auth.subject.organization_id,
                            project_id,
                            auth.subject.id,
                            auth.credential_id,
                            f"auto-{row['id']}",
                            "runtime_failure",
                            description,
                            None,
                            None,
                            None,
                            None,
                            "server",
                            "pending",
                            _timestamp(now),
                            _timestamp(now),
                        ),
                    )
            if row is None:
                raise DiagnosticError("DIAGNOSTIC_STORE_UNAVAILABLE", "diagnostic storage is unavailable", status=503)
            return str(row["id"])
        except DiagnosticError:
            raise
        except Exception as exc:
            raise DiagnosticError(
                "DIAGNOSTIC_STORE_UNAVAILABLE", "diagnostic storage is unavailable", status=503
            ) from exc

    def resolve_owned_retry_reference(
        self,
        *,
        auth: AuthContext,
        project_id: str,
        reference: str,
    ) -> str:
        """Resolve a retry chain to its first query without crossing ownership scope."""

        safe_reference = _safe_text(reference, required=True, limit=128) or ""
        connect = getattr(self.access_control, "_connect")
        try:
            with connect() as connection:
                row = connection.execute(
                    "SELECT query_id,original_query_id FROM query_diagnostics "
                    "WHERE organization_id=? AND project_id=? AND subject_id=? AND query_id=? "
                    "ORDER BY created_at DESC,id DESC LIMIT 1",
                    (
                        auth.subject.organization_id,
                        _safe_text(project_id, required=True, limit=256),
                        auth.subject.id,
                        safe_reference,
                    ),
                ).fetchone()
            if row is None or not isinstance(row["query_id"], str):
                raise DiagnosticError(
                    "DIAGNOSTIC_NOT_FOUND",
                    "the retry reference was not found for the current caller",
                    status=404,
                )
            original = row["original_query_id"]
            return str(original) if isinstance(original, str) and original else str(row["query_id"])
        except DiagnosticError:
            raise
        except Exception as exc:
            raise DiagnosticError(
                "DIAGNOSTIC_STORE_UNAVAILABLE",
                "diagnostic storage is unavailable",
                status=503,
            ) from exc

    def record_security_event(self, *, trace_id: str, transport: str, method: str | None) -> None:
        """Record authentication failure metadata without retaining request content."""

        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            connection.execute(
                "INSERT INTO diagnostic_security_events(id,trace_id,transport,method,status,created_at) VALUES(?,?,?,?,?,?)",
                (
                    f"sec_{uuid.uuid4().hex}",
                    _safe_text(trace_id, required=True, limit=128),
                    _safe_text(transport, required=True, limit=64),
                    _safe_text(method, limit=64),
                    "authentication_failed",
                    _timestamp(self.clock()),
                ),
            )

    def submit_feedback(
        self,
        *,
        auth: AuthContext,
        project_id: str,
        reference: str,
        idempotency_key: str,
        category: str,
        description: str,
        expected_behavior: str | None = None,
        question: str | None = None,
        semantic_sql: str | None = None,
        native_sql: str | None = None,
    ) -> dict[str, Any]:
        if category not in _CATEGORIES:
            raise DiagnosticError("INVALID_FEEDBACK", "feedback category is invalid")
        reference = _safe_text(reference, required=True, limit=128) or ""
        idempotency_key = _safe_text(idempotency_key, required=True, limit=128) or ""
        now = _timestamp(self.clock())
        connect = getattr(self.access_control, "_connect")
        try:
            with connect() as connection:
                existing = connection.execute(
                    "SELECT id,diagnostic_id,status,organization_id,project_id FROM query_feedback WHERE subject_id=? AND idempotency_key=?",
                    (auth.subject.id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["organization_id"] != auth.subject.organization_id
                        or existing["project_id"] != project_id
                    ):
                        raise DiagnosticError(
                            "IDEMPOTENCY_KEY_CONFLICT",
                            "the idempotency key is already bound to another request scope",
                            status=409,
                        )
                    return {
                        "feedbackId": existing["id"],
                        "diagnosticId": existing["diagnostic_id"],
                        "status": existing["status"],
                        "duplicate": True,
                    }
                diagnostic = connection.execute(
                    "SELECT id FROM query_diagnostics WHERE organization_id=? AND project_id=? AND subject_id=? AND (trace_id=? OR query_id=?) ORDER BY created_at DESC LIMIT 1",
                    (auth.subject.organization_id, project_id, auth.subject.id, reference, reference),
                ).fetchone()
                if diagnostic is None:
                    raise DiagnosticError(
                        "DIAGNOSTIC_NOT_FOUND",
                        "the referenced query was not found for the current caller",
                        status=404,
                    )
                feedback_id = f"fb_{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO query_feedback(id,diagnostic_id,organization_id,project_id,subject_id,credential_id,idempotency_key,category,description,expected_behavior,question,semantic_sql,native_sql,evidence_source,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        feedback_id,
                        diagnostic["id"],
                        auth.subject.organization_id,
                        project_id,
                        auth.subject.id,
                        auth.credential_id,
                        idempotency_key,
                        category,
                        _safe_text(description, required=True, limit=8_000),
                        _safe_text(expected_behavior, limit=8_000),
                        _safe_text(question),
                        _safe_text(semantic_sql),
                        _safe_text(native_sql),
                        "client",
                        "pending",
                        now,
                        now,
                    ),
                )
            return {
                "feedbackId": feedback_id,
                "diagnosticId": diagnostic["id"],
                "status": "pending",
                "duplicate": False,
            }
        except DiagnosticError:
            raise
        except Exception as exc:
            raise DiagnosticError(
                "DIAGNOSTIC_STORE_UNAVAILABLE", "feedback could not be saved; retry with the same idempotency key", status=503
            ) from exc

    def list_diagnostics(
        self,
        *,
        organization_id: str,
        project_id: str,
        limit: int = 50,
        cursor: str | None = None,
        reason_code: str | None = None,
        datasource_id: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        bounded = min(max(int(limit), 1), 100)
        clauses = ["organization_id=?", "project_id=?"]
        params: list[Any] = [organization_id, project_id]
        connect = getattr(self.access_control, "_connect")
        if cursor:
            safe_cursor = _safe_text(cursor, required=True, limit=128)
            with connect() as connection:
                cursor_row = connection.execute(
                    "SELECT created_at FROM query_diagnostics WHERE id=? AND organization_id=? AND project_id=?",
                    (safe_cursor, organization_id, project_id),
                ).fetchone()
            if cursor_row is None:
                raise DiagnosticError("INVALID_CURSOR", "diagnostic cursor is invalid")
            clauses.append("(created_at<? OR (created_at=? AND id<?))")
            params.extend([cursor_row["created_at"], cursor_row["created_at"], safe_cursor])
        if datasource_id:
            clauses.append("datasource_id=?")
            params.append(_safe_text(datasource_id, required=True, limit=512))
        if source:
            clauses.append("transport=?")
            params.append(_safe_text(source, required=True, limit=64))
        if reason_code:
            clauses.append("error_json LIKE ?")
            params.append(f'%"reasonCode":"{_safe_text(reason_code, required=True, limit=64)}"%')
        params.append(bounded + 1)
        with connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM query_diagnostics WHERE {' AND '.join(clauses)} ORDER BY created_at DESC,id DESC LIMIT ?",  # noqa: S608 - clauses are server constants
                tuple(params),
            ).fetchall()
        items = [self._diagnostic_public(row) for row in rows[:bounded]]
        return {"items": items, "nextCursor": items[-1]["id"] if len(rows) > bounded else None}

    def retrieval_explanation(
        self, trace_id: str, *, organization_id: str, project_id: str
    ) -> dict[str, Any]:
        """Return one bounded, administrator-scoped retrieval explanation."""

        safe_trace = _safe_text(trace_id, required=True, limit=128) or ""
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            row = connection.execute(
                "SELECT trace_id,semantic_version,created_at,content_purged_at,retrieval_explanation_json "
                "FROM query_diagnostics WHERE trace_id=? AND organization_id=? AND project_id=? "
                "AND method='context.ask' LIMIT 1",
                (safe_trace, organization_id, project_id),
            ).fetchone()
        if row is None:
            raise DiagnosticError(
                "RETRIEVAL_TRACE_NOT_FOUND", "retrieval trace was not found", status=404
            )
        return {
            "traceId": row["trace_id"],
            "semanticVersion": row["semantic_version"],
            "createdAt": row["created_at"],
            "contentPurgedAt": row["content_purged_at"],
            "explanation": (
                json.loads(row["retrieval_explanation_json"])
                if row["retrieval_explanation_json"]
                else None
            ),
        }

    def cleanup_expired(self) -> int:
        now = _timestamp(self.clock())
        trace_cutoff = _timestamp(self.clock() - timedelta(days=TRACE_RETENTION_DAYS))
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            cursor = connection.execute(
                "UPDATE query_diagnostics SET error_json=NULL,question=NULL,semantic_sql=NULL,native_sql=NULL,retrieval_explanation_json=NULL,content_purged_at=? WHERE expires_at<=? AND content_purged_at IS NULL",
                (now, now),
            )
            connection.execute(
                "DELETE FROM agent_trace_core_links WHERE trace_id IN "
                "(SELECT id FROM agent_traces WHERE expires_at<=?) OR "
                "NOT EXISTS (SELECT 1 FROM agent_trace_events e WHERE "
                "e.trace_id=agent_trace_core_links.trace_id AND "
                "e.event_id=agent_trace_core_links.event_id AND e.created_at>?)",
                (now, trace_cutoff),
            )
            connection.execute("DELETE FROM agent_trace_events WHERE created_at<=?", (trace_cutoff,))
            connection.execute(
                "DELETE FROM agent_trace_spans WHERE trace_id IN "
                "(SELECT id FROM agent_traces WHERE expires_at<=?) OR "
                "last_event_created_at IS NULL OR last_event_created_at<=?",
                (now, trace_cutoff),
            )
            connection.execute(
                "DELETE FROM agent_traces WHERE expires_at<=? OR NOT EXISTS "
                "(SELECT 1 FROM agent_trace_events WHERE trace_id=agent_traces.id)", (now,),
            )
        return max(int(cursor.rowcount), 0)

    def record_trace_events(
        self, *, auth: AuthContext, project_id: str, source_session_id: str,
        source_turn_id: str, events: Any,
    ) -> dict[str, Any]:
        session_id = _trace_id(source_session_id, "sourceSessionId")
        turn_id = _trace_id(source_turn_id, "sourceTurnId")
        if not isinstance(events, list) or not 1 <= len(events) <= 100:
            raise DiagnosticError("INVALID_TRACE", "trace event batch is invalid")
        safe_events = [_trace_event(event) for event in events]
        if len({event["eventId"] for event in safe_events}) != len(safe_events):
            raise DiagnosticError("INVALID_TRACE", "trace event batch has duplicate IDs")
        try:
            self.cleanup_expired()
        except Exception:
            # Cleanup is maintenance. A transient cleanup failure must not
            # discard newly submitted trace events.
            _LOGGER.error("trace retention cleanup failed")
        organization_id = auth.subject.organization_id
        subject_id = auth.subject.id
        now = _timestamp(self.clock())
        trace_id = f"atr_{uuid.uuid4().hex}"
        accepted = 0
        duplicates = 0
        connect = getattr(self.access_control, "_connect")
        lock = getattr(self.access_control, "_lock")
        with lock, connect() as connection:
            for event in safe_events:
                core_id = event.get("coreTraceId")
                if core_id is None:
                    continue
                owner = connection.execute(
                    "SELECT organization_id,project_id,subject_id FROM query_diagnostics WHERE trace_id=?",
                    (core_id,),
                ).fetchone()
                if owner is not None and (
                    owner["organization_id"] != organization_id
                    or owner["project_id"] != project_id
                    or owner["subject_id"] != subject_id
                ):
                    raise DiagnosticError("TRACE_LINK_FORBIDDEN", "Core trace is outside caller scope", status=403)
            connection.execute(
                "INSERT INTO agent_traces(id,organization_id,project_id,subject_id,source,"
                "source_session_id,source_turn_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT DO NOTHING",
                (trace_id, organization_id, project_id, subject_id, "codex", session_id,
                 turn_id, now, _timestamp(self.clock() + timedelta(days=TRACE_RETENTION_DAYS))),
            )
            row = connection.execute(
                "SELECT id FROM agent_traces WHERE organization_id=? AND project_id=? "
                "AND subject_id=? AND source='codex' AND source_session_id=? AND source_turn_id=?",
                (organization_id, project_id, subject_id, session_id, turn_id),
            ).fetchone()
            if row is None:
                raise DiagnosticError("DIAGNOSTIC_STORE_UNAVAILABLE", "diagnostic storage is unavailable", status=503)
            trace_id = row["id"]
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM agent_trace_events WHERE trace_id=?", (trace_id,),
            ).fetchone()["count"]
            for event in safe_events:
                encoded = _json(event, limit=4_096)
                existing = connection.execute(
                    "SELECT event_json FROM agent_trace_events WHERE trace_id=? AND event_id=?",
                    (trace_id, event["eventId"]),
                ).fetchone()
                if existing is not None:
                    if existing["event_json"] != encoded:
                        raise DiagnosticError("TRACE_EVENT_CONFLICT", "trace event ID has different content", status=409)
                    duplicates += 1
                    continue
                if count + accepted >= 5_000:
                    raise DiagnosticError("TRACE_EVENT_LIMIT", "trace event limit exceeded", status=413)
                core_id = event.get("coreTraceId")
                if isinstance(core_id, str):
                    claim = connection.execute(
                        "SELECT trace_id,status FROM agent_trace_core_claims WHERE core_trace_id=?",
                        (core_id,),
                    ).fetchone()
                    if claim is not None and (
                        claim["status"] != "verified" or claim["trace_id"] != trace_id
                    ):
                        raise DiagnosticError(
                            "TRACE_LINK_CONFLICT", "Core trace is already linked to another turn", status=409
                        )
                inserted = connection.execute(
                    "INSERT INTO agent_trace_events(trace_id,event_id,occurred_at,event_type,event_json,created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(trace_id,event_id) DO NOTHING",
                    (trace_id, event["eventId"], event["occurredAt"], event["type"], encoded, now),
                )
                if inserted.rowcount == 0:
                    raced = connection.execute(
                        "SELECT event_json FROM agent_trace_events WHERE trace_id=? AND event_id=?",
                        (trace_id, event["eventId"]),
                    ).fetchone()
                    if raced is None:
                        raise DiagnosticError(
                            "DIAGNOSTIC_STORE_UNAVAILABLE", "diagnostic storage is unavailable", status=503
                        )
                    if raced["event_json"] != encoded:
                        raise DiagnosticError(
                            "TRACE_EVENT_CONFLICT", "trace event ID has different content", status=409
                        )
                    duplicates += 1
                    continue
                self._upsert_trace_span(connection, trace_id, event, event_created_at=now)
                if isinstance(core_id, str):
                    connection.execute(
                        "INSERT INTO agent_trace_core_claims(core_trace_id,trace_id,status) "
                        "VALUES(?,?,?) ON CONFLICT(core_trace_id) DO NOTHING",
                        (core_id, trace_id, "verified"),
                    )
                    claim = connection.execute(
                        "SELECT trace_id,status FROM agent_trace_core_claims WHERE core_trace_id=?",
                        (core_id,),
                    ).fetchone()
                    if claim is None or claim["status"] != "verified" or claim["trace_id"] != trace_id:
                        raise DiagnosticError(
                            "TRACE_LINK_CONFLICT", "Core trace is already linked to another turn", status=409
                        )
                    connection.execute(
                        "INSERT INTO agent_trace_core_links(trace_id,event_id,core_trace_id) VALUES(?,?,?)",
                        (trace_id, event["eventId"], core_id),
                    )
                accepted += 1
            if accepted:
                connection.execute(
                    "UPDATE agent_traces SET expires_at=? WHERE id=?",
                    (_timestamp(self.clock() + timedelta(days=TRACE_RETENTION_DAYS)), trace_id),
                )
        return {"id": trace_id, "accepted": accepted, "duplicates": duplicates}

    @staticmethod
    def _upsert_trace_span(
        connection: Any,
        trace_id: str,
        event: Mapping[str, Any],
        *,
        event_created_at: str,
    ) -> None:
        kind = event["type"]
        span_specs: list[tuple[str, str, str | None]] = []
        if kind.startswith("turn_"):
            span_specs = [("turn", "turn", None), ("agent:main", "agent", "turn")]
        elif kind.startswith("subagent_") and event.get("agentId"):
            parent = event.get("parentAgentId")
            span_specs = [(f'agent:{event["agentId"]}', "agent", f"agent:{parent}" if parent else "turn")]
        elif kind.startswith("tool_") and event.get("toolUseId"):
            parent = event.get("parentAgentId")
            span_specs = [(f'tool:{event["toolUseId"]}', "tool", f"agent:{parent}" if parent else None)]
        for span_id, span_kind, parent_id in span_specs:
            existing = connection.execute(
                "SELECT started_at,ended_at,status,parent_span_id,last_event_created_at,"
                "terminal_event_at,terminal_event_id FROM agent_trace_spans "
                "WHERE trace_id=? AND span_id=?", (trace_id, span_id),
            ).fetchone()
            is_start = kind in {"turn_started", "subagent_started", "tool_started"}
            is_terminal = kind in {"turn_completed", "turn_interrupted", "subagent_completed", "tool_completed"}
            stamp = event["occurredAt"]
            start = existing["started_at"] if existing else None
            end = existing["ended_at"] if existing else None
            if is_start:
                if start is None or _trace_time(stamp) < _trace_time(start):
                    start = stamp
            if is_terminal and (end is None or _trace_time(stamp) > _trace_time(end)):
                end = stamp
            last_event_created_at = existing["last_event_created_at"] if existing else None
            if (
                last_event_created_at is None
                or _trace_time(event_created_at) > _trace_time(last_event_created_at)
            ):
                last_event_created_at = event_created_at

            terminal_event_at = existing["terminal_event_at"] if existing else None
            terminal_event_id = existing["terminal_event_id"] if existing else None
            status = existing["status"] if existing else "running"
            if is_start and terminal_event_at is None:
                status = event.get("status") or "running"
            if is_terminal:
                event_order = (_trace_time(stamp), event["eventId"])
                prior_order = (
                    (_trace_time(terminal_event_at), terminal_event_id)
                    if terminal_event_at is not None and terminal_event_id is not None
                    else None
                )
                if prior_order is None or event_order > prior_order:
                    if "status" in event:
                        status = event["status"]
                    elif kind == "turn_interrupted":
                        status = "cancelled"
                    elif kind == "turn_completed":
                        status = "success"
                    else:
                        # A stop/completion event proves that the span ended,
                        # but does not prove that the subagent/tool succeeded.
                        status = "unknown"
                    terminal_event_at = stamp
                    terminal_event_id = event["eventId"]
            if existing:
                connection.execute(
                    "UPDATE agent_trace_spans SET started_at=?,ended_at=?,status=?,parent_span_id=?,"
                    "last_event_created_at=?,terminal_event_at=?,terminal_event_id=? "
                    "WHERE trace_id=? AND span_id=?",
                    (
                        start, end, status, existing["parent_span_id"] or parent_id,
                        last_event_created_at, terminal_event_at, terminal_event_id, trace_id, span_id,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO agent_trace_spans(trace_id,span_id,schema_version,kind,parent_span_id,"
                    "started_at,ended_at,status,last_event_created_at,terminal_event_at,terminal_event_id) "
                    "VALUES(?,?,1,?,?,?,?,?,?,?,?)",
                    (
                        trace_id, span_id, span_kind, parent_id, start, end, status,
                        last_event_created_at, terminal_event_at, terminal_event_id,
                    ),
                )

    def list_traces(
        self, *, organization_id: str, project_id: str, limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["organization_id=?", "project_id=?", "expires_at>?"]
        params: list[Any] = [organization_id, project_id, _timestamp(self.clock())]
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            if cursor is not None:
                cursor_row = connection.execute(
                    "SELECT created_at,id FROM agent_traces WHERE id=? AND organization_id=? AND project_id=? AND expires_at>?",
                    (_trace_id(cursor, "cursor"), organization_id, project_id, _timestamp(self.clock())),
                ).fetchone()
                if cursor_row is None:
                    raise DiagnosticError("INVALID_FILTER", "trace cursor is invalid")
                clauses.append("(created_at<? OR (created_at=? AND id<?))")
                params.extend((cursor_row["created_at"], cursor_row["created_at"], cursor_row["id"]))
            params.append(limit + 1)
            rows = connection.execute(
                f"SELECT * FROM agent_traces WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC,id DESC LIMIT ?", tuple(params),
            ).fetchall()
            items = [self._trace_public(connection, row, include_events=False) for row in rows[:limit]]
        return {"items": items, "nextCursor": items[-1]["id"] if len(rows) > limit else None}

    def trace_detail(self, trace_id: str, *, organization_id: str, project_id: str) -> dict[str, Any]:
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_traces WHERE id=? AND organization_id=? AND project_id=? AND expires_at>?",
                (_trace_id(trace_id, "traceId"), organization_id, project_id, _timestamp(self.clock())),
            ).fetchone()
            if row is None:
                raise DiagnosticError("TRACE_NOT_FOUND", "trace was not found", status=404)
            return self._trace_public(connection, row, include_events=True)

    def trace_for_core(self, core_trace_id: str, *, organization_id: str, project_id: str) -> dict[str, Any]:
        """Resolve a verified Core association without exposing foreign subjects."""
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            safe_core_id = _trace_id(core_trace_id, "coreTraceId")
            claim = connection.execute(
                "SELECT trace_id,status FROM agent_trace_core_claims WHERE core_trace_id=?",
                (safe_core_id,),
            ).fetchone()
            if claim is not None and claim["status"] == "ambiguous":
                raise DiagnosticError(
                    "TRACE_LINK_AMBIGUOUS", "Core trace is linked to multiple turns", status=409
                )
            if claim is None or claim["status"] != "verified" or not isinstance(claim["trace_id"], str):
                raise DiagnosticError("TRACE_NOT_FOUND", "trace was not found", status=404)
            row = connection.execute(
                "SELECT t.* FROM agent_traces t "
                "JOIN query_diagnostics d ON d.trace_id=? "
                "AND d.organization_id=t.organization_id AND d.project_id=t.project_id "
                "AND d.subject_id=t.subject_id "
                "WHERE t.id=? AND t.organization_id=? AND t.project_id=? AND t.expires_at>?",
                (
                    safe_core_id, claim["trace_id"], organization_id, project_id,
                    _timestamp(self.clock()),
                ),
            ).fetchone()
            if row is None:
                raise DiagnosticError("TRACE_NOT_FOUND", "trace was not found", status=404)
            return self._trace_public(connection, row, include_events=True)

    def _trace_public(self, connection: Any, row: Mapping[str, Any], *, include_events: bool) -> dict[str, Any]:
        event_rows = connection.execute(
            "SELECT event_json FROM agent_trace_events WHERE trace_id=? AND created_at>? "
            "ORDER BY occurred_at,event_id",
            (row["id"], _timestamp(self.clock() - timedelta(days=TRACE_RETENTION_DAYS))),
        ).fetchall()
        events = [json.loads(item["event_json"]) for item in event_rows]
        claimed = sorted({event["coreTraceId"] for event in events if "coreTraceId" in event})
        verified: set[str] = set()
        issues: list[str] = []
        core_diagnostics: list[dict[str, Any]] = []
        for core_id in claimed:
            claim = connection.execute(
                "SELECT trace_id,status FROM agent_trace_core_claims WHERE core_trace_id=?",
                (core_id,),
            ).fetchone()
            if claim is None or claim["status"] != "verified" or claim["trace_id"] != row["id"]:
                continue
            diagnostic = connection.execute(
                "SELECT id,method,status,stage,duration_ms,phase_spans_json FROM query_diagnostics WHERE trace_id=? AND organization_id=? "
                "AND project_id=? AND subject_id=?",
                (core_id, row["organization_id"], row["project_id"], row["subject_id"]),
            ).fetchone()
            if diagnostic is None:
                continue
            verified.add(core_id)
            feedback = connection.execute(
                "SELECT id FROM query_feedback WHERE diagnostic_id=? AND organization_id=? "
                "AND project_id=? ORDER BY created_at,id",
                (diagnostic["id"], row["organization_id"], row["project_id"]),
            ).fetchall()
            diagnostic_issues = [item["id"] for item in feedback]
            issues.extend(diagnostic_issues)
            core_diagnostics.append({
                "traceId": core_id, "method": diagnostic["method"], "status": diagnostic["status"],
                "stage": diagnostic["stage"], "durationMs": float(diagnostic["duration_ms"] or 0),
                "phaseSpans": json.loads(diagnostic["phase_spans_json"] or "[]"),
                "issueIds": diagnostic_issues,
            })
        safe_events = [
            {key: value for key, value in event.items() if key != "coreTraceId" or value in verified}
            for event in events
        ]
        terminals = [event for event in events if event["type"] in {"turn_completed", "turn_interrupted"}]
        # A child agent's model or counters do not describe the main turn.
        turn_events = [event for event in events if event["type"].startswith("turn_")]
        latest_model = next((event["model"] for event in reversed(turn_events) if "model" in event), None)
        latest_usage = next((event["tokenUsage"] for event in reversed(turn_events) if "tokenUsage" in event), None)
        result: dict[str, Any] = {
            "id": row["id"], "source": row["source"], "sourceSessionId": row["source_session_id"],
            "sourceTurnId": row["source_turn_id"], "subjectId": row["subject_id"],
            "startedAt": events[0]["occurredAt"] if events else None,
            "endedAt": terminals[-1]["occurredAt"] if terminals else None,
            "status": (terminals[-1].get("status") or ("cancelled" if terminals[-1]["type"] == "turn_interrupted" else "success")) if terminals else "running",
            "model": latest_model, "tokenUsage": latest_usage,
            "eventCount": len(events), "issueCount": len(issues),
            "coreTraceIds": sorted(verified), "issueIds": issues,
            "createdAt": row["created_at"], "expiresAt": row["expires_at"],
        }
        if include_events:
            result["events"] = safe_events
            result["coreDiagnostics"] = core_diagnostics
            result["spans"] = [
                {
                    "id": span["span_id"], "schemaVersion": span["schema_version"],
                    "kind": span["kind"], "parentId": span["parent_span_id"],
                    "startedAt": span["started_at"], "endedAt": span["ended_at"],
                    "status": span["status"],
                }
                for span in connection.execute(
                    "SELECT * FROM agent_trace_spans WHERE trace_id=? ORDER BY started_at,span_id",
                    (row["id"],),
                ).fetchall()
            ]
        return result

    def list_feedback(
        self,
        *,
        organization_id: str,
        project_id: str,
        limit: int = 50,
        cursor: str | None = None,
        category: str | None = None,
        status: str | None = None,
        source: str | None = None,
        datasource_id: str | None = None,
        reason_code: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> dict[str, Any]:
        bounded = min(max(int(limit), 1), 100)
        clauses = ["f.organization_id=?", "f.project_id=?"]
        params: list[Any] = [organization_id, project_id]
        connect = getattr(self.access_control, "_connect")
        if cursor:
            safe_cursor = _safe_text(cursor, required=True, limit=128)
            with connect() as connection:
                cursor_row = connection.execute(
                    "SELECT created_at FROM query_feedback WHERE id=? AND organization_id=? AND project_id=?",
                    (safe_cursor, organization_id, project_id),
                ).fetchone()
            if cursor_row is None:
                raise DiagnosticError("INVALID_CURSOR", "diagnostic cursor is invalid")
            clauses.append("(f.created_at<? OR (f.created_at=? AND f.id<?))")
            params.extend([cursor_row["created_at"], cursor_row["created_at"], safe_cursor])
        for column, value, allowed, field_limit in (
            ("f.category", category, _CATEGORIES, 64),
            ("f.status", status, _STATUSES, 64),
        ):
            if value is not None:
                if value not in allowed:
                    raise DiagnosticError("INVALID_FILTER", "diagnostic filter is invalid")
                clauses.append(f"{column}=?")
                params.append(_safe_text(value, required=True, limit=field_limit))
        if source:
            clauses.append("d.transport=?")
            params.append(_safe_text(source, required=True, limit=64))
        if datasource_id:
            clauses.append("d.datasource_id=?")
            params.append(_safe_text(datasource_id, required=True, limit=512))
        if reason_code:
            clauses.append("d.error_json LIKE ?")
            params.append(f'%"reasonCode":"{_safe_text(reason_code, required=True, limit=64)}"%')
        if created_after:
            clauses.append("f.created_at>=?")
            params.append(_filter_timestamp(created_after))
        if created_before:
            clauses.append("f.created_at<=?")
            params.append(_filter_timestamp(created_before))
        params.append(bounded + 1)
        with connect() as connection:
            rows = connection.execute(
                    "SELECT f.*,d.trace_id,d.query_id,d.original_query_id,d.retrieval_trace_id,d.datasource_id,d.transport,d.method,d.stage,d.semantic_version,d.duration_ms,d.policy_versions_json,d.error_json,d.question AS automatic_question,d.semantic_sql AS automatic_semantic_sql,d.native_sql AS automatic_native_sql,d.created_at AS diagnostic_created_at,d.content_purged_at "
                f"FROM query_feedback f JOIN query_diagnostics d ON d.id=f.diagnostic_id WHERE {' AND '.join(clauses)} ORDER BY f.created_at DESC,f.id DESC LIMIT ?",  # noqa: S608 - clauses are server constants
                tuple(params),
            ).fetchall()
        items = [self._feedback_public(row) for row in rows[:bounded]]
        return {"items": items, "nextCursor": items[-1]["id"] if len(rows) > bounded else None}

    def feedback_detail(
        self, feedback_id: str, *, organization_id: str, project_id: str
    ) -> dict[str, Any]:
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            row = connection.execute(
                "SELECT f.*,d.trace_id,d.query_id,d.original_query_id,d.retrieval_trace_id,d.datasource_id,d.transport,d.method,d.stage,d.semantic_version,d.duration_ms,d.policy_versions_json,d.error_json,d.question AS automatic_question,d.semantic_sql AS automatic_semantic_sql,d.native_sql AS automatic_native_sql,d.created_at AS diagnostic_created_at,d.content_purged_at "
                "FROM query_feedback f JOIN query_diagnostics d ON d.id=f.diagnostic_id WHERE f.id=? AND f.organization_id=? AND f.project_id=?",
                (_safe_text(feedback_id, required=True, limit=128), organization_id, project_id),
            ).fetchone()
            if row is None:
                raise DiagnosticError("FEEDBACK_NOT_FOUND", "feedback was not found", status=404)
            history = connection.execute(
                "SELECT * FROM diagnostic_status_history WHERE feedback_id=? ORDER BY created_at,id",
                (feedback_id,),
            ).fetchall()
        result = self._feedback_public(row)
        result["history"] = [
            {
                "id": item["id"],
                "actorSubjectId": item["actor_subject_id"],
                "fromStatus": item["from_status"],
                "toStatus": item["to_status"],
                "note": item["note"],
                "createdAt": item["created_at"],
            }
            for item in history
        ]
        return result

    def update_feedback(
        self,
        feedback_id: str,
        *,
        auth: AuthContext,
        project_id: str,
        status: str,
        category: str | None = None,
        duplicate_of: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if status not in _STATUSES or (category is not None and category not in _CATEGORIES):
            raise DiagnosticError("INVALID_FEEDBACK", "feedback workflow value is invalid")
        connect = getattr(self.access_control, "_connect")
        now = _timestamp(self.clock())
        with connect() as connection:
            current = connection.execute(
                "SELECT status FROM query_feedback WHERE id=? AND organization_id=? AND project_id=?",
                (feedback_id, auth.subject.organization_id, project_id),
            ).fetchone()
            if current is None:
                raise DiagnosticError("FEEDBACK_NOT_FOUND", "feedback was not found", status=404)
            if duplicate_of:
                duplicate = connection.execute(
                    "SELECT 1 FROM query_feedback WHERE id=? AND organization_id=? AND project_id=?",
                    (duplicate_of, auth.subject.organization_id, project_id),
                ).fetchone()
                if duplicate is None or duplicate_of == feedback_id:
                    raise DiagnosticError("INVALID_DUPLICATE", "duplicate feedback reference is invalid")
            connection.execute(
                "UPDATE query_feedback SET status=?,category=COALESCE(?,category),duplicate_of=?,updated_at=? WHERE id=?",
                (status, category, duplicate_of, now, feedback_id),
            )
            connection.execute(
                "INSERT INTO diagnostic_status_history(id,feedback_id,actor_subject_id,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    f"fbe_{uuid.uuid4().hex}",
                    feedback_id,
                    auth.subject.id,
                    current["status"],
                    status,
                    _safe_text(note, limit=4_000),
                    now,
                ),
            )
        return self.feedback_detail(
            feedback_id, organization_id=auth.subject.organization_id, project_id=project_id
        )

    def create_regression_case(
        self,
        *,
        auth: AuthContext,
        project_id: str,
        feedback_id: str,
        case: Mapping[str, Any],
        enable: bool = False,
    ) -> dict[str, Any]:
        allowed = {
            "kind", "question", "clarificationAnswers", "role", "policyTestConfig",
            "semanticVersion", "semanticSnapshot", "testDatasetId", "semanticSql",
            "expectedResult", "expectedError",
        }
        if not isinstance(case, Mapping) or set(case) - allowed:
            raise DiagnosticError("INVALID_REGRESSION_CASE", "regression case is invalid")
        kind = case.get("kind")
        if kind not in {"deterministic_sql", "agent_evidence"}:
            raise DiagnosticError("INVALID_REGRESSION_CASE", "regression case kind is invalid")
        reproducible = bool(
            isinstance(case.get("question"), str)
            and case.get("question")
            and isinstance(case.get("testDatasetId"), str)
            and case.get("testDatasetId")
            and (case.get("expectedResult") is not None or case.get("expectedError") is not None)
            and (kind != "deterministic_sql" or isinstance(case.get("semanticSql"), str))
        )
        if enable and not reproducible:
            raise DiagnosticError(
                "REGRESSION_CASE_NOT_REPRODUCIBLE",
                "a reproducible dataset and assertion are required before enabling the case",
            )
        # Scope-check the source feedback before creating an independently
        # retained, explicitly reviewed case.
        self.feedback_detail(
            feedback_id, organization_id=auth.subject.organization_id, project_id=project_id
        )
        case_id = f"case_{uuid.uuid4().hex}"
        now = _timestamp(self.clock())
        payload = {"schemaVersion": 1, **dict(case)}
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            connection.execute(
                "INSERT INTO regression_cases(id,feedback_id,organization_id,project_id,schema_version,case_json,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    feedback_id,
                    auth.subject.organization_id,
                    project_id,
                    1,
                    _json(payload),
                    "enabled" if enable else "draft",
                    auth.subject.id,
                    now,
                    now,
                ),
            )
        return {"id": case_id, "status": "enabled" if enable else "draft", "case": payload}

    def export_regression_cases(self, *, organization_id: str, project_id: str) -> dict[str, Any]:
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            rows = connection.execute(
                "SELECT id,feedback_id,status,case_json FROM regression_cases WHERE organization_id=? AND project_id=? ORDER BY created_at,id",
                (organization_id, project_id),
            ).fetchall()
        return {
            "schemaVersion": 1,
            "projectId": project_id,
            "cases": [
                {
                    "id": row["id"],
                    "feedbackId": row["feedback_id"],
                    "status": row["status"],
                    **json.loads(row["case_json"]),
                }
                for row in rows
            ],
        }

    @staticmethod
    def _diagnostic_public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "traceId": row["trace_id"],
            "queryId": row["query_id"],
            "originalQueryId": row["original_query_id"],
            "retrievalTraceId": row["retrieval_trace_id"],
            "projectId": row["project_id"],
            "datasourceId": row["datasource_id"],
            "subjectId": row["subject_id"],
            "credentialId": row["credential_id"],
            "transport": row["transport"],
            "method": row["method"],
            "status": row["status"],
            "stage": row["stage"],
            "semanticVersion": row["semantic_version"],
            "durationMs": float(row["duration_ms"] or 0),
            "policyVersions": json.loads(row["policy_versions_json"]),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "question": row["question"],
            "semanticSql": row["semantic_sql"],
            "nativeSql": row["native_sql"],
            "evidenceSource": row["evidence_source"],
            "createdAt": row["created_at"],
            "expiresAt": row["expires_at"],
            "contentPurgedAt": row["content_purged_at"],
        }

    @staticmethod
    def _feedback_public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "diagnosticId": row["diagnostic_id"],
            "traceId": row["trace_id"],
            "queryId": row["query_id"],
            "originalQueryId": row["original_query_id"],
            "retrievalTraceId": row["retrieval_trace_id"],
            "datasourceId": row["datasource_id"],
            "subjectId": row["subject_id"],
            "credentialId": row["credential_id"],
            "transport": row["transport"],
            "method": row["method"],
            "stage": row["stage"],
            "semanticVersion": row["semantic_version"],
            "durationMs": float(row["duration_ms"] or 0),
            "policyVersions": json.loads(row["policy_versions_json"]),
            "category": row["category"],
            "description": row["description"],
            "expectedBehavior": row["expected_behavior"],
            "status": row["status"],
            "duplicateOf": row["duplicate_of"],
            "evidence": {
                "source": row["evidence_source"],
                "question": row["question"] or row["automatic_question"],
                "semanticSql": row["semantic_sql"] or row["automatic_semantic_sql"],
                "nativeSql": row["native_sql"] or row["automatic_native_sql"],
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "contentPurgedAt": row["content_purged_at"],
            },
            "createdAt": row["created_at"],
            "diagnosticCreatedAt": row["diagnostic_created_at"],
            "updatedAt": row["updated_at"],
        }


__all__ = ["DiagnosticError", "DiagnosticStore"]
