# Business trust v1 acceptance ledger

The complete alpha.9 campaign is accounted for in [the final results](business-trust-v1-final-results.md).
All 180 positions were attempted and independently reviewed; promotion gates failed,
so the feature remains opt-in and Context defaults are unchanged. The records below
preserve the earlier acceptance history. Their counts and candidate versions are
historical; current alpha.9 evidence and the final comparison are linked from the final results.

## Historical technical evidence

| Requirement | Current evidence | Limit |
| --- | --- | --- |
| Existing Console behavior and new verification/feedback integration | Full Python Console suite after opaque-ID and current-case fixes: 334 tests passed | Technical behavior; does not replace real business queries or Agent outcomes |
| Console reconstruction, bilingual controls and existing workbenches | Full web suite: 17 files, 152 tests passed | Does not replace a real published repair case |
| Existing Wren adapter and Sidecar behavior | Full Sidecar suite: 145 tests passed | Genuine business-query acceptance remains a separate gate |
| Independent evidence protocol and old RPC contracts | Contract suite: 20 tests passed; contract build passed | Runtime parser and exported JSON Schema are independent from query-result protocols |
| Strict stored evidence fields and HTTP access boundary | Eleven evidence tests plus three HTTP tests passed, including malformed fields, profile changes, publication fencing, admin scope and caller-supplied pass rejection | Included in the final full suite |
| TypeScript compatibility | Workspace typecheck passed | Recheck affected packages after additional changes |
| Standalone Core distribution contents | Six CLI tests, Core build, 76-file package verification; alpha.7 archive/checksum verified and offline npm installation/exact version/help passed; all 35 Console runtime files match source in the archive | Initial alpha.6 attempt with stale hardcoded CLI version was rejected and retained separately; corrected CLI reads the package manifest |
| Real PostgreSQL storage and authorization | Both control and business storage acceptance passed, including preparation migration v5 and evidence migration v3 | Covers storage, migrations, ownership, permission revocation, same-ID profile changes, staleness, expiry and redaction; not business SQL or Agent quality |
| Published execution bindings | Final runtime/preparation focused suite: 45 tests passed; included in full 330 suite | Caller, SQL, datasource profile, publication activation, row-filter/method drift, rollback and cancellation-preserving execution fencing |
| Actual repair lifecycle | Retained isolated salary/performance acceptance: actual source feedback, changed assets, fix publication, two linked current cases, genuine SQL passes, two external workers, independent PostgreSQL matches and approved same-version summaries | Both resolutions verified; fresh store reconstruction preserves all five passing checks. Separate from the formal comparison |
| Historical deterministic queries | Final preaccept run: 120/120 passed, 60 per group, through real Core/Wren/PostgreSQL | Original failed runs retained; comparison adapts lossless numeric strings only for finite numeric frozen oracle columns |
| Authenticated delivery | Final preaccept run: 60/60 Context v2 calls passed with hybrid backend, required fields and five-role filtering | Separate from Agent quality |
| Declared business checks and counterexamples | Final preaccept run: 17/17 passed, including five positive checks, nine conflicts and three unsupported SQL shapes; unsupported shapes rejected at governed execution with and without preparation IDs | Does not prove natural-language intent or Agent answer quality |
| Independent physical result and permission checks | Final preaccept run: canonical gate passed all 30 questions, with 20 result-producing cases checked against PostgreSQL and 18 actual permission-denied plans plus 18 zero-row governed execution boundaries | Denials require exact frozen POLICY_DENIED at plan; after genuinely non-ready prepare, execution may separately report CLARIFICATION_REQUIRED with explicit zero rows. Missing counts cannot establish zero |
| Actual external repair worker sessions | Two real worker session audits passed with observed gpt-6-luna/max, exact broker-bound six-call sequences and one provenance-bound empty exec-session poll; strict native Wren lineage and independent physical results matched | Root reviewed actual answers, units, snapshots and permission scope before administrator approval; seven adversarial transport tests and eighteen Oracle reviewer tests passed, with fixtures from both actual native queries |
| External model/tool observability | Independent smoke c and delayed smoke `btv-session-poll-20261014-r2` passed with observed gpt-6-luna/max and exact begin/tool/final transport; delayed smoke required exactly one provenance-bound empty poll | Earlier failed/interrupted smoke attempts retained and excluded; formal campaign results are tracked separately below |

Detailed local test logs are retained under the ignored
`.benchmark-data/business-trust-v1` directory. They are implementation evidence,
not replacement outcomes for any scheduled Agent job.

