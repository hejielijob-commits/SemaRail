# business-trust-v1 benchmark harness

This package freezes and evaluates 30 synthetic HR questions against the
approved baseline and current checkout. The questions cover current active
headcount, salary snapshots, and quarterly performance. Each scene has ten
questions; five roles appear twice per scene. The language mix is 20 Chinese
and 10 English questions.

The harness keeps its canonical semantic SQL and expected physical results in
private run files. Workers receive one question, one actor identity, and the
authenticated MCP tools exposed for that actor. They never receive the
baseline/candidate assignment, canonical SQL, expected rows, or database URL.
The frozen schedule has 180 positions: 30 questions × two groups × three
rounds, in a fixed interleaved order. Missing, invalid, timed-out, and
unreviewed jobs remain in that denominator. A run is one-shot; do not retry or
replace a failed position in the same run.

The interrupted A–E campaigns remain historical evidence, including every
actual failed attempt and every missing scheduled position. E's original
180-position report and all 17 actual-session reviews were retained before
changing the harness. Its nonresumable protocol is not upgraded in place; see
the [E disposition](../../docs/business-trust-v1-invalid-campaign-e.json).

The broker periodically persists expired workers, including the last scheduled
position without a subsequent request. Per-job locks serialize terminal writes;
already terminal snapshots are immutable. MCP timeouts before the global limit
remain infrastructure failures, while responses arriving after the global limit
cannot be accepted as successful tool calls.

Recovery is a separately declared frozen protocol, `worker-ready-resume-v1`.
It applies only after all startup gates pass and a durable worker-ready
checkpoint exists. Bootstrap failures remain nonresumable: the coordinator
must not recreate subjects, keys or state to repair an incomplete bootstrap.
Existing runs without this protocol cannot acquire recovery support later.

On supported Windows runs, original authentication material is stored only as
current-user DPAPI ciphertext in the private recovery directory. Public
recovery evidence contains no credentials. A resume must reuse the same Core
state, Wren directories, identities, keys, semantic publication and frozen
inputs; occupied runtime ports, missing private material or inconsistent
history fail closed. Recovery never reissues a completed or consumed job,
re-exports worker prompts, resets a deadline or replaces a failed worker.
New recovery diagnostics preserve the original gate evidence and its hashes.
The separate PostgreSQL service must be healthy before coordinator recovery.

The Core Console and remote MCP process can hold different in-memory index
objects even when they share persisted index files. Worker-ready and resume
bindings must observe the authenticated MCP retrieval boundary used by the
workers. A live Context v2 response must report a ready hybrid index at the
current published project revision. A stale or missing response is never
accepted, and historical startup evidence cannot replace this live check.
Recovery also pins the persisted index file set privately and checks it before
and after live observations, so a read that silently rebuilds an index cannot
hide changed recovery state.

## Prerequisites

### Required knowledge delivery

Context v2 is a bounded retrieval slice; a model appearing in it does not
prove all necessary columns were delivered. Before any formal Agent dispatch,
the trusted host must derive required model/column references privately from
the canonical AST and verify actual authenticated delivery through Context or
the same actor's `semarail_list_models` catalog. Context and catalog must refer
to the same project revision. Authorization still filters both responses.
Denial cases must not require delivery of a forbidden model or column.

Coverage evidence contains hashes, counts, fallback status and reason codes,
without canonical SQL, required-field mappings, raw context or private policies.
A missing field in both authorized responses, an ambiguous AST reference,
revision drift or a failed authenticated call blocks the campaign. Workers
receive only public questions and tool schemas; they can use the authorized
catalog when the bounded Context lacks a needed field.

The repository's `.venv` should contain the pinned Wren runtime, MCP client,
PostgreSQL driver, SQLGlot, and hybrid retrieval dependencies. The baseline
checkout and the archive/Git canonical-byte source manifest must already exist
under `.benchmark-data/business-trust-v1/baseline`. The harness requires
`baseline-source-manifest-v2.json`; the legacy v1 raw-worktree manifest remains
immutable and is not accepted as a campaign source manifest. The generated synthetic dataset
is read from the sibling `dsh-wren-data-agent` checkout. Configure the
PostgreSQL URL in the environment variable `BUSINESS_TRUST_DATABASE_URL` (or
pass another environment variable name with `--database-url-env`). The URL,
password, and actor API keys are never written to benchmark evidence.

