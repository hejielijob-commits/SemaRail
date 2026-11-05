"""Credential-free, bounded transport for one frozen benchmark job."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any
import uuid


def _thread_identity_header(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Attach trusted app session identity out of band, never from request JSON."""
    source = os.environ if environ is None else environ
    value = source.get("CODEX_THREAD_ID")
    if value is None:
        return {}
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("Codex session identity is invalid") from None
    if canonical != value.casefold():
        raise ValueError("Codex session identity is not canonical")
    return {"X-BTV-Thread-ID": canonical}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("job")
    parser.add_argument("operation", choices=("begin", "tool", "clarify", "final"))
    parser.add_argument("payload", choices=("-",))
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    if not re.fullmatch(r"job-\d{3}", args.job) or not 1 <= args.port <= 65535:
        raise SystemExit("invalid broker destination")
    raw = sys.stdin.buffer.read(128_001)
    if not 0 < len(raw) <= 128_000:
        raise SystemExit("bounded JSON object required")
    try:
        request = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SystemExit("valid JSON object required") from None
    if not isinstance(request, dict):
        raise SystemExit("JSON object required")
    if args.operation == "begin" and (
        set(request) != {"nonce"}
        or not isinstance(request.get("nonce"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{24,}", request["nonce"])
    ):
        raise SystemExit("the frozen job nonce is required")
    message = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    target = f"http://127.0.0.1:{args.port}/{args.job}/{args.operation}"
    try:
        headers = {"Content-Type": "application/json", **_thread_identity_header()}
    except ValueError:
        raise SystemExit("trusted Codex session identity is invalid") from None
    outbound = urllib.request.Request(
        target,
        data=message,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(outbound, timeout=50) as response:
            print(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print(error.read().decode("utf-8"))
        return 1
    except (urllib.error.URLError, TimeoutError):
        print(json.dumps({"error": "coordinator_unavailable"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
