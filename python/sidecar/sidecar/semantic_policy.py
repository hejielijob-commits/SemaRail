"""Structural authorization filtering for semantic metadata responses.

Core compiles the subject's policy before crossing into the sidecar. This
module consumes that resolved, secret-free shape at the response boundary. It
never searches or replaces SQL/text: metadata is filtered as records and a
dry plan is admitted only when the existing AST policy can authorize it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .errors import POLICY_DENIED, RpcFault
from .row_policy import RowPolicyError, apply_row_policy


_MAX_TABLES = 256
_RELATION_TERM = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*$"
)


def filter_semantic_result(method: str, result: Any, policy: Mapping[str, Any]) -> Any:
    """Return the policy-safe projection for a semantic RPC response."""

    rules, unrestricted = _rules(policy)
    if unrestricted:
        return result
    if method in {"project.describe", "context.ask"}:
        return _filter_context(result, rules)
    if method == "query.dryPlan":
        return _filter_dry_plan(result, policy, rules)
    raise _denied()


def _rules(policy: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], bool]:
    if policy.get("schemaVersion") != 1 or policy.get("defaultEffect") not in {"allow", "deny"}:
        raise _denied()
    raw_rules = policy.get("tables")
    if not isinstance(raw_rules, Mapping) or len(raw_rules) > _MAX_TABLES:
        raise _denied()
    rules: dict[str, Mapping[str, Any]] = {}
    for key, rule in raw_rules.items():
        if not isinstance(key, str) or not key or not isinstance(rule, Mapping):
            raise _denied()
        allowed = rule.get("allowedColumns")
        denied = rule.get("deniedColumns", [])
        if (allowed is not None and (not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed))) or (
            not isinstance(denied, list) or any(not isinstance(item, str) for item in denied)
        ):
            raise _denied()
        rules[key.lower()] = rule
    return rules, policy.get("defaultEffect") == "allow"


def _rule_for_name(name: Any, rules: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not isinstance(name, str) or not name:
        return None
    candidate = name.lower()
    # Match the same fully-qualified -> schema-qualified -> unqualified
    # sequence used by the native SQL policy.
    pieces = candidate.split(".")
    for index in range(len(pieces)):
        direct = rules.get(".".join(pieces[index:]))
        if direct is not None:
            return direct
    # Wren can project an unqualified model source while Core policy uses a
    # schema/catalog key. Resolve only a unique suffix; ambiguity is denied.
    matches = [rule for key, rule in rules.items() if key.endswith(f".{candidate}")]
    return matches[0] if len(matches) == 1 else None


def _filter_context(result: Any, rules: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise _denied()
    raw_models = result.get("models")
    if not isinstance(raw_models, list):
        raise _denied()
    models: list[dict[str, Any]] = []
    model_columns: dict[str, set[str]] = {}
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            continue
        rule = _rule_for_name(raw_model.get("table") or raw_model.get("name"), rules)
        if rule is None:
            continue
        model = _filter_model(raw_model, rule)
        if model is not None:
            models.append(model)
            model_columns[model["name"].lower()] = {
                column["name"].lower() for column in model["columns"]
            }

    # A relationship condition is executable semantic metadata. Retain only
    # the conservative form we can prove refers exclusively to two surviving
    # models and their surviving columns. Everything else is omitted rather
    # than copied across the authorization boundary.
    relationships: list[dict[str, Any]] = []
    raw_relationships = result.get("relationships", [])
    if not isinstance(raw_relationships, list):
        raise _denied()
    for item in raw_relationships:
        if not isinstance(item, Mapping):
            continue
        name, refs, join_type, condition = (
            item.get("name"),
            item.get("models"),
            item.get("joinType"),
            item.get("condition"),
        )
        if (
            isinstance(name, str)
            and isinstance(join_type, str)
            and isinstance(refs, list)
            and len(refs) == 2
            and all(isinstance(model, str) and model.lower() in model_columns for model in refs)
            and isinstance(condition, str)
            and _safe_relationship_condition(condition, refs, model_columns)
        ):
            relationships.append({
                "name": name,
                "models": list(refs),
                "joinType": join_type,
                "condition": condition,
            })

    filtered: dict[str, Any] = {key: result[key] for key in ("schemaVersion", "projectRevision") if key in result}
    filtered["models"] = models
    filtered["relationships"] = relationships
    # Views and unstructured context/recall text can contain arbitrary source
    # SQL or denied identifiers, and are omitted under a restricted policy.
    return filtered


def _safe_relationship_condition(
    condition: str,
    refs: list[Any],
    model_columns: Mapping[str, set[str]],
) -> bool:
    """Admit simple equality joins without leaking denied identifiers/text."""

    if not condition or len(condition) > 16_000:
        return False
    expected = {str(ref).lower() for ref in refs}
    if len(expected) != 2:
        return False
    terms = re.split(r"\s+AND\s+", condition, flags=re.IGNORECASE)
    if not terms:
        return False
    seen: set[str] = set()
    for term in terms:
        match = _RELATION_TERM.fullmatch(term)
        if match is None:
            return False
        left_model, left_column, right_model, right_column = (
            value.lower() for value in match.groups()
        )
        if {left_model, right_model} != expected:
            return False
        if left_column not in model_columns[left_model] or right_column not in model_columns[right_model]:
            return False
        seen.update((left_model, right_model))
    return seen == expected


def _filter_model(raw_model: Mapping[str, Any], rule: Mapping[str, Any]) -> dict[str, Any] | None:
    name = raw_model.get("name")
    if not isinstance(name, str) or not name:
        return None
    allowed = rule.get("allowedColumns")
    allowed_set = {str(item).lower() for item in allowed} if isinstance(allowed, list) else None
    denied = {str(item).lower() for item in rule.get("deniedColumns", [])}
    raw_columns = raw_model.get("columns", [])
    if not isinstance(raw_columns, list):
        return None
    columns: list[dict[str, Any]] = []
    for raw_column in raw_columns:
        if not isinstance(raw_column, Mapping):
            continue
        column_name = raw_column.get("name")
        if not isinstance(column_name, str) or not column_name or column_name.lower() in denied or (
            allowed_set is not None and column_name.lower() not in allowed_set
        ):
            continue
        # Descriptions and calculated expressions are arbitrary source text;
        # either can name a denied physical column. Only typed field metadata
        # crosses a restricted boundary.
        columns.append({
            key: raw_column[key]
            for key in ("name", "type", "isCalculated", "notNull", "isPrimaryKey")
            if key in raw_column
        })
    model = {key: raw_model[key] for key in ("name", "table") if key in raw_model}
    model["columns"] = columns
    primary_key = raw_model.get("primaryKey")
    if isinstance(primary_key, str) and any(column.get("name") == primary_key for column in columns):
        model["primaryKey"] = primary_key
    return model


def _filter_dry_plan(result: Any, policy: Mapping[str, Any], rules: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise _denied()
    native_sql = result.get("nativeSql")
    if not isinstance(native_sql, str) or not native_sql.strip():
        raise _denied()
    try:
        # Parse and validate every physical relation/column through lexical
        # SQL scope. The rewritten SQL is discarded: this is not string
        # filtering or output rewriting.
        apply_row_policy(native_sql, policy)
    except RowPolicyError as exc:
        raise _denied() from exc
    filtered = {key: result[key] for key in ("semanticSql", "nativeSql", "projectRevision") if key in result}
    filtered["allowedPhysical"] = _filter_allowed_physical(result.get("allowedPhysical"), rules)
    return filtered


def _filter_allowed_physical(value: Any, rules: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("tables"), list):
        raise _denied()
    tables: list[dict[str, str]] = []
    for item in value["tables"]:
        if not isinstance(item, Mapping) or not isinstance(item.get("table"), str):
            raise _denied()
        table, schema, catalog = item["table"], item.get("schema"), item.get("catalog")
        if (schema is not None and not isinstance(schema, str)) or (catalog is not None and not isinstance(catalog, str)):
            raise _denied()
        name = ".".join(part for part in (catalog, schema, table) if part)
        if _rule_for_name(name, rules) is None:
            continue
        tables.append({key: item[key] for key in ("catalog", "schema", "table") if key in item})
    return {
        "catalogs": sorted({item["catalog"].lower() for item in tables if "catalog" in item}),
        "schemas": sorted({item["schema"].lower() for item in tables if "schema" in item}),
        "tables": tables,
    }


def _denied() -> RpcFault:
    return RpcFault(POLICY_DENIED, "authorization", "semantic metadata denied by data access policy", False)


__all__ = ["filter_semantic_result"]
