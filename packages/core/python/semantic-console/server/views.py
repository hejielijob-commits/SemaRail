"""Structured CRUD for Wren v5 view metadata.

Views are source files, not database rows.  A view's definition lives in
``views/<name>/metadata.yml`` and its statement may be moved to a sibling
``sql.yml`` file.  The latter is the canonical Wren v5 extension for long or
editor-owned SQL and takes precedence when it contains a non-empty
``statement``.  This module keeps that file-level behaviour while exposing a
small, JSON-safe resource for the semantic console.
"""

from __future__ import annotations

import contextlib
import io
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

try:
    from .models import is_sensitive_key
    from .project import ProjectError, ProjectStore
except ImportError:  # pragma: no cover - direct module loading
    from models import is_sensitive_key  # type: ignore[no-redef]
    from project import ProjectError, ProjectStore  # type: ignore[no-redef]


VIEW_SCHEMA_VERSION = 1
VIEWS_ROOT = "views"
_VIEW_PATH = re.compile(r"^views/([^/]+)/metadata\.yml$", re.IGNORECASE)
_MODEL_PATH = re.compile(r"^models/([^/]+)/metadata\.yml$", re.IGNORECASE)
_CUBE_PATH = re.compile(r"^cubes/([^/]+)/metadata\.ya?ml$", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]*$")
_MAX_NAME = 255
_MAX_STATEMENT_BYTES = 64 * 1024
_STORAGE_VALUES = frozenset({"metadata", "sql"})
_QUERY_SQL_ROOTS = {"Select", "Union", "Intersect", "Except"}
_DENIED_SQL_NODES = {
    "Alter", "Attach", "Call", "Command", "Commit", "Copy", "Create", "Delete",
    "Detach", "Drop", "Grant", "Insert", "Into", "Lock", "Merge", "Prepare",
    "Refresh", "Rollback", "Set", "Transaction", "Truncate", "Update", "Use",
    "Vacuum", "Values",
}
_DANGEROUS_SQL_FUNCTIONS = {
    "current_setting", "set_config", "pg_sleep", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_terminate_backend", "dblink", "dblink_exec", "lo_import",
    "lo_export", "read_csv", "read_parquet", "read_json", "postgres_scan",
    "postgres_query", "mysql_scan", "mysql_query", "load_file",
}

# Keep this list in sync with Wren 0.13.2's Rust DataSource enum.  The file
# connector names do not have a distinct sqlglot grammar and therefore use
# PostgreSQL parsing as a conservative syntax boundary until Wren validates
# the staged project.
_VALID_DIALECTS = frozenset({
    "athena", "bigquery", "canner", "clickhouse", "databricks", "datafusion", "doris",
    "duckdb", "gcs_file", "local_file", "minio_file", "mssql", "mysql", "oracle",
    "postgres", "redshift", "s3_file", "snowflake", "spark", "trino",
})
_FILE_DIALECTS = frozenset({"gcs_file", "local_file", "minio_file", "s3_file"})
_SQLGLOT_DIALECT_ALIASES = {
    "canner": "postgres",
    "datafusion": "postgres",
    "mssql": "tsql",
}
_DSN = re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|clickhouse)://[^\s]+", re.IGNORECASE)


