from __future__ import annotations

import tempfile
import json
import shutil
import sqlite3
import subprocess
import threading
import unittest
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import urlopen

from server.access_control import AccessControlError, AccessControlStore
from server.access_api import AccessControlAdminApi
from server.identity import DingTalkProvider, GenericOidcProvider, IdentityProfile, IdentityProviderRegistry
from server.identity_api import IdentityApi
from server.app import SemanticConsoleHTTPServer, create_app
from server.project import ProjectStore
from server.runtime_rpc import RuntimeRpcGateway
from server.service import SemanticConsoleService


class RecordingTransport:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, url: str, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


@dataclass(frozen=True)
class FakeProvider:
    id: str = "work-sso"
    label: str = "Work SSO"
    redirect_uri: str = "http://127.0.0.1:48763/api/v1/auth/callback/work-sso"
    organization_id: str = "default"
    identity_key: str = "idp_fake-work-sso"

    def authorization_url(self, state: str) -> str:
        return f"https://identity.example/authorize?state={state}"

    def exchange(self, code: str) -> IdentityProfile:
        if code != "verified-code":
            raise AccessControlError("IDENTITY_PROVIDER_FAILED", "identity provider rejected the code", status=502)
        return IdentityProfile(
            "employee-001",
            "Employee A",
            "corp-1",
            {"employeeNumber": "A001", "email": "employee@example.com"},
        )


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-identity-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "access-control.sqlite3"
        self.store = AccessControlStore(
            self.database,
            bootstrap_token="bootstrap-token-that-is-at-least-thirty-two-characters",
        )

    def test_external_login_maps_to_one_user_without_overwriting_policy_attributes(self) -> None:
        first = self.store.upsert_external_user(
            provider="work-sso",
            external_subject="employee-001",
            name="Employee A",
            profile={"employeeNumber": "A001"},
        )
        self.store.update_user(first.id, attributes={"regionCodes": ["CN-JIA"]})

        second = self.store.upsert_external_user(
            provider="work-sso",
            external_subject="employee-001",
            name="Employee A Renamed",
            profile={"employeeNumber": "A001", "email": "a@example.com"},
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(second.name, "Employee A Renamed")
        self.assertEqual(second.attributes, {"regionCodes": ["CN-JIA"]})
        users = self.store.list_users()
        self.assertEqual(users[0]["identities"][0]["profile"]["employeeNumber"], "A001")
        self.assertEqual(users[0]["policyIds"], [])

    def test_employee_sessions_are_bounded_revocable_and_never_persist_plaintext(self) -> None:
        user = self.store.upsert_external_user(
            provider="work-sso", external_subject="employee-001", name="Employee A"
        )
        issued = self.store.issue_session(user.id, ttl_seconds=300)
        auth = self.store.authenticate(f"Bearer {issued['accessToken']}")

        self.assertEqual(auth.method, "oauth_session")
        self.assertEqual(auth.subject.id, user.id)
        self.assertNotIn(issued["accessToken"].encode(), self.database.read_bytes())

        self.store.revoke_session(auth.credential_id or "")
        with self.assertRaises(AccessControlError) as revoked:
            self.store.authenticate(f"Bearer {issued['accessToken']}")
        self.assertEqual(revoked.exception.code, "UNAUTHENTICATED")

    def test_expired_or_tampered_login_artifacts_fail_closed(self) -> None:
        current = [datetime(2026, 8, 31, tzinfo=UTC)]
        store = AccessControlStore(
            Path(self.temp.name) / "expiring.sqlite3",
            bootstrap_token="bootstrap-token-that-is-at-least-thirty-two-characters",
            clock=lambda: current[0],
        )
        transaction = store.begin_identity_login("work-sso", ttl_seconds=60)

        with self.assertRaises(AccessControlError) as tampered:
            store.verify_identity_state("work-sso", transaction["state"][:-1] + "x")
        self.assertEqual(tampered.exception.code, "INVALID_LOGIN")
        with self.assertRaises(AccessControlError) as wrong_provider:
            store.verify_identity_state("other-sso", transaction["state"])
        self.assertEqual(wrong_provider.exception.code, "INVALID_LOGIN")

        current[0] += timedelta(seconds=61)
        with self.assertRaises(AccessControlError) as expired_state:
            store.verify_identity_state("work-sso", transaction["state"])
        self.assertEqual(expired_state.exception.code, "LOGIN_EXPIRED")
        with self.assertRaises(AccessControlError) as expired_device:
            store.consume_identity_device_code(transaction["deviceCode"])
        self.assertEqual(expired_device.exception.code, "LOGIN_EXPIRED")

    def test_expired_employee_session_is_rejected(self) -> None:
        current = [datetime(2026, 8, 31, tzinfo=UTC)]
        store = AccessControlStore(
            Path(self.temp.name) / "session-expiry.sqlite3",
            bootstrap_token="bootstrap-token-that-is-at-least-thirty-two-characters",
            clock=lambda: current[0],
        )
        user = store.upsert_external_user(
            provider="work-sso", external_subject="employee-001", name="Employee A"
        )
        session = store.issue_session(user.id, ttl_seconds=300)
        current[0] += timedelta(seconds=301)

        with self.assertRaises(AccessControlError) as expired:
            store.authenticate(f"Bearer {session['accessToken']}")

        self.assertEqual(expired.exception.code, "UNAUTHENTICATED")

    def test_device_login_requires_browser_confirmation_then_returns_one_session(self) -> None:
        api = IdentityApi(self.store, IdentityProviderRegistry({"work-sso": FakeProvider()}))
        status, started = api.dispatch("POST", "/api/v1/auth/device/start", {}, {"provider": "work-sso"}, None) or (0, {})
        self.assertEqual(status, 201)
        state = parse_qs(urlsplit(started["verificationUriComplete"]).query)["state"][0]

        status, pending = api.dispatch(
            "POST", "/api/v1/auth/device/token", {}, {"deviceCode": started["deviceCode"]}, None
        ) or (0, {})
        self.assertEqual((status, pending["status"]), (202, "authorization_pending"))

        status, callback = api.dispatch(
            "GET",
            "/api/v1/auth/callback/work-sso",
            {"state": state, "code": "verified-code"},
            None,
            None,
        ) or (0, {})
        self.assertEqual((status, callback["status"]), (200, "confirmation_required"))
        self.assertRegex(callback["confirmationCode"], r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
        self.assertNotIn("accessToken", callback)
        self.assertNotIn(callback["confirmationCode"].encode(), self.database.read_bytes())

        status, confirmation_required = api.dispatch(
            "POST", "/api/v1/auth/device/token", {}, {"deviceCode": started["deviceCode"]}, None
        ) or (0, {})
        self.assertEqual((status, confirmation_required["status"]), (202, "confirmation_required"))

        status, session = api.dispatch(
            "POST",
            "/api/v1/auth/device/token",
            {},
            {
                "deviceCode": started["deviceCode"],
                "confirmationCode": callback["confirmationCode"],
            },
            None,
        ) or (0, {})
        self.assertEqual(status, 200)
        auth = self.store.authenticate(f"Bearer {session['accessToken']}")
        self.assertEqual(auth.subject.name, "Employee A")

        status, reused = api.dispatch(
            "POST", "/api/v1/auth/device/token", {}, {"deviceCode": started["deviceCode"]}, None
        ) or (0, {})
        self.assertEqual((status, reused["code"]), (409, "INVALID_LOGIN"))

    def test_forwarded_authorization_url_and_device_code_cannot_swap_the_victims_session(self) -> None:
        api = IdentityApi(self.store, IdentityProviderRegistry({"work-sso": FakeProvider()}))
        _, attacker_started = api.dispatch(
            "POST", "/api/v1/auth/device/start", {}, {"provider": "work-sso"}, None
        ) or (0, {})
        forwarded_state = parse_qs(
            urlsplit(attacker_started["verificationUriComplete"]).query
        )["state"][0]

        status, victim_browser = api.dispatch(
            "GET",
            "/api/v1/auth/callback/work-sso",
            {"state": forwarded_state, "code": "verified-code"},
            None,
            None,
        ) or (0, {})
        self.assertEqual((status, victim_browser["status"]), (200, "confirmation_required"))

        status, stolen_without_confirmation = api.dispatch(
            "POST",
            "/api/v1/auth/device/token",
            {},
            {"deviceCode": attacker_started["deviceCode"]},
            None,
        ) or (0, {})
        self.assertEqual(
            (status, stolen_without_confirmation["status"]),
            (202, "confirmation_required"),
        )
        self.assertNotIn("accessToken", stolen_without_confirmation)

        status, guessed = api.dispatch(
            "POST",
            "/api/v1/auth/device/token",
            {},
            {"deviceCode": attacker_started["deviceCode"], "confirmationCode": "AAAA-AAAA"},
            None,
        ) or (0, {})
        self.assertEqual((status, guessed["code"]), (400, "INVALID_CONFIRMATION"))
        self.assertNotIn("accessToken", guessed)

    def test_confirmation_guess_limit_is_committed_and_terminal(self) -> None:
        api = IdentityApi(self.store, IdentityProviderRegistry({"work-sso": FakeProvider()}))
        _, started = api.dispatch(
            "POST", "/api/v1/auth/device/start", {}, {"provider": "work-sso"}, None
        ) or (0, {})
        state = parse_qs(urlsplit(started["verificationUriComplete"]).query)["state"][0]
        _, callback = api.dispatch(
            "GET",
            "/api/v1/auth/callback/work-sso",
            {"state": state, "code": "verified-code"},
            None,
            None,
        ) or (0, {})

        for _ in range(5):
            status, denied = api.dispatch(
                "POST",
                "/api/v1/auth/device/token",
                {},
                {"deviceCode": started["deviceCode"], "confirmationCode": "1111-1111"},
                None,
            ) or (0, {})
            self.assertEqual((status, denied["code"]), (400, "INVALID_CONFIRMATION"))

        status, terminal = api.dispatch(
            "POST",
            "/api/v1/auth/device/token",
            {},
            {"deviceCode": started["deviceCode"], "confirmationCode": callback["confirmationCode"]},
            None,
        ) or (0, {})
        self.assertEqual((status, terminal["code"]), (409, "INVALID_LOGIN"))

    def test_identity_api_lists_providers_and_rejects_a_denied_callback(self) -> None:
        api = IdentityApi(self.store, IdentityProviderRegistry({"work-sso": FakeProvider()}))
        status, providers = api.dispatch("GET", "/api/v1/auth/providers", {}, None, None) or (0, {})
        self.assertEqual(status, 200)
        self.assertEqual(providers, {"items": [{"id": "work-sso", "label": "Work SSO"}]})

        _, started = api.dispatch(
            "POST", "/api/v1/auth/device/start", {}, {"provider": "work-sso"}, None
        ) or (0, {})
        state = parse_qs(urlsplit(started["verificationUriComplete"]).query)["state"][0]
        status, denied = api.dispatch(
            "GET",
            "/api/v1/auth/callback/work-sso",
            {"state": state, "error": "access_denied"},
            None,
            None,
        ) or (0, {})
        self.assertEqual((status, denied["code"]), (400, "LOGIN_DENIED"))

    def test_dingtalk_adapter_uses_current_oauth_endpoints_without_retaining_access_token(self) -> None:
        transport = RecordingTransport(
            [
                {"accessToken": "dingtalk-secret-token", "corpId": "corp-1"},
                {"unionId": "union-1", "openId": "open-1", "nick": "Employee A"},
            ]
        )
        provider = DingTalkProvider(
            id="dingtalk",
            label="DingTalk",
            client_id="ding-client",
            client_secret="ding-secret",
            redirect_uri="http://127.0.0.1:48763/api/v1/auth/callback/dingtalk",
            allowed_organization_external_ids=("corp-1",),
            transport=transport,
        )

        profile = provider.exchange("authorization-code")

        self.assertEqual(profile.external_subject, "union-1")
        self.assertEqual(profile.organization_external_id, "corp-1")
        self.assertNotIn("employeeNumber", profile.profile)
        self.assertNotIn("dingtalk-secret-token", repr(profile))
        self.assertNotIn("ding-secret", repr(provider))
        self.assertEqual(transport.calls[0]["url"], DingTalkProvider.TOKEN_ENDPOINT)
        self.assertEqual(
            transport.calls[1]["headers"], {"x-acs-dingtalk-access-token": "dingtalk-secret-token"}
        )

    def test_dingtalk_rejects_an_account_from_an_unconfigured_organization(self) -> None:
        transport = RecordingTransport(
            [
                {"accessToken": "dingtalk-secret-token", "corpId": "unexpected-corp"},
                {"unionId": "union-1", "nick": "Employee A"},
            ]
        )
        provider = DingTalkProvider(
            id="dingtalk",
            label="DingTalk",
            client_id="ding-client",
            client_secret="ding-secret",
            redirect_uri="http://127.0.0.1:48763/api/v1/auth/callback/dingtalk",
            allowed_organization_external_ids=("corp-1",),
            transport=transport,
        )

        with self.assertRaises(AccessControlError) as rejected:
            provider.exchange("authorization-code")

        self.assertEqual(rejected.exception.code, "ORGANIZATION_NOT_ALLOWED")

    def test_dingtalk_environment_configuration_requires_a_corporation_allowlist(self) -> None:
        config = json.dumps(
            {
                "dingtalk": {
                    "type": "dingtalk",
                    "clientId": "ding-client",
                    "clientSecret": "ding-secret",
                    "redirectUri": "http://127.0.0.1:48763/api/v1/auth/callback/dingtalk",
                }
            }
        )
        with patch.dict("os.environ", {"SEMARAIL_IDENTITY_PROVIDERS": config}):
            with self.assertRaisesRegex(ValueError, "allowedOrganizationExternalId"):
                IdentityProviderRegistry.from_environment()

    def test_environment_registry_accepts_explicit_dingtalk_and_single_tenant_oidc(self) -> None:
        config = json.dumps(
            {
                "dingtalk": {
                    "type": "dingtalk",
                    "label": "Company DingTalk",
                    "clientId": "ding-client",
                    "clientSecret": "ding-secret",
                    "redirectUri": "http://127.0.0.1:48763/api/v1/auth/callback/dingtalk",
                    "allowedOrganizationExternalIds": ["corp-1"],
                },
                "company-oidc": {
                    "type": "oidc",
                    "label": "Company SSO",
                    "clientId": "oidc-client",
                    "clientSecret": "oidc-secret",
                    "authorizationEndpoint": "https://identity.example/authorize",
                    "tokenEndpoint": "https://identity.example/token",
                    "userinfoEndpoint": "https://identity.example/userinfo",
                    "redirectUri": "http://127.0.0.1:48763/api/v1/auth/callback/company-oidc",
                    "singleTenantIssuer": True,
                },
            }
        )
        with patch.dict("os.environ", {"SEMARAIL_IDENTITY_PROVIDERS": config}):
            registry = IdentityProviderRegistry.from_environment()

        self.assertEqual(
            registry.public_items(),
            [
                {"id": "dingtalk", "label": "Company DingTalk"},
                {"id": "company-oidc", "label": "Company SSO"},
            ],
        )
        self.assertNotEqual(registry.get("dingtalk").identity_key, registry.get("company-oidc").identity_key)

    def test_provider_reconfiguration_cannot_take_over_an_existing_subject(self) -> None:
        first = self.store.upsert_external_user(
            provider="company-sso",
            provider_key="idp_original",
            external_subject="employee-001",
            name="Employee A",
        )
        second = self.store.upsert_external_user(
            provider="company-sso",
            provider_key="idp_reconfigured",
            external_subject="employee-001",
            name="Another Employee",
        )

        self.assertNotEqual(first.id, second.id)

    def test_pre_release_external_identity_schema_is_migrated_without_losing_users(self) -> None:
        user = self.store.upsert_external_user(
            provider="legacy-sso", external_subject="employee-legacy", name="Legacy Employee"
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                DROP INDEX identity_subject_idx;
                ALTER TABLE external_identities RENAME TO external_identities_new;
                CREATE TABLE external_identities (
                    provider TEXT NOT NULL,
                    external_subject TEXT NOT NULL,
                    subject_id TEXT NOT NULL REFERENCES subjects(id),
                    organization_external_id TEXT,
                    profile_json TEXT NOT NULL,
                    last_login_at TEXT NOT NULL,
                    PRIMARY KEY(provider,external_subject),
                    UNIQUE(provider,subject_id)
                );
                INSERT INTO external_identities(
                    provider,external_subject,subject_id,organization_external_id,profile_json,last_login_at
                )
                SELECT provider,external_subject,subject_id,organization_external_id,profile_json,last_login_at
                FROM external_identities_new;
                DROP TABLE external_identities_new;
                CREATE INDEX identity_subject_idx ON external_identities(subject_id);
                """
            )

        migrated = AccessControlStore(
            self.database,
            bootstrap_token="bootstrap-token-that-is-at-least-thirty-two-characters",
        )

        self.assertEqual(migrated.list_users()[0]["id"], user.id)
        with closing(sqlite3.connect(self.database)) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(external_identities)")
            }
        self.assertIn("provider_key", columns)

    def test_external_identity_cannot_silently_move_to_another_external_organization(self) -> None:
        self.store.upsert_external_user(
            provider="work-sso",
            external_subject="employee-001",
            name="Employee A",
            organization_external_id="corp-1",
        )

        with self.assertRaises(AccessControlError) as moved:
            self.store.upsert_external_user(
                provider="work-sso",
                external_subject="employee-001",
                name="Employee A",
                organization_external_id="corp-2",
            )

        self.assertEqual(moved.exception.code, "ORGANIZATION_MISMATCH")

    def test_generic_oidc_maps_only_selected_profile_claims(self) -> None:
        transport = RecordingTransport(
            [
                {"access_token": "oidc-secret-token"},
                {"sub": "employee-002", "name": "Employee B", "email": "b@example.com", "groups": ["admin"]},
            ]
        )
        provider = GenericOidcProvider(
            id="oidc",
            label="Company SSO",
            client_id="client",
            client_secret="secret",
            authorization_endpoint="https://identity.example/authorize",
            token_endpoint="https://identity.example/token",
            userinfo_endpoint="https://identity.example/userinfo",
            redirect_uri="http://127.0.0.1:48763/api/v1/auth/callback/oidc",
            transport=transport,
        )

        profile = provider.exchange("authorization-code")

        self.assertEqual(profile.external_subject, "employee-002")
        self.assertEqual(profile.profile, {"email": "b@example.com"})
        self.assertNotIn("groups", profile.profile)
        self.assertNotIn("oidc-secret-token", repr(profile))

    def test_generic_oidc_subject_claim_is_part_of_immutable_provider_identity(self) -> None:
        common = {
            "id": "oidc",
            "label": "Company SSO",
            "client_id": "client",
            "client_secret": "secret",
            "authorization_endpoint": "https://identity.example/authorize",
            "token_endpoint": "https://identity.example/token",
            "userinfo_endpoint": "https://identity.example/userinfo",
            "redirect_uri": "http://127.0.0.1:48763/api/v1/auth/callback/oidc",
        }

        self.assertNotEqual(
            GenericOidcProvider(**common, subject_claim="sub").identity_key,
            GenericOidcProvider(**common, subject_claim="employee_id").identity_key,
        )

    def test_administrator_can_assign_user_attributes_and_policy_after_first_login(self) -> None:
        user = self.store.upsert_external_user(
            provider="work-sso", external_subject="employee-001", name="Employee A"
        )
        policy = self.store.create_policy(
            "Sales reader", {"schemaVersion": 1, "tools": ["semantic:read"], "tables": {}}
        )
        api = AccessControlAdminApi(self.store)
        authorization = "Bearer bootstrap-token-that-is-at-least-thirty-two-characters"

        status, updated = api.dispatch(
            "PUT",
            f"/api/v1/access/users/{user.id}",
            {"attributes": {"regionCodes": ["CN-YI"]}},
            authorization,
        ) or (0, {})
        self.assertEqual(status, 200)
        self.assertEqual(updated["attributes"]["regionCodes"], ["CN-YI"])
        status, _ = api.dispatch(
            "POST",
            "/api/v1/access/policy-bindings",
            {"subjectId": user.id, "policyId": policy["id"]},
            authorization,
        ) or (0, {})
        self.assertEqual(status, 201)
        status, listed = api.dispatch(
            "GET", "/api/v1/access/users", None, authorization
        ) or (0, {})
        self.assertEqual(status, 200)
        self.assertEqual(listed["items"][0]["policyIds"], [policy["id"]])

        session = self.store.issue_session(user.id)
        status, disabled = api.dispatch(
            "PUT",
            f"/api/v1/access/users/{user.id}/status",
            {"status": "disabled"},
            authorization,
        ) or (0, {})
        self.assertEqual((status, disabled["status"]), (200, "disabled"))
        with self.assertRaises(AccessControlError) as rejected:
            self.store.authenticate(f"Bearer {session['accessToken']}")
        self.assertEqual(rejected.exception.code, "UNAUTHENTICATED")
        self.store.set_subject_status(user.id, "active")
        with self.assertRaises(AccessControlError) as still_revoked:
            self.store.authenticate(f"Bearer {session['accessToken']}")
        self.assertEqual(still_revoked.exception.code, "UNAUTHENTICATED")

    def test_real_cli_device_login_status_and_logout_round_trip(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        root = Path(self.temp.name)
        project = root / "project"
        project.mkdir()
        project.joinpath("wren_project.yml").write_text(
            "schema_version: 5\nname: identity-cli-test\ndata_source: postgres\n", encoding="utf-8"
        )
        project_store = ProjectStore(project, state_dir=root / "state")
        gateway = RuntimeRpcGateway(
            project_store,
            dispatcher=None,
            auth_token="bootstrap-token-that-is-at-least-thirty-two-characters",
            access_control=self.store,
        )
        identity_api = IdentityApi(self.store, IdentityProviderRegistry({"work-sso": FakeProvider()}))
        application = create_app(
            SemanticConsoleService(project_store), runtime_rpc=gateway, identity_api=identity_api
        )
        server = SemanticConsoleHTTPServer(("127.0.0.1", 0), application)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        session_file = root / "employee-session.json"
        cli = Path(__file__).resolve().parents[4] / "packages" / "core" / "bin" / "semarail.mjs"
        process = subprocess.Popen(
            [
                node,
                str(cli),
                "auth",
                "login",
                "--provider",
                "work-sso",
                "--endpoint",
                endpoint,
                "--session-file",
                str(session_file),
                "--no-open",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "Open this URL to continue:")
            authorization_url = process.stdout.readline().strip()
            state = parse_qs(urlsplit(authorization_url).query)["state"][0]
            with urlopen(
                f"{endpoint}/api/v1/auth/callback/work-sso?state={quote(state)}&code=verified-code",
                timeout=3,
            ) as response:
                callback = json.loads(response.read())
                self.assertEqual(callback["status"], "confirmation_required")
            stdout, stderr = process.communicate(input=f"{callback['confirmationCode']}\n", timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("Signed in as Employee A", stdout)
            saved = json.loads(session_file.read_text(encoding="utf-8"))
            self.assertTrue(saved["accessToken"].startswith("sr_session_"))

            status = subprocess.run(
                [node, str(cli), "auth", "status", "--session-file", str(session_file)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Signed in as Employee A", status.stdout)
            logout = subprocess.run(
                [node, str(cli), "auth", "logout", "--session-file", str(session_file)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(logout.returncode, 0, logout.stderr)
            self.assertFalse(session_file.exists())
            with self.assertRaises(AccessControlError):
                self.store.authenticate(f"Bearer {saved['accessToken']}")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
