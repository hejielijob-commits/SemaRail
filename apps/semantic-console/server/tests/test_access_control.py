from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from server.access_control import AccessControlError, AccessControlStore


class AccessControlStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-access-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "access-control.sqlite3"
        self.bootstrap = "bootstrap-token-that-is-at-least-thirty-two-characters"
        self.store = AccessControlStore(self.database, bootstrap_token=self.bootstrap)

    def test_two_service_accounts_receive_independent_keys_and_plaintext_is_not_persisted(self) -> None:
        account_a = self.store.create_service_account("Region A Agent", attributes={"regionCodes": ["CN-JIA"]})
        account_b = self.store.create_service_account("Region B Agent", attributes={"regionCodes": ["CN-YI"]})
        key_a = self.store.issue_api_key(account_a.id, label="codex-a")
        key_b = self.store.issue_api_key(account_b.id, label="codex-b")

        self.assertNotEqual(key_a["apiKey"], key_b["apiKey"])
        auth_a = self.store.authenticate(f"Bearer {key_a['apiKey']}")
        auth_b = self.store.authenticate(f"Bearer {key_b['apiKey']}")
        self.assertEqual(auth_a.subject.attributes["regionCodes"], ["CN-JIA"])
        self.assertEqual(auth_b.subject.attributes["regionCodes"], ["CN-YI"])

        database_bytes = self.database.read_bytes()
        self.assertNotIn(key_a["apiKey"].encode(), database_bytes)
        self.assertNotIn(key_b["apiKey"].encode(), database_bytes)
        with closing(sqlite3.connect(self.database)) as connection:
            columns = {item[1] for item in connection.execute("PRAGMA table_info(credentials)")}
        self.assertNotIn("api_key", columns)
        self.assertNotIn("secret", columns)

    def test_revocation_and_subject_disable_take_effect_immediately(self) -> None:
        account = self.store.create_service_account("Report Agent")
        issued = self.store.issue_api_key(account.id)
        self.store.authenticate(f"Bearer {issued['apiKey']}")

        self.store.revoke_credential(issued["credential"]["id"])
        with self.assertRaises(AccessControlError) as revoked:
            self.store.authenticate(f"Bearer {issued['apiKey']}")
        self.assertEqual(revoked.exception.code, "UNAUTHENTICATED")

        replacement = self.store.issue_api_key(account.id)
        self.store.set_subject_status(account.id, "disabled")
        with self.assertRaises(AccessControlError):
            self.store.authenticate(f"Bearer {replacement['apiKey']}")

    def test_rotation_revokes_old_key_and_returns_new_plaintext_once(self) -> None:
        account = self.store.create_service_account("Rotating Agent")
        issued = self.store.issue_api_key(account.id, label="first")

        rotated = self.store.rotate_credential(issued["credential"]["id"], label="second")

        with self.assertRaises(AccessControlError):
            self.store.authenticate(f"Bearer {issued['apiKey']}")
        auth = self.store.authenticate(f"Bearer {rotated['apiKey']}")
        self.assertEqual(auth.subject.id, account.id)
        self.assertEqual(rotated["replacedCredentialId"], issued["credential"]["id"])
        self.assertNotIn(rotated["apiKey"].encode(), self.database.read_bytes())

    def test_policy_binding_and_append_only_audit(self) -> None:
        account = self.store.create_service_account("Sales Agent")
        policy = self.store.create_policy(
            "Sales reader",
            {"schemaVersion": 1, "tools": ["semantic:read"], "tables": {}},
        )
        self.store.bind_policy(account.id, policy["id"])
        issued = self.store.issue_api_key(account.id)
        auth = self.store.authenticate(f"Bearer {issued['apiKey']}")
        event_id = self.store.record_audit(
            action="context.ask",
            decision="allowed",
            auth=auth,
            resource="sales",
            policy_version=f"{policy['id']}:1",
            details={"requestId": "req-1"},
        )

        policies = self.store.policies_for_subject(account.id)
        events = self.store.list_audit()
        self.assertEqual(policies[0]["id"], policy["id"])
        self.assertEqual(events[0]["id"], event_id)
        self.assertEqual(events[0]["subjectId"], account.id)
        self.assertEqual(events[0]["details"], {"requestId": "req-1"})

    def test_policy_update_is_visible_to_existing_binding_immediately(self) -> None:
        account = self.store.create_service_account("Mutable Policy Agent")
        policy = self.store.create_policy(
            "Sales regions",
            {"schemaVersion": 1, "tools": ["semantic:read"], "tables": {}},
        )
        self.store.bind_policy(account.id, policy["id"])

        updated = self.store.update_policy(
            policy["id"],
            {"schemaVersion": 1, "tools": ["query:execute"], "tables": {}},
        )

        self.assertEqual(updated["version"], 2)
        bound = self.store.policies_for_subject(account.id)
        self.assertEqual(bound[0]["version"], 2)
        self.assertEqual(bound[0]["document"]["tools"], ["query:execute"])

        changed = self.store.update_service_account(account.id, attributes={"regionCodes": ["CN-YI"]})
        self.assertEqual(changed.attributes["regionCodes"], ["CN-YI"])
        self.assertEqual(self.store.subject(account.id).attributes["regionCodes"], ["CN-YI"])
        self.assertEqual(self.store.list_policies()[0]["id"], policy["id"])

    def test_bootstrap_token_maps_to_virtual_administrator(self) -> None:
        auth = self.store.authenticate(f"Bearer {self.bootstrap}")
        self.assertEqual(auth.method, "bootstrap_token")
        self.assertEqual(auth.subject.attributes["roles"], ["admin"])


if __name__ == "__main__":
    unittest.main()