Run the focused unit tests first:

```powershell
node scripts/business-trust-v1.mjs test
```

The root repository's native PostgreSQL helper owns cluster lifecycle. The
benchmark only connects to the configured database and checks its host, port,
and database name against the frozen manifest.

Core hybrid retrieval uses the cached
`paraphrase-multilingual-MiniLM-L12-v2` checkpoint at revision
`e8f8c211226b894fcb81acc59f3b34ba3efd5f42`. Preflight and freeze verify the
revision and SHA-256 of every cached checkpoint file. Benchmark Core children
run with Hugging Face and Transformers offline mode enabled; a missing or
changed cache fails closed.

## Freeze and run

Use a new run ID for each official campaign. Freeze reads the private database
URL from the environment, materializes canonical physical rows, snapshots the
corpus, worker prompt, evaluator settings, source hashes, role policies,
semantic projects, and 180-position schedule, and writes a hash-checked
manifest. It never sends oracle data to workers.

The candidate source manifest covers the Console server and UI, contract source
and tests, Sidecar, packaged Core bin/runtime/Python/web payload, workspace and
package configuration, and the benchmark harness. When `dist` contains the Core
tarball, the candidate manifest also records its package name, version, and
SHA-256. The approved baseline v2 source and archive manifests remain immutable.

```powershell
node scripts/business-trust-v1.mjs preflight --database-url-env BUSINESS_TRUST_DATABASE_URL
node scripts/business-trust-v1.mjs freeze --run-id <unique-run-id> --database-url-env BUSINESS_TRUST_DATABASE_URL
node scripts/business-trust-v1.mjs verify-freeze --run-dir .benchmark-data/business-trust-v1/runs/<unique-run-id>
```

To opt a new Windows campaign into worker-ready recovery, add
`--recovery-protocol worker-ready-resume-v1` to `freeze` or `preaccept` before
the run is created. After an independently confirmed stop, use the separate
`resume --run-dir <same-run-directory>` command. An observation timeout alone
is not proof that the old process stopped. Resume refuses occupied ports and
does not terminate or adopt an unidentified process.

Before the official freeze, run the real host acceptance against an isolated
`preacceptance_only` run. It snapshots the same frozen inputs and materializes
the physical oracles, but its manifest permanently disables worker dispatch:

```powershell
node scripts/business-trust-v1.mjs preflight --database-url-env BUSINESS_TRUST_DATABASE_URL
node scripts/business-trust-v1.mjs preaccept --run-id preaccept-<unique-id> --database-url-env BUSINESS_TRUST_DATABASE_URL
node scripts/business-trust-v1.mjs serve --run-dir .benchmark-data/business-trust-v1/runs/preaccept-<unique-id>
```

The historical v1 comparison allows decimal tolerance only for columns whose
frozen expected values are finite numbers. This accounts for v1 Core responses
that serialize `BIGINT` cells as strings; text, boolean, null, and non-finite
values remain strict.

When the host prints `BTV_HOST_READY`, acceptance has passed: it ran the
historical 60-question regression once per group (120 authenticated executions),
five canonical business-check accepts plus twelve mandatory counterexamples
against the candidate (nine conflicts and three unsupported SQL shapes, including
governed-query rejection with and without a preparation ID), all 30 canonical
MCP cases against each group, and independent PostgreSQL checks for all 20
result-producing cases. For every denial case, acceptance calls
`semarail_plan_query` with the frozen canonical SQL and requires the exact
`POLICY_DENIED` code. It then calls the governed-query MCP boundary separately:
an actual `POLICY_DENIED` must include an explicit zero-row count, or a genuinely
non-ready preparation may be followed by `CLARIFICATION_REQUIRED` with an
explicit zero-row count. A blocked preparation alone never proves policy
enforcement. The safe report records each stage's status and error code, whether
the row-count field was present, its value when present, and whether result-row
fields were present and empty; it stores no SQL or result rows. Stop that preacceptance
host with Ctrl+C; the one-shot preacceptance run cannot dispatch workers or
become an official 180-position campaign. Use a fresh official run ID after
candidate source and configuration are stable.

