# 0005: Unified identities and data authorization

## Status

Accepted. The local service-account foundation is implemented; remote MCP,
interactive employee sign-in, and database-native defense in depth are staged
follow-up work.

## Context

SemaRail must serve both unattended agents and employees. An agent installation
needs a revocable credential, while an employee may authenticate through an
enterprise identity provider such as DingTalk. Both paths must produce the same
table, column, row, project, and tool decisions. Permissions must be adjustable
without reinstalling an agent or issuing a new long-lived token.

The analytical datasource is not an identity database. Credentials, policy
bindings, and audit events therefore need a separate control-plane store.
Datasource credentials must remain server-side and policy values must never be
accepted from an MCP tool argument.

## Decision

### Unified subject

Every authenticated actor becomes a versioned SemaRail `Subject`:

- `service_account` subjects authenticate with hashed, revocable API keys;
- `user` subjects will authenticate through an external OIDC/OAuth identity and
  retain the provider employee identifier as a trusted external identity;
- both carry server-managed attributes, such as organization and region codes;
- policies bind to subjects and are evaluated on every request.

The existing `SEMARAIL_API_TOKEN` remains a local bootstrap-administrator
credential during migration. It is not embedded in policy documents and is not
the future employee-login mechanism.

### Policy and enforcement

Policy schema version 1 grants stable tool scopes and explicit project/table
resources. Table rules can allow or deny columns and derive row predicates only
from trusted subject fields. Unknown fields, missing subject attributes,
unsupported operations, and unlisted tables fail closed. Explicit denies win.

For governed queries, SemaRail parses the planned SQL, verifies the physical
object allowlist, wraps every protected physical table with an inner filtered
subquery, and passes row values as database parameters. The mandatory inner
predicate cannot be removed by an outer `OR`, aggregation, alias, or join.
Resolved identity values and rewritten enforcement SQL are not returned to the
agent.

PostgreSQL Row Level Security is planned as a second enforcement layer for
production deployments. The common SemaRail policy engine remains authoritative
across PostgreSQL, MySQL, ClickHouse, SQLite, and DuckDB adapters; native database
controls are adapter-specific defense in depth.

### Control plane and audit

The local distribution stores identities, credential hashes, policy versions,
bindings, and append-only audit events in a private SQLite database outside the
semantic project. Plaintext API keys are returned once and never persisted.
Credential revocation, rotation, subject disabling, subject-attribute changes,
and policy version updates take effect on the next request.

A production multi-user deployment will move this metadata behind a transactional
control-plane service and durable database. The schema and API contract, rather
than the local SQLite file, are the compatibility boundary.

### Transport rollout

The existing stdio MCP servers remain suitable for one trusted local user. The
multi-user path will add remote HTTP MCP backed by the same authenticated Core
runtime. An optional local stdio bridge may connect clients that cannot speak
remote MCP. DingTalk authorization will create a short-lived user session and
then enter the same Subject/PolicyEngine path as an API key.

## Security invariants

- A client cannot choose its subject, trusted attributes, policy, project path,
  datasource credential, or server limits.
- API-key plaintext and datasource secrets never enter logs, project files,
  audit details, MCP results, or query results.
- Policy values are bound parameters, never SQL string interpolation.
- Every physical table is authorized; unsupported SQL and ambiguous policy data
  fail closed.
- Authentication, authorization decisions, credential lifecycle operations, and
  policy changes are auditable without recording query secrets or result rows.

## Consequences

- Local API-key service accounts can be introduced without waiting for an
  enterprise identity provider.
- DingTalk and future OIDC providers do not require a second authorization model.
- Immediate permission changes require policy lookup or bounded cache
  invalidation on every request.
- SQL rewriting provides one portable enforcement model, but production
  PostgreSQL deployments should still enable native RLS for stronger isolation.
