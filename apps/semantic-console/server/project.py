"""Wren project drafts, validation, publishing, and version snapshots.

The project directory is treated as a source tree, not as a database.  Edits
arrive in an in-memory draft overlay, validation runs against a temporary copy,
and publication swaps the validated tree into place.  Version snapshots live in
an application state directory outside the project so they cannot accidentally
be committed to Git.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any, Protocol

import yaml

try:
    from .models import DatasourceRecord, VersionRecord, utc_now
except ImportError:  # Direct module loading in a lightweight smoke test.
    from models import DatasourceRecord, VersionRecord, utc_now  # type: ignore[no-redef]


class ProjectError(RuntimeError):
    """Safe project operation failure."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.details = details


class ProjectValidator(Protocol):
    """Injectable Wren seam used by tests and alternative runtimes."""

    def health(self) -> dict[str, Any]: ...

    def validate(self, project_dir: Path) -> dict[str, Any]: ...

    def build(self, project_dir: Path) -> dict[str, Any]: ...


_IGNORED_DIRS = frozenset({".git", ".wren", "target", "__pycache__", ".semantic-console", "node_modules", ".venv", "venv", "dist", "build", "state"})
_MAX_FILE_BYTES = 2 * 1024 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_SECRET_LINE = re.compile(
    r"(?im)^\s*(?:password|passwd|pwd|token|api[_-]?key|secret|private[_-]?key|dsn|connection[_-]?url)\s*:\s*\S+"
)
_DSN = re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb)://[^\s]+", re.IGNORECASE)
_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def _redact_text(value: object, project_dir: Path | None = None, maximum: int = 1_000) -> str:
    """Bound and redact Wren/DB text before it becomes an API detail."""

    text = str(value) if isinstance(value, str) else "operation failed"
    if project_dir is not None:
        for candidate in (str(project_dir), project_dir.as_posix(), str(project_dir).replace("\\", "/")):
            if candidate:
                text = text.replace(candidate, "[project]")
    text = _DSN.sub("[redacted-dsn]", text)
    text = re.sub(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", text)
    text = re.sub(r"\b[A-Za-z]:[\\/][^\s]+", "[redacted-path]", text)
    return text[:maximum]


def _safe_relative(path: str, *, allow_target: bool = False) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ProjectError("INVALID_PATH", "file path is required")
    raw = path.replace("\\", "/")
    if raw.startswith("/") or _WINDOWS_ABS.match(raw) or "\x00" in raw:
        raise ProjectError("INVALID_PATH", "absolute file paths are not allowed")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ProjectError("INVALID_PATH", "parent traversal is not allowed")
    if not parts:
        raise ProjectError("INVALID_PATH", "file path is required")
    if parts[0] in {".git", ".wren", "__pycache__", ".semantic-console"}:
        raise ProjectError("INVALID_PATH", "private project paths are not exposed")
    if parts[0] == "target" and not allow_target:
        raise ProjectError("INVALID_PATH", "generated target files are read-only")
    if any(part.startswith(".") and part not in {".gitignore"} for part in parts):
        raise ProjectError("INVALID_PATH", "hidden files are not editable")
    return "/".join(parts)


def _secret_file_path(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1].lower()
    return (
        name in {".env", ".env.local", ".env.production", "credentials.json", "service-account.json", "id_rsa"}
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def _assert_safe_content(relative: str, content: str) -> None:
    if _secret_file_path(relative):
        raise ProjectError("CREDENTIALS_NOT_ALLOWED", "credential files cannot be stored in a Wren project")
    if _DSN.search(content) or _SECRET_LINE.search(content):
        raise ProjectError("CREDENTIALS_NOT_ALLOWED", "credential values cannot be stored in project files")


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    include_target: bool = False,
    exclude: Path | None = None,
) -> int:
    """Copy a project source tree while skipping generated/private material."""

    count = 0
    if not source.exists():
        return 0
    destination_resolved = destination.resolve()
    excluded = exclude.resolve() if exclude is not None else None
    ignored = _IGNORED_DIRS - ({"target"} if include_target else set())
    if source.is_dir():
        for current, directories, names in os.walk(source):
            current_path = Path(current)
            kept: list[str] = []
            for name in directories:
                directory = current_path / name
                if name in ignored or directory.is_symlink():
                    continue
                try:
                    if directory.resolve() == destination_resolved:
                        continue
                    if excluded is not None:
                        directory.resolve().relative_to(excluded)
                        continue
                except OSError:
                    continue
                except ValueError:
                    pass
                kept.append(name)
            directories[:] = kept
            for name in names:
                candidate = current_path / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                if excluded is not None:
                    try:
                        candidate.resolve().relative_to(excluded)
                        continue
                    except ValueError:
                        pass
                relative = candidate.relative_to(source)
                if any(part in ignored for part in relative.parts) or _secret_file_path(relative.as_posix()):
                    continue
                if candidate.stat().st_size > _MAX_FILE_BYTES:
                    raise ProjectError("FILE_TOO_LARGE", "project file exceeds the 2 MiB limit")
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate, target)
                count += 1
    return count


