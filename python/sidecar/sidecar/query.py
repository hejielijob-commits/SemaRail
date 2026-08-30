"""Safe PostgreSQL query execution behind the sidecar RPC boundary.

The Wren Python package owns semantic planning, but its general-purpose
``WrenEngine.query`` path is intentionally not used here.  The MVP execution
boundary needs stronger guarantees than a generic connector provides:

* only one read-only ``SELECT``/CTE statement is accepted;
* the transaction is read-only and has a bounded statement timeout;
* rows and preview bytes are bounded before they cross the process boundary;
* an active query is addressable by ``queryId`` and can be cancelled from a
  second RPC worker; and
* driver exceptions, connection details, and credentials never become wire
  diagnostics.

``PsycopgQueryExecutor`` is deliberately dependency-light.  ``psycopg`` is
imported only when an execution is requested, so health, validation, and dry
planning still work when the optional PostgreSQL driver is not installed.
"""

from __future__ import annotations

import base64
import datetime as _datetime
import json
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from .datasource_state import DatasourceStateError, load_active_connection
from .errors import (
    CANCELLED,
    DATABASE_ERROR,
    INVALID_PARAMS,
    POLICY_DENIED,
    RpcFault,
    TIMEOUT,
    WREN_UNAVAILABLE,
)
from .sql_policy import (
    DANGEROUS_FUNCTIONS,
    PhysicalAllowlist,
    PhysicalTable,
    SqlPolicyError,
    extract_table_names,
    physical_allowlist_from_dict,
    validate_native_sql,
    validate_read_only_sql,
    validate_semantic_sql,
)


# These values intentionally mirror packages/contract/src/json.ts.  The
# sidecar may use a smaller per-query limit, but never a larger one.
SCHEMA_VERSION = 1
MAX_QUERY_ROWS = 500
MAX_PREVIEW_ROWS = 200
MAX_PREVIEW_BYTES = 1_048_576
DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 30_000
MAX_QUERY_CONCURRENCY = 2


class QueryPlanner(Protocol):
    """Minimal Wren planning seam used by :class:`WrenQueryService`."""

    def dry_plan(self, params: Mapping[str, Any]) -> Any:
        """Return native SQL plus the MDL-derived physical allowlist."""


class DatabaseExecutor(Protocol):
    """Database execution seam; implementations must return JSON-safe data."""

    def execute(
        self,
        *,
        query_id: str,
        semantic_sql: str,
        native_sql: str,
        project_dir: str,
        connection_info: Mapping[str, Any] | None,
        limits: "QueryLimits",
    ) -> dict[str, Any]:
        """Execute one already-planned native SQL query."""

    def cancel(self, query_id: str) -> bool:
        """Request cancellation for one active query."""


@dataclass(frozen=True, slots=True)
class QueryLimits:
    """Validated execution limits shared by database adapters."""

    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_rows: int = MAX_QUERY_ROWS
    preview_rows: int = MAX_PREVIEW_ROWS
    max_bytes: int = MAX_PREVIEW_BYTES


@dataclass(slots=True)
class _ActiveQuery:
    """Connection and cancellation state for one in-flight query."""

    connection: Any
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    timeout_requested: threading.Event = field(default_factory=threading.Event)


def _safe_int(value: Any, *, field: str, default: int, maximum: int, minimum: int = 1) -> int:
    if value is None:
        return default
    if type(value) is not int or not minimum <= value <= maximum:
        raise RpcFault(
            INVALID_PARAMS,
            "validation",
            f"{field} is outside the supported range",
            retryable=False,
        )
    return value


