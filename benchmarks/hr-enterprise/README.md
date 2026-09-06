# HR enterprise benchmark

This local-only benchmark tests SemaRail against a reproducible enterprise-shaped HR workload. It does not add product features and it never sends employee rows, SQL, datasource credentials, or service-account keys to a report.

The completed direct-agent evaluation, methodology, failure routing, and
content-safe results are published in the
[`EVALUATION_REPORT.md`](EVALUATION_REPORT.md). DeepSeek Harness remains outside
the scope of that result.

## What it exercises

- 100,000 synthetic employees normalized into six regions and 54 regional departments
- 200,000 compensation rows, 400,000 performance rows, and 1,200,000 monthly attendance rows
- PostgreSQL 17 with six physical tables and a read-only runtime login
- five service accounts: employee, department manager, regional HRBP, regional compensation administrator, and HR director
- explicit table, column, and row policies, including fail-closed missing attributes and policy unbinding
- 60 fixed questions: 40 Chinese and 20 English; 30 basic, 15 analytical, and 15 authorization cases
- inline result limits, CSV artifact fallback, download authorization, and immediate credential revocation

The deterministic layer sends the checked-in canonical semantic SQL through the real semantic planner, policy engine, and PostgreSQL executor. It must pass all 60 cases and all security probes. The separate model layer evaluates real Harness captures with thresholds of 48/60 on the first pass, 54/60 after at most one repair, and 15/15 authorization denials.

## Pinned source and transformation

The source is Kaggle's [Employee Performance and Productivity Data](https://www.kaggle.com/datasets/mexwell/employee-performance-and-productivity-data), version 1, under CC0-1.0. `dataset.json` pins the archive and member byte sizes and SHA-256 digests. Before the first download, configure either `KAGGLE_API_TOKEN` or both `KAGGLE_USERNAME` and `KAGGLE_KEY`. Cached archives are accepted only when their pinned size and SHA-256 match. Credentials are read only for the HTTPS request and are never written to the manifest or command output.

Transformation seed `20260904` preserves every source employee's latest values. Earlier salary and review observations, monthly attendance allocation, region assignment, and valid direct managers are deterministic. Generated files live only under `.benchmark-data/hr-enterprise/`, which is gitignored.

## Run

Requirements are the normal source-development environment plus Docker Desktop (or Docker Engine with Compose v2). On Windows PowerShell:

```powershell
pnpm benchmark:hr:prepare
pnpm benchmark:hr
```

Preparation downloads, verifies, transforms, and re-verifies the source. The benchmark replaces only its dedicated Compose database volume, starts PostgreSQL on loopback port `55432`, starts a temporary Core on `48773`, creates the datasource and exactly five accounts, executes the workload, and writes a content-safe report to `.benchmark-data/hr-enterprise/reports/deterministic.json`. Core is stopped after the run; PostgreSQL remains available for local inspection until cleanup.

Run the non-Docker checks with:

```powershell
pnpm test:hr-benchmark
pnpm benchmark:hr:evaluate -- --dry-run
```

## Model/Harness evaluation

First create a schema-v2 capture template:

```powershell
pnpm benchmark:hr:evaluate -- --make-template .benchmark-data/hr-enterprise/harness-template.json
```

Replace placeholders with a real, provider-neutral Harness run. The evidence envelope records the model provider/id, parameters, the pinned data hash, and one or two attempts per question. Then evaluate it:

```powershell
pnpm benchmark:hr:evaluate -- `
  --evidence .benchmark-data/hr-enterprise/harness-evidence.json `
  --report .benchmark-data/hr-enterprise/reports/model-evaluation.json
```

When `--report` is omitted, the wrapper writes `.benchmark-data/hr-enterprise/reports/model-evaluation.json`.

The report contains pass/fail classifications and timings, but never captured rows, SQL text, error messages, or credentials. Synthetic evaluator self-tests are useful only for evaluator logic and are not a model-quality claim.

## Cleanup

```powershell
pnpm benchmark:hr:clean
```

This removes only the benchmark Compose containers/volume and the resolved `.benchmark-data/hr-enterprise` directory. Download and transformation must be repeated afterward.

## Deliberate boundaries

This benchmark covers direct-manager policies only. It does not claim recursive management hierarchy, group suppression/privacy thresholds, a production identity provider, non-PostgreSQL governed execution, or CI capacity for a 1.9-million-row local workload.
