from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.access_control import AccessControlStore
from server.business_evidence import validate_evidence
from server.business_verification import RULES_VERSION, evaluate_query
from server.knowledge import validate_execution_checks
from server.models import DatasourceRecord
from server.project import ProjectError, ProjectStore
from server.query_preparation import PreparationError, QueryPreparationStore
from server.service import SemanticConsoleService


class FakeValidator:
    def health(self):
        return {"available": True}

    def validate(self, _project_dir):
        return {"valid": True, "errors": [], "warnings": []}

    def build(self, _project_dir):
        return {"models": []}


class FakeIndexLifecycle:
    def stage(self, _manifest, _project_dir, _revision):
        pass

    def activate(self, _revision):
        pass

    def status(self, _revision=None):
        return {"status": "ready"}


class BusinessVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-business-verification-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project_dir = root / "project"
        self.project_dir.mkdir()
        (self.project_dir / "wren_project.yml").write_text(
            "schema_version: 5\nname: trust-test\ndata_source: postgres\n", encoding="utf-8"
        )
        self._model("employees", ["employee_id", "resigned", "department_code"])
        self._model("departments", ["department_code", "region_code"])
        self._model("compensation_history", ["employee_id", "effective_date", "monthly_salary"])
        self._model("performance_reviews", ["employee_id", "review_date", "performance_score"])
        (self.project_dir / "relationships.yml").write_text(
            "relationships:\n"
            "  - name: employees_department\n"
            "    models: [employees, departments]\n"
            "    join_type: MANY_TO_ONE\n"
            "    condition: employees.department_code = departments.department_code\n"
            "  - name: performance_employee\n"
            "    models: [performance_reviews, employees]\n"
            "    join_type: MANY_TO_ONE\n"
            "    condition: performance_reviews.employee_id = employees.employee_id\n"
            "  - name: compensation_employee\n"
            "    models: [compensation_history, employees]\n"
            "    join_type: MANY_TO_ONE\n"
            "    condition: compensation_history.employee_id = employees.employee_id\n",
            encoding="utf-8",
        )
        self.project = ProjectStore(
            self.project_dir,
            state_dir=root / "state",
            validator=FakeValidator(),
            semantic_index_lifecycle=FakeIndexLifecycle(),
        )
        self.service = SemanticConsoleService(self.project)

    def _model(self, name: str, columns: list[str]) -> None:
        folder = self.project_dir / "models" / name
        folder.mkdir(parents=True, exist_ok=True)
        body = "name: " + name + "\ncolumns:\n" + "".join(f"  - name: {column}\n    type: VARCHAR\n" for column in columns)
        (folder / "metadata.yml").write_text(body, encoding="utf-8")

    def _rule(self, title: str, checks: list[dict], *, confirmation=None) -> dict:
        payload = {
            "title": title,
            "content": "Published business verification rule.",
            "executionChecks": {"schemaVersion": 1, "checks": checks},
        }
        if confirmation is not None:
            payload["confirmationRule"] = confirmation
        rule = self.service.create_rule(payload)["rule"]
        self.project.publish()
        return rule

    def _headcount_rule(self):
        return self._rule("Active headcount", [
            {"id": "headcount", "kind": "metric", "model": "employees", "column": "employee_id", "aggregation": "count", "distinct": True},
            {"id": "active-only", "kind": "predicate", "model": "employees", "column": "resigned", "operator": "=", "value": False},
        ])

    def _check(self, evidence: dict, check_id: str) -> dict:
        return next(item for item in evidence["checks"] if item["id"].endswith(":" + check_id))

    @staticmethod
    def _ready(evidence: dict) -> bool:
        return all(
            item["status"] == "passed"
            or (not item["mandatory"] and item["status"] == "unverified")
            for item in evidence["checks"]
        )

    def test_headcount_requires_distinct_employee_and_false_resigned_predicate(self):
        rule = self._headcount_rule()
        valid = evaluate_query(
            self.project,
            "SELECT COUNT(DISTINCT e.employee_id) AS headcount FROM employees e WHERE e.resigned = FALSE",
            {},
            [],
        )
        self.assertTrue(self._ready(valid))
        self.assertEqual(self._check(valid, "headcount")["status"], "passed")
        self.assertEqual(self._check(valid, "active-only")["status"], "passed")

        non_distinct = evaluate_query(
            self.project,
            "SELECT COUNT(e.employee_id) FROM employees e WHERE e.resigned = FALSE",
            {},
            [],
        )
        self.assertEqual(self._check(non_distinct, "headcount")["status"], "conflict")
        self.assertFalse(self._ready(non_distinct))

        misleading_alias = evaluate_query(
            self.project,
            "SELECT COUNT(DISTINCT e.department_code) AS employee_id FROM employees e WHERE e.resigned = FALSE",
            {},
            [],
        )
        self.assertEqual(self._check(misleading_alias, "headcount")["status"], "unverified")
        self.assertFalse(self._ready(misleading_alias))

        missing_filter = evaluate_query(self.project, "SELECT COUNT(DISTINCT employee_id) FROM employees", {}, [])
        self.assertEqual(self._check(missing_filter, "active-only")["status"], "unverified")

        contradictory = evaluate_query(
            self.project,
            "SELECT COUNT(DISTINCT employee_id) FROM employees WHERE resigned = FALSE AND resigned = TRUE",
            {},
            [],
        )
        self.assertEqual(self._check(contradictory, "active-only")["status"], "conflict")

        aggregate_only_in_order = evaluate_query(
            self.project,
            "SELECT 1 FROM employees ORDER BY COUNT(DISTINCT employee_id)",
            {},
            [],
        )
        self.assertEqual(self._check(aggregate_only_in_order, "headcount")["status"], "unverified")

        detail = evaluate_query(
            self.project,
            "SELECT employee_id, resigned FROM employees WHERE resigned = TRUE",
            {},
            [],
        )
        self.assertFalse(any(item["ruleId"] == rule["id"] for item in detail["checks"]))

    def test_only_published_rules_affect_evidence_and_revision(self):
        rule = self.service.create_rule({
            "title": "Active headcount",
            "content": "Draft only.",
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "headcount", "kind": "metric", "model": "employees", "column": "employee_id", "aggregation": "count", "distinct": True},
                {"id": "active-only", "kind": "predicate", "model": "employees", "column": "resigned", "operator": "=", "value": False},
            ]},
        })["rule"]
        before_publish = evaluate_query(self.project, "SELECT COUNT(*) FROM employees", {}, [])
        self.assertEqual(before_publish["references"], [])
        self.assertTrue(self._ready(before_publish))

        self.project.publish()
        published = evaluate_query(self.project, "SELECT COUNT(*) FROM employees", {}, [])
        self.assertFalse(self._ready(published))
        revision = published["projectRevision"]

        self.service.update_rule(rule["id"], {"content": "Changed draft only."})
        after_draft = evaluate_query(self.project, "SELECT COUNT(*) FROM employees", {}, [])
        self.assertEqual(after_draft["projectRevision"], revision)
        self.assertEqual(after_draft["references"], published["references"])

    def test_execution_checks_fail_closed_on_unknown_schema_versions_and_fields(self):
        with self.assertRaises(ProjectError):
            validate_execution_checks({"schemaVersion": 2, "checks": []})
        with self.assertRaises(ProjectError):
            validate_execution_checks({
                "schemaVersion": 1,
                "checks": [{"kind": "metric", "model": "employees", "column": "employee_id", "aggregation": "count", "distinct": True, "unreviewed": True}],
            })
        trend_checks = validate_execution_checks({
            "schemaVersion": 1,
            "checks": [{
                "id": "trend",
                "kind": "timeGrouping",
                "model": "performance_reviews",
                "column": "review_date",
                "mode": "original",
                "whenGrouped": True,
                "conditionKey": "trend",
            }],
        })
        self.assertEqual(trend_checks["checks"][0]["conditionKey"], "trend")
        for invalid_check in (
            {"id": "trend", "kind": "timeGrouping", "model": "performance_reviews", "column": "review_date", "mode": "original", "whenGrouped": "yes"},
            {"id": "trend", "kind": "timeGrouping", "model": "performance_reviews", "column": "review_date", "mode": "original", "conditionKey": "trend"},
        ):
            with self.subTest(invalid_check=invalid_check), self.assertRaises(ProjectError):
                validate_execution_checks({"schemaVersion": 1, "checks": [invalid_check]})

    def test_salary_date_must_be_confirmed_or_match_published_default(self):
        self._rule("Current salary", [
            {"id": "average-salary", "kind": "metric", "model": "compensation_history", "column": "monthly_salary", "aggregation": "avg"},
            {"id": "effective-date", "kind": "timeFilter", "model": "compensation_history", "column": "effective_date", "conditionKey": "effectiveDate", "valueType": "date", "confirmation": "confirmedOrDefault", "defaultValue": "2024-09-01"},
            {"id": "salary-trend", "kind": "timeGrouping", "model": "compensation_history", "column": "effective_date", "mode": "original", "whenGrouped": True, "conditionKey": "trend"},
        ])
        query = "SELECT AVG(monthly_salary) FROM compensation_history WHERE effective_date = DATE '2024-09-01'"
        defaulted = evaluate_query(self.project, query, {}, [])
        self.assertTrue(self._ready(defaulted))
        self.assertEqual(defaulted["defaultsApplied"], {"effectiveDate": "2024-09-01"})
        self.assertEqual(defaulted["confirmations"]["effectiveDate"], "defaulted")
        self.assertEqual(self._check(defaulted, "salary-trend")["reasonCode"], "time_grouping_not_applicable")

        mismatched = evaluate_query(
            self.project,
            "SELECT AVG(monthly_salary) FROM compensation_history WHERE effective_date = DATE '2024-03-01'",
            {},
            [],
        )
        self.assertEqual(self._check(mismatched, "effective-date")["status"], "conflict")

        unconfirmed = evaluate_query(
            self.project,
            "SELECT AVG(monthly_salary) FROM compensation_history WHERE effective_date = DATE '2024-03-01'",
            {"effectiveDate": "2024-03-01"},
            [],
        )
        self.assertEqual(self._check(unconfirmed, "effective-date")["status"], "unverified")
        self.assertEqual(unconfirmed["confirmations"]["effectiveDate"], "missing")

    def test_performance_range_and_trend_require_original_review_date(self):
        self._rule("Quarterly performance", [
            {"id": "average-performance", "kind": "metric", "model": "performance_reviews", "column": "performance_score", "aggregation": "avg"},
            {"id": "review-period", "kind": "timeFilter", "model": "performance_reviews", "column": "review_date", "conditionKey": "reviewPeriod", "valueType": "dateRange", "confirmation": "confirmed"},
            {"id": "review-trend", "kind": "timeGrouping", "model": "performance_reviews", "column": "review_date", "mode": "original", "whenGrouped": True, "conditionKey": "trend"},
        ])
        date_range = {"start": "2024-07-01", "end": "2024-09-30"}
        quarterly = evaluate_query(
            self.project,
            "SELECT AVG(performance_score) FROM performance_reviews WHERE review_date BETWEEN DATE '2024-07-01' AND DATE '2024-09-30'",
            {"reviewPeriod": date_range},
            ["reviewPeriod"],
        )
        self.assertTrue(self._ready(quarterly))
        self.assertEqual(self._check(quarterly, "review-period")["status"], "passed")
        self.assertEqual(self._check(quarterly, "review-trend")["reasonCode"], "time_grouping_not_applicable")

        sql_only = evaluate_query(
            self.project,
            "SELECT AVG(performance_score) FROM performance_reviews WHERE review_date BETWEEN DATE '2024-07-01' AND DATE '2024-09-30'",
            {},
            [],
        )
        self.assertEqual(sql_only["time"]["source"], "sql")
        self.assertEqual(sql_only["confirmations"]["reviewPeriod"], "missing")
        self.assertEqual(self._check(sql_only, "review-period")["reasonCode"], "time_confirmation_missing")

        trend = evaluate_query(
            self.project,
            "SELECT review_date, AVG(performance_score) FROM performance_reviews GROUP BY 1 ORDER BY 1",
            {},
            [],
        )
        self.assertFalse(self._ready(trend))
        self.assertEqual(self._check(trend, "review-period")["status"], "unverified")
        self.assertEqual(self._check(trend, "review-period")["reasonCode"], "time_condition_missing")
        self.assertEqual(trend["confirmations"]["reviewPeriod"], "missing")
        self.assertEqual(self._check(trend, "review-trend")["reasonCode"], "original_date_grouping")

        trend_without_group = evaluate_query(
            self.project,
            "SELECT AVG(performance_score) FROM performance_reviews",
            {"trend": True},
            ["trend"],
        )
        self.assertEqual(self._check(trend_without_group, "review-trend")["reasonCode"], "time_grouping_missing")
        self.assertFalse(self._ready(trend_without_group))

        unconfirmed_trend_without_group = evaluate_query(
            self.project,
            "SELECT AVG(performance_score) FROM performance_reviews",
            {"trend": True},
            [],
        )
        self.assertEqual(self._check(unconfirmed_trend_without_group, "review-trend")["reasonCode"], "time_grouping_confirmation_missing")
        self.assertFalse(self._ready(unconfirmed_trend_without_group))

        truncated = evaluate_query(
            self.project,
            "SELECT DATE_TRUNC('quarter', review_date), AVG(performance_score) FROM performance_reviews GROUP BY 1",
            {},
            [],
        )
        self.assertEqual(self._check(truncated, "review-trend")["status"], "conflict")
        self.assertFalse(self._ready(truncated))

        for extra_predicate in (
            "review_date = DATE '2024-08-01'",
            "review_date <> DATE '2024-08-01'",
            "review_date >= DATE '2024-07-15'",
        ):
            with self.subTest(extra_predicate=extra_predicate):
                narrowed = evaluate_query(
                    self.project,
                    "SELECT AVG(performance_score) FROM performance_reviews "
                    "WHERE review_date BETWEEN DATE '2024-07-01' AND DATE '2024-09-30' "
                    f"AND {extra_predicate}",
                    {"reviewPeriod": date_range},
                    ["reviewPeriod"],
                )
                self.assertEqual(self._check(narrowed, "review-period")["status"], "conflict")
                self.assertFalse(self._ready(narrowed))

    def test_original_date_grouping_does_not_confirm_a_missing_or_unconfirmed_range(self):
        self._rule("Confirmed performance period", [
            {"id": "review-period", "kind": "timeFilter", "model": "performance_reviews", "column": "review_date", "conditionKey": "reviewPeriod", "valueType": "dateRange", "confirmation": "confirmed"},
        ])
        date_range = {"start": "2024-07-01", "end": "2024-09-30"}
        for grouping in ("GROUP BY 1", "GROUP BY period"):
            with self.subTest(grouping=grouping):
                projection = "review_date AS period" if grouping.endswith("period") else "review_date"
                sql = (
                    f"SELECT {projection}, AVG(performance_score) FROM performance_reviews "
                    "WHERE review_date BETWEEN DATE '2024-07-01' AND DATE '2024-09-30' "
                    f"{grouping}"
                )
                missing = evaluate_query(self.project, sql, {}, [])
                check = self._check(missing, "review-period")
                self.assertEqual(check["status"], "unverified")
                self.assertEqual(check["reasonCode"], "time_confirmation_missing")
                self.assertEqual(missing["confirmations"]["reviewPeriod"], "missing")
                self.assertEqual(missing["time"]["source"], "sql")
                self.assertEqual(missing["time"]["value"], date_range)
                self.assertFalse(self._ready(missing))

                unconfirmed = evaluate_query(self.project, sql, {"reviewPeriod": date_range}, [])
                self.assertEqual(self._check(unconfirmed, "review-period")["status"], "unverified")
                self.assertEqual(unconfirmed["confirmations"]["reviewPeriod"], "missing")
                self.assertFalse(self._ready(unconfirmed))

                confirmed = evaluate_query(self.project, sql, {"reviewPeriod": date_range}, ["reviewPeriod"])
                self.assertEqual(self._check(confirmed, "review-period")["status"], "passed")
                self.assertEqual(confirmed["confirmations"]["reviewPeriod"], "confirmed")
                self.assertTrue(self._ready(confirmed))

                invalid = evaluate_query(self.project, sql, {"reviewPeriod": {"start": "2024-07", "end": "2024-09-30"}}, ["reviewPeriod"])
                self.assertEqual(self._check(invalid, "review-period")["status"], "conflict")
                self.assertFalse(self._ready(invalid))

    def test_grouped_date_requires_confirmation_but_accepts_a_published_exact_default(self):
        rule = self._rule("Confirmed effective date", [
            {"id": "effective-date", "kind": "timeFilter", "model": "compensation_history", "column": "effective_date", "conditionKey": "effectiveDate", "valueType": "date", "confirmation": "confirmed"},
        ])
        grouped_sql = (
            "SELECT effective_date AS day, AVG(monthly_salary) FROM compensation_history "
            "WHERE effective_date = DATE '2024-09-01' GROUP BY day"
        )
        missing = evaluate_query(self.project, grouped_sql, {}, [])
        self.assertEqual(self._check(missing, "effective-date")["status"], "unverified")
        self.assertEqual(missing["time"]["source"], "sql")
        self.assertEqual(missing["time"]["value"], "2024-09-01")
        self.assertEqual(missing["confirmations"]["effectiveDate"], "missing")
        self.assertFalse(self._ready(missing))

        confirmed_mismatch = evaluate_query(self.project, grouped_sql, {"effectiveDate": "2024-08-01"}, ["effectiveDate"])
        self.assertEqual(self._check(confirmed_mismatch, "effective-date")["status"], "conflict")
        self.assertFalse(self._ready(confirmed_mismatch))

        self.service.update_rule(rule["id"], {"executionChecks": {"schemaVersion": 1, "checks": [
            {"id": "effective-date", "kind": "timeFilter", "model": "compensation_history", "column": "effective_date", "conditionKey": "effectiveDate", "valueType": "date", "confirmation": "confirmedOrDefault", "defaultValue": "2024-09-01"},
        ]}})
        self.project.publish()
        defaulted = evaluate_query(self.project, grouped_sql, {}, [])
        self.assertTrue(self._ready(defaulted))
        self.assertEqual(self._check(defaulted, "effective-date")["status"], "passed")
        self.assertEqual(defaulted["confirmations"]["effectiveDate"], "defaulted")
        self.assertEqual(defaulted["defaultsApplied"]["effectiveDate"], "2024-09-01")
        self.assertEqual(defaulted["time"]["source"], "publishedDefault")
        self.assertEqual(defaulted["time"]["value"], "2024-09-01")
        mismatched_default = evaluate_query(
            self.project, grouped_sql.replace("2024-09-01", "2024-08-01"), {}, [],
        )
        self.assertEqual(self._check(mismatched_default, "effective-date")["status"], "conflict")
        self.assertFalse(self._ready(mismatched_default))

    def test_grouped_range_uses_exact_published_default_without_inferring_confirmation(self):
        period = {"start": "2024-07-01", "end": "2024-09-30"}
        self._rule("Defaulted review period", [
            {"id": "period", "kind": "timeFilter", "model": "performance_reviews", "column": "review_date", "conditionKey": "reviewPeriod", "valueType": "dateRange", "confirmation": "confirmedOrDefault", "defaultValue": period},
        ])
        sql = (
            "SELECT review_date, AVG(performance_score) FROM performance_reviews "
            "WHERE review_date BETWEEN DATE '2024-07-01' AND DATE '2024-09-30' GROUP BY review_date"
        )
        defaulted = evaluate_query(self.project, sql, {}, [])
        self.assertTrue(self._ready(defaulted))
        self.assertEqual(defaulted["confirmations"]["reviewPeriod"], "defaulted")
        self.assertEqual(defaulted["time"]["source"], "publishedDefault")
        self.assertEqual(defaulted["time"]["value"], period)
        mismatch = evaluate_query(self.project, sql.replace("2024-09-30", "2024-08-31"), {}, [])
        self.assertEqual(self._check(mismatch, "period")["status"], "conflict")
        self.assertFalse(self._ready(mismatch))
        unconfirmed = evaluate_query(self.project, sql, {"reviewPeriod": period}, [])
        self.assertEqual(unconfirmed["confirmations"]["reviewPeriod"], "missing")
        self.assertFalse(self._ready(unconfirmed))

    def test_optional_grouped_time_filter_stays_unverified_without_blocking(self):
        self._rule("Optional review period", [
            {"id": "optional-period", "kind": "timeFilter", "model": "performance_reviews", "column": "review_date", "conditionKey": "optionalPeriod", "valueType": "dateRange", "confirmation": "confirmed", "mandatory": False},
        ])
        evidence = evaluate_query(
            self.project,
            "SELECT review_date, AVG(performance_score) FROM performance_reviews GROUP BY review_date",
            {},
            [],
        )
        self.assertEqual(self._check(evidence, "optional-period")["status"], "unverified")
        self.assertTrue(self._ready(evidence))

    def test_or_window_union_and_multiple_fact_sql_never_pass(self):
        self._headcount_rule()
        for sql in (
            "SELECT COUNT(DISTINCT employee_id) FROM employees WHERE resigned = FALSE OR resigned IS NULL",
            "SELECT COUNT(DISTINCT employee_id) OVER () FROM employees WHERE resigned = FALSE",
            "SELECT COUNT(DISTINCT employee_id) FROM employees WHERE resigned = FALSE UNION SELECT 1",
            "SELECT COUNT(DISTINCT e.employee_id) FROM (SELECT employee_id, resigned FROM employees) e WHERE e.resigned = FALSE",
            "WITH e AS (SELECT employee_id, resigned FROM employees) SELECT COUNT(DISTINCT employee_id) FROM e WHERE resigned = FALSE",
        ):
            with self.subTest(sql=sql):
                evidence = evaluate_query(self.project, sql, {}, [])
                self.assertFalse(self._ready(evidence))
                self.assertTrue(all(check["status"] == "unverified" for check in evidence["checks"]))

        self._rule("Current performance", [
            {"id": "performance-average", "kind": "metric", "model": "performance_reviews", "column": "performance_score", "aggregation": "avg"},
        ])
        multi_fact = evaluate_query(
            self.project,
            "SELECT AVG(c.monthly_salary), AVG(p.performance_score) FROM compensation_history c JOIN performance_reviews p ON c.employee_id = p.employee_id",
            {},
            [],
        )
        self.assertFalse(self._ready(multi_fact))
        self.assertTrue(any(check["reasonCode"] == "multiple_fact_models" for check in multi_fact["checks"]))

    def test_optional_unknown_is_reported_without_becoming_a_mandatory_block(self):
        rule = self._rule("Optional headcount", [
            {"id": "headcount", "kind": "metric", "model": "employees", "column": "employee_id", "aggregation": "count", "distinct": True, "mandatory": False},
        ])
        evidence = evaluate_query(
            self.project,
            "SELECT COUNT(DISTINCT employee_id) OVER () FROM employees",
            {},
            [],
        )
        self.assertTrue(self._ready(evidence))
        self.assertTrue(evidence["checks"])
        self.assertTrue(all(check["status"] == "unverified" for check in evidence["checks"]))
        self.assertTrue(all(not check["mandatory"] for check in evidence["checks"]))

        access = AccessControlStore(Path(self.temp.name) / "state" / "optional-checks.sqlite3")
        subject = access.create_service_account("Optional check verifier")
        key = access.issue_api_key(subject.id)
        auth = access.authenticate(f"Bearer {key['apiKey']}")
        preparations = QueryPreparationStore(access, self.project)
        prepared_unknown = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Show optional headcount",
            semantic_sql="SELECT COUNT(DISTINCT employee_id) OVER () FROM employees",
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(prepared_unknown["status"], "ready")

        self.service.update_rule(rule["id"], {
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "resigned-filter", "kind": "predicate", "model": "employees", "column": "resigned", "operator": "=", "value": False, "mandatory": False},
            ]},
        })
        self.project.publish()
        optional_conflict = evaluate_query(
            self.project,
            "SELECT employee_id FROM employees WHERE resigned = TRUE",
            {},
            [],
        )
        self.assertFalse(self._ready(optional_conflict))
        self.assertEqual(self._check(optional_conflict, "resigned-filter")["status"], "conflict")
        prepared_conflict = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Show active employees",
            semantic_sql="SELECT employee_id FROM employees WHERE resigned = TRUE",
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(prepared_conflict["status"], "blocked")

        self.service.update_rule(rule["id"], {
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "missing-column", "kind": "reference", "model": "employees", "column": "private_label", "mandatory": False},
            ]},
        })
        self.project.publish()
        optional_stale = evaluate_query(
            self.project,
            "SELECT employee_id FROM employees",
            {},
            [],
        )
        self.assertEqual(self._check(optional_stale, "missing-column")["status"], "stale")
        self.assertFalse(self._ready(optional_stale))

    def test_salary_join_does_not_apply_employee_headcount_rule_and_references_query_columns(self):
        headcount = self._headcount_rule()
        salary = self._rule("Current salary", [
            {"id": "average-salary", "kind": "metric", "model": "compensation_history", "column": "monthly_salary", "aggregation": "avg"},
        ])
        evidence = evaluate_query(
            self.project,
            "SELECT AVG(c.monthly_salary) FROM compensation_history c "
            "JOIN employees e ON c.employee_id = e.employee_id",
            {},
            [],
        )
        self.assertTrue(self._ready(evidence))
        self.assertFalse(any(check["ruleId"] == headcount["id"] for check in evidence["checks"]))
        self.assertTrue(any(check["ruleId"] == salary["id"] for check in evidence["checks"]))
        self.assertNotIn(
            {"ruleId": headcount["id"], "model": "employees", "column": "employee_id"},
            evidence["references"],
        )
        self.assertIn(
            {"ruleId": RULES_VERSION, "model": "employees", "column": "employee_id"},
            evidence["references"],
        )
        self.assertIn(
            {"ruleId": RULES_VERSION, "model": "compensation_history", "column": "monthly_salary"},
            evidence["references"],
        )
        self.assertEqual(evidence["unit"], {"status": "unverified", "value": None})

    def test_declared_many_to_one_join_passes_but_reverse_fact_join_does_not(self):
        self._headcount_rule()
        declared = evaluate_query(
            self.project,
            "SELECT d.region_code, COUNT(DISTINCT e.employee_id) FROM employees e JOIN departments d ON e.department_code = d.department_code WHERE e.resigned = FALSE GROUP BY d.region_code",
            {},
            [],
        )
        self.assertTrue(self._ready(declared))
        self.assertIn(
            {"ruleId": RULES_VERSION, "model": "departments", "column": "region_code"},
            declared["references"],
        )

        reverse = evaluate_query(
            self.project,
            "SELECT COUNT(DISTINCT e.employee_id) FROM employees e JOIN performance_reviews p ON e.employee_id = p.employee_id WHERE e.resigned = FALSE",
            {},
            [],
        )
        self.assertFalse(self._ready(reverse))
        self.assertTrue(any(check["reasonCode"] == "join_not_declared_many_to_one" for check in reverse["checks"]))

    def test_rule_columns_without_a_physical_binding_are_stale(self):
        employee_metadata = (
            "name: employees\ncolumns:\n"
            "  - name: employee_id\n    type: VARCHAR\n"
            "  - name: resigned\n    type: VARCHAR\n"
            "  - name: department_code\n    type: VARCHAR\n"
            "  - name: employee_label\n    type: VARCHAR\n"
            "    expression: CONCAT(employee_id, resigned)\n"
        )
        self.project.put_file("models/employees/metadata.yml", employee_metadata)
        self.project.publish()
        self._rule("Employee labels", [
            {"id": "employee-label", "kind": "reference", "model": "employees", "column": "employee_label"},
        ])

        evidence = evaluate_query(self.project, "SELECT employee_label FROM employees", {}, [])
        self.assertEqual(self._check(evidence, "employee-label")["status"], "stale")
        self.assertFalse(self._ready(evidence))

    def test_query_preparation_returns_rechecked_template_bound_to_published_revision(self):
        self._headcount_rule()
        access = AccessControlStore(Path(self.temp.name) / "state" / "access.sqlite3")
        subject = access.create_service_account("Business verifier")
        key = access.issue_api_key(subject.id)
        auth = access.authenticate(f"Bearer {key['apiKey']}")
        preparations = QueryPreparationStore(access, self.project)
        sql = "SELECT COUNT(DISTINCT employee_id) FROM employees WHERE resigned = FALSE"
        ready = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Current headcount?",
            semantic_sql=sql,
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(ready["status"], "ready")
        template = validate_evidence(ready["businessEvidence"], executed=False)
        self.assertEqual(template["rulesVersion"], RULES_VERSION)
        prepared_template = preparations.require_ready(
            auth=auth,
            project_id="trust-test",
            semantic_sql=sql,
            preparation_id=ready["preparationId"],
        )
        self.assertEqual(prepared_template, template)
        self.assertEqual(prepared_template.published_version_id, self.project.published_version_id)

        # An unpublished edit does not change the verified revision.
        self.project.put_file("models/employees/notes.md", "Draft-only note.\n")
        self.assertIsNotNone(preparations.require_ready(
            auth=auth,
            project_id="trust-test",
            semantic_sql=sql,
            preparation_id=ready["preparationId"],
        ))

        self.project.publish()
        with self.assertRaises(PreparationError):
            preparations.require_ready(
                auth=auth,
                project_id="trust-test",
                semantic_sql=sql,
                preparation_id=ready["preparationId"],
            )

    def test_employee_detail_preparation_does_not_require_headcount_aggregation(self):
        self._headcount_rule()
        access = AccessControlStore(Path(self.temp.name) / "state" / "detail-preparation.sqlite3")
        subject = access.create_service_account("Employee detail viewer")
        key = access.issue_api_key(subject.id)
        auth = access.authenticate(f"Bearer {key['apiKey']}")
        preparations = QueryPreparationStore(access, self.project)
        sql = "SELECT employee_id, resigned FROM employees WHERE resigned = TRUE"
        ready = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Show resigned employee details",
            semantic_sql=sql,
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["businessEvidence"]["checks"], [])
        self.assertIsNotNone(preparations.require_ready(
            auth=auth,
            project_id="trust-test",
            semantic_sql=sql,
            preparation_id=ready["preparationId"],
        ))

    def test_confirmed_trend_condition_survives_preparation_and_requires_original_date_group(self):
        self._rule("Performance trend", [
            {"id": "average-performance", "kind": "metric", "model": "performance_reviews", "column": "performance_score", "aggregation": "avg"},
            {"id": "review-trend", "kind": "timeGrouping", "model": "performance_reviews", "column": "review_date", "mode": "original", "whenGrouped": True, "conditionKey": "trend"},
        ])
        access = AccessControlStore(Path(self.temp.name) / "state" / "trend-preparation.sqlite3")
        subject = access.create_service_account("Trend verifier")
        key = access.issue_api_key(subject.id)
        auth = access.authenticate(f"Bearer {key['apiKey']}")
        preparations = QueryPreparationStore(access, self.project)
        scalar_sql = "SELECT AVG(performance_score) FROM performance_reviews"

        explicit_trend = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Show the performance trend",
            semantic_sql=scalar_sql,
            conditions={"trend": True},
            confirmed_conditions=["trend"],
        )
        self.assertEqual(explicit_trend["status"], "blocked")
        self.assertEqual(explicit_trend["businessEvidence"]["conditions"], {"trend": True})
        self.assertEqual(explicit_trend["businessEvidence"]["confirmations"]["trend"], "confirmed")
        with self.assertRaises(PreparationError) as caught:
            preparations.require_ready(
                auth=auth,
                project_id="trust-test",
                semantic_sql=scalar_sql,
                preparation_id=explicit_trend["preparationId"],
            )
        self.assertEqual(self._check(caught.exception.business_evidence, "review-trend")["reasonCode"], "time_grouping_missing")

        scalar = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="What is the quarterly performance average?",
            semantic_sql=scalar_sql,
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(scalar["status"], "ready")
        self.assertEqual(self._check(scalar["businessEvidence"], "review-trend")["reasonCode"], "time_grouping_not_applicable")

    def test_query_preparation_carries_safe_evidence_when_published_check_becomes_blocked(self):
        rule = self._headcount_rule()
        access = AccessControlStore(Path(self.temp.name) / "state" / "blocked-evidence.sqlite3")
        subject = access.create_service_account("Business verifier")
        key = access.issue_api_key(subject.id)
        auth = access.authenticate(f"Bearer {key['apiKey']}")
        preparations = QueryPreparationStore(access, self.project)
        sql = "SELECT COUNT(DISTINCT employee_id) FROM employees WHERE resigned = FALSE"
        ready = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Current headcount?",
            semantic_sql=sql,
            conditions={"unrelated": "credential-like input is not evidence"},
            confirmed_conditions=[],
        )
        self.assertEqual(ready["status"], "ready")
        self.assertNotIn("unrelated", ready["businessEvidence"]["conditions"])

        # A newly published active-only rule conflicts with the unchanged SQL.
        self.service.update_rule(rule["id"], {
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "headcount", "kind": "metric", "model": "employees", "column": "employee_id", "aggregation": "count", "distinct": True},
                {"id": "active-only", "kind": "predicate", "model": "employees", "column": "resigned", "operator": "=", "value": True},
            ]},
        })
        self.project.publish()
        with self.assertRaises(PreparationError) as caught:
            preparations.require_ready(
                auth=auth,
                project_id="trust-test",
                semantic_sql=sql,
                preparation_id=ready["preparationId"],
            )
        evidence = caught.exception.business_evidence
        self.assertIsNotNone(evidence)
        safe = validate_evidence(evidence, executed=False)
        self.assertEqual(self._check(safe, "active-only")["status"], "conflict")
        self.assertFalse(any(not check["mandatory"] and check["status"] == "passed" for check in safe["checks"]))

    def test_blocked_preparation_keeps_its_safe_template_when_run_is_rejected(self):
        self._headcount_rule()
        access = AccessControlStore(Path(self.temp.name) / "state" / "blocked-preparation.sqlite3")
        subject = access.create_service_account("Blocked preparation verifier")
        key = access.issue_api_key(subject.id)
        auth = access.authenticate(f"Bearer {key['apiKey']}")
        preparations = QueryPreparationStore(access, self.project)
        sql = "SELECT COUNT(DISTINCT employee_id) FROM employees"
        blocked = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Current headcount?",
            semantic_sql=sql,
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(blocked["status"], "blocked")

        with self.assertRaises(PreparationError) as caught:
            preparations.require_ready(
                auth=auth,
                project_id="trust-test",
                semantic_sql=sql,
                preparation_id=blocked["preparationId"],
            )
        safe = validate_evidence(caught.exception.business_evidence, executed=False)
        self.assertEqual(self._check(safe, "active-only")["status"], "unverified")

    def test_preparation_is_invalidated_when_active_datasource_changes(self):
        self._headcount_rule()
        self.project.datasource_records()["source-a"] = DatasourceRecord(
            "source-a", "Warehouse A", "postgres", {"database": "hr_a"}
        )
        self.project.datasource_records()["source-b"] = DatasourceRecord(
            "source-b", "Warehouse B", "postgres", {"database": "hr_b"}
        )
        self.project.active_datasource_id = "source-a"
        self.project.save_datasources()

        access = AccessControlStore(Path(self.temp.name) / "state" / "datasource-binding.sqlite3")
        subject = access.create_service_account("Datasource verifier")
        key = access.issue_api_key(subject.id)
        auth = access.authenticate(f"Bearer {key['apiKey']}")
        preparations = QueryPreparationStore(access, self.project)
        sql = "SELECT COUNT(DISTINCT employee_id) FROM employees WHERE resigned = FALSE"
        ready = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Current headcount?",
            semantic_sql=sql,
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(ready["status"], "ready")

        self.project.active_datasource_id = "source-b"
        self.project.save_datasources()
        with self.assertRaises(PreparationError):
            preparations.require_ready(
                auth=auth,
                project_id="trust-test",
                semantic_sql=sql,
                preparation_id=ready["preparationId"],
            )

    def test_preparation_does_not_revive_after_same_revision_publish_and_rollback(self):
        rule = self._headcount_rule()
        original_version_id = self.project.published_version_id
        original_revision = self.project.published_revision
        access = AccessControlStore(Path(self.temp.name) / "state" / "version-binding.sqlite3")
        subject = access.create_service_account("Version binding verifier")
        key = access.issue_api_key(subject.id)
        auth = access.authenticate(f"Bearer {key['apiKey']}")
        preparations = QueryPreparationStore(access, self.project)
        sql = "SELECT COUNT(DISTINCT employee_id) FROM employees WHERE resigned = FALSE"
        initial = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Current headcount?",
            semantic_sql=sql,
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(initial["status"], "ready")

        repeated = self.project.publish(label="same-content republish")
        self.assertEqual(repeated["version"]["revision"], original_revision)
        self.assertNotEqual(self.project.published_version_id, original_version_id)
        with self.assertRaises(PreparationError):
            preparations.require_ready(
                auth=auth,
                project_id="trust-test",
                semantic_sql=sql,
                preparation_id=initial["preparationId"],
            )

        after_republish = preparations.prepare(
            auth=auth,
            project_id="trust-test",
            question="Current headcount?",
            semantic_sql=sql,
            conditions={},
            confirmed_conditions=[],
        )
        self.service.update_rule(rule["id"], {"content": "Updated headcount definition."})
        self.project.publish(label="changed business definition")
        self.project.rollback(str(original_version_id))
        self.assertEqual(self.project.published_revision, original_revision)
        self.assertNotEqual(self.project.published_version_id, original_version_id)
        with self.assertRaises(PreparationError):
            preparations.require_ready(
                auth=auth,
                project_id="trust-test",
                semantic_sql=sql,
                preparation_id=after_republish["preparationId"],
            )


if __name__ == "__main__":
    unittest.main()
