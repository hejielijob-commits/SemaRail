"""Disposable fake broker for validating a fresh app worker's transport and JSONL audit."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app_dispatch import _locate_session, _validate_session_bytes
from experiment import REPO_ROOT, digest_bytes, safe_run_id, write_new
from review import (APP_BOOTSTRAP_MODE, APP_STORED_TRANSPORT_MODE, WorkerCallAudit,
                    _assert_no_reparse_components, _require_same_path,
                    public_prompt_launcher, public_prompt_stored_transport_message,
                    stored_transport_config, stored_transport_prompt)

HERE = Path(__file__).resolve().parent
SMOKE_ROOT = REPO_ROOT / ".benchmark-data" / "business-trust-v1" / "transport-smokes"
SMOKE_JOB = "job-001"
SMOKE_QUESTION = "Confirm that this disposable broker returns its fixed smoke context."
SMOKE_MODEL = "gpt-6-luna"
SMOKE_REASONING = "max"
SMOKE_TOOL = "semarail_get_context"
SMOKE_DELAY_SECONDS = 12


def _smoke_bootstrap_requirement(state: dict[str, Any], target: Path, *,
                                 worker_dir: Path, python: Path,
                                 client: Path, port: int
                                 ) -> tuple[Path, str, str, dict[str, Any] | None]:
    version = state.get("schemaVersion")
    if type(version) is not int or version not in (2, 3):
        raise ValueError("smoke bootstrap setup schema is unsupported")
    stored_transport = version == 3
    expected_mode = APP_STORED_TRANSPORT_MODE if stored_transport else APP_BOOTSTRAP_MODE
    if state.get("bootstrapMode") != expected_mode:
        raise ValueError("smoke bootstrap setup is unsupported")
    _assert_no_reparse_components(target, "smoke setup")
    _require_same_path(state.get("workerDir"), worker_dir,
        "smoke worker directory")
    if not worker_dir.is_dir() or worker_dir.name != SMOKE_JOB:
        raise ValueError("smoke worker directory is unavailable")
    worker_root = worker_dir.parent
    _assert_no_reparse_components(worker_root, "smoke worker root")
    if worker_root.resolve(strict=True).is_relative_to(REPO_ROOT.resolve()):
        raise ValueError("smoke worker directory must be outside the repository")
    if {entry.name for entry in worker_root.iterdir()} != {SMOKE_JOB, "prompts"}:
        raise ValueError("smoke worker root contents differ")
    if type(port) is not int or not 1024 <= port <= 65530:
        raise ValueError("smoke broker port is invalid")
    if state.get("port") != port:
        raise ValueError("smoke broker port differs from its prepared state")
    if not isinstance(state.get("nonce"), str) or not state["nonce"]:
        raise ValueError("smoke nonce is missing")
    _require_same_path(state.get("pythonPath"), python,
        "smoke Python executable")
    expected_client = HERE / "worker_client.py"
    client_path = _require_same_path(client, expected_client,
        "smoke worker client")

    expected_prompt_path = worker_root / "prompts" / f"{SMOKE_JOB}.md"
    prompt_path = _require_same_path(state.get("publicPromptPath"), expected_prompt_path,
        "smoke public prompt")
    if {entry.name for entry in expected_prompt_path.parent.iterdir()} != {f"{SMOKE_JOB}.md"}:
        raise ValueError("smoke public prompt directory contents differ")
    if not prompt_path.is_file():
        raise ValueError("smoke public prompt is unavailable")
    expected_prompt = _prompt(python=python, client=client_path,
        worker_dir=worker_dir, port=port, nonce=state["nonce"])
    expected_config = None
    if stored_transport:
        smoke_id = state.get("smokeId")
        if not isinstance(smoke_id, str) or not smoke_id:
            raise ValueError("smoke stored transport identity is unavailable")
        expected_config = stored_transport_config(run_id=smoke_id, job_id=SMOKE_JOB,
            nonce=state["nonce"], python=python, worker_client=client_path,
            worker_dir=worker_dir, broker_port=port)
        if state.get("storedTransportConfig") != expected_config:
            raise ValueError("smoke stored transport configuration differs")
        expected_prompt = stored_transport_prompt(expected_prompt, expected_config,
            expected_operations=("begin", "tool", "final"), adapt_instructions=False)
    expected_prompt_bytes = expected_prompt.replace("\r\n", "\n").replace(
        "\r", "\n").encode("utf-8")
    prompt_bytes = prompt_path.read_bytes()
    prompt_hash = hashlib.sha256(expected_prompt_bytes).hexdigest()
    if (prompt_bytes != expected_prompt_bytes
        or state.get("publicPromptSha256") != prompt_hash
        or state.get("promptSha256") != prompt_hash):
        raise ValueError("smoke public prompt differs from its prepared rendering")

    launcher_path = _require_same_path(target / "one-shot-prompt.md",
        target / "one-shot-prompt.md", "smoke launcher")
    if not launcher_path.is_file():
        raise ValueError("smoke launcher file is unavailable")
    launcher_bytes = launcher_path.read_bytes()
    expected_launcher_text = (public_prompt_stored_transport_message(
        prompt_path, worker_dir, expected_config) if stored_transport
        else public_prompt_launcher(prompt_path, worker_dir))
    expected_launcher = expected_launcher_text.encode("utf-8")
    launcher_hash = hashlib.sha256(expected_launcher).hexdigest()
    if (launcher_bytes != expected_launcher
        or state.get("launcherSha256") != launcher_hash):
        raise ValueError("smoke launcher content or hash differs")
    return prompt_path, prompt_hash, launcher_hash, expected_config


def _smoke_schema_version(state: dict[str, Any]) -> int:
    version = state.get("schemaVersion")
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError("smoke setup schema is unsupported")
    return version


def _wrapper(*, python: Path, client: Path, worker_dir: Path,
             port: int, operation: str, request: str) -> str:
    if operation not in {"begin", "tool", "final"}:
        raise ValueError("unknown smoke transport operation")
    command = (f"@'\n{request}\n'@ | & '{python.as_posix()}' -X utf8 "
        f"'{client.as_posix()}' {SMOKE_JOB} {operation} - --port {port}")
    return "text(await tools.exec_command(" + json.dumps({
        "cmd": command, "workdir": worker_dir.as_posix(), "max_output_tokens": 12000,
    }) + "));"


def _prompt(*, python: Path, client: Path, worker_dir: Path,
            port: int, nonce: str) -> str:
    begin = _wrapper(python=python, client=client, worker_dir=worker_dir,
        port=port, operation="begin", request=json.dumps({"nonce": nonce}, separators=(",", ":")))
    tool = _wrapper(python=python, client=client, worker_dir=worker_dir,
        port=port, operation="tool", request=json.dumps({"name": SMOKE_TOOL,
            "arguments": {"question": SMOKE_QUESTION, "context_version": 2}}, separators=(",", ":")))
    final = _wrapper(python=python, client=client, worker_dir=worker_dir,
        port=port, operation="final", request=json.dumps({"answer": "transport smoke passed",
            "rows": [], "columns": [], "scope": "smoke only", "conditions": [],
            "uncertain": False}, separators=(",", ":")))
    return f"""This is one disposable transport smoke check. It is excluded from business-trust-v1 results and makes no claim about HR data, product behavior, baseline, or candidate. Use no web, file, database, or tool other than the three exact `functions.exec` wrappers below and the single conditional read-only poll described below. Make calls one at a time and in order. Each `exec_command` wrapper is complete: keep its Python executable, worker-client path, job directory, job ID, broker port, and literal operation unchanged. A tool name belongs in the request JSON `name` field; the transport operation remains the literal `tool`. Do not edit or repair commands or paths.\n\nFirst call `begin`:\n\n```js\n{begin}\n```\n\nThe broker returns one fake smoke question and a tool schema. Call `tool` once with the exact returned question and `context_version: 2`:\n\n```js\n{tool}\n```\n\nThe fake broker deliberately holds this tool response for {SMOKE_DELAY_SECONDS} seconds. If the top-level result metadata from this exact `exec_command` call contains a `session_id`, poll only that ID with this exact empty read-only wait; fill the integer from that result metadata, never from command output text or another source:\n\n```js\ntext(await tools.write_stdin({{\"session_id\":<SESSION_ID_FROM_THIS_EXEC_RESULT>,\"chars\":\"\",\"yield_time_ms\":30000,\"max_output_tokens\":12000}}));\n```\n\nRepeat the same poll only if that poll's top-level result metadata still contains the same `session_id`. Stop polling when it no longer does. Never send characters, poll any other session, or terminate a process. If no top-level `session_id` is returned, do not poll.\n\nThen call `final` once with the fixed smoke-only answer:\n\n```js\n{final}\n```\n\nIf a shell, worker-client, unknown-operation, path, transport, or poll error occurs, stop immediately and report failure; do not retry. Do not issue another operation. After final, report only `{SMOKE_JOB} complete`.\n"""


