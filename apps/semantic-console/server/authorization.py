"""Structured, fail-closed authorization decisions for SemaRail resources."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

try:
    from .access_control import BOOTSTRAP_SUBJECT_ID, Subject
except ImportError:  # pragma: no cover - direct module loading
    from access_control import BOOTSTRAP_SUBJECT_ID, Subject  # type: ignore[no-redef]


class PolicyError(Exception):
    """Invalid or insufficient policy data."""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    policy_versions: tuple[str, ...] = ()
    row_filter: Mapping[str, Any] | None = None
    allowed_columns: tuple[str, ...] | None = None
    denied_columns: tuple[str, ...] = ()
    limits: Mapping[str, int] | None = None

    @property
    def version_key(self) -> str:
        return ",".join(self.policy_versions)


_METHOD_SCOPE = {
    "health": "runtime:health",
    "project.validate": "project:validate",
    "project.describe": "semantic:read",
    "context.ask": "semantic:read",
    "query.dryPlan": "query:plan",
    "query.run": "query:execute",
    "query.cancel": "query:cancel",
}
_ALLOWED_OPERATORS = {"eq", "in"}
_LIMIT_FIELDS = {"maxRows", "previewRows", "maxPreviewBytes", "timeoutMs"}
_DOCUMENT_FIELDS = {"schemaVersion", "projects", "tools", "denyTools", "limits", "tables"}
_TABLE_RULE_FIELDS = {"effect", "tenantField", "rows", "columns"}


def _identifier(value: Any, *, wildcard: bool = False) -> bool:
    if wildcard and value == "*":
        return True
    return isinstance(value, str) and bool(value) and all(
        part and part.replace("_", "").isalnum() for part in value.split(".")
    )


def validate_policy_document(document: Any) -> Mapping[str, Any]:
    """Statically validate a version-one policy before it is persisted."""

    if not isinstance(document, Mapping) or set(document) - _DOCUMENT_FIELDS:
        raise PolicyError("policy document has unknown fields")
    if document.get("schemaVersion") != 1:
        raise PolicyError("policy schema version is unsupported")
    for field in ("projects", "tools", "denyTools"):
        values = document.get(field, [])
        if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
            raise PolicyError(f"{field} must be an array of strings")
    _limits([document])
    tables = document.get("tables", {})
    if not isinstance(tables, Mapping):
        raise PolicyError("tables must be an object")
    for table, rule in tables.items():
        if (
            not _identifier(table)
            or "." not in table
            or not isinstance(rule, Mapping)
            or set(rule) - _TABLE_RULE_FIELDS
        ):
            raise PolicyError("table rule is invalid")
        if rule.get("effect", "allow") not in {"allow", "deny"}:
            raise PolicyError("table effect is invalid")
        tenant_field = rule.get("tenantField")
        if tenant_field is not None and not _identifier(tenant_field):
            raise PolicyError("tenant field is invalid")
        rows = rule.get("rows", [])
        if not isinstance(rows, list):
            raise PolicyError("rows must be an array")
        for condition in rows:
            if not isinstance(condition, Mapping) or set(condition) != {"field", "operator", "valueFrom"}:
                raise PolicyError("row condition is invalid")
            if not _identifier(condition.get("field")) or condition.get("operator") not in _ALLOWED_OPERATORS:
                raise PolicyError("row condition is invalid")
            source = condition.get("valueFrom")
            if source not in {"subject.id", "subject.organizationId"} and not (
                isinstance(source, str)
                and source.startswith("subject.attributes.")
                and _identifier(source.removeprefix("subject.attributes."))
                and "." not in source.removeprefix("subject.attributes.")
            ):
                raise PolicyError("valueFrom is not an allowed subject path")
        columns = rule.get("columns", {})
        if not isinstance(columns, Mapping) or set(columns) - {"allow", "deny"}:
            raise PolicyError("columns must be an object")
        for field in ("allow", "deny"):
            values = columns.get(field)
            if values is not None and (
                not isinstance(values, list) or any(not _identifier(item) for item in values)
            ):
                raise PolicyError("column list is invalid")
    return document


def scope_for_method(method: str) -> str:
    try:
        return _METHOD_SCOPE[method]
    except KeyError as exc:
        raise PolicyError("method has no authorization scope") from exc


def _matches(values: Any, target: str) -> bool:
    return isinstance(values, list) and all(isinstance(item, str) for item in values) and ("*" in values or target in values)


def _resolve_subject_value(subject: Subject, path: Any) -> Any:
    if path == "subject.id":
        return subject.id
    if path == "subject.organizationId":
        return subject.organization_id
    prefix = "subject.attributes."
    if isinstance(path, str) and path.startswith(prefix):
        key = path[len(prefix):]
        if not key or "." in key or key not in subject.attributes:
            raise PolicyError("required subject attribute is missing")
        return subject.attributes[key]
    raise PolicyError("valueFrom is not an allowed subject path")


def _normalize_condition(condition: Any, subject: Subject) -> dict[str, Any]:
    if not isinstance(condition, Mapping) or set(condition) != {"field", "operator", "valueFrom"}:
        raise PolicyError("row condition is invalid")
    field = condition.get("field")
    operator = condition.get("operator")
    if not isinstance(field, str) or not field or not all(part.replace("_", "").isalnum() for part in field.split(".")):
        raise PolicyError("row field is invalid")
    if operator not in _ALLOWED_OPERATORS:
        raise PolicyError("row operator is unsupported")
    value = _resolve_subject_value(subject, condition.get("valueFrom"))
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    if not values or any(not isinstance(item, (str, int, float, bool)) for item in values):
        raise PolicyError("row condition resolved to an invalid value")
    if operator == "eq" and len(values) != 1:
        raise PolicyError("eq row condition requires one value")
    return {"field": field, "operator": operator, "values": values}


def _table_rule(document: Mapping[str, Any], table: str) -> Mapping[str, Any] | None:
    tables = document.get("tables", {})
    if not isinstance(tables, Mapping):
        raise PolicyError("tables must be an object")
    rule = tables.get(table, tables.get("*"))
    if rule is None:
        return None
    if not isinstance(rule, Mapping):
        raise PolicyError("table rule must be an object")
    return rule


def _limits(documents: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    combined: dict[str, int] = {}
    for document in documents:
        raw = document.get("limits", {})
        if not isinstance(raw, Mapping):
            raise PolicyError("limits must be an object")
        for key, value in raw.items():
            if key not in _LIMIT_FIELDS or type(value) is not int or value <= 0:
                raise PolicyError("query limit is invalid")
            combined[key] = min(combined.get(key, value), value)
    return combined


class PolicyEngine:
    """Combine bound policies with deny-first and least-privilege semantics."""

    def authorize_method(
        self,
        subject: Subject,
        method: str,
        policies: Sequence[Mapping[str, Any]],
        *,
        project_id: str | None = None,
    ) -> PolicyDecision:
        try:
            return self.authorize_scope(subject, scope_for_method(method), policies, project_id=project_id)
        except PolicyError:
            return PolicyDecision(False, "policy evaluation failed closed")

    def authorize_scope(
        self,
        subject: Subject,
        scope: str,
        policies: Sequence[Mapping[str, Any]],
        *,
        project_id: str | None = None,
    ) -> PolicyDecision:
        """Authorize one stable SemaRail scope."""

        if subject.id == BOOTSTRAP_SUBJECT_ID:
            return PolicyDecision(True, "bootstrap administrator")
        try:
            documents = self._documents(subject, policies)
            if project_id is not None:
                documents = [item for item in documents if _matches(item[0].get("projects", []), project_id)]
                if not documents:
                    return PolicyDecision(False, "project is not allowed")
            if any(_matches(document.get("denyTools", []), scope) for document, _ in documents):
                return PolicyDecision(False, "tool scope is explicitly denied", self._versions(documents))
            allowed = [item for item in documents if _matches(item[0].get("tools", []), scope)]
            if not allowed:
                return PolicyDecision(False, "tool scope is not allowed", self._versions(documents))
            return PolicyDecision(True, "tool scope is allowed", self._versions(allowed), limits=_limits([item[0] for item in allowed]))
        except PolicyError:
            return PolicyDecision(False, "policy evaluation failed closed")

    def authorize_table(
        self,
        subject: Subject,
        table: str,
        policies: Sequence[Mapping[str, Any]],
        *,
        project_id: str | None = None,
    ) -> PolicyDecision:
        if subject.id == BOOTSTRAP_SUBJECT_ID:
            return PolicyDecision(True, "bootstrap administrator")
        try:
            documents = self._documents(subject, policies)
            if project_id is not None:
                documents = [item for item in documents if _matches(item[0].get("projects", []), project_id)]
                if not documents:
                    return PolicyDecision(False, "project is not allowed")
            matching: list[tuple[Mapping[str, Any], Mapping[str, Any], str]] = []
            for document, version in documents:
                rule = _table_rule(document, table)
                if rule is not None:
                    matching.append((document, rule, version))
            if not matching:
                return PolicyDecision(False, "table is not allowed", self._versions(documents))
            if any(rule.get("effect", "allow") == "deny" for _, rule, _ in matching):
                return PolicyDecision(False, "table is explicitly denied", tuple(item[2] for item in matching))

            row_scopes: list[dict[str, Any]] = []
            unrestricted_rows = False
            allow_sets: list[set[str]] = []
            denied: set[str] = set()
            for _, rule, _ in matching:
                if rule.get("effect", "allow") != "allow":
                    raise PolicyError("table effect is invalid")
                tenant_field = rule.get("tenantField")
                required: list[dict[str, Any]] = []
                if tenant_field is not None:
                    required.append(_normalize_condition({"field": tenant_field, "operator": "eq", "valueFrom": "subject.organizationId"}, subject))
                rows = rule.get("rows", [])
                if not isinstance(rows, list):
                    raise PolicyError("rows must be an array")
                required.extend(_normalize_condition(item, subject) for item in rows)
                if required:
                    row_scopes.append({"op": "and", "conditions": required})
                else:
                    unrestricted_rows = True
                columns = rule.get("columns", {})
                if not isinstance(columns, Mapping):
                    raise PolicyError("columns must be an object")
                allow = columns.get("allow")
                deny = columns.get("deny", [])
                if allow is not None:
                    if not isinstance(allow, list) or any(not isinstance(item, str) for item in allow):
                        raise PolicyError("column allow list is invalid")
                    allow_sets.append(set(allow))
                if not isinstance(deny, list) or any(not isinstance(item, str) for item in deny):
                    raise PolicyError("column deny list is invalid")
                denied.update(deny)
            allowed_columns = tuple(sorted(set().union(*allow_sets) - denied)) if allow_sets else None
            row_filter: Mapping[str, Any] | None = (
                None if unrestricted_rows or not row_scopes else {"op": "or", "conditions": row_scopes}
            )
            return PolicyDecision(
                True,
                "table is allowed",
                tuple(item[2] for item in matching),
                row_filter=row_filter,
                allowed_columns=allowed_columns,
                denied_columns=tuple(sorted(denied)),
                limits=_limits([item[0] for item in matching]),
            )
        except PolicyError:
            return PolicyDecision(False, "policy evaluation failed closed")

    def allowed_values(self, decision: PolicyDecision, field: str) -> tuple[Any, ...]:
        """Return the finite allowed values for one field, or fail closed."""

        if not decision.allowed or not decision.row_filter:
            return ()
        scopes = decision.row_filter.get("conditions")
        if not isinstance(scopes, list):
            return ()
        values: list[Any] = []
        for scope in scopes:
            if not isinstance(scope, Mapping):
                return ()
            conditions = scope.get("conditions")
            if not isinstance(conditions, list):
                return ()
            matching = [item for item in conditions if isinstance(item, Mapping) and item.get("field") == field]
            if not matching:
                return ()
            for item in matching:
                raw = item.get("values")
                if not isinstance(raw, list):
                    return ()
                values.extend(raw)
        return tuple(dict.fromkeys(values))

    def compile_data_policy(
        self,
        subject: Subject,
        policies: Sequence[Mapping[str, Any]],
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve bound table rules into a secret-free execution policy."""

        if subject.id == BOOTSTRAP_SUBJECT_ID:
            return {"schemaVersion": 1, "defaultEffect": "allow", "tables": {}, "policyVersions": []}
        try:
            documents = self._documents(subject, policies)
            if project_id is not None:
                documents = [item for item in documents if _matches(item[0].get("projects", []), project_id)]
                if not documents:
                    raise PolicyError("project is not allowed")
            table_names: set[str] = set()
            for document, _ in documents:
                tables = document.get("tables", {})
                if not isinstance(tables, Mapping):
                    raise PolicyError("tables must be an object")
                table_names.update(str(name) for name in tables if name != "*")
            compiled: dict[str, Any] = {}
            for table in sorted(table_names):
                decision = self.authorize_table(subject, table, policies, project_id=project_id)
                if not decision.allowed:
                    continue
                compiled[table] = {
                    "rowFilter": decision.row_filter,
                    "allowedColumns": list(decision.allowed_columns) if decision.allowed_columns is not None else None,
                    "deniedColumns": list(decision.denied_columns),
                }
            return {
                "schemaVersion": 1,
                "defaultEffect": "deny",
                "tables": compiled,
                "policyVersions": sorted(
                    {
                        version
                        for decision_table in table_names
                        for version in self.authorize_table(
                            subject, decision_table, policies, project_id=project_id
                        ).policy_versions
                    }
                ),
            }
        except (PolicyError, TypeError, ValueError) as exc:
            raise PolicyError("data policy compilation failed") from exc

    @staticmethod
    def _documents(subject: Subject, policies: Sequence[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], str]]:
        normalized: list[tuple[Mapping[str, Any], str]] = []
        for policy in policies:
            if policy.get("organizationId") != subject.organization_id or not isinstance(policy.get("document"), Mapping):
                raise PolicyError("policy organization or document is invalid")
            document = policy["document"]
            validate_policy_document(document)
            # JSON round-trip rejects non-serializable policy extensions.
            json.dumps(document, ensure_ascii=False)
            normalized.append((document, f"{policy.get('id')}:{policy.get('version')}"))
        return normalized

    @staticmethod
    def _versions(items: Sequence[tuple[Mapping[str, Any], str]]) -> tuple[str, ...]:
        return tuple(item[1] for item in items)


__all__ = ["PolicyDecision", "PolicyEngine", "PolicyError", "scope_for_method", "validate_policy_document"]
