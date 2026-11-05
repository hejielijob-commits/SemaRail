from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from sqlglot import exp, parse_one


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "lifecycle_independent_oracle_review",
    ROOT / "scripts" / "lifecycle-independent-oracle-review.py",
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import setup guard
    raise RuntimeError("oracle review module could not be loaded")
REVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVIEW)

SNAPSHOT_DATES = {
    "salarySnapshot": "2024-09-01",
    "performanceSnapshot": "2024-09-30",
}
FIXTURES = ROOT / "scripts" / "tests" / "fixtures"


def _native_wren_sql(scenario_id: str) -> str:
    return (FIXTURES / f"lifecycle-native-{scenario_id}.sql").read_text(encoding="utf-8")


class NativeWrenShapeTests(unittest.TestCase):
    def _reject_native(self, sql: str, scenario: dict[str, str], snapshot: str) -> None:
        with self.assertRaises(REVIEW.ReviewFailure):
            REVIEW._validate_native_wren_sql(sql, scenario, snapshot)

    def test_exact_wren_model_expansion_is_accepted(self) -> None:
        for scenario_id, scenario in REVIEW.SCENARIOS.items():
            with self.subTest(scenario=scenario["model"]):
                REVIEW._validate_native_wren_sql(
                    _native_wren_sql(scenario_id), scenario, SNAPSHOT_DATES[scenario_id]
                )

    def test_wrong_physical_table_is_rejected(self) -> None:
        for scenario_id, scenario in REVIEW.SCENARIOS.items():
            snapshot = SNAPSHOT_DATES[scenario_id]
            with self.subTest(scenario=scenario["model"]):
                tree = parse_one(_native_wren_sql(scenario_id), read="postgres")
                physical = next(table for table in tree.find_all(exp.Table) if table.db.casefold() == "hr")
                physical.set("this", exp.to_identifier("unlisted_table"))
                self._reject_native(tree.sql(dialect="postgres"), scenario, snapshot)

    def test_extra_join_is_rejected(self) -> None:
        for scenario_id, scenario in REVIEW.SCENARIOS.items():
            snapshot = SNAPSHOT_DATES[scenario_id]
            with self.subTest(scenario=scenario["model"]):
                tree = parse_one(_native_wren_sql(scenario_id), read="postgres")
                tree.set("joins", [exp.Join(this=exp.Table(this=exp.to_identifier("unlisted_table")))])
                self._reject_native(tree.sql(dialect="postgres"), scenario, snapshot)

    def test_extra_cte_is_rejected(self) -> None:
        for scenario_id, scenario in REVIEW.SCENARIOS.items():
            snapshot = SNAPSHOT_DATES[scenario_id]
            with self.subTest(scenario=scenario["model"]):
                tree = parse_one(_native_wren_sql(scenario_id), read="postgres")
                tree.args["with_"].expressions.append(tree.args["with_"].expressions[0].copy())
                self._reject_native(tree.sql(dialect="postgres"), scenario, snapshot)

    def test_non_equality_snapshot_filter_is_rejected(self) -> None:
        for scenario_id, scenario in REVIEW.SCENARIOS.items():
            snapshot = SNAPSHOT_DATES[scenario_id]
            with self.subTest(scenario=scenario["model"]):
                tree = parse_one(_native_wren_sql(scenario_id), read="postgres")
                predicate = tree.args["where"].this
                predicate.replace(exp.NEQ(this=predicate.this.copy(), expression=predicate.expression.copy()))
                self._reject_native(tree.sql(dialect="postgres"), scenario, snapshot)

    def test_missing_snapshot_filter_is_rejected(self) -> None:
        for scenario_id, scenario in REVIEW.SCENARIOS.items():
            snapshot = SNAPSHOT_DATES[scenario_id]
            with self.subTest(scenario=scenario["model"]):
                tree = parse_one(_native_wren_sql(scenario_id), read="postgres")
                tree.set("where", None)
                self._reject_native(tree.sql(dialect="postgres"), scenario, snapshot)

    def test_wrong_metric_is_rejected(self) -> None:
        for scenario_id, scenario in REVIEW.SCENARIOS.items():
            snapshot = SNAPSHOT_DATES[scenario_id]
            with self.subTest(scenario=scenario["model"]):
                tree = parse_one(_native_wren_sql(scenario_id), read="postgres")
                projection = tree.expressions[0]
                aggregate = projection.this if isinstance(projection, exp.Alias) else projection
                aggregate.set("this", exp.column("other_metric"))
                self._reject_native(tree.sql(dialect="postgres"), scenario, snapshot)

    def test_semantic_cte_is_rejected(self) -> None:
        for scenario_id, scenario in REVIEW.SCENARIOS.items():
            snapshot = SNAPSHOT_DATES[scenario_id]
            with self.subTest(scenario=scenario["model"]):
                model = scenario["model"]
                metric = scenario["column"]
                date_column = scenario["date_column"]
                sql = (
                    f"WITH {model} AS (SELECT {metric}, {date_column} FROM {model}) "
                    f"SELECT AVG({metric}) FROM {model} "
                    f"WHERE {date_column} = DATE '{snapshot}'"
                )
                with self.assertRaises(REVIEW.ReviewFailure):
                    REVIEW._validate_query_sql(
                        sql,
                        scenario,
                        snapshot,
                        require_hr_schema=False,
                        code="SEMANTIC_SQL_NOT_CURRENT_SNAPSHOT_AVERAGE",
                    )

    def test_semantic_physical_table_bypass_is_rejected(self) -> None:
        for scenario_id, scenario in REVIEW.SCENARIOS.items():
            snapshot = SNAPSHOT_DATES[scenario_id]
            with self.subTest(scenario=scenario["model"]):
                sql = (
                    f"SELECT AVG({scenario['column']}) FROM hr.{scenario['table']} "
                    f"WHERE {scenario['date_column']} = DATE '{snapshot}'"
                )
                with self.assertRaises(REVIEW.ReviewFailure):
                    REVIEW._validate_query_sql(
                        sql,
                        scenario,
                        snapshot,
                        require_hr_schema=False,
                        code="SEMANTIC_SQL_NOT_CURRENT_SNAPSHOT_AVERAGE",
                    )


