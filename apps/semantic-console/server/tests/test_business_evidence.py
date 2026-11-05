from __future__ import annotations

import json
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

from server.business_evidence import BusinessEvidenceStore, validate_evidence
from server.business_verification import RULES_VERSION
from server.diagnostics import DiagnosticError
from server.models import DatasourceRecord
from server.service import SemanticConsoleService
from server.tests import test_runtime_rpc as fixtures
from server.tests.test_business_verification import FakeIndexLifecycle


class BusinessEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        # These ledger/authorization tests do not exercise semantic retrieval.
        with patch("server.project._default_semantic_index_lifecycle", return_value=FakeIndexLifecycle()):
            fixtures.RuntimeRpcTests.setUp(self)
        directory = self.project.project_dir / "models" / "orders"
        directory.mkdir(parents=True)
        (directory / "metadata.yml").write_text("name: orders\ntable_reference: {schema: public, table: orders}\nprimary_key: id\ncolumns:\n  - {name: id, type: BIGINT}\n  - {name: amount, type: DECIMAL}\n", encoding="utf-8")
        self.auth = self.gateway.access_control.authenticate(self.authorization)
        self.initial_time = self.gateway.access_control.clock()
        self.store = self.gateway.business_evidence
        self.assertIsInstance(self.store, BusinessEvidenceStore)
        self.template = {
            "schemaVersion": 1, "rulesVersion": RULES_VERSION, "projectRevision": self.project.published_revision,
            "references": [{"ruleId": "r1", "model": "orders", "column": "amount"}],
            "models": ["orders"], "columns": ["orders.amount"], "metrics": ["averageAmount"],
            "unit": {"status": "unverified"}, "grain": ["orders.id"],
            "time": {"source": "unknown", "groupedByOriginal": None},
            "defaultsApplied": {}, "conditions": {}, "confirmations": {},
            "checks": [{"id": "average", "ruleId": "r1", "mandatory": True, "status": "passed", "reasonCode": "MATCHED"}],
        }

    def record(self, query_id="q-evidence"):
        return self.store.record(auth=self.auth, project_id="runtime-test", query_id=query_id, datasource_id=self.datasource_id, template=self.template, outcome="success")

    def get(self, auth=None, **kwargs):
        return self.store.get(auth=auth or self.auth, project_id="runtime-test", query_id="q-evidence", **kwargs)

    def test_owner_scope_current_column_revocation_and_no_sensitive_bodies(self):
        recorded = self.record()
        self.assertEqual(self.get(), recorded)
        with self.gateway.access_control._connect() as connection:
            row = connection.execute("SELECT record_json,bindings_json FROM business_query_evidence").fetchone()
        self.assertNotIn("semanticSql", row["record_json"])
        self.assertNotIn("previewRows", row["record_json"])
        self.assertNotIn(self.authorization, row["record_json"] + row["bindings_json"])
        other = self.gateway.access_control.create_service_account("Other")
        key = self.gateway.access_control.issue_api_key(other.id)
        other_auth = self.gateway.access_control.authenticate("Bearer " + key["apiKey"])
        with self.assertRaises(DiagnosticError) as rejected:
            self.get(other_auth)
        self.assertEqual(rejected.exception.status, 404)
        deny = self.gateway.access_control.create_policy("Deny amount", {"schemaVersion": 1, "datasourceId": self.datasource_id, "projects": ["runtime-test"], "tools": ["query:execute"], "tables": {"public.orders": {"effect": "allow", "columns": {"deny": ["amount"]}}}})
        self.gateway.access_control.bind_policy(self.auth.subject.id, deny["id"])
        with self.assertRaises(DiagnosticError) as revoked:
            self.get()
        self.assertEqual(revoked.exception.status, 403)

    def test_generated_opaque_datasource_ids_survive_record_and_current_permission_read(self):
        service = SemanticConsoleService(self.project)
        for index, identifier in enumerate(("-opaque-source", "9opaque-source", "_opaque-source")):
            with self.subTest(identifier=identifier):
                with patch("server.service.token_urlsafe", return_value=identifier):
                    created = service.create_datasource({"name": "Opaque ID warehouse", "type": "postgres", "connection": {"database": "runtime"}})
                self.assertEqual(created["id"], identifier)
                service.activate_datasource(identifier)
                policy = self.gateway.access_control.create_policy("Opaque ID access", {
                    "schemaVersion": 1, "datasourceId": identifier, "projects": ["runtime-test"],
                    "tools": ["query:execute"], "tables": {"public.orders": {"effect": "allow"}},
                })
                self.gateway.access_control.bind_policy(self.auth.subject.id, policy["id"])
                query_id = f"q-opaque-{index}"
                recorded = self.store.record(auth=self.auth, project_id="runtime-test", query_id=query_id,
                    datasource_id=identifier, template=self.template, outcome="success")
                self.assertEqual(recorded["datasourceId"], identifier)
                self.assertEqual(self.store.get(auth=self.auth, project_id="runtime-test", query_id=query_id), recorded)

    def test_opaque_object_ids_do_not_relax_reference_or_sensitive_text_checks(self):
        now = self.initial_time.isoformat()
        evidence = {**self.template, "queryId": "q-opaque", "datasourceId": "opaque-source",
                    "outcome": "success", "createdAt": now, "expiresAt": now}
        for identifier in ("C:/private/path", "/private/path", "Bearer sensitive", "https://private", "id with spaces"):
            with self.subTest(identifier=identifier), self.assertRaises(DiagnosticError):
                validate_evidence({**evidence, "datasourceId": identifier})
        with self.assertRaises(DiagnosticError):
            validate_evidence({**evidence, "references": [{"ruleId": "r1", "model": "-orders", "column": "amount"}]})

    def test_corrupt_nested_unit_and_time_types_fail_closed(self):
        for field, invalid in (("unit", {"status": "verified", "value": 12}), ("time", {"source": "sql", "field": 12}), ("time", {"source": "sql", "groupedByOriginal": "true"})):
            with self.subTest(field=field, invalid=invalid), self.assertRaises(DiagnosticError):
                validate_evidence({**self.template, field: invalid}, executed=False)

    def test_same_id_connection_change_and_legacy_binding_hide_old_evidence(self):
        recorded = self.store.record(auth=self.auth, project_id="runtime-test", query_id="q-evidence", datasource_id=self.datasource_id, template=self.template, outcome="success")
        datasource = self.project.datasource_records()[self.datasource_id]
        datasource.name = "Renamed display label"
        self.project.save_datasources()
        self.assertEqual(self.get(), recorded)
        datasource.connection["database"] = "another_dataset"
        self.project.save_datasources()
        with self.assertRaises(DiagnosticError) as changed:
            self.get()
        self.assertEqual(changed.exception.status, 404)
        datasource.connection["database"] = "runtime"
        self.project.save_datasources()
        self.assertEqual(self.get(), recorded)
        with self.gateway.access_control._connect() as connection:
            connection.execute("UPDATE business_query_evidence SET datasource_profile_binding_id=NULL")
        with self.assertRaises(DiagnosticError) as legacy:
            self.get()
        self.assertEqual(legacy.exception.status, 404)

    def test_published_change_stales_checks_and_draft_does_not(self):
        self.record()
        self.project.put_file("models/orders/metadata.yml", "name: draft_only\n")
        self.assertEqual(self.get()["checks"][0]["status"], "passed")
        (self.project.project_dir / "wren_project.yml").write_text("schema_version: 5\nname: runtime-test\ndata_source: postgres\n# published change\n", encoding="utf-8")
        self.assertEqual(self.get()["checks"][0]["status"], "stale")

    def test_evidence_from_an_old_verifier_version_is_stale(self):
        self.record()
        with self.gateway.access_control._connect() as connection:
            row = connection.execute("SELECT record_json FROM business_query_evidence").fetchone()
            record = json.loads(row["record_json"])
            record["rulesVersion"] = "business-trust-v1"
            connection.execute(
                "UPDATE business_query_evidence SET record_json=?",
                (json.dumps(record, ensure_ascii=False),),
            )

        evidence = self.get()
        self.assertEqual(evidence["rulesVersion"], "business-trust-v1")
        self.assertEqual(evidence["checks"][0]["status"], "stale")
        self.assertEqual(evidence["checks"][0]["reasonCode"], "RULES_VERSION_CHANGED")

    def test_expiry_cleanup_unknown_version_and_immutable_query_identity(self):
        self.record()
        with self.assertRaises(DiagnosticError):
            self.record()
        with self.gateway.access_control._connect() as connection:
            row = connection.execute("SELECT record_json FROM business_query_evidence").fetchone()
            record = json.loads(row["record_json"])
            record["schemaVersion"] = 2
            connection.execute("UPDATE business_query_evidence SET record_json=?", (json.dumps(record),))
        with self.assertRaises(DiagnosticError):
            self.get()
        self.gateway.access_control.clock = lambda: self.initial_time + timedelta(days=31)
        self.store.cleanup_expired()
        with self.assertRaises(DiagnosticError) as expired:
            self.get()
        self.assertEqual(expired.exception.status, 404)

    def test_republishing_identical_tree_stales_prior_activation_evidence(self):
        self.record()
        revision = self.project.published_revision
        self.project.publish(label="Identical semantic content, new activation")
        self.assertEqual(self.project.published_revision, revision)
        self.assertEqual(self.get()["checks"][0]["status"], "stale")

    def test_template_permission_rejects_a_non_active_datasource_binding(self):
        with self.assertRaises(DiagnosticError) as caught:
            self.store.validate_template_permission(
                auth=self.auth,
                project_id="runtime-test",
                template=self.template,
                datasource_id="some-other-datasource",
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "BUSINESS_EVIDENCE_STALE")

    def test_binding_fence_releases_after_mutation_exception_and_rejects_reader_upgrade(self):
        with self.project.bound_execution():
            with self.assertRaises(RuntimeError):
                with self.project.binding_mutation():
                    self.fail("an execution cannot upgrade itself into a mutation")
        with self.assertRaises(ValueError):
            with self.project.binding_mutation():
                raise ValueError("mutation failed")
        # Both exception paths must release their fence state for the next call.
        with self.project.bound_execution():
            pass

    def test_publish_and_datasource_switch_wait_for_governed_dispatch_but_cancel_does_not(self):
        class NoopIndexLifecycle:
            def stage(self, _manifest, _project_dir, _revision):
                pass

            def activate(self, _revision):
                pass

            def status(self, _revision=None):
                return {"status": "ready"}

        class BlockingDispatcher:
            def __init__(self, delegate):
                self.delegate = delegate
                self.entered = threading.Event()
                self.release = threading.Event()

            def dispatch(self, request):
                if request["method"] == "query.run":
                    self.entered.set()
                    if not self.release.wait(5):
                        raise TimeoutError("test dispatcher gate timed out")
                return self.delegate.dispatch(request)

        self.project.semantic_index_lifecycle = NoopIndexLifecycle()
        self.project.put_file(
            "models/orders/metadata.yml",
            "name: orders\ntable_reference: {schema: public, table: orders}\ncolumns:\n"
            "  - {name: id, type: BIGINT}\n  - {name: amount, type: DECIMAL}\n",
        )
        self.project.publish(label="business evidence fixture")
        service = SemanticConsoleService(self.project)
        service.create_rule({
            "title": "Average order amount",
            "content": "Use the average amount from orders.",
            "executionChecks": {"schemaVersion": 1, "checks": [
                {"id": "average", "kind": "metric", "model": "orders", "column": "amount", "aggregation": "avg"},
            ]},
        })
        self.project.publish(label="publish average amount rule")
        self.project.datasource_records()["alternate-source"] = DatasourceRecord(
            "alternate-source", "Alternate warehouse", "postgres", {"database": "alternate"}
        )
        self.project.save_datasources()

        prepare_status, prepared = self.gateway.dispatch({
            "protocolVersion": "3", "id": "prepare-concurrent-run", "method": "query.prepare",
            "params": {"question": "Average amount", "semanticSql": "SELECT AVG(amount) FROM orders", "conditions": {}, "confirmedConditions": []},
        }, self.authorization)
        self.assertEqual(prepare_status, 200)
        self.assertEqual(prepared["result"]["status"], "ready")

        blocking = BlockingDispatcher(self.dispatcher)
        self.gateway.dispatcher = blocking
        run_result = {}
        run_thread = threading.Thread(target=lambda: run_result.setdefault("response", self.gateway.dispatch({
            "protocolVersion": "3", "id": "run-concurrent", "method": "query.run",
            "params": {
                "question": "Average amount", "semanticSql": "SELECT AVG(amount) FROM orders",
                "queryId": "q-concurrent", "preparationId": prepared["result"]["preparationId"],
            },
        }, self.authorization)))
        run_thread.start()
        self.assertTrue(blocking.entered.wait(3), "query.run did not reach the dispatcher")

        mutation_done = threading.Event()
        mutation_thread = threading.Thread(target=lambda: (
            service.activate_datasource("alternate-source"), mutation_done.set()
        ))
        mutation_thread.start()
        publish_done = threading.Event()
        publish_thread = threading.Thread(target=lambda: (
            self.project.publish(label="publish queued behind governed execution"), publish_done.set()
        ))
        publish_thread.start()
        try:
            self.assertFalse(mutation_done.wait(0.1), "datasource mutation crossed the pinned execution")
            self.assertFalse(publish_done.is_set(), "publish crossed the pinned execution")
            # Cancellation uses the project lock briefly, but does not wait on
            # the execution fence held by query.run.
            cancel_status, cancel = self.gateway.dispatch({
                "protocolVersion": "3", "id": "cancel-concurrent", "method": "query.cancel",
                "params": {"queryId": "q-concurrent"},
            }, self.authorization)
            self.assertEqual(cancel_status, 200)
            self.assertTrue(cancel["ok"])
            self.assertFalse(mutation_done.is_set())
            self.assertFalse(publish_done.is_set())
        finally:
            blocking.release.set()
            run_thread.join(5)
            mutation_thread.join(5)
            publish_thread.join(5)

        self.assertFalse(run_thread.is_alive())
        self.assertFalse(mutation_thread.is_alive())
        self.assertFalse(publish_thread.is_alive())
        self.assertTrue(mutation_done.is_set())
        self.assertTrue(publish_done.is_set())
        self.assertIn(run_result["response"][0], {200, 409})
        self.assertTrue(any(item["method"] == "query.run" for item in self.dispatcher.requests))

    def test_rpc_is_core_owned_and_does_not_dispatch_to_wren(self):
        self.record()
        status, response = self.gateway.dispatch({"protocolVersion": "3", "id": "read", "method": "query.evidence", "params": {"queryId": "q-evidence"}}, self.authorization)
        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["schemaVersion"], 1)
        self.assertEqual(self.dispatcher.requests, [])
        status, _ = self.gateway.dispatch({"protocolVersion": "3", "id": "overwrite", "method": "query.evidence", "params": {"queryId": "q-evidence", "checks": []}}, self.authorization)
        self.assertEqual(status, 400)

    def test_sensitive_or_unknown_fields_are_rejected(self):
        for field, value in (("semanticSql", "SELECT 1"), ("conditions", {"snapshot": "Bearer sensitive"}), ("schemaVersion", 2)):
            with self.subTest(field=field), self.assertRaises(DiagnosticError):
                validate_evidence({**self.template, field: value}, executed=False)


if __name__ == "__main__":
    unittest.main()
