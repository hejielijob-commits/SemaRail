"""Stable loopback RPC boundary exposed by the standalone SemaRail Core.

The public request never accepts a project path, database credentials, or
execution limits.  Those deployment decisions stay inside Core while thin
agent integrations reuse the existing version-one sidecar response envelope.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

try:
    from .access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID
    from .authorization import MissingSubjectAttribute, PolicyDecision, PolicyEngine, scope_for_method
    from .artifact_store import (
        MAX_ARTIFACT_INLINE_BYTES,
        MAX_ARTIFACT_INLINE_ROWS,
        MAX_ARTIFACT_PREVIEW_ROWS,
        MAX_ARTIFACT_ROWS,
        ArtifactDownload,
        ArtifactError,
        ArtifactMetadata,
        ArtifactReservation,
        ArtifactStore,
    )
    from .diagnostics import DiagnosticError, DiagnosticStore
    from .project import ProjectStore
    from .query_preparation import PreparationError, QueryPreparationStore
except ImportError:  # pragma: no cover - direct module loading
    from access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID  # type: ignore[no-redef]
    from authorization import MissingSubjectAttribute, PolicyDecision, PolicyEngine, scope_for_method  # type: ignore[no-redef]
    from artifact_store import (  # type: ignore[no-redef]
        MAX_ARTIFACT_INLINE_BYTES,
        MAX_ARTIFACT_INLINE_ROWS,
        MAX_ARTIFACT_PREVIEW_ROWS,
        MAX_ARTIFACT_ROWS,
        ArtifactDownload,
        ArtifactError,
        ArtifactMetadata,
        ArtifactReservation,
        ArtifactStore,
    )
    from diagnostics import DiagnosticError, DiagnosticStore  # type: ignore[no-redef]
    from project import ProjectStore  # type: ignore[no-redef]
    from query_preparation import PreparationError, QueryPreparationStore  # type: ignore[no-redef]


CORE_API_VERSION = "1"
CORE_PROTOCOL_VERSION = "2"
LEGACY_CORE_PROTOCOL_VERSION = "1"
SIDECAR_PROTOCOL_VERSION = "2"
MAX_QUERY_ROWS = 500
MAX_PREVIEW_ROWS = 200
MAX_PREVIEW_BYTES = 1_048_576
MAX_TIMEOUT_MS = 30_000
_PUBLIC_METHODS = frozenset(
    {"health", "project.validate", "project.describe", "context.ask", "query.prepare", "query.dryPlan", "query.run", "query.cancel"}
)
_DATA_POLICY_METHODS = frozenset({"project.describe", "context.ask", "query.prepare", "query.dryPlan", "query.run"})
_DIAGNOSTIC_METHODS = frozenset({"context.ask", "query.dryPlan", "query.run"})
_REQUEST_FIELDS = frozenset({"protocolVersion", "id", "method", "params", "deadlineMs"})
_LOGGER = logging.getLogger("semarail-core.runtime")
DEFAULT_ARTIFACT_BASE_URL = "http://127.0.0.1:48763"


class RuntimeDispatcher(Protocol):
    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


def _error(
    request_id: str,
    code: str,
    message: str,
    *,
    protocol_version: str = LEGACY_CORE_PROTOCOL_VERSION,
    trace_id: str = "",
    reason_code: str = "INTERNAL_FAILURE",
    resources: list[dict[str, str]] | None = None,
    required_permissions: list[str] | None = None,
    suggestion: str = "Contact the SemaRail administrator if the problem persists.",
    origin: str = "core",
    phase: str = "protocol",
    retryable: bool = False,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "phase": phase,
        "message": message,
        "retryable": retryable,
    }
    if protocol_version == CORE_PROTOCOL_VERSION:
        error.update({
            "reasonCode": reason_code,
            "resources": resources or [],
            "requiredPermissions": required_permissions or [],
            "suggestion": suggestion,
            "origin": origin,
            "traceId": trace_id,
        })
    return {
        "protocolVersion": protocol_version,
        "id": request_id,
        "ok": False,
        "error": error,
    }


def _reason_for_error(code: str, phase: str) -> tuple[str, str, str, bool]:
    """Map a legacy Sidecar failure to deterministic v2 diagnostic fields."""

    if code == "POLICY_DENIED":
        return "EXPLICIT_DENIAL", "semarail-policy", "Adjust the requested scope or ask an administrator to update the applicable policy.", False
    if code == "DATABASE_ERROR":
        return "INTERNAL_FAILURE", "database", "Use the trace identifier to inspect the server-side database failure.", False
    if code == "SEMANTIC_ERROR":
        return "SEMANTIC_PARSE_FAILED", "semantic-runtime", "Revise the semantic SQL or inspect the published semantic model.", False
    if code == "TIMEOUT":
        return "QUERY_TIMEOUT", "database", "Reduce the query scope or retry after checking datasource health.", True
    if code in {"SIDECAR_UNAVAILABLE", "WREN_UNAVAILABLE"}:
        return "CONNECTION_FAILED", "transport", "Check SemaRail runtime health and retry.", True
    if code in {"UNSUPPORTED_PROTOCOL", "UNSUPPORTED_VERSION"}:
        return "INTERNAL_FAILURE", "transport", "Upgrade the client to a supported SemaRail protocol version.", False
    if phase in {"authorization", "policy"}:
        return "EXPLICIT_DENIAL", "semarail-policy", "Ask an administrator to review the applicable access policy.", False
    return "INTERNAL_FAILURE", "core", "Contact the SemaRail administrator if the problem persists.", False


def _request_id(value: Any) -> str:
    if isinstance(value, Mapping) and isinstance(value.get("id"), str):
        return str(value["id"])[:128]
    return ""


def _public_response(response: Mapping[str, Any], protocol_version: str, trace_id: str) -> dict[str, Any]:
    """Project a legacy Sidecar envelope into the requested public version."""

    projected = dict(response)
    projected["protocolVersion"] = protocol_version
    if protocol_version != CORE_PROTOCOL_VERSION or projected.get("ok") is True:
        return projected
    raw_error = projected.get("error")
    if not isinstance(raw_error, Mapping):
        return _error(
            str(projected.get("id") or ""),
            "INTERNAL_ERROR",
            "SemaRail runtime returned an invalid error",
            protocol_version=protocol_version,
            trace_id=trace_id,
        )
    code = str(raw_error.get("code") or "INTERNAL_ERROR")
    phase = str(raw_error.get("phase") or "dispatch")[:64]
    reason, origin, suggestion, default_retryable = _reason_for_error(code, phase)
    allowed_reasons = {
        "AUTHENTICATION_EXPIRED", "ACCOUNT_DISABLED", "PROJECT_PERMISSION_REQUIRED",
        "DATASOURCE_PERMISSION_REQUIRED", "TOOL_PERMISSION_REQUIRED", "TABLE_PERMISSION_REQUIRED",
        "COLUMN_PERMISSION_REQUIRED", "EXPLICIT_DENIAL", "UNAUTHORIZED", "ROW_ATTRIBUTE_MISSING",
        "DATABASE_PERMISSION_REQUIRED", "SQL_SAFETY_RESTRICTION", "SEMANTIC_PARSE_FAILED",
        "QUERY_TIMEOUT", "CONNECTION_FAILED", "UNSUPPORTED_DATASOURCE", "INTERNAL_FAILURE",
        "CLARIFICATION_REQUIRED",
    }
    allowed_origins = {
        "authentication", "semarail-policy", "query-safety", "semantic-runtime",
        "database", "transport", "core",
    }
    raw_reason = raw_error.get("reasonCode")
    if isinstance(raw_reason, str) and raw_reason in allowed_reasons:
        reason = raw_reason
    raw_origin = raw_error.get("origin")
    if isinstance(raw_origin, str) and raw_origin in allowed_origins:
        origin = raw_origin
    raw_suggestion = raw_error.get("suggestion")
    if isinstance(raw_suggestion, str) and 1 <= len(raw_suggestion) <= 2_000:
        suggestion = raw_suggestion
    resources: list[dict[str, str]] = []
    raw_resources = raw_error.get("resources")
    if isinstance(raw_resources, list) and len(raw_resources) <= 32:
        for item in raw_resources:
            if not isinstance(item, Mapping):
                resources = []
                break
            kind, name = item.get("kind"), item.get("name")
            if kind not in {"project", "datasource", "tool", "table", "column", "attribute"} or not isinstance(name, str) or not 1 <= len(name) <= 512:
                resources = []
                break
            resources.append({"kind": kind, "name": name})
    permissions: list[str] = []
    raw_permissions = raw_error.get("requiredPermissions")
    if isinstance(raw_permissions, list) and len(raw_permissions) <= 32 and all(
        isinstance(item, str) and 1 <= len(item) <= 256 for item in raw_permissions
    ):
        permissions = list(raw_permissions)
    return _error(
        str(projected.get("id") or ""),
        code,
        str(raw_error.get("message") or "SemaRail request failed")[:4_000],
        protocol_version=protocol_version,
        trace_id=trace_id,
        reason_code=reason,
        resources=resources,
        required_permissions=permissions,
        suggestion=suggestion,
        origin=origin,
        phase=phase,
        retryable=bool(raw_error.get("retryable", default_retryable)),
    )


def _default_dispatcher(project: ProjectStore) -> RuntimeDispatcher | None:
    try:
        from sidecar import Dispatcher, default_dependencies  # type: ignore[import-not-found]
        from sidecar.datasource_state import load_active_connection  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError):
        return None

    canonical_project = str(project.project_dir)
    state_file = project.datasource_state_file

    def resolve_connection(_project_dir: str, env_name: str) -> Mapping[str, Any] | None:
        return load_active_connection(canonical_project, env_name, state_file=state_file)

    return Dispatcher(default_dependencies(connection_resolver=resolve_connection))


class RuntimeRpcGateway:
    """Validate and pin public Core RPC requests before sidecar dispatch."""

    def __init__(
        self,
        project: ProjectStore,
        dispatcher: RuntimeDispatcher | None = None,
        *,
        auth_token: str | None = None,
        access_control: AccessControlStore | None = None,
        policy_engine: PolicyEngine | None = None,
        artifact_store: ArtifactStore | None = None,
        diagnostic_store: DiagnosticStore | None = None,
        preparation_store: QueryPreparationStore | None = None,
        artifact_base_url: str | None = None,
        artifact_ttl_seconds: int | None = None,
    ) -> None:
        self.project = project
        self.dispatcher = dispatcher if dispatcher is not None else _default_dispatcher(project)
        configured = auth_token if auth_token is not None else os.environ.get("SEMARAIL_API_TOKEN", "")
        self.auth_token = configured.strip()
        self.access_control = access_control or AccessControlStore.from_config(
            self.project.state_dir / "access-control.sqlite3",
            bootstrap_token=self.auth_token,
        )
        self.policy_engine = policy_engine or PolicyEngine()
        self.preparations = preparation_store or QueryPreparationStore(self.access_control, self.project)
        if diagnostic_store is not None:
            self.diagnostics: DiagnosticStore | None = diagnostic_store
        else:
            try:
                self.diagnostics = DiagnosticStore(self.access_control)
            except DiagnosticError:
                # Query execution must remain available if optional diagnostic
                # persistence is temporarily unavailable. Explicit feedback
                # endpoints report this state to their caller.
                _LOGGER.error("diagnostic storage initialization failed")
                self.diagnostics = None
        artifact_clock = getattr(self.access_control, "clock", None)
        configured_artifact_ttl = self._artifact_ttl_seconds(artifact_ttl_seconds)
        if artifact_store is not None:
            self.artifacts = artifact_store
        elif callable(artifact_clock):
            self.artifacts = ArtifactStore(
                self.project.state_dir,
                access_control=self.access_control,
                clock=artifact_clock,
                ttl_seconds=configured_artifact_ttl,
            )
        else:
            self.artifacts = ArtifactStore(
                self.project.state_dir,
                access_control=self.access_control,
                ttl_seconds=configured_artifact_ttl,
            )
        self._artifact_base_url_explicit = artifact_base_url is not None
        self._artifact_base_url = self._validate_artifact_base_url(
            artifact_base_url if artifact_base_url is not None else DEFAULT_ARTIFACT_BASE_URL
        )

    @staticmethod
    def _artifact_ttl_seconds(configured: int | None) -> int:
        """Resolve the server-owned TTL; public query parameters cannot set it."""

        from_value: int | str = (
            configured
            if configured is not None
            else os.environ.get("SEMARAIL_ARTIFACT_TTL_SECONDS", str(15 * 60))
        )
        try:
            value = int(from_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("artifact TTL configuration is invalid") from exc
        if isinstance(from_value, bool) or not 60 <= value <= 24 * 60 * 60:
            raise ValueError("artifact TTL configuration is invalid")
        return value

    @staticmethod
    def _validate_artifact_base_url(value: str) -> str:
        """Validate a trusted deployment URL used for generated downloads."""

        if not isinstance(value, str) or not value or any(ord(char) < 0x20 for char in value):
            raise ValueError("artifact base URL is invalid")
        try:
            parsed = urlsplit(value)
            # Accessing .port validates malformed numeric ports as well.
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("artifact base URL is invalid") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("artifact base URL is invalid")
        return value.rstrip("/")

    @property
    def artifact_base_url(self) -> str:
        """Trusted base authority used in public artifact descriptors."""

        return self._artifact_base_url

    @property
    def artifact_base_url_explicit(self) -> bool:
        """Whether deployment configuration supplied the base URL."""

        return self._artifact_base_url_explicit

    def set_artifact_base_url(self, value: str) -> None:
        """Set a server-known base URL; never derive it from request headers."""

        if self._artifact_base_url_explicit:
            return
        self._artifact_base_url = self._validate_artifact_base_url(value)

    def submit_feedback(
        self, body: Any, authorization: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Submit caller-owned feedback without exposing diagnostic reads."""

        if self.diagnostics is None:
            return 503, {
                "code": "DIAGNOSTIC_STORE_UNAVAILABLE",
                "message": "diagnostic storage is unavailable; retry with the same idempotency key",
            }
        allowed = {
            "reference", "idempotencyKey", "category", "description",
            "expectedBehavior", "question", "semanticSql", "nativeSql",
        }
        try:
            auth = self.access_control.authenticate(authorization)
            if not isinstance(body, Mapping) or set(body) - allowed:
                raise DiagnosticError("INVALID_REQUEST", "feedback request is invalid")
            result = self.diagnostics.submit_feedback(
                auth=auth,
                project_id=str(self.project.overview().get("name") or ""),
                reference=body.get("reference"),
                idempotency_key=body.get("idempotencyKey"),
                category=body.get("category"),
                description=body.get("description"),
                expected_behavior=body.get("expectedBehavior"),
                question=body.get("question"),
                semantic_sql=body.get("semanticSql"),
                native_sql=body.get("nativeSql"),
            )
            return 201, result
        except (AccessControlError, DiagnosticError) as exc:
            return exc.status, {"code": exc.code, "message": exc.safe_message}

    def dispatch(
        self,
        body: Any,
        authorization: str | None = None,
        *,
        transport: str = "runtime-rpc",
        artifact_base_url: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Dispatch one authenticated request and emit a metadata-only audit event.

        ``transport`` is trusted server context, not a public request field. It
        lets entry points distinguish remote MCP from the ordinary Core RPC
        boundary without copying bearer tokens or request content into audit.
        """

        request_id = _request_id(body)
        trace_id = f"trace-{uuid.uuid4().hex}"
        started_at = time.monotonic()
        raw_protocol = body.get("protocolVersion") if isinstance(body, Mapping) else None
        protocol_version = raw_protocol if raw_protocol in {LEGACY_CORE_PROTOCOL_VERSION, CORE_PROTOCOL_VERSION} else CORE_PROTOCOL_VERSION

        auth: AuthContext | None = None
        diagnostic_recorded = False
        diagnostic_policy_versions: tuple[str, ...] = ()
        original_query_id: str | None = None

        def capture(response: Mapping[str, Any], status: str) -> None:
            """Best-effort evidence capture that never changes query results."""

            nonlocal diagnostic_recorded
            if diagnostic_recorded or auth is None or self.diagnostics is None or not isinstance(body, Mapping):
                return
            method_value = body.get("method")
            if method_value not in _DIAGNOSTIC_METHODS:
                return
            params_value = body.get("params")
            safe_params = params_value if isinstance(params_value, Mapping) else {}
            error_value = response.get("error")
            result_value = response.get("result")
            result = result_value if isinstance(result_value, Mapping) else {}
            result_error = result.get("error")
            error = (
                dict(error_value) if isinstance(error_value, Mapping)
                else dict(result_error) if isinstance(result_error, Mapping) else None
            )
            try:
                self.diagnostics.record_execution(
                    auth=auth,
                    project_id=str(self.project.overview().get("name") or ""),
                    trace_id=trace_id,
                    query_id=(str(safe_params.get("queryId")) if isinstance(safe_params.get("queryId"), str) else None),
                    original_query_id=original_query_id,
                    datasource_id=(self.project.active_datasource_identifier() if method_value in _DATA_POLICY_METHODS else None),
                    transport=transport,
                    method=str(method_value),
                    status=status,
                    stage=str(error.get("phase") if error else "complete"),
                    semantic_version=str(self.project.overview().get("revision") or "") or None,
                    policy_versions=diagnostic_policy_versions,
                    error=error,
                    question=(str(safe_params.get("question")) if isinstance(safe_params.get("question"), str) else None),
                    semantic_sql=(str(safe_params.get("semanticSql")) if isinstance(safe_params.get("semanticSql"), str) else None),
                    native_sql=(str(result.get("nativeSql")) if isinstance(result.get("nativeSql"), str) else None),
                    duration_ms=max(0.0, (time.monotonic() - started_at) * 1000.0),
                )
                diagnostic_recorded = True
                self.diagnostics.cleanup_expired()
            except (DiagnosticError, AccessControlError, OSError, RuntimeError):
                _LOGGER.error("diagnostic write failed for trace %s", trace_id)

        def fail(code: str, message: str, **details: Any) -> dict[str, Any]:
            wire_code = "FORBIDDEN" if protocol_version == LEGACY_CORE_PROTOCOL_VERSION and code == "POLICY_DENIED" else code
            response = _error(
                request_id,
                wire_code,
                message,
                protocol_version=protocol_version,
                trace_id=trace_id,
                **details,
            )
            capture(response, "failure")
            return response

        try:
            auth = self.access_control.authenticate(authorization)
        except AccessControlError as exc:
            if self.diagnostics is not None:
                raw_method = body.get("method") if isinstance(body, Mapping) else None
                try:
                    self.diagnostics.record_security_event(
                        trace_id=trace_id,
                        transport=transport,
                        method=(raw_method if isinstance(raw_method, str) and len(raw_method) <= 64 else None),
                    )
                except (DiagnosticError, AccessControlError, OSError, RuntimeError):
                    _LOGGER.error("diagnostic security-event write failed for trace %s", trace_id)
            reason_code = "ACCOUNT_DISABLED" if exc.code in {"ACCOUNT_DISABLED", "SUBJECT_DISABLED"} else "AUTHENTICATION_EXPIRED"
            return exc.status, fail(
                exc.code,
                exc.safe_message,
                reason_code=reason_code,
                origin="authentication",
                suggestion="Sign in again or ask an administrator to reactivate the account.",
                phase="authentication",
            )
        if not isinstance(body, Mapping):
            return 400, fail("INVALID_REQUEST", "request must be a JSON object")
        if set(body) - _REQUEST_FIELDS:
            return 400, fail("INVALID_REQUEST", "request contains unknown fields")
        if body.get("protocolVersion") not in {LEGACY_CORE_PROTOCOL_VERSION, CORE_PROTOCOL_VERSION}:
            return 400, fail(
                "UNSUPPORTED_PROTOCOL",
                "protocolVersion is unsupported",
                origin="transport",
                suggestion="Upgrade the client to a supported SemaRail protocol version.",
            )
        if not isinstance(body.get("id"), str) or not 1 <= len(str(body.get("id"))) <= 128:
            return 400, {**fail("INVALID_REQUEST", "id is invalid"), "id": ""}
        method = body.get("method")
        if method not in _PUBLIC_METHODS:
            return 400, fail("METHOD_NOT_FOUND", "method is not supported")
        params = body.get("params")
        if not isinstance(params, Mapping):
            return 400, fail("INVALID_PARAMS", "params must be an object")
        # The bootstrap credential is an administration/recovery credential,
        # never an Agent data credential. Console management routes authenticate
        # it independently; the runtime boundary requires a revocable managed
        # service key or employee session.
        if auth.subject.id == BOOTSTRAP_SUBJECT_ID and method != "health":
            decision = PolicyDecision(False, "bootstrap credential is not accepted by the agent runtime")
            self._audit(
                auth, str(method), "denied", request_id, decision,
                transport=transport,
                query_id=params.get("queryId") if method in {"query.run", "query.cancel"} else None,
            )
            return 403, fail(
                "POLICY_DENIED",
                (
                    "managed Agent credentials are required"
                    if protocol_version == LEGACY_CORE_PROTOCOL_VERSION
                    else "The bootstrap credential cannot execute Agent requests."
                ),
                reason_code="TOOL_PERMISSION_REQUIRED",
                resources=[{"kind": "tool", "name": str(method)}],
                required_permissions=[str(method)],
                suggestion="Use a managed service-account credential or employee session.",
                origin="semarail-policy",
                phase="authorization",
            )
        policies = (
            [] if auth.subject.id == BOOTSTRAP_SUBJECT_ID
            else self.access_control.policies_for_subject(auth.subject.id)
        )
        diagnostic_policy_versions = tuple(
            f"{item.get('id')}:{item.get('version')}"
            for item in policies
            if isinstance(item, Mapping) and item.get("id") is not None and item.get("version") is not None
        )
        project_id = str(self.project.overview().get("name") or "")
        raw_retry_reference = params.get("retryOfQueryId") if method == "query.run" else None
        if raw_retry_reference is not None and protocol_version == LEGACY_CORE_PROTOCOL_VERSION:
            return 400, fail("INVALID_PARAMS", "query.run contains unsupported fields", phase="validation")
        if raw_retry_reference is not None:
            if (
                not isinstance(raw_retry_reference, str)
                or not raw_retry_reference.strip()
                or len(raw_retry_reference) > 128
                or raw_retry_reference == params.get("queryId")
            ):
                return 400, fail("INVALID_PARAMS", "retryOfQueryId is invalid", phase="validation")
            if self.diagnostics is None:
                return 503, fail(
                    "DIAGNOSTIC_STORE_UNAVAILABLE",
                    "retry linkage is unavailable",
                    phase="diagnostics",
                    retryable=True,
                )
            try:
                original_query_id = self.diagnostics.resolve_owned_retry_reference(
                    auth=auth,
                    project_id=project_id,
                    reference=raw_retry_reference,
                )
            except DiagnosticError as exc:
                return exc.status, fail(exc.code, exc.safe_message, phase="diagnostics")
        # Data-facing authorization is bound to the server-known active
        # datasource; callers cannot select or spoof a source in the public
        # RPC payload.
        datasource_id = self.project.active_datasource_identifier() if method in _DATA_POLICY_METHODS else None
        if method in _DATA_POLICY_METHODS and datasource_id is None:
            decision = PolicyDecision(False, "active datasource binding is required")
        else:
            decision = self.policy_engine.authorize_method(
                auth.subject, str(method), policies, project_id=project_id, datasource_id=datasource_id
            )
        if not decision.allowed:
            self._audit(
                auth, str(method), "denied", request_id, decision,
                transport=transport, datasource_id=datasource_id,
                query_id=params.get("queryId") if method in {"query.run", "query.cancel"} else None,
            )
            denial_reason = decision.reason.lower()
            if "explicitly denied" in denial_reason:
                resource_kind, resource_name = "tool", str(method)
                reason_code = "EXPLICIT_DENIAL"
                required_permission = scope_for_method(str(method))
            elif "project" in denial_reason:
                resource_kind, resource_name = "project", project_id
                reason_code = "PROJECT_PERMISSION_REQUIRED"
                required_permission = "project:access"
            elif "datasource" in denial_reason:
                resource_kind, resource_name = "datasource", str(datasource_id or "")
                reason_code = "DATASOURCE_PERMISSION_REQUIRED"
                required_permission = "datasource:access"
            else:
                resource_kind, resource_name = "tool", str(method)
                reason_code = "TOOL_PERMISSION_REQUIRED"
                required_permission = scope_for_method(str(method))
            return 403, fail(
                "POLICY_DENIED",
                f'Cannot perform {method}: permission for {resource_kind} "{resource_name}" is required.',
                reason_code=reason_code,
                resources=[{"kind": resource_kind, "name": resource_name}],
                required_permissions=[required_permission],
                suggestion="Ask a project administrator to update the applicable access policy.",
                origin="semarail-policy",
                phase="authorization",
            )
        normalized = self._normalize(method, params, decision, protocol_version=protocol_version)
        if isinstance(normalized, str):
            self._audit(
                auth, str(method), "denied", request_id, decision,
                transport=transport, datasource_id=datasource_id,
                query_id=params.get("queryId") if method in {"query.run", "query.cancel"} else None,
            )
            return 400, fail("INVALID_PARAMS", normalized, phase="validation")
        compiled_policy: Mapping[str, Any] | None = None
        if method in _DATA_POLICY_METHODS:
            try:
                compiled_policy = self.policy_engine.compile_data_policy(
                    auth.subject, policies, project_id=project_id, datasource_id=datasource_id
                )
                normalized["authorizationPolicy"] = compiled_policy
            except MissingSubjectAttribute as exc:
                self._audit(
                    auth, str(method), "denied", request_id, decision,
                    transport=transport, datasource_id=datasource_id,
                    query_id=normalized.get("queryId") if method in {"query.run", "query.cancel"} else None,
                )
                return 403, fail(
                    "POLICY_DENIED",
                    f'The trusted subject attribute "{exc.name}" required by row access policy is missing.',
                    reason_code="ROW_ATTRIBUTE_MISSING",
                    resources=[{"kind": "attribute", "name": exc.name}],
                    required_permissions=[f"subject.attribute:{exc.name}"],
                    suggestion="Ask an administrator to set the required trusted identity attribute.",
                    origin="semarail-policy",
                    phase="authorization",
                )
            except Exception:
                self._audit(
                    auth, str(method), "denied", request_id, decision,
                    transport=transport, datasource_id=datasource_id,
                    query_id=normalized.get("queryId") if method in {"query.run", "query.cancel"} else None,
                )
                return 403, fail(
                    "POLICY_DENIED",
                    "The applicable data access policy is invalid.",
                    reason_code="EXPLICIT_DENIAL",
                    resources=([{"kind": "datasource", "name": datasource_id}] if datasource_id else []),
                    suggestion="Ask a project administrator to repair and republish the data access policy.",
                    origin="semarail-policy",
                    phase="authorization",
                )
        if method == "query.prepare":
            try:
                prepared = self.preparations.prepare(
                    auth=auth,
                    project_id=project_id,
                    question=str(normalized["question"]),
                    semantic_sql=str(normalized["semanticSql"]),
                    conditions=normalized["conditions"],
                    confirmed_conditions=normalized["confirmedConditions"],
                )
            except PreparationError as exc:
                return exc.status, fail(exc.code, exc.safe_message, phase="preparation")
            self._audit(
                auth, str(method), "allowed", request_id, decision,
                transport=transport, datasource_id=datasource_id, compiled_policy=compiled_policy,
            )
            return 200, {
                "protocolVersion": protocol_version,
                "id": request_id,
                "ok": True,
                "result": prepared,
            }
        artifact_reservation: ArtifactReservation | None = None
        if method == "query.run":
            normalized.pop("retryOfQueryId", None)
            try:
                self.preparations.require_ready(
                    auth=auth,
                    project_id=project_id,
                    semantic_sql=str(normalized["semanticSql"]),
                    preparation_id=(str(normalized["preparationId"]) if "preparationId" in normalized else None),
                )
            except PreparationError as exc:
                self._audit(
                    auth, str(method), "denied", request_id, decision,
                    transport=transport, datasource_id=datasource_id,
                    query_id=normalized.get("queryId"), compiled_policy=compiled_policy,
                )
                return exc.status, fail(
                    exc.code,
                    exc.safe_message,
                    reason_code="CLARIFICATION_REQUIRED",
                    resources=[{"kind": "project", "name": project_id}],
                    suggestion="Call semarail_prepare_query, ask the returned questions, then execute with its ready preparationId.",
                    origin="core",
                    phase="preparation",
                )
            normalized.pop("preparationId", None)
            # Core owns the artifact identity, expiry, filename, and token.
            # The request field below is trusted in-process metadata; it is
            # injected after public params have been normalized, so a caller
            # cannot select a path, filename, or limit.
            raw_versions = compiled_policy.get("policyVersions") if compiled_policy else None
            if isinstance(raw_versions, list) and all(isinstance(item, str) for item in raw_versions):
                artifact_versions = tuple(raw_versions[:64])
            else:
                artifact_versions = tuple(decision.policy_versions)
            if not isinstance(datasource_id, str) or not auth.credential_id:
                self._audit(
                    auth,
                    str(method),
                    "denied",
                    request_id,
                    decision,
                    transport=transport,
                    datasource_id=datasource_id,
                )
                return 403, fail(
                    "POLICY_DENIED",
                    "Artifact identity binding is required.",
                    reason_code="UNAUTHORIZED",
                    suggestion="Use an authenticated managed credential bound to the active datasource.",
                    origin="semarail-policy",
                    phase="authorization",
                )
            try:
                artifact_reservation = self.artifacts.reserve(
                    subject_id=auth.subject.id,
                    organization_id=auth.subject.organization_id,
                    credential_id=auth.credential_id,
                    query_id=str(normalized["queryId"]),
                    datasource_id=datasource_id,
                    policy_versions=artifact_versions,
                )
                normalized["artifactRequest"] = self.artifacts.request_for_sidecar(artifact_reservation)
            except ArtifactError:
                self._audit(
                    auth,
                    str(method),
                    "error",
                    request_id,
                    decision,
                    transport=transport,
                    datasource_id=datasource_id,
                    query_id=normalized.get("queryId"),
                )
                return 503, fail(
                    "INTERNAL_ERROR",
                    "Artifact service is unavailable.",
                    suggestion="Retry after the SemaRail artifact service is restored.",
                    retryable=True,
                )
        if self.dispatcher is None:
            if artifact_reservation is not None:
                self.artifacts.fail(artifact_reservation)
            self._audit(
                auth,
                str(method),
                "error",
                request_id,
                decision,
                transport=transport,
                datasource_id=datasource_id,
                query_id=normalized.get("queryId") if method in {"query.run", "query.cancel"} else None,
                compiled_policy=compiled_policy,
            )
            return 503, fail(
                "WREN_UNAVAILABLE",
                "SemaRail runtime is unavailable",
                reason_code="CONNECTION_FAILED",
                suggestion="Check SemaRail runtime health and retry.",
                origin="transport",
                retryable=True,
            )
        internal = {
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "params": normalized,
            "traceId": trace_id,
            **({"deadlineMs": body["deadlineMs"]} if type(body.get("deadlineMs")) is int else {}),
        }
        try:
            response = self.dispatcher.dispatch(internal)
        except Exception:
            if artifact_reservation is not None:
                self.artifacts.fail(artifact_reservation)
            self._audit(
                auth,
                str(method),
                "error",
                request_id,
                decision,
                transport=transport,
                datasource_id=datasource_id,
                query_id=normalized.get("queryId") if method in {"query.run", "query.cancel"} else None,
                compiled_policy=compiled_policy,
            )
            return 503, fail(
                "WREN_UNAVAILABLE",
                "SemaRail runtime is unavailable",
                reason_code="CONNECTION_FAILED",
                suggestion="Check SemaRail runtime health and retry.",
                origin="transport",
                retryable=True,
            )
        if artifact_reservation is not None:
            response = self._integrate_artifact_response(
                response,
                artifact_reservation,
                artifact_base_url=artifact_base_url,
            )
        if method == "health" and response.get("ok") is True and isinstance(response.get("result"), Mapping):
            overview = self.project.overview()
            active = overview.get("activeDatasource")
            datasource_type = active.get("type") if isinstance(active, Mapping) else None
            legacy_postgres_ready = datasource_type is None and bool(os.environ.get("SEMARAIL_DATABASE_URL", "").strip())
            query_ready = datasource_type == "postgres" or legacy_postgres_ready
            response = {
                **response,
                "result": {
                    **response["result"],
                    "service": "semarail-core",
                    "apiVersion": CORE_API_VERSION,
                    "protocolVersion": protocol_version,
                    "capabilities": {
                        "semanticContext": True,
                        "governedQuery": query_ready,
                        "queryCancellation": True,
                    },
                    "readiness": {
                        "semanticContext": "ready",
                        "governedQuery": (
                            "ready" if query_ready else "setup_required" if datasource_type is None else "unsupported"
                        ),
                        "datasourceType": datasource_type,
                    },
                },
            }
        self._audit(
            auth,
            str(method),
            "allowed" if response.get("ok") is True else "error",
            request_id,
            decision,
            transport=transport,
            datasource_id=datasource_id,
            query_id=normalized.get("queryId") if method in {"query.run", "query.cancel"} else None,
            compiled_policy=compiled_policy,
        )
        public_response = _public_response(response, protocol_version, trace_id)
        public_result = public_response.get("result")
        result_failed = isinstance(public_result, Mapping) and public_result.get("status") == "error"
        capture(public_response, "success" if public_response.get("ok") is True and not result_failed else "failure")
        return 200, public_response

    def _integrate_artifact_response(
        self,
        response: Any,
        reservation: ArtifactReservation,
        *,
        artifact_base_url: str | None = None,
    ) -> dict[str, Any]:
        """Merge only safe sidecar artifact metadata into a query result.

        The sidecar response is untrusted at this boundary.  It may describe
        the bytes it wrote, but it may not choose Core's path, token, or URL.
        If a legacy dispatcher returns no artifact descriptor, the reservation
        is simply failed and the ordinary bounded query response is preserved.
        """

        if not isinstance(response, Mapping):
            self.artifacts.fail(reservation)
            return _error("", "WREN_UNAVAILABLE", "SemaRail runtime returned an invalid response")
        if response.get("ok") is not True:
            self.artifacts.fail(reservation)
            return dict(response)
        result = response.get("result")
        if not isinstance(result, Mapping):
            self.artifacts.fail(reservation)
            return dict(response)
        raw_artifact = result.get("artifact")
        status = result.get("status")
        preview_rows = result.get("previewRows")
        stats = result.get("stats")
        if status not in {"success", "error"} or not isinstance(preview_rows, list) or not isinstance(stats, Mapping):
            self.artifacts.fail(reservation)
            return _error(
                str(response.get("id") or ""),
                "ARTIFACT_INVALID_RESULT",
                "sidecar query result is invalid",
            )
        returned_rows = stats.get("returnedRows")
        if (
            type(returned_rows) is not int
            or not 0 <= returned_rows <= MAX_ARTIFACT_ROWS
            or len(preview_rows) > returned_rows
        ):
            self.artifacts.fail(reservation)
            return _error(
                str(response.get("id") or ""),
                "ARTIFACT_INVALID_RESULT",
                "sidecar query result is invalid",
            )
        public_stats = {**dict(stats), "previewedRows": len(preview_rows)}
        safe_result = {
            key: value
            for key, value in result.items()
            if key not in {"artifact", "delivery"}
        }
        safe_result["schemaVersion"] = 2
        safe_result["stats"] = public_stats

        if status == "error":
            self.artifacts.fail(reservation)
            safe_result.pop("chart", None)
            return {**dict(response), "result": safe_result}

        if raw_artifact is None:
            self.artifacts.fail(reservation)
            try:
                preview_size = len(
                    json.dumps(preview_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
            except (TypeError, ValueError):
                preview_size = MAX_ARTIFACT_INLINE_BYTES + 1
            if len(preview_rows) > MAX_ARTIFACT_INLINE_ROWS or preview_size > MAX_ARTIFACT_INLINE_BYTES:
                return _error(
                    str(response.get("id") or ""),
                    "ARTIFACT_INVALID_RESULT",
                    "large query result did not produce an artifact",
                )
            safe_result["delivery"] = "inline"
            return {**dict(response), "result": safe_result}

        if len(preview_rows) > MAX_ARTIFACT_PREVIEW_ROWS or "chart" in result:
            self.artifacts.fail(reservation)
            return _error(
                str(response.get("id") or ""),
                "ARTIFACT_INVALID_RESULT",
                "sidecar artifact presentation is invalid",
            )
        try:
            metadata = self.artifacts.register_sidecar_result(reservation, raw_artifact)
        except ArtifactError:
            self.artifacts.fail(reservation)
            return _error(
                str(response.get("id") or ""),
                "ARTIFACT_INVALID_RESULT",
                "sidecar artifact metadata is invalid",
            )
        public = self.artifacts.reservation_public(
            reservation,
            metadata,
            download_path=(
                f"{self._validate_artifact_base_url(artifact_base_url) if artifact_base_url is not None else self._artifact_base_url}"
                f"/api/v1/artifacts/{quote(reservation.id, safe='')}"
                f"/download?token={quote(reservation.token, safe='')}"
            ),
        )
        safe_result["delivery"] = "artifact"
        safe_result["artifact"] = public
        return {**dict(response), "result": safe_result}

    def download_artifact(
        self,
        artifact_id: str,
        token: str,
        authorization: str | None = None,
        *,
        transport: str = "core-http",
    ) -> ArtifactDownload:
        """Resolve an artifact for HTTP streaming after current-state checks."""

        # Token matching happens before authentication so every wrong artifact
        # token has one indistinguishable 404 response.
        try:
            metadata = self.artifacts.check_token(artifact_id, token)
        except ArtifactError:
            raise

        auth: AuthContext | None = None
        if authorization is not None:
            try:
                auth = self.access_control.authenticate(authorization)
            except AccessControlError as exc:
                self._audit_artifact(metadata, "denied", transport=transport)
                raise exc

        current_datasource_id: str | None = self.project.active_datasource_identifier()
        current_policy_versions: tuple[str, ...] = ()
        try:
            subject = self.access_control.subject(metadata.subject_id)
            policies = self.access_control.policies_for_subject(subject.id)
            project_id = str(self.project.overview().get("name") or "")
            decision = self.policy_engine.authorize_method(
                subject,
                "query.run",
                policies,
                project_id=project_id,
                datasource_id=current_datasource_id,
            )
            if decision.allowed:
                try:
                    compiled = self.policy_engine.compile_data_policy(
                        subject,
                        policies,
                        project_id=project_id,
                        datasource_id=current_datasource_id,
                    )
                    raw_versions = compiled.get("policyVersions")
                    if isinstance(raw_versions, list) and all(isinstance(item, str) for item in raw_versions):
                        current_policy_versions = tuple(raw_versions[:64])
                    else:
                        current_policy_versions = tuple(sorted(decision.policy_versions))
                except Exception:
                    current_policy_versions = ()
        except Exception:
            current_policy_versions = ()

        try:
            result = self.artifacts.resolve_download(
                artifact_id,
                token,
                current_datasource_id=current_datasource_id,
                current_policy_versions=current_policy_versions,
                authorization=auth,
            )
        except ArtifactError:
            self._audit_artifact(metadata, "denied", transport=transport)
            raise
        self._audit_artifact(metadata, "allowed", transport=transport)
        return result

    def _audit_artifact(self, metadata: ArtifactMetadata, decision: str, *, transport: str) -> None:
        """Write a metadata-only artifact event (never token, URL, or path)."""

        try:
            subject = self.access_control.subject(metadata.subject_id)
            auth = AuthContext(subject, "artifact_token", metadata.credential_id)
            self.access_control.record_audit(
                action="artifact.download",
                decision=decision,
                auth=auth,
                resource=f"artifact:{metadata.id}",
                policy_version=",".join(metadata.policy_versions) or None,
                details={
                    "artifactId": metadata.id,
                    "queryId": metadata.query_id,
                    "transport": transport if transport in {"core-http", "remote-mcp"} else "core-http",
                },
            )
        except Exception:
            _LOGGER.error("artifact audit write failed")

    def _normalize(
        self,
        method: Any,
        params: Mapping[str, Any],
        decision: PolicyDecision | None = None,
        *,
        protocol_version: str = CORE_PROTOCOL_VERSION,
    ) -> dict[str, Any] | str:
        project_dir = str(self.project.project_dir)
        if method == "health":
            return {} if not params else "health params must be empty"
        if method == "project.validate":
            return {"projectDir": project_dir} if not params else "project.validate params must be empty"
        if method == "project.describe":
            return {"projectDir": project_dir} if not params else "project.describe params must be empty"
        if method == "context.ask":
            if set(params) != {"question"} or not isinstance(params.get("question"), str):
                return "context.ask requires only question"
            return {"projectDir": project_dir, "question": params["question"]}
        if method == "query.cancel":
            if set(params) != {"queryId"} or not isinstance(params.get("queryId"), str):
                return "query.cancel requires only queryId"
            return {"queryId": params["queryId"]}
        if method == "query.dryPlan":
            if set(params) != {"semanticSql"} or not isinstance(params.get("semanticSql"), str):
                return "query.dryPlan requires only semanticSql"
            return {"projectDir": project_dir, "semanticSql": params["semanticSql"]}
        if method == "query.prepare":
            allowed = {"question", "semanticSql", "conditions", "confirmedConditions"}
            if set(params) - allowed:
                return "query.prepare contains unsupported fields"
            if not isinstance(params.get("question"), str) or not isinstance(params.get("semanticSql"), str):
                return "query.prepare requires question and semanticSql"
            conditions = params.get("conditions", {})
            confirmed = params.get("confirmedConditions", [])
            if not isinstance(conditions, Mapping) or not isinstance(confirmed, list):
                return "query.prepare conditions are invalid"
            return {
                "question": params["question"],
                "semanticSql": params["semanticSql"],
                "conditions": dict(conditions),
                "confirmedConditions": list(confirmed),
            }
        allowed = {"question", "semanticSql", "chartIntent", "queryId", "preparationId"}
        if protocol_version == CORE_PROTOCOL_VERSION:
            allowed.add("retryOfQueryId")
        if set(params) - allowed:
            return "query.run contains unsupported fields"
        for field in ("question", "semanticSql", "queryId"):
            if not isinstance(params.get(field), str) or not str(params[field]).strip():
                return f"query.run requires {field}"
        chart_intent = params.get("chartIntent")
        if chart_intent is not None and chart_intent not in {"auto", "table", "line", "bar", "pie"}:
            return "query.run chartIntent is invalid"
        policy_limits = decision.limits if decision and decision.limits else {}
        return {
            "projectDir": project_dir,
            "question": params["question"],
            "semanticSql": params["semanticSql"],
            "queryId": params["queryId"],
            "maxRows": min(MAX_QUERY_ROWS, policy_limits.get("maxRows", MAX_QUERY_ROWS)),
            "previewRows": min(MAX_PREVIEW_ROWS, policy_limits.get("previewRows", MAX_PREVIEW_ROWS)),
            "maxPreviewBytes": min(MAX_PREVIEW_BYTES, policy_limits.get("maxPreviewBytes", MAX_PREVIEW_BYTES)),
            "timeoutMs": min(MAX_TIMEOUT_MS, policy_limits.get("timeoutMs", MAX_TIMEOUT_MS)),
            "databaseDsnEnv": "SEMARAIL_DATABASE_URL",
            **({"chartIntent": chart_intent} if chart_intent is not None else {}),
            **({"preparationId": params["preparationId"]} if isinstance(params.get("preparationId"), str) else {}),
            **({"retryOfQueryId": params["retryOfQueryId"]} if isinstance(params.get("retryOfQueryId"), str) else {}),
        }

    def _audit(
        self,
        auth: AuthContext,
        action: str,
        result: str,
        request_id: str,
        decision: PolicyDecision,
        *,
        transport: str,
        datasource_id: str | None = None,
        query_id: Any = None,
        compiled_policy: Mapping[str, Any] | None = None,
    ) -> None:
        safe_transport = transport if transport in {"runtime-rpc", "remote-mcp"} else "runtime-rpc"
        safe_query_id = query_id[:128] if isinstance(query_id, str) else None
        policy_versions = list(decision.policy_versions)
        policy_tables: list[str] = []
        if compiled_policy is not None:
            raw_versions = compiled_policy.get("policyVersions")
            if isinstance(raw_versions, list) and all(isinstance(item, str) for item in raw_versions):
                policy_versions = raw_versions[:64]
            # These are the tables whose compiled controls were supplied to
            # the execution boundary, not a claim about the exact SQL access
            # path. Query text and resolved row values stay absent.
            raw_tables = compiled_policy.get("tables")
            if isinstance(raw_tables, Mapping):
                policy_tables = sorted(str(item) for item in raw_tables)[:1_000]
        details: dict[str, Any] = {
            "requestId": request_id,
            "transport": safe_transport,
            "authenticationMethod": auth.method,
            "policyTables": policy_tables,
            "policyVersions": policy_versions,
        }
        if datasource_id is not None:
            details["datasourceId"] = datasource_id
        if safe_query_id is not None:
            details["queryId"] = safe_query_id
        try:
            self.access_control.record_audit(
                action=action,
                decision=result,
                auth=auth,
                resource=str(self.project.overview().get("name") or ""),
                policy_version=decision.version_key or None,
                details=details,
            )
        except AccessControlError:
            _LOGGER.error("runtime audit write failed")


__all__ = ["CORE_API_VERSION", "CORE_PROTOCOL_VERSION", "RuntimeRpcGateway"]
