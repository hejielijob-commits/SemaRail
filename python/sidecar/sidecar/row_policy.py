"""Apply resolved SemaRail table, column, and row policy to native SQL."""

from __future__ import annotations

import re
import math
from dataclasses import dataclass
from typing import Any, Mapping

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.optimizer.scope import traverse_scope


class RowPolicyError(ValueError):
    """The query or resolved policy cannot be enforced safely."""


@dataclass(frozen=True, slots=True)
class AuthorizedQuery:
    sql: str
    parameters: Mapping[str, Any]
    applied_tables: tuple[str, ...]


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_MAX_POLICY_TABLES = 256
_MAX_CONDITIONS = 64
_MAX_VALUES = 1_000


def _table_candidates(table: exp.Table) -> tuple[str, ...]:
    catalog = table.catalog
    schema = table.db
    name = table.name
    candidates = [".".join(part for part in (catalog, schema, name) if part)]
    if schema:
        candidates.append(f"{schema}.{name}")
    candidates.append(name)
    return tuple(dict.fromkeys(item.lower() for item in candidates if item))


def _rule_for(table: exp.Table, rules: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    normalized = {str(key).lower(): value for key, value in rules.items()}
    for candidate in _table_candidates(table):
        value = normalized.get(candidate)
        if value is not None:
            if not isinstance(value, Mapping):
                raise RowPolicyError("table policy is invalid")
            return candidate, value
    return None


def _is_cte_reference(table: exp.Table, cte_names: set[str]) -> bool:
    """Distinguish a CTE reference from a same-named physical table.

    A schema-qualified table is always physical. An unqualified table inside
    the body of its own same-named CTE is also treated as physical (or denied),
    preventing ``WITH sales AS (SELECT ... FROM sales)`` from bypassing policy.
    """

    if table.catalog or table.db or table.name.lower() not in cte_names:
        return False
    owner = table.find_ancestor(exp.CTE)
    if owner is not None and owner.alias_or_name.lower() == table.name.lower():
        return False
    return True


def _condition(
    value: Any,
    parameters: dict[str, Any],
    *,
    counter: list[int],
) -> exp.Expression:
    if not isinstance(value, Mapping):
        raise RowPolicyError("row filter is invalid")
    op = value.get("op")
    if op in {"and", "or"}:
        conditions = value.get("conditions")
        if not isinstance(conditions, list) or not conditions or len(conditions) > _MAX_CONDITIONS:
            raise RowPolicyError("row filter group is invalid")
        expressions = [_condition(item, parameters, counter=counter) for item in conditions]
        combined = expressions[0]
        for item in expressions[1:]:
            combined = exp.and_(combined, item) if op == "and" else exp.or_(combined, item)
        return combined
    if set(value) != {"field", "operator", "values"}:
        raise RowPolicyError("row filter condition is invalid")
    field = value.get("field")
    operator = value.get("operator")
    values = value.get("values")
    if not isinstance(field, str) or not _IDENTIFIER.fullmatch(field):
        raise RowPolicyError("row filter field is invalid")
    if operator not in {"eq", "in"} or not isinstance(values, list) or not values or len(values) > _MAX_VALUES:
        raise RowPolicyError("row filter operator or values are invalid")
    if any(
        not isinstance(item, (str, int, float, bool))
        or item is None
        or (isinstance(item, float) and not math.isfinite(item))
        for item in values
    ):
        raise RowPolicyError("row filter value is invalid")
    if operator == "eq" and len(values) != 1:
        raise RowPolicyError("eq row filter requires one value")
    placeholders: list[exp.Placeholder] = []
    for item in values:
        name = f"srp_{counter[0]}"
        counter[0] += 1
        parameters[name] = item
        placeholders.append(exp.Placeholder(this=name))
    column = exp.column(field)
    if operator == "eq":
        return column.eq(placeholders[0])
    return column.isin(*placeholders)


def _is_restricted(rule: Mapping[str, Any]) -> bool:
    return rule.get("allowedColumns") is not None or bool(rule.get("deniedColumns"))


def _validate_column(name: str, rules: list[Mapping[str, Any]]) -> None:
    normalized_name = name.lower()
    for rule in rules:
        denied = rule.get("deniedColumns", [])
        allowed = rule.get("allowedColumns")
        if not isinstance(denied, list) or any(not isinstance(item, str) for item in denied):
            raise RowPolicyError("denied column policy is invalid")
        if allowed is not None and (not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed)):
            raise RowPolicyError("allowed column policy is invalid")
        normalized_denied = {item.lower() for item in denied}
        normalized_allowed = {item.lower() for item in allowed} if allowed is not None else None
        if normalized_name in normalized_denied or (
            normalized_allowed is not None and normalized_name not in normalized_allowed
        ):
            raise RowPolicyError("column is not allowed")


