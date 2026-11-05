"""Deterministic evaluation; observations never stand in for answer correctness."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import json
from typing import Any

VERSION = "evaluation-v1"


def compare_results(expected: Any, actual: Any, *, tolerance: float = 0, ordered: bool = False) -> str:
    """Compare complete row multisets, preserving duplicate multiplicity."""
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return "inconclusive"
    if actual.get("complete") is not True or actual.get("truncated") is not False:
        return "inconclusive"
    if ordered and actual.get("ordered") is not True:
        return "inconclusive"
    if expected.get("columns") != actual.get("columns"):
        return "failed"
    left, right = expected.get("rows"), actual.get("rows")
    if not isinstance(left, list) or not isinstance(right, list):
        return "inconclusive"
    columns = expected.get("columns")
    if not isinstance(columns, list) or not columns or len(set(columns)) != len(columns) or any(not isinstance(x, str) for x in columns):
        return "inconclusive"
    def scalar(x: Any) -> bool:
        return x is None or type(x) in (str, bool, int) or (type(x) is float and math.isfinite(x))
    if any(not isinstance(row, list) or len(row) != len(columns) or any(not scalar(x) for x in row) for row in left + right):
        return "inconclusive"
    if len(left) != len(right):
        return "failed"
    def equal(a: Any, b: Any) -> bool:
        if type(a) in (int, float) and type(b) in (int, float):
            try:
                return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal(str(tolerance))
            except InvalidOperation:
                return False
        return type(a) is type(b) and a == b
    def row_equal(a: Any, b: Any) -> bool:
        return isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if ordered:
        return "passed" if all(row_equal(a, b) for a, b in zip(left, right)) else "failed"
    # Bipartite matching avoids a greedy tolerance match stealing another row's only match.
    matched: dict[int, int] = {}
    def match(i: int, seen: set[int]) -> bool:
        for j, row in enumerate(right):
            if j not in seen and row_equal(left[i], row):
                seen.add(j)
                if j not in matched or match(matched[j], seen):
                    matched[j] = i
                    return True
        return False
    return "passed" if all(match(i, set()) for i in range(len(left))) else "failed"


def score(case: dict[str, Any], evidence: dict[str, Any], status: str) -> dict[str, Any]:
    """Produce independent dimensions from trusted runner observations."""
    behavior = case.get("expectedBehavior", "answer")
    result = {"result": "inconclusive", "firstSql": "inconclusive", "permission": "inconclusive", "clarification": "inconclusive", "businessConditions": "inconclusive", "finalAnswer": "pending"}
    if status != "completed" or case.get("_environmentDrift") is True:
        return {"version": VERSION, "status": "inconclusive", "dimensions": result}
    assertions = case.get("assertions") or {}
    expected = case.get("expectedResult") or evidence.get("expectedResult")
    if case.get("expectedResult") is None and isinstance(expected, dict) and (expected.get("complete") is not True or expected.get("truncated") is not False):
        # A reference query's artifact preview cannot establish a complete oracle.
        expected = None
    for dimension, key in (("result", "finalResult"), ("firstSql", "firstResult")):
        result[dimension] = compare_results(expected, evidence.get(key), tolerance=assertions.get("tolerance", 0), ordered=assertions.get("ordered", False))
    final = evidence.get("finalResult")
    if "rowCount" in assertions or assertions.get("cells"):
        try:
            from .regression_assertions import evaluate
        except ImportError:
            from regression_assertions import evaluate
        for dimension, key in (("result", "finalResult"), ("firstSql", "firstResult")):
            observed = evidence.get(key)
            checked = "inconclusive"
            if isinstance(observed, dict) and observed.get("complete") is True and observed.get("truncated") is False and (not assertions.get("cells") or observed.get("ordered") is True):
                checked, _ = evaluate({"status": "success", **{k: v for k, v in assertions.items() if k in {"rowCount", "cells"}}}, {"ok": True, "result": {"status": "success", "columns": observed.get("columns"), "rows": observed.get("rows"), "stats": {"truncated": False, "returnedRows": len(observed.get("rows", []))}}})
            result[dimension] = checked if expected is None else "failed" if "failed" in {checked, result[dimension]} else "passed" if checked == result[dimension] == "passed" else "inconclusive"
    denial = evidence.get("denial")
    if behavior == "deny":
        def disclosed(value: Any) -> bool:
            if isinstance(value, dict):
                return any(k in {"rows", "previewRows", "data"} and isinstance(v, list) and bool(v) or disclosed(v) for k, v in value.items())
            if isinstance(value, list):
                return any(disclosed(x) for x in value)
            if isinstance(value, str) and len(value) <= 2_000_000 and value.lstrip().startswith(("{", "[")):
                try:
                    return disclosed(json.loads(value))
                except (ValueError, TypeError):
                    pass
            return False
        no_rows = not disclosed({"first": evidence.get("firstResult"), "final": final, "tools": evidence.get("toolCalls", [])})
        result["permission"] = "passed" if isinstance(denial, dict) and denial.get("code") in {"POLICY_DENIED", "FORBIDDEN"} and no_rows else "failed"
    clarification = evidence.get("clarification")
    if isinstance(clarification, dict) and isinstance(clarification.get("confirmedIds"), list):
        required = set((case.get("clarificationReplies") or {}).keys())
        if required:
            result["clarification"] = "passed" if required <= set(clarification["confirmedIds"]) else "failed"
    checks = evidence.get("businessConditions")
    if isinstance(checks, list) and checks:
        result["businessConditions"] = "failed" if any(isinstance(c, dict) and c.get("status") in {"failed", "conflict"} for c in checks) else "passed" if all(isinstance(c, dict) and c.get("status") == "passed" for c in checks) else "inconclusive"
    if isinstance(evidence.get("firstError"), dict) and isinstance(evidence["firstError"].get("code"), str):
        result["firstSql"] = "failed"
    if behavior in {"answer", "clarify"} and isinstance(evidence.get("finalError"), dict) and isinstance(evidence["finalError"].get("code"), str):
        result["result"] = "failed"
    primary = "permission" if behavior == "deny" else "clarification" if behavior == "clarify" else "result"
    if behavior == "unsupported":
        unsupported = evidence.get("unsupported")
        observed_unsupported = isinstance(unsupported, dict) and (unsupported.get("code") in {"UNSUPPORTED_QUERY", "NOT_SUPPORTED", "UNSUPPORTED_OPERATION", "UNSUPPORTED_DATASOURCE"} or unsupported.get("reasonCode") in {"UNSUPPORTED_SQL_SHAPE", "UNSUPPORTED_BUSINESS_SHAPE"})
        result["result"] = "passed" if observed_unsupported and not final else "inconclusive"
    overall = result[primary]
    if behavior == "clarify":
        overall = "failed" if "failed" in {result["result"], result["clarification"]} else "passed" if result["result"] == result["clarification"] == "passed" else "inconclusive"
    if result["businessConditions"] == "failed":
        overall = "failed"
    return {"version": VERSION, "status": overall, "dimensions": result}
