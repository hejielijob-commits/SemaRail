"""Stable loopback RPC boundary exposed by the standalone SemaRail Core.

The public request never accepts a project path, database credentials, or
execution limits.  Those deployment decisions stay inside Core while thin
agent integrations reuse the existing version-one sidecar response envelope.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any, Protocol

try:
    from .access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID
    from .authorization import PolicyDecision, PolicyEngine
    from .project import ProjectStore
except ImportError:  # pragma: no cover - direct module loading
    from access_control import AccessControlError, AccessControlStore, AuthContext, BOOTSTRAP_SUBJECT_ID  # type: ignore[no-redef]
    from authorization import PolicyDecision, PolicyEngine  # type: ignore[no-redef]
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

    def dispatch(
        self,
        body: Any,
        authorization: str | None = None,
        *,
        transport: str = "runtime-rpc",
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
        if self.dispatcher is None:
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
        response = self.dispatcher.dispatch(internal)
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