def _validate_columns(statement: exp.Expression, table_rules: Mapping[int, Mapping[str, Any]]) -> None:
    """Validate columns against sources in each SELECT's lexical scope.

    A global alias map is unsafe because a nested query may shadow an outer
    alias. ``traverse_scope`` resolves each local source independently; an
    external correlated column is left for its owning outer scope.
    """

    for scope in traverse_scope(statement):
        local_rules = {
            alias.lower(): table_rules[id(source)]
            for alias, source in scope.sources.items()
            if isinstance(source, exp.Table) and id(source) in table_rules
        }
        restricted = {alias: rule for alias, rule in local_rules.items() if _is_restricted(rule)}
        if not restricted:
            continue
        if any(isinstance(selection, exp.Star) for selection in getattr(scope.expression, "selects", ())):
            raise RowPolicyError("wildcard columns are not allowed by column policy")
        for star in scope.stars:
            alias = star.table.lower() if star.table else ""
            if (alias and alias in restricted) or (not alias and restricted):
                raise RowPolicyError("wildcard columns are not allowed by column policy")
        for column in scope.columns:
            name = column.name
            if not name or name == "*":
                continue
            if column.table:
                rule = restricted.get(column.table.lower())
                candidates = [rule] if rule is not None else []
            else:
                # Without catalog metadata an unqualified reference in a
                # multi-source SELECT could resolve to any protected source.
                # Requiring every local protected source to allow it is the
                # only fail-closed choice.
                candidates = list(restricted.values())
            _validate_column(name, candidates)


def apply_row_policy(sql: str, policy: Mapping[str, Any]) -> AuthorizedQuery:
    """Wrap every physical table with its resolved, parameterized row filter."""

    if not isinstance(policy, Mapping) or policy.get("schemaVersion") != 1:
        raise RowPolicyError("authorization policy version is unsupported")
    if policy.get("defaultEffect") not in {"allow", "deny"}:
        raise RowPolicyError("authorization default effect is invalid")
    rules = policy.get("tables")
    if not isinstance(rules, Mapping) or len(rules) > _MAX_POLICY_TABLES:
        raise RowPolicyError("authorization table policy is invalid")
    try:
        statement = parse_one(sql, read="postgres")
    except (ParseError, SqlglotError, TypeError, ValueError) as exc:
        raise RowPolicyError("native SQL could not be parsed") from exc
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE) if cte.alias_or_name}
    physical_tables = [table for table in statement.find_all(exp.Table) if not _is_cte_reference(table, cte_names)]
    table_rules: dict[int, Mapping[str, Any]] = {}
    resolved: list[tuple[exp.Table, str, Mapping[str, Any]]] = []
    for table in physical_tables:
        matched = _rule_for(table, rules)
        if matched is None:
            if policy.get("defaultEffect") == "deny":
                raise RowPolicyError("table is not allowed")
            continue
        key, rule = matched
        table_rules[id(table)] = rule
        resolved.append((table, key, rule))
    _validate_columns(statement, table_rules)

    parameters: dict[str, Any] = {}
    applied: list[str] = []
    counter = [0]
    for table, key, rule in resolved:
        row_filter = rule.get("rowFilter")
        if row_filter is None:
            continue
        predicate = _condition(row_filter, parameters, counter=counter)
        alias = table.alias_or_name or table.name
        source = table.copy()
        source.set("alias", None)
        replacement = exp.select("*").from_(source).where(predicate).subquery(alias=alias)
        table.replace(replacement)
        applied.append(key)
    return AuthorizedQuery(
        statement.sql(dialect="postgres"),
        parameters,
        tuple(dict.fromkeys(applied)),
    )


__all__ = ["AuthorizedQuery", "RowPolicyError", "apply_row_policy"]
