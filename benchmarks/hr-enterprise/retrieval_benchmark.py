"""Offline retrieval benchmark utilities for the frozen HR enterprise corpus.

This module deliberately lives next to the benchmark rather than in SemaRail's
runtime packages.  It defines the ground-truth contract, evaluates recorded
retrieval results, and provides a small deterministic synthetic corpus for
capacity smoke tests.  It does not call a vector database or inspect employee
rows.

The result-file contract is intentionally provider neutral::

    {
      "schemaVersion": 1,
      "results": [{
        "questionId": "current-headcount",
        "candidates": [{"id": "model:employees", "kind": "model"}],
        "context": "...",
        "latencyMs": 4.2
      }]
    }

Candidate order is the rank supplied by the retrieval system.  Scores are
optional and are never used to reorder a result file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
CORPUS_ID = "semarail-hr-enterprise-v1"
DEFAULT_KS = (1, 3, 5, 10, 15)


class GroundTruthError(ValueError):
    """Raised when a retrieval artifact is malformed or incomplete."""


@dataclass(frozen=True)
class GroundTruthItem:
    """Independent retrieval oracle for one frozen question."""

    question_id: str
    language: str
    required_models: tuple[str, ...] = ()
    required_columns: tuple[str, ...] = ()
    required_relationships: tuple[str, ...] = ()
    required_rules: tuple[str, ...] = ()
    required_sql_examples: tuple[str, ...] = ()
    forbidden_models: tuple[str, ...] = ()
    forbidden_columns: tuple[str, ...] = ()

    def required_for(self, dimension: str) -> tuple[str, ...]:
        try:
            return getattr(self, _DIMENSION_FIELDS[dimension])
        except KeyError as exc:
            raise GroundTruthError(f"unknown retrieval dimension: {dimension}") from exc

    def forbidden(self) -> frozenset[str]:
        return frozenset((*self.forbidden_models, *self.forbidden_columns))

    def as_dict(self) -> dict[str, Any]:
        return {
            "questionId": self.question_id,
            "language": self.language,
            "requiredModels": list(self.required_models),
            "requiredColumns": list(self.required_columns),
            "requiredRelationships": list(self.required_relationships),
            "requiredRules": list(self.required_rules),
            "requiredSqlExamples": list(self.required_sql_examples),
            "forbiddenModels": list(self.forbidden_models),
            "forbiddenColumns": list(self.forbidden_columns),
        }


@dataclass(frozen=True)
class Candidate:
    """One ranked, client-visible retrieval candidate."""

    id: str
    kind: str = "unknown"
    score: float | None = None
    source: str | None = None
    visible: bool = True

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Candidate":
        identifier = payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise GroundTruthError("candidate.id must be a non-empty string")
        score = payload.get("score")
        if score is not None and not isinstance(score, (int, float)):
            raise GroundTruthError(f"candidate score must be numeric: {identifier}")
        kind = payload.get("kind", "unknown")
        if not isinstance(kind, str) or kind not in {
            "model", "column", "relationship", "cube", "metric", "dimension",
            "time_dimension", "rule", "sql_example", "view", "unknown",
        }:
            raise GroundTruthError(f"candidate kind is invalid: {identifier}")
        prefix_kind = {
            "model": "model", "column": "column", "relationship": "relationship",
            "cube": "cube", "metric": "metric", "dimension": "dimension",
            "time_dimension": "time_dimension", "rule": "rule",
            "sql_example": "sql_example", "view": "view",
        }.get(identifier.split(":", 1)[0])
        if kind != "unknown" and prefix_kind != kind:
            raise GroundTruthError(f"candidate kind does not match id: {identifier}")
        return cls(
            id=identifier,
            kind=kind,
            score=float(score) if score is not None else None,
            source=str(payload["source"]) if payload.get("source") is not None else None,
            visible=bool(payload.get("visible", True)),
        )


@dataclass(frozen=True)
class RetrievalResult:
    """Recorded retrieval output for one question."""

    question_id: str
    candidates: tuple[Candidate, ...] = ()
    context: str = ""
    latency_ms: float | None = None
    context_bytes: int | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RetrievalResult":
        identifier = payload.get("questionId", payload.get("question_id"))
        if not isinstance(identifier, str) or not identifier:
            raise GroundTruthError("retrieval result questionId must be a non-empty string")
        raw_candidates = payload.get("candidates", ())
        if not isinstance(raw_candidates, list):
            raise GroundTruthError(f"candidates must be a list for {identifier}")
        context = payload.get("context", "")
        if context is None:
            context = ""
        if not isinstance(context, str):
            raise GroundTruthError(f"context must be a string for {identifier}")
        context_bytes = payload.get("contextBytes", payload.get("context_bytes"))
        if context_bytes is not None:
            if not isinstance(context_bytes, int) or context_bytes < 0:
                raise GroundTruthError(f"contextBytes must be a non-negative integer for {identifier}")
        latency = payload.get("latencyMs", payload.get("latency_ms"))
        if latency is not None:
            if not isinstance(latency, (int, float)) or latency < 0:
                raise GroundTruthError(f"latencyMs must be a non-negative number for {identifier}")
            latency = float(latency)
        return cls(
            question_id=identifier,
            candidates=tuple(Candidate.from_dict(item) for item in raw_candidates),
            context=context,
            latency_ms=latency,
            context_bytes=context_bytes,
        )


_DIMENSION_FIELDS: dict[str, str] = {
    "models": "required_models",
    "columns": "required_columns",
    "relationships": "required_relationships",
    "rules": "required_rules",
    "sqlExamples": "required_sql_examples",
}

_DIMENSION_PREFIXES: dict[str, tuple[str, ...]] = {
    "models": ("model:",),
    "columns": ("column:",),
    "relationships": ("relationship:",),
    "rules": ("rule:",),
    "sqlExamples": ("sql_example:",),
}


def _as_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise GroundTruthError(f"{field} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise GroundTruthError(f"{field} contains duplicate identifiers")
    return tuple(value)


def _validate_identifier_prefix(identifier: str, prefix: str, field: str) -> None:
    if not identifier.startswith(prefix):
        raise GroundTruthError(f"{field} identifier must start with {prefix!r}: {identifier}")


def _validate_item(item: GroundTruthItem) -> None:
    if not item.question_id:
        raise GroundTruthError("questionId must be non-empty")
    if item.language not in {"zh-CN", "en"}:
        raise GroundTruthError(f"unsupported question language: {item.language}")
    for value in item.required_models:
        _validate_identifier_prefix(value, "model:", "requiredModels")
    for value in item.forbidden_models:
        _validate_identifier_prefix(value, "model:", "forbiddenModels")
    for value in item.required_columns:
        _validate_identifier_prefix(value, "column:", "requiredColumns")
    for value in item.forbidden_columns:
        _validate_identifier_prefix(value, "column:", "forbiddenColumns")
    for value in item.required_relationships:
        _validate_identifier_prefix(value, "relationship:", "requiredRelationships")
    for value in item.required_rules:
        _validate_identifier_prefix(value, "rule:", "requiredRules")
    for value in item.required_sql_examples:
        _validate_identifier_prefix(value, "sql_example:", "requiredSqlExamples")
    if set(item.required_models) & set(item.forbidden_models):
        raise GroundTruthError(f"model is both required and forbidden: {item.question_id}")
    if set(item.required_columns) & set(item.forbidden_columns):
        raise GroundTruthError(f"column is both required and forbidden: {item.question_id}")


def validate_ground_truth(
    truth: Mapping[str, GroundTruthItem], expected_question_ids: Iterable[str] | None = None
) -> None:
    """Validate coverage and safety invariants for an in-memory oracle."""

    if not truth:
        raise GroundTruthError("retrieval ground truth is empty")
    if len(truth) != len(set(truth)):
        raise GroundTruthError("retrieval ground truth has duplicate question IDs")
    for key, item in truth.items():
        if key != item.question_id:
            raise GroundTruthError(f"mapping key does not match questionId: {key}")
        _validate_item(item)
    if expected_question_ids is not None:
        expected = set(expected_question_ids)
        actual = set(truth)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing={missing}")
            if extra:
                parts.append(f"extra={extra}")
            raise GroundTruthError("ground-truth coverage mismatch: " + ", ".join(parts))


def load_ground_truth(path: str | Path) -> dict[str, GroundTruthItem]:
    """Load and validate a JSON retrieval ground-truth file."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GroundTruthError(f"cannot load ground truth {source}: {exc}") from exc
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise GroundTruthError(f"unsupported ground-truth schemaVersion: {payload.get('schemaVersion')!r}")
    questions = payload.get("questions")
    if not isinstance(questions, list):
        raise GroundTruthError("ground truth questions must be a list")
    parsed: dict[str, GroundTruthItem] = {}
    for raw in questions:
        if not isinstance(raw, Mapping):
            raise GroundTruthError("each ground-truth question must be an object")
        question_id = raw.get("questionId")
        language = raw.get("language")
        if not isinstance(question_id, str) or not isinstance(language, str):
            raise GroundTruthError("questionId and language are required strings")
        item = GroundTruthItem(
            question_id=question_id,
            language=language,
            required_models=_as_tuple(raw.get("requiredModels"), "requiredModels"),
            required_columns=_as_tuple(raw.get("requiredColumns"), "requiredColumns"),
            required_relationships=_as_tuple(raw.get("requiredRelationships"), "requiredRelationships"),
            required_rules=_as_tuple(raw.get("requiredRules"), "requiredRules"),
            required_sql_examples=_as_tuple(raw.get("requiredSqlExamples"), "requiredSqlExamples"),
            forbidden_models=_as_tuple(raw.get("forbiddenModels"), "forbiddenModels"),
            forbidden_columns=_as_tuple(raw.get("forbiddenColumns"), "forbiddenColumns"),
        )
        if question_id in parsed:
            raise GroundTruthError(f"duplicate ground-truth question: {question_id}")
        parsed[question_id] = item
    validate_ground_truth(parsed)
    return parsed


