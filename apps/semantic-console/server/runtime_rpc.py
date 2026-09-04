"""Stable loopback RPC boundary exposed by the standalone SemaRail Core.

The public request never accepts a project path, database credentials, or
execution limits.  Those deployment decisions stay inside Core while thin
agent integrations reuse the existing version-one sidecar response envelope.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

try:
    from .access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID
    from .authorization import PolicyDecision, PolicyEngine
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
    from .project import ProjectStore
except ImportError:  # pragma: no cover - direct module loading
    from access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID  # type: ignore[no-redef]
    from authorization import PolicyDecision, PolicyEngine  # type: ignore[no-redef]
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
    from project import ProjectStore  # type: ignore[no-redef]


CORE_API_VERSION = "1"
CORE_PROTOCOL_VERSION = "1"
MAX_QUERY_ROWS = 500
MAX_PREVIEW_ROWS = 200
MAX_PREVIEW_BYTES = 1_048_576
MAX_TIMEOUT_MS = 30_000
_PUBLIC_METHODS = frozenset(
    {"health", "project.validate", "project.describe", "context.ask", "query.dryPlan", "query.run", "query.cancel"}
)
_DATA_POLICY_METHODS = frozenset({"project.describe", "context.ask", "query.dryPlan", "query.run"})
_REQUEST_FIELDS = frozenset({"protocolVersion", "id", "method", "params", "deadlineMs"})
_LOGGER = logging.getLogger("semarail-core.runtime")
DEFAULT_ARTIFACT_BASE_URL = "http://127.0.0.1:48763"


class RuntimeDispatcher(Protocol):
    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


def _error(request_id: str, code: str, message: str) -> dict[str, Any]:
    return {
        "protocolVersion": CORE_PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": {"code": code, "phase": "protocol", "message": message, "retryable": False},
    }


def _request_id(value: Any) -> str:
    if isinstance(value, Mapping) and isinstance(value.get("id"), str):
        return str(value["id"])[:128]
    return ""


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
        try:
            auth = self.access_control.authenticate(authorization)
        except AccessControlError as exc:
            return exc.status, _error(request_id, exc.code, exc.safe_message)
        if not isinstance(body, Mapping):
            return 400, _error(request_id, "INVALID_REQUEST", "request must be a JSON object")
        if set(body) - _REQUEST_FIELDS:
            return 400, _error(request_id, "INVALID_REQUEST", "request contains unknown fields")
        if body.get("protocolVersion") != CORE_PROTOCOL_VERSION:
            return 400, _error(request_id, "UNSUPPORTED_PROTOCOL", "protocolVersion is unsupported")
        if not isinstance(body.get("id"), str) or not 1 <= len(str(body.get("id"))) <= 128:
            return 400, _error("", "INVALID_REQUEST", "id is invalid")
        method = body.get("method")
        if method not in _PUBLIC_METHODS:
            return 400, _error(request_id, "METHOD_NOT_FOUND", "method is not supported")
        params = body.get("params")
        if not isinstance(params, Mapping):
            return 400, _error(request_id, "INVALID_PARAMS", "params must be an object")
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
            return 403, _error(request_id, "FORBIDDEN", "managed Agent credentials are required")
        policies = (
            [] if auth.subject.id == BOOTSTRAP_SUBJECT_ID
            else self.access_control.policies_for_subject(auth.subject.id)
        )
        project_id = str(self.project.overview().get("name") or "")
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
            return 403, _error(request_id, "FORBIDDEN", "permission is required")
        normalized = self._normalize(method, params, decision)
        if isinstance(normalized, str):
            self._audit(
                auth, str(method), "denied", request_id, decision,
                transport=transport, datasource_id=datasource_id,
                query_id=params.get("queryId") if method in {"query.run", "query.cancel"} else None,
            )
            return 400, _error(request_id, "INVALID_PARAMS", normalized)
        compiled_policy: Mapping[str, Any] | None = None
        if method in _DATA_POLICY_METHODS:
            try:
                compiled_policy = self.policy_engine.compile_data_policy(
                    auth.subject, policies, project_id=project_id, datasource_id=datasource_id
                )
                normalized["authorizationPolicy"] = compiled_policy
            except Exception:
                self._audit(
                    auth, str(method), "denied", request_id, decision,
                    transport=transport, datasource_id=datasource_id,
                    query_id=normalized.get("queryId") if method in {"query.run", "query.cancel"} else None,
                )
                return 403, _error(request_id, "FORBIDDEN", "data access policy is invalid")
        artifact_reservation: ArtifactReservation | None = None
        if method == "query.run":
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
                return 403, _error(request_id, "FORBIDDEN", "artifact identity binding is required")
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
                return 503, _error(request_id, "ARTIFACT_UNAVAILABLE", "artifact service is unavailable")
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
            return 503, _error(request_id, "WREN_UNAVAILABLE", "SemaRail runtime is unavailable")
        internal = {
            "protocolVersion": CORE_PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "params": normalized,
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
            return 503, _error(request_id, "WREN_UNAVAILABLE", "SemaRail runtime is unavailable")
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
                    "protocolVersion": CORE_PROTOCOL_VERSION,
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
        return 200, response

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
        allowed = {"question", "semanticSql", "chartIntent", "queryId"}
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
