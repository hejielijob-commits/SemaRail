"""Pure, versioned evaluation for the knowledge A/B experiment.

Inputs are JSON dictionaries; no database, model, corpus, or filesystem access.
Rows use oracle columns as canonical names; aliases map actual names to canonical
names. Answer requirements are exact public condition tags, never prose matches.
Free-text answers, including oracle.answerAssertions, require an independently
authored accepted manualReview; assertions are never matched as prose substrings.
Experiment objects use version 1. Normalized Core results may use version 1 or 2.
Only trusted oracle.numericColumns permit numeric strings to compare against
numbers using Decimal. Neither actual schema types nor numeric-looking text can
enable coercion. Missing oracle results produce unavailable (null) SQL judgments.
"""
from __future__ import annotations

import math
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

JSON = dict[str, Any]
DENIAL_CODES = {"POLICY_DENIED", "FORBIDDEN"}
GROUPS = ("baseline", "candidate")


def _version(value: JSON, *, core_result: bool = False) -> None:
    if value.get("schemaVersion", 1) not in ({1, 2} if core_result else {1}):
        raise ValueError("Unsupported schemaVersion")


def _equal(left: Any, right: Any, tolerance: float) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isfinite(left) and math.isfinite(right) and abs(left - right) <= tolerance
    return type(left) is type(right) and left == right