def prepare(smoke_id: str, port: int, *,
            bootstrap_file_prompts: bool = False,
            bootstrap_stored_transport: bool = False) -> dict[str, Any]:
    if bootstrap_file_prompts and bootstrap_stored_transport:
        raise ValueError("smoke bootstrap modes are mutually exclusive")
    safe_run_id(smoke_id)
    if not 1024 <= port <= 65530:
        raise ValueError("smoke broker port is outside the supported range")
    target = SMOKE_ROOT / smoke_id
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()
    target.mkdir(parents=True, exist_ok=False)
    worker_root = Path(tempfile.mkdtemp(prefix=f"btv-transport-{smoke_id}-")).resolve()
    if worker_root.is_relative_to(REPO_ROOT.resolve()):
        raise ValueError("smoke worker directory must be outside the repository")
    worker_dir = worker_root / SMOKE_JOB
    worker_dir.mkdir()
    python = Path(sys.executable).resolve()
    client = HERE / "worker_client.py"
    nonce = secrets.token_urlsafe(24)
    prompt = _prompt(python=python, client=client, worker_dir=worker_dir,
        port=port, nonce=nonce)
    prompt_path = target / "one-shot-prompt.md"
    state = {"schemaVersion": 1, "smokeId": smoke_id, "status": "prepared",
        "port": port, "jobId": SMOKE_JOB, "nonce": nonce,
        "workerDir": str(worker_dir), "pythonPath": str(python),
        "workerClientPath": str(client.resolve()),
        "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "modelRequested": SMOKE_MODEL, "reasoningRequested": SMOKE_REASONING,
        "maxCalls": 3, "delaySeconds": SMOKE_DELAY_SECONDS,
        "maxPollCalls": 1, "deadlineSeconds": 300}
    if bootstrap_file_prompts or bootstrap_stored_transport:
        prompt = prompt.replace("\r\n", "\n").replace("\r", "\n")
        config = None
        if bootstrap_stored_transport:
            config = stored_transport_config(run_id=smoke_id, job_id=SMOKE_JOB,
                nonce=nonce, python=python, worker_client=client,
                worker_dir=worker_dir, broker_port=port)
            prompt = stored_transport_prompt(prompt, config,
                expected_operations=("begin", "tool", "final"), adapt_instructions=False)
        prompt_bytes = prompt.encode("utf-8")
        public_prompt_dir = worker_root / "prompts"
        public_prompt_dir.mkdir(parents=False, exist_ok=False)
        public_prompt_path = public_prompt_dir / f"{SMOKE_JOB}.md"
        public_prompt_path.write_bytes(prompt_bytes)
        launcher = (public_prompt_stored_transport_message(
            public_prompt_path, worker_dir, config) if bootstrap_stored_transport
            else public_prompt_launcher(public_prompt_path, worker_dir))
        launcher_bytes = launcher.encode("utf-8")
        prompt_path.write_bytes(launcher_bytes)
        state.update({
            "schemaVersion": 3 if bootstrap_stored_transport else 2,
            "bootstrapMode": (APP_STORED_TRANSPORT_MODE if bootstrap_stored_transport
                else APP_BOOTSTRAP_MODE),
            "publicPromptPath": str(public_prompt_path.resolve()),
            "publicPromptSha256": hashlib.sha256(prompt_bytes).hexdigest(),
            "launcherSha256": hashlib.sha256(launcher_bytes).hexdigest(),
        })
        state["promptSha256"] = hashlib.sha256(prompt_bytes).hexdigest()
        if bootstrap_stored_transport:
            state["storedTransportConfig"] = config
    else:
        prompt_path.write_text(prompt, encoding="utf-8", newline="\n")
    write_new(target / "state.json", state)
    result = {"smokeId": smoke_id, "port": port, "prompt": str(prompt_path),
        "state": str(target / "state.json"), "workerDir": str(worker_dir),
        "excludedFromCampaign": True}
    if bootstrap_file_prompts or bootstrap_stored_transport:
        result.update({"bootstrapMode": (APP_STORED_TRANSPORT_MODE if bootstrap_stored_transport
                else APP_BOOTSTRAP_MODE),
            "publicPrompt": str(public_prompt_path),
            "publicPromptSha256": state["publicPromptSha256"],
            "launcherSha256": state["launcherSha256"]})
    return result