class CredentialMetadataBoundaryTests(unittest.TestCase):
    @staticmethod
    def _record(tool: str, metadata: object) -> dict[str, object]:
        return {
            "request": {"method": "tools/call", "params": {"name": tool}},
            "response": {"result": {"structuredContent": {"authorization": metadata}}},
        }

    @staticmethod
    def _safe_metadata() -> dict[str, object]:
        return {
            "policyVersions": ["pol_" + "a" * 32 + ":2"],
            "rowPolicyApplied": False,
            "tableCount": 0,
        }

    def test_only_exact_governed_query_metadata_path_is_allowed(self) -> None:
        record = self._record("semarail_governed_query", self._safe_metadata())
        self.assertFalse(REVIEW._contains_sensitive_header_key(record))

    def test_plan_tool_metadata_is_rejected(self) -> None:
        record = self._record("semarail_plan_query", self._safe_metadata())
        self.assertTrue(REVIEW._contains_sensitive_header_key(record))

    def test_extra_metadata_field_is_rejected(self) -> None:
        metadata = {**self._safe_metadata(), "token": "opaque"}
        self.assertTrue(REVIEW._contains_sensitive_header_key(self._record("semarail_governed_query", metadata)))

    def test_invalid_policy_version_is_rejected(self) -> None:
        metadata = {**self._safe_metadata(), "policyVersions": ["not-a-policy-version"]}
        self.assertTrue(REVIEW._contains_sensitive_header_key(self._record("semarail_governed_query", metadata)))

    def test_bool_is_not_accepted_as_table_count(self) -> None:
        metadata = {**self._safe_metadata(), "tableCount": True}
        self.assertTrue(REVIEW._contains_sensitive_header_key(self._record("semarail_governed_query", metadata)))

    def test_non_bool_row_policy_flag_is_rejected(self) -> None:
        metadata = {**self._safe_metadata(), "rowPolicyApplied": 1}
        self.assertTrue(REVIEW._contains_sensitive_header_key(self._record("semarail_governed_query", metadata)))

    def test_auth_headers_and_nonce_keys_are_rejected(self) -> None:
        self.assertTrue(REVIEW._contains_sensitive_header_key({"requestHeaders": {"authHeaders": {}}}))
        self.assertTrue(REVIEW._contains_sensitive_header_key({"workerNonce": "opaque"}))

    def test_credential_text_under_other_key_is_rejected(self) -> None:
        self.assertTrue(REVIEW._contains_sensitive_secret_text({"comment": "sr_live_" + "a" * 64}))

    def test_nonce_hash_metadata_is_allowed(self) -> None:
        self.assertFalse(REVIEW._contains_sensitive_header_key({"workerNonceSha256": "a" * 64}))


if __name__ == "__main__":
    unittest.main()
