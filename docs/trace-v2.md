# Trace v2: reliable collection and query regression

Trace v2 extends the [v1 execution timeline](trace-v1.md) with local delivery
recovery, evidence-based rules, comparison, and manually reviewed query tests.
Core and Console belong to SemaRail; the optional Codex collector remains in the
separate [adapter repository](https://github.com/hejielijob-commits/semarail-codex-trace).

## Console workflow

1. Open **Quality → Agent traces**. Filter by ingestion time, source Session,
   subject, turn status, and linked issues. Open a Trace to inspect its persisted
   timeline, rule findings, evidence references, and reported collector health.
2. Open **Trace comparison**, choose two Trace IDs from the current project,
   and compare activity status, available model/version metadata and Core phase
   timings. `semanticVersions` preserves every distinct version observed in a
   Trace; the legacy singular `semanticVersion` is populated only when exactly
   one value is available. Each `versionChanges` value is `null` if either side
   lacks that metadata, so two missing values are not called equal. Uncertain
   ancestry, overlapping sibling Agents, missing starts and indistinguishable
   overlapping tool calls stay unmatched. Agent/tool durations are Hook
   observation intervals, not exact LLM or tool execution times.
3. In an **Issues & feedback** detail, choose **Create regression case** to open
   the manual case form under **Quality → Regression cases**. It seeds the
   feedback ID and, when the feedback's Core diagnostic has a verified
   same-project association, links the corresponding Agent Trace. The question,
   SQL, clarification conditions and assertions start blank and must be entered
   and reviewed by a person; Trace bodies are never extracted into a case.
4. In **Regression cases**, register a test dataset ID and version against an
   existing datasource. Activate that test datasource through the existing
   project workflow before running tests. Registration does not copy data,
   restore a snapshot, change the active datasource, or attest data immutability.
5. Review and enable the case, then run one case or a batch of at most twenty.
   Inspect persisted run history and open the Core diagnostic or original issue.

No diagnostic Agent, automatic repair, full Agent replay, scheduled execution,
Token estimation, or transcript collection is included.

## Reliable collection

All projected main Agent, child Agent, and observed MCP completion events enter
a local SQLite queue before delivery. The seven existing Hooks remain async.
The collector starts a detached worker with a single-instance lease; on worker
exit, the next Hook or an explicit flush resumes delivery. Credentials are read
from the configured environment/auth file and are never saved in the queue.

A graceful exit releases its lease. After a crash or forced termination, an
unrenewed lease can remain valid for up to 120 seconds. During that window,
`flush` reports `worker_active`; after expiry, the next Hook or explicit flush
can take over the queue. No automatic process restart is promised while there
are no further Hooks or manual commands.

- Delivery is at least once. Stable event IDs and server idempotency prevent
  duplicate spans after a lost acknowledgement.
- Network failures, timeouts, 429 and 5xx retry with backoff. Authentication and
  permission failures pause delivery; other terminal errors are quarantined.
- Pending metadata expires after seven days. Each project is capped at 10,000
  events and 20 MiB; oldest records are evicted and counted by reason.
- Credential rotation can resume old events only after the server verifies the
  same organization, project and subject as the stored verified identity. If an
  old identity was never verified, rotation leaves those events paused.
- Collection, storage, identity checks and network failures must not change
  Codex tool results or turn control.

Local commands, run with the adapter's installed script path:

```text
python scripts/trace_hook.py status
python scripts/trace_hook.py flush
python scripts/trace_hook.py flush --project D:\projects\my-project
python scripts/trace_hook.py clean
```

`clean` removes expired local metadata. `clean --all` explicitly discards the
local queue and routing metadata. No command displays credentials or bodies.

Collector health is a last-received report, not proof that every Hook ran. The
Console displays backlog, retries, pauses, quarantine and eviction counts, and
marks reports older than five minutes stale. Offline collectors cannot report
their current backlog until connectivity returns. No loss rate is inferred.

## Rules and evidence

Diagnosis is recomputed from persisted events and verified Core associations
at read time. Findings include a rules version, category, code, evidence IDs,
rationale and suggested manual action. Structured Core codes/phases distinguish
authentication, policy, preparation, datasource, SQL validation and runtime
failures. Raw error messages, SQL and result bodies are not rule inputs.

A measured Core phase longer than 5,000 ms is a performance inspection item;
timing alone does not establish root cause. Missing start/end/link evidence is
reported separately. Running spans are not converted into failures. Late
events can change a diagnosis after refresh. Unavailable counters and versions
remain unavailable.

## Query regression contract

Case schema v2 accepts only the listed top-level fields. `conditions` is a
JSON-safe object passed to Core query preparation, with at most 100 top-level
properties and a 256,000-byte serialized case limit; it is not a role or policy
override. Credential-shaped keys and values are rejected recursively, including
inside nested objects and arrays. `confirmedConditions` contains up to 100
strings of at most 128 characters. The case object rejects additional fields,
including legacy `role` and `policyTestConfig`; execution always uses the
current authenticated caller and current policy. Creating a v2 case accepts
only a draft (`enable` omitted or `false`); enabling is a separate reviewed
update.

On case creation, an optional `sourceTraceId` must resolve to an unexpired
Trace in the same organization and project, and that Trace must contain a
verified Core association for the Core diagnostic referenced by the selected
`feedbackId`. A same-project but unrelated Trace is rejected. Later edits keep
the saved source link and remain possible after that Trace expires.

Each case is manually authored as `deterministic_sql` with a question, semantic
SQL, clarification inputs, a registered `testDatasetId`, and assertions:

```json
{
  "schemaVersion": 2,
  "kind": "deterministic_sql",
  "question": "Return the test order count",
  "semanticSql": "SELECT count(*) AS n FROM orders ORDER BY n",
  "conditions": {},
  "confirmedConditions": [],
  "testDatasetId": "orders-fixture",
  "assertions": {
    "status": "success",
    "rowCount": 1,
    "cells": [{"row": 0, "column": "n", "expected": 3, "tolerance": 0}]
  }
}
```

Failure assertions may match `error.code`, `error.reasonCode`, and
`error.phase`. `rowCount` and `cells` are success-only assertions. Cell rows are
zero-based; column names are exact. Numeric tolerance is absolute.
Read-only SQL is parsed before enabling, and Core remains the authorization and
query-safety boundary. Cell assertions require a top-level `ORDER BY`; ordering
keys must be observable in a complete available result and distinguish its
rows. Ties, missing projected keys, incomplete previews, unavailable counters or
truncated output cannot produce a passing result assertion.
SQL row windows (`LIMIT`, `OFFSET`, `FETCH`, or `TOP`), including those inside
subqueries and CTEs, leave cell ordering unproven: a complete returned preview
does not expose tied keys outside the selected window. Status and row-count
assertions remain available for these queries.

Execution calls Core RPC v3 `query.prepare` then `query.run` using the current
managed caller. Unresolved clarification stops execution. Historical `role` or
`policyTestConfig` fields cannot confer permissions or impersonate a subject.
Core reauthenticates and reads current policy on each request, including cancel;
access policy is neither frozen as a run snapshot nor simulated by the runner.

Environment drift means a change in the active datasource, semantic project
configuration, or registered test-dataset version/binding during execution.
The active datasource must match the registered dataset, and its version must
match the case's reviewed version. Dataset changes require another case review.
Console configuration and test-dataset registry mutations compete for the same
project lock as runs, so the check and write are serialized across Console
instances. External changes outside the Console still mark an outcome
inconclusive; they are not hidden by using a stale snapshot. Administrators
remain responsible for keeping the declared test dataset stable. Core checks
the caller's current access policy on every RPC, so policy changes can affect
authorization while the run is in progress.

One batch runs per organization/project, with cases executed serially. Runs
store subject, case revision, environment versions, timings, Core IDs and
assertion statuses, never actual/expected result values or question/SQL bodies.
The trusted regression execution context also disables full-result CSV
artifacts; only the bounded preview is evaluated in memory and discarded.
The explicitly reviewed case retains its manual inputs until deletion. This
follows the existing case and feedback persistence behavior; Trace v2 adds no
feedback-body retention or deletion behavior. The 30-day cleanup applies to
Core diagnostic bodies, Trace event/collector records, and regression run
records. Expired run evidence returns a clear not-found response.

Cancel is restricted to the running subject and requires current Core cancel
permission. The worker stops subsequent cases and requests cancellation of the
current query. A cancellation request stops the batch; the current case still
records the actual Core outcome. A query that completes successfully before
Core accepts cancellation is not relabeled cancelled. Unaccepted cancellation
requests may be retried once per heartbeat until that query settles.
After a crash/restart, the last nonterminal run becomes interrupted
once its three-minute lease expires; it is never automatically replayed. Lease
renewal verifies continued ownership and cannot revive an expired lease. A
process mutex and a cross-process project lock remain held until the Core call
actually returns, including connection setup and cancellation. SQLite uses an
OS file lock in the control-store directory; PostgreSQL uses a dedicated
session advisory lock. They do not hold a SQLite write transaction or the
control-store transaction open while Core runs. An expired lease alone cannot
admit another run or Console configuration mutation while the Core call is
still live. Lock errors fail closed. Request `deadlineMs` is forwarded to Core;
it is not presented as a global wall-clock deadline for setup or compilation.

## Authenticated HTTP interfaces

All reads and case/dataset administration require project `console:admin`.
Event and health writes require managed project `trace:write`. Bootstrap
credentials cannot execute regression queries. Reads/writes are scoped to the
authenticated organization and current project, and Core-to-Agent associations
also verify subject ownership.

| Interface | Purpose |
| --- | --- |
| `GET /api/v1/traces` | Cursor pagination plus `limit`, `sourceSessionId`, `subjectId`, `createdAfter`, `createdBefore`, `status`, `hasIssues` |
| `GET /api/v1/traces/{id}/diagnosis` | Current versioned rules and evidence |
| `GET /api/v1/traces/compare?leftTraceId=…&rightTraceId=…` | Scoped structural comparison |
| `GET /api/v1/traces/by-core/{coreTraceId}` | Resolve a verified Core correlation to its project Trace |
| `POST /api/v1/traces/collector-status` | Submit the authenticated collector's bounded health report (`trace:write`) |
| `GET /api/v1/traces/collector-status` | Read project collector health and freshness (`console:admin`) |
| `GET /api/v1/diagnostics/core/{coreTraceId}` | Standalone safe Core phases, codes, versions and issue links |
| `GET/POST /api/v1/diagnostics/test-datasets` | List/register declared test environments |
| `PUT/DELETE /api/v1/diagnostics/test-datasets/{id}` | Update version/binding or remove registration |
| `GET/POST /api/v1/diagnostics/regression-cases` | List schema v2 plus legacy records, or create a schema v2 draft |
| `GET/PUT/DELETE /api/v1/diagnostics/regression-cases/{id}` | Read, revise with `expectedRevision`, or delete |
| `GET /api/v1/diagnostics/regression-cases/export?schemaVersion=2` | Export current reviewed records and legacy case records in a v2 bundle; omitting the parameter or using `schemaVersion=1` keeps the v1 export |
| `GET/POST /api/v1/diagnostics/regression-runs` | Latest 100 runs or start a bounded manual batch |
| `GET /api/v1/diagnostics/regression-runs/{id}` | Durable metadata-only run details |
| `POST /api/v1/diagnostics/regression-runs/{id}/cancel` | Request cancellation of the caller's own batch/query; poll the returned run until terminal |

Run creation and cancellation return the current run record directly with HTTP
202. A cancel response can still show `queued` or `running` while the worker and
Core settle cancellation. Poll the run detail endpoint while the status is
nonterminal; terminal run statuses are `completed`, `cancelled`, and
`interrupted`. Per-case results use `passed`, `failed`, `blocked`,
`inconclusive`, or `cancelled` and contain case/revision IDs, safe query/Core
IDs, timings, reason codes, environment metadata, issue/Trace links, and
assertion statuses only. Actual and expected result values are never retained.

Trace event schema v1 and Core RPC v1/v2/v3 remain compatible. Legacy case export
without `schemaVersion=2` retains its v1 shape. Legacy `agent_evidence` records
remain exportable and non-executable; old SQL cases need explicit v2 review.
Diagnostics schema v12 adds health storage, and the regression store has an
independent migration ledger for datasets, revisions, runs and leases.
