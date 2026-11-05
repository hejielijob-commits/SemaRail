"""Published-rule business verification for semantic query execution.

The verifier deliberately accepts a small SQL subset. Anything outside that
subset produces unverified mandatory checks, so complex SQL cannot accidentally
look like a proven business metric.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import io
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

try:
    from .knowledge import RuleStore, validate_execution_checks
    from .project import ProjectError, ProjectStore
except ImportError:  # pragma: no cover - direct module loading
    from knowledge import RuleStore, validate_execution_checks  # type: ignore[no-redef]
    from project import ProjectError, ProjectStore  # type: ignore[no-redef]


RULES_VERSION = "business-trust-v2"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,159}$")
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class VerificationError(RuntimeError):
    """Safe failure raised when published verification inputs are invalid."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def _read_published(project: ProjectStore, path: str) -> str | None:
    try:
        return str(project.read_published_file(path)["content"])
    except ProjectError as exc:
        if exc.code == "FILE_NOT_FOUND":
            return None
        raise VerificationError("PUBLISHED_MDL_UNAVAILABLE", "published semantic metadata is unavailable") from exc


def _load_mdl(
    project: ProjectStore,
) -> tuple[
    dict[str, set[str]],
    dict[str, set[str]],
    list[dict[str, Any]],
    dict[str, str],
    dict[tuple[str, str], str],
]:
    models: dict[str, set[str]] = {}
    physical_columns: dict[str, set[str]] = {}
    model_names: dict[str, str] = {}
    column_names: dict[tuple[str, str], str] = {}
    for item in project.published_files():
        path = item.get("path")
        if not isinstance(path, str) or not re.fullmatch(r"models/[^/]+/metadata\.yml", path):
            continue
        raw = _read_published(project, path)
        if raw is None:
            continue
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise VerificationError("PUBLISHED_MDL_INVALID", "published model metadata is invalid") from exc
        if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
            raise VerificationError("PUBLISHED_MDL_INVALID", "published model metadata is invalid")
        name = str(value["name"])
        if not _NAME.fullmatch(name):
            raise VerificationError("PUBLISHED_MDL_INVALID", "published model metadata is invalid")
        columns = value.get("columns")
        if not isinstance(columns, list):
            raise VerificationError("PUBLISHED_MDL_INVALID", "published model metadata is invalid")
        key = name.casefold()
        if key in models:
            raise VerificationError("PUBLISHED_MDL_INVALID", "published model metadata is invalid")
        names: set[str] = set()
        physical_names: set[str] = set()
        for column in columns:
            if isinstance(column, Mapping) and isinstance(column.get("name"), str) and _NAME.fullmatch(column["name"]):
                column_key = column["name"].casefold()
                if column_key in names:
                    raise VerificationError("PUBLISHED_MDL_INVALID", "published model metadata is invalid")
                names.add(column_key)
                column_names[(key, column_key)] = column["name"]
                # Business evidence can only bind permissions to physical
                # columns. Expressions and relationship projections do not
                # have a safe table/column binding of their own.
                if not column.get("expression") and not column.get("relationship"):
                    physical_names.add(column_key)
        models[key] = names
        physical_columns[key] = physical_names
        model_names[key] = name

    relationships: list[dict[str, Any]] = []
    raw_relationships = _read_published(project, "relationships.yml")
    if raw_relationships is not None:
        try:
            value = yaml.safe_load(raw_relationships)
        except yaml.YAMLError as exc:
            raise VerificationError("PUBLISHED_MDL_INVALID", "published relationship metadata is invalid") from exc
        records = value.get("relationships") if isinstance(value, Mapping) else None
        if not isinstance(records, list):
            raise VerificationError("PUBLISHED_MDL_INVALID", "published relationship metadata is invalid")
        for record in records:
            if isinstance(record, Mapping):
                relationships.append(dict(record))
    return models, physical_columns, relationships, model_names, column_names


def _safe_condition_value(value: Any, value_type: str) -> bool:
    if value_type == "date":
        if not isinstance(value, str) or not _DATE.fullmatch(value):
            return False
        try:
            _datetime.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if value_type == "dateRange":
        if not isinstance(value, Mapping) or set(value) != {"start", "end"}:
            return False
        start, end = value.get("start"), value.get("end")
        if not all(isinstance(item, str) and _DATE.fullmatch(item) for item in (start, end)):
            return False
        try:
            return _datetime.date.fromisoformat(start) <= _datetime.date.fromisoformat(end)
        except ValueError:
            return False
    return False


def _literal_value(node: Any) -> Any:
    """Extract only plain SQL constants, including explicit DATE casts."""

    while type(node).__name__ in {"Paren", "Cast", "TryCast"}:
        if type(node).__name__ in {"Cast", "TryCast"}:
            data_type = node.args.get("to")
            if data_type is not None:
                raw_type = getattr(data_type, "this", "")
                type_name = getattr(raw_type, "value", raw_type)
                if str(type_name).upper() != "DATE":
                    return _MISSING
        node = node.args.get("this")
    name = type(node).__name__
    if name == "Boolean":
        return bool(node.this)
    if name != "Literal":
        return _MISSING
    if node.args.get("is_string"):
        return str(node.this)
    text = str(node.this)
    try:
        if re.fullmatch(r"[-+]?\d+", text):
            return int(text)
        number = float(text)
        return number if math.isfinite(number) else _MISSING
    except ValueError:
        return _MISSING


_MISSING = object()


