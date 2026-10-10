# Trace v2 local acceptance

The desktop and rendered Console checks used an isolated synthetic HR project
and PostgreSQL 17 datasource. Production HR data, the original datasource, and
the original dirty checkout were not used for these checks. Core/Console source
belongs to SemaRail; the optional collector is maintained in the separate
[Codex adapter repository](https://github.com/hejielijob-commits/semarail-codex-trace).

## Actual Codex desktop execution

The installed collector was **0.2.2**, with the existing seven async Hook
definitions. The installed Hook and MCP observer scripts matched their source.
New desktop chats loaded the collector and actual `semarail_trace_acceptance`
MCP server. No HTTP-created Trace fixture substituted for desktop execution.

| Check | Observed result |
| --- | --- |
| Enabled project with event ingestion returning 503 | Main Agent and subAgent calls completed; 25 projected events persisted locally while the server had zero session Traces. |
| Event ingestion restored | All 25 queued event IDs appeared exactly once, pending count returned to zero, child start/stop and six verified Core diagnostics were present. |
| Initial query | Core `query.run` recorded `failure`, `DATABASE_ERROR`, and `database` stage. The failed tool links to its scoped issue. |
| Manually corrected query in the same chat | A distinct turn recorded a successful Core `query.run`; both turns share one Session and retain their own Trace IDs. |
| Unenabled project | Actual main/subAgent activity completed; both local session queue counts and the server Trace list remained empty. |
| Privacy | Prompt nonce, semantic SQL and credential probes were absent from projected server events and local queue/database files. No transcript was read. |

The enabled Session was `01a125b4-cfa7-7d32-8674-a01bccf0df96`:

| Execution | Agent Trace | Core query.run |
| --- | --- | --- |
| Contains the failed query | `atr_80d3cebee31f437ba73424c80641ebfb` | `trace-555adfab409b4d1c8882a30bf8abdf81` |
| Contains the corrected query | `atr_6f60740b46ab44fb9831590bdfeae620` | `trace-ae6df7126928422e8edd67ce51b60fb7` |

Both assistant turns completed normally; their tool/Core query outcomes are
different. Turn completion is not presented as proof that every tool succeeded.
This desktop omitted the initial turn-start Hook. The Console reports
`MISSING_TURN_START`, leaves the main model/Token/LLM metrics uncollected, and
keeps tools without reliable ownership under **Unknown owner**. Child tool
ownership uses explicit recorded parent evidence. Missing evidence was not
invented to improve the acceptance result.

The unenabled Session was `01a121a8-f767-73f3-9f10-db2718731dff`.

## Rendered Console workflow

The production frontend was loaded in the Codex in-app browser using an existing
managed test employee session. A new tab reconstructed the persisted Traces.
The following actions were verified in the rendered UI:

1. Open the failed tool's issue and return to its Agent Trace.
2. Compare the two real desktop Trace IDs. Child activity and its Core phase
   timings align; activities with unknown ancestry remain separate. Both sides'
   Core diagnostics are visible. Missing model metadata is unknown, rather than
   a claim that two models were equal.
3. From the original issue, create a manual regression draft. Only the feedback
   and verified source Trace links are seeded; question, SQL and assertions
   begin blank.
4. Explicitly enter an ordered aggregate query against the registered synthetic
   test dataset, review the saved draft, then enable revision 2.
5. Run that case as the current managed subject. Status, row-count and numeric
   cell assertions all passed. Open the run's Core diagnostic, original issue
   and source Trace links.

The reviewed case was `case_624fce053dad4f2b85130929921677a7`; its run was
`rr_2107b0a1c72d4a9db015b17990927d1a` after the final backend reload. The persisted result retained subject,
case revision, environment versions, assertion statuses and two Core IDs.
Question/SQL bodies and actual/expected cell values were absent from the run.
The separate API acceptance batch produced both passing and deliberately
failing assertions, and regression execution created no full-result CSV
artifacts.

The final run's Core preparation and execution IDs were
`trace-7a215c8b1c1445178b98a3836714d8cf` and
`trace-c5327c66502e46a58d1c5d1281abeb95`. Its rendered history successfully
opened the Core diagnostic, original issue and original Agent Trace.

Screenshots show this synthetic environment:

- [Collector health and scoped filters](images/trace-v2-collector.jpg).
- [Rules, missing evidence and failure diagnosis](images/trace-v2-diagnosis.jpg).
- [Independent Trace comparison page](images/trace-v2-comparison.jpg).
- [Regression history, assertion outcomes and evidence links](images/trace-v2-regression.jpg).

The collector screenshot intentionally shows a stale last-received report.
It does not claim that historical backlog counts are live device measurements.

## Concurrency and behavior verification

Real PostgreSQL advisory-lock verification used eight independent short-lived
sessions in the isolated cluster's control database, without table writes:
same-project execution and configuration mutations competed for the same lock;
different projects could proceed; explicit unlock and holder-session shutdown
both permitted reacquisition. SQLite cross-process tests used spawned processes
and an OS file lock, including an expired run lease while Core was still live.

Automated coverage is provided by:

- [Trace API tests](../apps/semantic-console/server/tests/test_traces_api.py):
  duplicate/late events, scope isolation, verified links, filters, rules, version
  gaps, uncertain matches, collector staleness and 30-day cleanup.
- [Regression execution tests](../apps/semantic-console/server/tests/test_regression.py)
  and [audit tests](../apps/semantic-console/server/tests/test_regression_audit.py):
  source mismatch, draft review, current permissions, clarification, environment
  drift, assertions, truncation, uncertain ordering, cancellation, cross-process
  locks, restart recovery, legacy compatibility and body/artifact exclusion.
- Persistent frontend fixtures and workbench tests: reload reconstruction,
  parallel child Agents, missing completions, late diagnosis, unmatched activity,
  stale collector state, reviewed cases, expired evidence and run links.
- Independent adapter tests: bounded outbox, retries, lost acknowledgement,
  identity rotation/races, worker lease, failed MCP completion and crash recovery.
  A crashed sender retains its lease for at most 120 seconds; the takeover test
  expires the fixture lease without a real-time sleep, then verifies delivery,
  queue removal and release by a new owner.

Final verification on 2026-10-11:

| Gate | Result |
| --- | --- |
| Complete Semantic Console server suite | 281 tests passed |
| Complete Console frontend suite | 143 tests passed |
| Independent Codex adapter suite | 57 tests passed |
| Core CLI and distribution gate | 6 tests passed; production build and 72-file package verification passed |
| Workspace lint and typecheck | Passed |
| Delivery audit | Four real JPEG screenshots; all 31 packaged server files match source; no test-session credentials in repository files |

The final service tests include deterministic cancellation/recovery races,
late cancellation diagnostic IDs, and
[dataset lock, v2 export and cross-organization API checks](../apps/semantic-console/server/tests/test_regression_api_v2_guards.py).

These checks establish the described local workflow and supported contracts.
They do not establish LLM-call timing, Token usage, automatic root-cause analysis,
automatic repair, immutable datasets or complete Agent replay.