def _file_digest(
    root: Path,
    drafts: Mapping[str, str | None] | None = None,
    *,
    exclude: Path | None = None,
) -> str:
    digest = hashlib.sha256()
    files: dict[str, Path] = {}
    excluded = exclude.resolve() if exclude is not None else None
    if root.exists():
        for current, directories, names in os.walk(root):
            current_path = Path(current)
            kept_directories: list[str] = []
            for name in directories:
                if name in _IGNORED_DIRS:
                    continue
                if excluded is not None:
                    try:
                        (current_path / name).resolve().relative_to(excluded)
                        continue
                    except ValueError:
                        pass
                kept_directories.append(name)
            directories[:] = kept_directories
            for name in names:
                candidate = current_path / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                if excluded is not None:
                    try:
                        candidate.resolve().relative_to(excluded)
                        continue
                    except ValueError:
                        pass
                relative = candidate.relative_to(root)
                if relative.parts and relative.parts[0] == "target":
                    continue
                files[relative.as_posix()] = candidate
    for relative, content in (drafts or {}).items():
        if content is None:
            files.pop(relative, None)
        else:
            files.pop(relative, None)
    names = set(files)
    names.update(relative for relative, content in (drafts or {}).items() if content is not None)
    for relative in sorted(names):
        encoded_name = relative.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        if drafts and relative in drafts and drafts[relative] is not None:
            data = drafts[relative].encode("utf-8")
        else:
            data = files[relative].read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return f"sha256:{digest.hexdigest()}"


def _managed_files(
    root: Path,
    *,
    include_target: bool = True,
    exclude: Path | None = None,
) -> dict[str, Path]:
    """List files the publisher may replace, leaving Git/hidden state alone."""

    result: dict[str, Path] = {}
    ignored = _IGNORED_DIRS - ({"target"} if include_target else set())
    if not root.is_dir():
        return result
    excluded = exclude.resolve() if exclude is not None else None
    for candidate in root.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if excluded is not None:
            try:
                candidate.resolve().relative_to(excluded)
                continue
            except ValueError:
                pass
        relative = candidate.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        if any(part.startswith(".") for part in relative.parts):
            # `.git`/`.wren` are already ignored above; preserve any other
            # hidden runtime files conservatively as well.
            continue
        result[relative.as_posix()] = candidate
    return result