def _parse_sql(sql: str) -> tuple[Any | None, str | None, Any | None]:
    try:
        from sqlglot import exp, parse  # type: ignore[import-not-found]
        from sqlglot.errors import ErrorLevel  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VerificationError("SQL_VALIDATION_UNAVAILABLE", "SQL verification is unavailable") from exc
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            statements = [item for item in parse(sql, read="postgres", error_level=ErrorLevel.RAISE) if item is not None]
    except Exception:
        return None, "sql_parse_failed", exp
    if len(statements) != 1 or type(statements[0]).__name__ != "Select":
        return None, "unsupported_statement", exp
    root = statements[0]
    denied = {
        "Union", "Intersect", "Except", "Subquery", "CTE", "Window", "Or", "Not",
        "Exists", "Qualify", "Having", "Lateral", "Unnest", "Pivot", "Unpivot",
    }
    if root.args.get("with_") or any(type(node).__name__ in denied for node in root.walk()):
        return None, "unsupported_sql_shape", exp
    from_clause = root.args.get("from_")
    if from_clause is None or type(from_clause.this).__name__ != "Table":
        return None, "unsupported_from", exp
    tables = [from_clause.this, *[join.this for join in root.args.get("joins") or []]]
    if any(type(table).__name__ != "Table" or table.args.get("db") or table.args.get("catalog") for table in tables):
        return None, "unsupported_table_reference", exp
    if not tables or len(tables) > 8:
        return None, "unsupported_table_count", exp
    for join in root.args.get("joins") or []:
        if join.args.get("on") is None or join.args.get("using") or join.args.get("method"):
            return None, "unsupported_join", exp
        if join.args.get("side") not in {None, "INNER", "LEFT", "RIGHT"}:
            return None, "unsupported_join", exp
    return root, None, exp


def _unwrap(node: Any) -> Any:
    while type(node).__name__ in {"Paren", "Alias"}:
        node = node.args.get("this")
    return node


def _table_bindings(root: Any) -> tuple[dict[str, str], list[str]]:
    from_clause = root.args["from_"]
    tables = [from_clause.this, *[join.this for join in root.args.get("joins") or []]]
    aliases: dict[str, str] = {}
    names: list[str] = []
    for table in tables:
        model = str(table.name)
        alias = str(table.alias_or_name or model)
        aliases[alias.casefold()] = model
        aliases[model.casefold()] = model
        names.append(model)
    return aliases, names


def _query_references(
    root: Any,
    aliases: Mapping[str, str],
    models: Sequence[str],
    mdl: Mapping[str, set[str]],
    physical_columns: Mapping[str, set[str]],
    model_names: Mapping[str, str],
    column_names: Mapping[tuple[str, str], str],
    exp: Any,
) -> tuple[set[tuple[str, str]], bool]:
    """Collect SQL-used/displayed columns and whether every use is physically bindable."""

    references: set[tuple[str, str]] = set()

    def add_model_columns(model: str) -> bool:
        key = model.casefold()
        canonical_model = model_names.get(key)
        if canonical_model is None:
            return False
        for column_key in mdl.get(key, set()):
            canonical_column = column_names.get((key, column_key))
            if canonical_column is None:
                continue
            references.add((canonical_model, canonical_column))
        return all(column in physical_columns.get(key, set()) for column in mdl.get(key, set()))

    bindable = True
    # A projected wildcard displays every physical column from its model.
    for projection in root.expressions:
        expression = _unwrap(projection)
        if type(expression).__name__ == "Star":
            for model in models:
                bindable = add_model_columns(model) and bindable
        elif type(expression).__name__ == "Column" and type(expression.args.get("this")).__name__ == "Star":
            table_alias = str(expression.table or "").casefold()
            if table_alias:
                model = aliases.get(table_alias)
                if model is None:
                    bindable = False
                else:
                    bindable = add_model_columns(model) and bindable
            else:
                for model in models:
                    bindable = add_model_columns(model) and bindable

    # ORDER BY / GROUP BY may refer to projected aliases. Their source columns
    # are already visited in the projection, so do not treat the alias itself
    # as a missing physical column.
    output_aliases = {
        str(expression.alias).casefold()
        for expression in root.expressions
        if type(expression).__name__ == "Alias" and expression.alias
    }
    alias_reference_ids: set[int] = set()
    for clause_name in ("order", "group"):
        clause = root.args.get(clause_name)
        if clause is None:
            continue
        for column in clause.find_all(exp.Column):
            if not column.table and str(column.name).casefold() in output_aliases:
                alias_reference_ids.add(id(column))

    for column in root.find_all(exp.Column):
        if type(column.args.get("this")).__name__ == "Star" or id(column) in alias_reference_ids:
            continue
        resolved = _resolve_column(column, aliases, models, mdl)
        if resolved is None:
            bindable = False
            continue
        model_key, column_key = resolved[0].casefold(), resolved[1].casefold()
        canonical_model = model_names.get(model_key)
        canonical_column = column_names.get((model_key, column_key))
        if canonical_model is None:
            bindable = False
            continue
        references.add((canonical_model, canonical_column or str(column.name)))
        if canonical_column is None or column_key not in physical_columns.get(model_key, set()):
            bindable = False
    return references, bindable


