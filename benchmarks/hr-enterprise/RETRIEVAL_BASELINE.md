# Semantic retrieval baseline

Recorded on 2026-09-19 against the unchanged 60-question HR corpus. Section
metrics use the independent Context v2 quotas (for example Model@5 and
Rule@5); `overall` retains the mixed global rank. The real vector runs use
`paraphrase-multilingual-MiniLM-L12-v2`, revision `main`, 384 dimensions,
normalized embeddings, CPU inference, and no mocked vectors.

| Configuration | Model R@5 | Column R@15 | Rule R@5 | Relationship R@15 | Mean bytes | p50 ms | p95 ms | Leaks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Full policy-safe context | 0.978 | 0.210 | 0.193 | 1.000 | 80,905 | 0.05 | 0.63 | 0 |
| Exact + Unicode BM25 | 1.000 | 0.821 | 0.882 | 0.000 | 27,802 | 16.94 | 17.65 | 0 |
| Vector | 1.000 | 0.723 | 0.649 | 0.857 | 36,586 | 19.16 | 26.94 | 0 |
| Hybrid + rule binding | 1.000 | 0.928 | 0.911 | 1.000 | 37,958 | 35.19 | 44.78 | 0 |
| Hybrid + rule binding + graph on every query | 0.978 | 0.967 | 0.911 | 1.000 | 37,992 | 35.44 | 43.99 | 0 |
| Hybrid + graph + lightweight reranker | 1.000 | 0.967 | 0.916 | 1.000 | 38,103 | 53.03 | 66.07 | 0 |
| Adaptive hybrid (graph only for cross-model intent) | 1.000 | 0.931 | 0.911 | 1.000 | 37,980 | 34.92 | 43.39 | 0 |

The selected adaptive hybrid run has global MRR 0.8882 and NDCG@10 0.7043.
English Recall@10 is 0.6681 and Chinese Recall@10 is 0.7211. Mean estimated
tokens fall from 20,226.3 (full context) to 9,495.3, a 53.1% reduction. Model,
column, rule, relationship, NDCG, latency, token-reduction, and zero-leakage
retrieval gates pass. The graph-everywhere row remains in the report because it
shows why graph traversal is intent-gated rather than blindly enabled. The
lightweight reranker is not selected for production: although its section
recall passes, overall Recall@5/10 falls to 0.5472/0.6876 and p95 rises to
66.07 ms, so it fails the selected configuration's global recall gates.

| Additional gate | Current | Target |
| --- | ---: | ---: |
| Overall Recall@5 | 0.5621 | >= 0.55 |
| Overall Recall@10 | 0.7034 | >= 0.70 |
| MRR | 0.8882 | >= 0.75 |
| NDCG@10 | 0.7043 | >= 0.65 |
| Permission leakage | 0 | 0 |
| First-pass SQL accuracy | 51/60 | >= 54/60 |

The SQL figure is the defensible optimized blind first-pass result already
recorded in `EVALUATION_REPORT.md`; the classification-corrected 60/60 result
is not used as the baseline because it also includes evaluator corrections.

Reproduce the retrieval capture and report without database rows or
credentials:

```powershell
python benchmarks/hr-enterprise/retrieval_benchmark.py capture-local `
  --project benchmarks/hr-enterprise/project `
  --golden benchmarks/hr-enterprise/golden-questions.json `
  --rule-metadata benchmarks/hr-enterprise/retrieval-rule-metadata.json `
  --locales benchmarks/hr-enterprise/retrieval-locales.json `
  --mode hybrid-adaptive `
  --output .benchmark-data/hr-enterprise/retrieval-hybrid-adaptive.json

python benchmarks/hr-enterprise/retrieval_benchmark.py evaluate `
  --ground-truth benchmarks/hr-enterprise/retrieval-ground-truth.json `
  --results .benchmark-data/hr-enterprise/retrieval-hybrid-adaptive.json `
  --output .benchmark-data/hr-enterprise/retrieval-hybrid-adaptive-report.json
```

The original HR project and all frozen questions remain byte-for-byte
unchanged. `retrieval-locales.json` and `retrieval-rule-metadata.json` are
explicit migration companions used by every row in the comparison; they model
the localized field metadata and structured rule bindings that a migrated
project supplies through `semantic-console/locales.yml` and
`semantic-console/rule-metadata.yml`. They are not derived from retrieval
outputs and the freeze suite rejects any mutation of the original project.

The checked-in ground truth uses the same stable IDs emitted by
`SemanticDocument`, including atomic rule IDs and `sql_example:` IDs. The
1,000-model runtime test additionally requires the dependency-free fallback to
finish under 10 seconds with less than 100 MiB peak traced memory. These are
generous regression ceilings, not production SLOs.