The later `preaccept-20261014-denial-boundary-poll-v1` process was interrupted
after its 120 historical executions passed, before the remaining gates produced
reports. It is not a completed preacceptance. A new preacceptance ID is required.
Root-owned native PostgreSQL and test hosts use hidden processes launched through
the Windows process provider; hosts own a Windows kill job for their children.
The delayed smoke's worker
identity was checked before stopping it, its listener closed, and the separately
owned PostgreSQL instance remained available. This is service-lifecycle evidence,
not Agent-quality evidence.

The complete preacceptance `preaccept-20261014-owned-wmi-final-v1` reached
`BTV_HOST_READY`. Safe gate metadata and report hashes are exported in
[the preacceptance record](business-trust-v1-preacceptance.json). Its host was
stopped only after process identity verification and all its listeners closed;
the separately owned PostgreSQL instance remained available. The formal campaign
`formal-20261014-business-trust-v1-a` is frozen with manifest SHA-256
`458b85b40123991d433c37df7303b1d9f9e76306118edba9321eb99a0cef7977`;
was stopped as an invalid experiment after a coordinator terminal-state bug and
two supervisor prompt-path deviations were observed. All 180 positions remain
in its retained denominator; original records are unchanged. Its two begun
workers and later erroneous timeout transition are documented in
[the invalid-campaign record](business-trust-v1-invalid-campaign-a.json).
No answer-quality comparison is claimed from that run. The coordinator now
expires only running jobs; two new focused state-machine tests preserve terminal
record bytes/hashes and verify a running timeout is captured once. All 39 harness
tests pass. The full replacement campaign `formal-20261015-business-trust-v1-b`
is frozen with manifest SHA-256
`0c5307808ea355ec93a72bfc16c9c01beee2f7b62bfb8b28c673a7fe0e151c0c`.
It retains the same questions, private Oracles, model/reasoning, common worker
instructions and scoring; only the coordinator terminal-state fix and its tests
changed. The fresh host reruns all authenticated gates before dispatch. Its full
180-position execution was stopped as invalid when root discovered that BTV-16
was declared Chinese but its question text was English. The frozen text corpus
had 19 Chinese and 11 English questions despite balanced metadata. All 180
positions remain in its retained denominator: four timeouts, one complete
submission and 175 missing positions. Five broker-bound sessions were imported
and independently reviewed; job 006 was interrupted before begin. Four workers
changed the fixed client path and failed strict audits. Job 005 passed its call
audit but failed answer review because it did not explain the HRBP permission
restriction and omitted required scope/refusal tags. None is comparison evidence
from a valid experiment. The original corpus, records, traces and five reviews
remain unchanged in [the invalid-campaign B record](business-trust-v1-invalid-campaign-b.json).

BTV-16 is now translated to Chinese without changing its actor, scope, private
SQL or Oracle. Corpus validation also checks the question script and rejects
wrong-language text or empty questions despite balanced labels. Two adversarial
tests cover these failures; all 41 harness tests pass. Root read all 30 question
texts and confirmed the actual 20 Chinese / 10 English mix. The full replacement
`formal-20261015-business-trust-v1-c` is frozen with manifest SHA-256
`0320b8a9f31ab44c10ff0375958dd825d6aaddee6b13653822dff2df888d8ac2`.
Model, reasoning, policies, data, scoring, common prompt, canonical results and
schedule match B. Fresh rules-index timestamps differ, while rule IDs, content
and checks are identical. Its host stopped at the canonical Oracle startup gate:
120 historical executions, 60 Context checks and 17 execution checks passed,
but candidate result-producing queries could not persist business evidence.
No worker export or dispatch occurred; all 180 positions remain missing. The
[C startup-failure record](business-trust-v1-invalid-campaign-c.json) preserves
the original report and record hashes. The report's joint-failure fallback also
labels successful groups as `RESULT_MISMATCH`; that label alone does not prove
a baseline result mismatch. Historical report bytes remain unchanged.

