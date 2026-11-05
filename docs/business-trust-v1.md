# Business answer trust v1

This opt-in extension checks declared business constraints against semantic SQL.
It does not prove that a query fully represents a natural-language question, or
that an Agent's explanation, business recommendation, or causal claim is correct.
Core does not invoke a model. External Agent evaluations remain external evidence.

## Sources and execution boundary

Business checks use the published project tree, not the Console draft overlay.
Add `executionChecks` to an existing rule, then publish it. Rules, model metadata,
and declared relationships remain the sources of truth; there is no second metric
catalog. Projects without this extension retain the existing clarification flow.

The versioned checks envelope is `{"schemaVersion":1,"checks":[...]}`. Unknown
versions or fields are rejected. Supported declarations cover metric aggregation,
equality predicates, confirmed/defaulted date filters, original-date grouping,
and model/column references. See the Console rule API and the verifier tests for
the precise check fields. Test dates are synthetic snapshots. Undefined units are
reported as undefined; Core does not infer a currency or rating scale.

The SQL verifier uses sqlglot ASTs. The v1 subset is a single fact model, aliases,
conjunctions, aggregations, grouping, and declared many-to-one dimension joins.
OR, set operations, windows, complex subqueries and multiple fact joins cannot
be proved by v1. Conflicts block execution. A mandatory unknown check also blocks;
optional unknown checks remain visibly unverified. Preparation does not authorize
a SQL query: the existing policy, physical allowlists, read-only runtime and
limits still apply during execution.

Preparations bind the subject, credential, SQL digest, active datasource,
published semantic revision and rule implementation version. Execution rechecks
the current bindings and constraints. Callers cannot submit verification statuses,
even when using a legacy query presentation version.

An internal datasource profile fingerprint also detects connection changes under
the same datasource ID; display-name changes do not invalidate it. Execution pins
publication and datasource configuration through dispatch, while cancellation
remains available. The final boundary rechecks method grants and compiled policy
versions; changed permissions require a new preparation. Fingerprints and private
policy details are never returned in the evidence payload.

## Independent evidence interface

The existing query result schemas are unchanged. Read evidence separately:

- Core RPC `query.evidence` with `{"queryId":"..."}`.
- Authenticated remote/stdio MCP `semarail_get_query_evidence(query_id)`.
- Console `GET /api/v1/diagnostics/queries/{queryId}/evidence`.

`BusinessEvidenceV1` contains schema/rule/semantic versions, query/datasource IDs,
execution outcome, timestamps, published rule/model/column references, declared
metrics, unit, grain, time source, defaults, conditions, confirmations and checks.
Confirmation states (`confirmed`, `defaulted`, `missing`) are independent from SQL
check statuses (`passed`, `conflict`, `unverified`, `stale`). SQL-extracted dates
are not represented as user-confirmed dates. Grouping by the original date does
not satisfy a separate required period confirmation. A published default applies
to grouped queries too, and its exact date or range must match the SQL filter.
A changed published revision or verifier implementation makes historical checks
stale. A changed active datasource denies historical reads.

The corrected verifier uses `rulesVersion: "business-trust-v2"`; this identifies
the implementation revision, while `BusinessEvidenceV1` and `executionChecks`
retain schema version 1. Preparations created by the previous verifier require
preparing again. Historical evidence remains readable under current permissions,
with its original rule version and checks marked `stale` / `RULES_VERSION_CHANGED`.

The ledger retains evidence for thirty days independently of Trace and audit
bodies. It contains no SQL, result rows, answers, conversation, bearer credentials,
physical file paths or row policy expressions. Permission bindings for safe
table/column rechecks remain internal. MCP requires original subject ownership
and current project/datasource/table/column permissions. Console requires a
same-project administrator and current data permissions. A query ID cannot
overwrite an existing evidence record. Storage failure cannot establish a pass.

## Feedback, repair and review

The Console issue detail links to existing asset editors, published versions,
regression cases and run records. It stores explicit repair associations:

