"""Authenticated MCP stdio bridge for a running SemaRail Core.

This process deliberately owns no semantic or query service.  It exposes the
stable SemaRail tools and forwards their public parameters to Core's single
authenticated runtime boundary, where identity and current policy are resolved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations


DEFAULT_CORE_ENDPOINT = "http://127.0.0.1:48763"
DEFAULT_TOKEN_ENV = "SEMARAIL_MCP_TOKEN"
_TOKEN_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SAFE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SERVICE_TOKEN_PATTERN = re.compile(r"^sr_live_[a-f0-9]{24}_[A-Za-z0-9_-]{32,128}$")
_SESSION_TOKEN_PATTERN = re.compile(r"^sr_session_[a-f0-9]{24}_[A-Za-z0-9_-]{32,128}$")
_SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+\S+|sr_(?:session|key)_[A-Za-z0-9._~-]+|"
    r"(?:postgres(?:ql)?|mysql|clickhouse)://\S+)"
)
_SAFE_TRACE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DETAILED_REASON_CODES = frozenset({
    "AUTHENTICATION_EXPIRED", "ACCOUNT_DISABLED", "PROJECT_PERMISSION_REQUIRED",
    "DATASOURCE_PERMISSION_REQUIRED", "TOOL_PERMISSION_REQUIRED", "TABLE_PERMISSION_REQUIRED",
    "COLUMN_PERMISSION_REQUIRED", "EXPLICIT_DENIAL", "UNAUTHORIZED", "ROW_ATTRIBUTE_MISSING",
    "DATABASE_PERMISSION_REQUIRED", "SQL_SAFETY_RESTRICTION", "SEMANTIC_PARSE_FAILED",
    "QUERY_TIMEOUT", "CONNECTION_FAILED", "UNSUPPORTED_DATASOURCE", "INTERNAL_FAILURE",
    "CLARIFICATION_REQUIRED",
})
_DETAILED_ORIGINS = frozenset({
    "authentication", "semarail-policy", "query-safety", "semantic-runtime",
    "database", "transport", "core",
})
_DETAILED_RESOURCE_KINDS = frozenset({
    "project", "datasource", "tool", "table", "column", "attribute",
})


class CoreTransport(Protocol):
    """Minimal authenticated Core RPC behavior used by the MCP tools."""

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]: ...

    async def submit_feedback(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    """Never forward a bearer credential across an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def normalize_core_endpoint(value: str) -> str:
    """Return a credential-free origin suitable for the fixed Core RPC path."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("Core endpoint is required")
    parsed = urlsplit(value.strip())
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme not in {"http", "https"} or (parsed.scheme == "http" and not loopback):
        raise ValueError("Core endpoint must be HTTPS or loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Core endpoint cannot contain credentials, query, or fragment")
    if parsed.path not in {"", "/"} or not parsed.hostname:
        raise ValueError("Core endpoint must be an origin without a path")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Core endpoint port is invalid") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("Core endpoint port is invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _parse_expiry(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("employee session is invalid; run `semarail auth login`")
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("employee session is invalid; run `semarail auth login`") from exc
    if expiry.tzinfo is None:
        raise ValueError("employee session is invalid; run `semarail auth login`")
    return expiry.astimezone(UTC)


def load_employee_session(path: str | Path) -> tuple[str, str]:
    """Read the protected session produced by ``semarail auth login``."""

    session_path = Path(path).expanduser()
    try:
        metadata = session_path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("employee session file must be a regular file")
        if metadata.st_size > 65_536:
            raise ValueError("employee session file is invalid")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("employee session file permissions are too broad")
        payload = json.loads(session_path.read_text(encoding="utf-8"))
    except ValueError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("employee session is unavailable; run `semarail auth login`") from exc
    token = payload.get("accessToken") if isinstance(payload, Mapping) else None
    endpoint = payload.get("endpoint") if isinstance(payload, Mapping) else None
    if not isinstance(token, str) or not _SESSION_TOKEN_PATTERN.fullmatch(token):
        raise ValueError("employee session is invalid; run `semarail auth login`")
    if _parse_expiry(payload.get("expiresAt")) <= datetime.now(UTC):
        raise ValueError("employee session has expired; run `semarail auth login`")
    return normalize_core_endpoint(endpoint), token


def resolve_authentication(
    *,
    endpoint: str | None,
    session_file: str | Path,
    token_env: str = DEFAULT_TOKEN_ENV,
) -> tuple[str, str]:
    """Resolve a service token from env, otherwise the employee session file."""

    if not _TOKEN_ENV_PATTERN.fullmatch(token_env):
        raise ValueError("token environment variable name is invalid")
    service_token = os.environ.get(token_env, "").strip()
    if service_token:
        # Only a managed, revocable service-account key may cross this Agent
        # boundary. In particular, an arbitrary bootstrap administrator token
        # must not be accepted merely because it is long enough.
        if not _SERVICE_TOKEN_PATTERN.fullmatch(service_token):
            raise ValueError(f"{token_env} must contain a SemaRail service-account key")
        return normalize_core_endpoint(endpoint or DEFAULT_CORE_ENDPOINT), service_token
    session_endpoint, session_token = load_employee_session(session_file)
    if endpoint is not None and normalize_core_endpoint(endpoint) != session_endpoint:
        raise ValueError("employee session belongs to a different Core endpoint")
    return session_endpoint, session_token


def _safe_error(status: int, payload: Any = None) -> dict[str, Any]:
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if isinstance(payload, Mapping) and payload.get("protocolVersion") == "2" and isinstance(error, Mapping):
        expected_fields = {
            "code", "phase", "message", "retryable", "reasonCode", "resources",
            "requiredPermissions", "suggestion", "origin", "traceId",
        }
        code = error.get("code")
        phase = error.get("phase")
        message = error.get("message")
        reason = error.get("reasonCode")
        resources = error.get("resources")
        permissions = error.get("requiredPermissions")
        suggestion = error.get("suggestion")
        origin = error.get("origin")
        trace_id = error.get("traceId")
        valid_resources = (
            isinstance(resources, list)
            and len(resources) <= 32
            and all(
                isinstance(item, Mapping)
                and set(item) == {"kind", "name"}
                and item.get("kind") in _DETAILED_RESOURCE_KINDS
                and isinstance(item.get("name"), str)
                and 1 <= len(item["name"]) <= 512
                and not _SECRET_PATTERN.search(item["name"])
                for item in resources
            )
        )
        valid_permissions = (
            isinstance(permissions, list)
            and len(permissions) <= 32
            and all(
                isinstance(item, str)
                and 1 <= len(item) <= 256
                and not _SECRET_PATTERN.search(item)
                for item in permissions
            )
        )
        if (
            set(error) == expected_fields
            and isinstance(code, str) and _SAFE_CODE_PATTERN.fullmatch(code)
            and isinstance(phase, str) and 1 <= len(phase) <= 64
            and isinstance(message, str) and 1 <= len(message) <= 500 and not _SECRET_PATTERN.search(message)
            and type(error.get("retryable")) is bool
            and reason in _DETAILED_REASON_CODES
            and valid_resources
            and valid_permissions
            and isinstance(suggestion, str) and 1 <= len(suggestion) <= 2_000 and not _SECRET_PATTERN.search(suggestion)
            and origin in _DETAILED_ORIGINS
            and isinstance(trace_id, str) and _SAFE_TRACE_PATTERN.fullmatch(trace_id)
        ):
            return {
                "code": code,
                "phase": phase,
                "message": message,
                "retryable": error["retryable"],
                "reasonCode": reason,
                "resources": [dict(item) for item in resources],
                "requiredPermissions": list(permissions),
                "suggestion": suggestion,
                "origin": origin,
                "traceId": trace_id,
            }
    raw_code = error.get("code") if isinstance(error, Mapping) else None
    code = raw_code if isinstance(raw_code, str) and _SAFE_CODE_PATTERN.fullmatch(raw_code) else "CORE_REQUEST_FAILED"
    if status == 401:
        code, message = "UNAUTHENTICATED", "authentication is required or no longer valid"
    elif status == 403:
        code, message = "FORBIDDEN", "permission is required"
    else:
        raw_message = error.get("message") if isinstance(error, Mapping) else None
        message = raw_message.strip()[:500] if isinstance(raw_message, str) else "SemaRail operation failed"
        if not message or _SECRET_PATTERN.search(message):
            message = "SemaRail operation failed"
    phase = error.get("phase") if isinstance(error, Mapping) else None
    return {
        "code": code,
        "phase": phase[:64] if isinstance(phase, str) else "core-rpc",
        "message": message,
        "retryable": bool(error.get("retryable")) if isinstance(error, Mapping) else False,
    }


class CoreHttpTransport:
    """Call one fixed Core RPC endpoint with an in-memory bearer credential."""

    def __init__(self, endpoint: str, token: str, *, timeout_seconds: float = 35.0) -> None:
        self._endpoint = normalize_core_endpoint(endpoint)
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Core bearer credential is invalid")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoRedirect())

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._call_sync, method, params)

    async def submit_feedback(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._submit_feedback_sync, payload)

    def _submit_feedback_sync(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(body) > 128 * 1024:
            raise ToolError(json.dumps({"code": "INVALID_FEEDBACK", "message": "feedback is too large"}))
        request = Request(
            f"{self._endpoint}/api/v1/feedback",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                status = response.status
                raw = response.read(131_073)
        except HTTPError as exc:
            status = exc.code
            raw = exc.read(131_073)
        except (OSError, URLError, TimeoutError):
            raise ToolError(
                json.dumps({"code": "FEEDBACK_SUBMISSION_FAILED", "message": "feedback could not be saved"})
            ) from None
        if len(raw) > 131_072:
            raise ToolError(json.dumps({"code": "FEEDBACK_SUBMISSION_FAILED", "message": "feedback could not be saved"}))
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ToolError(
                json.dumps({"code": "FEEDBACK_SUBMISSION_FAILED", "message": "feedback could not be saved"})
            ) from None
        if status != 201 or not isinstance(result, Mapping):
            code = result.get("code") if isinstance(result, Mapping) else None
            message = result.get("message") if isinstance(result, Mapping) else None
            safe = {
                "code": code if isinstance(code, str) and _SAFE_CODE_PATTERN.fullmatch(code) else "FEEDBACK_SUBMISSION_FAILED",
                "message": message[:500] if isinstance(message, str) and not _SECRET_PATTERN.search(message) else "feedback could not be saved",
            }
            raise ToolError(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))
        if self._token in json.dumps(result, ensure_ascii=False, separators=(",", ":")):
            raise ToolError(
                json.dumps({"code": "FEEDBACK_SUBMISSION_FAILED", "message": "feedback could not be saved"})
            )
        return dict(result)

    def _call_sync(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = f"stdio-mcp-{uuid.uuid4().hex}"
        body = json.dumps(
            {"protocolVersion": "2", "id": request_id, "method": method, "params": dict(params)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            f"{self._endpoint}/api/v1/runtime/rpc",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                status = response.status
                raw = response.read(2_097_153)
        except HTTPError as exc:
            status = exc.code
            raw = exc.read(2_097_153)
        except (OSError, URLError, TimeoutError):
            raise ToolError(json.dumps(_safe_error(503), separators=(",", ":"))) from None
        if len(raw) > 2_097_152:
            raise ToolError(json.dumps(_safe_error(502), separators=(",", ":")))
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ToolError(json.dumps(_safe_error(status), separators=(",", ":"))) from None
        if status != 200 or not isinstance(payload, Mapping) or payload.get("ok") is not True:
            raise ToolError(json.dumps(_safe_error(status, payload), ensure_ascii=False, separators=(",", ":")))
        if payload.get("id") != request_id or payload.get("protocolVersion") != "2":
            raise ToolError(json.dumps(_safe_error(502), separators=(",", ":")))
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise ToolError(json.dumps(_safe_error(502), separators=(",", ":")))
        if self._token in json.dumps(result, ensure_ascii=False, separators=(",", ":")):
            raise ToolError(json.dumps(_safe_error(502), separators=(",", ":")))
        return dict(result)


def _readonly(title: str, *, idempotent: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def _write(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _stateful(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def create_stdio_mcp_server(transport: CoreTransport) -> FastMCP:
    """Expose only SemaRail's stable, policy-neutral public tool parameters."""

    server = FastMCP(
        "semarail",
        instructions=(
            "Use SemaRail context and planning before governed execution. "
            "Identity, datasource selection, and policy are resolved by the running SemaRail Core."
        ),
        log_level="WARNING",
    )

    @server.tool(annotations=_readonly("Validate the SemaRail semantic project"))
    async def semarail_validate_project() -> dict[str, Any]:
        return await transport.call("project.validate", {})

    @server.tool(annotations=_readonly("List SemaRail semantic models"))
    async def semarail_list_models() -> dict[str, Any]:
        return await transport.call("project.describe", {})

    @server.tool(annotations=_readonly("Get SemaRail semantic context"))
    async def semarail_get_context(question: str) -> dict[str, Any]:
        return await transport.call("context.ask", {"question": question})

    @server.tool(annotations=_readonly("Plan a SemaRail semantic query"))
    async def semarail_plan_query(semantic_sql: str) -> dict[str, Any]:
        return await transport.call("query.dryPlan", {"semanticSql": semantic_sql})

    @server.tool(annotations=_stateful("Prepare and confirm a SemaRail query"))
    async def semarail_prepare_query(
        question: str,
        semantic_sql: str,
        conditions: dict[str, Any] | None = None,
        confirmed_conditions: list[str] | None = None,
    ) -> dict[str, Any]:
        return await transport.call(
            "query.prepare",
            {
                "question": question,
                "semanticSql": semantic_sql,
                "conditions": conditions or {},
                "confirmedConditions": confirmed_conditions or [],
            },
        )

    @server.tool(annotations=_readonly("Run a governed SemaRail query", idempotent=False))
    async def semarail_governed_query(
        question: str,
        semantic_sql: str,
        chart_intent: Literal["auto", "table", "line", "bar", "pie"] = "auto",
        preparation_id: str | None = None,
    ) -> dict[str, Any]:
        query_id = f"stdio-mcp-query-{uuid.uuid4().hex}"
        try:
            return await transport.call(
                "query.run",
                {
                    "question": question,
                    "semanticSql": semantic_sql,
                    "chartIntent": chart_intent,
                    "queryId": query_id,
                    **({"preparationId": preparation_id} if preparation_id is not None else {}),
                },
            )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(transport.call("query.cancel", {"queryId": query_id}))
            except Exception:
                pass
            raise

    @server.tool(annotations=_write("Submit SemaRail query feedback"))
    async def semarail_submit_feedback(
        reference: str,
        idempotency_key: str,
        category: Literal[
            "ambiguity", "knowledge_gap", "agent_understanding", "sql_generation",
            "permission_configuration", "runtime_failure", "evaluation", "other",
        ],
        description: str,
        expected_behavior: str | None = None,
        question: str | None = None,
        semantic_sql: str | None = None,
        native_sql: str | None = None,
    ) -> dict[str, Any]:
        return await transport.submit_feedback(
            {
                "reference": reference,
                "idempotencyKey": idempotency_key,
                "category": category,
                "description": description,
                **({"expectedBehavior": expected_behavior} if expected_behavior is not None else {}),
                **({"question": question} if question is not None else {}),
                **({"semanticSql": semantic_sql} if semantic_sql is not None else {}),
                **({"nativeSql": native_sql} if native_sql is not None else {}),
            }
        )

    # MCP 1.28's FastMCP compatibility default ignores unknown function
    # arguments.  That is unsafe at an authorization boundary: a caller could
    # believe it selected a subject, policy, or DSN even though Core ignored
    # it.  Make the advertised schema and the pinned SDK validator fail closed.
    for tool in server._tool_manager.list_tools():  # noqa: SLF001 - pinned MCP 1.28 contract
        tool.parameters["additionalProperties"] = False
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)

    return server


def _default_session_file() -> Path:
    configured = os.environ.get("SEMARAIL_AUTH_FILE", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".semarail" / "session.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bridge MCP stdio to an authenticated SemaRail Core")
    parser.add_argument("--endpoint", help="fixed SemaRail Core origin; defaults to the employee session origin")
    parser.add_argument("--session-file", default=str(_default_session_file()))
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="service-account token environment variable")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        endpoint, token = resolve_authentication(
            endpoint=args.endpoint,
            session_file=args.session_file,
            token_env=args.token_env,
        )
        server = create_stdio_mcp_server(CoreHttpTransport(endpoint, token))
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    server.run(transport="stdio")
    return 0


__all__ = [
    "CoreHttpTransport",
    "CoreTransport",
    "DEFAULT_CORE_ENDPOINT",
    "DEFAULT_TOKEN_ENV",
    "create_stdio_mcp_server",
    "load_employee_session",
    "main",
    "normalize_core_endpoint",
    "resolve_authentication",
]


if __name__ == "__main__":  # pragma: no cover - exercised by MCP clients
    raise SystemExit(main())
