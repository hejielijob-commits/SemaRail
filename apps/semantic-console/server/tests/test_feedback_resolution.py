from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.access_control import AccessControlStore
from server.diagnostics import DiagnosticError, DiagnosticStore, _timestamp
from server.feedback_resolution import FeedbackResolutionStore
from server.project import ProjectStore
from server.regression import RegressionStore


class FeedbackResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-feedback-resolution-")
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)
        self.bootstrap = "bootstrap-token-that-is-at-least-thirty-two-characters"
        self.access = AccessControlStore(
            Path(self.temp.name) / "control.sqlite3",
            bootstrap_token=self.bootstrap,
            clock=lambda: self.now,
        )
        self.auth = self.access.authenticate(f"Bearer {self.bootstrap}")
        self.project_id = "resolution-project"
        self.diagnostics = DiagnosticStore(self.access, clock=lambda: self.now)
        self.regression = RegressionStore(self.diagnostics)
        self.project = ProjectStore(
            Path(self.temp.name) / "mdl",
            state_dir=Path(self.temp.name) / "project-state",
        )
        asset = self.project.project_dir / "knowledge" / "rules" / "trusted-revenue.md"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text("original rule\n", encoding="utf-8")
        self.base_revision = self.project.published_revision
        self._write_version("published-base", self.base_revision, "original rule\n", self.now - timedelta(days=1))
        asset.write_text("corrected rule\n", encoding="utf-8")
        self.published_revision = self.project.published_revision
        self._write_version("published-fixed", self.published_revision, "corrected rule\n", self.now)
        self.store = FeedbackResolutionStore(
            self.access,
            self.diagnostics,
            self.project,
            self.regression,
            clock=lambda: self.now,
        )
        self.diagnostics.record_execution(
            auth=self.auth,
            project_id=self.project_id,
            trace_id="trace-feedback-1",
            query_id="query-feedback-1",
            datasource_id="warehouse-1",
            semantic_version=self.base_revision,
            transport="core-http",
            method="query.run",
            status="success",
            stage="complete",
        )
        submitted = self.diagnostics.submit_feedback(
            auth=self.auth,
            project_id=self.project_id,
            reference="query-feedback-1",
            idempotency_key="resolution-feedback-1",
            category="sql_generation",
            description="The business metric should exclude voided orders.",
        )
        self.feedback_id = submitted["feedbackId"]
        self.case_id = "case-resolution-1"
        self.case_revision = 3
        self.dataset_version = "dataset-2026-10"
        self._write_case()
        self.run_id = "rr-resolution-1"
        self._write_run()

    def _write_version(self, version_id: str, revision: str, content: str, created_at: datetime) -> None:
        version_dir = self.project.versions_dir / version_id
        snapshot = version_dir / "project" / "knowledge" / "rules"
        snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / "trusted-revenue.md").write_text(content, encoding="utf-8")
        (version_dir / "version.json").write_text(
            json.dumps(
                {
                    "id": version_id,
                    "revision": revision,
                    "createdAt": _timestamp(created_at),
                    "fileCount": 1,
                    "label": None,
                }
            ),
            encoding="utf-8",
        )

    def _write_case(self) -> None:
        record = {
            "id": self.case_id,
            "feedbackId": self.feedback_id,
            "sourceTraceId": None,
            "revision": self.case_revision,
            "status": "enabled",
            "case": {
                "schemaVersion": 2,
                "kind": "deterministic_sql",
                "testDatasetId": "fixture-dataset",
            },
            "datasetVersion": self.dataset_version,
            "createdAt": _timestamp(self.now),
            "updatedAt": _timestamp(self.now),
        }
        with self.access._connect() as connection:
            connection.execute(
                "INSERT INTO regression_case_versions(id,organization_id,project_id,revision,record_json) VALUES(?,?,?,?,?)",
                (
                    self.case_id,
                    self.auth.subject.organization_id,
                    self.project_id,
                    self.case_revision,
                    json.dumps(record),
                ),
            )

    def _write_case_head(self, revision: int, *, status: str = "enabled") -> None:
        record = {
            "id": self.case_id,
            "feedbackId": self.feedback_id,
            "sourceTraceId": None,
            "revision": revision,
            "status": status,
            "case": {
                "schemaVersion": 2,
                "kind": "deterministic_sql",
                "testDatasetId": "fixture-dataset",
            },
            "datasetVersion": self.dataset_version,
            "createdAt": _timestamp(self.now),
            "updatedAt": _timestamp(self.now),
        }
        with self.access._connect() as connection:
            connection.execute(
                "INSERT INTO regression_case_versions(id,organization_id,project_id,revision,record_json) VALUES(?,?,?,?,?)",
                (
                    self.case_id,
                    self.auth.subject.organization_id,
                    self.project_id,
                    revision,
                    json.dumps(record),
                ),
            )

    def _run_record(self, expires_at: str | None = None) -> dict:
        created_at = _timestamp(self.now)
        expiry = expires_at or _timestamp(self.now + timedelta(days=30))
        return {
            "schemaVersion": 1,
            "id": self.run_id,
            "status": "completed",
            "subjectId": self.auth.subject.id,
            "caseIds": [self.case_id],
            "results": [
                {
                    "caseId": self.case_id,
                    "caseRevision": self.case_revision,
                    "status": "passed",
                    "reasonCode": "ASSERTIONS_PASSED",
                    "durationMs": 12,
                    "coreTraceIds": ["core-trace-actual-1"],
                    "queryId": "regression-query-1",
                    "feedbackId": self.feedback_id,
                    "sourceTraceId": None,
                    "assertions": [{"kind": "rowCount", "status": "passed", "reasonCode": "MATCH"}],
                    "environment": {
                        "datasourceId": "warehouse-1",
                        "semanticVersion": self.published_revision,
                        "datasetVersion": self.dataset_version,
                        "policyVersions": [],
                    },
                }
            ],
            "createdAt": created_at,
            "updatedAt": created_at,
            "expiresAt": expiry,
        }

    def _write_run(self, expires_at: str | None = None) -> None:
        record = self._run_record(expires_at)
        with self.access._connect() as connection:
            connection.execute(
                "INSERT INTO regression_query_runs(id,organization_id,project_id,subject_id,status,record_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    self.auth.subject.organization_id,
                    self.project_id,
                    self.auth.subject.id,
                    record["status"],
                    json.dumps(record),
                    record["createdAt"],
                    record["expiresAt"],
                ),
            )

    def _save_full_resolution(self) -> dict:
        return self.store.save(
            self.auth,
            self.project_id,
            self.feedback_id,
            {
                "expectedRevision": 0,
                "assetPath": "knowledge/rules/trusted-revenue.md",
                "baseVersionId": "published-base",
                "publishedVersionId": "published-fixed",
                "caseId": self.case_id,
                "caseRevision": self.case_revision,
                "runId": self.run_id,
            },
        )

    def _import_evaluation(self, *, semantic_version: str | None = None) -> dict:
        return self.store.import_evaluation(
            self.auth,
            self.project_id,
            self.feedback_id,
            {
                "schemaVersion": 1,
                "campaignId": "campaign-business-trust-7",
                "reportSha256": "a" * 64,
                "source": "external-agent-evaluator",
                "model": "agent-review-v2",
                "reasoning": "high",
                "datasetVersion": self.dataset_version,
                "semanticVersion": semantic_version or self.published_revision,
                "verdictAggregates": {
                    "passed": 14,
                    "failed": 0,
                    "inconclusive": 0,
                    "total": 14,
                },
            },
        )

    def test_full_chain_requires_reviewed_same_version_external_evaluation(self) -> None:
        saved = self._save_full_resolution()
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["verification"]["status"], "incomplete")
        evaluation = self._import_evaluation()
        self.assertIsNone(evaluation["review"])

        pending = self.store.verify(self.auth, self.project_id, self.feedback_id)
        self.assertEqual(pending["verification"]["status"], "incomplete")
        approved = self.store.review_evaluation(
            self.auth,
            self.project_id,
            self.feedback_id,
            evaluation["id"],
            {"expectedRevision": 1, "decision": "approved"},
        )
        self.assertEqual(approved["review"]["reviewerSubjectId"], self.auth.subject.id)
        self.assertEqual(approved["review"]["reviewedAt"], _timestamp(self.now))

        verified = self.store.verify(self.auth, self.project_id, self.feedback_id)
        self.assertEqual(verified["verification"]["status"], "verified")
        self.assertTrue(all(check["status"] == "passed" for check in verified["verification"]["checks"]))
        payload = json.dumps(verified, ensure_ascii=False)
        for forbidden in ("semanticSql", "nativeSql", "expectedAnswer", "rows", "password", "apiKey"):
            self.assertNotIn(forbidden, payload)

    def test_edited_case_head_stales_existing_sql_and_approved_agent_evidence(self) -> None:
        self._save_full_resolution()
        evaluation = self._import_evaluation()
        self.store.review_evaluation(
            self.auth,
            self.project_id,
            self.feedback_id,
            evaluation["id"],
            {"expectedRevision": 1, "decision": "approved"},
        )
        self.assertEqual(self.store.verify(self.auth, self.project_id, self.feedback_id)["verification"]["status"], "verified")

        self._write_case_head(self.case_revision + 1)
        stale = self.store.verify(self.auth, self.project_id, self.feedback_id)
        self.assertEqual(stale["verification"]["status"], "stale")
        checks = {item["key"]: item for item in stale["verification"]["checks"]}
        self.assertEqual((checks["regressionCase"]["status"], checks["regressionCase"]["reasonCode"]), ("stale", "CASE_REVISION_STALE"))
        self.assertEqual(checks["sqlRun"]["status"], "stale")
        self.assertEqual(checks["agentEvaluation"]["status"], "unverified")

        with self.assertRaises(DiagnosticError) as outdated_link:
            self.store.save(
                self.auth,
                self.project_id,
                self.feedback_id,
                {
                    "expectedRevision": 1,
                    "assetPath": "knowledge/rules/trusted-revenue.md",
                    "baseVersionId": "published-base",
                    "publishedVersionId": "published-fixed",
                    "caseId": self.case_id,
                    "caseRevision": self.case_revision,
                    "runId": self.run_id,
                },
            )
        self.assertEqual((outdated_link.exception.code, outdated_link.exception.status), ("CASE_REVISION_STALE", 409))

    def test_deleted_case_head_stales_existing_resolution(self) -> None:
        self._save_full_resolution()
        self.regression.delete_case(
            (self.auth.subject.organization_id, self.project_id),
            self.case_id,
        )

        stale = self.store.verify(self.auth, self.project_id, self.feedback_id)
        self.assertEqual(stale["verification"]["status"], "stale")
        case_check = next(check for check in stale["verification"]["checks"] if check["key"] == "regressionCase")
        self.assertEqual((case_check["status"], case_check["reasonCode"]), ("stale", "CASE_REVISION_NOT_FOUND"))

        with self.assertRaises(DiagnosticError) as missing_link:
            self.store.save(
                self.auth,
                self.project_id,
                self.feedback_id,
                {
                    "expectedRevision": 1,
                    "assetPath": "knowledge/rules/trusted-revenue.md",
                    "baseVersionId": "published-base",
                    "publishedVersionId": "published-fixed",
                    "caseId": self.case_id,
                    "caseRevision": self.case_revision,
                    "runId": self.run_id,
                },
            )
        self.assertEqual((missing_link.exception.code, missing_link.exception.status), ("CASE_REVISION_NOT_FOUND", 404))

    def test_resolution_and_evaluation_writes_use_optimistic_revisions(self) -> None:
        self._save_full_resolution()
        with self.assertRaises(DiagnosticError) as stale_resolution:
            self.store.save(
                self.auth,
                self.project_id,
                self.feedback_id,
                {
                    "expectedRevision": 0,
                    "assetPath": "knowledge/rules/trusted-revenue.md",
                    "baseVersionId": "published-base",
                    "publishedVersionId": "published-fixed",
                    "caseId": self.case_id,
                    "caseRevision": self.case_revision,
                    "runId": self.run_id,
                },
            )
        self.assertEqual((stale_resolution.exception.code, stale_resolution.exception.status), ("FEEDBACK_RESOLUTION_REVISION_CONFLICT", 409))

        evaluation = self._import_evaluation()
        self.store.review_evaluation(
            self.auth,
            self.project_id,
            self.feedback_id,
            evaluation["id"],
            {"expectedRevision": 1, "decision": "approved"},
        )
        with self.assertRaises(DiagnosticError) as stale_review:
            self.store.review_evaluation(
                self.auth,
                self.project_id,
                self.feedback_id,
                evaluation["id"],
                {"expectedRevision": 1, "decision": "rejected"},
            )
        self.assertEqual((stale_review.exception.code, stale_review.exception.status), ("AGENT_EVALUATION_REVISION_CONFLICT", 409))

    def test_run_expiry_and_feedback_retention_never_verify(self) -> None:
        self._save_full_resolution()
        evaluation = self._import_evaluation()
        self.store.review_evaluation(
            self.auth,
            self.project_id,
            self.feedback_id,
            evaluation["id"],
            {"expectedRevision": 1, "decision": "approved"},
        )

        expired = _timestamp(self.now - timedelta(seconds=1))
        with self.access._connect() as connection:
            connection.execute(
                "UPDATE regression_query_runs SET expires_at=? WHERE id=?",
                (expired, self.run_id),
            )
        stale_run = self.store.verify(self.auth, self.project_id, self.feedback_id)
        self.assertNotEqual(stale_run["verification"]["status"], "verified")
        self.assertEqual(
            next(check for check in stale_run["verification"]["checks"] if check["key"] == "sqlRun")["status"],
            "stale",
        )

        with self.access._connect() as connection:
            connection.execute(
                "UPDATE regression_query_runs SET expires_at=? WHERE id=?",
                (_timestamp(self.now + timedelta(days=10)), self.run_id),
            )
            connection.execute(
                "UPDATE query_diagnostics SET expires_at=? WHERE trace_id=?",
                (expired, "trace-feedback-1"),
            )
        stale_feedback = self.store.verify(self.auth, self.project_id, self.feedback_id)
        self.assertNotEqual(stale_feedback["verification"]["status"], "verified")
        self.assertEqual(
            next(check for check in stale_feedback["verification"]["checks"] if check["key"] == "feedbackEvidence")["status"],
            "stale",
        )

    def test_asset_and_external_evaluation_versions_must_match_actual_run(self) -> None:
        self._save_full_resolution()
        evaluation = self._import_evaluation(semantic_version="sha256:another-publish")
        self.store.review_evaluation(
            self.auth,
            self.project_id,
            self.feedback_id,
            evaluation["id"],
            {"expectedRevision": 1, "decision": "approved"},
        )
        result = self.store.verify(self.auth, self.project_id, self.feedback_id)
        self.assertEqual(result["verification"]["status"], "conflict")
        self.assertEqual(
            next(check for check in result["verification"]["checks"] if check["key"] == "agentEvaluation")["reasonCode"],
            "AGENT_EVALUATION_VERSION_MISMATCH",
        )

    def test_source_feedback_must_match_base_revision_and_fix_must_still_be_current(self) -> None:
        self._save_full_resolution()
        with self.access._connect() as connection:
            connection.execute(
                "UPDATE query_diagnostics SET semantic_version=? WHERE trace_id=?",
                (self.published_revision, "trace-feedback-1"),
            )
        mismatched_source = self.store.verify(self.auth, self.project_id, self.feedback_id)
        feedback_check = next(check for check in mismatched_source["verification"]["checks"] if check["key"] == "feedbackEvidence")
        self.assertEqual((feedback_check["status"], feedback_check["reasonCode"]), ("conflict", "FEEDBACK_SEMANTIC_VERSION_MISMATCH"))

        with self.access._connect() as connection:
            connection.execute(
                "UPDATE query_diagnostics SET semantic_version=? WHERE trace_id=?",
                (self.base_revision, "trace-feedback-1"),
            )
        asset = self.project.project_dir / "knowledge" / "rules" / "trusted-revenue.md"
        asset.write_text("a later published change\n", encoding="utf-8")
        stale_publish = self.store.verify(self.auth, self.project_id, self.feedback_id)
        semantic_check = next(check for check in stale_publish["verification"]["checks"] if check["key"] == "semanticAsset")
        self.assertEqual((semantic_check["status"], semantic_check["reasonCode"]), ("stale", "PUBLISHED_VERSION_STALE"))

    def test_unmapped_historical_feedback_semantic_version_remains_unknown(self) -> None:
        self._save_full_resolution()
        with self.access._connect() as connection:
            connection.execute(
                "UPDATE query_diagnostics SET semantic_version=? WHERE trace_id=?",
                ("sha256:unmapped-draft", "trace-feedback-1"),
            )

        result = self.store.verify(self.auth, self.project_id, self.feedback_id)
        feedback_check = next(check for check in result["verification"]["checks"] if check["key"] == "feedbackEvidence")
        self.assertEqual((feedback_check["status"], feedback_check["reasonCode"]), ("unverified", "FEEDBACK_SEMANTIC_VERSION_UNKNOWN"))

    def test_base_version_must_predate_fix_version(self) -> None:
        with self.assertRaises(DiagnosticError) as reversed_versions:
            self.store.save(
                self.auth,
                self.project_id,
                self.feedback_id,
                {
                    "expectedRevision": 0,
                    "assetPath": "knowledge/rules/trusted-revenue.md",
                    "baseVersionId": "published-fixed",
                    "publishedVersionId": "published-base",
                    "caseId": None,
                    "caseRevision": None,
                    "runId": None,
                },
            )
        self.assertEqual(reversed_versions.exception.code, "INVALID_RESOLUTION")

    def test_unknown_resolution_and_evaluation_storage_versions_fail_closed(self) -> None:
        self._save_full_resolution()
        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM feedback_resolutions WHERE feedback_id=?",
                (self.feedback_id,),
            ).fetchone()
            record = json.loads(row["record_json"])
            record["schemaVersion"] = 99
            connection.execute(
                "UPDATE feedback_resolutions SET record_json=? WHERE feedback_id=?",
                (json.dumps(record), self.feedback_id),
            )
        with self.assertRaises(DiagnosticError) as unsupported_resolution:
            self.store.verify(self.auth, self.project_id, self.feedback_id)
        self.assertEqual((unsupported_resolution.exception.code, unsupported_resolution.exception.status), ("FEEDBACK_RESOLUTION_STORE_UNAVAILABLE", 503))

        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM feedback_resolutions WHERE feedback_id=?",
                (self.feedback_id,),
            ).fetchone()
            record = json.loads(row["record_json"])
            record["schemaVersion"] = 1
            connection.execute(
                "UPDATE feedback_resolutions SET record_json=? WHERE feedback_id=?",
                (json.dumps(record), self.feedback_id),
            )
        evaluation = self._import_evaluation()
        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM feedback_agent_evaluations WHERE id=?",
                (evaluation["id"],),
            ).fetchone()
            record = json.loads(row["record_json"])
            record["schemaVersion"] = 99
            connection.execute(
                "UPDATE feedback_agent_evaluations SET record_json=? WHERE id=?",
                (json.dumps(record), evaluation["id"]),
            )
        with self.assertRaises(DiagnosticError) as unsupported_evaluation:
            self.store.verify(self.auth, self.project_id, self.feedback_id)
        self.assertEqual((unsupported_evaluation.exception.code, unsupported_evaluation.exception.status), ("FEEDBACK_RESOLUTION_STORE_UNAVAILABLE", 503))

    def test_unknown_case_and_run_storage_versions_never_verify(self) -> None:
        self._save_full_resolution()
        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM regression_case_versions WHERE id=? AND revision=?",
                (self.case_id, self.case_revision),
            ).fetchone()
            record = json.loads(row["record_json"])
            record["case"]["schemaVersion"] = 99
            connection.execute(
                "UPDATE regression_case_versions SET record_json=? WHERE id=? AND revision=?",
                (json.dumps(record), self.case_id, self.case_revision),
            )
        unknown_case = self.store.verify(self.auth, self.project_id, self.feedback_id)
        case_check = next(check for check in unknown_case["verification"]["checks"] if check["key"] == "regressionCase")
        self.assertEqual((case_check["status"], case_check["reasonCode"]), ("conflict", "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE"))

        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM regression_query_runs WHERE id=?",
                (self.run_id,),
            ).fetchone()
            record = json.loads(row["record_json"])
            record["schemaVersion"] = 99
            connection.execute(
                "UPDATE regression_query_runs SET record_json=? WHERE id=?",
                (json.dumps(record), self.run_id),
            )
        unknown_run = self.store.verify(self.auth, self.project_id, self.feedback_id)
        run_check = next(check for check in unknown_run["verification"]["checks"] if check["key"] == "sqlRun")
        self.assertEqual((run_check["status"], run_check["reasonCode"]), ("conflict", "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE"))

    def test_manual_verified_workflow_label_does_not_complete_evidence_chain(self) -> None:
        with self.access._connect() as connection:
            connection.execute("UPDATE query_feedback SET status='verified' WHERE id=?", (self.feedback_id,))
        result = self._save_full_resolution()
        with self.access._connect() as connection:
            status = connection.execute("SELECT status FROM query_feedback WHERE id=?", (self.feedback_id,)).fetchone()["status"]
        self.assertEqual(status, "verified")
        self.assertEqual(result["verification"]["status"], "incomplete")

    def test_evaluation_requires_positive_total_and_server_owned_review_metadata(self) -> None:
        payload = {
            "schemaVersion": 1,
            "campaignId": "campaign-empty-summary",
            "reportSha256": "d" * 64,
            "source": "external-agent-evaluator",
            "model": "agent-review-v2",
            "reasoning": "high",
            "datasetVersion": self.dataset_version,
            "semanticVersion": self.published_revision,
            "verdictAggregates": {"passed": 0, "failed": 0, "inconclusive": 0, "total": 0},
        }
        with self.assertRaises(DiagnosticError) as empty_total:
            self.store.import_evaluation(self.auth, self.project_id, self.feedback_id, payload)
        self.assertEqual(empty_total.exception.code, "INVALID_AGENT_EVALUATION")

        evaluation = self._import_evaluation()
        with self.assertRaises(DiagnosticError) as client_review_metadata:
            self.store.review_evaluation(
                self.auth,
                self.project_id,
                self.feedback_id,
                evaluation["id"],
                {"expectedRevision": 1, "decision": "approved", "reviewerSubjectId": "forged", "reviewedAt": _timestamp(self.now)},
            )
        self.assertEqual(client_review_metadata.exception.code, "INVALID_AGENT_EVALUATION")

    def test_admin_and_project_scope_are_required(self) -> None:
        other = self.access.create_service_account("Unprivileged reviewer")
        issued = self.access.issue_api_key(other.id)
        unprivileged = self.access.authenticate(f"Bearer {issued['apiKey']}")
        with self.assertRaises(Exception) as denied:
            self.store.get(unprivileged, self.project_id, self.feedback_id)
        self.assertEqual(getattr(denied.exception, "status", None), 403)
        with self.assertRaises(DiagnosticError) as out_of_scope:
            self.store.get(self.auth, "another-project", self.feedback_id)
        self.assertEqual(out_of_scope.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
