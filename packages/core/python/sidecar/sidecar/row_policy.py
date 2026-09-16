"""Apply resolved SemaRail table, column, and row policy to native SQL."""

from __future__ import annotations

import re
import math
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Mapping

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from .sql_policy import PhysicalTable


class RowPolicyError(ValueError):
    """The query or resolved policy cannot be enforced safely."""

    def __init__(self, message: str, *, reason_code: str | None = None, resource_kind: str | None = None, resource_name: str | None = None) -> None:
        self.reason_code = reason_code
        self.resource_kind = resource_kind
        self.resource_name = resource_name
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AuthorizedQuery:
    sql: str
    parameters: Mapping[str, Any]
    applied_tables: tuple[str, ...]
    # Physical tables referenced only by policy-generated predicates.  The
    # query service may add these to the *rewritten* SQL allowlist after the
    # original native SQL has already been checked against MDL.  Keeping this
    # separate from ``applied_tables`` prevents a generated lookup relation
    # from becoming a user-visible authorization grant.
    lookup_tables: tuple[PhysicalTable, ...] = ()


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_MAX_POLICY_TABLES = 256
_MAX_CONDITIONS = 64
_MAX_VALUES = 1_000
_MAX_LOOKUP_IDENTIFIER = 512
_PERMISSION_LOOKUP_OPERATOR = "permissionLookup"
_PERMISSION_LOOKUP_FIELDS = {
    "field",
    "operator",
    "values",
    "lookup",
    "organizationValue",
    "includeSelf",
}
_PERMISSION_LOOKUP_REQUIRED_FIELDS = {
    "field",
    "operator",
    "values",
    "lookup",
    "organizationValue",
}
_PERMISSION_LOOKUP_CONFIG_FIELDS = {
    "table",
    "principalField",
    "targetField",
    "organizationField",
}


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


def _is_scalar(value: Any) -> bool:
    """Whether a resolved policy value is safe to bind as one SQL scalar."""

    return (
        isinstance(value, (str, int, float, bool))
        and not (isinstance(value, float) and not math.isfinite(value))
        and not (isinstance(value, str) and (not value or len(value) > 1_024))
    )


def _permission_lookup_table(
    value: Any,
) -> tuple[str, PhysicalTable]:
    """Parse a schema-qualified lookup relation without interpolating SQL."""

    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_LOOKUP_IDENTIFIER:
        raise RowPolicyError("permission lookup table is invalid")
    parts = value.split(".")
    # PhysicalAllowlist supports catalog.schema.table as well as schema.table.
    # A relation with no schema is deliberately rejected: lookup expansion
    # must not depend on the database search_path.
    if len(parts) == 2:
        catalog = None
        schema, table = parts
    elif len(parts) == 3:
        catalog, schema, table = parts
    else:
        raise RowPolicyError("permission lookup table must be schema-qualified")
    if any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise RowPolicyError("permission lookup table is invalid")
    return value, PhysicalTable(catalog, schema, table)


