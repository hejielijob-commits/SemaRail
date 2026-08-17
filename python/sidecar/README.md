# Wren data-agent sidecar

This package is the first Python process boundary for the Wren data agent. It
speaks a deliberately small, dependency-free RPC protocol so the Host can
supervise it without importing Wren into the Harness process.

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
  manifest, uses Wren's public context/description capability when available,
  and returns a `schemaVersion: 1` semantic context.
- `query.dryPlan` — requires `projectDir` and `semanticSql`, builds the
  manifest and calls `WrenEngine(..., connection_info={}).dry_plan()` to
  transform SQL without opening a database connection. `query.run` and
  `query.cancel` are deliberately not implemented yet.

Request envelopes fail closed like the TypeScript contract: only
`protocolVersion`, `id`, `method`, `params`, and optional `deadlineMs` are
accepted. `params` must be explicitly present and JSON-safe; method handlers
then enforce their object shape. `deadlineMs` is a non-negative integer and a
JSON boolean is not accepted as an integer.

Malformed transport frames cannot carry a recoverable request id. Their
framed diagnostic uses `id: ""` to mark an uncorrelated transport fault; it is
handled before the ordinary correlated `RpcResponse` parser.

The validator is injected through `SidecarDependencies`, and the CLI default
uses `LazyWrenAdapter`. Protocol and adapter tests can inject a fake Wren
context module, so they do not need Wren or a database installed.

## Local development

From this directory, run the standard-library test suite with:

```text
python -m unittest discover -s tests -v
```

When pytest is available, `python -m pytest` runs the same focused tests.

To run the process directly:

```text
python -m sidecar
```