def _resolve_column(node: Any, aliases: Mapping[str, str], models: Sequence[str], mdl: Mapping[str, set[str]]) -> tuple[str, str] | None:
    if type(node).__name__ != "Column":
        return None
    column = str(node.name)
    if not _NAME.fullmatch(column):
        return None
    qualifier = str(node.table or "").casefold()
    if qualifier:
        model = aliases.get(qualifier)
        if model is None:
            return None
        return model, column
    candidates = [model for model in models if column.casefold() in mdl.get(model.casefold(), set())]
    if len(candidates) == 1:
        return candidates[0], column
    if len(models) == 1:
        return models[0], column
    return None


def _flatten_and(node: Any) -> list[Any]:
    node = _unwrap(node)
    if type(node).__name__ == "And":
        return _flatten_and(node.args.get("this")) + _flatten_and(node.args.get("expression"))
    return [node]


def _where_terms(root: Any) -> list[Any]:
    where = root.args.get("where")
    return _flatten_and(where.args.get("this")) if where is not None else []


def _column_literal_equality(term: Any, aliases: Mapping[str, str], models: Sequence[str], mdl: Mapping[str, set[str]]) -> tuple[tuple[str, str], Any] | None:
    term = _unwrap(term)
    if type(term).__name__ != "EQ":
        return None
    left, right = term.args.get("this"), term.args.get("expression")
    resolved = _resolve_column(left, aliases, models, mdl)
    value = _literal_value(right)
    if resolved is not None and value is not _MISSING:
        return resolved, value
    resolved = _resolve_column(right, aliases, models, mdl)
    value = _literal_value(left)
    if resolved is not None and value is not _MISSING:
        return resolved, value
    return None


def _predicate_matches(root: Any, ref: tuple[str, str], expected: Any, aliases: Mapping[str, str], models: Sequence[str], mdl: Mapping[str, set[str]]) -> tuple[bool, bool]:
    found = False
    conflict = False
    for term in _where_terms(root):
        term = _unwrap(term)
        comparison_name = type(term).__name__
        column_refs = []
        try:
            from sqlglot import exp  # type: ignore[import-not-found]
            for column in term.find_all(exp.Column):
                resolved = _resolve_column(column, aliases, models, mdl)
                if resolved and (resolved[0].casefold(), resolved[1].casefold()) == (ref[0].casefold(), ref[1].casefold()):
                    column_refs.append(resolved)
        except ImportError:  # pragma: no cover
            pass
        if not column_refs:
            continue
        candidate = _column_literal_equality(term, aliases, models, mdl)
        if comparison_name == "EQ" and candidate is not None and candidate[1] == expected:
            found = True
        else:
            conflict = True
    return found, conflict


def _has_time_predicate(root: Any, ref: tuple[str, str], aliases: Mapping[str, str], models: Sequence[str], mdl: Mapping[str, set[str]]) -> bool:
    try:
        from sqlglot import exp  # type: ignore[import-not-found]
        return any(
            (resolved := _resolve_column(column, aliases, models, mdl)) is not None
            and (resolved[0].casefold(), resolved[1].casefold()) == (ref[0].casefold(), ref[1].casefold())
            for term in _where_terms(root)
            for column in term.find_all(exp.Column)
        )
    except ImportError:  # pragma: no cover
        return False


def _aggregate_column(node: Any) -> tuple[tuple[str, str] | None, bool]:
    value = node.args.get("this")
    distinct = type(value).__name__ == "Distinct"
    if distinct:
        expressions = value.args.get("expressions") or []
        if len(expressions) != 1:
            return None, distinct
        value = expressions[0]
    return value if type(value).__name__ == "Column" else None, distinct


def _projection_aggregates(root: Any, aggregate_types: tuple[type, ...]) -> list[Any]:
    result: list[Any] = []
    for projection in root.expressions:
        expression = _unwrap(projection)
        if isinstance(expression, aggregate_types):
            result.append(expression)
    return result


def _all_projection_aggregates(root: Any, aggregate_types: tuple[type, ...]) -> list[Any]:
    result: list[Any] = []
    seen: set[int] = set()
    for projection in root.expressions:
        expression = _unwrap(projection)
        candidates = ([expression] if isinstance(expression, aggregate_types) else [])
        candidates.extend(expression.find_all(*aggregate_types))
        for candidate in candidates:
            if id(candidate) not in seen:
                seen.add(id(candidate))
                result.append(candidate)
    return result


def _metric_match(root: Any, check: Mapping[str, Any], aliases: Mapping[str, str], models: Sequence[str], mdl: Mapping[str, set[str]]) -> tuple[str, str]:
    expected_model = str(check["model"])
    expected_column = str(check["column"])
    expected_aggregation = str(check["aggregation"]).casefold()
    expected_distinct = bool(check.get("distinct", False))
    relevant = False
    try:
        from sqlglot import exp  # type: ignore[import-not-found]
        aggregate_types = (exp.Count, exp.Avg, exp.Sum, exp.Min, exp.Max)
        for node in _projection_aggregates(root, aggregate_types):
            raw_column, distinct = _aggregate_column(node)
            resolved = _resolve_column(raw_column, aliases, models, mdl) if raw_column is not None else None
            if resolved is None or (resolved[0].casefold(), resolved[1].casefold()) != (expected_model.casefold(), expected_column.casefold()):
                continue
            relevant = True
            actual = {"Count": "count", "Avg": "avg"}.get(type(node).__name__)
            if actual == expected_aggregation and distinct == expected_distinct:
                return "passed", "metric_matches"
    except ImportError:  # pragma: no cover
        return "unverified", "sql_validation_unavailable"
    return ("conflict", "metric_conflicts") if relevant else ("unverified", "metric_missing")