def _query_limits(params: Mapping[str, Any]) -> QueryLimits:
    """Parse optional execution limits without allowing unbounded values."""

    return QueryLimits(
        timeout_ms=_safe_int(
            params.get("timeoutMs"),
            field="timeoutMs",
            default=DEFAULT_TIMEOUT_MS,
            maximum=MAX_TIMEOUT_MS,
        ),
        max_rows=_safe_int(
            params.get("maxRows"),
            field="maxRows",
            default=MAX_QUERY_ROWS,
            maximum=MAX_QUERY_ROWS,
        ),
        preview_rows=_safe_int(
            params.get("previewRows"),
            field="previewRows",
            default=MAX_PREVIEW_ROWS,
            maximum=MAX_PREVIEW_ROWS,
        ),
        max_bytes=_safe_int(
            params.get("maxPreviewBytes"),
            field="maxPreviewBytes",
            default=MAX_PREVIEW_BYTES,
            maximum=MAX_PREVIEW_BYTES,
        ),
    )


def _query_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise RpcFault(
            INVALID_PARAMS,
            "validation",
            "queryId must be a non-empty string",
            retryable=False,
        )
    return value


def _required_query_string(params: Mapping[str, Any], field: str, maximum: int) -> str:
    value = params.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RpcFault(
            INVALID_PARAMS,
            "validation",
            f"{field} must be a non-empty string",
            retryable=False,
        )
    return value


def _connection_info(params: Mapping[str, Any]) -> Mapping[str, Any] | None:
    # Production requests carry only an environment-variable name.  The
    # actual DSN is resolved inside the sidecar process, never on the Host ↔
    # sidecar JSON boundary.  Keep this helper as an explicit seam for future
    # process-local adapters; callers cannot provide a wire ``connectionInfo``.
    del params
    return None


