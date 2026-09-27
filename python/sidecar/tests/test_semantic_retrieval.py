from __future__ import annotations

import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from sidecar.semantic_index import SemanticDocument
from sidecar.semantic_retrieval import (
    HybridSemanticRetriever,
    SentenceTransformerEmbedder,
)


class DeterministicEmbedder:
    """Small provider double: income/revenue are intentionally synonyms."""

    config = {"provider": "test", "model": "deterministic-v1"}
    dimension = 2

    def embed(self, texts: list[str], **_: object) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            value = text.casefold()
            if any(token in value for token in ("revenue", "income", "销售额")):
                vectors.append([1.0, 0.0])
            elif any(token in value for token in ("employee", "员工")):
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.2, 0.2])
        return vectors


def documents(revision: str = "r1") -> list[SemanticDocument]:
    return [
        SemanticDocument(
            id="metric:revenue",
            kind="metric",
            projectRevision=revision,
            text="annual revenue",
            model="orders",
        ),
        SemanticDocument(
            id="model:employees",
            kind="model",
            projectRevision=revision,
            text="员工主数据",
        ),
        SemanticDocument(
            id="relationship:orders_employees",
            kind="relationship",
            projectRevision=revision,
            text="orders and employees",
            referencedModels=("orders", "employees"),
        ),
        SemanticDocument(
            id="rule:secret",
            kind="rule",
            projectRevision=revision,
            text="revenue internal only",
            visibility={"visible": False},
        ),
        SemanticDocument(
            id="rule:headcount",
            kind="rule",
            projectRevision=revision,
            text="Count distinct employee identifiers.",
            model="employees",
            referencedModels=("employees",),
            metadata={"aliases": ["current workforce size"]},
        ),
    ]