def _group_state(root: Any, ref: tuple[str, str], aliases: Mapping[str, str], models: Sequence[str], mdl: Mapping[str, set[str]]) -> str:
    group = root.args.get("group")
    if group is None:
        return "none"
    projections = list(root.expressions)
    aliases_by_name = {
        str(expression.alias).casefold(): expression.this
        for expression in projections
        if type(expression).__name__ == "Alias" and expression.alias
    }
    includes = False
    all_direct = True
    try:
        from sqlglot import exp  # type: ignore[import-not-found]
        for expression in group.expressions:
            candidate = _unwrap(expression)
            if type(candidate).__name__ == "Literal" and not candidate.args.get("is_string"):
                try:
                    index = int(candidate.this) - 1
                except (ValueError, TypeError):
                    continue
                candidate = _unwrap(projections[index]) if 0 <= index < len(projections) else candidate
            elif type(candidate).__name__ == "Column" and not candidate.table and str(candidate.name).casefold() in aliases_by_name:
                candidate = _unwrap(aliases_by_name[str(candidate.name).casefold()])
            column_nodes = (
                [candidate]
                if type(candidate).__name__ == "Column"
                else list(candidate.find_all(exp.Column)) if hasattr(candidate, "find_all") else []
            )
            resolved_nodes = [
                _resolve_column(column, aliases, models, mdl)
                for column in column_nodes
            ]
            matching = [
                item for item in resolved_nodes
                if item is not None and (item[0].casefold(), item[1].casefold()) == (ref[0].casefold(), ref[1].casefold())
            ]
            if matching:
                includes = True
                if type(candidate).__name__ != "Column" or len(column_nodes) != 1:
                    all_direct = False
        if not includes:
            return "none"
        return "original" if all_direct else "transformed"
    except ImportError:  # pragma: no cover
        return "unknown"


def _time_filter_state(root: Any, ref: tuple[str, str], value_type: str, value: Any, aliases: Mapping[str, str], models: Sequence[str], mdl: Mapping[str, set[str]]) -> tuple[bool, bool, bool]:
    """Return exact-match, conflict, and any-column-predicate flags."""

    terms = _where_terms(root)
    column_seen = False
    if value_type == "date":
        matched, conflict = _predicate_matches(root, ref, value, aliases, models, mdl)
        for term in terms:
            try:
                from sqlglot import exp  # type: ignore[import-not-found]
                if any(
                    (resolved := _resolve_column(column, aliases, models, mdl)) is not None
                    and (resolved[0].casefold(), resolved[1].casefold()) == (ref[0].casefold(), ref[1].casefold())
                    for column in term.find_all(exp.Column)
                ):
                    column_seen = True
            except ImportError:  # pragma: no cover
                pass
        return matched, conflict, column_seen

    wanted = dict(value)
    range_found = False
    lower = upper = False
    for raw_term in terms:
        term = _unwrap(raw_term)
        try:
            from sqlglot import exp  # type: ignore[import-not-found]
            mentions_ref = any(
                (resolved := _resolve_column(column, aliases, models, mdl)) is not None
                and (resolved[0].casefold(), resolved[1].casefold()) == (ref[0].casefold(), ref[1].casefold())
                for column in term.find_all(exp.Column)
            )
        except ImportError:  # pragma: no cover
            return False, False, False
        if not mentions_ref:
            continue
        column_seen = True
        name = type(term).__name__
        if name == "Between":
            resolved = _resolve_column(term.args.get("this"), aliases, models, mdl)
            low = _literal_value(term.args.get("low"))
            high = _literal_value(term.args.get("high"))
            if (
                resolved is not None
                and (resolved[0].casefold(), resolved[1].casefold()) == (ref[0].casefold(), ref[1].casefold())
                and low == wanted.get("start")
                and high == wanted.get("end")
            ):
                range_found = True
                continue
            return False, True, True
        if name in {"GTE", "LTE"}:
            resolved = _resolve_column(term.args.get("this"), aliases, models, mdl)
            bound = _literal_value(term.args.get("expression"))
            if resolved is None or (resolved[0].casefold(), resolved[1].casefold()) != (ref[0].casefold(), ref[1].casefold()):
                return False, True, True
            if name == "GTE" and bound == wanted.get("start"):
                lower = True
                continue
            if name == "LTE" and bound == wanted.get("end"):
                upper = True
                continue
        # Any additional or unsupported predicate on the same date field can
        # narrow or alter the requested range, so it cannot be treated as an
        # exact match.
        return False, True, True
    matched = range_found or (lower and upper)
    return matched, bool(column_seen and not matched), column_seen