Read-only replay isolated a generated datasource ID beginning with a hyphen.
Evidence validation had incorrectly required SQL identifier syntax for opaque
API IDs. The corrected validator accepts generated IDs beginning with a hyphen,
digit or underscore, while retaining strict semantic references and rejecting
sensitive or path-shaped values. The full Console suite passes 332 tests and
real PostgreSQL storage acceptance passes after the fix. Fresh authenticated
query results and a new frozen campaign are still required.
The deadline audit also found that the last idle worker could retain a durable
`running` record until another request. The broker now persists idle expiries
periodically, retries jobs whose locks are busy, preserves terminal snapshots,
and rejects MCP responses arriving after the global deadline. Focused checks
and all 46 harness tests pass, including independent Oracle failure attribution.
All 180 genuine positions and independent reviews remain required.
An independent product audit found that a previous case revision could still
satisfy feedback verification after its case was edited. The current-case guard
now makes the old case/SQL chain stale and its Agent evidence unverified; editing
rejects relinking with 409 and physical deletion with 404. Both negative cases
and the 14-test feedback module passed; the scoped current-head query also passed
on real PostgreSQL. The integrated Console suite passed all 334 tests after this fix.
The fresh isolated run `preaccept-20261016-alpha7-v1` is frozen with manifest
SHA-256 `f23e24070c973922dcd0db79f98c57c0a3d299b1f54041f044f4ca421f9fd4b9`.
Freeze verification and authenticated acceptance passed: 120 historical
executions, 60 Context calls, 17 execution checks and the 30-question Oracle
gate, including 20 physical result checks and 18 permission plan/execution
boundaries. Safe outcomes and report hashes are in
[the alpha7 preacceptance record](business-trust-v1-preacceptance-alpha7.json).
Its owned host was stopped after identity verification; all benchmark listeners
closed and the separately owned PostgreSQL listener remained live. Worker
dispatch is permanently disabled for this preacceptance-only run.

The new official run `formal-20261016-business-trust-v1-d` is frozen with
manifest SHA-256 `856b47d6e39ebcab9404e4eef4d1234039548066baebc432b8f79b65ffd95f5e`.
Baseline, corpus, common prompt, native results, role policies, model, runtime,
data and schedule match C. Candidate source changes cover the recorded fixes;
the checked project differs only in rules-index timestamps. Its fresh owned
host passed all authenticated gates and reached `BTV_HOST_READY`: 120 historical
executions, 60 Context calls, 17 execution checks and all 30 Oracle cases, with
20 physical checks and 18 permission plan/execution boundaries. All 180 worker
prompts were exported once. This run was subsequently stopped as invalid after
the job 003 supervisor confirmed changing the frozen transport path and poll
wrapper while manually copying its spawn message. Its three actual sessions
all began successfully, then used an absent v1 client path and produced no
context, SQL, result or final answer. All three reached their original deadlines,
were imported once and independently reviewed as unsuccessful with failed call
audits. All 180 positions remain: three timeouts and 177 missing positions.
Original snapshots, traces, reviews and aggregate report hashes are retained in
[the invalid D record](business-trust-v1-invalid-campaign-d.json). Its owned host
was stopped after identity verification; benchmark listeners closed and the
separate PostgreSQL service remained live. The earlier ledger observation that
job 001 did not begin was incorrect; an append-only correction preserves the
actual versioned broker evidence and the original observation. No comparison or
promotion conclusion is claimed. Encrypted supervisor call storage does not
independently prove exact sent-message bytes for jobs 001 and 002.

The replacement transport passed 63 harness tests (one Windows symlink-creation
skip, supplemented by an actual directory-junction rejection) and a new delayed
external app smoke. The fresh worker read its assigned public prompt with one
fixed audited call; the actual executor output matched the independently rendered
full prompt. The real export-to-review integration and outer-JavaScript-newline
regressions are covered. Launcher-message bytes remain unobservable and are not
claimed verified. [The bootstrap acceptance record](business-trust-v1-bootstrap-acceptance.json)
retains the source, test and actual-session hashes. Final-source preacceptance
passed all four gates in [its safe record](business-trust-v1-preacceptance-bootstrap.json).
The fresh official run `formal-20261018-business-trust-v1-e` has the same candidate
source manifest and passed its own four startup gates: 120 historical executions,
60 Context calls, 17 execution checks and all 30 Oracle cases, including 20
physical checks and 18 permission plan/execution boundaries. All 180 public
prompts were exported once. Fresh worker dispatch is authorized in the frozen
order with at most two active sessions. No position from D is reused in the new
campaign; individual outcomes and independent reviews remain outstanding.

Current product code also reopened isolated copies of the two genuine repair
cases after the current-case guard change. Both cases still derive verified with
all five checks passing; all 57 historical source files remain byte-identical.
[The reconstruction record](business-trust-v1-current-repair-reconstruction.json)
explicitly limits this to store reconstruction through the existing trusted
subject, rather than a new authentication, SQL execution or Agent evaluation.
The [interim requirement audit](business-trust-v1-requirement-audit.json) maps
technical evidence separately from the still-open full comparison and final
delivery audit.

## Formal E review and reporting boundaries

