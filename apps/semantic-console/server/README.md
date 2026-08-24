# Wren Semantic Console MVP server

This server is a local-only REST API for onboarding a datasource and editing a
Wren 0.13.2 project. It uses the public Wren APIs `wren.context.validate_project`,
`wren.context.build_json`, and `wren.context.save_target` when the runtime is
installed. PostgreSQL is supported through `psycopg`/`psycopg2`; MySQL uses
`mysql.connector` or `pymysql` when available.

Start it from the repository root with:

```text
python -m server --project-dir ./my-wren-project --state-dir ~/.wren/semantic-console/my-project
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
| `GET /api/health` | Process/Wren readiness |
| `GET /api/project` | Project overview, draft count, revision, active datasource |
| `GET /api/datasource-types` | Wren field metadata and driver availability |
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
| `POST /api/project/validate` | Validate the effective draft tree |
| `POST /api/project/publish` | Validate/build then transactionally publish |
| `GET /api/versions` | Published snapshot list |
| `POST /api/versions/{id}/rollback` | Validate and restore a snapshot |

Edits never mutate the project until publish. Publishing validates/builds a
temporary tree, writes a generated MDL target, snapshots that **new** tree, and
replaces managed source files while leaving `.git` and hidden runtime state in
place. A transaction backup allows recovery if a file replacement fails.
The MVP does not create Git commits; use the repository's normal Git workflow
if commit history is desired.

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
