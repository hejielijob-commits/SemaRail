# SemaRail

> A governed semantic layer that helps AI agents understand business data and run safe, inspectable queries.

[![License: MIT](https://img.shields.io/badge/License-MIT-0f766e.svg)](LICENSE)
![Project status: Alpha](https://img.shields.io/badge/status-alpha-f59e0b)
![Node.js](https://img.shields.io/badge/Node.js-%5E22.19_%7C%7C_%3E%3D24-339933)
![Python](https://img.shields.io/badge/Python-%3E%3D3.11-3776ab)

SemaRail turns database schemas, business definitions, relationships, rules, and reviewed SQL into a semantic context that AI agents can use consistently. It provides a visual Semantic Console for managing that context, a stable MCP interface for agent integration, and a governed query boundary for read-only data access.

SemaRail is agent-neutral. Any MCP-capable client can use its semantic tools. A dedicated DeepSeek Harness plugin is also included for a richer conversation experience with native Chart, Table, and SQL views.

> **Status:** Alpha. APIs, configuration, and storage formats may change before the first stable release. The project is currently installed from source; npm and PyPI packages are not published yet.

![SemaRail Semantic Console overview](docs/images/semantic-console-overview.png)

## Features

- **Visual semantic modeling** — import database schemas and manage models, fields, relationships, views, cubes, business rules, and reviewed SQL knowledge.
- **Agent-neutral MCP tools** — expose semantic context and governed query planning to Codex and other MCP-capable agents through stdio.
- **Governed data access** — parse generated PostgreSQL with `sqlglot`, enforce physical-object allowlists, reject unsafe statements, and apply read-only, timeout, row, byte, and concurrency limits.
- **PostgreSQL and MySQL metadata** — test connections, browse schemas, and import models from the datasource types available in the installed runtime.
- **Versioned semantic projects** — validate drafts, inspect generated source and diffs, publish revisions, and roll back changes.
- **Bilingual metadata** — maintain English and Simplified Chinese display names without changing stable technical identifiers.
- **DeepSeek Harness integration** — install an optional Host/Client bundle that renders durable Chart, Table, and SQL results directly in conversations.

### Datasource management

Datasource credentials stay on the server and are redacted from API responses. The standard Console installation includes PostgreSQL and MySQL drivers for connection testing, schema browsing, and model import.

![PostgreSQL and MySQL datasource management](docs/images/datasources.png)

### Semantic model workbench

Edit business names, descriptions, visibility, primary keys, and field dictionaries while keeping generated semantic source and a unified diff nearby.

![Semantic model workbench](docs/images/semantic-model-workbench.png)

### Relationship graph

Explore and maintain field-level model relationships in an interactive graph.

![Semantic relationship graph](docs/images/relationship-graph.png)

## Tech stack

- Python 3.11+
- TypeScript and Node.js
- React 18 and Vite
- Model Context Protocol (MCP) Python SDK
- WrenAI Python SDK/Core 0.13.2
- `sqlglot` for structural SQL validation
- PostgreSQL for governed query execution
- PostgreSQL and MySQL drivers for Console metadata workflows
- Apache ECharts for conversation-native charts

## Quick start

### Install the DeepSeek Harness plugin

Requirements:

- DeepSeek Harness `>=0.1.0-rc.10 <0.2.0`
- Python `>=3.11` available as `python` or configured with `pythonExecutable`

Download the SemaRail `.tgz` from GitHub Releases, then install it into the Web profile:

```powershell
dsh plugin --profile web add C:\Downloads\hejielijob-dsh-wren-data-agent-0.1.0.tgz
```

The first start creates SemaRail's private Python environment automatically. npm and PyPI publication are not required for this installation method.

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

Create the Python environment and install the semantic runtime, MCP servers, Console, PostgreSQL query driver, and MySQL metadata driver:

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

SemaRail provides two separate stdio servers so deployments can expose semantic discovery without automatically granting database access.

### Semantic MCP server

The semantic server reads the project but does not connect to the database. It exposes:

- `semarail_validate_project`
- `semarail_list_models`
- `semarail_get_context`
- `semarail_plan_query`

Start it with:

```powershell
& .\.venv\Scripts\semarail-mcp.exe `
  --project C:\path\to\semantic-project
```

Register the command, project argument, and repository-sidecar working directory in your MCP client. For Codex, MCP servers can be added in its MCP settings or with `codex mcp add`.

### Governed query MCP server

The optional execution server adds `semarail_governed_query`. Give it a read-only PostgreSQL DSN through an operating-system environment variable or secret manager; never place the DSN in a prompt or MCP tool argument.

```powershell
$env:SEMARAIL_DATABASE_URL = "postgresql://readonly_user:password@localhost:5432/database"
& .\.venv\Scripts\semarail-query-mcp.exe `
  --project C:\path\to\semantic-project `
  --database-dsn-env SEMARAIL_DATABASE_URL
```

Project selection and the credential source are fixed when the server starts. Generated SQL is checked against the semantic project's physical allowlist before it can run.

Run the credential-free MCP acceptance test with:

```powershell
pnpm acceptance:mcp
```

## DeepSeek Harness plugin

SemaRail includes a dedicated DeepSeek Harness bundle for users who want the semantic layer embedded in the Harness conversation UI. This integration is optional; the Semantic Console and MCP servers do not require DeepSeek Harness.

The plugin provides:

- A Host plugin that manages semantic context, governed PostgreSQL execution, process lifecycle, and cancellation.
- A Client plugin that renders durable Chart, Table, and SQL views from `tool/result.meta`.
- A shortcut from Harness to the local Semantic Console.
- Compatibility with DeepSeek Harness `>=0.1.0-rc.10 <0.2.0`.

### Conversation chart

![Daily revenue chart rendered inside DeepSeek Harness](docs/images/deepseek-harness-chart.png)

### Inspectable SQL

![Semantic SQL inspection inside DeepSeek Harness](docs/images/deepseek-harness-semantic-sql.png)

### Install the Harness plugin from source

The distribution bundle is named `@hejielijob/dsh-wren-data-agent`. It is now a single self-contained Harness package: the Host, Client, shared contract, Python sources, and production Console assets are all included in one tarball. Because it is not published to npm yet, build that tarball locally:

```powershell
pnpm install
pnpm package:plugin
```

Install the generated package with the same one-command Harness flow used by registry plugins:

```powershell
dsh plugin --profile web add .\dist\hejielijob-dsh-wren-data-agent-0.1.0.tgz
```

You can also download the `.tgz` from a future GitHub Release and pass its local path or HTTPS URL to the same command. Once the package is published to npm, installation will reduce to:

```powershell
dsh plugin --profile web add @hejielijob/dsh-wren-data-agent
```

Verify the complete single-tarball installation in a temporary Harness profile with:

```powershell
pnpm acceptance
```

### Configure the Harness Host

Use an absolute semantic project directory and a system Python 3.11 or newer:

```yaml
- id: wren-data-agent-host
  config:
    pythonExecutable: C:\Python311\python.exe
    projectDir: D:\data\semantic-project
    databaseDsnEnv: SEMARAIL_DATABASE_URL
    # semanticConsoleEnabled: false
    # workingDirectory: D:\managed\sidecar
    # pythonBootstrapEnabled: false # only for a self-managed Python environment
```

On first startup, SemaRail uses that interpreter to create a private, versioned virtual environment and install its fixed direct Python dependencies. Sidecar and Console startup share an installation lock, and later starts reuse the completed environment. The first initialization requires network access and may take several minutes; subsequent startup is normally immediate. Set `SEMARAIL_RUNTIME_HOME` only if the private runtime must live outside the operating-system cache directory.

Set `pythonBootstrapEnabled: false` only when `pythonExecutable` already points to an environment where the packaged Sidecar and Console dependencies have been installed manually.

Set `SEMARAIL_DATABASE_URL` in the Host process environment to a read-only PostgreSQL account. Do not put the DSN itself in bundle configuration.

The Client opens the Semantic Console at `http://127.0.0.1:48763` by default. An embedding can pass `semanticConsoleUrl` to the exported view/link props or set `localStorage['dsh-wren-data-agent.semantic-console-url']`; only credential-free absolute HTTP(S) URLs are accepted.

## Security model

All model-generated SQL is treated as untrusted input.

- PostgreSQL statements are parsed structurally with `sqlglot`.
- DML, multi-statement SQL, dangerous functions, and unauthorized objects fail closed.
- Query execution uses a read-only account with row, byte, timeout, concurrency, and cancellation limits.
- Protocol and presentation payloads are JSON-safe and versioned; unknown versions fail closed.
- Sidecar stdout is protocol-only; diagnostics go to stderr.
- Datasource credentials remain server-side and are redacted from Console API responses.
- The Console is loopback-only and unauthenticated in this alpha release. Team authentication, RBAC, approvals, and audit logging remain deployment work.

## Repository layout

| Path | Purpose |
| --- | --- |
| `apps/semantic-console` | Local Python server and React Semantic Console. |
| `python/sidecar` | Semantic planning, SQL policy/execution, framed RPC, and MCP servers. |
| `packages/contract` | Shared Host, Client, and Sidecar contracts. |
| `packages/host` | DeepSeek Harness Host plugin and packaged Python runtimes. |
| `packages/client` | DeepSeek Harness Chart, Table, SQL, and Console views. |
| `packages/bundle` | Installable DeepSeek Harness `dsh.bundle` composition. |
| `examples/wren-postgres` | Deterministic sales project and golden-question corpus. |
| `scripts` | Packaging, acceptance, replay, and evaluation gates. |

## Development

```powershell
pnpm typecheck
pnpm test
pnpm build
pnpm acceptance:mcp
```

Additional integration gates:

```powershell
pnpm acceptance
& .\.venv\Scripts\python.exe scripts\acceptance-postgres.py --dry-run
pnpm acceptance:replay --dry-run
pnpm evaluate:golden --self-test
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Report security issues through the private process in [SECURITY.md](SECURITY.md), not through a public issue. User-visible changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## Current scope

- SemaRail's semantic MCP interface can use datasources supported by the configured semantic profile.
- Governed query execution through MCP or DeepSeek Harness is currently PostgreSQL-only.
- The Semantic Console supports PostgreSQL and MySQL connection testing, schema browsing, and model import.
- The current semantic runtime does not support View-to-View references; nested View dependencies are rejected before execution.
- Browser hard-refresh rendering remains a separate real-Client acceptance step beyond the API-only replay gate.

## Upstream foundation

SemaRail is based on and adapted from the [WrenAI](https://github.com/Canner/WrenAI) codebase and Python SDK/Core. It currently uses `wrenai==0.13.2` and its public context, validation, build, field-registry, and project-format APIs.

SemaRail is an independent project, not an official WrenAI distribution or Canner product, and is not endorsed by or affiliated with Canner. The SemaRail name and branding are independent of the upstream project.

## License

This repository is released under the [MIT License](LICENSE), copyright © 2026 `hejielijob-commits`.

Third-party components retain their own licenses:

- `wrenai==0.13.2` identifies itself as Apache-2.0 and is maintained by the [WrenAI project](https://github.com/Canner/WrenAI).
- The Client bundles Apache ECharts `5.6.0`; its Apache-2.0 `LICENSE` and `NOTICE` are shipped in `packages/client/licenses/echarts`.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the dependency and artifact attribution inventory.