def _save_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    temporary.replace(path)


def serve(smoke_id: str) -> None:
    target = SMOKE_ROOT / safe_run_id(smoke_id)
    state_path = target / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "prepared":
        raise ValueError("smoke server is one-shot; prepare a new smoke ID")
    if state.get("delaySeconds") != SMOKE_DELAY_SECONDS or state.get("maxPollCalls") != 1:
        raise ValueError("smoke delay protocol differs from the prepared prompt")
    state["status"] = "running"
    state["startedAt"] = datetime.now(timezone.utc).isoformat()
    state["operations"] = []
    state["toolNames"] = []
    state["workerThreadId"] = None
    lock = threading.RLock()
    _save_state(state_path, state)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            with lock:
                try:
                    if self.headers.get("Host") != f"127.0.0.1:{state['port']}":
                        raise ValueError("host header mismatch")
                    if self.headers.get("Origin"):
                        raise ValueError("origin header forbidden")
                    path = self.path.strip("/").split("/")
                    if len(path) != 2 or path[0] != SMOKE_JOB:
                        raise ValueError("unexpected smoke route")
                    operation = path[1]
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 128_000:
                        raise ValueError("request size invalid")
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ValueError("request object required")
                    raw_thread_id = self.headers.get("X-BTV-Thread-ID")
                    if not raw_thread_id:
                        raise ValueError("trusted thread ID required")
                    thread_id = str(uuid.UUID(raw_thread_id))
                    if thread_id != raw_thread_id.casefold():
                        raise ValueError("trusted thread ID invalid")
                    if state["workerThreadId"] is not None and state["workerThreadId"] != thread_id:
                        raise ValueError("thread ID changed")
                    if operation == "begin":
                        if state["operations"] or body != {"nonce": state["nonce"]}:
                            raise ValueError("begin challenge mismatch")
                        state["workerThreadId"] = thread_id
                        state["operations"].append("begin")
                        result = {"schemaVersion": 1, "jobId": SMOKE_JOB,
                            "actor": "smoke", "language": "en",
                            "question": SMOKE_QUESTION,
                            "tools": [{"name": SMOKE_TOOL,
                                "description": "Return this disposable transport smoke response.",
                                "inputSchema": {"type": "object",
                                    "properties": {"question": {"type": "string"},
                                        "context_version": {"type": "integer"}},
                                    "required": ["question", "context_version"],
                                    "additionalProperties": False}}],
                            "limits": {"calls": 1, "seconds": 300}}
                    elif operation == "tool":
                        if (state["operations"] != ["begin"]
                            or set(body) != {"name", "arguments"}
                            or body.get("name") != SMOKE_TOOL
                            or body.get("arguments") != {"question": SMOKE_QUESTION,
                                "context_version": 2}):
                            raise ValueError("smoke tool request mismatch")
                        time.sleep(SMOKE_DELAY_SECONDS)
                        state["operations"].append("tool")
                        state["toolNames"].append(SMOKE_TOOL)
                        result = {"schemaVersion": 2, "status": "success",
                            "question": SMOKE_QUESTION,
                            "indexStatus": {"status": "ready", "backend": "fake-smoke"},
                            "schema": {"models": []}}
                    elif operation == "final":
                        expected = {"answer", "rows", "columns", "scope", "conditions", "uncertain"}
                        if (state["operations"] != ["begin", "tool"]
                            or set(body) != expected or body.get("answer") != "transport smoke passed"
                            or body.get("rows") != [] or body.get("columns") != []
                            or body.get("scope") != "smoke only" or body.get("conditions") != []
                            or body.get("uncertain") is not False):
                            raise ValueError("smoke final request mismatch")
                        state["operations"].append("final")
                        state["status"] = "complete"
                        state["endedAt"] = datetime.now(timezone.utc).isoformat()
                        result = {"recorded": True}
                    else:
                        raise ValueError("operation is not allowed")
                    _save_state(state_path, state)
                    payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
                    status = 200
                except Exception:
                    payload, status = b'{"error":"request_rejected"}', 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", state["port"]), Handler)
    server.daemon_threads = True
    try:
        print(f"BTV_TRANSPORT_SMOKE_READY port={state['port']} excludedFromCampaign=true", flush=True)
        server.serve_forever(poll_interval=.5)
    finally:
        server.server_close()


