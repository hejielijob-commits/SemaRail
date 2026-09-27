"""Versioned RPC request validation and method dispatch."""

from __future__ import annotations

import json
import hashlib
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
from .protocol import LEGACY_PROTOCOL_VERSION, PROTOCOL_VERSION
from .query import artifact_request_from_mapping
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
    {"protocolVersion", "id", "method", "params", "deadlineMs", "traceId"}
)

# Context API v2 is deliberately an opt-in request shape.  The legacy
# ``context.ask`` shape remains exactly ``projectDir`` + ``question`` so old
# clients and replay fixtures keep receiving the v1 context unchanged.
_CONTEXT_V2_VERSION = 2
_CONTEXT_V2_SECTIONS = ("schema", "relationships", "metrics", "rules", "sqlExamples", "views")
_CONTEXT_V2_DEFAULT_BUDGETS: dict[str, Any] = {
    "topK": {
        "schema": 15,
        "relationships": 8,
        "metrics": 8,
        "rules": 8,
        "sqlExamples": 3,
        "views": 3,
    },
    "maxBytes": 65_536,
    "maxTokens": 16_384,
    "maxRelationshipDepth": 2,
}


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
    """A validated legacy or current RPC request."""

    protocol_version: str
    id: str
    method: str
    params: Any
    deadline_ms: int | None = None
    trace_id: str = ""

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

        protocol_version = request.get("protocolVersion")
        if protocol_version not in {LEGACY_PROTOCOL_VERSION, PROTOCOL_VERSION}:
            raise RpcFault(
                UNSUPPORTED_PROTOCOL,
                "protocol",
                "protocolVersion is unsupported",
            )
        if protocol_version == LEGACY_PROTOCOL_VERSION and "traceId" in request:
            raise RpcFault(INVALID_REQUEST, "protocol", "traceId requires protocolVersion 2")
        trace_id = request.get("traceId", "")
        if protocol_version == PROTOCOL_VERSION and (not isinstance(trace_id, str) or not 1 <= len(trace_id) <= 128):
            raise RpcFault(INVALID_REQUEST, "protocol", "traceId is invalid")

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
            protocol_version=protocol_version,
            id=request_id,
            method=method,
            params=params,
            deadline_ms=deadline_ms,
            trace_id=trace_id,
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


def _response(request_id: str, *, protocol_version: str = LEGACY_PROTOCOL_VERSION, trace_id: str = "", result: Any = None, error: RpcError | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocolVersion": protocol_version,
        "id": request_id,
        "ok": error is None,
    }
    if error is None:
        response["result"] = result
    else:
        response["error"] = error.normalized().as_dict(protocol_version=protocol_version, trace_id=trace_id)
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
    """Dispatch supported protocol requests without importing Wren at module load."""

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
            if parsed.method == "health" and isinstance(result, Mapping):
                result = {**result, "protocolVersion": parsed.protocol_version}
            return _response(parsed.id, protocol_version=parsed.protocol_version, trace_id=parsed.trace_id, result=_ensure_json_safe(result))
        except RpcFault as fault:
            response_version = request.get("protocolVersion") if isinstance(request, Mapping) and request.get("protocolVersion") in {LEGACY_PROTOCOL_VERSION, PROTOCOL_VERSION} else LEGACY_PROTOCOL_VERSION
            response_trace = request.get("traceId", "") if isinstance(request, Mapping) else ""
            return _response(request_id, protocol_version=response_version, trace_id=response_trace, error=fault.error)
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
        if isinstance(params, Mapping) and "contextVersion" in params:
            return self._context_ask_v2(params)
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

    def _context_ask_v2(self, params: Any) -> Any:
        """Dispatch the opt-in partitioned Context API v2.

        Adapters may implement ``ask_v2`` as a native retrieval seam.  Until
        that is available, a v1-shaped adapter result is converted into the
        bounded v2 partitions here; this keeps transport and authorization
        behavior stable while the index implementation evolves independently.
        """

        object_params, authorization_policy, budgets = _semantic_v2_params(params)
        provider = self.dependencies.context_provider
        if provider is None:
            raise RpcFault(
                WREN_UNAVAILABLE,
                "context.ask",
                "SemaRail context provider is unavailable",
                retryable=True,
            )
        try:
            ask_v2 = getattr(provider, "ask_v2", None)
            if callable(ask_v2):
                result = ask_v2(object_params)
            elif callable(provider):
                result = provider(object_params)
            else:
                result = provider.ask(object_params)
            # Oversample/normalize first, then apply the structural policy
            # projection, and only then spend the caller's context budget.
            # This prevents denied records from consuming the visible quota.
            partitioned = _coerce_context_v2(result, budgets, apply_budget=False)
            catalog = result.get("_authorizationCatalog") if isinstance(result, Mapping) else None
            filtered = (
                filter_semantic_result("context.ask", partitioned, authorization_policy, context_catalog=catalog)
                if authorization_policy is not None
                else partitioned
            )
            return _apply_context_v2_budgets(filtered) if isinstance(filtered, dict) else filtered
        except RpcFault:
            raise
        except Exception as exc:
            self.logger.error("semantic context v2 lookup failed")
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