def load_retrieval_results(path: str | Path) -> list[RetrievalResult]:
    """Load a provider-neutral retrieval result envelope."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GroundTruthError(f"cannot load retrieval results {source}: {exc}") from exc
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise GroundTruthError(f"unsupported result schemaVersion: {payload.get('schemaVersion')!r}")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise GroundTruthError("retrieval results must be a list")
    results = [RetrievalResult.from_dict(item) for item in raw_results]
    identifiers = [item.question_id for item in results]
    if len(identifiers) != len(set(identifiers)):
        raise GroundTruthError("retrieval results contain duplicate question IDs")
    return results


def estimate_tokens(text: str) -> int:
    """Return a deterministic conservative token estimate (UTF-8 bytes / 4)."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return math.ceil(len(text.encode("utf-8")) / 4) if text else 0


def percentile(values: Sequence[float | int], percent: float) -> float | None:
    """Compute a linear-interpolated percentile without external dependencies."""

    if not values:
        return None
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _numeric_summary(values: Sequence[float | int]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None}
    numbers = [float(value) for value in values]
    return {
        "count": len(numbers),
        "min": min(numbers),
        "max": max(numbers),
        "mean": statistics.fmean(numbers),
        "p50": percentile(numbers, 50),
        "p95": percentile(numbers, 95),
    }


def summarize_payload(results: Sequence[RetrievalResult]) -> dict[str, Any]:
    """Summarize context bytes and token estimates for recorded responses."""

    context_bytes = [
        result.context_bytes
        if result.context_bytes is not None
        else len(result.context.encode("utf-8"))
        for result in results
    ]
    tokens = [estimate_tokens(result.context) for result in results]
    return {"bytes": _numeric_summary(context_bytes), "estimatedTokens": _numeric_summary(tokens)}


def _candidate_ids(result: RetrievalResult) -> tuple[str, ...]:
    # The first occurrence is the only valid rank for a duplicated identifier.
    seen: set[str] = set()
    identifiers: list[str] = []
    for candidate in result.candidates:
        if candidate.id not in seen:
            seen.add(candidate.id)
            identifiers.append(candidate.id)
    return tuple(identifiers)


def _recall(required: frozenset[str], ranked: Sequence[str], k: int) -> float | None:
    if not required:
        return None
    return len(required & set(ranked[:k])) / len(required)


def _mrr(required: frozenset[str], ranked: Sequence[str]) -> float | None:
    if not required:
        return None
    for rank, identifier in enumerate(ranked, start=1):
        if identifier in required:
            return 1.0 / rank
    return 0.0


def _ndcg(required: frozenset[str], ranked: Sequence[str], k: int) -> float | None:
    if not required:
        return None
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, identifier in enumerate(ranked[:k], start=1)
        if identifier in required
    )
    ideal_hits = min(k, len(required))
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / ideal if ideal else 0.0


def _mean_defined(values: Iterable[float | None]) -> float | None:
    defined = [float(value) for value in values if value is not None]
    return statistics.fmean(defined) if defined else None


