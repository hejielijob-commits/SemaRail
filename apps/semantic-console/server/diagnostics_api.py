"""Authenticated diagnostics and feedback HTTP routes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

try:
    from .access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID
    from .authorization import PolicyEngine
    from .diagnostics import DiagnosticError, DiagnosticStore
except ImportError:  # pragma: no cover
    from access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID  # type: ignore[no-redef]
    from authorization import PolicyEngine  # type: ignore[no-redef]
    from diagnostics import DiagnosticError, DiagnosticStore  # type: ignore[no-redef]


_FEEDBACK_DETAIL = re.compile(r"/api/v1/diagnostics/feedback/([^/]+)\Z")
_REGRESSION_CREATE = re.compile(r"/api/v1/diagnostics/feedback/([^/]+)/regression-cases\Z")


class DiagnosticsApi:
    def __init__(
        self,
        store: DiagnosticStore | None,
        access_control: AccessControlStore,
        policy_engine: PolicyEngine,
        *,
        project_id: str,
    ) -> None:
        self.store = store
        self.access_control = access_control
        self.policy_engine = policy_engine
        self.project_id = project_id

    def dispatch(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any],
        body: Any,
        authorization: str | None,
    ) -> tuple[int, dict[str, Any]] | None:
        if path != "/api/v1/feedback" and not path.startswith("/api/v1/diagnostics/"):
            return None
        if self.store is None:
            return 503, {
                "code": "DIAGNOSTIC_STORE_UNAVAILABLE",
                "message": "diagnostic storage is unavailable; retry feedback later with the same idempotency key",
            }
        try:
            auth = self.access_control.authenticate(authorization)
            if method == "POST" and path == "/api/v1/feedback":
                return 201, self._submit(auth, body)
            self._require_admin(auth, path)
            if method == "GET" and path == "/api/v1/diagnostics/feedback":
                return 200, self.store.list_feedback(
                    organization_id=auth.subject.organization_id,
                    project_id=self.project_id,
                    limit=self._limit(query.get("limit", 50)),
                    cursor=self._optional(query.get("cursor")),
                    category=self._optional(query.get("category")),
                    status=self._optional(query.get("status")),
                    source=self._optional(query.get("source")),
                    datasource_id=self._optional(query.get("datasourceId")),
                    reason_code=self._optional(query.get("reasonCode")),
                    created_after=self._optional(query.get("createdAfter")),
                    created_before=self._optional(query.get("createdBefore")),
                )
            detail = _FEEDBACK_DETAIL.fullmatch(path)
            if detail and method == "GET":
                return 200, self.store.feedback_detail(
                    detail.group(1),
                    organization_id=auth.subject.organization_id,
                    project_id=self.project_id,
                )
            if detail and method == "PUT":
                payload = self._body(body, {"status", "category", "duplicateOf", "note"})
                return 200, self.store.update_feedback(
                    detail.group(1),
                    auth=auth,
                    project_id=self.project_id,
                    status=payload.get("status"),
                    category=payload.get("category"),
                    duplicate_of=payload.get("duplicateOf"),
                    note=payload.get("note"),
                )
            regression = _REGRESSION_CREATE.fullmatch(path)
            if regression and method == "POST":
                payload = self._body(body, {"case", "enable"})
                case = payload.get("case")
                if not isinstance(case, Mapping) or type(payload.get("enable", False)) is not bool:
                    raise DiagnosticError("INVALID_REGRESSION_CASE", "regression case is invalid")
                return 201, self.store.create_regression_case(
                    auth=auth,
                    project_id=self.project_id,
                    feedback_id=regression.group(1),
                    case=case,
                    enable=payload.get("enable", False),
                )
            if method == "GET" and path == "/api/v1/diagnostics/regression-cases/export":
                return 200, self.store.export_regression_cases(
                    organization_id=auth.subject.organization_id,
                    project_id=self.project_id,
                )
            return 404, {"code": "NOT_FOUND", "message": "diagnostic endpoint was not found"}
        except (AccessControlError, DiagnosticError) as exc:
            return exc.status, {"code": exc.code, "message": exc.safe_message}

    def _submit(self, auth: AuthContext, body: Any) -> dict[str, Any]:
        payload = self._body(
            body,
            {
                "reference", "idempotencyKey", "category", "description",
                "expectedBehavior", "question", "semanticSql", "nativeSql",
            },
        )
        return self.store.submit_feedback(
            auth=auth,
            project_id=self.project_id,
            reference=payload.get("reference"),
            idempotency_key=payload.get("idempotencyKey"),
            category=payload.get("category"),
            description=payload.get("description"),
            expected_behavior=payload.get("expectedBehavior"),
            question=payload.get("question"),
            semantic_sql=payload.get("semanticSql"),
            native_sql=payload.get("nativeSql"),
        )

    def _require_admin(self, auth: AuthContext, resource: str) -> None:
        policies = (
            [] if auth.subject.id == BOOTSTRAP_SUBJECT_ID
            else self.access_control.policies_for_subject(auth.subject.id)
        )
        decision = self.policy_engine.authorize_scope(
            auth.subject, "console:admin", policies, project_id=self.project_id
        )
        if not decision.allowed:
            raise AccessControlError(
                "FORBIDDEN", "console administrator permission is required", status=403
            )
        # Metadata-only governance event; diagnostic evidence remains solely in
        # the diagnostics tables.
        self.access_control.record_audit(
            action="diagnostics.admin", decision="allowed", auth=auth, resource=resource
        )

    @staticmethod
    def _body(body: Any, allowed: set[str]) -> dict[str, Any]:
        if not isinstance(body, Mapping) or set(body) - allowed:
            raise DiagnosticError("INVALID_REQUEST", "request body is invalid")
        return dict(body)

    @staticmethod
    def _limit(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise DiagnosticError("INVALID_FILTER", "limit is invalid") from exc
        if not 1 <= parsed <= 100:
            raise DiagnosticError("INVALID_FILTER", "limit is invalid")
        return parsed

    @staticmethod
    def _optional(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None


__all__ = ["DiagnosticsApi"]
