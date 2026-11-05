from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import AbstractContextManager, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.access_control import AccessControlStore
from server.access_control_storage import PostgreSQLAccessControlStore
from server.models import DatasourceRecord
from server.project import ProjectStore
from server.query_preparation import PreparationError, QueryPreparationStore
from server.service import SemanticConsoleService


class FakeValidator:
    def health(self): return {"available": True}
    def validate(self, _path): return {"valid": True, "errors": [], "warnings": []}
    def build(self, _path): return {"models": []}


class FakeIndexLifecycle:
    def stage(self, _manifest, _project_dir, _revision): pass
    def activate(self, _revision): pass
    def status(self, _revision=None): return {"status": "ready"}


class _Transaction(AbstractContextManager[None]):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> None:
        self.connection.execute("BEGIN")
        return None

    def __exit__(self, exception_type, _exception, _traceback) -> bool:
        if exception_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        return False


class _SQLitePsycopgConnection:
    """SQLite test double for the PostgreSQL adapter's transaction surface."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")

    def transaction(self) -> _Transaction:
        return _Transaction(self.connection)

    def execute(self, statement: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if statement.startswith("SELECT pg_advisory_xact_lock("):
            return self.connection.execute("SELECT NULL AS pg_advisory_xact_lock")
        return self.connection.execute(statement.replace("%s", "?").replace("BYTEA", "BLOB"), params)

    def close(self) -> None:
        self.connection.close()


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
        self.project = ProjectStore(
            project_dir,
            state_dir=root / "state",
            validator=FakeValidator(),
            semantic_index_lifecycle=FakeIndexLifecycle(),
        )
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

    def test_published_execution_default_is_rederived_during_execution_recheck(self) -> None:
        model_dir = self.project.project_dir / "models" / "Sales"
        model_dir.mkdir(parents=True)
        (model_dir / "metadata.yml").write_text(
            "name: Sales\ncolumns:\n"
            "  - {name: revenue, type: DECIMAL}\n"
            "  - {name: snapshot_date, type: DATE}\n",
            encoding="utf-8",
        )
        snapshot_date = "2026-12-31"
        self.service.create_rule({
            "title": "Current revenue snapshot",
            "content": "Use the latest published revenue snapshot.",
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "average-revenue", "kind": "metric", "model": "Sales", "column": "revenue", "aggregation": "avg"},
                {"id": "snapshot-date", "kind": "timeFilter", "model": "Sales", "column": "snapshot_date",
                 "conditionKey": "snapshotDate", "valueType": "date", "confirmation": "confirmedOrDefault",
                 "defaultValue": snapshot_date},
            ]},
        })
        self.project.publish()

        semantic_sql = (
            "SELECT AVG(revenue) AS average_revenue FROM Sales "
            f"WHERE snapshot_date = DATE '{snapshot_date}'"
        )
        ready = self.store.prepare(
            auth=self.auth,
            project_id="prepare-test",
            question="What is average revenue in the current snapshot?",
            semantic_sql=semantic_sql,
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["defaultsApplied"], {"snapshotDate": snapshot_date})
        self.assertEqual(ready["conditions"], {"snapshotDate": snapshot_date})

        self.store.require_ready(
            auth=self.auth,
            project_id="prepare-test",
            semantic_sql=semantic_sql,
            preparation_id=ready["preparationId"],
        )

    def test_grouped_mandatory_period_cannot_bypass_prepare_or_execution_recheck(self) -> None:
        model_dir = self.project.project_dir / "models" / "Sales"
        model_dir.mkdir(parents=True)
        (model_dir / "metadata.yml").write_text(
            "name: Sales\ncolumns:\n"
            "  - {name: revenue, type: DECIMAL}\n"
            "  - {name: snapshot_date, type: DATE}\n",
            encoding="utf-8",
        )
        self.service.create_rule({
            "title": "Confirmed sales period",
            "content": "Confirm the sales reporting period.",
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "average-revenue", "kind": "metric", "model": "Sales", "column": "revenue", "aggregation": "avg"},
                {"id": "period", "kind": "timeFilter", "model": "Sales", "column": "snapshot_date",
                 "conditionKey": "salesPeriod", "valueType": "dateRange", "confirmation": "confirmed"},
            ]},
        })
        self.project.publish()
        semantic_sql = (
            "SELECT snapshot_date, AVG(revenue) FROM Sales "
            "WHERE snapshot_date BETWEEN DATE '2026-01-01' AND DATE '2026-06-30' GROUP BY 1"
        )

        missing = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue trend?",
            semantic_sql=semantic_sql, conditions={}, confirmed_conditions=[],
        )
        self.assertNotEqual(missing["status"], "ready")
        self.assertEqual(missing["businessEvidence"]["confirmations"]["salesPeriod"], "missing")
        self.assertEqual(missing["businessEvidence"]["time"]["source"], "sql")
        self.assertEqual(missing["businessEvidence"]["time"]["value"], {"start": "2026-01-01", "end": "2026-06-30"})
        with self.assertRaises(PreparationError):
            self.store.require_ready(
                auth=self.auth, project_id="prepare-test", semantic_sql=semantic_sql,
                preparation_id=missing["preparationId"],
            )

        confirmed = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Revenue trend?",
            semantic_sql=semantic_sql,
            conditions={"salesPeriod": {"start": "2026-01-01", "end": "2026-06-30"}},
            confirmed_conditions=["salesPeriod"],
        )
        self.assertEqual(confirmed["status"], "ready")
        self.assertEqual(confirmed["businessEvidence"]["confirmations"]["salesPeriod"], "confirmed")
        self.assertIsNotNone(self.store.require_ready(
            auth=self.auth, project_id="prepare-test", semantic_sql=semantic_sql,
            preparation_id=confirmed["preparationId"],
        ))

    def test_preparation_from_an_old_verifier_version_is_rejected(self) -> None:
        model_dir = self.project.project_dir / "models" / "Sales"
        model_dir.mkdir(parents=True)
        (model_dir / "metadata.yml").write_text(
            "name: Sales\ncolumns:\n  - {name: revenue, type: DECIMAL}\n", encoding="utf-8"
        )
        self.service.create_rule({
            "title": "Average sales revenue",
            "content": "Use the average sales revenue.",
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "average-revenue", "kind": "metric", "model": "Sales", "column": "revenue", "aggregation": "avg"},
            ]},
        })
        self.project.publish()
        sql = "SELECT AVG(revenue) FROM Sales"
        ready = self.store.prepare(
            auth=self.auth, project_id="prepare-test", question="Average revenue?",
            semantic_sql=sql, conditions={}, confirmed_conditions=[],
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["businessEvidence"]["rulesVersion"], "business-trust-v2")

        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT business_evidence_json FROM query_preparations WHERE id=?",
                (ready["preparationId"],),
            ).fetchone()
            template = json.loads(row["business_evidence_json"])
            template["rulesVersion"] = "business-trust-v1"
            connection.execute(
                "UPDATE query_preparations SET business_evidence_json=? WHERE id=?",
                (json.dumps(template, separators=(",", ":")), ready["preparationId"]),
            )

        with self.assertRaises(PreparationError):
            self.store.require_ready(
                auth=self.auth, project_id="prepare-test", semantic_sql=sql,
                preparation_id=ready["preparationId"],
            )

    def test_execution_preparation_is_invalidated_when_same_datasource_profile_changes(self) -> None:
        model_dir = self.project.project_dir / "models" / "Sales"
        model_dir.mkdir(parents=True)
        (model_dir / "metadata.yml").write_text(
            "name: Sales\ncolumns:\n  - {name: revenue, type: DECIMAL}\n",
            encoding="utf-8",
        )
        self.project.datasource_records()["source-a"] = DatasourceRecord(
            "source-a", "Warehouse", "postgres", {"database": "hr", "password": "old-secret"}
        )
        self.project.active_datasource_id = "source-a"
        self.project.save_datasources()
        self.service.create_rule({
            "title": "Revenue metric",
            "content": "Use average revenue.",
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "average-revenue", "kind": "metric", "model": "Sales", "column": "revenue", "aggregation": "avg"},
            ]},
        })
        self.project.publish()
        semantic_sql = "SELECT AVG(revenue) AS average_revenue FROM Sales"

        ready = self.store.prepare(
            auth=self.auth,
            project_id="prepare-test",
            question="What is average revenue?",
            semantic_sql=semantic_sql,
            conditions={},
            confirmed_conditions=[],
        )
        self.assertEqual(ready["status"], "ready")
        self.assertNotIn("datasource_profile_binding_id", ready)
        self.assertNotIn("old-secret", repr(ready))
        prepared_template = self.store.require_ready(
            auth=self.auth,
            project_id="prepare-test",
            semantic_sql=semantic_sql,
            preparation_id=ready["preparationId"],
        )
        self.assertIsInstance(prepared_template.datasource_profile_binding_id, str)
        self.assertEqual(prepared_template.datasource_profile_binding_id,
                         self.project.datasource_profile_binding_id("source-a"))

        # A display-name change and last-test metadata do not change the
        # connection binding; credential changes under the same datasource ID do.
        self.service.update_datasource("source-a", {"name": "Warehouse renamed"})
        self.assertIsNotNone(self.store.require_ready(
            auth=self.auth,
            project_id="prepare-test",
            semantic_sql=semantic_sql,
            preparation_id=ready["preparationId"],
        ))
        self.service.update_datasource("source-a", {"connection": {"password": "new-secret"}})
        self.assertEqual(self.project.active_datasource_identifier(), "source-a")
        with self.assertRaises(PreparationError):
            self.store.require_ready(
                auth=self.auth,
                project_id="prepare-test",
                semantic_sql=semantic_sql,
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

    def test_incremental_migrations_add_datasource_binding_on_sqlite_and_postgres(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy-preparations.sqlite3"
        legacy_access = AccessControlStore(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                "CREATE TABLE query_preparation_schema_migrations (version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE query_preparations (id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,project_id TEXT NOT NULL,subject_id TEXT NOT NULL,credential_id TEXT,semantic_sql_hash TEXT NOT NULL,rule_revision TEXT NOT NULL,status TEXT NOT NULL,conditions_json TEXT NOT NULL,defaults_json TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO query_preparation_schema_migrations(version,applied_at) VALUES(1,'2026-01-01T00:00:00Z')"
            )
            connection.commit()
        QueryPreparationStore(legacy_access, self.project)
        with closing(sqlite3.connect(legacy_path)) as connection:
            versions = [row[0] for row in connection.execute(
                "SELECT version FROM query_preparation_schema_migrations ORDER BY version"
            )]
            columns = {row[1] for row in connection.execute("PRAGMA table_info(query_preparations)")}
        self.assertEqual(versions, [1, 2, 3, 4, 5])
        self.assertTrue({"confirmed_json", "business_evidence_json", "datasource_id", "published_version_id", "datasource_profile_binding_id"} <= columns)

        postgres_path = Path(self.temp.name) / "postgres-preparations.sqlite3"
        def factory(_database_url: str) -> _SQLitePsycopgConnection:
            return _SQLitePsycopgConnection(postgres_path)

        postgres_access = PostgreSQLAccessControlStore(
            "postgresql://control.example/semarail",
            connection_factory=factory,
        )
        QueryPreparationStore(postgres_access, self.project)
        with closing(sqlite3.connect(postgres_path)) as connection:
            versions = [row[0] for row in connection.execute(
                "SELECT version FROM query_preparation_schema_migrations ORDER BY version"
            )]
            columns = {row[1] for row in connection.execute("PRAGMA table_info(query_preparations)")}
        self.assertEqual(versions, [1, 2, 3, 4, 5])
        self.assertTrue({"datasource_id", "published_version_id", "datasource_profile_binding_id"} <= columns)


if __name__ == "__main__":
    unittest.main()
