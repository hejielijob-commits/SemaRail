"""Independent query diagnostics, feedback, and regression-case storage.

The store deliberately shares only the configured control-plane database
connection with access control.  Its tables, migration ledger, retention, and
payloads are separate from the audit log.
"""

from __future__ import annotations

import json
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


DIAGNOSTIC_SCHEMA_VERSION = 4
_DIAGNOSTIC_MIGRATION_LOCK_ID = 8_341_972_315_443_002
DEFAULT_RETENTION_DAYS = 30
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
            _safe_text(question) if keep_content else None,
            _safe_text(semantic_sql) if keep_content else None,
            _safe_text(native_sql) if keep_content else None,
            evidence_source,
            _timestamp(now),
            _timestamp(now + timedelta(days=self.retention_days)),
            bounded_duration,
        )
        try:
            connect = getattr(self.access_control, "_connect")
            with connect() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO query_diagnostics(id,trace_id,query_id,original_query_id,organization_id,project_id,datasource_id,subject_id,credential_id,transport,method,status,stage,semantic_version,policy_versions_json,error_json,question,semantic_sql,native_sql,evidence_source,created_at,expires_at,duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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

    def cleanup_expired(self) -> int:
        now = _timestamp(self.clock())
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            cursor = connection.execute(
                "UPDATE query_diagnostics SET error_json=NULL,question=NULL,semantic_sql=NULL,native_sql=NULL,content_purged_at=? WHERE expires_at<=? AND content_purged_at IS NULL",
                (now, now),
            )
        return max(int(cursor.rowcount), 0)

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
                "SELECT f.*,d.trace_id,d.query_id,d.original_query_id,d.datasource_id,d.transport,d.method,d.stage,d.semantic_version,d.duration_ms,d.policy_versions_json,d.error_json,d.question AS automatic_question,d.semantic_sql AS automatic_semantic_sql,d.native_sql AS automatic_native_sql,d.created_at AS diagnostic_created_at,d.content_purged_at "
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
                "SELECT f.*,d.trace_id,d.query_id,d.original_query_id,d.datasource_id,d.transport,d.method,d.stage,d.semantic_version,d.duration_ms,d.policy_versions_json,d.error_json,d.question AS automatic_question,d.semantic_sql AS automatic_semantic_sql,d.native_sql AS automatic_native_sql,d.created_at AS diagnostic_created_at,d.content_purged_at "
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
