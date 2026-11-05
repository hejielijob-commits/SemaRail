"""Standalone, authenticated-MCP A/B harness for business-trust-v1.

The coordinator owns credentials, runs baseline/candidate Core instances and
records tool results. It never gives a worker its group, expected SQL, oracle,
database connection, or authorization key.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import random
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from tempfile import TemporaryDirectory

import checkpoint as recovery_checkpoint

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
BUNDLE_ROOT = REPO_ROOT / ".benchmark-data" / "business-trust-v1"
RUNS_ROOT = BUNDLE_ROOT / "runs"
BASELINE_DEFAULT = BUNDLE_ROOT / "baseline"
HISTORICAL_DEFAULT = REPO_ROOT.parent / "dsh-wren-data-agent"
CORPUS_PATH = HERE / "corpus.json"
PROMPT_PATH = HERE / "worker-prompt.md"
BASELINE_COMMIT = "2fc59738d3d09832d891e06fc9b39b597fa66f26"
BASELINE_SOURCE_MANIFEST = "baseline-source-manifest-v2.json"
BASELINE_SOURCE_MANIFEST_SHA256 = "6e88b5cd93bd15d2e4329cccd458cd4f0c694096d5685d2c7bed8424ba4cce43"
BASELINE_RUNTIME_MANIFEST = "baseline-runtime-manifest.json"
BASELINE_RUNTIME_MANIFEST_SHA256 = "66d37e3e15faaf0e010a512b16c012b05a0c3e91906ff86b1299f6c71d114095"
ALLOWED_REQUIRED = frozenset({
    "semarail_validate_project", "semarail_list_models", "semarail_get_context",
    "semarail_plan_query", "semarail_prepare_query", "semarail_governed_query",
})
ALLOWED_OPTIONAL = frozenset({"semarail_get_query_evidence"})
REPAIRABLE = frozenset({"SEMANTIC_ERROR"})
TERMINAL = frozenset({"POLICY_DENIED", "FORBIDDEN", "TIMEOUT", "CANCELLED"})
DENIAL_CODES = frozenset({"POLICY_DENIED", "FORBIDDEN"})
TREND_QUESTION_IDS = frozenset({"BTV-12", "BTV-14", "BTV-16", "BTV-20",
                                "BTV-22", "BTV-24", "BTV-26", "BTV-28"})
GROUPS = ("baseline", "candidate")
DEFAULT_PORT_BASE = 48781
DEFAULT_DATABASE_URL_ENV = "BUSINESS_TRUST_DATABASE_URL"
MAX_CALLS = 12
DEADLINE_SECONDS = 300
MAX_CONCURRENT = 2
CLARIFICATION_TOPICS = (
    ("timeRange", "Reporting date, snapshot, quarter, or date range."),
    ("granularity", "Requested grouping or level of aggregation."),
)
SCHEDULE_SEED = 20261012
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_MODEL_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
WORKER_READY_GATES = (
    "regression.json", "context-delivery-validation.json",
    "execution-check-validation.json", "oracle-validation.json",
    "tool-capabilities.json",
)


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return digest_bytes(encoded)


def public_clarification_protocol() -> dict[str, Any]:
    """Shared conversation transport topics, independent of published Core keys."""
    return {"schemaVersion": 1, "topics": [
        {"key": key, "description": description} for key, description in CLARIFICATION_TOPICS
    ]}


def _database_url_env_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("database URL environment variable name is invalid")
    return name


def database_connection(name: str = DEFAULT_DATABASE_URL_ENV) -> dict[str, Any]:
    """Read and parse the private PostgreSQL URL without persisting or printing it."""
    env_name = _database_url_env_name(name)
    value = os.environ.get(env_name)
    if not value:
        raise ValueError("database URL environment variable is not configured")
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, strict_parsing=True)
        if parsed.scheme.lower() not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ValueError
        if parsed.username is None or parsed.password is None or not parsed.path.strip("/"):
            raise ValueError
        if parsed.fragment or any(len(values) != 1 for values in query.values()):
            raise ValueError
        port = parsed.port or 5432
        if not 1 <= port <= 65535:
            raise ValueError
        ssl_mode = query.get("sslmode", [None])[0]
        if ssl_mode not in {None, "disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError
        return {"host": parsed.hostname, "port": port,
                "database": unquote(parsed.path.lstrip("/")),
                "user": unquote(parsed.username), "password": unquote(parsed.password),
                **({"ssl_mode": ssl_mode} if ssl_mode else {})}
    except (ValueError, UnicodeError):
        # urlsplit/parser exceptions may embed the original URL, so expose only a
        # fixed diagnostic and never include parser text in logs or artifacts.
        raise ValueError("database URL is not a supported PostgreSQL connection string") from None


def write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("run ID must contain only letters, digits, underscore or hyphen")
    return value


def run_path(run_id: str, runs_root: Path = RUNS_ROOT) -> Path:
    return runs_root / safe_run_id(run_id)


def validate_corpus(corpus: dict[str, Any]) -> None:
    questions = corpus.get("questions")
    if corpus.get("schemaVersion") != 1 or corpus.get("benchmarkId") != "business-trust-v1":
        raise ValueError("unsupported benchmark corpus version")
    if not isinstance(questions, list) or len(questions) != 30:
        raise ValueError("corpus must contain exactly 30 questions")
    if len({item.get("id") for item in questions}) != 30:
        raise ValueError("corpus question IDs must be unique")
    actor_counts: dict[str, int] = {}
    language_counts: dict[str, int] = {}
    scenario_counts: dict[str, int] = {}
    actor_scenario_counts: dict[tuple[str, str], int] = {}
    clarification_scenarios: set[str] = set()
    blocked = 0
    denials = 0
    for question in questions:
        if question.get("schemaVersion", 1) != 1:
            raise ValueError("unsupported question schema")
        actor, language, scenario = question.get("actor"), question.get("language"), question.get("scenario")
        actor_counts[actor] = actor_counts.get(actor, 0) + 1
        language_counts[language] = language_counts.get(language, 0) + 1
        scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1
        actor_scenario_counts[(scenario, actor)] = actor_scenario_counts.get((scenario, actor), 0) + 1
        if actor not in {"employee", "department_manager", "hrbp", "compensation_admin", "hr_director"}:
            raise ValueError("unknown benchmark actor")
        if language not in {"zh", "en"}:
            raise ValueError("unsupported question language")
        public_question = question.get("public", {}).get("question")
        if not isinstance(public_question, str) or not public_question.strip():
            raise ValueError("public question is required")
        # A finite-corpus script guard, not a general natural-language detector.
        # Balance metadata alone cannot establish the actual 20/10 question mix.
        has_han = re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", public_question) is not None
        if ((language == "zh" and not has_han)
            or (language == "en" and (has_han or re.search(r"[A-Za-z]", public_question) is None))):
            raise ValueError("public question text does not match declared language")
        columns = question.get("public", {}).get("outputColumns")
        tags = question.get("public", {}).get("conditionKeys")
        if (not isinstance(columns, list) or len(columns) != len(set(columns))
            or not isinstance(tags, list) or not tags or len(tags) != len(set(tags))):
            raise ValueError("public output columns and answer condition tags are required")
        if question.get("clarification"):
            prompt_keys = question["clarification"].get("promptKeys")
            known_topics = {key for key, _ in CLARIFICATION_TOPICS}
            if (not isinstance(prompt_keys, list) or not prompt_keys
                or any(not isinstance(key, str) or key not in known_topics for key in prompt_keys)):
                raise ValueError("clarification prompt keys must use the public conversation topics")
            clarification_scenarios.add(scenario)
        oracle = question.get("oracle", {})
        canonical = question.get("canonical") or {}
        conditions = canonical.get("conditions", {})
        confirmed = canonical.get("confirmedConditions", [])
        trend_enabled = conditions.get("trend") is True and "trend" in confirmed
        if (question["id"] in TREND_QUESTION_IDS) != trend_enabled:
            raise ValueError("only explicit original-date trend cases may activate the trend condition")
        if oracle.get("type") == "blocked":
            blocked += 1
            if question.get("canonical") or not oracle.get("requiresNoQuery"):
                raise ValueError("blocked case must not have canonical SQL")
        elif oracle.get("type") == "rows":
            if not question.get("canonical", {}).get("semanticSql"):
                raise ValueError("row cases require private canonical semantic SQL")
        elif oracle.get("type") == "denial":
            denials += 1
            if not question.get("canonical", {}).get("semanticSql"):
                raise ValueError("denial cases require private denied SQL")
            if oracle.get("errorCode") not in DENIAL_CODES:
                raise ValueError("denial case must use an approved policy error code")
        else:
            raise ValueError("unknown oracle type")
    if actor_counts != {"employee": 6, "department_manager": 6, "hrbp": 6,
                        "compensation_admin": 6, "hr_director": 6}:
        raise ValueError("actor design must be 6 questions per role")
    if language_counts != {"zh": 20, "en": 10}:
        raise ValueError("language design must be 20 Chinese and 10 English questions")
    scenarios = {"current_headcount", "salary_snapshots", "quarterly_performance"}
    actors = {"employee", "department_manager", "hrbp", "compensation_admin", "hr_director"}
    if scenario_counts != {scenario: 10 for scenario in scenarios}:
        raise ValueError("scenario design must contain 10 questions per current-headcount, salary, and quarterly-performance scene")
    if actor_scenario_counts != {(scenario, actor): 2 for scenario in scenarios for actor in actors}:
        raise ValueError("each scene must contain two questions for each of the five actors")
    if clarification_scenarios != scenarios:
        raise ValueError("each business scene must include an explicit clarification case")
    if denials < 6:
        raise ValueError("corpus must include role-based refusal cases")
    if blocked != 1:
        raise ValueError("corpus must contain exactly one explicit blocked complex-SQL case")


def make_schedule(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rng = random.Random(SCHEDULE_SEED)
    schedule: list[dict[str, Any]] = []
    for round_no in (1, 2, 3):
        question_ids = [question["id"] for question in questions]
        rng.shuffle(question_ids)
        for question_id in question_ids:
            groups = list(GROUPS)
            rng.shuffle(groups)
            for group in groups:
                schedule.append({"id": f"job-{len(schedule)+1:03d}",
                                 "questionId": question_id,
                                 "group": group, "round": round_no})
    validate_schedule(questions, schedule)
    return schedule


def validate_schedule(questions: list[dict[str, Any]], schedule: list[dict[str, Any]]) -> None:
    if len(schedule) != 180:
        raise ValueError("frozen schedule must contain exactly 180 jobs")
    if [item.get("id") for item in schedule] != [f"job-{i:03d}" for i in range(1, 181)]:
        raise ValueError("schedule IDs must be the fixed 001..180 positions")
    question_ids = {item["id"] for item in questions}
    seen: set[tuple[str, str, int]] = set()
    for entry in schedule:
        question_id, group, round_no = entry.get("questionId"), entry.get("group"), entry.get("round")
        if question_id not in question_ids or group not in GROUPS or type(round_no) is not int or round_no not in (1, 2, 3):
            raise ValueError("invalid schedule entry")
        key = (question_id, group, round_no)
        if key in seen:
            raise ValueError("schedule position duplicated")
        seen.add(key)
    expected = {(q, g, r) for q in question_ids for g in GROUPS for r in (1, 2, 3)}
    if seen != expected:
        raise ValueError("schedule must cover every question, group and round exactly once")
    counts = {group: sum(item["group"] == group for item in schedule) for group in GROUPS}
    if counts != {"baseline": 90, "candidate": 90}:
        raise ValueError("schedule must balance 90 baseline and 90 candidate jobs")


def source_files(root: Path) -> dict[str, str]:
    selected = (
        "apps/semantic-console/server", "apps/semantic-console/web/src",
        "python/sidecar/sidecar", "packages/contract/src", "packages/contract/tests",
        "packages/core/bin", "packages/core/runtime", "packages/core/python",
        "packages/core/scripts", "packages/core/tests", "packages/core/semantic-console-web",
        "benchmarks/hr-enterprise/project", "benchmarks/hr-enterprise/sql",
    )
    paths: set[Path] = set()
    for relative in selected:
        base = root / relative
        if base.is_dir():
            paths.update(path for path in base.rglob("*") if path.is_file())
    for relative in (
        "benchmarks/hr-enterprise/run.py", "benchmarks/hr-enterprise/prepare.py",
        "benchmarks/hr-enterprise/golden-questions.json",
        "apps/semantic-console/pyproject.toml",
        "apps/semantic-console/web/index.html", "apps/semantic-console/web/package.json",
        "apps/semantic-console/web/tsconfig.json", "apps/semantic-console/web/vite.config.ts",
        "apps/semantic-console/web/vitest.config.ts",
        "python/sidecar/pyproject.toml",
        "packages/contract/package.json", "packages/contract/tsconfig.json",
        "packages/core/package.json", "package.json", "pnpm-lock.yaml",
        "pnpm-workspace.yaml", "scripts/package-distribution.mjs",
    ):
        path = root / relative
        if path.is_file():
            paths.add(path)
    if root == REPO_ROOT:
        paths.update(path for path in HERE.iterdir() if path.is_file())
        test_dir = HERE / "tests"
        if test_dir.is_dir():
            paths.update(path for path in test_dir.rglob("*") if path.is_file() and path.suffix == ".py")
        runner = REPO_ROOT / "scripts" / "business-trust-v1.mjs"
        if runner.is_file():
            paths.add(runner)
    return {path.relative_to(root).as_posix(): digest(path)
            for path in sorted(paths) if "__pycache__" not in path.parts}


def distribution_artifact(root: Path) -> dict[str, Any] | None:
    """Record the package identity and, when present, the exact packed artifact."""
    package_path = root / "packages/core/package.json"
    if not package_path.is_file():
        return None
    package = read(package_path)
    name, version = package.get("name"), package.get("version")
    if (not isinstance(name, str) or not name.startswith("@") or "/" not in name
        or not isinstance(version, str) or not version):
        raise ValueError("candidate package identity is invalid")
    filename = f"{name[1:].replace('/', '-')}-{version}.tgz"
    relative = Path("dist") / filename
    artifact = root / relative
    result: dict[str, Any] = {"name": name, "version": version, "artifact": None}
    if artifact.is_file():
        result["artifact"] = {"path": relative.as_posix(), "sha256": digest(artifact)}
    return result


def verify_distribution_artifact(root: Path, frozen: Mapping[str, Any] | None) -> None:
    if frozen is None:
        return
    package = read(root / "packages/core/package.json")
    if package.get("name") != frozen.get("name") or package.get("version") != frozen.get("version"):
        raise ValueError("candidate package identity changed after freeze")
    artifact = frozen.get("artifact")
    if artifact is None:
        return
    if not isinstance(artifact, dict):
        raise ValueError("candidate distribution artifact record is invalid")
    relative = artifact.get("path")
    path = Path(relative) if isinstance(relative, str) else None
    if (path is None or path.is_absolute() or ".." in path.parts
        or path.as_posix() != relative):
        raise ValueError("candidate distribution artifact path is invalid")
    filename = f"{package['name'][1:].replace('/', '-')}-{package['version']}.tgz"
    if path != Path("dist") / filename:
        raise ValueError("candidate distribution artifact identity changed")
    artifact_path = root / path
    if not artifact_path.is_file() or digest(artifact_path) != artifact.get("sha256"):
        raise ValueError("candidate distribution artifact hash changed")


class _FixtureValidator:
    """Avoid invoking Wren while compiling the immutable knowledge fixture."""

    def health(self) -> dict[str, Any]:
        return {"available": True}

    def validate(self, _project_dir: Path) -> dict[str, Any]:
        return {"valid": True, "errors": [], "warnings": []}

    def build(self, _project_dir: Path) -> dict[str, Any]:
        return {"models": []}


class _FixtureIndex:
    def stage(self, _manifest: Mapping[str, Any], _project_dir: Path, _revision: str) -> None:
        return None

    def activate(self, _revision: str) -> None:
        return None

    def status(self, _revision: str | None = None) -> Mapping[str, Any]:
        return {"status": "ready", "backend": "hybrid"}


def _add_business_rules(project_dir: Path, state_dir: Path) -> None:
    """Write and publish the shared business-trust benchmark checks."""
    app = REPO_ROOT / "apps" / "semantic-console"
    sidecar = REPO_ROOT / "python" / "sidecar"
    for path in (str(app), str(sidecar)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from server.project import ProjectStore
    from server.service import SemanticConsoleService

    store = ProjectStore(project_dir, state_dir=state_dir,
                         validator=_FixtureValidator(),
                         semantic_index_lifecycle=_FixtureIndex())
    service = SemanticConsoleService(store)
    rules = (
        {
            "title": "Current active workforce headcount",
            "slug": "business-trust-active-headcount",
            "content": (
                "A current active headcount counts distinct employee_id values from "
                "employees and requires resigned = FALSE. Preserve the caller's "
                "authorized scope and requested grouping."
            ),
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "active-employee-count", "kind": "metric",
                 "model": "employees", "column": "employee_id",
                 "aggregation": "count", "distinct": True,
                 "grain": "employee"},
                {"id": "active-status", "kind": "predicate",
                 "model": "employees", "column": "resigned",
                 "operator": "=", "value": False},
            ]},
        },
        {
            "title": "Salary snapshot date and grain",
            "slug": "business-trust-salary-snapshot",
            "content": (
                "Salary history is a snapshot fact at effective_date grain. "
                "Use average monthly base salary. For a requested date range, carry "
                "a confirmed effectiveDateRange condition "
            "that matches the SQL filter. For a trend across snapshots, group "
            "by the original effective_date. Do not infer a currency."
            ),
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "average-monthly-salary", "kind": "metric",
                 "model": "compensation_history", "column": "monthly_salary",
                 "aggregation": "avg", "grain": "employee snapshot"},
                {"id": "effective-date", "kind": "timeFilter",
                 "model": "compensation_history", "column": "effective_date",
                 "conditionKey": "effectiveDateRange", "valueType": "dateRange",
                 "confirmation": "confirmed"},
                {"id": "snapshot-grain", "kind": "timeGrouping",
                 "model": "compensation_history", "column": "effective_date",
                 "mode": "original", "whenGrouped": True,
                 "conditionKey": "trend"},
            ]},
        },
        {
            "title": "Performance review date and grain",
            "slug": "business-trust-performance-review",
            "content": (
                "Performance reviews are historical facts at review_date grain. "
                "Use average performance_score. For a requested quarter or date "
                "range, carry a confirmed reviewPeriod condition that matches the "
                "SQL filter. For a trend, group by the original review_date; "
                "never transform it to a calendar bucket."
            ),
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "average-performance-score", "kind": "metric",
                 "model": "performance_reviews", "column": "performance_score",
                 "aggregation": "avg", "grain": "employee review"},
                {"id": "review-period", "kind": "timeFilter",
                 "model": "performance_reviews", "column": "review_date",
                 "conditionKey": "reviewPeriod", "valueType": "dateRange",
                 "confirmation": "confirmed"},
                {"id": "review-grain", "kind": "timeGrouping",
                 "model": "performance_reviews", "column": "review_date",
                 "mode": "original", "whenGrouped": True,
                 "conditionKey": "trend"},
            ]},
        },
    )
    for rule in rules:
        service.create_rule(rule)
    store.publish(label="business-trust-v1 shared benchmark fixture")


def build_checked_project_template(source: Path, target: Path,
                                   state_dir: Path) -> dict[str, str]:
    """Clone the frozen HR MDL and publish the shared benchmark checks."""
    shutil.copytree(source, target,
                    ignore=shutil.ignore_patterns(".wren", "__pycache__"))
    _add_business_rules(target, state_dir)
    return {path.relative_to(target).as_posix(): digest(path)
            for path in sorted(target.rglob("*"))
            if path.is_file() and ".wren" not in path.parts and "__pycache__" not in path.parts}


def materialize_oracles(corpus: Mapping[str, Any], database_url: str) -> dict[str, Any]:
    """Freeze independent physical results before any official worker starts."""
    from native_oracle import execute as native_execute

    output: dict[str, Any] = {}
    for question in corpus["questions"]:
        oracle_type = question["oracle"]["type"]
        if oracle_type == "rows":
            output[question["id"]] = native_execute(question, database_url=database_url)
        elif oracle_type == "denial":
            output[question["id"]] = {"schemaVersion": 2, "status": "error",
                "error": {"code": question["oracle"]["errorCode"]},
                "stats": {"returnedRows": 0, "truncated": False}}
        elif oracle_type == "blocked":
            output[question["id"]] = {"schemaVersion": 1, "type": "blocked",
                "noQueryRequired": True}
        else:
            raise ValueError("unknown canonical oracle type")
    if set(output) != {question["id"] for question in corpus["questions"]}:
        raise ValueError("physical oracle materialization is incomplete")
    return output


def verify_baseline(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = read(manifest_path)
    if digest(manifest_path) != BASELINE_SOURCE_MANIFEST_SHA256:
        raise ValueError("frozen baseline v2 source manifest hash mismatch")
    if manifest.get("schemaVersion") != 2 or manifest.get("baselineCommit") != BASELINE_COMMIT:
        raise ValueError("frozen baseline commit does not match the approved identity")
    v1_path = manifest_path.with_name("baseline-source-manifest.json")
    if (manifest.get("originalWorkingTreeManifestSha256") != digest(v1_path)
        or read(v1_path).get("baselineCommit") != BASELINE_COMMIT):
        raise ValueError("v2 baseline manifest does not link to the immutable v1 manifest")
    archive_path = manifest_path.with_name("baseline.tar")
    if manifest.get("baselineArchiveSha256") != digest(archive_path):
        raise ValueError("frozen baseline archive hash mismatch")
    if manifest.get("baselinePath") and Path(manifest["baselinePath"]).resolve() != root.resolve():
        raise ValueError("frozen baseline checkout path differs from its v2 manifest")
    if manifest.get("allArchiveFilesVerifiedAgainstGitCommit") is not True:
        raise ValueError("v2 baseline archive has no Git commit verification")
    expected = manifest.get("sourceHashes")
    git_hashes = manifest.get("gitBlobHashes")
    if (not isinstance(expected, dict) or len(expected) != 352
        or not isinstance(git_hashes, dict) or set(git_hashes) != set(expected)):
        raise ValueError("baseline source manifest is empty")
    hash_pattern = re.compile(r"[0-9a-f]{64}")
    if any(not isinstance(value, str) or not hash_pattern.fullmatch(value)
           for value in [*expected.values(), *git_hashes.values()]):
        raise ValueError("baseline v2 source hashes are malformed")
    if not isinstance(manifest.get("normalization"), str) or "Git" not in manifest["normalization"]:
        raise ValueError("baseline v2 byte normalization is not documented")
    raw_manifest = read(v1_path).get("sourceHashes", {})
    raw_differences = sorted(path for path in set(raw_manifest) & set(expected)
                             if raw_manifest[path] != expected[path])
    if raw_differences != manifest.get("rawWorkingTreeHashDifferences"):
        raise ValueError("baseline v2 raw-worktree difference record is inconsistent")

    tree = subprocess.run(["git", "ls-tree", "-r", "-z", BASELINE_COMMIT],
        cwd=REPO_ROOT, capture_output=True, check=True).stdout
    tree_blobs: dict[str, str] = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        header, name = entry.split(b"\t", 1)
        _mode, object_type, object_id = header.decode("ascii").split(" ")
        if object_type == "blob":
            tree_blobs[name.decode("utf-8")] = object_id
    if set(tree_blobs) != set(git_hashes):
        raise ValueError("v2 baseline archive file set differs from the approved Git commit")
    batch = subprocess.run(["git", "cat-file", "--batch"], cwd=REPO_ROOT,
        input=("\n".join(tree_blobs[name] for name in sorted(tree_blobs)) + "\n").encode("ascii"),
        capture_output=True, check=True).stdout
    offset = 0
    for name in sorted(tree_blobs):
        header_end = batch.find(b"\n", offset)
        if header_end < 0:
            raise ValueError("Git blob verification output is incomplete")
        _object_id, object_type, size_text = batch[offset:header_end].decode("ascii").split(" ")
        size = int(size_text)
        blob_start, blob_end = header_end + 1, header_end + 1 + size
        if object_type != "blob" or batch[blob_end:blob_end + 1] != b"\n":
            raise ValueError("Git blob verification returned malformed content")
        if digest_bytes(batch[blob_start:blob_end]) != git_hashes[name]:
            raise ValueError("v2 baseline canonical bytes differ from the approved Git commit")
        offset = blob_end + 1
    failures = []
    for relative, expected_hash in expected.items():
        path = root / Path(relative)
        if not path.is_file() or digest(path) != expected_hash:
            failures.append(relative)
    if failures:
        raise ValueError(f"frozen baseline source hash mismatch ({len(failures)} files)")
    return manifest


def verify_runtime_freeze(data_manifest: Path | None = None) -> dict[str, Any]:
    """Verify the pinned baseline Python/runtime freeze before a campaign."""
    path = BUNDLE_ROOT / BASELINE_RUNTIME_MANIFEST
    if digest(path) != BASELINE_RUNTIME_MANIFEST_SHA256:
        raise ValueError("baseline runtime manifest hash mismatch")
    manifest = read(path)
    if (manifest.get("schemaVersion") != 1
        or manifest.get("sourceBaselineCommit") != BASELINE_COMMIT
        or manifest.get("sameRuntimeRequiredForBothGroups") is not True
        or manifest.get("python") != sys.version):
        raise ValueError("baseline runtime identity differs from the active runtime")
    dependencies = manifest.get("pythonDependencies")
    if not isinstance(dependencies, dict) or not dependencies:
        raise ValueError("baseline runtime dependency freeze is missing")
    mismatched = []
    for package, expected in dependencies.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        if actual != expected:
            mismatched.append(package)
    if mismatched:
        raise ValueError("active Python dependencies differ from the frozen baseline runtime")
    if dependencies.get("wrenai") != "0.13.2":
        raise ValueError("baseline runtime does not pin Wren 0.13.2")
    constraints = BASELINE_DEFAULT / "packages/core/runtime/constraints.txt"
    python_freeze = BUNDLE_ROOT / "python-runtime-freeze.txt"
    if (digest(constraints) != manifest.get("runtimeConstraintsSha256")
        or digest(python_freeze) != manifest.get("pythonFreezeSha256")):
        raise ValueError("frozen runtime constraints or package freeze changed")
    node = subprocess.run(["node", "--version"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True).stdout.strip()
    if node != manifest.get("node"):
        raise ValueError("active Node.js version differs from the frozen baseline runtime")
    data_manifest = (data_manifest or HISTORICAL_DEFAULT /
        ".benchmark-data/hr-enterprise/generated/manifest.json").resolve()
    if digest(data_manifest) != manifest.get("dataManifestSha256"):
        raise ValueError("synthetic dataset differs from the frozen runtime manifest")
    return {"path": path, "manifest": manifest, "sha256": digest(path),
            "dataManifest": data_manifest}


def verify_embedding_freeze() -> dict[str, Any]:
    """Pin the local multilingual checkpoint so no network resolution can drift."""
    hf_home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    hub = Path(os.environ.get("HF_HUB_CACHE") or hf_home / "hub").resolve()
    model_cache = hub / f"models--sentence-transformers--{EMBEDDING_MODEL}"
    reference = model_cache / "refs" / "main"
    if not reference.is_file() or reference.read_text(encoding="utf-8").strip() != EMBEDDING_MODEL_REVISION:
        raise ValueError("pinned multilingual embedding checkpoint ref is unavailable")
    snapshot = model_cache / "snapshots" / EMBEDDING_MODEL_REVISION
    if not snapshot.is_dir() or not all((snapshot / name).is_file()
            for name in ("config.json", "modules.json", "sentence_bert_config.json")):
        raise ValueError("pinned multilingual embedding checkpoint snapshot is incomplete")
    snapshot_root = snapshot.resolve()
    files: dict[str, str] = {}
    for path in sorted(snapshot.rglob("*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(hub):
            raise ValueError("embedding checkpoint contains a file outside the configured HF cache")
        files[path.relative_to(snapshot).as_posix()] = digest(path)
    if not files:
        raise ValueError("pinned multilingual embedding checkpoint has no files")
    return {"model": EMBEDDING_MODEL, "revision": EMBEDDING_MODEL_REVISION,
            "cachePath": str(snapshot_root), "files": files,
            "sha256": canonical_hash(files)}


def configure_embedding_environment(environment: dict[str, str]) -> dict[str, str]:
    """Apply the frozen offline hybrid-retrieval settings to Core child processes."""
    environment.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "SEMARAIL_EMBEDDING_PROVIDER": "sentence-transformers",
        "SEMARAIL_EMBEDDING_MODEL": EMBEDDING_MODEL,
        "SEMARAIL_EMBEDDING_MODEL_REVISION": EMBEDDING_MODEL_REVISION,
    })
    return environment


def role_policy(actor: str, datasource_id: str) -> dict[str, Any]:
    """Pinned benchmark-only policy; each group receives the same role rules."""
    def rule(field: str | None = None, attribute: str | None = None, *, operator: str = "eq",
             deny: bool = False, denied: tuple[str, ...] = ()) -> dict[str, Any]:
        if deny:
            return {"effect": "deny"}
        value: dict[str, Any] = {"effect": "allow", "rows": [], "columns": {"deny": list(denied)}}
        if field and attribute:
            value["rows"] = [{"field": field, "operator": operator,
                              "valueFrom": f"subject.attributes.{attribute}"}]
        return value
    dimensions = {"hr.regions": rule(), "hr.departments": rule()}
    if actor == "employee":
        tables = {**dimensions,
            "hr.employees": rule("employee_id", "employeeId", denied=("gender", "age")),
            "hr.compensation_history": rule("employee_id", "employeeId"),
            "hr.performance_reviews": rule(deny=True),
            "hr.attendance_monthly": rule("employee_id", "employeeId")}
    elif actor == "department_manager":
        tables = {**dimensions,
            "hr.employees": rule("manager_id", "employeeId", denied=("gender", "age")),
            "hr.compensation_history": rule(deny=True),
            "hr.performance_reviews": rule("manager_id", "employeeId"),
            "hr.attendance_monthly": rule("manager_id", "employeeId")}
    elif actor == "hrbp":
        tables = {"hr.regions": rule("region_code", "regionCodes", operator="in"),
            "hr.departments": rule("region_code", "regionCodes", operator="in"),
            "hr.employees": rule("region_code", "regionCodes", operator="in"),
            "hr.compensation_history": rule(deny=True),
            "hr.performance_reviews": rule("region_code", "regionCodes", operator="in"),
            "hr.attendance_monthly": rule("region_code", "regionCodes", operator="in")}
    elif actor == "compensation_admin":
        tables = {"hr.regions": rule("region_code", "regionCodes", operator="in"),
            "hr.departments": rule("region_code", "regionCodes", operator="in"),
            "hr.employees": rule("region_code", "regionCodes", operator="in"),
            "hr.compensation_history": rule("region_code", "regionCodes", operator="in"),
            "hr.performance_reviews": rule(deny=True),
            "hr.attendance_monthly": rule(deny=True)}
    elif actor == "hr_director":
        tables = {name: rule() for name in (
            "hr.regions", "hr.departments", "hr.employees", "hr.compensation_history",
            "hr.performance_reviews", "hr.attendance_monthly")}
    else:
        raise ValueError("unknown benchmark actor")
    return {"schemaVersion": 1, "datasourceId": datasource_id,
        "projects": ["semarail_hr_enterprise"],
        "tools": ["runtime:health", "project:validate", "semantic:read", "query:plan", "query:execute", "query:cancel"],
        "limits": {"maxRows": 500, "previewRows": 50, "maxPreviewBytes": 131072, "timeoutMs": 10000},
        "tables": tables}


def role_policy_hashes() -> dict[str, str]:
    return {actor: canonical_hash(role_policy(actor, "benchmark-datasource"))
            for actor in ("employee", "department_manager", "hrbp", "compensation_admin", "hr_director")}


def repo_commit(root: Path) -> str:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                               text=True, encoding="utf-8", check=True)
    return completed.stdout.strip()


def freeze_run(run_id: str, *, baseline: Path = BASELINE_DEFAULT,
               candidate: Path = REPO_ROOT, data_manifest: Path | None = None,
               database_url_env: str = DEFAULT_DATABASE_URL_ENV,
               port_base: int = DEFAULT_PORT_BASE,
               model: str = "gpt-6-luna", reasoning: str = "max",
               runs_root: Path = RUNS_ROOT,
               execution_mode: str = "official_campaign",
               recovery_protocol: str | None = None) -> Path:
    if model != "gpt-6-luna" or reasoning != "max":
        raise ValueError("business-trust-v1 requires gpt-6-luna with max reasoning")
    if execution_mode not in {"official_campaign", "preacceptance_only"}:
        raise ValueError("unsupported benchmark execution mode")
    if recovery_protocol not in {None, recovery_checkpoint.RECOVERY_PROTOCOL["mode"]}:
        raise ValueError("unsupported benchmark recovery protocol")
    baseline, candidate = baseline.resolve(), candidate.resolve()
    if candidate != REPO_ROOT.resolve():
        raise ValueError("candidate must be the current semarail-trace-v2 checkout")
    baseline_manifest_path = BUNDLE_ROOT / BASELINE_SOURCE_MANIFEST
    baseline_manifest = verify_baseline(baseline, baseline_manifest_path)
    corpus = read(CORPUS_PATH)
    validate_corpus(corpus)
    data_manifest = (data_manifest or HISTORICAL_DEFAULT / ".benchmark-data/hr-enterprise/generated/manifest.json").resolve()
    if not data_manifest.is_file():
        raise ValueError("verified synthetic HR dataset manifest is missing")
    runtime_freeze = verify_runtime_freeze(data_manifest)
    embedding_freeze = verify_embedding_freeze()
    manifest_hash = digest(data_manifest)
    database_url_env = _database_url_env_name(database_url_env)
    connection = database_connection(database_url_env)
    if connection["port"] != runtime_freeze["manifest"].get("databasePort"):
        raise ValueError("PostgreSQL port differs from the frozen baseline runtime manifest")
    if not 1024 <= port_base <= 65530:
        raise ValueError("configured ports are outside supported range")
    schedule = make_schedule(corpus["questions"])
    dispatch_nonces = {item["id"]: secrets.token_urlsafe(24) for item in schedule}
    candidate_sources = source_files(candidate)
    if not candidate_sources:
        raise ValueError("candidate source hash set is empty")
    project_source = baseline / "benchmarks/hr-enterprise/project"
    if not project_source.is_dir():
        raise ValueError("baseline HR semantic project is missing")
    legacy_project_hashes = {path.relative_to(project_source).as_posix(): digest(path)
                             for path in project_source.rglob("*")
                             if path.is_file() and ".wren" not in path.parts}
    if not legacy_project_hashes:
        raise ValueError("semantic project snapshot is empty")
    oracle_results = materialize_oracles(corpus, os.environ[database_url_env])
    target = run_path(run_id, runs_root)
    target.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(baseline_manifest_path, target / BASELINE_SOURCE_MANIFEST)
    shutil.copyfile(runtime_freeze["path"], target / BASELINE_RUNTIME_MANIFEST)
    shutil.copyfile(CORPUS_PATH, target / "corpus.json")
    shutil.copyfile(PROMPT_PATH, target / "worker-prompt.md")
    shutil.copytree(project_source, target / "project-template",
                    ignore=shutil.ignore_patterns(".wren", "__pycache__"))
    with TemporaryDirectory(prefix="btv-project-fixture-") as temp_state:
        checked_project_hashes = build_checked_project_template(
            project_source, target / "checked-project-template", Path(temp_state) / "state")
    project_hashes = checked_project_hashes
    write_new(target / "oracle-results.json", oracle_results)
    write_new(target / "dispatch-nonces.json", dispatch_nonces)
    role_hashes = role_policy_hashes()
    evaluator_config = {
        "schemaVersion": 1, "sqlRepairCount": 1, "maxToolCalls": MAX_CALLS,
        "deadlineSeconds": DEADLINE_SECONDS, "maxConcurrentWorkers": MAX_CONCURRENT,
        "missingAndInvalidStayInDenominator": True,
        "firstSqlEligibility": {"oracleTypes": ["rows"],
            "candidateTools": ["semarail_plan_query", "semarail_prepare_query",
                               "semarail_governed_query"],
            "requiresExactFirstCandidateExecutionMatch": True,
            "unexecutedCandidateStatus": "unverified_not_executed",
            "denialAndBlocked": "not_applicable"},
        "promotion": {"candidateFirstSqlAtLeastBaseline": True,
            "candidateFinalAnswersAboveBaseline": True, "candidateWinsRoundsAtLeast": 2,
            "candidateSilentErrorsNotAboveBaseline": True,
            "candidateOmissionsNotAboveBaseline": True,
            "candidateDeterministicAcrossRounds": True,
            "zeroPermissionLeaks": True, "allRoleDenialsCorrect": True,
            "allRunsAuditedReviewedAndModelObserved": True},
    }
    write_new(target / "evaluator-config.json", evaluator_config)
    manifest = {
        "schemaVersion": 1, "benchmarkId": "business-trust-v1", "runId": run_id,
        "createdAt": utc(), "status": "frozen; execution pending",
        "executionMode": execution_mode,
        "workerDispatchAllowed": execution_mode == "official_campaign",
        "baseline": {"commit": baseline_manifest["baselineCommit"],
                     "path": str(baseline),
                     "sourceManifestSha256": digest(baseline_manifest_path),
                     "sourceHashes": baseline_manifest["sourceHashes"]},
        "baselineRuntime": {"manifestSha256": runtime_freeze["sha256"],
            "python": runtime_freeze["manifest"]["python"],
            "runtimeConstraintsSha256": runtime_freeze["manifest"]["runtimeConstraintsSha256"],
            "pythonFreezeSha256": runtime_freeze["manifest"]["pythonFreezeSha256"]},
        "candidate": {"commit": repo_commit(candidate), "path": str(candidate),
                      "sourceHashes": candidate_sources,
                      "sourceManifestSha256": canonical_hash(candidate_sources),
                      "distribution": distribution_artifact(candidate)},
        "corpusSha256": digest(target / "corpus.json"),
        "promptTemplateSha256": digest(target / "worker-prompt.md"),
        "evaluatorConfigSha256": digest(target / "evaluator-config.json"),
        "evaluatorSourceHashes": {name: candidate_sources[name] for name in candidate_sources
                                  if name.startswith("benchmarks/business-trust-v1/") or name == "scripts/business-trust-v1.mjs"},
        "projectSnapshot": {"source": str(project_source), "files": project_hashes,
                            "sha256": canonical_hash(project_hashes)},
        "legacyProjectSnapshot": {"source": str(project_source),
            "files": legacy_project_hashes, "sha256": canonical_hash(legacy_project_hashes)},
        "checkedProjectSnapshot": {"source": "baseline HR project plus published business-trust checks",
            "files": checked_project_hashes, "sha256": canonical_hash(checked_project_hashes)},
        "oracleResultsSha256": digest(target / "oracle-results.json"),
        "dispatchNoncesSha256": digest(target / "dispatch-nonces.json"),
        "rolePolicyHashes": role_hashes,
        "data": {"manifestPath": str(data_manifest), "manifestSha256": manifest_hash,
                 "databaseUrlEnv": database_url_env,
                 "postgresHost": connection["host"], "postgresPort": connection["port"],
                 "database": connection["database"]},
        "model": {"requested": model, "reasoningRequested": reasoning,
                  "observedEvidenceRequired": True,
                  "driftOrUnobservableBlocksPromotion": True},
        "runtime": {"python": sys.version.split()[0], "wrenTarget": "0.13.2",
                    "baselineAndCandidateUseSameProject": True,
                    "legacyRegressionUsesUnmodifiedProject": True,
                    "contextVersion": 2, "requiredRetrievalBackend": "hybrid",
                    "embedding": embedding_freeze,
                    "embeddingOffline": True},
        "ports": {"base": port_base, "baselineCore": port_base,
                  "baselineMcp": port_base + 1, "candidateCore": port_base + 2,
                  "candidateMcp": port_base + 3, "broker": port_base + 4},
        "scheduleSeed": SCHEDULE_SEED, "scheduleSha256": canonical_hash(schedule),
        "maxConcurrent": MAX_CONCURRENT, "maxToolCalls": MAX_CALLS,
        "deadlineSeconds": DEADLINE_SECONDS,
        "blindness": "fresh independent Codex processes; no group assignment or oracle; visible tool-call audit is not OS isolation",
    }
    if recovery_protocol is not None:
        manifest["recoveryProtocol"] = dict(recovery_checkpoint.RECOVERY_PROTOCOL)
    write_new(target / "schedule.json", schedule)
    write_new(target / "manifest.json", manifest)
    for entry in schedule:
        record_path = target / "records" / entry["id"] / "0000.json"
        write_new(record_path, {**entry, "schemaVersion": 1, "status": "missing",
                                "reason": "not_started", "events": [],
                                "audit": {"status": "unverified"}})
    for group in GROUPS:
        legacy_project = target / "legacy-projects" / group
        if not legacy_project.exists():
            shutil.copytree(target / "project-template", legacy_project)
        checked_project = target / "projects" / group
        if not checked_project.exists():
            shutil.copytree(target / "checked-project-template", checked_project)
    return target


async def _read_mcp_result(result: Any) -> dict[str, Any]:
    wire = result.model_dump(mode="json", exclude_none=True)
    payload = result.structuredContent
    if payload is None:
        text = "\n".join(item.text for item in result.content if hasattr(item, "text"))
        try:
            payload = json.loads(text)
        except ValueError:
            position = text.find("{")
            try:
                payload = json.loads(text[position:]) if position >= 0 else None
            except ValueError:
                payload = None
    if not isinstance(payload, dict):
        payload = {"code": "MCP_PROTOCOL_ERROR"}
    if result.isError:
        normalized = {"schemaVersion": 2, "status": "error", "error": payload,
                      "stats": {"returnedRows": 0, "truncated": False}}
    else:
        normalized = payload
    return {"wire": wire, "result": normalized}


async def call_mcp(port: int, token: str, tool: str,
                   arguments: dict[str, Any]) -> dict[str, Any]:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=45) as client:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await asyncio.wait_for(session.call_tool(tool, arguments), timeout=45)
                return await _read_mcp_result(result)


async def discover_tools(port: int, token: str) -> list[dict[str, Any]]:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=45) as client:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                response = await asyncio.wait_for(session.list_tools(), timeout=45)
                found = []
                for tool in response.tools:
                    if tool.name in ALLOWED_REQUIRED | ALLOWED_OPTIONAL:
                        found.append({"name": tool.name, "description": tool.description or "",
                                      "inputSchema": tool.inputSchema})
                return found


def _http_value(url: str, *, token: str, method: str = "GET",
                body: dict[str, Any] | None = None) -> Any:
    import urllib.error
    import urllib.request
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.loads(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        raise RuntimeError("authenticated local Core request failed") from None
    return value


def _http_json(url: str, *, token: str, method: str = "GET",
               body: dict[str, Any] | None = None) -> dict[str, Any]:
    value = _http_value(url, token=token, method=method, body=body)
    if not isinstance(value, dict):
        raise RuntimeError("Core returned a non-object response")
    return value


def _admin(core_port: int, token: str, path: str, *, method: str = "GET",
           body: dict[str, Any] | None = None) -> dict[str, Any]:
    return _http_json(f"http://127.0.0.1:{core_port}{path}", token=token, method=method, body=body)


def bootstrap_group(core_port: int, admin_token: str, corpus: dict[str, Any],
                    postgres: dict[str, Any]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    connection = {key: postgres[key] for key in ("host", "port", "database", "user", "password")}
    if postgres.get("ssl_mode"):
        connection["ssl_mode"] = postgres["ssl_mode"]
    datasource = _admin(core_port, admin_token, "/api/datasources", method="POST", body={
        "name": "Business trust benchmark", "type": "postgres",
        "connection": connection,
    })
    if not isinstance(datasource.get("id"), str):
        raise RuntimeError("benchmark datasource setup failed")
    _admin(core_port, admin_token, f"/api/datasources/{datasource['id']}/activate", method="POST", body={})
    keys: dict[str, str] = {}
    resources: dict[str, dict[str, str]] = {"_datasource": {"id": datasource["id"]}}
    actor_attributes = {
        "employee": {"employeeId": 1}, "department_manager": {"employeeId": 627},
        "hrbp": {"regionCodes": ["APAC"]}, "compensation_admin": {"regionCodes": ["APAC"]},
        "hr_director": {},
    }
    for actor, attributes in actor_attributes.items():
        account = _admin(core_port, admin_token, "/api/v1/access/service-accounts", method="POST",
                         body={"name": f"BTV {actor}", "attributes": attributes})
        policy = _admin(core_port, admin_token, "/api/v1/access/policies", method="POST",
                        body={"name": f"BTV {actor}", "document": role_policy(actor, datasource["id"])})
        _admin(core_port, admin_token, "/api/v1/access/policy-bindings", method="POST",
               body={"subjectId": account["id"], "policyId": policy["id"]})
        issued = _admin(core_port, admin_token,
            f"/api/v1/access/service-accounts/{account['id']}/keys", method="POST", body={"label": "benchmark"})
        key, credential = issued.get("apiKey"), issued.get("credential", {}).get("id")
        if not isinstance(key, str) or not isinstance(credential, str):
            raise RuntimeError("benchmark actor setup failed")
        keys[actor] = key
        resources[actor] = {"account": account["id"], "policy": policy["id"], "credential": credential}
    return keys, resources


def load_module(name: str, path: Path) -> Any:
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("benchmark runtime module unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Host:
    """Trusted local coordinator. Authentication tokens remain in process memory."""
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.manifest = read(self.directory / "manifest.json")
        self.questions = {item["id"]: item for item in read(self.directory / "corpus.json")["questions"]}
        self.schedule_list = read(self.directory / "schedule.json")
        self.schedule = {item["id"]: item for item in self.schedule_list}
        self.dispatch_nonces = read(self.directory / "dispatch-nonces.json")
        self.records: dict[str, dict[str, Any]] = {}
        self.started: dict[str, float] = {}
        self.locks = {job: threading.RLock() for job in self.schedule}
        self.guard = threading.RLock()
        self.processes: list[subprocess.Popen[bytes]] = []
        self.keys: dict[str, dict[str, str]] = {}
        self.admin_tokens: dict[str, str] = {}
        ports = self.manifest["ports"]
        self.core_ports = {"baseline": ports["baselineCore"], "candidate": ports["candidateCore"]}
        self.mcp_ports = {"baseline": ports["baselineMcp"], "candidate": ports["candidateMcp"]}
        self.group_tools: dict[str, dict[str, list[dict[str, Any]]]] = {g: {} for g in GROUPS}
        self.modules: dict[str, Any] = {}
        self.resource_ids: dict[str, dict[str, dict[str, str]]] = {}
        self.model = self.manifest["model"]["requested"]
        self.reasoning = self.manifest["model"]["reasoningRequested"]
        self.recovery_ledger: dict[str, Any] | None = None
        self.recovery_lock = threading.RLock()
        self.recovery_fault = False
        self.deadline_monotonic: dict[str, float] = {}
        self.expected_tool_capabilities: dict[str, dict[str, list[str]]] = {}
        self._recovery_mac_key: bytes | None = None
        self._wall_clock_anchor: tuple[float, float] | None = None
        self._last_clock_observation: tuple[float, float] | None = None
        self._last_clock_checkpoint_monotonic: float | None = None

    def _verify_frozen_inputs(self, *, allow_execution_records: bool = False) -> None:
        # The CLI normally verifies a run before starting the host, but keep the
        # coordinator fail-closed when invoked directly as a Python API too.
        # This also verifies the fixed schedule, frozen manifest, and the copied
        # approved-baseline manifest before any credentials or services start.
        verify_run_freeze(self.directory)
        validate_corpus(read(self.directory / "corpus.json"))
        if digest(self.directory / "corpus.json") != self.manifest["corpusSha256"]:
            raise ValueError("frozen corpus changed")
        if digest(self.directory / "worker-prompt.md") != self.manifest["promptTemplateSha256"]:
            raise ValueError("frozen worker prompt changed")
        if digest(self.directory / "evaluator-config.json") != self.manifest["evaluatorConfigSha256"]:
            raise ValueError("frozen evaluator config changed")
        if digest(self.directory / "oracle-results.json") != self.manifest["oracleResultsSha256"]:
            raise ValueError("frozen physical oracle results changed")
        for group, config in (("baseline", self.manifest["baseline"]),
                              ("candidate", self.manifest["candidate"])):
            root = Path(config["path"])
            for relative, expected in config["sourceHashes"].items():
                path = root / Path(relative)
                if not path.is_file() or digest(path) != expected:
                    raise ValueError(f"{group} runtime source changed after freeze")
        if digest(Path(self.manifest["data"]["manifestPath"])) != self.manifest["data"]["manifestSha256"]:
            raise ValueError("synthetic dataset manifest changed")
        connection = database_connection(self.manifest["data"]["databaseUrlEnv"])
        expected = self.manifest["data"]
        if any(connection[key] != expected[value] for key, value in (
                ("host", "postgresHost"), ("port", "postgresPort"), ("database", "database"))):
            raise ValueError("database URL target differs from the frozen database identity")
        project_hashes = self.manifest["projectSnapshot"]["files"]
        checked_hashes = self.manifest["checkedProjectSnapshot"]["files"]
        if project_hashes != checked_hashes:
            raise ValueError("shared checked project snapshots disagree")
        checked_template = self.directory / "checked-project-template"
        checked_actual = {path.relative_to(checked_template).as_posix(): digest(path)
                          for path in checked_template.rglob("*")
                          if path.is_file() and ".wren" not in path.parts}
        if checked_actual != checked_hashes:
            raise ValueError("checked project template changed after freeze")
        for group in GROUPS:
            project = self.directory / "projects" / group
            actual = {path.relative_to(project).as_posix(): digest(path)
                      for path in project.rglob("*") if path.is_file() and ".wren" not in path.parts}
            if actual != project_hashes:
                raise ValueError("baseline and candidate semantic project snapshots must stay identical")
        legacy_hashes = self.manifest["legacyProjectSnapshot"]["files"]
        for group in GROUPS:
            project = self.directory / "legacy-projects" / group
            actual = {path.relative_to(project).as_posix(): digest(path)
                      for path in project.rglob("*") if path.is_file() and ".wren" not in path.parts}
            if actual != legacy_hashes:
                raise ValueError("legacy regression project snapshots must stay identical")
        for job in self.schedule:
            versions = sorted((self.directory / "records" / job).glob("[0-9][0-9][0-9][0-9].json"))
            if not allow_execution_records and len(versions) != 1:
                raise ValueError("frozen run already has execution records; create a new run ID")

    def _state_directory(self, phase: str, group: str) -> Path:
        return self.directory / "state" / phase / group

    def _datasource_state_file(self, group: str) -> Path:
        return self._state_directory("business", group) / "datasources.secrets.json"

    @staticmethod
    def _is_reparse_path(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
        except OSError:
            return True

    def _semantic_index_file_hashes(self, group: str) -> dict[str, str]:
        root = self._state_directory("business", group) / "semantic-index"
        if self.directory not in root.parents or not root.is_dir():
            raise ValueError("private semantic index state is unavailable")
        current = root
        while current != self.directory:
            if self._is_reparse_path(current):
                raise ValueError("private semantic index state is unavailable")
            current = current.parent
        hashes: dict[str, str] = {}

        def raise_walk_error(error: OSError) -> None:
            raise error

        try:
            for current_name, directory_names, file_names in os.walk(
                root, topdown=True, followlinks=False, onerror=raise_walk_error
            ):
                current_path = Path(current_name)
                safe_directories = []
                for name in sorted(directory_names):
                    path = current_path / name
                    if self._is_reparse_path(path) or not path.is_dir():
                        raise ValueError("private semantic index state is unavailable")
                    safe_directories.append(name)
                directory_names[:] = safe_directories
                for name in sorted(file_names):
                    path = current_path / name
                    if self._is_reparse_path(path) or not path.is_file():
                        raise ValueError("private semantic index state is unavailable")
                    relative = path.relative_to(root).as_posix()
                    if (not relative or relative.startswith("/") or "\\" in relative
                        or ":" in relative
                        or any(part in {"", ".", ".."} for part in relative.split("/"))):
                        raise ValueError("private semantic index state is unavailable")
                    hashes[relative] = recovery_checkpoint.sha256_file(path)
        except OSError:
            raise ValueError("private semantic index state is unavailable") from None
        if not hashes:
            raise ValueError("private semantic index state is unavailable")
        return hashes

    def _assert_semantic_index_state(self, group: str, expected: Any) -> None:
        if (not isinstance(expected, dict) or not expected
            or any(not isinstance(name, str) or not isinstance(value, str)
                   or not recovery_checkpoint._is_sha256(value)
                   or name.startswith("/") or "\\" in name or ":" in name
                   or any(part in {"", ".", ".."} for part in name.split("/"))
                   for name, value in expected.items())):
            raise ValueError("private semantic index recovery binding is invalid")
        try:
            observed = self._semantic_index_file_hashes(group)
        except ValueError:
            raise ValueError("semantic index state changed after worker-ready checkpoint") from None
        if observed != expected:
            raise ValueError("semantic index state changed after worker-ready checkpoint")

    def _database_connection_fingerprint(self) -> str:
        connection = database_connection(self.manifest["data"]["databaseUrlEnv"])
        if any(connection[key] != self.manifest["data"][manifest_key] for key, manifest_key in (
                ("host", "postgresHost"), ("port", "postgresPort"), ("database", "database"))):
            raise ValueError("database URL target differs from the frozen database identity")
        return recovery_checkpoint.sha256_bytes(recovery_checkpoint.canonical_bytes(connection))

    @staticmethod
    def _expected_actor_attributes() -> dict[str, dict[str, Any]]:
        return {
            "employee": {"employeeId": 1},
            "department_manager": {"employeeId": 627},
            "hrbp": {"regionCodes": ["APAC"]},
            "compensation_admin": {"regionCodes": ["APAC"]},
            "hr_director": {},
        }

    def _binding_projection(self, group: str) -> dict[str, Any]:
        """Return only stable control-plane binding facts; callers persist its hash."""
        core_port = self.core_ports[group]
        admin_token = self.admin_tokens[group]
        resources = self.resource_ids[group]
        datasource_id = resources.get("_datasource", {}).get("id")
        if not isinstance(datasource_id, str) or not datasource_id:
            raise ValueError("worker-ready datasource binding is missing")

        raw_datasources = _http_value(
            f"http://127.0.0.1:{core_port}/api/datasources", token=admin_token)
        if not isinstance(raw_datasources, list) or len(raw_datasources) != 1:
            raise ValueError("worker-ready datasource set changed")
        datasource = raw_datasources[0]
        if not isinstance(datasource, dict) or datasource.get("id") != datasource_id:
            raise ValueError("worker-ready datasource identity changed")
        raw_project = _admin(core_port, admin_token, "/api/project")
        active = raw_project.get("activeDatasource")
        if not isinstance(active, dict) or active.get("id") != datasource_id:
            raise ValueError("worker-ready active datasource changed")
        connection = datasource.get("connection")
        current_db = database_connection(self.manifest["data"]["databaseUrlEnv"])
        if (datasource.get("type") != "postgres" or datasource.get("name") != "Business trust benchmark"
            or datasource.get("hasPassword") is not True or not isinstance(connection, dict)
            or connection.get("host") != current_db["host"]
            or connection.get("port") != current_db["port"]
            or connection.get("database") != current_db["database"]
            or connection.get("user") != current_db["user"]):
            raise ValueError("worker-ready datasource configuration changed")

        project = {
            key: raw_project.get(key) for key in (
                "name", "schemaVersion", "catalog", "schema", "dataSource",
                "modelCount", "revision", "projectExists")
        }
        project["activeDatasourceId"] = datasource_id
        if project["projectExists"] is not True or not isinstance(project["revision"], str):
            raise ValueError("worker-ready semantic project is unavailable")

        # The Core and remote MCP processes own separate semantic-index
        # lifecycle objects. Context delivery builds the MCP-side index after
        # Core has started, so Core's cached /semantic-index response can be
        # cold even when the index used by all authenticated Context calls is
        # ready. Read the status through that same authenticated MCP boundary;
        # resume pins and rechecks the persisted index files so this Context
        # read cannot silently hide a rebuild. Do not trust the Core cache.
        employee_key = self.keys.get(group, {}).get("employee")
        question = self.questions.get("BTV-01", {}).get("public", {}).get("question")
        if not isinstance(employee_key, str) or not employee_key or not isinstance(question, str):
            raise ValueError("worker-ready authenticated MCP semantic status is unavailable")
        try:
            context_response = asyncio.run(call_mcp(
                self.mcp_ports[group], employee_key, "semarail_get_context",
                {"question": question, "context_version": 2}))
        except Exception:
            raise ValueError("worker-ready authenticated MCP semantic status is unavailable") from None
        context = context_response.get("result") if isinstance(context_response, dict) else None
        live_index = context.get("indexStatus") if isinstance(context, dict) else None
        if (not isinstance(context, dict)
            or type(context.get("schemaVersion")) is not int
            or context.get("schemaVersion") != 2
            or context.get("projectRevision") != project["revision"]
            or not isinstance(live_index, dict)):
            raise ValueError("worker-ready authenticated MCP semantic status is unavailable")
        index_projection = {key: live_index.get(key) for key in (
            "status", "backend", "activeRevision", "indexedRevision", "documentCount",
            "embeddingModelId", "embeddingModelVersion", "embeddingDimension",
            "indexBuildVersion")}
        index_projection["revision"] = context["projectRevision"]
        index_projection["vectorAvailable"] = index_projection["backend"] in {"vector", "hybrid"}
        if (index_projection.get("status") != "ready"
            or index_projection.get("backend") != "hybrid"
            or live_index.get("staleReason") is not None
            or index_projection.get("activeRevision") != project["revision"]
            or index_projection.get("indexedRevision") != project["revision"]):
            raise ValueError("worker-ready semantic publication is not indexed")

        raw_versions = _http_value(f"http://127.0.0.1:{core_port}/api/versions", token=admin_token)
        if not isinstance(raw_versions, list) or any(not isinstance(item, dict) for item in raw_versions):
            raise ValueError("worker-ready semantic publication history is unavailable")
        versions = [{key: item.get(key) for key in ("id", "revision", "fileCount")}
                    for item in raw_versions]

        accounts_response = _admin(core_port, admin_token, "/api/v1/access/service-accounts")
        policies_response = _admin(core_port, admin_token, "/api/v1/access/policies")
        accounts = accounts_response.get("items")
        policies = policies_response.get("items")
        if not isinstance(accounts, list) or not isinstance(policies, list):
            raise ValueError("worker-ready role bindings are unavailable")
        expected_attributes = self._expected_actor_attributes()
        role_bindings = {}
        for actor, attributes in expected_attributes.items():
            resource = resources.get(actor)
            if not isinstance(resource, dict):
                raise ValueError("worker-ready role resource is missing")
            account_id = resource.get("account")
            policy_id = resource.get("policy")
            credential_id = resource.get("credential")
            account = next((item for item in accounts
                            if isinstance(item, dict) and item.get("id") == account_id), None)
            policy = next((item for item in policies
                           if isinstance(item, dict) and item.get("id") == policy_id), None)
            if (not isinstance(account, dict) or not isinstance(policy, dict)
                or account.get("name") != f"BTV {actor}"
                or account.get("type") != "service_account"
                or account.get("status") != "active"
                or account.get("attributes") != attributes
                or account.get("policyIds") != [policy_id]
                or policy.get("name") != f"BTV {actor}"
                or policy.get("document") != role_policy(actor, datasource_id)
                or type(policy.get("version")) is not int or policy.get("version") != 1):
                raise ValueError("worker-ready role policy or subject changed")
            credentials = account.get("credentials")
            if (not isinstance(credentials, list) or len(credentials) != 1
                or not isinstance(credentials[0], dict)
                or credentials[0].get("id") != credential_id
                or credentials[0].get("subjectId") != account_id
                or credentials[0].get("revokedAt") is not None
                or credentials[0].get("expiresAt") is not None):
                raise ValueError("worker-ready role credential binding changed")
            role_bindings[actor] = {
                "accountId": account_id,
                "policyId": policy_id,
                "credentialId": credential_id,
                "attributes": attributes,
                "policyVersion": policy["version"],
                "policyDocumentSha256": canonical_hash(policy["document"]),
            }

        return {
            "datasource": {
                "id": datasource_id, "name": datasource.get("name"),
                "type": datasource.get("type"), "connection": connection,
                "hasPassword": datasource.get("hasPassword"),
            },
            "project": project,
            "semanticIndex": index_projection,
            "publishedVersions": versions,
            "roleBindings": role_bindings,
        }

    def _recovery_tool_capability_evidence(self) -> dict[str, Any]:
        expected_groups = getattr(self, "expected_tool_capabilities", {})
        observed_groups = getattr(self, "group_tools", {})
        groups: dict[str, Any] = {}
        passed = True
        observed_role_count = 0
        for group in GROUPS:
            groups[group] = {}
            expected_actors = expected_groups.get(group, {})
            observed_actors = observed_groups.get(group, {})
            for actor in self._expected_actor_attributes():
                tools = observed_actors.get(actor)
                if isinstance(tools, list):
                    names = sorted(item.get("name") for item in tools
                                   if isinstance(item, dict) and isinstance(item.get("name"), str))
                    observed = len(names) == len(tools) and len(set(names)) == len(names)
                else:
                    names, observed = [], False
                expected = expected_actors.get(actor)
                matches = (observed and isinstance(expected, list)
                           and set(names) == set(expected))
                observed_role_count += int(observed)
                passed = passed and bool(matches)
                groups[group][actor] = {
                    "observed": observed,
                    "toolCount": len(names) if observed else None,
                    "toolNamesSha256": canonical_hash(names) if observed else None,
                    "matchesOriginal": bool(matches),
                }
        return {
            "schemaVersion": 1,
            "kind": "business-trust-v1-recovery-mcp-capabilities",
            "groups": groups,
            "observedRoleCount": observed_role_count,
            "requiredRoleCount": len(GROUPS) * len(self._expected_actor_attributes()),
            "passed": passed,
        }

    def _validated_dispatch_marker(self) -> dict[str, Any] | None:
        app_path = self.directory / "app-dispatch-start.json"
        runner_path = self.directory / "runner-start.json"
        if app_path.is_symlink() or runner_path.is_symlink():
            raise ValueError("worker dispatch marker cannot be a symbolic link")
        if app_path.is_file() == runner_path.is_file():
            if app_path.exists() or runner_path.exists():
                raise ValueError("worker dispatch setup is missing or ambiguous")
            return None
        path = app_path if app_path.is_file() else runner_path
        setup = recovery_checkpoint.read_json(path)
        schedule_ids = [item["id"] for item in self.schedule_list]
        common = {
            "runId": self.manifest["runId"],
            "scheduleSha256": self.manifest["scheduleSha256"],
            "promptTemplateSha256": self.manifest["promptTemplateSha256"],
            "brokerPort": self.manifest["ports"]["broker"],
            "maxConcurrent": MAX_CONCURRENT,
            "deadlineSeconds": DEADLINE_SECONDS,
        }
        if not isinstance(setup, dict) or any(setup.get(key) != value for key, value in common.items()):
            raise ValueError("worker dispatch marker differs from the frozen protocol")
        if path == app_path:
            if (setup.get("dispatchNoncesSha256") != self.manifest["dispatchNoncesSha256"]
                or setup.get("modelRequested") != self.model
                or setup.get("reasoningRequested") != self.reasoning
                or setup.get("actualModelObserved") is not False
                or setup.get("actualReasoningObserved") is not False):
                raise ValueError("app dispatch marker differs from the frozen protocol")
            selected = setup.get("selectedJobIds")
            allowed_modes = {
                "Codex app fresh-session worker prompts",
                "Codex app fresh-session worker prompts via public-file bootstrap",
                "Codex app fresh-session worker prompts via stored transport bootstrap",
            }
            mode = setup.get("dispatchMode")
            expected_client = (Path(self.manifest["candidate"]["path"])
                               / "benchmarks/business-trust-v1/worker_client.py").resolve()
            if (mode not in allowed_modes or not isinstance(selected, list)
                or not selected or len(set(selected)) != len(selected)
                or not set(selected) <= set(schedule_ids)
                or setup.get("workerClientPath") != str(expected_client)):
                raise ValueError("app dispatch selection or client binding is invalid")
            prompt_hashes = setup.get("promptHashes")
            if not isinstance(prompt_hashes, dict) or set(prompt_hashes) != set(selected):
                raise ValueError("app dispatch prompt hashes are invalid")
            for job in selected:
                if "bootstrapMode" not in setup:
                    prompt_file = self.directory / "app-prompts" / f"{job}.md"
                    if (not prompt_file.is_file() or prompt_file.is_symlink()
                        or digest(prompt_file) != prompt_hashes[job]):
                        raise ValueError("app dispatch prompt changed after export")
                else:
                    public_files = setup.get("publicPromptFiles")
                    launcher_files = setup.get("launcherFiles")
                    launcher_hashes = setup.get("launcherHashes")
                    launch_file = self.directory / "app-prompts" / f"{job}.md"
                    public_item = public_files.get(job) if isinstance(public_files, dict) else None
                    launcher_item = launcher_files.get(job) if isinstance(launcher_files, dict) else None
                    if (not isinstance(public_item, dict) or not isinstance(launcher_item, dict)
                        or launcher_hashes.get(job) != launcher_item.get("sha256")
                        or not launch_file.is_file() or launch_file.is_symlink()
                        or digest(launch_file) != launcher_hashes.get(job)):
                        raise ValueError("app dispatch launcher changed after export")
                    public_prompt = Path(public_item.get("path", ""))
                    if (not public_prompt.is_absolute() or not public_prompt.is_file()
                        or public_prompt.is_symlink() or digest(public_prompt) != prompt_hashes[job]
                        or public_item.get("sha256") != prompt_hashes[job]):
                        raise ValueError("app dispatch public prompt changed after export")
            file_name = path.name
        else:
            # CLI runner markers bind the nonce set through the frozen prompt
            # template and generated prompt bodies rather than a marker field.
            if (setup.get("dispatchNoncesSha256") is not None
                or setup.get("model") != self.model
                or setup.get("reasoning") != self.reasoning):
                raise ValueError("CLI dispatch marker differs from the frozen protocol")
            selected = schedule_ids
            mode = "codex-cli-runner-v1"
            expected_client = (Path(self.manifest["candidate"]["path"])
                               / "benchmarks/business-trust-v1/worker_client.py").resolve()
            if (setup.get("workerClientPath") != str(expected_client)
                or setup.get("transportPath") != expected_client.as_posix()):
                raise ValueError("CLI dispatch client binding is invalid")
            file_name = path.name
        worker_root_raw = setup.get("workerRoot")
        directories = setup.get("workerDirectories")
        python_raw = setup.get("pythonPath")
        if (not isinstance(worker_root_raw, str) or not isinstance(directories, dict)
            or set(directories) != set(schedule_ids) or not isinstance(python_raw, str)):
            raise ValueError("worker dispatch workspace binding is invalid")
        worker_root = Path(worker_root_raw)
        if (not worker_root.is_absolute() or not worker_root.is_dir() or worker_root.is_symlink()
            or worker_root.resolve().is_relative_to(Path(self.manifest["candidate"]["path"]).resolve())):
            raise ValueError("worker dispatch workspace is unavailable")
        if Path(python_raw).resolve() != Path(sys.executable).resolve() or not Path(python_raw).is_file():
            raise ValueError("worker Python executable differs from the coordinator runtime")
        resolved_dirs = set()
        for job, raw_directory in directories.items():
            if not isinstance(raw_directory, str):
                raise ValueError("worker dispatch directory is invalid")
            directory = Path(raw_directory)
            if (not directory.is_absolute() or directory.is_symlink() or not directory.is_dir()
                or directory.resolve().parent != worker_root.resolve()
                or directory.name != job or directory.resolve() in resolved_dirs):
                raise ValueError("worker dispatch directory binding changed")
            resolved_dirs.add(directory.resolve())
        if path == app_path and not set(selected) <= set(directories):
            raise ValueError("app dispatch directory selection is invalid")
        return {"file": file_name, "sha256": digest(path), "mode": mode,
                "selectedJobIds": list(selected)}

    def _bind_dispatch_marker(self, job: str) -> None:
        if self.recovery_ledger is None or self._recovery_mac_key is None:
            raise ValueError("worker-ready recovery checkpoint is unavailable")
        self.observe_recovery_clock()
        with self.recovery_lock:
            marker = self._validated_dispatch_marker()
            if marker is None or job not in marker["selectedJobIds"]:
                raise ValueError("job is outside the frozen worker dispatch selection")
            bound = self.recovery_ledger.get("dispatch")
            if bound is not None:
                if bound != marker:
                    raise ValueError("worker dispatch marker changed after checkpoint")
                return
            next_value = recovery_checkpoint.next_ledger(self.recovery_ledger, dispatch=marker)
            next_value = recovery_checkpoint.seal_ledger(next_value, self._recovery_mac_key)
            recovery_checkpoint.write_ledger(self.directory, next_value)
            self.recovery_ledger = next_value

    def _load_recovery_records(self) -> None:
        if self.recovery_ledger is None or self._recovery_mac_key is None:
            raise ValueError("worker-ready recovery checkpoint is unavailable")
        ledger = self.recovery_ledger
        expected_jobs = set(self.schedule)
        if set(ledger.get("records", {})) != expected_jobs:
            raise ValueError("recovery ledger does not cover the frozen schedule")
        observed_heads: dict[str, dict[str, Any]] = {}
        consumed: dict[str, dict[str, Any]] = {}
        loaded_records: dict[str, dict[str, Any]] = {}
        running = 0
        ahead_count = 0
        records_root = self.directory / "records"
        if records_root.is_symlink() or not records_root.is_dir():
            raise ValueError("frozen record root is unavailable")
        for job, entry in self.schedule.items():
            folder = records_root / job
            if not folder.is_dir() or folder.is_symlink():
                raise ValueError("frozen record history is unavailable")
            if any(path.is_symlink() for path in folder.iterdir()):
                raise ValueError("frozen record history contains a symbolic link")
            paths = sorted(folder.glob("[0-9][0-9][0-9][0-9].json"))
            if (not paths
                or [path.name for path in paths] != [f"{i:04d}.json" for i in range(len(paths))]
                or {path.name for path in folder.iterdir()} != {path.name for path in paths}):
                raise ValueError("record history is not a contiguous frozen prefix")
            if any(path.is_symlink() or not path.is_file() for path in paths):
                raise ValueError("frozen record history contains an invalid file")
            baseline = recovery_checkpoint.read_private_json(paths[0])
            if (not isinstance(baseline, dict)
                or any(baseline.get(key) != entry.get(key) for key in ("id", "questionId", "group", "round"))
                or baseline.get("status") != "missing"):
                raise ValueError("frozen record baseline changed")
            previous = recovery_checkpoint.sha256_file(paths[0])
            latest: dict[str, Any] = baseline
            for index, path in enumerate(paths[1:], 1):
                record = recovery_checkpoint.read_private_json(path)
                checkpoint = record.get("_checkpoint") if isinstance(record, dict) else None
                if (not isinstance(record, dict) or not isinstance(checkpoint, dict)
                    or set(checkpoint) != {"schemaVersion", "revision", "previousSha256"}
                    or type(checkpoint.get("schemaVersion")) is not int
                    or checkpoint.get("schemaVersion") != 1
                    or type(checkpoint.get("revision")) is not int
                    or checkpoint.get("revision") != index
                    or checkpoint.get("previousSha256") != previous
                    or any(record.get(key) != entry.get(key) for key in ("id", "questionId", "group", "round"))):
                    raise ValueError("record history hash chain is invalid")
                previous = recovery_checkpoint.sha256_file(path)
                latest = record
            actual_head = {"revision": len(paths) - 1, "sha256": previous}
            ledger_head = ledger["records"][job]
            if ledger_head == actual_head:
                pass
            elif (actual_head["revision"] == ledger_head.get("revision", -2) + 1
                  and paths[-1].name == f"{actual_head['revision']:04d}.json"):
                # A single new record is reconcilable only if the committed
                # prefix itself is byte-for-byte unchanged. Validating the new
                # file's chain against an altered prior head is insufficient:
                # an attacker could rewrite that head, rechain one record, and
                # otherwise make the replacement look like a crash-gap append.
                prior_snapshot = {
                    "revision": actual_head["revision"] - 1,
                    "sha256": recovery_checkpoint.sha256_file(paths[-2]),
                }
                latest_checkpoint = latest.get("_checkpoint")
                if (prior_snapshot != ledger_head
                    or not isinstance(latest_checkpoint, dict)
                    or latest_checkpoint.get("previousSha256") != ledger_head.get("sha256")):
                    raise ValueError("record history changed before the uncommitted append")
                ahead_count += 1
            else:
                raise ValueError("record history differs from the committed recovery ledger")
            observed_heads[job] = actual_head
            if actual_head["revision"] > 0:
                if latest.get("status") not in {
                    "running", "complete", "timeout", "invalid", "infrastructure_error",
                }:
                    raise ValueError("recovered record status is invalid")
                started_epoch = latest.get("startedAtEpoch")
                deadline_epoch = latest.get("deadlineAtEpoch")
                if (type(started_epoch) not in (int, float)
                    or type(deadline_epoch) not in (int, float)
                    or not math.isfinite(started_epoch) or not math.isfinite(deadline_epoch)
                    or deadline_epoch != started_epoch + DEADLINE_SECONDS):
                    raise ValueError("recovered worker deadline evidence is invalid")
                consumed[job] = {
                    "schedulePosition": list(self.schedule).index(job),
                    "startedAtEpoch": started_epoch,
                    "deadlineAtEpoch": deadline_epoch,
                    "status": latest["status"],
                }
                loaded_records[job] = latest
                if latest["status"] == "running":
                    running += 1
        if ahead_count > 1:
            raise ValueError("more than one durable record is ahead of the recovery ledger")
        if ahead_count == 0 and ledger.get("consumedJobs") != consumed:
            raise ValueError("recovery consumed positions differ from the record prefix")
        if ahead_count:
            next_value = recovery_checkpoint.next_ledger(
                ledger, records=observed_heads, consumedJobs=consumed)
            next_value = recovery_checkpoint.seal_ledger(next_value, self._recovery_mac_key)
            recovery_checkpoint.write_ledger(self.directory, next_value)
            self.recovery_ledger = next_value
        if running > MAX_CONCURRENT:
            raise ValueError("recovered running positions exceed the frozen concurrency limit")
        if running and self.recovery_ledger.get("dispatch") is None:
            raise ValueError("running records have no frozen dispatch binding")
        self.records = loaded_records
        self.started = {}
        self.deadline_monotonic = {}
        now_wall = recovery_checkpoint.check_wall_clock(self.recovery_ledger)
        now_mono = time.monotonic()
        self._wall_clock_anchor = (now_wall, now_mono)
        self._last_clock_observation = (now_wall, now_mono)
        self._last_clock_checkpoint_monotonic = now_mono
        for job, record in loaded_records.items():
            if record.get("status") != "running":
                continue
            started_epoch = record["startedAtEpoch"]
            deadline_epoch = record["deadlineAtEpoch"]
            elapsed = max(0.0, now_wall - started_epoch)
            self.started[job] = now_mono - elapsed
            self.deadline_monotonic[job] = now_mono + (deadline_epoch - now_wall)

    def observe_recovery_clock(self) -> None:
        if getattr(self, "recovery_ledger", None) is None or getattr(self, "_recovery_mac_key", None) is None:
            return
        try:
            with self.recovery_lock:
                now_wall = time.time()
                now_mono = time.monotonic()
                previous_observation = self._last_clock_observation
                if previous_observation is not None and now_wall < previous_observation[0]:
                    raise recovery_checkpoint.CheckpointError("wall clock moved backwards")
                recovery_checkpoint.check_wall_clock(self.recovery_ledger, now_wall)
                self._last_clock_observation = (now_wall, now_mono)
                last_checkpoint = self._last_clock_checkpoint_monotonic
                if last_checkpoint is None or now_mono - last_checkpoint >= 15.0:
                    next_value = recovery_checkpoint.next_ledger(self.recovery_ledger)
                    next_value["lastWallEpoch"] = now_wall
                    next_value = recovery_checkpoint.seal_ledger(next_value, self._recovery_mac_key)
                    recovery_checkpoint.write_ledger(self.directory, next_value)
                    self.recovery_ledger = next_value
                    self._last_clock_checkpoint_monotonic = now_mono
        except Exception:
            self.recovery_fault = True
            raise

    def _assert_recovery_ports_free(self) -> None:
        for port in (*self.core_ports.values(), *self.mcp_ports.values()):
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise ValueError("a frozen Core or MCP port is occupied; resume will not adopt it") from None
            finally:
                probe.close()

    def _write_worker_ready_checkpoint(self) -> None:
        if not recovery_checkpoint.protocol_supported(self.manifest):
            return
        recovery_dir = self.directory / ".recovery"
        if recovery_dir.exists() or recovery_checkpoint.ledger_path(self.directory).exists():
            raise ValueError("worker-ready recovery checkpoint already exists")
        if (self.directory / "app-dispatch-start.json").exists() or (self.directory / "runner-start.json").exists():
            raise ValueError("worker dispatch must be exported only after worker-ready checkpoint")
        marker = self._validated_dispatch_marker()
        if marker is not None:
            raise ValueError("worker dispatch marker appeared before worker-ready checkpoint")
        gate_hashes = recovery_checkpoint.gate_hashes(self.directory, WORKER_READY_GATES)
        record_hashes: dict[str, str] = {}
        for job in self.schedule:
            folder = self.directory / "records" / job
            paths = sorted(folder.glob("[0-9][0-9][0-9][0-9].json"))
            if len(paths) != 1 or paths[0].name != "0000.json" or paths[0].is_symlink():
                raise ValueError("fresh worker-ready checkpoint requires untouched frozen records")
            record_hashes[job] = recovery_checkpoint.sha256_file(paths[0])
        binding_hashes = {}
        private_state_hashes = {}
        semantic_index_hashes = {}
        for group in GROUPS:
            semantic_index_hashes[group] = self._semantic_index_file_hashes(group)
            projection = self._binding_projection(group)
            if self._semantic_index_file_hashes(group) != semantic_index_hashes[group]:
                raise ValueError("semantic index state changed during worker-ready binding")
            binding_hashes[group] = recovery_checkpoint.sha256_bytes(
                recovery_checkpoint.canonical_bytes(projection))
            state_file = self._datasource_state_file(group)
            if not state_file.is_file() or state_file.is_symlink():
                raise ValueError("private datasource state is unavailable for recovery")
            private_state_hashes[group] = recovery_checkpoint.sha256_file(state_file)
        capability_path = self.directory / "tool-capabilities.json"
        capabilities = read(capability_path)
        groups = capabilities.get("groups") if isinstance(capabilities, dict) else None
        if not isinstance(groups, dict):
            raise ValueError("authenticated MCP capability evidence is invalid")
        expected_capabilities = {}
        for group in GROUPS:
            actors = groups.get(group)
            if not isinstance(actors, dict):
                raise ValueError("authenticated MCP capability evidence is incomplete")
            expected_capabilities[group] = {}
            for actor in self._expected_actor_attributes():
                names = actors.get(actor)
                if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                    raise ValueError("authenticated MCP capability evidence is incomplete")
                expected_capabilities[group][actor] = list(names)
        mac_key = secrets.token_bytes(32)
        private_payload = {
            "schemaVersion": 1,
            "adminTokens": dict(self.admin_tokens),
            "keys": {group: dict(keys) for group, keys in self.keys.items()},
            "resourceIds": {group: dict(resources) for group, resources in self.resource_ids.items()},
            "expectedToolCapabilities": expected_capabilities,
            "privateStateHashes": private_state_hashes,
            "semanticIndexHashes": semantic_index_hashes,
            "databaseConnectionFingerprint": self._database_connection_fingerprint(),
            "ledgerMacKeyHex": mac_key.hex(),
        }
        run_id = self.manifest["runId"]
        manifest_sha256 = digest(self.directory / "manifest.json")
        private_blob_sha256 = recovery_checkpoint.write_secret_store(self.directory,
            run_id=run_id, manifest_sha256=manifest_sha256, secrets_payload=private_payload)
        ledger = recovery_checkpoint.make_worker_ready_ledger(
            run_id=run_id, manifest_sha256=manifest_sha256,
            secret_sha256=private_blob_sha256,
            schedule_sha256=self.manifest["scheduleSha256"],
            dispatch_nonces_sha256=self.manifest["dispatchNoncesSha256"],
            dispatch_file="", dispatch_sha256="", gate_hashes=gate_hashes,
            binding_hashes=binding_hashes, record_hashes=record_hashes)
        ledger = recovery_checkpoint.seal_ledger(ledger, mac_key)
        recovery_checkpoint.write_public_json_new(
            recovery_checkpoint.ledger_path(self.directory), ledger)
        self.recovery_ledger = ledger
        self.expected_tool_capabilities = expected_capabilities
        self._recovery_mac_key = mac_key
        now_mono = time.monotonic()
        self._wall_clock_anchor = (ledger["lastWallEpoch"], now_mono)
        self._last_clock_observation = (ledger["lastWallEpoch"], now_mono)
        self._last_clock_checkpoint_monotonic = now_mono

    @staticmethod
    def _wait_port(port: int, process: subprocess.Popen[bytes]) -> None:
        until = time.monotonic() + 90
        while time.monotonic() < until:
            if process.poll() is not None:
                raise RuntimeError("a benchmark Core or MCP process exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.3):
                    return
            except OSError:
                time.sleep(.25)
        raise TimeoutError("benchmark process startup timed out")

    def _launch_group(self, group: str, *, phase: str, resume: bool = False) -> None:
        root = Path(self.manifest[group]["path"])
        core_port = self.core_ports[group]
        mcp_port = self.mcp_ports[group]
        if phase not in {"legacy", "business"}:
            raise ValueError("unknown benchmark runtime phase")
        project_root = "legacy-projects" if phase == "legacy" else "projects"
        project = self.directory / project_root / group
        state = self.directory / "state" / phase / group
        wren_home = self.directory / "wren-home" / phase / group
        if resume:
            if (not state.is_dir() or state.is_symlink()
                or not wren_home.is_dir() or wren_home.is_symlink()):
                raise ValueError("worker-ready benchmark state is unavailable")
            token = self.admin_tokens.get(group)
            keys = self.keys.get(group)
            resources = self.resource_ids.get(group)
            if (not isinstance(token, str) or not isinstance(keys, dict)
                or not isinstance(resources, dict)):
                raise ValueError("private worker-ready credentials are incomplete")
        else:
            if state.exists() or wren_home.exists():
                raise ValueError("benchmark state already exists; this frozen run cannot be reset")
            state.mkdir(parents=True, exist_ok=False)
            wren_home.mkdir(parents=True, exist_ok=False)
            token = secrets.token_urlsafe(36)
            self.admin_tokens[group] = token
        env = os.environ.copy()
        for name in list(env):
            if (name.startswith(("BTV_", "BUSINESS_TRUST_", "PG", "POSTGRES_", "SEMARAIL_"))
                or name in {"DATABASE_URL", "WREN_DATABASE_URL"}):
                env.pop(name, None)
        sidecar = root / "python/sidecar"
        app = root / "apps/semantic-console"
        env["PYTHONPATH"] = os.pathsep.join([str(app), str(sidecar)])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["SEMARAIL_API_TOKEN"] = token
        env["WREN_HOME"] = str(wren_home)
        configure_embedding_environment(env)
        env.pop("SEMARAIL_DATABASE_URL", None)
        env.pop("DATABASE_URL", None)
        env.pop("SEMARAIL_CONTROL_DATABASE_URL", None)
        core = subprocess.Popen([sys.executable, "-m", "server", "--host", "127.0.0.1",
            "--port", str(core_port), "--project-dir", str(project), "--state-dir", str(state)],
            cwd=app, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.processes.append(core)
        self._wait_port(core_port, core)
        if resume:
            # Authenticate against the existing Core before attaching its MCP
            # process. This is read-only and never recreates subjects or keys.
            _admin(core_port, token, "/api/project")
        else:
            corpus = read(root / "benchmarks/hr-enterprise/golden-questions.json")
            postgres = database_connection(self.manifest["data"]["databaseUrlEnv"])
            if any(postgres[key] != self.manifest["data"][manifest_key] for key, manifest_key in (
                    ("host", "postgresHost"), ("port", "postgresPort"), ("database", "database"))):
                raise ValueError("database URL target differs from the frozen database identity")
            keys, resources = bootstrap_group(core_port, token, corpus, postgres)
            self.keys[group] = keys
            self.resource_ids[group] = resources
        remote = subprocess.Popen([sys.executable, "-m", "server.remote_mcp", "--project", str(project),
            "--state-dir", str(state), "--port", str(mcp_port)], cwd=app, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.processes.append(remote)
        self._wait_port(mcp_port, remote)
        for actor, key in keys.items():
            tools = asyncio.run(discover_tools(mcp_port, key))
            names = {item["name"] for item in tools}
            self.group_tools[group][actor] = tools
            missing = ALLOWED_REQUIRED - names
            if missing:
                raise RuntimeError(f"required authenticated MCP tools unavailable for {group}")
            if resume:
                expected = self.expected_tool_capabilities.get(group, {}).get(actor)
                if expected is None or names != set(expected):
                    raise RuntimeError(f"authenticated MCP tool catalog changed for {group}")
        # Admin and issued API keys are never written to evidence files.
        self.modules[group] = {"adminToken": token, "corePort": core_port,
                               "mcpPort": mcp_port, "root": root}

    def _stop_phase(self) -> None:
        self.close()
        self.processes.clear()
        self.keys.clear()
        self.resource_ids.clear()
        self.admin_tokens.clear()
        self.modules.clear()
        self.group_tools = {group: {} for group in GROUPS}

    def start(self) -> None:
        if recovery_checkpoint.protocol_supported(self.manifest) and (
            (self.directory / ".recovery").exists()
            or recovery_checkpoint.attempts_path(self.directory).exists()
        ):
            raise ValueError("worker-ready recovery artifacts already exist")
        self._verify_frozen_inputs()
        for group in GROUPS:
            self._launch_group(group, phase="legacy")
        self._run_legacy_regression()
        self._stop_phase()
        for group in GROUPS:
            self._launch_group(group, phase="business")
        self._validate_context_delivery()
        self._validate_execution_checks()
        self._validate_oracles()
        write_new(self.directory / "tool-capabilities.json", {
            "schemaVersion": 1,
            "groups": {group: {actor: [tool["name"] for tool in tools]
                               for actor, tools in actor_tools.items()}
                       for group, actor_tools in self.group_tools.items()},
            "optionalQueryEvidence": {
                group: {actor: "semarail_get_query_evidence" in {item["name"] for item in tools}
                        for actor, tools in actor_tools.items()}
                for group, actor_tools in self.group_tools.items()},
        })
        self._write_worker_ready_checkpoint()

    def resume(self) -> None:
        """Restore only a committed worker-ready run; never repeat bootstrap gates."""
        recovery_checkpoint.require_protocol(self.manifest)
        self._verify_frozen_inputs(allow_execution_records=True)
        manifest_sha256 = digest(self.directory / "manifest.json")
        recovery_dir = recovery_checkpoint.ledger_path(self.directory).parent
        if (recovery_dir.is_symlink() or not recovery_dir.is_dir()
            or recovery_checkpoint.ledger_path(self.directory).is_symlink()
            or recovery_checkpoint.secret_path(self.directory).is_symlink()):
            raise ValueError("worker-ready checkpoint contains a symbolic link")
        ledger = recovery_checkpoint.read_ledger(self.directory)
        private = recovery_checkpoint.read_secret_store(
            self.directory, run_id=self.manifest["runId"],
            manifest_sha256=manifest_sha256,
            expected_ciphertext_sha256=ledger.get("privateBlobSha256", ""))
        private_fields = {
            "schemaVersion", "adminTokens", "keys", "resourceIds",
            "expectedToolCapabilities", "privateStateHashes", "semanticIndexHashes",
            "databaseConnectionFingerprint", "ledgerMacKeyHex",
        }
        if (not isinstance(private, dict) or set(private) != private_fields
            or type(private.get("schemaVersion")) is not int or private["schemaVersion"] != 1):
            raise ValueError("private worker-ready checkpoint is invalid")
        try:
            mac_key = bytes.fromhex(private["ledgerMacKeyHex"])
        except (TypeError, ValueError):
            raise ValueError("private worker-ready checkpoint is invalid") from None
        if len(mac_key) != 32:
            raise ValueError("private worker-ready checkpoint is invalid")
        recovery_checkpoint.validate_worker_ready_ledger(
            ledger, run_id=self.manifest["runId"], manifest_sha256=manifest_sha256)
        recovery_checkpoint.verify_ledger_mac(ledger, mac_key)
        if (ledger.get("scheduleSha256") != self.manifest["scheduleSha256"]
            or ledger.get("dispatchNoncesSha256") != self.manifest["dispatchNoncesSha256"]
            or set(ledger.get("gateHashes", {})) != set(WORKER_READY_GATES)
            or set(ledger.get("bindingHashes", {})) != set(GROUPS)):
            raise ValueError("worker-ready checkpoint is bound to different frozen inputs")
        if recovery_checkpoint.gate_hashes(self.directory, WORKER_READY_GATES) != ledger["gateHashes"]:
            raise ValueError("an original worker-ready gate changed after checkpoint")
        if (not isinstance(private.get("databaseConnectionFingerprint"), str)
            or private["databaseConnectionFingerprint"] != self._database_connection_fingerprint()):
            raise ValueError("worker-ready database connection identity changed")

        group_set = set(GROUPS)
        actors = set(self._expected_actor_attributes())
        for field in ("adminTokens", "keys", "resourceIds", "expectedToolCapabilities",
                      "privateStateHashes", "semanticIndexHashes"):
            if not isinstance(private.get(field), dict) or set(private[field]) != group_set:
                raise ValueError("private worker-ready group bindings are incomplete")
        self.admin_tokens = private["adminTokens"]
        self.keys = private["keys"]
        self.resource_ids = private["resourceIds"]
        self.expected_tool_capabilities = private["expectedToolCapabilities"]
        for group in GROUPS:
            if (not isinstance(self.admin_tokens.get(group), str)
                or not isinstance(self.keys.get(group), dict)
                or set(self.keys[group]) != actors
                or not all(isinstance(key, str) and key for key in self.keys[group].values())
                or not isinstance(self.resource_ids.get(group), dict)
                or set(self.resource_ids[group]) != actors | {"_datasource"}
                or not isinstance(self.expected_tool_capabilities.get(group), dict)
                or set(self.expected_tool_capabilities[group]) != actors
                or not isinstance(private["privateStateHashes"].get(group), str)):
                raise ValueError("private worker-ready role bindings are invalid")
            for actor in actors:
                resource = self.resource_ids[group][actor]
                capabilities = self.expected_tool_capabilities[group][actor]
                if (not isinstance(resource, dict)
                    or set(resource) != {"account", "policy", "credential"}
                    or not all(isinstance(value, str) and value for value in resource.values())
                    or not isinstance(capabilities, list)
                    or any(not isinstance(name, str) for name in capabilities)):
                    raise ValueError("private worker-ready role binding is invalid")
            datasource = self.resource_ids[group]["_datasource"]
            if not isinstance(datasource, dict) or set(datasource) != {"id"} or not isinstance(datasource.get("id"), str):
                raise ValueError("private worker-ready datasource binding is invalid")
            state = self._state_directory("business", group)
            wren_home = self.directory / "wren-home" / "business" / group
            if (not state.is_dir() or state.is_symlink()
                or not wren_home.is_dir() or wren_home.is_symlink()):
                raise ValueError("worker-ready runtime state is unavailable")
            private_state_file = self._datasource_state_file(group)
            if (not private_state_file.is_file() or private_state_file.is_symlink()
                or recovery_checkpoint.sha256_file(private_state_file) != private["privateStateHashes"][group]):
                raise ValueError("private datasource state changed after checkpoint")
            self._assert_semantic_index_state(group, private["semanticIndexHashes"][group])
            if not recovery_checkpoint._is_sha256(ledger["bindingHashes"].get(group)):
                raise ValueError("worker-ready role/publication hash is invalid")

        current_dispatch = self._validated_dispatch_marker()
        bound_dispatch = ledger.get("dispatch")
        if bound_dispatch != current_dispatch:
            if bound_dispatch is not None or current_dispatch is None:
                raise ValueError("worker dispatch marker changed after checkpoint")
            # Dispatch export is intentionally a one-shot action performed only
            # after Host readiness; bind its complete validated marker before
            # recovering any consumed worker positions.
        self.recovery_ledger = ledger
        self._recovery_mac_key = mac_key
        if bound_dispatch is None and current_dispatch is not None:
            self._bind_dispatch_marker(current_dispatch["selectedJobIds"][0])
        self._load_recovery_records()
        dispatch = self.recovery_ledger.get("dispatch")
        consumed_jobs = set(self.recovery_ledger.get("consumedJobs", {}))
        if consumed_jobs and dispatch is None:
            raise ValueError("consumed worker positions have no dispatch binding")
        if dispatch is not None and not consumed_jobs <= set(dispatch["selectedJobIds"]):
            raise ValueError("consumed worker position is outside the frozen dispatch selection")
        self._assert_recovery_ports_free()

        attempt_id = uuid.uuid4().hex
        attempts_dir = recovery_checkpoint.attempts_path(self.directory)
        if attempts_dir.is_symlink():
            raise ValueError("recovery attempt directory cannot be a symbolic link")
        attempts_dir.mkdir(parents=True, exist_ok=True)
        context_evidence_path = attempts_dir / f"context-{attempt_id}.json"
        mcp_evidence_path = attempts_dir / f"mcp-{attempt_id}.json"
        attempt_path = attempts_dir / f"attempt-{attempt_id}.json"
        started_at_epoch = recovery_checkpoint.check_wall_clock(self.recovery_ledger)
        try:
            for group in GROUPS:
                self._launch_group(group, phase="business", resume=True)
            observed_binding_hashes = {}
            for group in GROUPS:
                projection = self._binding_projection(group)
                observed_binding_hashes[group] = recovery_checkpoint.sha256_bytes(
                    recovery_checkpoint.canonical_bytes(projection))
                self._assert_semantic_index_state(group, private["semanticIndexHashes"][group])
            if observed_binding_hashes != self.recovery_ledger["bindingHashes"]:
                raise ValueError("worker-ready subject, policy, datasource or publication state changed")
            mcp_evidence = self._recovery_tool_capability_evidence()
            if not mcp_evidence["passed"]:
                raise ValueError("authenticated MCP tool catalog changed after worker-ready checkpoint")
            recovery_checkpoint.write_public_json_new(mcp_evidence_path, mcp_evidence)
            context_evidence = self._validate_context_delivery(evidence_path=context_evidence_path)
            for group in GROUPS:
                self._assert_semantic_index_state(group, private["semanticIndexHashes"][group])
            completed_at_epoch = time.time()
            attempt = {
                "schemaVersion": 1,
                "kind": "business-trust-v1-worker-ready-recovery",
                "attemptId": attempt_id,
                "runId": self.manifest["runId"],
                "manifestSha256": manifest_sha256,
                "startedAtEpoch": started_at_epoch,
                "completedAtEpoch": completed_at_epoch,
                "workerReadyLedgerRevision": self.recovery_ledger["revision"],
                "originalGateHashes": dict(self.recovery_ledger["gateHashes"]),
                "bindingHashes": observed_binding_hashes,
                "dispatchMarkerSha256": (self.recovery_ledger.get("dispatch") or {}).get("sha256"),
                "consumedPositionCount": len(self.records),
                "runningPositionCount": sum(record.get("status") == "running"
                                             for record in self.records.values()),
                "mcpEvidenceSha256": digest(mcp_evidence_path),
                "mcpObservedRoleCount": mcp_evidence["observedRoleCount"],
                "contextDeliveryEvidenceSha256": digest(context_evidence_path),
                "contextQuestionCount": context_evidence["questions"],
                "passed": True,
                "limitation": "recovery resumes broker state only; supervisor owns dispatch reservations",
            }
            recovery_checkpoint.write_public_json_new(attempt_path, attempt)
        except Exception as error:
            if not mcp_evidence_path.exists():
                mcp_evidence = self._recovery_tool_capability_evidence()
                recovery_checkpoint.write_public_json_new(mcp_evidence_path, mcp_evidence)
            failure = {
                "schemaVersion": 1,
                "kind": "business-trust-v1-worker-ready-recovery",
                "attemptId": attempt_id,
                "runId": self.manifest["runId"],
                "manifestSha256": manifest_sha256,
                "startedAtEpoch": started_at_epoch,
                "workerReadyLedgerRevision": self.recovery_ledger["revision"],
                "failureType": type(error).__name__,
                "mcpEvidenceSha256": digest(mcp_evidence_path),
                "mcpObservedRoleCount": mcp_evidence["observedRoleCount"],
                "contextDeliveryEvidenceSha256": (digest(context_evidence_path)
                    if context_evidence_path.is_file() else None),
                "passed": False,
            }
            recovery_checkpoint.write_public_json_new(attempt_path, failure)
            raise
        self.observe_recovery_clock()

    def wait_port_for_test(self, port: int, process: subprocess.Popen[bytes]) -> None:
        self._wait_port(port, process)

    def close(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def _expire_job_if_due_locked(self, job: str, now: float) -> bool:
        """Persist a due deadline while the caller holds this job's lock."""
        record = self.records.get(job)
        started = self.started.get(job)
        deadline = getattr(self, "deadline_monotonic", {}).get(job)
        due = (now >= deadline if deadline is not None else
               started is not None and now - started >= DEADLINE_SECONDS)
        if (not isinstance(record, dict) or record.get("status") != "running"
            or started is None or not due):
            return False
        record["status"] = "timeout"
        record["endedAt"] = utc()
        self.capture(job)
        return True

    def expire_deadlines(self, *, now: float | None = None,
                         blocking: bool = True) -> list[str]:
        """Expire due workers; the service loop uses non-blocking lock acquisition."""
        if not self.guard.acquire(blocking=blocking):
            return []
        expired = []
        try:
            for job in tuple(self.started):
                lock = self.locks[job]
                if not lock.acquire(blocking=blocking):
                    continue
                try:
                    observed = time.monotonic() if now is None else now
                    if self._expire_job_if_due_locked(job, observed):
                        expired.append(job)
                finally:
                    lock.release()
        finally:
            self.guard.release()
        return expired

    def capture(self, job: str) -> None:
        folder = self.directory / "records" / job
        folder.mkdir(parents=True, exist_ok=True)
        paths = sorted(folder.glob("[0-9][0-9][0-9][0-9].json"))
        index = len(paths)
        ledger_state = getattr(self, "recovery_ledger", None)
        if ledger_state is None:
            write_new(folder / f"{index:04d}.json", self.records[job])
            return

        if getattr(self, "recovery_fault", False):
            raise RuntimeError("durable recovery state is unavailable")
        self.observe_recovery_clock()
        with getattr(self, "recovery_lock", threading.RLock()):
            paths = sorted(folder.glob("[0-9][0-9][0-9][0-9].json"))
            index = len(paths)
            if [path.name for path in paths] != [f"{position:04d}.json" for position in range(index)]:
                self.recovery_fault = True
                raise RuntimeError("record history is not a contiguous prefix")
            previous_sha256 = (recovery_checkpoint.sha256_file(paths[-1]) if paths else None)
            record = dict(self.records[job])
            record["_checkpoint"] = {"schemaVersion": 1, "revision": index,
                                     "previousSha256": previous_sha256}
            target = folder / f"{index:04d}.json"
            try:
                recovery_checkpoint.write_json_new(target, record)
                record_sha256 = recovery_checkpoint.sha256_file(target)
                ledger = dict(self.recovery_ledger)
                record_heads = dict(ledger.get("records", {}))
                record_heads[job] = {"revision": index, "sha256": record_sha256}
                consumed = dict(ledger.get("consumedJobs", {}))
                if record.get("status") != "missing":
                    previous = consumed.get(job, {})
                    consumed[job] = {
                        "schedulePosition": list(self.schedule).index(job),
                        "startedAtEpoch": record.get("startedAtEpoch", previous.get("startedAtEpoch")),
                        "deadlineAtEpoch": record.get("deadlineAtEpoch", previous.get("deadlineAtEpoch")),
                        "status": record.get("status"),
                    }
                dispatch = self._validated_dispatch_marker()
                bound = ledger.get("dispatch")
                if bound is not None and bound != dispatch:
                    raise ValueError("worker dispatch marker changed after checkpoint")
                if bound is None and dispatch is not None:
                    if job not in dispatch["selectedJobIds"]:
                        raise ValueError("job is outside the frozen worker dispatch selection")
                    ledger["dispatch"] = dispatch
                next_value = recovery_checkpoint.next_ledger(
                    ledger, records=record_heads, consumedJobs=consumed)
                if self._recovery_mac_key is None:
                    raise ValueError("worker-ready ledger authenticator is unavailable")
                next_value = recovery_checkpoint.seal_ledger(next_value, self._recovery_mac_key)
                recovery_checkpoint.write_ledger(self.directory, next_value)
                self.recovery_ledger = next_value
                self.records[job]["_checkpoint"] = record["_checkpoint"]
            except Exception:
                # The record file is an immutable prefix fact. If the ledger
                # update fails, stop accepting requests; resume can reconcile
                # exactly one durable record beyond the last ledger revision.
                self.recovery_fault = True
                raise

    def begin(self, job: str, nonce: str, worker_thread_id: str | None = None) -> dict[str, Any]:
        with self.guard:
            manifest = getattr(self, "manifest", {})
            ledger_state = getattr(self, "recovery_ledger", None)
            if job not in self.schedule or job in self.records:
                raise ValueError("unknown or already started job")
            if isinstance(manifest, dict) and "workerDispatchAllowed" in manifest \
                    and manifest.get("workerDispatchAllowed") is not True:
                raise ValueError("frozen run does not allow worker dispatch")
            if (isinstance(manifest, dict) and recovery_checkpoint.protocol_supported(manifest)
                and ledger_state is None):
                raise ValueError("worker-ready recovery checkpoint is unavailable")
            if not secrets.compare_digest(self.dispatch_nonces.get(job, ""), nonce):
                raise ValueError("dispatch nonce mismatch")
            if ledger_state is not None:
                self._bind_dispatch_marker(job)
                marker = self.recovery_ledger.get("dispatch")
                if marker is None:
                    raise ValueError("worker dispatch setup is missing")
                app_dispatch = marker["file"] == "app-dispatch-start.json"
                cli_dispatch = marker["file"] == "runner-start.json"
            else:
                app_dispatch = (self.directory / "app-dispatch-start.json").is_file()
                cli_dispatch = (self.directory / "runner-start.json").is_file()
                if app_dispatch == cli_dispatch:
                    raise ValueError("worker dispatch setup is missing or ambiguous")
            if app_dispatch and worker_thread_id is None:
                raise ValueError("app worker session identity is required")
            if cli_dispatch and worker_thread_id is not None:
                raise ValueError("CLI worker session identity must not be inherited")
            self.expire_deadlines()
            if sum(record["status"] == "running" for record in self.records.values()) >= MAX_CONCURRENT:
                raise ValueError("worker concurrency limit reached")
            entry = self.schedule[job]
            question = self.questions[entry["questionId"]]
            with self.locks[job]:
                if job in self.records:
                    raise ValueError("unknown or already started job")
                started_at_epoch = time.time() if ledger_state is not None else None
                self.records[job] = {**entry, "schemaVersion": 1, "status": "running",
                    "startedAt": utc(), "events": [], "clarificationAsked": False,
                    "audit": {"status": "unverified"}}
                if started_at_epoch is not None:
                    self.records[job]["startedAtEpoch"] = started_at_epoch
                    self.records[job]["deadlineAtEpoch"] = started_at_epoch + DEADLINE_SECONDS
                if worker_thread_id is not None:
                    self.records[job]["workerThreadId"] = worker_thread_id
                self.started[job] = time.monotonic()
                if started_at_epoch is not None:
                    if not hasattr(self, "deadline_monotonic"):
                        self.deadline_monotonic = {}
                    self.deadline_monotonic[job] = self.started[job] + DEADLINE_SECONDS
                self.capture(job)
            public = dict(question["public"])
            if question.get("clarification"):
                public.pop("outputColumns", None)
            group = entry["group"]
            actor_tools = self.group_tools[group][question["actor"]]
            public_tools = [item for item in actor_tools if item["name"] in ALLOWED_REQUIRED | ALLOWED_OPTIONAL]
            return {"schemaVersion": 1, "jobId": job, "actor": question["actor"],
                    "language": question["language"], **public,
                    "clarificationProtocol": public_clarification_protocol(),
                    "tools": public_tools,
                    "limits": {"calls": MAX_CALLS, "seconds": DEADLINE_SECONDS}}

    def reject(self, job: str, reason: str) -> None:
        """Record sanitized broker rejection evidence without retaining request bodies."""
        lock = self.locks.get(job)
        if lock is None:
            return
        with lock:
            record = self.records.get(job)
            if isinstance(record, dict) and record.get("status") == "running":
                record.setdefault("brokerRejections", []).append(reason)
                record["status"] = "invalid"
                record["endedAt"] = utc()
                self.capture(job)

    def command(self, job: str, operation: str, body: dict[str, Any],
                worker_thread_id: str | None = None) -> dict[str, Any]:
        if getattr(self, "recovery_fault", False):
            raise RuntimeError("durable recovery state is unavailable")
        if job not in self.locks:
            raise ValueError("unknown job")
        with self.locks[job]:
            record = self.records.get(job)
            if not isinstance(record, dict) or record.get("status") != "running":
                raise ValueError("job is not running")
            expected_thread_id = record.get("workerThreadId")
            if expected_thread_id != worker_thread_id:
                self.reject(job, "dispatch_identity_mismatch")
                raise ValueError("worker session identity changed")
            if self._expire_job_if_due_locked(job, time.monotonic()):
                raise TimeoutError("job deadline reached")
            question = self.questions[record["questionId"]]
            events = record["events"]
            if operation == "clarify":
                if set(body) != {"question", "key"} or not isinstance(body.get("question"), str) or not isinstance(body.get("key"), str):
                    self.reject(job, "invalid_clarification_request")
                    raise ValueError("clarification payload invalid")
                if any(event.get("tool") == "user_clarification" for event in events):
                    self.reject(job, "clarification_repeated")
                    raise ValueError("only one clarification is allowed")
                configured = question.get("clarification")
                matched = bool(configured and body["key"] in configured.get("promptKeys", []))
                response = (configured["answer"] if matched else
                            "No additional business definition is available; do not invent one.")
                record["clarificationAsked"] = record["clarificationAsked"] or matched
                events.append({"tool": "user_clarification", "arguments": body,
                    "result": {"answer": response, "matched": matched}, "durationMs": 0})
                self.capture(job)
                return {"answer": response,
                        **({"outputColumns": question["public"]["outputColumns"]} if matched else {})}
            if operation == "final":
                required = {"answer", "rows", "columns", "scope", "conditions", "uncertain"}
                if set(body) != required or not isinstance(body.get("answer"), str) or not isinstance(body.get("rows"), list) or not isinstance(body.get("columns"), list) or not isinstance(body.get("scope"), str) or not isinstance(body.get("conditions"), list) or not all(isinstance(item, str) for item in body["conditions"]) or type(body.get("uncertain")) is not bool:
                    self.reject(job, "invalid_final_payload")
                    raise ValueError("final payload invalid")
                if "final" in record:
                    self.reject(job, "final_repeated")
                    raise ValueError("final already submitted")
                record.update(final=body, status="complete", endedAt=utc(),
                    durationMs=(time.monotonic() - self.started[job]) * 1000)
                self.capture(job)
                return {"recorded": True}
            if operation != "tool" or set(body) != {"name", "arguments"} or not isinstance(body.get("name"), str) or not isinstance(body.get("arguments"), dict):
                self.reject(job, "invalid_tool_request")
                raise ValueError("tool payload invalid")
            name, arguments = body["name"], body["arguments"]
            actual_tools = {item["name"] for item in self.group_tools[record["group"]][question["actor"]]}
            allowed = (ALLOWED_REQUIRED | ALLOWED_OPTIONAL) & actual_tools
            if name not in allowed:
                self.reject(job, "tool_not_exposed_or_not_allowed")
                raise ValueError("tool not exposed or not allowed")
            if (self.questions[record["questionId"]]["oracle"]["type"] == "blocked"
                and name in {"semarail_plan_query", "semarail_prepare_query",
                             "semarail_governed_query", "semarail_get_query_evidence"}):
                self.reject(job, "query_tool_forbidden_for_blocked_case")
                raise ValueError("query tools are forbidden for the blocked case")
            formal = [event for event in events if event.get("tool", "").startswith("semarail_")]
            if len(formal) >= MAX_CALLS:
                self.reject(job, "tool_budget_exceeded")
                raise ValueError("tool call budget exceeded")
            if name == "semarail_get_context" and (
                set(arguments) - {"question", "context_version", "budgets"}
                or arguments.get("question") != question["public"]["question"]
                or arguments.get("context_version") != 2
            ):
                self.reject(job, "context_question_or_version_mismatch")
                raise ValueError("context must use exact assigned question and version 2")
            queries = [event for event in events if event.get("tool") == "semarail_governed_query"]
            prior_errors = [event.get("result", {}).get("error", {}).get("code")
                            for event in events if event.get("result", {}).get("status") == "error"]
            if any(code in TERMINAL for code in prior_errors):
                self.reject(job, "tool_after_terminal_error")
                raise ValueError("terminal error; submit final without more tools")
            if name == "semarail_get_query_evidence" and not queries:
                self.reject(job, "query_evidence_without_query")
                raise ValueError("query evidence is only available after a governed query")
            if name == "semarail_governed_query" and queries:
                last_code = queries[-1].get("result", {}).get("error", {}).get("code")
                if len(queries) >= 2 or last_code not in REPAIRABLE or record.get("repairUsed"):
                    self.reject(job, "unjustified_or_excessive_query_repair")
                    raise ValueError("only one repair after a semantic error is allowed")
            plans = [event for event in events if event.get("tool") == "semarail_plan_query"]
            if name == "semarail_plan_query" and plans and arguments.get("semantic_sql") != plans[-1].get("arguments", {}).get("semantic_sql"):
                last_code = plans[-1].get("result", {}).get("error", {}).get("code")
                if last_code not in REPAIRABLE or record.get("repairUsed"):
                    self.reject(job, "unjustified_or_excessive_plan_repair")
                    raise ValueError("only one repair after a semantic error is allowed")
                record["repairUsed"] = True
            if name == "semarail_governed_query":
                if not any(event.get("tool") == "semarail_get_context" for event in events):
                    self.reject(job, "query_without_context")
                    raise ValueError("semantic context must precede execution")
                if record.get("repairUsed") and queries:
                    self.reject(job, "repair_budget_already_used")
                    raise ValueError("repair budget already used")
                sql = arguments.get("semantic_sql")
                if not isinstance(sql, str) or not sql.strip():
                    self.reject(job, "invalid_semantic_sql")
                    raise ValueError("semantic SQL is required")
                prepared = any(event.get("tool") == "semarail_prepare_query"
                    and event.get("arguments", {}).get("semantic_sql") == sql for event in events)
                if not prepared:
                    self.reject(job, "query_without_matching_prepare")
                    raise ValueError("matching prepare must precede execution")
            port = self.mcp_ports[record["group"]]
            token = self.keys[record["group"]][question["actor"]]
            started_at = time.monotonic()
            try:
                response = asyncio.run(asyncio.wait_for(
                    call_mcp(port, token, name, arguments),
                    timeout=min(45, max(.1, DEADLINE_SECONDS - (started_at - self.started[job])))))
            except TimeoutError:
                observed = time.monotonic()
                if self._expire_job_if_due_locked(job, observed):
                    raise TimeoutError("job deadline reached") from None
                record["status"] = "infrastructure_error"
                record["endedAt"] = utc()
                self.capture(job)
                raise RuntimeError("authenticated MCP request failed; details withheld") from None
            except Exception:
                record["status"] = "infrastructure_error"
                record["endedAt"] = utc()
                self.capture(job)
                raise RuntimeError("authenticated MCP request failed; details withheld") from None
            completed_at = time.monotonic()
            if self._expire_job_if_due_locked(job, completed_at):
                raise TimeoutError("job deadline reached")
            if name == "semarail_get_context":
                context = response.get("result")
                index = context.get("indexStatus") if isinstance(context, dict) else None
                if (not isinstance(context, dict) or context.get("schemaVersion") != 2
                    or not isinstance(index, dict) or index.get("status") != "ready"
                    or index.get("backend") != "hybrid"
                    or "retrievalTrace" in response.get("result", {})
                    or "retrievalSummary" in response.get("result", {})):
                    record["status"] = "infrastructure_error"
                    record["endedAt"] = utc()
                    self.capture(job)
                    raise RuntimeError("required safe hybrid Context v2 is unavailable")
            event = {"tool": name, "arguments": arguments, **response,
                     "durationMs": (completed_at - started_at) * 1000}
            events.append(event)
            if name in {"semarail_plan_query", "semarail_governed_query"}:
                result = event.get("result", {})
                if result.get("status") == "error" and result.get("error", {}).get("code") in REPAIRABLE:
                    record["repairUsed"] = True
            self.capture(job)
            return response["wire"]

    def _validate_context_shape(self, question: dict[str, Any], group: str,
                                context: dict[str, Any]) -> list[str]:
        failures = []
        if context.get("schemaVersion") != 2:
            failures.append("context_v2_unavailable")
        index = context.get("indexStatus")
        if not isinstance(index, dict) or index.get("status") != "ready" or index.get("backend") != "hybrid":
            failures.append("hybrid_index_not_ready")
        if "retrievalTrace" in context or "retrievalSummary" in context:
            failures.append("diagnostic_retrieval_metadata_exposed")
        schema = context.get("schema")
        models = schema.get("models") if isinstance(schema, dict) else None
        if not isinstance(models, list):
            failures.append("context_schema_models_missing")
            models = []
        visible: dict[str, set[str]] = {}
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("name"), str):
                failures.append("malformed_context_model")
                continue
            columns = model.get("columns")
            if not isinstance(columns, list):
                failures.append("context_model_columns_missing")
                continue
            visible[model["name"].lower()] = {
                item["name"].lower() for item in columns
                if isinstance(item, dict) and isinstance(item.get("name"), str)}
        try:
            import sqlglot
            from sqlglot import exp
            canonical = question.get("canonical", {}).get("semanticSql")
            if canonical:
                parsed = sqlglot.parse_one(canonical, read="postgres")
                referenced = {table.name.lower() for table in parsed.find_all(exp.Table)}
                forbidden = {
                    "employee": {"performance_reviews"},
                    "department_manager": {"compensation_history"},
                    "hrbp": {"compensation_history"},
                    "compensation_admin": {"performance_reviews", "attendance_monthly"},
                    "hr_director": set(),
                }[question["actor"]]
                required = referenced - forbidden
                # Model names can be normalized with the physical table suffix.
                normalized_visible = {name.rsplit(".", 1)[-1] for name in visible}
                if not required <= normalized_visible:
                    failures.append("required_safe_context_model_missing")
                if forbidden & normalized_visible:
                    failures.append("forbidden_model_in_context_projection")
        except Exception:
            failures.append("context_requirements_unverifiable")
        if question["actor"] in {"employee", "department_manager"}:
            if any("gender" in columns or "age" in columns for columns in visible.values()):
                failures.append("forbidden_column_in_context_projection")
        return failures

    def _validate_context_delivery(self, *, evidence_path: Path | None = None) -> dict[str, Any]:
        from knowledge_delivery import catalog_coverage, context_coverage

        evidence = []
        failures = []
        catalogs: dict[tuple[str, str], Any] = {}
        for group in GROUPS:
            for actor in self._expected_actor_attributes():
                try:
                    response = asyncio.run(call_mcp(
                        self.mcp_ports[group], self.keys[group][actor],
                        "semarail_list_models", {}))
                    candidate = response.get("result")
                    if isinstance(candidate, dict) and candidate.get("status") != "error":
                        catalogs[(group, actor)] = candidate
                except Exception:
                    # A failed catalog read matters only when direct Context
                    # coverage needs the authorized catalog as fallback.
                    continue
        for question in self.questions.values():
            for group in GROUPS:
                actor = question["actor"]
                try:
                    response = asyncio.run(call_mcp(self.mcp_ports[group], self.keys[group][actor],
                        "semarail_get_context", {"question": question["public"]["question"], "context_version": 2}))
                    context = response.get("result")
                    if not isinstance(context, dict):
                        raise ValueError("context response missing")
                    coverage = context_coverage(question, context)
                    delivery = coverage
                    if not coverage["passed"] and coverage["catalogNeeded"]:
                        catalog = catalogs.get((group, actor))
                        if catalog is not None:
                            delivery = catalog_coverage(question, context, catalog)
                    shape_reasons = self._validate_context_shape(question, group, context)
                    # The legacy shape check requires every canonical model in
                    # Context itself. The v1 delivery contract also accepts a
                    # complete actor-authorized catalog at the same revision.
                    if delivery.get("passed") and delivery.get("fallbackUsed"):
                        shape_reasons = [reason for reason in shape_reasons
                                         if reason != "required_safe_context_model_missing"]
                    reasons = shape_reasons
                    if not delivery.get("passed"):
                        for reason in delivery.get("reasonCodes", []):
                            if reason not in reasons:
                                reasons.append(reason)
                    failures.extend({"questionId": question["id"], "group": group, "reason": reason}
                                    for reason in reasons)
                    index = context.get("indexStatus", {})
                    evidence.append({"questionId": question["id"], "group": group,
                        "actor": actor, "language": question["language"],
                        "contextSha256": canonical_hash(context),
                        "models": len(context.get("schema", {}).get("models", [])),
                        "backend": index.get("backend"), "status": index.get("status"),
                        "knowledgeDelivery": {
                            key: delivery.get(key) for key in (
                                "passed", "catalogNeeded", "fallbackUsed",
                                "requiredModelCount", "requiredColumnCount",
                                "contextCoveredModelCount", "contextCoveredColumnCount",
                                "catalogCoveredModelCount", "catalogCoveredColumnCount",
                                "denialExcludedModelCount", "denialExcludedColumnCount",
                                "contextRevision", "catalogRevision", "reasonCodes")
                        }})
                except Exception:
                    failures.append({"questionId": question["id"], "group": group,
                                     "reason": "authenticated_context_call_failed"})
        result = {"schemaVersion": 1, "contextVersion": 2,
            "requiredBackend": "hybrid", "projectSnapshotSha256": self.manifest["projectSnapshot"]["sha256"],
            "passed": not failures and len(evidence) == 60, "questions": len(evidence),
            "failures": failures, "evidence": evidence,
            "limitation": "hashes and safe shape only; raw context is not persisted"}
        if evidence_path is None:
            write_new(self.directory / "context-delivery-validation.json", result)
        else:
            recovery_checkpoint.write_public_json_new(evidence_path, result)
        if not result["passed"]:
            raise RuntimeError("authenticated safe Context v2 hybrid validation failed")
        return result

    def _rpc(self, group: str, actor: str, request_id: str,
             semantic_sql: str, question: str) -> dict[str, Any]:
        token = self.keys[group][actor]
        response = _http_json(f"http://127.0.0.1:{self.core_ports[group]}/api/v1/runtime/rpc",
            token=token, method="POST", body={
                "protocolVersion": "1", "id": request_id, "method": "query.run",
                "params": {"question": question, "semanticSql": semantic_sql, "queryId": request_id},
            })
        if response.get("ok") is True and isinstance(response.get("result"), dict):
            return response["result"]
        error = response.get("error")
        return {"schemaVersion": 2, "status": "error", "error": error or {},
                "stats": {"returnedRows": 0, "durationMs": 0, "truncated": False}}

    def _run_legacy_regression(self) -> None:
        results = {group: [] for group in GROUPS}
        total = 0
        for group in GROUPS:
            root = Path(self.manifest[group]["path"])
            corpus = read(root / "benchmarks/hr-enterprise/golden-questions.json")
            if not isinstance(corpus.get("questions"), list) or len(corpus["questions"]) != 60:
                raise ValueError("the frozen legacy HR regression must contain 60 questions")
            for question in corpus["questions"]:
                total += 1
                started = time.monotonic()
                try:
                    result = self._rpc(group, question["actor"], f"btv-reg-{group}-{question['id']}",
                        question["canonical"]["semanticSql"], question["question"])
                    passed, reason, _ = self._assert_legacy(question, result)
                except Exception:
                    passed, reason = False, "authenticated_regression_call_failed"
                results[group].append({"id": question["id"], "passed": passed,
                    "reason": reason, "durationMs": (time.monotonic() - started) * 1000})
        failure_count = sum(not item["passed"] for group in GROUPS for item in results[group])
        output = {"schemaVersion": 1, "casesPerGroup": 60,
            "total": total, "passed": total - failure_count,
            "byGroup": {group: {"total": len(results[group]),
                "passed": sum(item["passed"] for item in results[group]),
                "cases": results[group]} for group in GROUPS},
            "status": "pass" if failure_count == 0 and total == 120 else "fail"}
        write_new(self.directory / "regression.json", output)
        if output["status"] != "pass":
            raise ValueError("legacy 60-question regression failed for one or more groups")

    @staticmethod
    def _safe_prepare_summary(result: Mapping[str, Any]) -> dict[str, Any]:
        reasons = set()
        for item in result.get("blockedReasons", []):
            if isinstance(item, Mapping) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"])
        evidence = result.get("businessEvidence")
        if isinstance(evidence, Mapping):
            for item in evidence.get("checks", []):
                if isinstance(item, Mapping) and item.get("status") != "passed":
                    code = item.get("reasonCode")
                    if isinstance(code, str):
                        reasons.add(code)
        error = result.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("code"), str):
            reasons.add(error["code"])
        return {"status": result.get("status", "unknown"),
                "reasonCodes": sorted(reasons)}

    def _prepare_canonical(self, group: str, question: Mapping[str, Any],
                           semantic_sql: str | None = None,
                           conditions: Mapping[str, Any] | None = None,
                           confirmed_conditions: list[str] | None = None) -> dict[str, Any]:
        canonical = question.get("canonical") or {}
        sql = semantic_sql or canonical.get("semanticSql")
        if not isinstance(sql, str) or not sql:
            raise ValueError("private canonical SQL is missing")
        prepared = asyncio.run(call_mcp(self.mcp_ports[group], self.keys[group][question["actor"]],
            "semarail_prepare_query", {
                "question": question["public"]["question"], "semantic_sql": sql,
                "conditions": dict(conditions if conditions is not None else canonical.get("conditions", {})),
                "confirmed_conditions": list(confirmed_conditions if confirmed_conditions is not None
                    else canonical.get("confirmedConditions", [])),
            }))
        return prepared["result"]

    def _validate_execution_checks(self) -> None:
        """Probe accepts, existing counterexamples, and grouped confirmation gaps."""
        candidate = "candidate"
        questions = self.questions
        probes: list[tuple[str, dict[str, Any], str, dict[str, Any], list[str], str | None]] = []
        for probe_id, question_id in (
            ("headcount-current-ready", "BTV-01"),
            ("salary-snapshot-scalar-ready", "BTV-11"),
            ("salary-original-date-trend-ready", "BTV-12"),
            ("performance-quarter-scalar-ready", "BTV-23"),
            ("performance-original-date-trend-ready", "BTV-24"),
        ):
            question = questions[question_id]
            canonical = question["canonical"]
            probes.append((probe_id, question, canonical["semanticSql"],
                canonical.get("conditions", {}), canonical.get("confirmedConditions", []), None))

        headcount = questions["BTV-01"]
        probes.append(("headcount-nondistinct-count-blocked", headcount,
            "SELECT COUNT(employee_id) AS active_employee_count FROM employees WHERE resigned = FALSE AND employee_id = 1",
            {}, [], "metric_conflicts"))
        probes.append(("headcount-missing-active-predicate-blocked", headcount,
            "SELECT COUNT(DISTINCT employee_id) AS active_employee_count FROM employees WHERE employee_id = 1",
            {}, [], "predicate_missing"))
        probes.append(("headcount-conflicting-active-predicate-blocked", headcount,
            "SELECT COUNT(DISTINCT employee_id) AS active_employee_count FROM employees WHERE resigned = TRUE AND employee_id = 1",
            {}, [], "predicate_conflicts"))
        salary = questions["BTV-11"]
        probes.append(("salary-wrong-snapshot-blocked", salary,
            "SELECT AVG(monthly_salary) AS average_monthly_salary FROM compensation_history WHERE effective_date = DATE '2024-03-01' AND employee_id = 1",
            salary["canonical"]["conditions"], salary["canonical"]["confirmedConditions"], "time_filter_conflicts"))
        probes.append(("salary-unconfirmed-snapshot-blocked", salary,
            salary["canonical"]["semanticSql"], salary["canonical"]["conditions"], [],
            "time_confirmation_missing"))
        probes.append(("salary-snapshot-condition-missing-blocked", salary,
            salary["canonical"]["semanticSql"], {}, [], "time_confirmation_missing"))
        trend = questions["BTV-12"]
        probes.append(("salary-transformed-date-trend-blocked", trend,
            "SELECT date_trunc('month', effective_date) AS effective_date, AVG(monthly_salary) AS average_monthly_salary FROM compensation_history WHERE effective_date BETWEEN DATE '2024-03-01' AND DATE '2024-09-01' AND employee_id = 1 GROUP BY date_trunc('month', effective_date)",
            trend["canonical"]["conditions"], trend["canonical"]["confirmedConditions"], "time_grouping_transformed"))
        performance = questions["BTV-23"]
        probes.append(("performance-wrong-quarter-blocked", performance,
            "SELECT AVG(performance_score) AS average_performance_score FROM performance_reviews WHERE review_date BETWEEN DATE '2024-04-01' AND DATE '2024-06-30' AND manager_id = 627",
            performance["canonical"]["conditions"], performance["canonical"]["confirmedConditions"], "time_filter_conflicts"))
        performance_trend = questions["BTV-24"]
        confirmation_probes = {}
        for prefix, question, key in (
            ("salary", trend, "effectiveDateRange"),
            ("performance", performance_trend, "reviewPeriod"),
        ):
            canonical = question["canonical"]
            other_confirmed = [item for item in canonical["confirmedConditions"] if item != key]
            for state in ("missing", "unconfirmed"):
                probe_id = f"{prefix}-original-date-trend-period-{state}-blocked"
                values = dict(canonical["conditions"])
                if state == "missing":
                    values.pop(key)
                probes.append((probe_id, question, canonical["semanticSql"], values,
                    other_confirmed, "time_confirmation_missing"))
                confirmation_probes[probe_id] = key
        probes.append(("performance-transformed-date-trend-blocked", performance_trend,
            "SELECT date_trunc('quarter', review_date) AS review_date, AVG(performance_score) AS average_performance_score FROM performance_reviews WHERE review_date BETWEEN DATE '2023-12-31' AND DATE '2024-09-30' AND manager_id = 627 GROUP BY date_trunc('quarter', review_date)",
            performance_trend["canonical"]["conditions"], performance_trend["canonical"]["confirmedConditions"], "time_grouping_transformed"))

        # Exercise the verifier's deliberately limited SQL shape on an
        # authorized representative from each scene. These cases must remain
        # unverified and blocked, including at the governed-query boundary used
        # by older MCP clients that omit preparation_id.
        headcount = questions["BTV-01"]
        probes.append(("headcount-or-shape-blocked", headcount,
            "SELECT COUNT(DISTINCT employee_id) AS active_employee_count FROM employees WHERE resigned = FALSE AND (employee_id = 1 OR employee_id = 2)",
            headcount["canonical"].get("conditions", {}),
            headcount["canonical"].get("confirmedConditions", []), "unsupported_sql_shape"))
        salary = questions["BTV-11"]
        probes.append(("salary-subquery-shape-blocked", salary,
            "SELECT AVG(monthly_salary) AS average_monthly_salary FROM compensation_history WHERE effective_date BETWEEN DATE '2024-09-01' AND DATE '2024-09-01' AND employee_id IN (SELECT employee_id FROM employees WHERE employee_id = 1)",
            salary["canonical"]["conditions"], salary["canonical"]["confirmedConditions"], "unsupported_sql_shape"))
        performance = questions["BTV-23"]
        probes.append(("performance-subquery-shape-blocked", performance,
            "SELECT AVG(performance_score) AS average_performance_score FROM performance_reviews WHERE review_date BETWEEN DATE '2024-07-01' AND DATE '2024-09-30' AND manager_id = 627 AND employee_id IN (SELECT employee_id FROM employees WHERE manager_id = 627)",
            performance["canonical"]["conditions"], performance["canonical"]["confirmedConditions"], "unsupported_sql_shape"))

        results = []
        failures = []
        execution_boundary_probes = {
            "headcount-or-shape-blocked",
            "salary-subquery-shape-blocked",
            "performance-subquery-shape-blocked",
        } | set(confirmation_probes)
        for probe_id, question, sql, conditions, confirmed, expected_failure in probes:
            try:
                prepared = self._prepare_canonical(candidate, question, sql, conditions, confirmed)
                summary = self._safe_prepare_summary(prepared)
                passed = ((summary["status"] == "ready") if expected_failure is None
                          else summary["status"] in {"blocked", "needs_clarification"}
                          and expected_failure in summary["reasonCodes"])
            except Exception:
                prepared = {}
                summary = {"status": "unobservable", "reasonCodes": []}
                passed = False

            if probe_id in confirmation_probes:
                keys = [item.get("conditionKey") for item in prepared.get("clarifications", [])
                        if isinstance(item, Mapping)]
                summary["publishedConditionKeyDiscoverable"] = confirmation_probes[probe_id] in keys
                passed = passed and summary["publishedConditionKeyDiscoverable"]

            execution_boundary: dict[str, Any] | None = None
            if probe_id in execution_boundary_probes:
                preparation_id = prepared.get("preparationId") if isinstance(prepared, Mapping) else None
                attempts = {
                    "legacyWithoutPreparationId": None,
                    "blockedPreparationId": preparation_id,
                }
                boundary_results: dict[str, Any] = {}
                for attempt_name, attempt_preparation_id in attempts.items():
                    if attempt_name == "blockedPreparationId" and not isinstance(attempt_preparation_id, str):
                        boundary_results[attempt_name] = {
                            "status": "preparation_id_missing", "errorCode": None,
                            "returnedRows": None, "passed": False,
                        }
                        continue
                    try:
                        arguments: dict[str, Any] = {
                            "question": question["public"]["question"],
                            "semantic_sql": sql,
                            "chart_intent": "table",
                        }
                        if isinstance(attempt_preparation_id, str):
                            arguments["preparation_id"] = attempt_preparation_id
                        wire = asyncio.run(call_mcp(
                            self.mcp_ports[candidate], self.keys[candidate][question["actor"]],
                            "semarail_governed_query", arguments))
                        result = wire.get("result") if isinstance(wire, Mapping) else None
                        error = result.get("error") if isinstance(result, Mapping) else None
                        error_code = error.get("code") if isinstance(error, Mapping) else None
                        if not isinstance(error_code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", error_code):
                            error_code = None
                        stats = result.get("stats") if isinstance(result, Mapping) else None
                        returned_rows = stats.get("returnedRows") if isinstance(stats, Mapping) else None
                        rows = result.get("previewRows", []) if isinstance(result, Mapping) else None
                        boundary_ok = (
                            isinstance(result, Mapping)
                            and result.get("status") == "error"
                            and error_code == "CLARIFICATION_REQUIRED"
                            and type(returned_rows) is int
                            and returned_rows == 0
                            and rows == []
                        )
                        boundary_results[attempt_name] = {
                            "status": result.get("status", "unknown") if isinstance(result, Mapping) else "unobservable",
                            "errorCode": error_code,
                            "returnedRows": returned_rows if type(returned_rows) is int and returned_rows >= 0 else None,
                            "passed": boundary_ok,
                        }
                    except Exception:
                        boundary_results[attempt_name] = {
                            "status": "unobservable", "errorCode": None,
                            "returnedRows": None, "passed": False,
                        }
                execution_boundary = boundary_results
                passed = passed and all(item["passed"] for item in boundary_results.values())

            check: dict[str, Any] = {"probeId": probe_id, **summary, "passed": passed}
            if execution_boundary is not None:
                check["executionBoundary"] = execution_boundary
            results.append(check)
            if not passed:
                failures.append(probe_id)
        output = {"schemaVersion": 1, "candidateGroup": candidate,
            "positiveAcceptanceCases": 5, "mandatoryCounterexamples": 16,
            "conflictCounterexamples": 9, "unsupportedShapeCounterexamples": 3,
            "groupedConfirmationCounterexamples": 4,
            "executionBoundaryCases": 7, "totalChecks": 21,
            "checks": results, "passed": not failures and len(results) == 21,
            "failures": failures,
            "limitation": "authenticated MCP prepare checks; seven unsupported-shape or missing-confirmation cases also exercise governed execution with and without preparation IDs; no raw SQL, conditions, or result rows persisted"}
        write_new(self.directory / "execution-check-validation.json", output)
        if not output["passed"]:
            raise RuntimeError("candidate published business execution-check preflight failed")

    @staticmethod
    def _assert_legacy(question: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str, float]:
        oracle = question["oracle"]
        duration = result.get("stats", {}).get("durationMs", -1)
        if oracle["type"] == "denial":
            error = result.get("error", {})
            ok = result.get("status") == "error" and error.get("code") == oracle["errorCode"] and result.get("stats", {}).get("returnedRows") == oracle["databaseRowsReturned"] and 0 <= duration <= 10000
            return ok, "PASS" if ok else "POLICY_NOT_DENIED", float(duration)
        if result.get("status") != "success":
            return False, "QUERY_ERROR", float(duration)
        if result.get("delivery", "inline") != "inline" or result.get("stats", {}).get("truncated"):
            return False, "INCOMPLETE_RESULT", float(duration)
        rows = result.get("previewRows")
        if not isinstance(rows, list):
            return False, "ROWS", float(duration)
        from legacy_compare import rows_equal
        ok = rows_equal(rows, oracle["rows"], float(oracle["numericTolerance"]),
                        bool(oracle.get("orderSensitive")), oracle.get("columnAliases"),
                        oracle.get("allowedExtraColumns"))
        return (ok and 0 <= duration <= 10000,
                "PASS" if ok else "ORACLE_MISMATCH", float(duration))

    def _validate_oracles(self) -> None:
        from evaluation import compare_rows
        from native_oracle import execute as native_execute
        outputs = read(self.directory / "oracle-results.json")
        failures = []
        permission_boundaries: list[dict[str, Any]] = []

        def no_result_rows(result: Any) -> bool:
            if not isinstance(result, Mapping):
                return False
            for name in ("previewRows", "rows"):
                if name in result and (not isinstance(result[name], list) or result[name]):
                    return False
            return True

        def is_policy_plan_denial(result: Any, expected_rows: int) -> bool:
            if (not isinstance(result, Mapping) or result.get("status") != "error"
                    or not isinstance(result.get("error"), Mapping)
                    or result["error"].get("code") != "POLICY_DENIED"
                    or not no_result_rows(result)):
                return False
            # Planning does not return query rows. If the protocol supplies a
            # returnedRows field anyway, preserve and check that actual value.
            stats = result.get("stats")
            if isinstance(stats, Mapping) and "returnedRows" in stats:
                return type(stats["returnedRows"]) is int and stats["returnedRows"] == expected_rows
            return True

        def is_governed_error(result: Any, expected_code: str, expected_rows: int) -> bool:
            if (not isinstance(result, Mapping) or result.get("status") != "error"
                    or not isinstance(result.get("error"), Mapping)
                    or result["error"].get("code") != expected_code
                    or not no_result_rows(result)):
                return False
            stats = result.get("stats")
            return (isinstance(stats, Mapping)
                and type(stats.get("returnedRows")) is int
                and stats["returnedRows"] == expected_rows)

        def safe_stage(result: Any, *, tool: str) -> dict[str, Any]:
            result = result if isinstance(result, Mapping) else {}
            error = result.get("error")
            stats = result.get("stats")
            status = result.get("status")
            code = error.get("code") if isinstance(error, Mapping) else None
            has_rows = isinstance(stats, Mapping) and "returnedRows" in stats
            returned_rows = stats.get("returnedRows") if has_rows else None
            return {
                "tool": tool,
                "notARowReturningTool": tool in {"semarail_plan_query", "semarail_prepare_query"},
                "status": status if isinstance(status, str) and re.fullmatch(r"[a-z_]{1,32}", status) else "unknown",
                "errorCode": code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code) else None,
                "statsReturnedRowsPresent": has_rows,
                "returnedRows": returned_rows if type(returned_rows) is int and returned_rows >= 0 else None,
                "previewRowsPresent": "previewRows" in result,
                "previewRowsEmpty": isinstance(result.get("previewRows"), list) and not result["previewRows"],
                "rowsPresent": "rows" in result,
                "rowsEmpty": isinstance(result.get("rows"), list) and not result["rows"],
                "noResultRows": no_result_rows(result),
            }

        def row_result_failure(result: Any, frozen: Any,
                               oracle: Mapping[str, Any]) -> str | None:
            if not isinstance(result, Mapping):
                return "RESULT_UNAVAILABLE"
            error = result.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code):
                return code
            if result.get("status") != "success":
                return "QUERY_ERROR"
            stats = result.get("stats")
            if (result.get("delivery", "inline") != "inline"
                    or isinstance(stats, Mapping) and stats.get("truncated")):
                return "INCOMPLETE_RESULT"
            try:
                comparison = compare_rows(result, frozen, oracle)
            except Exception:
                return "RESULT_MISMATCH"
            return None if comparison.get("correct") is True else "RESULT_MISMATCH"

        for question in self.questions.values():
            oracle_type = question["oracle"]["type"]
            if oracle_type == "blocked":
                continue
            pair = {}
            denial_stages: dict[str, dict[str, Any]] = {}
            for group in GROUPS:
                actor = question["actor"]
                plan_result = None
                if oracle_type == "denial":
                    plan_wire = asyncio.run(call_mcp(self.mcp_ports[group], self.keys[group][actor],
                        "semarail_plan_query", {
                            "semantic_sql": question["canonical"]["semanticSql"]}))
                    plan_result = plan_wire["result"]
                prepared = self._prepare_canonical(group, question)
                preparation_id = prepared.get("preparationId") if isinstance(prepared, Mapping) else None
                if prepared.get("status") == "ready" and isinstance(preparation_id, str):
                    arguments = {"question": question["public"]["question"],
                        "semantic_sql": question["canonical"]["semanticSql"],
                        "preparation_id": preparation_id}
                    boundary_method = "prepared_governed_query"
                elif oracle_type == "denial":
                    # Keep policy denial and execution blocking as separate evidence:
                    # the plan must deny the frozen SQL, while governed execution
                    # must either deny it too or require a real preparation.
                    arguments = {"question": question["public"]["question"],
                        "semantic_sql": question["canonical"]["semanticSql"],
                        "chart_intent": "table"}
                    boundary_method = "plan_denial_and_unprepared_query_block"
                else:
                    pair[group] = {"schemaVersion": 2, "status": "error",
                        "error": {"code": "PREPARATION_NOT_READY"},
                        "stats": {"returnedRows": 0, "truncated": False}}
                    continue
                wire = asyncio.run(call_mcp(self.mcp_ports[group], self.keys[group][actor],
                    "semarail_governed_query", arguments))
                pair[group] = wire["result"]
                if oracle_type == "denial":
                    denial_stages[group] = {
                        "plan": plan_result,
                        "prepare": prepared,
                        "governedQuery": pair[group],
                        "governedQueryPreparationIdProvided": "preparation_id" in arguments,
                        "method": boundary_method,
                    }
            if oracle_type == "denial":
                expected_code = question["oracle"]["errorCode"]
                expected_rows = question["oracle"]["databaseRowsReturned"]
                group_checks: dict[str, dict[str, Any]] = {}
                safe_boundaries = {}
                for group, stages in denial_stages.items():
                    prepared = stages["prepare"]
                    preparation_ready = (isinstance(prepared, Mapping)
                        and prepared.get("status") == "ready"
                        and isinstance(prepared.get("preparationId"), str))
                    preparation_nonready = (isinstance(prepared, Mapping)
                        and prepared.get("status") in {"blocked", "needs_clarification", "error"})
                    plan_ok = (expected_code == "POLICY_DENIED"
                        and is_policy_plan_denial(stages["plan"], expected_rows))
                    governed = stages["governedQuery"]
                    governed_denial = is_governed_error(governed, expected_code, expected_rows)
                    governed_block = (preparation_nonready
                        and is_governed_error(governed, "CLARIFICATION_REQUIRED", expected_rows))
                    governed_ok = governed_denial or governed_block
                    group_passed = plan_ok and governed_ok
                    group_checks[group] = {"passed": group_passed,
                        "failureCode": (None if group_passed else
                            "PLAN_POLICY_DENIAL_NOT_CONFIRMED" if not plan_ok
                            else "GOVERNED_QUERY_DENIAL_NOT_CONFIRMED")}
                    safe_boundaries[group] = {
                        "method": stages["method"],
                        "policyDenialProven": plan_ok,
                        "governedBoundaryPassed": governed_ok,
                        "preparationReady": preparation_ready,
                        "preparationNonReady": preparation_nonready,
                        "governedQueryPreparationIdProvided": stages["governedQueryPreparationIdProvided"],
                        "passed": group_passed,
                        "plan": safe_stage(stages["plan"], tool="semarail_plan_query"),
                        "prepare": safe_stage(stages["prepare"], tool="semarail_prepare_query"),
                        "governedQuery": safe_stage(governed, tool="semarail_governed_query"),
                    }
                permission_boundaries.append({"questionId": question["id"],
                    "groups": safe_boundaries})
                passed = all(item["passed"] for item in group_checks.values())
            else:
                frozen = outputs[question["id"]]
                group_failures = {group: failure
                    for group, result in pair.items()
                    if (failure := row_result_failure(result, frozen, question["oracle"])) is not None}
                native_failure_code = "NATIVE_ORACLE_MISMATCH"
                try:
                    native = native_execute(question,
                        database_url=os.environ[self.manifest["data"]["databaseUrlEnv"]])
                    native_matches_frozen = (
                        compare_rows(native, frozen, question["oracle"]).get("correct") is True)
                except Exception:
                    native_matches_frozen = False
                    native_failure_code = "NATIVE_ORACLE_UNAVAILABLE"
                native_check = {"passed": native_matches_frozen}
                if not native_matches_frozen:
                    native_check["failureCode"] = native_failure_code
                passed = not group_failures and native_matches_frozen
            if not passed:
                failure = {"questionId": question["id"], "type": oracle_type,
                    "groups": ({group: item["failureCode"] for group, item in group_checks.items()
                                if not item["passed"]} if oracle_type == "denial" else
                        group_failures)}
                if oracle_type == "rows":
                    failure["nativeOracle"] = native_check
                failures.append(failure)
        validation = {"schemaVersion": 1, "total": len(outputs), "failures": failures,
            "physicalPostgresChecked": sum(question["oracle"]["type"] == "rows" for question in self.questions.values()),
            "permissionBoundaryExecutions": sum(
                len(item["groups"]) for item in permission_boundaries),
            "permissionPlanExecutions": sum(
                len(item["groups"]) for item in permission_boundaries),
            "permissionBoundaryResults": permission_boundaries,
            "passed": not failures and len(outputs) == 30}
        write_new(self.directory / "oracle-validation.json", validation)
        if not validation["passed"]:
            raise ValueError("canonical semantic SQL or independent physical PostgreSQL oracle check failed")

    def _handle_request(self, job: str, operation: str, body: dict[str, Any],
                        worker_thread_id: str | None = None) -> Any:
        if operation == "begin":
            if set(body) != {"nonce"} or not isinstance(body.get("nonce"), str):
                self.reject(job, "invalid_begin_payload")
                raise ValueError("begin request must include the dispatch nonce")
            return self.begin(job, body["nonce"], worker_thread_id)
        return self.command(job, operation, body, worker_thread_id)