[The safe progress snapshot](business-trust-v1-campaign-progress.json) records
actual spawn returns, terminal records, imported traces and independent reviews.
It retains all 180 scheduled positions and is not the final comparison report.
The pre-spawn job 004 ledger digest transcription error was preserved with an
append-only root correction. The public files matched their recorded hashes,
the worker directory was empty and the position had not begun; no worker retry
or campaign replacement resulted from that metadata correction. Later request
metadata is computed from the actual files by a supervisor-only helper.
Reservations include already spawned workers that have not yet reached `begin`,
as well as running broker records; a preventive guard stopped job 013 before
its first spawn while both earlier positions were reserved. Existing workers
and their original deadlines continued unchanged.

The frozen evaluator remains unchanged. Its `omission` flag measures incomplete
runs or missing answer/scope fields. Missing required public condition tags
separately prevent a deterministic final-answer pass. Neither flag alone proves
whether the prose preserves the business metric, time basis, unit and permission
scope. Independent content reviews therefore record those business assertions,
critical business omissions and actual disclosure observations separately, bound
to the same terminal-record and imported-trace hashes. These supplemental
judgments use the original frozen question requirements and do not replace the
primary scores, silently reinterpret tag values or repair a worker submission.

The overall final-answer denominator is 180 (90 per group). The frozen
first-SQL applicability rule covers the 20 result-producing questions across
both groups and three rounds: 120 applicable positions, 60 per group. The 54
permission-refusal and six unsupported-query positions remain in the overall
design and are explicitly not applicable to first-SQL accuracy. Missing,
timed-out and audit-failed applicable positions remain in its denominator.
Permission and safe-refusal outcomes are reported separately rather than being
counted as successful SQL executions.

For example, a numerically and semantically correct salary narrative with
`key=value` condition strings can pass independent prose review while failing
the frozen exact-tag requirement. A refusal with no result rows can still fail
business review if it misstates a permission restriction as an unknown scope.
The final report must distinguish these cases from missing answers and transport
audit failures, and show both the frozen omission measure and independently
reviewed critical business omissions. A correct prose review cannot turn a
failed transport audit into eligible campaign evidence.

Job 013 subsequently ended before its first valid broker request. Its actual
unique app session was located through the session's agent path and controller
parent ID, with matching observed model/settings, a completed app turn and the
canonical public-file receipt. The only subsequent command used the wrong client
path and exited nonzero. Its original missing broker snapshot remains unchanged;
no broker identity or official app import was invented. The actual raw session
is retained separately as detached failure evidence, with a failed external
review and a hash-bound root disposition releasing the ended reservation.
Job 014's initially missing observation later became a real running broker
record; it followed the normal timeout/import path. An actual-case negative
check rejects treating that broker-bound session as an unbegun failure.

Independent review completion counts must be read separately from the frozen
`reviewPending` eligibility flag: the frozen loader also leaves that flag set
when a completed review has a failed call audit. The supplemental report binds
each independent judgment to its actual record and trace hashes, and reports
detached failures explicitly. It also reports observed app-turn duration from
actual `task_complete.duration_ms` metadata separately from the unchanged broker
duration; unknown or ambiguous duration observations remain unknown.

## Outstanding acceptance gates

### Interrupted E campaign disposition

The later interruption supersedes the earlier in-progress E status above.
The owned coordinator and PostgreSQL processes were authoritatively absent,
and their listeners were closed. The frozen coordinator rejects restarting a
run with execution records or resetting existing Core state. Its actor and
administrator tokens existed only in the lost process memory. No startup
guard was bypassed, no identity was invented, and no worker was replayed.

[The retained E disposition](business-trust-v1-invalid-campaign-e.json) binds
all 180 original positions, all 17 actual app sessions, 17 independent content
reviews, 16 official trace imports and one detached pre-begin failure. Latest
statuses are four complete, twelve timeout and 164 missing, including the
unbegun failed position. The 163 positions never spawned remain missing.
Four call audits passed and thirteen failed. The unchanged frozen evaluator
produced a full-denominator report; [its safe summary](business-trust-v1-interrupted-e-summary.json)
does not establish a valid completed comparison or promotion.

Job 017's original running snapshot is unchanged. Once its actual app turn was
confirmed terminal and its original wall deadline had elapsed, the unmodified
frozen `Host.expire_deadlines` appended a timeout snapshot. Its `endedAt` is the
time of delayed deadline persistence, not an inferred service termination time.
The local certificate binds the original start, prior snapshot hashes and
actual source session. No service was started for this disposition.

Before harness changes, all 277 frozen candidate source files were preserved
in a local archive and individually hash-verified against the E manifest.
Historical reports, traces and snapshots remain retained. E public-prompt
receipts were verified independently, while initial encrypted-message bytes
remain unobservable. Subsequent worker substitutions of the absent v1 client
path remain actual failed attempts and are distinct from D's supervisor
transcription error.

