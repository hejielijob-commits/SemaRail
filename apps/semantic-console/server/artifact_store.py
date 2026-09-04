"""Short-lived, subject-bound query artifacts.

Artifacts are an intentionally small control-plane primitive.  The sidecar is
allowed to produce a file for an artifact reservation, but it never owns the
download credential or a public URL.  Core owns the random name, token, expiry,
and all checks performed immediately before a download.

Only the artifact metadata database and files in ``<state_dir>/artifacts`` are
managed here.  In particular, this module never accepts a caller supplied
filesystem path.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Callable

try:
    from .access_control import AccessControlStore
except ImportError:  # pragma: no cover - direct module loading
    from access_control import AccessControlStore  # type: ignore[no-redef]


ARTIFACT_TTL_SECONDS = 15 * 60
MIN_ARTIFACT_TTL_SECONDS = 60
MAX_ARTIFACT_TTL_SECONDS = 24 * 60 * 60
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_INLINE_ROWS = 50
MAX_ARTIFACT_INLINE_BYTES = 128 * 1024
MAX_ARTIFACT_PREVIEW_ROWS = 20
MAX_ARTIFACT_ROWS = 500
CLEANUP_BATCH_SIZE = 100
ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_ARTIFACT_CONTENT_TYPE = "text/csv; charset=utf-8"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT_ID = re.compile(r"art_[a-f0-9]{32}\Z")
_ARTIFACT_FILENAME = re.compile(r"artifact-[a-f0-9]{32}\.csv\Z")
_EXTENSION = re.compile(r"[a-z0-9]{1,12}\Z")
_STATUSES = frozenset({"pending", "ready", "failed", "expired"})
_SIDECAR_METADATA_FIELDS = frozenset(
    {"id", "format", "fileName", "rowCount", "sizeBytes", "sha256", "expiresAt"}
)
_FORBIDDEN_SIDECAR_KEYS = frozenset(
    {
        "path",
        "localPath",
        "local_path",
        "filePath",
        "file_path",
        "token",
        "downloadUrl",
        "download_url",
        "url",
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404) from exc


class ArtifactError(RuntimeError):
    """Safe error raised by the artifact control plane."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.status = status