def _safe_json_value(value: Any) -> Any:
    """Convert DB values to JSON-safe scalar values without repr leakage."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (list, tuple, dict)):
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
                default=lambda item: _safe_json_value(item),
            )
        except (TypeError, ValueError, UnicodeError):
            return "[unrepresentable]"
    # Do not use repr(value): driver objects may include credentials or SQL.
    return "[unrepresentable]"


def _description_name(description: Any, index: int) -> str:
    value = getattr(description, "name", None)
    if not isinstance(value, str) or not value:
        if isinstance(description, (tuple, list)) and description:
            value = description[0]
    if not isinstance(value, str) or not value:
        value = f"column_{index + 1}"
    return value[:256]


def _description_type(description: Any) -> str:
    value = getattr(description, "type_name", None)
    if isinstance(value, str) and value:
        return value.upper()[:128]
    type_code = getattr(description, "type_code", None)
    if type_code is None and isinstance(description, (tuple, list)) and len(description) > 1:
        type_code = description[1]
    # PostgreSQL OID names are stable and avoid importing psycopg's type
    # registry.  Unknown OIDs remain a safe opaque type name.
    oid_names = {
        16: "BOOLEAN",
        17: "BYTEA",
        20: "BIGINT",
        21: "SMALLINT",
        23: "INTEGER",
        25: "TEXT",
        700: "REAL",
        701: "DOUBLE",
        1082: "DATE",
        1083: "TIME",
        1114: "TIMESTAMP",
        1184: "TIMESTAMPTZ",
        1700: "NUMERIC",
        2950: "UUID",
        3802: "JSONB",
    }
    if isinstance(type_code, int):
        return oid_names.get(type_code, f"OID_{type_code}")
    if isinstance(type_code, str) and type_code:
        return type_code.upper()[:128]
    return "UNKNOWN"


def _is_measure(type_name: str) -> bool:
    upper = type_name.upper()
    return bool(
        re.match(
            r"^(?:BIGINT|INT8|INT64|INTEGER|INT|INT4|SMALLINT|INT2|NUMERIC|DECIMAL|REAL|FLOAT|DOUBLE|MONEY)(?:\b|\()",
            upper,
        )
    )


def _column_specs(description: Iterable[Any]) -> list[tuple[str, str, str]]:
    specs: list[tuple[str, str, str]] = []
    names: dict[str, int] = {}
    for index, item in enumerate(description):
        base = _description_name(item, index)
        seen = names.get(base, 0)
        names[base] = seen + 1
        name = base if seen == 0 else f"{base}_{seen + 1}"
        type_name = _description_type(item)
        role = "measure" if _is_measure(type_name) else "dimension"
        specs.append((name, type_name, role))
    return specs


def _scalar_for_column(value: Any, type_name: str) -> Any:
    # TypeScript's presentation parser requires exact strings for values whose
    # numeric/date precision would otherwise be lossy in JavaScript.
    if value is None:
        return None
    upper = type_name.upper()
    if re.match(r"^(?:BIGINT|INT64|LONG|DECIMAL|NUMERIC|DATE|TIME|TIMESTAMP|DATETIME)(?:\b|\()", upper):
        if isinstance(value, str):
            return value
        if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time, Decimal)):
            return _safe_json_value(value)
        if isinstance(value, int):
            return str(value)
    return _safe_json_value(value)


def _row_json_bytes(row: Mapping[str, Any]) -> int:
    try:
        return len(
            json.dumps(
                row,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeError):
        return MAX_PREVIEW_BYTES + 1


def _is_temporal_dimension(type_name: str) -> bool:
    """Return whether a column type is a safe time/category axis hint."""

    return bool(
        re.match(
            r"^(?:DATE|TIME|TIMETZ|TIMESTAMP|TIMESTAMPTZ|DATETIME)(?:\b|\()",
            type_name.strip().upper(),
        )
    )


def _is_drawable_number(value: Any) -> bool:
    """Accept finite JSON numbers and exact finite numeric strings only."""

    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return Decimal(value.strip()).is_finite()
    except Exception:
        return False


def _chart_spec_for_result(
    result: Mapping[str, Any],
    chart_intent: str | None,
) -> dict[str, Any] | None:
    """Derive the small, non-executable ChartSpecV1 subset from real output.

    Omitted intent is the conservative ``auto`` mode.  A successful query is
    still useful when its shape cannot be drawn, so an unsupported shape
    yields no chart rather than turning the whole result into an error.
    ``auto`` deliberately does not infer part-to-whole meaning from low row
    cardinality: temporal dimensions select a line chart and all other
    dimensions select a bar chart.
    """

    intent = "auto" if chart_intent is None else chart_intent
    if intent == "table" or result.get("status") != "success":
        return None

    raw_columns = result.get("columns")
    raw_rows = result.get("previewRows")
    if not isinstance(raw_columns, list) or not isinstance(raw_rows, list) or not raw_rows:
        return None
    if not all(isinstance(row, Mapping) for row in raw_rows):
        return None

    dimensions: list[tuple[str, str]] = []
    measures: list[str] = []
    for column in raw_columns:
        if not isinstance(column, Mapping):
            continue
        name = column.get("name")
        type_name = column.get("type")
        role = column.get("semanticRole")
        if not isinstance(name, str) or not name or not isinstance(type_name, str):
            continue
        # Recheck the physical type as well as semanticRole.  This prevents a
        # boolean/text column mislabeled by an injected adapter from becoming
        # a measure in a generated chart.
        if role == "measure" and _is_measure(type_name):
            values = [row.get(name) for row in raw_rows]
            non_null = [value for value in values if value is not None]
            if non_null and all(_is_drawable_number(value) for value in non_null):
                measures.append(name)
        elif role == "dimension":
            if any(row.get(name) is not None for row in raw_rows):
                dimensions.append((name, type_name))

    if not dimensions or not measures:
        return None

    temporal = next((column for column in dimensions if _is_temporal_dimension(column[1])), None)
    if intent == "auto":
        chart_type = "line" if temporal is not None else "bar"
    else:
        chart_type = intent
    x_name = temporal[0] if chart_type == "line" and temporal is not None else dimensions[0][0]
    # Multiple pie measures produce ambiguous overlapping pies in the MVP
    # renderer.  Select the first stable measure; line/bar may safely expose
    # up to the contract maximum of eight series.
    y_names = measures[:1] if chart_type == "pie" else measures[:8]
    return {
        "version": 1,
        "type": chart_type,
        "x": x_name,
        "y": y_names,
        "tooltip": True,
    }


def _apply_chart_spec(
    result: Mapping[str, Any],
    chart_intent: str | None,
) -> dict[str, Any]:
    """Replace any adapter-supplied chart with the bounded derived spec."""

    presentation = dict(result)
    presentation.pop("chart", None)
    chart = _chart_spec_for_result(presentation, chart_intent)
    if chart is not None:
        presentation["chart"] = chart
    return presentation


class WrenQueryService:
    """Plan semantic SQL with Wren, then execute via an injected DB adapter."""

    def __init__(
        self,
        planner: QueryPlanner,
        executor: DatabaseExecutor,
        connection_resolver: Callable[[str, str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.connection_resolver = connection_resolver or load_active_connection

    def run(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Validate, dry-plan, and execute one bounded query."""

        fields = {
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
        }
        if set(params) - fields:
            raise RpcFault(INVALID_PARAMS, "validation", "query.run params are invalid")
        project_dir = _required_query_string(params, "projectDir", 32_768)
        question = _required_query_string(params, "question", 16_000)
        semantic_sql = _required_query_string(params, "semanticSql", 64_000)
        query_id = _query_id(params.get("queryId"))
        limits = _query_limits(params)
        chart_intent = params.get("chartIntent")
        if chart_intent is not None and chart_intent not in {"auto", "table", "line", "bar", "pie"}:
            raise RpcFault(INVALID_PARAMS, "validation", "chartIntent is invalid")
        try:
            # This is deliberately before Wren planning.  Wren's planner is
            # still a useful semantic transformation, but it must not become
            # the first place where a model-supplied DML/multi-statement input
            # is interpreted.
            semantic_sql = validate_semantic_sql(semantic_sql)
        except SqlPolicyError as exc:
            raise RpcFault(
                POLICY_DENIED,
                "policy",
                "semantic SQL must be one read-only query",
                retryable=False,
            ) from exc
        sql_history: list[dict[str, Any]] = []
        context_lookup = getattr(self.planner, "ask", None)
        if callable(context_lookup):
            try:
                context = context_lookup({"projectDir": project_dir, "question": question})
                raw_history = context.get("sqlHistory", []) if isinstance(context, Mapping) else []
                if isinstance(raw_history, list):
                    sql_history = [dict(item) for item in raw_history[:5] if isinstance(item, Mapping)]
            except Exception:
                # Recall is advisory. A missing/stale optional index must not
                # turn a safe database read into a failed query.
                sql_history = []
        try:
            plan = self.planner.dry_plan(
                {"projectDir": project_dir, "semanticSql": semantic_sql}
            )
        except RpcFault:
            raise
        except Exception as exc:
            # The Wren adapter normally sanitizes this.  Keep this seam safe
            # when an embedding supplies a bare planner implementation.
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.dryPlan",
                "SemaRail semantic planner is unavailable",
                retryable=True,
            ) from exc
        if not isinstance(plan, Mapping):
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.dryPlan",
                "SemaRail semantic planner is unavailable",
                retryable=True,
            )
        native_sql = plan.get("nativeSql")
        if not isinstance(native_sql, str) or not native_sql.strip():
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.dryPlan",
                "SemaRail semantic planner is unavailable",
                retryable=True,
            )
        try:
            # The production Wren adapter returns an explicit MDL-derived
            # physical allowlist.  Older injected test planners can omit it;
            # in that seam use only the semantic table names as a conservative
            # compatibility allowlist, never an arbitrary model-provided table.
            raw_allowlist = plan.get("allowedPhysical")
            if isinstance(raw_allowlist, Mapping):
                allowed_physical: PhysicalAllowlist | Mapping[str, Any] | None = raw_allowlist
            else:
                semantic_tables = extract_table_names(semantic_sql)
                allowed_physical = PhysicalAllowlist(
                    frozenset(PhysicalTable(None, None, name) for name in semantic_tables)
                )
            native_sql = validate_native_sql(
                native_sql,
                allowed_physical=allowed_physical,
            )
        except SqlPolicyError as exc:
            raise RpcFault(
                POLICY_DENIED,
                "policy",
                "query denied by read-only SQL policy",
                retryable=False,
            ) from exc
        env_name = params.get("databaseDsnEnv", "SEMARAIL_DATABASE_URL")
        if (
            not isinstance(env_name, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env_name)
        ):
            raise RpcFault(
                INVALID_PARAMS,
                "validation",
                "databaseDsnEnv must be a valid environment variable name",
            )
        try:
            info = self.connection_resolver(project_dir, env_name)
        except DatasourceStateError as exc:
            raise RpcFault(
                DATABASE_ERROR,
                "database",
                "database connection is not configured",
                retryable=False,
            ) from exc
        if info is None:
            raise RpcFault(
                DATABASE_ERROR,
                "database",
                "database connection is not configured",
                retryable=False,
            )
        # This mapping is process-local and is never copied into an RPC
        # response. In particular, no credential can arrive from Client.
        result = self.executor.execute(
            query_id=query_id,
            semantic_sql=semantic_sql,
            native_sql=native_sql,
            project_dir=project_dir,
            connection_info=info,
            limits=limits,
        )
        presentation = _apply_chart_spec(result, chart_intent)
        presentation["question"] = question
        if sql_history:
            presentation["sqlHistory"] = sql_history
        return presentation

    def cancel(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Request cancellation by query id; unknown ids are harmless."""

        if set(params) != {"queryId"}:
            raise RpcFault(INVALID_PARAMS, "validation", "query.cancel params are invalid")
        query_id = _query_id(params.get("queryId"))
        return {"queryId": query_id, "cancelled": bool(self.executor.cancel(query_id))}


class PsycopgQueryExecutor:
    """Bounded, read-only PostgreSQL executor with cross-thread cancellation."""

    def __init__(
        self,
        *,
        connection_factory: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self._connection_factory = connection_factory or self._default_connection
        self._active: dict[str, _ActiveQuery] = {}
        self._lock = threading.RLock()
        # Keep the direct executor limit equal to the protocol server's worker
        # limit.  This remains effective for embedders that call the service
        # without JsonRpcServer.
        self._slots = threading.BoundedSemaphore(MAX_QUERY_CONCURRENCY)

    def execute(
        self,
        *,
        query_id: str,
        semantic_sql: str,
        native_sql: str,
        project_dir: str,
        connection_info: Mapping[str, Any] | None,
        limits: QueryLimits,
    ) -> dict[str, Any]:
        del project_dir  # Resolution happens before this boundary.
        _assert_executor_limits(limits)
        try:
            native_sql = validate_native_sql(native_sql)
        except SqlPolicyError as exc:
            raise RpcFault(
                POLICY_DENIED,
                "policy",
                "query denied by read-only SQL policy",
                retryable=False,
            ) from exc

        if not self._slots.acquire(blocking=False):
            raise RpcFault(
                DATABASE_ERROR,
                "concurrency",
                "query concurrency limit reached",
                retryable=True,
            )

        started = time.monotonic()
        state: _ActiveQuery | None = None
        deadline_timer: threading.Timer | None = None
        try:
            connection = self._connection_factory(connection_info or {})
        except RpcFault:
            self._slots.release()
            raise
        except ImportError as exc:
            self._slots.release()
            raise RpcFault(
                DATABASE_ERROR,
                "database",
                "PostgreSQL driver is unavailable",
                retryable=True,
            ) from exc
        except Exception as exc:
            self._slots.release()
            raise RpcFault(
                DATABASE_ERROR,
                "database",
                "database connection failed",
                retryable=True,
            ) from exc

        try:
            with self._lock:
                if query_id in self._active:
                    self._close_connection(connection)
                    state = None
                    raise RpcFault(
                        INVALID_PARAMS,
                        "validation",
                        "queryId is already running",
                        retryable=False,
                    )
                state = _ActiveQuery(connection=connection)
                self._active[query_id] = state

            # The database statement timeout is the primary hard wall.  The
            # watchdog also covers an adapter/driver that ignores SET LOCAL or
            # blocks in execute/fetch, and races safely with query.cancel.
            def expire() -> None:
                assert state is not None
                state.timeout_requested.set()
                self.cancel(query_id)

            deadline_timer = threading.Timer(limits.timeout_ms / 1000.0, expire)
            deadline_timer.daemon = True
            deadline_timer.start()

            self._configure_read_only(connection, limits.timeout_ms)
            cursor = connection.cursor()
            try:
                cursor.execute(_bounded_select_sql(native_sql, limits.max_rows))
                return self._collect_result(
                    cursor,
                    query_id=query_id,
                    semantic_sql=semantic_sql,
                    native_sql=native_sql,
                    limits=limits,
                    started=started,
                )
            finally:
                close_cursor = getattr(cursor, "close", None)
                if callable(close_cursor):
                    try:
                        close_cursor()
                    except Exception:
                        pass
        except RpcFault as fault:
            # A cancel can race with configuration/driver code that already
            # translated its own failure into RpcFault. Preserve the explicit
            # timeout/cancel outcome for that race.
            if state is not None and state.timeout_requested.is_set():
                raise RpcFault(
                    TIMEOUT,
                    "database",
                    "query timed out",
                    retryable=True,
                ) from fault
            if (
                state is not None
                and state.cancel_requested.is_set()
                and fault.error.code == DATABASE_ERROR
            ):
                raise RpcFault(
                    CANCELLED,
                    "cancel",
                    "query was cancelled",
                    retryable=False,
                ) from fault
            raise
        except Exception as exc:
            if state is not None and state.timeout_requested.is_set():
                raise RpcFault(
                    TIMEOUT,
                    "database",
                    "query timed out",
                    retryable=True,
                ) from exc
            if state is not None and state.cancel_requested.is_set():
                raise RpcFault(
                    CANCELLED,
                    "cancel",
                    "query was cancelled",
                    retryable=False,
                ) from exc
            if _is_timeout_exception(exc):
                raise RpcFault(
                    TIMEOUT,
                    "database",
                    "query timed out",
                    retryable=True,
                ) from exc
            raise RpcFault(
                DATABASE_ERROR,
                "database",
                "database query failed",
                retryable=False,
            ) from exc
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            if state is not None:
                with self._lock:
                    self._active.pop(query_id, None)
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    try:
                        rollback()
                    except Exception:
                        pass
                self._close_connection(connection)
            self._slots.release()

    def cancel(self, query_id: str) -> bool:
        """Mark an active query and invoke the driver's cancellation hook."""

        with self._lock:
            state = self._active.get(query_id)
            if state is None:
                return False
            state.cancel_requested.set()
            connection = state.connection
        cancel = getattr(connection, "cancel", None)
        if not callable(cancel):
            return False
        try:
            cancel()
        except Exception:
            # The run worker will convert a resulting driver failure to a
            # stable DATABASE_ERROR/CANCELLED response.  Do not leak details.
            return False
        return True

    @staticmethod
    def _configure_read_only(connection: Any, timeout_ms: int) -> None:
        set_session = getattr(connection, "set_session", None)
        if callable(set_session):
            try:
                # psycopg3's public spelling is ``readonly``.
                set_session(readonly=True, autocommit=False)
            except TypeError:
                # Keep injected DB-API adapters useful if they expose the
                # alternate spelling used by some wrappers.
                set_session(read_only=True, autocommit=False)
        else:
            begin = getattr(connection, "execute", None)
            if not callable(begin):
                raise RpcFault(
                    DATABASE_ERROR,
                    "database",
                    "database does not support read-only transactions",
                    retryable=False,
                )
            begin("BEGIN READ ONLY")
        cursor = connection.cursor()
        try:
            # timeout_ms has already been type/range checked, so interpolating
            # this one integer avoids relying on server-specific SET binding.
            cursor.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
        finally:
            close_cursor = getattr(cursor, "close", None)
            if callable(close_cursor):
                try:
                    close_cursor()
                except Exception:
                    pass

    @staticmethod
    def _collect_result(
        cursor: Any,
        *,
        query_id: str,
        semantic_sql: str,
        native_sql: str,
        limits: QueryLimits,
        started: float,
    ) -> dict[str, Any]:
        description = getattr(cursor, "description", None) or []
        specs = _column_specs(description)
        columns = [
            {"name": name, "type": type_name, "semanticRole": role}
            for name, type_name, role in specs
        ]
        preview_rows: list[dict[str, Any]] = []
        preview_bytes = 2  # JSON array brackets.
        returned_rows = 0
        truncated = False
        for index, row in enumerate(_iter_rows(cursor, limits.max_rows + 1)):
            if index >= limits.max_rows:
                truncated = True
                break
            returned_rows += 1
            values = row if isinstance(row, (list, tuple)) else tuple(row)
            mapped: dict[str, Any] = {}
            for column_index, (name, type_name, _role) in enumerate(specs):
                value = values[column_index] if column_index < len(values) else None
                mapped[name] = _scalar_for_column(value, type_name)
            if len(preview_rows) >= limits.preview_rows:
                truncated = True
                continue
            row_bytes = _row_json_bytes(mapped)
            # Account for commas between rows.  If this row would cross the
            # cap, stop retaining previews but continue counting bounded rows.
            additional = row_bytes + (1 if preview_rows else 0)
            if preview_bytes + additional > limits.max_bytes:
                truncated = True
                continue
            preview_rows.append(mapped)
            preview_bytes += additional

        duration_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "queryId": query_id,
            "status": "success",
            "semanticSql": semantic_sql,
            "nativeSql": native_sql,
            "columns": columns,
            "previewRows": preview_rows,
            "stats": {
                "returnedRows": returned_rows,
                "durationMs": round(duration_ms, 3),
                "truncated": truncated,
            },
        }

    @staticmethod
    def _close_connection(connection: Any) -> None:
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    @staticmethod
    def _default_connection(connection_info: Mapping[str, Any]) -> Any:
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError:
            raise
        data = dict(connection_info)
        # Profiles store datasource alongside connection fields.  It is not a
        # psycopg keyword and is intentionally discarded at this boundary.
        data.pop("datasource", None)
        data.pop("dataSource", None)
        dsn = data.pop("connectionUrl", data.pop("connection_url", None))
        if dsn is not None:
            if not isinstance(dsn, str) or not dsn.strip():
                raise ValueError("invalid PostgreSQL connection URL")
            return psycopg.connect(dsn, connect_timeout=30)

        allowed = {
            "host",
            "port",
            "dbname",
            "database",
            "user",
            "password",
            "sslmode",
            "sslrootcert",
            "sslcert",
            "sslkey",
            "application_name",
            "connect_timeout",
        }
        kwargs: dict[str, Any] = {}
        for key in allowed:
            if key in data and data[key] is not None:
                value = data[key]
                getter = getattr(value, "get_secret_value", None)
                kwargs[key] = getter() if callable(getter) else value
        if "database" in kwargs:
            kwargs["dbname"] = kwargs.pop("database")
        kwargs.setdefault("connect_timeout", 30)
        if "host" not in kwargs or "dbname" not in kwargs or "user" not in kwargs:
            raise ValueError("incomplete PostgreSQL connection information")
        return psycopg.connect(**kwargs)


