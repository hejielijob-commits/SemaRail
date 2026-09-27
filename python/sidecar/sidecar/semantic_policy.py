"""Structural authorization filtering for semantic metadata responses.

Core compiles the subject's policy before crossing into the sidecar. This
module consumes that resolved, secret-free shape at the response boundary. It
never searches or replaces SQL/text: metadata is filtered as records and a
dry plan is admitted only when the existing AST policy can authorize it.
"""

from __future__ import annotations

import re
import math
from collections.abc import Mapping
from typing import Any

from .errors import POLICY_DENIED, RpcFault
from .row_policy import RowPolicyError, apply_row_policy


_MAX_TABLES = 256
_RELATION_TERM = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*$"
)


def filter_semantic_result(
    method: str,
    result: Any,
    policy: Mapping[str, Any],
    *,
    context_catalog: Any = None,
) -> Any:
    """Return the policy-safe projection for a semantic RPC response."""

    rules, unrestricted = _rules(policy)
    if unrestricted:
        return result
    if method in {"project.describe", "context.ask"}:
        if isinstance(result, Mapping) and result.get("schemaVersion") == 2:
            return _filter_context_v2(result, rules, context_catalog)
        return _filter_context(result, rules)
    if method == "query.dryPlan":
        return _filter_dry_plan(result, policy, rules)
    raise _denied()


def semantic_document_visible(document: Any, policy: Mapping[str, Any] | None) -> bool:
    """Return whether a document may participate in retrieval for ``policy``.

    This predicate is intentionally usable by retrieval backends before exact,
    lexical, or vector scoring.  Restricted callers never score (and therefore
    never receive trace influence from) documents whose bindings cannot be
    proven safe.  Model documents that aggregate denied columns are rejected;
    their individually allowed column documents remain searchable.
    """

    if policy is None:
        return True
    try:
        rules, unrestricted = _rules(policy)
    except RpcFault:
        return False
    if unrestricted:
        return True

    def value(wire_name: str, python_name: str) -> Any:
        if isinstance(document, Mapping):
            return document.get(wire_name, document.get(python_name))
        return getattr(document, wire_name, getattr(document, python_name, None))

    def refs(raw: Any, singular: Any = None) -> list[str]:
        if isinstance(raw, (list, tuple)) and all(isinstance(item, str) and item for item in raw):
            return list(raw)
        return [singular] if isinstance(singular, str) and singular else []

    models = refs(value("referencedModels", "referenced_models"), value("model", "model"))
    columns = refs(value("referencedColumns", "referenced_columns"))
    # Unbound free text cannot be proven safe at a restricted boundary.
    if not models:
        return False
    resolved: dict[str, Mapping[str, Any]] = {}
    for model in models:
        rule = _rule_for_name(model, rules)
        if rule is None:
            return False
        resolved[model.lower()] = rule
    for reference in columns:
        pieces = reference.lower().split(".")
        if len(pieces) != 2:
            return False
        model_name, column_name = pieces
        rule = resolved.get(model_name) or _rule_for_name(model_name, rules)
        if rule is None:
            return False
        allowed = rule.get("allowedColumns")
        allowed_set = {str(item).lower() for item in allowed} if isinstance(allowed, list) else None
        denied = {str(item).lower() for item in rule.get("deniedColumns", [])}
        if column_name in denied or (allowed_set is not None and column_name not in allowed_set):
            return False
    return True


def _rules(policy: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], bool]:
    if (
        type(policy.get("schemaVersion")) is not int
        or policy.get("schemaVersion") not in {1, 2}
        or policy.get("defaultEffect") not in {"allow", "deny"}
    ):
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


