# HR enterprise evaluation report

This report records the assessment cases, blind-generation protocol, execution
path, failure routing, and results for the SemaRail HR enterprise benchmark.
It is intentionally evidence-focused: generated employee rows, model SQL,
database credentials, and local runtime state are not committed.

## Outcome

| Stage | Basic | Analytical | Authorization | Total | Interpretation |
| --- | ---: | ---: | ---: | ---: | --- |
| Original blind first pass | 8/30 | 3/15 | 15/15 | 26/60 | Baseline before question and semantic-layer work |
| Original run after one repair | 24/30 | 3/15 | 15/15 | 42/60 | Repair received only failed IDs and safe error classes |
| Optimized blind first pass | 23/30 | 13/15 | 15/15 | 51/60 | Fresh Luna Max agents; exceeds the 48/60 first-pass target |
| Strict classification correction | 30/30 | 15/15 | 15/15 | 60/60 | The same optimized answers were re-evaluated after narrow test/oracle corrections |

The defensible model-quality comparison is **26/60 to 51/60**. The final
60/60 also includes corrections to the test and evaluator, so it must not be
attributed entirely to model improvement.

The deterministic SemaRail-to-Wren-to-PostgreSQL path separately passed 60/60
cases on the 1.9-million-row generated corpus. The final run had a 47 ms median,
250 ms p95, and 578 ms maximum query time.

## Case inventory

The fixed corpus contains 60 cases:

- 30 basic queries, 15 analytical queries, and 15 authorization cases;
- 40 Chinese and 20 English prompts;
- five actors: employee, department manager, regional HRBP, regional
  compensation administrator, and HR director;
- six semantic models covering employees, departments, regions, compensation,
  performance, and attendance.

Every question, role, expected outcome, canonical query, and oracle is versioned
in [`golden-questions.json`](golden-questions.json). Authorization cases require
`POLICY_DENIED` and zero database rows; successful cases require semantic result
equivalence, not merely executable SQL.

## Evaluation process

1. **Freeze the corpus.** The same 60 questions, roles, thresholds, snapshot
   date, and pinned data hash are used across runs.
2. **Generate answers blind.** Three fresh `gpt-5.6-luna` agents at max reasoning
   handle basic, analytical, and authorization partitions. They receive the
   public question fields and may read the checked-in semantic project, but may
   not read canonical SQL, native SQL, oracle rows, or expected results.
3. **Run the real path.** Generated SQL passes through SemaRail authorization,
   Wren semantic planning, and PostgreSQL execution. Agents cannot execute their
   own queries while composing answers.
4. **Constrain repair.** For the original repair round, only the failed case ID
   and safe failure class are returned. Standard SQL and result rows remain
   hidden. Authorization failures are never retried.
5. **Route failures by ownership.** Residuals are assigned to test error,
   missing knowledge, Skill contract, evaluator error, table/data error, or
   unclear requirement before any change is made.
6. **Retest with fresh agents.** The optimized run uses new agents rather than
   continuing the original context.
7. **Run deterministic regression.** Canonical semantic SQL is executed through
   the same policy, planner, and database path to separate infrastructure defects
   from model-generation defects.
8. **Run repository checks.** Corpus/evaluator tests, Wren validation, dry-plan,
   and the full repository test suite guard against score-only changes.

## What changed before the optimized blind run

The question and semantic layers were improved without adding product features:

- snapshot dates and the meaning of "current" were made explicit;
- cross-fact joins specify employee-grain aggregation before joining;
- region and department code output requirements were clarified;
- training-hour bands and chronological ordering rules were documented;
- model metadata and knowledge examples were aligned with the fixed questions;
- ambiguous sorting and tie-breaking language was added where correctness
  otherwise could not be judged consistently.

These changes produced the 25-case first-pass improvement, including analytical
growth from 3/15 to 13/15.

## Residual classification

The optimized first pass left nine apparent failures:

| Classification | Count | Evidence | Resolution |
| --- | ---: | --- | --- |
| Evaluator misjudgment | 7 | Six equivalent aliases and one safe extra employee ID | Per-question alias/extra-column allowlists only |
| Test-set error | 1 | `manager-count` said "current" while the old oracle included resigned employees | Align the oracle with the stated question |
| Unclear requirement | 1 | `education-count` omitted ordering and tie-break behavior | State ordering and tie-break explicitly |
| Missing knowledge | 0 | No optimized residual supported this diagnosis | No residual-driven change |
| Skill contract error | 0 | No direct-agent evidence implicated Harness context assembly | Deferred to Harness testing |
| Table/data problem | 0 | Deterministic execution remained 60/60 | No data correction |

Evaluator tolerance remains fail-closed. Only aliases and safe extra columns
declared on the individual oracle are accepted; unknown output columns still
fail. The 60/60 classification-corrected result uses the same generated answers,
not a model rewrite.

## Independent checks

| Check | Result |
| --- | --- |
| HR benchmark unit tests | 12/12 passed |
| Wren project validation | 0 errors, 0 warnings |
| Wren dry-plan | 45/45 successful queries |
| PostgreSQL deterministic regression | 60/60 |
| Luna Max optimized blind run after strict classification correction | 60/60 |
| Repository test suite | Passed |

The machine-readable, content-safe result summary is in
[`results/evaluation-summary.json`](results/evaluation-summary.json). Local raw
captures and generated data remain under `.benchmark-data/`, which is ignored by
Git.

## Limitations

- The source is a public Kaggle HR dataset expanded deterministically into an
  enterprise-shaped synthetic corpus; it is not a production employee
  distribution.
- The original 26/60 and optimized 51/60 first-pass totals were recorded in the
  task run log. Later re-evaluation overwrote the corresponding intermediate
  local summary files, so this report preserves those stage totals but does not
  claim immutable per-case artifacts for them.
- This run validates direct Codex Luna Max agents and the SemaRail execution
  path. **DeepSeek Harness was deliberately not tested**, so these results do not
  establish plugin installation, Harness context injection, or DeepSeek model
  behavior.

## Reproduction

See [`README.md`](README.md) for pinned dataset preparation, deterministic
execution, evaluator commands, cleanup, and the provider-neutral Harness capture
format.