def _assert_executor_limits(limits: QueryLimits) -> None:
    """Apply hard limits even when an executor is called without Dispatcher."""

    if (
        type(limits.timeout_ms) is not int
        or not 1 <= limits.timeout_ms <= MAX_TIMEOUT_MS
        or type(limits.max_rows) is not int
        or not 1 <= limits.max_rows <= MAX_QUERY_ROWS
        or type(limits.preview_rows) is not int
        or not 1 <= limits.preview_rows <= MAX_PREVIEW_ROWS
        or type(limits.max_bytes) is not int
        or not 1 <= limits.max_bytes <= MAX_PREVIEW_BYTES
    ):
        raise RpcFault(
            INVALID_PARAMS,
            "validation",
            "query limits are outside the supported range",
            retryable=False,
        )


def _bounded_select_sql(native_sql: str, max_rows: int) -> str:
    """Wrap one parsed SELECT so PostgreSQL returns at most maxRows+1 rows.

    ``max_rows`` is validated before this function is called.  Rendering from
    the AST removes a trailing semicolon safely (including when comments or
    dollar-quoted literals are present) and keeps the wrapper itself free of
    model-controlled identifiers.
    """

    try:
        from sqlglot import parse_one

        normalized = parse_one(native_sql, read="postgres").sql(dialect="postgres")
    except Exception as exc:
        raise RpcFault(
            POLICY_DENIED,
            "policy",
            "query denied by read-only SQL policy",
            retryable=False,
        ) from exc
    return f"SELECT * FROM ({normalized}) AS __dsh_bounded_result LIMIT {max_rows + 1}"


