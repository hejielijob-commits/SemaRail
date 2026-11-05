#!/usr/bin/env python3
"""Expose a nonce-protected, read-only MCP bridge for one retained lifecycle.

The broker reads the MCP bearer token from the retained acceptance's private
file and never returns it to the worker. Tool exchanges, including actual query
results, are written to a protected JSONL transcript for independent review.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
import uuid


MAX_BODY_BYTES = 1_048_576
ALLOWED_TOOLS = {
    "semarail_validate_project",
    "semarail_list_models",
    "semarail_get_context",
    "semarail_plan_query",
    "semarail_prepare_query",
    "semarail_governed_query",
    "semarail_get_query_evidence",
}
ALLOWED_METHODS = {"initialize", "notifications/initialized", "ping", "tools/list", "tools/call"}


class BrokerFailure(RuntimeError):
    """A safe worker-broker failure code."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if not 0 < len(raw) <= 1_048_576:
            raise ValueError("invalid size")
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise BrokerFailure(code) from exc
    if not isinstance(result, dict):
        raise BrokerFailure(code)
    return result


def _protect_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
        if os.name == "nt":
            identity = subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"],
                check=True,
                capture_output=True,
                text=True,
            )
            import csv

            rows = list(csv.reader(identity.stdout.splitlines()))
            sid = next(
                (
                    row[-1].strip()
                    for row in rows
                    if row and re.fullmatch(r"S-1-(?:[0-9]+-)+[0-9]+", row[-1].strip())
                ),
                None,
            )
            if sid is None:
                raise OSError("current user SID is unavailable")
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(M)"],
                check=True,
                capture_output=True,
                text=True,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise BrokerFailure("PRIVATE_ARTIFACT_PROTECTION_FAILED") from exc


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        temporary.write_bytes(raw)
        _protect_file(temporary)
        os.replace(temporary, path)
        _protect_file(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise BrokerFailure("PRIVATE_ARTIFACT_WRITE_FAILED") from exc


def _read_mcp_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BrokerFailure("MCP_CREDENTIAL_UNAVAILABLE") from exc
    if not re.fullmatch(r"sr_live_[A-Za-z0-9_-]{20,}", token):
        raise BrokerFailure("MCP_CREDENTIAL_INVALID")
    return token


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class LifecycleBroker:
    def __init__(self, work_dir: Path, *, port: int, nonce: str) -> None:
        self.work_dir = work_dir.expanduser().resolve()
        self.report_path = self.work_dir / "acceptance-report.json"
        self.report = _read_json(self.report_path, "ACCEPTANCE_REPORT_UNAVAILABLE")
        if (
            self.report.get("status") != "sql_lifecycle_passed_agent_review_pending"
            or self.report.get("externalAgentEvaluationImported") is not False
            or self.report.get("sourceFeedbackAndRegressionRecordsAreActual") is not True
            or self.report.get("queryValuesIncluded") is not False
        ):
            raise BrokerFailure("ACCEPTANCE_NOT_READY_FOR_AGENT_REVIEW")

        endpoint = self.report.get("mcpEndpoint")
        if not isinstance(endpoint, str):
            raise BrokerFailure("MCP_ENDPOINT_UNAVAILABLE")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path != "/mcp" or parsed.query or parsed.fragment:
            raise BrokerFailure("MCP_ENDPOINT_INVALID")
        try:
            endpoint_port = parsed.port
        except ValueError as exc:
            raise BrokerFailure("MCP_ENDPOINT_INVALID") from exc
        if endpoint_port is None or not 1 <= endpoint_port <= 65_535:
            raise BrokerFailure("MCP_ENDPOINT_INVALID")
        self.target_url = endpoint
        self.target_port = endpoint_port

        state_dir = Path(str(self.report.get("isolatedStateDirectory", ""))).expanduser().resolve()
        try:
            state_dir.relative_to(self.work_dir)
        except ValueError as exc:
            raise BrokerFailure("ACCEPTANCE_PATH_OUTSIDE_WORKSPACE") from exc
        credential_file = Path(str(self.report.get("mcpCredentialFile", ""))).expanduser().resolve()
        expected_file = (state_dir / "private" / "agent-mcp-token").resolve()
        if credential_file != expected_file:
            raise BrokerFailure("MCP_CREDENTIAL_PATH_INVALID")
        self.token = _read_mcp_token(credential_file)

        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce):
            raise BrokerFailure("WORKER_NONCE_INVALID")
        self.nonce = nonce.encode("ascii")
        self.nonce_sha256 = hashlib.sha256(self.nonce).hexdigest()
        self.port = port
        self._lock = threading.Lock()
        self._sequence = 0
        self._started = _timestamp()
        private_dir = self.work_dir / "private"
        private_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        artifact_suffix = secrets.token_hex(4)
        self.events_path = private_dir / f"worker-events-{timestamp}-{artifact_suffix}.jsonl"
        descriptor = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        _protect_file(self.events_path)
        self.artifact_path = private_dir / f"worker-artifact-{timestamp}-{artifact_suffix}.json"
        self.event_count = 0
        self._write_artifact_metadata()

    def _write_artifact_metadata(self) -> None:
        try:
            digest = hashlib.sha256(self.events_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise BrokerFailure("PRIVATE_ARTIFACT_READ_FAILED") from exc
        _write_private_json(
            self.artifact_path,
            {
                "schemaVersion": 1,
                "source": "authenticated_actual_mcp_transcript",
                "createdAt": self._started,
                "updatedAt": _timestamp(),
                "mcpEndpoint": self.target_url,
                "workerNonceSha256": self.nonce_sha256,
                "projectId": self.report.get("projectId"),
                "datasetVersion": self.report.get("datasetVersion"),
                "semanticVersion": self.report.get("fixSemanticVersion"),
                "eventsFile": self.events_path.name,
                "eventCount": self.event_count,
                "sha256": digest,
            },
        )

    def _record(
        self,
        request: dict[str, Any],
        response: dict[str, Any] | None,
        status: int,
        *,
        worker_thread_id: str,
    ) -> None:
        with self._lock:
            self._sequence += 1
            record = {
                "schemaVersion": 1,
                "sequence": self._sequence,
                "timestamp": _timestamp(),
                "httpStatus": status,
                "workerThreadId": worker_thread_id,
                "request": request,
                "response": response,
            }
            rendered = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            try:
                with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(rendered)
                self.event_count += 1
                self._write_artifact_metadata()
            except OSError as exc:
                raise BrokerFailure("PRIVATE_ARTIFACT_WRITE_FAILED") from exc

    @staticmethod
    def _validate_rpc(payload: Any) -> dict[str, Any]:
        if (
            not isinstance(payload, dict)
            or set(payload) - {"jsonrpc", "id", "method", "params"}
            or payload.get("jsonrpc") != "2.0"
            or _contains_forbidden_identity_metadata(payload)
        ):
            raise BrokerFailure("MCP_REQUEST_INVALID")
        method = payload.get("method")
        if not isinstance(method, str) or method not in ALLOWED_METHODS:
            raise BrokerFailure("MCP_METHOD_NOT_ALLOWED")
        if method == "tools/call":
            params = payload.get("params")
            if (
                not isinstance(params, dict)
                or set(params) - {"name", "arguments"}
                or params.get("name") not in ALLOWED_TOOLS
            ):
                raise BrokerFailure("MCP_TOOL_NOT_ALLOWED")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise BrokerFailure("MCP_ARGUMENTS_INVALID")
            if _contains_forbidden_identity_metadata(arguments):
                raise BrokerFailure("MCP_IDENTITY_METADATA_NOT_ALLOWED")
        return payload

    def exchange(
        self,
        payload: dict[str, Any],
        *,
        worker_thread_id: str,
    ) -> tuple[int, bytes, str]:
        method = payload["method"]
        if method not in {"notifications/initialized"} and "id" not in payload:
            raise BrokerFailure("MCP_REQUEST_ID_REQUIRED")
        request_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        upstream_headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if method != "initialize":
            upstream_headers["MCP-Protocol-Version"] = "2025-03-26"
        request = Request(
            self.target_url,
            data=request_body,
            headers=upstream_headers,
            method="POST",
        )
        status = 502
        response_body = b""
        content_type = "application/json"
        try:
            with urlopen(request, timeout=45) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "application/json")
                response_body = response.read(MAX_BODY_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json") if exc.headers else "application/json"
            response_body = exc.read(MAX_BODY_BYTES + 1)
        except (URLError, TimeoutError, OSError) as exc:
            # Do not expose transport exception text because it may contain the
            # URL or authorization details from a lower-level implementation.
            self._record(payload, {"error": "UPSTREAM_UNAVAILABLE"}, 502, worker_thread_id=worker_thread_id)
            raise BrokerFailure("UPSTREAM_UNAVAILABLE") from exc
        if len(response_body) > MAX_BODY_BYTES:
            self._record(payload, {"error": "UPSTREAM_RESPONSE_TOO_LARGE"}, status, worker_thread_id=worker_thread_id)
            raise BrokerFailure("UPSTREAM_RESPONSE_TOO_LARGE")
        parsed_response: dict[str, Any] | None = None
        if response_body:
            try:
                if "text/event-stream" in content_type:
                    lines = [line[5:].strip() for line in response_body.decode("utf-8").splitlines() if line.startswith("data:")]
                    response_body = next((line.encode("utf-8") for line in reversed(lines) if line), b"")
                    content_type = "application/json"
                parsed = json.loads(response_body.decode("utf-8"), object_pairs_hook=_unique_object) if response_body else None
                if isinstance(parsed, dict):
                    parsed_response = parsed
                elif parsed is not None:
                    raise ValueError("response must be an object")
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                self._record(payload, {"error": "UPSTREAM_RESPONSE_INVALID"}, status, worker_thread_id=worker_thread_id)
                raise BrokerFailure("UPSTREAM_RESPONSE_INVALID") from exc
        if payload.get("method") == "tools/list" and isinstance(parsed_response, dict):
            result = parsed_response.get("result")
            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                result["tools"] = [tool for tool in result["tools"] if isinstance(tool, dict) and tool.get("name") in ALLOWED_TOOLS]
                response_body = json.dumps(parsed_response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                content_type = "application/json"
        self._record(payload, parsed_response, status, worker_thread_id=worker_thread_id)
        return status, response_body, content_type


def _contains_forbidden_identity_metadata(value: Any) -> bool:
    if isinstance(value, dict):
        blocked = {
            "workerthreadid",
            "threadid",
            "codexthreadid",
            "workerid",
            "sessionid",
            "sessionmetadata",
            "metadata",
            "meta",
        }
        if any(
            re.sub(r"[^a-z]", "", key.casefold()) in blocked
            for key in value
            if isinstance(key, str)
        ):
            return True
        return any(_contains_forbidden_identity_metadata(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_identity_metadata(item) for item in value)
    return False


def _strict_worker_thread_id(value: str) -> str | None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
    return value if str(parsed) == value else None


class BrokerHandler(BaseHTTPRequestHandler):
    server_version = "SemaRailLifecycleBroker"
    sys_version = ""

    @property
    def broker(self) -> LifecycleBroker:
        return self.server.broker  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *_args: Any) -> None:
        # Default HTTP logging includes remote details and request paths.
        return

    def handle_error(self, _request: Any, _client_address: Any) -> None:
        # Never dump request bodies, tool results, or local connection details.
        return

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        rendered = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(rendered)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(rendered)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self._send_json(403, {"error": "LOOPBACK_ONLY"})
            return
        if self.path != "/mcp":
            self._send_json(404, {"error": "NOT_FOUND"})
            return
        supplied = self.headers.get("X-SemaRail-Worker-Nonce", "").encode("utf-8")
        if not hmac.compare_digest(supplied, self.broker.nonce):
            self._send_json(401, {"error": "WORKER_NONCE_INVALID"})
            return
        worker_thread_id = _strict_worker_thread_id(
            self.headers.get("X-SemaRail-Worker-Thread-ID", "")
        )
        if worker_thread_id is None:
            self._send_json(400, {"error": "WORKER_THREAD_ID_INVALID"})
            return
        try:
            raw_size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "REQUEST_SIZE_INVALID"})
            return
        if not 0 < raw_size <= MAX_BODY_BYTES:
            self._send_json(413, {"error": "REQUEST_SIZE_INVALID"})
            return
        try:
            raw = self.rfile.read(raw_size)
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            payload = self.broker._validate_rpc(payload)
            status, response_body, content_type = self.broker.exchange(
                payload,
                worker_thread_id=worker_thread_id,
            )
        except BrokerFailure as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "MCP_REQUEST_INVALID"})
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=48_765)
    parser.add_argument("--nonce", help="one-time worker capability; generated if omitted")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.port <= 65_535:
            raise BrokerFailure("BROKER_PORT_INVALID")
        nonce = args.nonce or secrets.token_urlsafe(32)
        broker = LifecycleBroker(args.work_dir, port=args.port, nonce=nonce)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), BrokerHandler)
        server.broker = broker  # type: ignore[attr-defined]
        server.daemon_threads = True
        print(
            json.dumps(
                {
                    "status": "ready",
                    "endpoint": f"http://127.0.0.1:{args.port}/mcp",
                    "workerNonce": nonce,
                    "artifactMetadata": str(broker.artifact_path),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.5)
        return 0
    except BrokerFailure as exc:
        print(json.dumps({"status": "failed", "code": str(exc)}, separators=(",", ":")))
        return 1
    except OSError:
        print(json.dumps({"status": "failed", "code": "BROKER_START_FAILED"}, separators=(",", ":")))
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
