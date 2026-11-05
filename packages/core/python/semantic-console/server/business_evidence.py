"""Core-owned, bounded business evidence, separate from Trace and audit bodies."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import yaml

try:
    from .access_control import AccessControlStore, AuthContext, _timestamp
    from .authorization import PolicyEngine
    from .business_verification import RULES_VERSION
    from .diagnostics import DiagnosticError
    from .project import ProjectStore
except ImportError:  # pragma: no cover
    from access_control import AccessControlStore, AuthContext, _timestamp
    from authorization import PolicyEngine
    from business_verification import RULES_VERSION
    from diagnostics import DiagnosticError
    from project import ProjectStore

_LOCK = 8_341_972_315_443_005
_FIELDS = frozenset({
    "schemaVersion", "rulesVersion", "projectRevision", "references", "models", "columns",
    "metrics", "unit", "grain", "time", "defaultsApplied", "conditions", "confirmations",
    "checks", "queryId", "datasourceId", "outcome", "createdAt", "expiresAt",
})
_SECRET = re.compile(r"(?i)(bearer\s|sr_(?:live|session|key)_|(?:password|token|secret|dsn)\s*[:=]|[a-z]+://|[A-Z]:[\\/])")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,255}\Z")
_OBJECT_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.:-]{0,255}\Z")
_PRIVATE_FIELDS = frozenset({"semanticsql", "nativesql", "rowfilter", "authorizationpolicy", "password", "token", "secret", "prompt", "conversation", "answer", "previewrows", "rows", "physicalpath"})


def validate_evidence(value: Any, *, executed: bool = True) -> dict[str, Any]:
    """Reject unknown schemas, fields, unsafe text and non-JSON data at storage/read."""
    required = _FIELDS if executed else _FIELDS - {"queryId", "datasourceId", "outcome", "createdAt", "expiresAt"}
    if not isinstance(value, Mapping) or set(value) != required or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise DiagnosticError("INVALID_BUSINESS_EVIDENCE", "business evidence schema is invalid")
    def safe(item: Any, depth: int = 0) -> None:
        if depth > 8:
            raise ValueError()
        if isinstance(item, str):
            if len(item) > 2000 or _SECRET.search(item) or re.search(r"(?:^|\s)/[^\s]+", item) or any(ord(c) < 32 for c in item):
                raise ValueError()
        elif isinstance(item, list):
            if len(item) > 256:
                raise ValueError()
            for entry in item:
                safe(entry, depth + 1)
        elif isinstance(item, Mapping):
            if len(item) > 256 or any(not isinstance(key, str) or len(key) > 256 for key in item):
                raise ValueError()
            for key, entry in item.items():
                if key.casefold() in _PRIVATE_FIELDS:
                    raise ValueError()
                safe(key, depth + 1)
                safe(entry, depth + 1)
        elif item is not None and type(item) not in {int, float, bool}:
            raise ValueError()
    try:
        safe(value)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(raw.encode("utf-8")) > 128 * 1024:
            raise ValueError()
        for field in ("rulesVersion", "projectRevision"):
            if not isinstance(value[field], str) or not _IDENTIFIER.fullmatch(value[field]):
                raise ValueError()
        # API object IDs are opaque tokens, not SQL identifiers. Generated
        # datasource IDs can start with a digit or hyphen.
        if executed:
            for field in ("queryId", "datasourceId"):
                if not isinstance(value[field], str) or not _OBJECT_ID.fullmatch(value[field]):
                    raise ValueError()
        for field in ("references", "models", "columns", "metrics", "grain", "checks"):
            if not isinstance(value[field], list):
                raise ValueError()
        for reference in value["references"]:
            if not isinstance(reference, Mapping) or set(reference) != {"ruleId", "model", "column"} or any(not isinstance(reference[k], str) or not _IDENTIFIER.fullmatch(reference[k]) for k in reference):
                raise ValueError()
        for check in value["checks"]:
            if not isinstance(check, Mapping) or set(check) != {"id", "ruleId", "mandatory", "status", "reasonCode"} or type(check["mandatory"]) is not bool or check["status"] not in {"passed", "conflict", "unverified", "stale"}:
                raise ValueError()
            if any(not isinstance(check[k], str) or not _IDENTIFIER.fullmatch(check[k]) for k in ("id", "ruleId", "reasonCode")):
                raise ValueError()
        if not isinstance(value["unit"], Mapping) or set(value["unit"]) - {"status", "value"} or value["unit"].get("status") not in {"verified", "unverified"}:
            raise ValueError()
        if value["unit"].get("value") is not None and not isinstance(value["unit"]["value"], str):
            raise ValueError()
        if not isinstance(value["time"], Mapping) or set(value["time"]) - {"field", "value", "source", "groupedByOriginal"} or value["time"].get("source") not in {"confirmed", "publishedDefault", "sql", "unknown"}:
            raise ValueError()
        if value["time"].get("field") is not None and not isinstance(value["time"]["field"], str):
            raise ValueError()
        if value["time"].get("groupedByOriginal") is not None and type(value["time"]["groupedByOriginal"]) is not bool:
            raise ValueError()
        for field in ("defaultsApplied", "conditions", "confirmations"):
            if not isinstance(value[field], Mapping):
                raise ValueError()
        if any(status not in {"confirmed", "defaulted", "missing"} for status in value["confirmations"].values()):
            raise ValueError()
        if executed and value["outcome"] not in {"success", "failure", "blocked"}:
            raise ValueError()
        return json.loads(raw)
    except (ValueError, TypeError, KeyError) as exc:
        raise DiagnosticError("INVALID_BUSINESS_EVIDENCE", "business evidence fields are invalid") from exc


class BusinessEvidenceStore:
    """SQLite/PostgreSQL evidence ledger with owner and current policy rechecks."""
    def __init__(self, access: AccessControlStore, project: ProjectStore, policy: PolicyEngine) -> None:
        self.access, self.project, self.policy = access, project, policy
        with access._lock, access._connect() as connection:
            if getattr(access, "path", None) is None:
                connection.execute("SELECT pg_advisory_xact_lock(?)", (_LOCK,))
            else:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS business_evidence_schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
            versions = [int(row["version"]) for row in connection.execute("SELECT version FROM business_evidence_schema_migrations ORDER BY version").fetchall()]
            if versions not in ([], [1], [1, 2], [1, 2, 3]):
                raise DiagnosticError("BUSINESS_EVIDENCE_UNAVAILABLE", "business evidence storage schema is incompatible", status=503)
            if not versions:
                connection.execute("CREATE TABLE business_query_evidence(organization_id TEXT NOT NULL,project_id TEXT NOT NULL,query_id TEXT NOT NULL,subject_id TEXT NOT NULL,record_json TEXT NOT NULL,bindings_json TEXT NOT NULL,expires_at TEXT NOT NULL,PRIMARY KEY(organization_id,project_id,query_id))")
                connection.execute("CREATE INDEX business_evidence_expiry_idx ON business_query_evidence(expires_at)")
                connection.execute("INSERT INTO business_evidence_schema_migrations(version,applied_at) VALUES(?,?)", (1, _timestamp(access.clock())))
                versions = [1]
            if versions == [1]:
                connection.execute("ALTER TABLE business_query_evidence ADD COLUMN publication_binding TEXT")
                connection.execute("INSERT INTO business_evidence_schema_migrations(version,applied_at) VALUES(?,?)", (2, _timestamp(access.clock())))
                versions = [1, 2]
            if versions == [1, 2]:
                connection.execute("ALTER TABLE business_query_evidence ADD COLUMN datasource_profile_binding_id TEXT")
                connection.execute("INSERT INTO business_evidence_schema_migrations(version,applied_at) VALUES(?,?)", (3, _timestamp(access.clock())))

    def _bindings(self, evidence: Mapping[str, Any]) -> list[dict[str, str]]:
        models: dict[str, Any] = {}
        for entry in self.project.published_files():
            path = entry.get("path", "")
            if path.startswith("models/") and path.endswith("/metadata.yml"):
                record = yaml.safe_load(self.project.read_published_file(path)["content"])
                if isinstance(record, Mapping):
                    models[str(record.get("name"))] = record
        result = []
        for reference in evidence["references"]:
            record = models.get(reference["model"])
            table = record.get("table_reference", record.get("tableReference")) if record else None
            column = next((c for c in record.get("columns", []) if c.get("name") == reference["column"]), None) if record else None
            if not isinstance(table, Mapping) or not column or column.get("expression") or column.get("relationship"):
                raise DiagnosticError("BUSINESS_EVIDENCE_UNAVAILABLE", "evidence permission binding cannot be established", status=503)
            pieces = [table.get(k) for k in ("catalog", "schema", "table") if table.get(k)]
            if not pieces or any(not isinstance(p, str) or not _IDENTIFIER.fullmatch(p) for p in pieces):
                raise DiagnosticError("BUSINESS_EVIDENCE_UNAVAILABLE", "evidence permission binding is invalid", status=503)
            result.append({"table": ".".join(pieces), "column": reference["column"]})
        return result

    def record(self, *, auth: AuthContext, project_id: str, query_id: str, datasource_id: str, template: Mapping[str, Any], outcome: str, permission_bindings: list[dict[str, str]] | None = None, publication_binding: str | None = None, datasource_profile_binding_id: str | None = None) -> dict[str, Any]:
        now = self.access.clock()
        evidence = validate_evidence({**template, "queryId": query_id, "datasourceId": datasource_id, "outcome": outcome, "createdAt": _timestamp(now), "expiresAt": _timestamp(now + timedelta(days=30))})
        bindings = permission_bindings if permission_bindings is not None else self._bindings(evidence)
        activation = publication_binding or self.project.published_version_id
        if not isinstance(activation, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,255}", activation):
            raise DiagnosticError("BUSINESS_EVIDENCE_STALE", "published activation cannot be established", status=409)
        profile_binding = datasource_profile_binding_id or self.project.datasource_profile_binding_id(datasource_id)
        if not isinstance(profile_binding, str) or not re.fullmatch(r"[a-f0-9]{64}", profile_binding):
            raise DiagnosticError("BUSINESS_EVIDENCE_STALE", "datasource profile binding cannot be established", status=409)
        with self.access._lock, self.access._connect() as connection:
            # Query IDs cannot overwrite another query's evidence, even for its owner.
            try:
                connection.execute("INSERT INTO business_query_evidence(organization_id,project_id,query_id,subject_id,record_json,bindings_json,expires_at,publication_binding,datasource_profile_binding_id) VALUES(?,?,?,?,?,?,?,?,?)", (auth.subject.organization_id, project_id, query_id, auth.subject.id, json.dumps(evidence, ensure_ascii=False), json.dumps(bindings), evidence["expiresAt"], activation, profile_binding))
            except Exception as exc:
                raise DiagnosticError("BUSINESS_EVIDENCE_UNAVAILABLE", "query evidence could not be persisted", status=503) from exc
        self.cleanup_expired()
        return evidence

    def validate_template_permission(self, *, auth: AuthContext, project_id: str, template: Mapping[str, Any], datasource_id: str, publication_binding: str | None = None) -> list[dict[str, str]]:
        """Validate safe bindings before preparation evidence or execution reaches a client."""
        with self.project._lock:
            evidence = validate_evidence(template, executed=False)
            if datasource_id != self.project.active_datasource_identifier():
                raise DiagnosticError("BUSINESS_EVIDENCE_STALE", "active datasource changed; prepare again", status=409)
            if evidence["projectRevision"] != self.project.published_revision:
                raise DiagnosticError("BUSINESS_EVIDENCE_STALE", "published semantic revision changed; prepare again", status=409)
            activation = self.project.published_version_id
            if activation is None or (publication_binding is not None and activation != publication_binding):
                raise DiagnosticError("BUSINESS_EVIDENCE_STALE", "published activation changed; prepare again", status=409)
            bindings = self._bindings(evidence)
        self._check_bindings(auth, project_id, datasource_id, bindings)
        return bindings

    def _check_bindings(self, auth: AuthContext, project_id: str, datasource_id: str, bindings: Any) -> None:
        policies = self.access.policies_for_subject(auth.subject.id)
        if not isinstance(bindings, list) or len(bindings) > 256:
            raise DiagnosticError("INVALID_BUSINESS_EVIDENCE", "evidence permission bindings are invalid")
        for binding in bindings:
            if not isinstance(binding, Mapping) or set(binding) != {"table", "column"} or any(not isinstance(v, str) or not _IDENTIFIER.fullmatch(v) for v in binding.values()):
                raise DiagnosticError("INVALID_BUSINESS_EVIDENCE", "evidence permission bindings are invalid")
            decision = self.policy.authorize_table(auth.subject, binding["table"], policies, project_id=project_id, datasource_id=datasource_id)
            column = binding["column"].lower()
            if not decision.allowed or column in {c.lower() for c in decision.denied_columns} or (decision.allowed_columns is not None and column not in {c.lower() for c in decision.allowed_columns}):
                raise DiagnosticError("POLICY_DENIED", "current data permission is required", status=403)

    def get(self, *, auth: AuthContext, project_id: str, query_id: str, administrator: bool = False) -> dict[str, Any]:
        policies = self.access.policies_for_subject(auth.subject.id)
        if administrator:
            decision = self.policy.authorize_scope(auth.subject, "console:admin", policies, project_id=project_id)
            if not decision.allowed:
                raise DiagnosticError("POLICY_DENIED", "project administration permission is required", status=403)
        with self.access._connect() as connection:
            row = connection.execute("SELECT * FROM business_query_evidence WHERE organization_id=? AND project_id=? AND query_id=?", (auth.subject.organization_id, project_id, query_id)).fetchone()
        if row is None or row["expires_at"] <= _timestamp(self.access.clock()) or (not administrator and row["subject_id"] != auth.subject.id):
            raise DiagnosticError("BUSINESS_EVIDENCE_NOT_FOUND", "query evidence is unavailable", status=404)
        evidence = validate_evidence(json.loads(row["record_json"]))
        datasource = self.project.active_datasource_identifier()
        if datasource != evidence["datasourceId"]:
            raise DiagnosticError("BUSINESS_EVIDENCE_NOT_FOUND", "query evidence is unavailable", status=404)
        if row["datasource_profile_binding_id"] != self.project.datasource_profile_binding_id(str(datasource)):
            # Same-ID connection changes may point to a different dataset. Old
            # evidence without a profile binding must not silently remain valid.
            raise DiagnosticError("BUSINESS_EVIDENCE_NOT_FOUND", "query evidence is unavailable", status=404)
        if not administrator:
            decision = self.policy.authorize_method(auth.subject, "query.evidence", policies, project_id=project_id, datasource_id=datasource)
            if not decision.allowed:
                raise DiagnosticError("POLICY_DENIED", "current datasource permission is required", status=403)
        bindings = json.loads(row["bindings_json"])
        if not isinstance(bindings, list) or len(bindings) != len(evidence["references"]):
            raise DiagnosticError("INVALID_BUSINESS_EVIDENCE", "evidence permission bindings are incomplete")
        self._check_bindings(auth, project_id, str(datasource), bindings)
        if evidence["rulesVersion"] != RULES_VERSION:
            evidence["checks"] = [{**check, "status": "stale", "reasonCode": "RULES_VERSION_CHANGED"} for check in evidence["checks"]]
        elif evidence["projectRevision"] != self.project.published_revision or row["publication_binding"] != self.project.published_version_id:
            evidence["checks"] = [{**check, "status": "stale", "reasonCode": "PUBLISHED_REVISION_CHANGED"} for check in evidence["checks"]]
        return evidence

    def cleanup_expired(self) -> None:
        with self.access._connect() as connection:
            connection.execute("DELETE FROM business_query_evidence WHERE expires_at<=?", (_timestamp(self.access.clock()),))