def _sql_time_value(root: Any, ref: tuple[str, str], value_type: str, aliases: Mapping[str, str], models: Sequence[str], mdl: Mapping[str, set[str]]) -> Any:
    """Read a safe date constant from SQL for evidence, never as confirmation."""

    if value_type == "date":
        for term in _where_terms(root):
            candidate = _column_literal_equality(term, aliases, models, mdl)
            if candidate and (candidate[0][0].casefold(), candidate[0][1].casefold()) == (ref[0].casefold(), ref[1].casefold()) and _safe_condition_value(candidate[1], "date"):
                return candidate[1]
        return None
    lower = upper = None
    for term in _where_terms(root):
        term = _unwrap(term)
        if type(term).__name__ == "Between":
            resolved = _resolve_column(term.args.get("this"), aliases, models, mdl)
            low, high = _literal_value(term.args.get("low")), _literal_value(term.args.get("high"))
            if resolved and (resolved[0].casefold(), resolved[1].casefold()) == (ref[0].casefold(), ref[1].casefold()):
                value = {"start": low, "end": high}
                return value if _safe_condition_value(value, "dateRange") else None
        if type(term).__name__ not in {"GTE", "LTE"}:
            continue
        resolved = _resolve_column(term.args.get("this"), aliases, models, mdl)
        bound = _literal_value(term.args.get("expression"))
        if resolved and (resolved[0].casefold(), resolved[1].casefold()) == (ref[0].casefold(), ref[1].casefold()) and _safe_condition_value(bound, "date"):
            if type(term).__name__ == "GTE":
                lower = bound
            else:
                upper = bound
    value = {"start": lower, "end": upper}
    return value if lower is not None and upper is not None and _safe_condition_value(value, "dateRange") else None


def _time_summary(fields: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not fields:
        return {"field": None, "value": None, "source": "unknown", "groupedByOriginal": None}
    first = fields[0]
    return {
        "field": first.get("column"),
        "value": first.get("value"),
        "source": first.get("source", "unknown"),
        "groupedByOriginal": first.get("groupedByOriginal"),
    }


def _relationship_edges(relationships: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, frozenset[tuple[str, str]]]]:
    edges: list[tuple[str, str, frozenset[tuple[str, str]]]] = []
    try:
        from sqlglot import exp, parse_one  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        return edges
    for relationship in relationships:
        models = relationship.get("models")
        condition = relationship.get("condition")
        if (
            str(relationship.get("join_type", "")).upper() != "MANY_TO_ONE"
            or not isinstance(models, list) or len(models) != 2
            or not all(isinstance(model, str) and _NAME.fullmatch(model) for model in models)
            or not isinstance(condition, str)
        ):
            continue
        try:
            expression = parse_one(condition, read="postgres")
        except Exception:
            continue
        terms = _flatten_and(expression)
        pairs: set[tuple[str, str]] = set()
        valid = bool(terms)
        for term in terms:
            if type(term).__name__ != "EQ":
                valid = False
                break
            left, right = term.args.get("this"), term.args.get("expression")
            if type(left).__name__ != "Column" or type(right).__name__ != "Column":
                valid = False
                break
            left_ref = (str(left.table).casefold(), str(left.name).casefold())
            right_ref = (str(right.table).casefold(), str(right.name).casefold())
            if {left_ref[0], right_ref[0]} != {str(models[0]).casefold(), str(models[1]).casefold()}:
                valid = False
                break
            pairs.add(tuple(sorted((f"{left_ref[0]}.{left_ref[1]}", f"{right_ref[0]}.{right_ref[1]}"))))
        if valid:
            edges.append((str(models[0]), str(models[1]), frozenset(pairs)))
    return edges


def _query_join_edges(root: Any, aliases: Mapping[str, str], models: Sequence[str], mdl: Mapping[str, set[str]]) -> list[tuple[str, str, frozenset[tuple[str, str]]]] | None:
    parsed: list[tuple[str, str, frozenset[tuple[str, str]]]] = []
    seen = [models[0]]
    try:
        from sqlglot import exp  # type: ignore[import-not-found]
        for join in root.args.get("joins") or []:
            joined_table = join.this
            joined = str(joined_table.name)
            on = join.args.get("on")
            terms = _flatten_and(on)
            pairs: set[tuple[str, str]] = set()
            for term in terms:
                if type(term).__name__ != "EQ":
                    return None
                refs = []
                for side in (term.args.get("this"), term.args.get("expression")):
                    resolved = _resolve_column(side, aliases, models, mdl)
                    if resolved is None:
                        return None
                    refs.append(f"{resolved[0].casefold()}.{resolved[1].casefold()}")
                pairs.add(tuple(sorted(refs)))
            prior = next((model for model in seen if model.casefold() in {item.split(".", 1)[0] for pair in pairs for item in pair} and model.casefold() != joined.casefold()), None)
            if prior is None or not pairs:
                return None
            parsed.append((prior, joined, frozenset(pairs)))
            seen.append(joined)
        return parsed
    except ImportError:  # pragma: no cover
        return None


def _joins_are_declared(root: Any, models: Sequence[str], fact_models: set[str], relationships: Sequence[Mapping[str, Any]], aliases: Mapping[str, str], mdl: Mapping[str, set[str]]) -> bool:
    if not root.args.get("joins"):
        return len(models) == 1
    if len(fact_models) != 1:
        return False
    actual = _query_join_edges(root, aliases, models, mdl)
    if actual is None:
        return False
    declarations = _relationship_edges(relationships)
    used_edges: list[tuple[str, str]] = []
    for prior, joined, pairs in actual:
        match = next((edge for edge in declarations if edge[2] == pairs and {edge[0].casefold(), edge[1].casefold()} == {prior.casefold(), joined.casefold()}), None)
        if match is None:
            return False
        # MANY_TO_ONE is directional: the many side must be reachable from
        # the single fact root, and the one side is the join target.
        used_edges.append((match[0], match[1]))
    root_model = next(iter(fact_models)).casefold()
    reachable = {root_model}
    changed = True
    while changed:
        changed = False
        for many, one in used_edges:
            if many.casefold() in reachable and one.casefold() not in reachable:
                reachable.add(one.casefold())
                changed = True
    return {model.casefold() for model in models} <= reachable


