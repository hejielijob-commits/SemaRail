"""Stable loopback RPC boundary exposed by the standalone SemaRail Core.

The public request never accepts a project path, database credentials, or
execution limits.  Those deployment decisions stay inside Core while thin
agent integrations reuse the existing version-one sidecar response envelope.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Mapping
from typing import Any, Protocol

try:
    from .project import ProjectStore
except ImportError:  # pragma: no cover - direct module loading
    from project import ProjectStore  # type: ignore[no-redef]


CORE_API_VERSION = "1"
CORE_PROTOCOL_VERSION = "1"
MAX_QUERY_ROWS = 500
MAX_PREVIEW_ROWS = 200
MAX_PREVIEW_BYTES = 1_048_576
MAX_TIMEOUT_MS = 30_000
_PUBLIC_METHODS = frozenset({"health", "project.validate", "context.ask", "query.run", "query.cancel"})
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
    ) -> None:
        self.project = project
        self.dispatcher = dispatcher if dispatcher is not None else _default_dispatcher(project)
        configured = auth_token if auth_token is not None else os.environ.get("SEMARAIL_API_TOKEN", "")
        self.auth_token = configured.strip()

    def dispatch(self, body: Any, authorization: str | None = None) -> tuple[int, dict[str, Any]]:
        request_id = _request_id(body)
        if len(self.auth_token) < 32:
            return 503, _error(request_id, "WREN_UNAVAILABLE", "SemaRail Core authentication is not configured")
        expected = f"Bearer {self.auth_token}"
        if not isinstance(authorization, str) or not secrets.compare_digest(authorization, expected):
            return 401, _error(request_id, "UNAUTHENTICATED", "authentication is required")
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
        normalized = self._normalize(method, params)
        if isinstance(normalized, str):
            return 400, _error(request_id, "INVALID_PARAMS", normalized)
        if self.dispatcher is None:
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
        return 200, response

    def _normalize(self, method: Any, params: Mapping[str, Any]) -> dict[str, Any] | str:
        project_dir = str(self.project.project_dir)
        if method == "health":
            return {} if not params else "health params must be empty"
        if method == "project.validate":
            return {"projectDir": project_dir} if not params else "project.validate params must be empty"
        if method == "context.ask":
            if set(params) != {"question"} or not isinstance(params.get("question"), str):
                return "context.ask requires only question"
            return {"projectDir": project_dir, "question": params["question"]}
        if method == "query.cancel":
            if set(params) != {"queryId"} or not isinstance(params.get("queryId"), str):
                return "query.cancel requires only queryId"
            return {"queryId": params["queryId"]}
        allowed = {"question", "semanticSql", "chartIntent", "queryId"}
        if set(params) - allowed:
            return "query.run contains unsupported fields"
        for field in ("question", "semanticSql", "queryId"):
            if not isinstance(params.get(field), str) or not str(params[field]).strip():
                return f"query.run requires {field}"
        chart_intent = params.get("chartIntent")
        if chart_intent is not None and chart_intent not in {"auto", "table", "line", "bar", "pie"}:
            return "query.run chartIntent is invalid"
        return {
            "projectDir": project_dir,
            "question": params["question"],
            "semanticSql": params["semanticSql"],
            "queryId": params["queryId"],
            "maxRows": MAX_QUERY_ROWS,
            "previewRows": MAX_PREVIEW_ROWS,
            "maxPreviewBytes": MAX_PREVIEW_BYTES,
            "timeoutMs": MAX_TIMEOUT_MS,
            "databaseDsnEnv": "SEMARAIL_DATABASE_URL",
            **({"chartIntent": chart_intent} if chart_intent is not None else {}),
        }


__all__ = ["CORE_API_VERSION", "CORE_PROTOCOL_VERSION", "RuntimeRpcGateway"]
