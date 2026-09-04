# SemaRail Semantic Console server

This server is a local-only REST API for onboarding a datasource and editing a
SemaRail semantic project. The current implementation uses pinned public
upstream runtime APIs for validation, build, and target generation. The
standard Console install includes PostgreSQL (`psycopg`), MySQL
(`mysql.connector`), SQLite (Python standard library), ClickHouse
(`clickhouse-connect`), and DuckDB (`duckdb`) metadata drivers. The datasource
type API and UI expose only configured types whose drivers can be imported by
the running Python environment. SQLite and DuckDB open existing files read-only.

Start it from the repository root with:

```text
python -m server --project-dir ./semantic-project --state-dir ~/.semarail/semantic-console/my-project
```

(`PYTHONPATH=apps/semantic-console` is needed when running from a checkout.)
The default bind is `127.0.0.1:48763`. `--host` can be changed deliberately,
but the default never listens on a LAN interface. `--static-dir` optionally
serves a built SPA and falls back to its `index.html` for non-API routes.

## API contract

`openapi.json` is the machine-readable contract. HTTP success responses return
the resource or list directly. Errors return:

```json
{"code":"VALIDATION_FAILED","message":"project validation failed","details":{}}
```

The primary routes are:

| Route | Purpose |
| --- | --- |
| `GET /api/health` | Core process and semantic-runtime readiness |
| `POST /api/v1/runtime/rpc` | Authenticated, versioned agent runtime handshake/context/query/cancel boundary |
| `GET /api/v1/artifacts/{id}/download?token=...` | Core-owned, short-lived query artifact download (`attachment`, `no-store`; invalid token is 404 and expired token is 410) |
| `GET /api/v1/auth/providers` | Public list of configured identity provider ids and labels |
| `POST /api/v1/auth/device/start` | Create a bounded one-time browser/device login transaction |
| `GET /api/v1/auth/callback/{provider}` | Verify OAuth/OIDC state and show a browser-only one-time confirmation code |
| `POST /api/v1/auth/device/token` | Poll, submit the browser confirmation code, and consume the transaction for one SemaRail session |
| `GET /api/v1/auth/capabilities` | Resolve independent current-project Console and Access administrator capabilities |
| `GET /api/v1/auth/me` | Resolve the current employee session |
| `POST /api/v1/auth/logout` | Revoke the current employee session |
| `GET /api/v1/access/users` | Administrator-only employee identities, trusted attributes, and policy bindings |
| `PUT /api/v1/access/users/{id}` | Administrator-only employee name/attribute update |
| `PUT /api/v1/access/users/{id}/status` | Administrator-only immediate employee enable/disable |
| `GET /api/mcp-integration` | Authenticated remote MCP readiness, secret-free client configuration, and trusted-local compatibility guidance |
| `GET /api/project` | Project overview, draft count, revision, active datasource |
| `GET /api/datasource-types` | Field metadata for configured, runtime-available drivers |
| `GET/POST /api/datasources` | Redacted datasource list/create |
| `GET/PUT/DELETE /api/datasources/{id}` | Redacted datasource CRUD |
| `POST /api/datasources/{id}/test` | `SELECT 1` connection test |
| `POST /api/datasources/{id}/activate` | Persist the active profile id |
| `GET /api/datasources/{id}/schemas` | Schema browser |
| `GET /api/datasources/{id}/tables?schema=...` | Table browser |
| `GET /api/datasources/{id}/columns?schema=...&table=...` | Column browser |
| `POST /api/datasources/{id}/models` | Generate `models/<name>/metadata.yml` as a draft |
| `POST /api/project/import` | Import a source directory or `{files:[...]}` as a draft |
| `GET /api/project/files` | Draft-aware file metadata |
| `GET/PUT /api/project/file?path=...` | Read/edit a draft file |
| `GET /api/semantic-project` | Read the structured business-model snapshot used by the visual editor |
| `PUT /api/semantic-models/{name}` | Update one model, its fields, and semantic metadata |
| `PUT /api/semantic-relationships` | Replace validated model relationships and their semantic metadata |
| `GET/POST /api/knowledge/rules` | List or create one-rule records |
| `GET/PUT/DELETE /api/knowledge/rules/{id}` | Read/edit/delete one rule |
| `POST /api/knowledge/rules/{id}/enable` | Enable a rule in the effective semantic-project knowledge directory |
| `POST /api/knowledge/rules/{id}/disable` | Disable a rule and move its content out of the effective directory |
| `GET/POST /api/knowledge/sql-candidates` | List or submit SQL candidates for review |
| `GET/PUT /api/knowledge/sql-candidates/{id}` | Read or edit a pending SQL candidate |
| `POST /api/knowledge/sql-candidates/{id}/approve` | Approve and create a `knowledge/sql/<slug>.md` draft |
| `POST /api/knowledge/sql-candidates/{id}/reject` | Reject a candidate without writing `knowledge/sql` |
| `POST /api/knowledge/sql-candidates/{id}/resubmit` | Move a rejected candidate back to pending review |
| `GET /api/project/diff?path=...` | Read the bounded diff between published and draft content |
| `POST /api/project/validate` | Validate the effective draft tree |
| `POST /api/project/publish` | Validate/build then transactionally publish |
| `GET /api/versions` | Published snapshot list |
| `POST /api/versions/{id}/rollback` | Validate and restore a snapshot |

