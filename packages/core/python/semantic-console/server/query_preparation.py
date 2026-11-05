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
    from .business_evidence import validate_evidence
    from .business_verification import VerificationError, evaluate_query
    from .diagnostics import DiagnosticError
    from .knowledge import RuleStore
    from .project import ProjectError, ProjectStore
except ImportError:  # pragma: no cover
    from access_control import AccessControlStore, AuthContext, _timestamp, _utc_now  # type: ignore[no-redef]
    from business_evidence import validate_evidence  # type: ignore[no-redef]
    from business_verification import VerificationError, evaluate_query  # type: ignore[no-redef]
    from diagnostics import DiagnosticError  # type: ignore[no-redef]
    from knowledge import RuleStore  # type: ignore[no-redef]
    from project import ProjectError, ProjectStore  # type: ignore[no-redef]


class PreparationError(RuntimeError):
    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        status: int = 400,
        business_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status = status
        self.business_evidence = dict(business_evidence) if business_evidence is not None else None


class PreparedBusinessEvidence(dict[str, Any]):
    """Validated template with Python-only bindings for the governed run."""

    __slots__ = ("published_version_id", "datasource_profile_binding_id")

    def __init__(
        self,
        evidence: Mapping[str, Any],
        published_version_id: str,
        datasource_profile_binding_id: str,
    ) -> None:
        super().__init__(evidence)
        self.published_version_id = published_version_id
        self.datasource_profile_binding_id = datasource_profile_binding_id


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
                else:
                    connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS query_preparation_schema_migrations (version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)"
                )
                rows = connection.execute(
                    "SELECT version FROM query_preparation_schema_migrations ORDER BY version"
                ).fetchall()
                applied = [int(row["version"]) for row in rows]
                if applied not in ([], [1], [1, 2], [1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4, 5]):
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
                    applied = [1]
                if applied == [1]:
                    connection.execute(
                        "ALTER TABLE query_preparations ADD COLUMN confirmed_json TEXT NOT NULL DEFAULT '[]'"
                    )
                    connection.execute(
                        "ALTER TABLE query_preparations ADD COLUMN business_evidence_json TEXT"
                    )
                    connection.execute(
                        "INSERT INTO query_preparation_schema_migrations(version,applied_at) VALUES(?,?)",
                        (2, _timestamp(self.clock())),
                    )
                    applied = [1, 2]
                if applied == [1, 2]:
                    connection.execute(
                        "ALTER TABLE query_preparations ADD COLUMN datasource_id TEXT NOT NULL DEFAULT ''"
                    )
                    connection.execute(
                        "INSERT INTO query_preparation_schema_migrations(version,applied_at) VALUES(?,?)",
                        (3, _timestamp(self.clock())),
                    )
                    applied = [1, 2, 3]
                if applied == [1, 2, 3]:
                    connection.execute(
                        "ALTER TABLE query_preparations ADD COLUMN published_version_id TEXT NOT NULL DEFAULT ''"
                    )
                    connection.execute(
                        "INSERT INTO query_preparation_schema_migrations(version,applied_at) VALUES(?,?)",
                        (4, _timestamp(self.clock())),
                    )
                    applied = [1, 2, 3, 4]
                if applied == [1, 2, 3, 4]:
                    connection.execute(
                        "ALTER TABLE query_preparations ADD COLUMN datasource_profile_binding_id TEXT NOT NULL DEFAULT ''"
                    )
                    connection.execute(
                        "INSERT INTO query_preparation_schema_migrations(version,applied_at) VALUES(?,?)",
                        (5, _timestamp(self.clock())),
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

    @staticmethod
    def _business_ready(evidence: Mapping[str, Any]) -> bool:
        checks = evidence.get("checks")
        if not isinstance(checks, list) or any(not isinstance(item, Mapping) for item in checks):
            return False
        return all(
            item.get("status") == "passed"
            or (not item.get("mandatory") and item.get("status") == "unverified")
            for item in checks
        )

    def _applicable(self, semantic_sql: str) -> tuple[str, list[dict[str, Any]]]:
        snapshot = self.rules.list()
        revision = str(snapshot.get("revision") or "")
        configured: list[dict[str, Any]] = []
        for record in snapshot.get("rules", []):
            if not isinstance(record, Mapping) or not record.get("enabled"):
                continue
            if record.get("executionChecks") is not None:
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
        datasource_id = self.project.active_datasource_identifier() or ""
        datasource_profile_binding_id = self.project.datasource_profile_binding_id(datasource_id) or ""
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

        business_evidence: dict[str, Any] | None = None
        business_rules: list[dict[str, Any]] = []
        try:
            published = self.rules.list_published()
            business_rules = [
                dict(record)
                for record in published.get("rules", [])
                if isinstance(record, Mapping)
                and record.get("enabled")
                and not record.get("missing")
                and isinstance(record.get("executionChecks"), Mapping)
            ]
            if business_rules:
                business_evidence = evaluate_query(
                    self.project,
                    semantic_sql,
                    resolved,
                    confirmed_conditions,
                )
        except VerificationError as exc:
            raise PreparationError(exc.code, exc.safe_message, status=409) from exc
        except ProjectError as exc:
            raise PreparationError("PUBLISHED_RULES_UNAVAILABLE", "published business rules are unavailable", status=503) from exc

        if business_evidence is not None:
            try:
                # Keep prepare responses and stored templates within the same
                # bounded, secret-screened contract as persisted query evidence.
                business_evidence = validate_evidence(business_evidence, executed=False)
            except DiagnosticError as exc:
                raise PreparationError(
                    "BUSINESS_EVIDENCE_INVALID",
                    "business verification evidence is unavailable",
                    status=409,
                ) from exc

        if business_evidence is not None:
            for key, value in business_evidence.get("defaultsApplied", {}).items():
                if isinstance(key, str):
                    resolved[key] = value
                    defaults[key] = value
            for item in business_evidence.get("checks", []):
                if not isinstance(item, Mapping):
                    continue
                if item.get("status") == "passed" or (
                    not item.get("mandatory") and item.get("status") == "unverified"
                ):
                    continue
                reason = item.get("reasonCode")
                if item.get("status") == "unverified" and reason in {"time_condition_missing", "time_confirmation_missing"}:
                    matched_spec = next(
                        (
                            spec for rule in business_rules
                            if rule.get("id") == item.get("ruleId")
                            for spec in rule.get("executionChecks", {}).get("checks", [])
                            if isinstance(spec, Mapping)
                            and spec.get("kind") == "timeFilter"
                            and f"{rule.get('id')}:{spec.get('id')}" == item.get("id")
                        ),
                        None,
                    )
                    if isinstance(matched_spec, Mapping) and isinstance(matched_spec.get("conditionKey"), str):
                        key = matched_spec["conditionKey"]
                        if not any(entry.get("conditionKey") == key for entry in clarification):
                            clarification.append({
                                "conditionKey": key,
                                "kind": "timeRange",
                                "question": f"Please confirm the reporting period for {matched_spec.get('column')}.",
                                "options": [],
                                "missingReason": "required" if reason == "time_condition_missing" else "confirmation_required",
                            })
                    else:
                        blocked.append({"conditionKey": "businessVerification", "reason": str(reason or "unverified")})
                else:
                    blocked.append({"conditionKey": "businessVerification", "reason": str(reason or "unverified")})
        status = "blocked" if blocked else "needs_clarification" if clarification else "ready"
        if business_evidence is not None and not self._business_ready(business_evidence) and not blocked and not clarification:
            blocked.append({"conditionKey": "businessVerification", "reason": "mandatory_check_unverified"})
            status = "blocked"
        if business_evidence is not None and business_evidence.get("projectRevision"):
            if not rules:
                revision = str(business_evidence["projectRevision"])
        published_version_id = ""
        if business_evidence is not None:
            published_version_id = self.project.published_version_id or ""
            if not published_version_id:
                raise PreparationError(
                    "PUBLISHED_VERSION_UNAVAILABLE",
                    "published project version is unavailable",
                    status=503,
                )
        preparation_id = f"prep_{uuid.uuid4().hex}"
        now = self.clock().astimezone(UTC)
        # RuntimeRPC holds the ProjectStore execution fence around this call.
        # Keep the store safe for direct callers too: do not persist a ready
        # template if its datasource profile changed during evaluation.
        if (
            datasource_id != (self.project.active_datasource_identifier() or "")
            or datasource_profile_binding_id
            != (self.project.datasource_profile_binding_id(datasource_id) or "")
        ):
            raise PreparationError(
                "BUSINESS_EVIDENCE_STALE",
                "datasource profile changed; prepare again",
                status=409,
            )
        connect = getattr(self.access_control, "_connect")
        with connect() as connection:
            connection.execute(
                "INSERT INTO query_preparations(id,organization_id,project_id,subject_id,credential_id,semantic_sql_hash,rule_revision,status,conditions_json,defaults_json,created_at,expires_at,confirmed_json,business_evidence_json,datasource_id,published_version_id,datasource_profile_binding_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    preparation_id, auth.subject.organization_id, project_id, auth.subject.id,
                    auth.credential_id, self._hash_sql(semantic_sql), revision, status,
                    # Persist only caller-supplied conditions. Published defaults
                    # are returned to the caller, but must be re-derived from
                    # the current published rule during execution revalidation;
                    # persisting them here would make them look like unconfirmed
                    # caller input on the second verification pass.
                    json.dumps(dict(conditions), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(defaults, ensure_ascii=False, separators=(",", ":")),
                    _timestamp(now), _timestamp(now + timedelta(minutes=self.ttl_minutes)),
                    json.dumps(sorted(set(confirmed_conditions)), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(business_evidence, ensure_ascii=False, separators=(",", ":")) if business_evidence is not None else None,
                    datasource_id,
                    published_version_id,
                    datasource_profile_binding_id if business_evidence is not None else "",
                ),
            )
        result = {
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
        if business_evidence is not None:
            result["businessEvidence"] = business_evidence
        return result

    def require_ready(
        self, *, auth: AuthContext, project_id: str, semantic_sql: str, preparation_id: str | None
    ) -> dict[str, Any] | None:
        revision, legacy_rules = self._applicable(semantic_sql)
        try:
            published = self.rules.list_published()
            has_business_rules = any(
                isinstance(record, Mapping)
                and record.get("enabled")
                and not record.get("missing")
                and isinstance(record.get("executionChecks"), Mapping)
                for record in published.get("rules", [])
            )
        except ProjectError as exc:
            raise PreparationError("PUBLISHED_RULES_UNAVAILABLE", "published business rules are unavailable", status=503) from exc
        if not legacy_rules and not has_business_rules:
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
            row is None or row["semantic_sql_hash"] != self._hash_sql(semantic_sql)
            or row["credential_id"] != auth.credential_id
            or row["datasource_id"] != (self.project.active_datasource_identifier() or "")
            or (legacy_rules and row["rule_revision"] != revision) or expiry <= now
        ):
            raise PreparationError("CLARIFICATION_REQUIRED", "query preparation is missing, stale, or not ready", status=409)

        business_evidence: dict[str, Any] | None = None
        if has_business_rules:
            current_datasource_profile_binding_id = self.project.datasource_profile_binding_id(
                self.project.active_datasource_identifier()
            ) or ""
            if row["datasource_profile_binding_id"] != current_datasource_profile_binding_id:
                raise PreparationError(
                    "CLARIFICATION_REQUIRED",
                    "query preparation is missing, stale, or not ready",
                    status=409,
                )
            try:
                stored_conditions = json.loads(str(row["conditions_json"]))
                stored_confirmed = json.loads(str(row["confirmed_json"] or "[]"))
                stored_template = json.loads(str(row["business_evidence_json"])) if row["business_evidence_json"] else None
            except (TypeError, ValueError) as exc:
                raise PreparationError("CLARIFICATION_REQUIRED", "query preparation is missing, stale, or not ready", status=409) from exc
            if not isinstance(stored_conditions, Mapping) or not isinstance(stored_confirmed, list) or not isinstance(stored_template, Mapping):
                raise PreparationError("CLARIFICATION_REQUIRED", "query preparation is missing, stale, or not ready", status=409)
            try:
                business_evidence = evaluate_query(self.project, semantic_sql, stored_conditions, stored_confirmed)
            except VerificationError as exc:
                raise PreparationError(exc.code, exc.safe_message, status=409) from exc
            try:
                business_evidence = validate_evidence(business_evidence, executed=False)
            except DiagnosticError as exc:
                raise PreparationError(
                    "BUSINESS_EVIDENCE_INVALID",
                    "business verification evidence is unavailable",
                    status=409,
                ) from exc
            if not self._business_ready(business_evidence):
                raise PreparationError(
                    "CLARIFICATION_REQUIRED",
                    "published business checks are not satisfied",
                    status=409,
                    business_evidence=business_evidence,
                )
            if row["status"] != "ready":
                raise PreparationError(
                    "CLARIFICATION_REQUIRED",
                    "query preparation is missing, stale, or not ready",
                    status=409,
                    business_evidence=business_evidence,
                )
            if (
                stored_template.get("schemaVersion") != 1
                or stored_template.get("rulesVersion") != business_evidence.get("rulesVersion")
                or stored_template.get("projectRevision") != business_evidence.get("projectRevision")
            ):
                raise PreparationError(
                    "CLARIFICATION_REQUIRED",
                    "query preparation is missing, stale, or not ready",
                    status=409,
                    business_evidence=business_evidence,
                )
            current_version_id = self.project.published_version_id or ""
            if row["published_version_id"] != current_version_id:
                raise PreparationError(
                    "CLARIFICATION_REQUIRED",
                    "query preparation is missing, stale, or not ready",
                    status=409,
                    business_evidence=business_evidence,
                )
            business_evidence = PreparedBusinessEvidence(
                business_evidence,
                str(row["published_version_id"]),
                str(row["datasource_profile_binding_id"]),
            )
        elif row["status"] != "ready":
            raise PreparationError("CLARIFICATION_REQUIRED", "query preparation is missing, stale, or not ready", status=409)
        return business_evidence


__all__ = ["PreparationError", "PreparedBusinessEvidence", "QueryPreparationStore"]
