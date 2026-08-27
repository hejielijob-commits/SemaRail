from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.knowledge import RULE_INDEX_PATH
from server.project import ProjectError, ProjectStore
from server.service import ApiServiceError, SemanticConsoleService


class FakeValidator:
    def health(self):
        return {"available": False, "version": None}

    def validate(self, project_dir: Path):
        return {"valid": True, "errors": [], "warnings": [], "errorCount": 0, "warningCount": 0}

    def build(self, project_dir: Path):
        return {}


class KnowledgeGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="semantic-console-knowledge-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project_dir = self.root / "project"
        self.project_dir.mkdir()
        (self.project_dir / "wren_project.yml").write_text(
            "schema_version: 5\nname: knowledge-test\ndata_source: postgres\n",
            encoding="utf-8",
        )

    def service(self) -> SemanticConsoleService:
        return SemanticConsoleService(
            ProjectStore(self.project_dir, state_dir=self.root / "state", validator=FakeValidator())
        )

    def test_legacy_multi_rule_file_can_disable_one_without_loss_and_restore_exactly(self):
        original = "## Rules\n\n- First rule\n  continuation\n- Second rule\n  more\n"
        rule_path = self.project_dir / "knowledge" / "rules" / "sales.md"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text(original, encoding="utf-8")
        service = self.service()

        listed = service.list_rules()
        self.assertEqual(len(listed["rules"]), 2)
        first = next(item for item in listed["rules"] if item["title"] == "First rule")
        second = next(item for item in listed["rules"] if item["title"] == "Second rule")
        disabled = service.set_rule_enabled(first["id"], False)

        active = service.project.read_file("knowledge/rules/sales.md")["content"]
        self.assertNotIn("First rule", active)
        self.assertIn("Second rule", active)
        self.assertFalse(next(item for item in disabled["rules"] if item["id"] == first["id"])["enabled"])
        disabled_path = service.get_rule(first["id"])["disabledPath"]
        self.assertTrue(disabled_path.startswith("semantic-console/rules-disabled/"))
        self.assertEqual(service.project.read_file(disabled_path)["content"].replace("\r\n", "\n"), "- First rule\n  continuation\n")
        self.assertFalse(any(item["path"].startswith("knowledge/rules/") and "First rule" in service.project.read_file(item["path"])["content"] for item in service.project.files()))

        restored = service.set_rule_enabled(first["id"], True)
        self.assertTrue(next(item for item in restored["rules"] if item["id"] == first["id"])["enabled"])
        self.assertEqual(service.project.read_file("knowledge/rules/sales.md")["content"].replace("\r\n", "\n"), original)
        self.assertEqual(next(item for item in service.list_rules()["rules"] if item["id"] == second["id"])["title"], "Second rule")

    def test_single_rule_disable_moves_entire_document_out_of_effective_directory(self):
        path = self.project_dir / "knowledge" / "rules" / "privacy.md"
        path.parent.mkdir(parents=True)
        path.write_text("Do not expose private fields.\n", encoding="utf-8")
        service = self.service()
        rule = service.list_rules()["rules"][0]
        result = service.set_rule_enabled(rule["id"], False)
        self.assertFalse(result["rule"]["enabled"])
        with self.assertRaises(ProjectError):
            service.project.read_file("knowledge/rules/privacy.md")
        disabled_path = service.get_rule(rule["id"])["disabledPath"]
        self.assertEqual(service.project.read_file(disabled_path)["content"].replace("\r\n", "\n"), "Do not expose private fields.\n")
        service.set_rule_enabled(rule["id"], True)
        self.assertEqual(service.project.read_file("knowledge/rules/privacy.md")["content"].replace("\r\n", "\n"), "Do not expose private fields.\n")

    def test_rule_index_unknown_root_and_record_fields_survive_toggle(self):
        rules = self.project_dir / "knowledge" / "rules"
        rules.mkdir(parents=True)
        (rules / "sales.md").write_text("- Keep this\n", encoding="utf-8")
        service = self.service()
        index = {
            "schemaVersion": 1,
            "customRoot": {"owner": "analytics"},
            "rules": [{
                "id": "rule_aaaaaaaaaaaaaaaaaaaaaaaa",
                "sourcePath": "knowledge/rules/sales.md",
                "sourceFormat": "single",
                "customRecord": "preserve",
                "enabled": True,
            }],
        }
        # Seed the index through the project draft API so the test exercises
        # the same path safety and revision machinery as the UI.
        service.project.put_file(RULE_INDEX_PATH, __import__("json").dumps(index))
        # The seeded id does not match the generated single-file identity;
        # list still safely derives the canonical record and writes a fresh
        # record only when the rule changes.  Unknown root data must remain.
        rule = service.list_rules()["rules"][0]
        service.set_rule_enabled(rule["id"], False)
        saved = __import__("json").loads(service.project.read_file(RULE_INDEX_PATH)["content"])
        self.assertEqual(saved["customRoot"], {"owner": "analytics"})
        self.assertTrue(any(item.get("customRecord") == "preserve" for item in saved["rules"]))

    def test_rule_paths_and_identifiers_fail_closed(self):
        service = self.service()
        with self.assertRaises(ApiServiceError) as error:
            service.create_rule({"slug": "../../outside", "content": "- unsafe"})
        self.assertEqual(error.exception.code, "INVALID_PATH")
        with self.assertRaises(ApiServiceError) as error:
            service.get_rule("../secret")
        self.assertEqual(error.exception.code, "INVALID_KNOWLEDGE")

    def test_sql_candidate_is_json_safe_deduplicated_and_only_approved_sql_enters_project(self):
        service = self.service()
        payload = {
            "question": "Show revenue",
            "sql": "SELECT 1\nFROM orders",
            "queryId": "query-1",
            "sessionId": "session-1",
            "dialect": "postgres",
            "stats": {"returnedRows": 1, "durationMs": 4},
            "sqlHistory": [{"id": "history-1", "question": "Older", "sql": "SELECT 2", "sourcePath": "knowledge/sql/older.md"}],
            "customMetadata": {"reviewContext": "manual"},
        }
        submitted = service.submit_sql_candidate(payload)
        self.assertTrue(submitted["created"])
        candidate = submitted["candidate"]
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(candidate["sqlHistory"], payload["sqlHistory"])
        self.assertEqual(candidate["historySqlRefs"], payload["sqlHistory"])
        self.assertEqual(candidate["stats"], payload["stats"])
        self.assertFalse(any(item["path"].startswith("knowledge/sql/") for item in service.project.files()))
        duplicate = service.submit_sql_candidate(payload)
        self.assertFalse(duplicate["created"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["candidate"]["id"], candidate["id"])
        self.assertEqual(len(service.list_sql_candidates()["candidates"]), 1)

        approved = service.approve_sql_candidate(candidate["id"])
        self.assertTrue(approved["approved"])
        self.assertTrue(approved["draft"])
        path = approved["path"]
        self.assertTrue(path.startswith("knowledge/sql/"))
        content = service.project.read_file(path)["content"]
        self.assertIn("nl: Show revenue", content)
        self.assertIn("SELECT 1", content)
        self.assertEqual(service.get_sql_candidate(candidate["id"])["status"], "approved")

    def test_sql_candidate_status_cannot_bypass_review_and_rejected_can_be_resubmitted(self):
        service = self.service()
        with self.assertRaises(ApiServiceError):
            service.submit_sql_candidate({"question": "Q", "sql": "SELECT 1", "status": "approved"})
        candidate = service.submit_sql_candidate({"question": "Q", "sql": "SELECT 1"})["candidate"]
        rejected = service.reject_sql_candidate(candidate["id"], {"reviewNote": "Check metric", "reviewer": "analyst"})
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(ApiServiceError):
            service.approve_sql_candidate(candidate["id"])
        pending = service.resubmit_sql_candidate(candidate["id"], {"reviewer": "analyst"})
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(service.approve_sql_candidate(candidate["id"])["approved"])

    def test_sql_candidate_validation_is_read_only_and_approval_rechecks(self):
        service = self.service()
        safe = service.submit_sql_candidate({"question": "Q", "sql": "WITH x AS (SELECT 1) SELECT * FROM x"})["candidate"]
        self.assertTrue(service.validate_sql_candidate(safe["id"])["valid"])
        unsafe = service.submit_sql_candidate({"question": "Delete?", "sql": "DELETE FROM orders"})["candidate"]
        with self.assertRaises(ApiServiceError) as raised:
            service.validate_sql_candidate(unsafe["id"])
        self.assertEqual(raised.exception.code, "INVALID_SQL_CANDIDATE")
        with self.assertRaises(ApiServiceError):
            service.approve_sql_candidate(unsafe["id"])
        self.assertEqual(service.get_sql_candidate(unsafe["id"])["status"], "pending")

    def test_sql_approval_rolls_project_draft_back_when_queue_write_fails(self):
        service = self.service()
        candidate = service.submit_sql_candidate({"question": "Q", "sql": "SELECT 1"})["candidate"]
        with patch.object(service.sql_candidates, "_write", side_effect=ProjectError("KNOWLEDGE_STATE_FAILED", "failed")):
            with self.assertRaises(ApiServiceError) as error:
                service.approve_sql_candidate(candidate["id"])
        self.assertEqual(error.exception.code, "KNOWLEDGE_STATE_FAILED")
        self.assertEqual(service.get_sql_candidate(candidate["id"])["status"], "pending")
        self.assertFalse(any(path.startswith("knowledge/sql/") for path in service.project.drafts))

    def test_sql_paths_secrets_and_metadata_types_are_rejected(self):
        service = self.service()
        with self.assertRaises(ApiServiceError) as error:
            service.submit_sql_candidate({"question": "Q", "sql": "SELECT 1", "slug": "../escape"})
        self.assertEqual(error.exception.code, "INVALID_PATH")
        with self.assertRaises(ApiServiceError) as error:
            service.submit_sql_candidate({"question": "Q", "sql": "SELECT * FROM postgres://u:p@host/db"})
        self.assertEqual(error.exception.code, "CREDENTIALS_NOT_ALLOWED")
        with self.assertRaises(ApiServiceError) as error:
            service.submit_sql_candidate({"question": "Q", "sql": "SELECT 1", "sqlHistory": [{"bad": object()}]})
        self.assertEqual(error.exception.code, "INVALID_KNOWLEDGE")
        with self.assertRaises(ApiServiceError) as error:
            service.submit_sql_candidate({"question": "Q", "sql": "SELECT 1", "sqlHistory": ["oops"]})
        self.assertEqual(error.exception.code, "INVALID_KNOWLEDGE")
        with self.assertRaises(ApiServiceError) as error:
            service.submit_sql_candidate({"question": "Q", "sql": "SELECT 1", "metadata": {"note": "safe\npassword: dummy"}})
        self.assertEqual(error.exception.code, "CREDENTIALS_NOT_ALLOWED")
        with self.assertRaises(ApiServiceError) as error:
            service.submit_sql_candidate({"question": "Q", "sql": "SELECT 1", "sqlHistory": [{"id": "h", "question": "Q", "sql": "SELECT 1", "sourcePath": "../secret.md"}]})
        self.assertEqual(error.exception.code, "INVALID_PATH")

    def test_sql_candidate_public_response_uses_a_safe_allow_list(self):
        service = self.service()
        candidate = service.submit_sql_candidate({"question": "Q", "sql": "SELECT 1", "customMetadata": {"owner": "analytics"}})["candidate"]
        self.assertNotIn("customMetadata", candidate)
        stored = service.sql_candidates._read()["candidates"][0]
        self.assertEqual(stored["customMetadata"], {"owner": "analytics"})


if __name__ == "__main__":
    unittest.main()
