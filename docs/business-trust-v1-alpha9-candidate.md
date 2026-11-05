# Business trust alpha.9 candidate

The local installable candidate is `@hejielijob/semarail-core@0.1.0-alpha.9`.
It has passed technical and authenticated MCP acceptance. All 180 external Agent
positions have been attempted and independently reviewed. The frozen comparison
failed promotion gates: baseline/candidate final correctness is 51/90 versus
38/90, and first SQL correctness is 41/60 versus 32/60. Business trust remains
explicit opt-in; Context keeps its v1 default. See [final results](business-trust-v1-final-results.md).

## Install the accepted artifact

From the repository root, verify the retained artifact before installation:

```powershell
Get-FileHash .\dist\hejielijob-semarail-core-0.1.0-alpha.9.tgz -Algorithm SHA256
npm install --global .\dist\hejielijob-semarail-core-0.1.0-alpha.9.tgz
semarail --version
semarail --help
```

Expected artifact SHA-256:
`b9aec5419cef6509998d1f88f1f0c34dbd3c17cd3f393851d81a39bf233dd828`.
Use this exact retained tarball for the candidate under evaluation. Running
`pnpm package:core` creates a new build and does not establish that its bytes
match this artifact. Packages have not been published to npm or PyPI.

The retained tarball's README predates this candidate and mentions alpha.7 in
its installation example. The command above selects alpha.9. The tarball is
preserved byte-for-byte for the formal comparison.

## Configure and upgrade

Maintain the existing Wren 0.13.2, PostgreSQL execution and authenticated
MCP/HTTP boundaries. Configure versioned `executionChecks` through the existing
rule editor and publish them. Draft edits do not enable checks. Start with the
isolated HR acceptance project; the Core does not invoke a model.

Back up both the control-plane database and semantic project before upgrading.
SQLite and PostgreSQL apply their incremental migrations through the existing
stores. Unknown or discontinuous migration versions fail closed. See
[business trust contracts and storage](business-trust-v1.md) for the interfaces,
access restrictions, retention and migration versions.

The corrected verifier identifies itself as `business-trust-v2`; public evidence
and execution-check schemas remain version 1. Prepare again after upgrading:
old preparations cannot authorize execution under the new verifier. Historical
evidence retains its original version and shows stale checks when the verifier
version changes. Required period confirmation also applies to date-grouped
queries, and published defaults must match the exact SQL date or range.

## Disable and roll back

Disable or remove execution checks in the existing rule editor and publish the
change to stop applying those checks. A semantic rollback invalidates relevant
preparations and historical checks. Retain the newer server for evidence reads
until retention expires. A full code downgrade requires the matching database
and project backup; do not edit migration ledgers to force a downgrade.

## Evidence and remaining acceptance

- [Candidate package acceptance](business-trust-v1-candidate-alpha9-acceptance.json)
  records the artifact and isolated installation verification.
- [Correction acceptance](business-trust-v1-correction-acceptance-alpha9.json)
  records deterministic checks, PostgreSQL storage and real MCP acceptance.
- [Repair reconstruction](business-trust-v1-alpha9-repair-reconstruction.json)
  records current-code reads of the genuine salary and performance repair
  histories. It does not claim a new SQL run or external Agent evaluation.
- [G campaign progress](business-trust-v1-campaign-g-progress.json) retains all
  180 positions. Partial progress is not the final comparison report.

The [final results](business-trust-v1-final-results.md) link the immutable
comparison and independent content reviews. The [final requirement audit](business-trust-v1-final-requirement-audit.json)
distinguishes delivered implementation from failed promotion and transport gates.
Technical acceptance does not establish improved final answers.
