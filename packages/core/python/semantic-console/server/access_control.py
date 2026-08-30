"""Durable identities, API credentials, policy bindings, and audit events.

The control-plane database is intentionally separate from every analytical
datasource. API-key plaintext is returned once and never persisted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


DEFAULT_ORGANIZATION_ID = "default"
BOOTSTRAP_SUBJECT_ID = "bootstrap-admin"
_KEY_PATTERN = re.compile(r"sr_live_([a-f0-9]{24})_([A-Za-z0-9_-]{32,128})\Z")
_PBKDF2_ITERATIONS = 210_000


class AccessControlError(Exception):
    """Stable error safe to expose from an access-control API."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.status = status


@dataclass(frozen=True)
class Subject:
    """One authenticated human or non-human actor."""

    id: str
    organization_id: str
    kind: str
    name: str
    attributes: Mapping[str, Any]
    status: str = "active"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "organizationId": self.organization_id,
            "type": self.kind,
            "name": self.name,
            "attributes": dict(self.attributes),
            "status": self.status,
        }


@dataclass(frozen=True)
class AuthContext:
    """Verified request identity before authorization is evaluated."""

    subject: Subject
    method: str
    credential_id: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise AccessControlError("INVALID_EXPIRY", "credential expiry is invalid") from exc


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AccessControlError("INVALID_REQUEST", f"{field} must be an object")
    try:
        encoded = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise AccessControlError("INVALID_REQUEST", f"{field} must be JSON-safe") from exc
    if not isinstance(decoded, dict):  # pragma: no cover - mapping invariant
        raise AccessControlError("INVALID_REQUEST", f"{field} must be an object")
    return decoded


def _validate_name(value: Any, *, field: str = "name") -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120:
        raise AccessControlError("INVALID_REQUEST", f"{field} is invalid")
    return value.strip()