def verify(smoke_id: str, thread_id: str | None = None) -> dict[str, Any]:
    target = SMOKE_ROOT / safe_run_id(smoke_id)
    state_path = target / "state.json"
    _assert_no_reparse_components(target, "smoke setup")
    _assert_no_reparse_components(state_path, "smoke state")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "complete" or state.get("operations") != ["begin", "tool", "final"]:
        raise ValueError("smoke broker did not record the complete one-shot operation sequence")
    observed_thread = state.get("workerThreadId")
    if not isinstance(observed_thread, str) or (thread_id and thread_id != observed_thread):
        raise ValueError("smoke thread ID differs from the trusted transport record")
    trace_path = _locate_session(observed_thread, None)
    trace_bytes = trace_path.read_bytes()
    _validate_session_bytes(trace_bytes, observed_thread)
    models: list[str] = []
    efforts: list[str] = []
    worker_dir = Path(state["workerDir"])
    python = Path(state["pythonPath"])
    client = Path(state["workerClientPath"])
    bootstrap_path: Path | None = None
    bootstrap_hash: str | None = None
    launcher_hash: str | None = None
    schema_version = _smoke_schema_version(state)
    if schema_version == 2:
        bootstrap_path, bootstrap_hash, launcher_hash, stored_config = _smoke_bootstrap_requirement(
            state, target, worker_dir=worker_dir, python=python,
            client=client, port=state.get("port"))
    elif schema_version == 3:
        bootstrap_path, bootstrap_hash, launcher_hash, stored_config = _smoke_bootstrap_requirement(
            state, target, worker_dir=worker_dir, python=python,
            client=client, port=state.get("port"))
    elif any(key in state for key in (
            "bootstrapMode", "publicPromptPath", "publicPromptSha256", "launcherSha256",
            "storedTransportConfig")):
        raise ValueError("legacy smoke setup cannot include bootstrap evidence")
    else:
        stored_config = None
    call_audit = WorkerCallAudit(job_id=SMOKE_JOB, worker_dir=worker_dir,
        python=python, worker_client=client, broker_port=state["port"],
        expected_nonce=state["nonce"], bootstrap_prompt_path=bootstrap_path,
        bootstrap_prompt_sha256=bootstrap_hash,
        stored_transport_config=stored_config)
    for line_no, line in enumerate(trace_bytes.splitlines(), 1):
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError(f"session trace has malformed JSON at line {line_no}") from None
        payload = event.get("payload") or {}
        if event.get("type") == "turn_context" and isinstance(payload, dict):
            model = payload.get("model")
            effort = next((payload.get(key) for key in (
                "reasoning_effort", "reasoningEffort", "model_reasoning_effort", "effort")
                if isinstance(payload.get(key), str) and payload.get(key)), None)
            if isinstance(model, str):
                models.append(model)
            if isinstance(effort, str):
                efforts.append(effort)
        if event.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") in {"custom_tool_call", "function_call"}:
            call_audit.accept_call(payload)
        elif payload.get("type") == "custom_tool_call_output":
            call_audit.accept_output(payload)
    call_audit.finish()
    operations = call_audit.operations
    tools = call_audit.tool_names
    if operations != ["begin", "tool", "final"] or tools != [SMOKE_TOOL]:
        raise ValueError("session JSONL does not prove the exact smoke transport sequence")
    if call_audit.poll_count != 1:
        raise ValueError("session JSONL does not prove the delayed own-session poll")
    if sorted(set(models)) != [SMOKE_MODEL] or sorted(set(efforts)) != [SMOKE_REASONING]:
        raise ValueError("session JSONL does not prove the requested model and reasoning effort")
    output = {"schemaVersion": 1, "smokeId": smoke_id,
        "status": "passed", "excludedFromCampaign": True,
        "threadId": observed_thread, "traceSha256": hashlib.sha256(trace_bytes).hexdigest(),
        "observedModels": sorted(set(models)), "observedReasoning": sorted(set(efforts)),
        "operations": operations, "toolNames": tools,
        "provenanceBoundEmptyPolls": call_audit.poll_count,
        "auditedAt": datetime.now(timezone.utc).isoformat()}
    if bootstrap_path is not None:
        output.update({"bootstrapMode": (APP_STORED_TRANSPORT_MODE if stored_config is not None
                else APP_BOOTSTRAP_MODE),
            "publicPromptReceiptVerified": call_audit.bootstrap_verified,
            "sentInputIndependentlyByteVerified": False,
            "publicPromptSha256": bootstrap_hash,
            "publicPromptReceiptSha256": call_audit.bootstrap_output_sha256,
            "launcherSha256": launcher_hash})
        if stored_config is not None:
            output["storedTransportConfigSha256"] = hashlib.sha256(json.dumps(
                stored_config, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    write_new(target / "verification.json", output)
    result = {key: output[key] for key in (
        "smokeId", "status", "excludedFromCampaign", "observedModels",
        "observedReasoning", "operations", "toolNames", "traceSha256")}
    if bootstrap_path is not None:
        result.update({"bootstrapMode": output["bootstrapMode"],
            "publicPromptReceiptVerified": output["publicPromptReceiptVerified"],
            "sentInputIndependentlyByteVerified": output["sentInputIndependentlyByteVerified"],
            "publicPromptSha256": bootstrap_hash,
            "publicPromptReceiptSha256": output["publicPromptReceiptSha256"],
            "launcherSha256": launcher_hash})
        if stored_config is not None:
            result["storedTransportConfigSha256"] = output["storedTransportConfigSha256"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--smoke-id", required=True)
    prep.add_argument("--port", type=int, default=48995)
    prep.add_argument("--bootstrap-file-prompts", action="store_true")
    prep.add_argument("--bootstrap-stored-transport", action="store_true")
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--smoke-id", required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--smoke-id", required=True)
    verify_parser.add_argument("--thread-id")
    args = parser.parse_args()
    try:
        result = (prepare(args.smoke_id, args.port,
            bootstrap_file_prompts=args.bootstrap_file_prompts,
            bootstrap_stored_transport=args.bootstrap_stored_transport) if args.command == "prepare"
            else verify(args.smoke_id, args.thread_id) if args.command == "verify"
            else serve(args.smoke_id))
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"BTV_TRANSPORT_SMOKE_ERROR: {type(error).__name__}: smoke evidence rejected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