class WrenProjectAdapter:
    """Lazy wrapper around Wren 0.13.2's public context APIs."""

    def __init__(self, module_loader: Any | None = None) -> None:
        self.module_loader = module_loader or importlib.import_module
        self._context: Any | None = None
        self._version: str | None = None

    def _load_context(self) -> Any:
        if self._context is None:
            self._context = self.module_loader("wren.context")
        return self._context

    def health(self) -> dict[str, Any]:
        # ``import wren.context`` can load optional native connectors and is
        # intentionally avoided by liveness probes.  Package discovery is
        # enough to report readiness; the actual public functions are checked
        # lazily by validate/build.
        try:
            import importlib.metadata

            try:
                version = importlib.metadata.version("wrenai")
            except importlib.metadata.PackageNotFoundError:
                version = None
            # Metadata lookup does not execute Wren's package initializers.
            # An editable checkout may have no metadata; it is treated as a
            # degraded runtime and validated explicitly on demand.
            available = isinstance(version, str) and bool(version)
        except Exception:
            version = None
            available = False
        return {"available": bool(available), "version": version if isinstance(version, str) else None, "expectedVersion": "0.13.2"}

    def validate(self, project_dir: Path) -> dict[str, Any]:
        health = self.health()
        if not health["available"]:
            structural = _structural_validate(project_dir)
            structural.update({"wrenAvailable": False, "wrenVersion": health.get("version")})
            return structural
        try:
            issues = self._load_context().validate_project(project_dir)
            normalized = _normalize_issues(issues, project_dir)
            # A build catches structural problems that validate_project may not
            # see (and is the same public path used by Wren's CLI).
            self._load_context().build_json(project_dir)
            return {
                "valid": not any(item["level"] == "error" for item in normalized),
                "errors": [item for item in normalized if item["level"] == "error"],
                "warnings": [item for item in normalized if item["level"] == "warning"],
                "errorCount": sum(item["level"] == "error" for item in normalized),
                "warningCount": sum(item["level"] == "warning" for item in normalized),
                "wrenAvailable": True,
                "wrenVersion": health.get("version"),
            }
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise ProjectError("WREN_VALIDATION_FAILED", "Wren project validation failed") from exc
            return {
                "valid": False,
                "errors": [{"level": "error", "path": "project", "message": _redact_text(exc, project_dir)}],
                "warnings": [],
                "errorCount": 1,
                "warningCount": 0,
                "wrenAvailable": True,
                "wrenVersion": health.get("version"),
            }

    def build(self, project_dir: Path) -> dict[str, Any]:
        try:
            context = self._load_context()
            manifest = context.build_json(project_dir)
            if not isinstance(manifest, dict):
                raise ValueError("Wren build returned an invalid manifest")
            return manifest
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise ProjectError("WREN_BUILD_FAILED", "Wren project build failed") from exc
            raise ProjectError("WREN_BUILD_FAILED", "Wren project build failed") from exc

    def write_target(self, project_dir: Path, manifest: Mapping[str, Any]) -> None:
        try:
            save_target = getattr(self._load_context(), "save_target", None)
            if callable(save_target):
                save_target(dict(manifest), project_dir)
        except Exception as exc:
            raise ProjectError("WREN_BUILD_FAILED", "Wren target build failed") from exc


def _normalize_issues(issues: Any, project_dir: Path) -> list[dict[str, str]]:
    if issues is None:
        return []
    if isinstance(issues, Mapping):
        raw: list[Any] = []
        raw.extend(issues.get("errors", []) if isinstance(issues.get("errors", []), list) else [])
        raw.extend(issues.get("warnings", []) if isinstance(issues.get("warnings", []), list) else [])
    else:
        try:
            raw = list(issues)
        except TypeError:
            raw = [issues]
    result: list[dict[str, str]] = []
    for issue in raw[:100]:
        if isinstance(issue, Mapping):
            level = issue.get("level", "error")
            path = issue.get("path", "project")
            message = issue.get("message", "validation issue")
        else:
            level = getattr(issue, "level", "error")
            path = getattr(issue, "path", "project")
            message = getattr(issue, "message", str(issue))
        safe_level = "warning" if str(level).lower() == "warning" else "error"
        result.append({"level": safe_level, "path": _redact_text(path, project_dir, 300), "message": _redact_text(message, project_dir, 1_000)})
    return result