def _semantic_v2_params(
    params: Any,
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None, dict[str, Any]]:
    """Validate the versioned v2 context call and return adapter-safe params."""

    allowed = {"projectDir", "question", "contextVersion", "budgets", "authorizationPolicy"}
    if not isinstance(params, Mapping) or set(params) - allowed:
        raise RpcFault(INVALID_PARAMS, "validation", "context.ask v2 params are invalid")
    if set(params) & {"projectDir", "question", "contextVersion"} != {"projectDir", "question", "contextVersion"}:
        raise RpcFault(INVALID_PARAMS, "validation", "context.ask v2 params are invalid")
    version = params.get("contextVersion")
    if type(version) is not int or version != _CONTEXT_V2_VERSION:
        raise RpcFault(UNSUPPORTED_PROTOCOL, "validation", "contextVersion is unsupported")
    _required_string(params, "projectDir", maximum=32_768)
    _required_string(params, "question", maximum=16_000)
    budgets = _validate_context_v2_budgets(params.get("budgets"))
    policy = params.get("authorizationPolicy")
    if policy is not None and not isinstance(policy, Mapping):
        raise RpcFault(INVALID_PARAMS, "validation", "authorizationPolicy is invalid")
    adapter_params: dict[str, Any] = {
        "projectDir": params["projectDir"],
        "question": params["question"],
        "contextVersion": _CONTEXT_V2_VERSION,
    }
    # The in-process retrieval backend needs the compiled, secret-free policy
    # to exclude denied documents before exact/lexical/vector scoring.  The
    # response is still projected again below as a defence-in-depth boundary.
    if policy is not None:
        adapter_params["authorizationPolicy"] = policy
    if "budgets" in params:
        adapter_params["budgets"] = budgets
    return adapter_params, cast(Mapping[str, Any] | None, policy), budgets


def _validate_context_v2_budgets(value: Any) -> dict[str, Any]:
    """Validate bounded v2 budgets and merge deterministic defaults."""

    defaults = {
        "topK": dict(_CONTEXT_V2_DEFAULT_BUDGETS["topK"]),
        "maxBytes": _CONTEXT_V2_DEFAULT_BUDGETS["maxBytes"],
        "maxTokens": _CONTEXT_V2_DEFAULT_BUDGETS["maxTokens"],
        "maxRelationshipDepth": _CONTEXT_V2_DEFAULT_BUDGETS["maxRelationshipDepth"],
    }
    if value is None:
        return defaults
    if not isinstance(value, Mapping):
        raise RpcFault(INVALID_PARAMS, "validation", "budgets must be an object")
    allowed = {"topK", "maxBytes", "maxTokens", "maxRelationshipDepth"}
    if set(value) - allowed:
        raise RpcFault(INVALID_PARAMS, "validation", "budgets contains unknown fields")
    raw_top_k = value.get("topK")
    if raw_top_k is not None:
        if not isinstance(raw_top_k, Mapping) or set(raw_top_k) - set(_CONTEXT_V2_SECTIONS):
            raise RpcFault(INVALID_PARAMS, "validation", "budgets.topK is invalid")
        for section, raw_limit in raw_top_k.items():
            if type(raw_limit) is not int or raw_limit < 0 or raw_limit > 1_000:
                raise RpcFault(INVALID_PARAMS, "validation", "budgets.topK is invalid")
            defaults["topK"][section] = raw_limit
    for field, maximum in (("maxBytes", 4 * 1024 * 1024), ("maxTokens", 256_000), ("maxRelationshipDepth", 8)):
        raw = value.get(field)
        if raw is not None:
            if type(raw) is not int or raw < 1 or raw > maximum:
                raise RpcFault(INVALID_PARAMS, "validation", f"budgets.{field} is invalid")
            defaults[field] = raw
    return defaults