def _iter_rows(cursor: Any, maximum: int) -> Iterable[Any]:
    fetchone = getattr(cursor, "fetchone", None)
    if callable(fetchone):
        for _ in range(maximum):
            row = fetchone()
            if row is None:
                return
            yield row
        return
    fetchmany = getattr(cursor, "fetchmany", None)
    if callable(fetchmany):
        remaining = maximum
        while remaining > 0:
            batch = fetchmany(min(remaining, 64))
            if not batch:
                return
            for row in batch:
                yield row
                remaining -= 1
                if remaining <= 0:
                    return
        return
    fetchall = getattr(cursor, "fetchall", None)
    if callable(fetchall):
        # The executor wraps every native SELECT with LIMIT maxRows+1 before
        # calling the driver.  Do not copy the driver's result into another
        # list; the wrapper is what makes even a fetchall-only test adapter
        # bounded in memory.
        rows = fetchall()
        for index, row in enumerate(rows):
            if index >= maximum:
                return
            yield row


def _is_timeout_exception(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    module = type(exc).__module__.lower()
    return "timeout" in name or "querycanceled" in name and "psycopg" in module


class EnvPsycopgExecutor(PsycopgQueryExecutor):
    """Psycopg executor for a process-local DSN supplied by the sidecar."""

    def execute(
        self,
        *,
        query_id: str,
        semantic_sql: str,
        native_sql: str,
        project_dir: str,
        connection_info: Mapping[str, Any] | None,
        limits: QueryLimits,
    ) -> dict[str, Any]:
        info = connection_info
        if info is None:
            raise RpcFault(
                DATABASE_ERROR,
                "database",
                "database connection is not configured",
                retryable=False,
            )
        datasource = info.get("datasource", info.get("dataSource"))
        if datasource is not None and str(datasource).lower() != "postgres":
            raise RpcFault(
                POLICY_DENIED,
                "policy",
                "only PostgreSQL execution is enabled",
                retryable=False,
            )
        return super().execute(
            query_id=query_id,
            semantic_sql=semantic_sql,
            native_sql=native_sql,
            project_dir=project_dir,
            connection_info=info,
            limits=limits,
        )


# Short aliases make the seam discoverable to Host adapters and tests.
QueryExecutor = DatabaseExecutor
ProfilePsycopgExecutor = EnvPsycopgExecutor
PostgresQueryExecutor = EnvPsycopgExecutor
