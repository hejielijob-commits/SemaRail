# Wren data-agent sidecar

This package is the first Python process boundary for the Wren data agent. It
speaks a deliberately small RPC protocol so the Host can supervise it without
importing Wren into the Harness process. `sqlglot` is the PostgreSQL AST policy
boundary; the package remains independent of the Harness runtime.

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
  variable is `WREN_DATABASE_URL`; the DSN is resolved inside the sidecar and
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

## Governed MCP adapter

The optional MCP entry point reuses the same `WrenQueryService` and
`EnvPsycopgExecutor` instead of reimplementing query policy:

```text
dsh-data-agent-mcp --project C:\data\wren-project
```

It exposes only `dsh_governed_query` over stdio. The project directory and DSN
environment-variable name are fixed at process startup and never appear as tool
arguments. MCP request cancellation is forwarded to the executor's database
cancellation hook. Install `.[wren,mcp]` to use this entry point.

For governed Agent deployments, pair it with `wren serve mcp --no-connect`.
Wren supplies semantic discovery and planning; DSH owns database execution.

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
