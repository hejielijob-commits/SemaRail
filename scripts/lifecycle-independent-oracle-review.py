#!/usr/bin/env python3
"""Independently compare one real lifecycle worker query with PostgreSQL.

The protected broker transcript is the source of the requested semantic SQL,
the executed native SQL, and the observed result. This reviewer binds a single
Codex worker UUID, checks the query shape, reads the current physical snapshot
through the private PostgreSQL reader, and writes a protected report. It does
not approve an Agent evaluation; final lifecycle review remains an explicit
root/admin action.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA = ROOT / ".benchmark-data" / "business-trust-v1-postgres"
PASSWORD_PATH = PRIVATE_DATA / ".reader-password"
MANIFEST_PATH = PRIVATE_DATA / "load-report.json"
MAX_TRANSCRIPT_BYTES = 25 * 1024 * 1024
MAX_REPORT_BYTES = 10 * 1024 * 1024
_PUBLIC_POLICY_VERSION = re.compile(r"pol_[0-9a-f]{32}:[1-9][0-9]{0,9}\Z")
_SEMARAIL_SECRET_TEXT = re.compile(r"\bsr_(?:live|session|device|state)_[A-Za-z0-9_-]{20,}", re.IGNORECASE)

SCENARIOS: dict[str, dict[str, str]] = {
    "salarySnapshot": {
        "question": "What is the average monthly salary in the current salary snapshot?",
        "model": "compensation_history",
        "table": "compensation_history",
        "column": "monthly_salary",
        "date_column": "effective_date",
    },
    "performanceSnapshot": {
        "question": "What is the average performance score in the current review snapshot?",
        "model": "performance_reviews",
        "table": "performance_reviews",
        "column": "performance_score",
        "date_column": "review_date",
    },
}


class ReviewFailure(RuntimeError):
    """A sanitized reviewer failure; never contains SQL, values, or secrets."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _read_json(path: Path, code: str, *, maximum: int = MAX_REPORT_BYTES) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if not 0 < len(raw) <= maximum:
            raise ValueError("invalid JSON size")
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewFailure(code) from exc
    if not isinstance(parsed, dict):
        raise ReviewFailure(code)
    return parsed


def _strict_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReviewFailure("WORKER_THREAD_ID_INVALID") from exc
    if str(parsed) != value:
        raise ReviewFailure("WORKER_THREAD_ID_INVALID")
    return value


def _thread_id_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return _strict_uuid(value)
    except ReviewFailure:
        return None


def _private_current_user_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
        if os.name == "nt":
            identity = subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"],
                check=True,
                capture_output=True,
                text=True,
            )
            rows = list(csv.reader(identity.stdout.splitlines()))
            sid = next(
                (
                    row[-1].strip()
                    for row in rows
                    if row and re.fullmatch(r"S-1-(?:[0-9]+-)+[0-9]+", row[-1].strip())
                ),
                None,
            )
            if sid is None:
                raise OSError("current user SID unavailable")
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(M)"],
                check=True,
                capture_output=True,
                text=True,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ReviewFailure("PRIVATE_ORACLE_REPORT_PROTECTION_FAILED") from exc


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_REPORT_BYTES:
        raise ReviewFailure("ORACLE_REPORT_TOO_LARGE")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        _private_current_user_file(temporary)
        os.replace(temporary, path)
        _private_current_user_file(path)
    except FileExistsError as exc:
        raise ReviewFailure("ORACLE_REPORT_ALREADY_EXISTS") from exc
    except ReviewFailure:
        raise
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ReviewFailure("ORACLE_REPORT_WRITE_FAILED") from exc


def _read_transcript(path: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReviewFailure("WORKER_TRANSCRIPT_UNAVAILABLE") from exc
    if not 0 < len(raw) <= MAX_TRANSCRIPT_BYTES:
        raise ReviewFailure("WORKER_TRANSCRIPT_SIZE_INVALID")
    records: list[dict[str, Any]] = []
    try:
        lines = raw.decode("utf-8").splitlines()
        for line in lines:
            if not line:
                continue
            value = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_float=Decimal,
                parse_constant=_reject_constant,
            )
            if not isinstance(value, dict):
                raise ValueError("record must be an object")
            records.append(value)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewFailure("WORKER_TRANSCRIPT_INVALID") from exc
    if not records:
        raise ReviewFailure("WORKER_TRANSCRIPT_EMPTY")
    return records, hashlib.sha256(raw).hexdigest()


