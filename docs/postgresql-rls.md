# PostgreSQL row-level security

SemaRail enforces table, column, and row policy before a query reaches the
database. PostgreSQL Row Level Security (RLS) is an additional production
boundary: the query role receives the same server-verified Subject context in
transaction-local settings, and PostgreSQL evaluates it again for every row.

## Session contract

Immediately after opening a read-only transaction and before executing user
SQL, SemaRail sets four fixed settings with bound parameters:

- `semarail.subject_id`
- `semarail.organization_id`
- `semarail.attributes` — a JSON object containing only attributes referenced
  by effective row rules
- `semarail.policy_versions` — a JSON array used for diagnostics and audit

The settings use PostgreSQL's transaction-local `set_config(..., true)` form.
They disappear when SemaRail rolls the transaction back and closes the
connection. MCP arguments cannot supply or override this context.

## Query role requirements

Use a dedicated login that:

- has `NOSUPERUSER NOBYPASSRLS`;
- does not own protected tables;
- has only `CONNECT`, schema `USAGE`, and required `SELECT` grants;
- has `default_transaction_read_only = on`;
- is known only to SemaRail, not to Agent clients or employees.

Table owners normally bypass RLS, while roles with `BYPASSRLS` always do. Use
`FORCE ROW LEVEL SECURITY` as defense in depth and keep migrations on a
separate owner connection.

## Sales-region example

The following policy assumes `sales.organization_id`, `sales.region_code`, and
a Subject attribute named `regionCodes`:

```sql
ALTER TABLE analytics.sales ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics.sales FORCE ROW LEVEL SECURITY;

CREATE POLICY semarail_sales_region
ON analytics.sales
FOR SELECT
TO semarail_query
USING (
  organization_id = current_setting('semarail.organization_id', true)
  AND region_code IN (
    SELECT jsonb_array_elements_text(
      COALESCE(
        NULLIF(current_setting('semarail.attributes', true), ''),
        '{}'
      )::jsonb -> 'regionCodes'
    )
  )
);
```

With employee A assigned `{"regionCodes":["甲"]}` and employee B assigned
`{"regionCodes":["乙"]}`, the same aggregate query sees different input rows.
Missing session context yields no matching region values and therefore no rows.

Test the role itself before using it in SemaRail:

```sql
SELECT rolsuper, rolbypassrls
FROM pg_roles
WHERE rolname = 'semarail_query';

SELECT pg_get_userbyid(relowner) AS table_owner
FROM pg_class
WHERE oid = 'analytics.sales'::regclass;
```

Both role flags must be false and `table_owner` must not be `semarail_query`.
Do not grant employees direct access to this login: a PostgreSQL user able to
run arbitrary statements could set custom settings without passing through
SemaRail.

## Control-plane database

SQLite remains the local single-host default. For a durable production control
plane, set a separate PostgreSQL URL before starting Core:

```powershell
$env:SEMARAIL_ACCESS_CONTROL_DATABASE_URL = "postgresql://<control-user>:<password>@<host>/<database>"
semarail start --project C:\path\to\semantic-project
```

This URL stores Subjects, credential hashes, sessions, policies, bindings, and
audit events. It is separate from the analytical datasource URL. If the
configured driver, connection, or schema migration is unavailable, Core fails
closed and does not fall back to SQLite.

## Acceptance gate

`pnpm run acceptance:postgres` provisions an isolated database and
`NOSUPERUSER NOBYPASSRLS` read-only role when PostgreSQL administrator settings
are available. The gate copies the semantic project into its private run
directory and normalizes the accepted connection to `SEMARAIL_DATABASE_URL`,
so saved Console datasource state cannot redirect the test. It verifies that
two trusted Subject contexts executing the same SQL receive disjoint regional
rows. `--mode existing` never changes the target database, so the RLS
provisioning probe is intentionally skipped there.
