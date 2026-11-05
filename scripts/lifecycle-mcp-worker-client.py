#!/usr/bin/env python3
"""Call the nonce-protected lifecycle MCP broker without reading credentials.

Examples:
    python scripts/lifecycle-mcp-worker-client.py --endpoint http://127.0.0.1:48765/mcp --nonce ... tools
    python scripts/lifecycle-mcp-worker-client.py --endpoint http://127.0.0.1:48765/mcp --nonce ... call semarail_get_context --arguments-json '{"question":"..."}'

The worker capability is sent only to the loopback broker. The MCP bearer key
is held and forwarded by that broker and is never available to this process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid


MAX_RESPONSE_BYTES = 1_048_576
MAX_ARGUMENT_BYTES = 262_144
ALLOWED_TOOLS = {
    "semarail_validate_project",
    "semarail_list_models",
    "semarail_get_context",
    "semarail_plan_query",
    "semarail_prepare_query",
    "semarail_governed_query",
    "semarail_get_query_evidence",
}


class ClientFailure(RuntimeError):
    """A safe worker-client failure code."""


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ClientFailure("BROKER_ENDPOINT_INVALID") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        raise ClientFailure("BROKER_ENDPOINT_INVALID")
    return value


def _thread_id_from_environment() -> str:
    """Read the caller's trusted task identity without a worker override."""

    value = os.environ.get("CODEX_THREAD_ID", "")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ClientFailure("WORKER_THREAD_ID_INVALID") from exc
    if str(parsed) != value:
        raise ClientFailure("WORKER_THREAD_ID_INVALID")
    return value


def _rpc(
    endpoint: str,
    nonce: str,
    worker_thread_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        endpoint,
        data=raw,
        headers={
            "X-SemaRail-Worker-Nonce": nonce,
            "X-SemaRail-Worker-Thread-ID": worker_thread_id,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=50) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        body = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ClientFailure("BROKER_RESPONSE_TOO_LARGE") from exc
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_json_object)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise ClientFailure("BROKER_REQUEST_FAILED") from exc
        if isinstance(value, dict):
            return value
        raise ClientFailure("BROKER_REQUEST_FAILED") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ClientFailure("BROKER_UNAVAILABLE") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise ClientFailure("BROKER_RESPONSE_TOO_LARGE")
    if not body:
        return None
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_json_object)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ClientFailure("BROKER_RESPONSE_INVALID") from exc
    if not isinstance(value, dict):
        raise ClientFailure("BROKER_RESPONSE_INVALID")
    return value


def _initialized(endpoint: str, nonce: str, worker_thread_id: str) -> None:
    response = _rpc(
        endpoint,
        nonce,
        worker_thread_id,
        {
            "jsonrpc": "2.0",
            "id": f"init-{uuid.uuid4().hex}",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "semarail-lifecycle-worker", "version": "1.0.0"},
            },
        },
    )
    if not isinstance(response, dict) or "error" in response or not isinstance(response.get("result"), dict):
        raise ClientFailure("MCP_INITIALIZE_FAILED")
    _rpc(
        endpoint,
        nonce,
        worker_thread_id,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )


def _read_arguments(value: str) -> dict[str, Any]:
    if value != "-":
        raw = value.encode("utf-8")
    else:
        raw = sys.stdin.buffer.read(MAX_ARGUMENT_BYTES + 1)
    if len(raw) > MAX_ARGUMENT_BYTES:
        raise ClientFailure("TOOL_ARGUMENTS_TOO_LARGE")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ClientFailure("TOOL_ARGUMENTS_INVALID") from exc
    if not isinstance(parsed, dict):
        raise ClientFailure("TOOL_ARGUMENTS_INVALID")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--nonce", required=True, help="short-lived local broker capability; never an MCP or database key")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("tools", help="list the broker's allowed read/query MCP tools")
    call = subparsers.add_parser("call", help="call one allowed MCP tool")
    call.add_argument("tool", choices=sorted(ALLOWED_TOOLS))
    call.add_argument(
        "--arguments-json",
        default="{}",
        help="strict JSON object; use '-' to read bounded JSON from stdin",
    )
    args = parser.parse_args(argv)

    try:
        endpoint = _endpoint(args.endpoint)
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", args.nonce):
            raise ClientFailure("WORKER_NONCE_INVALID")
        worker_thread_id = _thread_id_from_environment()
        try:
            arguments = _read_arguments(args.arguments_json) if args.action == "call" else {}
        except ClientFailure:
            raise
        _initialized(endpoint, args.nonce, worker_thread_id)
        if args.action == "tools":
            payload = {"jsonrpc": "2.0", "id": f"tools-{uuid.uuid4().hex}", "method": "tools/list"}
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": f"call-{uuid.uuid4().hex}",
                "method": "tools/call",
                "params": {"name": args.tool, "arguments": arguments},
            }
        response = _rpc(endpoint, args.nonce, worker_thread_id, payload)
        if response is None:
            raise ClientFailure("MCP_RESPONSE_MISSING")
        print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
        return 0 if "error" not in response else 1
    except ClientFailure as exc:
        print(json.dumps({"status": "failed", "code": str(exc)}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