@dataclass(frozen=True, slots=True)
class ArtifactReservation:
    """Core-issued reservation; ``token`` is intentionally transient."""

    id: str
    filename: str
    content_type: str
    created_at: str
    expires_at: str
    subject_id: str
    organization_id: str
    credential_id: str
    query_id: str
    datasource_id: str
    policy_versions: tuple[str, ...]
    token: str
    # The directory is a Core-owned capability.  It is set by ``reserve`` and
    # never accepted from a public request or sidecar response.  Keeping it on
    # the reservation makes ``internal_request()`` safe on its own while the
    # store helper below remains useful for test/in-process dispatch seams.
    directory: str = ""

    def internal_request(self) -> dict[str, Any]:
        """Return the only artifact data accepted by the sidecar boundary."""

        return {
            "id": self.id,
            "directory": self.directory,
            "filename": self.filename,
            "inlineMaxRows": MAX_ARTIFACT_INLINE_ROWS,
            "inlineMaxBytes": MAX_ARTIFACT_INLINE_BYTES,
            "previewRows": MAX_ARTIFACT_PREVIEW_ROWS,
            "maxBytes": MAX_ARTIFACT_BYTES,
            "expiresAt": self.expires_at,
        }

    def public_metadata(
        self,
        *,
        size: int | None = None,
        row_count: int | None = None,
        sha256: str | None = None,
        download_path: str | None = None,
    ) -> dict[str, Any]:
        """Return a JSON-safe artifact descriptor for an authenticated caller."""

        result: dict[str, Any] = {
            "id": self.id,
            "format": "csv",
            "fileName": self.filename,
            "rowCount": row_count,
            "sizeBytes": size,
            "sha256": sha256,
            "expiresAt": self.expires_at,
        }
        if download_path is not None:
            result["downloadUrl"] = download_path
        return result


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Persisted metadata without the download token."""

    id: str
    filename: str
    content_type: str
    status: str
    created_at: str
    expires_at: str
    subject_id: str
    organization_id: str
    credential_id: str
    query_id: str
    datasource_id: str
    policy_versions: tuple[str, ...]
    size: int | None
    sha256: str | None
    row_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "contentType": self.content_type,
            "status": self.status,
            "queryId": self.query_id,
            "organizationId": self.organization_id,
            "size": self.size,
            "rowCount": self.row_count,
            "sha256": self.sha256,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    """An already-authorized immutable file ready for HTTP streaming."""

    metadata: ArtifactMetadata
    path: Path


class ArtifactStore:
    """Persist and authorize short-lived artifacts in a private state dir."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        access_control: AccessControlStore | None = None,
        clock: Callable[[], datetime] = _utc_now,
        ttl_seconds: int = ARTIFACT_TTL_SECONDS,
    ) -> None:
        if type(ttl_seconds) is not int or not MIN_ARTIFACT_TTL_SECONDS <= ttl_seconds <= MAX_ARTIFACT_TTL_SECONDS:
            raise ValueError("artifact TTL must be between 60 and 86400 seconds")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.root = self.state_dir / "artifacts"
        self.database_path = self.state_dir / "artifacts.sqlite3"
        self.access_control = access_control
        self.clock = clock
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        for candidate in (self.state_dir, self.root):
            try:
                os.chmod(candidate, 0o700)
            except OSError:
                pass
        self._initialize()
        # Startup cleanup is deliberately part of construction so stale files
        # left by a crashed Core process cannot accumulate.
        self.cleanup()

    @property
    def path(self) -> Path:
        """Compatibility alias for callers that refer to the metadata DB."""

        return self.database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
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
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL,
                    token_salt BLOB NOT NULL,
                    token_hash BLOB NOT NULL,
                    subject_id TEXT NOT NULL,
                    organization_id TEXT,
                    credential_id TEXT NOT NULL,
                    query_id TEXT,
                    datasource_id TEXT NOT NULL,
                    policy_versions_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','ready','failed','expired')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    size INTEGER,
                    row_count INTEGER,
                    sha256 TEXT,
                    CHECK(size IS NULL OR (size >= 0 AND size <= 16777216)),
                    CHECK(row_count IS NULL OR (row_count >= 0 AND row_count <= 500)),
                    CHECK(sha256 IS NULL OR length(sha256) = 64)
                );
                CREATE INDEX IF NOT EXISTS artifacts_expiry_idx ON artifacts(expires_at);
                CREATE INDEX IF NOT EXISTS artifacts_subject_idx ON artifacts(subject_id, credential_id);
                """
            )
            # Databases created by the first pre-release implementation did
            # not have a row count.  Migrate that one additive field without
            # rewriting or weakening any existing metadata.
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
            }
            if "row_count" not in columns:
                connection.execute("ALTER TABLE artifacts ADD COLUMN row_count INTEGER")
            if "organization_id" not in columns:
                connection.execute("ALTER TABLE artifacts ADD COLUMN organization_id TEXT")
            if "query_id" not in columns:
                connection.execute("ALTER TABLE artifacts ADD COLUMN query_id TEXT")
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            pass

    def cleanup(self) -> int:
        """Mark expired rows and remove expired files and orphan ``*.tmp`` files."""

        with self._lock:
            return self._cleanup_locked()

    def _cleanup_locked(self, *, limit: int | None = None) -> int:
        now = self.clock().astimezone(UTC)
        now_text = _timestamp(now)
        removed = 0
        with self._connect() as connection:
            statement = "SELECT id,filename,status FROM artifacts WHERE expires_at<=? AND status<>? ORDER BY expires_at,id"
            parameters: tuple[Any, ...] = (now_text, "expired")
            if limit is not None:
                statement += " LIMIT ?"
                parameters += (limit,)
            rows = connection.execute(statement, parameters).fetchall()
            for row in rows:
                connection.execute("UPDATE artifacts SET status='expired' WHERE id=?", (row["id"],))
            active_pending = connection.execute(
                "SELECT id,filename FROM artifacts WHERE status='pending' AND expires_at>?",
                (now_text,),
            ).fetchall()
        for row in rows:
            removed += self._unlink_filename(str(row["filename"]))
        # Sidecar writes happen outside this process lock. Preserve temporary
        # files that belong to a live pending reservation; another concurrent
        # query may otherwise unlink a CSV while it is still being streamed.
        pending_ids = {str(row["id"]) for row in active_pending}
        pending_filenames = {str(row["filename"]) for row in active_pending}
        try:
            temporary_budget = None if limit is None else max(0, limit - len(rows))
            for candidate in self.root.glob("*.tmp"):
                if temporary_budget == 0:
                    break
                name = candidate.name
                belongs_to_pending = any(
                    name.startswith(f".{artifact_id}.")
                    for artifact_id in pending_ids
                ) or any(
                    name.startswith(f"{filename}.")
                    for filename in pending_filenames
                )
                if belongs_to_pending:
                    continue
                if candidate.is_file() or candidate.is_symlink():
                    try:
                        candidate.unlink()
                        removed += 1
                        if temporary_budget is not None:
                            temporary_budget -= 1
                    except OSError:
                        pass
        except OSError:
            pass
        return removed

    def _unlink_filename(self, filename: str) -> int:
        if not _ARTIFACT_FILENAME.fullmatch(filename):
            return 0
        candidate = self.root / filename
        try:
            candidate.resolve().relative_to(self.root)
        except (OSError, ValueError):
            return 0
        try:
            candidate.unlink()
            return 1
        except FileNotFoundError:
            return 0
        except OSError:
            return 0

    def reserve(
        self,
        *,
        subject_id: str,
        organization_id: str,
        credential_id: str,
        query_id: str,
        datasource_id: str,
        policy_versions: tuple[str, ...] | list[str],
        content_type: str = DEFAULT_ARTIFACT_CONTENT_TYPE,
        extension: str = "csv",
    ) -> ArtifactReservation:
        """Create a Core-owned pending reservation and return its one-time token."""

        if not all(
            isinstance(value, str) and value.strip() and len(value.encode("utf-8")) <= 256
            for value in (subject_id, organization_id, credential_id, query_id, datasource_id)
        ):
            raise ArtifactError("ARTIFACT_INVALID_BINDING", "artifact binding is invalid")
        if not isinstance(content_type, str) or not 1 <= len(content_type) <= 200 or "\r" in content_type or "\n" in content_type:
            raise ArtifactError("ARTIFACT_INVALID_PARAMS", "artifact content type is invalid")
        if not isinstance(extension, str) or not _EXTENSION.fullmatch(extension.lower()):
            raise ArtifactError("ARTIFACT_INVALID_PARAMS", "artifact extension is invalid")
        if extension.lower() != "csv":
            raise ArtifactError("ARTIFACT_INVALID_PARAMS", "artifact format is invalid")
        versions = tuple(str(item) for item in policy_versions if isinstance(item, str))
        if len(versions) > 64 or any(not item or len(item.encode("utf-8")) > 256 for item in versions):
            raise ArtifactError("ARTIFACT_INVALID_BINDING", "artifact policy binding is invalid")
        with self._lock:
            self._cleanup_locked(limit=CLEANUP_BATCH_SIZE)
            created = self.clock().astimezone(UTC)
            expires = created + timedelta(seconds=self.ttl_seconds)
            # ID and filename are independent random values.  A caller never
            # gets to select either, including through a sidecar response.
            artifact_id = "art_" + secrets.token_hex(16)
            # ID and filename are independently random.  The sidecar receives
            # this Core-selected basename and may only use it as a leaf name.
            filename = "artifact-" + secrets.token_hex(16) + "." + extension.lower()
            token = secrets.token_urlsafe(32)
            salt = secrets.token_bytes(16)
            digest = self._token_digest(token, salt)
            created_text, expires_text = _timestamp(created), _timestamp(expires)
            try:
                with self._connect() as connection:
                    connection.execute(
                        "INSERT INTO artifacts(id,filename,content_type,token_salt,token_hash,subject_id,organization_id,credential_id,query_id,datasource_id,policy_versions_json,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            artifact_id,
                            filename,
                            content_type,
                            sqlite3.Binary(salt),
                            sqlite3.Binary(digest),
                            subject_id,
                            organization_id,
                            credential_id,
                            query_id,
                            datasource_id,
                            self._encode_versions(versions),
                            "pending",
                            created_text,
                            expires_text,
                        ),
                    )
            except sqlite3.Error as exc:
                raise ArtifactError("ARTIFACT_STORE_FAILED", "artifact store is unavailable", status=503) from exc
            return ArtifactReservation(
                artifact_id,
                filename,
                content_type,
                created_text,
                expires_text,
                subject_id,
                organization_id,
                credential_id,
                query_id,
                datasource_id,
                versions,
                token,
                str(self.root),
            )

    @staticmethod
    def _token_digest(token: str, salt: bytes) -> bytes:
        return hashlib.sha256(salt + token.encode("utf-8")).digest()

    @staticmethod
    def _encode_versions(versions: tuple[str, ...]) -> str:
        # Avoid importing json for one tiny, strictly string-only list.  JSON
        # encoding is still used so metadata remains independently inspectable.
        import json

        return json.dumps(list(versions), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_versions(raw: str) -> tuple[str, ...]:
        import json

        try:
            values = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404) from exc
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404)
        return tuple(values)

    def _row_metadata(self, row: Mapping[str, Any]) -> ArtifactMetadata:
        status = str(row["status"])
        if status not in _STATUSES:
            raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404)
        size = row["size"]
        if size is not None and (type(size) is not int or not 0 <= size <= MAX_ARTIFACT_BYTES):
            raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404)
        sha256 = row["sha256"]
        if sha256 is not None and (not isinstance(sha256, str) or not _HEX64.fullmatch(sha256)):
            raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404)
        row_count = row["row_count"]
        if row_count is not None and (type(row_count) is not int or not 0 <= row_count <= MAX_ARTIFACT_ROWS):
            raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404)
        bindings = {
            "subject_id": row["subject_id"],
            "organization_id": row["organization_id"],
            "credential_id": row["credential_id"],
            "query_id": row["query_id"],
            "datasource_id": row["datasource_id"],
        }
        if any(not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256 for value in bindings.values()):
            raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404)
        return ArtifactMetadata(
            id=str(row["id"]),
            filename=str(row["filename"]),
            content_type=str(row["content_type"]),
            status=status,
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            subject_id=bindings["subject_id"],
            organization_id=bindings["organization_id"],
            credential_id=bindings["credential_id"],
            query_id=bindings["query_id"],
            datasource_id=bindings["datasource_id"],
            policy_versions=self._decode_versions(str(row["policy_versions_json"])),
            size=size,
            sha256=sha256,
            row_count=row_count,
        )

    def request_for_sidecar(self, reservation: ArtifactReservation) -> dict[str, Any]:
        """Return the exact trusted Core-to-sidecar artifact capability."""

        request = reservation.internal_request()
        # Reservations produced by this store already carry this value.  The
        # assignment also protects a test/in-process reservation assembled
        # from persisted metadata without allowing any caller-selected path.
        request["directory"] = str(self.root)
        row = self._row(reservation.id)
        if row is None or not _ARTIFACT_FILENAME.fullmatch(str(row["filename"])):
            raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404)
        request["filename"] = str(row["filename"])
        return request

    def _row(self, artifact_id: str) -> sqlite3.Row | None:
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            return None
        with self._connect() as connection:
            return connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()

    def metadata(self, artifact_id: str) -> ArtifactMetadata:
        """Read persisted metadata without disclosing the token or path."""

        with self._lock:
            self._cleanup_locked()
            row = self._row(artifact_id)
            if row is None:
                raise ArtifactError("ARTIFACT_NOT_FOUND", "artifact was not found", status=404)
            return self._row_metadata(row)

    def finalize(
        self,
        artifact: ArtifactReservation | str,
        content: bytes | bytearray | memoryview | BinaryIO | Iterator[bytes],
        *,
        sidecar_metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactMetadata:
        """Atomically write bytes and transition a reservation to ``ready``."""

        artifact_id = artifact.id if isinstance(artifact, ArtifactReservation) else artifact
        with self._lock:
            self._cleanup_locked()
            row = self._row(artifact_id)
            if row is None:
                raise ArtifactError("ARTIFACT_NOT_FOUND", "artifact was not found", status=404)
            metadata = self._row_metadata(row)
            self._assert_pending(metadata)
            filename = metadata.filename
            if not _ARTIFACT_FILENAME.fullmatch(filename):
                raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact metadata is invalid", status=404)
            temporary = self.root / (filename + "." + secrets.token_hex(8) + ".tmp")
            digest = hashlib.sha256()
            size = 0
            try:
                with temporary.open("xb") as stream:
                    for chunk in self._chunks(content):
                        if not isinstance(chunk, (bytes, bytearray, memoryview)):
                            raise ArtifactError("ARTIFACT_INVALID_CONTENT", "artifact content is invalid")
                        raw = bytes(chunk)
                        size += len(raw)
                        if size > MAX_ARTIFACT_BYTES:
                            raise ArtifactError("ARTIFACT_TOO_LARGE", "artifact exceeds the 16 MiB limit", status=413)
                        digest.update(raw)
                        stream.write(raw)
                    stream.flush()
                    try:
                        os.fsync(stream.fileno())
                    except OSError:
                        pass
                final_path = self.root / filename
                os.replace(temporary, final_path)
                try:
                    os.chmod(final_path, 0o600)
                except OSError:
                    pass
                sha256 = digest.hexdigest()
                row_count = self._validate_sidecar_metadata(
                    sidecar_metadata,
                    artifact_id,
                    filename,
                    metadata.expires_at,
                    size,
                    sha256,
                )
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE artifacts SET status='ready',size=?,row_count=?,sha256=? WHERE id=? AND status='pending'",
                        (size, row_count, sha256, artifact_id),
                    )
                updated = self._row(artifact_id)
                if updated is None:
                    raise ArtifactError("ARTIFACT_STORE_FAILED", "artifact store is unavailable", status=503)
                return self._row_metadata(updated)
            except ArtifactError:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                self._mark_failed(artifact_id)
                raise
            except (OSError, sqlite3.Error) as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                self._mark_failed(artifact_id)
                raise ArtifactError("ARTIFACT_STORE_FAILED", "artifact could not be stored", status=503) from exc

    @staticmethod
    def _chunks(content: bytes | bytearray | memoryview | BinaryIO | Iterator[bytes]) -> Iterator[bytes]:
        if isinstance(content, (bytes, bytearray, memoryview)):
            yield bytes(content)
            return
        reader = getattr(content, "read", None)
        if callable(reader):
            while True:
                chunk = reader(1024 * 1024)
                if not chunk:
                    break
                yield chunk
            return
        yield from content

    @staticmethod
    def _assert_pending(metadata: ArtifactMetadata) -> None:
        if metadata.status == "expired":
            raise ArtifactError("ARTIFACT_EXPIRED", "artifact has expired", status=410)
        if metadata.status != "pending":
            raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact is unavailable", status=404)

    @staticmethod
    def _validate_sidecar_metadata(
        raw: Mapping[str, Any] | None,
        artifact_id: str,
        filename: str,
        expires_at: str,
        size: int,
        sha256: str,
    ) -> int | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
        if set(raw) != _SIDECAR_METADATA_FIELDS or any(key in raw for key in _FORBIDDEN_SIDECAR_KEYS):
            raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
        if (
            raw.get("id") != artifact_id
            or raw.get("format") != "csv"
            or raw.get("fileName") != filename
            or raw.get("expiresAt") != expires_at
        ):
            raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
        row_count = raw.get("rowCount")
        if type(row_count) is not int or not 0 <= row_count <= MAX_ARTIFACT_ROWS:
            raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
        reported_size = raw.get("sizeBytes")
        reported_sha = raw.get("sha256")
        if type(reported_size) is not int or reported_size != size or not isinstance(reported_sha, str) or reported_sha != sha256:
            raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
        return row_count

    def register_sidecar_result(
        self,
        artifact: ArtifactReservation | str,
        raw: Mapping[str, Any],
    ) -> ArtifactMetadata:
        """Record safe sidecar metadata for a file written by the trusted sink.

        A sidecar may finish the shared file immediately before returning its
        result.  Registration therefore validates the supplied id/size/hash,
        but Core remains the source of filename, token, subject binding, and
        expiry.  Download performs the final on-disk hash check as well.
        """

        artifact_id = artifact.id if isinstance(artifact, ArtifactReservation) else artifact
        with self._lock:
            self._cleanup_locked()
            row = self._row(artifact_id)
            if row is None:
                raise ArtifactError("ARTIFACT_NOT_FOUND", "artifact was not found", status=404)
            metadata = self._row_metadata(row)
            if not isinstance(raw, Mapping) or any(key in raw for key in _FORBIDDEN_SIDECAR_KEYS):
                self._mark_failed(artifact_id)
                raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
            if raw.get("id") != artifact_id:
                self._mark_failed(artifact_id)
                raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
            try:
                row_count = self._validate_sidecar_metadata(
                    raw,
                    artifact_id,
                    metadata.filename,
                    metadata.expires_at,
                    raw.get("sizeBytes"),
                    raw.get("sha256"),
                )
            except ArtifactError:
                self._mark_failed(artifact_id)
                raise
            size, sha256 = raw.get("sizeBytes"), raw.get("sha256")
            if type(size) is not int or not 0 <= size <= MAX_ARTIFACT_BYTES or not isinstance(sha256, str) or not _HEX64.fullmatch(sha256):
                self._mark_failed(artifact_id)
                raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
            final_path = self.root / metadata.filename
            try:
                final_path.resolve().relative_to(self.root)
                stat = final_path.stat()
            except (OSError, ValueError):
                self._mark_failed(artifact_id)
                raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
            if not final_path.is_file() or stat.st_size != size:
                self._mark_failed(artifact_id)
                raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
            digest = hashlib.sha256()
            try:
                with final_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                self._mark_failed(artifact_id)
                raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502) from exc
            if not hmac.compare_digest(digest.hexdigest(), sha256):
                self._mark_failed(artifact_id)
                raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
            # A trusted in-process sidecar sink may finalize the shared file
            # before returning its response.  Accept that already-ready row
            # only when the sidecar descriptor exactly matches Core's stored
            # metadata; it still cannot replace the Core-generated name/token.
            if metadata.status == "ready":
                if metadata.size != size or metadata.sha256 != sha256 or metadata.row_count != row_count:
                    raise ArtifactError("ARTIFACT_INVALID_RESULT", "sidecar artifact metadata is invalid", status=502)
                return metadata
            self._assert_pending(metadata)
            with self._connect() as connection:
                connection.execute(
                    "UPDATE artifacts SET status='ready',size=?,row_count=?,sha256=? WHERE id=? AND status='pending'",
                    (size, row_count, sha256, artifact_id),
                )
            updated = self._row(artifact_id)
            if updated is None:
                raise ArtifactError("ARTIFACT_STORE_FAILED", "artifact store is unavailable", status=503)
            return self._row_metadata(updated)

    def fail(self, artifact: ArtifactReservation | str) -> None:
        """Mark a reservation failed after a sidecar error."""

        artifact_id = artifact.id if isinstance(artifact, ArtifactReservation) else artifact
        with self._lock:
            self._mark_failed(artifact_id)

    def _mark_failed(self, artifact_id: str) -> None:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT filename FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
                connection.execute("UPDATE artifacts SET status='failed' WHERE id=? AND status='pending'", (artifact_id,))
            if row is not None:
                self._unlink_filename(str(row["filename"]))
        except sqlite3.Error:
            # Preserve the original error and keep diagnostics out of the API.
            pass

    def _resolve_token(self, artifact_id: str, token: str) -> tuple[ArtifactMetadata, sqlite3.Row]:
        """Find a token-bound row; all malformed/wrong tokens are 404."""

        if not isinstance(token, str) or not token or len(token) > 256:
            raise ArtifactError("ARTIFACT_NOT_FOUND", "artifact was not found", status=404)
        row = self._row(artifact_id)
        if row is None:
            raise ArtifactError("ARTIFACT_NOT_FOUND", "artifact was not found", status=404)
        try:
            supplied = self._token_digest(token, bytes(row["token_salt"]))
            stored = bytes(row["token_hash"])
        except (TypeError, ValueError):
            raise ArtifactError("ARTIFACT_NOT_FOUND", "artifact was not found", status=404)
        if not hmac.compare_digest(supplied, stored):
            # Do not reveal whether this id exists or is already expired.
            raise ArtifactError("ARTIFACT_NOT_FOUND", "artifact was not found", status=404)
        return self._row_metadata(row), row

    def resolve_download(
        self,
        artifact_id: str,
        token: str,
        *,
        current_datasource_id: str | None = None,
        current_policy_versions: tuple[str, ...] | list[str] | None = None,
        authorization: AuthContext | None = None,
    ) -> ArtifactDownload:
        """Validate token, expiry, identity binding, source, policy, and bytes."""

        with self._lock:
            self._cleanup_locked()
            metadata, row = self._resolve_token(artifact_id, token)
            now = self.clock().astimezone(UTC)
            if _parse_timestamp(metadata.expires_at) <= now or metadata.status == "expired":
                raise ArtifactError("ARTIFACT_EXPIRED", "artifact has expired", status=410)
            if metadata.status != "ready":
                raise ArtifactError("ARTIFACT_NOT_READY", "artifact is not ready", status=404)
            if current_datasource_id != metadata.datasource_id:
                self._invalidate(metadata)
                raise ArtifactError("ARTIFACT_CONTEXT_CHANGED", "artifact is no longer valid", status=410)
            if current_policy_versions is None or tuple(current_policy_versions) != metadata.policy_versions:
                self._invalidate(metadata)
                raise ArtifactError("ARTIFACT_CONTEXT_CHANGED", "artifact is no longer valid", status=410)
            if self.access_control is not None:
                if not self._binding_is_valid(metadata):
                    self._invalidate(metadata)
                    raise ArtifactError("ARTIFACT_CONTEXT_CHANGED", "artifact is no longer valid", status=410)
                if authorization is not None and (
                    authorization.subject.id != metadata.subject_id
                    or authorization.credential_id != metadata.credential_id
                ):
                    raise ArtifactError("ARTIFACT_CONTEXT_CHANGED", "artifact is no longer valid", status=410)
            if not _ARTIFACT_FILENAME.fullmatch(metadata.filename):
                raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact is unavailable", status=404)
            path = self.root / metadata.filename
            try:
                path.resolve().relative_to(self.root)
                stat = path.stat()
            except (OSError, ValueError):
                raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact is unavailable", status=404)
            if not path.is_file() or stat.st_size > MAX_ARTIFACT_BYTES or metadata.size != stat.st_size:
                raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact is unavailable", status=404)
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact is unavailable", status=404) from exc
            if metadata.sha256 is None or not hmac.compare_digest(digest.hexdigest(), metadata.sha256):
                self._mark_failed(metadata.id)
                raise ArtifactError("ARTIFACT_UNAVAILABLE", "artifact is unavailable", status=404)
            return ArtifactDownload(metadata, path)

    def _invalidate(self, metadata: ArtifactMetadata) -> None:
        """Permanently revoke bytes after a server-side binding changes."""

        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE artifacts SET status='expired' WHERE id=? AND status IN ('pending','ready')",
                    (metadata.id,),
                )
            self._unlink_filename(metadata.filename)
        except sqlite3.Error:
            # Authorization already fails closed even if cleanup must retry.
            pass

    def _binding_is_valid(self, metadata: ArtifactMetadata) -> bool:
        """Check the original subject and credential without receiving a token."""

        checker = getattr(self.access_control, "is_subject_credential_active", None)
        if callable(checker):
            try:
                return bool(checker(metadata.subject_id, metadata.credential_id))
            except Exception:
                return False
        # A store that cannot attest credential state must fail closed; a
        # subject-only lookup would allow a revoked credential to download.
        return False

    def reservation_public(
        self,
        reservation: ArtifactReservation,
        metadata: ArtifactMetadata,
        *,
        download_path: str,
    ) -> dict[str, Any]:
        """Combine Core token and persisted safe metadata for a query result."""

        return reservation.public_metadata(
            size=metadata.size,
            row_count=metadata.row_count,
            sha256=metadata.sha256,
            download_path=download_path,
        )

    def check_token(self, artifact_id: str, token: str) -> ArtifactMetadata:
        """Resolve only token/status, useful for embedders and diagnostics."""

        with self._lock:
            self._cleanup_locked()
            metadata, _ = self._resolve_token(artifact_id, token)
            if metadata.status == "expired" or _parse_timestamp(metadata.expires_at) <= self.clock().astimezone(UTC):
                raise ArtifactError("ARTIFACT_EXPIRED", "artifact has expired", status=410)
            return metadata


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ARTIFACT_TTL_SECONDS",
    "CLEANUP_BATCH_SIZE",
    "DEFAULT_ARTIFACT_CONTENT_TYPE",
    "MAX_ARTIFACT_BYTES",
    "MAX_ARTIFACT_ROWS",
    "MAX_ARTIFACT_TTL_SECONDS",
    "MIN_ARTIFACT_TTL_SECONDS",
    "ArtifactDownload",
    "ArtifactError",
    "ArtifactMetadata",
    "ArtifactReservation",
    "ArtifactStore",
]
