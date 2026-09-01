# 0003: Governed query MCP composition

## Status

Superseded as a deployment recommendation by
[0004](0004-stable-semarail-semantic-mcp.md) and
[0005](0005-enterprise-identity-and-data-authorization.md). The structural
query boundary remains accepted, but shared Agent deployments use authenticated
Core HTTP MCP or its authenticated stdio bridge.

> Historical interface note: the names and two-process deployment below
> describe the superseded trusted-local implementation. `semarail-query-mcp`
> remains available for isolated local evaluation; it does not resolve a managed
> Subject or enforce current per-user policy. Shared deployments must not use it
> as an employee or multi-tenant authorization boundary.

## Context

Wren's native MCP server provides the broad semantic tool surface, while the
DSH Harness sidecar already enforces stricter PostgreSQL AST, object, timeout,
row, byte, concurrency, and cancellation policies. Letting agents execute with
the native `run_sql` tool would bypass those DSH controls; duplicating all Wren
tools inside DSH would create a second semantic API.

## Decision

A governed deployment composes two stdio MCP servers:

1. `wren serve mcp --no-connect` provides context, schema, knowledge, resources,
   prompts, and semantic SQL planning without database execution tools.
2. `dsh-data-agent-mcp` exposes only `dsh_governed_query` and delegates to
   the same `WrenQueryService` and `EnvPsycopgExecutor` used by the Harness
   framed sidecar.

The DSH server fixes the Wren project directory and DSN environment-variable
name at process startup. Neither is accepted as a tool argument. Each request
gets a server-generated query id, and MCP task cancellation invokes the shared
query service's database cancellation path.

Only stdio transport is supported for the DSH adapter in this phase. This keeps
credentials process-local and avoids presenting an unauthenticated network
service as production-ready.

## Consequences

- Harness RPC and presentation contracts remain unchanged.
- MCP clients receive the same versioned result, policy errors, and hard limits
  as Harness queries.
- The upstream MCP and direct SemaRail adapters remain available for isolated
  trusted-local evaluation. Shared governed deployments now route all tools
  through authenticated Core HTTP MCP or `semarail mcp bridge`.
- The DSH execution adapter remains PostgreSQL-only. MySQL support in the
  Semantic Console does not imply governed MySQL execution.
- Real PostgreSQL acceptance now exercises both the framed sidecar and the
  stdio MCP adapter against the same temporary read-only role.
