"""Knowledge governance services for Wren rules and SQL examples.

The Wren knowledge directories are deliberately treated as *effective*
inputs.  A disabled rule is therefore moved out of ``knowledge/rules`` rather
than being annotated in place (Wren 0.13.2 does not define a portable
per-rule ``enabled`` flag).  Legacy rule files containing several top-level
bullets are projected into individual records while their original bytes are
archived in the console namespace before the first edit.

SQL examples follow a two-step review flow.  Candidates live in the
sidecar's private state directory until a reviewer approves them; only then
is a ``knowledge/sql/<slug>.md`` draft created.  State writes use a same
directory temporary file and ``os.replace`` so a process interruption cannot
leave a partially-written JSON document.
"""

from __future__ import annotations

import hashlib
import contextlib
import io
import json
import math
import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

try:
    from .models import is_sensitive_key, utc_now
    from .project import ProjectError, ProjectStore
except ImportError:  # pragma: no cover - direct module loading
    from models import is_sensitive_key, utc_now  # type: ignore[no-redef]
    from project import ProjectError, ProjectStore  # type: ignore[no-redef]


RULES_ROOT = "knowledge/rules"
SQL_ROOT = "knowledge/sql"
RULE_INDEX_PATH = "semantic-console/rules-index.json"
RULE_DISABLED_ROOT = "semantic-console/rules-disabled"
RULE_ARCHIVE_ROOT = "semantic-console/rules-archive"
SQL_CANDIDATES_FILENAME = "sql-candidates.json"

_DENIED_SQL_NODES = {
    "Alter", "Attach", "Call", "Command", "Commit", "Copy", "Create", "Delete",
    "Detach", "Drop", "Grant", "Insert", "Into", "Lock", "Merge", "Prepare",
    "Refresh", "Rollback", "Set", "Transaction", "Truncate", "Update", "Use",
    "Vacuum", "Values",
}
_QUERY_SQL_ROOTS = {"Select", "Union", "Intersect", "Except"}
_DANGEROUS_SQL_FUNCTIONS = {
    "current_setting", "set_config", "pg_sleep", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_terminate_backend", "dblink", "dblink_exec", "lo_import",
    "lo_export", "read_csv", "read_parquet", "read_json", "postgres_scan",
    "postgres_query", "mysql_scan", "mysql_query", "load_file",
}


def validate_review_sql(value: Any) -> dict[str, Any]:
    """Fail closed unless *value* is one parseable read-only query."""

    sql = _text(value, "sql", maximum=_MAX_TEXT)
    try:
        from sqlglot import exp, parse  # type: ignore[import-not-found]
        from sqlglot.errors import ErrorLevel  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProjectError("SQL_VALIDATION_UNAVAILABLE", "SQL validation is unavailable") from exc
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            statements = [item for item in parse(sql, read="postgres", error_level=ErrorLevel.RAISE) if item is not None]
    except Exception as exc:
        raise ProjectError("INVALID_SQL_CANDIDATE", "SQL candidate could not be parsed") from exc
    if len(statements) != 1 or type(statements[0]).__name__ not in _QUERY_SQL_ROOTS:
        raise ProjectError("INVALID_SQL_CANDIDATE", "SQL candidate must be one read-only query")
    root = statements[0]
    for node in root.walk():
        if type(node).__name__ in _DENIED_SQL_NODES:
            raise ProjectError("INVALID_SQL_CANDIDATE", "SQL candidate must be one read-only query")
        if isinstance(node, exp.Func):
            name = str(node.sql_name() or "").lower()
            if name in _DANGEROUS_SQL_FUNCTIONS:
                raise ProjectError("INVALID_SQL_CANDIDATE", "SQL candidate uses a denied function")
    return {"valid": True, "status": "passed", "message": "SQL is one read-only query"}
KNOWLEDGE_SCHEMA_VERSION = 1

_RULE_ID = re.compile(r"^rule_[A-Za-z0-9_-]{8,100}$")
_CANDIDATE_ID = re.compile(r"^sql_[A-Za-z0-9_-]{8,100}$")
_SLUG = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,119}$")
_BULLET = re.compile(r"^(?P<indent>\s*)(?P<marker>[-*+])\s+\S")
_SECRET_TEXT = re.compile(
    r"(?i)(?:\b(?:postgres(?:ql)?|mysql|mariadb)://[^\s]+|"
    r"^\s*(?:password|passwd|pwd|token|api[_-]?key|secret|private[_-]?key|dsn|connection[_-]?url)\s*:\s*\S+)",
    re.MULTILINE,
)
_MAX_TEXT = 2 * 1024 * 1024
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_STATUSES = frozenset({"pending", "approved", "rejected"})


