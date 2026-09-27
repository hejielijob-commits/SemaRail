from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sidecar.semantic_index import (
    InMemorySemanticIndex,
    SemanticDocument,
    SemanticIndexError,
    build_semantic_documents,
)


class SemanticDocumentBuilderTests(unittest.TestCase):
    def _manifest(self) -> dict:
        return {
            "models": [
                {
                    "name": "employees",
                    "primaryKey": "employee_id",
                    "columns": [
                        {"name": "employee_id", "type": "BIGINT"},
                        {
                            "name": "salary",
                            "type": "DECIMAL",
                            "acceptedValues": ["monthly", "annualized"],
                            "properties": {
                                "description": "Monthly salary / 月薪",
                                "grain": "employee-month",
                                "timeBasis": "payroll month",
                                "dataRange": "2020-present",
                            },
                        },
                    ],
                },
                {"name": "departments", "columns": [{"name": "code", "type": "TEXT"}]},
            ],
            "relationships": [
                {
                    "name": "employees_department",
                    "models": ["employees", "departments"],
                    "joinType": "MANY_TO_ONE",
                    "condition": "employees.department_code = departments.code",
                }
            ],
            "cubes": [
                {
                    "name": "employee_metrics",
                    "baseObject": "employees",
                    "measures": [{"name": "headcount", "expression": "COUNT(employee_id)"}],
                    "dimensions": [{"name": "department", "expression": "department_code"}],
                    "timeDimensions": [{"name": "hired_at", "expression": "hire_date"}],
                }
            ],
            "views": [{"name": "active_employees", "statement": "SELECT * FROM employees"}],
        }

    def test_builds_schema_and_companion_documents_with_locale_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "knowledge/rules").mkdir(parents=True)
            (root / "knowledge/sql").mkdir(parents=True)
            (root / "knowledge/rules/hr.md").write_text(
                "## Current headcount\n\nUse COUNT(DISTINCT employees.employee_id).\n\n"
                "## Current salary\n\nUse the latest salary snapshot.",
                encoding="utf-8",
            )
            (root / "knowledge/sql/headcount.md").write_text(
                "---\nnl: 按部门统计在职人数\nsql: |\n  SELECT department_code FROM employees\n---\n\n"
                "Headcount example.",
                encoding="utf-8",
            )
            locales = {
                "models": {
                    "employees": {
                        "display_name": {"zh-CN": "员工", "en-US": "Employees"},
                        "description": {"zh-CN": "员工主数据", "en-US": "Employee master"},
                        "business_domain": "HR",
                        "columns": {
                            "salary": {
                                "display_name": {"zh-CN": "月薪", "en-US": "Monthly salary"},
                                "semantic_role": "measure",
                                "visible": True,
                            }
                        },
                    },
                },
                "cubes": {
                    "employee_metrics": {
                        "display_name": {"zh-CN": "员工指标", "en-US": "Employee metrics"},
                        "dimensions": {
                            "department": {"display_name": {"zh-CN": "部门"}}
                        },
                        "timeDimensions": {
                            "hired_at": {"display_name": {"zh-CN": "入职时间"}, "grain": "day"}
                        },
                    },
                }
            }
            rule_metadata = {
                "current-headcount": {
                    "models": ["employees"],
                    "fields": ["employees.employee_id"],
                    "aliases": ["current workforce size"],
                    "mandatory": True,
                }
            }
            first = build_semantic_documents(
                self._manifest(), root, project_revision="r1", locales=locales,
                rule_metadata=rule_metadata,
            )
            second = build_semantic_documents(
                self._manifest(), root, project_revision="r1", locales=locales,
                rule_metadata=rule_metadata,
            )

        self.assertEqual(first, second)
        self.assertEqual({item.projectRevision for item in first}, {"r1"})
        self.assertTrue({item.kind for item in first} >= {
            "model", "column", "relationship", "cube", "metric", "dimension",
            "time_dimension", "rule", "sql_example", "view",
        })
        employee = next(item for item in first if item.id == "model:employees")
        self.assertIn("员工", employee.text)
        self.assertIn("Employee master", employee.text)
        salary = next(item for item in first if item.id == "column:employees.salary")
        self.assertEqual(salary.metadata["semanticRole"], "measure")
        self.assertEqual(salary.metadata["acceptedValues"], ["monthly", "annualized"])
        self.assertEqual(salary.metadata["grain"], "employee-month")
        self.assertEqual(salary.metadata["timeBasis"], "payroll month")
        self.assertEqual(salary.metadata["dataRange"], "2020-present")
        self.assertIn("employees.salary", salary.referencedColumns)
        cube = next(item for item in first if item.id == "cube:employee_metrics")
        self.assertEqual(cube.metadata["displayName"]["zh-CN"], "员工指标")
        hired_at = next(item for item in first if item.id == "time_dimension:employee_metrics.hired_at")
        self.assertEqual(hired_at.metadata["grain"], "day")
        self.assertIn("入职时间", hired_at.text)
        relationship = next(item for item in first if item.id == "relationship:employees_department")
        self.assertEqual(set(relationship.referencedModels), {"employees", "departments"})
        self.assertTrue(all(item.contentHash.startswith("sha256:") for item in first))
        self.assertTrue(any(item.id == "rule:hr#current-headcount" for item in first))
        headcount = next(item for item in first if item.id == "rule:hr#current-headcount")
        self.assertEqual(headcount.metadata["aliases"], ["current workforce size"])
        self.assertTrue(headcount.metadata["mandatory"])
        sql_example = next(item for item in first if item.id == "sql_example:knowledge/sql/headcount")
        self.assertIn("employees", sql_example.referencedModels)

    def test_document_hash_changes_when_semantic_content_changes(self) -> None:
        original = SemanticDocument(id="column:x.salary", kind="column", projectRevision="r", text="月薪")
        changed = SemanticDocument(id="column:x.salary", kind="column", projectRevision="r", text="年薪")
        self.assertNotEqual(original.contentHash, changed.contentHash)
        republished = SemanticDocument(id="column:x.salary", kind="column", projectRevision="r2", text="月薪")
        self.assertEqual(original.contentHash, republished.contentHash)


