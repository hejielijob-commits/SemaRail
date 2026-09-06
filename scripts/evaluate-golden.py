#!/usr/bin/env python3
"""Evaluate real Harness golden-question evidence for AC02.

This program does not invoke a model and never synthesizes an acceptance
claim.  It consumes normalized captures produced by a real Harness/model run,
validates the one-context / first-query / at-most-one-repair protocol, checks
both SQL statements with the production AST policy, and compares bounded real
query results with the deterministic PostgreSQL fixture oracles.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SIDECAR_ROOT = ROOT / "python" / "sidecar"
if str(SIDECAR_ROOT) not in sys.path:
    sys.path.insert(0, str(SIDECAR_ROOT))

try:
    from sidecar.sql_policy import (  # type: ignore[import-not-found]
        PhysicalAllowlist,
        PhysicalTable,
        SqlPolicyError,
        extract_table_names,
        validate_native_sql,
        validate_semantic_sql,
    )
except ImportError as exc:  # pragma: no cover - exercised by prerequisite failure
    raise SystemExit(
        "golden evaluator requires the Sidecar environment (sqlglot); "
        "set WREN_PYTHON or use the project .venv"
    ) from exc


CORPUS_DEFAULT = ROOT / "examples" / "wren-postgres" / "golden-questions.json"
FIRST_PASS_TARGET = 16
AFTER_REPAIR_TARGET = 18
QUESTION_COUNT = 20
REPAIRABLE_CODES = frozenset({"SEMANTIC_ERROR"})
NEVER_RETRY_CODES = frozenset({"POLICY_DENIED", "TIMEOUT", "CANCELLED"})
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DATE_TOKEN = re.compile(r"^\$\{FIXTURE_DATE(?:(?P<sign>[+-])(?P<days>\d+)D)?\}(?P<suffix>.*)$")
PHYSICAL_ALLOWLIST = PhysicalAllowlist(
    frozenset(
        PhysicalTable("wren", "public", table)
        for table in ("customers", "order_items", "orders", "products", "regions")
    )
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_PARAMETER_KEYS = frozenset(
    {"temperature", "topP", "maxTokens", "seed", "reasoningEffort"}
)


@dataclass(frozen=True, slots=True)
class CorpusConfig:
    """Validated corpus policy kept separate from untrusted question data."""

    schema_version: int
    questions: tuple[Mapping[str, Any], ...]
    first_pass_target: int
    after_repair_target: int
    authorization_target: int
    physical_allowlist: PhysicalAllowlist
    corpus_id: str | None = None
    data_sha256: str | None = None


class EvidenceError(ValueError):
    """A safe structural diagnostic that never embeds captured values."""


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise EvidenceError(f"{label} contains unsupported fields")


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise EvidenceError(f"{label} must be a bounded safe identifier")
    return value


def _positive_index(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise EvidenceError(f"{label} must be a positive integer")
    return value


def _iso_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise EvidenceError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise EvidenceError(f"{label} must use canonical YYYY-MM-DD form")
    return parsed


def _validate_success_oracle(oracle: Mapping[str, Any], label: str, *, v2: bool) -> None:
    columns = oracle.get("expectedColumns")
    models = oracle.get("requiredModels")
    rows = oracle.get("rows")
    try:
        tolerance = Decimal(str(oracle.get("numericTolerance")))
    except (InvalidOperation, ValueError) as exc:
        raise EvidenceError(f"{label} has an invalid numeric tolerance") from exc
    if tolerance < 0:
        raise EvidenceError(f"{label} has a negative numeric tolerance")
    if (
        not isinstance(columns, list)
        or not columns
        or len(columns) != len(set(columns))
        or not all(isinstance(item, str) and item for item in columns)
    ):
        raise EvidenceError(f"{label} has invalid expected columns")
    if (
        not isinstance(models, list)
        or not models
        or not all(isinstance(item, str) and item for item in models)
    ):
        raise EvidenceError(f"{label} has invalid required models")
    if v2 and rows is None:
        return
    if not isinstance(rows, list) or not all(isinstance(item, Mapping) for item in rows):
        raise EvidenceError(f"{label} has invalid oracle rows")
    expected_keys = set(columns)
    if any(set(row) != expected_keys for row in rows):
        raise EvidenceError(f"{label} oracle row keys do not match columns")
    aliases = oracle.get("columnAliases", {})
    if not isinstance(aliases, Mapping) or not set(aliases).issubset(expected_keys):
        raise EvidenceError(f"{label} has invalid column aliases")
    seen_aliases: set[str] = set()
    for column, values in aliases.items():
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value for value in values)
            or len(values) != len(set(values))
        ):
            raise EvidenceError(f"{label} has invalid aliases for {column}")
        for value in values:
            if value in expected_keys or value in seen_aliases:
                raise EvidenceError(f"{label} has ambiguous column alias {value}")
            seen_aliases.add(value)
    extras = oracle.get("allowedExtraColumns", [])
    if (
        not isinstance(extras, list)
        or not all(isinstance(value, str) and value for value in extras)
        or len(extras) != len(set(extras))
        or set(extras) & (expected_keys | seen_aliases)
    ):
        raise EvidenceError(f"{label} has invalid allowed extra columns")


def _load_corpus(path: Path) -> tuple[CorpusConfig, bytes]:
    raw = path.read_bytes()
    try:
        corpus = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("golden corpus is not valid UTF-8 JSON") from exc
    root = _object(corpus, "corpus")
    schema_version = root.get("schemaVersion")
    if schema_version not in {1, 2} or root.get("oracleVersion") != schema_version:
        raise EvidenceError("unsupported golden corpus/oracle version")
    questions = root.get("questions")
    if not isinstance(questions, list):
        raise EvidenceError("golden corpus questions must be an array")
    if schema_version == 1 and len(questions) != QUESTION_COUNT:
        raise EvidenceError("golden corpus must contain exactly 20 questions")
    if schema_version == 2 and not questions:
        raise EvidenceError("golden corpus must contain questions")
    seen: set[str] = set()
    authorization_count = 0
    for index, raw_question in enumerate(questions):
        question = _object(raw_question, f"question[{index}]")
        question_id = _safe_id(question.get("id"), f"question[{index}].id")
        if question_id in seen:
            raise EvidenceError("golden corpus contains a duplicate question id")
        seen.add(question_id)
        if not isinstance(question.get("question"), str) or not question["question"].strip():
            raise EvidenceError(f"question[{index}] has no prompt")
        oracle = _object(question.get("oracle"), f"question[{index}].oracle")
        if schema_version == 1:
            _validate_success_oracle(oracle, f"question[{index}]", v2=False)
            continue
        category = question.get("category")
        if category not in {"basic", "analytical", "authorization"}:
            raise EvidenceError(f"question[{index}] has invalid category")
        if question.get("language") not in {"zh-CN", "en"}:
            raise EvidenceError(f"question[{index}] has invalid language")
        _safe_id(question.get("actor"), f"question[{index}].actor")
        canonical = _object(question.get("canonical"), f"question[{index}].canonical")
        if set(canonical) != {"semanticSql", "nativeSql"} or not all(
            isinstance(value, str) and value.strip() for value in canonical.values()
        ):
            raise EvidenceError(f"question[{index}] has invalid canonical SQL")
        try:
            validate_semantic_sql(str(canonical["semanticSql"]))
        except SqlPolicyError as exc:
            raise EvidenceError(f"question[{index}] canonical semantic SQL violates policy") from exc
        expectation = _object(question.get("expectation"), f"question[{index}].expectation")
        outcome = expectation.get("outcome")
        if outcome == "denied":
            authorization_count += 1
            if category != "authorization" or expectation.get("errorCode") != "POLICY_DENIED" or expectation.get("allowRepair") is not False:
                raise EvidenceError(f"question[{index}] denial must be a non-retryable authorization scenario")
            if oracle != {"type": "denial", "errorCode": "POLICY_DENIED", "databaseRowsReturned": 0}:
                raise EvidenceError(f"question[{index}] has an invalid denial oracle")
        elif outcome == "success":
            if expectation.get("allowRepair") is not True or oracle.get("type") != "canonical_query":
                raise EvidenceError(f"question[{index}] has an invalid success expectation")
            _validate_success_oracle(oracle, f"question[{index}]", v2=True)
            if type(oracle.get("orderSensitive")) is not bool:
                raise EvidenceError(f"question[{index}] has invalid orderSensitive")
            if "maxRows" in oracle and (type(oracle["maxRows"]) is not int or oracle["maxRows"] < 1 or oracle["maxRows"] > 500):
                raise EvidenceError(f"question[{index}] has invalid maxRows")
        else:
            raise EvidenceError(f"question[{index}] has invalid expected outcome")

    if schema_version == 1:
        return CorpusConfig(1, tuple(questions), FIRST_PASS_TARGET, AFTER_REPAIR_TARGET, 0, PHYSICAL_ALLOWLIST), raw

    thresholds = _object(root.get("thresholds"), "corpus.thresholds")
    first_target = thresholds.get("firstPass")
    final_target = thresholds.get("afterAtMostOneRepair")
    auth_target = thresholds.get("authorization")
    if any(type(value) is not int or value < 0 or value > len(questions) for value in (first_target, final_target, auth_target)):
        raise EvidenceError("corpus thresholds are invalid")
    if first_target > final_target or auth_target != authorization_count:
        raise EvidenceError("corpus thresholds conflict with question categories")
    corpus_id = _safe_id(root.get("corpusId"), "corpus.corpusId")
    table_names = root.get("physicalTables")
    if not isinstance(table_names, list) or not table_names or len(table_names) != len(set(table_names)):
        raise EvidenceError("corpus physicalTables is invalid")
    physical_tables: set[PhysicalTable] = set()
    for item in table_names:
        if not isinstance(item, str):
            raise EvidenceError("corpus physicalTables is invalid")
        parts = item.split(".")
        if len(parts) != 2 or any(not SAFE_ID.fullmatch(part) for part in parts):
            raise EvidenceError("corpus physicalTables must use schema.table names")
        physical_tables.add(PhysicalTable(None, parts[0], parts[1]))
    dataset = _object(root.get("dataset"), "corpus.dataset")
    data_sha256 = dataset.get("sha256")
    if not isinstance(data_sha256, str) or not SHA256.fullmatch(data_sha256):
        raise EvidenceError("corpus dataset.sha256 is invalid")
    allowlist = PhysicalAllowlist(frozenset(physical_tables))
    for index, question in enumerate(questions):
        native_sql = str(_object(question["canonical"], "canonical")["nativeSql"])
        if "${ACTOR_" in native_sql:
            continue
        try:
            validate_native_sql(native_sql, allowed_physical=allowlist)
        except SqlPolicyError as exc:
            raise EvidenceError(f"question[{index}] canonical native SQL violates policy") from exc
    return CorpusConfig(2, tuple(questions), first_target, final_target, auth_target, allowlist, corpus_id, data_sha256), raw


def load_corpus(path: Path) -> tuple[list[Mapping[str, Any]], bytes]:
    """Compatibility wrapper preserving the v1 caller contract."""
    config, raw = _load_corpus(path)
    return list(config.questions), raw


def _safe_run_metadata(value: Any) -> Mapping[str, Any]:
    run = _object(value, "evidence.run")
    _exact_keys(run, {"model", "parameters"}, "evidence.run")
    model = _object(run.get("model"), "evidence.run.model")
    _exact_keys(model, {"provider", "id", "version"}, "evidence.run.model")
    provider = _safe_id(model.get("provider"), "evidence.run.model.provider")
    model_id = _safe_id(model.get("id"), "evidence.run.model.id")
    version = _safe_id(model.get("version"), "evidence.run.model.version")
    parameters = _object(run.get("parameters"), "evidence.run.parameters")
    if set(parameters) - SAFE_PARAMETER_KEYS:
        raise EvidenceError("evidence.run.parameters contains unsupported fields")
    for key, item in parameters.items():
        if isinstance(item, bool) or not isinstance(item, (str, int, float)) or isinstance(item, float) and (item != item or abs(item) == float("inf")):
            raise EvidenceError(f"evidence.run.parameters.{key} is invalid")
        if isinstance(item, str) and (not item or len(item) > 64):
            raise EvidenceError(f"evidence.run.parameters.{key} is invalid")
    return {
        "model": {"provider": provider, "id": model_id, "version": version},
        "parameters": dict(parameters),
    }


def _load_evidence(path: Path, schema_version: int) -> tuple[list[Mapping[str, Any]], bytes, Mapping[str, Any] | None, str | None]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("evidence is not UTF-8") from exc
    records: Any
    if path.suffix.lower() == ".jsonl":
        parsed: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EvidenceError(f"evidence JSONL line {line_number} is invalid") from exc
        records = parsed
    else:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EvidenceError("evidence JSON is invalid") from exc
        if isinstance(value, Mapping):
            if value.get("schemaVersion") != schema_version or not isinstance(value.get("records"), list):
                raise EvidenceError(f"evidence envelope must be schemaVersion {schema_version} with records")
            records = value["records"]
            if schema_version == 2:
                run_metadata = _safe_run_metadata(value.get("run"))
                data_sha256 = value.get("dataSha256")
                if not isinstance(data_sha256, str) or not SHA256.fullmatch(data_sha256):
                    raise EvidenceError("evidence.dataSha256 is invalid")
            else:
                run_metadata = None
                data_sha256 = None
        else:
            if schema_version == 2:
                raise EvidenceError("schemaVersion 2 evidence requires an envelope")
            records = value
            run_metadata = None
            data_sha256 = None
    if not isinstance(records, list) or not all(isinstance(item, Mapping) for item in records):
        raise EvidenceError("evidence must be a JSON array/envelope or JSONL objects")
    if path.suffix.lower() == ".jsonl":
        if schema_version == 2:
            raise EvidenceError("schemaVersion 2 evidence requires a JSON envelope")
        run_metadata = None
        data_sha256 = None
    return records, raw, run_metadata, data_sha256


def load_evidence(path: Path) -> tuple[list[Mapping[str, Any]], bytes]:
    """Compatibility wrapper preserving schemaVersion 1 evidence loading."""
    records, raw, _, _ = _load_evidence(path, 1)
    return records, raw


def _validate_error(value: Any, label: str) -> Mapping[str, Any]:
    error = _object(value, label)
    _exact_keys(error, {"code", "phase", "message", "retryable"}, label)
    _safe_id(error.get("code"), f"{label}.code")
    if not isinstance(error.get("phase"), str) or not error["phase"]:
        raise EvidenceError(f"{label}.phase is required")
    if not isinstance(error.get("message"), str) or not error["message"]:
        raise EvidenceError(f"{label}.message is required")
    if type(error.get("retryable")) is not bool:
        raise EvidenceError(f"{label}.retryable must be boolean")
    return error


def _validate_presentation(value: Any, label: str) -> Mapping[str, Any]:
    presentation = _object(value, label)
    schema_version = presentation.get("schemaVersion")
    if schema_version not in {1, 2}:
        raise EvidenceError(f"{label} has unsupported schemaVersion")
    _exact_keys(
        presentation,
        {
            "schemaVersion", "queryId", "status", "semanticSql", "question", "sqlHistory",
            "nativeSql", "columns", "previewRows", "chart", "stats", "error",
            "delivery", "artifact",
        },
        label,
    )
    _safe_id(presentation.get("queryId"), f"{label}.queryId")
    if presentation.get("status") not in {"success", "error"}:
        raise EvidenceError(f"{label}.status must be success or error")
    sql = presentation.get("semanticSql")
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 64_000:
        raise EvidenceError(f"{label}.semanticSql is required and bounded")
    columns = presentation.get("columns")
    rows = presentation.get("previewRows")
    stats = _object(presentation.get("stats"), f"{label}.stats")
    expected_stats = {"returnedRows", "durationMs", "truncated"}
    if schema_version == 2:
        expected_stats.add("previewedRows")
    _exact_keys(stats, expected_stats, f"{label}.stats")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise EvidenceError(f"{label} columns/previewRows must be arrays")
    if type(stats.get("returnedRows")) is not int or stats["returnedRows"] < 0:
        raise EvidenceError(f"{label}.stats.returnedRows is invalid")
    if type(stats.get("truncated")) is not bool:
        raise EvidenceError(f"{label}.stats.truncated must be boolean")
    if not isinstance(stats.get("durationMs"), (int, float)) or isinstance(stats.get("durationMs"), bool) or stats["durationMs"] < 0:
        raise EvidenceError(f"{label}.stats.durationMs is invalid")
    if schema_version == 2:
        if type(stats.get("previewedRows")) is not int or stats["previewedRows"] != len(rows):
            raise EvidenceError(f"{label}.stats.previewedRows must equal previewRows length")
    if presentation["status"] == "error":
        if "delivery" in presentation or "artifact" in presentation or "chart" in presentation:
            raise EvidenceError(f"{label} error must not contain delivery, artifact, or chart")
        _validate_error(presentation.get("error"), f"{label}.error")
    elif "error" in presentation:
        raise EvidenceError(f"{label} success must not contain error")
    elif schema_version == 2:
        delivery = presentation.get("delivery")
        if delivery not in {"inline", "artifact"}:
            raise EvidenceError(f"{label}.delivery must be inline or artifact")
        if delivery == "inline" and "artifact" in presentation:
            raise EvidenceError(f"{label} inline delivery must not contain artifact")
        if delivery == "artifact":
            artifact = _object(presentation.get("artifact"), f"{label}.artifact")
            if artifact.get("rowCount") != stats.get("returnedRows") or "chart" in presentation:
                raise EvidenceError(f"{label} artifact delivery metadata is inconsistent")
    return presentation


def normalize_records(
    records: Sequence[Mapping[str, Any]],
    question_ids: set[str],
    *,
    expected_model: str | None = None,
) -> tuple[dict[str, dict[int, Mapping[str, Any]]], str, date]:
    grouped: dict[str, dict[int, Mapping[str, Any]]] = {}
    run_id: str | None = None
    fixture_date: date | None = None
    for index, record in enumerate(records):
        label = f"record[{index}]"
        _exact_keys(
            record,
            {"schemaVersion", "runId", "questionId", "attempt", "fixtureDate", "capturedAt", "model", "context", "query", "repairOfAttempt", "repairReasonCode"},
            label,
        )
        if record.get("schemaVersion") != 1:
            raise EvidenceError(f"{label} has unsupported schemaVersion")
        current_run = _safe_id(record.get("runId"), f"{label}.runId")
        if expected_model is not None and record.get("model") != expected_model:
            raise EvidenceError("all evidence records must match the declared model identity")
        question_id = _safe_id(record.get("questionId"), f"{label}.questionId")
        if question_id not in question_ids:
            raise EvidenceError(f"{label} references an unknown question")
        attempt = record.get("attempt")
        if attempt not in {1, 2}:
            raise EvidenceError(f"{label}.attempt must be 1 or 2")
        if attempt == 1 and ("repairOfAttempt" in record or "repairReasonCode" in record):
            raise EvidenceError(f"{label} first pass cannot declare repair provenance")
        current_date = _iso_date(record.get("fixtureDate"), f"{label}.fixtureDate")
        if run_id is None:
            run_id = current_run
            fixture_date = current_date
        elif current_run != run_id or current_date != fixture_date:
            raise EvidenceError("all evidence records must share one runId and fixtureDate")
        if "capturedAt" in record:
            try:
                datetime.fromisoformat(str(record["capturedAt"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise EvidenceError(f"{label}.capturedAt must be ISO-8601") from exc
        context = _object(record.get("context"), f"{label}.context")
        _exact_keys(context, {"status", "callIndex", "error"}, f"{label}.context")
        if context.get("status") not in {"success", "error"}:
            raise EvidenceError(f"{label}.context.status must be success or error")
        context_index = _positive_index(context.get("callIndex"), f"{label}.context.callIndex")
        if context_index != 1:
            raise EvidenceError(f"{label} semantic context must be the first data-agent tool call")
        if context["status"] == "error":
            _validate_error(context.get("error"), f"{label}.context.error")
            if "query" in record:
                raise EvidenceError(f"{label} cannot query after failed context")
        else:
            if "error" in context:
                raise EvidenceError(f"{label} successful context must not contain error")
            query = _object(record.get("query"), f"{label}.query")
            _exact_keys(query, {"callIndex", "presentation"}, f"{label}.query")
            query_index = _positive_index(query.get("callIndex"), f"{label}.query.callIndex")
            expected_index = 2 if attempt == 1 else 3
            if query_index != expected_index:
                raise EvidenceError(f"{label} query has an invalid first-pass/repair call sequence")
            _validate_presentation(query.get("presentation"), f"{label}.query.presentation")
        attempts = grouped.setdefault(question_id, {})
        if attempt in attempts:
            raise EvidenceError("duplicate question/attempt evidence is not allowed")
        attempts[attempt] = record
    if run_id is None or fixture_date is None:
        raise EvidenceError("evidence is empty")
    if set(grouped) != question_ids:
        raise EvidenceError("evidence must contain every golden question exactly once at first pass")
    if any(1 not in attempts for attempts in grouped.values()):
        raise EvidenceError("every question requires one first-pass record")
    return grouped, run_id, fixture_date


def _presentation(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    query = record.get("query")
    if not isinstance(query, Mapping):
        return None
    value = query.get("presentation")
    return value if isinstance(value, Mapping) else None


def validate_repair_protocol(grouped: Mapping[str, Mapping[int, Mapping[str, Any]]]) -> None:
    for attempts in grouped.values():
        repair = attempts.get(2)
        if repair is None:
            continue
        first = attempts[1]
        first_presentation = _presentation(first)
        if first_presentation is None or first_presentation.get("status") != "error":
            raise EvidenceError("repair is allowed only after a failed first query")
        error = _object(first_presentation.get("error"), "first-pass error")
        code = error.get("code")
        if code in NEVER_RETRY_CODES:
            raise EvidenceError("policy, timeout, and cancellation failures must never be retried")
        if code not in REPAIRABLE_CODES or error.get("retryable") is not True:
            raise EvidenceError("repair requires a retryable semantic error")
        if repair.get("repairOfAttempt") != 1 or repair.get("repairReasonCode") != code:
            raise EvidenceError("repair provenance must identify first attempt and its error code")
        first_context = _object(first.get("context"), "first context")
        repair_context = _object(repair.get("context"), "repair context")
        if repair_context.get("status") != "success" or repair_context.get("callIndex") != first_context.get("callIndex"):
            raise EvidenceError("repair must reuse the successful first-pass semantic context")
        first_query = _object(first.get("query"), "first query")
        repair_query = _object(repair.get("query"), "repair query")
        if repair_query.get("callIndex", 0) <= first_query.get("callIndex", 0):
            raise EvidenceError("repair query must follow the failed first query")


def _resolve_expected(value: Any, fixture_date: date) -> Any:
    if not isinstance(value, str):
        return value
    match = DATE_TOKEN.match(value)
    if not match:
        return value
    days = int(match.group("days") or "0")
    if match.group("sign") == "-":
        days = -days
    resolved = (fixture_date + timedelta(days=days)).isoformat()
    return resolved + match.group("suffix")


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _scalar_matches(actual: Any, expected: Any, fixture_date: date, tolerance: Decimal) -> bool:
    expected = _resolve_expected(expected, fixture_date)
    if isinstance(expected, str) and expected.endswith("*"):
        return isinstance(actual, str) and actual.startswith(expected[:-1])
    actual_decimal = _decimal(actual)
    expected_decimal = _decimal(expected)
    if actual_decimal is not None and expected_decimal is not None:
        return abs(actual_decimal - expected_decimal) <= tolerance
    if isinstance(expected, str) and len(expected) == 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", expected):
        return isinstance(actual, str) and actual[:10] == expected
    return actual == expected


def _row_matches(actual: Mapping[str, Any], expected: Mapping[str, Any], fixture_date: date, tolerance: Decimal) -> bool:
    return set(actual) == set(expected) and all(
        _scalar_matches(actual[key], value, fixture_date, tolerance)
        for key, value in expected.items()
    )


def _unordered_rows_match(actual: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]], fixture_date: date, tolerance: Decimal) -> bool:
    if len(actual) != len(expected):
        return False
    remaining = list(actual)
    for expected_row in expected:
        for index, actual_row in enumerate(remaining):
            if _row_matches(actual_row, expected_row, fixture_date, tolerance):
                remaining.pop(index)
                break
        else:
            return False
    return not remaining


def _normalize_result_columns(
    actual_names: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    oracle: Mapping[str, Any],
) -> list[dict[str, Any]] | None:
    expected = list(oracle["expectedColumns"])
    aliases = oracle.get("columnAliases", {})
    allowed_extras = set(oracle.get("allowedExtraColumns", []))
    projection: dict[str, str] = {}
    consumed: set[str] = set()
    for expected_name in expected:
        candidates = [expected_name, *aliases.get(expected_name, [])]
        matches = [candidate for candidate in candidates if candidate in actual_names]
        if len(matches) != 1:
            return None
        projection[expected_name] = matches[0]
        consumed.add(matches[0])
    if set(actual_names) - consumed - allowed_extras:
        return None
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if set(row) != set(actual_names):
            return None
        normalized.append({expected_name: row[actual_name] for expected_name, actual_name in projection.items()})
    return normalized


def evaluate_attempt(
    record: Mapping[str, Any],
    question: Mapping[str, Any],
    fixture_date: date,
    *,
    physical_allowlist: PhysicalAllowlist = PHYSICAL_ALLOWLIST,
) -> tuple[bool, str]:
    context = _object(record["context"], "context")
    if context.get("status") != "success":
        return False, "CONTEXT_ERROR"
    presentation = _presentation(record)
    oracle = _object(question["oracle"], "oracle")
    if oracle.get("type") == "denial":
        if presentation is None or presentation.get("status") != "error":
            return False, "POLICY_NOT_DENIED"
        error = presentation.get("error")
        stats = presentation.get("stats")
        if not isinstance(error, Mapping) or error.get("code") != oracle.get("errorCode"):
            return False, "WRONG_DENIAL"
        if not isinstance(stats, Mapping) or stats.get("returnedRows") != oracle.get("databaseRowsReturned"):
            return False, "POLICY_ROWS_RETURNED"
        return True, "PASS"
    if presentation is None or presentation.get("status") != "success":
        error = presentation.get("error") if isinstance(presentation, Mapping) else None
        code = error.get("code") if isinstance(error, Mapping) else None
        return False, f"QUERY_{code}" if isinstance(code, str) and SAFE_ID.fullmatch(code) else "QUERY_ERROR"
    if presentation.get("schemaVersion") == 2 and presentation.get("delivery") != "inline":
        return False, "INCOMPLETE_RESULT"
    semantic_sql = presentation.get("semanticSql")
    native_sql = presentation.get("nativeSql")
    if not isinstance(semantic_sql, str) or not isinstance(native_sql, str):
        return False, "MISSING_SQL"
    try:
        validate_semantic_sql(semantic_sql)
        validate_native_sql(native_sql, allowed_physical=physical_allowlist)
        referenced = {name.lower() for name in extract_table_names(semantic_sql)}
    except SqlPolicyError:
        return False, "SQL_POLICY"
    required = {str(item).lower() for item in oracle["requiredModels"]}
    if not required.issubset(referenced):
        return False, "MISSING_CORE_MODEL"
    columns = presentation.get("columns")
    actual_names: list[str] = []
    if not isinstance(columns, list):
        return False, "COLUMNS"
    for column in columns:
        if not isinstance(column, Mapping) or not isinstance(column.get("name"), str):
            return False, "COLUMNS"
        actual_names.append(column["name"])
    if len(actual_names) != len(set(actual_names)):
        return False, "COLUMNS"
    rows = presentation.get("previewRows")
    stats = presentation.get("stats")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows) or not isinstance(stats, Mapping):
        return False, "ROWS"
    normalized_rows = _normalize_result_columns(actual_names, rows, oracle)
    if normalized_rows is None:
        return False, "COLUMNS"
    if stats.get("truncated") is not False or stats.get("returnedRows") != len(rows):
        return False, "INCOMPLETE_RESULT"
    if "maxRows" in oracle and len(rows) > oracle["maxRows"]:
        return False, "ROW_LIMIT"
    tolerance = Decimal(str(oracle["numericTolerance"]))
    expected_rows = oracle.get("rows")
    if expected_rows is not None:
        if oracle.get("orderSensitive"):
            matches = len(normalized_rows) == len(expected_rows) and all(
                _row_matches(actual, expected, fixture_date, tolerance)
                for actual, expected in zip(normalized_rows, expected_rows)
            )
        else:
            matches = _unordered_rows_match(normalized_rows, expected_rows, fixture_date, tolerance)
        if not matches:
            return False, "ORACLE_MISMATCH"
    return True, "PASS"


def evaluate(questions: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]], corpus_raw: bytes, evidence_raw: bytes) -> dict[str, Any]:
    by_id = {str(item["id"]): item for item in questions}
    grouped, run_id, fixture_date = normalize_records(records, set(by_id))
    validate_repair_protocol(grouped)
    question_rows: list[dict[str, Any]] = []
    first_count = 0
    final_count = 0
    repair_count = 0
    for question in questions:
        question_id = str(question["id"])
        attempts = grouped[question_id]
        first_pass, first_reason = evaluate_attempt(attempts[1], question, fixture_date)
        final_pass, final_reason = first_pass, first_reason
        repaired = 2 in attempts
        if repaired:
            repair_count += 1
            final_pass, final_reason = evaluate_attempt(attempts[2], question, fixture_date)
        first_count += int(first_pass)
        final_count += int(final_pass)
        question_rows.append({
            "id": question_id,
            "firstPass": first_pass,
            "afterAtMostOneRepair": final_pass,
            "repairAttempted": repaired,
            "firstReason": first_reason,
            "finalReason": final_reason,
        })
    passed = first_count >= FIRST_PASS_TARGET and final_count >= AFTER_REPAIR_TARGET
    return {
        "schemaVersion": 1,
        "status": "pass" if passed else "fail",
        "claim": "AC02_VERIFIED" if passed else "AC02_NOT_VERIFIED",
        "runId": run_id,
        "fixtureDate": fixture_date.isoformat(),
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "corpusSha256": hashlib.sha256(corpus_raw).hexdigest(),
        "evidenceSha256": hashlib.sha256(evidence_raw).hexdigest(),
        "thresholds": {"firstPass": FIRST_PASS_TARGET, "afterAtMostOneRepair": AFTER_REPAIR_TARGET, "total": QUESTION_COUNT},
        "counts": {"firstPass": first_count, "afterAtMostOneRepair": final_count, "repairs": repair_count},
        "questions": question_rows,
    }


def _latency_summary(values: Sequence[float]) -> Mapping[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "median": None, "p95": None}
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    p95 = ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]
    return {"count": len(ordered), "median": round(median, 3), "p95": round(p95, 3)}


def _attempt_latency(record: Mapping[str, Any]) -> float | None:
    presentation = _presentation(record)
    stats = presentation.get("stats") if isinstance(presentation, Mapping) else None
    duration = stats.get("durationMs") if isinstance(stats, Mapping) else None
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0 or duration > 86_400_000:
        return None
    return float(duration)


def evaluate_v2(
    config: CorpusConfig,
    records: Sequence[Mapping[str, Any]],
    corpus_raw: bytes,
    evidence_raw: bytes,
    run_metadata: Mapping[str, Any],
    evidence_data_sha256: str,
) -> dict[str, Any]:
    """Evaluate configurable enterprise corpora without exposing captured rows."""
    if config.schema_version != 2 or config.data_sha256 is None:
        raise EvidenceError("schemaVersion 2 corpus configuration is required")
    if evidence_data_sha256 != config.data_sha256:
        raise EvidenceError("evidence data hash does not match the corpus dataset")
    model = _object(run_metadata.get("model"), "evidence.run.model")
    model_id = str(model["id"])
    questions = config.questions
    by_id = {str(item["id"]): item for item in questions}
    grouped, run_id, fixture_date = normalize_records(records, set(by_id), expected_model=model_id)
    validate_repair_protocol(grouped)
    question_rows: list[dict[str, Any]] = []
    first_count = final_count = repair_count = successful_repairs = authorization_count = 0
    latencies: list[float] = []
    first_failures: dict[str, int] = {}
    final_failures: dict[str, int] = {}
    for question in questions:
        question_id = str(question["id"])
        attempts = grouped[question_id]
        first_pass, first_reason = evaluate_attempt(
            attempts[1], question, fixture_date, physical_allowlist=config.physical_allowlist
        )
        final_pass, final_reason = first_pass, first_reason
        repaired = 2 in attempts
        if question.get("category") == "authorization":
            authorization_count += int(first_pass)
        if repaired:
            repair_count += 1
            final_pass, final_reason = evaluate_attempt(
                attempts[2], question, fixture_date, physical_allowlist=config.physical_allowlist
            )
            successful_repairs += int(not first_pass and final_pass)
        first_count += int(first_pass)
        final_count += int(final_pass)
        if first_reason != "PASS":
            first_failures[first_reason] = first_failures.get(first_reason, 0) + 1
        if final_reason != "PASS":
            final_failures[final_reason] = final_failures.get(final_reason, 0) + 1
        for record in attempts.values():
            latency = _attempt_latency(record)
            if latency is None:
                raise EvidenceError("schemaVersion 2 evidence requires a bounded durationMs for every query attempt")
            latencies.append(latency)
        question_rows.append({
            "id": question_id,
            "category": question["category"],
            "firstPass": first_pass,
            "afterAtMostOneRepair": final_pass,
            "repairAttempted": repaired,
            "firstReason": first_reason,
            "finalReason": final_reason,
        })
    passed = (
        first_count >= config.first_pass_target
        and final_count >= config.after_repair_target
        and authorization_count == config.authorization_target
    )
    total = len(questions)
    return {
        "schemaVersion": 2,
        "status": "pass" if passed else "fail",
        "claim": "HR_ENTERPRISE_VERIFIED" if passed else "HR_ENTERPRISE_NOT_VERIFIED",
        "corpusId": config.corpus_id,
        "runId": run_id,
        "fixtureDate": fixture_date.isoformat(),
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "model": dict(model),
        "runParameters": dict(_object(run_metadata.get("parameters"), "evidence.run.parameters")),
        "hashes": {
            "corpusSha256": hashlib.sha256(corpus_raw).hexdigest(),
            "dataSha256": evidence_data_sha256,
            "evidenceSha256": hashlib.sha256(evidence_raw).hexdigest(),
        },
        "thresholds": {
            "firstPass": config.first_pass_target,
            "afterAtMostOneRepair": config.after_repair_target,
            "authorization": config.authorization_target,
            "total": total,
        },
        "counts": {
            "firstPass": first_count,
            "afterAtMostOneRepair": final_count,
            "authorization": authorization_count,
            "repairs": repair_count,
        },
        "metrics": {
            "firstPassAccuracy": first_count / total,
            "afterAtMostOneRepairAccuracy": final_count / total,
            "repairRate": repair_count / total,
            "repairSuccessRate": successful_repairs / repair_count if repair_count else 0.0,
            "latencyMs": _latency_summary(latencies),
        },
        "failureClassification": {
            "firstPass": dict(sorted(first_failures.items())),
            "final": dict(sorted(final_failures.items())),
        },
        "questions": question_rows,
    }


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def make_template(path: Path, questions: Sequence[Mapping[str, Any]]) -> None:
    if path.exists():
        raise EvidenceError("capture template destination already exists")
    records = []
    for question in questions:
        records.append({
            "schemaVersion": 1,
            "runId": "REPLACE_WITH_REAL_HARNESS_RUN_ID",
            "questionId": question["id"],
            "attempt": 1,
            "fixtureDate": "REPLACE_WITH_POSTGRES_CURRENT_DATE",
            "capturedAt": "REPLACE_WITH_ISO_8601_TIMESTAMP",
            "model": "REPLACE_WITH_REAL_MODEL_ID",
            "context": {"status": "success", "callIndex": 1},
            "query": {"callIndex": 2, "presentation": "REPLACE_WITH_DATA_QUERY_TOOL_RESULT_META"},
        })
    envelope = {
        "schemaVersion": 1,
        "instructions": "Replace every placeholder with one real Harness capture. Append attempt=2 only after a retryable SEMANTIC_ERROR; reuse context callIndex and set repairOfAttempt=1 plus repairReasonCode.",
        "records": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_template_v2(path: Path, config: CorpusConfig) -> None:
    """Write a secret-free v2 Harness capture envelope."""

    if path.exists() or config.data_sha256 is None:
        raise EvidenceError("capture template destination already exists or corpus is incomplete")
    records = []
    for question in config.questions:
        records.append({
            "schemaVersion": 1,
            "runId": "REPLACE_WITH_REAL_HARNESS_RUN_ID",
            "questionId": question["id"],
            "attempt": 1,
            "fixtureDate": "2024-09-01",
            "capturedAt": "REPLACE_WITH_ISO_8601_TIMESTAMP",
            "model": "REPLACE_WITH_REAL_MODEL_ID",
            "context": {"status": "success", "callIndex": 1},
            "query": {"callIndex": 2, "presentation": "REPLACE_WITH_DATA_QUERY_TOOL_RESULT_META"},
        })
    envelope = {
        "schemaVersion": 2,
        "dataSha256": config.data_sha256,
        "run": {
            "model": {
                "provider": "REPLACE_WITH_PROVIDER",
                "id": "REPLACE_WITH_REAL_MODEL_ID",
                "version": "REPLACE_WITH_MODEL_VERSION",
            },
            "parameters": {},
        },
        "instructions": (
            "Replace placeholders with one real Harness run. Add attempt=2 only after a "
            "retryable SEMANTIC_ERROR; authorization denials must never be retried."
        ),
        "records": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _synthetic_sql(models: Sequence[str], *, native: bool) -> str:
    names = [f"public.{name}" if native else name for name in models]
    sql = f"SELECT * FROM {names[0]}"
    for name in names[1:]:
        sql += f" JOIN {name} ON TRUE"
    return sql


def _synthetic_success(question: Mapping[str, Any], question_id: str, fixture: date, query_index: int) -> Mapping[str, Any]:
    oracle = _object(question["oracle"], "oracle")
    rows = [
        {key: _resolve_expected(value, fixture).removesuffix("*") if isinstance(_resolve_expected(value, fixture), str) else _resolve_expected(value, fixture) for key, value in row.items()}
        for row in oracle["rows"]
    ]
    return {
        "callIndex": query_index,
        "presentation": {
            "schemaVersion": 1,
            "queryId": f"self-{question_id}-{query_index}",
            "status": "success",
            "semanticSql": _synthetic_sql(oracle["requiredModels"], native=False),
            "nativeSql": _synthetic_sql(oracle["requiredModels"], native=True),
            "columns": [{"name": name, "type": "TEXT", "semanticRole": "dimension"} for name in oracle["expectedColumns"]],
            "previewRows": rows,
            "stats": {"returnedRows": len(rows), "durationMs": 1, "truncated": False},
        },
    }


def _synthetic_failure(question: Mapping[str, Any], question_id: str) -> Mapping[str, Any]:
    oracle = _object(question["oracle"], "oracle")
    return {
        "callIndex": 2,
        "presentation": {
            "schemaVersion": 1,
            "queryId": f"self-{question_id}-2",
            "status": "error",
            "semanticSql": _synthetic_sql(oracle["requiredModels"], native=False),
            "columns": [],
            "previewRows": [],
            "stats": {"returnedRows": 0, "durationMs": 1, "truncated": False},
            "error": {"code": "SEMANTIC_ERROR", "phase": "plan", "message": "safe self-test failure", "retryable": True},
        },
    }


def self_test(questions: Sequence[Mapping[str, Any]], corpus_raw: bytes) -> None:
    fixture = date(2026, 1, 20)
    records: list[Mapping[str, Any]] = []
    for index, question in enumerate(questions):
        question_id = str(question["id"])
        base: dict[str, Any] = {
            "schemaVersion": 1, "runId": "self-test", "questionId": question_id,
            "attempt": 1, "fixtureDate": fixture.isoformat(),
            "context": {"status": "success", "callIndex": 1},
        }
        base["query"] = _synthetic_success(question, question_id, fixture, 2) if index < 16 else _synthetic_failure(question, question_id)
        records.append(base)
        if 16 <= index < 18:
            repair = copy.deepcopy(base)
            repair.update({"attempt": 2, "repairOfAttempt": 1, "repairReasonCode": "SEMANTIC_ERROR"})
            repair["query"] = _synthetic_success(question, question_id, fixture, 3)
            records.append(repair)
    raw = json.dumps(records, ensure_ascii=False).encode()
    report = evaluate(questions, records, corpus_raw, raw)
    if report["status"] != "pass" or report["counts"] != {"firstPass": 16, "afterAtMostOneRepair": 18, "repairs": 2}:
        raise AssertionError("threshold self-test failed")
    secret_records = copy.deepcopy(records)
    secret_target = next(item for item in secret_records if item["questionId"] == questions[18]["id"] and item["attempt"] == 1)
    secret_target["query"]["presentation"]["error"]["message"] = "postgres://credential-sentinel"
    secret_report = evaluate(questions, secret_records, corpus_raw, b"secret-bearing-evidence")
    if "credential-sentinel" in json.dumps(secret_report, ensure_ascii=False):
        raise AssertionError("safe report leaked captured data")
    duplicate = list(records) + [copy.deepcopy(records[0])]
    try:
        evaluate(questions, duplicate, corpus_raw, b"duplicate")
    except EvidenceError:
        pass
    else:
        raise AssertionError("duplicate evidence was accepted")
    policy_retry = copy.deepcopy(records)
    target = next(item for item in policy_retry if item["questionId"] == questions[16]["id"] and item["attempt"] == 1)
    target["query"]["presentation"]["error"]["code"] = "POLICY_DENIED"
    target["query"]["presentation"]["error"]["retryable"] = False
    try:
        evaluate(questions, policy_retry, corpus_raw, b"policy-retry")
    except EvidenceError:
        pass
    else:
        raise AssertionError("policy retry was accepted")
    missing = [item for item in records if item["questionId"] != questions[-1]["id"]]
    try:
        evaluate(questions, missing, corpus_raw, b"missing")
    except EvidenceError:
        pass
    else:
        raise AssertionError("missing question was accepted")


def self_test_v2(config: CorpusConfig, corpus_raw: bytes) -> None:
    """Exercise v2 thresholds, denials, hashes, and safe reporting."""

    fixture = date(2024, 9, 1)
    model_id = "self-test-model"
    records: list[Mapping[str, Any]] = []
    for question in config.questions:
        question_id = str(question["id"])
        oracle = _object(question["oracle"], "oracle")
        if oracle.get("type") == "denial":
            presentation: Mapping[str, Any] = {
                "schemaVersion": 1,
                "queryId": f"self-{question_id}",
                "status": "error",
                "semanticSql": str(_object(question["canonical"], "canonical")["semanticSql"]),
                "columns": [],
                "previewRows": [],
                "stats": {"returnedRows": 0, "durationMs": 1, "truncated": False},
                "error": {"code": "POLICY_DENIED", "phase": "authorization", "message": "request denied", "retryable": False},
            }
        else:
            canonical = _object(question["canonical"], "canonical")
            rows = list(oracle.get("rows", []))
            native_sql = str(canonical["nativeSql"])
            native_sql = re.sub(r"\$\{ACTOR_(?:EMPLOYEE|MANAGER)_ID\}", "1", native_sql)
            native_sql = native_sql.replace("${ACTOR_REGION_CODE}", "'APAC'")
            presentation = {
                "schemaVersion": 1,
                "queryId": f"self-{question_id}",
                "status": "success",
                "semanticSql": canonical["semanticSql"],
                "nativeSql": native_sql,
                "columns": [{"name": name, "type": "TEXT", "semanticRole": "dimension"} for name in oracle["expectedColumns"]],
                "previewRows": rows,
                "stats": {"returnedRows": len(rows), "durationMs": 1, "truncated": False},
            }
        records.append({
            "schemaVersion": 1,
            "runId": "self-test-v2",
            "questionId": question_id,
            "attempt": 1,
            "fixtureDate": fixture.isoformat(),
            "model": model_id,
            "context": {"status": "success", "callIndex": 1},
            "query": {"callIndex": 2, "presentation": presentation},
        })
    raw = json.dumps(records, ensure_ascii=False).encode()
    run = {"model": {"provider": "self", "id": model_id, "version": "self-test-v1"}, "parameters": {"temperature": 0}}
    report = evaluate_v2(config, records, corpus_raw, raw, run, str(config.data_sha256))
    if report["status"] != "pass" or report["counts"]["authorization"] != config.authorization_target:
        failed = [item["id"] for item in report["questions"] if not item["firstPass"]]
        raise AssertionError(f"v2 threshold or authorization self-test failed: {report['failureClassification']} {failed}")
    if any(key in json.dumps(report) for key in ("previewRows", "monthly_salary", "request denied")):
        raise AssertionError("v2 safe report leaked captured content")
    current_presentations = copy.deepcopy(records)
    for record in current_presentations:
        presentation = record.get("query", {}).get("presentation")
        if not isinstance(presentation, dict):
            continue
        presentation["schemaVersion"] = 2
        presentation["stats"]["previewedRows"] = len(presentation["previewRows"])
        if presentation["status"] == "success":
            presentation["delivery"] = "inline"
    current_report = evaluate_v2(
        config, current_presentations, corpus_raw,
        json.dumps(current_presentations, ensure_ascii=False).encode(), run, str(config.data_sha256),
    )
    if current_report["status"] != "pass":
        raise AssertionError("current schemaVersion 2 query presentations were rejected")
    bad = copy.deepcopy(records)
    denial = next(item for item in bad if _object(config.questions[[q["id"] for q in config.questions].index(item["questionId"])]["oracle"], "oracle").get("type") == "denial")
    denial["query"]["presentation"]["status"] = "success"
    denial["query"]["presentation"].pop("error", None)
    bad_report = evaluate_v2(config, bad, corpus_raw, b"bad", run, str(config.data_sha256))
    if bad_report["status"] != "fail" or bad_report["counts"]["authorization"] >= config.authorization_target:
        raise AssertionError("v2 authorization bypass was accepted")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate AC02 against real normalized Harness evidence")
    parser.add_argument("--corpus", type=Path, default=CORPUS_DEFAULT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--evidence", type=Path, help="real JSON/JSONL Harness capture")
    mode.add_argument("--make-template", type=Path, help="write a capture template; does not evaluate AC02")
    mode.add_argument("--dry-run", action="store_true", help="validate corpus and prerequisites only")
    mode.add_argument("--self-test", action="store_true", help="test evaluator logic with synthetic data; does not verify AC02")
    parser.add_argument("--report", type=Path, help="safe JSON report path; required with --evidence")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config, corpus_raw = _load_corpus(args.corpus.resolve())
        questions = list(config.questions)
        if args.dry_run:
            print(
                "GOLDEN_EVALUATOR_DRY_RUN_OK: "
                f"schema={config.schema_version}, corpus={len(questions)}, "
                f"first-pass target={config.first_pass_target}, "
                f"after-one-repair target={config.after_repair_target}; not evaluated"
            )
            return 0
        if args.make_template:
            if config.schema_version == 1:
                make_template(args.make_template.resolve(), questions)
            else:
                make_template_v2(args.make_template.resolve(), config)
            print("GOLDEN_CAPTURE_TEMPLATE_WRITTEN: placeholders are not evidence; AC02 not evaluated")
            return 0
        if args.self_test:
            if config.schema_version == 1:
                self_test(questions, corpus_raw)
            else:
                self_test_v2(config, corpus_raw)
            print("GOLDEN_EVALUATOR_SELF_TEST_OK: evaluator logic only; AC02 not evaluated")
            return 0
        if args.report is None:
            raise EvidenceError("--report is required with --evidence")
        evidence_path = args.evidence.resolve()
        report_path = args.report.resolve()
        corpus_path = args.corpus.resolve()
        if report_path in {evidence_path, corpus_path}:
            raise EvidenceError("report path must differ from corpus and evidence paths")
        records, evidence_raw, run_metadata, data_sha256 = _load_evidence(evidence_path, config.schema_version)
        if config.schema_version == 1:
            report = evaluate(questions, records, corpus_raw, evidence_raw)
        else:
            if run_metadata is None or data_sha256 is None:
                raise EvidenceError("schemaVersion 2 evidence metadata is required")
            report = evaluate_v2(config, records, corpus_raw, evidence_raw, run_metadata, data_sha256)
        write_report(report_path, report)
        counts = report["counts"]
        total = len(questions)
        print(
            f"{report['claim']}: first-pass={counts['firstPass']}/{total}, "
            f"after-at-most-one-repair={counts['afterAtMostOneRepair']}/{total}"
        )
        return 0 if report["status"] == "pass" else 1
    except (EvidenceError, OSError) as exc:
        print(f"GOLDEN_EVALUATION_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