def serve(directory: Path, *, resume: bool = False) -> None:
    host = Host(directory)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            # Request bodies and worker SQL must never appear in access logs.
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health" or self.headers.get("Host") != f"127.0.0.1:{host.manifest['ports']['broker']}":
                self.send_error(404)
                return
            payload = json.dumps({"ready": True}, separators=(",", ":")).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802
            job = ""
            try:
                if getattr(host, "recovery_fault", False):
                    raise RuntimeError("durable recovery state is unavailable")
                if self.headers.get("Host") != f"127.0.0.1:{host.manifest['ports']['broker']}":
                    raise ValueError("loopback host header required")
                if self.headers.get("Origin"):
                    raise ValueError("browser-origin requests are not accepted")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 128_000:
                    raise ValueError("request size outside limit")
                path = self.path.strip("/").split("/")
                if len(path) != 2:
                    raise ValueError("expected job/operation path")
                job, operation = path
                if operation not in {"begin", "tool", "clarify", "final"}:
                    host.reject(job, "operation_not_allowed")
                    raise ValueError("operation not allowed")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    host.reject(job, "invalid_json_object")
                    raise ValueError("JSON object required")
                worker_thread_id = self.headers.get("X-BTV-Thread-ID")
                if worker_thread_id is not None:
                    try:
                        canonical_thread_id = str(uuid.UUID(worker_thread_id))
                    except (ValueError, AttributeError):
                        raise ValueError("worker session identity invalid") from None
                    if canonical_thread_id != worker_thread_id.casefold():
                        raise ValueError("worker session identity invalid")
                    worker_thread_id = canonical_thread_id
                result = host._handle_request(job, operation, body, worker_thread_id)
                status = 200
            except (ValueError, TimeoutError) as error:
                if job:
                    reason = str(error)
                    sanitized = {
                        "unknown or already started job": "unknown_or_reused_job",
                        "worker concurrency limit reached": "concurrency_limit",
                        "job is not running": "job_not_running",
                        "job deadline reached": "deadline_reached",
                        "begin request must include the dispatch nonce": "invalid_begin_payload",
                        "dispatch nonce mismatch": "dispatch_nonce_mismatch",
                        "worker session identity invalid": "dispatch_identity_invalid",
                        "worker session identity changed": "dispatch_identity_mismatch",
                        "app worker session identity is required": "dispatch_identity_missing",
                        "CLI worker session identity must not be inherited": "dispatch_identity_unexpected",
                        "worker dispatch setup is missing or ambiguous": "dispatch_setup_invalid",
                        "request size outside limit": "request_size_limit",
                        "expected job/operation path": "invalid_path",
                        "operation not allowed": "operation_not_allowed",
                        "JSON object required": "invalid_json_object",
                    }.get(reason, "request_rejected")
                    host.reject(job, sanitized)
                result, status = {"error": type(error).__name__, "message": "request rejected"}, 400
            except Exception:
                if job:
                    host.reject(job, "coordinator_failure")
                result, status = {"error": "CoordinatorError", "message": "coordinator operation failed"}, 500
            try:
                payload = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                payload, status = b'{"error":"InvalidResponse"}', 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

    class BrokerHTTPServer(ThreadingHTTPServer):
        def service_actions(self) -> None:
            host.observe_recovery_clock()
            host.expire_deadlines(blocking=False)

    httpd = BrokerHTTPServer(("127.0.0.1", host.manifest["ports"]["broker"]), Handler)
    httpd.daemon_threads = True
    try:
        if resume:
            host.resume()
        else:
            host.start()
        print("BTV_HOST_READY", flush=True)
        httpd.serve_forever(poll_interval=.5)
    finally:
        httpd.server_close()
        host.close()