def _structural_validate(project_dir: Path) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    config_file = project_dir / "wren_project.yml"
    config: dict[str, Any] = {}
    if not config_file.is_file():
        errors.append({"level": "error", "path": "wren_project.yml", "message": "project file is missing"})
    else:
        try:
            parsed = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            config = parsed if isinstance(parsed, dict) else {}
        except (OSError, yaml.YAMLError):
            errors.append({"level": "error", "path": "wren_project.yml", "message": "project file is not valid YAML"})
    for field in ("name", "data_source"):
        if not config.get(field):
            errors.append({"level": "error", "path": "wren_project.yml", "message": f"missing required field '{field}'"})
    models_dir = project_dir / "models"
    if models_dir.is_dir():
        for metadata in models_dir.rglob("metadata.yml"):
            try:
                model = yaml.safe_load(metadata.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                errors.append({"level": "error", "path": metadata.relative_to(project_dir).as_posix(), "message": "model metadata is not valid YAML"})
                continue
            relative = metadata.relative_to(project_dir).as_posix()
            if not isinstance(model, dict) or not model.get("name"):
                errors.append({"level": "error", "path": relative, "message": "model name is required"})
            if isinstance(model, dict) and not model.get("columns"):
                errors.append({"level": "error", "path": relative, "message": "model columns are required"})
    return {"valid": not errors, "errors": errors, "warnings": [], "errorCount": len(errors), "warningCount": 0}


class ProjectStore:
    """Own the current project, drafts, and immutable version snapshots."""

    def __init__(
        self,
        project_dir: str | Path | None = None,
        *,
        state_dir: str | Path | None = None,
        validator: ProjectValidator | None = None,
    ) -> None:
        configured = project_dir or os.environ.get("WREN_PROJECT_HOME") or os.getcwd()
        self.project_dir = Path(configured).expanduser().resolve()
        if self.project_dir.exists() and self.project_dir.is_symlink():
            raise ProjectError("INVALID_PROJECT", "project directory may not be a symlink")
        if state_dir is None:
            digest = hashlib.sha256(str(self.project_dir).encode("utf-8")).hexdigest()[:16]
            state_dir = Path.home() / ".wren" / "semantic-console" / digest
        self.state_dir = Path(state_dir).expanduser().resolve()
        try:
            self.state_dir.relative_to(self.project_dir)
        except ValueError:
            pass
        else:
            raise ProjectError("INVALID_STATE_DIR", "state directory must be outside the Wren project")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.versions_dir = self.state_dir / "versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.validator = validator or WrenProjectAdapter()
        self.drafts: dict[str, str | None] = {}
        self._lock = threading.RLock()
        self._datasources: dict[str, DatasourceRecord] = {}
        self.active_datasource_id: str | None = None
        self._datasource_secret_file = self.state_dir / "datasources.secrets.json"
        self._load_datasources()

    # ---- project read/draft operations ---------------------------------

    def overview(self) -> dict[str, Any]:
        with self._lock:
            config = self._config(self.project_dir)
            effective_files = self.files()
            model_count = len(
                {
                    parts[1]
                    for item in effective_files
                    for parts in [item["path"].split("/")]
                    if len(parts) == 3 and parts[0] == "models" and parts[2] == "metadata.yml"
                }
            )
            revision = _file_digest(self.project_dir, self.drafts, exclude=self.state_dir)
            active = self._datasources.get(self.active_datasource_id) if self.active_datasource_id else None
            return {
                "name": config.get("name"),
                "schemaVersion": config.get("schema_version"),
                "catalog": config.get("catalog", "wren"),
                "schema": config.get("schema", "public"),
                "dataSource": config.get("data_source"),
                "modelCount": model_count,
                "draftCount": len(self.drafts),
                "revision": revision,
                "projectExists": self.project_dir.is_dir(),
                "activeDatasource": active.public() if active else None,
                "wren": self.validator.health(),
            }

    def files(self) -> list[dict[str, Any]]:
        with self._lock:
            entries: dict[str, dict[str, Any]] = {}
            if self.project_dir.is_dir():
                for candidate in self.project_dir.rglob("*"):
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    relative = candidate.relative_to(self.project_dir).as_posix()
                    if any(part in _IGNORED_DIRS for part in Path(relative).parts) or relative.startswith("target/"):
                        continue
                    if _secret_file_path(relative):
                        continue
                    try:
                        data = candidate.read_bytes()
                    except OSError:
                        continue
                    entries[relative] = {
                        "path": relative,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "draft": False,
                    }
            for relative, content in self.drafts.items():
                if content is None:
                    entries.pop(relative, None)
                    continue
                data = content.encode("utf-8")
                entries[relative] = {
                    "path": relative,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "draft": True,
                }
            return [entries[key] for key in sorted(entries)]

    def read_file(self, relative: str) -> dict[str, Any]:
        relative = _safe_relative(relative, allow_target=False)
        with self._lock:
            if relative in self.drafts:
                content = self.drafts[relative]
                if content is None:
                    raise ProjectError("FILE_NOT_FOUND", "project file is deleted in the current draft")
                return {"path": relative, "content": content, "draft": True, "revision": _file_digest(self.project_dir, self.drafts, exclude=self.state_dir)}
            candidate = self.project_dir / relative
            if not candidate.is_file() or candidate.is_symlink():
                raise ProjectError("FILE_NOT_FOUND", "project file was not found")
            try:
                data = candidate.read_bytes()
            except OSError as exc:
                raise ProjectError("FILE_READ_FAILED", "project file could not be read") from exc
            if len(data) > _MAX_FILE_BYTES:
                raise ProjectError("FILE_TOO_LARGE", "project file exceeds the 2 MiB limit")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProjectError("FILE_NOT_TEXT", "project file is not UTF-8 text") from exc
            return {"path": relative, "content": content, "draft": False, "revision": _file_digest(self.project_dir, self.drafts, exclude=self.state_dir)}

    def put_file(self, relative: str, content: str | None, *, delete: bool = False, expected_revision: str | None = None) -> dict[str, Any]:
        relative = _safe_relative(relative)
        with self._lock:
            current_revision = _file_digest(self.project_dir, self.drafts, exclude=self.state_dir)
            if expected_revision and expected_revision != current_revision:
                raise ProjectError("REVISION_CONFLICT", "project changed since this draft was read", {"revision": current_revision})
            if delete:
                self.drafts[relative] = None
            else:
                if not isinstance(content, str):
                    raise ProjectError("INVALID_CONTENT", "file content must be a string")
                if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
                    raise ProjectError("FILE_TOO_LARGE", "project file exceeds the 2 MiB limit")
                _assert_safe_content(relative, content)
                self.drafts[relative] = content
            return {"path": relative, "draft": True, "revision": _file_digest(self.project_dir, self.drafts, exclude=self.state_dir)}

    def import_project(self, source: str | Path | None = None, files: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        with self._lock:
            if source is not None:
                source_path = Path(source).expanduser().resolve()
                if not source_path.is_dir() or source_path.is_symlink():
                    raise ProjectError("INVALID_PROJECT", "import source directory was not found")
                if not (source_path / "wren_project.yml").is_file():
                    raise ProjectError("INVALID_PROJECT", "import source is not a Wren project")
                temp = Path(tempfile.mkdtemp(prefix="semantic-console-import-", dir=self.state_dir))
                try:
                    _copy_tree(source_path, temp)
                    self.drafts.clear()
                    for candidate in temp.rglob("*"):
                        if candidate.is_file():
                            relative = candidate.relative_to(temp).as_posix()
                            if _secret_file_path(relative):
                                continue
                            try:
                                content = candidate.read_text(encoding="utf-8")
                            except UnicodeDecodeError as exc:
                                raise ProjectError("FILE_NOT_TEXT", "imported project contains a non-text file") from exc
                            _assert_safe_content(relative, content)
                            self.drafts[relative] = content
                finally:
                    shutil.rmtree(temp, ignore_errors=True)
            if files is not None:
                self.drafts.clear()
                for item in files:
                    if not isinstance(item, Mapping):
                        raise ProjectError("INVALID_IMPORT", "import files must be objects")
                    relative = _safe_relative(item.get("path"))
                    content = item.get("content")
                    if not isinstance(content, str):
                        raise ProjectError("INVALID_CONTENT", "import file content must be a string")
                    _assert_safe_content(relative, content)
                    self.drafts[relative] = content
            return {"files": self.files(), "revision": _file_digest(self.project_dir, self.drafts, exclude=self.state_dir), "draft": True}

    def validate(self) -> dict[str, Any]:
        with self._lock:
            stage = self._stage()
            try:
                result = self.validator.validate(stage)
                if not isinstance(result, dict):
                    raise ProjectError("VALIDATION_FAILED", "project validator returned an invalid result")
                result = dict(result)
                result["revision"] = _file_digest(self.project_dir, self.drafts, exclude=self.state_dir)
                result["draft"] = bool(self.drafts)
                return result
            finally:
                shutil.rmtree(stage, ignore_errors=True)

    # ---- publish/version operations ------------------------------------

    def publish(self, *, label: str | None = None) -> dict[str, Any]:
        with self._lock:
            stage = self._stage()
            try:
                result = self.validator.validate(stage)
                if not isinstance(result, dict) or not result.get("valid"):
                    details = result if isinstance(result, dict) else None
                    raise ProjectError("VALIDATION_FAILED", "project validation failed", _safe_details(details))
                manifest = self.validator.build(stage)
                if isinstance(self.validator, WrenProjectAdapter):
                    self.validator.write_target(stage, manifest)
                return self._publish_stage(stage, label=label, revision=_file_digest(self.project_dir, self.drafts, exclude=self.state_dir), clear_drafts=True)
            except ProjectError:
                raise
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)

    def versions(self) -> list[dict[str, Any]]:
        with self._lock:
            records = []
            for metadata in self.versions_dir.glob("*/version.json"):
                try:
                    data = json.loads(metadata.read_text(encoding="utf-8"))
                    snapshot = metadata.parent / "project"
                    if not snapshot.is_dir():
                        continue
                    record = VersionRecord(
                        id=str(data["id"]),
                        revision=str(data["revision"]),
                        created_at=str(data["createdAt"]),
                        file_count=int(data.get("fileCount", 0)),
                        label=data.get("label") if isinstance(data.get("label"), str) else None,
                    )
                    records.append(record)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            records.sort(key=lambda item: item.created_at, reverse=True)
            return [record.public() for record in records]

    def rollback(self, version_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,100}", version_id or ""):
            raise ProjectError("INVALID_VERSION", "version id is invalid")
        with self._lock:
            snapshot = self.versions_dir / version_id / "project"
            if not snapshot.is_dir():
                raise ProjectError("VERSION_NOT_FOUND", "version was not found")
            stage = Path(tempfile.mkdtemp(prefix="semantic-console-rollback-", dir=self.state_dir))
            try:
                _copy_tree(snapshot, stage)
                result = self.validator.validate(stage)
                if not isinstance(result, dict) or not result.get("valid"):
                    raise ProjectError("VALIDATION_FAILED", "stored version no longer validates", _safe_details(result if isinstance(result, dict) else None))
                manifest = self.validator.build(stage)
                if isinstance(self.validator, WrenProjectAdapter):
                    self.validator.write_target(stage, manifest)
                return self._publish_stage(stage, label=f"rollback:{version_id}", revision=_file_digest(snapshot), clear_drafts=True)
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)

    def _stage(self) -> Path:
        stage = Path(tempfile.mkdtemp(prefix="semantic-console-stage-", dir=self.state_dir))
        _copy_tree(self.project_dir, stage, exclude=self.state_dir)
        for relative, content in self.drafts.items():
            candidate = stage / relative
            if content is None:
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
            else:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                # Preserve draft bytes exactly; ``Path.write_text`` applies
                # platform newline conversion on Windows and would make the
                # published revision differ from the editor's revision.
                with candidate.open("w", encoding="utf-8", newline="") as output:
                    output.write(content)
        return stage

    def _publish_stage(self, stage: Path, *, label: str | None, revision: str, clear_drafts: bool) -> dict[str, Any]:
        version_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + token_hex(4)
        version_dir = self.versions_dir / version_id
        snapshot = version_dir / "project"
        version_dir.mkdir(parents=True, exist_ok=False)
        # A version describes the *new validated tree*, not the pre-publish
        # tree.  Keep a separate transaction backup only until the replacement
        # succeeds.  Both stay outside the project and therefore outside Git.
        snapshot.mkdir(parents=True, exist_ok=True)
        _copy_tree(stage, snapshot, include_target=True)
        backup = self.state_dir / ("backup-" + version_id)
        backup.mkdir(parents=True, exist_ok=True)
        _copy_tree(self.project_dir, backup, include_target=True)
        swapped = False
        try:
            self.project_dir.parent.mkdir(parents=True, exist_ok=True)
            self._replace_managed_files(stage, backup)
            swapped = True
            shutil.rmtree(backup, ignore_errors=True)
        except Exception as exc:
            self._restore_managed_files(backup)
            raise ProjectError("PUBLISH_FAILED", "project could not be published") from exc
        finally:
            if backup.exists() and swapped:
                shutil.rmtree(backup, ignore_errors=True)
        if clear_drafts:
            self.drafts.clear()
        file_count = len([item for item in self.files() if item.get("draft") is False])
        record = VersionRecord(version_id, revision, utc_now(), file_count, label)
        metadata = record.public()
        version_dir.joinpath("version.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"version": metadata, "project": self.overview()}

    def _replace_managed_files(self, stage: Path, backup: Path) -> None:
        """Transactionally replace source/target files while preserving Git.

        Renaming the whole project directory would take ``.git`` and hidden
        runtime state with it.  We instead atomically replace each managed file
        and retain a complete backup for rollback if a later file operation
        fails.  The service lock serializes readers/writers during this short
        transaction, so API callers never observe a partial tree.
        """

        self.project_dir.mkdir(parents=True, exist_ok=True)
        current = _managed_files(self.project_dir, exclude=self.state_dir)
        incoming = _managed_files(stage)
        for relative in sorted(set(current) - set(incoming)):
            try:
                current[relative].unlink()
            except FileNotFoundError:
                pass
        for relative in sorted(incoming):
            destination = self.project_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".semantic-console.tmp")
            shutil.copyfile(incoming[relative], temporary)
            os.replace(temporary, destination)

    def _restore_managed_files(self, backup: Path) -> None:
        """Best-effort recovery after a failed managed-file swap."""

        try:
            current = _managed_files(self.project_dir, exclude=self.state_dir)
            original = _managed_files(backup)
            for relative in sorted(set(current) - set(original)):
                try:
                    current[relative].unlink()
                except OSError:
                    pass
            self.project_dir.mkdir(parents=True, exist_ok=True)
            for relative in sorted(original):
                destination = self.project_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".semantic-console.restore.tmp")
                shutil.copyfile(original[relative], temporary)
                os.replace(temporary, destination)
        except OSError:
            # The original publish error remains the useful API diagnostic.
            pass

    @staticmethod
    def _config(directory: Path) -> dict[str, Any]:
        path = directory / "wren_project.yml"
        if not path.is_file():
            return {}
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, yaml.YAMLError):
            return {}

    # ---- datasource record ownership -----------------------------------

    def datasource_records(self) -> dict[str, DatasourceRecord]:
        return self._datasources

    def save_datasources(self) -> None:
        """Persist profiles outside the project with restrictive permissions.

        This file is deliberately named and scoped separately from project
        files.  It is never returned by an API route, copied into a Wren
        snapshot, or included in Git.  A future production build should swap
        this small local vault for the host OS keychain.
        """

        with self._lock:
            records = {
                key: {
                    "id": record.id,
                    "name": record.name,
                    "type": record.type,
                    "connection": record.connection,
                    "createdAt": record.created_at,
                    "updatedAt": record.updated_at,
                    "lastTest": record.last_test,
                }
                for key, record in self._datasources.items()
            }
            payload = {"activeDatasourceId": self.active_datasource_id, "datasources": records}
            temporary = self._datasource_secret_file.with_name(self._datasource_secret_file.name + ".tmp")
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                os.replace(temporary, self._datasource_secret_file)
                try:
                    os.chmod(self._datasource_secret_file, 0o600)
                except OSError:
                    pass
            except OSError as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ProjectError("DATASOURCE_STATE_FAILED", "datasource state could not be saved") from exc

    def _load_datasources(self) -> None:
        try:
            raw = json.loads(self._datasource_secret_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, Mapping):
            return
        self.active_datasource_id = raw.get("activeDatasourceId") if isinstance(raw.get("activeDatasourceId"), str) else None
        records = raw.get("datasources") if isinstance(raw.get("datasources"), Mapping) else raw
        for key, item in records.items():
            if not isinstance(item, Mapping):
                continue
            identifier = item.get("id", key)
            name = item.get("name")
            kind = item.get("type")
            connection = item.get("connection")
            if not all(isinstance(value, str) and value for value in (identifier, name, kind)) or not isinstance(connection, Mapping):
                continue
            self._datasources[str(identifier)] = DatasourceRecord(
                str(identifier),
                str(name),
                str(kind),
                dict(connection),
                str(item.get("createdAt") or utc_now()),
                str(item.get("updatedAt") or utc_now()),
                dict(item.get("lastTest")) if isinstance(item.get("lastTest"), Mapping) else None,
            )
        if self.active_datasource_id not in self._datasources:
            self.active_datasource_id = None


def _safe_details(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    result: dict[str, Any] = {}
    for key in ("errors", "warnings", "errorCount", "warningCount", "wrenAvailable", "wrenVersion"):
        item = value.get(key)
        if key in {"errors", "warnings"} and isinstance(item, list):
            result[key] = [
                {"level": str(issue.get("level", "error")), "path": _redact_text(issue.get("path", "project"), maximum=300), "message": _redact_text(issue.get("message", "validation issue"), maximum=1_000)}
                for issue in item[:100]
                if isinstance(issue, Mapping)
            ]
        elif isinstance(item, (str, int, bool)) or item is None:
            result[key] = item
    return result or None


__all__ = [
    "ProjectError",
    "ProjectStore",
    "ProjectValidator",
    "WrenProjectAdapter",
]