def _text(value: Any, field: str, *, maximum: int = _MAX_NAME, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ProjectError("INVALID_VIEW", f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise ProjectError("INVALID_VIEW", f"{field} is required")
    if len(result) > maximum:
        raise ProjectError("INVALID_VIEW", f"{field} exceeds the permitted length")
    return result


def _view_name(value: Any, *, field: str = "view name") -> str:
    name = _text(value, field, maximum=_MAX_NAME, required=True)
    if not _IDENTIFIER.fullmatch(name):
        raise ProjectError("INVALID_VIEW", f"{field} is not a valid identifier")
    return name


def _statement(value: Any, field: str = "statement") -> str:
    if not isinstance(value, str):
        raise ProjectError("INVALID_VIEW", f"{field} must be a string")
    if not value.strip():
        raise ProjectError("INVALID_VIEW", f"{field} is required")
    if len(value.encode("utf-8")) > _MAX_STATEMENT_BYTES:
        raise ProjectError("STATEMENT_TOO_LARGE", "view statement exceeds the 64 KiB limit")
    return value


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectError("INVALID_VIEW", f"{field} must be an object")
    return dict(value)


def _safe_json(value: Any, *, path: str = "properties", depth: int = 0) -> Any:
    """Copy JSON-safe properties while rejecting secrets and pathological data."""

    if depth > 12:
        raise ProjectError("INVALID_VIEW", f"{path} is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_STATEMENT_BYTES:
            raise ProjectError("INVALID_VIEW", f"{path} exceeds the permitted length")
        if isinstance(value, str) and _DSN.search(value):
            raise ProjectError("CREDENTIALS_NOT_ALLOWED", "credential values cannot be stored in view properties")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectError("INVALID_VIEW", f"{path} must contain finite numbers")
        return value
    if isinstance(value, list):
        if len(value) > 1_000:
            raise ProjectError("INVALID_VIEW", f"{path} contains too many items")
        return [_safe_json(item, path=f"{path}[{index}]", depth=depth + 1) for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        if len(value) > 500:
            raise ProjectError("INVALID_VIEW", f"{path} contains too many fields")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > _MAX_NAME:
                raise ProjectError("INVALID_VIEW", f"{path} contains an invalid key")
            if is_sensitive_key(key):
                raise ProjectError("CREDENTIALS_NOT_ALLOWED", "credential metadata is not allowed")
            result[key] = _safe_json(item, path=f"{path}.{key}", depth=depth + 1)
        return result
    raise ProjectError("INVALID_VIEW", f"{path} must be JSON-safe")


def _source_path(name: str) -> str:
    return f"views/{name}/metadata.yml"


def _sql_path(name: str) -> str:
    return f"views/{name}/sql.yml"


def _yaml(content: str, path: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise ProjectError("INVALID_YAML", f"{path} is not valid YAML") from exc
    if not isinstance(parsed, Mapping):
        raise ProjectError("INVALID_VIEW", f"{path} must contain an object")
    return dict(parsed)


def _read_yaml(project: ProjectStore, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    result = project.read_file(path)
    content = result.get("content")
    if not isinstance(content, str):
        raise ProjectError("INVALID_VIEW", f"{path} does not contain text")
    return _yaml(content, path), result


def _read_optional_yaml(project: ProjectStore, path: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    try:
        return _read_yaml(project, path)
    except ProjectError as exc:
        if exc.code == "FILE_NOT_FOUND":
            return None
        raise


def _dialect(value: Any, field: str = "dialect") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProjectError("INVALID_DIALECT", f"{field} must be a supported dialect")
    result = value.strip().lower()
    if result not in _VALID_DIALECTS:
        raise ProjectError("INVALID_DIALECT", f"unknown dialect '{value}'")
    return result


def _sqlglot_read_dialect(dialect: str | None) -> str:
    if dialect in _FILE_DIALECTS:
        return "postgres"
    return _SQLGLOT_DIALECT_ALIASES.get(dialect or "", dialect or "postgres")


def validate_statement(value: Any, dialect: str | None = None) -> dict[str, Any]:
    """Fail closed unless *value* is one read-only SELECT/set query."""

    sql = _statement(value)
    try:
        from sqlglot import exp, parse  # type: ignore[import-not-found]
        from sqlglot.errors import ErrorLevel  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProjectError("SQL_VALIDATION_UNAVAILABLE", "SQL validation is unavailable") from exc
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            statements = [
                item
                for item in parse(sql, read=_sqlglot_read_dialect(dialect), error_level=ErrorLevel.RAISE)
                if item is not None
            ]
    except Exception as exc:
        raise ProjectError("INVALID_SQL", "view statement could not be parsed") from exc
    if len(statements) != 1 or type(statements[0]).__name__ not in _QUERY_SQL_ROOTS:
        raise ProjectError("INVALID_SQL", "view statement must be one read-only query")
    root = statements[0]
    for node in root.walk():
        if type(node).__name__ in _DENIED_SQL_NODES:
            raise ProjectError("INVALID_SQL", "view statement must be one read-only query")
        if isinstance(node, exp.Func):
            name = str(node.sql_name() or "").lower()
            if name in _DANGEROUS_SQL_FUNCTIONS:
                raise ProjectError("INVALID_SQL", "view statement uses a denied function")
    return {"valid": True, "status": "passed", "message": "view statement is one read-only query"}


def _effective_statement(metadata: Mapping[str, Any], sql_data: Mapping[str, Any] | None, path: str) -> tuple[str, str]:
    """Return the Wren-effective statement and its storage source."""

    if sql_data is not None and "statement" in sql_data:
        candidate = sql_data.get("statement")
        if candidate is not None and not isinstance(candidate, str):
            raise ProjectError("INVALID_VIEW", f"{path.replace('/metadata.yml', '/sql.yml')} statement must be a string")
        if isinstance(candidate, str) and candidate.strip():
            return _statement(candidate, f"{path.replace('/metadata.yml', '/sql.yml')} statement"), "sql"
    candidate = metadata.get("statement")
    return _statement(candidate, f"{path} statement"), "metadata"


def _model_names(project: ProjectStore) -> set[str]:
    names: set[str] = set()
    for item in project.files():
        path = str(item.get("path", ""))
        match = _MODEL_PATH.fullmatch(path)
        if not match:
            continue
        names.add(match.group(1))
        try:
            raw, _ = _read_yaml(project, path)
        except ProjectError:
            continue
        value = raw.get("name")
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
    return names


def _statement_table_names(statement: str, dialect: str | None) -> set[str]:
    """Return statically referenced table names for dependency protection."""

    try:
        from sqlglot import exp, parse_one  # type: ignore[import-not-found]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            root = parse_one(statement, read=_sqlglot_read_dialect(dialect))
    except Exception:
        # Invalid legacy SQL is handled by project validation. Delete remains
        # fail-closed for dependencies we can resolve without guessing.
        return set()
    return {table.name for table in root.find_all(exp.Table) if table.name}


def _view_dependents(project: ProjectStore, snapshot: Mapping[str, Any], view_name: str) -> list[dict[str, str]]:
    """Find semantic objects that would be broken by deleting *view_name*."""

    dependents: list[dict[str, str]] = []
    target = view_name.casefold()
    raw_views = snapshot.get("views")
    if isinstance(raw_views, list):
        for candidate in raw_views:
            if not isinstance(candidate, Mapping) or candidate.get("name") == view_name:
                continue
            statement = candidate.get("statement")
            if not isinstance(statement, str):
                continue
            dialect = candidate.get("dialect") if isinstance(candidate.get("dialect"), str) else None
            if target in {name.casefold() for name in _statement_table_names(statement, dialect)}:
                dependents.append({
                    "kind": "view",
                    "name": str(candidate.get("name", "")),
                    "path": str(candidate.get("sourcePath", "")),
                })

    for file_info in project.files():
        path = str(file_info.get("path", ""))
        if not _CUBE_PATH.fullmatch(path):
            continue
        try:
            raw, _ = _read_yaml(project, path)
        except ProjectError:
            continue
        base = raw.get("base_object")
        if isinstance(base, str) and base.casefold() == target:
            dependents.append({"kind": "cube", "name": str(raw.get("name", Path(path).parent.name)), "path": path})
    return sorted(dependents, key=lambda item: (item["kind"], item["name"].casefold(), item["path"]))


def _validation_error(exc: ProjectError, path: str) -> dict[str, str]:
    return {
        "path": path,
        "code": exc.code,
        "message": exc.safe_message,
        "severity": "error",
    }


def validate_view_payload(
    payload: Mapping[str, Any],
    *,
    view_name: str | None = None,
    existing_view_names: set[str] | None = None,
    model_names: set[str] | None = None,
    default_storage: str = "sql",
) -> dict[str, Any]:
    """Validate a projected view payload without changing project state."""

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(payload, Mapping):
        errors.append({"path": "view", "code": "INVALID_VIEW", "message": "view payload must be an object", "severity": "error"})
        return {"valid": False, "errors": errors, "warnings": warnings, "errorCount": 1, "warningCount": 0}

    raw_name = payload.get("name", view_name)
    name: str | None = None
    try:
        name = _view_name(raw_name)
    except ProjectError as exc:
        errors.append(_validation_error(exc, "name"))
    if view_name is not None and name is not None and name != view_name:
        errors.append({"path": "name", "code": "INVALID_VIEW", "message": "view name cannot be changed from its source path", "severity": "error"})

    storage_value = payload.get("storage", default_storage)
    storage = storage_value.strip().lower() if isinstance(storage_value, str) else None
    if storage not in _STORAGE_VALUES:
        errors.append({"path": "storage", "code": "INVALID_VIEW", "message": "storage must be 'metadata' or 'sql'", "severity": "error"})

    dialect_value = payload.get("dialect")
    try:
        dialect = _dialect(dialect_value)
    except ProjectError as exc:
        dialect = None
        errors.append(_validation_error(exc, "dialect"))

    statement = payload.get("statement")
    try:
        validate_statement(statement, dialect)
    except ProjectError as exc:
        errors.append(_validation_error(exc, "statement"))

    properties = payload.get("properties", {})
    try:
        if properties is not None and not isinstance(properties, Mapping):
            raise ProjectError("INVALID_VIEW", "properties must be an object")
        if properties is not None:
            _safe_json(properties)
    except ProjectError as exc:
        errors.append(_validation_error(exc, "properties"))

    if name is not None:
        if existing_view_names and name in existing_view_names:
            errors.append({"path": "name", "code": "FILE_EXISTS", "message": f"view '{name}' already exists", "severity": "error"})
        if model_names and name in model_names:
            errors.append({"path": "name", "code": "NAME_CONFLICT", "message": f"view name '{name}' conflicts with a model", "severity": "error"})
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "errorCount": len(errors),
        "warningCount": len(warnings),
    }


class ViewStore:
    """Read and update Wren v5 ``views/<name>`` directories."""

    def __init__(self, project: ProjectStore) -> None:
        self.project = project

    def snapshot(self) -> dict[str, Any]:
        views: list[dict[str, Any]] = []
        source_files: list[dict[str, Any]] = []
        seen: set[str] = set()
        file_infos = {
            str(item.get("path")): dict(item)
            for item in self.project.files()
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        }
        for path, file_info in sorted(file_infos.items()):
            match = _VIEW_PATH.fullmatch(path)
            if not match:
                continue
            metadata, _ = _read_yaml(self.project, path)
            name = _view_name(metadata.get("name"), field=f"{path} name")
            if name in seen:
                raise ProjectError("INVALID_VIEW", f"duplicate view name '{name}'")
            sql_path = _sql_path(match.group(1))
            sql_result = _read_optional_yaml(self.project, sql_path)
            sql_data = sql_result[0] if sql_result is not None else None
            statement, storage = _effective_statement(metadata, sql_data, path)
            dialect = _dialect(metadata.get("dialect"), f"{path} dialect")
            properties = metadata.get("properties")
            if properties is None:
                safe_properties: dict[str, Any] = {}
            else:
                safe_properties = _safe_json(_mapping(properties, f"{path} properties"), path=f"{path} properties")
            record: dict[str, Any] = {
                "name": name,
                "sourcePath": path,
                "sqlPath": sql_path if sql_result is not None else None,
                "statement": statement,
                "statementSource": storage,
                "storage": storage,
                "properties": safe_properties,
                "draft": bool(file_info.get("draft")) or bool(sql_result and sql_result[1].get("draft")),
            }
            if dialect is not None:
                record["dialect"] = dialect
            views.append(record)
            seen.add(name)
            source_files.append(file_info)
            if sql_result is not None:
                source_files.append(dict(file_infos.get(sql_path, sql_result[1])))

        model_names = _model_names(self.project)
        conflicts = sorted(name for name in seen if name in model_names)
        if conflicts:
            raise ProjectError("NAME_CONFLICT", f"view name '{conflicts[0]}' conflicts with a model")
        overview = self.project.overview()
        views.sort(key=lambda item: str(item["name"]).lower())
        source_files.sort(key=lambda item: str(item.get("path", "")).lower())
        return {
            "schemaVersion": VIEW_SCHEMA_VERSION,
            "revision": overview["revision"],
            "draftCount": overview["draftCount"],
            "views": views,
            "sourceFiles": source_files,
        }

    def validate(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        view_name = _view_name(name)
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_VIEW", "view payload must be an object")
        snapshot = self.snapshot()
        current = next((item for item in snapshot["views"] if item.get("name") == view_name), None)
        candidate = dict(payload)
        candidate.setdefault("name", view_name)
        if current is not None:
            for field in ("statement", "storage", "properties", "dialect"):
                if field not in candidate and field in current:
                    candidate[field] = current[field]
            existing = {str(item["name"]) for item in snapshot["views"] if item.get("name") != view_name}
        else:
            existing = {str(item["name"]) for item in snapshot["views"]}
        result = validate_view_payload(
            candidate,
            view_name=view_name,
            existing_view_names=existing,
            model_names=_model_names(self.project),
            default_storage=str(current.get("storage")) if current else "sql",
        )
        return result

    def get_view(self, name: str) -> dict[str, Any]:
        view_name = _view_name(name)
        snapshot = self.snapshot()
        view = next((item for item in snapshot["views"] if item.get("name") == view_name), None)
        if view is None:
            raise ProjectError("VIEW_NOT_FOUND", "view was not found")
        return view

    def create_view(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_VIEW", "view payload must be an object")
        name = _view_name(payload.get("name"))
        snapshot = self.snapshot()
        if any(item.get("name") == name or item.get("sourcePath") == _source_path(name) for item in snapshot["views"]):
            raise ProjectError("FILE_EXISTS", "view already exists", {"path": _source_path(name)})
        if name in _model_names(self.project):
            raise ProjectError("NAME_CONFLICT", f"view name '{name}' conflicts with a model")
        return self.save_view(name, payload)

    def save_view(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        view_name = _view_name(name)
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_VIEW", "view payload must be an object")
        snapshot = self.snapshot()
        expected = payload.get("expectedRevision")
        if expected is not None and (not isinstance(expected, str) or not expected.strip()):
            raise ProjectError("INVALID_REVISION", "expectedRevision must be a non-empty string")
        if expected is not None and expected != snapshot["revision"]:
            raise ProjectError("REVISION_CONFLICT", "project changed since views were read", {"revision": snapshot["revision"]})
        current = next((item for item in snapshot["views"] if item.get("name") == view_name), None)
        if current is None:
            existing_names = {str(item["name"]) for item in snapshot["views"]}
            if view_name in existing_names or any(item.get("sourcePath") == _source_path(view_name) for item in snapshot["views"]):
                raise ProjectError("FILE_EXISTS", "view already exists", {"path": _source_path(view_name)})
            metadata_path = _source_path(view_name)
            sql_path = _sql_path(view_name)
            metadata: dict[str, Any] = {}
            sql_data: dict[str, Any] = {}
            effective: dict[str, Any] = dict(payload)
            effective.setdefault("name", view_name)
            effective.setdefault("storage", "sql")
            default_storage = "sql"
        else:
            metadata_path = str(current["sourcePath"])
            sql_path = str(current.get("sqlPath") or (Path(metadata_path).parent / "sql.yml").as_posix())
            metadata, _ = _read_yaml(self.project, metadata_path)
            sql_result = _read_optional_yaml(self.project, sql_path)
            sql_data = sql_result[0] if sql_result is not None else {}
            effective = dict(payload)
            effective.setdefault("name", view_name)
            effective.setdefault("statement", current["statement"])
            effective.setdefault("storage", current.get("storage", "metadata"))
            effective.setdefault("properties", current.get("properties", {}))
            if "dialect" not in effective and "dialect" in current:
                effective["dialect"] = current["dialect"]
            default_storage = str(current.get("storage", "metadata"))

        existing_names = {str(item["name"]) for item in snapshot["views"] if item.get("name") != view_name}
        validation = validate_view_payload(
            effective,
            view_name=view_name,
            existing_view_names=existing_names,
            model_names=_model_names(self.project),
            default_storage=default_storage,
        )
        if validation["errors"]:
            raise ProjectError("INVALID_VIEW", "view contains invalid fields", {"errors": validation["errors"], "warnings": validation["warnings"]})

        statement = _statement(effective["statement"])
        storage = str(effective.get("storage", default_storage)).strip().lower()
        dialect_value = effective.get("dialect")
        dialect = _dialect(dialect_value) if dialect_value is not None else None
        properties_value = effective.get("properties", {})
        properties = _safe_json(_mapping(properties_value, "properties"), path="properties") if properties_value is not None else None

        metadata["name"] = view_name
        if properties is None:
            metadata.pop("properties", None)
        else:
            current_properties = _mapping(metadata.get("properties"), "properties")
            current_properties.update(properties)
            metadata["properties"] = current_properties
        if dialect is None:
            metadata.pop("dialect", None)
        else:
            metadata["dialect"] = dialect

        updates: dict[str, str | None] = {}
        if storage == "sql":
            metadata.pop("statement", None)
            sql_data = dict(sql_data)
            sql_data["statement"] = statement
            updates[metadata_path] = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
            updates[sql_path] = yaml.safe_dump(sql_data, allow_unicode=True, sort_keys=False)
        else:
            metadata["statement"] = statement
            updates[metadata_path] = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
            if current is not None and current.get("sqlPath"):
                updates[sql_path] = None
        self.project.put_files(updates, expected_revision=expected)
        return self.snapshot()

    def delete_view(self, name: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        view_name = _view_name(name)
        payload = payload if isinstance(payload, Mapping) else {}
        snapshot = self.snapshot()
        expected = payload.get("expectedRevision")
        if expected is not None and (not isinstance(expected, str) or not expected.strip()):
            raise ProjectError("INVALID_REVISION", "expectedRevision must be a non-empty string")
        if expected is not None and expected != snapshot["revision"]:
            raise ProjectError("REVISION_CONFLICT", "project changed since views were read", {"revision": snapshot["revision"]})
        current = next((item for item in snapshot["views"] if item.get("name") == view_name), None)
        if current is None:
            raise ProjectError("VIEW_NOT_FOUND", "view was not found")
        dependents = _view_dependents(self.project, snapshot, view_name)
        if dependents:
            raise ProjectError(
                "VIEW_IN_USE",
                "view is referenced by other semantic objects",
                {"dependents": dependents},
            )
        updates: dict[str, str | None] = {str(current["sourcePath"]): None}
        if current.get("sqlPath"):
            updates[str(current["sqlPath"])] = None
        self.project.put_files(updates, expected_revision=expected)
        return self.snapshot()


def validate_view_tree(project_dir: Path) -> list[dict[str, str]]:
    """Structural fallback checks used when Wren is not installed."""

    errors: list[dict[str, str]] = []
    model_names: set[str] = set()
    models_dir = project_dir / "models"
    if models_dir.is_dir():
        for metadata in models_dir.rglob("metadata.yml"):
            model_names.add(metadata.parent.name)
            try:
                raw = yaml.safe_load(metadata.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(raw, Mapping) and isinstance(raw.get("name"), str) and raw["name"].strip():
                model_names.add(raw["name"].strip())

    views_dir = project_dir / "views"
    names: set[str] = set()
    if not views_dir.is_dir():
        return errors
    for metadata_path in sorted(views_dir.rglob("metadata.yml")):
        relative = metadata_path.relative_to(project_dir).as_posix()
        try:
            metadata = _yaml(metadata_path.read_text(encoding="utf-8"), relative)
        except (OSError, ProjectError) as exc:
            if isinstance(exc, ProjectError):
                message = exc.safe_message
            else:
                message = "view metadata could not be read"
            errors.append({"level": "error", "path": relative, "message": message})
            continue
        try:
            name = _view_name(metadata.get("name"), field=f"{relative} name")
        except ProjectError as exc:
            errors.append({"level": "error", "path": relative, "message": exc.safe_message})
            continue
        if name in names or name in model_names:
            errors.append({"level": "error", "path": relative, "message": f"duplicate name '{name}'"})
        names.add(name)
        sql_path = metadata_path.parent / "sql.yml"
        sql_data: dict[str, Any] | None = None
        if sql_path.is_file():
            sql_relative = sql_path.relative_to(project_dir).as_posix()
            try:
                sql_data = _yaml(sql_path.read_text(encoding="utf-8"), sql_relative)
            except (OSError, ProjectError) as exc:
                message = exc.safe_message if isinstance(exc, ProjectError) else "view SQL file could not be read"
                errors.append({"level": "error", "path": sql_relative, "message": message})
        try:
            statement, _ = _effective_statement(metadata, sql_data, relative)
            _dialect(metadata.get("dialect"), f"{relative} dialect")
            validate_statement(statement, _dialect(metadata.get("dialect")))
        except ProjectError as exc:
            errors.append({"level": "error", "path": relative, "message": exc.safe_message})
    return errors


__all__ = [
    "VIEW_SCHEMA_VERSION",
    "ViewStore",
    "validate_statement",
    "validate_view_payload",
    "validate_view_tree",
]
