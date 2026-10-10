"""Rule-based, evidence-limited analysis for persisted agent traces.

This module accepts only the already scope-checked public trace shape assembled
by :mod:`diagnostics`. It never reads raw query bodies or Core error messages.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Mapping


TRACE_DIAGNOSIS_RULES_VERSION = "trace-diagnosis-v1"
_CATEGORIES = frozenset(
    {"authentication", "policy", "preparation", "datasource", "sqlValidation", "runtime"}
)
_PHASE_CATEGORY = {
    "authentication": "authentication",
    "auth": "authentication",
    "authn": "authentication",
    "policy": "policy",
    "authorization": "policy",
    "permission": "policy",
    "preparation": "preparation",
    "query_preparation": "preparation",
    "planning": "preparation",
    "datasource": "datasource",
    "connection": "datasource",
    "sqlvalidation": "sqlValidation",
    "sql_validation": "sqlValidation",
    "validation": "sqlValidation",
    "runtime": "runtime",
}
_SUGGESTED_ACTIONS = {
    "authentication": "Check the Core authentication configuration and retry with an authorized identity.",
    "policy": "Review the project policy and the caller's granted scopes.",
    "preparation": "Inspect semantic preparation inputs and the persisted preparation evidence.",
    "datasource": "Check datasource availability and the configured connection path.",
    "sqlValidation": "Review the SQL validation result and adjust the generated query before retrying.",
    "runtime": "Inspect the verified Core execution and runtime phase evidence.",
}


def _category(*values: Any) -> str:
    # Specific, verified failure codes take precedence over a broad runtime
    # stage (for example a connection failure raised inside query.run).
    codes = {
        "UNAUTHORIZED": "authentication", "AUTHENTICATION_EXPIRED": "authentication",
        "ACCOUNT_DISABLED": "authentication",
        "POLICY_DENIED": "policy", "PROJECT_PERMISSION_REQUIRED": "policy",
        "DATASOURCE_PERMISSION_REQUIRED": "policy", "TOOL_PERMISSION_REQUIRED": "policy",
        "TABLE_PERMISSION_REQUIRED": "policy", "COLUMN_PERMISSION_REQUIRED": "policy",
        "EXPLICIT_DENIAL": "policy", "ROW_ATTRIBUTE_MISSING": "policy",
        "CLARIFICATION_REQUIRED": "preparation", "SEMANTIC_PARSE_FAILED": "preparation",
        "CONNECTION_FAILED": "datasource", "UNSUPPORTED_DATASOURCE": "datasource",
        "DATABASE_PERMISSION_REQUIRED": "datasource",
        "SQL_VALIDATION_FAILED": "sqlValidation", "SQL_SAFETY_RESTRICTION": "sqlValidation",
        "QUERY_TIMEOUT": "runtime", "INTERNAL_FAILURE": "runtime",
    }
    for value in values:
        if isinstance(value, str) and value.upper() in codes:
            return codes[value.upper()]
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip().replace("-", "_").replace(" ", "_").lower()
        if normalized in _PHASE_CATEGORY:
            return _PHASE_CATEGORY[normalized]
        if any(part in normalized for part in ("auth", "credential")):
            return "authentication"
        if any(part in normalized for part in ("policy", "permission", "forbidden", "authoriz")):
            return "policy"
        if any(part in normalized for part in ("sql_validation", "sqlvalidation", "unsafe_sql")):
            return "sqlValidation"
        if any(part in normalized for part in ("datasource", "connection", "warehouse", "driver")):
            return "datasource"
        if any(part in normalized for part in ("prepar", "semantic", "planning")):
            return "preparation"
    return "runtime"


def _finding(
    *,
    number: int,
    category: str,
    code: str,
    severity: str,
    phase: str | None,
    evidence_ids: list[str],
    rationale: str,
    suggested_action: str,
) -> dict[str, Any]:
    return {
        "id": f"finding-{number}",
        "category": category if category in _CATEGORIES else "runtime",
        "code": code,
        "severity": severity,
        "phase": phase,
        "evidenceIds": list(dict.fromkeys(evidence_ids)),
        "rationale": rationale,
        "suggestedAction": suggested_action,
    }


def diagnose_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    """Derive safe findings from persisted agent events and verified Core data."""

    events = [event for event in trace.get("events", []) if isinstance(event, Mapping)]
    cores = [core for core in trace.get("coreDiagnostics", []) if isinstance(core, Mapping)]
    findings: list[dict[str, Any]] = []

    turn_starts = [event for event in events if event.get("type") == "turn_started"]
    turn_ends = [
        event for event in events
        if event.get("type") in {"turn_completed", "turn_interrupted"}
    ]
    if not turn_starts:
        findings.append(_finding(
            number=len(findings) + 1, category="runtime", code="MISSING_TURN_START",
            severity="info", phase=None,
            evidence_ids=[str(event.get("eventId")) for event in events if event.get("eventId")],
            rationale="No persisted turn start event is available, so the beginning of this trace cannot be inspected.",
            suggested_action="Check that the collector records turn_started events for this session.",
        ))
    if not turn_ends:
        findings.append(_finding(
            number=len(findings) + 1, category="runtime", code="MISSING_TURN_END",
            severity="info", phase=None,
            evidence_ids=[str(event.get("eventId")) for event in events if event.get("eventId")],
            rationale="No persisted terminal turn event is available, so the final turn state cannot be confirmed.",
            suggested_action="Check that the collector flushes turn_completed or turn_interrupted events.",
        ))

    core_ids = {str(core.get("traceId")) for core in cores if core.get("traceId")}
    for event in events:
        if event.get("type") != "tool_completed":
            continue
        linked = event.get("coreTraceId")
        # Most Codex tools do not call Core. A missing association is evidence
        # of a gap only when an event explicitly references a Core request.
        if not isinstance(linked, str) or linked in core_ids:
            continue
        findings.append(_finding(
            number=len(findings) + 1, category="runtime", code="MISSING_CORE_EVIDENCE",
            severity="info", phase=None,
            evidence_ids=[str(event["eventId"]) ] if event.get("eventId") else [],
            rationale="This tool event references a Core request whose diagnostic is not currently verified or retained.",
            suggested_action="Confirm that the tool completion is linked to a retained Core trace when Core execution occurred.",
        ))

    for kind, identifier, start_type, end_type in (
        ("tool", "toolUseId", "tool_started", "tool_completed"),
        ("agent", "agentId", "subagent_started", "subagent_completed"),
    ):
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for event in events:
            if event.get("type") in {start_type, end_type} and isinstance(event.get(identifier), str):
                grouped[event[identifier]].append(event)
        for records in grouped.values():
            types = {event.get("type") for event in records}
            for expected, suffix in ((start_type, "START"), (end_type, "END")):
                if expected in types:
                    continue
                findings.append(_finding(
                    number=len(findings) + 1, category="runtime",
                    code=f"MISSING_{kind.upper()}_{suffix}", severity="info", phase=None,
                    evidence_ids=[str(event["eventId"]) for event in records if event.get("eventId")],
                    rationale=f"No persisted {kind} {suffix.lower()} event is available; the observation interval is incomplete.",
                    suggested_action="Check collector delivery and refresh after late events arrive; this gap does not establish failure.",
                ))

    for core in cores:
        core_id = str(core.get("traceId", ""))
        evidence = [core_id] if core_id else []
        evidence.extend(item for item in core.get("issueIds", []) if isinstance(item, str))
        raw_phase = core.get("phase") or core.get("stage")
        phase = raw_phase if isinstance(raw_phase, str) else None
        code = core.get("code") or core.get("reasonCode")
        safe_code = code if isinstance(code, str) else "CORE_FAILURE"
        if core.get("status") == "failure":
            category = _category(phase, core.get("stage"), core.get("reasonCode"), safe_code)
            findings.append(_finding(
                number=len(findings) + 1, category=category, code=safe_code,
                severity="error", phase=phase, evidence_ids=evidence,
                rationale=f"Verified Core evidence records a failure in the {category} category.",
                suggested_action=_SUGGESTED_ACTIONS[category],
            ))
        phases = core.get("phaseSpans", [])
        if not isinstance(phases, list):
            continue
        for span in phases:
            if not isinstance(span, Mapping):
                continue
            elapsed = span.get("durationMs")
            phase_name = span.get("name")
            if (
                isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
                or elapsed <= 5_000 or not isinstance(phase_name, str)
            ):
                continue
            category = _category(phase_name)
            findings.append(_finding(
                number=len(findings) + 1, category=category, code="SLOW_CORE_PHASE",
                severity="info", phase=phase_name, evidence_ids=evidence,
                rationale="A verified Core phase exceeded 5000 ms; duration alone does not identify a cause.",
                suggested_action="Inspect Core phase timing and system load for this operation.",
            ))

    for event in events:
        if event.get("status") != "failure":
            continue
        if event.get("type") not in {
            "turn_completed", "tool_completed", "subagent_completed", "turn_interrupted",
        }:
            continue
        event_id = event.get("eventId")
        findings.append(_finding(
            number=len(findings) + 1, category="runtime", code="AGENT_EVENT_FAILURE",
            severity="error", phase=None,
            evidence_ids=[str(event_id)] if event_id else [],
            rationale="A persisted trace event records a failed operation.",
            suggested_action="Inspect the related tool or turn execution and its verified Core evidence.",
        ))

    if any(item["severity"] == "error" for item in findings):
        status = "issues"
    elif findings:
        status = "incomplete"
    else:
        status = "healthy"
    return {
        "schemaVersion": 1,
        "traceId": trace.get("id"),
        "rulesVersion": TRACE_DIAGNOSIS_RULES_VERSION,
        "status": status,
        "findings": findings,
    }


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _duration(start: Any, end: Any) -> float | None:
    first, last = _timestamp(start), _timestamp(end)
    if first is None or last is None:
        return None
    return max((last - first).total_seconds() * 1000, 0.0)


def _version_summary(trace: Mapping[str, Any]) -> dict[str, Any]:
    cores = [item for item in trace.get("coreDiagnostics", []) if isinstance(item, Mapping)]
    semantic_values = sorted({
        item["semanticVersion"] for item in cores
        if isinstance(item.get("semanticVersion"), str) and item["semanticVersion"]
    })
    policy_values = sorted({
        value
        for item in cores
        for value in item.get("policyVersions", [])
        if isinstance(value, str) and value
    })
    return {
        "traceId": trace.get("id"),
        "status": trace.get("status", "unknown"),
        "model": trace.get("model") if isinstance(trace.get("model"), str) else None,
        "semanticVersion": semantic_values[0] if len(semantic_values) == 1 else None,
        "semanticVersions": semantic_values,
        "policyVersions": policy_values,
        "_semanticVersions": semantic_values,
    }


def _comparison_nodes(trace: Mapping[str, Any]) -> list[dict[str, Any]]:
    spans = [span for span in trace.get("spans", []) if isinstance(span, Mapping)]
    events = [event for event in trace.get("events", []) if isinstance(event, Mapping)]
    span_by_id = {str(span.get("id")): span for span in spans}
    event_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        kind, identifier = event.get("type"), None
        if isinstance(kind, str) and kind.startswith("subagent_"):
            identifier = event.get("agentId")
            group_kind = "agent"
        elif isinstance(kind, str) and kind.startswith("tool_"):
            identifier = event.get("toolUseId")
            group_kind = "tool"
        else:
            continue
        if isinstance(identifier, str):
            event_groups[(group_kind, identifier)].append(event)

    agent_records: dict[str, dict[str, Any]] = {}
    tool_records: list[dict[str, Any]] = []
    for (group_kind, identifier), grouped in event_groups.items():
        ordered = sorted(grouped, key=lambda item: (str(item.get("occurredAt", "")), str(item.get("eventId", ""))))
        has_start = any(str(item.get("type", "")).endswith("_started") for item in ordered)
        span_id = f"agent:{identifier}" if group_kind == "agent" else f"tool:{identifier}"
        span = span_by_id.get(span_id, {})
        record = {
            "id": span_id,
            "status": span.get("status", "unknown"),
            "durationMs": _duration(span.get("startedAt"), span.get("endedAt")),
            "startedAt": span.get("startedAt") if has_start else None,
            "endedAt": span.get("endedAt"),
            "parentAgentId": next(
                (item.get("parentAgentId") for item in ordered if item.get("parentAgentId") is not None),
                None,
            ),
            "hasParentAttribution": any("parentAgentId" in item for item in ordered),
            "toolName": next((item.get("toolName") for item in ordered if item.get("toolName")), None),
            "coreTraceId": next((item.get("coreTraceId") for item in reversed(ordered) if item.get("coreTraceId")), None),
            "hasStart": has_start,
        }
        if group_kind == "agent":
            agent_records[identifier] = {**record, "agentId": identifier}
        else:
            tool_records.append(record)

    # Use IDs only to reconstruct parent relationships within each trace. Never
    # compare those IDs across traces. Ordinals are safe only for sequential
    # siblings with known intervals; parallel or incomplete siblings remain
    # individually unmatched because start order cannot establish identity.
    children: dict[str | None, list[str]] = defaultdict(list)
    for agent_id, record in agent_records.items():
        parent = record.get("parentAgentId")
        children[str(parent) if isinstance(parent, str) else None].append(agent_id)
    for siblings in children.values():
        siblings.sort(key=lambda key: (str(agent_records[key].get("startedAt") or ""), key))

    def intervals_overlap(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
        first_start, second_start = _timestamp(first.get("startedAt")), _timestamp(second.get("startedAt"))
        first_end, second_end = _timestamp(first.get("endedAt")), _timestamp(second.get("endedAt"))
        if first_start is None or second_start is None or first_end is None or second_end is None:
            return True
        return first_start <= second_end and second_start <= first_end

    ambiguous_agents: set[str] = set()
    for siblings in children.values():
        if len(siblings) > 1 and any(
            intervals_overlap(agent_records[first], agent_records[second])
            for index, first in enumerate(siblings)
            for second in siblings[index + 1:]
        ):
            ambiguous_agents.update(siblings)

    agent_paths: dict[str, list[str] | None] = {}

    def path_for(agent_id: str, visiting: set[str] | None = None) -> list[str] | None:
        if agent_id in agent_paths:
            return agent_paths[agent_id]
        record = agent_records[agent_id]
        if not record["hasStart"] or agent_id in ambiguous_agents:
            agent_paths[agent_id] = None
            return None
        seen = set(visiting or ())
        if agent_id in seen:
            agent_paths[agent_id] = None
            return None
        seen.add(agent_id)
        parent = record.get("parentAgentId")
        if parent is None:
            parent_id = None
            parent_path: list[str] = []
        else:
            parent_id = str(parent)
            if parent_id not in agent_records:
                agent_paths[agent_id] = None
                return None
            resolved_parent = path_for(parent_id, seen)
            if resolved_parent is None:
                agent_paths[agent_id] = None
                return None
            parent_path = resolved_parent
        siblings = children[parent_id]
        ordinal = siblings.index(agent_id) + 1 if agent_id in siblings else 1
        result = [*parent_path, f"agent#{ordinal}"]
        agent_paths[agent_id] = result
        return result

    turn_span = span_by_id.get("turn", {})
    result: list[dict[str, Any]] = [{
        "key": "turn", "kind": "turn", "toolName": None, "agentPath": [],
        "id": "turn", "status": turn_span.get("status", trace.get("status", "unknown")),
        "durationMs": _duration(turn_span.get("startedAt"), turn_span.get("endedAt")),
        "phaseDurations": [],
    }]
    for agent_id, record in agent_records.items():
        path = path_for(agent_id)
        result.append({
            "key": (
                "agent/" + "/".join(path)
                if path is not None else f"unmatched/{trace.get('id')}/agent:{agent_id}"
            ),
            "kind": "agent", "toolName": None,
            "agentPath": path[:-1] if path is not None else [], "id": record["id"], "status": record["status"],
            "durationMs": record["durationMs"], "phaseDurations": [],
        })

    tools_by_parent_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in tool_records:
        name = record.get("toolName")
        parent = record.get("parentAgentId")
        parent_id = str(parent) if isinstance(parent, str) else None
        path = path_for(parent_id) if parent_id in agent_records else None
        if not isinstance(name, str) or not record["hasParentAttribution"] or path is None:
            result.append({
                "key": f"unmatched/{trace.get('id')}/{record['id']}", "kind": "tool", "toolName": name,
                "agentPath": path or [], "id": record["id"], "status": record["status"],
                "durationMs": record["durationMs"], "phaseDurations": [],
            })
            continue
        parent_key = "/".join(path)
        tools_by_parent_name[(parent_key, name)].append({**record, "agentPath": path})
    core_by_id = {
        str(core.get("traceId")): core
        for core in trace.get("coreDiagnostics", [])
        if isinstance(core, Mapping) and core.get("traceId")
    }
    for (parent_key, name), siblings in tools_by_parent_name.items():
        siblings.sort(key=lambda item: (str(item.get("startedAt", "")), str(item["id"])))
        ambiguous = len(siblings) > 1 and any(
            intervals_overlap(first, second)
            for index, first in enumerate(siblings)
            for second in siblings[index + 1:]
        )
        for ordinal, record in enumerate(siblings, start=1):
            core = core_by_id.get(str(record.get("coreTraceId")), {})
            phases = core.get("phaseSpans", []) if isinstance(core, Mapping) else []
            phase_durations = [
                {"name": phase["name"], "durationMs": float(phase["durationMs"])}
                for phase in phases
                if isinstance(phase, Mapping)
                and isinstance(phase.get("name"), str)
                and isinstance(phase.get("durationMs"), (int, float))
                and not isinstance(phase.get("durationMs"), bool)
            ] if isinstance(phases, list) else []
            hierarchy = f"{parent_key}/" if parent_key else ""
            result.append({
                "key": (
                    f"unmatched/{trace.get('id')}/{record['id']}"
                    if ambiguous or not record["hasStart"]
                    else f"tool/{hierarchy}{name}#{ordinal}"
                ),
                "kind": "tool",
                "toolName": name, "agentPath": record["agentPath"], "id": record["id"],
                "status": record["status"], "durationMs": record["durationMs"],
                "phaseDurations": phase_durations,
            })
    return result


def _phase_differences(left: list[Mapping[str, Any]], right: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    left_phases = {str(item["name"]): float(item["durationMs"]) for item in left}
    right_phases = {str(item["name"]): float(item["durationMs"]) for item in right}
    return [
        {
            "name": name,
            "leftMs": left_phases.get(name),
            "rightMs": right_phases.get(name),
            "deltaMs": right_phases[name] - left_phases[name]
            if name in left_phases and name in right_phases else None,
        }
        for name in sorted(left_phases.keys() | right_phases.keys())
    ]


def compare_traces(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Compare traces using only stable structural agent/tool identity."""

    left_summary, right_summary = _version_summary(left), _version_summary(right)
    left_nodes = {node["key"]: node for node in _comparison_nodes(left)}
    right_nodes = {node["key"]: node for node in _comparison_nodes(right)}
    aligned: list[dict[str, Any]] = []
    left_only: list[dict[str, Any]] = []
    right_only: list[dict[str, Any]] = []

    def public_node(node: Mapping[str, Any]) -> dict[str, Any]:
        return {key: node[key] for key in ("id", "status", "durationMs")}

    matchable_left = {key for key in left_nodes if not key.startswith("unmatched/")}
    matchable_right = {key for key in right_nodes if not key.startswith("unmatched/")}
    aligned_keys = matchable_left & matchable_right
    for key in sorted(aligned_keys):
        first, second = left_nodes[key], right_nodes[key]
        first_duration, second_duration = first.get("durationMs"), second.get("durationMs")
        phase_differences = _phase_differences(
            first.get("phaseDurations", []), second.get("phaseDurations", []),
        )
        aligned.append({
            "key": key, "kind": first["kind"], "toolName": first.get("toolName"),
            "agentPath": first.get("agentPath", []),
            "left": public_node(first), "right": public_node(second),
            "stateChanged": first.get("status") != second.get("status"),
            "durationDeltaMs": (
                second_duration - first_duration
                if isinstance(first_duration, (int, float)) and isinstance(second_duration, (int, float))
                else None
            ),
            "phaseDurations": phase_differences,
        })
    for key in sorted(left_nodes.keys() - aligned_keys):
        node = left_nodes[key]
        left_only.append({
            "key": key, "kind": node["kind"], "toolName": node.get("toolName"),
            "agentPath": node.get("agentPath", []), **public_node(node),
        })
    for key in sorted(right_nodes.keys() - aligned_keys):
        node = right_nodes[key]
        right_only.append({
            "key": key, "kind": node["kind"], "toolName": node.get("toolName"),
            "agentPath": node.get("agentPath", []), **public_node(node),
        })

    def version_changed(key: str) -> bool | None:
        first, second = left_summary[key], right_summary[key]
        return first != second if first and second else None
    return {
        "schemaVersion": 1,
        "left": {key: value for key, value in left_summary.items() if not key.startswith("_")},
        "right": {key: value for key, value in right_summary.items() if not key.startswith("_")},
        "aligned": aligned,
        "leftOnly": left_only,
        "rightOnly": right_only,
        "versionChanges": {
            "model": version_changed("model"),
            "semanticVersion": version_changed("semanticVersions"),
            "policyVersions": version_changed("policyVersions"),
        },
    }


__all__ = ["TRACE_DIAGNOSIS_RULES_VERSION", "diagnose_trace", "compare_traces"]
