# Evaluation center v1

The Evaluation center runs a versioned question suite against a real Agent,
records deterministic checks separately from human answer review, and compares
the results with a retained baseline. It targets an explicitly enabled test
project and its published semantic revision. It does not deploy an old Core or
evaluate a draft automatically.

## Component ownership

The Console and Core own generic evaluation administration, immutable suite and
profile versions, project-scoped scheduling, scoring, evidence and comparison.
The independent sibling project `semarail-eval-runner` owns Codex CLI process
execution and its task-local MCP adapter. Core never starts a model process.
Existing golden-question scripts and frozen benchmark campaigns are retained.

The runner needs a locally installed and authenticated Codex CLI, a configured
model, and host-side aliases for managed test-role credentials. Neither browser
configuration nor suite imports may supply executable shell commands. The
grader's read-only identity is independent of the identity being evaluated.

Data fingerprints must use the host's independent read-only PostgreSQL test
connection and cover all registered tables, columns and duplicate rows. A row
count is not a content fingerprint. Computing it through the semantic layer
would conflate semantic changes with physical data changes. The runner's
`data_fingerprint` helper computes a repeatable-read raw-data checksum; its
database credential is excluded from child Agent environments and evidence.

## User workflow

1. Register a PostgreSQL test dataset and its version in the existing regression
   dataset registry. Use synthetic or approved test data. Register its fingerprint
   with the runner, configure role and scoring credentials on the host, and bind
   the dataset to the managed read-only scorer subject in Evaluation setup.
   Registration stores the subject ID and dataset version, never a credential.
2. Enable evaluation for that test project. Publish a suite and an Agent profile.
   Suite cases declare a question, actor, expected behavior, reference SQL or
   assertions, optional clarification replies and review criteria.
3. Review the planned question count, repetitions and target environment. Start
   a run. Defaults are two concurrent attempts, one repetition and 300 seconds
   per attempt. Model choice is explicit.
4. Inspect progress and per-case results. A fresh Agent handles each attempt;
   references and expected answers belong exclusively to the trusted grader.
5. Review final answers independently of automatic result checks. Set a complete
   run as the baseline, publish a semantic change in the test project, then run
   the same suite again and inspect repairs and regressions.

## Installation and host configuration

Build Core with `pnpm --filter @hejielijob/semarail-core test`, then pack it into
a new output directory using `npm pack --ignore-scripts --pack-destination
<directory>` from `packages/core`. Install that local archive with `npm install
--global <archive>`. Existing published or frozen archives must remain intact.
The evaluation preview is not an npm publication.

The exact local preview artifacts are in
`dist/evaluation-center-v1-candidate-4`. To install them on this workspace:

```powershell
npm install --global 'D:\xiaohe\wrenAI\semarail-trace-v2\dist\evaluation-center-v1-candidate-4\hejielijob-semarail-core-0.1.0-alpha.9.tgz'
python -m pip install 'D:\xiaohe\wrenAI\semarail-trace-v2\dist\evaluation-center-v1-candidate-4\semarail_eval_runner-0.1.0-py3-none-any.whl[postgres]'
```

These files retain the development version and have distinct hashes; they do
not replace the frozen archive at the root of `dist`.

Install the independent runner with Python 3.11+:

```powershell
python -m pip install 'D:\xiaohe\wrenAI\semarail-eval-runner[postgres]'
semarail-eval-runner configure --config eval-runner.toml
```

In Console Access Control, create separate managed service accounts for the
worker, read-only scorer and each test role. Bind the worker to a project-scoped
policy such as this (replace the project name):

```json
{"schemaVersion":1,"projects":["isolated-test-project"],"tools":["eval:worker"]}
```

The scorer and roles use datasource-bound read/query policies, including
`semantic:read`, `query:plan`, `query:execute` and `query:cancel`, with explicit
table, column and row grants. The scorer must be a different subject from every
test role and the worker. It must have only the read access needed for references;
the physical PostgreSQL fingerprint login must also be SELECT-only. Do not give
the runner the bootstrap or Console administrator credential.

Set the environment variable names generated in `eval-runner.toml`, add all role
aliases used by suites, and bind every registered test dataset under `[datasets]`:

```toml
[datasets.hr-enterprise-v1]
database_url_env = "SEMARAIL_EVAL_TEST_DATABASE_URL"
schema = "hr"
tables = ["regions", "departments", "employees", "compensation_history", "performance_reviews", "attendance_monthly"]
```

