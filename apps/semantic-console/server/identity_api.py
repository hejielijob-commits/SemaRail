"""OAuth/OIDC employee login boundary for the Semantic Console and CLI."""

from __future__ import annotations

import re
from typing import Any, Mapping

try:
    from .access_control import AccessControlError, AccessControlStore, AuthContext
    from .identity import IdentityProviderRegistry
except ImportError:  # pragma: no cover - direct module loading
    from access_control import AccessControlError, AccessControlStore, AuthContext  # type: ignore[no-redef]
    from identity import IdentityProviderRegistry  # type: ignore[no-redef]


class IdentityApi:
    """Device-style bridge around browser authorization-code login.

    The browser callback never receives a SemaRail bearer token. The initiating
    CLI or client exchanges its high-entropy one-time device code after the
    external identity has been verified.
    """

    def __init__(self, store: AccessControlStore, providers: IdentityProviderRegistry | None = None) -> None:
        self.store = store
        self.providers = providers or IdentityProviderRegistry.from_environment()

    def dispatch(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any],
        body: Any,
        authorization: str | None,
    ) -> tuple[int, dict[str, Any]] | None:
        if not path.startswith("/api/v1/auth/"):
            return None
        payload = body if isinstance(body, Mapping) else {}
        try:
            if method == "GET" and path == "/api/v1/auth/providers":
                return 200, {"items": self.providers.public_items()}
            if method == "POST" and path == "/api/v1/auth/device/start":
                provider_id = payload.get("provider")
                if not isinstance(provider_id, str):
                    raise AccessControlError("INVALID_REQUEST", "provider is required")
                provider = self.providers.get(provider_id)
                transaction = self.store.begin_identity_login(provider.id)
                return 201, {
                    "provider": provider.id,
                    "verificationUriComplete": provider.authorization_url(transaction["state"]),
                    "deviceCode": transaction["deviceCode"],
                    "expiresAt": transaction["expiresAt"],
                    "interval": 2,
                }
            callback = re.fullmatch(r"/api/v1/auth/callback/([^/]+)", path)
            if method == "GET" and callback:
                provider = self.providers.get(callback.group(1))
                state = query.get("state")
                code = query.get("authCode") or query.get("code")
                if (
                    query.get("error")
                    or not isinstance(state, str)
                    or not isinstance(code, str)
                    or not code.strip()
                    or len(code) > 4096
                ):
                    raise AccessControlError("LOGIN_DENIED", "identity login was not completed", status=400)
                transaction_id = self.store.verify_identity_state(provider.id, state)
                profile = provider.exchange(code.strip())
                subject = self.store.upsert_external_user(
                    provider=provider.id,
                    provider_key=provider.identity_key,
                    external_subject=profile.external_subject,
                    name=profile.name,
                    organization_external_id=profile.organization_external_id,
                    profile=profile.profile,
                    organization_id=provider.organization_id,
                )
                self.store.complete_identity_login(transaction_id, subject.id)
                auth = AuthContext(subject, "external_identity")
                self.store.record_audit(
                    action="identity.login",
                    decision="allowed",
                    auth=auth,
                    resource=provider.id,
                    details={"transactionId": transaction_id},
                )
                return 200, {
                    "status": "authenticated",
                    "provider": provider.id,
                    "message": "Identity verified. You can return to SemaRail.",
                }
            if method == "POST" and path == "/api/v1/auth/device/token":
                device_code = payload.get("deviceCode")
                if not isinstance(device_code, str):
                    raise AccessControlError("INVALID_REQUEST", "deviceCode is required")
                subject = self.store.consume_identity_device_code(device_code)
                if subject is None:
                    return 202, {"status": "authorization_pending"}
                session = self.store.issue_session(subject.id)
                self.store.record_audit(
                    action="identity.session.issue",
                    decision="allowed",
                    auth=AuthContext(subject, "external_identity"),
                    resource="employee-session",
                )
                return 200, session
            if method == "GET" and path == "/api/v1/auth/me":
                auth = self.store.authenticate(authorization)
                return 200, {"subject": auth.subject.as_dict(), "authenticationMethod": auth.method}
            if method == "POST" and path == "/api/v1/auth/logout":
                auth = self.store.authenticate(authorization)
                if auth.method != "oauth_session" or auth.credential_id is None:
                    raise AccessControlError("INVALID_SESSION", "employee session is required", status=409)
                self.store.revoke_session(auth.credential_id)
                self.store.record_audit(
                    action="identity.logout", decision="allowed", auth=auth, resource="employee-session"
                )
                return 200, {"status": "signed_out"}
            return 404, {"code": "NOT_FOUND", "message": "identity endpoint was not found"}
        except AccessControlError as exc:
            return exc.status, {"code": exc.code, "message": exc.safe_message}


__all__ = ["IdentityApi"]