def _check(rule_id: str, spec: Mapping[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {
        "id": f"{rule_id}:{spec['id']}",
        "ruleId": rule_id,
        "mandatory": bool(spec.get("mandatory", True)),
        "status": status,
        "reasonCode": reason,
    }


def evaluate_query(
    project: ProjectStore,
    semantic_sql: str,
    conditions: Mapping[str, Any],
    confirmed_conditions: Sequence[str],
) -> dict[str, Any]:
    """Verify semantic SQL against the currently published rule and MDL state.

    The returned template contains no SQL text, file paths, policy prose,
    credentials, or result rows. A mandatory unverified/conflicting check is
    never reported as ready.
    """

    if not isinstance(semantic_sql, str) or not semantic_sql.strip() or len(semantic_sql) > 128_000:
        raise VerificationError("INVALID_PARAMS", "semanticSql is invalid")
    if not isinstance(conditions, Mapping) or not isinstance(confirmed_conditions, Sequence) or isinstance(confirmed_conditions, (str, bytes)):
        raise VerificationError("INVALID_PARAMS", "business conditions are invalid")
    if len(conditions) > 64 or len(confirmed_conditions) > 64 or any(not isinstance(item, str) or not _KEY.fullmatch(item) for item in confirmed_conditions):
        raise VerificationError("INVALID_PARAMS", "business conditions are invalid")

    try:
        with project._lock:
            snapshot = RuleStore(project).list_published()
            revision = str(snapshot["revision"])
            mdl, physical_columns, relationships, model_names, column_names = _load_mdl(project)
    except (ProjectError, KeyError, TypeError, ValueError) as exc:
        raise VerificationError("PUBLISHED_MDL_UNAVAILABLE", "published business metadata is unavailable") from exc

    configured: list[tuple[str, dict[str, Any]]] = []
    for record in snapshot.get("rules", []):
        if not isinstance(record, Mapping) or not record.get("enabled") or record.get("missing"):
            continue
        raw_checks = record.get("executionChecks")
        if raw_checks is None:
            continue
        try:
            execution_checks = validate_execution_checks(raw_checks)
        except ProjectError as exc:
            raise VerificationError("EXECUTION_CHECKS_INVALID", "published execution checks are invalid") from exc
        rule_id = record.get("id")
        if not isinstance(rule_id, str) or not re.fullmatch(r"rule_[A-Za-z0-9_-]{8,100}", rule_id):
            raise VerificationError("EXECUTION_CHECKS_INVALID", "published execution checks are invalid")
        configured.append((rule_id, execution_checks))

    root, shape_reason, exp = _parse_sql(semantic_sql)
    checks: list[dict[str, Any]] = []
    metric_evidence: list[dict[str, Any]] = []
    time_evidence: list[dict[str, Any]] = []
    grain_items: list[dict[str, Any]] = []
    unit_values: set[str] = set()
    safe_conditions: dict[str, Any] = {}
    defaults_applied: dict[str, Any] = {}
    confirmations: dict[str, str] = {}
    if root is None:
        if configured:
            checks.append({
                "id": f"{RULES_VERSION}:sql-shape",
                "ruleId": RULES_VERSION,
                "mandatory": any(
                    bool(spec.get("mandatory", True))
                    for _rule_id, group in configured
                    for spec in group["checks"]
                ),
                "status": "unverified",
                "reasonCode": shape_reason or "unsupported_sql_shape",
            })
        return {
            "schemaVersion": 1,
            "rulesVersion": RULES_VERSION,
            "projectRevision": revision,
            "references": [],
            "models": [],
            "columns": [],
            "metrics": metric_evidence,
            "unit": {"status": "unverified", "value": None},
            "grain": grain_items,
            "time": _time_summary(time_evidence),
            "defaultsApplied": defaults_applied,
            "conditions": safe_conditions,
            "confirmations": confirmations,
            "checks": checks,
        }

    aliases, query_models = _table_bindings(root)
    query_columns, query_columns_bindable = _query_references(
        root,
        aliases,
        query_models,
        mdl,
        physical_columns,
        model_names,
        column_names,
        exp,
    )
    # AST shape checks are global to the query. The parser above rejects OR,
    # windows, sets, CTEs, and subqueries before any semantic check can pass.
    unknown_models = [model for model in query_models if model.casefold() not in mdl]
    shape_ok = not unknown_models
    if not query_columns_bindable:
        shape_ok = False
        shape_reason = "column_binding_unavailable"
    metric_source_models: set[str] = set()
    has_aggregate_intent = False
    try:
        aggregate_types = (exp.Count, exp.Avg, exp.Sum, exp.Min, exp.Max)
        has_aggregate_intent = any(isinstance(node, aggregate_types) for node in root.walk())
        for node in _all_projection_aggregates(root, aggregate_types):
            raw_column, _distinct = _aggregate_column(node)
            resolved = _resolve_column(raw_column, aliases, query_models, mdl) if raw_column is not None else None
            if resolved is not None:
                metric_source_models.add(resolved[0].casefold())
    except Exception:
        metric_source_models = set()
    if len(metric_source_models) > 1:
        shape_ok = False
        shape_reason = "multiple_fact_models"
    elif root.args.get("joins") and not _joins_are_declared(root, query_models, metric_source_models, relationships, aliases, mdl):
        shape_ok = False
        shape_reason = "join_not_declared_many_to_one"
    if unknown_models:
        shape_reason = "model_not_in_published_mdl"

    query_models_by_name = {model.casefold() for model in query_models}
    query_facts = set(metric_source_models)
    if not query_facts and len(query_models) == 1:
        query_facts.add(query_models[0].casefold())
    applicable: list[tuple[str, dict[str, Any]]] = []
    for rule_id, execution_checks in configured:
        specs = execution_checks["checks"]
        metric_models = {
            str(spec["model"]).casefold()
            for spec in specs
            if spec.get("kind") == "metric"
        }
        if metric_models:
            applies = bool(metric_models & metric_source_models)
            # A detail query over a metric's model is not itself a request for
            # that metric. If SQL does express an aggregate but its source is
            # not attributable (for example COUNT(*)), conservatively retain
            # a matching rule as unverified so the query cannot bypass it.
            if not metric_source_models and has_aggregate_intent and len(query_models) == 1:
                applies = bool(metric_models & query_models_by_name)
            elif not metric_source_models and has_aggregate_intent:
                # With several tables and no attributable aggregate column,
                # keep matching metric rules unverified instead of silently
                # treating an ambiguous multi-model query as unrelated.
                applies = bool(metric_models & query_models_by_name)
        else:
            applies = any(str(spec["model"]).casefold() in query_facts for spec in specs)
            if not metric_source_models and len(query_models) > 1:
                applies = any(str(spec["model"]).casefold() in query_models_by_name for spec in specs)
        if applies:
            applicable.append((rule_id, execution_checks))

    declared_refs = {
        (
            rule_id,
            model_names.get(str(spec["model"]).casefold(), str(spec["model"])),
            column_names.get(
                (str(spec["model"]).casefold(), str(spec["column"]).casefold()),
                str(spec["column"]),
            ),
        )
        for rule_id, execution_checks in applicable
        for spec in execution_checks["checks"]
    }
    declared_refs.update((RULES_VERSION, model, column) for model, column in query_columns)
    references = [
        {"ruleId": rule_id, "model": model, "column": column}
        for rule_id, model, column in sorted(declared_refs, key=lambda item: (item[1].casefold(), item[2].casefold(), item[0]))
    ]
    referenced_models = sorted({item["model"] for item in references}, key=str.casefold)
    referenced_columns = sorted(
        ({"model": item["model"], "column": item["column"]} for item in references),
        key=lambda item: (item["model"].casefold(), item["column"].casefold()),
    )

    if not shape_ok and applicable:
        global_spec = {
            "id": "sql-shape",
            "mandatory": any(bool(spec.get("mandatory", True)) for _, group in applicable for spec in group["checks"]),
        }
        checks.append(_check(RULES_VERSION, global_spec, "unverified", shape_reason or "unsupported_sql_shape"))

    confirmed = set(confirmed_conditions)
    for rule_id, execution_checks in applicable:
        for spec in execution_checks["checks"]:
            model = str(spec["model"])
            column = str(spec["column"])
            if model.casefold() not in mdl or column.casefold() not in physical_columns.get(model.casefold(), set()):
                checks.append(_check(rule_id, spec, "stale", "reference_not_in_published_mdl"))
                continue
            if model.casefold() not in query_models_by_name:
                checks.append(_check(rule_id, spec, "unverified", "required_model_missing_from_query"))
                continue
            if not shape_ok:
                # Already reported as a global unverified shape check.
                checks.append(_check(rule_id, spec, "unverified", shape_reason or "unsupported_sql_shape"))
                continue
            kind = spec["kind"]
            ref = (str(spec["model"]), str(spec["column"]))
            if kind == "metric":
                status, reason = _metric_match(root, spec, aliases, query_models, mdl)
                metric_item: dict[str, Any] = {
                    "ruleId": rule_id,
                    "checkId": spec["id"],
                    "model": spec["model"],
                    "column": spec["column"],
                    "aggregation": spec["aggregation"],
                    "distinct": bool(spec.get("distinct", False)),
                    "status": status,
                }
                if "unit" in spec:
                    metric_item["unit"] = spec["unit"]
                    if status == "passed":
                        unit_values.add(str(spec["unit"]))
                if "grain" in spec:
                    metric_item["grain"] = spec["grain"]
                    grain_items.append({"model": spec["model"], "column": spec["column"], "value": spec["grain"], "status": "verified"})
                metric_evidence.append(metric_item)
                checks.append(_check(rule_id, spec, status, reason))
            elif kind == "predicate":
                value = spec["value"]
                matched, conflicting = _predicate_matches(root, ref, value, aliases, query_models, mdl)
                status, reason = (
                    ("conflict", "predicate_conflicts") if conflicting else
                    ("passed", "predicate_matches") if matched else
                    ("unverified", "predicate_missing")
                )
                checks.append(_check(rule_id, spec, status, reason))
            elif kind == "timeFilter":
                key = str(spec["conditionKey"])
                group_state = _group_state(root, ref, aliases, query_models, mdl)
                value = conditions.get(key, _MISSING)
                state = "missing"
                source = "unknown"
                if value is not _MISSING:
                    if not _safe_condition_value(value, str(spec["valueType"])):
                        checks.append(_check(rule_id, spec, "conflict", "time_condition_invalid"))
                        confirmations[key] = "missing"
                        time_evidence.append({"ruleId": rule_id, "checkId": spec["id"], "model": spec["model"], "column": spec["column"], "value": None, "source": "unknown", "confirmation": "missing", "groupedByOriginal": None})
                        continue
                    value = dict(value) if isinstance(value, Mapping) else value
                    if key not in confirmed:
                        state = "unconfirmed"
                    else:
                        state = "confirmed"
                        source = "confirmed"
                elif "defaultValue" in spec and spec["confirmation"] == "confirmedOrDefault":
                    value = dict(spec["defaultValue"]) if isinstance(spec["defaultValue"], Mapping) else spec["defaultValue"]
                    state = "defaulted"
                    source = "publishedDefault"
                    defaults_applied[key] = value
                if value is _MISSING:
                    sql_value = _sql_time_value(root, ref, str(spec["valueType"]), aliases, query_models, mdl)
                    if sql_value is not None:
                        status, reason = "unverified", "time_confirmation_missing"
                        state = "missing"
                        source = "sql"
                        value = sql_value
                    else:
                        status, reason = "unverified", "time_condition_missing"
                elif state == "unconfirmed":
                    status, reason = "unverified", "time_confirmation_missing"
                else:
                    matched, conflict, column_seen = _time_filter_state(
                        root, ref, str(spec["valueType"]), value, aliases, query_models, mdl
                    )
                    if matched and not conflict:
                        status, reason = "passed", "time_filter_matches"
                    elif conflict:
                        status, reason = "conflict", "time_filter_conflicts"
                    elif column_seen:
                        status, reason = "conflict", "time_filter_conflicts"
                    else:
                        status, reason = "conflict", "time_filter_missing_from_sql"
                confirmations[key] = state if state in {"confirmed", "defaulted"} else "missing"
                if value is not _MISSING:
                    if state in {"confirmed", "defaulted", "unconfirmed"}:
                        safe_conditions[key] = value
                time_evidence.append({
                    "ruleId": rule_id,
                    "checkId": spec["id"],
                    "model": spec["model"],
                    "column": spec["column"],
                    "value": None if value is _MISSING else value,
                    "conditionKey": key,
                    "source": source,
                    "confirmation": state if state in {"confirmed", "defaulted"} else "missing",
                    "groupedByOriginal": group_state == "original" if group_state != "none" else False,
                })
                checks.append(_check(rule_id, spec, status, reason))
            elif kind == "timeGrouping":
                group_state = _group_state(root, ref, aliases, query_models, mdl)
                predicate_seen = _has_time_predicate(root, ref, aliases, query_models, mdl)
                when_grouped = bool(spec.get("whenGrouped", False))
                condition_key = spec.get("conditionKey")
                trend_requested = False
                if isinstance(condition_key, str):
                    condition_value = conditions.get(condition_key, _MISSING)
                    if condition_value is not _MISSING:
                        safe_conditions[condition_key] = condition_value
                        confirmations[condition_key] = "confirmed" if condition_key in confirmed else "missing"
                        if type(condition_value) is not bool:
                            checks.append(_check(rule_id, spec, "conflict", "time_grouping_condition_invalid"))
                            grain_items.append({"model": spec["model"], "column": spec["column"], "value": "original", "status": "conflict"})
                            continue
                        trend_requested = condition_value is True
                if group_state == "original":
                    status, reason = "passed", "original_date_grouping"
                elif group_state == "transformed":
                    status, reason = "conflict", "time_grouping_transformed"
                elif when_grouped and not trend_requested:
                    # Scalar snapshots and filtered-period aggregates do not
                    # assert a trend grain. A confirmed trend condition below
                    # explicitly activates the original-date requirement.
                    status, reason = "passed", "time_grouping_not_applicable"
                elif when_grouped and condition_key in confirmed and trend_requested:
                    status, reason = "unverified", "time_grouping_missing"
                elif when_grouped and trend_requested:
                    status, reason = "unverified", "time_grouping_confirmation_missing"
                elif predicate_seen:
                    status, reason = "passed", "not_required_for_filtered_period"
                else:
                    status, reason = "unverified", "time_grouping_missing"
                grain_status = "not_applicable" if reason == "time_grouping_not_applicable" else "verified" if status == "passed" else status
                grain_items.append({"model": spec["model"], "column": spec["column"], "value": "original", "status": grain_status})
                checks.append(_check(rule_id, spec, status, reason))
            elif kind == "reference":
                present = str(spec["column"]).casefold() in physical_columns.get(str(spec["model"]).casefold(), set())
                checks.append(_check(rule_id, spec, "passed" if present else "stale", "reference_in_published_mdl" if present else "reference_not_in_published_mdl"))

    mandatory_ready = all(
        item["status"] == "passed"
        for item in checks
        if item.get("mandatory")
    )
    unit = {"status": "verified", "value": next(iter(unit_values))} if len(unit_values) == 1 else {"status": "unverified", "value": None}
    if len(unit_values) > 1:
        checks.append({"id": f"{RULES_VERSION}:unit-consistency", "ruleId": RULES_VERSION, "mandatory": True, "status": "conflict", "reasonCode": "unit_conflicts"})
        mandatory_ready = False
    return {
        "schemaVersion": 1,
        "rulesVersion": RULES_VERSION,
        "projectRevision": revision,
        "references": references,
        "models": referenced_models,
        "columns": referenced_columns,
        "metrics": metric_evidence,
        "unit": unit,
        "grain": grain_items,
        "time": _time_summary(time_evidence),
        "defaultsApplied": defaults_applied,
        "conditions": safe_conditions,
        "confirmations": confirmations,
        "checks": checks,
    }


__all__ = ["RULES_VERSION", "VerificationError", "evaluate_query"]