def _metric_report(
    pairs: Sequence[tuple[GroundTruthItem, RetrievalResult]],
    required_selector: str,
    ks: Sequence[int],
    *,
    section_rank: bool = True,
) -> dict[str, Any]:
    per_question: list[dict[str, Any]] = []
    for truth, result in pairs:
        required = frozenset(truth.required_for(required_selector))
        # Context v2 has independent per-section quotas. Dimension metrics
        # therefore measure rank within that section (Model@5, Rule@5, etc.),
        # while the separate ``overall`` report keeps the global mixed rank.
        prefixes = _DIMENSION_PREFIXES.get(required_selector, ()) if section_rank else ()
        ranked = tuple(
            identifier
            for identifier in _candidate_ids(result)
            if not prefixes or identifier.startswith(prefixes)
        )
        entry: dict[str, Any] = {"questionId": truth.question_id, "required": len(required)}
        for k in ks:
            entry[f"recall@{k}"] = _recall(required, ranked, k)
            entry[f"ndcg@{k}"] = _ndcg(required, ranked, k)
        entry["mrr"] = _mrr(required, ranked)
        per_question.append(entry)
    report: dict[str, Any] = {
        "questions": len(pairs),
        "evaluatedQuestions": sum(item["required"] > 0 for item in per_question),
        "perQuestion": per_question,
        "mrr": _mean_defined(item["mrr"] for item in per_question),
    }
    for k in ks:
        report[f"recall@{k}"] = _mean_defined(item[f"recall@{k}"] for item in per_question)
        report[f"ndcg@{k}"] = _mean_defined(item[f"ndcg@{k}"] for item in per_question)
    return report