def _safe_public_query_authorization_metadata(record: dict[str, Any], value: Any) -> bool:
    """Validate the one credential-free authorization object Core returns for a governed query."""

    request = record.get("request")
    params = request.get("params") if isinstance(request, dict) else None
    response = record.get("response")
    result = response.get("result") if isinstance(response, dict) else None
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if (
        not isinstance(request, dict)
        or request.get("method") != "tools/call"
        or not isinstance(params, dict)
        or params.get("name") != "semarail_governed_query"
        or not isinstance(result, dict)
        or result.get("isError") is True
        or not isinstance(structured, dict)
        or structured.get("authorization") is not value
        or not isinstance(value, dict)
        or set(value) != {"policyVersions", "rowPolicyApplied", "tableCount"}
    ):
        return False
    versions = value.get("policyVersions")
    return (
        type(value.get("rowPolicyApplied")) is bool
        and type(value.get("tableCount")) is int
        and 0 <= value["tableCount"] <= 64
        and isinstance(versions, list)
        and 1 <= len(versions) <= 64
        and all(type(item) is str and _PUBLIC_POLICY_VERSION.fullmatch(item) for item in versions)
        and len(set(versions)) == len(versions)
    )


def _contains_sensitive_header_key(value: Any, *, _record: dict[str, Any] | None = None, _path: tuple[str, ...] = ()) -> bool:
    if isinstance(value, dict):
        record = value if _record is None else _record
        for key, item in value.items():
            normalized = re.sub(r"[^a-z]", "", key.casefold()) if isinstance(key, str) else ""
            path = _path + ((key,) if isinstance(key, str) else ("",))
            sensitive = (
                "authorization" in normalized
                or normalized in {"authheader", "authheaders"}
                or normalized.endswith("authheader")
                or normalized.endswith("authheaders")
                or normalized == "nonce"
                or normalized.endswith("nonce")
            )
            if sensitive:
                if (
                    key == "authorization"
                    and path == ("response", "result", "structuredContent", "authorization")
                    and _safe_public_query_authorization_metadata(record, item)
                ):
                    continue
                return True
            if _contains_sensitive_header_key(item, _record=record, _path=path):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_header_key(item, _record=_record, _path=_path) for item in value)
    return False


def _contains_sensitive_secret_text(value: Any) -> bool:
    """Reject recognizable SemaRail credential text even under an innocuous key."""

    if isinstance(value, str):
        return _SEMARAIL_SECRET_TEXT.search(value) is not None
    if isinstance(value, dict):
        return any(
            _contains_sensitive_secret_text(key) or _contains_sensitive_secret_text(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_secret_text(item) for item in value)
    return False


def _decimal(value: Any, code: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise ReviewFailure(code)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ReviewFailure(code) from exc
    if not parsed.is_finite():
        raise ReviewFailure(code)
    return parsed


def _sql_ast(sql: Any, code: str) -> Any:
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 64_000:
        raise ReviewFailure(code)
    try:
        from sqlglot import parse
        from sqlglot.errors import ErrorLevel

        expressions = [item for item in parse(sql, read="postgres", error_level=ErrorLevel.RAISE) if item is not None]
    except Exception as exc:
        raise ReviewFailure(code) from exc
    if len(expressions) != 1:
        raise ReviewFailure(code)
    return expressions[0]


def _column_name(expression: Any) -> str | None:
    from sqlglot import exp

    current = expression
    while isinstance(current, exp.Paren):
        current = current.this
    return current.name.casefold() if isinstance(current, exp.Column) else None


def _literal_date(expression: Any) -> str | None:
    from sqlglot import exp

    current = expression
    while isinstance(current, exp.Paren):
        current = current.this
    if isinstance(current, exp.Cast):
        current = current.this
    if isinstance(current, exp.Literal) and current.is_string:
        value = current.this
        return value if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) else None
    if isinstance(current, exp.Date) and isinstance(current.this, exp.Literal) and current.this.is_string:
        value = current.this.this
        return value if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) else None
    return None


