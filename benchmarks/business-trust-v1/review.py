"""Fail-closed JSONL tool audit and append-only independent answer review."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiment import ALLOWED_OPTIONAL, ALLOWED_REQUIRED, read, run_path
from runner import render_prompt

WRAPPER = re.compile(r"\s*text\(await tools\.exec_command\((\{.*\})\)\);\s*", re.DOTALL)
POLL_WRAPPER = re.compile(r"\s*text\(await tools\.write_stdin\((\{.*\})\)\);\s*", re.DOTALL)
EXEC_METADATA_FIELDS = {"chunk_id", "wall_time_seconds", "session_id", "exit_code",
                        "original_token_count", "output"}
APP_BOOTSTRAP_MODE = "public-file-one-read-v1"
APP_BOOTSTRAP_DISPATCH_MODE = "Codex app fresh-session worker prompts via public-file bootstrap"
APP_STORED_TRANSPORT_MODE = "public-file-stored-transport-v1"
APP_STORED_TRANSPORT_DISPATCH_MODE = "Codex app fresh-session worker prompts via stored transport bootstrap"
STORED_TRANSPORT_MODE = "business-trust-v1-stored-transport-v1"
STORED_TRANSPORT_OPERATIONS = {"begin", "tool", "clarify", "final"}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _normalized_path(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return str(Path(value).resolve()).replace("\\", "/").casefold()


def _lexical_absolute_path(value: Any, label: str) -> Path:
    if isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} path is missing")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    return Path(os.path.abspath(path))


def _assert_no_reparse_components(path: Path, label: str) -> None:
    """Reject symlinks, junctions, and other reparse points along an evidence path."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except OSError as error:
            raise ValueError(f"{label} path is unavailable") from error
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise ValueError(f"{label} path contains a symlink or reparse point")


def _require_same_path(actual: Any, expected: Path, label: str) -> Path:
    path = _lexical_absolute_path(actual, label)
    expected_absolute = Path(os.path.abspath(expected))
    if os.path.normcase(str(path)) != os.path.normcase(str(expected_absolute)):
        raise ValueError(f"{label} path differs from its fixed location")
    _assert_no_reparse_components(path, label)
    return path


def public_prompt_read_wrapper(prompt_path: Path, worker_dir: Path) -> str:
    """Render the only permitted one-shot public prompt read for an app worker."""
    quoted_path = Path(prompt_path).resolve().as_posix().replace("'", "''")
    command = ("[Console]::Write([IO.File]::ReadAllText('" + quoted_path
        + "',[Text.Encoding]::UTF8))")
    return "text(await tools.exec_command(" + json.dumps({
        "cmd": command, "workdir": str(Path(worker_dir).resolve()),
        "max_output_tokens": 12000,
    }) + "));"


def public_prompt_launcher(prompt_path: Path, worker_dir: Path) -> str:
    """Render the short app message that reads and then delegates to a public prompt."""
    wrapper = public_prompt_read_wrapper(prompt_path, worker_dir)
    return (
        "Before reading the participant prompt, use no other tools, files, environment, or network. "
        "Make exactly this one functions.exec call to read the public prompt:\n\n"
        "```js\n" + wrapper + "\n```\n\n"
        "Then follow the returned prompt exactly and use only its specified transport. "
        "Do not repeat the read or access another file.\n"
    )


def stored_transport_store_key(run_id: str, job_id: str) -> str:
    if not isinstance(run_id, str) or not run_id or not re.fullmatch(r"job-\d{3}", job_id):
        raise ValueError("stored transport identity is invalid")
    digest = hashlib.sha256(f"{run_id}\0{job_id}".encode("utf-8")).hexdigest()[:32]
    return f"btv-stored-v1-{digest}"


def _powershell_argument(path: str) -> str:
    if not isinstance(path, str) or not path or any(char in path for char in "\r\n\0"):
        raise ValueError("stored transport path is invalid")
    return "'" + path.replace("'", "''") + "'"


def stored_transport_config(*, run_id: str, job_id: str, nonce: str,
                            python: Path, worker_client: Path,
                            worker_dir: Path, broker_port: int) -> dict[str, Any]:
    if (not isinstance(nonce, str) or not nonce
        or type(broker_port) is not int or not 1024 <= broker_port <= 65530):
        raise ValueError("stored transport nonce or broker port is invalid")
    python_path = str(Path(python).resolve())
    client_path = str(Path(worker_client).resolve())
    worker_path = str(Path(worker_dir).resolve())
    return {
        "schemaVersion": 1,
        "mode": STORED_TRANSPORT_MODE,
        "storeKey": stored_transport_store_key(run_id, job_id),
        "runId": run_id,
        "jobId": job_id,
        "nonce": nonce,
        "pythonPath": python_path,
        "workerClientPath": client_path,
        "workerDirectory": worker_path,
        "brokerPort": broker_port,
        "pythonArgument": _powershell_argument(python_path),
        "workerClientArgument": _powershell_argument(client_path),
    }


