# Multilingual embedding evaluation

Recorded on 2026-09-19 against the fixed 18-query
`embedding-evaluation.json` set: six English, six Simplified Chinese, and six
mixed Chinese/English questions. Both models used normalized CPU embeddings,
the same query/document text normalization, the same semantic documents, and
vector-only Top-5 retrieval.

| Model | Dimensions | Overall R@5 | Overall MRR | English R@5 | Chinese R@5 | Mixed R@5 | p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `paraphrase-multilingual-MiniLM-L12-v2` | 384 | 0.889 | 0.532 | 0.833 | 0.833 | 1.000 | 26.91 |
| `sentence-transformers/distiluse-base-multilingual-cased-v2` | 512 | 0.667 | 0.462 | 0.667 | 0.500 | 0.833 | 24.06 |

`paraphrase-multilingual-MiniLM-L12-v2` remains the default. The alternative
is slightly faster on this small warm run, but loses 22.2 percentage points of
overall Recall@5 and is materially worse for Chinese and mixed-language
queries. Model ID, revision, normalization setting, and vector dimension are
part of the index compatibility identity, so changing the selected model
forces a rebuild rather than mixing embeddings.

Reproduce either row without changing the frozen 60-question benchmark:

```powershell
python benchmarks/hr-enterprise/retrieval_benchmark.py embedding-eval `
  --project benchmarks/hr-enterprise/project `
  --evaluation benchmarks/hr-enterprise/embedding-evaluation.json `
  --rule-metadata benchmarks/hr-enterprise/retrieval-rule-metadata.json `
  --locales benchmarks/hr-enterprise/retrieval-locales.json `
  --embedding-model paraphrase-multilingual-MiniLM-L12-v2 `
  --output .benchmark-data/hr-enterprise/embedding-evaluation-report.json
```

The current model misses the specific `performance_score` column for one
English and one Chinese vector-only query. Hybrid retrieval compensates with
lexical, exact, rule-binding, and graph evidence; this report does not conceal
those vector-only misses or claim that vector recall alone meets the full HR
retrieval gate.
