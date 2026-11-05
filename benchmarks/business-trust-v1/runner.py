"""One-shot fresh Codex JSONL worker launcher for a prepared BTV run."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiment import read, run_path, verify_run_freeze, write_new


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def codex_command() -> str:
    configured = os.environ.get("BTV_CODEX")
    resolved = configured or shutil.which("codex")
    if not resolved:
        raise ValueError("Codex CLI is unavailable")
    return resolved


def broker_ready(port: int) -> bool:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            value = json.loads(response.read())
            return response.status == 200 and value.get("ready") is True
    except (OSError, urllib.error.URLError, ValueError):
        return False


def render_prompt(template: str, *, worker_dir: Path, job_id: str, broker_port: int,
                  python: str, client: Path, nonce: str) -> str:
    directory = worker_dir.resolve().as_posix()
    client_path = client.resolve().as_posix()
    return (template.replace("{PYTHON}", python.replace("\\", "/"))
        .replace("{WORKER_CLIENT}", client_path)
        .replace("{JOB_ID}", job_id)
        .replace("{BROKER_PORT}", str(broker_port))
        .replace("{JOB_NONCE}", nonce)
        .replace("{EMPTY_JOB_DIRECTORY}", directory))


def worker_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Remove coordinator and database secrets before starting an external worker."""
    environment = dict(os.environ if source is None else source)
    for name in list(environment):
        upper = name.upper()
        if (upper.startswith(("BTV_", "BUSINESS_TRUST_", "PG", "POSTGRES_", "SEMARAIL_"))
            or upper in {"DATABASE_URL", "WREN_DATABASE_URL"}):
            environment.pop(name, None)
    environment.pop("CODEX_THREAD_ID", None)
    return environment


