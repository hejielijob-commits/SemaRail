"""Authenticated SemaRail MCP over stateless Streamable HTTP."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.server import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

try:
    from .access_control import AccessControlError, AccessControlStore, BOOTSTRAP_SUBJECT_ID
    from .artifact_store import ArtifactDownload, ArtifactError
    from .project import ProjectStore
    from .runtime_rpc import RuntimeRpcGateway
except ImportError:  # pragma: no cover - direct module loading
    from access_control import AccessControlError, AccessControlStore, BOOTSTRAP_SUBJECT_ID  # type: ignore[no-redef]
    from artifact_store import ArtifactDownload, ArtifactError  # type: ignore[no-redef]
    from project import ProjectStore  # type: ignore[no-redef]
    from runtime_rpc import RuntimeRpcGateway  # type: ignore[no-redef]


DEFAULT_REMOTE_MCP_HOST = "127.0.0.1"
DEFAULT_REMOTE_MCP_PORT = 48764


class SemaRailTokenVerifier:
    """Adapt the control-plane API keys to the MCP SDK bearer boundary."""

    def __init__(self, store: AccessControlStore) -> None:
        self.store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            auth = self.store.authenticate(f"Bearer {token}")
        except AccessControlError:
            return None
        # The bootstrap credential exists only for loopback administration and
        # must never become a reusable bearer credential on a network-facing
        # MCP endpoint. Remote MCP accepts managed, revocable identities only.
        if auth.subject.id == BOOTSTRAP_SUBJECT_ID:
            return None
        return AccessToken(
            token=token,
            client_id=auth.subject.id,
            subject=auth.subject.id,
            scopes=["semarail"],
            claims={
                "organizationId": auth.subject.organization_id,
                "subjectType": auth.subject.kind,
                **({"credentialId": auth.credential_id} if auth.credential_id else {}),
            },
        )


class RuntimeMcpBridge:
    """Translate stable MCP tools into the authenticated Core RPC contract."""

    def __init__(self, gateway: RuntimeRpcGateway, *, artifact_base_url: str | None = None) -> None:
        self.gateway = gateway
        self.artifact_base_url = artifact_base_url

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        access = get_access_token()
        if access is None or not access.token:
            raise ToolError("authentication is required")
        request_id = f"remote-mcp-{uuid.uuid4().hex}"
        status, response = await asyncio.to_thread(
            self.gateway.dispatch,
            {
                "protocolVersion": "1",
                "id": request_id,
                "method": method,
                "params": dict(params),
            },
            f"Bearer {access.token}",
            transport="remote-mcp",
            artifact_base_url=self.artifact_base_url,
        )
        if status != 200 or response.get("ok") is not True:
            error = response.get("error")
            safe = error if isinstance(error, Mapping) else {
                "code": "INTERNAL_ERROR",
                "phase": "remote-mcp",
                "message": "SemaRail operation failed",
                "retryable": False,
            }
            raise ToolError(json.dumps(dict(safe), ensure_ascii=False, separators=(",", ":")))
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ToolError("SemaRail operation returned an invalid result")
        return dict(result)


def _readonly(title: str, *, idempotent: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def create_remote_mcp_server(
    *,
    project: str | Path,
    state_dir: str | Path | None = None,
    host: str = DEFAULT_REMOTE_MCP_HOST,
    port: int = DEFAULT_REMOTE_MCP_PORT,
    allowed_hosts: Sequence[str] | None = None,
    gateway: RuntimeRpcGateway | None = None,
    artifact_base_url: str | None = None,
) -> FastMCP:
    """Create one authenticated, stateless Streamable HTTP MCP server."""

    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535:
        raise ValueError("remote MCP port is invalid")
    project_store = gateway.project if gateway is not None else ProjectStore(project, state_dir=state_dir)
    runtime = gateway or RuntimeRpcGateway(project_store)
    verifier = SemaRailTokenVerifier(runtime.access_control)
    defaults = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    safe_hosts = list(allowed_hosts) if allowed_hosts is not None else defaults
    if not safe_hosts or any(not isinstance(item, str) or not item.strip() for item in safe_hosts):
        raise ValueError("at least one allowed Host header is required")
    issuer_host = "localhost" if host in {"127.0.0.1", "::1"} else host
    trusted_artifact_base_url = artifact_base_url
    if trusted_artifact_base_url is None:
        artifact_host = "localhost" if host in {"127.0.0.1", "::1"} else host
        authority = f"[{artifact_host}]" if ":" in artifact_host and not artifact_host.startswith("[") else artifact_host
        trusted_artifact_base_url = f"http://{authority}:{port}"
    # RuntimeRpcGateway performs strict HTTP(S)/authority validation.  Do it
    # once at server construction, before any tool can emit a URL.
    trusted_artifact_base_url = runtime._validate_artifact_base_url(trusted_artifact_base_url)  # noqa: SLF001
    bridge = RuntimeMcpBridge(runtime, artifact_base_url=trusted_artifact_base_url)
    server = FastMCP(
        "semarail",
        instructions=(
            "Use SemaRail context and planning before governed execution. "
            "Every operation is authorized against the current service-account or employee policy."
        ),
        host=host,
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=f"http://{issuer_host}:{port}",
            resource_server_url=None,
            required_scopes=[],
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=safe_hosts,
            allowed_origins=[],
        ),
        log_level="WARNING",
    )

    @server.custom_route(
        "/api/v1/artifacts/{artifact_id}/download",
        methods=["GET"],
        name="semarail_artifact_download",
        include_in_schema=False,
    )
    async def semarail_artifact_download(request: Request) -> StreamingResponse | JSONResponse:
        """Stream a Core-owned artifact; custom routes do not redirect."""

        artifact_id = request.path_params.get("artifact_id", "")
        token = request.query_params.get("token", "")
        try:
            download = await asyncio.to_thread(
                runtime.download_artifact,
                artifact_id,
                token,
                request.headers.get("authorization"),
                transport="remote-mcp",
            )
        except (ArtifactError, AccessControlError) as exc:
            return JSONResponse(
                {"code": exc.code, "message": exc.safe_message},
                status_code=exc.status,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        if not isinstance(download, ArtifactDownload):  # pragma: no cover - gateway contract guard
            return JSONResponse(
                {"code": "ARTIFACT_UNAVAILABLE", "message": "artifact is unavailable"},
                status_code=404,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )

        async def chunks() -> Any:
            try:
                with download.path.open("rb") as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk
            except OSError:
                return

        metadata = download.metadata
        return StreamingResponse(
            chunks(),
            media_type=metadata.content_type.split(";", 1)[0],
            headers={
                "Content-Disposition": f'attachment; filename="{metadata.filename}"',
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Content-Length": str(metadata.size or 0),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @server.tool(annotations=_readonly("Validate the SemaRail semantic project"))
    async def semarail_validate_project() -> dict[str, Any]:
        return await bridge.call("project.validate", {})

    @server.tool(annotations=_readonly("List SemaRail semantic models"))
    async def semarail_list_models() -> dict[str, Any]:
        return await bridge.call("project.describe", {})

    @server.tool(annotations=_readonly("Get SemaRail semantic context"))
    async def semarail_get_context(question: str) -> dict[str, Any]:
        return await bridge.call("context.ask", {"question": question})

    @server.tool(annotations=_readonly("Plan a SemaRail semantic query"))
    async def semarail_plan_query(semantic_sql: str) -> dict[str, Any]:
        return await bridge.call("query.dryPlan", {"semanticSql": semantic_sql})

    @server.tool(annotations=_readonly("Run a governed SemaRail query", idempotent=False))
    async def semarail_governed_query(
        question: str,
        semantic_sql: str,
        chart_intent: Literal["auto", "table", "line", "bar", "pie"] = "auto",
    ) -> dict[str, Any]:
        query_id = f"remote-mcp-query-{uuid.uuid4().hex}"
        try:
            return await bridge.call(
                "query.run",
                {
                    "question": question,
                    "semanticSql": semantic_sql,
                    "chartIntent": chart_intent,
                    "queryId": query_id,
                },
            )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(bridge.call("query.cancel", {"queryId": query_id}))
            except Exception:
                pass
            raise

    # MCP 1.28's compatibility mode otherwise ignores unknown arguments.
    # Reject them so a caller cannot believe it supplied a Subject, policy,
    # datasource, project path, or credential override that Core did not use.
    for tool in server._tool_manager.list_tools():  # noqa: SLF001 - pinned MCP 1.28 contract
        tool.parameters["additionalProperties"] = False
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve SemaRail over authenticated MCP Streamable HTTP")
    parser.add_argument("--project", required=True, help="fixed semantic project directory")
    parser.add_argument("--state-dir", help="shared SemaRail control-plane state directory")
    parser.add_argument("--host", default=DEFAULT_REMOTE_MCP_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_REMOTE_MCP_PORT)
    parser.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        help="allowed HTTP Host header, repeat for multiple public hostnames",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allowed_hosts:
        parser.error("non-loopback remote MCP requires at least one --allowed-host")
    try:
        server = create_remote_mcp_server(
            project=args.project,
            state_dir=args.state_dir,
            host=args.host,
            port=args.port,
            allowed_hosts=args.allowed_hosts,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    server.run(transport="streamable-http")
    return 0


__all__ = [
    "DEFAULT_REMOTE_MCP_HOST",
    "DEFAULT_REMOTE_MCP_PORT",
    "RuntimeMcpBridge",
    "SemaRailTokenVerifier",
    "create_remote_mcp_server",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
