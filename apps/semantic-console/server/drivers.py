"""Datasource drivers used by the Semantic Console.

Only PostgreSQL is a hard runtime requirement for the MVP.  MySQL uses the
first installed DB-API driver (``mysql.connector`` or ``pymysql``), and reports
an explicit unavailable status when neither is installed.  SQL used for
introspection is read-only and all user-controlled values are parameters rather
than interpolated identifiers.
"""

from __future__ import annotations

import importlib
import importlib.util
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse


CONFIGURED_DATASOURCES = ("postgres", "mysql")


class DriverError(RuntimeError):
    """A safe datasource error suitable for an API response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class DriverStatus:
    """Driver availability information shown by ``/api/datasource-types``."""

    type: str
    label: str
    available: bool
    module: str | None
    supports_schema_browse: bool = True
    supports_test: bool = True
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "label": self.label,
            "available": self.available,
            "module": self.module,
            "supportsSchemaBrowse": self.supports_schema_browse,
            "supportsTest": self.supports_test,
            **({"note": self.note} if self.note else {}),
        }


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def datasource_types() -> list[dict[str, Any]]:
    """Return configured, runtime-available datasource types and field metadata.

    Wren exposes many connector definitions, but the Console intentionally
    advertises only drivers it implements and can import in this runtime.
    """

    fields_by_type: dict[str, list[dict[str, Any]]] = {}
    try:
        from wren.model.field_registry import get_fields  # type: ignore[import-not-found]

        for name in CONFIGURED_DATASOURCES:
            try:
                fields_by_type[name] = [
                    {
                        "name": item.name,
                        "label": item.label,
                        "inputType": item.input_type,
                        "placeholder": item.placeholder,
                        "hint": item.hint,
                        "required": item.required,
                        "sensitive": item.sensitive,
                        "alias": item.alias,
                        "examples": list(item.examples),
                        **({"accept": item.accept} if item.accept else {}),
                    }
                    for item in get_fields(name)
                ]
            except Exception:
                fields_by_type[name] = []
    except Exception:
        fields_by_type = {}

    labels = {
        "postgres": "PostgreSQL",
        "mysql": "MySQL",
    }
    result: list[dict[str, Any]] = []
    for name in CONFIGURED_DATASOURCES:
        if name == "postgres":
            postgres_module = (
                "psycopg"
                if _module_available("psycopg")
                else "psycopg2"
                if _module_available("psycopg2")
                else None
            )
            status = DriverStatus(
                name,
                labels.get(name, name.title()),
                postgres_module is not None,
                postgres_module,
                note=None if postgres_module else "Install psycopg[binary] to browse PostgreSQL metadata",
            )
        elif name == "mysql":
            mysql_module = (
                "mysql.connector"
                if _module_available("mysql.connector")
                else "pymysql"
                if _module_available("pymysql")
                else None
            )
            status = DriverStatus(
                name,
                labels.get(name, name.title()),
                mysql_module is not None,
                mysql_module,
                note=None
                if mysql_module
                else "Install mysql-connector-python or pymysql for MySQL browsing",
            )
        if status.available:
            result.append({**status.as_dict(), "fields": fields_by_type.get(name, [])})
    return result


def driver_status(name: str) -> DriverStatus:
    """Return one datasource driver status without exposing environment data."""

    for item in datasource_types():
        if item.get("type") == name:
            return DriverStatus(
                type=name,
                label=str(item.get("label", name.title())),
                available=bool(item.get("available")),
                module=item.get("module") if isinstance(item.get("module"), str) else None,
                supports_schema_browse=bool(item.get("supportsSchemaBrowse", True)),
                supports_test=bool(item.get("supportsTest", True)),
                note=item.get("note") if isinstance(item.get("note"), str) else None,
            )
    return DriverStatus(name, name.title(), False, None, False, False, "Unknown datasource")


def _value(values: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in values and values[name] is not None:
            return values[name]
    return None


def _connection_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize aliases without ever logging or returning the values."""

    result = dict(values)
    aliases = {
        "connectionUrl": "connection_url",
        "connectionURL": "connection_url",
        "db": "database",
        "dbname": "database",
        "userName": "user",
        "sslMode": "ssl_mode",
    }
    for source, target in aliases.items():
        if target not in result and source in result:
            result[target] = result[source]
    return result


def _url_values(url: str, expected: str) -> dict[str, Any]:
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise DriverError("INVALID_CONNECTION", "connection URL is invalid") from exc
    if parsed.scheme.lower().split("+")[0] not in {
        expected,
        "postgresql" if expected == "postgres" else expected,
    }:
        raise DriverError("INVALID_CONNECTION", "connection URL type does not match datasource")
    values: dict[str, Any] = {}
    if parsed.hostname:
        values["host"] = parsed.hostname
    if parsed.port:
        values["port"] = parsed.port
    if parsed.username:
        values["user"] = unquote(parsed.username)
    if parsed.password:
        values["password"] = unquote(parsed.password)
    if parsed.path and parsed.path != "/":
        values["database"] = unquote(parsed.path.lstrip("/"))
    return values