def _coerce_context_v2(
    result: Any,
    budgets: Mapping[str, Any],
    *,
    apply_budget: bool = True,
) -> dict[str, Any]:
    """Normalize a native v2 or legacy adapter result into the v2 wire shape."""

    if not isinstance(result, Mapping):
        raise RpcFault(SEMANTIC_ERROR, "context.ask", "semantic context lookup failed")
    source_version = result.get("schemaVersion")
    if source_version not in {1, _CONTEXT_V2_VERSION}:
        raise RpcFault(UNSUPPORTED_PROTOCOL, "context.ask", "semantic context schemaVersion is unsupported")
    revision = result.get("projectRevision")
    if not isinstance(revision, str) or not revision:
        raise RpcFault(SEMANTIC_ERROR, "context.ask", "semantic context lookup failed")

    raw_schema = result.get("schema")
    if isinstance(raw_schema, Mapping):
        raw_models = raw_schema.get("models", [])
    else:
        raw_models = result.get("models", [])
    if not isinstance(raw_models, list):
        raw_models = []

    rules = _context_v2_rules(result, revision)
    sql_examples = _context_v2_sql_examples(result)
    raw_index_status = result.get("indexStatus")
    index_status = _safe_context_v2_index_status(raw_index_status, revision)
    trace = _safe_context_v2_trace(result.get("retrievalTrace"), revision)
    normalized_budgets = _validate_context_v2_budgets(budgets)
    output: dict[str, Any] = {
        "schemaVersion": _CONTEXT_V2_VERSION,
        "projectRevision": revision,
        "schema": {"models": _context_v2_models(raw_models)},
        "relationships": _context_v2_records(result.get("relationships"), ("name", "models", "joinType", "condition", "description")),
        "metrics": _context_v2_metrics(result.get("metrics")),
        "rules": rules,
        "sqlExamples": sql_examples,
        "views": _context_v2_records(result.get("views"), ("name", "statement", "description", "referencedModels", "referencedColumns")),
        "budgets": normalized_budgets,
        "indexStatus": index_status,
        "retrievalSummary": _safe_context_v2_summary(result.get("retrievalSummary")),
        "retrievalTrace": trace,
    }
    return _apply_context_v2_budgets(output) if apply_budget else output


def _context_v2_records(value: Any, allowed: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {key: item[key] for key in allowed if key in item}
        for item in value
        if isinstance(item, Mapping)
    ]


def _context_v2_metrics(value: Any) -> list[dict[str, Any]]:
    records = _context_v2_records(
        value,
        (
            "name", "kind", "expression", "type", "model", "cube", "baseObject",
            "description", "properties", "referencedModels", "referencedColumns",
        ),
    )
    # Normalize legacy metric providers into the v2 discriminated shape.
    for record in records:
        record.setdefault("kind", "measure")
    return records


