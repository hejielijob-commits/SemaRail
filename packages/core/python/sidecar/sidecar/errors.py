"""Stable errors used at the sidecar protocol boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

    def as_dict(self) -> dict[str, Any]:
        """Return the wire representation with stable field names."""

        return {
            "code": self.code,
            "phase": self.phase,
            "message": self.message,
            "retryable": self.retryable,
        }

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
    ) -> None:
        self.error = RpcError(
            code=code,
            phase=phase,
            message=message,
            retryable=retryable,
        )
        super().__init__(message)


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
