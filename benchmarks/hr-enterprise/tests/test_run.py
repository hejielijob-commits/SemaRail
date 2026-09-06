from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO_ROOT = HERE.parents[1]


def load_runner():
    spec = importlib.util.spec_from_file_location("hr_benchmark_run", HERE / "run.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = load_runner()
        sys.path.insert(0, str(REPO_ROOT / "apps" / "semantic-console"))
        from server.authorization import validate_policy_document
        cls.validate_policy_document = staticmethod(validate_policy_document)

    def test_all_five_policy_documents_validate_and_cover_every_table(self) -> None:
        for actor in ("employee", "department_manager", "hrbp", "compensation_admin", "hr_director"):
            with self.subTest(actor=actor):
                document = self.runner.policy_document(actor, "source_test")
                self.validate_policy_document(document)
                self.assertEqual(set(document["tables"]), set(self.runner.TABLES))
                self.assertEqual(document["limits"]["timeoutMs"], 10_000)

    def test_sensitive_table_denials_are_explicit(self) -> None:
        employee = self.runner.policy_document("employee", "source_test")["tables"]
        manager = self.runner.policy_document("department_manager", "source_test")["tables"]
        hrbp = self.runner.policy_document("hrbp", "source_test")["tables"]
        compensation = self.runner.policy_document("compensation_admin", "source_test")["tables"]
        self.assertEqual(employee["hr.performance_reviews"]["effect"], "deny")
        self.assertEqual(manager["hr.compensation_history"]["effect"], "deny")
        self.assertEqual(hrbp["hr.compensation_history"]["effect"], "deny")
        self.assertEqual(compensation["hr.performance_reviews"]["effect"], "deny")
        self.assertEqual(compensation["hr.attendance_monthly"]["effect"], "deny")
        self.assertEqual(employee["hr.employees"]["columns"]["deny"], ["gender", "age"])

    def test_top_level_policy_denial_is_accepted_only_with_zero_rows(self) -> None:
        question = {
            "oracle": {"type": "denial", "errorCode": "POLICY_DENIED", "databaseRowsReturned": 0},
        }
        result = {
            "schemaVersion": 2,
            "status": "error",
            "error": {"code": "POLICY_DENIED"},
            "stats": {"returnedRows": 0, "durationMs": 12, "truncated": False},
        }
        self.assertEqual(self.runner._assert_scenario(question, result)[:2], (True, "PASS"))
        result["stats"]["returnedRows"] = 1
        self.assertEqual(self.runner._assert_scenario(question, result)[0], False)

    def test_result_mismatch_and_runtime_limit_are_distinguished(self) -> None:
        question = {
            "oracle": {
                "type": "canonical_query",
                "rows": [{"headcount": 10}],
                "numericTolerance": "0.01",
                "orderSensitive": False,
            },
        }
        result = {
            "status": "success",
            "delivery": "inline",
            "previewRows": [{"headcount": 11}],
            "stats": {"durationMs": 12, "truncated": False},
        }
        self.assertEqual(self.runner._assert_scenario(question, result)[1], "ORACLE_MISMATCH")
        result["previewRows"] = [{"headcount": 10}]
        result["stats"]["durationMs"] = 10_001
        self.assertEqual(self.runner._assert_scenario(question, result)[1], "RUNTIME_LIMIT")

    def test_corpus_shape_and_thresholds_are_fixed(self) -> None:
        corpus = json.loads((HERE / "golden-questions.json").read_text(encoding="utf-8"))
        questions = corpus["questions"]
        self.assertEqual(len(questions), 60)
        self.assertEqual(sum(item["language"] == "zh-CN" for item in questions), 40)
        self.assertEqual(sum(item["language"] == "en" for item in questions), 20)
        self.assertEqual(sum(item["category"] == "authorization" for item in questions), 15)
        self.assertEqual(corpus["thresholds"], {
            "firstPass": 48, "afterAtMostOneRepair": 54, "authorization": 15,
        })
        authorization_features = {
            feature
            for item in questions if item["category"] == "authorization"
            for feature in item["features"]
        }
        self.assertTrue({
            "cross_department", "cross_region", "sensitive_column",
            "bulk_export", "complex_sql", "cte",
        }.issubset(authorization_features))
        manager_count = next(item for item in questions if item["id"] == "manager-report-count")
        self.assertNotIn("COUNT(*)", manager_count["canonical"]["semanticSql"].upper())

    def test_model_facing_questions_state_non_obvious_result_contracts(self) -> None:
        corpus = json.loads((HERE / "golden-questions.json").read_text(encoding="utf-8"))
        questions = {item["id"]: item["question"] for item in corpus["questions"]}
        required_phrases = {
            "headcount-department": ("20", "department_code", "从高到低"),
            "average-training": ("20", "department_code", "从高到低"),
            "performance-trend": ("review_date", "不要重新截断日期"),
            "training-performance": ("20", "60", "low", "medium", "high"),
            "overtime-performance-department": ("10", "平均加班时数", "平均绩效"),
            "salary-change-title": ("10", "2024-03-01", "2024-09-01"),
            "education-count": ("从高到低", "相同时"),
            "manager-count": ("当前在职员工", "直属下属"),
        }
        for identifier, phrases in required_phrases.items():
            with self.subTest(identifier=identifier):
                for phrase in phrases:
                    self.assertIn(phrase, questions[identifier])

    def test_sql_knowledge_examples_match_benchmark_contracts(self) -> None:
        corpus = json.loads((HERE / "golden-questions.json").read_text(encoding="utf-8"))
        canonical = {item["id"]: item["canonical"]["semanticSql"] for item in corpus["questions"]}
        examples = {
            "headcount-department": "current-headcount-by-department.md",
            "current-salary-region": "current-average-salary.md",
            "performance-trend": "quarterly-performance-trend.md",
            "salary-performance-region": "current-salary-performance-by-region.md",
        }
        for identifier, filename in examples.items():
            with self.subTest(identifier=identifier):
                raw = (HERE / "project" / "knowledge" / "sql" / filename).read_text(encoding="utf-8")
                sql_block = raw.split("sql: |", 1)[1].split("---", 1)[0]
                actual = " ".join(line.strip() for line in sql_block.splitlines() if line.strip())
                self.assertEqual(canonical[identifier], actual)

    def test_oracle_accepts_declared_aliases_and_safe_extra_columns_only(self) -> None:
        question = {
            "oracle": {
                "type": "canonical_query",
                "rows": [{"headcount": 10}],
                "numericTolerance": "0.01",
                "orderSensitive": False,
                "columnAliases": {"headcount": ["active_count"]},
                "allowedExtraColumns": ["employee_id"],
            },
        }
        result = {
            "status": "success",
            "delivery": "inline",
            "previewRows": [{"active_count": 10, "employee_id": 1}],
            "stats": {"durationMs": 12, "truncated": False},
        }
        self.assertEqual(self.runner._assert_scenario(question, result)[:2], (True, "PASS"))
        result["previewRows"][0]["salary"] = 100
        self.assertEqual(self.runner._assert_scenario(question, result)[1], "ORACLE_MISMATCH")


if __name__ == "__main__":
    unittest.main()
