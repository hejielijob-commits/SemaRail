"""Versioned evaluation records and isolated expiring evidence in the control DB."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import math
import re
import secrets
import uuid
from typing import Any, Iterator

try:
    from .access_control import _timestamp
    from .diagnostics import DiagnosticError
    from .evaluation_scoring import VERSION, score
except ImportError:
    from access_control import _timestamp
    from diagnostics import DiagnosticError
    from evaluation_scoring import VERSION, score

TERMINAL = {"completed", "timeout", "process_error", "cancelled", "unsupported_interaction", "interrupted"}


def invalid(message: str, status: int = 400) -> None:
    raise DiagnosticError("INVALID_EVALUATION", message, status=status)


def text(value: Any, field: str, limit: int = 64000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        invalid(field + " is invalid")
    return value


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        invalid("identifier is invalid")
    return value


def scrub_evidence(value: Any) -> Any:
    """Remove credential fields and hidden model reasoning recursively."""
    if isinstance(value, dict):
        return {k: scrub_evidence(v) for k, v in value.items() if not any(part in k.lower() for part in ("password", "secret", "credential", "authorization", "apikey", "access_token", "reasoning", "thinking")) and k.lower() not in {"token", "bearer", "authtoken", "api_key"}}
    if isinstance(value, list):
        return [scrub_evidence(v) for v in value]
    return value


def validate(kind: str, body: Any) -> dict[str, Any]:
    def scalar(value: Any) -> bool:
        return value is None or type(value) in (str, bool, int) or (type(value) is float and math.isfinite(value))
    if not isinstance(body, dict):
        invalid("payload must be an object")
    allowed = ({"name", "testDatasetId", "cases"} if kind == "suites" else {"name", "model", "reasoningEffort", "prompt", "contextVersion", "timeoutSeconds", "concurrency", "rounds"})
    if set(body) - allowed:
        invalid("unsupported fields")
    value = dict(body)
    text(value.get("name"), "name", 200)
    if kind == "profiles":
        text(value.get("model"), "model", 200)
        text(value.get("prompt"), "prompt")
        value.setdefault("reasoningEffort", "medium")
        value.setdefault("contextVersion", 1)
        value.setdefault("timeoutSeconds", 300)
        value.setdefault("concurrency", 2)
        value.setdefault("rounds", 1)
        for key, lower, upper in (("contextVersion", 1, 2), ("timeoutSeconds", 1, 1800), ("concurrency", 1, 4), ("rounds", 1, 3)):
            if type(value[key]) is not int or not lower <= value[key] <= upper:
                invalid(key + " is out of range")
        text(value["reasoningEffort"], "reasoningEffort", 40)
    else:
        identifier(value.get("testDatasetId"))
        cases = value.get("cases")
        if not isinstance(cases, list) or not 1 <= len(cases) <= 500:
            invalid("suite requires 1 to 500 cases")
        ids = set()
        for case in cases:
            if not isinstance(case, dict) or set(case) - {"id", "question", "role", "expectedBehavior", "referenceSql", "expectedResult", "assertions", "conditions", "clarificationReplies", "tags", "reviewGuidance"}:
                invalid("case fields are invalid")
            case_id = identifier(case.get("id"))
            if case_id in ids:
                invalid("case IDs must be unique")
            ids.add(case_id)
            text(case.get("question"), "question")
            identifier(case.get("role"))
            if "tags" in case and (not isinstance(case["tags"], list) or len(case["tags"]) > 100 or any(not isinstance(tag, str) or not tag or len(tag) > 128 for tag in case["tags"])):
                invalid("case tags must be bounded strings")
            if "reviewGuidance" in case and (not isinstance(case["reviewGuidance"], str) or len(case["reviewGuidance"]) > 64000):
                invalid("review guidance must be bounded text")
            if case.get("expectedBehavior") not in {"answer", "clarify", "deny", "unsupported"}:
                invalid("expected behavior is invalid")
            if case.get("referenceSql"):
                from sqlglot import parse, exp
                try:
                    statements = parse(text(case["referenceSql"], "referenceSql"), read="postgres")
                    if len(statements) != 1 or not isinstance(statements[0], exp.Query) or any(isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Into)) for n in statements[0].walk()):
                        invalid("reference SQL must be read-only")
                except DiagnosticError:
                    raise
                except Exception:
                    invalid("reference SQL is invalid")
            for key in ("conditions", "clarificationReplies", "assertions"):
                if key in case and not isinstance(case[key], dict):
                    invalid(key + " must be an object")
            expected = case.get("expectedResult")
            if expected is not None and (not isinstance(expected, dict) or not isinstance(expected.get("columns"), list) or not isinstance(expected.get("rows"), list) or len(expected["rows"]) > 500):
                invalid("expected result is invalid or exceeds 500 rows")
            assertions = case.get("assertions", {})
            if set(assertions) - {"rowCount", "cells", "tolerance", "ordered"}:
                invalid("unsupported assertion")
            if "tolerance" in assertions and (type(assertions["tolerance"]) not in (int, float) or not math.isfinite(assertions["tolerance"]) or assertions["tolerance"] < 0):
                invalid("tolerance must be nonnegative and finite")
            if "ordered" in assertions and type(assertions["ordered"]) is not bool:
                invalid("ordered must be boolean")
            if "rowCount" in assertions and (type(assertions["rowCount"]) is not int or not 0 <= assertions["rowCount"] <= 500):
                invalid("row count is out of range")
            cells = assertions.get("cells", [])
            if not isinstance(cells, list) or len(cells) > 100:
                invalid("cell assertions are invalid")
            for cell in cells:
                if not isinstance(cell, dict) or set(cell) - {"row", "column", "expected", "tolerance"} or type(cell.get("row")) is not int or not 0 <= cell["row"] < 500 or not isinstance(cell.get("column"), str) or "expected" not in cell:
                    invalid("cell assertion is invalid")
                tolerance = cell.get("tolerance", 0)
                if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance < 0:
                    invalid("cell tolerance must be nonnegative and finite")
                if not scalar(cell["expected"]):
                    invalid("cell expected value must be a finite scalar")
            if expected is not None:
                columns = expected["columns"]
                if not columns or any(not isinstance(x, str) for x in columns) or len(set(columns)) != len(columns) or any(not isinstance(row, list) or len(row) != len(columns) for row in expected["rows"]):
                    invalid("expected result rows and columns do not match")
                if any(not scalar(value) for row in expected["rows"] for value in row):
                    invalid("expected result values must be finite scalars")
    try:
        if len(json.dumps(value, allow_nan=False).encode()) > 2_000_000:
            invalid("payload exceeds limit")
    except (TypeError, ValueError):
        invalid("payload must be finite JSON")
    return value


class EvaluationStore:
    """Scoped storage; every mutation is serialized across processes."""

    def __init__(self, diagnostics: Any) -> None:
        self.access = diagnostics.access_control
        self.clock = diagnostics.clock
        with self.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS evaluation_schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
            versions = [r["version"] for r in connection.execute("SELECT version FROM evaluation_schema_migrations ORDER BY version").fetchall()]
            if versions not in ([], [1]):
                invalid("unsupported evaluation storage version", 503)
            connection.execute("CREATE TABLE IF NOT EXISTS evaluation_records(organization_id TEXT NOT NULL,project_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,record_json TEXT NOT NULL,PRIMARY KEY(organization_id,project_id,kind,id))")
            connection.execute("CREATE TABLE IF NOT EXISTS evaluation_evidence(organization_id TEXT NOT NULL,project_id TEXT NOT NULL,attempt_id TEXT NOT NULL,body_json TEXT NOT NULL,expires_at TEXT NOT NULL,PRIMARY KEY(organization_id,project_id,attempt_id))")
            if not versions:
                connection.execute("INSERT INTO evaluation_schema_migrations(version,applied_at) VALUES(?,?)", (1, self.now()))

    def now(self) -> str:
        return _timestamp(self.clock())

    def expires(self, seconds: int) -> str:
        return _timestamp(self.clock() + timedelta(seconds=seconds))

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.access._lock, self.access._connect() as connection:
            if getattr(self.access, "path", None) is None:
                connection.execute("SELECT pg_advisory_xact_lock(?)", (8341972315443008,))
            else:
                connection.execute("BEGIN IMMEDIATE")
            yield connection

    def get(self, connection: Any, scope: tuple[str, str], kind: str, id: str) -> dict[str, Any]:
        row = connection.execute("SELECT record_json FROM evaluation_records WHERE organization_id=? AND project_id=? AND kind=? AND id=?", (*scope, kind, id)).fetchone()
        if row is None:
            invalid("evaluation record was not found", 404)
        return json.loads(row["record_json"])

    def items(self, connection: Any, scope: tuple[str, str], kind: str) -> list[dict[str, Any]]:
        return [json.loads(row["record_json"]) for row in connection.execute("SELECT record_json FROM evaluation_records WHERE organization_id=? AND project_id=? AND kind=? ORDER BY id", (*scope, kind)).fetchall()]

    def put(self, connection: Any, scope: tuple[str, str], kind: str, value: dict[str, Any]) -> dict[str, Any]:
        connection.execute("DELETE FROM evaluation_records WHERE organization_id=? AND project_id=? AND kind=? AND id=?", (*scope, kind, value["id"]))
        connection.execute("INSERT INTO evaluation_records(organization_id,project_id,kind,id,record_json) VALUES(?,?,?,?,?)", (*scope, kind, value["id"], json.dumps(value, allow_nan=False)))
        return value

    def save_asset(self, connection: Any, scope: tuple[str, str], kind: str, body: Any, id: str | None = None) -> dict[str, Any]:
        value = validate(kind, body)
        old = self.get(connection, scope, kind, id) if id else None
        revision = (old["revision"] + 1) if old and old["status"] == "published" else old["revision"] if old else 1
        return self.put(connection, scope, kind, {**value, "schemaVersion": 1, "id": id or uuid.uuid4().hex, "revision": revision, "status": "draft", "updatedAt": self.now()})

    def publish(self, connection: Any, scope: tuple[str, str], kind: str, id: str) -> dict[str, Any]:
        value = self.get(connection, scope, kind, id)
        if value["status"] == "published":
            return value
        if kind == "suites":
            for case in value["cases"]:
                assertions = case.get("assertions", {})
                if case["expectedBehavior"] in {"answer", "clarify"} and not (case.get("referenceSql") or case.get("expectedResult") or "rowCount" in assertions or assertions.get("cells")):
                    invalid("answer cases require a deterministic reference or assertion before publication")
                if case["expectedBehavior"] == "clarify" and not case.get("clarificationReplies"):
                    invalid("clarification cases require configured replies before publication")
        value["status"] = "published"
        value["publishedAt"] = self.now()
        self.put(connection, scope, kind + "_versions", {**value, "id": id + ":" + str(value["revision"]), "assetId": id})
        return self.put(connection, scope, kind, value)

    def attempts(self, connection: Any, scope: tuple[str, str], run_id: str) -> list[dict[str, Any]]:
        return [a for a in self.items(connection, scope, "attempts") if a["runId"] == run_id]

    def summarize(self, connection: Any, scope: tuple[str, str], run: dict[str, Any]) -> dict[str, Any]:
        attempts = self.attempts(connection, scope, run["id"])
        summary = {"total": len(attempts), "started": sum(a.get("startedAt") is not None for a in attempts), "passed": 0, "failed": 0, "inconclusive": 0, "errors": 0, "cancelled": 0}
        for attempt in attempts:
            if attempt["status"] == "cancelled":
                summary["cancelled"] += 1
            elif attempt["status"] in {"timeout", "process_error", "interrupted", "unsupported_interaction"}:
                summary["errors"] += 1
            elif attempt.get("score", {}).get("status") in {"passed", "failed", "inconclusive"}:
                summary[attempt["score"]["status"]] += 1
        summary["passRate"] = summary["passed"] / len(attempts) if attempts else 0
        if attempts and all(a["status"] in TERMINAL and a.get("guardUntil", "") <= self.now() for a in attempts):
            run["status"] = "cancelled" if run.get("cancelRequested") else "completed"
            self.put(connection, scope, "runs", run)
        return {**run, "summary": summary, "attempts": [{k: v for k, v in a.items() if k not in {"leaseToken", "completionHash"}} for a in attempts]}

    def recover(self, connection: Any, scope: tuple[str, str]) -> None:
        connection.execute("DELETE FROM evaluation_evidence WHERE expires_at<=?", (self.now(),))
        for attempt in self.items(connection, scope, "attempts"):
            if attempt["status"] in {"claimed", "running"} and (attempt["expiresAt"] <= self.now() or attempt.get("executionDeadline", attempt["expiresAt"]) <= self.now()):
                attempt["status"] = "interrupted"
                attempt["endedAt"] = self.now()
                # A missing heartbeat does not prove the external process stopped.
                # Keep configuration fenced until its enforced execution deadline.
                attempt["guardUntil"] = attempt.get("executionDeadline", self.expires(1800))
                self.put(connection, scope, "attempts", attempt)
        for run in self.items(connection, scope, "runs"):
            if run["status"] in {"queued", "running"}:
                self.summarize(connection, scope, run)

    def active(self, connection: Any, scope: tuple[str, str]) -> bool:
        return any(r["status"] in {"queued", "running"} for r in self.items(connection, scope, "runs"))

    def cleanup_expired(self) -> int:
        """Delete expired detailed evidence without changing durable summaries."""
        with self.transaction() as connection:
            return connection.execute("DELETE FROM evaluation_evidence WHERE expires_at<=?", (self.now(),)).rowcount

    def complete(self, connection: Any, scope: tuple[str, str], attempt: dict[str, Any], body: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if attempt["status"] in TERMINAL:
            if attempt.get("completionHash") != digest:
                invalid("attempt already has a different terminal result", 409)
            return {k: v for k, v in attempt.items() if k not in {"leaseToken", "completionHash"}}
        status = body.get("status")
        if status not in TERMINAL:
            invalid("invalid completion status")
        evidence = body.get("evidence", {})
        if not isinstance(evidence, dict) or len(json.dumps(evidence, allow_nan=False).encode()) > 2_000_000:
            invalid("evidence exceeds limit")
        evidence = scrub_evidence(evidence)
        previous = connection.execute("SELECT body_json FROM evaluation_evidence WHERE organization_id=? AND project_id=? AND attempt_id=?", (*scope, attempt["id"])).fetchone()
        if previous:
            evidence["events"] = json.loads(previous["body_json"]).get("events", [])
        expires = self.expires(30 * 86400)
        connection.execute("DELETE FROM evaluation_evidence WHERE organization_id=? AND project_id=? AND attempt_id=?", (*scope, attempt["id"]))
        connection.execute("INSERT INTO evaluation_evidence(organization_id,project_id,attempt_id,body_json,expires_at) VALUES(?,?,?,?,?)", (*scope, attempt["id"], json.dumps(evidence), expires))
        attempt.update(status=status, endedAt=self.now(), completionHash=digest, score=score(case, evidence, status), evidenceExpiresAt=expires, durationMs=evidence.get("durationMs"), coreTraceIds=evidence.get("coreTraceIds", []), observedTokens=evidence.get("observedTokens"))
        self.put(connection, scope, "attempts", attempt)
        return {k: v for k, v in attempt.items() if k not in {"leaseToken", "completionHash"}}