Start the coordinator in one terminal. Before it announces `BTV_HOST_READY`,
it runs the historical 60-question regression on unmodified project copies,
then starts fresh baseline and candidate instances with the same checked
business project. It verifies safe Context v2 with the hybrid index, candidate
headcount/snapshot/quarter/trend checks, all 30 canonical MCP results, and the
independent physical PostgreSQL results. The historical regression and checked
business project use different state and Wren directories.

```powershell
node scripts/business-trust-v1.mjs serve --run-dir .benchmark-data/business-trust-v1/runs/<unique-run-id>
```

Only after the coordinator reports ready, run the one-shot external worker
campaign in a second terminal:

```powershell
node scripts/business-trust-v1.mjs run-workers --run-dir .benchmark-data/business-trust-v1/runs/<unique-run-id>
```

The runner fixes the requested model to `gpt-6-luna` with `max` reasoning,
limits concurrency to two, allows up to 12 MCP calls per job, sets a 300-second
deadline, and does not retry. Each job gets a fresh empty worker directory and
an ephemeral Codex process. It uses `--ignore-user-config` and `--ignore-rules`
so user instructions and repository rules do not affect the worker prompt.

An app-session supervisor can use the same coordinator transport without the
CLI runner. After `serve` is ready, export the frozen prompts, then dispatch
each prompt as a fresh app worker session in the saved interleaved order with
no more than two active sessions:

```powershell
node scripts/business-trust-v1.mjs export-app-jobs --run-dir .benchmark-data/business-trust-v1/runs/<unique-run-id>
```

For file-backed app prompts, add `--bootstrap-file-prompts`. Export then
returns a short launcher directory and a public prompt directory outside the
candidate checkout. Dispatch the matching launcher as the app's initial
message; it permits one exact read of the adjacent public prompt before the
worker transport begins:

```powershell
node scripts/business-trust-v1.mjs export-app-jobs --run-dir .benchmark-data/business-trust-v1/runs/<unique-run-id> --bootstrap-file-prompts
```

For schema 3, use `--bootstrap-stored-transport` instead. Its one-time launcher
reads the public prompt, stores the exact frozen transport configuration for
that worker session, and preserves the executor result metadata. Later calls
load the fixed configuration rather than repeating executable and client
paths. Review reconstructs the configuration, public prompt and launcher;
changing the store key, paths, nonce, port, operation or JavaScript skeleton
fails closed. Equivalent JSON data whitespace and Unicode escapes remain
valid; duplicate keys, non-finite numbers, prototype mutations and additional
JavaScript are rejected. Schemas 1 and 2 keep their explicit review paths.

Review reconstructs every selected public prompt from the frozen
`worker-prompt.md`, frozen job nonce, and recorded worker directory, Python,
client, and broker port. It checks those bytes and the launcher against the
reconstruction, so changing a prompt and its recorded hashes together still
fails review. The JSONL receipt proves that the app worker read the public
prompt; the app's encrypted initial message is not exposed in JSONL, so
`sentInputIndependentlyByteVerified` remains `false`.

Every `begin` response includes the same versioned `clarificationProtocol` for
both groups and all questions. Its conversation topics are `timeRange` (date,
snapshot, quarter or range) and `granularity` (grouping or aggregation level).
These are coordinator routing keys. Core preparation keys come separately from
the published rule text or `semarail_prepare_query` clarifications. The protocol
does not reveal which question has an available clarification or its answer.
Corpus validation rejects clarification routing keys absent from this public
contract. A protocol or prompt change requires a new frozen campaign; historical
records are never rescored with the changed instruction.

Before using a fresh app worker for business questions, verify the app
transport independently against a disposable loopback fake broker. This smoke
creates no benchmark run, uses no HR data, and cannot be imported into campaign
results. The fake broker delays its one context response long enough to exercise
executor session metadata. The prompt permits only an empty read-only poll of the
session ID returned in that exact call's top-level result metadata; the verifier
correlates the poll to that call and rejects IDs copied from command output or
another source. Prepare one exact one-shot prompt and start the broker in a
separate terminal:

```powershell
node scripts/business-trust-v1.mjs transport-smoke prepare --smoke-id <unique-smoke-id> --port 48995
node scripts/business-trust-v1.mjs transport-smoke serve --smoke-id <unique-smoke-id>
```

To exercise the same file-backed bootstrap, add
`--bootstrap-file-prompts` to `transport-smoke prepare` and dispatch its
`one-shot-prompt.md` launcher. Smoke verification rebuilds the expected public
prompt from its prepared transport state and rejects coordinated prompt/hash
changes.

Dispatch the generated `one-shot-prompt.md` as one fresh `gpt-6-luna` / `max`
app worker. After it finishes, import/audit its exact session JSONL and stop
the fake broker with Ctrl+C:

```powershell
node scripts/business-trust-v1.mjs transport-smoke verify --smoke-id <unique-smoke-id>
```

The verifier requires the fake broker's `begin` → `semarail_get_context` →
`final` sequence, a completed provenance-bound empty poll, the broker-recorded app
thread ID, one matching `session_meta`, and observed model/reasoning in that
session's JSONL. A failed or incomplete smoke is not campaign evidence and must
use a new smoke ID for another transport-only attempt.

Each prompt contains only its public job challenge, an empty external worker
directory, and the approved loopback transport. The first `begin` request must
include that job's unique nonce. The trusted transport also takes the app's
`CODEX_THREAD_ID` from its own process environment and adds the ID as a
separate loopback header; the worker cannot set it in request JSON. After a
long-running approved transport call, the trace auditor accepts only an empty
`write_stdin` poll bound to that call's returned top-level `session_id`, and
requires that poll to close the same session before another command. After a
worker finishes, import its JSONL by scheduled job ID. The importer obtains the
thread ID from the completed broker record and resolves only that thread's
unique session JSONL whose path contains that thread ID. An explicit trace path
must identify the same file. Duplicate JSON keys or a missing or mismatched
`session_meta.id` fail closed before the bytes are imported:

```powershell
node scripts/business-trust-v1.mjs import-app-trace --run-dir .benchmark-data/business-trust-v1/runs/<unique-run-id> --job-id job-001
```

The imported JSONL must contain the actual turn context and the exact audited
`functions.exec` transport call. A dispatch request or worker's selected model
does not count as observed model evidence. The review fails closed when the
model or reasoning effort is missing or differs from the frozen request, so
app runs with unobservable settings cannot pass promotion.

The JSONL reviewer validates the visible tool calls against the frozen
transport and limits, then records an independent answer judgment. Review is
append-only; one invocation reviews one scheduled position:

```powershell
node scripts/business-trust-v1.mjs review --run-dir .benchmark-data/business-trust-v1/runs/<unique-run-id> --job-id job-001 --trace .benchmark-data/business-trust-v1/runs/<unique-run-id>/traces/job-001.jsonl --answer-correct true --reason "The answer matches the verified result and scope."
```

Repeat with each job ID and an evidence-based `true` or `false` judgment, then
write the report:

```powershell
node scripts/business-trust-v1.mjs summarize --run-dir .benchmark-data/business-trust-v1/runs/<unique-run-id>
```

The private report contains run-level evidence. The separate
`console-feedback-*.json` export contains only version hashes, counts, and
aggregates; it does not include question text, SQL, raw answers, rows, or trace
bodies. Promotion requires complete reviewed evidence, observable matching
model and reasoning settings, clean role denials, at least two candidate round
wins, first-SQL correctness no worse than baseline, higher final-answer
correctness, no increase in silent errors or omissions, no permission leaks,
and deterministic candidate results across three rounds.

`firstSQL` eligibility is frozen by oracle type: result-producing cases are
included even when a worker times out or emits no SQL; refusal and unsupported
complex-SQL cases are reported as not applicable. The first candidate is the
first semantic SQL supplied to plan, prepare, or execute. It counts correct
only when that exact SQL is later executed and its result matches the oracle.
An unexecuted candidate is reported as unverified and does not add a correct
count. Refusals can pass the denial outcome without issuing SQL; clarification
questions still require a final answer after the coordinator supplies the
requested condition.

Codex model settings and tool calls must be observable in the actual JSONL.
Missing or mismatched evidence fails review and blocks promotion. A standalone
CLI smoke attempt is outside the official run and is not benchmark evidence.