def verify_run_freeze(directory: Path) -> dict[str, Any]:
    manifest = read(directory / "manifest.json")
    if manifest.get("benchmarkId") != "business-trust-v1":
        raise ValueError("unsupported frozen run")
    # Recovery is an explicit opt-in for new freezes. Legacy manifests remain
    # valid for fresh starts, but unknown declared recovery protocols fail closed.
    if "recoveryProtocol" in manifest and not recovery_checkpoint.protocol_supported(manifest):
        raise recovery_checkpoint.CheckpointError("frozen run declares an unsupported recovery protocol")
    if (manifest.get("executionMode") not in {"official_campaign", "preacceptance_only"}
        or manifest.get("workerDispatchAllowed") is not (manifest.get("executionMode") == "official_campaign")):
        raise ValueError("frozen run execution mode is invalid")
    validate_corpus(read(directory / "corpus.json"))
    if digest(directory / "corpus.json") != manifest["corpusSha256"]:
        raise ValueError("corpus hash mismatch")
    if digest(directory / "worker-prompt.md") != manifest["promptTemplateSha256"]:
        raise ValueError("worker prompt hash mismatch")
    if digest(directory / "evaluator-config.json") != manifest["evaluatorConfigSha256"]:
        raise ValueError("evaluator config hash mismatch")
    if digest(directory / "oracle-results.json") != manifest["oracleResultsSha256"]:
        raise ValueError("frozen oracle result hash mismatch")
    if digest(directory / "dispatch-nonces.json") != manifest.get("dispatchNoncesSha256"):
        raise ValueError("frozen dispatch nonce set changed")
    dispatch_nonces = read(directory / "dispatch-nonces.json")
    scheduled_ids = {item["id"] for item in read(directory / "schedule.json")}
    if (not isinstance(dispatch_nonces, dict) or set(dispatch_nonces) != scheduled_ids
        or len(set(dispatch_nonces.values())) != len(scheduled_ids)
        or any(not isinstance(value, str) or len(value) < 24 for value in dispatch_nonces.values())):
        raise ValueError("frozen dispatch nonce set is invalid")
    checked_hashes = manifest["checkedProjectSnapshot"]["files"]
    checked_template = directory / "checked-project-template"
    checked_actual = {path.relative_to(checked_template).as_posix(): digest(path)
                      for path in checked_template.rglob("*")
                      if path.is_file() and ".wren" not in path.parts}
    if checked_actual != checked_hashes or checked_hashes != manifest["projectSnapshot"]["files"]:
        raise ValueError("checked project template hash mismatch")
    validate_schedule(read(directory / "corpus.json")["questions"], read(directory / "schedule.json"))
    if canonical_hash(read(directory / "schedule.json")) != manifest["scheduleSha256"]:
        raise ValueError("schedule hash mismatch")
    baseline_manifest_path = directory / BASELINE_SOURCE_MANIFEST
    baseline_manifest = read(baseline_manifest_path)
    if digest(baseline_manifest_path) != manifest["baseline"]["sourceManifestSha256"]:
        raise ValueError("frozen baseline source-manifest copy changed")
    if digest(BUNDLE_ROOT / BASELINE_SOURCE_MANIFEST) != manifest["baseline"]["sourceManifestSha256"]:
        raise ValueError("approved baseline source manifest changed")
    verify_baseline(Path(manifest["baseline"]["path"]),
                    BUNDLE_ROOT / BASELINE_SOURCE_MANIFEST)
    if digest(directory / BASELINE_RUNTIME_MANIFEST) != manifest["baselineRuntime"]["manifestSha256"]:
        raise ValueError("frozen baseline runtime manifest copy changed")
    runtime_freeze = verify_runtime_freeze(Path(manifest["data"]["manifestPath"]))
    if runtime_freeze["sha256"] != manifest["baselineRuntime"]["manifestSha256"]:
        raise ValueError("approved baseline runtime manifest changed")
    embedding_freeze = verify_embedding_freeze()
    if (manifest.get("runtime", {}).get("embedding") != embedding_freeze
        or manifest.get("runtime", {}).get("embeddingOffline") is not True):
        raise ValueError("offline embedding model or file hashes differ from the frozen run")
    if baseline_manifest.get("baselineCommit") != BASELINE_COMMIT:
        raise ValueError("approved baseline commit mismatch")
    for group in GROUPS:
        root = Path(manifest[group]["path"])
        for relative, expected in manifest[group]["sourceHashes"].items():
            path = root / Path(relative)
            if not path.is_file() or digest(path) != expected:
                raise ValueError(f"{group} source changed after run freeze")
    candidate_root = Path(manifest["candidate"]["path"])
    if source_files(candidate_root) != manifest["candidate"]["sourceHashes"]:
        raise ValueError("candidate source inventory changed after freeze")
    verify_distribution_artifact(candidate_root, manifest["candidate"].get("distribution"))
    if digest(Path(manifest["data"]["manifestPath"])) != manifest["data"]["manifestSha256"]:
        raise ValueError("dataset source manifest changed")
    project_hashes = manifest["projectSnapshot"]["files"]
    for group in GROUPS:
        project = directory / "projects" / group
        actual = {path.relative_to(project).as_posix(): digest(path)
                  for path in project.rglob("*") if path.is_file() and ".wren" not in path.parts}
        if actual != project_hashes:
            raise ValueError("semantic project snapshot changed")
    legacy_hashes = manifest["legacyProjectSnapshot"]["files"]
    for group in GROUPS:
        project = directory / "legacy-projects" / group
        actual = {path.relative_to(project).as_posix(): digest(path)
                  for path in project.rglob("*") if path.is_file() and ".wren" not in path.parts}
        if actual != legacy_hashes:
            raise ValueError("legacy regression project snapshot changed")
    if role_policy_hashes() != manifest["rolePolicyHashes"]:
        raise ValueError("benchmark role policy hash mismatch")
    return manifest


