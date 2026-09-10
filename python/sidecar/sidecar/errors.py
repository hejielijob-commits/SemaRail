"""Stable errors used at the sidecar protocol boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class RpcError:
    """The versioned, JSON-safe error presented to the Host.

    Error messages are intentionally short and do not contain exception text,
    SQL, paths, credentials, or database responses. This keeps errors stable
    and prevents accidental data disclosure across the process boundary.
    """

    code: str
    phase: str
    message: str
    retryable: bool
    reason_code: str | None = None
    resources: tuple[Mapping[str, str], ...] = ()
    required_permissions: tuple[str, ...] = ()
    suggestion: str | None = None
    origin: str | None = None

    def as_dict(self, *, protocol_version: str = "1", trace_id: str = "") -> dict[str, Any]:
        """Return the wire representation with stable field names."""

        result: dict[str, Any] = {
            "code": self.code,
            "phase": self.phase,
            "message": self.message,
            "retryable": self.retryable,
        }
        if protocol_version == "2":
            reason_code, origin, suggestion = _detail_defaults(self.code, self.phase)
            result.update({
                "reasonCode": self.reason_code or reason_code,
                "resources": [dict(item) for item in self.resources],
                "requiredPermissions": list(self.required_permissions),
                "suggestion": self.suggestion or suggestion,
                "origin": self.origin or origin,
                "traceId": trace_id,
            })
        return result

    def normalized(self) -> "RpcError":
        """Fail closed when an adapter supplies an unknown wire error code."""

        if self.code in STABLE_ERROR_CODES:
            return self
        return RpcError(
            code=INTERNAL_ERROR,
            phase="dispatch",
            message="internal sidecar error",
            retryable=False,
        )


class RpcFault(Exception):
    """An expected failure that can be returned without exposing internals."""

    def __init__(
        self,
        code: str,
        phase: str,
        message: str,
        retryable: bool = False,
        *,
        reason_code: str | None = None,
        resources: Sequence[Mapping[str, str]] = (),
        required_permissions: Sequence[str] = (),
        suggestion: str | None = None,
        origin: str | None = None,
    ) -> None:
        self.error = RpcError(
            code=code,
            phase=phase,
            message=message,
            retryable=retryable,
            reason_code=reason_code,
            resources=tuple(dict(item) for item in resources),
            required_permissions=tuple(required_permissions),
            suggestion=suggestion,
            origin=origin,
        )
        super().__init__(message)


def _detail_defaults(code: str, phase: str) -> tuple[str, str, str]:
    if code == POLICY_DENIED:
        if phase == "policy":
            return "SQL_SAFETY_RESTRICTION", "query-safety", "Revise the query to one permitted read-only statement."
        return "EXPLICIT_DENIAL", "semarail-policy", "Adjust the query scope or ask an administrator to update the applicable policy."
    if code == DATABASE_ERROR:
        return "INTERNAL_FAILURE", "database", "Use the trace identifier to inspect the database execution failure."
    if code == SEMANTIC_ERROR:
        return "SEMANTIC_PARSE_FAILED", "semantic-runtime", "Revise the semantic SQL or inspect the published semantic model."
    if code == TIMEOUT:
        return "QUERY_TIMEOUT", "database", "Reduce the query scope or retry after checking datasource health."
    if code in {SIDECAR_UNAVAILABLE, WREN_UNAVAILABLE}:
        return "CONNECTION_FAILED", "transport", "Check SemaRail runtime health and retry."
    return "INTERNAL_FAILURE", "core", "Contact the SemaRail administrator if the problem persists."


# The names below are intentionally constants rather than an enum. They are
# also useful to Host adapters and make it harder to introduce spelling drift.
INVALID_REQUEST = "INVALID_REQUEST"
SEMANTIC_ERROR = "SEMANTIC_ERROR"
POLICY_DENIED = "POLICY_DENIED"
DATABASE_ERROR = "DATABASE_ERROR"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
SIDECAR_UNAVAILABLE = "SIDECAR_UNAVAILABLE"
UNSUPPORTED_PROTOCOL = "UNSUPPORTED_PROTOCOL"
INVALID_PARAMS = "INVALID_PARAMS"
METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
WREN_UNAVAILABLE = "WREN_UNAVAILABLE"
PROJECT_VALIDATION_FAILED = "PROJECT_VALIDATION_FAILED"
HEALTHCHECK_FAILED = "HEALTHCHECK_FAILED"
INTERNAL_ERROR = "INTERNAL_ERROR"
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
PROTOCOL_ERROR = "PROTOCOL_ERROR"
FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
TRUNCATED_FRAME = "TRUNCATED_FRAME"
RESULT_TOO_LARGE = "RESULT_TOO_LARGE"

# Keep this set synchronized with packages/contract/src/errors.ts. Every error
# is normalized against it at the final response boundary, including errors
# raised by injected adapters.
STABLE_ERROR_CODES = frozenset(
    {
        SEMANTIC_ERROR,
        POLICY_DENIED,
        DATABASE_ERROR,
        TIMEOUT,
        CANCELLED,
        SIDECAR_UNAVAILABLE,
        UNSUPPORTED_PROTOCOL,
        INVALID_PARAMS,
        METHOD_NOT_FOUND,
        WREN_UNAVAILABLE,
        PROJECT_VALIDATION_FAILED,
        HEALTHCHECK_FAILED,
        FRAME_TOO_LARGE,
        TRUNCATED_FRAME,
        INVALID_REQUEST,
        PROTOCOL_ERROR,
        UNSUPPORTED_VERSION,
        INTERNAL_ERROR,
        RESULT_TOO_LARGE,
    }
)