def _filter_context_v2(
    result: Mapping[str, Any],
    rules: Mapping[str, Mapping[str, Any]],
    context_catalog: Any = None,
) -> dict[str, Any]:
    """Project Context API v2 without discarding provably safe knowledge.

    V1 intentionally keeps its conservative projection for compatibility. V2
    carries explicit semantic bindings on rules, SQL examples, metrics, and
    views, so those records can cross a restricted boundary only when every
    referenced model/column is present in the subject's allowed projection.
    Unbound or textually suspicious records are omitted.
    """

    revision = result.get("projectRevision")
    schema = result.get("schema")
    if not isinstance(revision, str) or not revision or not isinstance(schema, Mapping):
        raise _denied()
    raw_models = schema.get("models")
    if not isinstance(raw_models, list):
        raise _denied()
    # A selected result cannot reveal the names of *other* denied models or
    # physical tables. Only a complete manifest catalog can justify copying
    # arbitrary descriptions into restricted Context; missing means fail closed.
    catalog_forbidden = _catalog_forbidden_identifiers(context_catalog, rules)
    models: list[dict[str, Any]] = []
    model_columns: dict[str, set[str]] = {}
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            continue
        rule = _rule_for_name(raw_model.get("table") or raw_model.get("name"), rules)
        if rule is None:
            continue
        model = _filter_model(raw_model, rule, text_guard=catalog_forbidden)
        if model is not None:
            models.append(model)
            model_columns[model["name"].lower()] = {
                column["name"].lower() for column in model["columns"]
            }

    forbidden_identifiers = _forbidden_context_identifiers(raw_models, model_columns)
    if catalog_forbidden is not None:
        forbidden_identifiers.update(catalog_forbidden)
    relationships = _safe_relationships(result.get("relationships"), model_columns)
    filtered_metrics = _filter_bound_records(result.get("metrics"), model_columns, forbidden_identifiers, kind="metric")
    filtered_rules = _filter_bound_records(result.get("rules"), model_columns, forbidden_identifiers, kind="rule")
    filtered_sql_examples = _filter_bound_records(result.get("sqlExamples"), model_columns, forbidden_identifiers, kind="sqlExample")
    filtered_views = _filter_bound_records(result.get("views"), model_columns, forbidden_identifiers, kind="view")
    trace = _safe_v2_trace(result.get("retrievalTrace"), revision)
    raw_counts = {
        "metrics": len(result.get("metrics", [])) if isinstance(result.get("metrics"), list) else 0,
        "rules": len(result.get("rules", [])) if isinstance(result.get("rules"), list) else 0,
        "sqlExamples": len(result.get("sqlExamples", [])) if isinstance(result.get("sqlExamples"), list) else 0,
        "views": len(result.get("views", [])) if isinstance(result.get("views"), list) else 0,
    }
    filtered_counts = {
        "schema": len(models),
        "relationships": len(relationships),
        "metrics": len(filtered_metrics),
        "rules": len(filtered_rules),
        "sqlExamples": len(filtered_sql_examples),
        "views": len(filtered_views),
    }
    raw_counts["schema"] = len(raw_models)
    raw_counts["relationships"] = len(result.get("relationships", [])) if isinstance(result.get("relationships"), list) else 0
    for item in trace:
        source = item.get("source")
        if source in filtered_counts and raw_counts[source] > filtered_counts[source]:
            item["authorizationFiltered"] = True
    filtered: dict[str, Any] = {
        "schemaVersion": 2,
        "projectRevision": revision,
        "schema": {"models": models},
        "relationships": relationships,
        "metrics": filtered_metrics,
        "rules": filtered_rules,
        "sqlExamples": filtered_sql_examples,
        "views": filtered_views,
        "budgets": _safe_v2_budgets(result.get("budgets")),
        "indexStatus": _safe_v2_index_status(result.get("indexStatus")),
        "retrievalSummary": _safe_v2_summary(result.get("retrievalSummary")),
        "retrievalTrace": trace,
    }
    # Preserve the caller's deterministic budget truncation after filtering;
    # this projection never re-expands a section or adds hidden text.
    return filtered


