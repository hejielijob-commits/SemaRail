"""Thin SemaRail MCP adapter over the existing governed query service.

Wren's native MCP server remains the semantic discovery/planning interface.
This server adds one governed execution tool that reuses the exact service
and PostgreSQL policy boundary used by the DeepSeek Harness sidecar.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .errors import INTERNAL_ERROR, RpcError, RpcFault


DEFAULT_DATABASE_DSN_ENV = "SEMARAIL_DATABASE_URL"
DEFAULT_CANCELLATION_GRACE_SECONDS = 2.0
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")


class GovernedQueryService(Protocol):
    """Host-neutral query service shared with the framed Harness sidecar."""

    def run(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Plan and execute one bounded query."""

    def cancel(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Cancel one active query by its server-generated id."""


def _tool_error(error: RpcError) -> ToolError:
    payload = error.normalized().as_dict()
    return ToolError(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _consume_worker_exception(worker: asyncio.Task[dict[str, Any]]) -> None:
    """Retrieve late worker failures after an MCP cancellation grace timeout."""

    try:
        worker.exception()
    except BaseException:
        pass


def _default_query_service() -> GovernedQueryService:
    # Imported lazily so the ordinary framed sidecar still starts without the
    # optional MCP SDK installed.
    from .wren_adapter import default_dependencies

    service = default_dependencies().query_service
    if service is None:  # pragma: no cover - construction invariant
        raise RuntimeError("governed query service is unavailable")
    return service


def create_governed_mcp_server(
    *,
    project: str | Path,
    database_dsn_env: str = DEFAULT_DATABASE_DSN_ENV,
    query_service: GovernedQueryService | None = None,
    cancellation_grace_seconds: float = DEFAULT_CANCELLATION_GRACE_SECONDS,
) -> FastMCP:
    """Create a stdio MCP server pinned to one project and connection policy."""

    project_path = Path(project).expanduser().resolve()
    if not project_path.is_dir():
        raise ValueError("project directory is unavailable")
    if not isinstance(database_dsn_env, str) or not _ENV_NAME.fullmatch(database_dsn_env):
        raise ValueError("database DSN environment variable name is invalid")
    if (
        not isinstance(cancellation_grace_seconds, (int, float))
        or isinstance(cancellation_grace_seconds, bool)
        or not 0 <= float(cancellation_grace_seconds) <= 30
    ):
        raise ValueError("cancellation grace must be between zero and 30 seconds")

    service = query_service or _default_query_service()
    logger = logging.getLogger("sidecar.mcp_gateway")
    server = FastMCP(
        "semarail-query",
        instructions=(
            "Use SemaRail's semantic MCP tools for context and planning, then "
            "use semarail_governed_query when PostgreSQL policy and execution limits "
            "are required."
        ),
        log_level="WARNING",
    )

    @server.tool(
        annotations=ToolAnnotations(
            title="Run a SemaRail governed semantic query",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    async def semarail_governed_query(
        question: str,
        semantic_sql: str,
        chart_intent: Literal["auto", "table", "line", "bar", "pie"] = "auto",
        timeout_ms: int = 30_000,
        max_rows: int = 500,
        preview_rows: int = 200,
        max_preview_bytes: int = 1_048_576,
    ) -> dict[str, Any]:
        """Execute one semantic SQL query through SemaRail's governed PostgreSQL path.

        The project directory and datasource credential location are fixed by
        the server operator and cannot be selected by the agent. Semantic SQL
        is planned through Wren, checked against the MDL-derived physical
        allowlist, and executed read-only with hard time, row, byte, and
        concurrency limits. The result uses SemaRail presentation schema version 1.
        """

        query_id = f"semarail-mcp-{uuid.uuid4().hex}"
        params = {
            "projectDir": str(project_path),
            "question": question,
            "semanticSql": semantic_sql,
            "queryId": query_id,
            "chartIntent": chart_intent,
            "timeoutMs": timeout_ms,
            "maxRows": max_rows,
            "previewRows": preview_rows,
            "maxPreviewBytes": max_preview_bytes,
            "databaseDsnEnv": database_dsn_env,
        }
        worker = asyncio.create_task(asyncio.to_thread(service.run, params))
        worker.add_done_callback(_consume_worker_exception)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            try:
                service.cancel({"queryId": query_id})
            except Exception:
                logger.error("governed MCP cancellation failed")
            try:
                await asyncio.wait_for(
                    asyncio.shield(worker),
                    timeout=float(cancellation_grace_seconds),
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                # The executor's own watchdog and connection close remain the
                # final hard wall. Never put driver details in MCP diagnostics.
                pass
            raise
        except RpcFault as fault:
            raise _tool_error(fault.error) from None
        except Exception:
            logger.error("governed MCP query failed")
            raise _tool_error(
                RpcError(
                    code=INTERNAL_ERROR,
                    phase="query.run",
                    message="query execution failed",
                    retryable=False,
                )
            ) from None

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the SemaRail governed PostgreSQL query tool over MCP stdio"
    )
    parser.add_argument("--project", required=True, help="fixed semantic project directory")
    parser.add_argument(
        "--database-dsn-env",
        default=DEFAULT_DATABASE_DSN_ENV,
        help="server-side environment variable containing the PostgreSQL DSN",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        server = create_governed_mcp_server(
            project=args.project,
            database_dsn_env=args.database_dsn_env,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    server.run(transport="stdio")
    return 0


__all__ = [
    "DEFAULT_DATABASE_DSN_ENV",
    "GovernedQueryService",
    "create_governed_mcp_server",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised by MCP clients
    raise SystemExit(main())
