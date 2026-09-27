from __future__ import annotations

import importlib.util
import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hr_retrieval_benchmark", HERE / "retrieval_benchmark.py"
)
assert SPEC and SPEC.loader
retrieval = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = retrieval
SPEC.loader.exec_module(retrieval)


class RetrievalBenchmarkTests(unittest.TestCase):
    def test_embedding_evaluation_is_fixed_and_covers_three_language_groups(self) -> None:
        evaluation = json.loads(
            (HERE / "embedding-evaluation.json").read_text(encoding="utf-8")
        )
        questions = evaluation["questions"]
        self.assertEqual(len(questions), 18)
        self.assertEqual(
            {item["language"] for item in questions},
            {"en", "zh-CN", "mixed"},
        )
        self.assertEqual(len({item["id"] for item in questions}), len(questions))
        self.assertTrue(all(item["requiredIds"] for item in questions))

    def test_ground_truth_covers_frozen_corpus_without_editing_golden(self) -> None:
        golden = json.loads((HERE / "golden-questions.json").read_text(encoding="utf-8"))
        ground_truth = retrieval.load_ground_truth(HERE / "retrieval-ground-truth.json")
        self.assertEqual(set(ground_truth), {item["id"] for item in golden["questions"]})
        self.assertEqual(len(ground_truth), 60)
        self.assertTrue(all(
            item.required_models
            or item.required_columns
            or item.required_relationships
            or item.required_rules
            or item.required_sql_examples
            or item.forbidden_models
            or item.forbidden_columns
            for item in ground_truth.values()
        ))
        for identifier in (
            "current-headcount",
            "salary-performance-region",
            "employee-profile",
            "salary-change-title",
        ):
            item = ground_truth[identifier]
            self.assertTrue(item.required_models, identifier)
            self.assertTrue(item.required_columns, identifier)
            self.assertTrue(item.required_rules, identifier)
        self.assertTrue(ground_truth["salary-performance-region"].required_relationships)
        self.assertTrue(ground_truth["salary-performance-region"].required_sql_examples)
        golden_digest = hashlib.sha256((HERE / "golden-questions.json").read_bytes()).hexdigest()
        self.assertEqual(
            golden_digest,
            "621ca276f4981c6a003dbf93cff4b224a8b3803f793560fec66a7785052c6228",
        )

    def test_ground_truth_artifact_matches_deterministic_generator(self) -> None:
        expected = retrieval.generate_ground_truth(HERE / "golden-questions.json")
        actual = json.loads((HERE / "retrieval-ground-truth.json").read_text(encoding="utf-8"))
        self.assertEqual(actual, expected)

    def test_recall_mrr_ndcg_and_language_split_use_ranked_candidates(self) -> None:
        truth = {
            "q-zh": retrieval.GroundTruthItem(
                question_id="q-zh",
                language="zh-CN",
                required_models=("model:a",),
                required_columns=("column:a.x", "column:a.y"),
                required_relationships=(),
                required_rules=("rule:headcount",),
                required_sql_examples=(),
                forbidden_models=(),
                forbidden_columns=(),
            ),
            "q-en": retrieval.GroundTruthItem(
                question_id="q-en",
                language="en",
                required_models=("model:b",),
                required_columns=(),
                required_relationships=(),
                required_rules=(),
                required_sql_examples=(),
                forbidden_models=(),
                forbidden_columns=(),
            ),
        }
        results = [
            retrieval.RetrievalResult(
                question_id="q-zh",
                candidates=(
                    retrieval.Candidate(id="model:wrong", kind="model"),
                    retrieval.Candidate(id="model:a", kind="model"),
                    retrieval.Candidate(id="column:a.x", kind="column"),
                    retrieval.Candidate(id="column:a.y", kind="column"),
                    retrieval.Candidate(id="rule:headcount", kind="rule"),
                ),
                context="中文上下文",
                latency_ms=10.0,
            ),
            retrieval.RetrievalResult(
                question_id="q-en",
                candidates=(retrieval.Candidate(id="model:b", kind="model"),),
                context="english context",
                latency_ms=30.0,
            ),
        ]
        report = retrieval.evaluate(truth, results, ks=(1, 3, 5))
        self.assertAlmostEqual(report["overall"]["recall@1"], 0.5)
        self.assertAlmostEqual(report["overall"]["recall@3"], 0.75)
        self.assertAlmostEqual(report["models"]["mrr"], 0.75)
        self.assertAlmostEqual(
            report["models"]["ndcg@3"],
            (1.0 / math.log2(3) + 1.0) / 2.0,
        )
        self.assertEqual(report["byLanguage"]["zh-CN"]["questions"], 1)
        self.assertEqual(report["byLanguage"]["en"]["questions"], 1)
        self.assertAlmostEqual(
            report["byLanguage"]["zh-CN"]["dimensions"]["columns"]["recall@3"],
            1.0,
        )
        self.assertEqual(report["context"]["bytes"]["count"], 2)
        self.assertEqual(report["latencyMs"]["count"], 2)
        self.assertEqual(report["permission"]["leakageCount"], 0)

    def test_permission_leakage_is_counted_even_when_candidate_is_marked_hidden(self) -> None:
        truth = {
            "deny": retrieval.GroundTruthItem(
                question_id="deny",
                language="en",
                required_models=(),
                required_columns=(),
                required_relationships=(),
                required_rules=(),
                required_sql_examples=(),
                forbidden_models=("model:compensation_history",),
                forbidden_columns=("column:compensation_history.monthly_salary",),
            )
        }
        result = retrieval.RetrievalResult(
            question_id="deny",
            candidates=(
                retrieval.Candidate(
                    id="model:compensation_history", kind="model", visible=False
                ),
                retrieval.Candidate(
                    id="column:compensation_history.monthly_salary",
                    kind="column",
                    visible=False,
                ),
            ),
            context="",
            latency_ms=2,
        )
        report = retrieval.evaluate(truth, [result], ks=(5,))
        self.assertEqual(report["permission"]["leakageCount"], 2)
        self.assertEqual(report["byLanguage"]["en"]["permissionLeakageCount"], 2)

    def test_percentiles_context_bytes_and_token_estimates_are_deterministic(self) -> None:
        self.assertEqual(retrieval.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(retrieval.percentile([], 95), None)
        self.assertEqual(retrieval.estimate_tokens("你好"), 2)
        self.assertEqual(retrieval.estimate_tokens("abcd"), 1)
        results = [
            retrieval.RetrievalResult(
                question_id="q",
                candidates=(),
                context="你好",
                latency_ms=1,
                context_bytes=None,
            ),
            retrieval.RetrievalResult(
                question_id="q2",
                candidates=(),
                context="abcd",
                latency_ms=4,
                context_bytes=None,
            ),
        ]
        summary = retrieval.summarize_payload(results)
        self.assertEqual(summary["bytes"], {"count": 2, "min": 4, "max": 6, "mean": 5.0, "p50": 5.0, "p95": 5.9})
        self.assertEqual(summary["estimatedTokens"]["mean"], 1.5)

    def test_synthetic_benchmark_scales_to_100_500_and_1000_models_without_artifacts(self) -> None:
        for model_count in (100, 500, 1000):
            with self.subTest(model_count=model_count):
                docs = retrieval.generate_synthetic_documents(model_count)
                self.assertEqual(len(docs), model_count * 4)
                self.assertEqual(docs[0].id, "model:synthetic_0000")
                self.assertEqual(docs[-1].id, f"column:synthetic_{model_count - 1:04d}.team_size")
                result = retrieval.synthetic_retrieve(docs, f"synthetic_{model_count - 1:04d} team size", limit=5)
                self.assertEqual(result[0].id, f"model:synthetic_{model_count - 1:04d}")

    def test_ground_truth_validator_rejects_unknown_question_and_forbidden_overlap(self) -> None:
        with self.assertRaises(retrieval.GroundTruthError):
            retrieval.validate_ground_truth(
                {"q": retrieval.GroundTruthItem(
                    question_id="q",
                    language="en",
                    required_models=("model:a",),
                    required_columns=(),
                    required_relationships=(),
                    required_rules=(),
                    required_sql_examples=(),
                    forbidden_models=("model:a",),
                    forbidden_columns=(),
                )},
                {"known"},
            )

    def test_candidate_validator_rejects_kind_id_mismatch(self) -> None:
        with self.assertRaisesRegex(retrieval.GroundTruthError, "kind does not match"):
            retrieval.Candidate.from_dict({"id": "model:employees", "kind": "column"})

    def test_evaluator_rejects_candidate_outside_frozen_corpus(self) -> None:
        truth = {
            "q": retrieval.GroundTruthItem(
                question_id="q",
                language="en",
                required_models=("model:employees",),
                required_columns=(),
                required_relationships=(),
                required_rules=(),
                required_sql_examples=(),
                forbidden_models=(),
                forbidden_columns=(),
            )
        }
        results = [
            retrieval.RetrievalResult(
                question_id="q",
                candidates=(retrieval.Candidate(id="model:not-in-corpus", kind="model"),),
            )
        ]
        with self.assertRaisesRegex(retrieval.GroundTruthError, "outside the frozen semantic corpus"):
            retrieval.evaluate(
                truth,
                results,
                candidate_catalog=frozenset({"model:employees"}),
            )


if __name__ == "__main__":
    unittest.main()
