from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import knowledge_delivery


REVISION = "sha256:" + "a" * 64


def question(sql: str | None, *, actor: str = "compensation_admin",
             oracle_type: str = "rows") -> dict[str, object]:
    value: dict[str, object] = {"actor": actor, "oracle": {"type": oracle_type}}
    if sql is not None:
        value["canonical"] = {"semanticSql": sql}
    return value


def context(models: list[dict[str, object]], *, revision: str | None = REVISION,
            schema_version: int = 2) -> dict[str, object]:
    return {
        "schemaVersion": schema_version,
        "projectRevision": revision,
        "schema": {"models": models},
    }


def catalog(models: list[dict[str, object]], *, revision: str | None = REVISION,
            schema_version: int = 1) -> dict[str, object]:
    return {
        "schemaVersion": schema_version,
        "projectRevision": revision,
        "models": models,
        "relationships": [],
    }


def model(name: str, columns: list[str], *, table: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "name": name,
        "columns": [{"name": item, "type": "TEXT"} for item in columns],
    }
    if table is not None:
        value["table"] = table
    return value


class KnowledgeDeliveryTests(unittest.TestCase):
    def test_context_omission_is_recovered_by_same_revision_authorized_catalog(self) -> None:
        assigned = question(
            "SELECT job_title, COUNT(DISTINCT employee_id) FROM employees "
            "WHERE region_code = 'APAC' AND resigned = FALSE GROUP BY job_title"
        )
        visible_context = context([model("employees", ["employee_id", "region_code", "resigned"])])
        authorized_catalog = catalog([model(
            "employees", ["employee_id", "job_title", "region_code", "resigned"],
            table="hr.employees",
        )])

        direct = knowledge_delivery.context_coverage(assigned, visible_context)
        final = knowledge_delivery.catalog_coverage(assigned, visible_context, authorized_catalog)

        self.assertFalse(direct["passed"])
        self.assertTrue(direct["catalogNeeded"])
        self.assertEqual(direct["requiredColumnCount"], 4)
        self.assertEqual(direct["contextCoveredColumnCount"], 3)
        self.assertTrue(final["passed"])
        self.assertTrue(final["fallbackUsed"])
        self.assertTrue(final["catalogNeeded"])
        self.assertEqual(final["catalogCoveredColumnCount"], 4)
        self.assertEqual(final["contextRevision"], final["catalogRevision"])
        self.assertEqual(final["reasonCodes"], [])

        rendered = repr((direct, final))
        self.assertNotIn("job_title", rendered)
        self.assertNotIn("employees", rendered)
        self.assertNotIn("SELECT", rendered)

    def test_both_context_and_catalog_missing_required_column_fail_closed(self) -> None:
        assigned = question("SELECT job_title FROM employees")
        visible = [model("employees", ["employee_id"])]
        result = knowledge_delivery.catalog_coverage(
            assigned,
            context(visible),
            catalog(visible),
        )

        self.assertFalse(result["passed"])
        self.assertTrue(result["fallbackUsed"])
        self.assertIn("required_fields_missing_from_catalog", result["reasonCodes"])

    def test_aliases_conjunction_count_distinct_and_unqualified_single_source(self) -> None:
        assigned = question(
            "SELECT e.region_code, COUNT(DISTINCT e.employee_id) AS staff "
            "FROM employees AS e WHERE e.region_code = 'APAC' AND resigned = FALSE "
            "GROUP BY e.region_code"
        )
        available = context([model("employees", ["employee_id", "region_code", "resigned"])])

        result = knowledge_delivery.context_coverage(assigned, available)

        self.assertTrue(result["passed"])
        self.assertEqual(result["requiredModelCount"], 1)
        self.assertEqual(result["requiredColumnCount"], 3)
        self.assertEqual(result["contextCoveredColumnCount"], 3)

    def test_cte_column_aliases_resolve_back_to_source_columns(self) -> None:
        assigned = question(
            "WITH scoped (staff_id, position, area) AS ("
            "SELECT e.employee_id, e.job_title, e.region_code FROM employees AS e "
            "WHERE e.resigned = FALSE AND e.region_code = 'APAC') "
            "SELECT s.position FROM scoped AS s WHERE s.staff_id > 0"
        )
        available = context([model(
            "employees", ["employee_id", "job_title", "region_code", "resigned"]
        )])

        result = knowledge_delivery.context_coverage(assigned, available)

        self.assertTrue(result["passed"])
        self.assertEqual(result["requiredColumnCount"], 4)

    def test_denial_case_excludes_forbidden_relation_from_delivery_requirements(self) -> None:
        assigned = question(
            "SELECT employee_id, review_date FROM performance_reviews",
            actor="employee",
            oracle_type="denial",
        )
        visible = context([])
        authorized = catalog([model("employees", ["employee_id"], table="hr.employees")])

        direct = knowledge_delivery.context_coverage(assigned, visible)
        final = knowledge_delivery.catalog_coverage(assigned, visible, authorized)

        self.assertTrue(direct["passed"])
        self.assertTrue(final["passed"])
        self.assertEqual(final["requiredModelCount"], 0)
        self.assertEqual(final["requiredColumnCount"], 0)
        self.assertEqual(final["denialExcludedModelCount"], 1)
        self.assertEqual(final["denialExcludedColumnCount"], 2)
        self.assertNotIn("performance_reviews", repr(final))

    def test_denial_case_does_not_require_a_forbidden_column(self) -> None:
        assigned = question(
            "SELECT employee_id, gender FROM employees",
            actor="employee",
            oracle_type="denial",
        )
        visible = [model("employees", ["employee_id"])]

        result = knowledge_delivery.catalog_coverage(
            assigned,
            context(visible),
            catalog([model("employees", ["employee_id"], table="hr.employees")]),
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["requiredColumnCount"], 1)
        self.assertEqual(result["denialExcludedColumnCount"], 1)

    def test_non_denial_canonical_cannot_require_an_actor_denied_column(self) -> None:
        result = knowledge_delivery.context_coverage(
            question("SELECT gender FROM employees", actor="employee"),
            context([model("employees", ["gender"])]),
        )

        self.assertFalse(result["passed"])
        self.assertIn("canonical_uses_unauthorized_reference", result["reasonCodes"])

    def test_forbidden_model_or_column_in_projection_fails_closed(self) -> None:
        assigned = question("SELECT employee_id FROM employees", actor="employee")
        forbidden_model_context = context([
            model("employees", ["employee_id"]),
            model("performance_reviews", ["employee_id"]),
        ])
        forbidden_column_catalog = catalog([
            model("employees", ["employee_id", "gender"], table="hr.employees"),
        ])

        context_result = knowledge_delivery.context_coverage(assigned, forbidden_model_context)
        catalog_result = knowledge_delivery.catalog_coverage(
            assigned,
            context([model("employees", ["employee_id"])]),
            forbidden_column_catalog,
        )

        self.assertFalse(context_result["passed"])
        self.assertIn("forbidden_context_model_present", context_result["reasonCodes"])
        self.assertFalse(catalog_result["passed"])
        self.assertIn("forbidden_catalog_column_present", catalog_result["reasonCodes"])

    def test_revision_missing_or_drifted_never_passes(self) -> None:
        assigned = question("SELECT employee_id FROM employees")
        visible = [model("employees", ["employee_id"])]

        missing_context = knowledge_delivery.context_coverage(
            assigned, context(visible, revision=None)
        )
        missing_catalog = knowledge_delivery.catalog_coverage(
            assigned,
            context(visible),
            catalog(visible, revision=None),
        )
        drift = knowledge_delivery.catalog_coverage(
            assigned,
            context(visible),
            catalog(visible, revision="sha256:" + "b" * 64),
        )

        self.assertFalse(missing_context["passed"])
        self.assertIn("context_revision_missing", missing_context["reasonCodes"])
        self.assertFalse(missing_catalog["passed"])
        self.assertIn("catalog_revision_missing", missing_catalog["reasonCodes"])
        self.assertFalse(drift["passed"])
        self.assertIn("project_revision_mismatch", drift["reasonCodes"])

    def test_ambiguous_unqualified_column_unknown_relation_and_wildcard_fail_closed(self) -> None:
        cases = [
            ("SELECT employee_id FROM employees e JOIN departments d ON e.department_id = d.department_id",
             "unqualified_column_ambiguous"),
            ("SELECT missing_field FROM made_up_model", "unknown_relation"),
            ("SELECT * FROM employees", "wildcard_unverifiable"),
            ("SELECT COUNT(*) FROM employees", "wildcard_unverifiable"),
            ("SELECT e.employee_id FROM employees e JOIN departments d USING (department_id)",
             "canonical_join_unsupported"),
            ("SELECT e.employee_id FROM employees e NATURAL JOIN departments d",
             "canonical_join_unsupported"),
        ]
        for sql, reason in cases:
            with self.subTest(reason=reason):
                result = knowledge_delivery.context_coverage(
                    question(sql), context([model("employees", ["employee_id"])]),
                )
                self.assertFalse(result["passed"])
                self.assertIn(reason, result["reasonCodes"])

    def test_unprojected_cte_field_and_unparseable_sql_fail_closed(self) -> None:
        cases = [
            ("WITH e AS (SELECT employee_id FROM employees) SELECT e.job_title FROM e",
             "canonical_source_unresolved"),
            ("SELECT FROM", "canonical_parse_failed"),
        ]
        for sql, reason in cases:
            with self.subTest(reason=reason):
                result = knowledge_delivery.context_coverage(question(sql), context([]))
                self.assertFalse(result["passed"])
                self.assertIn(reason, result["reasonCodes"])

    def test_no_canonical_is_allowed_only_for_expected_blocked_question(self) -> None:
        blocked = knowledge_delivery.context_coverage(
            question(None, actor="hr_director", oracle_type="blocked"),
            context([]),
        )
        missing = knowledge_delivery.context_coverage(
            question(None, actor="hr_director", oracle_type="rows"),
            context([]),
        )

        self.assertTrue(blocked["passed"])
        self.assertEqual(blocked["requiredColumnCount"], 0)
        self.assertFalse(missing["passed"])
        self.assertIn("canonical_missing", missing["reasonCodes"])

    def test_all_benchmark_canonical_queries_have_supported_source_lineage(self) -> None:
        corpus_path = HERE / "corpus.json"
        questions = json.loads(corpus_path.read_text(encoding="utf-8"))["questions"]
        self.assertEqual(len(questions), 30)
        empty_context = context([])

        for assigned in questions:
            with self.subTest(question_id=assigned["id"]):
                result = knowledge_delivery.context_coverage(assigned, empty_context)
                if result["reasonCodes"] == []:
                    self.assertEqual(result["requiredModelCount"], 0)
                    self.assertEqual(result["requiredColumnCount"], 0)
                    self.assertTrue(result["passed"])
                else:
                    self.assertEqual(result["reasonCodes"], ["required_fields_missing_from_context"])
                    self.assertGreater(
                        result["requiredModelCount"] + result["requiredColumnCount"], 0
                    )

    def test_model_table_alias_collision_fails_even_when_column_sets_match(self) -> None:
        assigned = question("SELECT employee_id FROM employees")
        projection = [
            model("employees", ["employee_id"]),
            model("employee_view", ["employee_id"], table="hr.employees"),
        ]

        direct = knowledge_delivery.context_coverage(assigned, context(projection))
        catalog_result = knowledge_delivery.catalog_coverage(
            assigned,
            context([model("employees", ["employee_id"])]),
            catalog(projection),
        )

        self.assertFalse(direct["passed"])
        self.assertIn("projection_model_ambiguous", direct["reasonCodes"])
        self.assertFalse(catalog_result["passed"])
        self.assertIn("projection_model_ambiguous", catalog_result["reasonCodes"])

    def test_arbitrary_revision_text_is_not_returned_or_accepted(self) -> None:
        assigned = question("SELECT employee_id FROM employees")
        visible = [model("employees", ["employee_id"])]
        untrusted_revision = "not-a-revision:contains-arbitrary-text"

        direct = knowledge_delivery.context_coverage(
            assigned, context(visible, revision=untrusted_revision)
        )
        combined = knowledge_delivery.catalog_coverage(
            assigned,
            context(visible, revision=untrusted_revision),
            catalog(visible, revision=untrusted_revision),
        )

        self.assertFalse(direct["passed"])
        self.assertIsNone(direct["contextRevision"])
        self.assertIn("context_revision_missing", direct["reasonCodes"])
        self.assertFalse(combined["passed"])
        self.assertIsNone(combined["contextRevision"])
        self.assertIsNone(combined["catalogRevision"])
        self.assertNotIn(untrusted_revision, repr((direct, combined)))


if __name__ == "__main__":
    unittest.main()
