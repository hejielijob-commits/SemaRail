# 0008 — Independent query diagnostics and explicit feedback

## Decision

Query diagnostics use separate versioned tables in the same configured
SQLite/PostgreSQL control-plane database as access control. They do not reuse
the append-only audit log. Core records failed context, planning, and execution
requests with bounded redacted question/SQL evidence; successful executions
retain ownership and trace metadata only. Authentication failures use a
metadata-only security-event table.

Explicit feedback is bound server-side to the authenticated subject,
organization, and current project. A caller-supplied reference can locate only
that caller's query or trace. Submission is idempotent per subject and returns
the original feedback identifier on retry. Diagnostic persistence failures are
best-effort for query execution but are explicit errors for feedback writes.

Diagnostic bodies expire after 30 days while classification metadata remains.
Reviewed regression cases are stored independently and exported as
schema-versioned JSON; incomplete cases remain drafts and cannot be enabled.

## Consequences

- Browser and MCP clients never receive database credentials or a diagnostic
  read capability merely because they can submit feedback.
- Project administrators can filter and manage only their organization and the
  server-pinned project.
- The MCP feedback tool advertises a write side effect and an idempotency key.
- Feedback never becomes SQL Knowledge or a business rule automatically.
