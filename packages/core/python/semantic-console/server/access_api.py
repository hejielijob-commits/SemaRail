"""Administrative HTTP boundary for SemaRail identities and policies."""

from __future__ import annotations

import re
from typing import Any, Mapping

try:
    from .access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID
    from .authorization import PolicyEngine, PolicyError, validate_policy_document
except ImportError:  # pragma: no cover - direct module loading
    from access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID  # type: ignore[no-redef]
    from authorization import PolicyEngine, PolicyError, validate_policy_document  # type: ignore[no-redef]


class AccessControlAdminApi:
    """Small versioned API; every route requires current ``access:admin``."""

    def __init__(
        self,
        store: AccessControlStore,
        policy_engine: PolicyEngine | None = None,
        *,
        project_id: str | None = None,
    ) -> None:
        self.store = store
        self.policy_engine = policy_engine or PolicyEngine()
        self.project_id = project_id

    def dispatch(self, method: str, path: str, body: Any, authorization: str | None) -> tuple[int, dict[str, Any]] | None:
        if not path.startswith("/api/v1/access/"):
            return None
        try:
            auth = self.store.authenticate(authorization)
            policies = [] if auth.subject.id == BOOTSTRAP_SUBJECT_ID else self.store.policies_for_subject(auth.subject.id)
            decision = self.policy_engine.authorize_scope(
                auth.subject, "access:admin", policies, project_id=self.project_id
            )
            if not decision.allowed:
                self.store.record_audit(action="access.admin", decision="denied", auth=auth, resource=path)
                return 403, {"code": "FORBIDDEN", "message": "administrator permission is required"}
            result = self._admin_dispatch(method, path, body, auth)
            self.store.record_audit(action=f"access.{method.lower()}", decision="allowed", auth=auth, resource=path)
            return result
        except AccessControlError as exc:
            return exc.status, {"code": exc.code, "message": exc.safe_message}
        except PolicyError:
            return 400, {"code": "INVALID_POLICY", "message": "policy document is invalid"}

    def _admin_dispatch(
        self, method: str, path: str, body: Any, auth: AuthContext
    ) -> tuple[int, dict[str, Any]]:
        payload = body if isinstance(body, Mapping) else {}
        organization_scope = None if auth.subject.id == BOOTSTRAP_SUBJECT_ID else auth.subject.organization_id
        if method == "GET" and path == "/api/v1/access/service-accounts":
            return 200, {"items": self._filter_organization(self.store.list_service_accounts(), organization_scope)}
        if method == "GET" and path == "/api/v1/access/users":
            return 200, {"items": self._filter_organization(self.store.list_users(), organization_scope)}
        if method == "POST" and path == "/api/v1/access/service-accounts":
            account = self.store.create_service_account(
                payload.get("name"),
                organization_id=organization_scope or payload.get("organizationId", "default"),
                attributes=payload.get("attributes"),
            )
            return 201, account.as_dict()
        match = re.fullmatch(r"/api/v1/access/service-accounts/([^/]+)", path)
        if method == "PUT" and match:
            self._require_subject_organization(match.group(1), organization_scope, kind="service_account")
            return 200, self.store.update_service_account(
                match.group(1), name=payload.get("name"), attributes=payload.get("attributes")
            ).as_dict()
        match = re.fullmatch(r"/api/v1/access/users/([^/]+)", path)
        if method == "PUT" and match:
            self._require_subject_organization(match.group(1), organization_scope, kind="user")
            return 200, self.store.update_user(
                match.group(1), name=payload.get("name"), attributes=payload.get("attributes")
            ).as_dict()
        match = re.fullmatch(r"/api/v1/access/service-accounts/([^/]+)/keys", path)
        if method == "POST" and match:
            self._require_subject_organization(match.group(1), organization_scope, kind="service_account")
            return 201, self.store.issue_api_key(
                match.group(1), label=payload.get("label", "default"), expires_at=payload.get("expiresAt")
            )
        match = re.fullmatch(r"/api/v1/access/service-accounts/([^/]+)/status", path)
        if method == "PUT" and match:
            self._require_subject_organization(match.group(1), organization_scope, kind="service_account")
            return 200, self.store.set_subject_status(match.group(1), payload.get("status")).as_dict()
        match = re.fullmatch(r"/api/v1/access/users/([^/]+)/status", path)
        if method == "PUT" and match:
            self._require_subject_organization(match.group(1), organization_scope, kind="user")
            return 200, self.store.set_subject_status(match.group(1), payload.get("status")).as_dict()
        match = re.fullmatch(r"/api/v1/access/credentials/([^/]+)/revoke", path)
        if method == "POST" and match:
            self._require_credential_organization(match.group(1), organization_scope)
            return 200, self.store.revoke_credential(match.group(1))
        match = re.fullmatch(r"/api/v1/access/credentials/([^/]+)/rotate", path)
        if method == "POST" and match:
            self._require_credential_organization(match.group(1), organization_scope)
            return 201, self.store.rotate_credential(
                match.group(1), label=payload.get("label"), expires_at=payload.get("expiresAt")
            )
        if method == "GET" and path == "/api/v1/access/policies":
            return 200, {"items": self._filter_organization(self.store.list_policies(), organization_scope)}
        if method == "POST" and path == "/api/v1/access/policies":
            validate_policy_document(payload.get("document"))
            return 201, self.store.create_policy(
                payload.get("name"), payload.get("document"),
                organization_id=organization_scope or payload.get("organizationId", "default")
            )
        match = re.fullmatch(r"/api/v1/access/policies/([^/]+)", path)
        if method == "PUT" and match:
            self._require_policy_organization(match.group(1), organization_scope)
            validate_policy_document(payload.get("document"))
            return 200, self.store.update_policy(match.group(1), payload.get("document"))
        if method == "POST" and path == "/api/v1/access/policy-bindings":
            subject_id = payload.get("subjectId")
            policy_id = payload.get("policyId")
            if not isinstance(subject_id, str) or not isinstance(policy_id, str):
                raise AccessControlError("INVALID_REQUEST", "subjectId and policyId are required")
            self._require_subject_organization(subject_id, organization_scope)
            self._require_policy_organization(policy_id, organization_scope)
            self.store.bind_policy(subject_id, policy_id)
            return 201, {"subjectId": subject_id, "policyId": policy_id}
        match = re.fullmatch(r"/api/v1/access/policy-bindings/([^/]+)/([^/]+)", path)
        if method == "DELETE" and match:
            subject_id, policy_id = match.groups()
            self._require_subject_organization(subject_id, organization_scope)
            self._require_policy_organization(policy_id, organization_scope)
            self.store.unbind_policy(subject_id, policy_id)
            return 200, {"subjectId": subject_id, "policyId": policy_id, "status": "unbound"}
        if method == "GET" and path == "/api/v1/access/audit":
            return 200, {"items": self._filter_organization(self.store.list_audit(), organization_scope)}
        return 404, {"code": "NOT_FOUND", "message": "access-control endpoint was not found"}

    @staticmethod
    def _filter_organization(items: list[dict[str, Any]], organization_id: str | None) -> list[dict[str, Any]]:
        if organization_id is None:
            return items
        return [item for item in items if item.get("organizationId") == organization_id]

    def _require_subject_organization(
        self, subject_id: str, organization_id: str | None, *, kind: str | None = None
    ) -> None:
        subject = self.store.subject(subject_id)
        if (kind is not None and subject.kind != kind) or (
            organization_id is not None and subject.organization_id != organization_id
        ):
            raise AccessControlError("SUBJECT_NOT_FOUND", "subject was not found", status=404)

    def _require_policy_organization(self, policy_id: str, organization_id: str | None) -> None:
        policy = next((item for item in self.store.list_policies() if item["id"] == policy_id), None)
        if policy is None or (organization_id is not None and policy["organizationId"] != organization_id):
            raise AccessControlError("POLICY_NOT_FOUND", "policy was not found", status=404)

    def _require_credential_organization(self, credential_id: str, organization_id: str | None) -> None:
        if organization_id is None:
            return
        found = any(
            credential["id"] == credential_id
            for account in self._filter_organization(self.store.list_service_accounts(), organization_id)
            for credential in account["credentials"]
        )
        if not found:
            raise AccessControlError("CREDENTIAL_NOT_FOUND", "credential was not found", status=404)


__all__ = ["AccessControlAdminApi"]
