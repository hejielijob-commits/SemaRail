# DeepSeek Harness Wren Data Agent

An out-of-tree, installable DeepSeek Harness bundle that uses Wren as the
semantic SQL layer and renders query results inside Harness conversations.

## Non-negotiable boundary

This project extends DeepSeek Harness exclusively through its public Bundle,
Cordis plugin, tool, session presentation metadata, and Client slot APIs. It
does not patch, fork, or modify the DeepSeek Harness repository.

## Target baseline

- DeepSeek Harness `>=0.1.0-rc.10 <0.2.0`, compiled and accepted against
  the current DSH Desktop runtime `0.1.1-rc.2`
- Wren Python CLI/Core `0.13.2`
- Node.js `^22.19.0 || >=24`
- Python `>=3.11`

### Client rc.10 compatibility and semantic-console URL

The Client package now targets the public rc.10-compatible slot/runtime API:
its peer range is `>=0.1.0-rc.10 <0.2.0`.  The public npm registry did not
contain `@deepseek-ai/dsh-client-runtime@0.1.0-rc.10` (the representative
`npm view ...@0.1.0-rc.10` query returned `E404` during this migration), so
the final typecheck/build was run against the official `0.1.1-rc.2` packages
shipped in DSH Desktop 2.0.2 at
`C:\Users\20786\AppData\Local\Programs\DSH Desktop\resources\app.asar.unpacked\node_modules`
(the path is installation-specific).  Those packages expose the
`ClientContext` from `@deepseek-ai/dsh-client-runtime/client`, the keyed
`tool.call.toolview` slot, and the public list slot
`sidebar.footer.action` with a `{ wide: boolean }` owner.  No Harness source
or DOM internals are required by this plugin.

The public Client `apply(ctx)` receives the slot-bearing context only; there
is no stable Host-to-Client `consoleUrl` field in this API.  Configuration is
therefore deliberately local to the browser: an embedding may pass a
`semanticConsoleUrl` to the exported view/link props, or set
`localStorage['dsh-wren-data-agent.semantic-console-url']`.  Both paths accept
only absolute `http:`/`https:` URLs without credentials (maximum 2,048
characters).  Otherwise the link uses the explicit loopback default
`http://127.0.0.1:48763`.  External links use `target="_blank"`
`rel="noopener noreferrer"`; localStorage is a same-origin convenience and
not a trust boundary.  The Bundle patch intentionally does not claim to
forward a Host config value that the public Client context cannot receive.

## Semantic Console

The Host starts a second, independently supervised Python process for the
local Wren Semantic Console. It listens only on
`http://127.0.0.1:48763`, serves the packaged production SPA, and manages data
sources, project drafts, Wren validation/build, publication, versions, and
rollback. The Client exposes the console through the public
`sidebar.footer.action` slot and through each completed query card. No Harness
source or DOM is patched.

The Console projects Wren's file-based model metadata into a bilingual
business-model editor. Models and fields can be edited visually, relationships
are shown on an interactive graph, and every visual edit remains traceable to
its generated `metadata.yml`, `relationships.yml`, localized metadata, and
unified source diff. English and Simplified Chinese are available without
changing the technical identifiers used by Wren queries.

Datasource credentials are stored outside the Wren project at a deterministic
per-project path below `~/.wren/semantic-console`. The query sidecar reads the
active PostgreSQL profile from that same state on every query and falls back to
`WREN_DATABASE_URL` only when no active Console profile exists. The Console is
local-only and unauthenticated in this MVP; team login, RBAC, approval, and
audit are later deployment work.

## Deploying the packaged Python runtimes

The Host package stages both `python/sidecar` and the Semantic Console server
and SPA into its npm tarball. It does not require a checkout of this repository
at runtime. The default query-sidecar working directory is the packaged
directory. Set `workingDirectory` only when an explicitly managed query
sidecar directory should override that default.

Install the PostgreSQL/Wren runtime into the Python environment that the Host
will use.  `.[wren]` is intentionally installed from the packaged directory,
so the source checkout is not needed:

