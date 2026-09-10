from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.access_control import AccessControlStore
from server.project import ProjectStore
from server.query_preparation import PreparationError, QueryPreparationStore
from server.service import SemanticConsoleService


class FakeValidator:
    def health(self): return {"available": True}
    def validate(self, _path): return {"valid": True, "errors": [], "warnings": []}
    def build(self, _path): return {"models": []}


class QueryPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-prepare-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        project_dir = root / "project"
        project_dir.mkdir()
        (project_dir / "wren_project.yml").write_text(
            "schema_version: 5\nname: prepare-test\ndata_source: postgres\n", encoding="utf-8"
        )
        self.project = ProjectStore(project_dir, state_dir=root / "state", validator=FakeValidator())
        self.service = SemanticConsoleService(self.project)
        self.now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
        self.access = AccessControlStore(root / "state" / "control.sqlite3", clock=lambda: self.now)
        subject = self.access.create_service_account("Preparation Agent")
        key = self.access.issue_api_key(subject.id)
        self.auth = self.access.authenticate(f"Bearer {key['apiKey']}")
        self.store = QueryPreparationStore(self.access, self.project)

    def rule(self, **overrides):
        confirmation = {
            "kind": "timeRange", "models": ["Sales"], "conditionKey": "timeRange",
            "required": True, "valueType": "dateRange", "requireConfirmation": True,
            "prompt": "Which reporting period?",
            **overrides,
        }
        self.service.create_rule({"title": "Sales period", "content": "Confirm period", "confirmationRule": confirmation})

    def test_missing_then_confirmed_condition_controls_execution(self) -> None:
        self.rule()
        pending = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue?",
            semantic_sql="SELECT revenue FROM Sales", conditions={}, confirmed_conditions=[],
        )
        self.assertEqual(pending["status"], "needs_clarification")
        self.assertEqual(pending["clarifications"][0]["conditionKey"], "timeRange")
        with self.assertRaises(PreparationError):
            self.store.require_ready(
                auth=self.auth, project_id="prepare-test", semantic_sql="SELECT revenue FROM Sales",
                preparation_id=pending["preparationId"],
            )

        ready = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue?",
            semantic_sql="SELECT revenue FROM Sales",
            conditions={"timeRange": {"start": "2026-01-01", "end": "2026-06-30"}},
            confirmed_conditions=["timeRange"],
        )
        self.assertEqual(ready["status"], "ready")
        self.store.require_ready(
            auth=self.auth, project_id="prepare-test", semantic_sql="SELECT revenue FROM Sales",
            preparation_id=ready["preparationId"],
        )
        with self.assertRaises(PreparationError):
            self.store.require_ready(
                auth=self.auth, project_id="prepare-test", semantic_sql="SELECT profit FROM Sales",
                preparation_id=ready["preparationId"],
            )

    def test_default_avoids_question_and_expired_record_is_rejected(self) -> None:
        self.rule(requireConfirmation=False, defaultValue={"start": "2026-01-01", "end": "2026-12-31"})
        ready = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue?",
            semantic_sql="SELECT revenue FROM Sales", conditions={}, confirmed_conditions=[],
        )
        self.assertEqual(ready["status"], "ready")
        self.assertIn("timeRange", ready["defaultsApplied"])
        self.now += timedelta(minutes=31)
        with self.assertRaises(PreparationError):
            self.store.require_ready(
                auth=self.auth, project_id="prepare-test", semantic_sql="SELECT revenue FROM Sales",
                preparation_id=ready["preparationId"],
            )

    def test_model_detection_uses_sql_ast_and_invalid_dates_are_blocked(self) -> None:
        self.rule()
        comment_only = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Other?",
            semantic_sql="SELECT revenue FROM Other -- Sales", conditions={}, confirmed_conditions=[],
        )
        self.assertEqual(comment_only["status"], "ready")
        cte_alias = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Other?",
            semantic_sql="WITH Sales AS (SELECT revenue FROM Other) SELECT * FROM Sales",
            conditions={}, confirmed_conditions=[],
        )
        self.assertEqual(cte_alias["status"], "ready")
        quoted = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue?",
            semantic_sql='SELECT revenue FROM "Sales"', conditions={}, confirmed_conditions=[],
        )
        self.assertEqual(quoted["status"], "needs_clarification")
        invalid_date = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue?",
            semantic_sql="SELECT revenue FROM Sales",
            conditions={"timeRange": {"start": "2026-02-30", "end": "2026-01-01"}},
            confirmed_conditions=["timeRange"],
        )
        self.assertEqual(invalid_date["status"], "blocked")
        with self.assertRaisesRegex(PreparationError, "valid semantic SQL"):
            self.store.prepare(
                auth=self.auth, project_id="prepare-test", question="Revenue?",
                semantic_sql="SELECT FROM", conditions={}, confirmed_conditions=[],
            )

    def test_metric_and_business_definition_questions_are_merged_and_answers_stay_local(self) -> None:
        metric = self.service.create_rule({
            "title": "Revenue metric",
            "content": "Confirm which revenue metric is intended.",
            "confirmationRule": {
                "kind": "metric", "models": ["Sales"], "conditionKey": "metric",
                "required": True, "allowedValues": ["gross", "net"],
                "requireConfirmation": True, "prompt": "Gross or net revenue?",
            },
        })["rule"]
        definition = self.service.create_rule({
            "title": "Customer definition",
            "content": "Confirm the active-customer definition.",
            "confirmationRule": {
                "kind": "businessDefinition", "models": ["Sales"],
                "conditionKey": "customerDefinition", "required": True,
                "allowedValues": ["ordered", "paid"], "requireConfirmation": True,
                "prompt": "Does active mean ordered or paid?",
            },
        })["rule"]
        pending = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue per active customer?",
            semantic_sql="SELECT revenue FROM Sales", conditions={}, confirmed_conditions=[],
        )
        self.assertEqual(pending["status"], "needs_clarification")
        self.assertEqual(
            {(item["kind"], item["conditionKey"]) for item in pending["clarifications"]},
            {("metric", "metric"), ("businessDefinition", "customerDefinition")},
        )

        supplied = {"metric": "net", "customerDefinition": "paid"}
        ready = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue per active customer?",
            semantic_sql="SELECT revenue FROM Sales", conditions=supplied,
            confirmed_conditions=list(supplied),
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["conditions"], supplied)
        rules = {item["id"]: item for item in self.service.list_rules()["rules"]}
        self.assertNotIn("defaultValue", rules[metric["id"]]["confirmationRule"])
        self.assertNotIn("defaultValue", rules[definition["id"]]["confirmationRule"])

    def test_preparation_is_invalidated_by_rule_revision_and_is_subject_scoped(self) -> None:
        self.rule(requireConfirmation=False, defaultValue={"start": "2026-01-01", "end": "2026-12-31"})
        ready = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue?",
            semantic_sql="SELECT revenue FROM Sales", conditions={}, confirmed_conditions=[],
        )
        other = self.access.create_service_account("Other preparation Agent")
        other_key = self.access.issue_api_key(other.id)
        other_auth = self.access.authenticate(f"Bearer {other_key['apiKey']}")
        with self.assertRaises(PreparationError):
            self.store.require_ready(
                auth=other_auth, project_id="prepare-test", semantic_sql="SELECT revenue FROM Sales",
                preparation_id=ready["preparationId"],
            )

        rule = self.service.list_rules()["rules"][0]
        self.service.update_rule(rule["id"], {"content": "Confirm the revised reporting period."})
        with self.assertRaises(PreparationError):
            self.store.require_ready(
                auth=self.auth, project_id="prepare-test", semantic_sql="SELECT revenue FROM Sales",
                preparation_id=ready["preparationId"],
            )


if __name__ == "__main__":
    unittest.main()