def _safe_relationships(value: Any, model_columns: Mapping[str, set[str]]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _denied()
    relationships: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name, refs, join_type, condition = (
            item.get("name"), item.get("models"), item.get("joinType"), item.get("condition")
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
    return relationships


def _filter_bound_records(
    value: Any,
    model_columns: Mapping[str, set[str]],
    forbidden_identifiers: set[str],
    *,
    kind: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _denied()
    filtered: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        models = _record_refs(item.get("referencedModels"), item.get("model"))
        columns = _record_refs(item.get("referencedColumns"), item.get("columns"))
        if not models or not _references_allowed(models, columns, model_columns):
            continue
        if _contains_denied_identifier(item, forbidden_identifiers):
            continue
        if kind == "rule":
            allowed_keys = (
                "id", "text", "referencedModels", "referencedColumns", "sourcePath",
                "ruleType", "priority", "mandatory", "effectiveFrom", "allowedRoles",
            )
        elif kind == "sqlExample":
            allowed_keys = (
                "id", "question", "sql", "referencedModels", "referencedColumns",
                "sourcePath", "language", "tags", "reviewed", "dataSource",
                "roles", "version",
            )
        elif kind == "metric":
            allowed_keys = (
                "name", "kind", "expression", "type", "model", "cube", "baseObject",
                "description", "properties", "referencedModels", "referencedColumns",
            )
        else:
            allowed_keys = ("name", "statement", "description", "referencedModels", "referencedColumns")
        filtered.append({key: item[key] for key in allowed_keys if key in item})
    return filtered


def _record_refs(value: Any, singular: Any = None) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return list(value)
    if isinstance(singular, str) and singular:
        return [singular]
    return []


def _references_allowed(
    models: list[str],
    columns: list[str],
    model_columns: Mapping[str, set[str]],
) -> bool:
    if any(model.lower() not in model_columns for model in models):
        return False
    for column in columns:
        pieces = column.lower().split(".")
        if len(pieces) == 2:
            model, name = pieces
            if model not in model_columns or name not in model_columns[model]:
                return False
        elif len(pieces) == 1:
            if sum(pieces[0] in fields for fields in model_columns.values()) != 1:
                return False
        else:
            return False
    return True


def _forbidden_context_identifiers(
    raw_models: list[Any],
    allowed_model_columns: Mapping[str, set[str]],
) -> set[str]:
    forbidden: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            continue
        name = raw_model.get("name")
        table = raw_model.get("table")
        if not isinstance(name, str) or not name:
            continue
        normalized_name = name.lower()
        allowed_columns = allowed_model_columns.get(normalized_name)
        if allowed_columns is None:
            forbidden.add(normalized_name)
            if isinstance(table, str) and table:
                forbidden.update({table.lower(), table.lower().rsplit(".", 1)[-1]})
        raw_columns = raw_model.get("columns", [])
        for raw_column in raw_columns if isinstance(raw_columns, list) else []:
            column = raw_column.get("name") if isinstance(raw_column, Mapping) else None
            if not isinstance(column, str) or not column:
                continue
            normalized_column = column.lower()
            if allowed_columns is None or normalized_column not in allowed_columns:
                forbidden.update({normalized_column, f"{normalized_name}.{normalized_column}"})
    return forbidden


def _catalog_forbidden_identifiers(
    catalog: Any,
    rules: Mapping[str, Mapping[str, Any]],
) -> set[str] | None:
    """Build the restricted-text guard from the *full* manifest catalog.

    A selected Context slice is not a complete denial vocabulary. If the
    provider cannot supply a well-formed full catalog, descriptions remain
    unavailable to restricted callers rather than guessing what is hidden.
    """

    if not isinstance(catalog, list) or not catalog:
        return None
    forbidden: set[str] = set()
    allowed_model_names: set[str] = set()
    for raw_model in catalog:
        if not isinstance(raw_model, Mapping):
            return None
        name, table, columns = (
            raw_model.get("name"), raw_model.get("table"), raw_model.get("columns")
        )
        if not isinstance(name, str) or not name or not isinstance(columns, list) or any(
            not isinstance(column, str) or not column for column in columns
        ) or (table is not None and not isinstance(table, str)):
            return None
        rule = _rule_for_name(table or name, rules) or _rule_for_name(name, rules)
        if isinstance(table, str) and table:
            forbidden.add(table.lower())
            if table.lower().rsplit(".", 1)[-1] != name.lower():
                forbidden.add(table.lower().rsplit(".", 1)[-1])
        if rule is None:
            forbidden.add(name.lower())
        else:
            allowed_model_names.add(name.lower())
        allowed = rule.get("allowedColumns") if rule is not None else None
        allowed_set = {str(item).lower() for item in allowed} if isinstance(allowed, list) else None
        denied = {str(item).lower() for item in rule.get("deniedColumns", [])} if rule is not None else set()
        for column in columns:
            normalized = column.lower()
            if rule is None or normalized in denied or (allowed_set is not None and normalized not in allowed_set):
                forbidden.update({normalized, f"{name.lower()}.{normalized}"})
    # Physical policy relations and row-scope values are not semantic facts.
    # Treat them as sensitive even if the corresponding semantic model is
    # otherwise visible. This also covers permission lookup tables.
    for table, rule in rules.items():
        forbidden.add(table)
        suffix = table.rsplit(".", 1)[-1]
        if suffix not in allowed_model_names:
            forbidden.add(suffix)
        forbidden.update(_policy_row_tokens(rule.get("rowFilter")))
    return forbidden


def _policy_row_tokens(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {
            token.lower()
            for key, item in value.items()
            if key in {"values", "organizationValue", "lookup"}
            for token in _structured_strings(item)
            if token
        } | {
            token for key, item in value.items() if key not in {"values", "organizationValue", "lookup"}
            for token in _policy_row_tokens(item)
        }
    if isinstance(value, list):
        return {token for item in value for token in _policy_row_tokens(item)}
    return set()


def _contains_denied_identifier(item: Mapping[str, Any], forbidden: set[str]) -> bool:
    """Reject arbitrary text containing policy-denied identifiers."""

    text_values = [
        str(value)
        for key, value in item.items()
        if key in {"text", "question", "sql", "expression", "statement", "description"}
        and isinstance(value, str)
    ]
    properties = item.get("properties")
    if isinstance(properties, Mapping):
        text_values.extend(_structured_strings(properties))
    text = " ".join(text_values).lower()
    return any(
        re.search(rf"(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])", text)
        for token in forbidden
        if token
    )


def _structured_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _structured_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _structured_strings(item)]
    return []


def _safe_v2_budgets(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    top_k = value.get("topK")
    if isinstance(top_k, Mapping):
        result["topK"] = {key: top_k[key] for key in ("schema", "relationships", "metrics", "rules", "sqlExamples", "views") if type(top_k.get(key)) is int and 0 <= top_k[key] <= 1_000}
    for key, maximum in (("maxBytes", 4 * 1024 * 1024), ("maxTokens", 256_000), ("maxRelationshipDepth", 8)):
        if type(value.get(key)) is int and 1 <= value[key] <= maximum:
            result[key] = value[key]
    return result


def _safe_v2_index_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": "unavailable", "backend": "none"}
    result: dict[str, Any] = {}
    status = value.get("status", value.get("indexStatus"))
    status = {"active": "ready", "staged": "building"}.get(status, status)
    if status in {"ready", "missing", "stale", "building", "unavailable", "degraded"}:
        result["status"] = status
    else:
        result["status"] = "unavailable"
    if value.get("backend") in {"vector", "lexical", "hybrid", "none"}:
        result["backend"] = value["backend"]
    for key in ("activeRevision", "indexedRevision"):
        if isinstance(value.get(key), str) and 1 <= len(value[key]) <= 256:
            result[key] = value[key]
    if type(value.get("documentCount")) is int and 0 <= value["documentCount"] <= 10_000_000:
        result["documentCount"] = value["documentCount"]
    for key in ("embeddingModelId", "embeddingModelVersion", "lastBuildAt"):
        raw = value.get(key)
        if isinstance(raw, str) and 1 <= len(raw) <= (64 if key == "lastBuildAt" else 256):
            result[key] = raw
    for key in ("embeddingDimension", "indexBuildVersion"):
        raw = value.get(key)
        if type(raw) is int and 0 <= raw <= 1_000_000:
            result[key] = raw
    duration = value.get("buildDurationMs")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and 0 <= duration <= 86_400_000:
        result["buildDurationMs"] = float(duration)
    stale_reason = {
        "revision_mismatch": "revisionMismatch",
        "revision_not_active": "unknown",
        "revision_not_built": "missing",
        "no_revision": "missing",
        "embedding_config_mismatch": "backendUnavailable",
        "embedding_dimension_mismatch": "backendUnavailable",
        "corrupt_active_pointer": "buildFailed",
        "corrupt_partition": "buildFailed",
        "active_partition_missing": "buildFailed",
        "active_pointer_invalid": "buildFailed",
    }.get(value.get("staleReason"), value.get("staleReason"))
    if isinstance(stale_reason, str) and stale_reason.startswith("active_pointer_unreadable:"):
        stale_reason = "buildFailed"
    if stale_reason in {"missing", "revisionMismatch", "backendUnavailable", "buildFailed", "unknown"}:
        result["staleReason"] = stale_reason
    result.setdefault("backend", "none")
    return result


def _safe_v2_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"candidateCount": 0, "filteredCount": 0, "selectedCount": 0, "latencyMs": 0.0}
    summary: dict[str, Any] = {}
    for key in ("candidateCount", "filteredCount", "selectedCount"):
        raw = value.get(key)
        summary[key] = raw if type(raw) is int and 0 <= raw <= 10_000_000 else 0
    latency = value.get("latencyMs")
    summary["latencyMs"] = (
        float(latency)
        if isinstance(latency, (int, float)) and not isinstance(latency, bool)
        and math.isfinite(latency) and 0 <= latency <= 86_400_000
        else 0.0
    )
    fallback = value.get("fallbackReason")
    if fallback in {"embeddingUnavailable", "vectorSearchFailed", "indexDegraded"}:
        summary["fallbackReason"] = fallback
    return summary


def _safe_v2_trace(value: Any, revision: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    safe: list[dict[str, Any]] = []
    sources = {"schema", "relationships", "metrics", "rules", "sqlExamples", "views"}
    types = {"exact", "lexical", "vector", "graph", "ruleBinding", "fallback"}
    reasons = {"exactMatch", "lexicalMatch", "vectorMatch", "graphExpansion", "ruleBinding", "fallback", "permissionFiltered", "budgetLimited"}
    for item in value[:2_000]:
        if not isinstance(item, Mapping) or item.get("source") not in sources or item.get("retrievalType") not in types:
            continue
        trace = {
            "source": item["source"],
            "retrievalType": item["retrievalType"],
            "reasonCode": item.get("reasonCode") if item.get("reasonCode") in reasons else "fallback",
            "projectRevision": revision,
            "authorizationFiltered": bool(item.get("authorizationFiltered", False)),
        }
        document_id = item.get("documentId")
        if isinstance(document_id, str) and 1 <= len(document_id) <= 512:
            trace["documentId"] = document_id
        if isinstance(item.get("relevance"), (int, float)) and not isinstance(item.get("relevance"), bool) and 0 <= item["relevance"] <= 1:
            trace["relevance"] = float(item["relevance"])
        if isinstance(item.get("selected"), bool):
            trace["selected"] = item["selected"]
        safe.append(trace)
    return safe


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


def _filter_model(
    raw_model: Mapping[str, Any],
    rule: Mapping[str, Any],
    *,
    text_guard: set[str] | None = None,
) -> dict[str, Any] | None:
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
        projected_column = {
            key: raw_column[key]
            for key in ("name", "type", "isCalculated", "notNull", "isPrimaryKey", "semanticRole")
            if key in raw_column
        }
        description = raw_column.get("description")
        if text_guard is not None and isinstance(description, str) and not _contains_denied_identifier(
            {"description": description}, text_guard
        ):
            projected_column["description"] = description
        safe_properties = _restricted_semantic_properties(raw_column.get("properties"), denied | (text_guard or set()))
        if safe_properties:
            projected_column["properties"] = safe_properties
        columns.append(projected_column)
    model = {key: raw_model[key] for key in ("name", "table") if key in raw_model}
    description = raw_model.get("description")
    if text_guard is not None and isinstance(description, str) and not _contains_denied_identifier(
        {"description": description}, text_guard
    ):
        model["description"] = description
    safe_model_properties = _restricted_semantic_properties(raw_model.get("properties"), denied | (text_guard or set()))
    if safe_model_properties:
        model["properties"] = safe_model_properties
    model["columns"] = columns
    primary_key = raw_model.get("primaryKey")
    if isinstance(primary_key, str) and any(column.get("name") == primary_key for column in columns):
        model["primaryKey"] = primary_key
    return model


def _restricted_semantic_properties(value: Any, denied: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "displayName", "businessDomain", "dataScope", "format", "unit",
        "acceptedValues", "grain", "timeBasis", "dataRange", "visible",
    }
    properties = {key: value[key] for key in allowed if key in value}
    normalized = " ".join(_structured_strings(properties)).lower()
    if any(identifier and identifier in normalized for identifier in denied):
        return {}
    return properties


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


__all__ = ["filter_semantic_result", "semantic_document_visible"]