def _permission_lookup_field(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise RowPolicyError("permission lookup field is invalid")
    return value


def _contains_permission_lookup(value: Any) -> bool:
    """Return whether a row-filter tree contains a lookup leaf."""

    if not isinstance(value, Mapping):
        return False
    if value.get("operator") == _PERMISSION_LOOKUP_OPERATOR:
        return True
    conditions = value.get("conditions")
    return isinstance(conditions, list) and any(
        _contains_permission_lookup(item) for item in conditions
    )


def _new_internal_alias(used: set[str], prefix: str) -> str:
    """Allocate a deterministic alias that cannot shadow user identifiers."""

    index = 0
    while True:
        candidate = f"__srp_{prefix}_{index}"
        index += 1
        normalized = candidate.lower()
        if normalized not in used:
            used.add(normalized)
            return candidate


def _collect_identifiers(statement: exp.Expression) -> set[str]:
    """Collect all user identifiers before policy-generated nodes are added."""

    return {
        identifier.name.lower()
        for identifier in statement.find_all(exp.Identifier)
        if isinstance(identifier.name, str)
    }


def _condition(
    value: Any,
    parameters: dict[str, Any],
    *,
    counter: list[int],
    source_alias: str | None = None,
    next_alias: Callable[[str], str] | None = None,
    lookup_tables: list[PhysicalTable] | None = None,
) -> exp.Expression:
    if not isinstance(value, Mapping):
        raise RowPolicyError("row filter is invalid")
    op = value.get("op")
    if op in {"and", "or"}:
        conditions = value.get("conditions")
        if not isinstance(conditions, list) or not conditions or len(conditions) > _MAX_CONDITIONS:
            raise RowPolicyError("row filter group is invalid")
        expressions = [
            _condition(
                item,
                parameters,
                counter=counter,
                source_alias=source_alias,
                next_alias=next_alias,
                lookup_tables=lookup_tables,
            )
            for item in conditions
        ]
        combined = expressions[0]
        for item in expressions[1:]:
            combined = exp.and_(combined, item) if op == "and" else exp.or_(combined, item)
        return combined
    operator = value.get("operator")
    if operator == _PERMISSION_LOOKUP_OPERATOR:
        if (
            set(value) - _PERMISSION_LOOKUP_FIELDS
            or not _PERMISSION_LOOKUP_REQUIRED_FIELDS.issubset(value)
            or ("includeSelf" in value and type(value.get("includeSelf")) is not bool)
        ):
            raise RowPolicyError("permission lookup condition is invalid")
        if source_alias is None or next_alias is None or lookup_tables is None:
            # A lookup predicate must be correlated to the protected source.
            # This is an internal invariant, but fail closed if a future call
            # site forgets to supply the lexical source context.
            raise RowPolicyError("permission lookup source is invalid")
        field = value.get("field")
        if not isinstance(field, str) or not _IDENTIFIER.fullmatch(field):
            raise RowPolicyError("row filter field is invalid")
        values = value.get("values")
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not _is_scalar(values[0])
            or isinstance(values[0], bool)
        ):
            raise RowPolicyError("permission lookup principal is invalid")
        organization_value = value.get("organizationValue")
        if not _is_scalar(organization_value):
            raise RowPolicyError("permission lookup organization is invalid")
        lookup = value.get("lookup")
        if not isinstance(lookup, Mapping) or set(lookup) != _PERMISSION_LOOKUP_CONFIG_FIELDS:
            raise RowPolicyError("permission lookup configuration is invalid")
        lookup_name, lookup_table = _permission_lookup_table(lookup.get("table"))
        principal_field = _permission_lookup_field(lookup.get("principalField"))
        target_field = _permission_lookup_field(lookup.get("targetField"))
        organization_field = _permission_lookup_field(lookup.get("organizationField"))

        principal_name = f"srp_{counter[0]}"
        counter[0] += 1
        parameters[principal_name] = values[0]
        organization_name = f"srp_{counter[0]}"
        counter[0] += 1
        parameters[organization_name] = organization_value
        lookup_alias = next_alias("lookup")
        lookup_source = exp.to_table(lookup_name)
        lookup_source.set("alias", exp.TableAlias(this=exp.to_identifier(lookup_alias)))
        principal_match = exp.column(
            principal_field,
            table=lookup_alias,
        ).eq(exp.Placeholder(this=principal_name))
        organization_match = exp.column(
            organization_field,
            table=lookup_alias,
        ).eq(exp.Placeholder(this=organization_name))
        target_match = exp.column(
            target_field,
            table=lookup_alias,
        ).eq(exp.column(field, table=source_alias))
        lookup_query = exp.select(exp.Literal.number(1)).from_(lookup_source).where(
            exp.and_(principal_match, organization_match, target_match)
        )
        predicate: exp.Expression = exp.Exists(this=lookup_query)
        if bool(value.get("includeSelf", False)):
            predicate = exp.or_(
                predicate,
                exp.column(field, table=source_alias).eq(exp.Placeholder(this=principal_name)),
            )
        if lookup_table not in lookup_tables:
            lookup_tables.append(lookup_table)
        return predicate
    if set(value) != {"field", "operator", "values"}:
        raise RowPolicyError("row filter condition is invalid")
    field = value.get("field")
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
    column = exp.column(field, table=source_alias) if source_alias else exp.column(field)
    if operator == "eq":
        return column.eq(placeholders[0])
    return column.isin(*placeholders)


