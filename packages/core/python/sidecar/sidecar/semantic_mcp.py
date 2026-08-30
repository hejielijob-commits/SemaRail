"""SemaRail's stable semantic MCP interface.

The MCP tool names and envelopes in this module belong to SemaRail. The thin
``SemanticService`` underneath continues to call the pinned WrenAI package and
uses its existing project structures directly; no upstream implementation is
copied here and no parallel semantic file format is introduced.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .errors import INTERNAL_ERROR, RpcError, RpcFault
from .semantic_service import SemanticService


class SemanticMcpService(Protocol):
    """Stable service behavior exposed by the semantic MCP transport."""

    def validate_project(self) -> dict[str, Any]: ...

    def list_models(self) -> dict[str, Any]: ...

    def get_context(self, question: str) -> dict[str, Any]: ...

    def plan_query(self, semantic_sql: str) -> dict[str, Any]: ...


_T = TypeVar("_T")


def _annotations(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _tool_error(error: RpcError) -> ToolError:
    return ToolError(
        json.dumps(
            error.normalized().as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


async def _invoke(
    operation: Callable[[], _T],
    *,
    phase: str,
    logger: logging.Logger,
) -> _T:
    try:
        # Wren Core embeds a native runtime whose project/build operations can
        # stall when first initialized from an arbitrary worker thread on
        # Windows. MCP already isolates this server in its own stdio process,
        # so invoke the bounded, database-free semantic operation on that
        # process's main event-loop thread.
        return operation()
    except RpcFault as fault:
        raise _tool_error(fault.error) from None
    except Exception:
        # Runtime exceptions can contain SQL, credentials, or absolute paths.
        # Keep diagnostics generic on both the MCP wire and default logs.
        logger.error("semantic MCP operation failed")
        raise _tool_error(
            RpcError(
                code=INTERNAL_ERROR,
                phase=phase,
                message="semantic operation failed",
                retryable=False,
            )
        ) from None


def create_semantic_mcp_server(
    *,
    project: str | Path,
    semantic_service: SemanticMcpService | None = None,
) -> FastMCP:
    """Create SemaRail's project-pinned, database-disconnected MCP server."""

    service = semantic_service or SemanticService(project)
    logger = logging.getLogger("sidecar.semantic_mcp")
    server = FastMCP(
        "semarail-semantic",
        instructions=(
            "Use semarail_get_context before composing semantic SQL, inspect "
            "the project with semarail_list_models when needed, and validate "
            "candidate SQL with semarail_plan_query. This server never opens "
            "a datasource connection."
        ),
        log_level="WARNING",
    )

    @server.tool(annotations=_annotations("Validate the SemaRail semantic project"))
    async def semarail_validate_project() -> dict[str, Any]:
        """Validate the server-selected semantic project and return safe counts."""

        return await _invoke(
            service.validate_project,
            phase="project.validate",
            logger=logger,
        )

    @server.tool(annotations=_annotations("List SemaRail semantic models"))
    async def semarail_list_models() -> dict[str, Any]:
        """List models, relationships, and views from the selected project."""

        return await _invoke(
            service.list_models,
            phase="project.describe",
            logger=logger,
        )

    @server.tool(annotations=_annotations("Get SemaRail semantic context"))
    async def semarail_get_context(question: str) -> dict[str, Any]:
        """Return bounded semantic context for the exact user question."""

        return await _invoke(
            lambda: service.get_context(question),
            phase="context.ask",
            logger=logger,
        )

    @server.tool(annotations=_annotations("Plan a SemaRail semantic query"))
    async def semarail_plan_query(semantic_sql: str) -> dict[str, Any]:
        """Dry-plan one read-only semantic SQL statement without executing it."""

        return await _invoke(
            lambda: service.plan_query(semantic_sql),
            phase="query.dryPlan",
            logger=logger,
        )

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve SemaRail's stable semantic tools over MCP stdio"
    )
    parser.add_argument(
        "--project",
        required=True,
        help="fixed semantic project directory",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        semantic_service = SemanticService(args.project)
        semantic_service.prepare()
        server = create_semantic_mcp_server(
            project=args.project,
            semantic_service=semantic_service,
        )
    except (OSError, RuntimeError, ValueError, RpcFault) as exc:
        parser.error(str(exc))
    server.run(transport="stdio")
    return 0


__all__ = [
    "SemanticMcpService",
    "create_semantic_mcp_server",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised by MCP clients
    raise SystemExit(main())
