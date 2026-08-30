"""Small, dependency-free data contracts for the Semantic Console server.

The web layer deliberately uses dictionaries at its JSON boundary.  These
dataclasses keep the state held by the service explicit and, most importantly,
make it difficult to accidentally include a datasource password in a public
representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "client_secret",
        "private_key",
        "credentials",
        "credential",
        "dsn",
        "connection_url",
        "connectionurl",
    }
)


def utc_now() -> str:
    """Return a stable, JSON-safe UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_sensitive_key(key: object) -> bool:
    """Whether a request/configuration key must never cross the JSON boundary."""

    if not isinstance(key, str):
        return False
    normalized = key.replace("-", "_").lower()
    return normalized in SENSITIVE_KEYS or any(
        marker in normalized
        for marker in ("password", "secret", "token", "private_key", "api_key")
    )


def public_connection_fields(values: dict[str, Any]) -> dict[str, Any]:
    """Copy non-secret connection fields for a datasource response.

    This function is intentionally allow-list-ish: unknown fields are retained
    only when they are not obviously sensitive.  It is used on every read path
    instead of relying on callers to remember to remove ``password``.
    """

    result: dict[str, Any] = {}
    for key, value in values.items():
        if is_sensitive_key(key):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
    return result


@dataclass
class DatasourceRecord:
    """A datasource profile with secrets kept in a private in-memory field."""

    id: str
    name: str
    type: str
    connection: dict[str, Any] = field(repr=False)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_test: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        """Return the redacted datasource shape used by all API responses."""

        fields = public_connection_fields(self.connection)
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "connection": fields,
            "hasPassword": bool(self.connection.get("password")),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastTest": self.last_test,
        }

@dataclass(frozen=True)
class VersionRecord:
    """A published project snapshot kept outside the semantic project directory."""

    id: str
    revision: str
    created_at: str
    file_count: int
    label: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision": self.revision,
            "createdAt": self.created_at,
            "fileCount": self.file_count,
            **({"label": self.label} if self.label else {}),
        }


@dataclass(frozen=True)
class ApiError:
    """Stable error payload; ``details`` must remain JSON-safe and redacted."""

    code: str
    message: str
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            **({"details": self.details} if self.details else {}),
        }
