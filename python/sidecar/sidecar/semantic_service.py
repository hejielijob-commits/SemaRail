"""Stable SemaRail semantic operations over the pinned upstream runtime.

This module is the product-owned boundary between SemaRail transports and the
semantic implementation.  It intentionally keeps WrenAI's project format and
public runtime APIs in place; it does not introduce a second semantic model or
copy upstream implementation code.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .errors import INTERNAL_ERROR, INVALID_PARAMS, RpcFault
from .wren_adapter import LazyWrenAdapter


SEMANTIC_SCHEMA_VERSION = 1
MAX_QUESTION_CHARS = 16_000
MAX_SEMANTIC_SQL_CHARS = 64_000
MAX_SEMANTIC_RESULT_BYTES = 2 * 1024 * 1024


class SemanticRuntime(Protocol):
    """Minimum runtime behavior consumed by SemaRail's semantic service."""

    def validate(self, params: Mapping[str, Any]) -> dict[str, Any]: ...

    def describe(self, params: Mapping[str, Any]) -> dict[str, Any]: ...

    def ask(self, params: Mapping[str, Any]) -> dict[str, Any]: ...

    def dry_plan(self, params: Mapping[str, Any]) -> dict[str, Any]: ...


class SemanticService:
    """Project-pinned semantic service used by SemaRail transports.

    The project path is fixed when the service starts and is never exposed as
    an Agent-selectable argument.  Results remain the bounded JSON projections
    produced from the existing WrenAI project and runtime structures.
    """

    def __init__(
        self,
        project: str | Path,
        *,
        runtime: SemanticRuntime | None = None,
    ) -> None:
        try:
            project_path = Path(project).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("project directory is unavailable") from exc
        if not project_path.is_dir():
            raise ValueError("project directory is unavailable")
        self.project_path = project_path
        self.runtime = runtime or LazyWrenAdapter()

    def prepare(self) -> None:
        """Warm the optional runtime before a stdio transport starts."""

        prepare = getattr(self.runtime, "prepare", None)
        if callable(prepare):
            prepare()

    def validate_project(self) -> dict[str, Any]:
        """Validate the fixed semantic project without returning its path."""

        return self._result(
            self.runtime.validate(self._project_params()),
            phase="project.validate",
            required={
                "valid": bool,
                "errorCount": int,
                "warningCount": int,
                "projectRevision": str,
            },
        )

    def list_models(self) -> dict[str, Any]:
        """Return the fixed project's models, relationships, and views."""

        return self._result(
            self.runtime.describe(self._project_params()),
            phase="project.describe",
            required={
                "projectRevision": str,
                "models": list,
                "relationships": list,
            },
        )

    def get_context(self, question: str) -> dict[str, Any]:
        """Resolve bounded semantic context for one natural-language question."""

        question = _required_text(question, "question", MAX_QUESTION_CHARS)
        return self._result(
            self.runtime.ask({**self._project_params(), "question": question}),
            phase="context.ask",
            required={
                "projectRevision": str,
                "models": list,
                "relationships": list,
            },
        )

    def plan_query(self, semantic_sql: str) -> dict[str, Any]:
        """Dry-plan one read-only semantic query without database access."""

        semantic_sql = _required_text(
            semantic_sql,
            "semanticSql",
            MAX_SEMANTIC_SQL_CHARS,
        )
        return self._result(
            self.runtime.dry_plan(
                {**self._project_params(), "semanticSql": semantic_sql}
            ),
            phase="query.dryPlan",
            required={
                "semanticSql": str,
                "nativeSql": str,
                "allowedPhysical": Mapping,
                "projectRevision": str,
            },
        )

    def _project_params(self) -> dict[str, str]:
        return {"projectDir": str(self.project_path)}

    @staticmethod
    def _result(
        value: Any,
        *,
        phase: str,
        required: Mapping[str, type[Any]],
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise RpcFault(
                INTERNAL_ERROR,
                phase,
                "semantic service returned an invalid result",
                retryable=False,
            )
        result = dict(value)
        result.setdefault("schemaVersion", SEMANTIC_SCHEMA_VERSION)
        if result.get("schemaVersion") != SEMANTIC_SCHEMA_VERSION or any(
            key not in result or not isinstance(result[key], expected)
            for key, expected in required.items()
        ):
            raise RpcFault(
                INTERNAL_ERROR,
                phase,
                "semantic service returned an invalid result",
                retryable=False,
            )
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RpcFault(
                INTERNAL_ERROR,
                phase,
                "semantic service returned an invalid result",
                retryable=False,
            ) from exc
        if len(encoded) > MAX_SEMANTIC_RESULT_BYTES:
            raise RpcFault(
                INTERNAL_ERROR,
                phase,
                "semantic service returned an oversized result",
                retryable=False,
            )
        return result


def _required_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RpcFault(
            INVALID_PARAMS,
            "validation",
            f"{name} must be a non-empty string no longer than {maximum} characters",
            retryable=False,
        )
    return value.strip()


__all__ = [
    "MAX_QUESTION_CHARS",
    "MAX_SEMANTIC_SQL_CHARS",
    "MAX_SEMANTIC_RESULT_BYTES",
    "SEMANTIC_SCHEMA_VERSION",
    "SemanticRuntime",
    "SemanticService",
]