def _table_matches(table: Any, table_name: str, *, require_hr_schema: bool) -> bool:
    from sqlglot import exp

    if (
        not isinstance(table, exp.Table)
        or table.name.casefold() != table_name.casefold()
        or table.catalog
    ):
        return False
    schema = table.db.casefold()
    if require_hr_schema:
        return schema == "hr"
    # Semantic SQL must address the published model name, not bypass the
    # semantic layer through a physical-schema-qualified table reference.
    return schema == ""


def _is_max_date_subquery(expression: Any, scenario: dict[str, str], *, require_hr_schema: bool) -> bool:
    from sqlglot import exp

    current = expression
    while isinstance(current, exp.Paren):
        current = current.this
    if isinstance(current, exp.Subquery):
        current = current.this
    if not isinstance(current, exp.Select) or current.args.get("with_") is not None:
        return False
    if (
        len(current.expressions) != 1
        or current.args.get("where") is not None
        or current.args.get("joins")
        or current.args.get("group") is not None
        or current.args.get("having") is not None
        or current.args.get("order") is not None
        or current.args.get("limit") is not None
        or current.args.get("offset") is not None
        or current.args.get("distinct") is not None
    ):
        return False
    projection = current.expressions[0]
    if isinstance(projection, exp.Alias):
        projection = projection.this
    if not isinstance(projection, exp.Max) or _column_name(projection.this) != scenario["date_column"]:
        return False
    from_clause = current.args.get("from_")
    return (
        isinstance(from_clause, exp.From)
        and _table_matches(from_clause.this, scenario["table"], require_hr_schema=require_hr_schema)
    )


def _has_current_date_predicate(
    select: Any,
    scenario: dict[str, str],
    snapshot_date: str,
    *,
    require_hr_schema: bool,
) -> bool:
    from sqlglot import exp

    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return False
    predicate = where.this
    while isinstance(predicate, exp.Paren):
        predicate = predicate.this
    if not isinstance(predicate, exp.EQ):
        return False
    left, right = predicate.this, predicate.expression
    if _column_name(left) == scenario["date_column"]:
        date_expression = right
    elif _column_name(right) == scenario["date_column"]:
        date_expression = left
    else:
        return False
    literal = _literal_date(date_expression)
    if literal is not None:
        return literal == snapshot_date
    return _is_max_date_subquery(
        date_expression,
        scenario,
        require_hr_schema=require_hr_schema,
    )


def _validate_query_sql(
    sql: Any,
    scenario: dict[str, str],
    snapshot_date: str,
    *,
    require_hr_schema: bool,
    code: str,
) -> str:
    from sqlglot import exp

    root = _sql_ast(sql, code)
    if not isinstance(root, exp.Select) or root.args.get("with_") is not None:
        raise ReviewFailure(code)
    if (
        len(root.expressions) != 1
        or root.args.get("joins")
        or root.args.get("group") is not None
        or root.args.get("having") is not None
        or root.args.get("order") is not None
        or root.args.get("limit") is not None
        or root.args.get("offset") is not None
        or root.args.get("distinct") is not None
    ):
        raise ReviewFailure(code)
    from_clause = root.args.get("from_")
    if (
        not isinstance(from_clause, exp.From)
        or not _table_matches(from_clause.this, scenario["table"], require_hr_schema=require_hr_schema)
    ):
        raise ReviewFailure(code)
    projection = root.expressions[0]
    if isinstance(projection, exp.Alias):
        projection = projection.this
    if (
        not isinstance(projection, exp.Avg)
        or projection.args.get("distinct") is not None
        or projection.args.get("filter") is not None
        or projection.args.get("over") is not None
        or _column_name(projection.this) != scenario["column"]
    ):
        raise ReviewFailure(code)
    # Reject extra tables and columns that could silently change the metric.
    tables = list(root.find_all(exp.Table))
    if not tables or any(
        not _table_matches(table, scenario["table"], require_hr_schema=require_hr_schema)
        for table in tables
    ):
        raise ReviewFailure(code)
    columns = list(root.find_all(exp.Column))
    permitted = {scenario["column"], scenario["date_column"]}
    if not columns or any(column.name.casefold() not in permitted for column in columns):
        raise ReviewFailure(code)
    if not _has_current_date_predicate(
        root,
        scenario,
        snapshot_date,
        require_hr_schema=require_hr_schema,
    ):
        raise ReviewFailure(code)
    return sql


