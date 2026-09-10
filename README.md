# SemaRail

> A governed semantic layer that helps AI agents understand business data and run safe, inspectable queries.

[![License: MIT](https://img.shields.io/badge/License-MIT-0f766e.svg)](LICENSE)
![Project status: Alpha](https://img.shields.io/badge/status-alpha-f59e0b)
![Node.js](https://img.shields.io/badge/Node.js-%5E22.19_%7C%7C_%3E%3D24-339933)
![Python](https://img.shields.io/badge/Python-%3E%3D3.11-3776ab)

SemaRail turns database schemas, business definitions, relationships, rules, and reviewed SQL into a semantic context that AI agents can use consistently. It provides a visual Semantic Console for managing that context, a stable MCP interface for agent integration, and a governed query boundary for read-only data access.

SemaRail is agent-neutral. Any MCP-capable client can use its semantic tools through the authenticated HTTP endpoint or stdio bridge.

> **Status:** Alpha. APIs, configuration, and storage formats may change before the first stable release. The Core tarball can be built from source; npm and PyPI packages are not published yet.

![SemaRail Semantic Console overview](docs/images/semantic-console-overview.png)

## Features

- **Visual semantic modeling** — import database schemas and manage models, fields, relationships, views, cubes, business rules, and reviewed SQL knowledge.
- **Agent-neutral MCP tools** — expose semantic context and governed queries through authenticated Streamable HTTP, with an authenticated stdio bridge for clients that require it.
- **Governed data access** — resolve every request to a current Subject and policy, parse generated PostgreSQL with `sqlglot`, enforce table/column/row rules and physical-object allowlists, and apply read-only, timeout, row, byte, and concurrency limits.
- **Bounded Agent results** — return small query results inline, but turn larger results into a short-lived CSV download with only a 20-row preview in the Agent context.
- **Enterprise identity and policy** — use revocable service-account keys or DingTalk/OIDC employee sessions, change permissions without reinstalling Agents, and audit decisions without storing SQL, result rows, or secrets.
- **Common database metadata** — test connections, browse schemas, and import models from PostgreSQL, MySQL, SQLite, ClickHouse, and DuckDB.
- **Versioned semantic projects** — validate drafts, inspect generated source and diffs, publish revisions, and roll back changes.
- **Bilingual metadata** — maintain English and Simplified Chinese display names without changing stable technical identifiers.
- **Actionable query diagnostics** — carry a Core trace across policy, planning, and execution, preserve versioned detailed errors, and retain bounded redacted failure evidence for 30 days without storing result rows.
- **Explicit feedback and regression review** — submit caller-owned feedback from MCP or a Console link, classify it in the Console, and export only reviewed reproducible regression cases.
- **Business-condition clarification** — publish structured metric, time-range, grain, and business-definition confirmation rules; required conditions block execution until `semarail_prepare_query` returns ready.

### Datasource management

Datasource credentials stay on the server and are redacted from API responses. The standard Console installation includes PostgreSQL, MySQL, SQLite, ClickHouse, and DuckDB drivers for connection testing, schema browsing, and model import. Local SQLite and DuckDB files are opened read-only.

![Datasource management](docs/images/datasources.png)

### Semantic model workbench

Edit business names, descriptions, visibility, primary keys, and field dictionaries while keeping generated semantic source and a unified diff nearby.

![Semantic model workbench](docs/images/semantic-model-workbench.png)

### Relationship graph

Explore and maintain field-level model relationships in an interactive graph.

![Semantic relationship graph](docs/images/relationship-graph.png)

## Project roadmap

This roadmap highlights major project milestones. For file-level release notes,
see [CHANGELOG.md](CHANGELOG.md).

| Date | Status | Milestone |
| --- | --- | --- |
| 2026-08-30 | Completed | Established the SemaRail brand, Semantic Console, and stable semantic MCP contract. |
| 2026-08-31 | Completed | Added DingTalk and OIDC employee sign-in, revocable sessions, trusted subject attributes, and administrator-managed account access. |
| 2026-09-01 | Completed | Added project-, datasource-, table-, column-, and row-scoped authorization with immediate policy and credential revocation. |
| 2026-09-02 | Completed | Added multi-user authenticated MCP, PostgreSQL-backed access-control storage, transaction-local subject context, and PostgreSQL RLS isolation. |
| 2026-09-03 | Completed | Hardened permission-control acceptance with real PostgreSQL 17 tests, first-request MCP query startup, clean Linux CI builds, and A/B employee row-isolation verification. |
| 2026-09-04 | Completed | Added bounded query-result delivery: up to 50 rows and 128 KiB inline, otherwise a revocable 15-minute CSV artifact with a 20-row Agent preview and 16 MiB ceiling. |
| 2026-09-10 | Completed | Added versioned detailed query errors and traces, independent 30-day diagnostics, explicit feedback and reviewed regression cases, and Core-enforced query clarification rules. |
| Next | Planned | Extend governed query execution beyond PostgreSQL while preserving the same policy, limits, audit, and cancellation contract. |
| Next | Planned | Add a managed CSV/Excel ingestion workflow backed by DuckDB, without exposing uploaded files or local paths to Agents. |
| Later | Planned | Publish versioned SemaRail Core packages after the alpha installation and upgrade flow is stable. |

## Tech stack

- Python 3.11+
- TypeScript and Node.js
- React 18 and Vite
- Model Context Protocol (MCP) Python SDK
- `sqlglot` for structural SQL validation
- PostgreSQL for governed query execution
- PostgreSQL, MySQL, SQLite, ClickHouse, and DuckDB drivers for Console metadata workflows

## Quick start

### Install SemaRail Core

Requirements:

- Node.js `^22.19.0 || >=24`
- Python `>=3.11`

Until the package is published, build the local Core tarball from the repository:

```powershell
pnpm install
pnpm package:core
npm install --global .\dist\hejielijob-semarail-core-0.1.0-alpha.4.tgz
$env:SEMARAIL_API_TOKEN = semarail token create
semarail start --project C:\path\to\semantic-project
```

The Core process owns the semantic project, database credentials, execution limits, Semantic Console, and MCP servers. Open [http://127.0.0.1:48763](http://127.0.0.1:48763) after it starts. Keep `SEMARAIL_API_TOKEN` private: it is the local bootstrap-administrator credential used to create narrower, revocable service-account keys.

### Run from source

Requirements:

- Git
- Node.js `^22.19.0 || >=24`
- pnpm `11.x`
- Python `>=3.11`
- PostgreSQL only if you want to execute governed queries

```powershell
git clone https://github.com/hejielijob-commits/SemaRail.git
cd SemaRail
pnpm install
pnpm build
```

For a larger, reproducible local validation against 100,000 synthetic employees and five authorization roles, see the [HR enterprise benchmark](benchmarks/hr-enterprise/README.md). It runs separately from CI and requires Docker.

### HR enterprise benchmark: overview and results

The benchmark contains 60 fixed questions drawn from enterprise HR scenarios,
built on a reproducible dataset of 100,000 employees and 1.9 million PostgreSQL
rows. It includes 30 basic reporting questions, 15 cross-model analytical
questions, and 15 authorization-boundary questions in Chinese and English across
five enterprise roles. Together, they validate the MDL models, relationships,
metric rules, SQL knowledge, semantic planning, governed execution, and access
control. **All 60 questions passed.**

The checked-in semantic layer and evaluation evidence include:

- [Wren MDL project](benchmarks/hr-enterprise/project/wren_project.yml)
- [six semantic model definitions](benchmarks/hr-enterprise/project/models/)
- [model relationships](benchmarks/hr-enterprise/project/relationships.yml)
- [HR metric and authorization rules](benchmarks/hr-enterprise/project/knowledge/rules/hr-metrics.md)
- [SQL knowledge examples](benchmarks/hr-enterprise/project/knowledge/sql/)
- [PostgreSQL schema](benchmarks/hr-enterprise/sql/001_schema.sql)
- [60 evaluation cases](benchmarks/hr-enterprise/golden-questions.json)
- [evaluation process and results](benchmarks/hr-enterprise/EVALUATION_REPORT.md)
- [machine-readable result summary](benchmarks/hr-enterprise/results/evaluation-summary.json)

Create the Python environment and install the semantic runtime, MCP servers, Console, governed PostgreSQL query driver, and Console metadata drivers:

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install `
  -e ".\python\sidecar[wren,mcp]" `
  -e ".\apps\semantic-console[wren]"
```

### Start the Semantic Console

The repository includes a deterministic sales project for a local tour:

```powershell
$stateDir = Join-Path $env:LOCALAPPDATA "semarail\semantic-console\sales-demo"
& .\.venv\Scripts\python.exe -m server `
  --project-dir .\examples\wren-postgres `
  --state-dir $stateDir `
  --static-dir .\apps\semantic-console\web\dist
```

Open [http://127.0.0.1:48763](http://127.0.0.1:48763). The server binds to loopback by default.

## Use SemaRail with MCP agents

The default multi-user integration is SemaRail's authenticated Streamable HTTP
MCP endpoint. It exposes the same seven stable tools to any MCP-capable Agent:

- `semarail_validate_project`
- `semarail_list_models`
- `semarail_get_context`
- `semarail_plan_query`
- `semarail_prepare_query`
- `semarail_governed_query`
- `semarail_submit_feedback`

![SemaRail MCP integration](docs/images/mcp-integration.png)

### Start authenticated MCP

Start the MCP endpoint against the same project and state directory as Core:

```powershell
$env:SEMARAIL_API_TOKEN = "<local bootstrap token>"
semarail mcp serve `
  --project C:\path\to\semantic-project `
  --state-dir C:\path\to\semarail-state
```

The endpoint is `http://127.0.0.1:48764/mcp`. The bootstrap token initializes
the shared control-plane store but is rejected by remote MCP. In **Access
control**, create a service account, assign trusted attributes, bind a
project/datasource/table/column/row policy, and issue a one-time key. Configure
that managed key in the Agent's private environment or secret manager:

```json
{
  "mcpServers": {
    "semarail": {
      "url": "http://127.0.0.1:48764/mcp",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer ${SEMARAIL_TOKEN}"
      }
    }
  }
}
```

Every call re-authenticates the key or employee session and reads current policy,
so disabling an account, revoking a key, changing attributes, or unbinding a
policy affects the next request. Datasource credentials and project paths remain
inside Core and never enter MCP client configuration. Loopback is the safe
default; a non-loopback bind requires an explicit `--allowed-host` and a TLS
reverse proxy.

Governed query results use a bounded delivery contract. Results of at most 50
rows whose UTF-8 JSON representation is at most 128 KiB stay inline. Larger
results return at most 20 preview rows plus a temporary CSV download URL; the
full CSV is never inserted into the model context. Downloads expire after 15
minutes and are invalidated immediately when the issuing credential, subject,
datasource, or policy context is no longer current. The alpha query ceiling
remains 500 rows and the CSV ceiling is 16 MiB; this is not a bulk-export API.
Administrators may set `SEMARAIL_ARTIFACT_TTL_SECONDS` to a value from 60 to
86400 seconds; the default is 900 seconds and MCP callers cannot override it.

After generating candidate semantic SQL, an Agent calls `semarail_prepare_query`
with extracted business conditions. Core applies the rules published for the
referenced models and returns `ready`, `needs_clarification`, or `blocked`.
Required or explicitly confirmable conditions cannot be bypassed by calling the
query endpoint directly, including through an older client. Open-ended language
interpretation and answer extraction remain Agent responsibilities; Core validates
the structured conditions and one-query preparation record.

`semarail_submit_feedback` is explicitly declared as a write operation. It accepts
only a query or trace owned by the current caller and supports idempotent retries.
Failures are captured automatically with bounded redacted question/SQL evidence;
successful executions retain metadata only unless the user submits feedback. No
result rows or full Agent conversation are stored. Administrators can classify
issues, advance their workflow, link duplicates, create reviewed regression cases,
and export versioned JSON from the Console's **Issues & feedback** and
**Regression cases** pages.

### Service accounts, employees, and row permissions (alpha)

SemaRail Core includes a local management API for service accounts and externally authenticated employees, one-time API-key issuance, key rotation/revocation, short-lived employee sessions, versioned policy bindings, and audit events. Policies can restrict tool scopes, projects, physical tables, columns, query limits, and rows derived from trusted subject attributes. Mandatory row predicates are injected with bound database parameters before execution; missing or malformed permissions fail closed.

For example, two agents can run the same sales query while account A is restricted to region `CN-JIA` and account B to `CN-YI`. Updating the account attributes or policy is effective on the next request. See [Access control (alpha)](docs/access-control.md) and [the architecture decision](docs/decisions/0005-enterprise-identity-and-data-authorization.md).

Employees can sign in through a configured DingTalk or generic OIDC provider with `semarail auth login --provider <id>`. The browser callback never receives a SemaRail bearer token; the initiating CLI exchanges a one-time device code for a bounded session and then enters the same Subject/PolicyEngine path as an API key. New employees have no data policy until an administrator assigns trusted attributes and a policy in **Access control**. See [Access control (alpha)](docs/access-control.md) for provider configuration and security boundaries.

### Authenticated stdio bridge

For an Agent that only supports stdio, log in once and configure the bridge as
its MCP command:

```powershell
semarail auth login --provider dingtalk --endpoint http://127.0.0.1:48763
semarail mcp bridge --endpoint http://127.0.0.1:48763
```

The bridge reads the ACL-protected employee session written by `semarail auth
login` and forwards every tool to Core's authenticated runtime. It does not load
Wren, open a database, accept a Subject/policy/DSN argument, or print the token.
For a service account instead, set `SEMARAIL_MCP_TOKEN` in the bridge process's
private environment. Use `--token-env <NAME>` to select another environment
variable name.

### Trusted local operator compatibility

`semarail-mcp` and `semarail-query-mcp` remain available for compatibility and
isolated local evaluation. They directly load the project/Sidecar and therefore
do **not** provide per-user Subject resolution, immediate policy changes, or
identity audit. Do not use them as a shared employee or multi-tenant boundary.
Use authenticated HTTP MCP or `semarail mcp bridge` instead.

Run MCP acceptance tests with:

```powershell
pnpm acceptance:mcp
pnpm acceptance:core
```

## Security model

All model-generated SQL is treated as untrusted input.

- PostgreSQL statements are parsed structurally with `sqlglot`.
- DML, multi-statement SQL, dangerous functions, and unauthorized objects fail closed.
- Query execution uses a read-only account with row, byte, timeout, concurrency, and cancellation limits.
- PostgreSQL deployments can add transaction-local Subject context and native RLS as a second enforcement layer; see [PostgreSQL row-level security](docs/postgresql-rls.md).
- Protocol and presentation payloads are JSON-safe and versioned; unknown versions fail closed.
- Sidecar stdout is protocol-only; diagnostics go to stderr.
- Datasource credentials remain server-side and are redacted from Console API responses.
- The Console remains loopback-only in this alpha release. Subject policies, table/column/row authorization, PostgreSQL RLS context, optional PostgreSQL control-plane storage, and metadata-only audit events are implemented. Internet exposure still requires a hardened reverse proxy, TLS, deployment monitoring, backup/restore, and an organization-specific identity configuration.

## Repository layout

| Path | Purpose |
| --- | --- |
| `apps/semantic-console` | Local Python server and React Semantic Console. |
| `python/sidecar` | Semantic planning, SQL policy/execution, framed RPC, and MCP servers. |
| `packages/contract` | Shared versioned Core, MCP, and Console contracts. |
| `packages/core` | Independently installable SemaRail Core CLI/runtime distribution. |
| `examples/wren-postgres` | Deterministic sales project and golden-question corpus. |
| `scripts` | Packaging, acceptance, and evaluation gates. |

## Development

```powershell
pnpm typecheck
pnpm test
pnpm build
pnpm acceptance:core
pnpm acceptance:mcp
```

Additional integration gates:

```powershell
# Run the real PostgreSQL 17/RLS gate. It requires administrator settings and
# creates, then cleans, isolated test database/role fixtures.
pnpm acceptance:postgres
# Run the PostgreSQL control-store, diagnostics, feedback, regression, and
# clarification gate. The administrator URL is read only from this environment
# variable; the script creates and removes one isolated database.
$env:SEMARAIL_ACCEPTANCE_ADMIN_DATABASE_URL = "<PostgreSQL administrator URL>"
pnpm acceptance:control-postgres
# Preview the PostgreSQL acceptance prerequisites without changing the database.
& .\.venv\Scripts\python.exe scripts\acceptance-postgres.py --dry-run
pnpm evaluate:golden --self-test
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Report security issues through the private process in [SECURITY.md](SECURITY.md), not through a public issue. User-visible changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## Current scope

- SemaRail's semantic MCP interface can use datasources supported by the configured semantic profile.
- Governed query execution through MCP is currently PostgreSQL-only.
- The Semantic Console supports PostgreSQL, MySQL, SQLite, ClickHouse, and DuckDB connection testing, schema browsing, and model import.
- The current semantic runtime does not support View-to-View references; nested View dependencies are rejected before execution.

## Upstream foundation

SemaRail is based on and adapted from the [WrenAI](https://github.com/Canner/WrenAI) codebase and Python SDK/Core. It currently uses `wrenai==0.13.2` and its public context, validation, build, field-registry, and project-format APIs.

SemaRail is an independent project, not an official WrenAI distribution or Canner product, and is not endorsed by or affiliated with Canner. The SemaRail name and branding are independent of the upstream project.

## License

This repository is released under the [MIT License](LICENSE), copyright © 2026 `hejielijob-commits`.

Third-party components retain their own licenses:

- `wrenai==0.13.2` identifies itself as Apache-2.0 and is maintained by the [WrenAI project](https://github.com/Canner/WrenAI).
- The Semantic Console bundles its browser dependencies; their license files
  are staged under `semantic-console-web/licenses` in the Core artifact.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the dependency and artifact attribution inventory.