def _is_restricted(rule: Mapping[str, Any]) -> bool:
    return rule.get("allowedColumns") is not None or bool(rule.get("deniedColumns"))


def _validate_column(name: str, rules: list[tuple[str, Mapping[str, Any]]]) -> None:
    normalized_name = name.lower()
    for table_name, rule in rules:
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
            raise RowPolicyError("column is not allowed", reason_code="COLUMN_PERMISSION_REQUIRED", resource_kind="column", resource_name=f"{table_name}.{name}")


def _validate_columns(statement: exp.Expression, table_rules: Mapping[int, tuple[str, Mapping[str, Any]]]) -> None:
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
        restricted = {alias: value for alias, value in local_rules.items() if _is_restricted(value[1])}
        if not restricted:
            continue
        if any(isinstance(selection, exp.Star) for selection in getattr(scope.expression, "selects", ())):
            table_name = next(iter(restricted.values()))[0]
            raise RowPolicyError("wildcard columns are not allowed by column policy", reason_code="COLUMN_PERMISSION_REQUIRED", resource_kind="column", resource_name=f"{table_name}.*")
        for star in scope.stars:
            alias = star.table.lower() if star.table else ""
            if (alias and alias in restricted) or (not alias and restricted):
                table_name = restricted[alias][0] if alias in restricted else next(iter(restricted.values()))[0]
                raise RowPolicyError("wildcard columns are not allowed by column policy", reason_code="COLUMN_PERMISSION_REQUIRED", resource_kind="column", resource_name=f"{table_name}.*")
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

    if (
        not isinstance(policy, Mapping)
        or type(policy.get("schemaVersion")) is not int
        or policy.get("schemaVersion") not in {1, 2}
    ):
        raise RowPolicyError("authorization policy version is unsupported")
    schema_version = int(policy["schemaVersion"])
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
    table_rules: dict[int, tuple[str, Mapping[str, Any]]] = {}
    resolved: list[tuple[exp.Table, str, Mapping[str, Any]]] = []
    for table in physical_tables:
        matched = _rule_for(table, rules)
        if matched is None:
            if policy.get("defaultEffect") == "deny":
                raise RowPolicyError("table is not allowed", reason_code="TABLE_PERMISSION_REQUIRED", resource_kind="table", resource_name=_table_candidates(table)[0])
            continue
        key, rule = matched
        table_rules[id(table)] = (key, rule)
        resolved.append((table, key, rule))
    _validate_columns(statement, table_rules)

    parameters: dict[str, Any] = {}
    applied: list[str] = []
    lookup_tables: list[PhysicalTable] = []
    counter = [0]
    used_identifiers = _collect_identifiers(statement)

    def next_alias(prefix: str) -> str:
        return _new_internal_alias(used_identifiers, prefix)

    for table, key, rule in resolved:
        row_filter = rule.get("rowFilter")
        if row_filter is None:
            continue
        has_lookup = _contains_permission_lookup(row_filter)
        if has_lookup and schema_version != 2:
            raise RowPolicyError("permission lookup requires authorization policy version 2")
        source_alias = next_alias("source") if has_lookup else None
        predicate = _condition(
            row_filter,
            parameters,
            counter=counter,
            source_alias=source_alias,
            next_alias=next_alias,
            lookup_tables=lookup_tables,
        )
        alias = table.alias_or_name or table.name
        source = table.copy()
        source.set("alias", None)
        if source_alias is not None:
            source.set("alias", exp.TableAlias(this=exp.to_identifier(source_alias)))
        replacement = exp.select("*").from_(source).where(predicate).subquery(alias=alias)
        table.replace(replacement)
        applied.append(key)
    return AuthorizedQuery(
        statement.sql(dialect="postgres"),
        parameters,
        tuple(dict.fromkeys(applied)),
        tuple(lookup_tables),
    )


__all__ = ["AuthorizedQuery", "RowPolicyError", "apply_row_policy"]