def _plain_projection_select(
    select: Any,
    code: str,
    *,
    allow_with: bool = False,
    allow_where: bool = False,
) -> bool:
    """Whether one Wren source wrapper is projection-only and row preserving."""

    from sqlglot import exp

    if not isinstance(select, exp.Select):
        return False
    forbidden = (
        "joins", "group", "having", "qualify", "order", "limit",
        "offset", "distinct", "windows", "sample", "cluster", "distribute", "sort",
        "connect", "match", "pivots",
    )
    if not allow_with:
        forbidden += ("with_",)
    if not allow_where:
        forbidden += ("where",)
    for field in forbidden:
        value = select.args.get(field)
        if value is not None and not (field == "joins" and value == []):
            return False
    return True


def _projection_fields(
    select: Any,
    expected_fields: set[str],
    *,
    source_alias: str,
    code: str,
) -> bool:
    """Require a one-to-one projection of named source columns."""

    from sqlglot import exp

    if not _plain_projection_select(select, code) or len(select.expressions) != len(expected_fields):
        return False
    found: set[str] = set()
    for item in select.expressions:
        if isinstance(item, exp.Alias):
            alias = item.alias.casefold()
            expression = item.this
        else:
            expression = item
            alias = item.name.casefold() if isinstance(item, exp.Column) else ""
        if (
            not isinstance(expression, exp.Column)
            or alias not in expected_fields
            or expression.name.casefold() != alias
            or expression.table.casefold() != source_alias.casefold()
            or expression.db
            or expression.catalog
            or alias in found
        ):
            return False
        found.add(alias)
    return found == expected_fields


def _subquery_source(select: Any, expected_alias: str) -> Any | None:
    """Return the exact named SELECT source from a Wren model wrapper."""

    from sqlglot import exp

    from_clause = select.args.get("from_") if isinstance(select, exp.Select) else None
    source = from_clause.this if isinstance(from_clause, exp.From) else None
    if (
        not isinstance(source, exp.Subquery)
        or source.alias_or_name.casefold() != expected_alias.casefold()
        or not isinstance(source.this, exp.Select)
    ):
        return None
    return source.this


