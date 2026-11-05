#!/usr/bin/env python3
"""Audit only the two explicitly selected lifecycle workers and their broker calls.

Raw answers stay in a protected local report. Empty polling is accepted only for
an exec session returned by a previously accepted transport call in this trace.
This audit is lifecycle evidence, never a substitute for the scheduled campaign.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import uuid

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ["tools/list", "semarail_get_context", "semarail_plan_query",
         "semarail_prepare_query", "semarail_governed_query", "semarail_get_query_evidence"]
WRAPPER = re.compile(r"\s*text\(await tools\.(exec_command|write_stdin)\((\{.*\})\)\);\s*", re.S)


def require(value: object, code: str) -> None:
    if not value:
        raise ValueError(code)


def strict_json(raw: str) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: require(False, "NONFINITE_JSON"))


def audit(trace: Path, thread: str, metadata: dict, records: list, port: int) -> dict:
    require(str(uuid.UUID(thread)) == thread, "INVALID_THREAD_ID")
    raw = trace.read_bytes()
    items = [strict_json(line) for line in raw.decode("utf8").splitlines() if line]
    sessions = [x["payload"].get("id") for x in items if x.get("type") == "session_meta"]
    require(sessions == [thread], "SESSION_ID_MISMATCH")
    contexts = [x["payload"] for x in items if x.get("type") == "turn_context"]
    require(contexts and all(x.get("model") == "gpt-6-luna" and x.get("effort") == "max"
                            for x in contexts), "OBSERVED_MODEL_MISMATCH")
    broker = []
    for record in records:
        if record.get("workerThreadId") != thread:
            continue
        request = record.get("request", {})
        if request.get("method") == "tools/list":
            broker.append(("tools/list", None))
        elif request.get("method") == "tools/call":
            params = request.get("params", {})
            broker.append((params.get("name"), params.get("arguments")))
    require([x[0] for x in broker] == TOOLS, "BROKER_CALL_SEQUENCE_MISMATCH")
    fixed = f"& '{ROOT.as_posix()}/.venv/Scripts/python.exe' -X utf8 '{ROOT.as_posix()}/scripts/lifecycle-mcp-worker-client.py' --endpoint 'http://127.0.0.1:{port}/mcp' --nonce '"
    accepted = {}
    answered = set()
    active = set()
    commands = []
    polls = []
    final = []
    for item in items:
        if item.get("type") != "response_item":
            continue
        payload = item.get("payload", {})
        kind = payload.get("type")
        require(kind != "function_call", "UNAPPROVED_FUNCTION_CALL")
        if kind == "custom_tool_call":
            require(set(accepted) == answered, "CALL_BEFORE_PRIOR_OUTPUT")
            require(payload.get("name") == "exec", "UNAPPROVED_TOOL_NAMESPACE")
            match = WRAPPER.fullmatch(payload.get("input", ""))
            require(match is not None, "UNAPPROVED_TOOL_WRAPPER")
            operation, arguments = match.groups()
            args = strict_json(arguments)
            cid = payload.get("call_id")
            require(isinstance(cid, str) and cid not in accepted, "DUPLICATE_CALL_ID")
            if operation == "write_stdin":
                require(set(args) == {"session_id", "chars", "yield_time_ms", "max_output_tokens"}
                        and type(args["session_id"]) is int and args["session_id"] in active
                        and args["chars"] == "" and args["yield_time_ms"] == 30000
                        and args["max_output_tokens"] == 12000, "UNAPPROVED_EXEC_POLL")
                accepted[cid] = (operation, args["session_id"])
                polls.append({"callId": cid, "sessionId": args["session_id"]})
                continue
            require(not active, "COMMAND_WITH_UNFINISHED_EXEC_SESSION")
            require(set(args) == {"cmd", "workdir", "max_output_tokens"}
                    and args["workdir"] == ROOT.as_posix() and args["max_output_tokens"] == 12000,
                    "UNAPPROVED_EXEC_ARGUMENTS")
            cmd = args["cmd"]
            body = None
            if cmd.startswith("@'\n"):
                split = cmd.split("\n'@ | ")
                require(len(split) == 2, "INVALID_HEREDOC")
                body = strict_json(split[0][3:])
                tail = split[1]
            else:
                tail = cmd
            require(tail.startswith(fixed), "UNAPPROVED_TRANSPORT_PATH")
            remainder = tail[len(fixed):]
            parts = remainder.split("' ")
            require(len(parts) == 2, "UNAPPROVED_TRANSPORT_SUFFIX")
            nonce, action = parts
            require(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce)
                    and hashlib.sha256(nonce.encode()).hexdigest() == metadata["workerNonceSha256"],
                    "BROKER_NONCE_HASH_MISMATCH")
            if action == "tools":
                require(body is None, "TOOLS_ARGUMENTS_INVALID")
                call = ("tools/list", None)
            else:
                suffix = re.fullmatch(r"call (semarail_[a-z_]+) --arguments-json -", action)
                require(suffix and isinstance(body, dict), "UNAPPROVED_MCP_CALL")
                call = (suffix.group(1), body)
            require(len(commands) < len(broker) and call == broker[len(commands)], "RAW_CALL_BROKER_MISMATCH")
            commands.append(call)
            accepted[cid] = (operation, None)
        elif kind == "custom_tool_call_output":
            cid = payload.get("call_id")
            require(cid in accepted and cid not in answered, "UNKNOWN_OR_DUPLICATE_TOOL_OUTPUT")
            operation, polled = accepted[cid]
            output = payload.get("output")
            require(isinstance(output, list), "TOOL_OUTPUT_SHAPE_INVALID")
            chunks = []
            for block in output:
                text = block.get("text", "") if isinstance(block, dict) else ""
                if text.startswith("{"):
                    value = strict_json(text)
                    require(isinstance(value, dict) and isinstance(value.get("output"), str)
                            and isinstance(value.get("chunk_id"), str)
                            and set(value) <= {"chunk_id", "wall_time_seconds", "session_id", "exit_code",
                                              "original_token_count", "output"}, "EXEC_METADATA_INVALID")
                    chunks.append(value)
            require(len(chunks) == 1, "EXEC_METADATA_MISSING")
            value = chunks[0]
            if "session_id" in value:
                sid = value["session_id"]
                require(type(sid) is int and (polled is None or sid == polled), "EXEC_SESSION_MISMATCH")
                active.add(sid)
            else:
                require(value.get("exit_code") == 0, "TRANSPORT_PROCESS_FAILED")
                if polled is not None:
                    active.remove(polled)
            answered.add(cid)
        elif payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
            final.append("".join(x.get("text", "") for x in payload.get("content", []) if isinstance(x, dict)))
    require(len(commands) == len(broker) and set(accepted) == answered
            and not active and len(final) == 1 and final[0], "INCOMPLETE_WORKER")
    return {"workerThreadId": thread, "traceSha256": hashlib.sha256(raw).hexdigest(),
            "observedModel": "gpt-6-luna", "observedReasoning": "max", "status": "passed",
            "transportCalls": len(commands), "provenanceBoundEmptyPolls": polls,
            "finalAnswer": final[0], "finalAnswerSha256": hashlib.sha256(final[0].encode()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--worker-thread-id", action="append", required=True)
    parser.add_argument("--broker-port", type=int, required=True)
    args = parser.parse_args()
    try:
        work = args.work_dir.resolve()
        args.metadata.resolve().relative_to(work / "private")
        metadata = strict_json(args.metadata.read_text(encoding="utf8"))
        transcript = work / "private" / metadata["eventsFile"]
        raw = transcript.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == metadata["sha256"], "BROKER_TRANSCRIPT_HASH_MISMATCH")
        records = [strict_json(x) for x in raw.decode("utf8").splitlines() if x]
        require(len(records) == metadata["eventCount"], "BROKER_EVENT_COUNT_MISMATCH")
        reports = []
        for tid in args.worker_thread_id:
            require(str(uuid.UUID(tid)) == tid, "INVALID_THREAD_ID")
            paths = list((Path.home() / ".codex" / "sessions").rglob(f"*{tid}*.jsonl"))
            require(len(paths) == 1, "NONUNIQUE_WORKER_TRACE")
            reports.append(audit(paths[0], tid, metadata, records, args.broker_port))
        spec = importlib.util.spec_from_file_location("oracle_review", ROOT / "scripts/lifecycle-independent-oracle-review.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        result = {"schemaVersion": 1, "source": "actual_external_worker_session_audit",
                  "transcriptSha256": metadata["sha256"], "workers": reports}
        output = work / "private" / f"agent-session-audit-{uuid.uuid4().hex[:8]}.json"
        helper._write_private_json(output, result)
        print(json.dumps({"status": "passed", "workers": len(reports), "report": str(output),
                          "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}))
        return 0
    except Exception as exc:
        code = str(exc) if type(exc) is ValueError and re.fullmatch(r"[A-Z_]+", str(exc)) else "AUDIT_FAILED"
        print(json.dumps({"status": "failed", "code": code, "type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
