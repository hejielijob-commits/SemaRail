"""Resolve the active Semantic Console datasource without crossing the wire.

The Console keeps credentials in a project-scoped state directory under the
current OS user's home.  The query sidecar reads only the active profile at
execution time, so changing a connection does not require a Harness restart.
Nothing from this module is copied into RPC results or diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


MAX_STATE_BYTES = 1_048_576


class DatasourceStateError(RuntimeError):
    """Safe active-datasource failure with no credential-bearing detail."""


def semantic_console_state_file(
    project_dir: str | Path,
    *,
    home: str | Path | None = None,
) -> Path:
    """Return the Console state file for one canonical Wren project."""

    project = Path(project_dir).expanduser().resolve()
    digest = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:16]
    root = Path(home).expanduser().resolve() if home is not None else Path.home()
    return root / ".wren" / "semantic-console" / digest / "datasources.secrets.json"


def _records(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("datasources", "records"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    # Compatibility with the first Console draft, where the root object was
    # the record map and each value carried its own id/type/connection.
    if all(isinstance(value, Mapping) for value in payload.values()):
        return payload
    return {}


def _active_record(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    records = _records(payload)
    active_id = payload.get("activeDatasourceId", payload.get("active_datasource_id"))
    if isinstance(active_id, str) and active_id:
        record = records.get(active_id)
        return record if isinstance(record, Mapping) else None
    marked = [
        record
        for record in records.values()
        if isinstance(record, Mapping) and record.get("active") is True
    ]
    if len(marked) == 1:
        return marked[0]
    # Backward-compatible, deterministic first-run behavior: a state file
    # containing exactly one profile has no ambiguous selection.
    candidates = [record for record in records.values() if isinstance(record, Mapping)]
    return candidates[0] if len(candidates) == 1 else None


def _safe_connection(record: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(record.get("type", record.get("datasource", ""))).strip().lower()
    if kind == "postgresql":
        kind = "postgres"
    if kind != "postgres":
        raise DatasourceStateError("the active datasource is not enabled for queries")
    connection = record.get("connection")
    if not isinstance(connection, Mapping) or len(connection) > 64:
        raise DatasourceStateError("the active datasource configuration is invalid")
    result: dict[str, Any] = {"datasource": "postgres"}
    for key, value in connection.items():
        if not isinstance(key, str) or len(key) > 128:
            raise DatasourceStateError("the active datasource configuration is invalid")
        if value is None or isinstance(value, (str, int, float, bool)):
            if isinstance(value, str) and len(value) > 65_536:
                raise DatasourceStateError("the active datasource configuration is invalid")
            result[key] = value
        else:
            raise DatasourceStateError("the active datasource configuration is invalid")
    return result


def load_active_connection(
    project_dir: str | Path,
    env_name: str,
    *,
    environ: Mapping[str, str] | None = None,
    state_file: str | Path | None = None,
) -> Mapping[str, Any] | None:
    """Load the active local profile, falling back to the legacy DSN env.

    A missing state file or an unselected profile preserves the existing
    ``WREN_DATABASE_URL`` deployment behavior.  A selected but malformed or
    unsupported profile fails closed instead of silently querying a different
    database from the environment.
    """

    candidate = (
        Path(state_file).expanduser().resolve()
        if state_file is not None
        else semantic_console_state_file(project_dir)
    )
    if candidate.exists():
        if candidate.is_symlink() or not candidate.is_file():
            raise DatasourceStateError("the datasource state file is invalid")
        try:
            if candidate.stat().st_size > MAX_STATE_BYTES:
                raise DatasourceStateError("the datasource state file is too large")
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except DatasourceStateError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise DatasourceStateError("the datasource state file is invalid") from exc
        if not isinstance(payload, Mapping):
            raise DatasourceStateError("the datasource state file is invalid")
        active = _active_record(payload)
        if active is not None:
            return _safe_connection(active)

    environment = os.environ if environ is None else environ
    dsn = environment.get(env_name)
    if not dsn:
        return None
    return {"connectionUrl": dsn, "datasource": "postgres"}


__all__ = [
    "DatasourceStateError",
    "load_active_connection",
    "semantic_console_state_file",
]