def _text(value: Any, name: str, *, maximum: int = _MAX_TEXT, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ProjectError("INVALID_KNOWLEDGE", f"{name} must be a string")
    if required and not value.strip():
        raise ProjectError("INVALID_KNOWLEDGE", f"{name} is required")
    if len(value.encode("utf-8")) > maximum:
        raise ProjectError("FILE_TOO_LARGE", f"{name} exceeds the permitted length")
    return value


def _safe_rule_id(value: Any) -> str:
    candidate = _text(value, "ruleId", maximum=120)
    if not _RULE_ID.fullmatch(candidate):
        raise ProjectError("INVALID_KNOWLEDGE", "ruleId is invalid")
    return candidate


def _safe_candidate_id(value: Any) -> str:
    candidate = _text(value, "candidateId", maximum=120)
    if not _CANDIDATE_ID.fullmatch(candidate):
        raise ProjectError("INVALID_KNOWLEDGE", "candidateId is invalid")
    return candidate


def _safe_slug(value: Any, *, name: str = "slug") -> str:
    candidate = _text(value, name, maximum=120)
    # Do not silently turn a path into a different name.  Rejecting it makes
    # path traversal attempts observable and keeps approval deterministic.
    if "/" in candidate or "\\" in candidate or candidate in {".", ".."} or not _SLUG.fullmatch(candidate):
        raise ProjectError("INVALID_PATH", f"{name} is invalid")
    return candidate


def _generated_slug(question: str) -> str:
    # Keep human-readable ASCII where possible and use a hash for non-ASCII
    # questions.  The hash suffix makes two translated questions distinct.
    words = re.findall(r"[A-Za-z0-9]+", question.lower())
    stem = "-".join(words)[:72].strip("-")
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
    if not stem:
        stem = "query"
    result = f"{stem}-{digest}"
    result = re.sub(r"[^A-Za-z0-9_-]+", "-", result).strip("-")
    if result and result[0].isdigit():
        result = "query-" + result
    return result[:120] or f"query-{digest}"


def _safe_json(value: Any, *, path: str = "metadata", depth: int = 0) -> Any:
    """Copy JSON values while rejecting non-JSON values and secret keys."""

    if depth > 12:
        raise ProjectError("INVALID_KNOWLEDGE", f"{path} is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            if len(value.encode("utf-8")) > _MAX_TEXT:
                raise ProjectError("FILE_TOO_LARGE", f"{path} exceeds the permitted length")
            _assert_safe_knowledge_text(value, path)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectError("INVALID_KNOWLEDGE", f"{path} must contain finite numbers")
        return value
    if isinstance(value, list):
        if len(value) > 1_000:
            raise ProjectError("INVALID_KNOWLEDGE", f"{path} contains too many items")
        return [_safe_json(item, path=f"{path}[{index}]", depth=depth + 1) for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        if len(value) > 500:
            raise ProjectError("INVALID_KNOWLEDGE", f"{path} contains too many fields")
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 255:
                raise ProjectError("INVALID_KNOWLEDGE", f"{path} contains an invalid key")
            if is_sensitive_key(key):
                raise ProjectError("CREDENTIALS_NOT_ALLOWED", "credential metadata is not allowed")
            copied[key] = _safe_json(item, path=f"{path}.{key}", depth=depth + 1)
        return copied
    raise ProjectError("INVALID_KNOWLEDGE", f"{path} must be JSON-safe")


def _assert_safe_knowledge_text(value: str, name: str) -> None:
    if _SECRET_TEXT.search(value):
        raise ProjectError("CREDENTIALS_NOT_ALLOWED", f"credential values cannot be stored in {name}")


def _rule_id_for(source_path: str, block_index: int) -> str:
    digest = hashlib.sha256(f"{source_path}\0{block_index}".encode("utf-8")).hexdigest()[:24]
    return f"rule_{digest}"


def _candidate_id_for(question: str, sql: str, dialect: str) -> str:
    normalized_sql = re.sub(r"\s+", " ", sql.strip()).casefold()
    normalized_question = re.sub(r"\s+", " ", question.strip()).casefold()
    value = json.dumps([normalized_question, normalized_sql, dialect.casefold()], ensure_ascii=False, separators=(",", ":"))
    return "sql_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _split_bullets(content: str) -> tuple[str, list[str], str]:
    """Return prefix, top-level bullet blocks, and suffix preserving bytes."""

    lines = content.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if _BULLET.match(line) and not _BULLET.match(line).group("indent")]
    if len(starts) <= 1:
        return content, [], ""
    prefix = "".join(lines[: starts[0]])
    blocks = ["".join(lines[start:end]) for start, end in zip(starts, starts[1:] + [len(lines)])]
    # A final blank separator is part of the preceding bullet block.  This is
    # intentional: reconstructing from the archive then preserves all source
    # bytes except the disabled block itself.
    suffix = ""
    return prefix, blocks, suffix


def _rule_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        value = re.sub(r"^\s*(?:[-*+]\s+|#+\s*)", "", line).strip()
        if value:
            return value[:160]
    return fallback[:160]


def _read_optional(project: ProjectStore, path: str) -> str | None:
    try:
        return str(project.read_file(path)["content"])
    except ProjectError as exc:
        if exc.code == "FILE_NOT_FOUND":
            return None
        raise


def _json_document(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n"
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ProjectError("FILE_TOO_LARGE", "knowledge metadata exceeds the permitted length")
    return encoded


class RuleStore:
    """Project-backed rules with safe enable/disable transitions."""

    def __init__(self, project: ProjectStore) -> None:
        self.project = project

    def _index(self) -> dict[str, Any]:
        raw = _read_optional(self.project, RULE_INDEX_PATH)
        if raw is None:
            return {"schemaVersion": KNOWLEDGE_SCHEMA_VERSION, "rules": []}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ProjectError("INVALID_KNOWLEDGE_STATE", "rules index is not valid JSON") from exc
        if not isinstance(value, Mapping):
            raise ProjectError("INVALID_KNOWLEDGE_STATE", "rules index must contain an object")
        result = dict(value)
        records = result.get("rules", [])
        if not isinstance(records, list):
            raise ProjectError("INVALID_KNOWLEDGE_STATE", "rules index rules must be a list")
        result["rules"] = [dict(item) for item in records if isinstance(item, Mapping)]
        result.setdefault("schemaVersion", KNOWLEDGE_SCHEMA_VERSION)
        return result

    def _active_paths(self) -> list[str]:
        return sorted(
            str(item["path"])
            for item in self.project.files()
            if isinstance(item.get("path"), str)
            and str(item["path"]).startswith(RULES_ROOT + "/")
            and str(item["path"]).lower().endswith((".md", ".markdown"))
        )

    def _collect(self) -> list[dict[str, Any]]:
        index = self._index()
        records = [record for record in index["rules"] if isinstance(record, Mapping)]
        by_id = {str(record.get("id")): dict(record) for record in records if isinstance(record.get("id"), str)}
        by_source_index = {
            (str(record.get("sourcePath")), int(record.get("blockIndex"))): dict(record)
            for record in records
            if isinstance(record.get("sourcePath"), str)
            and isinstance(record.get("blockIndex"), int)
            and record.get("blockIndex") >= 0
        }
        by_source = {
            str(record.get("sourcePath")): dict(record)
            for record in records
            if isinstance(record.get("sourcePath"), str)
            and record.get("sourceFormat") == "single"
        }
        # Once a legacy file has been edited it may contain zero or one
        # remaining bullet, so inspecting only the current file would make it
        # look like a new single-rule document and resurrect disabled rules.
        # The index/archive marker keeps the logical projection stable across
        # those transitions.
        legacy_sources = {
            str(record.get("sourcePath"))
            for record in records
            if isinstance(record.get("sourcePath"), str)
            and (record.get("sourceFormat") == "legacy-list" or isinstance(record.get("archivePath"), str))
        }
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source_path in self._active_paths():
            content = _read_optional(self.project, source_path) or ""
            if source_path in legacy_sources:
                source_records = [
                    dict(record)
                    for record in records
                    if record.get("sourcePath") == source_path and record.get("sourceFormat") == "legacy-list"
                ]
                archive_path = next(
                    (str(record.get("archivePath")) for record in source_records if isinstance(record.get("archivePath"), str)),
                    None,
                )
                archive_content = _read_optional(self.project, archive_path) if archive_path else None
                _archive_prefix, archive_blocks, _archive_suffix = _split_bullets(archive_content or content)
                if archive_blocks:
                    records_by_index = {
                        int(record.get("blockIndex")): record
                        for record in source_records
                        if isinstance(record.get("blockIndex"), int) and record.get("blockIndex") >= 0
                    }
                    for block_index, block in enumerate(archive_blocks):
                        record = dict(records_by_index.get(block_index) or {})
                        rule_id = str(record.get("id") or _rule_id_for(source_path, block_index))
                        disabled_content = _read_optional(self.project, record.get("disabledPath")) if isinstance(record.get("disabledPath"), str) else None
                        record.update({
                            "id": rule_id,
                            "sourcePath": source_path,
                            "sourceFormat": "legacy-list",
                            "blockIndex": block_index,
                            "content": disabled_content if disabled_content is not None else str(record.get("content") or block),
                            "enabled": bool(record.get("enabled", True)),
                        })
                        record.setdefault("title", _rule_title(str(record["content"]), rule_id))
                        result.append(record)
                        seen.add(rule_id)
                    continue
            _prefix, blocks, _suffix = _split_bullets(content)
            if blocks:
                for block_index, block in enumerate(blocks):
                    rule_id = _rule_id_for(source_path, block_index)
                    record = dict(by_id.get(rule_id) or by_source_index.get((source_path, block_index)) or {})
                    record.update({
                        "id": rule_id,
                        "sourcePath": source_path,
                        "sourceFormat": "legacy-list",
                        "blockIndex": block_index,
                        "content": block,
                        "enabled": True,
                    })
                    record.setdefault("title", _rule_title(block, rule_id))
                    result.append(record)
                    seen.add(rule_id)
            else:
                record = dict(by_id.get(str(by_source.get(source_path, {}).get("id"))) or by_source.get(source_path) or {})
                rule_id = str(record.get("id") or _rule_id_for(source_path, 0))
                record.update({
                    "id": rule_id,
                    "sourcePath": source_path,
                    "sourceFormat": "single",
                    "content": content,
                    "enabled": True,
                })
                record.setdefault("title", _rule_title(content, source_path.rsplit("/", 1)[-1]))
                result.append(record)
                seen.add(rule_id)

        # Disabled records are represented by their console-side copy and do
        # not appear in knowledge/rules.  Retain a missing marker for a
        # recoverable record whose copy was removed externally.
        for raw in records:
            record = dict(raw)
            rule_id = record.get("id")
            if not isinstance(rule_id, str) or rule_id in seen:
                continue
            if record.get("enabled", True):
                # A missing enabled source is reported, not silently dropped.
                record["missing"] = True
            else:
                disabled_path = record.get("disabledPath")
                disabled_content = _read_optional(self.project, disabled_path) if isinstance(disabled_path, str) else None
                if disabled_content is not None:
                    record["content"] = disabled_content
                record["missing"] = disabled_content is None
            record.setdefault("sourceFormat", "single")
            record.setdefault("title", _rule_title(str(record.get("content", "")), rule_id))
            result.append(record)
        result.sort(key=lambda item: (str(item.get("title", "")).casefold(), str(item.get("id", ""))))
        return result

    def list(self) -> dict[str, Any]:
        rules = [self._public(record) for record in self._collect()]
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "revision": self.project.overview()["revision"],
            "rules": rules,
            "enabledCount": sum(bool(item.get("enabled")) and not item.get("missing") for item in rules),
            "disabledCount": sum(not bool(item.get("enabled")) for item in rules),
        }

    @staticmethod
    def _public(record: Mapping[str, Any]) -> dict[str, Any]:
        result = {str(key): value for key, value in record.items() if isinstance(key, str)}
        result.setdefault("enabled", True)
        result.setdefault("content", "")
        result.setdefault("sourcePath", "")
        result.setdefault("title", _rule_title(str(result["content"]), str(result.get("id", "rule"))))
        return result

    def _find(self, rule_id: str) -> dict[str, Any]:
        for record in self._collect():
            if record.get("id") == rule_id:
                return record
        raise ProjectError("RULE_NOT_FOUND", "rule was not found")

    def get(self, rule_id: str) -> dict[str, Any]:
        return self._public(self._find(_safe_rule_id(rule_id)))

    def _write_index_and_files(
        self,
        index: dict[str, Any],
        files: Mapping[str, str | None],
        *,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        updates = dict(files)
        updates[RULE_INDEX_PATH] = _json_document(index)
        self.project.put_files(updates, expected_revision=expected_revision)
        return self.list()

    @staticmethod
    def _index_records(index: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [dict(item) for item in index.get("rules", []) if isinstance(item, Mapping)]

    def _replace_record(self, index: dict[str, Any], record: Mapping[str, Any]) -> None:
        rule_id = str(record["id"])
        records = self._index_records(index)
        replaced = False
        for offset, existing in enumerate(records):
            if existing.get("id") == rule_id:
                records[offset] = dict(record)
                replaced = True
                break
        if not replaced:
            records.append(dict(record))
        index["rules"] = records

    def _remove_record(self, index: dict[str, Any], rule_id: str) -> None:
        index["rules"] = [record for record in self._index_records(index) if record.get("id") != rule_id]

    def _legacy_content(self, source_path: str, records: list[Mapping[str, Any]]) -> str:
        archive_path = next(
            (str(record.get("archivePath")) for record in records if isinstance(record.get("archivePath"), str)),
            None,
        )
        archive = _read_optional(self.project, archive_path) if archive_path else None
        base = archive or (_read_optional(self.project, source_path) or "")
        prefix, blocks, suffix = _split_bullets(base)
        if not blocks:
            return base
        by_index = {
            int(record.get("blockIndex")): record
            for record in records
            if isinstance(record.get("blockIndex"), int) and record.get("blockIndex") >= 0
        }
        output = [prefix]
        for index, original in enumerate(blocks):
            record = by_index.get(index)
            if record is not None and not bool(record.get("enabled", True)):
                continue
            output.append(str(record.get("content")) if record is not None and isinstance(record.get("content"), str) else original)
        output.append(suffix)
        return "".join(output)

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_KNOWLEDGE", "rule payload must be an object")
        content = _text(payload.get("content", ""), "content")
        _assert_safe_knowledge_text(content, "rule")
        title = _text(payload.get("title", ""), "title", maximum=160, required=False)
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ProjectError("INVALID_KNOWLEDGE", "enabled must be a boolean")
        slug_value = payload.get("slug")
        slug = _safe_slug(slug_value) if slug_value is not None else _generated_slug(title or content)
        source_path = f"{RULES_ROOT}/{slug}.md"
        if _read_optional(self.project, source_path) is not None:
            raise ProjectError("FILE_EXISTS", "a rule with this slug already exists")
        rule_id = _rule_id_for(source_path, 0)
        index = self._index()
        if any(record.get("id") == rule_id for record in self._index_records(index)):
            raise ProjectError("FILE_EXISTS", "a rule with this identity already exists")
        record: dict[str, Any] = {
            "id": rule_id,
            "title": title or _rule_title(content, slug),
            "sourcePath": source_path,
            "sourceFormat": "single",
            "content": content,
            "enabled": enabled,
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
        }
        disabled_path = f"{RULE_DISABLED_ROOT}/{rule_id}.md"
        if not enabled:
            record["disabledPath"] = disabled_path
        for key in ("description", "tags", "scope"):
            if key in payload:
                record[key] = _safe_json(payload[key], path=f"rule.{key}")
        index["schemaVersion"] = KNOWLEDGE_SCHEMA_VERSION
        self._replace_record(index, record)
        files: dict[str, str | None] = {}
        if enabled:
            files[source_path] = content
        else:
            files[disabled_path] = content
        result = self._write_index_and_files(index, files, expected_revision=self._expected(payload))
        created = self.get(rule_id)
        return {"rule": created, "rules": result["rules"], "revision": result["revision"], "created": True}

    @staticmethod
    def _expected(payload: Mapping[str, Any]) -> str | None:
        expected = payload.get("expectedRevision")
        if expected is not None and (not isinstance(expected, str) or not expected):
            raise ProjectError("INVALID_REVISION", "expectedRevision must be a non-empty string")
        return expected

    def save(self, rule_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        rule_id = _safe_rule_id(rule_id)
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_KNOWLEDGE", "rule payload must be an object")
        current = self._find(rule_id)
        index = self._index()
        record = next((dict(item) for item in self._index_records(index) if item.get("id") == rule_id), dict(current))
        content = str(current.get("content", ""))
        if "content" in payload:
            content = _text(payload.get("content"), "content")
            _assert_safe_knowledge_text(content, "rule")
        if "title" in payload:
            record["title"] = _text(payload.get("title"), "title", maximum=160, required=False) or _rule_title(content, rule_id)
        elif not record.get("title"):
            record["title"] = _rule_title(content, rule_id)
        for key in ("description", "tags", "scope"):
            if key in payload:
                record[key] = _safe_json(payload[key], path=f"rule.{key}")
        record["content"] = content
        record["updatedAt"] = utc_now()
        if "enabled" in payload:
            if not isinstance(payload.get("enabled"), bool):
                raise ProjectError("INVALID_KNOWLEDGE", "enabled must be a boolean")
            target_enabled = bool(payload["enabled"])
        else:
            target_enabled = bool(current.get("enabled", True))
        files: dict[str, str | None] = {}
        source_path = str(current.get("sourcePath", ""))
        source_format = str(current.get("sourceFormat", "single"))
        record["sourcePath"] = source_path
        record["sourceFormat"] = source_format
        record["enabled"] = target_enabled
        if source_format == "legacy-list":
            source_records = [
                dict(item)
                for item in self._collect()
                if item.get("sourcePath") == source_path and item.get("sourceFormat") == "legacy-list"
            ]
            if not any(item.get("id") == rule_id for item in source_records):
                source_records.append(record)
            else:
                source_records = [record if item.get("id") == rule_id else item for item in source_records]
            if not any(item.get("archivePath") for item in source_records):
                archive_digest = hashlib.sha256((_read_optional(self.project, source_path) or "").encode("utf-8")).hexdigest()[:16]
                archive_path = f"{RULE_ARCHIVE_ROOT}/{rule_id}-{archive_digest}.md"
                for item in source_records:
                    item["archivePath"] = archive_path
                files[archive_path] = _read_optional(self.project, source_path) or ""
            for item in source_records:
                self._replace_record(index, item)
            files[source_path] = self._legacy_content(source_path, source_records)
            disabled_path = str(record.get("disabledPath") or f"{RULE_DISABLED_ROOT}/{rule_id}.md")
            if target_enabled:
                files[disabled_path] = None
                record.pop("disabledPath", None)
            else:
                record["disabledPath"] = disabled_path
                files[disabled_path] = content
            self._replace_record(index, record)
        else:
            disabled_path = str(record.get("disabledPath") or f"{RULE_DISABLED_ROOT}/{rule_id}.md")
            if target_enabled:
                files[source_path] = content
                files[disabled_path] = None
                record.pop("disabledPath", None)
            else:
                files[source_path] = None
                files[disabled_path] = content
                record["disabledPath"] = disabled_path
            self._replace_record(index, record)
        result = self._write_index_and_files(index, files, expected_revision=self._expected(payload))
        return {"rule": self.get(rule_id), "rules": result["rules"], "revision": result["revision"]}

    def set_enabled(self, rule_id: str, enabled: bool, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ProjectError("INVALID_KNOWLEDGE", "enabled must be a boolean")
        body = dict(payload) if isinstance(payload, Mapping) else {}
        body["enabled"] = enabled
        return self.save(rule_id, body)

    def delete(self, rule_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        rule_id = _safe_rule_id(rule_id)
        current = self._find(rule_id)
        index = self._index()
        self._remove_record(index, rule_id)
        files: dict[str, str | None] = {}
        source_path = str(current.get("sourcePath", ""))
        if current.get("sourceFormat") == "legacy-list":
            source_records = [
                dict(item)
                for item in self._collect()
                if item.get("sourcePath") == source_path and item.get("id") != rule_id
            ]
            files[source_path] = self._legacy_content(source_path, source_records)
            for item in source_records:
                self._replace_record(index, item)
        elif current.get("enabled", True):
            files[source_path] = None
        disabled_path = current.get("disabledPath")
        if isinstance(disabled_path, str):
            files[disabled_path] = None
        result = self._write_index_and_files(index, files, expected_revision=self._expected(payload or {}))
        return {"deleted": True, "id": rule_id, "rules": result["rules"], "revision": result["revision"]}


class SqlCandidateStore:
    """Durable, private SQL review queue."""

    def __init__(self, project: ProjectStore) -> None:
        self.project = project
        self.path = project.state_dir / SQL_CANDIDATES_FILENAME
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"schemaVersion": KNOWLEDGE_SCHEMA_VERSION, "candidates": []}
        except OSError as exc:
            raise ProjectError("KNOWLEDGE_STATE_FAILED", "SQL review state could not be read") from exc
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ProjectError("INVALID_KNOWLEDGE_STATE", "SQL review state is not valid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ProjectError("INVALID_KNOWLEDGE_STATE", "SQL review state must contain an object")
        result = dict(parsed)
        candidates = result.get("candidates", [])
        if not isinstance(candidates, list) or any(not isinstance(item, Mapping) for item in candidates):
            raise ProjectError("INVALID_KNOWLEDGE_STATE", "SQL review candidates must be a list of objects")
        result["candidates"] = [dict(item) for item in candidates]
        result.setdefault("schemaVersion", KNOWLEDGE_SCHEMA_VERSION)
        return result

    def _write(self, document: Mapping[str, Any]) -> None:
        encoded = _json_document(document)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="") as output:
                output.write(encoded)
                output.flush()
                try:
                    os.fsync(output.fileno())
                except OSError:
                    pass
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProjectError("KNOWLEDGE_STATE_FAILED", "SQL review state could not be saved") from exc

    @staticmethod
    def _public(record: Mapping[str, Any]) -> dict[str, Any]:
        # State files may predate the current ingress validation.  A strict
        # response projection prevents legacy or hand-edited extension data
        # from being reflected to the browser.
        allowed = {
            "id", "status", "question", "sql", "dialect", "queryId", "sessionId",
            "stats", "historySqlRefs", "sqlHistory", "slug", "fingerprint",
            "createdAt", "updatedAt", "editedAt", "reviewedAt", "approvedAt",
            "rejectedAt", "approvedPath", "reviewNote", "reviewer",
        }
        return {key: _safe_json(value, path=f"candidate.{key}") for key, value in record.items() if key in allowed}

    def _find(self, candidate_id: str, document: Mapping[str, Any] | None = None) -> dict[str, Any]:
        candidate_id = _safe_candidate_id(candidate_id)
        source = document if document is not None else self._read()
        for raw in source.get("candidates", []):
            if isinstance(raw, Mapping) and raw.get("id") == candidate_id:
                return dict(raw)
        raise ProjectError("SQL_CANDIDATE_NOT_FOUND", "SQL candidate was not found")

    def list(self, status: str | None = None) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            if status is not None:
                status = _text(status, "status", maximum=32)
                if status not in _STATUSES:
                    raise ProjectError("INVALID_KNOWLEDGE", "status is not supported")
            candidates = [
                self._public(item)
                for item in document["candidates"]
                if isinstance(item, Mapping) and (status is None or item.get("status") == status)
            ]
            candidates.sort(key=lambda item: str(item.get("updatedAt", item.get("createdAt", ""))), reverse=True)
            return {
                "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
                "candidates": candidates,
                "pendingCount": sum(item.get("status") == "pending" for item in document["candidates"] if isinstance(item, Mapping)),
                "approvedCount": sum(item.get("status") == "approved" for item in document["candidates"] if isinstance(item, Mapping)),
                "rejectedCount": sum(item.get("status") == "rejected" for item in document["candidates"] if isinstance(item, Mapping)),
            }

    def get(self, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            return self._public(self._find(candidate_id))

    @staticmethod
    def _history(payload: Mapping[str, Any]) -> list[dict[str, str]]:
        # ``sqlHistory`` is the presentation contract used by the Harness
        # client; the other spellings keep the queue compatible with early
        # console builds and direct API callers.
        for key in ("sqlHistory", "historySqlRefs", "historySql", "usedHistorySql", "usedHistorySqlRefs"):
            if key in payload:
                value = payload[key]
                if not isinstance(value, list):
                    raise ProjectError("INVALID_KNOWLEDGE", f"{key} must be a list")
                if len(value) > 5:
                    raise ProjectError("INVALID_KNOWLEDGE", f"{key} contains too many items")
                history: list[dict[str, str]] = []
                allowed = {"id", "question", "sql", "sourcePath"}
                for index, raw in enumerate(value):
                    path = f"{key}[{index}]"
                    if not isinstance(raw, Mapping) or any(field not in allowed for field in raw):
                        raise ProjectError("INVALID_KNOWLEDGE", f"{path} must be a SQL history reference")
                    item = {
                        "id": _text(raw.get("id"), f"{path}.id", maximum=255),
                        "question": _text(raw.get("question"), f"{path}.question", maximum=8_000),
                        "sql": _text(raw.get("sql"), f"{path}.sql", maximum=_MAX_TEXT),
                    }
                    for field_value in item.values():
                        _assert_safe_knowledge_text(field_value, path)
                    source_path = raw.get("sourcePath")
                    if source_path is not None:
                        source_path = _text(source_path, f"{path}.sourcePath", maximum=255)
                        if not re.fullmatch(r"knowledge/sql/[A-Za-z0-9_.-]+\.md", source_path):
                            raise ProjectError("INVALID_PATH", f"{path}.sourcePath is invalid")
                        item["sourcePath"] = source_path
                    history.append(item)
                return history
        return []

    def submit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_KNOWLEDGE", "SQL candidate payload must be an object")
        question = _text(payload.get("question", payload.get("nl", "")), "question", maximum=8_000)
        sql = _text(payload.get("sql", ""), "sql", maximum=_MAX_TEXT)
        _assert_safe_knowledge_text(question, "SQL candidate")
        _assert_safe_knowledge_text(sql, "SQL candidate")
        dialect = _text(payload.get("dialect", "unknown"), "dialect", maximum=64, required=False).strip() or "unknown"
        requested_status = payload.get("status")
        if requested_status is not None and requested_status not in {"pending", ""}:
            # The create endpoint may carry a status field from a replayed
            # presentation, but callers cannot bypass human review by asking
            # it to create an already-approved candidate.
            raise ProjectError("INVALID_KNOWLEDGE", "new SQL candidates must start in pending status")
        query_id = payload.get("queryId")
        session_id = payload.get("sessionId")
        if query_id is not None:
            query_id = _text(query_id, "queryId", maximum=255)
        if session_id is not None:
            session_id = _text(session_id, "sessionId", maximum=255)
        history = self._history(payload)
        stats = _safe_json(payload.get("stats", {}), path="stats") if "stats" in payload else {}
        slug_value = payload.get("slug")
        slug = _safe_slug(slug_value) if slug_value is not None else _generated_slug(question)
        candidate_id = _candidate_id_for(question, sql, dialect)
        with self._lock:
            document = self._read()
            existing = next((dict(item) for item in document["candidates"] if isinstance(item, Mapping) and item.get("id") == candidate_id), None)
            if existing is not None:
                return {"candidate": self._public(existing), "created": False, "duplicate": True}
            # Preserve forward-compatible JSON metadata but never accept
            # client-controlled lifecycle fields or secret-bearing keys.
            record: dict[str, Any] = {}
            reserved = {"id", "status", "createdAt", "updatedAt", "approvedAt", "rejectedAt", "reviewedAt", "approvedPath", "fingerprint"}
            for key, value in payload.items():
                if not isinstance(key, str) or key in reserved or key in {"question", "nl", "sql", "dialect", "queryId", "sessionId", "slug", "status", "stats", "sqlHistory", "historySql", "historySqlRefs", "usedHistorySql", "usedHistorySqlRefs"}:
                    continue
                record[key] = _safe_json(value, path=f"metadata.{key}")
            record.update({
                "id": candidate_id,
                "status": "pending",
                "question": question,
                "sql": sql,
                "dialect": dialect,
                "queryId": query_id,
                "sessionId": session_id,
                "stats": stats,
                "historySqlRefs": history,
                # Keep the presentation spelling in the public record too;
                # this is useful when the queue is inspected after a restart.
                "sqlHistory": history,
                "slug": slug,
                "fingerprint": candidate_id.removeprefix("sql_"),
                "createdAt": utc_now(),
                "updatedAt": utc_now(),
            })
            document["schemaVersion"] = KNOWLEDGE_SCHEMA_VERSION
            document["candidates"].append(record)
            self._write(document)
            return {"candidate": self._public(record), "created": True, "duplicate": False}

    def _replace(self, document: dict[str, Any], record: Mapping[str, Any]) -> None:
        for index, existing in enumerate(document["candidates"]):
            if isinstance(existing, Mapping) and existing.get("id") == record.get("id"):
                document["candidates"][index] = dict(record)
                return
        raise ProjectError("SQL_CANDIDATE_NOT_FOUND", "SQL candidate was not found")

    def mark_review(
        self,
        candidate_id: str,
        status: str,
        *,
        note: Any = None,
        reviewer: Any = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidate_id = _safe_candidate_id(candidate_id)
        if status not in {"pending", "rejected"}:
            raise ProjectError("INVALID_KNOWLEDGE", "review status is not supported")
        with self._lock:
            document = self._read()
            record = self._find(candidate_id, document)
            if record.get("status") == "approved":
                raise ProjectError("SQL_CANDIDATE_LOCKED", "approved SQL candidates cannot be edited")
            if note is not None:
                record["reviewNote"] = _text(note, "reviewNote", maximum=8_000, required=False)
            if reviewer is not None:
                record["reviewer"] = _text(reviewer, "reviewer", maximum=255)
            if extra is not None:
                for key, value in extra.items():
                    if key in {"status", "id", "createdAt", "updatedAt"} or is_sensitive_key(key):
                        continue
                    record[key] = _safe_json(value, path=f"review.{key}")
            record["status"] = status
            record["updatedAt"] = utc_now()
            if status == "rejected":
                record["rejectedAt"] = utc_now()
                record.pop("approvedAt", None)
                record.pop("approvedPath", None)
            else:
                record.pop("rejectedAt", None)
            self._replace(document, record)
            self._write(document)
            return self._public(record)

    def update_pending(self, candidate_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Edit a candidate while it is still reviewable.

        This endpoint is intentionally separate from approval so a reviewer
        can correct the question/SQL and still see a pending state.  The
        candidate identity remains stable (its original fingerprint is kept
        for audit/deduplication), while the edited values become the ones
        persisted on approval.
        """

        candidate_id = _safe_candidate_id(candidate_id)
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_KNOWLEDGE", "SQL candidate payload must be an object")
        with self._lock:
            document = self._read()
            record = self._find(candidate_id, document)
            if record.get("status") == "approved":
                raise ProjectError("SQL_CANDIDATE_LOCKED", "approved SQL candidates cannot be edited")
            if "question" in payload or "nl" in payload:
                question = _text(payload.get("question", payload.get("nl")), "question", maximum=8_000)
                _assert_safe_knowledge_text(question, "SQL candidate")
                record["question"] = question
            if "sql" in payload:
                sql = _text(payload.get("sql"), "sql", maximum=_MAX_TEXT)
                _assert_safe_knowledge_text(sql, "SQL candidate")
                record["sql"] = sql
            if "dialect" in payload:
                record["dialect"] = _text(payload.get("dialect"), "dialect", maximum=64, required=False).strip() or "unknown"
            for key in ("queryId", "sessionId"):
                if key in payload:
                    value = payload[key]
                    record[key] = None if value is None else _text(value, key, maximum=255)
            if any(key in payload for key in ("sqlHistory", "historySqlRefs", "historySql", "usedHistorySql", "usedHistorySqlRefs")):
                record["historySqlRefs"] = self._history(payload)
                record["sqlHistory"] = record["historySqlRefs"]
            if "stats" in payload:
                record["stats"] = _safe_json(payload["stats"], path="stats")
            if "slug" in payload:
                record["slug"] = _safe_slug(payload["slug"])
            reserved = {"question", "nl", "sql", "dialect", "queryId", "sessionId", "sqlHistory", "historySqlRefs", "historySql", "usedHistorySql", "usedHistorySqlRefs", "stats", "slug", "status", "id"}
            for key, value in payload.items():
                if isinstance(key, str) and key not in reserved and not is_sensitive_key(key):
                    record[key] = _safe_json(value, path=f"metadata.{key}")
            record["editedAt"] = utc_now()
            record["updatedAt"] = utc_now()
            self._replace(document, record)
            self._write(document)
            return self._public(record)

    def set_approved(
        self,
        candidate_id: str,
        path: str,
        *,
        reviewer: str | None = None,
        note: str | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidate_id = _safe_candidate_id(candidate_id)
        with self._lock:
            document = self._read()
            record = self._find(candidate_id, document)
            if record.get("status") == "approved":
                return self._public(record)
            if record.get("status") != "pending":
                raise ProjectError("SQL_CANDIDATE_STATE", "only pending SQL candidates can be approved")
            if updates:
                # Apply only the fields that the approval endpoint is allowed
                # to override.  ``update_pending`` contains the same
                # validation rules; keeping this small path local means the
                # queue write remains one atomic operation.
                if "question" in updates:
                    question = _text(updates["question"], "question", maximum=8_000)
                    _assert_safe_knowledge_text(question, "SQL candidate")
                    record["question"] = question
                if "sql" in updates:
                    sql = _text(updates["sql"], "sql", maximum=_MAX_TEXT)
                    _assert_safe_knowledge_text(sql, "SQL candidate")
                    record["sql"] = sql
                if "slug" in updates:
                    record["slug"] = _safe_slug(updates["slug"])
            record["status"] = "approved"
            record["approvedPath"] = path
            record["approvedAt"] = utc_now()
            record["updatedAt"] = utc_now()
            if reviewer is not None:
                record["reviewer"] = _text(reviewer, "reviewer", maximum=255)
            if note is not None:
                record["reviewNote"] = _text(note, "reviewNote", maximum=8_000, required=False)
            self._replace(document, record)
            self._write(document)
            return self._public(record)

    def reset_rejected(self, candidate_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload) if isinstance(payload, Mapping) else {}
        body.pop("status", None)
        return self.mark_review(candidate_id, "pending", note=body.get("reviewNote"), reviewer=body.get("reviewer"))


def sql_markdown(question: str, sql: str, *, note: str | None = None) -> str:
    """Render a review-approved SQL example without interpolating YAML."""

    # yaml.safe_dump quotes/escapes arbitrary questions and keeps SQL in a
    # literal block, avoiding frontmatter injection via ``---`` in user text.
    frontmatter: dict[str, Any] = {"nl": question, "sql": sql}
    rendered = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False)
    body = "---\n" + rendered + "---\n"
    if note:
        body += "\n" + note.rstrip() + "\n"
    return body


__all__ = [
    "KNOWLEDGE_SCHEMA_VERSION",
    "RULES_ROOT",
    "SQL_ROOT",
    "RULE_INDEX_PATH",
    "RuleStore",
    "SqlCandidateStore",
    "sql_markdown",
]
