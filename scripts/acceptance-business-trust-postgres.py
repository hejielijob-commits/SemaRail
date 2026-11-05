#!/usr/bin/env python3
"""Real PostgreSQL storage/authorization acceptance in one owned temporary database.

This proves control-plane behavior, not Agent quality or analytical query results.
The administrator URL comes only from the environment and is never printed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/semantic-console"))
from server.access_control import AccessControlStore
from server.authorization import PolicyEngine
from server.business_evidence import BusinessEvidenceStore
from server.business_verification import RULES_VERSION
from server.diagnostics import DiagnosticError, DiagnosticStore
from server.feedback_resolution import FeedbackResolutionStore
from server.models import DatasourceRecord
from server.project import ProjectStore
from server.query_preparation import PreparationError, QueryPreparationStore
from server.regression import RegressionStore
from server.service import SemanticConsoleService


class Validator:
    def health(self): return {"available": True}
    def validate(self, _path): return {"valid": True, "errors": [], "warnings": []}
    def build(self, _path): return {"models": []}


class NoopIndexLifecycle:
    def stage(self, _manifest, _project_dir, _revision): pass
    def activate(self, _revision): pass
    def status(self, _revision=None): return {"status": "ready"}


def verify_period_preparations(access, auth, root):
    """Exercise the actual PostgreSQL preparation ledger; no analytical dispatch."""
    directory = root / "period-project"
    model = directory / "models/Sales"
    model.mkdir(parents=True)
    (directory / "wren_project.yml").write_text(
        "schema_version: 5\nname: business-period-pg\ndata_source: postgres\n", encoding="utf-8")
    (model / "metadata.yml").write_text(
        "name: Sales\ntable_reference: {schema: public, table: sales}\ncolumns:\n"
        " - {name: revenue, type: DECIMAL}\n - {name: snapshot_date, type: DATE}\n", encoding="utf-8")
    project = ProjectStore(directory, state_dir=root / "period-state", validator=Validator(),
        semantic_index_lifecycle=NoopIndexLifecycle())
    project.datasource_records()["warehouse"] = DatasourceRecord(
        "warehouse", "Period storage fixture", "postgres", {"database": "hr_enterprise"})
    project.active_datasource_id = "warehouse"
    project.save_datasources()
    SemanticConsoleService(project).create_rule({
        "title": "Confirmed reporting period", "content": "Confirm the reporting period.",
        "executionChecks": {"schemaVersion": 1, "checks": [
            {"id": "period", "kind": "timeFilter", "model": "Sales", "column": "snapshot_date",
             "conditionKey": "reportingPeriod", "valueType": "dateRange", "confirmation": "confirmed"},
        ]},
    })
    project.publish()
    preparations = QueryPreparationStore(access, project)
    query = (
        "SELECT snapshot_date, AVG(revenue) FROM Sales "
        "WHERE snapshot_date BETWEEN DATE '2024-07-01' AND DATE '2024-09-30' GROUP BY 1"
    )
    def prepare(confirmed):
        return preparations.prepare(auth=auth, project_id="business-period-pg", question="Revenue trend?",
            semantic_sql=query,
            conditions={"reportingPeriod": {"start": "2024-07-01", "end": "2024-09-30"}} if confirmed else {},
            confirmed_conditions=["reportingPeriod"] if confirmed else [])
    def require(preparation_id):
        return preparations.require_ready(auth=auth, project_id="business-period-pg", semantic_sql=query,
            preparation_id=preparation_id)
    missing = prepare(False)
    assert missing["status"] == "needs_clarification"
    assert missing["businessEvidence"]["confirmations"]["reportingPeriod"] == "missing"
    for preparation_id in (None, missing["preparationId"]):
        try:
            require(preparation_id)
        except PreparationError as error:
            assert error.code == "CLARIFICATION_REQUIRED"
        else:
            raise AssertionError("missing mandatory period must block execution")
    ready = prepare(True)
    assert ready["status"] == "ready"
    assert require(ready["preparationId"])["rulesVersion"] == RULES_VERSION
    with access._connect() as connection:
        row = connection.execute("SELECT business_evidence_json FROM query_preparations WHERE id=?",
            (ready["preparationId"],)).fetchone()
        previous = json.loads(row["business_evidence_json"])
        previous["rulesVersion"] = "business-trust-v1"
        connection.execute("UPDATE query_preparations SET business_evidence_json=? WHERE id=?",
            (json.dumps(previous), ready["preparationId"]))
    try:
        require(ready["preparationId"])
    except PreparationError as error:
        assert error.code == "CLARIFICATION_REQUIRED"
    else:
        raise AssertionError("old verifier preparation must require preparation again")


def rejected(callback, status):
    try:
        callback()
    except DiagnosticError as exc:
        assert exc.status == status, (exc.code, exc.status)
    else:
        raise AssertionError("expected request rejection")


def main():
    admin_url = os.environ.get("SEMARAIL_ACCEPTANCE_ADMIN_DATABASE_URL", "")
    parsed = urlsplit(admin_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise RuntimeError("SEMARAIL_ACCEPTANCE_ADMIN_DATABASE_URL is required")
    database = "semarail_business_" + uuid.uuid4().hex[:16]
    control_url = urlunsplit((parsed.scheme, parsed.netloc, "/" + database, parsed.query, ""))
    admin = psycopg.connect(admin_url, autocommit=True)
    admin.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(database)))
    try:
        now = [datetime(2026, 10, 12, tzinfo=UTC)]
        with tempfile.TemporaryDirectory(prefix="semarail-business-pg-") as temp:
            root = Path(temp)
            access = AccessControlStore.from_config(root / "unused.sqlite3", database_url=control_url, clock=lambda: now[0])
            assert access.path is None
            actor = access.create_service_account("Evidence owner")
            key = access.issue_api_key(actor.id)
            auth = access.authenticate("Bearer " + key["apiKey"])
            project_dir = root / "project"
            model_dir = project_dir / "models/Sales"
            model_dir.mkdir(parents=True)
            (project_dir / "wren_project.yml").write_text("schema_version: 5\nname: business-pg\ndata_source: postgres\n", encoding="utf-8")
            (model_dir / "metadata.yml").write_text("name: Sales\ntable_reference: {schema: public, table: sales}\ncolumns:\n - {name: revenue, type: DECIMAL}\n", encoding="utf-8")
            project = ProjectStore(project_dir, state_dir=root / "state", validator=Validator())
            project.datasource_records()["warehouse"] = DatasourceRecord("warehouse", "Acceptance warehouse", "postgres", {"database": "hr_enterprise"})
            project.active_datasource_id = "warehouse"
            project.save_datasources()
            policy = access.create_policy("Data permission", {"schemaVersion": 1, "datasourceId": "warehouse", "projects": ["business-pg"], "tools": ["query:execute", "query:plan"], "tables": {"public.sales": {"effect": "allow"}}})
            access.bind_policy(actor.id, policy["id"])
            diagnostics = DiagnosticStore(access)
            preparations = QueryPreparationStore(access, project)
            regressions = RegressionStore(diagnostics)
            resolutions = FeedbackResolutionStore(access, diagnostics, project, regressions)
            store = BusinessEvidenceStore(access, project, PolicyEngine())
            # Reopening each ledger is idempotent on the actual PostgreSQL backend.
            BusinessEvidenceStore(access, project, PolicyEngine())
            FeedbackResolutionStore(access, diagnostics, project, regressions)
            QueryPreparationStore(access, project)
            # The current-case guard uses the same scoped head query on PostgreSQL.
            case_scope = (auth.subject.organization_id, "business-pg")
            for revision in (1, 2):
                case_record = {"id": "case-head-storage", "feedbackId": "feedback-head-storage",
                    "sourceTraceId": None, "revision": revision, "status": "enabled",
                    "case": {"schemaVersion": 2}, "datasetVersion": "storage-fixture-v1",
                    "createdAt": now[0].isoformat(), "updatedAt": now[0].isoformat()}
                with access._connect() as connection:
                    connection.execute(
                        "INSERT INTO regression_case_versions(id,organization_id,project_id,revision,record_json) VALUES(?,?,?,?,?)",
                        ("case-head-storage", *case_scope, revision, json.dumps(case_record)))
                assert resolutions._case_revision(case_scope, "feedback-head-storage", "case-head-storage", revision)["revision"] == revision
            rejected(lambda: resolutions._case_revision(case_scope, "feedback-head-storage", "case-head-storage", 1), 409)
            template = {"schemaVersion": 1, "rulesVersion": RULES_VERSION, "projectRevision": project.published_revision,
                "references": [{"ruleId": "rule_storage", "model": "Sales", "column": "revenue"}], "models": ["Sales"], "columns": [{"model": "Sales", "column": "revenue"}],
                "metrics": [], "unit": {"status": "unverified", "value": None}, "grain": [], "time": {"source": "unknown", "field": None, "value": None, "groupedByOriginal": None},
                "defaultsApplied": {}, "conditions": {}, "confirmations": {}, "checks": [{"id": "storage", "ruleId": "rule_storage", "mandatory": True, "status": "passed", "reasonCode": "STORAGE_FIXTURE"}]}
            store.validate_template_permission(auth=auth, project_id="business-pg", datasource_id="warehouse", template=template)
            evidence = store.record(auth=auth, project_id="business-pg", query_id="query-storage", datasource_id="warehouse", template=template, outcome="success")
            def get(actor_auth=auth, **kwargs):
                return store.get(auth=actor_auth, project_id="business-pg", query_id="query-storage", **kwargs)
            assert get() == evidence
            # Upgrading the verifier preserves historical reads, but an old pass
            # cannot remain current under the corrected implementation.
            with access._connect() as connection:
                prior = {**evidence, "rulesVersion": "business-trust-v1"}
                connection.execute("UPDATE business_query_evidence SET record_json=? WHERE query_id=?",
                    (json.dumps(prior), "query-storage"))
            previous = get()
            assert previous["rulesVersion"] == "business-trust-v1"
            assert previous["checks"][0]["status"] == "stale"
            assert previous["checks"][0]["reasonCode"] == "RULES_VERSION_CHANGED"
            with access._connect() as connection:
                connection.execute("UPDATE business_query_evidence SET record_json=? WHERE query_id=?",
                    (json.dumps(evidence), "query-storage"))
            assert get() == evidence
            other = access.create_service_account("Project administrator")
            other_key = access.issue_api_key(other.id)
            other_auth = access.authenticate("Bearer " + other_key["apiKey"])
            rejected(lambda: get(other_auth), 404)
            admin_policy = access.create_policy("Administrative data read", {"schemaVersion": 1, "datasourceId": "warehouse", "projects": ["business-pg"], "tools": ["console:admin"], "tables": {"public.sales": {"effect": "allow"}}})
            access.bind_policy(other.id, admin_policy["id"])
            assert get(other_auth, administrator=True) == evidence
            profile = project.datasource_records()["warehouse"]
            original_connection = dict(profile.connection)
            profile.connection["database"] = "another_fixture"
            project.save_datasources()
            rejected(lambda: get(other_auth, administrator=True), 404)
            profile.connection.clear()
            profile.connection.update(original_connection)
            project.save_datasources()
            assert get(other_auth, administrator=True) == evidence
            rejected(lambda: store.get(auth=auth, project_id="another-project", query_id="query-storage"), 404)
            rejected(lambda: store.record(auth=auth, project_id="business-pg", query_id="query-storage", datasource_id="warehouse", template=template, outcome="success"), 503)
            project.put_file("models/Sales/metadata.yml", "name: unpublished_draft\n")
            assert get()["checks"][0]["status"] == "passed"
            (project_dir / "wren_project.yml").write_text("schema_version: 5\nname: business-pg\ndata_source: postgres\n# changed\n", encoding="utf-8")
            assert get()["checks"][0]["status"] == "stale"
            deny = access.create_policy("Revoke column", {"schemaVersion": 1, "datasourceId": "warehouse", "projects": ["business-pg"], "tools": ["query:execute"], "tables": {"public.sales": {"effect": "allow", "columns": {"deny": ["revenue"]}}}})
            access.bind_policy(actor.id, deny["id"])
            rejected(get, 403)
            with access._connect() as connection:
                raw = connection.execute("SELECT record_json,bindings_json FROM business_query_evidence").fetchone()
                assert all(key not in raw["record_json"] for key in ("semanticSql", "nativeSql", "previewRows", "authorizationPolicy", "datasource_profile_binding_id"))
                assert key["apiKey"] not in raw["record_json"] + raw["bindings_json"]
                future = json.loads(raw["record_json"])
                future["schemaVersion"] = 2
                connection.execute("UPDATE business_query_evidence SET record_json=?", (json.dumps(future),))
            rejected(lambda: get(other_auth, administrator=True), 400)
            now[0] += timedelta(days=31)
            store.cleanup_expired()
            rejected(lambda: get(other_auth, administrator=True), 404)
            with access._connect() as connection:
                assert connection.execute("SELECT COUNT(*) AS n FROM business_query_evidence").fetchone()["n"] == 0
                ledgers = {table: connection.execute(f"SELECT MAX(version) AS v FROM {table}").fetchone()["v"] for table in ("business_evidence_schema_migrations", "feedback_resolution_schema_migrations", "query_preparation_schema_migrations")}
                assert ledgers["business_evidence_schema_migrations"] == 3
                assert ledgers["feedback_resolution_schema_migrations"] == 1
                assert ledgers["query_preparation_schema_migrations"] == 5
            verify_period_preparations(access, auth, root)
            print("BUSINESS_TRUST_POSTGRES_STORAGE_PASS")
            print(json.dumps({"schemaVersion": 1, "checks": ["independent_incremental_ledgers", "idempotent_reopen", "current_case_revision_guard", "owner_project_scope", "admin_current_data_permission", "column_revocation", "draft_isolation", "published_staleness", "verifier_version_staleness", "mandatory_grouped_period_confirmation", "old_verifier_preparation", "unknown_record_version", "immutable_query_identity", "thirty_day_cleanup", "no_query_rows_or_credentials"]}))
        return 0
    finally:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (database,))
        admin.execute(psycopg.sql.SQL("DROP DATABASE {}").format(psycopg.sql.Identifier(database)))
        admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