def _context_v2_models(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    models: list[dict[str, Any]] = []
    # Context v2 is a semantic projection. Physical table names never cross
    # this boundary, even if a legacy/native provider includes one.
    model_keys = ("name", "description", "columns", "primaryKey", "properties")
    column_keys = ("name", "type", "description", "isCalculated", "notNull", "isPrimaryKey", "semanticRole", "expression", "properties")
    for item in value:
        if not isinstance(item, Mapping):
            continue
        model = {key: item[key] for key in model_keys if key in item}
        columns = item.get("columns")
        if isinstance(columns, list):
            model["columns"] = [
                {key: column[key] for key in column_keys if key in column}
                for column in columns
                if isinstance(column, Mapping)
            ]
        models.append(model)
    return models


def _context_v2_rules(result: Mapping[str, Any], revision: str) -> list[dict[str, Any]]:
    raw = result.get("rules")
    if isinstance(raw, list):
        allowed = (
            "id", "text", "referencedModels", "referencedColumns", "sourcePath",
            "ruleType", "priority", "mandatory", "effectiveFrom", "allowedRoles",
        )
        return [{key: item[key] for key in allowed if key in item} for item in raw if isinstance(item, Mapping)]
    # Legacy Wren returns unstructured knowledge strings. They are retained for
    # unrestricted callers, but the restricted projection will drop them
    # because there are no explicit model/column bindings to prove safety.
    knowledge = result.get("knowledge")
    if not isinstance(knowledge, list):
        return []
    rules: list[dict[str, Any]] = []
    for index, item in enumerate(knowledge):
        if not isinstance(item, str) or not item.strip():
            continue
        identity = hashlib.sha256(f"{revision}:rule:{index}:{item}".encode("utf-8")).hexdigest()[:24]
        rules.append({
            "id": f"rule:{identity}",
            "text": item,
            "referencedModels": [],
            "referencedColumns": [],
        })
    return rules


def _context_v2_sql_examples(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("sqlExamples")
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    history = result.get("sqlHistory")
    if not isinstance(history, list):
        return []
    examples: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, Mapping):
            continue
        example = {
            key: item[key]
            for key in (
                "id", "question", "sql", "sourcePath", "language", "tags",
                "reviewed", "dataSource", "roles", "version",
                "referencedModels", "referencedColumns",
            )
            if key in item
        }
        example.setdefault("referencedModels", [])
        example.setdefault("referencedColumns", [])
        if all(isinstance(example.get(key), str) and example[key] for key in ("id", "question", "sql")):
            examples.append(example)
    return examples


def _safe_context_v2_index_status(value: Any, revision: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": "unavailable", "backend": "none", "staleReason": "backendUnavailable"}
    status = value.get("status", value.get("indexStatus"))
    status = {"active": "ready", "staged": "building"}.get(status, status)
    if status not in {"ready", "missing", "stale", "building", "unavailable", "degraded"}:
        status = "unavailable"
    result: dict[str, Any] = {"status": status}
    for key in ("activeRevision", "indexedRevision"):
        raw = value.get(key)
        if isinstance(raw, str) and 1 <= len(raw) <= 256:
            result[key] = raw
    document_count = value.get("documentCount")
    if type(document_count) is int and 0 <= document_count <= 10_000_000:
        result["documentCount"] = document_count
    for key in ("embeddingModelId", "embeddingModelVersion", "lastBuildAt"):
        raw = value.get(key)
        if isinstance(raw, str) and 1 <= len(raw) <= (64 if key == "lastBuildAt" else 256):
            result[key] = raw
    for key in ("embeddingDimension", "indexBuildVersion"):
        raw = value.get(key)
        if type(raw) is int and 0 <= raw <= 1_000_000:
            result[key] = raw
    duration = value.get("buildDurationMs")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and 0 <= duration <= 86_400_000:
        result["buildDurationMs"] = float(duration)
    backend = value.get("backend")
    if backend in {"vector", "lexical", "hybrid", "none"}:
        result["backend"] = backend
    stale_reason = value.get("staleReason")
    stale_reason = {
        "revision_mismatch": "revisionMismatch",
        "revision_not_active": "unknown",
        "revision_not_built": "missing",
        "no_revision": "missing",
        "embedding_config_mismatch": "backendUnavailable",
        "embedding_dimension_mismatch": "backendUnavailable",
        "corrupt_active_pointer": "buildFailed",
        "corrupt_partition": "buildFailed",
        "active_partition_missing": "buildFailed",
        "active_pointer_invalid": "buildFailed",
    }.get(stale_reason, stale_reason)
    if isinstance(stale_reason, str) and stale_reason.startswith("active_pointer_unreadable:"):
        stale_reason = "buildFailed"
    if stale_reason in {"missing", "revisionMismatch", "backendUnavailable", "buildFailed", "unknown"}:
        result["staleReason"] = stale_reason
    result.setdefault("backend", "none")
    if status == "stale":
        result.setdefault("staleReason", "revisionMismatch" if result.get("indexedRevision") not in {None, revision} else "unknown")
    return result


def _safe_context_v2_trace(value: Any, revision: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    safe: list[dict[str, Any]] = []
    allowed_sources = set(_CONTEXT_V2_SECTIONS)
    allowed_types = {"exact", "lexical", "vector", "graph", "ruleBinding", "fallback"}
    allowed_reasons = {"exactMatch", "lexicalMatch", "vectorMatch", "graphExpansion", "ruleBinding", "fallback", "permissionFiltered", "budgetLimited"}
    for item in value[:2_000]:
        if not isinstance(item, Mapping) or item.get("source") not in allowed_sources or item.get("retrievalType") not in allowed_types:
            continue
        trace: dict[str, Any] = {
            "source": item["source"],
            "retrievalType": item["retrievalType"],
            "reasonCode": item.get("reasonCode") if item.get("reasonCode") in allowed_reasons else "fallback",
            "projectRevision": revision,
            "authorizationFiltered": bool(item.get("authorizationFiltered", False)),
        }
        document_id = item.get("documentId")
        if isinstance(document_id, str) and 1 <= len(document_id) <= 512:
            trace["documentId"] = document_id
        relevance = item.get("relevance")
        if isinstance(relevance, (int, float)) and not isinstance(relevance, bool) and 0 <= relevance <= 1:
            trace["relevance"] = float(relevance)
        if isinstance(item.get("selected"), bool):
            trace["selected"] = item["selected"]
        safe.append(trace)
    return safe


def _safe_context_v2_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"candidateCount": 0, "filteredCount": 0, "selectedCount": 0, "latencyMs": 0.0}
    summary: dict[str, Any] = {}
    for key in ("candidateCount", "filteredCount", "selectedCount"):
        raw = value.get(key)
        summary[key] = raw if type(raw) is int and 0 <= raw <= 10_000_000 else 0
    latency = value.get("latencyMs")
    summary["latencyMs"] = (
        float(latency)
        if isinstance(latency, (int, float)) and not isinstance(latency, bool)
        and math.isfinite(latency) and 0 <= latency <= 86_400_000
        else 0.0
    )
    fallback = value.get("fallbackReason")
    if fallback in {"embeddingUnavailable", "vectorSearchFailed", "indexDegraded"}:
        summary["fallbackReason"] = fallback
    return summary


def _apply_context_v2_budgets(value: dict[str, Any]) -> dict[str, Any]:
    """Apply per-section and aggregate budgets with stable tail truncation."""

    budgets = value["budgets"]
    top_k = budgets["topK"]
    schema = value["schema"]
    if isinstance(schema, Mapping) and isinstance(schema.get("models"), list):
        schema["models"] = schema["models"][: top_k["schema"]]
    for section in ("relationships", "metrics", "rules", "sqlExamples", "views"):
        items = value.get(section)
        if isinstance(items, list):
            value[section] = items[: top_k[section]]

    def encoded_size() -> tuple[int, int]:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return len(encoded), max(1, (len(encoded) + 3) // 4)

    max_bytes, max_tokens = budgets["maxBytes"], budgets["maxTokens"]
    # Drop optional results in a deterministic order until both aggregate caps
    # hold. The schema itself is retained as long as possible.
    for section in ("sqlExamples", "rules", "metrics", "relationships", "views"):
        while True:
            size, tokens = encoded_size()
            items = value.get(section)
            if size <= max_bytes and tokens <= max_tokens:
                return value
            if not isinstance(items, list) or not items:
                break
            items.pop()
    return value


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
        "artifactRequest",
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
    if "artifactRequest" in params:
        # Core supplies this capability only after authenticating the public
        # request.  Validate its shape and hard limits here as well as in the
        # query service, because injected query providers may bypass the
        # PostgreSQL executor.
        artifact_request_from_mapping(params["artifactRequest"])
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