`POST /api/v1/runtime/rpc` requires `Authorization: Bearer <token>`. Set a token of at least 32 characters in `SEMARAIL_API_TOKEN` before starting Core. The public request cannot select a project, send database credentials, choose a Subject, supply an authorization policy, or override query limits; those values are resolved by Core. The currently supported v1 methods are `health`, `project.validate`, `project.describe`, `context.ask`, `query.dryPlan`, `query.run`, and `query.cancel`. The authenticated Streamable HTTP MCP endpoint and `semarail mcp bridge` both enter this same boundary. Direct `semarail-mcp` and `semarail-query-mcp` processes are trusted-local compatibility tools and are not a per-user authorization boundary. Identity-provider configuration and employee CLI login are documented in `docs/access-control.md` at the repository root.

The bootstrap administrator token is accepted by the Console management APIs and the read-only Core health check, but intentionally rejected by semantic/query runtime methods, remote MCP, and the authenticated stdio bridge. Create a scoped service-account key or use an employee session for Agent access. Configure `SEMARAIL_REMOTE_MCP_URL` when the public MCP endpoint differs from the loopback default; the value must be an HTTP(S) URL without embedded credentials, query parameters, or fragments.

### Query artifacts

`query.run` is the only public operation that can produce an artifact. Core
creates a random reservation before entering the sidecar and injects a trusted
internal `artifactRequest` containing only the artifact id, random filename,
CSV content type, expiry, and the fixed 16 MiB maximum. Set
`SEMARAIL_ARTIFACT_TTL_SECONDS` to 60–86400 seconds on the Core/MCP service to
change the default 900-second lifetime. Public callers cannot select the expiry
or provide a path, filename, output format, or execution limit. A sidecar success
response may include only safe metadata:

```json
{"artifact":{"id":"art_<random>","format":"csv","fileName":"artifact-<random>.csv","rowCount":500,"sizeBytes":1234,"sha256":"<64 hex>","expiresAt":"<Core expiry>"}}
```

Core adds the temporary `token` and `downloadUrl` to the result. The
token is returned only in that response and is persisted only as a salted hash
in `<state_dir>/artifacts.sqlite3`; bytes live under `<state_dir>/artifacts`.
The reservation expires after the configured lifetime (15 minutes by default).
Its database record also binds the query id, organization, subject, credential,
datasource, and policy versions. Sidecar metadata must not contain a
local path, token, URL, SQL, or rows. Before streaming, Core rechecks that the
original subject and credential remain active, the active datasource id is the
same, the current compiled policy-version list is unchanged, the file is no
larger than 16 MiB, and its SHA-256 matches. Expired/context-invalid artifacts
are not downloadable. The remote Streamable HTTP MCP server registers the
same route with `FastMCP.custom_route`; it returns the same attachment and
cache headers without redirecting.

Access-control state defaults to a local SQLite file under the private state directory. For a shared production control plane, set `SEMARAIL_ACCESS_CONTROL_DATABASE_URL` to a PostgreSQL URI. If that configured database is unavailable or has an unknown migration version, startup fails closed and never falls back to SQLite. Governed PostgreSQL execution uses `SEMARAIL_DATABASE_URL`; keep both database URLs only in the Core process environment.

Edits never mutate the project until publish. Publishing validates/builds a
temporary tree, writes a generated MDL target, snapshots that **new** tree, and
replaces managed source files while leaving `.git` and hidden runtime state in
place. A transaction backup allows recovery if a file replacement fails.
The MVP does not create Git commits; use the repository's normal Git workflow
if commit history is desired.

### Structured semantic-model API

`GET /api/semantic-project` returns models projected from
`models/*/metadata.yml`, relationships from `relationships.yml`, and the
optional bilingual companion file `semantic-console/locales.yml`.  The
companion file is outside Wren's recognized source paths, so Wren 0.13.2
ignores it while the console uses it for `zh-CN`/`en-US` display names and
descriptions.  It is parsed with `yaml.safe_load`, bounded by the same project
file size/credential checks as other project files, and never interpreted as
Python/YAML tags.