```
feedback -> changed semantic asset -> published version
         -> case revision -> SQL run -> reviewed external Agent summary
```

Routes are scoped to `/api/v1/diagnostics/feedback/{feedbackId}`:

- `GET/PUT /resolution` (PUT requires `expectedRevision`).
- `POST /resolution/verify` (empty body; Core derives the state).
- `POST /agent-evaluations` (v1 safe external summary).
- `POST /agent-evaluations/{evaluationId}/review` (`expectedRevision`, `decision`).

An external summary records campaign/report digest, source, observed model,
reasoning setting, dataset/semantic versions, and verdict aggregates. It never
includes raw answers or result rows. Reviewer subject/time are server-owned.
Importing a summary does not itself approve it or turn it into a Core evaluation.

Full verification requires an actually changed asset in the current published
snapshot and unexpired source feedback evidence matching the base semantic
version. The linked current case revision must be enabled and identify a known
dataset version. Its completed, non-expired SQL run must pass assertions, include
a Core query ID and Core trace IDs, and match the published semantic and dataset
versions. An approved external evaluation must match those same versions and
have all verdicts passing.
Editing or deleting the regression case makes the old association stale. Reading
that historical association remains supported; linking an outdated revision is
rejected with 409. The current delete operation removes the case revisions, so
linking a revision removed this way, or any missing revision, is rejected with
404. Retained legacy records marked deleted cannot establish verification.
An SQL pass alone means query regression passed. Historical manual workflow
statuses are labeled as historical manual states, not retroactively verified.
Expired feedback/run evidence, unrelated cases, version mismatches and unreviewed
summaries cannot establish a new verified state.

## Storage migration and rollback

The new ledgers share the configured control-plane connection with access
control, using independent migration tables:

- `business_evidence_schema_migrations` / `business_query_evidence`.
- `feedback_resolution_schema_migrations` / `feedback_resolutions` /
  `feedback_agent_evaluations`.
- Incremental query preparation migration adds business binding fields.

Current preparation migration v5 includes the internal profile binding. Evidence
migration v3 adds the matching internal binding; older records without that
binding cannot be used as currently verified evidence. The public evidence
schema remains version 1.

SQLite migrations use the existing process lock plus an immediate transaction.
PostgreSQL uses transaction advisory locks. Newer or discontinuous migration
versions fail closed. Back up the control-plane database and semantic project
before an upgrade. Do not edit migration ledgers to force a downgrade.

To disable the feature, remove/disable the execution checks through the existing
rule editor and publish the change. A semantic rollback also invalidates relevant
preparations and historical checks. Retain the newer server for evidence reads
until retention expires. Downgrading code may make new records unavailable;
restore the matching database/project backup when a full downgrade is required.
Never restore credentials through an Agent payload or a checked-in fixture.

## Evaluation and promotion

The new thirty-question corpus covers three HR scenes, five roles, twenty Chinese
and ten English questions. The historical sixty-question deterministic suite and
historical thirty-question experiment remain separate. Freeze questions,
clarification answers, private oracles, scoring, dependencies, data and roles
before observing campaign outcomes. Real authenticated MCP/hybrid retrieval and
safe knowledge delivery must pass before the Agent campaign.

Compare frozen baseline and candidate in three interleaved rounds: ninety jobs
per group, 180 immutable schedule positions. Use identical observed model,
reasoning, task instructions, roles and data. All positions remain in the
denominator, including timeouts, missing records and audit failures; do not select
successful retries. Report first SQL, final answer, clarification, authorization,
business omission and elapsed time, broken down by scene, language and role.

Promotion requires complete audit evidence and stable observable settings, zero
confirmed unauthorized disclosure, no first-SQL regression, more final correct
answers overall and in at least two rounds, no increase in silent errors or
critical omissions, and passing deterministic counterexamples. Without those
results this remains explicit opt-in and Context defaults stay unchanged.

Implementation tests are technical evidence. They are not a substitute for the
180-job campaign or the real published snapshot repair acceptance case.
