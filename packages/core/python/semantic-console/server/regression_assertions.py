"""Versioned query regression assertions; observed values never leave evaluation."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from typing import Any

try:
    from .diagnostics import DiagnosticError
except ImportError:  # pragma: no cover
    from diagnostics import DiagnosticError


def _invalid(message: str = "regression case is invalid") -> None:
    raise DiagnosticError("INVALID_REGRESSION_CASE", message)


def validate_case(value: Any, *, enabled: bool) -> dict[str, Any]:
    """Validate manual case inputs without interpreting stored role/policy fields."""
    allowed = {"schemaVersion", "kind", "question", "semanticSql", "conditions",
               "confirmedConditions", "testDatasetId", "assertions"}
    if not isinstance(value, Mapping) or set(value) - allowed:
        _invalid()
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 2:
        _invalid("case schema version must be 2")
    if value.get("kind") != "deterministic_sql":
        _invalid("only deterministic SQL cases can execute")
    result = dict(value)
    for name in ("question", "semanticSql"):
        text = value.get(name)
        if not isinstance(text, str) or len(text) > 64_000 or (enabled and not text.strip()):
            _invalid(f"{name} is required before enabling")
    dataset = value.get("testDatasetId")
    if not isinstance(dataset, str) or len(dataset) > 128 or (enabled and not dataset):
        _invalid("a registered test dataset is required")
    conditions = value.get("conditions", {})
    confirmed = value.get("confirmedConditions", [])
    if not isinstance(conditions, Mapping) or not isinstance(confirmed, list):
        _invalid("clarification conditions are invalid")
    if len(conditions) > 100 or len(confirmed) > 100 or any(not isinstance(x, str) or len(x) > 128 for x in confirmed):
        _invalid("clarification conditions are invalid")
    result.update(conditions=dict(conditions), confirmedConditions=list(confirmed))
    assertions = value.get("assertions")
    if not isinstance(assertions, Mapping) or set(assertions) - {"status", "error", "rowCount", "cells"}:
        _invalid("assertions are invalid")
    if assertions.get("status") not in {"success", "failure"}:
        _invalid("an expected status is required")
    expected_error = assertions.get("error")
    if expected_error is not None:
        if assertions["status"] != "failure" or not isinstance(expected_error, Mapping) or not expected_error:
            _invalid("error assertions require failure status")
        if set(expected_error) - {"code", "reasonCode", "phase"} or any(
            not isinstance(x, str) or not x or len(x) > 128 for x in expected_error.values()
        ):
            _invalid("error assertion fields are invalid")
    cells = assertions.get("cells", [])
    if not isinstance(cells, list) or len(cells) > 100:
        _invalid("cell assertions are invalid")
    if (cells or "rowCount" in assertions) and assertions["status"] != "success":
        _invalid("result assertions require success status")
    if "rowCount" in assertions and (type(assertions["rowCount"]) is not int or not 0 <= assertions["rowCount"] <= 100_000):
        _invalid("row count assertion is invalid")
    for cell in cells:
        if not isinstance(cell, Mapping) or set(cell) - {"row", "column", "expected", "tolerance"}:
            _invalid("cell assertion is invalid")
        if type(cell.get("row")) is not int or not 0 <= cell["row"] < 100_000:
            _invalid("cell row is invalid")
        if not isinstance(cell.get("column"), str) or not cell["column"] or len(cell["column"]) > 255:
            _invalid("cell column is invalid")
        if "expected" not in cell or not _scalar(cell["expected"]):
            _invalid("expected cell value must be a finite JSON scalar")
        tolerance = cell.get("tolerance")
        if tolerance is not None and (type(tolerance) not in {int, float} or not math.isfinite(tolerance) or tolerance < 0
                                      or type(cell["expected"]) not in {int, float}):
            _invalid("numeric tolerance is invalid")
    if enabled:
        try:
            from sqlglot import exp, parse
            from sqlglot.errors import ErrorLevel
            statements = parse(value["semanticSql"], error_level=ErrorLevel.RAISE)
            if len(statements) != 1 or not isinstance(statements[0], exp.Query):
                _invalid("regression SQL must be one read-only query")
            if any(isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Into))
                   for node in statements[0].walk()):
                _invalid("regression SQL must be read-only")
            if cells and statements[0].args.get("order") is None:
                _invalid("cell assertions require a top-level ORDER BY")
        except DiagnosticError:
            raise
        except Exception as exc:
            raise DiagnosticError("INVALID_REGRESSION_CASE", "regression SQL could not be validated") from exc
    result["assertions"] = dict(assertions)
    return result


def _scalar(value: Any) -> bool:
    return value is None or type(value) in {str, bool, int} or (type(value) is float and math.isfinite(value))


def _stable_rows(sql: str, rows: list[Any], columns: list[str], stats: Mapping[str, Any]) -> bool:
    """Prove observable ordering for cell assertions, rejecting ambiguous ties."""
    if stats.get("returnedRows") != len(rows) or stats.get("truncated") is not False:
        return False
    if any(not isinstance(name, str) for name in columns) or len(columns) != len(set(columns)):
        return False
    try:
        from sqlglot import exp, parse_one
        from sqlglot.errors import SqlglotError
        query = parse_one(sql)
        # A complete preview proves completeness only after SQL row limiting.
        # Tied keys outside that window may select different rows on another
        # execution, including when a nested query/CTE applies the limit.
        # Without trusted source uniqueness evidence, do not certify cells.
        if any(isinstance(node, (exp.Limit, exp.Offset, exp.Fetch)) for node in query.walk()):
            return False
        order = query.args.get("order")
        if order is None:
            return False
        positions = []
        for item in order.expressions:
            expression = item.this
            if isinstance(expression, exp.Column):
                if expression.table or expression.db or expression.catalog:
                    # A qualified source column can have the same leaf name as
                    # a different projected column. Only an exact, unique
                    # projection proves which observable value ORDER BY used.
                    projections = query.selects
                    if len(projections) != len(columns) or any(projection.is_star for projection in projections):
                        return False
                    matches = [index for index, projection in enumerate(projections)
                               if (projection.this if isinstance(projection, exp.Alias) else projection) == expression]
                    if len(matches) != 1:
                        return False
                    positions.append(matches[0])
                else:
                    positions.append(columns.index(expression.name))
            elif isinstance(expression, exp.Literal) and expression.is_int:
                position = int(expression.this) - 1
                if not 0 <= position < len(columns):
                    return False
                positions.append(position)
            else:
                return False  # Computed ordering needs explicit projected keys.
        keys = [tuple(row[columns[index]] if isinstance(row, Mapping) else row[index] for index in positions) for row in rows]
        encoded = [json_key(key) for key in keys]
        return len(encoded) == len(set(encoded))
    except (ValueError, TypeError, KeyError, IndexError, SqlglotError):
        return False


def json_key(key: tuple[Any, ...]) -> str:
    import json
    return json.dumps(key, sort_keys=True, allow_nan=False)


def evaluate(assertions: Mapping[str, Any], response: Mapping[str, Any], *, semantic_sql: str | None = None) -> tuple[str, list[dict[str, str]]]:
    """Return pass/fail/unknown metadata only, excluding expected and actual values."""
    checks: list[dict[str, str]] = []

    def check(kind: str, passed: bool | None, reason: str) -> None:
        checks.append({"kind": kind, "status": "inconclusive" if passed is None else "passed" if passed else "failed",
                       "reasonCode": reason})

    result = response.get("result")
    result = result if isinstance(result, Mapping) else {}
    result_status = result.get("status")
    if response.get("ok") is False or result_status == "error" or result_status == "cancelled":
        actual_success: bool | None = False
    elif response.get("ok") is True and result_status == "success":
        actual_success = True
    else:
        # A positive RPC envelope without Core's explicit terminal success
        # status does not prove that query execution completed successfully.
        actual_success = None
    expected_success = assertions["status"] == "success"
    if actual_success is None:
        check("status", None, "RESULT_INCOMPLETE")
    else:
        check("status", actual_success == expected_success, "STATUS_MATCH" if actual_success == expected_success else "STATUS_MISMATCH")
    error = response.get("error") if isinstance(response.get("error"), Mapping) else result.get("error", {})
    error = error if isinstance(error, Mapping) else {}
    for name, expected in assertions.get("error", {}).items():
        check("error", error.get(name) == expected, "ERROR_MATCH" if error.get(name) == expected else "ERROR_MISMATCH")
    stats = result.get("stats") if isinstance(result.get("stats"), Mapping) else {}
    rows = result.get("previewRows", result.get("rows"))
    columns = result.get("columns")
    truncated = stats.get("truncated") is not False
    if "rowCount" in assertions:
        count = stats.get("returnedRows")
        if actual_success is None:
            check("rowCount", None, "RESULT_INCOMPLETE")
        elif not actual_success:
            check("rowCount", False, "QUERY_FAILED")
        elif truncated or type(count) is not int or not isinstance(rows, list):
            check("rowCount", None, "RESULT_INCOMPLETE")
        else:
            check("rowCount", count == assertions["rowCount"], "ROW_COUNT_MATCH" if count == assertions["rowCount"] else "ROW_COUNT_MISMATCH")
    names = [x.get("name") if isinstance(x, Mapping) else x for x in columns] if isinstance(columns, list) else []
    stable_order = semantic_sql is None or isinstance(rows, list) and _stable_rows(semantic_sql, rows, names, stats)
    for cell in assertions.get("cells", []):
        if actual_success is None:
            check("cell", None, "RESULT_INCOMPLETE")
            continue
        if not actual_success:
            check("cell", False, "QUERY_FAILED")
            continue
        if not stable_order:
            check("cell", None, "ORDERING_UNPROVEN")
            continue
        if truncated or not isinstance(rows, list) or len(names) != len(set(str(x) for x in names)):
            check("cell", None, "RESULT_INCOMPLETE")
            continue
        if cell["column"] not in names:
            check("cell", False, "COLUMN_MISSING")
            continue
        if cell["row"] >= len(rows):
            complete = result.get("delivery") in {None, "inline"} and stats.get("returnedRows") == len(rows)
            check("cell", False if complete else None, "ROW_MISSING" if complete else "PREVIEW_INCOMPLETE")
            continue
        row = rows[cell["row"]]
        try:
            actual = row[cell["column"]] if isinstance(row, Mapping) else row[names.index(cell["column"])]
        except (KeyError, IndexError, TypeError):
            check("cell", None, "RESULT_INVALID")
            continue
        if not _scalar(actual):
            check("cell", None, "RESULT_INVALID")
            continue
        expected = cell["expected"]
        column = columns[names.index(cell["column"])]
        numeric_type = str(column.get("type", "")).upper() if isinstance(column, Mapping) else ""
        numeric_string = isinstance(actual, str) and any(x in numeric_type for x in ("INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL"))
        numeric_actual = None
        if type(expected) in {int, float} and (numeric_string or type(actual) in {int, float}):
            try:
                candidate = Decimal(str(actual))
                numeric_actual = candidate if candidate.is_finite() else None
            except InvalidOperation:
                pass
        if "tolerance" in cell:
            matched = numeric_actual is not None and abs(numeric_actual - Decimal(str(expected))) <= Decimal(str(cell["tolerance"]))
        elif numeric_actual is not None:
            matched = numeric_actual == Decimal(str(expected))
        else:
            matched = actual == expected and (type(actual) is type(expected) or type(actual) in {int, float} and type(expected) in {int, float})
        check("cell", matched, "CELL_MATCH" if matched else "CELL_MISMATCH")
    state = "failed" if any(x["status"] == "failed" for x in checks) else "inconclusive" if any(x["status"] == "inconclusive" for x in checks) else "passed"
    return state, checks