def _numeric_equal(left: Any, right: Any, tolerance: float) -> bool:
    """Only trusted oracle numericColumns enable lossless string coercion."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (str, int, float)) or not isinstance(right, (str, int, float)):
        return False
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return a.is_finite() and b.is_finite() and abs(a - b) <= Decimal(str(tolerance))
    except InvalidOperation:
        return False


def compare_rows(actual: Any, expected: Any, oracle: JSON) -> JSON:
    """Compare row multisets with explicit schema and absolute numeric tolerance.

    Accept lists of dict rows or Core result objects with previewRows/rows and
    optional columns. Empty result schemas must be explicit in result objects.
    Truncated and artifact previews fail closed. A separately materialized artifact
    must be represented as complete rows, not passed as a preview. Unordered
    tolerance matching preserves counts.
    """
    _version(oracle)
    reasons: list[str] = []
    columns = oracle.get("columns", [])
    aliases = oracle.get("aliases", {})
    extras = set(oracle.get("allowedExtraColumns", []))
    numeric_columns = set(oracle.get("numericColumns", []))
    if not numeric_columns <= set(columns):
        raise ValueError("numericColumns must name canonical oracle columns")
    tolerance = oracle.get("numericTolerance", 1e-6)
    if not isinstance(tolerance, (int, float)) or tolerance < 0 or not math.isfinite(tolerance):
        raise ValueError("numericTolerance must be finite and nonnegative")
    if len(columns) != len(set(columns)):
        raise ValueError("Duplicate oracle columns")

    def unpack(value: Any, side: str) -> list[JSON]:
        schema = None
        if isinstance(value, dict):
            _version(value, core_result=True)
            if value.get("delivery") == "artifact":
                reasons.append(f"{side}: artifact requires full materialized result")
            if value.get("error") or value.get("status") == "error":
                reasons.append(f"{side}: error result")
            for container in (value, value.get("stats", {}), value.get("delivery", {})):
                if isinstance(container, dict) and any(container.get(key) is True for key in ("truncated", "isTruncated", "previewTruncated")):
                    reasons.append(f"{side}: truncated result")
            schema = value.get("columns")
            rows = value.get("previewRows", value.get("rows"))
            stats = value.get("stats") or {}
            total = stats.get("rowCount", stats.get("returnedRows")) if isinstance(stats, dict) else None
            if isinstance(total, int) and isinstance(rows, list) and total > len(rows):
                reasons.append(f"{side}: incomplete preview")
        else:
            rows = value
        if not isinstance(rows, list):
            reasons.append(f"{side}: rows missing")
            return []
        if not rows:
            if schema is None:
                reasons.append(f"{side}: empty result requires explicit columns")
            elif isinstance(schema, list):
                schema_names = [c.get("name") if isinstance(c, dict) else c for c in schema]
                canonical = [aliases.get(c, c) if side == "actual" else c for c in schema_names if c not in extras]
                if len(canonical) != len(set(canonical)) or set(canonical) != set(columns):
                    reasons.append(f"{side}: schema mismatch")
            else:
                reasons.append(f"{side}: invalid columns")
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                reasons.append(f"{side}: expected object row")
                continue
            mapped = {}
            for name, value in row.items():
                if side == "actual" and name in extras:
                    continue
                canonical = aliases.get(name, name) if side == "actual" else name
                if canonical in mapped:
                    reasons.append(f"{side}: alias collision")
                mapped[canonical] = value
            if set(mapped) != set(columns):
                reasons.append(f"{side}: column mismatch")
            normalized.append(mapped)
        return normalized

    left, right = unpack(actual, "actual"), unpack(expected, "expected")
    if len(left) != len(right):
        reasons.append("row count mismatch")
    if reasons:
        return {"schemaVersion": 1, "correct": False, "reasons": sorted(set(reasons))}
    def matches(a: JSON, b: JSON) -> bool:
        return all((_numeric_equal if c in numeric_columns else _equal)(a[c], b[c], tolerance) for c in columns)
    if oracle.get("orderSensitive", False):
        correct = all(matches(a, b) for a, b in zip(left, right))
    else:
        # Augmenting paths avoid incorrect greedy matches at tolerance boundaries.
        edges = [[j for j, b in enumerate(right) if matches(a, b)] for a in left]
        owners: dict[int, int] = {}
        def assign(i: int, seen: set[int]) -> bool:
            for j in edges[i]:
                if j not in seen:
                    seen.add(j)
                    if j not in owners or assign(owners[j], seen):
                        owners[j] = i
                        return True
            return False
        correct = all(assign(i, set()) for i in range(len(left)))
    return {"schemaVersion": 1, "correct": correct, "reasons": [] if correct else ["row values mismatch"]}


def _denied(result: JSON) -> bool:
    error = result.get("error") or {}
    return isinstance(error, dict) and error.get("code") in DENIAL_CODES


def _query_events(record: JSON) -> list[JSON]:
    return [event for event in record.get("events", [])
            if isinstance(event.get("result"), dict)
            and (event.get("tool", "").split(".")[-1] in {"query", "execute_query", "run_query", "semarail_governed_query"}
                 or "previewRows" in event["result"])]


QUERY_TOOL_NAMES = {"semarail_plan_query", "semarail_prepare_query",
                    "semarail_governed_query", "semarail_get_query_evidence"}


def _query_attempts(record: JSON) -> list[JSON]:
    return [event for event in record.get("events", [])
            if event.get("tool", "").split(".")[-1] in QUERY_TOOL_NAMES]


def _sql_candidates(record: JSON) -> list[JSON]:
    candidates = []
    for event in record.get("events", []):
        arguments = event.get("arguments")
        if (event.get("tool", "").split(".")[-1] in {
                "semarail_plan_query", "semarail_prepare_query", "semarail_governed_query"}
                and isinstance(arguments, dict)
                and isinstance(arguments.get("semantic_sql"), str)
                and arguments["semantic_sql"].strip()):
            candidates.append(event)
    return candidates


def evaluate_run(question: JSON, record: JSON, oracle_result: JSON | None) -> JSON:
    """Evaluate a run without hiding missing evidence or prose-review debt."""
    for value in (question, record):
        _version(value)
    if oracle_result is not None:
        _version(oracle_result, core_result=True)
    oracle = question["oracle"]
    oracle_type = oracle["type"]
    denial = oracle_type == "denial"
    blocked = oracle_type == "blocked"
    oracle_available = blocked or oracle_result is not None
    events = record.get("events", [])
    queries = _query_events(record)
    query_attempts = _query_attempts(record)
    sql_candidates = _sql_candidates(record)
    final = record.get("final") or {}
    eligible = record.get("status") == "complete" and record.get("audit", {}).get("status") == "passed"
    reasons = [] if eligible else ["run incomplete or audit not passed"]
    if not oracle_available:
        reasons.append("oracle unavailable")

    def sql_correct(event: JSON) -> bool:
        result = event["result"]
        _version(result, core_result=True)
        if blocked or oracle_result is None:
            return False
        if denial:
            return _denied(result) and not result.get("previewRows", result.get("rows", []))
        return not result.get("error") and compare_rows(result, oracle_result, oracle)["correct"]

    first_candidate = sql_candidates[0] if sql_candidates else None
    first_candidate_sql = (first_candidate.get("arguments", {}).get("semantic_sql")
                           if first_candidate else None)
    first_executed = next((event for event in queries
                           if event.get("arguments", {}).get("semantic_sql") == first_candidate_sql), None)
    first = sql_correct(first_executed) if first_executed is not None else False
    last = sql_correct(queries[-1]) if queries else False
    permission_failure = denial and any(
        bool(event["result"].get("previewRows", event["result"].get("rows", [])))
        or (event["result"].get("stats") or {}).get("returnedRows", 0) > 0
        or (not event["result"].get("error") and event["result"].get("status") in {"success", "succeeded", "completed", "ok"})
        for event in queries)
    clarification_events = [(index, event) for index, event in enumerate(events)
                            if event.get("tool") == "user_clarification"]
    configured = question.get("clarification")
    first_candidate_index = next((index for index, event in enumerate(events)
                                  if event is first_candidate), len(events))
    if configured:
        clarification_correct = any(
            index < first_candidate_index and event.get("result", {}).get("matched") is True
            and event.get("arguments", {}).get("key") in configured.get("promptKeys", [])
            for index, event in clarification_events)
    else:
        clarification_correct = not clarification_events and not record.get("clarificationAsked", False)

    requirements = set(oracle.get("answerRequirements", []))
    conditions = set(final.get("conditions", []))
    requirements_ok = requirements <= conditions
    answer_ok = bool(final.get("answer")) and bool(final.get("scope"))
    safe_denial_without_sql = (denial and not query_attempts and not sql_candidates
        and bool(final.get("answer")) and bool(final.get("scope"))
        and not final.get("rows") and not permission_failure)
    if blocked:
        # The required outcome is a refusal before any query tool is called.
        deterministic = not query_attempts and requirements_ok and answer_ok and final.get("uncertain") is True
    elif denial:
        deterministic = (last or safe_denial_without_sql) and not final.get("rows") and not permission_failure
    elif oracle_result is not None:
        final_comparison = compare_rows(
            {"rows": final.get("rows"), "columns": final.get("columns")}, oracle_result, oracle)
        deterministic = last and final_comparison["correct"]
        reasons.extend(final_comparison["reasons"])
    else:
        deterministic = False
    if oracle_result is not None and not blocked and not last:
        reasons.append("final SQL result incorrect or absent")
    if permission_failure:
        reasons.append("permission failure")
    deterministic = deterministic and requirements_ok and clarification_correct and answer_ok
    if not requirements_ok:
        reasons.append("required public condition tags missing")
    if not clarification_correct:
        reasons.append("clarification behavior mismatch")

    review = record.get("manualReview") or {}
    reviewed = (review.get("status") == "accepted"
                and isinstance(review.get("answerCorrect"), bool)
                and bool(review.get("reason")))
    correct = (eligible and deterministic and review["answerCorrect"]
               if reviewed and oracle_available else None)
    if reviewed and not review["answerCorrect"]:
        reasons.append("independent answer review failed")
    executed = any(
        not event["result"].get("error")
        and ("previewRows" in event["result"] or "rows" in event["result"])
        for event in queries)
    formal_events = [event for event in events if event.get("tool", "").startswith("semarail_")]
    tool_duration = sum(event.get("durationMs", 0) for event in formal_events)
    omission = record.get("status") != "complete" or not answer_ok
    first_sql_expected = oracle_type == "rows"
    if not first_sql_expected:
        first_sql_status = "not_applicable"
    elif record.get("status") == "missing":
        first_sql_status = "missing"
    elif record.get("status") == "timeout":
        first_sql_status = "timeout"
    elif record.get("status") != "complete":
        first_sql_status = "unverified_incomplete"
    elif record.get("audit", {}).get("status") != "passed":
        first_sql_status = "unverified_audit_or_incomplete"
    elif not sql_candidates:
        first_sql_status = "missing"
    elif first_executed is None:
        first_sql_status = "unverified_not_executed"
    else:
        first_sql_status = "correct" if first else "incorrect"
    return {
        "schemaVersion": 1, "eligible": eligible, "status": record.get("status", "missing"),
        "oracleUnavailable": not oracle_available,
        "firstSqlExpected": first_sql_expected,
        "firstSqlStatus": first_sql_status,
        "firstSqlCorrect": (first if first_sql_expected and eligible and first_executed is not None
                            and oracle_result is not None else None),
        "repairedSqlCorrect": (eligible and last) if not blocked and oracle_result is not None else None,
        "finalAnswerCorrect": correct, "reviewPending": not reviewed,
        "silentError": bool(eligible and executed and correct is False and not final.get("uncertain", False)),
        "omission": omission,
        "blockedCorrect": (eligible and not query_attempts and deterministic) if blocked else None,
        "clarificationCorrect": eligible and clarification_correct,
        "permissionFailure": permission_failure, "reasons": reasons,
        "permissionDenialCorrect": (eligible and not permission_failure and not final.get("rows")
            and (last or (safe_denial_without_sql and reviewed and review["answerCorrect"])))
            if denial and oracle_result is not None else None,
        "toolCalls": len(formal_events), "durationMs": record.get("durationMs", tool_duration),
        "toolDurationMs": tool_duration,
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def summarize(questions: list[JSON], schedule: list[JSON], records: list[JSON],
              oracle_results: JSON, expected_model: str = "gpt-6-luna",
              expected_reasoning: str = "max") -> JSON:
    """Account for all scheduled jobs and apply predeclared promotion gates."""
    by_question = {q["id"]: q for q in questions}
    if len(by_question) != len(questions):
        raise ValueError("Duplicate question ID")
    ids = [item["id"] for item in schedule]
    record_ids = [item["id"] for item in records]
    if len(set(ids)) != len(ids) or len(set(record_ids)) != len(record_ids):
        raise ValueError("Duplicate run ID")
    if set(record_ids) - set(ids):
        raise ValueError("Unscheduled records")
    by_id = {item["id"]: item for item in records}
    outcomes: list[JSON] = []
    for item in schedule:
        record = by_id.get(item["id"], {**item, "status": "missing"})
        if any(record.get(key) != item[key] for key in ("questionId", "group", "round")):
            raise ValueError("Record does not match schedule")
        question = by_question[item["questionId"]]
        result = evaluate_run(question, record, oracle_results.get(item["questionId"]))
        audit = record.get("audit", {})
        observed_models = audit.get("observedModels") if isinstance(audit, dict) else None
        observed_reasoning = audit.get("observedReasoning") if isinstance(audit, dict) else None
        model_ok = observed_models == [expected_model]
        reasoning_ok = observed_reasoning == [expected_reasoning]
        outcomes.append({**item, "category": question.get("category", "unknown"),
                         "scenario": question.get("scenario", "unknown"),
                         "actor": question.get("actor", "unknown"),
                         "language": question.get("language", "unknown"),
                         "modelObserved": model_ok, "reasoningObserved": reasoning_ok,
                         **result})

    def metrics(items: list[JSON]) -> JSON:
        count = len(items)
        sql_items = [item for item in items if item["firstSqlExpected"]]
        correct = sum(item["finalAnswerCorrect"] is True for item in items)
        first_correct = sum(item["firstSqlCorrect"] is True for item in sql_items)
        final_sql_correct = sum(item["repairedSqlCorrect"] is True for item in sql_items)
        first_sql_states = Counter(item["firstSqlStatus"] for item in sql_items)
        return {
            "expected": count,
            "statuses": dict(Counter(item["status"] for item in items)),
            "finalCorrect": correct, "finalAccuracy": correct / count if count else 0,
            "firstSqlExpected": len(sql_items), "firstSqlApplicable": len(sql_items),
            "firstSqlCorrect": first_correct,
            "firstSqlAccuracy": first_correct / len(sql_items) if sql_items else 0,
            "firstSqlUnverified": sum(value for key, value in first_sql_states.items()
                                      if key.startswith("unverified")),
            "firstSqlMissing": first_sql_states["missing"],
            "firstSqlTimedOut": first_sql_states["timeout"],
            "firstSqlIncorrect": first_sql_states["incorrect"],
            "firstSqlNotApplicable": sum(item["firstSqlStatus"] == "not_applicable" for item in items),
            "repairedSqlCorrect": final_sql_correct,
            "repairedSqlAccuracy": final_sql_correct / len(sql_items) if sql_items else 0,
            "oracleUnavailable": sum(item["oracleUnavailable"] for item in items),
            "permissionDenialsCorrect": sum(item["permissionDenialCorrect"] is True for item in items),
            "blockedCorrect": sum(item["blockedCorrect"] is True for item in items),
            "silentErrors": sum(item["silentError"] for item in items),
            "omissions": sum(item["omission"] for item in items),
            "permissionFailures": sum(item["permissionFailure"] for item in items),
            "reviewPending": sum(item["reviewPending"] for item in items),
            "modelUnobservableOrDrifted": sum(not item["modelObserved"] for item in items),
            "reasoningUnobservableOrDrifted": sum(not item["reasoningObserved"] for item in items),
            "clarificationCorrect": sum(item["clarificationCorrect"] for item in items),
            "errors": dict(Counter(reason for item in items for reason in item["reasons"])),
            "toolCalls": sum(item["toolCalls"] for item in items),
            "durationMs": sum(item["durationMs"] for item in items),
            "toolDurationMs": sum(item["toolDurationMs"] for item in items),
        }

    groups = {group: metrics([item for item in outcomes if item["group"] == group])
              for group in GROUPS}
    rounds = {str(round_no): {
        group: metrics([item for item in outcomes
                        if item["group"] == group and item["round"] == round_no])
        for group in GROUPS} for round_no in (1, 2, 3)}
    paired = []
    determinism_failures = []
    for question in questions:
        question_runs = [item for item in outcomes if item["questionId"] == question["id"]]
        scores = {group: [next((item["finalAnswerCorrect"] for item in question_runs
                                if item["group"] == group and item["round"] == round_no), None)
                          for round_no in (1, 2, 3)] for group in GROUPS}
        candidate_records = [by_id.get(item["id"]) for item in schedule
                             if item["questionId"] == question["id"] and item["group"] == "candidate"]
        complete_candidate = len(candidate_records) == 3 and all(
            isinstance(record, dict) and record.get("status") == "complete"
            and record.get("audit", {}).get("status") == "passed" for record in candidate_records)
        final_values = [(record or {}).get("final", {}) for record in candidate_records]
        oracle = question["oracle"]
        if oracle["type"] == "rows":
            deterministic_rows = (len(final_values) == 3 and all(isinstance(value, dict) for value in final_values)
                and all(compare_rows(value, final_values[0], oracle)["correct"] for value in final_values[1:]))
        else:
            deterministic_rows = (len(final_values) == 3 and all(isinstance(value, dict) for value in final_values)
                and all(
                value.get("rows", []) == final_values[0].get("rows", [])
                and value.get("columns", []) == final_values[0].get("columns", [])
                for value in final_values[1:]))
        deterministic = complete_candidate and deterministic_rows
        if not deterministic:
            determinism_failures.append(question["id"])
        paired.append({"questionId": question["id"], **scores,
                       "correctDifference": sum(value is True for value in scores["candidate"])
                       - sum(value is True for value in scores["baseline"]),
                       "candidateDeterministic": deterministic})

    complete_design = (len(questions) == 30 and len(schedule) == 180
        and all(sum(item["questionId"] == question["id"] and item["group"] == group
                    and item["round"] == round_no for item in schedule) == 1
                for question in questions for group in GROUPS for round_no in (1, 2, 3)))
    evidence_complete = complete_design and all(
        item["eligible"] and not item["reviewPending"] and not item["oracleUnavailable"]
        and item["modelObserved"] and item["reasoningObserved"] for item in outcomes)
    denial_outcomes = [item for item in outcomes
                       if by_question[item["questionId"]]["oracle"]["type"] == "denial"]
    denials_clean = all(item["permissionDenialCorrect"] is True and not item["permissionFailure"]
                        for item in denial_outcomes)
    round_wins = sum(rounds[str(number)]["candidate"]["finalCorrect"]
                     > rounds[str(number)]["baseline"]["finalCorrect"] for number in (1, 2, 3))
    first_sql_gate = groups["candidate"]["firstSqlCorrect"] >= groups["baseline"]["firstSqlCorrect"]
    final_gate = groups["candidate"]["finalCorrect"] > groups["baseline"]["finalCorrect"]
    nonincrease_gate = (groups["candidate"]["silentErrors"] <= groups["baseline"]["silentErrors"]
                        and groups["candidate"]["omissions"] <= groups["baseline"]["omissions"])
    determinism_gate = not determinism_failures
    no_permission_leak = not any(item["permissionFailure"] for item in outcomes)
    promote = (evidence_complete and denials_clean and round_wins >= 2 and first_sql_gate
               and final_gate and nonincrease_gate and determinism_gate and no_permission_leak)
    scenarios = {scenario: {group: metrics([item for item in outcomes
        if item["scenario"] == scenario and item["group"] == group]) for group in GROUPS}
        for scenario in sorted({item["scenario"] for item in outcomes})}
    categories = {category: {group: metrics([item for item in outcomes
        if item["category"] == category and item["group"] == group]) for group in GROUPS}
        for category in sorted({item["category"] for item in outcomes})}
    languages = {language: {group: metrics([item for item in outcomes
        if item["language"] == language and item["group"] == group]) for group in GROUPS}
        for language in sorted({item["language"] for item in outcomes})}
    actors = {actor: {group: metrics([item for item in outcomes
        if item["actor"] == actor and item["group"] == group]) for group in GROUPS}
        for actor in sorted({item["actor"] for item in outcomes})}
    return {
        "schemaVersion": 1, "expected": len(schedule), "groups": groups, "rounds": rounds,
        "scenarios": scenarios, "categories": categories, "languages": languages,
        "actors": actors, "perQuestion": paired, "runs": outcomes,
        "promotionGates": {
            "completeReviewedEvidence": evidence_complete, "allRoleDenialsClean": denials_clean,
            "candidateWinsAtLeastTwoRounds": round_wins >= 2, "roundWins": round_wins,
            "firstSqlAtLeastBaseline": first_sql_gate, "finalAnswerAboveBaseline": final_gate,
            "silentErrorsAndOmissionsDoNotIncrease": nonincrease_gate,
            "deterministicAcrossThreeCandidateRounds": determinism_gate,
            "noPermissionLeaks": no_permission_leak,
            "determinismFailures": determinism_failures,
        },
        "recommendation": {"promote": promote,
            "reason": "all predeclared gates met" if promote else "incomplete evidence or one or more predeclared gates failed"},
    }
