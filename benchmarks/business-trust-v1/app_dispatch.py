"""Export one-shot prompts and import explicitly selected Codex app-session JSONL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiment import read, verify_run_freeze, write_new
from runner import broker_ready, render_prompt
from review import (APP_BOOTSTRAP_DISPATCH_MODE, APP_BOOTSTRAP_MODE,
                    APP_STORED_TRANSPORT_DISPATCH_MODE, APP_STORED_TRANSPORT_MODE,
                    public_prompt_launcher, public_prompt_stored_transport_message,
                    stored_transport_config, stored_transport_prompt)

MAX_SESSION_BYTES = 25 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _sessions_root() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return (codex_home / "sessions").resolve()


def _locate_session(thread_id: str, explicit_path: Path | None) -> Path:
    try:
        uuid.UUID(thread_id)
    except (ValueError, AttributeError):
        raise ValueError("a valid Codex thread ID is required") from None
    root = _sessions_root()
    matches = list(root.rglob(f"*{thread_id}*.jsonl"))
    if explicit_path is not None:
        target = explicit_path.resolve()
        if not target.is_relative_to(root):
            raise ValueError("app trace must be inside the Codex sessions directory")
        if not target.is_file():
            raise ValueError("app trace file is unavailable")
        if len(matches) != 1 or matches[0].resolve() != target:
            raise ValueError("explicit app trace does not identify the broker-recorded thread")
        return target
    if len(matches) != 1:
        raise ValueError("thread ID must resolve to exactly one session JSONL")
    target = matches[0].resolve()
    if not target.is_relative_to(root):
        raise ValueError("resolved app trace escaped the Codex sessions directory")
    return target


def _validate_session_bytes(data: bytes, thread_id: str) -> None:
    if not data or len(data) > MAX_SESSION_BYTES:
        raise ValueError("app trace is empty or exceeds the import limit")
    session_ids = []
    for line_no, line in enumerate(data.splitlines(), 1):
        try:
            event = json.loads(line, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ValueError(f"app trace has malformed JSON at line {line_no}") from None
        if not isinstance(event, dict):
            raise ValueError(f"app trace has a non-object event at line {line_no}")
        payload = event.get("payload")
        if event.get("type") == "session_meta" and isinstance(payload, dict):
            session_id = payload.get("id")
            if isinstance(session_id, str):
                session_ids.append(session_id)
    if session_ids != [thread_id]:
        raise ValueError("app trace must contain exactly its declared session identity")


def export_app_jobs(run_dir: Path, job_ids: list[str] | None = None, *,
                    bootstrap_file_prompts: bool = False,
                    bootstrap_stored_transport: bool = False) -> dict[str, Any]:
    if bootstrap_file_prompts and bootstrap_stored_transport:
        raise ValueError("app prompt bootstrap modes are mutually exclusive")
    file_prompts = bootstrap_file_prompts or bootstrap_stored_transport
    directory = run_dir.resolve()
    manifest = verify_run_freeze(directory)
    if manifest.get("workerDispatchAllowed") is not True:
        raise ValueError("preacceptance-only runs cannot export benchmark worker prompts")
    if ((directory / "runner-start.json").exists()
        or (directory / "app-dispatch-start.json").exists()):
        raise ValueError("worker dispatch is one-shot; setup evidence already exists")
    broker_port = manifest["ports"]["broker"]
    if not broker_ready(broker_port):
        raise ValueError("trusted coordinator is not ready")
    schedule = read(directory / "schedule.json")
    schedule_ids = [item["id"] for item in schedule]
    selected = list(schedule_ids if not job_ids else job_ids)
    if (len(set(selected)) != len(selected)
        or not set(selected) <= set(schedule_ids)):
        raise ValueError("requested app job IDs must be unique frozen schedule positions")
    client = Path(manifest["candidate"]["path"]) / "benchmarks/business-trust-v1/worker_client.py"
    if not client.is_file():
        raise ValueError("frozen worker transport is unavailable")
    # File-backed prompts record the same canonical path they render so review
    # can reconstruct the public prompt without guessing executable aliases.
    python = (str(Path(sys.executable).resolve()) if file_prompts
        else sys.executable)
    worker_root = Path(tempfile.mkdtemp(prefix=f"business-trust-v1-app-{manifest['runId']}-")).resolve()
    if worker_root.is_relative_to(Path(manifest["candidate"]["path"]).resolve()):
        raise ValueError("app worker directories unexpectedly reside inside the candidate checkout")
    worker_directories: dict[str, str] = {}
    for job_id in schedule_ids:
        worker_dir = worker_root / job_id
        worker_dir.mkdir(parents=False, exist_ok=False)
        worker_directories[job_id] = str(worker_dir)
    prompt_template = (directory / "worker-prompt.md").read_text(encoding="utf-8")
    nonces = read(directory / "dispatch-nonces.json")
    prompt_dir = directory / "app-prompts"
    prompt_dir.mkdir(exist_ok=False)
    prompt_hashes: dict[str, str] = {}
    prompt_files: dict[str, dict[str, str]] = {}
    launcher_hashes: dict[str, str] = {}
    launcher_files: dict[str, dict[str, str]] = {}
    stored_configs: dict[str, dict[str, Any]] = {}
    public_prompt_dir: Path | None = None
    if file_prompts:
        public_prompt_dir = worker_root / "prompts"
        public_prompt_dir.mkdir(parents=False, exist_ok=False)
    for job_id in selected:
        worker_dir = Path(worker_directories[job_id])
        prompt = render_prompt(prompt_template, worker_dir=worker_dir,
            job_id=job_id, broker_port=broker_port, python=python, client=client,
            nonce=nonces[job_id])
        config: dict[str, Any] | None = None
        if bootstrap_stored_transport:
            config = stored_transport_config(run_id=manifest["runId"], job_id=job_id,
                nonce=nonces[job_id], python=Path(python), worker_client=client,
                worker_dir=worker_dir, broker_port=broker_port)
            stored_configs[job_id] = config
            prompt = stored_transport_prompt(prompt, config)
        if file_prompts:
            prompt = prompt.replace("\r\n", "\n").replace("\r", "\n")
            prompt_bytes = prompt.encode("utf-8")
            prompt_path = public_prompt_dir / f"{job_id}.md"
            prompt_path.write_bytes(prompt_bytes)
            prompt_hash = hashlib.sha256(prompt_bytes).hexdigest()
            launcher_path = prompt_dir / f"{job_id}.md"
            launcher_text = (public_prompt_stored_transport_message(prompt_path, worker_dir, config)
                if bootstrap_stored_transport else public_prompt_launcher(prompt_path, worker_dir))
            launcher_bytes = launcher_text.encode("utf-8")
            launcher_path.write_bytes(launcher_bytes)
            launcher_hash = hashlib.sha256(launcher_bytes).hexdigest()
            prompt_files[job_id] = {"path": str(prompt_path.resolve()), "sha256": prompt_hash}
            launcher_files[job_id] = {"path": str(launcher_path.resolve()), "sha256": launcher_hash}
            launcher_hashes[job_id] = launcher_hash
            prompt_hashes[job_id] = prompt_hash
        else:
            prompt_path = prompt_dir / f"{job_id}.md"
            prompt_path.write_text(prompt, encoding="utf-8", newline="\n")
            prompt_hashes[job_id] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    setup = {
        "schemaVersion": (3 if bootstrap_stored_transport else 2 if bootstrap_file_prompts else 1),
        "runId": manifest["runId"],
        "dispatchMode": (APP_STORED_TRANSPORT_DISPATCH_MODE if bootstrap_stored_transport
            else APP_BOOTSTRAP_DISPATCH_MODE if bootstrap_file_prompts
            else "Codex app fresh-session worker prompts"),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "scheduleSha256": manifest["scheduleSha256"],
        "dispatchNoncesSha256": manifest["dispatchNoncesSha256"],
        "modelRequested": manifest["model"]["requested"],
        "reasoningRequested": manifest["model"]["reasoningRequested"],
        "actualModelObserved": False,
        "actualReasoningObserved": False,
        "workerDirectories": worker_directories,
        "selectedJobIds": selected,
        "workerRoot": str(worker_root),
        "pythonPath": str(Path(python).resolve()),
        "workerClientPath": str(client.resolve()),
        "promptTemplateSha256": manifest["promptTemplateSha256"],
        "promptHashes": prompt_hashes,
        "brokerPort": broker_port,
        "maxConcurrent": manifest["maxConcurrent"],
        "deadlineSeconds": manifest["deadlineSeconds"],
        "notice": "supervisor dispatches at most two fresh app sessions; no retry or run replacement",
    }
    if file_prompts:
        setup.update({
            "bootstrapMode": (APP_STORED_TRANSPORT_MODE if bootstrap_stored_transport
                else APP_BOOTSTRAP_MODE),
            "publicPromptDirectory": str(public_prompt_dir),
            "launcherDirectory": str(prompt_dir),
            "publicPromptFiles": prompt_files,
            "launcherFiles": launcher_files,
            "launcherHashes": launcher_hashes,
        })
    if bootstrap_stored_transport:
        setup["storedTransportConfigs"] = stored_configs
    write_new(directory / "app-dispatch-start.json", setup)
    result = {"runId": manifest["runId"], "selected": len(selected),
        "expected": len(schedule), "promptDirectory": str(prompt_dir),
        "dispatchStart": str(directory / "app-dispatch-start.json"),
        "actualModelObserved": False}
    if file_prompts:
        result.update({"bootstrapMode": (APP_STORED_TRANSPORT_MODE if bootstrap_stored_transport
                else APP_BOOTSTRAP_MODE),
            "launcherDirectory": str(prompt_dir),
            "publicPromptDirectory": str(public_prompt_dir)})
    if bootstrap_stored_transport:
        result["storedTransportStoreKeys"] = {
            job_id: config["storeKey"] for job_id, config in stored_configs.items()}
    return result


def import_app_trace(run_dir: Path, job_id: str, thread_id: str | None = None,
                     trace_path: Path | None = None) -> dict[str, Any]:
    directory = run_dir.resolve()
    manifest = verify_run_freeze(directory)
    if manifest.get("workerDispatchAllowed") is not True:
        raise ValueError("preacceptance-only run cannot import worker traces")
    setup_path = directory / "app-dispatch-start.json"
    if not setup_path.is_file() or (directory / "runner-start.json").exists():
        raise ValueError("run is not configured for app-dispatched workers")
    setup = read(setup_path)
    if setup.get("runId") != manifest["runId"] or job_id not in setup.get("selectedJobIds", []):
        raise ValueError("job is not part of this app dispatch")
    if job_id not in {item["id"] for item in read(directory / "schedule.json")}:
        raise ValueError("unknown frozen job ID")
    record_paths = sorted((directory / "records" / job_id).glob("[0-9][0-9][0-9][0-9].json"))
    if not record_paths:
        raise ValueError("coordinator has no completed record for this app job")
    record = read(record_paths[-1])
    if record.get("status") not in {"complete", "invalid", "timeout"}:
        raise ValueError("app job must reach a terminal coordinator state before trace import")
    observed_thread_id = record.get("workerThreadId")
    if not isinstance(observed_thread_id, str):
        raise ValueError("trusted transport did not record an app thread ID")
    if thread_id is not None and thread_id != observed_thread_id:
        raise ValueError("requested thread ID differs from broker-recorded app identity")
    thread_id = observed_thread_id
    source = _locate_session(thread_id, trace_path)
    data = source.read_bytes()
    _validate_session_bytes(data, thread_id)
    imports_dir = directory / "app-imports"
    imports_dir.mkdir(exist_ok=True)
    target = directory / "traces" / f"{job_id}.jsonl"
    evidence_path = imports_dir / f"{job_id}.json"
    if target.exists() or evidence_path.exists():
        raise ValueError("app trace import is one-shot for each scheduled position")
    for existing in imports_dir.glob("job-*.json"):
        if read(existing).get("threadId") == thread_id:
            raise ValueError("one app session cannot be assigned to multiple jobs")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(data)
    trace_hash = hashlib.sha256(data).hexdigest()
    evidence = {"schemaVersion": 1, "jobId": job_id, "threadId": thread_id,
        "importedAt": datetime.now(timezone.utc).isoformat(),
        "traceSha256": trace_hash,
        "sourceSha256": trace_hash,
        "sourceRelativePath": source.relative_to(_sessions_root()).as_posix(),
        "sourceKind": "explicit Codex app session JSONL selected by thread ID"}
    write_new(evidence_path, evidence)
    return {"jobId": job_id, "threadId": thread_id,
        "tracePath": str(target), "traceSha256": trace_hash,
        "modelObservation": "pending strict JSONL review"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--run-dir", required=True, type=Path)
    export.add_argument("--job-id", action="append")
    export.add_argument("--bootstrap-file-prompts", action="store_true")
    export.add_argument("--bootstrap-stored-transport", action="store_true")
    imp = sub.add_parser("import")
    imp.add_argument("--run-dir", required=True, type=Path)
    imp.add_argument("--job-id", required=True)
    imp.add_argument("--thread-id")
    imp.add_argument("--trace", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "export":
            result = export_app_jobs(args.run_dir, args.job_id,
                bootstrap_file_prompts=args.bootstrap_file_prompts,
                bootstrap_stored_transport=args.bootstrap_stored_transport)
        else:
            result = import_app_trace(args.run_dir, args.job_id, args.thread_id, args.trace)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"BTV_APP_DISPATCH_ERROR: {type(error).__name__}: dispatch evidence rejected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
