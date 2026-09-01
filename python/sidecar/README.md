# SemaRail sidecar

This package is the Python process boundary used by SemaRail Core. It speaks a
deliberately small RPC protocol so Core and optional agent adapters do not need
to import the semantic runtime into their own processes. `sqlglot` is the
PostgreSQL AST policy boundary; the package remains independent of any agent
client runtime.

## Wire protocol

Each message is a UTF-8 JSON object preceded by a four-byte unsigned,
big-endian payload length. The length covers only the JSON bytes. No logging is
written to stdout; diagnostics are written to stderr by the executable.

Requests use protocol version `"1"`:

```json
{"protocolVersion":"1","id":"request-1","method":"health","params":{}}
```

Responses always carry protocol version `"1"` and either `ok: true` with a
`result`, or `ok: false` with this stable error shape:

```json
{"protocolVersion":"1","id":"request-1","ok":false,"error":{
  "code":"INVALID_PARAMS",
  "phase":"validation",
  "message":"params must be an object",
  "retryable":false
}}
```

The initial methods are:

- `health` — checks that the sidecar process is alive and reports
  `wrenAvailable` / `wrenVersion`. A missing Wren installation is reported as
  a degraded dependency while process health remains available.
- `project.validate` — requires `params.projectDir`, then lazily calls Wren
  0.13.2's `wren.context.validate_project` and `build_json` APIs. It returns
  only `valid`, `errorCount`, `warningCount`, and a deterministic
  `sha256:<digest>` `projectRevision`; Wren issue paths/messages and project
  paths never cross the RPC boundary.
- `context.ask` — requires `projectDir` and `question`, builds the Wren
  manifest, loads Wren's public `load_rules` knowledge/rules content plus the
  context/description capability when available, applies bounded UTF-8
  redaction, and returns a `schemaVersion: 1` semantic context.
- `query.dryPlan` — requires `projectDir` and `semanticSql`, rejects anything
  other than one read-only AST before Wren is called, then builds the manifest
  and calls `WrenEngine(..., connection_info={}, config=WrenConfig(strict_mode=True,
  denied_functions=...)).dry_plan()` without opening a database connection.
  The result includes an MDL-derived `allowedPhysical` schema/table allowlist.
- `query.run` — requires `projectDir`, `question`, `semanticSql`, and
  `queryId`. Optional `chartIntent`, `timeoutMs`, `maxRows` (at most 500),
  `previewRows` (at most 200), `maxPreviewBytes` (at most 1 MiB), and
  `databaseDsnEnv` are accepted. The sidecar dry-plans through Wren, then
  applies a second PostgreSQL AST check for one read-only statement, allowed
  functions, and MDL-derived physical tables. The default DSN environment
  variable is `SEMARAIL_DATABASE_URL`; the DSN is resolved inside the sidecar and
  is never sent in an RPC request. A hard 30-second timeout and hard two-query
  concurrency limit apply. Native SQL is wrapped in a server-side `LIMIT
  maxRows+1` before execution, and results are shaped as DataQueryPresentation
  schema version 1 with exact numeric/date scalar strings.
- `query.cancel` — requires `queryId`. The server handles this method while a
  `query.run` worker is waiting on PostgreSQL and calls the driver's cancel
  hook. Unknown query ids are harmless and return `cancelled: false`.

Request envelopes fail closed like the TypeScript contract: only
`protocolVersion`, `id`, `method`, `params`, and optional `deadlineMs` are
accepted. `params` must be explicitly present and JSON-safe; method handlers
then enforce their object shape. `deadlineMs` is a non-negative integer and a
JSON boolean is not accepted as an integer.

Malformed transport frames cannot carry a recoverable request id. Their
framed diagnostic uses `id: ""` to mark an uncorrelated transport fault; it is
handled before the ordinary correlated `RpcResponse` parser.

The validator and query service are injected through `SidecarDependencies`,
and the CLI default uses `LazyWrenAdapter` plus a lazy psycopg executor.
Protocol and adapter tests can inject fake Wren/database implementations, so
they do not need Wren or a database installed.

## Trusted-local MCP adapters

The optional direct MCP entry points reuse `WrenQueryService` and
`EnvPsycopgExecutor` instead of reimplementing structural SQL policy:

```text
semarail-mcp --project C:\data\semantic-project
semarail-query-mcp --project C:\data\semantic-project
```

These direct processes are for one trusted local operator. They do not resolve a
managed Subject, retrieve current per-user policies, or produce identity audit.
They must not be used as a shared employee or multi-tenant access boundary. The
default deployment is authenticated Core HTTP MCP; stdio-only clients should use
`semarail mcp bridge`, which forwards all tools to Core.

The direct governed-query server exposes only `semarail_governed_query` over stdio. The project directory and DSN
environment-variable name are fixed at process startup and never appear as tool
arguments. MCP request cancellation is forwarded to the executor's database
cancellation hook. Install `.[wren,mcp]` to use this entry point.

For isolated local evaluation, pair `semarail-mcp` (the stable read-only tools
`semarail_validate_project`, `semarail_list_models`, `semarail_get_context`, and
`semarail_plan_query`) with
`semarail-query-mcp` (governed execution). The legacy `dsh-data-agent-mcp`
entry point remains available as a compatibility alias.
The thin `SemanticService` calls the pinned WrenAI runtime with the existing
project structures directly. It does not maintain an intermediate semantic
format. SemaRail owns both the stable MCP contract and the governed
database-execution boundary.

## Local development

From this directory, install the test extra and run the test suite with:

```text
python -m pip install -e ".[test]"
python -m unittest discover -s tests -v
```

When pytest is available, `python -m pytest` runs the same focused tests.

To run the process directly:

```text
python -m sidecar
```
