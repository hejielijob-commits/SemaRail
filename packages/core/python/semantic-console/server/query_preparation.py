"""Core-owned structured query confirmation and short-lived preparations."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable

try:
    from .access_control import AccessControlStore, AuthContext, _timestamp, _utc_now
    from .knowledge import RuleStore
    from .project import ProjectStore
except ImportError:  # pragma: no cover
    from access_control import AccessControlStore, AuthContext, _timestamp, _utc_now  # type: ignore[no-redef]
    from knowledge import RuleStore  # type: ignore[no-redef]
    from project import ProjectStore  # type: ignore[no-redef]


class PreparationError(RuntimeError):
    def __init__(self, code: str, safe_message: str, *, status: int = 400) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status = status


class QueryPreparationStore:
    _MIGRATION_LOCK_ID = 8_341_972_315_443_003

    def __init__(
        self,
        access_control: AccessControlStore,
        project: ProjectStore,
        *,
        clock: Callable[[], datetime] = _utc_now,
        ttl_minutes: int = 30,
    ) -> None:
        self.access_control = access_control
        self.project = project
        self.rules = RuleStore(project)
        self.clock = getattr(access_control, "clock", clock)
        if type(ttl_minutes) is not int or not 1 <= ttl_minutes <= 120:
            raise ValueError("query preparation TTL is invalid")
        self.ttl_minutes = ttl_minutes
        self._initialize()

    def _initialize(self) -> None:
        connect = getattr(self.access_control, "_connect")
        lock = getattr(self.access_control, "_lock")
        try:
            with lock, connect() as connection:
                if getattr(self.access_control, "path", None) is None:
                    connection.execute("SELECT pg_advisory_xact_lock(?)", (self._MIGRATION_LOCK_ID,))
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS query_preparation_schema_migrations (version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)"
                )
                rows = connection.execute(
                    "SELECT version FROM query_preparation_schema_migrations ORDER BY version"
                ).fetchall()
                applied = [int(row["version"]) for row in rows]
                if applied not in ([], [1]):
                    raise PreparationError(
                        "PREPARATION_SCHEMA_INCOMPATIBLE", "query preparation storage is incompatible", status=503
                    )
                if not applied:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS query_preparations (id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,project_id TEXT NOT NULL,subject_id TEXT NOT NULL,credential_id TEXT,semantic_sql_hash TEXT NOT NULL,rule_revision TEXT NOT NULL,status TEXT NOT NULL,conditions_json TEXT NOT NULL,defaults_json TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS preparation_owner_expiry_idx ON query_preparations(subject_id,project_id,expires_at)"
                    )
                    connection.execute(
                        "INSERT INTO query_preparation_schema_migrations(version,applied_at) VALUES(?,?)",
                        (1, _timestamp(self.clock())),
                    )
        except PreparationError:
            raise
        except Exception as exc:
            raise PreparationError(
                "PREPARATION_STORE_UNAVAILABLE", "query preparation storage is unavailable", status=503
            ) from exc

    @staticmethod
    def _hash_sql(semantic_sql: str) -> str:
        return hashlib.sha256(semantic_sql.encode("utf-8")).hexdigest()

    def _applicable(self, semantic_sql: str) -> tuple[str, list[dict[str, Any]]]:
        snapshot = self.rules.list()
        revision = str(snapshot.get("revision") or "")
        configured: list[dict[str, Any]] = []
        for record in snapshot.get("rules", []):
            if not isinstance(record, Mapping) or not record.get("enabled"):
                continue
            rule = record.get("confirmationRule")
            if not isinstance(rule, Mapping):
                continue
            configured.append(dict(rule))
        if not configured:
            return revision, []
        try:
            from sqlglot import exp, parse  # type: ignore[import-not-found]
            from sqlglot.errors import ErrorLevel  # type: ignore[import-not-found]

            statements = parse(semantic_sql, error_level=ErrorLevel.RAISE)
            if len(statements) != 1:
                raise ValueError("one statement is required")
            root = statements[0]
            cte_names = {
                cte.alias_or_name.casefold()
                for cte in root.find_all(exp.CTE)
                if isinstance(cte.alias_or_name, str) and cte.alias_or_name
            }
            models = {
                table.name.casefold()
                for table in root.find_all(exp.Table)
                if isinstance(table.name, str) and table.name and table.name.casefold() not in cte_names
            }
        except Exception as exc:
            raise PreparationError(
                "CLARIFICATION_REQUIRED",
                "query confirmation requires valid semantic SQL before execution",
                status=409,
            ) from exc
        applicable = [
            rule for rule in configured
            if any(isinstance(model, str) and model.casefold() in models for model in rule.get("models", []))
        ]
        return revision, applicable

    @staticmethod
    def _valid_value(rule: Mapping[str, Any], value: Any) -> bool:
        choices = rule.get("allowedValues")
        if isinstance(choices, list):
            return isinstance(value, str) and value in choices
        value_type = rule.get("valueType")
        if value_type == "integer":
            return type(value) is int and -(2**63) <= value <= 2**63 - 1
        if value_type == "date":
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return False
            try:
                date.fromisoformat(value)
            except ValueError:
                return False
            return True
        if value_type == "dateRange":
            if not isinstance(value, Mapping) or set(value) != {"start", "end"}:
                return False
            try:
                start = date.fromisoformat(value["start"])
                end = date.fromisoformat(value["end"])
            except (TypeError, ValueError):
                return False
            return start <= end
        return isinstance(value, str) and 0 < len(value) <= 1_000

    def prepare(
        self,
        *,
        auth: AuthContext,
        project_id: str,
        question: str,
        semantic_sql: str,
        conditions: Mapping[str, Any],
        confirmed_conditions: list[str],
    ) -> dict[str, Any]:
        if not isinstance(question, str) or not 1 <= len(question) <= 32_000:
            raise PreparationError("INVALID_PARAMS", "question is invalid")
        if not isinstance(semantic_sql, str) or not 1 <= len(semantic_sql) <= 128_000:
            raise PreparationError("INVALID_PARAMS", "semanticSql is invalid")
        if not isinstance(conditions, Mapping) or len(conditions) > 64:
            raise PreparationError("INVALID_PARAMS", "conditions are invalid")
        try:
            encoded_conditions = json.dumps(dict(conditions), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise PreparationError("INVALID_PARAMS", "conditions are invalid") from exc
        if len(encoded_conditions.encode("utf-8")) > 32_000:
            raise PreparationError("INVALID_PARAMS", "conditions are too large")
        if (
            not isinstance(confirmed_conditions, list) or len(confirmed_conditions) > 64
            or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in confirmed_conditions)
        ):
            raise PreparationError("INVALID_PARAMS", "confirmedConditions are invalid")
        revision, rules = self._applicable(semantic_sql)
        resolved = dict(conditions)
        defaults: dict[str, Any] = {}
        clarification: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        confirmed = set(confirmed_conditions)
        for rule in rules:
            key = str(rule["conditionKey"])
            value = resolved.get(key)
            if value is None and "defaultValue" in rule and not rule.get("requireConfirmation"):
                value = rule["defaultValue"]
                resolved[key] = value
                defaults[key] = value
            if value is not None and not self._valid_value(rule, value):
                blocked.append({"conditionKey": key, "reason": "invalid_value"})
                continue
            missing = value is None and bool(rule.get("required", True))
            unconfirmed = value is not None and bool(rule.get("requireConfirmation")) and key not in confirmed
            if missing or unconfirmed:
                clarification.append(
                    {
                        "conditionKey": key,
                        "kind": rule["kind"],
                        "question": rule["prompt"],
                        "options": list(rule.get("allowedValues") or []),
                        "missingReason": "required" if missing else "confirmation_required",
                    }
                )
        status = "blocked" if blocked else "needs_clarification" if clarification else "ready"
        preparation_id = f"prep_{uuid.uuid4().hex}"
        now = self.clock().astimezone(UTC)
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            connection.execute(
                "INSERT INTO query_preparations(id,organization_id,project_id,subject_id,credential_id,semantic_sql_hash,rule_revision,status,conditions_json,defaults_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    preparation_id, auth.subject.organization_id, project_id, auth.subject.id,
                    auth.credential_id, self._hash_sql(semantic_sql), revision, status,
                    json.dumps(resolved, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(defaults, ensure_ascii=False, separators=(",", ":")),
                    _timestamp(now), _timestamp(now + timedelta(minutes=self.ttl_minutes)),
                ),
            )
        return {
            "schemaVersion": 1,
            "status": status,
            "preparationId": preparation_id,
            "conditions": resolved,
            "defaultsApplied": defaults,
            "clarifications": clarification,
            "blockedReasons": blocked,
            "ruleRevision": revision,
            "expiresAt": _timestamp(now + timedelta(minutes=self.ttl_minutes)),
        }

    def require_ready(
        self, *, auth: AuthContext, project_id: str, semantic_sql: str, preparation_id: str | None
    ) -> None:
        revision, rules = self._applicable(semantic_sql)
        if not rules:
            return
        if not isinstance(preparation_id, str):
            raise PreparationError("CLARIFICATION_REQUIRED", "query confirmation is required before execution", status=409)
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            row = connection.execute(
                "SELECT * FROM query_preparations WHERE id=? AND organization_id=? AND project_id=? AND subject_id=?",
                (preparation_id, auth.subject.organization_id, project_id, auth.subject.id),
            ).fetchone()
        now = self.clock().astimezone(UTC)
        try:
            expiry = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00")) if row else now
        except ValueError:
            expiry = now
        if (
            row is None or row["status"] != "ready" or row["semantic_sql_hash"] != self._hash_sql(semantic_sql)
            or row["rule_revision"] != revision or expiry <= now
        ):
            raise PreparationError("CLARIFICATION_REQUIRED", "query preparation is missing, stale, or not ready", status=409)


__all__ = ["PreparationError", "QueryPreparationStore"]
