"""Application service for the Wren Semantic Console REST API."""

from __future__ import annotations

import re
from collections.abc import Mapping
from secrets import token_urlsafe
from typing import Any, Callable

import yaml

try:
    from .drivers import DriverError, datasource_types, driver_for
    from .models import DatasourceRecord, is_sensitive_key, utc_now
    from .project import ProjectError, ProjectStore
except ImportError:  # Direct module loading in a lightweight smoke test.
    from drivers import DriverError, datasource_types, driver_for  # type: ignore[no-redef]
    from models import DatasourceRecord, is_sensitive_key, utc_now  # type: ignore[no-redef]
    from project import ProjectError, ProjectStore  # type: ignore[no-redef]


class ApiServiceError(RuntimeError):
    """A safe error from request validation or a service operation."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.details = details
        self.status = status


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]*$")


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiServiceError("INVALID_PARAMS", f"{name} is required")
    return value.strip()


def _safe_identifier(value: Any, name: str) -> str:
    text = _required_string(value, name)
    if len(text) > 255 or not _IDENTIFIER.fullmatch(text):
        raise ApiServiceError("INVALID_PARAMS", f"{name} is not a valid identifier")
    return text


def _public_error(exc: Exception) -> ApiServiceError:
    if isinstance(exc, ApiServiceError):
        return exc
    if isinstance(exc, ProjectError):
        status = 409 if exc.code == "REVISION_CONFLICT" else 400
        return ApiServiceError(exc.code, exc.safe_message, exc.details, status)
    if isinstance(exc, DriverError):
        status = 503 if exc.code in {"DRIVER_UNAVAILABLE", "CONNECTION_FAILED"} else 400
        return ApiServiceError(exc.code, exc.safe_message, None, status)
    return ApiServiceError("INTERNAL_ERROR", "semantic console operation failed", status=500)


class SemanticConsoleService:
    """Stateful API facade.

    ``ProjectStore`` is injectable so focused tests can use a temporary
    directory and fake Wren/DB adapters without touching a user's project.
    """

    def __init__(
        self,
        project: ProjectStore | None = None,
        *,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.project = project or ProjectStore()
        self.connection_factory = connection_factory
        self.datasources = self.project.datasource_records()

    # ---- simple resources -----------------------------------------------

    def health(self) -> dict[str, Any]:
        wren = self.project.validator.health()
        return {
            "status": "ok",
            "service": "semantic-console",
            "apiVersion": "1",
            "wren": wren,
            "project": self.project.overview(),
            "datasourceCount": len(self.datasources),
        }

    def project_overview(self) -> dict[str, Any]:
        return self.project.overview()

    def datasource_type_list(self) -> list[dict[str, Any]]:
        return datasource_types()

    # ---- datasource CRUD -------------------------------------------------

    def list_datasources(self) -> list[dict[str, Any]]:
        return [record.public() for record in self.datasources.values()]

    def get_datasource(self, datasource_id: str) -> dict[str, Any]:
        return self._record(datasource_id).public()

    def create_datasource(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ApiServiceError("INVALID_PARAMS", "request body must be an object")
        kind = str(payload.get("type", payload.get("datasource", ""))).strip().lower()
        if kind == "postgresql":
            kind = "postgres"
        if kind not in {item.get("type") for item in datasource_types()} and kind not in {"postgres", "mysql"}:
            raise ApiServiceError("UNSUPPORTED_DATASOURCE", "datasource type is not recognized")
        name = str(payload.get("name", kind.title())).strip()
        if not name or len(name) > 120:
            raise ApiServiceError("INVALID_PARAMS", "datasource name is required")
        connection = self._connection_payload(payload)
        record = DatasourceRecord(token_urlsafe(12), name, kind, connection)
        self.datasources[record.id] = record
        if self.project.active_datasource_id is None:
            self.project.active_datasource_id = record.id
        self.project.save_datasources()
        return record.public()

    def update_datasource(self, datasource_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = self._record(datasource_id)
        if not isinstance(payload, Mapping):
            raise ApiServiceError("INVALID_PARAMS", "request body must be an object")
        if "name" in payload:
            name = _required_string(payload.get("name"), "name")
            if len(name) > 120:
                raise ApiServiceError("INVALID_PARAMS", "datasource name is too long")
            record.name = name
        if "type" in payload or "datasource" in payload:
            kind = str(payload.get("type", payload.get("datasource", record.type))).strip().lower()
            if kind == "postgresql":
                kind = "postgres"
            if kind not in {"postgres", "mysql"}:
                raise ApiServiceError("UNSUPPORTED_DATASOURCE", "datasource type is not supported by this MVP")
            record.type = kind
        incoming = self._connection_payload(payload)
        # Updates are patch-like.  Omitted password means "keep existing" and
        # omitted host/database/etc. preserve the prior profile; a null value
        # explicitly clears it.  No response path ever emits a secret value.
        merged = dict(record.connection)
        merged.update(incoming)
        record.connection = merged
        record.updated_at = utc_now()
        self.project.save_datasources()
        return record.public()

    def delete_datasource(self, datasource_id: str) -> dict[str, Any]:
        self._record(datasource_id)
        del self.datasources[datasource_id]
        if self.project.active_datasource_id == datasource_id:
            self.project.active_datasource_id = next(iter(self.datasources), None)
        self.project.save_datasources()
        return {"id": datasource_id, "deleted": True}

    def activate_datasource(self, datasource_id: str) -> dict[str, Any]:
        record = self._record(datasource_id)
        self.project.active_datasource_id = record.id
        self.project.save_datasources()
        return {"activeDatasource": record.public(), "project": self.project.overview()}

    def test_datasource(self, datasource_id: str) -> dict[str, Any]:
        record = self._record(datasource_id)
        try:
            driver = driver_for(record.type, self.connection_factory)
            result = driver.test_connection(record.connection)
        except Exception as exc:
            error = _public_error(exc)
            result = {"ok": False, "code": error.code, "message": error.safe_message}
        record.last_test = {"ok": bool(result.get("ok")), "at": utc_now(), **({"latencyMs": result["latencyMs"]} if "latencyMs" in result else {})}
        record.updated_at = utc_now()
        self.project.save_datasources()
        return result

    def datasource_schemas(self, datasource_id: str) -> list[dict[str, Any]]:
        record = self._record(datasource_id)
        try:
            return driver_for(record.type, self.connection_factory).schemas(record.connection)
        except Exception as exc:
            raise _public_error(exc) from exc

    def datasource_tables(self, datasource_id: str, schema: Any) -> list[dict[str, Any]]:
        record = self._record(datasource_id)
        schema_name = _safe_identifier(schema, "schema")
        try:
            return driver_for(record.type, self.connection_factory).tables(record.connection, schema_name)
        except Exception as exc:
            raise _public_error(exc) from exc

    def datasource_columns(self, datasource_id: str, schema: Any, table: Any) -> list[dict[str, Any]]:
        record = self._record(datasource_id)
        schema_name = _safe_identifier(schema, "schema")
        table_name = _safe_identifier(table, "table")
        try:
            return driver_for(record.type, self.connection_factory).columns(record.connection, schema_name, table_name)
        except Exception as exc:
            raise _public_error(exc) from exc

    def generate_model(self, datasource_id: str, schema: Any, table: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        record = self._record(datasource_id)
        schema_name = _safe_identifier(schema, "schema")
        table_name = _safe_identifier(table, "table")
        try:
            columns = driver_for(record.type, self.connection_factory).columns(record.connection, schema_name, table_name)
        except Exception as exc:
            raise _public_error(exc) from exc
        payload = payload if isinstance(payload, Mapping) else {}
        requested_name = payload.get("name", table_name)
        model_name = _safe_model_name(requested_name)
        metadata_path = f"models/{model_name}/metadata.yml"
        if any(item.get("path") == metadata_path for item in self.project.files()) and not bool(payload.get("overwrite")):
            raise ApiServiceError("FILE_EXISTS", "model already exists; pass overwrite=true to replace it", status=409)
        primary = next((item["name"] for item in columns if item.get("primaryKey")), None)
        model_columns: list[dict[str, Any]] = []
        for column in columns:
            item: dict[str, Any] = {"name": column["name"], "type": _wren_type(column.get("type") or column.get("dataType"))}
            if column.get("primaryKey"):
                item["is_primary_key"] = True
            if column.get("nullable") is False:
                item["not_null"] = True
            model_columns.append(item)
        model: dict[str, Any] = {
            "name": model_name,
            "table_reference": {"schema": schema_name, "table": table_name},
            "columns": model_columns,
        }
        if primary:
            model["primary_key"] = primary
        content = yaml.safe_dump(model, allow_unicode=True, sort_keys=False)
        draft = self.project.put_file(metadata_path, content)
        return {"model": model, "file": metadata_path, "draft": True, "revision": draft["revision"]}

    # ---- project files and lifecycle ------------------------------------

    def import_project(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        payload = payload if isinstance(payload, Mapping) else {}
        source = payload.get("path") or payload.get("projectDir")
        files = payload.get("files")
        if files is not None and not isinstance(files, list):
            raise ApiServiceError("INVALID_IMPORT", "files must be a list")
        try:
            return self.project.import_project(source, files)
        except Exception as exc:
            raise _public_error(exc) from exc

    def files(self) -> list[dict[str, Any]]:
        return self.project.files()

    def read_file(self, path: Any) -> dict[str, Any]:
        try:
            return self.project.read_file(_required_string(path, "path"))
        except Exception as exc:
            raise _public_error(exc) from exc

    def put_file(self, path: Any, payload: Mapping[str, Any] | str | None) -> dict[str, Any]:
        path = _required_string(path, "path")
        if isinstance(payload, str):
            content = payload
            delete = False
            expected = None
        elif isinstance(payload, Mapping):
            content = payload.get("content")
            delete = bool(payload.get("delete", False))
            expected = payload.get("expectedRevision")
            if expected is not None and not isinstance(expected, str):
                raise ApiServiceError("INVALID_PARAMS", "expectedRevision must be a string")
        elif payload is None:
            content = None
            delete = False
            expected = None
        else:
            raise ApiServiceError("INVALID_PARAMS", "request body must be an object")
        try:
            return self.project.put_file(path, content, delete=delete, expected_revision=expected)
        except Exception as exc:
            raise _public_error(exc) from exc

    def validate_project(self) -> dict[str, Any]:
        try:
            return self.project.validate()
        except Exception as exc:
            raise _public_error(exc) from exc

    def publish_project(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        payload = payload if isinstance(payload, Mapping) else {}
        label = payload.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 120):
            raise ApiServiceError("INVALID_PARAMS", "label must be a short string")
        try:
            return self.project.publish(label=label)
        except Exception as exc:
            raise _public_error(exc) from exc

    def versions(self) -> list[dict[str, Any]]:
        return self.project.versions()

    def rollback(self, version_id: str) -> dict[str, Any]:
        try:
            return self.project.rollback(_required_string(version_id, "versionId"))
        except Exception as exc:
            raise _public_error(exc) from exc

    # ---- request dispatch ------------------------------------------------

    def dispatch(self, method: str, path: str, query: Mapping[str, Any] | None = None, body: Any = None) -> tuple[int, dict[str, Any]]:
        """Dispatch one REST request and return ``(status, data)``.

        The HTTP adapter serializes this direct resource/error shape; keeping
        dispatch pure makes endpoint tests fast and avoids requiring an HTTP
        client library.
        """

        query = query or {}
        clean_path = path.rstrip("/") or "/"
        try:
            if method == "GET" and clean_path == "/api/health":
                return 200, self.health()
            if method == "GET" and clean_path == "/api/project":
                return 200, self.project_overview()
            if method == "GET" and clean_path == "/api/datasource-types":
                return 200, self.datasource_type_list()
            if method == "GET" and clean_path == "/api/datasources":
                return 200, self.list_datasources()
            if method == "POST" and clean_path == "/api/datasources":
                return 201, self.create_datasource(body if isinstance(body, Mapping) else {})
            match = re.fullmatch(r"/api/datasources/([^/]+)", clean_path)
            if match:
                datasource_id = match.group(1)
                if method == "GET":
                    return 200, self.get_datasource(datasource_id)
                if method == "PUT":
                    return 200, self.update_datasource(datasource_id, body if isinstance(body, Mapping) else {})
                if method == "DELETE":
                    return 200, self.delete_datasource(datasource_id)
            match = re.fullmatch(r"/api/datasources/([^/]+)/test", clean_path)
            if match and method == "POST":
                return 200, self.test_datasource(match.group(1))
            match = re.fullmatch(r"/api/datasources/([^/]+)/activate", clean_path)
            if match and method == "POST":
                return 200, self.activate_datasource(match.group(1))
            match = re.fullmatch(r"/api/datasources/([^/]+)/schemas", clean_path)
            if match and method == "GET":
                return 200, self.datasource_schemas(match.group(1))
            match = re.fullmatch(r"/api/datasources/([^/]+)/tables", clean_path)
            if match and method == "GET":
                return 200, self.datasource_tables(match.group(1), query.get("schema"))
            match = re.fullmatch(r"/api/datasources/([^/]+)/columns", clean_path)
            if match and method == "GET":
                return 200, self.datasource_columns(match.group(1), query.get("schema"), query.get("table"))
            match = re.fullmatch(r"/api/datasources/([^/]+)/(?:models|generate-model)", clean_path)
            if match and method == "POST":
                return 201, self.generate_model(match.group(1), query.get("schema") or (body or {}).get("schema") if isinstance(body, Mapping) else query.get("schema"), query.get("table") or (body or {}).get("table") if isinstance(body, Mapping) else query.get("table"), body if isinstance(body, Mapping) else {})
            if method == "POST" and clean_path in {"/api/project/model", "/api/project/models"}:
                if not isinstance(body, Mapping):
                    raise ApiServiceError("INVALID_PARAMS", "request body must be an object")
                datasource_id = body.get("datasourceId") or body.get("dataSourceId")
                return 201, self.generate_model(datasource_id, body.get("schema"), body.get("table"), body)
            if method == "POST" and clean_path == "/api/project/import":
                return 200, self.import_project(body if isinstance(body, Mapping) else {})
            if method == "GET" and clean_path == "/api/project/files":
                return 200, self.files()
            if method == "GET" and clean_path == "/api/project/file":
                return 200, self.read_file(query.get("path"))
            if method == "PUT" and clean_path == "/api/project/file":
                return 200, self.put_file(query.get("path"), body)
            if method == "POST" and clean_path == "/api/project/validate":
                return 200, self.validate_project()
            if method == "POST" and clean_path == "/api/project/publish":
                return 200, self.publish_project(body if isinstance(body, Mapping) else {})
            if method == "GET" and clean_path == "/api/versions":
                return 200, self.versions()
            match = re.fullmatch(r"/api/versions/([^/]+)/rollback", clean_path)
            if match and method == "POST":
                return 200, self.rollback(match.group(1))
            raise ApiServiceError("NOT_FOUND", "API route was not found", status=404)
        except Exception as exc:
            error = _public_error(exc)
            return error.status, {"__error__": {"code": error.code, "message": error.safe_message, **({"details": error.details} if error.details else {})}}

    def _record(self, datasource_id: str) -> DatasourceRecord:
        if not isinstance(datasource_id, str) or datasource_id not in self.datasources:
            raise ApiServiceError("DATASOURCE_NOT_FOUND", "datasource was not found", status=404)
        return self.datasources[datasource_id]

    @staticmethod
    def _connection_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        nested = payload.get("connection")
        source: Mapping[str, Any] = nested if isinstance(nested, Mapping) else payload
        result: dict[str, Any] = {}
        for key, value in source.items():
            if key in {"id", "name", "type", "datasource", "connection"}:
                continue
            if is_sensitive_key(key) or key in {"host", "port", "database", "user", "sslmode", "ssl_mode", "connectionUrl", "connectionURL", "connection_url", "dsn", "bigquery_type"}:
                # Credentials are accepted in the private record but never
                # copied to a public payload or project file.
                result[key] = value
            elif isinstance(value, (str, int, float, bool)) or value is None:
                result[key] = value
        return result


def _safe_model_name(value: Any) -> str:
    name = _required_string(value, "name")
    if not _IDENTIFIER.fullmatch(name):
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "model"
    if name[0].isdigit():
        name = "model_" + name
    return name[:120]


def _wren_type(value: Any) -> str:
    raw = str(value or "UNKNOWN").upper()
    if any(token in raw for token in ("INT", "SERIAL")):
        return "INTEGER"
    if any(token in raw for token in ("DECIMAL", "NUMERIC", "MONEY", "REAL", "DOUBLE", "FLOAT")):
        return "DECIMAL"
    if any(token in raw for token in ("DATE", "TIME", "TIMESTAMP")):
        return "TIMESTAMP" if "TIME" in raw else "DATE"
    if any(token in raw for token in ("BOOL",)):
        return "BOOLEAN"
    if any(token in raw for token in ("JSON", "XML")):
        return "JSON"
    return "VARCHAR"


__all__ = ["ApiServiceError", "SemanticConsoleService"]