The two write endpoints are draft operations.  Send the `revision` returned by
the snapshot as `expectedRevision`; a stale revision returns HTTP 409 with the
current revision and leaves every file unchanged.  A model save updates its
metadata and locale companion together.  A relationship save replaces the
visualizable relationship list and updates its locale records together, while
retaining source relationships that reference unknown models for later repair.
Both operations preserve unknown Wren keys (including custom model/column keys,
relationship root/entry keys, and extension keys in `locales.yml`) and only
overwrite fields owned by the visual editor.

The snapshot keeps the backwards-compatible `LocalizedText` shape with both
`zh-CN` and `en-US` keys.  New clients may send a single string for
`displayName` or `description`; an optional request-only `locale` (`zh-CN` or
`en-US`, with `zh`/`en` aliases) selects the language slot.  When it is omitted,
an existing record with only one populated locale is updated in that locale;
otherwise `en-US` is the stable default.  A localized object may contain either
or both keys.  Missing keys are patches, not clears, so an English-only edit
cannot silently remove an existing Chinese translation.  The locale hint is
never written to Wren files.  Existing callers that send the full bilingual
object continue to work unchanged.

Relationship entries must name two existing models, use one of Wren's four
join types, and contain a condition that qualifies both referenced models
(for example `orders.customer_id = customers.id`).  Existing hand-edited
relationships that point at an unknown model are omitted from the graph and
reported in `relationshipErrors`; they remain visible through the underlying
file view for repair.  For direct equality terms, the snapshot also exposes
transient `fieldPairs` (including composite joins) for field-level graph edges;
the Wren `condition` remains the only persisted truth, so complex expressions
are preserved verbatim and are not rewritten into an extension format.
`GET /api/project/diff` shows the exact bounded unified diff for that underlying
file.

### Knowledge governance API

Rules are returned as individual records even when an existing Markdown file
contains several top-level bullet rules. Such a legacy file is split
logically, without discarding its original bytes. Before the first edit the
source is copied to `semantic-console/rules-archive/`; disabling a single
legacy rule removes that bullet from the active `knowledge/rules/` draft and
stores its exact content under `semantic-console/rules-disabled/`. A disabled
rule is never left in `knowledge/rules/`, and enabling it reconstructs the
archived document in its original order. The index at
`semantic-console/rules-index.json` retains unknown root/record fields for
forward compatibility. Rule changes are project drafts and become effective
after the normal validate/publish operation.

SQL candidates are deliberately kept outside the semantic project in the
sidecar's private state directory (`sql-candidates.json`). `POST
/api/knowledge/sql-candidates` accepts `question`, `sql`, optional `queryId`,
`sessionId`, `dialect`, `stats`, and `sqlHistory` (an array of JSON-safe
`{id, question, sql, sourcePath?}` references). Legacy aliases `nl`,
`historySql`, and `historySqlRefs` remain accepted. Submitting the same
normalized question/SQL/dialect returns the existing candidate instead of
creating a duplicate. New candidates always start as `pending`; a client
cannot bypass review by supplying `status: approved`.

`POST /api/knowledge/sql-candidates/{id}/validate` parses a pending candidate
without changing its review status. It accepts one read-only query root
(`SELECT`, including set operations), rejects DDL/DML and dangerous functions,
and the approve endpoint repeats the same validation before writing a draft.

Only the approve endpoint can create a SQL example, and it writes it as a
validated project draft. Approval is idempotent and may include a reviewer SQL
correction before the file is generated. If the private queue cannot be
persisted, the project draft is rolled back and the candidate remains pending.
Approved candidates are immutable; rejected candidates can be explicitly
resubmitted. Candidate metadata is recursively checked for JSON-safe values,
bounded in size, and rejected when it contains credential-shaped keys or
connection strings.

Datasource passwords and connection URLs are accepted only in create/update
bodies. They are held in memory and persisted, for restart support, in
`<state-dir>/datasources.secrets.json` with best-effort `0600` file and `0700`
directory permissions. A custom `--state-dir` must also be outside the Wren
project. The file is never returned by the
API, excluded from project snapshots, and never sent to the sidecar/client.
The active datasource id is stored alongside it; the password is never written
to `wren_project.yml`.

For browser development, set `SEMANTIC_CONSOLE_ORIGINS` to a comma-separated
explicit allow-list such as `http://localhost:5173`. No wildcard CORS is used.
All requests must use a localhost Host header, and write requests with a body
must use `application/json`.

## Focused tests

The server tests use only the Python standard library and isolated temporary
directories (no PostgreSQL server or user project is touched):

```powershell
$env:PYTHONPATH = "apps/semantic-console"
.venv\Scripts\python.exe -m unittest discover -s apps/semantic-console/server/tests -v
```