class LexicalIndexTests(unittest.TestCase):
    def _documents(self) -> list[SemanticDocument]:
        return [
            SemanticDocument(
                id="model:employees",
                kind="model",
                projectRevision="r1",
                text="Employees 员工主数据",
            ),
            SemanticDocument(
                id="column:employees.salary",
                kind="column",
                projectRevision="r1",
                model="employees",
                field="salary",
                text="Monthly salary 月薪 measure",
            ),
            SemanticDocument(
                id="column:employees.ssn",
                kind="column",
                projectRevision="r1",
                model="employees",
                field="ssn",
                text="Sensitive identity number",
                visibility={"visible": False},
            ),
            SemanticDocument(
                id="rule:hr#salary",
                kind="rule",
                projectRevision="r1",
                text="Average salary uses the latest snapshot / 平均薪资使用最新快照",
            ),
        ]

    def test_exact_and_chinese_lexical_retrieval_is_deterministic_and_quota_aware(self) -> None:
        index = InMemorySemanticIndex()
        documents = self._documents()
        index.build(documents, revision="r1")
        # Staged indexes are intentionally unavailable until atomically activated.
        self.assertEqual(index.search("salary", revision="r1").indexStatus["indexStatus"], "staged")
        index.activate("r1")
        first = index.search("月薪", revision="r1", limit=10, quotas={"column": 1})
        second = index.search("月薪", revision="r1", limit=10, quotas={"column": 1})
        self.assertEqual([hit.document.id for hit in first], [hit.document.id for hit in second])
        self.assertEqual(first[0].document.id, "column:employees.salary")
        self.assertEqual(first[0].match_type, "lexical")
        self.assertNotIn("column:employees.ssn", [hit.document.id for hit in first])
        exact = index.search("employees.salary", revision="r1")
        self.assertEqual(exact[0].document.id, "column:employees.salary")
        self.assertEqual(exact[0].match_type, "exact")

    def test_revision_mismatch_fails_closed_and_remove_is_safe(self) -> None:
        index = InMemorySemanticIndex()
        index.build(self._documents(), revision="r1")
        index.activate("r1")
        stale = index.search("salary", revision="r2")
        self.assertEqual(stale.indexStatus["indexStatus"], "stale")
        self.assertEqual(len(stale), 0)
        with self.assertRaises(SemanticIndexError):
            index.activate("r2")
        self.assertTrue(index.remove_revision("r1"))
        self.assertEqual(index.status()["indexStatus"], "missing")
        self.assertFalse(index.remove_revision("r1"))

    def test_build_rejects_mixed_revision_and_duplicate_ids(self) -> None:
        index = InMemorySemanticIndex()
        with self.assertRaises(SemanticIndexError):
            index.build(
                [
                    SemanticDocument(id="model:a", kind="model", projectRevision="r1"),
                    SemanticDocument(id="model:b", kind="model", projectRevision="r2"),
                ]
            )
        with self.assertRaises(SemanticIndexError):
            index.build(self._documents() + [self._documents()[0]], revision="r1")


if __name__ == "__main__":
    unittest.main()