def run_one(*, codex: str, run_dir: Path, job: dict[str, Any], worker_dir: Path,
            prompt_template: str,
            model: str, reasoning: str, broker_port: int, python: str,
            worker_client: Path, nonce: str) -> dict[str, Any]:
    job_id = job["id"]
    workdir = worker_dir.resolve()
    if not workdir.is_dir() or any(workdir.iterdir()):
        raise ValueError("worker workspace must be a fresh empty directory")
    trace_path = run_dir / "traces" / f"{job_id}.jsonl"
    stderr_path = run_dir / "traces" / f"{job_id}.stderr.txt"
    prompt = render_prompt(prompt_template, worker_dir=workdir, job_id=job_id,
        broker_port=broker_port, python=python, client=worker_client, nonce=nonce)
    args = [codex, "exec", "--json", "--ephemeral", "--ignore-user-config",
        "--ignore-rules", "--model", model,
        "--config", f'model_reasoning_effort="{reasoning}"',
        "--cd", str(workdir), "--skip-git-repo-check", "--sandbox", "read-only", "-"]
    started = time.monotonic()
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("xb") as trace, stderr_path.open("xb") as stderr:
        try:
            completed = subprocess.run(args, input=prompt.encode("utf-8"),
                stdout=trace, stderr=stderr, cwd=workdir, timeout=300,
                check=False, env=worker_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            status = "complete" if completed.returncode == 0 else "process_error"
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            status, exit_code = "timeout", None
        except OSError:
            status, exit_code = "process_error", None
    return {"jobId": job_id, "status": status, "exitCode": exit_code,
            "elapsedMs": round((time.monotonic() - started) * 1000, 1),
            "tracePath": str(trace_path), "stderrPath": str(stderr_path),
            "promptSha256": __import__("hashlib").sha256(prompt.encode("utf-8")).hexdigest()}


def run_workers(directory: Path, *, max_workers: int = 2) -> dict[str, Any]:
    directory = directory.resolve()
    manifest = verify_run_freeze(directory)
    if manifest.get("workerDispatchAllowed") is not True:
        raise ValueError("preacceptance-only run cannot dispatch benchmark workers")
    if (directory / "app-dispatch-start.json").exists():
        raise ValueError("app-dispatched run cannot also use the Codex CLI runner")
    if max_workers != 2:
        raise ValueError("business-trust-v1 fixes worker concurrency at two")
    runner_start = directory / "runner-start.json"
    runner_result = directory / "runner-result.json"
    if runner_start.exists() or runner_result.exists():
        raise ValueError("worker runner is one-shot; create a new frozen run for another attempt")
    codex = codex_command()
    broker_port = manifest["ports"]["broker"]
    if not broker_ready(broker_port):
        raise ValueError("trusted coordinator is not ready")
    schedule = read(directory / "schedule.json")
    nonces = read(directory / "dispatch-nonces.json")
    template = (directory / "worker-prompt.md").read_text(encoding="utf-8")
    client = directory.parent.parent.parent / "benchmarks/business-trust-v1/worker_client.py"
    # Runs may be stored outside the repository, so resolve the frozen candidate
    # path from the manifest rather than deriving it from the artifact location.
    client = Path(manifest["candidate"]["path"]) / "benchmarks/business-trust-v1/worker_client.py"
    python = sys.executable
    if not client.is_file():
        raise ValueError("frozen worker transport is unavailable")
    worker_root = Path(tempfile.mkdtemp(prefix=f"business-trust-v1-{manifest['runId']}-")).resolve()
    if worker_root.is_relative_to(Path(manifest["candidate"]["path"]).resolve()):
        raise ValueError("external worker workspace unexpectedly resides inside the candidate checkout")
    worker_directories = {}
    for item in schedule:
        worker_dir = worker_root / item["id"]
        worker_dir.mkdir(parents=False, exist_ok=False)
        worker_directories[item["id"]] = str(worker_dir.resolve())
    trace_dir = directory / "traces"
    trace_dir.mkdir(exist_ok=False)
    worker_setup = {"schemaVersion": 1, "runId": manifest["runId"],
        "startedAt": utc(), "scheduleSha256": manifest["scheduleSha256"],
        "model": manifest["model"]["requested"],
        "reasoning": manifest["model"]["reasoningRequested"],
        "codexExecutable": str(Path(codex).resolve()), "pythonExecutable": Path(python).name,
        "pythonPath": str(Path(python).resolve()),
        "workerClientPath": str(client.resolve()), "workerRoot": str(worker_root),
        "workerDirectories": worker_directories,
        "maxConcurrent": max_workers, "deadlineSeconds": 300,
        "promptTemplateSha256": manifest["promptTemplateSha256"],
        "transportPath": client.as_posix(),
        "notice": "one fresh, ephemeral Codex process per scheduled job; no retries"}
    write_new(runner_start, worker_setup)
    results: list[dict[str, Any]] = []
    # Submit entries in their frozen interleaved order; only two processes may
    # run concurrently. Futures are consumed in schedule order for stable output.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending: list[tuple[dict[str, Any], concurrent.futures.Future[dict[str, Any]]]] = []
        for item in schedule:
            future = pool.submit(run_one, codex=codex, run_dir=directory, job=item,
                worker_dir=Path(worker_directories[item["id"]]),
                prompt_template=template, model=manifest["model"]["requested"],
                reasoning=manifest["model"]["reasoningRequested"],
                broker_port=broker_port, python=python, worker_client=client,
                nonce=nonces[item["id"]])
            pending.append((item, future))
            if len(pending) == max_workers:
                job, completed = pending.pop(0)
                results.append(completed.result())
        for _job, completed in pending:
            results.append(completed.result())
    summary = {"schemaVersion": 1, "runId": manifest["runId"], "endedAt": utc(),
        "expected": len(schedule), "started": len(results),
        "processStatuses": {key: sum(item["status"] == key for item in results)
                            for key in ("complete", "timeout", "process_error")},
        "traceFiles": sum(Path(item["tracePath"]).is_file() for item in results),
        "jobEvidence": results}
    write_new(runner_result, summary)
    return {"runId": manifest["runId"], "expected": len(schedule),
            "started": len(results), "runnerResult": str(runner_result)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--max-workers", type=int, default=2)
    args = parser.parse_args()
    try:
        print(json.dumps(run_workers(args.run_dir, max_workers=args.max_workers), ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"BTV_RUNNER_ERROR: {type(error).__name__}: prerequisite or runner validation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
