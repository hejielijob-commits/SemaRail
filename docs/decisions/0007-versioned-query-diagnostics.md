# 0007: Versioned query diagnostics

## Status

Accepted for the staged problem-feedback implementation.

## Context

The original RPC error has four fields: `code`, `phase`, `message`, and `retryable`. Its validators reject unknown fields, so adding permission objects or a trace identifier to version 1 would break existing Sidecar clients and durable result metadata. Generic `POLICY_DENIED` output also cannot tell an operator whether SemaRail policy or the database account rejected a request.

## Decision

Public Core RPC version 2 adds `reasonCode`, request-relevant `resources`, `requiredPermissions`, `suggestion`, `origin`, and a Core-generated `traceId`. Core accepts version 1 during migration and returns its original four-field failure representation. Unknown protocol versions remain rejected.

Core creates one trace identifier before authentication and carries it over the version 2 Core-to-Sidecar request. Sidecar version 2 returns details created at the decision point. Core validates these bounded fields before exposing them and always owns the public trace identifier.

Detailed failures use a versioned, JSON-safe error payload. Existing version 2 inline and CSV artifact success representations do not change. Every transport receives the same safe reason, objects, permissions, origin, suggestion, and trace identifier.

SQL table and column policy failures carry only objects referenced by the rejected query. A denied column uses its resolved policy table plus column name. An unqualified column across multiple restricted sources is not assigned to an invented table; enforcement remains fail-closed and reports only details that the decision point can establish.

## Consequences

The error protocol can evolve without weakening strict unknown-field rejection. HTTP and MCP paths can render actionable failures. Diagnostic persistence, feedback submission, clarification preparation, and Console workflows build on the trace identifier but are separate staged changes.
