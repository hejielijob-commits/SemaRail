"""Safe runtime preview for Wren View drafts.

This module embeds the already-packaged sidecar policy boundary in the
Semantic Console process. It does not execute SQL through the Console's schema
drivers: every preview still passes semantic/native AST policy, Wren planning,
the manifest-derived physical allowlist, a read-only transaction, timeout,
concurrency, row, and byte limits.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

try:
    from .project import ProjectStore
except ImportError:  # pragma: no cover - direct module loading
    from project import ProjectStore  # type: ignore[no-redef]


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]*$")
_MAX_PREVIEW_ROWS = 200
_MAX_PREVIEW_BYTES = 1_048_576
_MAX_TIMEOUT_MS = 30_000


class PreviewDispatcher(Protocol):
    """Minimal stable sidecar dispatcher seam used by tests and production."""

    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class ViewPreviewError(RuntimeError):
    """A stable preview failure safe to expose through the local REST API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.status = status
        self.details = dict(details) if details is not None else None


def _default_dispatcher(project: ProjectStore) -> PreviewDispatcher | None:
    """Create one singleton sidecar boundary without making startup fragile."""

    try:
        from sidecar import Dispatcher, default_dependencies  # type: ignore[import-not-found]
        from sidecar.datasource_state import load_active_connection  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError):
        return None

    canonical_project = str(project.project_dir)

    def resolve_connection(_staged_project: str, env_name: str) -> Mapping[str, Any] | None:
        # Datasource state is keyed by the canonical project path. Planning is
        # intentionally performed against a temporary draft snapshot, but
        # connection lookup must never use that temporary path's hash.
        return load_active_connection(canonical_project, env_name)

    dependencies = default_dependencies(connection_resolver=resolve_connection)
    return Dispatcher(dependencies)


def _integer(
    value: Any,
    *,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if type(value) is not int or not minimum <= value <= maximum:
        raise ViewPreviewError(
            "INVALID_PARAMS",
            f"{field} is outside the supported range",
            status=400,
        )
    return value


def _error_status(code: str) -> int:
    if code in {"INVALID_PARAMS", "POLICY_DENIED", "SEMANTIC_ERROR", "PROJECT_VALIDATION_FAILED"}:
        return 400
    if code in {"DATABASE_ERROR", "WREN_UNAVAILABLE", "SIDECAR_UNAVAILABLE"}:
        return 503
    if code == "TIMEOUT":
        return 504
    if code == "CANCELLED":
        return 408
    return 500


class ViewPreviewService:
    """Run bounded View previews against an isolated current-draft snapshot."""

    def __init__(
        self,
        project: ProjectStore,
        dispatcher: PreviewDispatcher | None = None,
    ) -> None:
        self.project = project
        self.dispatcher = dispatcher if dispatcher is not None else _default_dispatcher(project)

    def run(self, view_name: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(view_name, str) or not _IDENTIFIER.fullmatch(view_name):
            raise ViewPreviewError("INVALID_PARAMS", "view name is invalid", status=400)
        request = payload if isinstance(payload, Mapping) else {}
        unknown = set(request) - {"limit", "maxBytes", "timeoutMs"}
        if unknown:
            raise ViewPreviewError("INVALID_PARAMS", "preview request contains unknown fields", status=400)
        limit = _integer(
            request.get("limit"),
            field="limit",
            default=100,
            minimum=1,
            maximum=_MAX_PREVIEW_ROWS,
        )
        max_bytes = _integer(
            request.get("maxBytes"),
            field="maxBytes",
            default=512 * 1024,
            minimum=1_024,
            maximum=_MAX_PREVIEW_BYTES,
        )
        timeout_ms = _integer(
            request.get("timeoutMs"),
            field="timeoutMs",
            default=15_000,
            minimum=100,
            maximum=_MAX_TIMEOUT_MS,
        )
        if self.dispatcher is None:
            raise ViewPreviewError(
                "WREN_UNAVAILABLE",
                "safe Wren preview runtime is unavailable",
                status=503,
                details={"retryable": True, "phase": "preview"},
            )

        query_id = f"view-preview-{uuid.uuid4().hex}"
        # The identifier is already constrained to a narrow ASCII grammar. It
        # is still double-quoted so PostgreSQL/Wren preserve case and symbols.
        semantic_sql = f'SELECT * FROM "{view_name}"'
        with self.project.staged_snapshot() as (stage, revision):
            envelope = self.dispatcher.dispatch(
                {
                    "protocolVersion": "1",
                    "id": query_id,
                    "method": "query.run",
                    "params": {
                        "projectDir": str(stage),
                        "question": "Preview Wren View",
                        "semanticSql": semantic_sql,
                        "queryId": query_id,
                        "chartIntent": "table",
                        "timeoutMs": timeout_ms,
                        "maxRows": limit,
                        "previewRows": limit,
                        "maxPreviewBytes": max_bytes,
                        "databaseDsnEnv": "WREN_DATABASE_URL",
                    },
                    "deadlineMs": timeout_ms + 1_000,
                }
            )

        if not isinstance(envelope, Mapping) or envelope.get("ok") is not True:
            raw_error = envelope.get("error") if isinstance(envelope, Mapping) else None
            error = raw_error if isinstance(raw_error, Mapping) else {}
            code = error.get("code") if isinstance(error.get("code"), str) else "INTERNAL_ERROR"
            message = error.get("message") if isinstance(error.get("message"), str) else "view preview failed"
            phase = error.get("phase") if isinstance(error.get("phase"), str) else "preview"
            retryable = bool(error.get("retryable"))
            raise ViewPreviewError(
                code,
                message,
                status=_error_status(code),
                details={"phase": phase, "retryable": retryable},
            )
        result = envelope.get("result")
        if not isinstance(result, Mapping):
            raise ViewPreviewError("INTERNAL_ERROR", "view preview returned an invalid result", status=500)
        response = dict(result)
        response["projectRevision"] = revision
        response["stale"] = self.project.overview().get("revision") != revision
        return response


__all__ = ["PreviewDispatcher", "ViewPreviewError", "ViewPreviewService"]
