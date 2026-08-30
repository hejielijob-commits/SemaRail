# 0002: Native MCP agent boundary

## Status

Superseded by [0004](0004-stable-semarail-semantic-mcp.md).

> Historical interface note: new integrations use `semarail-mcp` and
> `semarail-query-mcp`. Command names in the decision record below describe the
> superseded implementation and must not be copied into new client settings.

## Context

The first SemaRail experience was packaged for DeepSeek Harness. WrenAI
0.13.2 also provides a native Model Context Protocol (MCP) server over stdio or
Streamable HTTP. Reimplementing its schema, context, planning, knowledge,
resource, and prompt tools in the Harness plugin would create two incompatible
agent APIs.

## Decision

Wren's native MCP server is the standard agent-neutral interface to the semantic
layer. Any MCP-capable agent can use it without installing DeepSeek Harness.
The DeepSeek Harness bundle remains an optional enhanced adapter for durable
Chart/Table/SQL conversation views and the stricter SemaRail query boundary.

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
- Other agents use `wren serve mcp`; they do not depend on Harness packages.
- Harness continues to provide the current governed PostgreSQL execution and
  conversation-native presentation path.
- MySQL support in the Semantic Console remains metadata, connection testing,
  schema browsing, and model import; it is not yet SemaRail governed query execution.
- The host-neutral governed query service is shared by Harness and the thin SemaRail
  MCP adapter without replacing Wren's native semantic tools.