def evaluate(
    truth: Mapping[str, GroundTruthItem],
    results: Sequence[RetrievalResult],
    *,
    ks: Sequence[int] = DEFAULT_KS,
    candidate_catalog: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Evaluate ranked retrievals against ground truth.

    Recall is the mean per-question fraction of required items in the first K
    candidates. MRR uses the first required item, and NDCG uses binary
    relevance for every required item. Questions with no required item in a
    dimension are excluded from that dimension's mean. Overall metrics use the
    union of all required dimensions for each question.
    """

    validate_ground_truth(truth)
    if not ks or any(not isinstance(k, int) or k <= 0 for k in ks):
        raise ValueError("ks must contain positive integers")
    by_id: dict[str, RetrievalResult] = {}
    for result in results:
        if result.question_id in by_id:
            raise GroundTruthError(f"duplicate retrieval result: {result.question_id}")
        if result.question_id not in truth:
            raise GroundTruthError(f"retrieval result has unknown question: {result.question_id}")
        if candidate_catalog is not None:
            unknown = sorted({item.id for item in result.candidates} - candidate_catalog)
            if unknown:
                raise GroundTruthError(
                    f"retrieval result contains IDs outside the frozen semantic corpus: {unknown[:5]}"
                )
        by_id[result.question_id] = result
    missing = sorted(set(truth) - set(by_id))
    if missing:
        raise GroundTruthError(f"retrieval results missing questions: {missing}")
    pairs = [(item, by_id[item.question_id]) for item in truth.values()]

    union_truth: dict[str, GroundTruthItem] = {}
    for item in truth.values():
        required = tuple(
            dict.fromkeys(
                item.required_models
                + item.required_columns
                + item.required_relationships
                + item.required_rules
                + item.required_sql_examples
            )
        )
        union_truth[item.question_id] = GroundTruthItem(
            question_id=item.question_id,
            language=item.language,
            required_models=required,
        )
    overall = _metric_report(
        [(union_truth[item.question_id], result) for item, result in pairs],
        "models",
        ks,
        section_rank=False,
    )
    dimensions = {
        name: _metric_report(pairs, name, ks) for name in _DIMENSION_FIELDS
    }

    languages: dict[str, dict[str, Any]] = {}
    for language in sorted({item.language for item in truth.values()}):
        language_pairs = [(item, result) for item, result in pairs if item.language == language]
        language_union = {
            item.question_id: union_truth[item.question_id] for item, _ in language_pairs
        }
        language_overall = _metric_report(
            [(language_union[item.question_id], result) for item, result in language_pairs],
            "models",
            ks,
            section_rank=False,
        )
        leak_count = sum(
            len(set(_candidate_ids(result)) & item.forbidden())
            for item, result in language_pairs
        )
        language_overall["permissionLeakageCount"] = leak_count
        language_overall["dimensions"] = {
            name: _metric_report(language_pairs, name, ks) for name in _DIMENSION_FIELDS
        }
        languages[language] = language_overall

    leakage_by_question: dict[str, int] = {}
    leakage_ids_by_question: dict[str, list[str]] = {}
    for item, result in pairs:
        leaked = sorted(set(_candidate_ids(result)) & item.forbidden())
        leakage_by_question[item.question_id] = len(leaked)
        if leaked:
            leakage_ids_by_question[item.question_id] = leaked
    total_leakage = sum(leakage_by_question.values())

    latency_values = [result.latency_ms for _, result in pairs if result.latency_ms is not None]
    payload = summarize_payload([result for _, result in pairs])
    return {
        "schemaVersion": SCHEMA_VERSION,
        "corpusId": CORPUS_ID,
        "questions": len(pairs),
        "ks": list(ks),
        "overall": overall,
        **dimensions,
        "byLanguage": languages,
        "context": payload,
        "latencyMs": _numeric_summary(latency_values),
        "permission": {
            "leakageCount": total_leakage,
            "leakageByQuestion": leakage_by_question,
            "leakedCandidateIdsByQuestion": leakage_ids_by_question,
        },
    }


@dataclass(frozen=True)
class SyntheticDocument:
    """Small in-memory document used by deterministic scale smoke tests."""

    id: str
    kind: str
    text: str
    project_revision: str = "synthetic-v1"


def generate_synthetic_documents(model_count: int) -> tuple[SyntheticDocument, ...]:
    """Generate four documents per model without writing a large artifact."""

    if not isinstance(model_count, int) or model_count <= 0:
        raise ValueError("model_count must be a positive integer")
    documents: list[SyntheticDocument] = []
    for index in range(model_count):
        model = f"synthetic_{index:04d}"
        documents.append(
            SyntheticDocument(
                id=f"model:{model}",
                kind="model",
                text=f"{model} workforce model / 人员模型 with employee team size and region",
            )
        )
        documents.extend(
            (
                SyntheticDocument(
                    id=f"column:{model}.department_code",
                    kind="column",
                    text=f"{model} department code / 部门编码",
                ),
                SyntheticDocument(
                    id=f"column:{model}.region_code",
                    kind="column",
                    text=f"{model} region code / 区域编码",
                ),
                SyntheticDocument(
                    id=f"column:{model}.team_size",
                    kind="column",
                    text=f"{model} team size / 团队规模",
                ),
            )
        )
    return tuple(documents)


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in re.findall(r"[a-z0-9_]+", text.lower()))


def synthetic_retrieve(
    documents: Sequence[SyntheticDocument], query: str, *, limit: int = 10
) -> tuple[SyntheticDocument, ...]:
    """Deterministically rank synthetic documents by exact ID and token overlap."""

    if limit <= 0:
        return ()
    query_tokens = set(_tokens(query))
    target_match = re.search(r"synthetic_\d+", query.lower())

    def rank(document: SyntheticDocument) -> tuple[int, int, str]:
        doc_tokens = set(_tokens(document.text + " " + document.id))
        exact_model = bool(
            target_match and document.id == f"model:{target_match.group(0)}"
        )
        return (
            1 if exact_model else 0,
            len(query_tokens & doc_tokens),
            document.id,
        )

    return tuple(sorted(documents, key=rank, reverse=True)[:limit])


# The six MDL model names and their stable relationship IDs are intentionally
# copied into this benchmark module.  The generator remains usable without a
# YAML dependency and the checked-in MDL remains the source of truth for the
# runtime itself.
_MODEL_NAMES = (
    "employees",
    "departments",
    "regions",
    "compensation_history",
    "performance_reviews",
    "attendance_monthly",
)
_MODEL_COLUMNS: dict[str, tuple[str, ...]] = {
    "employees": (
        "employee_id", "department_code", "region_code", "manager_id", "gender", "age",
        "job_title", "hire_date", "years_at_company", "education_level", "work_hours_per_week",
        "projects_handled", "remote_work_frequency", "team_size", "training_hours", "promotions",
        "resigned",
    ),
    "departments": ("department_code", "department_name", "region_code", "cost_center"),
    "regions": ("region_code", "region_name_en", "region_name_zh"),
    "compensation_history": (
        "employee_id", "effective_date", "manager_id", "department_code", "region_code", "monthly_salary",
    ),
    "performance_reviews": (
        "employee_id", "review_date", "manager_id", "department_code", "region_code",
        "performance_score", "satisfaction_score",
    ),
    "attendance_monthly": (
        "employee_id", "attendance_month", "manager_id", "department_code", "region_code",
        "overtime_hours", "sick_days", "overtime_hours_rolling_12m", "sick_days_rolling_12m",
    ),
}
_RELATIONSHIP_BY_PAIR: dict[frozenset[str], str] = {
    frozenset(("departments", "regions")): "departments_region",
    frozenset(("employees", "departments")): "employees_department",
    frozenset(("employees", "regions")): "employees_region",
    frozenset(("compensation_history", "employees")): "compensation_employee",
    frozenset(("compensation_history", "departments")): "compensation_department",
    frozenset(("compensation_history", "regions")): "compensation_region",
    frozenset(("performance_reviews", "employees")): "performance_employee",
    frozenset(("performance_reviews", "departments")): "performance_department",
    frozenset(("performance_reviews", "regions")): "performance_region",
    frozenset(("attendance_monthly", "employees")): "attendance_employee",
    frozenset(("attendance_monthly", "departments")): "attendance_department",
    frozenset(("attendance_monthly", "regions")): "attendance_region",
}

_SQL_EXAMPLE_BY_ID: dict[str, tuple[str, ...]] = {
    "current-headcount": ("current-headcount-by-department.md",),
    "headcount-department": ("current-headcount-by-department.md",),
    "active-by-remote": ("current-headcount-by-department.md",),
    "current-salary-region": ("current-average-salary.md",),
    "salary-trend": ("current-average-salary.md",),
    "comp-salary-trend": ("current-average-salary.md",),
    "performance-trend": ("quarterly-performance-trend.md",),
    "satisfaction-trend": ("quarterly-performance-trend.md",),
    "salary-performance-region": ("current-salary-performance-by-region.md",),
}

# Some policy-scoped questions do not mention the scope key in their canonical
# SQL because the policy engine injects it.  Retrieval still needs the key and
# its scope rule in context, so these are deliberately curated additions rather
# than inferred from SQL text.
_QUESTION_COLUMN_AUGMENTS: dict[str, tuple[str, ...]] = {
    "employee-salary": ("column:compensation_history.employee_id",),
    "manager-report-count": ("column:employees.manager_id",),
    "hrbp-active-count": ("column:employees.region_code",),
    "comp-admin-average": ("column:compensation_history.region_code",),
    "manager-performance": ("column:performance_reviews.manager_id",),
    "hrbp-performance-department": ("column:performance_reviews.region_code",),
    "comp-salary-trend": ("column:compensation_history.region_code",),
    "employee-attendance": ("column:attendance_monthly.employee_id",),
}
_QUESTION_RULE_AUGMENTS: dict[str, tuple[str, ...]] = {
    "employee-profile": ("rule:hr-metrics#security-semantics-安全语义-02",),
    "employee-salary": ("rule:hr-metrics#security-semantics-安全语义-02",),
    "manager-report-count": ("rule:hr-metrics#security-semantics-安全语义-04",),
    "hrbp-active-count": ("rule:hr-metrics#security-semantics-安全语义-02",),
    "comp-admin-average": ("rule:hr-metrics#security-semantics-安全语义-02",),
    "manager-performance": ("rule:hr-metrics#security-semantics-安全语义-04",),
    "hrbp-performance-department": ("rule:hr-metrics#security-semantics-安全语义-02",),
    "comp-salary-trend": ("rule:hr-metrics#security-semantics-安全语义-02",),
    "employee-attendance": ("rule:hr-metrics#security-semantics-安全语义-02",),
}


def frozen_semantic_document_ids() -> frozenset[str]:
    """Return the exact SemanticDocument ID catalog for the frozen HR project."""

    identifiers = {f"model:{model}" for model in _MODEL_NAMES}
    identifiers.update(
        f"column:{model}.{column}"
        for model, columns in _MODEL_COLUMNS.items()
        for column in columns
    )
    relationship_fields = {
        "employees": ("department", "region"),
        "departments": ("region",),
        "compensation_history": ("employee", "department", "region"),
        "performance_reviews": ("employee", "department", "region"),
        "attendance_monthly": ("employee", "department", "region"),
    }
    identifiers.update(
        f"column:{model}.{field}"
        for model, fields in relationship_fields.items()
        for field in fields
    )
    identifiers.update(f"relationship:{name}" for name in _RELATIONSHIP_BY_PAIR.values())
    for heading, count in (
        ("hr-metric-definitions-人力指标口径", 6),
        ("query-shape-查询结果形状", 5),
        ("cross-model-analysis-跨模型分析", 4),
        ("security-semantics-安全语义", 4),
    ):
        identifiers.update(
            f"rule:hr-metrics#{heading}-{index:02d}"
            for index in range(1, count + 1)
        )
    identifiers.update(
        f"sql_example:knowledge/sql/{name.removesuffix('.md')}"
        for values in _SQL_EXAMPLE_BY_ID.values()
        for name in values
    )
    return frozenset(identifiers)


def _models_from_sql(sql: str) -> tuple[str, ...]:
    names = re.findall(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", sql, flags=re.IGNORECASE)
    return tuple(dict.fromkeys(name for name in names if name in _MODEL_NAMES))


def _aliases_from_sql(sql: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for match in re.finditer(
        r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)(?:\s+(?:AS\s+)?([a-z_][a-z0-9_]*))?",
        sql,
        flags=re.IGNORECASE,
    ):
        model, alias = match.groups()
        if model in _MODEL_NAMES:
            aliases[model] = model
            if alias and alias.upper() not in {"ON", "WHERE", "GROUP", "ORDER", "LIMIT"}:
                aliases[alias] = model
    return aliases


def _columns_from_sql(sql: str, models: Sequence[str]) -> tuple[str, ...]:
    aliases = _aliases_from_sql(sql)
    found: list[str] = []
    # Qualified references are unambiguous and cover all benchmark joins.
    for alias, model in aliases.items():
        for column in _MODEL_COLUMNS[model]:
            if re.search(rf"\b{re.escape(alias)}\.{re.escape(column)}\b", sql, flags=re.IGNORECASE):
                found.append(f"column:{model}.{column}")
    # Unqualified references are assigned to every participating model that
    # owns the name.  This is deliberately conservative: missing a required
    # join key would make a recall score look better than the actual contract.
    for model in models:
        for column in _MODEL_COLUMNS[model]:
            if re.search(rf"(?<![.\w]){re.escape(column)}\b", sql, flags=re.IGNORECASE):
                found.append(f"column:{model}.{column}")
    return tuple(dict.fromkeys(found))


def _relationships_from_models(models: Sequence[str]) -> tuple[str, ...]:
    relationships: list[str] = []
    for left_index, left in enumerate(models):
        for right in models[left_index + 1 :]:
            relationship = _RELATIONSHIP_BY_PAIR.get(frozenset((left, right)))
            if relationship:
                relationships.append(f"relationship:{relationship}")
                continue
            # Cross-fact joins in this corpus are made at employee grain.  MDL
            # exposes the two employee edges rather than a direct fact-to-fact
            # edge, so both edges are part of the retrieval contract.
            left_edge = _RELATIONSHIP_BY_PAIR.get(frozenset((left, "employees")))
            right_edge = _RELATIONSHIP_BY_PAIR.get(frozenset((right, "employees")))
            if left_edge and right_edge:
                relationships.extend(
                    (f"relationship:{left_edge}", f"relationship:{right_edge}")
                )
    return tuple(dict.fromkeys(relationships))


def _rules_from_question(question: Mapping[str, Any], sql: str) -> tuple[str, ...]:
    prompt = str(question.get("question", ""))
    haystack = f"{prompt} {sql}".lower()
    rules: list[str] = []

    def add(rule: str, condition: bool) -> None:
        if condition:
            rules.append(f"rule:{rule}")

    add("headcount", "count(distinct employee_id)" in haystack or any(word in prompt for word in ("员工数", "员工数量", "headcount", "active employees")))
    add("current_employee_scope", "resigned = false" in haystack or "在职" in prompt or "active" in prompt.lower())
    add("attrition_rate", "attrition" in haystack or "离职率" in prompt)
    add("current_salary", "monthly_salary" in haystack and "2024-09-01" in haystack)
    add("current_performance", "performance_score" in haystack and "2024-09-30" in haystack)
    add("current_satisfaction", "satisfaction_score" in haystack and "2024-09-30" in haystack)
    add("annual_overtime", "overtime_hours_rolling_12m" in haystack or "年度加班" in prompt)
    add("annual_sick_days", "sick_days_rolling_12m" in haystack or "年度病假" in prompt)
    add(
        "snapshot_preservation",
        any(column in haystack for column in ("effective_date", "review_date", "attendance_month"))
        and any(marker in haystack for marker in ("trend", "history", "趋势", "变化")),
    )
    add("employee_grain_join", " join " in f" {sql.lower()} " and "employee_id" in haystack)
    add("snapshot_join", " join " in f" {sql.lower()} " and any(date in haystack for date in ("2024-09-01", "2024-09-30")))
    add("training_bands", "training_band" in haystack or "training_hours < 20" in haystack or "培训时数" in prompt)
    add("top_n_tie_break", "limit" in haystack and ("order by 2 desc, 1" in haystack or "相同" in prompt or "ties" in prompt.lower()))
    add("organization_keys", any(key in haystack for key in ("region_code", "department_code", "区域编码", "部门编码")))
    add("direct_reports", "manager_id" in haystack or "直属" in prompt or "direct report" in prompt.lower())
    add("distribution", "group by" in haystack and not any(rule in rules for rule in ("rule:headcount", "rule:organization_keys", "rule:top_n_tie_break")))
    if question.get("category") == "authorization":
        add("authorization_fail_closed", True)
        add("sensitive_access", True)
    document_by_concept = {
        "headcount": "rule:hr-metrics#hr-metric-definitions-人力指标口径-01",
        "current_employee_scope": "rule:hr-metrics#hr-metric-definitions-人力指标口径-01",
        "attrition_rate": "rule:hr-metrics#hr-metric-definitions-人力指标口径-02",
        "current_salary": "rule:hr-metrics#hr-metric-definitions-人力指标口径-03",
        "current_performance": "rule:hr-metrics#hr-metric-definitions-人力指标口径-04",
        "current_satisfaction": "rule:hr-metrics#hr-metric-definitions-人力指标口径-04",
        "annual_overtime": "rule:hr-metrics#hr-metric-definitions-人力指标口径-05",
        "annual_sick_days": "rule:hr-metrics#hr-metric-definitions-人力指标口径-05",
        "organization_keys": "rule:hr-metrics#hr-metric-definitions-人力指标口径-06",
        "distribution": "rule:hr-metrics#query-shape-查询结果形状-02",
        "snapshot_preservation": "rule:hr-metrics#query-shape-查询结果形状-03",
        "top_n_tie_break": "rule:hr-metrics#query-shape-查询结果形状-04",
        "employee_grain_join": "rule:hr-metrics#cross-model-analysis-跨模型分析-01",
        "snapshot_join": "rule:hr-metrics#cross-model-analysis-跨模型分析-02",
        "training_bands": "rule:hr-metrics#cross-model-analysis-跨模型分析-04",
        "sensitive_access": "rule:hr-metrics#security-semantics-安全语义-01",
        "self_scope": "rule:hr-metrics#security-semantics-安全语义-02",
        "regional_scope": "rule:hr-metrics#security-semantics-安全语义-02",
        "authorization_fail_closed": "rule:hr-metrics#security-semantics-安全语义-03",
        "direct_reports": "rule:hr-metrics#security-semantics-安全语义-04",
    }
    return tuple(
        dict.fromkeys(
            document_by_concept[rule.removeprefix("rule:")]
            for rule in rules
            if rule.removeprefix("rule:") in document_by_concept
        )
    )


def generate_ground_truth(golden_path: str | Path) -> dict[str, Any]:
    """Generate the independent retrieval oracle from frozen question semantics."""

    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    questions: list[dict[str, Any]] = []
    for question in golden.get("questions", []):
        sql = question.get("canonical", {}).get("semanticSql", "")
        models = _models_from_sql(sql)
        for model in question.get("oracle", {}).get("requiredModels", []) or []:
            if model in _MODEL_NAMES and model not in models:
                models = (*models, model)
        model_ids = tuple(f"model:{model}" for model in models)
        columns = tuple(
            dict.fromkeys(
                _columns_from_sql(sql, models)
                + _QUESTION_COLUMN_AUGMENTS.get(question["id"], ())
            )
        )
        relationships = tuple(
            _relationships_from_models(models)
        )
        rules = tuple(
            dict.fromkeys(
                _rules_from_question(question, sql)
                + _QUESTION_RULE_AUGMENTS.get(question["id"], ())
            )
        )
        examples = tuple(
            f"sql_example:knowledge/sql/{name.removesuffix('.md')}"
            for name in _SQL_EXAMPLE_BY_ID.get(question["id"], ())
        )
        forbidden_models: tuple[str, ...] = ()
        forbidden_columns: tuple[str, ...] = ()
        authorization_case = question.get("category") == "authorization"
        if authorization_case:
            # Denied requests must not require metric/join knowledge bound to
            # the forbidden objects. Only policy semantics are safe and useful
            # retrieval targets for these cases.
            rules = tuple(
                rule for rule in rules if "#security-semantics-" in rule
            )
            policy = _actor_retrieval_policy(str(question.get("actor")))
            allowed_tables = {
                key.rsplit(".", 1)[-1]
                for key in policy.get("tables", {})
                if isinstance(key, str)
            }
            forbidden_models = tuple(
                model_id for model_id in model_ids
                if model_id.removeprefix("model:") not in allowed_tables
            )
            forbidden_columns = tuple(
                column for column in columns
                if column.removeprefix("column:").split(".", 1)[0] not in allowed_tables
                or column.rsplit(".", 1)[-1].lower()
                in {
                    denied.lower()
                    for table in policy.get("tables", {}).values()
                    if isinstance(table, Mapping)
                    for denied in table.get("deniedColumns", [])
                    if isinstance(denied, str)
                }
            )
        item = GroundTruthItem(
            question_id=question["id"],
            language=question["language"],
            required_models=() if authorization_case else model_ids,
            required_columns=() if authorization_case else columns,
            required_relationships=() if authorization_case else relationships,
            required_rules=rules,
            required_sql_examples=() if authorization_case else examples,
            forbidden_models=forbidden_models,
            forbidden_columns=forbidden_columns,
        )
        # A denied query still needs its security rule, while a successful
        # query must have at least one schema item for a meaningful score.
        _validate_item(item)
        questions.append(item.as_dict())
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "corpusId": golden.get("corpusId", CORPUS_ID),
        "source": "golden-questions.json (read-only)",
        "metricContract": {
            "recall": "mean per-question fraction of required IDs in top K",
            "mrr": "mean reciprocal rank of the first required ID",
            "ndcg": "binary relevance NDCG over required IDs",
            "estimatedTokens": "ceil(UTF-8 context bytes / 4)",
            "permissionLeakage": "count of forbidden IDs present in client-visible candidates",
        },
        "questions": questions,
    }
    return result


def write_ground_truth(golden_path: str | Path, output_path: str | Path) -> None:
    """Generate and atomically write a retrieval ground-truth JSON artifact."""

    payload = generate_ground_truth(golden_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


def _actor_retrieval_policy(actor: str) -> dict[str, Any]:
    if actor == "hr_director":
        return {"schemaVersion": 1, "defaultEffect": "allow", "tables": {}}
    allowed: dict[str, tuple[str, ...]] = {
        "employee": ("regions", "departments", "employees", "compensation_history", "attendance_monthly"),
        "department_manager": ("regions", "departments", "employees", "performance_reviews", "attendance_monthly"),
        "hrbp": ("regions", "departments", "employees", "performance_reviews", "attendance_monthly"),
        "compensation_admin": ("regions", "departments", "employees", "compensation_history"),
    }
    denied_columns = {
        "employee": {"employees": ["gender", "age"]},
        "department_manager": {"employees": ["gender", "age"]},
    }
    if actor not in allowed:
        raise GroundTruthError(f"unknown benchmark actor: {actor}")
    return {
        "schemaVersion": 1,
        "defaultEffect": "deny",
        "tables": {
            f"hr.{model}": {
                "deniedColumns": denied_columns.get(actor, {}).get(model, []),
            }
            for model in allowed[actor]
        },
    }


def capture_local_retrieval(
    project_path: str | Path,
    golden_path: str | Path,
    output_path: str | Path,
    *,
    limit: int = 50,
    mode: str = "lexical",
    embedding_model: str | None = None,
    rule_metadata_path: str | Path | None = None,
    locales_path: str | Path | None = None,
) -> dict[str, Any]:
    """Capture one real retrieval configuration for the frozen HR corpus."""

    import sys

    repo = Path(__file__).resolve().parents[2]
    sidecar_source = repo / "python" / "sidecar"
    if str(sidecar_source) not in sys.path:
        sys.path.insert(0, str(sidecar_source))
    from sidecar.semantic_index import build_semantic_documents
    from sidecar.semantic_policy import semantic_document_visible
    from sidecar.semantic_retrieval import HybridSemanticRetriever, SentenceTransformerEmbedder
    from sidecar.wren_adapter import _semantic_question_type
    from wren.context import build_json

    project = Path(project_path).resolve()
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    manifest = build_json(project)
    revision = "benchmark:" + hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    rule_metadata: Mapping[str, Any] | None = None
    if rule_metadata_path is not None:
        loaded = json.loads(Path(rule_metadata_path).read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise GroundTruthError("rule metadata overlay must be an object")
        nested = loaded.get("ruleMetadata", loaded)
        if not isinstance(nested, Mapping):
            raise GroundTruthError("rule metadata overlay must contain ruleMetadata")
        rule_metadata = nested
    locales: Mapping[str, Any] | None = None
    if locales_path is not None:
        loaded_locales = json.loads(Path(locales_path).read_text(encoding="utf-8"))
        if not isinstance(loaded_locales, Mapping):
            raise GroundTruthError("locale overlay must be an object")
        locales = loaded_locales
    documents = build_semantic_documents(
        manifest,
        project,
        project_revision=revision,
        rule_metadata=rule_metadata,
        locales=locales,
    )
    channel_sets = {
        "full": (),
        "lexical": ("exact", "lexical"),
        "vector": ("vector",),
        "hybrid": ("exact", "lexical", "vector", "rule_binding"),
        "hybrid-graph": ("exact", "lexical", "vector", "graph", "rule_binding"),
        "hybrid-graph-rerank": ("exact", "lexical", "vector", "graph", "rule_binding"),
        "hybrid-adaptive": ("exact", "lexical", "vector", "rule_binding"),
    }
    if mode not in channel_sets:
        raise GroundTruthError(f"unsupported retrieval mode: {mode}")
    embedder = None
    if "vector" in channel_sets[mode]:
        embedder = SentenceTransformerEmbedder(model_name=embedding_model) if embedding_model else SentenceTransformerEmbedder()
    index = HybridSemanticRetriever(embedder=embedder)
    index.build(documents, revision=revision)
    index.activate(revision)
    section_quotas = {
        "model": 15,
        "column": 15,
        "relationship": 8,
        "cube": 8,
        "metric": 8,
        "dimension": 8,
        "time_dimension": 8,
        "rule": 8,
        "sql_example": 3,
        "view": 3,
    }
    results: list[dict[str, Any]] = []
    for question in golden.get("questions", []):
        policy = _actor_retrieval_policy(str(question.get("actor")))
        started = time.perf_counter()
        if mode == "full":
            ranked = [
                (document, "fallback", 1.0)
                for document in documents
                if semantic_document_visible(document, policy)
            ]
        else:
            selected_channels = channel_sets[mode]
            if mode == "hybrid-adaptive" and _semantic_question_type(
                str(question.get("question", ""))
            ) == "crossModel":
                selected_channels = ("exact", "lexical", "vector", "graph", "rule_binding")
            response = index.search(
                str(question.get("question", "")),
                revision=revision,
                limit=limit,
                quotas=section_quotas,
                visibility_filter=lambda document, selected=policy: semantic_document_visible(document, selected),
                relationship_depth=2,
                channels=selected_channels,
                restricted=policy.get("defaultEffect") != "allow",
                rerank=mode == "hybrid-graph-rerank",
            )
            ranked = [(hit.document, hit.match_type, hit.score) for hit in response]
        elapsed_ms = (time.perf_counter() - started) * 1_000
        selected = [document.to_dict() for document, _, _ in ranked]
        candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for document, source, score in ranked:
            if document.id not in seen_ids:
                seen_ids.add(document.id)
                candidates.append({
                    "id": document.id,
                    "kind": document.kind,
                    "source": source,
                    "score": score,
                })
            # Context assembly materializes the owning model for selected
            # columns/rules/examples. Count that client-visible object at the
            # point it first becomes available, not merely the internal hit.
            for model in ((document.model,) + tuple(document.referencedModels)):
                model_id = f"model:{model}" if isinstance(model, str) and model else ""
                if model_id and model_id not in seen_ids:
                    seen_ids.add(model_id)
                    candidates.append({
                        "id": model_id,
                        "kind": "model",
                        "source": "graph",
                        "score": score,
                    })
        results.append({
            "questionId": question["id"],
            "candidates": candidates,
            "context": json.dumps(selected, ensure_ascii=False, separators=(",", ":")),
            "latencyMs": round(elapsed_ms, 3),
        })
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "corpusId": golden.get("corpusId", CORPUS_ID),
        "backend": mode,
        "embedding": index.embedder_status.as_dict(),
        "projectRevision": revision,
        "results": results,
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return payload


def evaluate_embedding_languages(
    project_path: str | Path,
    evaluation_path: str | Path,
    output_path: str | Path,
    *,
    embedding_model: str | None = None,
    rule_metadata_path: str | Path | None = None,
    locales_path: str | Path | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Evaluate one real embedding model on fixed English/Chinese/mixed queries."""

    import sys

    if not isinstance(limit, int) or limit <= 0:
        raise GroundTruthError("embedding evaluation limit must be positive")
    evaluation = json.loads(Path(evaluation_path).read_text(encoding="utf-8"))
    if not isinstance(evaluation, Mapping) or evaluation.get("schemaVersion") != 1:
        raise GroundTruthError("embedding evaluation has an unsupported schema")
    questions = evaluation.get("questions")
    if not isinstance(questions, list) or not questions:
        raise GroundTruthError("embedding evaluation questions must be a non-empty array")
    parsed: list[tuple[str, str, str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for raw in questions:
        if not isinstance(raw, Mapping):
            raise GroundTruthError("embedding evaluation question must be an object")
        identifier = raw.get("id")
        language = raw.get("language")
        question = raw.get("question")
        required = raw.get("requiredIds")
        if (
            not isinstance(identifier, str) or not identifier.strip()
            or identifier in seen
            or language not in {"en", "zh-CN", "mixed"}
            or not isinstance(question, str) or not question.strip()
            or not isinstance(required, list) or not required
            or any(not isinstance(item, str) or not item.strip() for item in required)
        ):
            raise GroundTruthError("embedding evaluation question is invalid")
        seen.add(identifier)
        parsed.append((identifier, language, question, tuple(required)))
    if {item[1] for item in parsed} != {"en", "zh-CN", "mixed"}:
        raise GroundTruthError("embedding evaluation must contain English, Chinese, and mixed queries")

    repo = Path(__file__).resolve().parents[2]
    sidecar_source = repo / "python" / "sidecar"
    if str(sidecar_source) not in sys.path:
        sys.path.insert(0, str(sidecar_source))
    from sidecar.semantic_index import build_semantic_documents
    from sidecar.semantic_retrieval import HybridSemanticRetriever, SentenceTransformerEmbedder
    from wren.context import build_json

    project = Path(project_path).resolve()
    manifest = build_json(project)
    revision = "embedding-eval:" + hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    rule_metadata = None
    if rule_metadata_path is not None:
        loaded = json.loads(Path(rule_metadata_path).read_text(encoding="utf-8"))
        rule_metadata = loaded.get("ruleMetadata", loaded) if isinstance(loaded, Mapping) else None
        if not isinstance(rule_metadata, Mapping):
            raise GroundTruthError("rule metadata overlay must be an object")
    locales = None
    if locales_path is not None:
        locales = json.loads(Path(locales_path).read_text(encoding="utf-8"))
        if not isinstance(locales, Mapping):
            raise GroundTruthError("locale overlay must be an object")
    documents = build_semantic_documents(
        manifest,
        project,
        project_revision=revision,
        rule_metadata=rule_metadata,
        locales=locales,
    )
    catalog = {document.id for document in documents}
    unknown = sorted({item for _, _, _, required in parsed for item in required} - catalog)
    if unknown:
        raise GroundTruthError(f"embedding evaluation references unknown IDs: {', '.join(unknown)}")
    embedder = (
        SentenceTransformerEmbedder(model_name=embedding_model)
        if embedding_model
        else SentenceTransformerEmbedder()
    )
    index = HybridSemanticRetriever(embedder=embedder)
    index.build(documents, revision=revision)
    index.activate(revision)
    rows: list[dict[str, Any]] = []
    for identifier, language, question, required in parsed:
        started = time.perf_counter()
        response = index.search(
            question,
            revision=revision,
            limit=limit,
            channels=("vector",),
        )
        ranked_ids = [hit.document.id for hit in response]
        ranks = [ranked_ids.index(item) + 1 for item in required if item in ranked_ids]
        rows.append({
            "id": identifier,
            "language": language,
            "requiredIds": list(required),
            "rankedIds": ranked_ids,
            "recallAtK": len(ranks) / len(required),
            "reciprocalRank": 1.0 / min(ranks) if ranks else 0.0,
            "latencyMs": round((time.perf_counter() - started) * 1_000, 3),
        })

    def summarize(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return {
            "questions": len(group),
            "recallAtK": statistics.fmean(float(item["recallAtK"]) for item in group),
            "mrr": statistics.fmean(float(item["reciprocalRank"]) for item in group),
            "p95LatencyMs": percentile([float(item["latencyMs"]) for item in group], 95),
        }

    report = {
        "schemaVersion": 1,
        "corpusId": evaluation.get("corpusId"),
        "topK": limit,
        "embedding": index.embedder_status.as_dict(),
        "overall": summarize(rows),
        "byLanguage": {
            language: summarize([item for item in rows if item["language"] == language])
            for language in ("en", "zh-CN", "mixed")
        },
        "results": rows,
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return report


def _load_and_validate_coverage(golden_path: Path, truth_path: Path) -> dict[str, GroundTruthItem]:
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    expected = [item["id"] for item in golden.get("questions", [])]
    truth = load_ground_truth(truth_path)
    validate_ground_truth(truth, expected)
    return truth


def _json_default(value: Any) -> Any:
    if isinstance(value, (GroundTruthItem, Candidate, RetrievalResult, SyntheticDocument)):
        return value.__dict__
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate retrieval-ground-truth.json")
    generate.add_argument("--golden", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)

    validate = subparsers.add_parser("validate", help="validate ground-truth coverage")
    validate.add_argument("--golden", type=Path, required=True)
    validate.add_argument("--ground-truth", type=Path, required=True)

    evaluate_parser = subparsers.add_parser("evaluate", help="evaluate retrieval result envelope")
    evaluate_parser.add_argument("--ground-truth", type=Path, required=True)
    evaluate_parser.add_argument("--results", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path)
    evaluate_parser.add_argument("--k", type=int, action="append", dest="ks")

    synthetic = subparsers.add_parser("synthetic", help="run in-memory scale smoke benchmark")
    synthetic.add_argument("--models", type=int, nargs="+", default=[100, 500, 1000])

    capture = subparsers.add_parser("capture-local", help="capture the real local lexical fallback")
    capture.add_argument("--project", type=Path, required=True)
    capture.add_argument("--golden", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--limit", type=int, default=50)
    capture.add_argument(
        "--mode",
        choices=(
            "full", "lexical", "vector", "hybrid", "hybrid-graph",
            "hybrid-graph-rerank", "hybrid-adaptive",
        ),
        default="lexical",
    )
    capture.add_argument("--embedding-model")
    capture.add_argument("--rule-metadata", type=Path)
    capture.add_argument("--locales", type=Path)

    embedding_eval = subparsers.add_parser(
        "embedding-eval", help="evaluate a real embedding model by language"
    )
    embedding_eval.add_argument("--project", type=Path, required=True)
    embedding_eval.add_argument("--evaluation", type=Path, required=True)
    embedding_eval.add_argument("--output", type=Path, required=True)
    embedding_eval.add_argument("--embedding-model")
    embedding_eval.add_argument("--rule-metadata", type=Path)
    embedding_eval.add_argument("--locales", type=Path)
    embedding_eval.add_argument("--limit", type=int, default=5)

    args = parser.parse_args()
    if args.command == "generate":
        write_ground_truth(args.golden, args.output)
        return 0
    if args.command == "validate":
        truth = _load_and_validate_coverage(args.golden, args.ground_truth)
        print(json.dumps({"valid": True, "questions": len(truth)}, ensure_ascii=False))
        return 0
    if args.command == "evaluate":
        truth = load_ground_truth(args.ground_truth)
        results = load_retrieval_results(args.results)
        report = evaluate(
            truth,
            results,
            ks=tuple(args.ks or DEFAULT_KS),
            candidate_catalog=frozen_semantic_document_ids(),
        )
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    if args.command == "synthetic":
        measurements = []
        for count in args.models:
            started = time.perf_counter()
            documents = generate_synthetic_documents(count)
            build_ms = (time.perf_counter() - started) * 1000
            query = f"synthetic_{count - 1:04d} team size"
            started = time.perf_counter()
            top = synthetic_retrieve(documents, query, limit=5)
            search_ms = (time.perf_counter() - started) * 1000
            measurements.append({
                "models": count,
                "documents": len(documents),
                "buildMs": round(build_ms, 3),
                "searchMs": round(search_ms, 3),
                "topId": top[0].id if top else None,
            })
        print(json.dumps({"schemaVersion": SCHEMA_VERSION, "measurements": measurements}, indent=2))
        return 0
    if args.command == "capture-local":
        capture_local_retrieval(
            args.project,
            args.golden,
            args.output,
            limit=args.limit,
            mode=args.mode,
            embedding_model=args.embedding_model,
            rule_metadata_path=args.rule_metadata,
            locales_path=args.locales,
        )
        return 0
    if args.command == "embedding-eval":
        evaluate_embedding_languages(
            args.project,
            args.evaluation,
            args.output,
            embedding_model=args.embedding_model,
            rule_metadata_path=args.rule_metadata,
            locales_path=args.locales,
            limit=args.limit,
        )
        return 0
    raise AssertionError(f"unsupported command {args.command}")


if __name__ == "__main__":
    raise SystemExit(_cli())