Keep credential values in the trusted host environment. The TOML stores aliases
and environment-variable names. Start Core and its HTTP MCP service with the
same test project and state directory. After setting the Core administrator
credential in the host environment, use separate terminals:

```powershell
semarail start --project 'D:\path\to\test-project' --state-dir 'D:\path\to\test-state' --port 48763
semarail mcp serve --project 'D:\path\to\test-project' --state-dir 'D:\path\to\test-state' --port 48764
```

Then start the independent worker using its scoped host credentials:

```powershell
semarail-eval-runner check --config eval-runner.toml
semarail-eval-runner run --config eval-runner.toml
```

`check` verifies CLI login, tool/cancellation capability, distinct managed
identities and physical data fingerprinting. No model is launched. The host
must already have the explicitly chosen model available. `run --once` handles
at most one attempt; ordinary `run` polls the project queue and respects its
configured concurrency. Communication retries submit the original completion
payload and do not invoke the model again.

The CLI uses [official non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode)
and task-local [MCP configuration](https://learn.chatgpt.com/docs/extend/mcp).
Ambient project instructions are excluded through the documented
[`project_doc_max_bytes` setting](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

## API notes

All payloads use schema version 1. Management and evidence endpoints require the
current project's `console:admin`; lease endpoints require its `eval:worker`.
Workers cannot read administrator reports or evidence using their worker grant.

- `/suites` and `/profiles`: draft CRUD; `/{id}/publish` freezes a revision,
  `/{id}/copy` creates a draft, `/{id}/export` exports a versioned envelope.
  `/suites/import` accepts `{schemaVersion:1,suite:{...}}` and `/profiles/import`
  accepts `{schemaVersion:1,profile:{...}}`; both create validated drafts.
- `GET /datasets` lists the existing test registry and evaluation scorer bindings.
  `PUT /datasets/{id}/scorer {subjectId}` explicitly registers the independent
  read-only scorer for that dataset version. Re-register after a dataset version
  or datasource change. Binding changes are blocked during active evaluations.
- `GET /preflight?suiteId=...&profileId=...` explains missing conditions.
  `POST /runs {suiteId,profileId,caseIds?}` freezes a complete schedule and
  environment. `POST /runs/{id}/rerun {caseIds?}` retains the source suite and
  profile snapshots while observing the current environment; it never silently
  upgrades to edited asset heads or changes the original attempts.
- Run details, `/cancel`, `/baseline`, `/export` and `/compare` expose durable
  progress, scores, manual decisions and environment differences. Per-attempt
  `/review` appends history. `/evidence` is an independently protected read/delete
  resource; expiration returns 410 while the report remains available.

The full prefix is `/api/v1/evaluations`. OpenAPI documents each route. Expected
results are limited to 500 rows per case and detailed evidence to 2 MB; incomplete
or bounded result previews cannot establish a passing score.

## Interpretation

`examples/evaluation/hr-smoke-suite.json` is an editable import example for the
existing HR schema. It expects the registered dataset ID `hr-enterprise-v1`,
host role aliases `hr_director`, `employee`, `compensation_admin`, and a published
salary rule with condition ID `salarySnapshot`. Adapt these bindings before
publishing; the example itself is not evidence of a passing model evaluation.

Tool success is not answer correctness. Reports distinguish execution status,
first-query correctness, final result correctness, authorization behavior,
observable business conditions, manual answer review and observed efficiency.
No hidden reasoning is collected. Missing token or Trace observations are
unavailable, not zero.

Unordered comparisons preserve duplicate multiplicities. Numeric tolerances
are explicit. Truncated results or unproven ordering are inconclusive. The
pass-rate denominator is the entire frozen attempt schedule, including timeout,
cancelled, interrupted and unstarted positions. Reruns create new records.

Strict comparisons require matching suite, data and scoring versions. Semantic,
Agent, prompt and permission changes remain visible as comparison factors.
Other comparisons are exploratory and cannot establish a regression. Human
review revisions do not change the original automatic score.

Detailed evidence is separate from ordinary metadata-only Trace storage, is
project-admin-only, and expires after 30 days by default. Deleting or expiring
evidence leaves scores, version summaries and explicit expiry markers intact.
Credentials are excluded from stored tool exchanges and reports.

## Acceptance ledger

The following gates and their original evidence are retained.
`docs/evaluation-center-audit.json` records the final requirement-level status.
Final acceptance completed on 2026-11-06 (Asia/Shanghai); all 64 objective
clauses are verified at the evidence scopes recorded in the audit.

- **Real HR campaign:** `.benchmark-data/evaluation-center/20261105-152709/acceptance-summary.json`
  passed all eight planned executions using the real PostgreSQL HR datasource
  and real Codex `gpt-6-luna` / low. Both rounds cover query, clarification,
  permission denial and a deliberately wrong expected result. These are
  integration cases with explicit SQL, not a natural-language accuracy benchmark.
- **Semantic regression:** baseline `21ba7d5f073744f88ca8540969693886`
  scored 3/4; candidate `3ec1af52bdce46afac6d53ea855de61b` scored 2/4.
  The intentional negative remained failed. Only the published semantic revision
  changed; the query became a regression, while clarification and denial stayed
  passed. Data fingerprints, suite and scoring version remained equal.
- **Real UI and evidence:** independent Playwright/Edge inspected the actual
  comparison and Core diagnostic. Proof is in
  `.benchmark-data/evaluation-browser/live-proof/live-browser-report.json` and
  `actual-semantic-regression.png`. Complete Agent Trace absence is explicitly
  labeled. Browser profiles are temporary and access the local fixture only.
- **One-click installed worker:** the real page started run
  `722d3511c7fc4ae1b0a678128cd06f75`; the installed runner daemon automatically
  claimed it and completed a real query, scoring 1/1. Its first UI verification
  hit a refresh timing error after execution had passed. The original failed
  report remains in `.benchmark-data/evaluation-oneclick/20261105-154810`;
  `oneclick-followup-report.json` verifies the same run and diagnostic without
  rerunning the model. The final run `90e1b957540c41c3abd01cb77a415650`
  passed directly through the page and installed daemon, including reference SQL
  under the dataset-registered scorer. Its original `oneclick-report.json` in
  `.benchmark-data/evaluation-oneclick/20261105-162628` passed without a follow-up
  or model retry; the frozen scorer matches the registered binding.
- **Controlled browser checks:**
  `.benchmark-data/evaluation-browser/scorer-proof/report.json` verifies actual
  scorer registration, suite and
  profile import/export, immutable copy, run preview/start, baseline comparison,
  manual review persistence, refresh/re-authentication and desktop/mobile views.
  This script uses a controlled worker; it is distinct from real-model proof.
- **Backend compatibility:** the full server suite passed 378 tests before the
  final parallel-round and scorer-registration additions. The final evaluation
  and existing regression suite passed all 58 tests; 39 are evaluation tests.
  The full Console suite passed 190 tests and type checking passed. Checks cover
  immutable versions, scoring, leases, restart, revocation, cancellation,
  independent reviews and expiry. The PostgreSQL acceptance script uses an
  actual scratch control database, then removes it; it does not fall back to
  SQLite. The existing synthetic HR database is read only.
- **Runner:** 46 tests passed with both optional CLI-isolation and PostgreSQL
  checks enabled; none skipped. This includes actual CLI requests to a local
  scripted provider, oracle/credential isolation, duplicate-aware ordering,
  raw-data fingerprint mutation, no model retry, and Windows process-tree kill.
  The scripted provider tests do not perform inference.
- **Core cancellation:** a real long-running PostgreSQL query was cancelled via
  the same MCP runtime and original subject; proof is
  `.benchmark-data/evaluation-browser/proof/actual-cancellation.json`.
- **Packaging:** the final Core archive and runner wheel passed all six checks
  in a fresh installation:
  `.benchmark-data/evaluation-distribution/20261105-163354/report.json`.
  These cover installation, private Python runtime initialization, evaluation
  APIs, packaged Console assets, supervised launcher lifetime and process-tree
  cleanup. Windows build sources use a short private temporary directory.
  On Windows, bootstrap version 4 waits for the child runtime rather
  than using execv, which could return early and orphan a listening server. A
  real-process regression test reproduces the old early exit and verifies the
  runtime exit code is propagated after the fix. All seven bootstrap tests pass.
  Final archives are in `dist/evaluation-center-v1-candidate-4`; their
  `source-verification.json` confirms 79 Core files and all five runner modules
  match current packaged source, and `sha256.json` identifies the artifacts.

Earlier failed model, browser and installation attempts remain preserved in
`.benchmark-data`. They are superseded only by separately recorded successful
checks, never relabeled or deleted. Natural-language answer reviews remain
pending until a human adjudicates them; deterministic scores do not imply a
human review has passed. This version does not add a model judge, scheduled
execution, CI deployment gate, draft deployment or another Agent runner.
