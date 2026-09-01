# 0004: Stable SemaRail semantic MCP contract

## Status

Accepted.

## Context

SemaRail needs an agent-neutral interface that can evolve independently of a
specific agent client and of changes to the upstream MCP tool surface. The
semantic implementation and project files already supplied by WrenAI 0.13.2 are
usable and should not be duplicated or converted into another model.

## Decision

SemaRail owns a stable, transport-independent MCP contract. Authenticated Core
Streamable HTTP is the default multi-user transport, and `semarail mcp bridge`
provides the same contract to stdio-only clients. The contract exposes four
read-only semantic tools in schema version 1:

1. `semarail_validate_project` validates the server-selected project.
2. `semarail_list_models` lists its models, relationships, and views.
3. `semarail_get_context` resolves bounded context for a question.
4. `semarail_plan_query` dry-plans semantic SQL without opening a datasource.

The project directory is fixed when the server starts and is not a tool
argument. `SemanticService` is a thin internal boundary: it invokes the pinned
WrenAI public runtime APIs and passes through the existing bounded project
structures. It is not a second semantic model, parser, or persistence format.

The authenticated endpoint and bridge also expose `semarail_governed_query`.
Every call enters Core, resolves a managed Subject and its current policy, and
keeps project paths and datasource credentials server-side.

The direct `semarail-mcp` and `semarail-query-mcp` commands remain trusted-local
operator compatibility tools. They do not implement per-user identity or policy
isolation and are not the multi-user contract.

WrenAI's native MCP server may remain in compatibility tests or advanced local
experiments, but its tool names and schemas are not SemaRail's public contract.

## Compatibility policy

- Existing version-one tool names and required inputs remain stable within the
  current major version.
- Additive result fields are allowed; removing or changing fields requires a
  schema-version change and migration notes.
- Updating WrenAI requires contract tests to pass before the dependency pin is
  changed.
- Errors crossing MCP remain JSON-safe, bounded, and free of project paths,
  SQL text, datasource credentials, and internal exception details.

## Consequences

- Any MCP-capable agent integrates with SemaRail rather than an upstream CLI
  command or an agent-specific adapter.
- SemaRail can replace or modify semantic internals later without changing MCP
  clients, while today's implementation continues to use WrenAI directly.
- The smaller surface is easier to secure and test, but does not expose all
  optional upstream resources, prompts, or execution tools.
