# 0002: Native MCP agent boundary

## Status

Accepted.

## Context

The first DSH Data Agent experience was packaged for DeepSeek Harness. WrenAI
0.13.2 also provides a native Model Context Protocol (MCP) server over stdio or
Streamable HTTP. Reimplementing its schema, context, planning, knowledge,
resource, and prompt tools in the Harness plugin would create two incompatible
agent APIs.

## Decision

Wren's native MCP server is the standard agent-neutral interface to the semantic
layer. Any MCP-capable agent can use it without installing DeepSeek Harness.
The DeepSeek Harness bundle remains an optional enhanced adapter for durable
Chart/Table/SQL conversation views and the stricter DSH query boundary.

DSH will not duplicate Wren's complete MCP tool surface. The Semantic Console
remains a separate local management plane; datasource credentials, project
publication, and rollback are not exposed through the default MCP server.

The native MCP query tools and the DSH Harness query sidecar do not currently
share one enforcement path. Native MCP is read-only by default because its
write tool requires an explicit `--allow-write`, but it does not inherit DSH's
PostgreSQL AST allowlist, byte/time/concurrency limits, or cancellation policy.
Streamable HTTP must therefore remain loopback-only until authentication and an
explicit deployment policy are added.

## Consequences

- Native MCP behavior is exercised in CI with an isolated DuckDB project and
  the official Python MCP client.
- Other agents use `wren serve mcp`; they do not depend on Harness packages.
- Harness continues to provide the current governed PostgreSQL execution and
  conversation-native presentation path.
- MySQL support in the Semantic Console remains metadata, connection testing,
  schema browsing, and model import; it is not yet DSH governed query execution.
- A future host-neutral governed-query gateway can be shared by Harness and a
  thin DSH MCP adapter without replacing Wren's native semantic tools.
