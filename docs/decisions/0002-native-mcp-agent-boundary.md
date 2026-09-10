# 0002: Native MCP agent boundary

## Status

Superseded by [0004](0004-stable-semarail-semantic-mcp.md).

> Historical interface note: new integrations use SemaRail's authenticated
> Streamable HTTP MCP endpoint or `semarail mcp bridge`. The direct commands and
> upstream interface described below are trusted-local history and must not be
> copied into shared client settings.

## Context

WrenAI 0.13.2 provides a native Model Context Protocol (MCP) server over stdio
or Streamable HTTP. Reimplementing its schema, context, planning, knowledge,
resource, and prompt tools for each agent would create incompatible agent APIs.

## Decision

Wren's native MCP server is the standard agent-neutral interface to the semantic
layer. Any MCP-capable agent can use it without installing a client-specific
adapter.

SemaRail will not duplicate the complete upstream MCP tool surface. The Semantic Console
remains a separate local management plane; datasource credentials, project
publication, and rollback are not exposed through the default MCP server.

Native MCP query tools do not inherit SemaRail's PostgreSQL AST allowlist,
byte/time/concurrency limits, or cancellation policy. Governed deployments
therefore run native MCP with `--no-connect` and compose it with the thin SemaRail
execution adapter defined in
[`0003-governed-query-mcp.md`](0003-governed-query-mcp.md). Streamable HTTP must
remain loopback-only until authentication and an explicit deployment policy are
added.

## Consequences

- Native MCP behavior is exercised in CI with an isolated DuckDB project and
  the official Python MCP client.
- At the time of this decision, other agents used the upstream MCP server. This
  is no longer the supported shared deployment boundary.
- Core provides governed PostgreSQL execution through its authenticated MCP
  boundary.
- MySQL support in the Semantic Console remains metadata, connection testing,
  schema browsing, and model import; it is not yet SemaRail governed query execution.
- The governed query service is shared by Core's MCP transports without
  replacing Wren's native semantic tools.
