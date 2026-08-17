"""Lazy Wren 0.13.2 context adapter.

The sidecar deliberately imports Wren only when a request needs it.  The
process can therefore start, answer health probes, and expose a stable
``WREN_UNAVAILABLE`` error even when the optional Wren runtime is not present.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import re
from collections.abc import Callable, Mapping
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

from .errors import (
    INVALID_PARAMS,
    PROJECT_VALIDATION_FAILED,
    RpcFault,
    SEMANTIC_ERROR,
    WREN_UNAVAILABLE,
)
from .protocol import PROTOCOL_VERSION


WREN_PACKAGE_NAME = "wrenai"
WREN_SUPPORTED_VERSION = "0.13.2"
_PROJECT_FILE = "wren_project.yml"
_IGNORED_REVISION_DIRS = frozenset({".git", ".wren", "__pycache__", "target"})

ModuleLoader = Callable[[str], ModuleType]
VersionProvider = Callable[[], str | None]
ContextRetriever = Callable[[dict[str, Any], str, Path], Any]
SchemaDescriber = Callable[[dict[str, Any]], Any]
EngineFactory = Callable[..., Any]


def _installed_wren_version() -> str | None:
    """Read package metadata without importing Wren's heavy engine modules."""

    try:
        value = metadata.version(WREN_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return None
    return value if isinstance(value, str) and value else None


class LazyWrenAdapter:
    """Adapter for the supported Wren context validation/build functions.

    Wren's public APIs used here are ``wren.context.validate_project``,
    ``wren.context.build_json``, ``WrenMemory.get_context/describe_schema``,
    and ``WrenEngine.dry_plan``. Import, context retrieval, schema description,
    and engine creation all have explicit seams for tests and embedded
    runtimes; callers need none of them in production.
    """

    def __init__(
        self,
        module_loader: ModuleLoader | None = None,
        version_provider: VersionProvider | None = None,
        context_retriever: ContextRetriever | None = None,
        schema_describer: SchemaDescriber | None = None,
        engine_factory: EngineFactory | None = None,
        *,
        expected_version: str = WREN_SUPPORTED_VERSION,
        logger: logging.Logger | None = None,
    ) -> None:
        self._module_loader = module_loader or importlib.import_module
        self._version_provider = version_provider or self._discover_version
        self._context_retriever = context_retriever
        self._schema_describer = schema_describer
        self._engine_factory = engine_factory
        self.expected_version = expected_version
        self.logger = logger or logging.getLogger("sidecar.wren")
        self._context: ModuleType | None = None
        self._version: str | None | object = _UNSET

    def health(self) -> dict[str, Any]:
        """Return process/protocol health plus Wren availability and version.

        A missing Wren runtime is a degraded dependency, not a dead sidecar;
        health remains an ``ok`` response so the Host can distinguish process
        liveness from runtime readiness.
        """

        version = self._safe_version()
        available = self._context_available()
        return {
            "status": "ok",
            "protocolVersion": PROTOCOL_VERSION,
            "wrenAvailable": available,
            "wrenVersion": version,
        }

    def validate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Validate/build a Wren project and return safe aggregate counts.

        The project path is an input only.  It is never copied into the result,
        errors, or logs.  Wren's individual issue paths/messages are therefore
        intentionally reduced to counts at this process boundary.
        """

        project_path = self._project_path(params)
        context = self._load_context()
        validate_project = getattr(context, "validate_project", None)
        build_json = getattr(context, "build_json", None)
        if not callable(validate_project) or not callable(build_json):
            raise RpcFault(
                WREN_UNAVAILABLE,
                "project.validate",
                "Wren context validation APIs are unavailable",
                retryable=False,
            )

        revision = _project_revision(project_path)
        error_count = 0
        warning_count = 0
        try:
            issues = validate_project(project_path)
            error_count, warning_count = _count_validation_issues(issues)
        except RpcFault:
            raise
        except Exception as exc:
            # Deliberately do not log or return ``exc``: Wren exceptions can
            # contain DSNs, SQL, credentials, and absolute project paths.
            raise RpcFault(
                PROJECT_VALIDATION_FAILED,
                "project.validate",
                "Wren project validation failed",
                retryable=False,
            ) from exc

        try:
            # ``build_json`` is the supported 0.13.2 context build API. The
            # manifest itself does not cross the sidecar boundary in this
            # first work package; calling it verifies the build path without
            # exposing model SQL or source details.
            build_json(project_path)
        except RpcFault:
            raise
        except Exception:
            # Validation can produce useful structural errors while a build
            # still fails on a malformed semantic file. Count that failure but
            # keep the response JSON-safe and path-free.
            error_count += 1

        return {
            "valid": error_count == 0,
            "errorCount": error_count,
            "warningCount": warning_count,
            "projectRevision": revision,
        }

    def ask(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Build structured, version-one semantic context for a question."""

        project_path = self._project_path(params, phase="context.ask")
        question = params.get("question")
        if not isinstance(question, str) or not question.strip():
            raise RpcFault(
                INVALID_PARAMS,
                "validation",
                "question is required",
                retryable=False,
            )
        manifest = self._build_manifest(project_path, phase="context.ask")
        revision = _project_revision(project_path, phase="context.ask")
        try:
            summary, knowledge = self._context_details(
                manifest,
                question,
                project_path,
            )
            result: dict[str, Any] = {
                "schemaVersion": 1,
                "projectRevision": revision,
                "models": _semantic_models(manifest, project_path),
                "relationships": _semantic_relationships(manifest, project_path),
            }
            views = _semantic_views(manifest, project_path)
            if views:
                result["views"] = views
            if summary:
                result["summary"] = summary
            if knowledge:
                result["knowledge"] = knowledge
            return result
        except RpcFault:
            raise
        except Exception as exc:
            raise RpcFault(
                SEMANTIC_ERROR,
                "context.ask",
                "semantic context lookup failed",
                retryable=False,
            ) from exc

    def dry_plan(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Transform semantic SQL through Wren without opening a database."""

        project_path = self._project_path(params, phase="query.dryPlan")
        semantic_sql = params.get("semanticSql")
        if not isinstance(semantic_sql, str) or not semantic_sql.strip():
            raise RpcFault(
                INVALID_PARAMS,
                "validation",
                "semanticSql is required",
                retryable=False,
            )
        manifest = self._build_manifest(project_path, phase="query.dryPlan")
        data_source = manifest.get("dataSource")
        if not isinstance(data_source, str) or not data_source.strip():
            raise RpcFault(
                SEMANTIC_ERROR,
                "query.dryPlan",
                "Wren project data source is missing",
                retryable=False,
            )
        try:
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            manifest_str = base64.b64encode(manifest_bytes).decode("ascii")
            factory = self._engine_factory or self._load_engine_factory()
            engine = factory(
                manifest_str=manifest_str,
                data_source=data_source.lower(),
                connection_info={},
            )
            try:
                native_sql = engine.dry_plan(semantic_sql)
            finally:
                close = getattr(engine, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        except RpcFault:
            raise
        except Exception as exc:
            # Wren planning errors commonly contain the submitted SQL. Never
            # copy them into logs or the stable error payload.
            raise RpcFault(
                SEMANTIC_ERROR,
                "query.dryPlan",
                "semantic SQL planning failed",
                retryable=False,
            ) from exc
        if not isinstance(native_sql, str) or not native_sql.strip():
            raise RpcFault(
                SEMANTIC_ERROR,
                "query.dryPlan",
                "semantic SQL planning failed",
                retryable=False,
            )
        return {
            "semanticSql": semantic_sql,
            "nativeSql": native_sql,
            "projectRevision": _project_revision(
                project_path,
                phase="query.dryPlan",
            ),
        }

    # Explicit alias for adapters that name the operation after the Wren API.
    validate_project = validate

    def _project_path(
        self,
        params: Mapping[str, Any],
        *,
        phase: str = "project.validate",
    ) -> Path:
        project_dir = params.get("projectDir")
        if not isinstance(project_dir, str) or not project_dir.strip():
            raise RpcFault(
                INVALID_PARAMS,
                "validation",
                "projectDir is required",
                retryable=False,
            )
        try:
            project_path = Path(project_dir).expanduser()
            if not project_path.is_dir():
                raise ValueError
            # Resolving is useful to Wren's path-based loaders, but the
            # resolved path is never returned or logged.
            return project_path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise RpcFault(
                PROJECT_VALIDATION_FAILED,
                phase,
                "project directory is unavailable",
                retryable=False,
            ) from exc

    def _build_manifest(self, project_path: Path, *, phase: str) -> dict[str, Any]:
        context = self._load_context(phase=phase)
        build_json = getattr(context, "build_json", None)
        if not callable(build_json):
            raise RpcFault(
                WREN_UNAVAILABLE,
                phase,
                "Wren context build API is unavailable",
                retryable=False,
            )
        try:
            manifest = build_json(project_path)
        except Exception as exc:
            raise RpcFault(
                SEMANTIC_ERROR,
                phase,
                "Wren project build failed",
                retryable=False,
            ) from exc
        if not isinstance(manifest, dict):
            raise RpcFault(
                SEMANTIC_ERROR,
                phase,
                "Wren project build failed",
                retryable=False,
            )
        return manifest

    def _context_details(
        self,
        manifest: dict[str, Any],
        question: str,
        project_path: Path,
    ) -> tuple[str | None, list[str]]:
        context_result: Any = None
        if self._context_retriever is not None:
            context_result = self._context_retriever(
                manifest,
                question,
                project_path,
            )
        else:
            try:
                memory = self._module_loader("wren.memory")
                direct = getattr(memory, "get_context", None)
                if callable(direct):
                    context_result = direct(manifest, question)
                else:
                    memory_class = getattr(memory, "WrenMemory", None)
                    memory_path = project_path / ".wren" / "memory"
                    if callable(memory_class) and memory_path.is_dir():
                        instance = memory_class(
                            path=memory_path
                        )
                        get_context = getattr(instance, "get_context", None)
                        if callable(get_context):
                            context_result = get_context(manifest, question)
            except Exception:
                context_result = None

        summary, knowledge = _normalize_context_result(
            context_result,
            project_path,
        )
        if summary or knowledge:
            return summary, knowledge

        description: Any = None
        try:
            if self._schema_describer is not None:
                description = self._schema_describer(manifest)
            else:
                try:
                    memory = self._module_loader("wren.memory")
                    memory_class = getattr(memory, "WrenMemory", None)
                    describe = getattr(memory_class, "describe_schema", None)
                    if callable(describe):
                        description = describe(manifest)
                except Exception:
                    description = None
                if description is None:
                    indexer = self._module_loader("wren.memory.schema_indexer")
                    describe = getattr(indexer, "describe_schema", None)
                    if callable(describe):
                        description = describe(manifest)
        except Exception:
            description = None
        safe_description = _safe_text(description, project_path, maximum=16_000)
        return safe_description, []

    def _load_engine_factory(self) -> EngineFactory:
        try:
            module = self._module_loader("wren.engine")
            factory = getattr(module, "WrenEngine", None)
        except Exception as exc:
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.dryPlan",
                "Wren query planner is unavailable",
                retryable=True,
            ) from exc
        if not callable(factory):
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.dryPlan",
                "Wren query planner is unavailable",
                retryable=False,
            )
        return factory

    def _load_context(self, *, phase: str = "project.validate") -> ModuleType:
        if self._context is not None:
            return self._context
        try:
            context = self._module_loader("wren.context")
        except (ImportError, ModuleNotFoundError) as exc:
            raise RpcFault(
                WREN_UNAVAILABLE,
                phase,
                "Wren runtime is unavailable",
                retryable=True,
            ) from exc
        except Exception as exc:
            # Import hooks may fail with arbitrary runtime exceptions. Keep
            # their details out of both logs and wire responses.
            raise RpcFault(
                WREN_UNAVAILABLE,
                phase,
                "Wren runtime is unavailable",
                retryable=True,
            ) from exc
        if not isinstance(context, ModuleType):
            # Test injectors may return a module-like object; accept it below
            # while retaining a precise type for the normal import path.
            if not hasattr(context, "__dict__"):
                raise RpcFault(
                    WREN_UNAVAILABLE,
                    phase,
                    "Wren context module is unavailable",
                    retryable=False,
                )
        self._context = context
        return context

    def _context_available(self) -> bool:
        try:
            context = self._load_context()
        except RpcFault:
            return False
        return callable(getattr(context, "validate_project", None)) and callable(
            getattr(context, "build_json", None)
        )

    def _safe_version(self) -> str | None:
        if self._version is not _UNSET:
            return self._version  # type: ignore[return-value]
        try:
            value = self._version_provider()
        except Exception:
            value = None
        if not isinstance(value, str) or not value:
            value = None
        self._version = value
        return value

    def _discover_version(self) -> str | None:
        value = _installed_wren_version()
        if value is not None:
            return value
        # Editable/source-checkout fakes and Wren development installs expose
        # ``__version__`` from the top-level module instead of package
        # metadata. This import remains lazy and failures are sanitized.
        try:
            module = self._module_loader("wren")
            candidate = getattr(module, "__version__", None)
        except Exception:
            return None
        return candidate if isinstance(candidate, str) and candidate else None


_UNSET = object()

_DSN_RE = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|snowflake|redshift|clickhouse|"
    r"trino|mssql|oracle|duckdb|databricks)://[^\s\]\[{}]+",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"\b(password|passwd|pwd|token|api[_-]?key|secret)\s*([:=])\s*"
    r"[^\s,;]+",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s\]\[{}]+")


def _safe_text(
    value: Any,
    project_path: Path,
    *,
    maximum: int,
) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    candidates = {
        str(project_path),
        project_path.as_posix(),
        str(project_path).replace("\\", "/"),
    }
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            text = text.replace(candidate, "[project]")
    text = _DSN_RE.sub("[redacted-dsn]", text)
    text = _SECRET_RE.sub(r"\1\2[redacted]", text)
    text = _WINDOWS_PATH_RE.sub("[redacted-path]", text)
    return text[:maximum] if text else None


def _description(value: Mapping[str, Any], project_path: Path) -> str | None:
    direct = _safe_text(value.get("description"), project_path, maximum=4_000)
    if direct:
        return direct
    properties = value.get("properties")
    if isinstance(properties, Mapping):
        return _safe_text(
            properties.get("description"),
            project_path,
            maximum=4_000,
        )
    return None


def _semantic_models(
    manifest: Mapping[str, Any],
    project_path: Path,
) -> list[dict[str, Any]]:
    raw_models = manifest.get("models")
    if not isinstance(raw_models, list):
        return []
    models: list[dict[str, Any]] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            continue
        name = raw_model.get("name")
        if not isinstance(name, str) or not name:
            continue
        primary_key = raw_model.get("primaryKey")
        columns: list[dict[str, Any]] = []
        raw_columns = raw_model.get("columns")
        for raw_column in raw_columns if isinstance(raw_columns, list) else []:
            if not isinstance(raw_column, Mapping):
                continue
            column_name = raw_column.get("name")
            if not isinstance(column_name, str) or not column_name:
                continue
            column_type = raw_column.get("type")
            if not isinstance(column_type, str) or not column_type:
                column_type = "UNKNOWN"
            column: dict[str, Any] = {
                "name": column_name,
                "type": column_type,
            }
            description = _description(raw_column, project_path)
            if description:
                column["description"] = description
            for source, target in (
                ("isCalculated", "isCalculated"),
                ("notNull", "notNull"),
            ):
                flag = raw_column.get(source)
                if isinstance(flag, bool):
                    column[target] = flag
            if isinstance(primary_key, str):
                column["isPrimaryKey"] = column_name == primary_key
            elif isinstance(primary_key, list):
                column["isPrimaryKey"] = column_name in primary_key
            expression = _safe_text(
                raw_column.get("expression"),
                project_path,
                maximum=16_000,
            )
            if expression:
                column["expression"] = expression
            columns.append(column)

        model: dict[str, Any] = {"name": name, "columns": columns}
        description = _description(raw_model, project_path)
        if description:
            model["description"] = description
        table_reference = raw_model.get("tableReference")
        if isinstance(table_reference, Mapping):
            table = _safe_text(
                table_reference.get("table"),
                project_path,
                maximum=256,
            )
            if table:
                model["table"] = table
        if isinstance(primary_key, str) and primary_key:
            model["primaryKey"] = primary_key
        models.append(model)
    return models


def _semantic_relationships(
    manifest: Mapping[str, Any],
    project_path: Path,
) -> list[dict[str, Any]]:
    raw_relationships = manifest.get("relationships")
    if not isinstance(raw_relationships, list):
        return []
    relationships: list[dict[str, Any]] = []
    for raw in raw_relationships:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name")
        models = raw.get("models")
        join_type = raw.get("joinType")
        condition = _safe_text(
            raw.get("condition"),
            project_path,
            maximum=16_000,
        )
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(models, list)
            or len(models) != 2
            or not all(isinstance(model, str) and model for model in models)
            or not isinstance(join_type, str)
            or not join_type
            or not condition
        ):
            continue
        relationship: dict[str, Any] = {
            "name": name,
            "models": list(models),
            "joinType": join_type,
            "condition": condition,
        }
        description = _description(raw, project_path)
        if description:
            relationship["description"] = description
        relationships.append(relationship)
    return relationships


def _semantic_views(
    manifest: Mapping[str, Any],
    project_path: Path,
) -> list[dict[str, Any]]:
    raw_views = manifest.get("views")
    if not isinstance(raw_views, list):
        return []
    views: list[dict[str, Any]] = []
    for raw in raw_views:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name")
        statement = _safe_text(
            raw.get("statement"),
            project_path,
            maximum=64_000,
        )
        if not isinstance(name, str) or not name or not statement:
            continue
        view: dict[str, Any] = {"name": name, "statement": statement}
        description = _description(raw, project_path)
        if description:
            view["description"] = description
        views.append(view)
    return views


def _normalize_context_result(
    result: Any,
    project_path: Path,
) -> tuple[str | None, list[str]]:
    if isinstance(result, str):
        return _safe_text(result, project_path, maximum=16_000), []
    if not isinstance(result, Mapping):
        return None, []
    summary = _safe_text(
        result.get("schema", result.get("summary")),
        project_path,
        maximum=16_000,
    )
    raw_items = result.get("results", result.get("knowledge", []))
    knowledge: list[str] = []
    if isinstance(raw_items, list):
        for item in raw_items[:20]:
            value = item.get("text") if isinstance(item, Mapping) else item
            safe = _safe_text(value, project_path, maximum=16_000)
            if safe:
                knowledge.append(safe)
    return summary, knowledge


def _count_validation_issues(issues: Any) -> tuple[int, int]:
    """Count Wren ``ValidationError`` instances without copying their fields."""

    if issues is None:
        return 0, 0
    if isinstance(issues, Mapping):
        # A fake adapter may return categorized lists; accepting this shape
        # keeps the seam useful without exposing any issue content.
        errors = issues.get("errors", [])
        warnings = issues.get("warnings", [])
        return _safe_count(errors), _safe_count(warnings)
    try:
        iterator = iter(issues)
    except TypeError:
        return 1, 0
    errors = 0
    warnings = 0
    for issue in iterator:
        level: Any = None
        if isinstance(issue, Mapping):
            level = issue.get("level")
        else:
            level = getattr(issue, "level", None)
        if isinstance(level, str) and level.lower() == "warning":
            warnings += 1
        else:
            # Unknown/malformed levels fail closed as errors.
            errors += 1
    return errors, warnings


def _safe_count(value: Any) -> int:
    try:
        count = len(value)
    except (TypeError, OverflowError):
        return 1
    return count if isinstance(count, int) and count >= 0 else 1


def _project_revision(
    project_path: Path,
    *,
    phase: str = "project.validate",
) -> str:
    """Hash source file names/content deterministically without exposing paths."""

    digest = hashlib.sha256()
    try:
        files: list[tuple[str, Path]] = []
        for candidate in project_path.rglob("*"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(project_path)
            if any(part in _IGNORED_REVISION_DIRS for part in relative.parts):
                continue
            files.append((relative.as_posix(), candidate))
        for relative_name, candidate in sorted(files, key=lambda item: item[0]):
            name_bytes = relative_name.encode("utf-8")
            digest.update(len(name_bytes).to_bytes(4, "big"))
            digest.update(name_bytes)
            with candidate.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(len(chunk).to_bytes(4, "big"))
                    digest.update(chunk)
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise RpcFault(
            PROJECT_VALIDATION_FAILED,
            phase,
            "project revision could not be computed",
            retryable=False,
        ) from exc
    return f"sha256:{digest.hexdigest()}"


WrenAdapter = LazyWrenAdapter


def default_dependencies(*, logger: logging.Logger | None = None) -> Any:
    """Build the CLI's default dependency set around one lazy adapter."""

    # Import locally to keep this module's Wren-facing boundary independent of
    # dispatch construction and to avoid a circular import at module import.
    from .dispatch import SidecarDependencies

    adapter = LazyWrenAdapter(logger=logger)
    return SidecarDependencies(
        project_validator=adapter,
        context_provider=adapter,
        query_planner=adapter,
        health_provider=adapter.health,
    )