def _validate_native_wren_sql(sql: Any, scenario: dict[str, str], snapshot_date: str) -> str:
    """Validate Wren's exact row-preserving model expansion to one HR table."""

    from sqlglot import exp

    code = "OBSERVED_NATIVE_SQL_NOT_CURRENT_SNAPSHOT_AVERAGE"
    root = _sql_ast(sql, code)
    with_clause = root.args.get("with_") if isinstance(root, exp.Select) else None
    if (
        not isinstance(root, exp.Select)
        or not isinstance(with_clause, exp.With)
        or with_clause.args.get("recursive") is True
        or len(with_clause.expressions) != 1
        or not _plain_projection_select(root, code, allow_with=True, allow_where=True)
        or len(root.expressions) != 1
    ):
        raise ReviewFailure(code)

    model = scenario["model"].casefold()
    table_name = scenario["table"].casefold()
    metric = scenario["column"].casefold()
    date_column = scenario["date_column"].casefold()
    fields = {metric, date_column}

    cte = with_clause.expressions[0]
    cte_alias = cte.args.get("alias")
    if (
        not isinstance(cte, exp.CTE)
        or cte.alias_or_name.casefold() != model
        or not isinstance(cte_alias, exp.TableAlias)
        or cte_alias.columns
        or not isinstance(cte.this, exp.Select)
        or cte.this.args.get("with_") is not None
    ):
        raise ReviewFailure(code)

    root_from = root.args.get("from_")
    root_table = root_from.this if isinstance(root_from, exp.From) else None
    if (
        not isinstance(root_table, exp.Table)
        or root_table.name.casefold() != model
        or root_table.db
        or root_table.catalog
        or root_table.alias_or_name.casefold() != model
    ):
        raise ReviewFailure(code)

    root_projection = root.expressions[0]
    if isinstance(root_projection, exp.Alias):
        root_projection = root_projection.this
    if (
        not isinstance(root_projection, exp.Avg)
        or root_projection.args.get("distinct") is not None
        or root_projection.args.get("filter") is not None
        or root_projection.args.get("over") is not None
        or not isinstance(root_projection.this, exp.Column)
        or root_projection.this.name.casefold() != metric
        or root_projection.this.table
        or root_projection.this.db
        or root_projection.this.catalog
    ):
        raise ReviewFailure(code)

    where = root.args.get("where")
    predicate = where.this if isinstance(where, exp.Where) else None
    if not isinstance(predicate, exp.EQ):
        raise ReviewFailure(code)
    left, right = predicate.this, predicate.expression
    if isinstance(left, exp.Column) and left.name.casefold() == date_column:
        date_expression, date_ref = right, left
    elif isinstance(right, exp.Column) and right.name.casefold() == date_column:
        date_expression, date_ref = left, right
    else:
        raise ReviewFailure(code)
    if (
        date_ref.table not in {"", model}
        or date_ref.db
        or date_ref.catalog
        or _literal_date(date_expression) != snapshot_date
    ):
        raise ReviewFailure(code)

    cte_query = cte.this
    if not _projection_fields(
        cte_query,
        fields,
        source_alias=f"wren_src_{model}",
        code=code,
    ):
        raise ReviewFailure(code)
    model_source = _subquery_source(cte_query, f"wren_src_{model}")
    if (
        model_source is None
        # Wren qualifies these two columns with the model alias at this layer.
        or not _projection_fields(model_source, fields, source_alias=model, code=code)
    ):
        raise ReviewFailure(code)
    physical_source = _subquery_source(model_source, model)
    if (
        physical_source is None
        or not _projection_fields(physical_source, fields, source_alias="__source", code=code)
    ):
        raise ReviewFailure(code)

    physical_from = physical_source.args.get("from_")
    physical_table = physical_from.this if isinstance(physical_from, exp.From) else None
    if (
        not isinstance(physical_table, exp.Table)
        or physical_table.name.casefold() != table_name
        or physical_table.db.casefold() != "hr"
        or physical_table.catalog
        or physical_table.alias_or_name.casefold() != "__source"
    ):
        raise ReviewFailure(code)

    all_selects = list(root.find_all(exp.Select))
    all_tables = list(root.find_all(exp.Table))
    all_subqueries = list(root.find_all(exp.Subquery))
    all_ctes = list(root.find_all(exp.CTE))
    all_columns = list(root.find_all(exp.Column))
    all_aggregates = list(root.find_all(exp.AggFunc))
    if (
        len(all_selects) != 4
        or len(all_tables) != 2
        or len(all_subqueries) != 2
        or len(all_ctes) != 1
        or len(all_aggregates) != 1
        or len(list(root.find_all(exp.Join))) != 0
        or len(list(root.find_all(exp.SetOperation))) != 0
        or len(list(root.find_all(exp.Window))) != 0
        or any(column.name.casefold() not in fields for column in all_columns)
    ):
        raise ReviewFailure(code)
    return sql


def _database_url() -> str:
    try:
        password = PASSWORD_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ReviewFailure("DATABASE_CREDENTIAL_UNAVAILABLE") from exc
    if not password or any(character in password for character in "\r\n\x00"):
        raise ReviewFailure("DATABASE_CREDENTIAL_INVALID")
    return (
        "postgresql://hr_benchmark_reader:"
        + quote(password, safe="")
        + "@127.0.0.1:55433/hr_enterprise?sslmode=disable"
    )