def _latest_record(directory: Path, job: str) -> tuple[dict[str, Any] | None, Path | None]:
    paths = sorted((directory / "records" / job).glob("[0-9][0-9][0-9][0-9].json"))
    if not paths:
        return None, None
    path = paths[-1]
    record = read(path)
    if record.get("status") == "running":
        record["status"] = "timeout"
    return record, path


def _load_review(directory: Path, job: str, record: dict[str, Any], record_path: Path) -> None:
    review_path = directory / "reviews" / f"{job}.json"
    if not review_path.is_file():
        record["audit"] = {"status": "failed", "reasons": ["review evidence missing"],
                           "observedModels": [], "observedReasoning": []}
        record["manualReview"] = {"status": "pending"}
        return
    review = read(review_path)
    trace_path = Path(review.get("tracePath", ""))
    expected_model = "gpt-6-luna"
    expected_reasoning = "max"
    valid = (review.get("jobId") == job
        and review.get("recordSha256") == digest(record_path)
        and trace_path.is_file()
        and review.get("audit", {}).get("traceSha256") == digest(trace_path)
        and review.get("audit", {}).get("status") == "passed"
        and review.get("audit", {}).get("observedModels") == [expected_model]
        and review.get("audit", {}).get("observedReasoning") == [expected_reasoning])
    if valid:
        record["audit"] = review["audit"]
        record["manualReview"] = review.get("manualReview", {})
    else:
        record["audit"] = {**review.get("audit", {}), "status": "failed",
                            "reasons": review.get("audit", {}).get("reasons", []) + ["stale, unobservable or mismatched review evidence"]}
        record["manualReview"] = {"status": "pending"}