class HybridRetrievalTests(unittest.TestCase):
    def test_optional_lightweight_reranker_reorders_the_fused_candidate_set(self) -> None:
        class RerankEmbedder:
            config = {"provider": "test", "model": "rerank-v1"}
            dimension = 2

            def embed(self, texts: list[str], **_: object) -> list[list[float]]:
                values: list[list[float]] = []
                for text in texts:
                    if text == "alpha beta" or "vector favorite" in text:
                        values.append([1.0, 0.0])
                    else:
                        values.append([0.8, 0.6])
                return values

        items = [
            SemanticDocument(
                id="model:coverage", kind="model", projectRevision="r1",
                model="coverage", text="alpha beta relevant",
            ),
            SemanticDocument(
                id="model:vector", kind="model", projectRevision="r1",
                model="vector", text="alpha vector favorite",
            ),
        ]
        index = HybridSemanticRetriever(embedder=RerankEmbedder())
        index.build(items, revision="r1")
        index.activate("r1")

        vector_order = index.search(
            "alpha beta", limit=2, channels=("vector",), rerank=False
        )
        reranked = index.search(
            "alpha beta", limit=2, channels=("vector",), rerank=True
        )

        self.assertEqual(vector_order[0].document.id, "model:vector")
        self.assertEqual(reranked[0].document.id, "model:coverage")
        self.assertIn("lightweight reranking", reranked[0].reason)
    def test_vector_only_synonym_recall_and_trace(self) -> None:
        index = HybridSemanticRetriever(embedder=DeterministicEmbedder())
        index.build(documents(), revision="r1")
        index.activate("r1")

        result = index.search("income", limit=1)

        self.assertEqual(result[0].document.id, "metric:revenue")
        self.assertEqual(result[0].match_type, "vector")
        self.assertEqual(result.trace[0].retrieval_type, "vector")
        self.assertFalse(result.trace[0].authorization_filtered)

    def test_build_reuses_unchanged_embeddings_across_revisions(self) -> None:
        class CountingEmbedder(DeterministicEmbedder):
            def __init__(self) -> None:
                self.batch_sizes: list[int] = []

            def embed(self, texts: list[str], **kwargs: object) -> list[list[float]]:
                self.batch_sizes.append(len(texts))
                return super().embed(texts, **kwargs)

        embedder = CountingEmbedder()
        index = HybridSemanticRetriever(embedder=embedder)
        first = documents("r1")
        index.build(first, revision="r1")
        index.activate("r1")
        second = documents("r2")
        second[0] = SemanticDocument(
            id=second[0].id,
            kind=second[0].kind,
            projectRevision="r2",
            text="quarterly revenue",
            model=second[0].model,
        )

        index.build(second, revision="r2")
        index.activate("r2")

        self.assertEqual(embedder.batch_sizes, [len(first), 1])
        self.assertEqual(index.status("r2").document_count, len(second))
        self.assertEqual(index.search("income", revision="r2")[0].document.id, "metric:revenue")

    def test_filter_precedes_candidates_and_trace_and_quotas_apply(self) -> None:
        index = HybridSemanticRetriever(embedder=DeterministicEmbedder())
        index.build(documents(), revision="r1")
        index.activate("r1")

        result = index.search(
            "revenue",
            limit=10,
            quotas={"metric": 1},
            visibility_filter=lambda document: document.kind != "relationship",
        )

        ids = [hit.document.id for hit in result]
        self.assertNotIn("rule:secret", ids)
        self.assertNotIn("rule:secret", [trace.document_id for trace in result.trace])
        self.assertNotIn("relationship:orders_employees", ids)
        self.assertEqual(ids.count("metric:revenue"), 1)
        self.assertGreaterEqual(result.candidate_count, result.selected_count)
        self.assertEqual(result.filtered_count, 2)

    def test_structured_rule_alias_has_auditable_rule_binding_trace(self) -> None:
        index = HybridSemanticRetriever(embedder=DeterministicEmbedder())
        index.build(documents(), revision="r1")
        index.activate("r1")

        result = index.search("current workforce size", limit=3)

        hit = next(item for item in result if item.document.id == "rule:headcount")
        trace = next(item for item in result.trace if item.document_id == "rule:headcount")
        self.assertEqual(hit.match_type, "ruleBinding")
        self.assertIn("ruleBinding", hit.retrieval_types)
        self.assertEqual(trace.reason_code, "ruleBinding")

    def test_lexical_retrieval_humanizes_identifiers(self) -> None:
        items = [
            SemanticDocument(
                id="model:employees", kind="model", projectRevision="r1",
                model="employees", text="employee model",
            ),
            SemanticDocument(
                id="column:employees.job_title", kind="column", projectRevision="r1",
                model="employees", field="job_title", text="job_title",
                referencedModels=("employees",),
                referencedColumns=("employees.job_title",),
            ),
            SemanticDocument(
                id="rule:titles", kind="rule", projectRevision="r1",
                text="Return the requested title grouping.",
                referencedModels=("employees",),
                referencedColumns=("employees.job_title",),
                metadata={
                    "aliases": ["top job titles"],
                    "mandatory": True,
                    "ruleType": "join_semantics",
                },
            ),
            SemanticDocument(
                id="relationship:employees_titles", kind="relationship",
                projectRevision="r1", text="employees title join",
                referencedModels=("employees", "titles"),
                referencedColumns=("employees.job_title", "titles.name"),
            ),
        ]
        index = HybridSemanticRetriever(embedder=DeterministicEmbedder())
        index.build(items, revision="r1")
        index.activate("r1")

        lexical = index.search("job title", limit=3, channels=("lexical",))
        self.assertIn("column:employees.job_title", [hit.document.id for hit in lexical])
        bound = index.search(
            "top job titles", limit=4,
            channels=("rule_binding", "graph"), relationship_depth=2,
        )
        self.assertIn("relationship:employees_titles", [hit.document.id for hit in bound])
        small_budget = index.search(
            "top job titles",
            limit=1,
            quotas={"relationship": 0, "column": 1},
            channels=("rule_binding", "graph"),
            relationship_depth=2,
        )
        self.assertEqual(len(small_budget), 1)
        self.assertEqual(small_budget[0].document.kind, "column")

    def test_relationship_graph_expands_two_hops_without_same_name_field_cliques(self) -> None:
        graph_documents = [
            SemanticDocument(
                id="model:a", kind="model", projectRevision="r1", model="a", text="alpha ledger"
            ),
            SemanticDocument(
                id="model:b", kind="model", projectRevision="r1", model="b", text="beta ledger"
            ),
            SemanticDocument(
                id="column:a.id", kind="column", projectRevision="r1", model="a", field="id", text="alpha key"
            ),
            SemanticDocument(
                id="column:b.id", kind="column", projectRevision="r1", model="b", field="id", text="beta key"
            ),
            SemanticDocument(
                id="relationship:a_b", kind="relationship", projectRevision="r1",
                text="alpha to beta", referencedModels=("a", "b"),
                referencedColumns=("a.id", "b.id"),
            ),
        ]
        index = HybridSemanticRetriever()
        index.build(graph_documents, revision="r1")
        index.activate("r1")

        result = index.search(
            "a", limit=5, channels=("exact", "graph"), relationship_depth=2
        )
        ids = [item.document.id for item in result]

        self.assertIn("relationship:a_b", ids)
        self.assertIn("model:b", ids)
        self.assertNotEqual(ids[0], "column:b.id")

    def test_persistent_revision_pointer_and_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index = HybridSemanticRetriever(
                embedder=DeterministicEmbedder(),
                storage_path=Path(directory),
            )
            index.build(documents(), revision="r1")
            self.assertEqual(index.search("revenue", revision="r1").hits, ())
            index.activate("r1")

            reopened = HybridSemanticRetriever(
                embedder=DeterministicEmbedder(),
                storage_path=Path(directory),
            )
            self.assertEqual(reopened.status().state, "active")
            self.assertEqual(reopened.search("revenue")[0].document.id, "metric:revenue")
            stale = reopened.search("revenue", revision="r2")
            self.assertEqual(stale.status.state, "stale")
            self.assertEqual(stale.hits, ())
            self.assertEqual(stale.trace, ())

            class WrongDimensionEmbedder(DeterministicEmbedder):
                dimension = 3

                def embed(self, texts: list[str], **_: object) -> list[list[float]]:
                    return [[1.0, 0.0, 0.0] for _ in texts]

            incompatible = HybridSemanticRetriever(
                embedder=WrongDimensionEmbedder(), storage_path=Path(directory)
            )
            self.assertEqual(incompatible.status().state, "stale")
            self.assertEqual(incompatible.status().stale_reason, "embedding_dimension_mismatch")
            self.assertEqual(incompatible.search("revenue").hits, ())

    def test_corrupt_active_pointer_is_reported_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "active.json").write_text("{broken", encoding="utf-8")

            reopened = HybridSemanticRetriever(storage_path=root)

            self.assertEqual(reopened.status().state, "stale")
            self.assertTrue(reopened.status().stale_reason.startswith("active_pointer_unreadable:"))
            self.assertEqual(reopened.search("anything").hits, ())

    def test_invalid_active_pointer_stays_fail_closed_until_reactivated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = HybridSemanticRetriever(
                embedder=DeterministicEmbedder(), storage_path=root
            )
            index.build(documents(), revision="r1")
            index.activate("r1")
            (root / "active.json").write_text(
                '{"revision":"r1","partition":"partition-wrong.json"}',
                encoding="utf-8",
            )

            reopened = HybridSemanticRetriever(
                embedder=DeterministicEmbedder(), storage_path=root
            )
            self.assertEqual(reopened.status().stale_reason, "active_pointer_invalid")
            self.assertEqual(reopened.search("revenue").hits, ())

            reopened.build(documents("r2"), revision="r2")
            self.assertEqual(reopened.status().stale_reason, "active_pointer_invalid")
            self.assertEqual(reopened.search("revenue").hits, ())

            reopened.activate("r2")
            self.assertEqual(reopened.status().state, "active")
            self.assertEqual(reopened.search("revenue")[0].document.id, "metric:revenue")

    def test_sentence_transformer_missing_dependency_is_explicitly_degraded(self) -> None:
        index = HybridSemanticRetriever(
            embedder=SentenceTransformerEmbedder(
                model_name="missing-test-model",
                device="cpu",
                batch_size=4,
            )
        )
        index.build(documents(), revision="r1")
        status = index.activate("r1")

        self.assertEqual(status.state, "degraded")
        self.assertEqual(status.backend, "lexical")
        self.assertEqual(status.stale_reason, "backend_unavailable")
        self.assertEqual(index.search("revenue")[0].document.id, "metric:revenue")

    def test_lexical_fallback_scales_to_one_thousand_models_with_bounded_memory(self) -> None:
        items: list[SemanticDocument] = []
        for number in range(1_000):
            model = f"synthetic_{number:04d}"
            items.append(SemanticDocument(
                id=f"model:{model}", kind="model", projectRevision="scale-v1",
                model=model, text=f"{model} workforce 人员模型",
                referencedModels=[model],
            ))
            for field in ("department_code", "region_code", "team_size"):
                items.append(SemanticDocument(
                    id=f"column:{model}.{field}", kind="column", projectRevision="scale-v1",
                    model=model, field=field, text=f"{model} {field} 团队规模",
                    referencedModels=[model], referencedColumns=[f"{model}.{field}"],
                ))
        tracemalloc.start()
        started = time.perf_counter()
        index = HybridSemanticRetriever()
        index.build(items, revision="scale-v1")
        index.activate("scale-v1")
        result = index.search("synthetic_0999 team_size", limit=5)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertIn("column:synthetic_0999.team_size", [hit.document.id for hit in result])
        self.assertLess(elapsed, 10.0)
        self.assertLess(peak, 100 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