The original Context gate verified model shape and policy filtering, but did
not prove every required column was delivered. BTV-07's bounded Context omitted
`job_title`; this alone does not establish absence from the full authorized
model catalog. A future gate must verify required fields through actual
authenticated tool delivery and make any catalog fallback explicit. Fresh
transport and recovery protocols require focused tests and real process/app
smokes before a new full campaign. E outcomes are never replaced by selected
retries. Default Context and explicit opt-in behavior remain unchanged.

### Stored transport verification

The schema-3 transport reads its public prompt once and stores the fixed
transport configuration for that app session. Subsequent calls load that
configuration, so the model does not repeat executable/client paths on each
operation. The audit reconstructs the frozen configuration and JavaScript
skeleton; request JSON may use equivalent whitespace or Unicode escapes,
while extra code, prototype keys, duplicate keys and non-finite numbers fail.
The shared task instructions also clarify exact public condition tags and
authorized-catalog fallback when bounded Context lacks a required field.

The first real smoke completed the broker sequence, but its audit failed:
the new verifier incorrectly expected nested executor metadata. The actual
JSONL showed flat metadata, disproving the unit fixture assumption. Its
actual session, original six source files and negative disposition remain
retained; it was not relabeled passing after the fix.

A fresh second smoke, `transport-v3-real-20261018-b`, passed with observed
`gpt-6-luna` / `max`, the exact begin/tool/final sequence, one provenance-bound
empty poll, and a matching public-prompt receipt. Its trace SHA-256 is
`ce44c15d48db17db229adc5f1e231ad043f04d76fe92e88770fa3d74bcba6986`.
Both fake brokers were stopped after owned-process identity checks. Fixed
transport suite completed 27 tests with one Windows symlink-creation skip.
[The safe transport record](business-trust-v1-stored-transport-acceptance.json)
binds sources, tests and both actual smoke outcomes. These smokes are excluded
from the HR campaign and establish transport behavior only. Initial encrypted
launcher bytes remain unobservable, so the corresponding independent-byte
verification flag remains false.

| Gate | Evidence required before completion |
| --- | --- |
| Three scene counterexamples | Complete in the final full preaccept run; safe outcomes and hashes retained |
| Real repair lifecycle | Complete for both retained scenarios; safe durable metadata is exported in [the lifecycle acceptance record](business-trust-v1-lifecycle-acceptance.json) |
| Fixed comparison | Frozen baseline/candidate/data/roles/model/settings/prompt/scoring, 180 interleaved one-shot positions, raw traces and independent review |
| Promotion | Complete audit, stable observed settings, zero unauthorized disclosure, no first-SQL regression, overall final-answer improvement and at least two round wins, no extra silent errors/omissions |
| Release | Installable candidate archive/checksum, interface/configuration documentation, incremental migrations/rollback notes and final report |

Preacceptance runs and transport smoke tests are separate from the official
campaign. Missing model observability blocks promotion; requested settings must
not be substituted for observed settings. Every official position remains in the
denominator, including timeout, missing, invalid and unreviewed outcomes.

### Worker-ready recovery acceptance

The first recovery preacceptance passed its startup gates but failed before
saving a checkpoint. Its state and frozen sources remain preserved. An actual
Console API reproducer confirmed that Core's index lifecycle can retain an
empty cache after the independent MCP process has built the persistent index.
The harness now binds a live authenticated Context v2 response with matching
published revisions and ready hybrid status, and privately pins index files
before and after recovery observations.

The fresh `preaccept-20261018-recovery-v2` passed 120 historical executions,
60 authenticated context checks (six explicit catalog fallbacks), 17 business
checks, and all 30 Oracle cases, including 20 physical PostgreSQL checks and
18 permission plans plus 18 execution boundaries. Its 281 frozen source hashes
match the passing 107-test harness suite (one skip).

After an independent authenticated check, the owned coordinator was terminated
and all five runtime ports were confirmed free. Resuming the same run reused
the original credentials, authenticated all ten group/role combinations and
rechecked 60 contexts. Independent observations confirmed unchanged original
gate files, all 180 untouched records, private ciphertext and binding hashes.
Both rehearsal hosts were then stopped. No Agent worker was active during this
rehearsal; active deadline recovery retains deterministic test evidence only.
See [the safe recovery acceptance record](business-trust-v1-preacceptance-recovery-v2.json).
This proves technical recovery and does not establish Agent answer quality.