def _physical_oracle(scenario: dict[str, str]) -> tuple[str, Decimal]:
    try:
        import psycopg

        with psycopg.connect(
            _database_url(),
            connect_timeout=5,
            options="-c default_transaction_read_only=on",
        ) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            identity = connection.execute(
                "SELECT current_database(), current_user, current_setting('server_version_num')::int"
            ).fetchone()
            if (
                identity is None
                or identity[0] != "hr_enterprise"
                or identity[1] != "hr_benchmark_reader"
                or identity[2] // 10_000 != 17
            ):
                raise ReviewFailure("DATABASE_IDENTITY_MISMATCH")
            date_column = scenario["date_column"]
            table = scenario["table"]
            column = scenario["column"]
            latest = connection.execute(
                f"SELECT MAX({date_column}) FROM hr.{table}"
            ).fetchone()
            if latest is None or latest[0] is None:
                raise ReviewFailure("ORACLE_SNAPSHOT_DATE_MISSING")
            snapshot_date = latest[0].isoformat()
            result = connection.execute(
                f"SELECT AVG({column}) FROM hr.{table} WHERE {date_column} = %s",
                (latest[0],),
            ).fetchone()
            if result is None or result[0] is None:
                raise ReviewFailure("ORACLE_RESULT_MISSING")
            return snapshot_date, Decimal(result[0])
    except ReviewFailure:
        raise
    except Exception as exc:
        raise ReviewFailure("DATABASE_ORACLE_FAILED") from exc


def _structured_result(record: dict[str, Any]) -> dict[str, Any] | None:
    response = record.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        return None
    tool_result = response["result"]
    if tool_result.get("isError") is True:
        return None
    structured = tool_result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    contents = tool_result.get("content")
    if isinstance(contents, list):
        for content in contents:
            if not isinstance(content, dict) or not isinstance(content.get("text"), str):
                continue
            try:
                parsed = json.loads(
                    content["text"],
                    object_pairs_hook=_unique_object,
                    parse_float=Decimal,
                    parse_constant=_reject_constant,
                )
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _tool_call(record: dict[str, Any], tool_name: str) -> tuple[int, dict[str, Any], dict[str, Any]] | None:
    request = record.get("request")
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return None
    params = request.get("params")
    if not isinstance(params, dict) or params.get("name") != tool_name:
        return None
    arguments = params.get("arguments")
    result = _structured_result(record)
    sequence = record.get("sequence")
    if not isinstance(arguments, dict) or type(sequence) is not int or result is None:
        return None
    return sequence, arguments, result


