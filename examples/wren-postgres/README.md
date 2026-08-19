# Sales analytics example

This Wren v5 project is the deterministic PostgreSQL fixture for the MVP. It
models a small sales domain with customers, regions, products, orders, and
order items. `seed.sql` creates only the tables and deterministic fixture rows.
The real PostgreSQL gate creates its own uniquely named login and grants it
`SELECT` on the five analytics tables, so loading the seed cannot mutate a
pre-existing role or password.

The fixture intentionally excludes customer email and other direct identifiers
from MDL. The application must connect as the generated read-only role in the
gate (or as an equivalently restricted role in a managed environment). No
password is embedded in the fixture. The gate keeps its generated password in
memory and exposes the resulting DSN to the Sidecar only through the child
process environment.

After loading `seed.sql`, validate and build the project with Wren 0.13.2:

```text
wren context validate --project .
wren context build --project .
```

`golden-questions.json` is the acceptance corpus. Phase 0 requires the four
marked smoke cases to cover aggregation, date grain, relationship joins, and
null handling. Phase 3 evaluates all twenty questions through the Harness
agent and records first-pass and at-most-one-repair outcomes. Each question's
oracle fixes the expected output aliases, core semantic models, numeric
tolerance, and deterministic seed rows. `${FIXTURE_DATE}` placeholders refer
to the PostgreSQL `CURRENT_DATE` observed for that seeded run. See
`scripts/evaluate-golden.py` for the normalized real-Harness capture contract;
its synthetic self-test proves evaluator behavior only, not agent accuracy.

## Real PostgreSQL acceptance gate

The repository contains a real database gate at
`scripts/acceptance-postgres.py`. It never downloads or starts PostgreSQL. In
its default `provision` mode it uses the current libpq `PGHOST`/`PGPORT`/
`PGUSER`/`PGPASSWORD` (or an optional admin DSN environment variable), creates
an isolated temporary database and login, loads `seed.sql`, runs the real
Python Sidecar, and drops only the generated objects on exit. Passwords and
DSNs are never written to diagnostics or passed in RPC parameters.

First validate local configuration without connecting:

```text
.venv\\Scripts\\python.exe scripts\\acceptance-postgres.py --dry-run
```

Run the complete gate against an existing PostgreSQL administrator connection:

```text
.venv\\Scripts\\python.exe scripts\\acceptance-postgres.py
```

If the administrator connection is held in a secret-manager-backed environment
variable, use its name (the value itself is never put on the command line):

```text
.venv\\Scripts\\python.exe scripts\\acceptance-postgres.py --admin-dsn-env WREN_POSTGRES_ADMIN_URL
```

When the administrator DSN is assembled from explicit host/user options and a
non-standard secret variable, pass only that variable's name with
`--admin-password-env NAME`.

For a managed database that must not be provisioned or changed, use
`--mode existing --database-dsn-env NAME`, where `NAME` already contains the
read-only role's DSN. Existing mode performs the query/policy/timeout/cancel
checks and does not create or drop database objects.

The gate covers Wren project validation, the four smoke queries and chart/
column/row metadata, server-side row bounds, DML/multi-statement/dangerous
function/unauthorized-object rejection, a direct read-only-account write
probe, and timeout plus framed `query.cancel` behavior. PostgreSQL and the
Python environment are explicit prerequisites; the script fails with a safe
diagnostic when either is unavailable.
