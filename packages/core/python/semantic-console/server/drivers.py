"""Read-only datasource metadata drivers used by the Semantic Console.

The Console advertises a datasource only when its runtime driver is installed.
Remote systems use their official Python clients; SQLite and DuckDB open an
existing local database file in read-only mode.  Introspection values are
parameters rather than interpolated identifiers.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse


CONFIGURED_DATASOURCES = ("postgres", "mysql", "sqlite", "clickhouse", "duckdb")


_BUILTIN_FIELDS: dict[str, list[dict[str, Any]]] = {
    "postgres": [
        {"name": "host", "label": "Host", "inputType": "text", "placeholder": "localhost", "required": True},
        {"name": "port", "label": "Port", "inputType": "number", "placeholder": "5432", "required": True},
        {"name": "database", "label": "Database", "inputType": "text", "placeholder": "analytics", "required": True},
        {"name": "user", "label": "User", "inputType": "text", "placeholder": "semantic_reader", "required": True},
        {"name": "password", "label": "Password", "inputType": "password", "sensitive": True},
        {"name": "ssl_mode", "label": "SSL mode", "inputType": "select", "examples": ["require", "verify-full", "disable"]},
    ],
    "mysql": [
        {"name": "host", "label": "Host", "inputType": "text", "placeholder": "localhost", "required": True},
        {"name": "port", "label": "Port", "inputType": "number", "placeholder": "3306", "required": True},
        {"name": "database", "label": "Database", "inputType": "text", "placeholder": "analytics", "required": True},
        {"name": "user", "label": "User", "inputType": "text", "placeholder": "semantic_reader", "required": True},
        {"name": "password", "label": "Password", "inputType": "password", "sensitive": True},
        {"name": "ssl_mode", "label": "SSL mode", "inputType": "select", "examples": ["required", "preferred", "disabled"]},
    ],
    "sqlite": [
        {"name": "path", "label": "Database file", "inputType": "text", "placeholder": "C:\\data\\analytics.sqlite", "hint": "Path to an existing .sqlite or .db file", "required": True},
    ],
    "clickhouse": [
        {"name": "host", "label": "Host", "inputType": "text", "placeholder": "localhost", "required": True},
        {"name": "port", "label": "HTTP port", "inputType": "number", "placeholder": "8123", "required": True},
        {"name": "database", "label": "Database", "inputType": "text", "placeholder": "default", "required": True},
        {"name": "user", "label": "User", "inputType": "text", "placeholder": "default", "required": True},
        {"name": "password", "label": "Password", "inputType": "password", "sensitive": True},
        {"name": "secure", "label": "TLS", "inputType": "select", "examples": ["true", "false"]},
    ],
    "duckdb": [
        {"name": "path", "label": "Database file", "inputType": "text", "placeholder": "C:\\data\\analytics.duckdb", "hint": "Path to an existing .duckdb file", "required": True},
    ],
}


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
        "sqlite": "SQLite",
        "clickhouse": "ClickHouse",
        "duckdb": "DuckDB",
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
        elif name == "sqlite":
            status = DriverStatus(name, labels[name], True, "sqlite3")
        elif name == "clickhouse":
            module = "clickhouse_connect" if _module_available("clickhouse_connect") else None
            status = DriverStatus(
                name,
                labels[name],
                module is not None,
                module,
                note=None if module else "Install clickhouse-connect to browse ClickHouse metadata",
            )
        elif name == "duckdb":
            module = "duckdb" if _module_available("duckdb") else None
            status = DriverStatus(
                name,
                labels[name],
                module is not None,
                module,
                note=None if module else "Install duckdb to browse DuckDB metadata",
            )
        if status.available:
            result.append({**status.as_dict(), "fields": fields_by_type.get(name) or _BUILTIN_FIELDS[name]})
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


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _database_file(values: Mapping[str, Any]) -> str:
    raw = _value(_connection_values(values), "path", "file", "database")
    if not isinstance(raw, str) or not raw.strip():
        raise DriverError("INVALID_CONNECTION", "database file path is required")
    if raw.strip() == ":memory:":
        return ":memory:"
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise DriverError("INVALID_CONNECTION", "database file does not exist")
    return str(path)


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


class SqliteDriver(BaseDriver):
    """Browse an existing SQLite database without creating or mutating it."""

    name = "sqlite"

    def connect(self, values: Mapping[str, Any]) -> Any:
        if self.connection_factory is not None:
            try:
                return self.connection_factory(_connection_values(values))
            except TypeError:
                return self.connection_factory(**_connection_values(values))
        sqlite3 = importlib.import_module("sqlite3")
        path = _database_file(values)
        if path == ":memory:":
            return sqlite3.connect(path)
        return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)

    def schemas(self, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        connection = self._open(values)
        try:
            rows = self._execute(connection, "PRAGMA database_list")
            return [
                {"name": str(row.get("name", ""))}
                for row in rows
                if row.get("name") in {"main", "temp"}
            ]
        finally:
            self._close(connection)

    def tables(self, values: Mapping[str, Any], schema: str) -> list[dict[str, Any]]:
        if schema not in {"main", "temp"}:
            return []
        connection = self._open(values)
        try:
            catalog = "sqlite_temp_master" if schema == "temp" else "sqlite_master"
            rows = self._execute(
                connection,
                f"SELECT name, type FROM {catalog} "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name",
            )
            return [
                {"name": str(row.get("name", "")), "type": "VIEW" if row.get("type") == "view" else "BASE TABLE"}
                for row in rows
                if row.get("name")
            ]
        finally:
            self._close(connection)

    def columns(self, values: Mapping[str, Any], schema: str, table: str) -> list[dict[str, Any]]:
        if schema not in {"main", "temp"}:
            return []
        connection = self._open(values)
        try:
            rows = self._execute(
                connection,
                'SELECT name, type, "notnull" AS is_not_null, pk, cid FROM pragma_table_info(?, ?)',
                (table, schema),
            )
            return [
                {
                    "name": str(row.get("name", "")),
                    "type": str(row.get("type") or "UNKNOWN").upper(),
                    "dataType": str(row.get("type") or "UNKNOWN").upper(),
                    "nullable": not bool(row.get("is_not_null")) and not bool(row.get("pk")),
                    "ordinal": int(row.get("cid") or 0) + 1,
                    "primaryKey": bool(row.get("pk")),
                }
                for row in rows
                if row.get("name")
            ]
        finally:
            self._close(connection)


class DuckdbDriver(BaseDriver):
    """Browse an existing DuckDB database in read-only mode."""

    name = "duckdb"

    def connect(self, values: Mapping[str, Any]) -> Any:
        if self.connection_factory is not None:
            try:
                return self.connection_factory(_connection_values(values))
            except TypeError:
                return self.connection_factory(**_connection_values(values))
        duckdb = importlib.import_module("duckdb")
        path = _database_file(values)
        return duckdb.connect(database=path, read_only=path != ":memory:")

    def schemas(self, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        connection = self._open(values)
        try:
            rows = self._execute(
                connection,
                "SELECT DISTINCT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('information_schema', 'pg_catalog') ORDER BY schema_name",
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
                "WHERE table_schema = ? ORDER BY table_name",
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
                "SELECT column_name, data_type, is_nullable, ordinal_position "
                "FROM information_schema.columns WHERE table_schema = ? AND table_name = ? "
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
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_catalog = kcu.constraint_catalog AND tc.constraint_schema = kcu.constraint_schema "
            "AND tc.constraint_name = kcu.constraint_name "
            "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = ? AND tc.table_name = ?",
            (schema, table),
        )
        return {str(row["column_name"]) for row in rows if row.get("column_name")}


class ClickhouseDriver(BaseDriver):
    """Metadata adapter for ClickHouse's official HTTP Python client."""

    name = "clickhouse"

    def connect(self, values: Mapping[str, Any]) -> Any:
        normalized = _connection_values(values)
        if self.connection_factory is not None:
            try:
                return self.connection_factory(normalized)
            except TypeError:
                return self.connection_factory(**normalized)
        clickhouse_connect = importlib.import_module("clickhouse_connect")
        kwargs = {
            "host": str(_value(normalized, "host") or "localhost"),
            "port": _safe_int(_value(normalized, "port"), 8123),
            "username": str(_value(normalized, "user", "username") or "default"),
            "password": str(_value(normalized, "password") or ""),
            "database": str(_value(normalized, "database") or "default"),
            "secure": _safe_bool(_value(normalized, "secure")),
            "connect_timeout": 10,
        }
        return clickhouse_connect.get_client(**kwargs)

    @staticmethod
    def _query(client: Any, sql: str, parameters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        result = client.query(sql, parameters=dict(parameters or {}))
        names = [str(name).lower() for name in result.column_names]
        return [{name: value for name, value in zip(names, row)} for row in result.result_rows]

    def test_connection(self, values: Mapping[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        client = self._open(values)
        try:
            self._query(client, "SELECT 1")
            return {"ok": True, "latencyMs": round((time.monotonic() - started) * 1000, 1), "driver": self.name}
        finally:
            self._close(client)

    def schemas(self, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        client = self._open(values)
        try:
            rows = self._query(
                client,
                "SELECT name FROM system.databases "
                "WHERE name NOT IN ('system', 'information_schema', 'INFORMATION_SCHEMA') ORDER BY name",
            )
            return [{"name": str(row.get("name", ""))} for row in rows if row.get("name")]
        finally:
            self._close(client)

    def tables(self, values: Mapping[str, Any], schema: str) -> list[dict[str, Any]]:
        client = self._open(values)
        try:
            rows = self._query(
                client,
                "SELECT name, engine FROM system.tables WHERE database = {schema:String} ORDER BY name",
                {"schema": schema},
            )
            return [
                {
                    "name": str(row.get("name", "")),
                    "type": "VIEW" if "view" in str(row.get("engine", "")).lower() else "BASE TABLE",
                }
                for row in rows
                if row.get("name")
            ]
        finally:
            self._close(client)

    def columns(self, values: Mapping[str, Any], schema: str, table: str) -> list[dict[str, Any]]:
        client = self._open(values)
        try:
            rows = self._query(
                client,
                "SELECT name, type, position, is_in_primary_key FROM system.columns "
                "WHERE database = {schema:String} AND table = {table:String} ORDER BY position",
                {"schema": schema, "table": table},
            )
            return [
                {
                    "name": str(row.get("name", "")),
                    "type": str(row.get("type") or "UNKNOWN").upper(),
                    "dataType": str(row.get("type") or "UNKNOWN").upper(),
                    "nullable": str(row.get("type") or "").startswith("Nullable("),
                    "ordinal": int(row.get("position") or 0),
                    "primaryKey": bool(row.get("is_in_primary_key")),
                }
                for row in rows
                if row.get("name")
            ]
        finally:
            self._close(client)


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
    if normalized == "sqlite":
        return SqliteDriver(connection_factory)
    if normalized == "clickhouse":
        return ClickhouseDriver(connection_factory)
    if normalized == "duckdb":
        return DuckdbDriver(connection_factory)
    raise DriverError("UNSUPPORTED_DATASOURCE", "datasource type is not supported")


__all__ = [
    "BaseDriver",
    "CONFIGURED_DATASOURCES",
    "DriverError",
    "DriverStatus",
    "ClickhouseDriver",
    "DuckdbDriver",
    "MysqlDriver",
    "PostgresDriver",
    "SqliteDriver",
    "datasource_types",
    "driver_for",
    "driver_status",
]
