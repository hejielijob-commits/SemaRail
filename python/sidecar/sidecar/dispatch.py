"""Versioned RPC request validation and method dispatch."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .errors import (
    HEALTHCHECK_FAILED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    POLICY_DENIED,
    PROJECT_VALIDATION_FAILED,
    SEMANTIC_ERROR,
    RpcError,
    RpcFault,
    UNSUPPORTED_PROTOCOL,
    WREN_UNAVAILABLE,
)
from .protocol import PROTOCOL_VERSION
from .semantic_policy import filter_semantic_result
from .sql_policy import SqlPolicyError, validate_semantic_sql


RPC_METHODS = frozenset(
    {
        "health",
        "project.validate",
        "project.describe",
        "context.ask",
        "query.dryPlan",
        "query.run",
        "query.cancel",
    }
)
_REQUEST_FIELDS = frozenset(
    {"protocolVersion", "id", "method", "params", "deadlineMs"}
)


class ProjectValidator(Protocol):
    """The only Wren-facing dependency needed by ``project.validate``."""

    def validate(self, params: Mapping[str, Any]) -> Any:
        """Validate a Wren project and return a JSON-safe result."""


class ContextProvider(Protocol):
    """Wren-facing dependency used by semantic context methods."""

    def describe(self, params: Mapping[str, Any]) -> Any:
        """Return structured models and relationships for a project."""

    def ask(self, params: Mapping[str, Any]) -> Any:
        """Return structured semantic context for a question."""


class QueryPlanner(Protocol):
    """Wren-facing dependency used by ``query.dryPlan``."""

    def dry_plan(self, params: Mapping[str, Any]) -> Any:
        """Transform semantic SQL without executing it."""


class QueryService(Protocol):
    """Wren query execution/cancellation dependency."""

    def run(self, params: Mapping[str, Any]) -> Any:
        """Plan and execute one bounded query."""

    def cancel(self, params: Mapping[str, Any]) -> Any:
        """Cancel one query by its query id."""


ProjectValidatorCallable = Callable[[Mapping[str, Any]], Any]
ContextProviderCallable = Callable[[Mapping[str, Any]], Any]
QueryPlannerCallable = Callable[[Mapping[str, Any]], Any]
QueryServiceCallable = Callable[[Mapping[str, Any]], Any]
HealthProvider = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class SidecarDependencies:
    """Injectable process dependencies.

    ``project_validator`` may be an object exposing ``validate`` or a callable
    accepting the request params. Keeping this interface free of Wren imports
    lets tests and Host integration use a fake validator.
    """

    project_validator: ProjectValidator | ProjectValidatorCallable | None = None
    context_provider: ContextProvider | ContextProviderCallable | None = None
    query_planner: QueryPlanner | QueryPlannerCallable | None = None
    query_service: QueryService | None = None
    query_runner: QueryService | QueryServiceCallable | None = None
    health_provider: HealthProvider | None = None


@dataclass(frozen=True, slots=True)
class RpcRequest:
    """A validated version-one RPC request."""

    id: str
    method: str
    params: Any
    deadline_ms: int | None = None

    @classmethod
    def from_mapping(cls, request: Mapping[str, Any]) -> "RpcRequest":
        if not isinstance(request, Mapping):
            raise RpcFault(
                INVALID_REQUEST,
                "protocol",
                "request must be a JSON object",
            )

        unknown = set(request) - _REQUEST_FIELDS
        if unknown:
            raise RpcFault(
                INVALID_REQUEST,
                "protocol",
                "request contains unknown fields",
            )

        if request.get("protocolVersion") != PROTOCOL_VERSION:
            raise RpcFault(
                UNSUPPORTED_PROTOCOL,
                "protocol",
                "protocolVersion must be \"1\"",
            )

        request_id = request.get("id")
        if not isinstance(request_id, str) or not (1 <= len(request_id) <= 128):
            raise RpcFault(
                INVALID_REQUEST,
                "protocol",
                "id must be a string between 1 and 128 characters",
            )

        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise RpcFault(INVALID_REQUEST, "protocol", "method must be a non-empty string")
        if method not in RPC_METHODS:
            raise RpcFault(METHOD_NOT_FOUND, "dispatch", "method is not supported")

        if "params" not in request:
            raise RpcFault(INVALID_REQUEST, "protocol", "params is required")
        params = request["params"]
        if not _is_json_safe(params):
            raise RpcFault(INVALID_REQUEST, "protocol", "params must be JSON-safe")

        deadline_ms: int | None = None
        if "deadlineMs" in request:
            deadline = request["deadlineMs"]
            if type(deadline) is not int or deadline < 0:
                raise RpcFault(
                    INVALID_REQUEST,
                    "protocol",
                    "deadlineMs must be a non-negative integer",
                )
            deadline_ms = deadline
        return cls(
            id=request_id,
            method=method,
            params=params,
            deadline_ms=deadline_ms,
        )


def _is_json_safe(value: Any, depth: int = 0) -> bool:
    if depth > 64:
        return False
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_safe(item, depth + 1) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _is_json_safe(item, depth + 1)
            for key, item in value.items()
        )
    return False


def _safe_request_id(request: Any) -> str:
    if isinstance(request, Mapping):
        request_id = request.get("id")
        if isinstance(request_id, str):
            return request_id
    return ""


def _response(request_id: str, *, result: Any = None, error: RpcError | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "id": request_id,
        "ok": error is None,
    }
    if error is None:
        response["result"] = result
    else:
        response["error"] = error.normalized().as_dict()
    return response


def _ensure_json_safe(value: Any) -> Any:
    """Reject adapter results that cannot cross the JSON process boundary."""

    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RpcFault(
            INTERNAL_ERROR,
            "dispatch",
            "handler returned a non-JSON result",
        ) from exc
    return value


class Dispatcher:
    """Dispatch version-one requests without importing Wren at module load."""

    def __init__(
        self,
        dependencies: SidecarDependencies | None = None,
        *,
        project_validator: ProjectValidator | ProjectValidatorCallable | None = None,
        context_provider: ContextProvider | ContextProviderCallable | None = None,
        query_planner: QueryPlanner | QueryPlannerCallable | None = None,
        query_service: QueryService | None = None,
        query_runner: QueryService | QueryServiceCallable | None = None,
        health_provider: HealthProvider | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if dependencies is not None and (
            project_validator is not None
            or context_provider is not None
            or query_planner is not None
            or query_service is not None
            or query_runner is not None
            or health_provider is not None
        ):
            raise ValueError("pass dependencies or keyword dependencies, not both")
        self.dependencies = dependencies or SidecarDependencies(
            project_validator=project_validator,
            context_provider=context_provider,
            query_planner=query_planner,
            query_service=query_service,
            query_runner=query_runner,
            health_provider=health_provider,
        )
        self.logger = logger or logging.getLogger("sidecar.dispatch")

    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Return a protocol response for every request, including failures."""

        request_id = _safe_request_id(request)
        try:
            parsed = RpcRequest.from_mapping(request)
            if parsed.method == "health":
                result = self._health(parsed.params)
            elif parsed.method == "project.validate":
                result = self._project_validate(parsed.params)
            elif parsed.method == "project.describe":
                result = self._project_describe(parsed.params)
            elif parsed.method == "context.ask":
                result = self._context_ask(parsed.params)
            elif parsed.method == "query.dryPlan":
                result = self._query_dry_plan(parsed.params)
            elif parsed.method == "query.run":
                result = self._query_run(parsed.params)
            elif parsed.method == "query.cancel":
                result = self._query_cancel(parsed.params)
            else:
                raise RpcFault(
                    METHOD_NOT_FOUND,
                    "dispatch",
                    "method is not supported",
                )
            return _response(parsed.id, result=_ensure_json_safe(result))
        except RpcFault as fault:
            return _response(request_id, error=fault.error)
        except Exception:
            # The exception is intentionally not sent to the caller or logger:
            # it may contain a DSN, credential, SQL fragment, or path.
            self.logger.error("unexpected sidecar dispatch failure")
            return _response(
                request_id,
                error=RpcError(
                    code=INTERNAL_ERROR,
                    phase="dispatch",
                    message="internal sidecar error",
                    retryable=False,
                ),
            )

    def _health(self, params: Any) -> Any:
        if not isinstance(params, Mapping):
            raise RpcFault(INVALID_PARAMS, "validation", "params must be an object")
        if params:
            raise RpcFault(INVALID_PARAMS, "validation", "health params must be empty")
        provider = self.dependencies.health_provider
        if provider is None:
            return {"status": "ok", "protocolVersion": PROTOCOL_VERSION}
        try:
            return provider()
        except RpcFault:
            raise
        except Exception as exc:
            raise RpcFault(
                HEALTHCHECK_FAILED,
                "health",
                "health check failed",
                retryable=True,
            ) from exc

    def _project_validate(self, params: Any) -> Any:
        if not isinstance(params, Mapping):
            raise RpcFault(INVALID_PARAMS, "validation", "params must be an object")
        if set(params) != {"projectDir"}:
            raise RpcFault(
                INVALID_PARAMS,
                "validation",
                "project.validate params must contain only projectDir",
            )
        project_dir = params.get("projectDir")
        if not isinstance(project_dir, str) or not project_dir.strip():
            raise RpcFault(INVALID_PARAMS, "validation", "projectDir is required")
        validator = self.dependencies.project_validator
        if validator is None:
            raise RpcFault(
                WREN_UNAVAILABLE,
                "project.validate",
                "SemaRail project validator is unavailable",
                retryable=True,
            )
        try:
            if callable(validator):
                return validator(cast(Mapping[str, Any], params))
            return validator.validate(cast(Mapping[str, Any], params))
        except RpcFault:
            raise
        except Exception as exc:
            # Never include exception text or traceback: Wren errors may carry
            # DSNs, credentials, SQL, or absolute project paths.
            self.logger.error("project validation failed")
            raise RpcFault(
                PROJECT_VALIDATION_FAILED,
                "project.validate",
                "project validation failed",
                retryable=False,
            ) from exc

    def _context_ask(self, params: Any) -> Any:
        object_params, authorization_policy = _semantic_params(
            params,
            method="context.ask",
            fields={"projectDir", "question"},
        )
        _required_string(object_params, "projectDir", maximum=32_768)
        _required_string(object_params, "question", maximum=16_000)
        provider = self.dependencies.context_provider
        if provider is None:
            raise RpcFault(
                WREN_UNAVAILABLE,
                "context.ask",
                "SemaRail context provider is unavailable",
                retryable=True,
            )
        try:
            if callable(provider):
                result = provider(object_params)
            else:
                result = provider.ask(object_params)
            return _filter_semantic_response("context.ask", result, authorization_policy)
        except RpcFault:
            raise
        except Exception as exc:
            self.logger.error("semantic context lookup failed")
            raise RpcFault(
                SEMANTIC_ERROR,
                "context.ask",
                "semantic context lookup failed",
                retryable=False,
            ) from exc

    def _project_describe(self, params: Any) -> Any:
        object_params, authorization_policy = _semantic_params(
            params,
            method="project.describe",
            fields={"projectDir"},
        )
        _required_string(object_params, "projectDir", maximum=32_768)
        provider = self.dependencies.context_provider
        describe = getattr(provider, "describe", None) if provider is not None else None
        if not callable(describe):
            raise RpcFault(
                WREN_UNAVAILABLE,
                "project.describe",
                "SemaRail project description is unavailable",
                retryable=True,
            )
        try:
            return _filter_semantic_response("project.describe", describe(object_params), authorization_policy)
        except RpcFault:
            raise
        except Exception as exc:
            self.logger.error("semantic project description failed")
            raise RpcFault(
                SEMANTIC_ERROR,
                "project.describe",
                "semantic project description failed",
                retryable=False,
            ) from exc

    def _query_dry_plan(self, params: Any) -> Any:
        object_params, authorization_policy = _semantic_params(
            params,
            method="query.dryPlan",
            fields={"projectDir", "semanticSql"},
        )
        _required_string(object_params, "projectDir", maximum=32_768)
        semantic_sql = _required_string(object_params, "semanticSql", maximum=64_000)
        try:
            validate_semantic_sql(semantic_sql)
        except SqlPolicyError as exc:
            raise RpcFault(
                POLICY_DENIED,
                "policy",
                "semantic SQL must be one read-only query",
                retryable=False,
            ) from exc
        planner = self.dependencies.query_planner
        if planner is None:
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.dryPlan",
                "SemaRail semantic planner is unavailable",
                retryable=True,
            )
        try:
            if callable(planner):
                result = planner(object_params)
            else:
                result = planner.dry_plan(object_params)
            return _filter_semantic_response("query.dryPlan", result, authorization_policy)
        except RpcFault:
            raise
        except Exception as exc:
            self.logger.error("semantic SQL planning failed")
            raise RpcFault(
                SEMANTIC_ERROR,
                "query.dryPlan",
                "semantic SQL planning failed",
                retryable=False,
            ) from exc

    def _query_run(self, params: Any) -> Any:
        object_params = _query_run_params(params)
        provider = self.dependencies.query_service or self.dependencies.query_runner
        if provider is None:
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.run",
                "SemaRail query runner is unavailable",
                retryable=True,
            )
        try:
            if callable(provider) and not hasattr(provider, "run"):
                return provider(object_params)
            return provider.run(object_params)  # type: ignore[union-attr]
        except RpcFault:
            raise
        except Exception as exc:
            # Keep driver/planner exception text out of both logs and wire.
            self.logger.error("query execution failed")
            raise RpcFault(
                INTERNAL_ERROR,
                "query.run",
                "query execution failed",
                retryable=False,
            ) from exc

    def _query_cancel(self, params: Any) -> Any:
        object_params = _query_cancel_params(params)
        provider = self.dependencies.query_service or self.dependencies.query_runner
        if provider is None:
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.cancel",
                "SemaRail query runner is unavailable",
                retryable=True,
            )
        try:
            cancel = getattr(provider, "cancel", None)
            if not callable(cancel):
                raise RpcFault(
                    WREN_UNAVAILABLE,
                    "query.cancel",
                    "SemaRail query cancellation is unavailable",
                    retryable=True,
                )
            return cancel(object_params)
        except RpcFault:
            raise
        except Exception as exc:
            self.logger.error("query cancellation failed")
            raise RpcFault(
                INTERNAL_ERROR,
                "query.cancel",
                "query cancellation failed",
                retryable=False,
            ) from exc


