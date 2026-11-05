"""Private-AST checks for authenticated business-trust knowledge delivery.

The functions in this module are intended for the local coordinator only.
They summarize coverage and revisions; they never return canonical SQL,
model/column names, policy material, context text, or query rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope


_MODEL_NAMES = frozenset({
    "attendance_monthly",
    "compensation_history",
    "departments",
    "employees",
    "performance_reviews",
    "regions",
})
_DENIED_MODELS = {
    "employee": frozenset({"performance_reviews"}),
    "department_manager": frozenset({"compensation_history"}),
    "hrbp": frozenset({"compensation_history"}),
    "compensation_admin": frozenset({"performance_reviews", "attendance_monthly"}),
    "hr_director": frozenset(),
}
_DENIED_COLUMNS = {
    "employee": {"employees": frozenset({"age", "gender"})},
    "department_manager": {"employees": frozenset({"age", "gender"})},
    "hrbp": {},
    "compensation_admin": {},
    "hr_director": {},
}
_EXPECTED_ACTORS = frozenset(_DENIED_MODELS)
_EXPECTED_ORACLE_TYPES = frozenset({"rows", "denial", "blocked"})
_PROJECT_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass
class _ScopeResult:
    models: set[str] = field(default_factory=set)
    columns: set[tuple[str, str]] = field(default_factory=set)
    outputs: list[tuple[str, set[tuple[str, str]]]] = field(default_factory=list)


class _RequirementError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def context_coverage(question: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """Measure direct Context v2 coverage and say whether catalog fallback is needed.

    ``passed`` means every authorized canonical requirement is directly
    present in Context. A well-formed but incomplete retrieval returns
    ``catalogNeeded=True`` so the caller can check ``semarail_list_models``.
    """

    result = _base_result()
    try:
        requirements = _requirements(question)
        result.update(_requirement_counts(requirements))
        models, columns, revision = _projection(context, schema_version=2, context=True)
        result["contextRevision"] = revision
        _record_forbidden_projection(result, models, requirements["actor"], label="context")
        covered_models, covered_columns = _coverage(requirements, models, columns)
        result["contextCoveredModelCount"] = covered_models
        result["contextCoveredColumnCount"] = covered_columns
        if covered_models != result["requiredModelCount"] or covered_columns != result["requiredColumnCount"]:
            result["catalogNeeded"] = True
            result["reasonCodes"].append("required_fields_missing_from_context")
        if revision is None:
            result["reasonCodes"].append("context_revision_missing")
        result["passed"] = (
            not result["catalogNeeded"]
            and revision is not None
            and not result["reasonCodes"]
        )
    except _RequirementError as exc:
        result["reasonCodes"].append(exc.code)
    except Exception:
        result["reasonCodes"].append("context_validation_failed")
    result["reasonCodes"] = _unique_codes(result["reasonCodes"])
    return result


def catalog_coverage(
    question: Mapping[str, Any],
    context: Any,
    catalog: Any,
) -> dict[str, Any]:
    """Check full authorized catalog coverage against Context at one revision.

    The catalog must be the result of an authenticated ``semarail_list_models``
    call for the same subject and project. This helper validates its safe
    response shape and coverage; it does not invoke MCP or infer authorization.
    """

    result = _base_result()
    try:
        requirements = _requirements(question)
        result.update(_requirement_counts(requirements))
        context_models, context_columns, context_revision = _projection(
            context, schema_version=2, context=True
        )
        catalog_models, catalog_columns, catalog_revision = _projection(
            catalog, schema_version=1, context=False
        )
        result["contextRevision"] = context_revision
        result["catalogRevision"] = catalog_revision
        _record_forbidden_projection(result, context_models, requirements["actor"], label="context")
        _record_forbidden_projection(result, catalog_models, requirements["actor"], label="catalog")
        context_model_count, context_column_count = _coverage(
            requirements, context_models, context_columns
        )
        catalog_model_count, catalog_column_count = _coverage(
            requirements, catalog_models, catalog_columns
        )
        result["contextCoveredModelCount"] = context_model_count
        result["contextCoveredColumnCount"] = context_column_count
        result["catalogCoveredModelCount"] = catalog_model_count
        result["catalogCoveredColumnCount"] = catalog_column_count
        result["fallbackUsed"] = (
            context_model_count != result["requiredModelCount"]
            or context_column_count != result["requiredColumnCount"]
        )
        result["catalogNeeded"] = result["fallbackUsed"]
        if context_revision is None:
            result["reasonCodes"].append("context_revision_missing")
        if catalog_revision is None:
            result["reasonCodes"].append("catalog_revision_missing")
        elif context_revision is not None and catalog_revision != context_revision:
            result["reasonCodes"].append("project_revision_mismatch")
        if catalog_model_count != result["requiredModelCount"] or catalog_column_count != result["requiredColumnCount"]:
            result["reasonCodes"].append("required_fields_missing_from_catalog")
        result["passed"] = (
            not result["reasonCodes"]
            and context_revision is not None
            and catalog_revision is not None
            and context_revision == catalog_revision
            and catalog_model_count == result["requiredModelCount"]
            and catalog_column_count == result["requiredColumnCount"]
        )
    except _RequirementError as exc:
        result["reasonCodes"].append(exc.code)
    except Exception:
        result["reasonCodes"].append("catalog_validation_failed")
    result["reasonCodes"] = _unique_codes(result["reasonCodes"])
    return result


def _base_result() -> dict[str, Any]:
    return {
        "passed": False,
        "catalogNeeded": False,
        "fallbackUsed": False,
        "requiredModelCount": 0,
        "requiredColumnCount": 0,
        "contextCoveredModelCount": 0,
        "contextCoveredColumnCount": 0,
        "catalogCoveredModelCount": 0,
        "catalogCoveredColumnCount": 0,
        "denialExcludedModelCount": 0,
        "denialExcludedColumnCount": 0,
        "contextRevision": None,
        "catalogRevision": None,
        "reasonCodes": [],
    }


def _requirements(question: Mapping[str, Any]) -> dict[str, Any]:
    actor = question.get("actor")
    if actor not in _EXPECTED_ACTORS:
        raise _RequirementError("unknown_actor")
    oracle = question.get("oracle")
    oracle_type = oracle.get("type") if isinstance(oracle, Mapping) else None
    if oracle_type not in _EXPECTED_ORACLE_TYPES:
        raise _RequirementError("oracle_type_invalid")
    canonical = question.get("canonical")
    semantic_sql = canonical.get("semanticSql") if isinstance(canonical, Mapping) else None
    if not isinstance(semantic_sql, str) or not semantic_sql.strip():
        if oracle_type == "blocked":
            return {
                "actor": actor,
                "models": set(),
                "columns": set(),
                "deniedModels": set(),
                "deniedColumns": set(),
            }
        raise _RequirementError("canonical_missing")
    try:
        expression = sqlglot.parse_one(semantic_sql, read="postgres")
    except Exception:
        raise _RequirementError("canonical_parse_failed") from None
    if not isinstance(expression, exp.Query):
        raise _RequirementError("canonical_query_unsupported")
    if any(True for _ in expression.find_all(exp.Star)):
        raise _RequirementError("wildcard_unverifiable")
    if any(True for _ in expression.find_all(exp.Into)):
        raise _RequirementError("canonical_query_unsupported")

    raw_models, raw_columns = _resolve_query(expression)
    denied_models = set(_DENIED_MODELS[actor])
    denied_columns_by_model = _DENIED_COLUMNS[actor]
    excluded_models = raw_models & denied_models
    excluded_columns = {
        (model, column)
        for model, column in raw_columns
        if model in denied_models or column in denied_columns_by_model.get(model, frozenset())
    }
    if oracle_type not in {"denial", "blocked"} and (excluded_models or excluded_columns):
        raise _RequirementError("canonical_uses_unauthorized_reference")
    required_models = raw_models - denied_models
    required_columns = {
        (model, column)
        for model, column in raw_columns
        if model not in denied_models
        and column not in denied_columns_by_model.get(model, frozenset())
    }
    return {
        "actor": actor,
        "models": required_models,
        "columns": required_columns,
        "deniedModels": excluded_models,
        "deniedColumns": excluded_columns,
    }


def _resolve_query(expression: exp.Expression) -> tuple[set[str], set[tuple[str, str]]]:
    try:
        scopes = list(traverse_scope(expression))
    except Exception:
        raise _RequirementError("canonical_scope_unsupported") from None
    if not scopes:
        raise _RequirementError("canonical_source_unresolved")
    results: dict[int, _ScopeResult] = {}
    for scope in scopes:
        try:
            if isinstance(scope.expression, exp.Select):
                results[id(scope)] = _resolve_select(scope, results)
            elif isinstance(scope.expression, exp.SetOperation):
                results[id(scope)] = _resolve_set_operation(scope, results)
            else:
                raise _RequirementError("canonical_query_unsupported")
        except _RequirementError:
            raise
        except Exception:
            raise _RequirementError("canonical_source_unresolved") from None
    roots = [results[id(scope)] for scope in scopes if scope.parent is None]
    if not roots:
        raise _RequirementError("canonical_source_unresolved")
    models: set[str] = set()
    columns: set[tuple[str, str]] = set()
    for root in roots:
        models.update(root.models)
        columns.update(root.columns)
    return models, columns


def _resolve_select(scope: Scope, results: Mapping[int, _ScopeResult]) -> _ScopeResult:
    selected: dict[str, tuple[str, Any]] = {}
    result = _ScopeResult()
    for alias, (_node, source) in scope.selected_sources.items():
        normalized_alias = _normalize(alias)
        if not normalized_alias or normalized_alias in selected:
            raise _RequirementError("canonical_source_ambiguous")
        if isinstance(source, exp.Table):
            model = _model_for_table(source)
            selected[normalized_alias] = ("model", model)
            result.models.add(model)
        elif isinstance(source, Scope):
            child = results.get(id(source))
            if child is None:
                raise _RequirementError("canonical_source_unresolved")
            selected[normalized_alias] = ("scope", child)
            result.models.update(child.models)
            result.columns.update(child.columns)
        else:
            raise _RequirementError("canonical_source_unsupported")

    local_column_ids = {id(column) for column in scope.columns}
    outputs: list[tuple[str, set[tuple[str, str]]]] = []
    expression = scope.expression
    # SQLGlot stores USING columns as identifiers rather than ordinary
    # Column nodes. Reject this join form until explicit lineage support
    # exists, so a join key cannot disappear from required-field coverage.
    for join in expression.find_all(exp.Join):
        if join.args.get("using") or join.args.get("method") == "NATURAL":
            raise _RequirementError("canonical_join_unsupported")
    projections = list(expression.selects)
    for projection in projections:
        name = _projection_name(projection)
        lineage: set[tuple[str, str]] = set()
        for column in projection.find_all(exp.Column):
            if id(column) in local_column_ids:
                lineage.update(_resolve_column(column, scope, selected, {}, results))
        outputs.append((name, lineage))
    output_map: dict[str, list[set[tuple[str, str]]]] = {}
    for name, lineage in outputs:
        if name:
            output_map.setdefault(name, []).append(lineage)

    projection_column_ids = {
        id(column)
        for projection in projections
        for column in projection.find_all(exp.Column)
        if id(column) in local_column_ids
    }
    for column in scope.columns:
        if id(column) not in projection_column_ids and not column.table:
            alias_lineage = output_map.get(_normalize(column.name))
            if alias_lineage is not None:
                if len(alias_lineage) != 1:
                    raise _RequirementError("canonical_source_ambiguous")
                result.columns.update(alias_lineage[0])
                continue
        result.columns.update(_resolve_column(column, scope, selected, output_map, results))
    for model, _column in result.columns:
        result.models.add(model)

    cte_aliases = _cte_output_names(scope)
    if cte_aliases is not None:
        if len(cte_aliases) != len(outputs):
            raise _RequirementError("canonical_source_unresolved")
        outputs = [(cte_aliases[index], outputs[index][1]) for index in range(len(outputs))]
    result.outputs = outputs
    return result


def _resolve_set_operation(scope: Scope, results: Mapping[int, _ScopeResult]) -> _ScopeResult:
    branches = [results.get(id(branch)) for branch in scope.union_scopes]
    if not branches or any(branch is None for branch in branches):
        raise _RequirementError("canonical_source_unresolved")
    concrete = [branch for branch in branches if branch is not None]
    width = len(concrete[0].outputs)
    if any(len(branch.outputs) != width for branch in concrete):
        raise _RequirementError("canonical_source_unresolved")
    combined = _ScopeResult()
    for branch in concrete:
        combined.models.update(branch.models)
        combined.columns.update(branch.columns)
    combined.outputs = []
    for index in range(width):
        name = concrete[0].outputs[index][0]
        lineage: set[tuple[str, str]] = set()
        for branch in concrete:
            lineage.update(branch.outputs[index][1])
        combined.outputs.append((name, lineage))
    return combined


def _resolve_column(
    column: exp.Column,
    scope: Scope,
    selected: Mapping[str, tuple[str, Any]],
    output_map: Mapping[str, list[set[tuple[str, str]]]],
    results: Mapping[int, _ScopeResult],
) -> set[tuple[str, str]]:
    column_name = _normalize(column.name)
    if not column_name:
        raise _RequirementError("canonical_source_unresolved")
    if column.table:
        source = _find_qualified_source(_normalize(column.table), scope, selected, results)
        return _resolve_binding(source, column_name)

    alias_lineage = output_map.get(column_name)
    if alias_lineage is not None:
        if len(alias_lineage) != 1:
            raise _RequirementError("canonical_source_ambiguous")
        return set(alias_lineage[0])
    if len(selected) == 1:
        return _resolve_binding(next(iter(selected.values())), column_name)
    if len(selected) > 1:
        raise _RequirementError("unqualified_column_ambiguous")
    raise _RequirementError("canonical_source_unresolved")


def _find_qualified_source(
    alias: str,
    scope: Scope,
    selected: Mapping[str, tuple[str, Any]],
    results: Mapping[int, _ScopeResult],
) -> tuple[str, Any]:
    current: Scope | None = scope
    current_selected: Mapping[str, tuple[str, Any]] = selected
    while current is not None:
        source = current_selected.get(alias)
        if source is not None:
            return source
        parent = current.parent
        if parent is None:
            break
        current = parent
        current_selected = _selected_sources_for_parent(current, results)
    raise _RequirementError("canonical_source_unresolved")


def _selected_sources_for_parent(
    scope: Scope,
    results: Mapping[int, _ScopeResult],
) -> dict[str, tuple[str, Any]]:
    selected: dict[str, tuple[str, Any]] = {}
    for alias, (_node, source) in scope.selected_sources.items():
        normalized_alias = _normalize(alias)
        if isinstance(source, exp.Table):
            selected[normalized_alias] = ("model", _model_for_table(source))
        elif isinstance(source, Scope):
            child = results.get(id(source))
            if child is not None:
                selected[normalized_alias] = ("scope", child)
    return selected


def _resolve_binding(binding: tuple[str, Any], column_name: str) -> set[tuple[str, str]]:
    kind, value = binding
    if kind == "model":
        return {(value, column_name)}
    if kind == "scope":
        matches = [lineage for name, lineage in value.outputs if _normalize(name) == column_name]
        if len(matches) != 1:
            raise _RequirementError("canonical_source_unresolved")
        return set(matches[0])
    raise _RequirementError("canonical_source_unresolved")


def _cte_output_names(scope: Scope) -> list[str] | None:
    parent = scope.expression.parent
    if not isinstance(parent, exp.CTE):
        return None
    alias = parent.args.get("alias")
    columns = alias.args.get("columns") if isinstance(alias, exp.TableAlias) else None
    if not isinstance(columns, list) or not columns:
        return None
    return [_normalize(item.name) for item in columns if isinstance(item, exp.Identifier)]


def _projection_name(projection: exp.Expression) -> str:
    value = projection.alias_or_name
    return _normalize(value) if isinstance(value, str) else ""


def _model_for_table(table: exp.Table) -> str:
    name = _normalize(table.name)
    candidates = [name]
    db = _normalize(table.db)
    catalog = _normalize(table.catalog)
    if db:
        candidates.append(f"{db}.{name}")
    if catalog and db:
        candidates.append(f"{catalog}.{db}.{name}")
    matches = {candidate.rsplit(".", 1)[-1] for candidate in candidates
               if candidate.rsplit(".", 1)[-1] in _MODEL_NAMES}
    if len(matches) != 1:
        raise _RequirementError("unknown_relation")
    return next(iter(matches))


def _projection(
    value: Any,
    *,
    schema_version: int,
    context: bool,
) -> tuple[dict[str, set[str]], set[tuple[str, str]], str | None]:
    if (
        not isinstance(value, Mapping)
        or type(value.get("schemaVersion")) is not int
        or value.get("schemaVersion") != schema_version
    ):
        raise _RequirementError("context_schema_invalid" if context else "catalog_schema_invalid")
    revision = value.get("projectRevision")
    if not isinstance(revision, str) or _PROJECT_REVISION.fullmatch(revision) is None:
        revision = None
    if context:
        schema = value.get("schema")
        raw_models = schema.get("models") if isinstance(schema, Mapping) else None
    else:
        raw_models = value.get("models")
    if not isinstance(raw_models, list) or (not context and not raw_models):
        raise _RequirementError("context_schema_invalid" if context else "catalog_schema_invalid")
    models: dict[str, set[str]] = {}
    columns: set[tuple[str, str]] = set()
    seen_names: set[str] = set()
    alias_owners: dict[str, str] = {}
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            raise _RequirementError("context_schema_invalid" if context else "catalog_schema_invalid")
        name = raw_model.get("name")
        raw_columns = raw_model.get("columns")
        if not isinstance(name, str) or not name.strip() or not isinstance(raw_columns, list):
            raise _RequirementError("context_schema_invalid" if context else "catalog_schema_invalid")
        normalized_name = _normalize(name)
        if normalized_name in seen_names:
            raise _RequirementError("projection_model_ambiguous")
        seen_names.add(normalized_name)
        aliases = {normalized_name, normalized_name.rsplit(".", 1)[-1]}
        table = raw_model.get("table")
        if table is not None:
            if not isinstance(table, str) or not table.strip():
                raise _RequirementError("catalog_schema_invalid")
            aliases.update({_normalize(table), _normalize(table).rsplit(".", 1)[-1]})
        for alias in aliases:
            owner = alias_owners.get(alias)
            if owner is not None and owner != normalized_name:
                raise _RequirementError("projection_model_ambiguous")
            alias_owners[alias] = normalized_name
        column_names: set[str] = set()
        for raw_column in raw_columns:
            if not isinstance(raw_column, Mapping):
                raise _RequirementError("context_schema_invalid" if context else "catalog_schema_invalid")
            column_name = raw_column.get("name")
            if not isinstance(column_name, str) or not column_name.strip():
                raise _RequirementError("context_schema_invalid" if context else "catalog_schema_invalid")
            column_names.add(_normalize(column_name))
        for alias in aliases:
            existing = models.get(alias)
            if existing is not None and existing != column_names:
                raise _RequirementError("projection_model_ambiguous")
            models[alias] = column_names
        model_name = _normalize(name).rsplit(".", 1)[-1]
        for column_name in column_names:
            columns.add((model_name, column_name))
    return models, columns, revision


def _coverage(
    requirements: Mapping[str, Any],
    models: Mapping[str, set[str]],
    columns: set[tuple[str, str]],
) -> tuple[int, int]:
    model_count = 0
    for required_model in requirements["models"]:
        if _lookup_model(models, required_model) is not None:
            model_count += 1
    column_count = 0
    for model, column in requirements["columns"]:
        available = _lookup_model(models, model)
        if available is not None and column in available:
            column_count += 1
    return model_count, column_count


def _lookup_model(models: Mapping[str, set[str]], model: str) -> set[str] | None:
    candidate = _normalize(model).rsplit(".", 1)[-1]
    return models.get(candidate)


def _record_forbidden_projection(
    result: dict[str, Any],
    models: Mapping[str, set[str]],
    actor: str,
    *,
    label: str,
) -> None:
    for forbidden_model in _DENIED_MODELS[actor]:
        if _lookup_model(models, forbidden_model) is not None:
            result["reasonCodes"].append(f"forbidden_{label}_model_present")
            break
    for model, denied_columns in _DENIED_COLUMNS[actor].items():
        available = _lookup_model(models, model)
        if available is not None and available.intersection(denied_columns):
            result["reasonCodes"].append(f"forbidden_{label}_column_present")
            break


def _requirement_counts(requirements: Mapping[str, Any]) -> dict[str, int]:
    return {
        "requiredModelCount": len(requirements["models"]),
        "requiredColumnCount": len(requirements["columns"]),
        "denialExcludedModelCount": len(requirements["deniedModels"]),
        "denialExcludedColumnCount": len(requirements["deniedColumns"]),
    }


def _normalize(value: Any) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _unique_codes(values: list[str]) -> list[str]:
    return sorted({value for value in values if isinstance(value, str) and value})