class AccessControlStore:
    """SQLite-backed control plane for identities and authorization metadata."""

    def __init__(
        self,
        path: str | Path,
        *,
        bootstrap_token: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.bootstrap_token = (bootstrap_token if bootstrap_token is not None else os.environ.get("SEMARAIL_API_TOKEN", "")).strip()
        self.clock = clock
        self._lock = threading.RLock()
        self._initialize()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS organizations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subjects (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL REFERENCES organizations(id),
                    kind TEXT NOT NULL CHECK(kind IN ('user','service_account')),
                    name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','disabled')),
                    attributes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL REFERENCES subjects(id),
                    label TEXT NOT NULL,
                    salt BLOB NOT NULL,
                    secret_hash BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT,
                    last_used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS policies (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL REFERENCES organizations(id),
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policy_bindings (
                    subject_id TEXT NOT NULL REFERENCES subjects(id),
                    policy_id TEXT NOT NULL REFERENCES policies(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(subject_id, policy_id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    organization_id TEXT,
                    subject_id TEXT,
                    credential_id TEXT,
                    action TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    resource TEXT,
                    policy_version TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS credentials_subject_idx ON credentials(subject_id);
                CREATE INDEX IF NOT EXISTS audit_occurred_idx ON audit_events(occurred_at);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO organizations(id,name,created_at) VALUES(?,?,?)",
                (DEFAULT_ORGANIZATION_ID, "Default organization", _timestamp(self.clock())),
            )

    def create_service_account(
        self,
        name: str,
        *,
        organization_id: str = DEFAULT_ORGANIZATION_ID,
        attributes: Mapping[str, Any] | None = None,
    ) -> Subject:
        account_name = _validate_name(name)
        organization = _validate_name(organization_id, field="organizationId")
        safe_attributes = _json_object(attributes, field="attributes")
        subject_id = f"svc_{uuid.uuid4().hex}"
        now = _timestamp(self.clock())
        with self._lock, self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM organizations WHERE id=?", (organization,)).fetchone()
            if exists is None:
                raise AccessControlError("ORGANIZATION_NOT_FOUND", "organization was not found", status=404)
            connection.execute(
                "INSERT INTO subjects(id,organization_id,kind,name,status,attributes_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (subject_id, organization, "service_account", account_name, "active", json.dumps(safe_attributes, separators=(",", ":")), now, now),
            )
        return Subject(subject_id, organization, "service_account", account_name, safe_attributes)

    def subject(self, subject_id: str) -> Subject:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM subjects WHERE id=?", (subject_id,)).fetchone()
        if row is None:
            raise AccessControlError("SUBJECT_NOT_FOUND", "subject was not found", status=404)
        return self._subject_from_row(row)

    def list_service_accounts(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM subjects WHERE kind='service_account' ORDER BY created_at,id"
            ).fetchall()
            credentials = connection.execute(
                "SELECT id,subject_id,label,created_at,expires_at,revoked_at,last_used_at FROM credentials ORDER BY created_at,id"
            ).fetchall()
        by_subject: dict[str, list[dict[str, Any]]] = {}
        for row in credentials:
            by_subject.setdefault(str(row["subject_id"]), []).append(self._credential_public(row))
        return [{**self._subject_from_row(row).as_dict(), "credentials": by_subject.get(str(row["id"]), [])} for row in rows]

    def set_subject_status(self, subject_id: str, status: str) -> Subject:
        if status not in {"active", "disabled"}:
            raise AccessControlError("INVALID_REQUEST", "subject status is invalid")
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                "UPDATE subjects SET status=?,updated_at=? WHERE id=?",
                (status, _timestamp(self.clock()), subject_id),
            ).rowcount
        if not changed:
            raise AccessControlError("SUBJECT_NOT_FOUND", "subject was not found", status=404)
        return self.subject(subject_id)

    def update_service_account(
        self,
        subject_id: str,
        *,
        name: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Subject:
        """Update trusted service-account attributes used by policy resolution."""

        current = self.subject(subject_id)
        if current.kind != "service_account":
            raise AccessControlError("SUBJECT_NOT_FOUND", "service account was not found", status=404)
        account_name = current.name if name is None else _validate_name(name)
        safe_attributes = dict(current.attributes) if attributes is None else _json_object(attributes, field="attributes")
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE subjects SET name=?,attributes_json=?,updated_at=? WHERE id=?",
                (account_name, json.dumps(safe_attributes, separators=(",", ":")), _timestamp(self.clock()), subject_id),
            )
        return self.subject(subject_id)

    def issue_api_key(self, subject_id: str, *, label: str = "default", expires_at: str | None = None) -> dict[str, Any]:
        key_label = _validate_name(label, field="label")
        expiry = _parse_timestamp(expires_at)
        if expiry is not None and expiry <= self.clock().astimezone(UTC):
            raise AccessControlError("INVALID_EXPIRY", "credential expiry must be in the future")
        subject = self.subject(subject_id)
        if subject.status != "active":
            raise AccessControlError("SUBJECT_DISABLED", "subject is disabled", status=409)
        credential_id = secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        digest = self._derive(secret, salt)
        created_at = _timestamp(self.clock())
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO credentials(id,subject_id,label,salt,secret_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (credential_id, subject_id, key_label, salt, digest, created_at, _timestamp(expiry) if expiry else None),
            )
        return {
            "apiKey": f"sr_live_{credential_id}_{secret}",
            "credential": {
                "id": credential_id,
                "subjectId": subject_id,
                "label": key_label,
                "createdAt": created_at,
                "expiresAt": _timestamp(expiry) if expiry else None,
                "revokedAt": None,
                "lastUsedAt": None,
            },
        }

    def revoke_credential(self, credential_id: str) -> dict[str, Any]:
        now = _timestamp(self.clock())
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                "UPDATE credentials SET revoked_at=COALESCE(revoked_at,?) WHERE id=?",
                (now, credential_id),
            ).rowcount
            row = connection.execute(
                "SELECT id,subject_id,label,created_at,expires_at,revoked_at,last_used_at FROM credentials WHERE id=?",
                (credential_id,),
            ).fetchone()
        if not changed or row is None:
            raise AccessControlError("CREDENTIAL_NOT_FOUND", "credential was not found", status=404)
        return self._credential_public(row)

    def rotate_credential(
        self,
        credential_id: str,
        *,
        label: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Replace one live API key and revoke it in the same transaction."""

        expiry = _parse_timestamp(expires_at)
        if expiry is not None and expiry <= self.clock().astimezone(UTC):
            raise AccessControlError("INVALID_EXPIRY", "credential expiry must be in the future")
        new_id = secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        digest = self._derive(secret, salt)
        now = _timestamp(self.clock())
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT c.subject_id,c.label,c.revoked_at,s.status FROM credentials c "
                "JOIN subjects s ON s.id=c.subject_id WHERE c.id=?",
                (credential_id,),
            ).fetchone()
            if current is None:
                raise AccessControlError("CREDENTIAL_NOT_FOUND", "credential was not found", status=404)
            if current["revoked_at"] is not None:
                raise AccessControlError("CREDENTIAL_REVOKED", "credential is already revoked", status=409)
            if current["status"] != "active":
                raise AccessControlError("SUBJECT_DISABLED", "subject is disabled", status=409)
            key_label = _validate_name(label if label is not None else current["label"], field="label")
            connection.execute(
                "INSERT INTO credentials(id,subject_id,label,salt,secret_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (new_id, current["subject_id"], key_label, salt, digest, now, _timestamp(expiry) if expiry else None),
            )
            connection.execute("UPDATE credentials SET revoked_at=? WHERE id=?", (now, credential_id))
        return {
            "apiKey": f"sr_live_{new_id}_{secret}",
            "credential": {
                "id": new_id,
                "subjectId": current["subject_id"],
                "label": key_label,
                "createdAt": now,
                "expiresAt": _timestamp(expiry) if expiry else None,
                "revokedAt": None,
                "lastUsedAt": None,
            },
            "replacedCredentialId": credential_id,
        }

    def authenticate(self, authorization: str | None) -> AuthContext:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise AccessControlError("UNAUTHENTICATED", "authentication is required", status=401)
        token = authorization[7:]
        if len(self.bootstrap_token) >= 32 and secrets.compare_digest(token, self.bootstrap_token):
            return AuthContext(
                Subject(BOOTSTRAP_SUBJECT_ID, DEFAULT_ORGANIZATION_ID, "user", "Bootstrap administrator", {"roles": ["admin"]}),
                "bootstrap_token",
            )
        match = _KEY_PATTERN.fullmatch(token)
        if match is None:
            raise AccessControlError("UNAUTHENTICATED", "authentication is required", status=401)
        credential_id, secret = match.groups()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT c.*,s.organization_id,s.kind,s.name,s.status,s.attributes_json FROM credentials c JOIN subjects s ON s.id=c.subject_id WHERE c.id=?",
                (credential_id,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None or row["status"] != "active":
                raise AccessControlError("UNAUTHENTICATED", "authentication is required", status=401)
            expiry = _parse_timestamp(row["expires_at"])
            if expiry is not None and expiry <= self.clock().astimezone(UTC):
                raise AccessControlError("UNAUTHENTICATED", "authentication is required", status=401)
            if not secrets.compare_digest(self._derive(secret, bytes(row["salt"])), bytes(row["secret_hash"])):
                raise AccessControlError("UNAUTHENTICATED", "authentication is required", status=401)
            connection.execute("UPDATE credentials SET last_used_at=? WHERE id=?", (_timestamp(self.clock()), credential_id))
        subject = Subject(
            str(row["subject_id"]),
            str(row["organization_id"]),
            str(row["kind"]),
            str(row["name"]),
            json.loads(str(row["attributes_json"])),
            str(row["status"]),
        )
        return AuthContext(subject, "api_key", credential_id)

    def create_policy(self, name: str, document: Mapping[str, Any], *, organization_id: str = DEFAULT_ORGANIZATION_ID) -> dict[str, Any]:
        policy_name = _validate_name(name)
        safe_document = _json_object(document, field="document")
        policy_id = f"pol_{uuid.uuid4().hex}"
        now = _timestamp(self.clock())
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO policies(id,organization_id,name,version,document_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (policy_id, organization_id, policy_name, 1, json.dumps(safe_document, separators=(",", ":")), now, now),
            )
        return {"id": policy_id, "organizationId": organization_id, "name": policy_name, "version": 1, "document": safe_document}

    def update_policy(self, policy_id: str, document: Mapping[str, Any]) -> dict[str, Any]:
        """Replace a policy document and monotonically advance its version."""

        safe_document = _json_object(document, field="document")
        now = _timestamp(self.clock())
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                "UPDATE policies SET document_json=?,version=version+1,updated_at=? WHERE id=?",
                (json.dumps(safe_document, separators=(",", ":")), now, policy_id),
            ).rowcount
            row = connection.execute("SELECT * FROM policies WHERE id=?", (policy_id,)).fetchone()
        if not changed or row is None:
            raise AccessControlError("POLICY_NOT_FOUND", "policy was not found", status=404)
        return {
            "id": row["id"],
            "organizationId": row["organization_id"],
            "name": row["name"],
            "version": row["version"],
            "document": json.loads(row["document_json"]),
        }

    def list_policies(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM policies ORDER BY created_at,id").fetchall()
        return [
            {
                "id": row["id"],
                "organizationId": row["organization_id"],
                "name": row["name"],
                "version": row["version"],
                "document": json.loads(row["document_json"]),
            }
            for row in rows
        ]

    def bind_policy(self, subject_id: str, policy_id: str) -> None:
        subject = self.subject(subject_id)
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT organization_id FROM policies WHERE id=?", (policy_id,)).fetchone()
            if row is None:
                raise AccessControlError("POLICY_NOT_FOUND", "policy was not found", status=404)
            if row["organization_id"] != subject.organization_id:
                raise AccessControlError("ORGANIZATION_MISMATCH", "policy and subject organizations differ", status=409)
            connection.execute(
                "INSERT OR IGNORE INTO policy_bindings(subject_id,policy_id,created_at) VALUES(?,?,?)",
                (subject_id, policy_id, _timestamp(self.clock())),
            )

    def policies_for_subject(self, subject_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT p.* FROM policies p JOIN policy_bindings b ON b.policy_id=p.id WHERE b.subject_id=? ORDER BY p.id",
                (subject_id,),
            ).fetchall()
        return [
            {"id": row["id"], "organizationId": row["organization_id"], "name": row["name"], "version": row["version"], "document": json.loads(row["document_json"])}
            for row in rows
        ]

    def record_audit(
        self,
        *,
        action: str,
        decision: str,
        auth: AuthContext | None = None,
        resource: str | None = None,
        policy_version: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        if decision not in {"allowed", "denied", "error"}:
            raise AccessControlError("INVALID_AUDIT", "audit decision is invalid")
        safe_details = _json_object(details, field="details")
        event_id = f"aud_{uuid.uuid4().hex}"
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_events(id,occurred_at,organization_id,subject_id,credential_id,action,decision,resource,policy_version,details_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    _timestamp(self.clock()),
                    auth.subject.organization_id if auth else None,
                    auth.subject.id if auth else None,
                    auth.credential_id if auth else None,
                    _validate_name(action, field="action"),
                    decision,
                    resource,
                    policy_version,
                    json.dumps(safe_details, separators=(",", ":")),
                ),
            )
        return event_id

    def list_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY occurred_at DESC,id DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [
            {
                "id": row["id"], "occurredAt": row["occurred_at"], "organizationId": row["organization_id"],
                "subjectId": row["subject_id"], "credentialId": row["credential_id"], "action": row["action"],
                "decision": row["decision"], "resource": row["resource"], "policyVersion": row["policy_version"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _derive(secret: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, _PBKDF2_ITERATIONS)

    @staticmethod
    def _subject_from_row(row: sqlite3.Row) -> Subject:
        return Subject(str(row["id"]), str(row["organization_id"]), str(row["kind"]), str(row["name"]), json.loads(str(row["attributes_json"])), str(row["status"]))

    @staticmethod
    def _credential_public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "subjectId": row["subject_id"], "label": row["label"],
            "createdAt": row["created_at"], "expiresAt": row["expires_at"],
            "revokedAt": row["revoked_at"], "lastUsedAt": row["last_used_at"],
        }


__all__ = [
    "AccessControlError", "AccessControlStore", "AuthContext", "BOOTSTRAP_SUBJECT_ID",
    "DEFAULT_ORGANIZATION_ID", "Subject",
]