def _review(
    work_dir: Path,
    transcript_path: Path,
    worker_thread_id: str,
    scenario_key: str,
) -> tuple[dict[str, Any], str]:
    work_dir = work_dir.expanduser().resolve()
    report_path = work_dir / "acceptance-report.json"
    report_bytes = report_path.read_bytes()
    acceptance = _read_json(report_path, "ACCEPTANCE_REPORT_UNAVAILABLE")
    if (
        acceptance.get("schemaVersion") != 1
        or acceptance.get("status") != "sql_lifecycle_passed_agent_review_pending"
        or acceptance.get("sourceFeedbackAndRegressionRecordsAreActual") is not True
        or acceptance.get("externalAgentEvaluationImported") is not False
        or acceptance.get("queryValuesIncluded") is not False
    ):
        raise ReviewFailure("ACCEPTANCE_NOT_PENDING_AGENT_REVIEW")
    scenario = SCENARIOS.get(scenario_key)
    if scenario is None:
        raise ReviewFailure("SCENARIO_INVALID")
    scenario_reports = acceptance.get("scenarios")
    scenario_report = scenario_reports.get(scenario_key) if isinstance(scenario_reports, dict) else None
    if (
        not isinstance(scenario_report, dict)
        or scenario_report.get("runStatus") != "completed"
        or scenario_report.get("sqlStatus") != "passed"
        or scenario_report.get("resolutionStatus") != "incomplete"
    ):
        raise ReviewFailure("ACCEPTANCE_SCENARIO_NOT_PENDING")

    transcript_path = transcript_path.expanduser().resolve()
    private_dir = (work_dir / "private").resolve()
    try:
        transcript_path.relative_to(private_dir)
    except ValueError as exc:
        raise ReviewFailure("WORKER_TRANSCRIPT_OUTSIDE_PRIVATE_DIRECTORY") from exc
    if not transcript_path.is_file() or transcript_path.suffix.casefold() != ".jsonl":
        raise ReviewFailure("WORKER_TRANSCRIPT_INVALID_PATH")
    records, transcript_sha256 = _read_transcript(transcript_path)
    if any(
        _contains_sensitive_header_key(record) or _contains_sensitive_secret_text(record)
        for record in records
    ):
        raise ReviewFailure("WORKER_TRANSCRIPT_CONTAINS_CREDENTIAL_HEADERS")

    selected = [record for record in records if record.get("workerThreadId") == worker_thread_id]
    if not selected:
        raise ReviewFailure("WORKER_THREAD_NOT_FOUND_IN_TRANSCRIPT")
    if any(_thread_id_or_none(record.get("workerThreadId")) is None for record in selected):
        raise ReviewFailure("WORKER_THREAD_BINDING_INVALID")

    calls: dict[str, list[tuple[int, dict[str, Any], dict[str, Any]]]] = {
        "semarail_get_context": [],
        "semarail_plan_query": [],
        "semarail_prepare_query": [],
        "semarail_governed_query": [],
    }
    for record in selected:
        request = record.get("request")
        params = request.get("params") if isinstance(request, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        tool_name = params.get("name") if isinstance(params, dict) else None
        if tool_name not in calls or not isinstance(arguments, dict):
            continue
        question = arguments.get("question")
        if tool_name == "semarail_plan_query":
            # Planning has only SQL as an argument; associate it below through
            # the exact prepare and execution inputs.
            pass
        elif question != scenario["question"]:
            continue
        parsed = _tool_call(record, tool_name)
        if parsed is not None:
            calls[tool_name].append(parsed)

    if not calls["semarail_get_context"] or not calls["semarail_prepare_query"]:
        raise ReviewFailure("WORKER_SCENARIO_CONTEXT_OR_PREPARATION_MISSING")
    prepare_call = calls["semarail_prepare_query"][-1]
    prepare_sequence, prepare_args, prepare_result = prepare_call
    prior_context_calls = [
        item for item in calls["semarail_get_context"] if item[0] < prepare_sequence
    ]
    if not prior_context_calls:
        raise ReviewFailure("WORKER_SCENARIO_CONTEXT_OR_PREPARATION_MISSING")
    context_call = prior_context_calls[-1]
    context_sequence, context_args, _context_result = context_call
    if context_args.get("question") != scenario["question"]:
        raise ReviewFailure("WORKER_CONTEXT_QUESTION_MISMATCH")
    if (
        prepare_args.get("question") != scenario["question"]
        or prepare_result.get("status") != "ready"
        or not isinstance(prepare_result.get("preparationId"), str)
    ):
        raise ReviewFailure("WORKER_PREPARATION_NOT_READY")

    submitted_semantic_sql = prepare_args.get("semantic_sql")
    snapshot_date, oracle_average = _physical_oracle(scenario)
    _validate_query_sql(
        submitted_semantic_sql,
        scenario,
        snapshot_date,
        require_hr_schema=False,
        code="SEMANTIC_SQL_NOT_CURRENT_SNAPSHOT_AVERAGE",
    )
    plan_matches = [
        item
        for item in calls["semarail_plan_query"]
        if item[1].get("semantic_sql") == submitted_semantic_sql
        and item[0] < prepare_sequence
    ]
    if not plan_matches:
        raise ReviewFailure("WORKER_QUERY_PLAN_MISSING")
    if not context_sequence < plan_matches[-1][0] < prepare_sequence:
        raise ReviewFailure("WORKER_TOOL_SEQUENCE_INVALID")

    successful_queries: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for item in calls["semarail_governed_query"]:
        sequence, arguments, result = item
        if sequence <= prepare_sequence:
            continue
        if arguments.get("semantic_sql") != submitted_semantic_sql:
            continue
        if arguments.get("preparation_id") != prepare_result.get("preparationId"):
            continue
        if arguments.get("question") != scenario["question"]:
            continue
        if result.get("status") == "success":
            successful_queries.append(item)
    if not successful_queries:
        raise ReviewFailure("WORKER_GOVERNED_QUERY_NOT_SUCCESSFUL")
    query_sequence, _query_arguments, query_result = successful_queries[-1]
    observed_semantic_sql = query_result.get("semanticSql")
    if observed_semantic_sql != submitted_semantic_sql:
        raise ReviewFailure("OBSERVED_SEMANTIC_SQL_MISMATCH")
    observed_native_sql = query_result.get("nativeSql")
    _validate_native_wren_sql(observed_native_sql, scenario, snapshot_date)
    preview_rows = query_result.get("previewRows")
    stats = query_result.get("stats")
    if (
        not isinstance(preview_rows, list)
        or len(preview_rows) != 1
        or not isinstance(preview_rows[0], dict)
        or len(preview_rows[0]) != 1
        or not isinstance(stats, dict)
        or stats.get("returnedRows") != 1
        or stats.get("truncated") is not False
    ):
        raise ReviewFailure("OBSERVED_QUERY_RESULT_SHAPE_INVALID")
    observed_average = _decimal(next(iter(preview_rows[0].values())), "OBSERVED_QUERY_RESULT_NOT_NUMERIC")
    matched = observed_average == oracle_average
    event_worker_ids = sorted(
        {
            item.get("workerThreadId")
            for item in records
            if isinstance(item.get("workerThreadId"), str)
        }
    )
    artifact_sha256 = hashlib.sha256(report_bytes).hexdigest()
    oracle_report: dict[str, Any] = {
        "schemaVersion": 1,
        "source": "independent_postgresql_physical_oracle",
        "status": "oracle_match_pending_root_agent_review" if matched else "oracle_mismatch_pending_root_agent_review",
        "createdAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "projectId": acceptance.get("projectId"),
        "datasetVersion": acceptance.get("datasetVersion"),
        "semanticVersion": acceptance.get("fixSemanticVersion"),
        "retainedAcceptanceReportSha256": artifact_sha256,
        "scenario": scenario_key,
        "workerThreadId": worker_thread_id,
        "workerThreadIdsSeenInTranscript": event_worker_ids,
        "transcriptFile": transcript_path.name,
        "transcriptSha256": transcript_sha256,
        "recordSequences": {
            "context": context_sequence,
            "plan": plan_matches[-1][0],
            "prepare": prepare_sequence,
            "governedQuery": query_sequence,
        },
        "requestedQuestion": scenario["question"],
        "submittedSemanticSql": submitted_semantic_sql,
        "observedSemanticSql": observed_semantic_sql,
        "observedNativeSql": observed_native_sql,
        "observedResult": {"average": str(observed_average)},
        "physicalOracle": {
            "snapshotDate": snapshot_date,
            "average": str(oracle_average),
        },
        "matched": matched,
        "agentReview": {"decision": "pending_root_review"},
    }
    private_output_dir = work_dir / "private"
    output_path = private_output_dir / (
        f"independent-oracle-{scenario_key}-{worker_thread_id}-{uuid.uuid4().hex[:8]}.json"
    )
    _write_private_json(output_path, oracle_report)
    return oracle_report, str(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--worker-thread-id", required=True)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), required=True)
    args = parser.parse_args(argv)
    try:
        thread_id = _strict_uuid(args.worker_thread_id)
        review, output_path = _review(args.work_dir, args.transcript, thread_id, args.scenario)
        safe_result = {
            "status": review["status"],
            "scenario": review["scenario"],
            "workerThreadId": review["workerThreadId"],
            "transcriptSha256": review["transcriptSha256"],
            "oracleReport": output_path,
            "matched": review["matched"],
            "agentReview": "pending_root_review",
        }
        print(json.dumps(safe_result, sort_keys=True, separators=(",", ":")))
        return 0 if review["matched"] else 1
    except ReviewFailure as exc:
        print(json.dumps({"status": "failed", "code": str(exc)}, separators=(",", ":")))
        return 1
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not code.replace("_", "").isalnum():
            code = "UNEXPECTED_REVIEW_FAILURE"
        print(json.dumps({"status": "failed", "code": code, "type": type(exc).__name__}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