```powershell
$sidecarDir = 'C:\path\to\node_modules\@hejielijob\dsh-wren-data-agent-host\python\sidecar'
$consoleDir = 'C:\path\to\node_modules\@hejielijob\dsh-wren-data-agent-host\python\semantic-console'
$python = 'C:\Python311\python.exe'
Push-Location $sidecarDir
& $python -m pip install '.[wren]'
Pop-Location
& $python -m pip install $consoleDir
```

Configure the Host with an absolute Wren project directory and, when the
`python` command is not the intended interpreter, an explicit executable in a
Harness profile patch (a row patch replaces that row's complete `config`):

```yaml
- id: wren-data-agent-host
  config:
    pythonExecutable: C:\Python311\python.exe
    projectDir: D:\data\wren-project
    databaseDsnEnv: WREN_DATABASE_URL
    # semanticConsoleEnabled: false  # optional; enabled by default
    # workingDirectory: D:\managed\sidecar  # optional explicit override
```

Provide the PostgreSQL DSN to the sidecar through the operating-system or
secret-manager environment before starting Harness (for example,
`WREN_DATABASE_URL`).  `databaseDsnEnv` is only the environment-variable name;
the DSN itself must never be placed in Bundle/Host configuration, tool
arguments, session metadata, or logs.  The sidecar reads it inside the Python
process and the Host only returns credential-free stable error messages.

## Packages

- `packages/contract`: shared JSON-safe Host/Client and Sidecar contracts.
- `packages/host`: two tools, query Sidecar supervision, independent Semantic
  Console supervision, cancellation, and durable result projection.
- `packages/client`: keyed `data_query` Chart/Table/SQL conversation view with a paginated preview table, plus the public semantic-console sidebar/card entries.
- `packages/bundle`: installable `dsh.bundle` composition.
- `apps/semantic-console`: local REST server and responsive React management
  console for data sources and Wren project lifecycle.
- `python/sidecar`: framed RPC server, Wren context/planning, PostgreSQL query
  execution, AST policy, limits, and chart specification.

The Client bundles Apache ECharts 5.6.0; its original Apache-2.0 `LICENSE` and
`NOTICE` are shipped under `packages/client/licenses/echarts` and included in
the Client npm tarball.

The implementation is an MVP candidate. Automated unit, packaging, isolated
Harness, and real PostgreSQL gates are provided below. AC02 remains a separate
real-model quality gate and is never inferred from unit or smoke tests.

## Development and isolated Harness acceptance

Build the plugin packages before packing them:

```powershell
pnpm build
```

The repeatable acceptance flow packs `contract`, `host`, `client`, and the
installable bundle into a fresh temporary directory, creates an isolated
`DSH_HOME/profiles/web`, installs the tarballs with pnpm, and runs the built
Harness CLI. It validates both the composed `dump-config` rows and the live
web server (`GET /` and the plugin's `client.js` must return HTTP 200). The
gate also imports the Host from the freshly installed npm package and starts
the Python Sidecar from that package's staged `python/sidecar` directory,
checking health, Wren validation, context retrieval, and dry planning. The
Harness checkout is read-only from this flow; no Harness source is copied or
modified. The temporary web process is killed on exit, including its child
processes.

From this repository on Windows:

```powershell
pnpm acceptance
```

The script targets the sibling `deepseek-harness` checkout and the `node` and
`pnpm` executables on `PATH` by default. Explicit paths can be supplied when
using the bundled runtimes or another checkout:

```powershell
pwsh -NoLogo -NoProfile -File scripts/acceptance.ps1 `
  -HarnessDir D:\xiaohe\wrenAI\deepseek-harness `
  -TempDir C:\temp `
  -NodePath C:\path\to\node.exe `
  -PnpmPath C:\path\to\pnpm.cmd `
  -PythonPath C:\path\to\python.exe `
  -KeepTemp
```

The selected Python environment must already contain the packaged Sidecar's
`.[wren]` dependencies. By default the script uses this repository's
`.venv\Scripts\python.exe` when present, otherwise `python` from `PATH`.

`TempDir` is a parent directory; each run creates a unique child and never
removes the parent. Successful runs clean their child automatically. Failed
runs retain logs and `dump-config.txt` and print
`ACCEPTANCE_FAIL_WORKDIR=...`; use `-KeepTemp` to retain a successful run as
well. Use `-SkipBuild` only when all package artifacts are already current.

Expected terminal evidence includes:

```text
ACCEPTANCE_PASS
  dump-config: Host + Client rows detected
  HTTP: GET http://127.0.0.1:<port>/ -> 200
  HTTP: GET http://127.0.0.1:<port>/plugins/@hejielijob/dsh-wren-data-agent-client/client.js -> 200
```

## Real PostgreSQL acceptance

`examples/wren-postgres` is paired with
`scripts/acceptance-postgres.py`, a real PostgreSQL gate for the Wren/Sidecar
boundary. It never downloads or installs PostgreSQL. The default `provision`
mode uses an existing libpq administrator configuration (`PGHOST`, `PGPORT`,
`PGUSER`, `PGPASSWORD`, or a service), creates a uniquely named temporary
database and read-only role, loads the seed, runs the real Sidecar, and drops
only those generated objects. An admin DSN may instead be supplied through
`--admin-dsn-env NAME`; the DSN value and generated password stay in memory and
the child process environment only.

Validate configuration without a database connection:

```powershell
& .venv\Scripts\python.exe scripts\acceptance-postgres.py --dry-run
```

Run the complete gate after PostgreSQL and the Python Wren environment are
available:

```powershell
& .venv\Scripts\python.exe scripts\acceptance-postgres.py
```

For a managed database that must not be provisioned or changed, use
`--mode existing --database-dsn-env NAME`, with the read-only DSN already held
in that environment variable. Existing mode performs the same query result,
policy, row-bound, read-only-write, timeout, and cancellation checks without
creating or dropping database objects. Failed runs retain a safe diagnostics
directory; no password or DSN is written there.

## AC02 golden-question evidence

The twenty-question corpus contains deterministic, machine-checkable oracles
for result columns, required semantic models, and rows from `seed.sql`. Date
expectations are relative to the PostgreSQL `CURRENT_DATE` used while seeding.
AC02 is evaluated only from a real DeepSeek Harness/model capture; the
evaluator does not call an LLM and cannot manufacture a pass.

Validate the corpus and evaluator prerequisites, or exercise evaluator logic
without making an AC02 claim:

```powershell
pnpm evaluate:golden --dry-run
pnpm evaluate:golden --self-test
```

Create a twenty-record capture template, replace every placeholder from real
Harness tool events, and evaluate it:

```powershell
pnpm evaluate:golden --make-template .acceptance/golden-capture.json
pnpm evaluate:golden `
  --evidence .acceptance/golden-real.jsonl `
  --report .acceptance/golden-report.json
```

Each record identifies one question and attempt, the fixture date, the
`wren_semantic_context` call index/status, and the subsequent `data_query`
presentation from durable `tool/result.meta`. Every question needs exactly one
first-pass record. An optional attempt 2 must reuse the same successful
context, follow a retryable `SEMANTIC_ERROR`, and declare
`repairOfAttempt: 1`; policy, timeout, and cancellation failures may not be
retried. SQL is rechecked by the Sidecar's PostgreSQL AST policy and results
must be complete (not truncated) before oracle comparison.

The report contains only safe identifiers, hashes, counts, and per-question
reason codes—never SQL, rows, prompts, model output, DSNs, or credentials. It
passes only at first-pass >= 16/20 and after at most one repair >= 18/20.
`--dry-run`, `--self-test`, and template generation explicitly state that they
do not verify AC02.

## AC05 replay evidence

The API-only replay collector uses only the rc.10-compatible public
`session.list/history/fork` endpoints. It reads the durable
`tool/result.message.meta` projection, hashes the complete data-query
presentation, forks at the last completed turn, and checks that the fork
preserves the same metadata. It never reads Harness JSONL files or private
internals:

```powershell
pnpm acceptance:replay --dry-run
pnpm acceptance:replay --base-url http://127.0.0.1:3080 --session-id <id>
```

This is a public-history/fork gate only. It explicitly reports that browser
hard-refresh rendering is not evaluated; AC05 is not considered complete until
the same durable metadata is also observed in the real Client after refresh
and restore.