def summarize_run(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = verify_run_freeze(directory)
    questions = read(directory / "corpus.json")["questions"]
    schedule = read(directory / "schedule.json")
    oracle_path = directory / "oracle-results.json"
    oracle_results = read(oracle_path) if oracle_path.is_file() else {}
    records = []
    provenance = []
    for item in schedule:
        record, path = _latest_record(directory, item["id"])
        if record is None:
            record = {**item, "status": "missing"}
        else:
            if any(record.get(key) != item[key] for key in ("questionId", "group", "round")):
                raise ValueError("job record differs from frozen schedule")
            if path is not None:
                provenance.append({"jobId": item["id"], "recordSha256": digest(path)})
                _load_review(directory, item["id"], record, path)
        records.append(record)
    from evaluation import summarize
    report = summarize(questions, schedule, records, oracle_results,
                       expected_model=manifest["model"]["requested"],
                       expected_reasoning=manifest["model"]["reasoningRequested"])
    report["campaign"] = {"benchmarkId": manifest["benchmarkId"], "runId": manifest["runId"],
        "createdAt": utc(), "manifestSha256": digest(directory / "manifest.json"),
        "scheduleSha256": manifest["scheduleSha256"], "corpusSha256": manifest["corpusSha256"],
        "baselineCommit": manifest["baseline"]["commit"], "candidateCommit": manifest["candidate"]["commit"],
        "datasetManifestSha256": manifest["data"]["manifestSha256"],
        "rolePolicyHashes": manifest["rolePolicyHashes"],
        "runtime": manifest["runtime"], "model": manifest["model"],
        "recordProvenance": provenance}
    safe = make_safe_export(report, manifest)
    return report, safe


def _safe_metrics(value: dict[str, Any]) -> dict[str, Any]:
    keys = ("expected", "statuses", "finalCorrect", "finalAccuracy", "firstSqlExpected",
        "firstSqlApplicable", "firstSqlCorrect", "firstSqlAccuracy", "firstSqlUnverified",
        "firstSqlMissing", "firstSqlIncorrect", "firstSqlNotApplicable",
        "firstSqlTimedOut",
        "repairedSqlCorrect", "repairedSqlAccuracy",
        "oracleUnavailable", "permissionDenialsCorrect", "blockedCorrect", "silentErrors",
        "omissions", "permissionFailures", "reviewPending", "modelUnobservableOrDrifted",
        "reasoningUnobservableOrDrifted", "clarificationCorrect", "toolCalls", "durationMs",
        "toolDurationMs")
    return {key: value[key] for key in keys if key in value}


def make_safe_export(report: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Console-feedback artifact: versions and aggregates only, no run bodies."""
    return {"schemaVersion": 1, "kind": "business-trust-v1-safe-summary",
        "runId": manifest["runId"], "reportSha256": canonical_hash(report),
        "versions": {"baseline": manifest["baseline"]["commit"],
                     "candidate": manifest["candidate"]["commit"],
                     "corpusSha256": manifest["corpusSha256"],
                     "scheduleSha256": manifest["scheduleSha256"],
                     "datasetManifestSha256": manifest["data"]["manifestSha256"],
                     "model": manifest["model"]["requested"],
                     "reasoning": manifest["model"]["reasoningRequested"]},
        "groups": {group: _safe_metrics(metrics) for group, metrics in report["groups"].items()},
        "rounds": {round_id: {group: _safe_metrics(metrics) for group, metrics in pair.items()}
                   for round_id, pair in report["rounds"].items()},
        "scenarios": {scenario: {group: _safe_metrics(metrics) for group, metrics in pair.items()}
                      for scenario, pair in report["scenarios"].items()},
        "languages": {language: {group: _safe_metrics(metrics) for group, metrics in pair.items()}
                      for language, pair in report["languages"].items()},
        "actors": {actor: {group: _safe_metrics(metrics) for group, metrics in pair.items()}
                   for actor, pair in report["actors"].items()},
        "promotionGates": report["promotionGates"],
        "recommendation": report["recommendation"]}


def summarize_to_files(directory: Path) -> dict[str, str]:
    report, safe = summarize_run(directory)
    report_path = directory / "reports" / f"report-{uuid.uuid4().hex}.json"
    write_new(report_path, report)
    safe["reportSha256"] = digest(report_path)
    safe_path = directory / "reports" / f"console-feedback-{uuid.uuid4().hex}.json"
    write_new(safe_path, safe)
    return {"report": str(report_path), "safeSummary": str(safe_path),
            "reportSha256": digest(report_path), "safeSummarySha256": digest(safe_path),
            "expected": report["expected"], "recommendation": report["recommendation"]}


def _preflight(*, baseline: Path, candidate: Path, data_manifest: Path | None,
               database_url_env: str, port_base: int) -> dict[str, Any]:
    output: dict[str, Any] = {"schemaVersion": 1, "benchmarkId": "business-trust-v1"}
    try:
        verify_baseline(baseline.resolve(), BUNDLE_ROOT / BASELINE_SOURCE_MANIFEST)
        output["baselineHashVerified"] = True
    except Exception:
        output["baselineHashVerified"] = False
    try:
        validate_corpus(read(CORPUS_PATH))
        output["corpusValid"] = True
        output["corpusSha256"] = digest(CORPUS_PATH)
    except Exception:
        output["corpusValid"] = False
    output["candidateIsCurrentCheckout"] = candidate.resolve() == REPO_ROOT.resolve()
    output["candidateCommit"] = repo_commit(candidate) if output["candidateIsCurrentCheckout"] else None
    path = (data_manifest or HISTORICAL_DEFAULT / ".benchmark-data/hr-enterprise/generated/manifest.json").resolve()
    output["datasetManifestPresent"] = path.is_file()
    output["datasetManifestSha256"] = digest(path) if path.is_file() else None
    try:
        runtime_freeze = verify_runtime_freeze(path)
        output["baselineRuntimeVerified"] = True
        output["baselineRuntimeManifestSha256"] = runtime_freeze["sha256"]
    except Exception:
        runtime_freeze = None
        output["baselineRuntimeVerified"] = False
    try:
        embedding_freeze = verify_embedding_freeze()
        output["embeddingCacheVerified"] = True
        output["embeddingModel"] = embedding_freeze["model"]
        output["embeddingRevision"] = embedding_freeze["revision"]
        output["embeddingFilesSha256"] = embedding_freeze["sha256"]
    except Exception:
        output["embeddingCacheVerified"] = False
    connection = None
    try:
        connection = database_connection(database_url_env)
        with socket.create_connection((connection["host"], connection["port"]), timeout=1.5):
            output["postgresReachable"] = True
    except OSError:
        output["postgresReachable"] = False
    except ValueError:
        output["postgresReachable"] = False
    output["databaseUrlEnv"] = _database_url_env_name(database_url_env)
    output["postgresPort"] = connection["port"] if connection else None
    output["databaseUrlConfigured"] = connection is not None
    output["databasePortMatchesRuntime"] = bool(connection and runtime_freeze
        and connection["port"] == runtime_freeze["manifest"].get("databasePort"))
    output["python"] = sys.version.split()[0]
    versions = {}
    for package in ("wrenai", "mcp", "psycopg", "sqlglot", "PyYAML", "sentence-transformers"):
        try:
            versions[package] = importlib.metadata.version(package)
        except Exception:
            versions[package] = None
    output["packages"] = versions
    output["pinnedWren"] = versions["wrenai"] == "0.13.2"
    output["hybridRuntimeInstalled"] = versions["sentence-transformers"] is not None
    output["modelConfig"] = {"model": "gpt-6-luna", "reasoning": "max",
                             "observabilityGate": "required in every Codex JSONL review",
                             "embeddingProvider": "sentence-transformers",
                             "embeddingRevision": EMBEDDING_MODEL_REVISION,
                             "embeddingOffline": True}
    ports = [port_base + offset for offset in range(5)]
    port_checks = {}
    for port in ports:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            port_checks[str(port)] = True
        except OSError:
            port_checks[str(port)] = False
        finally:
            probe.close()
    output["benchmarkPortsFree"] = all(port_checks.values())
    output["portChecks"] = port_checks
    output["ready"] = all(output.get(key) is True for key in (
        "baselineHashVerified", "corpusValid", "candidateIsCurrentCheckout",
        "datasetManifestPresent", "baselineRuntimeVerified", "postgresReachable", "databaseUrlConfigured",
        "databasePortMatchesRuntime", "embeddingCacheVerified", "pinnedWren",
        "hybridRuntimeInstalled", "benchmarkPortsFree"))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    def paths(command: argparse.ArgumentParser) -> None:
        command.add_argument("--baseline", type=Path, default=BASELINE_DEFAULT)
        command.add_argument("--candidate", type=Path, default=REPO_ROOT)
        command.add_argument("--data-manifest", type=Path)
        command.add_argument("--database-url-env", default=DEFAULT_DATABASE_URL_ENV)
        command.add_argument("--port-base", type=int, default=DEFAULT_PORT_BASE)
        command.add_argument("--runs-root", type=Path, default=RUNS_ROOT)
    preflight = sub.add_parser("preflight")
    paths(preflight)
    freeze = sub.add_parser("freeze")
    paths(freeze)
    freeze.add_argument("--run-id", required=True)
    freeze.add_argument("--model", default="gpt-6-luna")
    freeze.add_argument("--reasoning", default="max")
    freeze.add_argument("--recovery-protocol",
                        choices=[recovery_checkpoint.RECOVERY_PROTOCOL["mode"]])
    preaccept = sub.add_parser("preaccept")
    paths(preaccept)
    preaccept.add_argument("--run-id", required=True)
    preaccept.add_argument("--recovery-protocol",
                           choices=[recovery_checkpoint.RECOVERY_PROTOCOL["mode"]])
    verify = sub.add_parser("verify-freeze")
    verify.add_argument("--run-dir", required=True, type=Path)
    serve_cmd = sub.add_parser("serve")
    serve_cmd.add_argument("--run-dir", required=True, type=Path)
    resume_cmd = sub.add_parser("resume")
    resume_cmd.add_argument("--run-dir", required=True, type=Path)
    summarize = sub.add_parser("summarize")
    summarize.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            result = _preflight(baseline=args.baseline, candidate=args.candidate,
                data_manifest=args.data_manifest, database_url_env=args.database_url_env,
                port_base=args.port_base)
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["ready"] else 2
        if args.command in {"freeze", "preaccept"}:
            result = freeze_run(args.run_id, baseline=args.baseline, candidate=args.candidate,
                data_manifest=args.data_manifest, database_url_env=args.database_url_env,
                port_base=args.port_base,
                model=getattr(args, "model", "gpt-6-luna"),
                reasoning=getattr(args, "reasoning", "max"),
                runs_root=args.runs_root,
                execution_mode=("preacceptance_only" if args.command == "preaccept"
                                else "official_campaign"),
                recovery_protocol=args.recovery_protocol)
            print(json.dumps({"runDir": str(result), "frozen": True,
                "executionMode": "preacceptance_only" if args.command == "preaccept"
                                else "official_campaign",
                "workerDispatchAllowed": args.command == "freeze"}, ensure_ascii=False))
            return 0
        if args.command == "verify-freeze":
            manifest = verify_run_freeze(args.run_dir.resolve())
            print(json.dumps({"runId": manifest["runId"], "verified": True,
                              "manifestSha256": digest(args.run_dir / "manifest.json")}, ensure_ascii=False))
            return 0
        if args.command == "serve":
            serve(args.run_dir.resolve())
            return 0
        if args.command == "resume":
            serve(args.run_dir.resolve(), resume=True)
            return 0
        result = summarize_to_files(args.run_dir.resolve())
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Deliberately omit arbitrary exception text because lower layers can
        # include credentials, SQL, row contents, or filesystem paths.
        print(f"BTV_ERROR: {type(error).__name__}: prerequisite or evidence validation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