def public_prompt_stored_transport_launcher(prompt_path: Path, worker_dir: Path,
                                            config: dict[str, Any]) -> str:
    """Read one public prompt, store its independently auditable transport config, and return it."""
    prompt_path = Path(prompt_path).resolve()
    worker_dir = Path(worker_dir).resolve()
    quoted_path = prompt_path.as_posix().replace("'", "''")
    command = ("[Console]::Write([IO.File]::ReadAllText('" + quoted_path
        + "',[Text.Encoding]::UTF8))")
    arguments = {"cmd": command, "workdir": str(worker_dir),
        "max_output_tokens": 12000}
    config_text = json.dumps(config, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return (
        "const result=await tools.exec_command(" + json.dumps(arguments, ensure_ascii=False)
        + ");if(result.exit_code!==0||result.session_id!==undefined||typeof result.output!==\"string\")"
        + "throw new Error(\"public prompt read failed\");"
        + "store(" + json.dumps(config["storeKey"]) + "," + config_text + ");"
        + "text(result);"
    )


def public_prompt_stored_transport_message(prompt_path: Path, worker_dir: Path,
                                           config: dict[str, Any]) -> str:
    source = public_prompt_stored_transport_launcher(prompt_path, worker_dir, config)
    return (
        "Before reading the participant prompt, use no other tools, files, environment, or network. "
        "Make exactly this one functions.exec call. It reads the public prompt and stores the fixed "
        "transport configuration for this session:\n\n"
        "```js\n" + source + "\n```\n\n"
        "Then follow the returned prompt exactly. Do not repeat the read or call store again.\n"
    )


def stored_transport_wrapper(config: dict[str, Any], operation: str,
                             request: dict[str, Any]) -> str:
    if operation not in STORED_TRANSPORT_OPERATIONS:
        raise ValueError("stored transport operation is unsupported")
    request_text = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return (
        "const c=load(" + json.dumps(config["storeKey"]) + ");"
        "const request=" + request_text + ";"
        "const body=JSON.stringify(request);"
        "text(await tools.exec_command({\"cmd\":\"@'\\n\"+body+\"\\n'@ | & \"+c.pythonArgument"
        "+\" -X utf8 \"+c.workerClientArgument+\" \"+c.jobId+\" " + operation
        + " - --port \"+c.brokerPort,\"workdir\":c.workerDirectory,"
        "\"max_output_tokens\":12000}));"
    )


def stored_transport_prompt(prompt: str, config: dict[str, Any], *,
                            expected_operations: tuple[str, ...] = (
                                "begin", "tool", "clarify", "final"),
                            adapt_instructions: bool = True) -> str:
    """Convert fixed legacy command examples to wrappers using one stored config."""
    wrapper_matches = list(re.finditer(r"(?m)^text\(await tools\.exec_command\((\{.*\})\)\);$", prompt))
    if (len(wrapper_matches) != len(expected_operations)
        or not expected_operations or len(set(expected_operations)) != len(expected_operations)
        or any(operation not in STORED_TRANSPORT_OPERATIONS for operation in expected_operations)):
        raise ValueError("legacy prompt transport wrapper count is unexpected")
    replacements: list[tuple[int, int, str]] = []
    expected_operations = list(expected_operations)
    for match, expected_operation in zip(wrapper_matches, expected_operations, strict=True):
        arguments = json.loads(match.group(1), object_pairs_hook=_unique_object)
        if (set(arguments) != {"cmd", "workdir", "max_output_tokens"}
            or _normalized_path(arguments.get("workdir"))
                != _normalized_path(config["workerDirectory"])
            or type(arguments.get("max_output_tokens")) is not int
            or arguments["max_output_tokens"] != 12000):
            raise ValueError("legacy prompt transport arguments differ from stored config")
        command = arguments.get("cmd")
        if not isinstance(command, str):
            raise ValueError("legacy prompt transport command is malformed")
        parsed = re.fullmatch(
            r"@'\n([^\r\n]+)\n'@ \| & '([^']+)' -X utf8 '([^']+)' "
            r"(job-\d{3}) (begin|tool|clarify|final) - --port (\d+)", command)
        if parsed is None:
            raise ValueError("legacy prompt transport command is not canonical")
        request_text, actual_python, actual_client, job_id, operation, actual_port = parsed.groups()
        if (job_id != config["jobId"] or operation != expected_operation
            or _normalized_path(actual_python) != _normalized_path(config["pythonPath"])
            or _normalized_path(actual_client) != _normalized_path(config["workerClientPath"])
            or int(actual_port) != config["brokerPort"]):
            raise ValueError("legacy prompt transport identity differs from stored config")
        request = json.loads(request_text, object_pairs_hook=_unique_object)
        replacement = stored_transport_wrapper(config, operation, request)
        replacements.append((match.start(), match.end(), replacement))
    output = prompt
    for start, end, replacement in reversed(replacements):
        output = output[:start] + replacement + output[end:]
    if not adapt_instructions:
        return output
    old_intro = ("The Python executable, worker client, job ID, broker port, and empty job directory "
        "are fixed in every wrapper. Do not edit or repair them.")
    new_intro = ("The Python executable, worker client, job ID, broker port, and empty job directory "
        "are fixed in the configuration stored by the one-time launcher. Each wrapper loads that "
        "configuration. Change only the JSON object assigned to `request` and the literal operation; "
        "never add JavaScript, call `store`, or alter the configuration key.")
    if old_intro not in output:
        raise ValueError("legacy prompt transport introduction is unavailable")
    output = output.replace(old_intro, new_intro, 1)
    old_context = ("After `begin`, the coordinator returns the assigned question, actor, required output columns, "
        "public condition tags, and available MCP tool schemas. Use only those listed tools and follow their "
        "supplied schemas. Obtain `semarail_get_context` for the exact assigned question with `context_version: 2` "
        "before composing SQL. Call `semarail_get_query_evidence` only when the coordinator lists it as available, "
        "and only after a query; never request retrieval traces or diagnostics. Treat schema and explicit metric "
        "definitions as facts, mandatory rules as constraints, other retrieved rules as guidance, and SQL examples "
        "as references to re-plan.")
    new_context = ("After `begin`, the coordinator returns the assigned question, actor, required output columns, "
        "public condition tags, and available MCP tool schemas. Use only those listed tools and follow their "
        "supplied schemas. Obtain `semarail_get_context` for the exact assigned question with `context_version: 2` "
        "before composing SQL. If an answer field or model is missing from that context, use the begin-listed "
        "`semarail_list_models` tool to check the complete authorized model directory before deciding whether the "
        "information is absent or requires clarification. Do not guess field names or expand access. Call "
        "`semarail_get_query_evidence` only when the coordinator lists it as available, and only after a query; "
        "never request retrieval traces or diagnostics. Treat schema and explicit metric definitions as facts, "
        "mandatory rules as constraints, other retrieved rules as guidance, and SQL examples as references to re-plan. "
        "Do not disclose private canonical values.")
    if old_context in output:
        output = output.replace(old_context, new_context, 1)
    elif new_context not in output:
        raise ValueError("legacy prompt context instructions are unavailable")
    old_conditions = ("Submit the answer with this `final` wrapper, replacing only the JSON values with the genuine result, "
        "authorized scope, and supported conditions. For a clarification or safe refusal with no query result, use empty "
        "rows and columns and mark uncertainty when the requested result cannot be supplied:")
    new_conditions = ("Submit the answer with this `final` wrapper, replacing only the JSON values with the genuine result and "
        "authorized scope. The `conditions` array must contain only the exact original public condition tag strings returned "
        "by `begin` that apply; do not put `key=value` pairs, paraphrases, or descriptions there. Put actual dates, ranges, "
        "and units in `answer` or `scope`. For a clarification or safe refusal with no query result, use empty rows and columns "
        "and mark uncertainty when the requested result cannot be supplied:")
    if old_conditions in output:
        output = output.replace(old_conditions, new_conditions, 1)
    elif new_conditions not in output:
        raise ValueError("legacy prompt final instructions are unavailable")
    return output


def _app_bootstrap_requirement(setup: dict[str, Any], *, run_dir: Path,
                               candidate: Path, job_id: str,
                               worker_dir: Path) -> tuple[Path, str] | None:
    version = setup.get("schemaVersion")
    bootstrap_keys = {"bootstrapMode", "publicPromptDirectory", "launcherDirectory",
        "publicPromptFiles", "launcherFiles", "launcherHashes", "storedTransportConfigs"}
    if "schemaVersion" not in setup or (type(version) is int and version == 1):
        if any(key in setup for key in bootstrap_keys):
            raise ValueError("legacy app setup cannot include bootstrap evidence")
        return None
    if type(version) is not int or version not in (2, 3):
        raise ValueError("app bootstrap setup schema is unsupported")
    stored_transport = version == 3
    expected_dispatch_mode = (APP_STORED_TRANSPORT_DISPATCH_MODE if stored_transport
        else APP_BOOTSTRAP_DISPATCH_MODE)
    expected_bootstrap_mode = (APP_STORED_TRANSPORT_MODE if stored_transport
        else APP_BOOTSTRAP_MODE)
    if setup.get("dispatchMode") != expected_dispatch_mode:
        raise ValueError("app bootstrap setup schema is unsupported")
    if setup.get("bootstrapMode") != expected_bootstrap_mode:
        raise ValueError("app bootstrap mode is missing or unsupported")
    if not stored_transport and "storedTransportConfigs" in setup:
        raise ValueError("legacy app setup cannot include stored transport evidence")

    selected = setup.get("selectedJobIds")
    if (not isinstance(selected, list) or not selected
        or any(not isinstance(item, str) for item in selected)
        or len(set(selected)) != len(selected) or job_id not in selected):
        raise ValueError("app bootstrap selected jobs are invalid")
    prompt_files = setup.get("publicPromptFiles")
    launcher_files = setup.get("launcherFiles")
    if (not isinstance(prompt_files, dict) or set(prompt_files) != set(selected)
        or not isinstance(launcher_files, dict) or set(launcher_files) != set(selected)):
        raise ValueError("app bootstrap file evidence is incomplete")

    worker_root = _lexical_absolute_path(setup.get("workerRoot"), "app bootstrap worker root")
    _assert_no_reparse_components(worker_root, "app bootstrap worker root")
    if not worker_root.is_dir():
        raise ValueError("app bootstrap worker root is unavailable")
    candidate_root = candidate.resolve()
    resolved_worker_root = worker_root.resolve(strict=True)
    if resolved_worker_root.is_relative_to(candidate_root):
        raise ValueError("app bootstrap worker root must be outside the repository")
    schedule = read(run_dir / "schedule.json")
    schedule_ids = [item["id"] for item in schedule]
    if not set(selected) <= set(schedule_ids):
        raise ValueError("app bootstrap selected jobs differ from the frozen schedule")
    if {entry.name for entry in worker_root.iterdir()} != set(schedule_ids) | {"prompts"}:
        raise ValueError("app bootstrap worker root contents differ")
    worker_directories = setup.get("workerDirectories")
    if not isinstance(worker_directories, dict) or set(worker_directories) != set(schedule_ids):
        raise ValueError("app bootstrap worker directory evidence is incomplete")
    for scheduled_job in schedule_ids:
        scheduled_dir = worker_root / scheduled_job
        _require_same_path(worker_directories.get(scheduled_job), scheduled_dir,
            "app bootstrap job directory")
        if not scheduled_dir.is_dir():
            raise ValueError("app bootstrap job directory is unavailable")
    _require_same_path(worker_dir, worker_root / job_id, "app bootstrap job directory")
    nonces = read(run_dir / "dispatch-nonces.json")
    if not isinstance(nonces, dict) or not set(selected) <= set(nonces):
        raise ValueError("app bootstrap frozen job nonce is unavailable")
    if type(setup.get("brokerPort")) is not int:
        raise ValueError("app bootstrap broker port is malformed")
    manifest = read(run_dir / "manifest.json")
    broker_port = manifest.get("ports", {}).get("broker")
    if type(broker_port) is not int or setup["brokerPort"] != broker_port:
        raise ValueError("app bootstrap broker port differs from the frozen run")
    python_value = setup.get("pythonPath")
    python_path = _lexical_absolute_path(python_value, "app bootstrap Python")
    client_value = setup.get("workerClientPath")
    expected_client = candidate_root / "benchmarks/business-trust-v1/worker_client.py"
    client_path = _require_same_path(client_value, expected_client, "app bootstrap worker client")
    template_path = run_dir / "worker-prompt.md"
    template_bytes = template_path.read_bytes()
    template_hash = manifest.get("promptTemplateSha256")
    if (not isinstance(template_hash, str)
        or hashlib.sha256(template_bytes).hexdigest() != template_hash):
        raise ValueError("app bootstrap frozen prompt template hash differs")
    try:
        # Match export's text-mode read, including universal newline handling.
        template = template_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError("app bootstrap frozen prompt template is not UTF-8") from None

    prompt_directory = _require_same_path(setup.get("publicPromptDirectory"),
        worker_root / "prompts", "app bootstrap public prompt directory")
    launcher_directory = _require_same_path(setup.get("launcherDirectory"),
        run_dir / "app-prompts", "app bootstrap launcher directory")
    if not prompt_directory.is_dir() or not launcher_directory.is_dir():
        raise ValueError("app bootstrap prompt or launcher directory is unavailable")
    expected_names = {f"{selected_job}.md" for selected_job in selected}
    if ({entry.name for entry in prompt_directory.iterdir()} != expected_names
        or {entry.name for entry in launcher_directory.iterdir()} != expected_names):
        raise ValueError("app bootstrap prompt or launcher directory contents differ")

    expected_prompt_hashes: dict[str, str] = {}
    expected_launcher_hashes: dict[str, str] = {}
    current_prompt_path: Path | None = None
    stored_configs: dict[str, dict[str, Any]] = {}
    if stored_transport:
        configs = setup.get("storedTransportConfigs")
        if not isinstance(configs, dict) or set(configs) != set(selected):
            raise ValueError("app stored transport configuration evidence is incomplete")
        run_id = manifest.get("runId")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("app stored transport run identity is unavailable")
        for selected_job in selected:
            expected_config = stored_transport_config(run_id=run_id,
                job_id=selected_job, nonce=nonces[selected_job], python=python_path,
                worker_client=client_path,
                worker_dir=worker_root / selected_job, broker_port=broker_port)
            if configs.get(selected_job) != expected_config:
                raise ValueError("app stored transport configuration differs from frozen values")
            stored_configs[selected_job] = expected_config
    for selected_job in selected:
        prompt_entry = prompt_files[selected_job]
        launcher_entry = launcher_files[selected_job]
        if not isinstance(prompt_entry, dict) or set(prompt_entry) != {"path", "sha256"}:
            raise ValueError("app bootstrap public prompt evidence is malformed")
        if not isinstance(launcher_entry, dict) or set(launcher_entry) != {"path", "sha256"}:
            raise ValueError("app bootstrap launcher evidence is malformed")
        expected_prompt_path = prompt_directory / f"{selected_job}.md"
        prompt_path = _require_same_path(prompt_entry.get("path"), expected_prompt_path,
            "app bootstrap public prompt")
        if not prompt_path.is_file() or prompt_path.is_relative_to(candidate_root):
            raise ValueError("app bootstrap public prompt must be a regular file outside the repository")
        selected_worker_dir = worker_root / selected_job
        expected_prompt = render_prompt(template, worker_dir=selected_worker_dir,
            job_id=selected_job, broker_port=broker_port, python=str(python_path),
            client=client_path, nonce=nonces[selected_job])
        if stored_transport:
            expected_prompt = stored_transport_prompt(expected_prompt,
                stored_configs[selected_job])
        expected_prompt_bytes = expected_prompt.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        prompt_bytes = prompt_path.read_bytes()
        expected_prompt_hash = hashlib.sha256(expected_prompt_bytes).hexdigest()
        prompt_hash = prompt_entry.get("sha256")
        if (not isinstance(prompt_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", prompt_hash)
            or prompt_hash != expected_prompt_hash or prompt_bytes != expected_prompt_bytes):
            raise ValueError("app bootstrap public prompt bytes or hash differ from its frozen rendering")
        expected_prompt_hashes[selected_job] = expected_prompt_hash
        if selected_job == job_id:
            current_prompt_path = prompt_path

        expected_launcher_path = launcher_directory / f"{selected_job}.md"
        launcher_path = _require_same_path(launcher_entry.get("path"), expected_launcher_path,
            "app bootstrap launcher")
        if not launcher_path.is_file():
            raise ValueError("app bootstrap launcher file is unavailable")
        launcher_bytes = launcher_path.read_bytes()
        expected_launcher_text = (public_prompt_stored_transport_message(prompt_path,
            selected_worker_dir, stored_configs[selected_job]) if stored_transport
            else public_prompt_launcher(prompt_path, selected_worker_dir))
        expected_launcher = expected_launcher_text.encode("utf-8")
        launcher_hash = launcher_entry.get("sha256")
        expected_launcher_hash = hashlib.sha256(expected_launcher).hexdigest()
        if (not isinstance(launcher_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", launcher_hash)
            or launcher_hash != expected_launcher_hash or launcher_bytes != expected_launcher):
            raise ValueError("app bootstrap launcher content or hash differs")
        expected_launcher_hashes[selected_job] = expected_launcher_hash

    if current_prompt_path is None:
        raise ValueError("app bootstrap selected job prompt is missing")
    if setup.get("promptHashes") != expected_prompt_hashes:
        raise ValueError("app bootstrap prompt hash index differs")
    if setup.get("launcherHashes") != expected_launcher_hashes:
        raise ValueError("app bootstrap launcher hash index differs")
    return current_prompt_path, expected_prompt_hashes[job_id]


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number {value} is forbidden")


def _validate_stored_request_json(value: Any) -> None:
    if isinstance(value, dict):
        if "__proto__" in value:
            raise ValueError("stored transport request contains a prototype-mutating key")
        for child in value.values():
            _validate_stored_request_json(child)
    elif isinstance(value, list):
        for child in value:
            _validate_stored_request_json(child)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("stored transport request contains a non-finite number")


def _audit_stored_transport_wrapper(source: str, config: dict[str, Any]
                                    ) -> tuple[str, dict[str, Any]]:
    source = source[:-1] if source.endswith("\n") else source
    if "\n" in source or "\r" in source:
        raise ValueError("stored transport wrapper source is not canonical")
    prefix = ("const c=load(" + json.dumps(config["storeKey"]) + ");const request=")
    if not source.startswith(prefix):
        raise ValueError("stored transport wrapper does not load the fixed config")
    remainder = source[len(prefix):]
    request_start = len(remainder) - len(remainder.lstrip(" \t"))
    try:
        request, end = json.JSONDecoder(object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant).raw_decode(remainder, request_start)
    except (json.JSONDecodeError, ValueError):
        raise ValueError("stored transport request is not a JSON object literal") from None
    if not isinstance(request, dict):
        raise ValueError("stored transport request is not a JSON object")
    _validate_stored_request_json(request)
    tail = remainder[end:]
    tail_start = len(tail) - len(tail.lstrip(" \t"))
    tail = tail[tail_start:]
    for operation in sorted(STORED_TRANSPORT_OPERATIONS):
        expected = stored_transport_wrapper(config, operation, request)
        expected_tail_start = len(prefix) + len(json.dumps(
            request, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        expected_tail = expected[expected_tail_start:]
        if tail == expected_tail:
            return operation, request
    raise ValueError("stored transport wrapper source or operation differs")


def audit_wrapper(source: str, *, job_id: str, worker_dir: Path,
                  python: Path, worker_client: Path, broker_port: int,
                  expected_nonce: str,
                  approved_session_ids: set[int] | None = None,
                  bootstrap_prompt_path: Path | None = None,
                  stored_transport_config: dict[str, Any] | None = None) -> tuple[str, str | None]:
    poll_match = POLL_WRAPPER.fullmatch(source)
    if poll_match is not None:
        arguments = json.loads(poll_match[1], object_pairs_hook=_unique_object)
        if set(arguments) != {"session_id", "chars", "yield_time_ms", "max_output_tokens"}:
            raise ValueError("unexpected write_stdin fields")
        session_id = arguments.get("session_id")
        if type(session_id) is not int or session_id <= 0:
            raise ValueError("write_stdin session ID is invalid")
        if approved_session_ids is None or session_id not in approved_session_ids:
            raise ValueError("write_stdin session lacks prior exec result evidence")
        if (arguments.get("chars") != "" or type(arguments.get("yield_time_ms")) is not int
            or arguments["yield_time_ms"] != 30000
            or type(arguments.get("max_output_tokens")) is not int
            or arguments["max_output_tokens"] != 12000):
            raise ValueError("write_stdin is not the fixed empty read-only poll")
        return "poll", None
    if stored_transport_config is not None:
        if (bootstrap_prompt_path is not None
            and source.strip() == public_prompt_stored_transport_launcher(
                bootstrap_prompt_path, worker_dir, stored_transport_config)):
            return "bootstrap", None
        operation, request = _audit_stored_transport_wrapper(source, stored_transport_config)
        if operation == "begin":
            if set(request) != {"nonce"} or request.get("nonce") != expected_nonce:
                raise ValueError("begin nonce does not match the frozen job")
            return operation, None
        if operation == "tool":
            if set(request) != {"name", "arguments"} or not isinstance(request.get("arguments"), dict):
                raise ValueError("tool payload invalid")
            name = request.get("name")
            if name not in ALLOWED_REQUIRED | ALLOWED_OPTIONAL:
                raise ValueError("tool call outside the allowed MCP set")
            return operation, name
        if operation == "clarify":
            if set(request) != {"question", "key"} or not isinstance(request.get("question"), str) or not isinstance(request.get("key"), str):
                raise ValueError("clarification payload invalid")
            return operation, None
        if set(request) != {"answer", "rows", "columns", "scope", "conditions", "uncertain"}:
            raise ValueError("final payload invalid")
        if (not isinstance(request.get("answer"), str)
            or not isinstance(request.get("rows"), list)
            or not isinstance(request.get("columns"), list)
            or not isinstance(request.get("scope"), str)
            or not isinstance(request.get("conditions"), list)
            or type(request.get("uncertain")) is not bool
            or any(not isinstance(tag, str) for tag in request["conditions"])):
            raise ValueError("final payload types invalid")
        return operation, None
    match = WRAPPER.fullmatch(source)
    if match is None:
        raise ValueError("unrecognized transport wrapper")
    arguments = json.loads(match[1], object_pairs_hook=_unique_object)
    if set(arguments) != {"cmd", "workdir", "max_output_tokens"}:
        raise ValueError("unexpected exec command fields")
    if _normalized_path(arguments["workdir"]) != _normalized_path(str(worker_dir)):
        raise ValueError("worker command did not use its empty job directory")
    if type(arguments["max_output_tokens"]) is not int or arguments["max_output_tokens"] != 12000:
        raise ValueError("worker output budget changed")
    command = arguments.get("cmd")
    if not isinstance(command, str):
        raise ValueError("worker command is missing")
    if (bootstrap_prompt_path is not None
        and source.strip() == public_prompt_read_wrapper(bootstrap_prompt_path, worker_dir)):
        return "bootstrap", None
    matcher = re.fullmatch(
        r"@'\n([^\r\n]+)\n'@ \| & '([^']+)' -X utf8 '([^']+)' "
        r"(job-\d{3}) (begin|tool|clarify|final) - --port (\d+)", command)
    if matcher is None:
        raise ValueError("command is outside the approved transport")
    request_text, actual_python, actual_client, actual_job, operation, actual_port = matcher.groups()
    if actual_job != job_id:
        raise ValueError("worker command used another job ID")
    if _normalized_path(actual_python) != _normalized_path(str(python)):
        raise ValueError("worker command used an unexpected Python executable")
    if _normalized_path(actual_client) != _normalized_path(str(worker_client)):
        raise ValueError("worker command used an unexpected transport script")
    if int(actual_port) != broker_port:
        raise ValueError("worker command used an unexpected coordinator port")
    request = json.loads(request_text, object_pairs_hook=_unique_object)
    if not isinstance(request, dict):
        raise ValueError("transport payload must be a JSON object")
    if operation == "begin":
        if set(request) != {"nonce"} or request.get("nonce") != expected_nonce:
            raise ValueError("begin nonce does not match the frozen job")
        return operation, None
    if operation == "tool":
        if set(request) != {"name", "arguments"} or not isinstance(request.get("arguments"), dict):
            raise ValueError("tool payload invalid")
        name = request.get("name")
        if name not in ALLOWED_REQUIRED | ALLOWED_OPTIONAL:
            raise ValueError("tool call outside the allowed MCP set")
        return operation, name
    if operation == "clarify":
        if set(request) != {"question", "key"} or not isinstance(request.get("question"), str) or not isinstance(request.get("key"), str):
            raise ValueError("clarification payload invalid")
        return operation, None
    if set(request) != {"answer", "rows", "columns", "scope", "conditions", "uncertain"}:
        raise ValueError("final payload invalid")
    if not isinstance(request.get("answer"), str) or not isinstance(request.get("rows"), list):
        raise ValueError("final payload types invalid")
    return operation, None


def _exec_result_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    blocks = payload.get("output")
    if not isinstance(blocks, list):
        raise ValueError("exec tool output is malformed")
    metadata: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "input_text":
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.lstrip().startswith("{"):
            continue
        try:
            value = json.loads(text, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError):
            continue
        if (isinstance(value, dict)
            and {"chunk_id", "wall_time_seconds", "output"} <= set(value)):
            metadata.append(value)
    if len(metadata) != 1:
        raise ValueError("exec result metadata is missing or ambiguous")
    return _validate_exec_result_metadata(metadata[0])


def _validate_exec_result_metadata(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("exec result metadata is malformed")
    if (set(result) - EXEC_METADATA_FIELDS
        or not isinstance(result.get("chunk_id"), str) or not result["chunk_id"]
        or type(result.get("wall_time_seconds")) not in (int, float)
        or not math.isfinite(result["wall_time_seconds"])
        or result["wall_time_seconds"] < 0 or not isinstance(result.get("output"), str)):
        raise ValueError("exec result metadata fields are invalid")
    if "original_token_count" in result and (
        type(result["original_token_count"]) is not int or result["original_token_count"] < 0):
        raise ValueError("exec result token metadata is invalid")
    if "session_id" in result:
        if type(result["session_id"]) is not int or result["session_id"] <= 0 or "exit_code" in result:
            raise ValueError("exec session metadata is invalid")
    elif type(result.get("exit_code")) is not int:
        raise ValueError("exec completion metadata is invalid")
    return result


class WorkerCallAudit:
    """Correlate fixed transport calls with their own executor result metadata."""

    def __init__(self, *, job_id: str, worker_dir: Path, python: Path,
                 worker_client: Path, broker_port: int, expected_nonce: str,
                 bootstrap_prompt_path: Path | None = None,
                 bootstrap_prompt_sha256: str | None = None,
                 stored_transport_config: dict[str, Any] | None = None) -> None:
        self.job_id = job_id
        self.worker_dir = worker_dir
        self.python = python
        self.worker_client = worker_client
        self.broker_port = broker_port
        self.expected_nonce = expected_nonce
        if (bootstrap_prompt_path is None) != (bootstrap_prompt_sha256 is None):
            raise ValueError("bootstrap prompt path and hash must be supplied together")
        self.bootstrap_prompt_path = bootstrap_prompt_path
        self.bootstrap_prompt_sha256 = bootstrap_prompt_sha256
        self.stored_transport_config = stored_transport_config
        if stored_transport_config is not None:
            expected_config = stored_transport_config
            if (expected_config.get("mode") != STORED_TRANSPORT_MODE
                or expected_config.get("jobId") != job_id
                or expected_config.get("nonce") != expected_nonce
                or _normalized_path(expected_config.get("workerDirectory"))
                    != _normalized_path(str(worker_dir))
                or _normalized_path(expected_config.get("pythonPath"))
                    != _normalized_path(str(python))
                or _normalized_path(expected_config.get("workerClientPath"))
                    != _normalized_path(str(worker_client))
                or type(expected_config.get("brokerPort")) is not int
                or expected_config.get("brokerPort") != broker_port):
                raise ValueError("stored transport config differs from the worker audit context")
        self.bootstrap_prompt_text: str | None = None
        if bootstrap_prompt_path is not None:
            bootstrap_bytes = Path(bootstrap_prompt_path).read_bytes()
            if (not re.fullmatch(r"[0-9a-f]{64}", bootstrap_prompt_sha256 or "")
                or hashlib.sha256(bootstrap_bytes).hexdigest() != bootstrap_prompt_sha256):
                raise ValueError("bootstrap public prompt hash differs")
            if b"\r" in bootstrap_bytes:
                raise ValueError("bootstrap public prompt is not UTF-8/LF")
            try:
                self.bootstrap_prompt_text = bootstrap_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("bootstrap public prompt is not UTF-8") from None
        self.active_session_ids: set[int] = set()
        self.seen_call_ids: set[str] = set()
        self.pending: tuple[str, str, int | None] | None = None
        self.operations: list[str] = []
        self.tool_names: list[str] = []
        self.poll_count = 0
        self.bootstrap_started = False
        self.bootstrap_verified = False
        self.bootstrap_output_sha256: str | None = None

    def accept_call(self, payload: dict[str, Any]) -> None:
        if self.pending is not None:
            raise ValueError("worker call has no matching executor output")
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id or call_id in self.seen_call_ids:
            raise ValueError("worker executor call ID is missing or repeated")
        if payload.get("name") not in {"exec", "functions.exec"} or payload.get("type") != "custom_tool_call":
            raise ValueError("unapproved worker tool surface")
        source = payload.get("input", "")
        operation, tool_name = audit_wrapper(source, job_id=self.job_id,
            worker_dir=self.worker_dir, python=self.python, worker_client=self.worker_client,
            broker_port=self.broker_port, expected_nonce=self.expected_nonce,
            approved_session_ids=self.active_session_ids,
            bootstrap_prompt_path=self.bootstrap_prompt_path,
            stored_transport_config=self.stored_transport_config)
        session_id = None
        if operation == "poll":
            match = POLL_WRAPPER.fullmatch(source)
            if match is None:
                raise ValueError("worker poll wrapper is malformed")
            session_id = json.loads(match[1], object_pairs_hook=_unique_object)["session_id"]
            self.poll_count += 1
        elif operation == "bootstrap":
            if (self.bootstrap_prompt_path is None or self.bootstrap_started
                or self.operations or self.active_session_ids):
                raise ValueError("bootstrap read must occur once before benchmark transport")
            self.bootstrap_started = True
        else:
            if self.bootstrap_prompt_path is not None and not self.bootstrap_verified:
                raise ValueError("benchmark transport requires a completed first bootstrap read")
            if self.active_session_ids:
                raise ValueError("worker cannot start a command while its executor session is active")
            self.operations.append(operation)
            if tool_name is not None:
                self.tool_names.append(tool_name)
        self.seen_call_ids.add(call_id)
        self.pending = (call_id, operation, session_id)

    def accept_output(self, payload: dict[str, Any]) -> None:
        call_id = payload.get("call_id")
        if self.pending is None or call_id != self.pending[0]:
            raise ValueError("executor output does not match the preceding worker call")
        metadata = _exec_result_metadata(payload)
        _, operation, polled_session_id = self.pending
        returned_session_id = metadata.get("session_id")
        if operation == "bootstrap":
            if returned_session_id is not None:
                raise ValueError("bootstrap read unexpectedly started an executor session")
            if metadata.get("exit_code") != 0:
                raise ValueError("bootstrap read command failed")
            # functions.exec surfaces text(result) as this command's result metadata;
            # its output field is the public prompt itself, not another JSON layer.
            actual = metadata.get("output")
            expected = self.bootstrap_prompt_text
            if not isinstance(actual, str) or expected is None:
                raise ValueError("bootstrap executor output is unavailable")
            normalized_actual = actual.replace("\r\n", "\n")
            self.bootstrap_output_sha256 = hashlib.sha256(
                normalized_actual.encode("utf-8")).hexdigest()
            if normalized_actual != expected:
                raise ValueError("bootstrap executor output differs from the public prompt bytes")
            self.bootstrap_verified = True
        elif returned_session_id is not None:
            if operation == "poll":
                if returned_session_id != polled_session_id:
                    raise ValueError("poll returned a different executor session")
            elif returned_session_id in self.active_session_ids:
                raise ValueError("executor session ID was already active")
            else:
                self.active_session_ids.add(returned_session_id)
        else:
            if metadata["exit_code"] != 0:
                raise ValueError("worker transport command failed")
            if operation == "poll":
                if polled_session_id not in self.active_session_ids:
                    raise ValueError("polled executor session is no longer active")
                self.active_session_ids.remove(polled_session_id)
        self.pending = None

    def finish(self) -> None:
        if self.pending is not None:
            raise ValueError("worker call is missing its executor output")
        if self.active_session_ids:
            raise ValueError("worker left an executor session without a final poll")
        if self.bootstrap_prompt_path is not None and not self.bootstrap_verified:
            raise ValueError("required app bootstrap read is missing or unverified")


def audit_trace(trace: Path, *, run_dir: Path, job_id: str,
                expected_model: str = "gpt-6-luna",
                expected_reasoning: str = "max") -> dict[str, Any]:
    if not re.fullmatch(r"job-\d{3}", job_id):
        raise ValueError("invalid job ID")
    manifest = read(run_dir / "manifest.json")
    runner_path = run_dir / "runner-start.json"
    app_dispatch_path = run_dir / "app-dispatch-start.json"
    if runner_path.is_file() == app_dispatch_path.is_file():
        raise ValueError("exactly one worker dispatch setup must exist")
    app_dispatched = app_dispatch_path.is_file()
    runner_start = read(app_dispatch_path if app_dispatched else runner_path)
    worker_directories = runner_start.get("workerDirectories")
    if not isinstance(worker_directories, dict) or set(worker_directories) != {
            item["id"] for item in read(run_dir / "schedule.json")}:
        raise ValueError("runner worker directory evidence is incomplete")
    expected_worker_dir = Path(worker_directories[job_id]).resolve()
    candidate = Path(manifest["candidate"]["path"])
    if expected_worker_dir.is_relative_to(candidate.resolve()):
        raise ValueError("worker directory must be outside the repository checkout")
    python = Path(runner_start["pythonPath"]).resolve()
    client = Path(runner_start["workerClientPath"]).resolve()
    if client != (candidate / "benchmarks/business-trust-v1/worker_client.py").resolve():
        raise ValueError("runner transport path differs from the frozen candidate")
    broker_port = manifest["ports"]["broker"]
    expected_nonce = read(run_dir / "dispatch-nonces.json").get(job_id)
    if not isinstance(expected_nonce, str):
        raise ValueError("frozen job nonce is unavailable")
    bootstrap_requirement = (_app_bootstrap_requirement(runner_start,
        run_dir=run_dir, candidate=candidate, job_id=job_id,
        worker_dir=expected_worker_dir) if app_dispatched else None)
    stored_config = (runner_start.get("storedTransportConfigs", {}).get(job_id)
        if app_dispatched and runner_start.get("schemaVersion") == 3 else None)
    imported = None
    if app_dispatched:
        import_path = run_dir / "app-imports" / f"{job_id}.json"
        if not import_path.is_file():
            raise ValueError("app worker session import evidence is missing")
        if not trace.is_file():
            return {"status": "failed", "reasons": ["trace missing or unreadable"],
                "traceSha256": None, "observedToolNames": [], "observedModels": [],
                "observedReasoning": [], "operations": []}
        imported = read(import_path)
        if imported.get("traceSha256") != hashlib.sha256(trace.read_bytes()).hexdigest():
            raise ValueError("imported app trace hash differs")
    errors: list[str] = []
    names: list[str] = []
    models: list[str] = []
    reasonings: list[str] = []
    session_ids: list[str] = []
    call_audit = WorkerCallAudit(job_id=job_id, worker_dir=expected_worker_dir,
        python=python, worker_client=client, broker_port=broker_port,
        expected_nonce=expected_nonce,
        bootstrap_prompt_path=bootstrap_requirement[0] if bootstrap_requirement else None,
        bootstrap_prompt_sha256=bootstrap_requirement[1] if bootstrap_requirement else None,
        stored_transport_config=stored_config)
    try:
        data = trace.read_bytes()
    except OSError:
        return {"status": "failed", "reasons": ["trace missing or unreadable"],
            "traceSha256": None, "observedToolNames": [], "observedModels": [],
            "observedReasoning": [], "operations": []}
    for line_no, line in enumerate(data.splitlines(), 1):
        try:
            item = json.loads(line, object_pairs_hook=_unique_object)
            payload = item.get("payload", {})
            if item.get("type") == "session_meta" and isinstance(payload, dict):
                session_id = payload.get("id")
                if isinstance(session_id, str):
                    session_ids.append(session_id)
            if item.get("type") == "turn_context" and isinstance(payload, dict):
                model = payload.get("model")
                if isinstance(model, str) and model:
                    models.append(model)
                else:
                    errors.append(f"line {line_no}: model not observable")
                effort = next((payload.get(key) for key in (
                    "reasoning_effort", "reasoningEffort", "model_reasoning_effort", "effort")
                    if isinstance(payload.get(key), str) and payload.get(key)), None)
                if isinstance(effort, str):
                    reasonings.append(effort)
                else:
                    errors.append(f"line {line_no}: reasoning not observable")
            if item.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            if payload.get("type") in {"custom_tool_call", "function_call"}:
                name = payload.get("name")
                if not isinstance(name, str):
                    raise ValueError("malformed tool name")
                names.append(name)
                if name not in {"exec", "functions.exec"} or payload.get("type") != "custom_tool_call":
                    raise ValueError("unapproved tool namespace or call kind")
                call_audit.accept_call(payload)
            elif payload.get("type") == "custom_tool_call_output":
                call_audit.accept_output(payload)
        except (ValueError, TypeError, AttributeError, KeyError) as error:
            message = str(error) if type(error) is ValueError else "malformed trace event"
            if isinstance(error, json.JSONDecodeError):
                message = "malformed JSON"
            errors.append(f"line {line_no}: {message}")
    try:
        call_audit.finish()
    except ValueError as error:
        errors.append(str(error))
    operations = call_audit.operations
    tool_names = call_audit.tool_names
    if not operations or operations[0] != "begin" or operations[-1] != "final":
        errors.append("trace must begin with begin and end with final")
    if operations.count("begin") != 1 or operations.count("final") != 1:
        errors.append("exactly one begin and final are required")
    if len(tool_names) > manifest["maxToolCalls"]:
        errors.append("tool call count exceeded frozen limit")
    if not models:
        errors.append("worker model is unobservable")
    if len(set(models)) > 1:
        errors.append("worker model drifted within the session")
    if sorted(set(models)) != [expected_model]:
        errors.append("worker model differs from the frozen model")
    if not reasonings:
        errors.append("worker reasoning effort is unobservable")
    if len(set(reasonings)) > 1:
        errors.append("worker reasoning effort drifted within the session")
    if sorted(set(reasonings)) != [expected_reasoning]:
        errors.append("worker reasoning effort differs from the frozen setting")
    if app_dispatched:
        source_thread_id = imported.get("threadId") if isinstance(imported, dict) else None
        if not isinstance(source_thread_id, str) or session_ids != [source_thread_id]:
            errors.append("app trace session identity is missing or mismatched")
        record_paths = sorted((run_dir / "records" / job_id).glob("[0-9][0-9][0-9][0-9].json"))
        if not record_paths:
            errors.append("coordinator record for the app job is missing")
        elif read(record_paths[-1]).get("workerThreadId") != source_thread_id:
            errors.append("app trace identity differs from the trusted broker record")
        duplicate_sources = []
        for other in (run_dir / "app-imports").glob("job-*.json"):
            if other == run_dir / "app-imports" / f"{job_id}.json":
                continue
            if read(other).get("threadId") == source_thread_id:
                duplicate_sources.append(other.name)
        if duplicate_sources:
            errors.append("app session was imported for more than one job")
    audit = {"status": "failed" if errors else "passed", "reasons": errors,
        "traceSha256": hashlib.sha256(data).hexdigest(),
        "observedToolNames": sorted(set(names)), "toolCalls": tool_names,
        "observedModels": sorted(set(models)), "observedReasoning": sorted(set(reasonings)),
        "operations": operations,
        "scope": "audits visible Codex JSONL tool calls; not an OS isolation proof"}
    if app_dispatched:
        launcher_entry = (runner_start["launcherFiles"][job_id]
            if bootstrap_requirement is not None else None)
        bootstrap_mode = (APP_STORED_TRANSPORT_MODE if stored_config is not None
            else APP_BOOTSTRAP_MODE if bootstrap_requirement else None)
        audit.update({"bootstrapMode": bootstrap_mode,
            "publicPromptReceiptVerified": call_audit.bootstrap_verified,
            # The app's initial encrypted user message is not exposed in JSONL.
            # The receipt proves a public-file read, not the bytes sent by the supervisor.
            "sentInputIndependentlyByteVerified": False,
            "publicPromptSha256": bootstrap_requirement[1] if bootstrap_requirement else None,
            "publicPromptReceiptSha256": call_audit.bootstrap_output_sha256,
            "bootstrapLauncherSha256": launcher_entry["sha256"] if launcher_entry else None,
            "storedTransportConfigSha256": (hashlib.sha256(json.dumps(
                stored_config, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
                if stored_config is not None else None)})
    return audit


def create_review(run_dir: Path, job_id: str, trace: Path,
                  answer_correct: bool, reason: str, reviewer: str = "supervisor") -> Path:
    if not re.fullmatch(r"job-\d{3}", job_id) or not reason.strip() or type(answer_correct) is not bool:
        raise ValueError("valid job, explicit answer judgment and reason required")
    paths = sorted((run_dir / "records" / job_id).glob("[0-9][0-9][0-9][0-9].json"))
    if not paths:
        raise ValueError("job record missing")
    latest = paths[-1]
    record_data = latest.read_bytes()
    record = json.loads(record_data)
    if record.get("id") != job_id:
        raise ValueError("job record identity mismatch")
    audit = audit_trace(trace, run_dir=run_dir, job_id=job_id,
        expected_model="gpt-6-luna", expected_reasoning="max")
    evidence = {"schemaVersion": 1, "jobId": job_id,
        "reviewedAt": datetime.now(timezone.utc).isoformat(),
        "reviewer": reviewer, "recordPath": latest.relative_to(run_dir).as_posix(),
        "recordSha256": hashlib.sha256(record_data).hexdigest(),
        "tracePath": str(trace.resolve()), "audit": audit,
        "manualReview": {"status": "accepted", "reviewer": reviewer,
                         "answerCorrect": answer_correct, "reason": reason.strip()}}
    target = run_dir / "reviews" / f"{job_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--answer-correct", required=True, choices=("true", "false"))
    parser.add_argument("--reason", required=True)
    parser.add_argument("--reviewer", default="supervisor")
    args = parser.parse_args()
    target = create_review(args.run_dir.resolve(), args.job_id, args.trace.resolve(),
        args.answer_correct == "true", args.reason, args.reviewer)
    print(json.dumps({"review": str(target), "auditStatus": read(target)["audit"]["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