RpcDispatcher = Dispatcher


def _method_params(
    params: Any,
    *,
    method: str,
    fields: set[str],
) -> Mapping[str, Any]:
    if not isinstance(params, Mapping):
        raise RpcFault(INVALID_PARAMS, "validation", "params must be an object")
    if set(params) != fields:
        raise RpcFault(
            INVALID_PARAMS,
            "validation",
            f"{method} params are invalid",
        )
    return cast(Mapping[str, Any], params)


def _semantic_params(
    params: Any,
    *,
    method: str,
    fields: set[str],
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
    """Keep Core's compiled policy out of the Wren adapter call shape."""

    if not isinstance(params, Mapping):
        raise RpcFault(INVALID_PARAMS, "validation", "params must be an object")
    if set(params) - (fields | {"authorizationPolicy"}) or not fields.issubset(params):
        raise RpcFault(INVALID_PARAMS, "validation", f"{method} params are invalid")
    policy = params.get("authorizationPolicy")
    if policy is not None and not isinstance(policy, Mapping):
        raise RpcFault(INVALID_PARAMS, "validation", "authorizationPolicy is invalid")
    return ({key: params[key] for key in fields}, cast(Mapping[str, Any] | None, policy))


def _filter_semantic_response(method: str, result: Any, policy: Mapping[str, Any] | None) -> Any:
    # RuntimeRpcGateway always supplies one (including bootstrap's explicit
    # allow policy); direct sidecar/MCP embedders retain their v1 behavior.
    return result if policy is None else filter_semantic_result(method, result, policy)


def _required_string(
    params: Mapping[str, Any],
    field: str,
    *,
    maximum: int,
) -> str:
    value = params.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RpcFault(
            INVALID_PARAMS,
            "validation",
            f"{field} must be a non-empty string",
        )
    return value


def _query_run_params(params: Any) -> Mapping[str, Any]:
    """Validate the sidecar-facing query.run shape before adapter code runs."""

    allowed = {
        "projectDir",
        "question",
        "semanticSql",
        "queryId",
        "chartIntent",
        "timeoutMs",
        "maxRows",
        "previewRows",
        "maxPreviewBytes",
        "databaseDsnEnv",
        "authorizationPolicy",
    }
    if not isinstance(params, Mapping):
        raise RpcFault(INVALID_PARAMS, "validation", "params must be an object")
    if set(params) - allowed:
        raise RpcFault(INVALID_PARAMS, "validation", "query.run params are invalid")
    _required_string(params, "projectDir", maximum=32_768)
    _required_string(params, "question", maximum=16_000)
    semantic_sql = _required_string(params, "semanticSql", maximum=64_000)
    try:
        validate_semantic_sql(semantic_sql)
    except SqlPolicyError as exc:
        raise RpcFault(
            POLICY_DENIED,
            "policy",
            "semantic SQL must be one read-only query",
            retryable=False,
        ) from exc
    _required_string(params, "queryId", maximum=128)
    for field, maximum in (
        # AC-07 is a hard 30 second wall; a request must never enlarge it.
        ("timeoutMs", 30_000),
        ("maxRows", 500),
        ("previewRows", 200),
        ("maxPreviewBytes", 1_048_576),
    ):
        if field in params:
            value = params[field]
            if type(value) is not int or value < 1 or value > maximum:
                raise RpcFault(
                    INVALID_PARAMS,
                    "validation",
                    f"{field} is outside the supported range",
                )
    if "chartIntent" in params and params["chartIntent"] not in {"auto", "table", "line", "bar", "pie"}:
        raise RpcFault(INVALID_PARAMS, "validation", "chartIntent is invalid")
    if "databaseDsnEnv" in params:
        env_name = params["databaseDsnEnv"]
        if not isinstance(env_name, str) or not env_name or len(env_name) > 128:
            raise RpcFault(INVALID_PARAMS, "validation", "databaseDsnEnv is invalid")
    if "authorizationPolicy" in params and not isinstance(params["authorizationPolicy"], Mapping):
        raise RpcFault(INVALID_PARAMS, "validation", "authorizationPolicy is invalid")
    return cast(Mapping[str, Any], params)


def _query_cancel_params(params: Any) -> Mapping[str, Any]:
    if not isinstance(params, Mapping) or set(params) != {"queryId"}:
        raise RpcFault(INVALID_PARAMS, "validation", "query.cancel params are invalid")
    _required_string(params, "queryId", maximum=128)
    return cast(Mapping[str, Any], params)


def dispatch_request(
    request: Mapping[str, Any],
    dependencies: SidecarDependencies | None = None,
    *,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Convenience function for adapters and focused contract tests."""

    return Dispatcher(dependencies, logger=logger).dispatch(request)