def _normalize_db_values(values: Mapping[str, Any], expected: str) -> dict[str, Any]:
    normalized = _connection_values(values)
    url = _value(normalized, "connection_url", "dsn")
    if isinstance(url, str) and url.strip():
        from_url = _url_values(url, expected)
        from_url.update({key: value for key, value in normalized.items() if key not in {"connection_url", "dsn"}})
        normalized = from_url
    return normalized


def _safe_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if 1 <= parsed <= 65535 else default


class BaseDriver:
    """DB-API-neutral metadata operations."""

    name = ""

    def __init__(self, connection_factory: Callable[..., Any] | None = None) -> None:
        self.connection_factory = connection_factory

    def connect(self, values: Mapping[str, Any]) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def _open(self, values: Mapping[str, Any]) -> Any:
        try:
            connection = self.connect(values)
            if hasattr(connection, "autocommit"):
                connection.autocommit = True
            return connection
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError("CONNECTION_FAILED", "database connection failed") from exc

    @staticmethod
    def _close(connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            pass

    @staticmethod
    def _fetch_rows(cursor: Any) -> list[dict[str, Any]]:
        rows = cursor.fetchall()
        description = getattr(cursor, "description", None) or []
        names = [str(column[0]).lower() for column in description]
        result: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, Mapping):
                result.append({str(key).lower(): value for key, value in row.items()})
            else:
                result.append({name: value for name, value in zip(names, row)})
        return result

    @staticmethod
    def _execute(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return BaseDriver._fetch_rows(cursor)
        finally:
            try:
                cursor.close()
            except Exception:
                pass

    def test_connection(self, values: Mapping[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        connection = self._open(values)
        try:
            self._execute(connection, "SELECT 1")
            return {"ok": True, "latencyMs": round((time.monotonic() - started) * 1000, 1), "driver": self.name}
        finally:
            self._close(connection)

    def schemas(self, values: Mapping[str, Any]) -> list[dict[str, Any]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def tables(self, values: Mapping[str, Any], schema: str) -> list[dict[str, Any]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def columns(self, values: Mapping[str, Any], schema: str, table: str) -> list[dict[str, Any]]:  # pragma: no cover - abstract
        raise NotImplementedError


class PostgresDriver(BaseDriver):
    name = "postgres"

    def connect(self, values: Mapping[str, Any]) -> Any:
        normalized = _normalize_db_values(values, "postgres")
        if self.connection_factory is not None:
            try:
                return self.connection_factory(normalized)
            except TypeError:
                return self.connection_factory(**normalized)
        try:
            psycopg = importlib.import_module("psycopg")
        except (ImportError, ModuleNotFoundError) as exc:
            try:
                psycopg2 = importlib.import_module("psycopg2")
            except (ImportError, ModuleNotFoundError) as exc2:
                raise DriverError("DRIVER_UNAVAILABLE", "PostgreSQL driver is unavailable") from exc2
            return psycopg2.connect(**_postgres_kwargs(normalized))
        url = _value(normalized, "connection_url", "dsn")
        if isinstance(url, str) and url.strip():
            return psycopg.connect(url, connect_timeout=10)
        return psycopg.connect(**_postgres_kwargs(normalized))

    def schemas(self, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        connection = self._open(values)
        try:
            rows = self._execute(
                connection,
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT LIKE 'pg_%' AND schema_name <> 'information_schema' "
                "ORDER BY schema_name",
            )
            return [{"name": str(row.get("schema_name", ""))} for row in rows if row.get("schema_name")]
        finally:
            self._close(connection)

    def tables(self, values: Mapping[str, Any], schema: str) -> list[dict[str, Any]]:
        connection = self._open(values)
        try:
            rows = self._execute(
                connection,
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name",
                (schema,),
            )
            return [
                {"name": str(row.get("table_name", "")), "type": str(row.get("table_type", "TABLE"))}
                for row in rows
                if row.get("table_name")
            ]
        finally:
            self._close(connection)

    def columns(self, values: Mapping[str, Any], schema: str, table: str) -> list[dict[str, Any]]:
        connection = self._open(values)
        try:
            rows = self._execute(
                connection,
                "SELECT column_name, data_type, udt_name, is_nullable, ordinal_position "
                "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema, table),
            )
            primary = self._primary_keys(connection, schema, table)
            return [_column(row, primary) for row in rows if row.get("column_name")]
        finally:
            self._close(connection)

    def _primary_keys(self, connection: Any, schema: str, table: str) -> set[str]:
        rows = self._execute(
            connection,
            "SELECT kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema AND tc.table_name = kcu.table_name "
            "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s AND tc.table_name = %s",
            (schema, table),
        )
        return {str(row["column_name"]) for row in rows if row.get("column_name")}


def _postgres_kwargs(values: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"host", "port", "dbname", "database", "user", "password", "sslmode", "ssl_mode"}
    kwargs = {key: value for key, value in values.items() if key in allowed and value is not None}
    if "database" in kwargs and "dbname" not in kwargs:
        kwargs["dbname"] = kwargs.pop("database")
    if "ssl_mode" in kwargs and "sslmode" not in kwargs:
        kwargs["sslmode"] = kwargs.pop("ssl_mode")
    kwargs["connect_timeout"] = 10
    return kwargs


class MysqlDriver(BaseDriver):
    name = "mysql"

    def connect(self, values: Mapping[str, Any]) -> Any:
        normalized = _normalize_db_values(values, "mysql")
        if self.connection_factory is not None:
            try:
                return self.connection_factory(normalized)
            except TypeError:
                return self.connection_factory(**normalized)
        kwargs = {
            key: value
            for key, value in normalized.items()
            if key in {"host", "port", "database", "user", "password", "ssl_ca", "ssl_mode"}
            and value is not None
        }
        kwargs["port"] = _safe_int(kwargs.get("port"), 3306)
        kwargs["connect_timeout"] = 10
        try:
            connector = importlib.import_module("mysql.connector")
        except (ImportError, ModuleNotFoundError):
            try:
                pymysql = importlib.import_module("pymysql")
            except (ImportError, ModuleNotFoundError) as exc:
                raise DriverError("DRIVER_UNAVAILABLE", "MySQL driver is unavailable") from exc
            kwargs.pop("ssl_mode", None)
            return pymysql.connect(**kwargs)
        kwargs.pop("ssl_mode", None)
        return connector.connect(**kwargs)

    def schemas(self, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        connection = self._open(values)
        try:
            rows = self._execute(
                connection,
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys') "
                "ORDER BY schema_name",
            )
            return [{"name": str(row.get("schema_name", ""))} for row in rows if row.get("schema_name")]
        finally:
            self._close(connection)

    def tables(self, values: Mapping[str, Any], schema: str) -> list[dict[str, Any]]:
        connection = self._open(values)
        try:
            rows = self._execute(
                connection,
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name",
                (schema,),
            )
            return [
                {"name": str(row.get("table_name", "")), "type": str(row.get("table_type", "TABLE"))}
                for row in rows
                if row.get("table_name")
            ]
        finally:
            self._close(connection)

    def columns(self, values: Mapping[str, Any], schema: str, table: str) -> list[dict[str, Any]]:
        connection = self._open(values)
        try:
            rows = self._execute(
                connection,
                "SELECT column_name, data_type, column_type, is_nullable, ordinal_position "
                "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema, table),
            )
            primary = self._primary_keys(connection, schema, table)
            return [_column(row, primary) for row in rows if row.get("column_name")]
        finally:
            self._close(connection)

    def _primary_keys(self, connection: Any, schema: str, table: str) -> set[str]:
        rows = self._execute(
            connection,
            "SELECT kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema AND tc.table_name = kcu.table_name "
            "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s AND tc.table_name = %s",
            (schema, table),
        )
        return {str(row["column_name"]) for row in rows if row.get("column_name")}


def _column(row: Mapping[str, Any], primary: set[str]) -> dict[str, Any]:
    name = str(row.get("column_name", ""))
    raw_type = row.get("udt_name") or row.get("column_type") or row.get("data_type") or "UNKNOWN"
    return {
        "name": name,
        "type": str(raw_type).upper(),
        "dataType": str(row.get("data_type") or raw_type).upper(),
        "nullable": str(row.get("is_nullable", "YES")).upper() == "YES",
        "ordinal": int(row.get("ordinal_position") or 0),
        "primaryKey": name in primary,
    }


def driver_for(name: str, connection_factory: Callable[..., Any] | None = None) -> BaseDriver:
    """Construct the supported driver, keeping extension points explicit."""

    normalized = name.lower()
    if normalized in {"postgres", "postgresql"}:
        return PostgresDriver(connection_factory)
    if normalized in {"mysql", "mariadb"}:
        return MysqlDriver(connection_factory)
    raise DriverError("UNSUPPORTED_DATASOURCE", "datasource type is not supported by this MVP")


__all__ = [
    "BaseDriver",
    "CONFIGURED_DATASOURCES",
    "DriverError",
    "DriverStatus",
    "MysqlDriver",
    "PostgresDriver",
    "datasource_types",
    "driver_for",
    "driver_status",
]
