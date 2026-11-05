"""Durable, evidence-backed resolution of feedback reports.

This store records links to published semantic assets, immutable regression
case revisions, actual SQL regression runs, and reviewed external Agent
evaluation summaries. It never stores query results, SQL, report bodies, or
Core Agent claims.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

try:
    from .access_control import (
        AccessControlError,
        AuthContext,
        BOOTSTRAP_SUBJECT_ID,
        _timestamp,
        _utc_now,
    )
    from .authorization import PolicyEngine
    from .diagnostics import DiagnosticError, _SECRET
    from .project import ProjectError, _safe_relative, _secret_file_path
except ImportError:  # pragma: no cover - direct module loading
    from access_control import (  # type: ignore[no-redef]
        AccessControlError,
        AuthContext,
        BOOTSTRAP_SUBJECT_ID,
        _timestamp,
        _utc_now,
    )
    from authorization import PolicyEngine  # type: ignore[no-redef]
    from diagnostics import DiagnosticError, _SECRET  # type: ignore[no-redef]
    from project import ProjectError, _safe_relative, _secret_file_path  # type: ignore[no-redef]


_SCHEMA_VERSION = 1
_MIGRATION_LOCK = 8_341_972_315_443_005
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_RESOLUTION_FIELDS = {
    "assetPath",
    "baseVersionId",
    "publishedVersionId",
    "caseId",
    "caseRevision",
    "runId",
}
_EVALUATION_FIELDS = {
    "schemaVersion",
    "campaignId",
    "reportSha256",
    "source",
    "model",
    "reasoning",
    "datasetVersion",
    "semanticVersion",
    "verdictAggregates",
}


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - all callers validate first
        raise DiagnosticError("INVALID_REQUEST", "resolution data must be JSON-safe") from exc


def _identifier(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value) or _SECRET.search(value):
        raise DiagnosticError("INVALID_REQUEST", f"{field} is invalid")
    return value


def _text(value: Any, field: str, *, max_length: int = 160) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > max_length
        or _SECRET.search(value)
        or any(ord(character) < 32 for character in value)
    ):
        raise DiagnosticError("INVALID_AGENT_EVALUATION", f"{field} is invalid")
    return value.strip()


def _now(clock: Callable[[], datetime]) -> datetime:
    result = clock()
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _parse_stored_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(UTC)


class FeedbackResolutionStore:
    """Persist feedback-resolution links in a separate control-plane ledger."""

    def __init__(
        self,
        access: Any,
        diagnostics: Any,
        project: Any,
        regression_store: Any,
        *,
        policy_engine: PolicyEngine | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.access = access
        self.diagnostics = diagnostics
        self.project = project
        self.regression_store = regression_store
        self.policy_engine = policy_engine or PolicyEngine()
        self.clock = clock or getattr(access, "clock", _utc_now)
        self._migrate()

    def _migrate(self) -> None:
        with self.access._lock, self.access._connect() as connection:
            if getattr(self.access, "path", None) is None:
                connection.execute("SELECT pg_advisory_xact_lock(?)", (_MIGRATION_LOCK,))
            else:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS feedback_resolution_schema_migrations "
                "(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)"
            )
            versions = [
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM feedback_resolution_schema_migrations ORDER BY version"
                ).fetchall()
            ]
            if versions != list(range(1, len(versions) + 1)) or any(
                version > _SCHEMA_VERSION for version in versions
            ):
                raise DiagnosticError(
                    "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE",
                    "feedback resolution storage schema is incompatible",
                    status=503,
                )
            version = versions[-1] if versions else 0
            if version < 1:
                for statement in (
                    "CREATE TABLE feedback_resolutions(organization_id TEXT NOT NULL,project_id TEXT NOT NULL,feedback_id TEXT NOT NULL,revision INTEGER NOT NULL,record_json TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(organization_id,project_id,feedback_id))",
                    "CREATE TABLE feedback_agent_evaluations(id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,project_id TEXT NOT NULL,feedback_id TEXT NOT NULL,revision INTEGER NOT NULL,record_json TEXT NOT NULL,imported_at TEXT NOT NULL)",
                    "CREATE INDEX feedback_agent_eval_scope_idx ON feedback_agent_evaluations(organization_id,project_id,feedback_id,imported_at,id)",
                ):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO feedback_resolution_schema_migrations(version,applied_at) VALUES(?,?)",
                    (1, _timestamp(self.clock())),
                )

    def _require_admin(self, auth: AuthContext, project_id: str) -> None:
        policies = (
            []
            if auth.subject.id == BOOTSTRAP_SUBJECT_ID
            else self.access.policies_for_subject(auth.subject.id)
        )
        decision = self.policy_engine.authorize_scope(
            auth.subject, "console:admin", policies, project_id=project_id
        )
        if not decision.allowed:
            raise AccessControlError(
                "FORBIDDEN", "console administrator permission is required", status=403
            )

    @staticmethod
    def _stored_mapping(value: Any, label: str) -> dict[str, Any]:
        try:
            record = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise DiagnosticError(
                "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE",
                f"stored {label} record is invalid",
                status=503,
            ) from exc
        if not isinstance(record, Mapping):
            raise DiagnosticError(
                "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE",
                f"stored {label} record is invalid",
                status=503,
            )
        return dict(record)

    @staticmethod
    def _require_stored_schema(record: Mapping[str, Any], version: int, label: str) -> None:
        if type(record.get("schemaVersion")) is not int or record.get("schemaVersion") != version:
            raise DiagnosticError(
                "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE",
                f"stored {label} schema is unsupported",
                status=503,
            )

    @staticmethod
    def _invalid_stored_record(label: str) -> DiagnosticError:
        return DiagnosticError(
            "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE",
            f"stored {label} record is invalid",
            status=503,
        )

    def _resolution_record(self, value: Any, feedback_id: str, row_revision: Any) -> dict[str, Any]:
        record = self._stored_mapping(value, "feedback resolution")
        self._require_stored_schema(record, 1, "feedback resolution")
        expected_fields = {
            "schemaVersion", "feedbackId", "revision", "assetPath", "baseVersionId",
            "publishedVersionId", "assetSha256", "caseId", "caseRevision", "runId",
            "updatedAt", "updatedBy",
        }
        if set(record) != expected_fields:
            raise self._invalid_stored_record("feedback resolution")
        if (
            record.get("feedbackId") != feedback_id
            or type(record.get("revision")) is not int
            or record["revision"] < 1
            or type(row_revision) is not int
            or record["revision"] != row_revision
        ):
            raise self._invalid_stored_record("feedback resolution")
        for field in ("assetPath", "baseVersionId", "publishedVersionId", "caseId", "runId", "updatedBy"):
            if record[field] is not None and not isinstance(record[field], str):
                raise self._invalid_stored_record("feedback resolution")
        try:
            if record["assetPath"] is not None:
                self._asset_path(record["assetPath"], required=True)
            for field in ("baseVersionId", "publishedVersionId", "caseId", "runId", "updatedBy"):
                if record[field] is not None:
                    _identifier(record[field], field)
        except DiagnosticError as exc:
            raise self._invalid_stored_record("feedback resolution") from exc
        if record["caseRevision"] is not None and (
            type(record["caseRevision"]) is not int or record["caseRevision"] < 1
        ):
            raise self._invalid_stored_record("feedback resolution")
        if record["assetSha256"] is not None and (
            not isinstance(record["assetSha256"], str) or not _SHA256.fullmatch(record["assetSha256"])
        ):
            raise self._invalid_stored_record("feedback resolution")
        if (record["caseId"] is None) != (record["caseRevision"] is None):
            raise self._invalid_stored_record("feedback resolution")
        if record["updatedAt"] is not None and _parse_stored_time(record["updatedAt"]) is None:
            raise self._invalid_stored_record("feedback resolution")
        return record

    def _evaluation_record(self, value: Any, row_revision: Any | None = None) -> dict[str, Any]:
        record = self._stored_mapping(value, "external Agent evaluation")
        self._require_stored_schema(record, 1, "external Agent evaluation")
        expected_fields = {
            "schemaVersion", "id", "feedbackId", "campaignId", "reportSha256", "source",
            "model", "reasoning", "datasetVersion", "semanticVersion", "verdictAggregates",
            "revision", "importedBy", "importedAt", "review",
        }
        if set(record) != expected_fields:
            raise self._invalid_stored_record("external Agent evaluation")
        if (
            any(not isinstance(record.get(field), str) or not record[field] for field in (
                "id", "feedbackId", "campaignId", "source", "model", "reasoning",
                "datasetVersion", "semanticVersion", "importedBy",
            ))
            or not isinstance(record.get("reportSha256"), str)
            or not _SHA256.fullmatch(record["reportSha256"])
            or type(record.get("revision")) is not int
            or record["revision"] < 1
            or (row_revision is not None and record["revision"] != row_revision)
            or _parse_stored_time(record.get("importedAt")) is None
        ):
            raise self._invalid_stored_record("external Agent evaluation")
        try:
            for field in ("id", "feedbackId", "campaignId", "importedBy"):
                _identifier(record[field], field)
            for field in ("source", "model", "reasoning", "datasetVersion", "semanticVersion"):
                _text(record[field], field)
        except DiagnosticError as exc:
            raise self._invalid_stored_record("external Agent evaluation") from exc
        aggregates = record.get("verdictAggregates")
        if (
            not isinstance(aggregates, Mapping)
            or set(aggregates) != {"passed", "failed", "inconclusive", "total"}
            or any(type(aggregates.get(key)) is not int or aggregates[key] < 0 for key in aggregates)
            or aggregates["total"] < 1
            or aggregates["total"] > 1_000_000
            or sum(aggregates[key] for key in ("passed", "failed", "inconclusive")) != aggregates["total"]
        ):
            raise self._invalid_stored_record("external Agent evaluation")
        review = record.get("review")
        if review is not None and (
            not isinstance(review, Mapping)
            or set(review) != {"decision", "reviewerSubjectId", "reviewedAt"}
            or review.get("decision") not in {"approved", "rejected"}
            or not isinstance(review.get("reviewerSubjectId"), str)
            or not review["reviewerSubjectId"]
            or _parse_stored_time(review.get("reviewedAt")) is None
        ):
            raise self._invalid_stored_record("external Agent evaluation")
        if review is not None:
            try:
                _identifier(review["reviewerSubjectId"], "reviewerSubjectId")
            except DiagnosticError as exc:
                raise self._invalid_stored_record("external Agent evaluation") from exc
        return record

    @staticmethod
    def scope(auth: AuthContext, project_id: str) -> tuple[str, str]:
        if not isinstance(project_id, str) or not project_id.strip():
            raise DiagnosticError("INVALID_REQUEST", "project identifier is invalid")
        return auth.subject.organization_id, project_id

    def get(self, auth: AuthContext, project_id: str, feedback_id: str) -> dict[str, Any]:
        self._require_admin(auth, project_id)
        scope = self.scope(auth, project_id)
        feedback_id = self._feedback_id(feedback_id, scope)
        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT revision,record_json FROM feedback_resolutions "
                "WHERE organization_id=? AND project_id=? AND feedback_id=?",
                (*scope, feedback_id),
            ).fetchone()
        if row is None:
            stored = self._empty_resolution(feedback_id)
        else:
            stored = self._resolution_record(row["record_json"], feedback_id, row["revision"])
        evaluations = self._evaluations(scope, feedback_id)
        stored["agentEvaluations"] = evaluations
        stored["verification"] = self._verify(scope, feedback_id, stored, evaluations)
        return stored

    def save(
        self,
        auth: AuthContext,
        project_id: str,
        feedback_id: str,
        payload: Any,
    ) -> dict[str, Any]:
        self._require_admin(auth, project_id)
        scope = self.scope(auth, project_id)
        feedback_id = self._feedback_id(feedback_id, scope)
        if not isinstance(payload, Mapping) or set(payload) != {"expectedRevision", *_RESOLUTION_FIELDS}:
            raise DiagnosticError("INVALID_REQUEST", "feedback resolution fields are invalid")
        expected = payload.get("expectedRevision")
        if type(expected) is not int or expected < 0:
            raise DiagnosticError("INVALID_REQUEST", "expectedRevision is invalid")

        asset_path = self._asset_path(payload.get("assetPath"), required=False)
        base_version_id = _identifier(payload.get("baseVersionId"), "baseVersionId", required=False)
        published_version_id = _identifier(
            payload.get("publishedVersionId"), "publishedVersionId", required=False
        )
        case_id = _identifier(payload.get("caseId"), "caseId", required=False)
        case_revision = payload.get("caseRevision")
        if case_revision is not None and (type(case_revision) is not int or case_revision < 1):
            raise DiagnosticError("INVALID_REQUEST", "caseRevision is invalid")
        run_id = _identifier(payload.get("runId"), "runId", required=False)

        if any(value is not None for value in (asset_path, base_version_id, published_version_id)):
            if not all(value is not None for value in (asset_path, base_version_id, published_version_id)):
                raise DiagnosticError(
                    "INVALID_REQUEST", "assetPath and both published version identifiers are required together"
                )
            asset_sha256 = self._changed_asset_hash(asset_path, base_version_id, published_version_id)
        else:
            asset_sha256 = None
        if (case_id is None) != (case_revision is None):
            raise DiagnosticError("INVALID_REQUEST", "caseId and caseRevision must be provided together")
        if run_id is not None and case_id is None:
            raise DiagnosticError("INVALID_REQUEST", "a run must be linked to a case revision")
        if case_id is not None:
            self._case_revision(scope, feedback_id, case_id, case_revision)
        if run_id is not None:
            self._run(scope, feedback_id, case_id, case_revision, run_id)

        now = _timestamp(self.clock())
        with self.access._lock, self.access._connect() as connection:
            if getattr(self.access, "path", None) is None:
                connection.execute("SELECT pg_advisory_xact_lock(?)", (_MIGRATION_LOCK,))
            else:
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM feedback_resolutions "
                "WHERE organization_id=? AND project_id=? AND feedback_id=?",
                (*scope, feedback_id),
            ).fetchone()
            actual = int(row["revision"]) if row is not None else 0
            if actual != expected:
                raise DiagnosticError(
                    "FEEDBACK_RESOLUTION_REVISION_CONFLICT",
                    "feedback resolution changed since it was read",
                    status=409,
                )
            revision = actual + 1
            record = {
                "schemaVersion": 1,
                "feedbackId": feedback_id,
                "revision": revision,
                "assetPath": asset_path,
                "baseVersionId": base_version_id,
                "publishedVersionId": published_version_id,
                "assetSha256": asset_sha256,
                "caseId": case_id,
                "caseRevision": case_revision,
                "runId": run_id,
                "updatedAt": now,
                "updatedBy": auth.subject.id,
            }
            if row is None:
                connection.execute(
                    "INSERT INTO feedback_resolutions(organization_id,project_id,feedback_id,revision,record_json,updated_at) VALUES(?,?,?,?,?,?)",
                    (*scope, feedback_id, revision, _json(record), now),
                )
            else:
                changed = connection.execute(
                    "UPDATE feedback_resolutions SET revision=?,record_json=?,updated_at=? "
                    "WHERE organization_id=? AND project_id=? AND feedback_id=? AND revision=?",
                    (revision, _json(record), now, *scope, feedback_id, expected),
                ).rowcount
                if changed != 1:
                    raise DiagnosticError(
                        "FEEDBACK_RESOLUTION_REVISION_CONFLICT",
                        "feedback resolution changed since it was read",
                        status=409,
                    )
        return self.get(auth, project_id, feedback_id)

    def verify(self, auth: AuthContext, project_id: str, feedback_id: str) -> dict[str, Any]:
        return self.get(auth, project_id, feedback_id)

    def import_evaluation(
        self,
        auth: AuthContext,
        project_id: str,
        feedback_id: str,
        payload: Any,
    ) -> dict[str, Any]:
        self._require_admin(auth, project_id)
        scope = self.scope(auth, project_id)
        feedback_id = self._feedback_id(feedback_id, scope)
        if not isinstance(payload, Mapping) or set(payload) != _EVALUATION_FIELDS:
            raise DiagnosticError("INVALID_AGENT_EVALUATION", "external Agent summary fields are invalid")
        if type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1:
            raise DiagnosticError("INVALID_AGENT_EVALUATION", "external Agent summary schema is unsupported")
        campaign_id = _identifier(payload.get("campaignId"), "campaignId")
        report_sha = payload.get("reportSha256")
        if not isinstance(report_sha, str) or not _SHA256.fullmatch(report_sha):
            raise DiagnosticError("INVALID_AGENT_EVALUATION", "reportSha256 must be a SHA-256 digest")
        source = _text(payload.get("source"), "source")
        model = _text(payload.get("model"), "model")
        reasoning = _text(payload.get("reasoning"), "reasoning")
        dataset_version = _text(payload.get("datasetVersion"), "datasetVersion")
        semantic_version = _text(payload.get("semanticVersion"), "semanticVersion")
        aggregates = payload.get("verdictAggregates")
        if not isinstance(aggregates, Mapping) or set(aggregates) != {
            "passed", "failed", "inconclusive", "total"
        }:
            raise DiagnosticError("INVALID_AGENT_EVALUATION", "verdictAggregates fields are invalid")
        counts = {key: aggregates.get(key) for key in ("passed", "failed", "inconclusive", "total")}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise DiagnosticError("INVALID_AGENT_EVALUATION", "verdictAggregates counts are invalid")
        if counts["total"] < 1 or counts["total"] > 1_000_000 or sum(
            counts[key] for key in ("passed", "failed", "inconclusive")
        ) != counts["total"]:
            raise DiagnosticError("INVALID_AGENT_EVALUATION", "verdictAggregates total does not match")

        now = _timestamp(self.clock())
        summary = {
            "schemaVersion": 1,
            "id": f"aeval_{uuid.uuid4().hex}",
            "feedbackId": feedback_id,
            "campaignId": campaign_id,
            "reportSha256": report_sha,
            "source": source,
            "model": model,
            "reasoning": reasoning,
            "datasetVersion": dataset_version,
            "semanticVersion": semantic_version,
            "verdictAggregates": counts,
            "revision": 1,
            "importedBy": auth.subject.id,
            "importedAt": now,
            "review": None,
        }
        with self.access._lock, self.access._connect() as connection:
            if getattr(self.access, "path", None) is None:
                connection.execute("SELECT pg_advisory_xact_lock(?)", (_MIGRATION_LOCK,))
            else:
                connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id,revision,record_json FROM feedback_agent_evaluations "
                "WHERE organization_id=? AND project_id=? AND feedback_id=? AND imported_at IN "
                "(SELECT imported_at FROM feedback_agent_evaluations WHERE organization_id=? AND project_id=? AND feedback_id=?) "
                "AND id IN (SELECT id FROM feedback_agent_evaluations WHERE organization_id=? AND project_id=? AND feedback_id=?) "
                "ORDER BY imported_at,id",
                (*scope, feedback_id, *scope, feedback_id, *scope, feedback_id),
            ).fetchall()
            for item in existing:
                previous = self._evaluation_record(item["record_json"], item["revision"])
                if previous.get("campaignId") == campaign_id and previous.get("reportSha256") == report_sha:
                    same_summary = all(
                        previous.get(key) == summary.get(key)
                        for key in (
                            "schemaVersion", "campaignId", "reportSha256", "source", "model",
                            "reasoning", "datasetVersion", "semanticVersion", "verdictAggregates",
                        )
                    )
                    if not same_summary:
                        raise DiagnosticError(
                            "AGENT_EVALUATION_CONFLICT",
                            "campaign and report digest already identify a different summary",
                            status=409,
                        )
                    return previous
            connection.execute(
                "INSERT INTO feedback_agent_evaluations(id,organization_id,project_id,feedback_id,revision,record_json,imported_at) VALUES(?,?,?,?,?,?,?)",
                (summary["id"], *scope, feedback_id, 1, _json(summary), now),
            )
        return summary

    def review_evaluation(
        self,
        auth: AuthContext,
        project_id: str,
        feedback_id: str,
        evaluation_id: str,
        payload: Any,
    ) -> dict[str, Any]:
        self._require_admin(auth, project_id)
        scope = self.scope(auth, project_id)
        feedback_id = self._feedback_id(feedback_id, scope)
        evaluation_id = _identifier(evaluation_id, "evaluationId")
        if not isinstance(payload, Mapping) or set(payload) != {"expectedRevision", "decision"}:
            raise DiagnosticError("INVALID_AGENT_EVALUATION", "review fields are invalid")
        expected = payload.get("expectedRevision")
        decision = payload.get("decision")
        if type(expected) is not int or expected < 1 or decision not in {"approved", "rejected"}:
            raise DiagnosticError("INVALID_AGENT_EVALUATION", "review fields are invalid")
        now = _timestamp(self.clock())
        with self.access._lock, self.access._connect() as connection:
            if getattr(self.access, "path", None) is None:
                connection.execute("SELECT pg_advisory_xact_lock(?)", (_MIGRATION_LOCK,))
            else:
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision,record_json FROM feedback_agent_evaluations "
                "WHERE id=? AND organization_id=? AND project_id=? AND feedback_id=?",
                (evaluation_id, *scope, feedback_id),
            ).fetchone()
            if row is None:
                raise DiagnosticError("AGENT_EVALUATION_NOT_FOUND", "external Agent summary was not found", status=404)
            if int(row["revision"]) != expected:
                raise DiagnosticError(
                    "AGENT_EVALUATION_REVISION_CONFLICT",
                    "external Agent summary changed since it was read",
                    status=409,
                )
            record = self._evaluation_record(row["record_json"], row["revision"])
            revision = expected + 1
            record["revision"] = revision
            record["review"] = {
                "decision": decision,
                "reviewerSubjectId": auth.subject.id,
                "reviewedAt": now,
            }
            changed = connection.execute(
                "UPDATE feedback_agent_evaluations SET revision=?,record_json=? "
                "WHERE id=? AND organization_id=? AND project_id=? AND feedback_id=? AND revision=?",
                (revision, _json(record), evaluation_id, *scope, feedback_id, expected),
            ).rowcount
            if changed != 1:
                raise DiagnosticError(
                    "AGENT_EVALUATION_REVISION_CONFLICT",
                    "external Agent summary changed since it was read",
                    status=409,
                )
        return record

    def _feedback_id(self, feedback_id: Any, scope: tuple[str, str]) -> str:
        identifier = _identifier(feedback_id, "feedbackId")
        self.diagnostics.feedback_detail(
            identifier,
            organization_id=scope[0],
            project_id=scope[1],
        )
        return identifier

    @staticmethod
    def _empty_resolution(feedback_id: str) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "feedbackId": feedback_id,
            "revision": 0,
            "assetPath": None,
            "baseVersionId": None,
            "publishedVersionId": None,
            "assetSha256": None,
            "caseId": None,
            "caseRevision": None,
            "runId": None,
            "updatedAt": None,
            "updatedBy": None,
        }

    def _asset_path(self, value: Any, *, required: bool) -> str | None:
        if value is None and not required:
            return None
        try:
            relative = _safe_relative(value, allow_target=False)
        except (ProjectError, TypeError) as exc:
            raise DiagnosticError("INVALID_RESOLUTION", "semantic asset path is invalid") from exc
        if (
            not relative.startswith(("models/", "knowledge/", "views/", "cubes/"))
            or _secret_file_path(relative)
            or _SECRET.search(relative)
        ):
            raise DiagnosticError("INVALID_RESOLUTION", "semantic asset path is invalid")
        return relative

    def _versions(self) -> dict[str, dict[str, Any]]:
        try:
            versions = self.project.versions()
        except Exception as exc:
            raise DiagnosticError(
                "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE",
                "published semantic versions are unavailable",
                status=503,
            ) from exc
        return {item["id"]: item for item in versions if isinstance(item, Mapping) and isinstance(item.get("id"), str)}

    def _version_asset_hash(self, version_id: str, asset_path: str) -> str | None:
        version = self._versions().get(version_id)
        if version is None:
            return None
        root = Path(self.project.versions_dir) / version_id / "project"
        candidate = root / Path(asset_path)
        try:
            resolved_root = root.resolve(strict=True)
            try:
                resolved_candidate = candidate.resolve(strict=True)
            except FileNotFoundError:
                return None
            resolved_candidate.relative_to(resolved_root)
            if candidate.is_symlink() or not resolved_candidate.is_file():
                raise DiagnosticError("INVALID_RESOLUTION", "semantic asset snapshot is not a regular file")
            if resolved_candidate.stat().st_size > 2_097_152:
                raise DiagnosticError("INVALID_RESOLUTION", "semantic asset snapshot exceeds the size limit")
            data = resolved_candidate.read_bytes()
        except DiagnosticError:
            raise
        except (OSError, ValueError) as exc:
            raise DiagnosticError(
                "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE",
                "published semantic asset snapshot is unavailable",
                status=503,
            ) from exc
        return hashlib.sha256(data).hexdigest()

    def _changed_asset_hash(self, asset_path: str, base_version_id: str, published_version_id: str) -> str:
        if base_version_id == published_version_id:
            raise DiagnosticError(
                "INVALID_RESOLUTION", "the semantic asset must be compared across two published versions"
            )
        versions = self._versions()
        if base_version_id not in versions or published_version_id not in versions:
            raise DiagnosticError("PUBLISHED_VERSION_NOT_FOUND", "a published semantic version was not found", status=404)
        base_created = _parse_stored_time(versions[base_version_id].get("createdAt"))
        published_created = _parse_stored_time(versions[published_version_id].get("createdAt"))
        if base_created is None or published_created is None or base_created >= published_created:
            raise DiagnosticError(
                "INVALID_RESOLUTION",
                "the base semantic version must predate the published fix version",
            )
        try:
            current_revision = self.project.published_revision
        except Exception as exc:
            raise DiagnosticError(
                "FEEDBACK_RESOLUTION_STORE_UNAVAILABLE",
                "current published semantic revision is unavailable",
                status=503,
            ) from exc
        if versions[published_version_id].get("revision") != current_revision:
            raise DiagnosticError(
                "PUBLISHED_VERSION_STALE",
                "the selected fix version is not the current published semantic revision",
                status=409,
            )
        current_hash = self._version_asset_hash(published_version_id, asset_path)
        if current_hash is None:
            raise DiagnosticError("SEMANTIC_ASSET_NOT_FOUND", "semantic asset was not found in the published version", status=404)
        base_hash = self._version_asset_hash(base_version_id, asset_path)
        if base_hash == current_hash:
            raise DiagnosticError("SEMANTIC_ASSET_UNCHANGED", "semantic asset did not change between published versions")
        return current_hash

    def _case_revision(
        self,
        scope: tuple[str, str],
        feedback_id: str,
        case_id: str,
        case_revision: int,
    ) -> dict[str, Any] | None:
        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT c.revision,c.record_json,(SELECT MAX(x.revision) "
                "FROM regression_case_versions x "
                "WHERE x.organization_id=c.organization_id AND x.project_id=c.project_id AND x.id=c.id) AS head_revision "
                "FROM regression_case_versions c "
                "WHERE c.organization_id=? AND c.project_id=? AND c.id=? AND c.revision=?",
                (*scope, case_id, case_revision),
            ).fetchone()
        if row is None:
            raise DiagnosticError("CASE_REVISION_NOT_FOUND", "regression case revision was not found", status=404)
        record = self._stored_mapping(row["record_json"], "regression case")
        case = record.get("case")
        if (
            set(record) != {
                "id", "feedbackId", "sourceTraceId", "revision", "status", "case",
                "datasetVersion", "createdAt", "updatedAt",
            }
            or not isinstance(case, Mapping)
            or type(case.get("schemaVersion")) is not int
            or case.get("schemaVersion") != 2
            or record.get("id") != case_id
            or type(record.get("revision")) is not int
            or record.get("revision") != row["revision"]
            or record.get("status") not in {"draft", "enabled", "deleted"}
            or (record.get("datasetVersion") is not None and not isinstance(record.get("datasetVersion"), str))
            or _parse_stored_time(record.get("createdAt")) is None
            or _parse_stored_time(record.get("updatedAt")) is None
        ):
            raise self._invalid_stored_record("regression case")
        if record.get("feedbackId") != feedback_id:
            raise DiagnosticError("RESOLUTION_EVIDENCE_CONFLICT", "regression case belongs to different feedback", status=409)
        if type(row["head_revision"]) is not int or row["head_revision"] != case_revision:
            raise DiagnosticError(
                "CASE_REVISION_STALE",
                "linked regression case revision is no longer current",
                status=409,
            )
        return record

    def _run(
        self,
        scope: tuple[str, str],
        feedback_id: str,
        case_id: str | None,
        case_revision: int | None,
        run_id: str,
    ) -> dict[str, Any]:
        if case_id is None or case_revision is None:
            raise DiagnosticError("INVALID_RESOLUTION", "a run must be linked to a case revision")
        try:
            run = self.regression_store.run(scope, run_id)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise self._invalid_stored_record("regression run") from exc
        if not isinstance(run, Mapping):
            raise self._invalid_stored_record("regression run")
        run = dict(run)
        self._require_stored_schema(run, 1, "regression run")
        if set(run) != {
            "schemaVersion", "id", "status", "subjectId", "caseIds", "results",
            "createdAt", "updatedAt", "expiresAt",
        }:
            raise self._invalid_stored_record("regression run")
        expiry = _parse_stored_time(run.get("expiresAt"))
        if expiry is None:
            raise self._invalid_stored_record("regression run")
        if expiry <= _now(self.clock):
            raise DiagnosticError("RUN_NOT_FOUND", "regression run was not found or has expired", status=404)
        if (
            run.get("id") != run_id
            or run.get("status") not in {"queued", "running", "completed", "cancelled", "interrupted"}
            or not isinstance(run.get("subjectId"), str)
            or not isinstance(run.get("caseIds"), list)
            or not isinstance(run.get("results"), list)
            or _parse_stored_time(run.get("createdAt")) is None
            or _parse_stored_time(run.get("updatedAt")) is None
        ):
            raise self._invalid_stored_record("regression run")
        result = next(
            (
                item
                for item in run.get("results", [])
                if isinstance(item, Mapping) and item.get("caseId") == case_id
            ),
            None,
        )
        if (
            run.get("status") != "completed"
            or result is None
            or result.get("caseRevision") != case_revision
            or result.get("feedbackId") != feedback_id
        ):
            raise DiagnosticError("RESOLUTION_EVIDENCE_CONFLICT", "run does not match this feedback and case revision", status=409)
        return run

    def _evaluations(self, scope: tuple[str, str], feedback_id: str) -> list[dict[str, Any]]:
        with self.access._connect() as connection:
            rows = connection.execute(
                "SELECT revision,record_json FROM feedback_agent_evaluations "
                "WHERE organization_id=? AND project_id=? AND feedback_id=? ORDER BY imported_at,id",
                (*scope, feedback_id),
            ).fetchall()
        return [self._evaluation_record(row["record_json"], row["revision"]) for row in rows]

    def _feedback_evidence_status(
        self,
        scope: tuple[str, str],
        feedback_id: str,
        base_semantic_version: str | None,
        published_semantic_versions: set[str],
    ) -> tuple[str, str]:
        with self.access._connect() as connection:
            row = connection.execute(
                "SELECT d.expires_at,d.content_purged_at,d.semantic_version,d.query_id "
                "FROM query_feedback f "
                "JOIN query_diagnostics d ON d.id=f.diagnostic_id "
                "WHERE f.id=? AND f.organization_id=? AND f.project_id=?",
                (feedback_id, *scope),
            ).fetchone()
        if row is None:
            return "conflict", "FEEDBACK_EVIDENCE_UNAVAILABLE"
        expiry = _parse_stored_time(row["expires_at"])
        if row["content_purged_at"] is not None or expiry is None or expiry <= _now(self.clock):
            return "stale", "FEEDBACK_EVIDENCE_EXPIRED"
        if not isinstance(row["query_id"], str) or not row["query_id"] or base_semantic_version is None:
            return "unverified", "FEEDBACK_SEMANTIC_VERSION_UNKNOWN"
        feedback_semantic_version = row["semantic_version"]
        if not isinstance(feedback_semantic_version, str) or not feedback_semantic_version:
            return "unverified", "FEEDBACK_SEMANTIC_VERSION_UNKNOWN"
        if feedback_semantic_version == base_semantic_version:
            return "passed", "SOURCE_FEEDBACK_MATCHES_BASE_VERSION"
        if feedback_semantic_version not in published_semantic_versions:
            return "unverified", "FEEDBACK_SEMANTIC_VERSION_UNKNOWN"
        return "conflict", "FEEDBACK_SEMANTIC_VERSION_MISMATCH"

    def _verify(
        self,
        scope: tuple[str, str],
        feedback_id: str,
        resolution: Mapping[str, Any],
        evaluations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        checks: list[dict[str, str]] = []

        def check(key: str, status: str, reason: str) -> None:
            checks.append({"key": key, "status": status, "reasonCode": reason})

        path = resolution.get("assetPath")
        base_id = resolution.get("baseVersionId")
        published_id = resolution.get("publishedVersionId")
        versions = self._versions()
        base_version = versions.get(base_id) if isinstance(base_id, str) else None
        published_version = versions.get(published_id) if isinstance(published_id, str) else None
        semantic_version: str | None = None
        base_semantic_version = base_version.get("revision") if base_version is not None else None
        if not isinstance(path, str) or not isinstance(base_id, str) or not isinstance(published_id, str):
            check("semanticAsset", "unverified", "SEMANTIC_ASSET_NOT_LINKED")
        elif published_version is None or base_version is None:
            check("semanticAsset", "stale", "PUBLISHED_VERSION_UNAVAILABLE")
        else:
            try:
                current_hash = self._changed_asset_hash(path, base_id, published_id)
            except DiagnosticError as exc:
                if exc.code in {"PUBLISHED_VERSION_NOT_FOUND", "PUBLISHED_VERSION_STALE", "SEMANTIC_ASSET_NOT_FOUND"}:
                    state = "stale"
                else:
                    state = "conflict"
                check("semanticAsset", state, exc.code)
            else:
                semantic_version = published_version.get("revision")
                if current_hash != resolution.get("assetSha256"):
                    check("semanticAsset", "conflict", "SEMANTIC_ASSET_DIGEST_CHANGED")
                else:
                    check("semanticAsset", "passed", "PUBLISHED_ASSET_CHANGED")

        feedback_status, feedback_reason = self._feedback_evidence_status(
            scope,
            feedback_id,
            base_semantic_version if isinstance(base_semantic_version, str) else None,
            {
                item["revision"]
                for item in versions.values()
                if isinstance(item.get("revision"), str) and item["revision"]
            },
        )
        check("feedbackEvidence", feedback_status, feedback_reason)

        case_id = resolution.get("caseId")
        case_revision = resolution.get("caseRevision")
        case_record: dict[str, Any] | None = None
        if not isinstance(case_id, str) or type(case_revision) is not int:
            check("regressionCase", "unverified", "CASE_REVISION_NOT_LINKED")
        else:
            try:
                case_record = self._case_revision(scope, feedback_id, case_id, case_revision)
                if case_record.get("status") == "deleted":
                    check("regressionCase", "stale", "CASE_REVISION_DELETED")
                    case_record = None
                elif case_record.get("status") != "enabled":
                    check("regressionCase", "conflict", "CASE_REVISION_NOT_ENABLED")
                    case_record = None
                elif not isinstance(case_record.get("datasetVersion"), str) or not case_record["datasetVersion"]:
                    check("regressionCase", "conflict", "CASE_DATASET_VERSION_UNKNOWN")
                    case_record = None
                else:
                    check("regressionCase", "passed", "CASE_REVISION_LINKED")
            except DiagnosticError as exc:
                check(
                    "regressionCase",
                    "stale" if exc.status == 404 or exc.code == "CASE_REVISION_STALE" else "conflict",
                    exc.code,
                )

        run_id = resolution.get("runId")
        run: dict[str, Any] | None = None
        result: Mapping[str, Any] | None = None
        if not isinstance(run_id, str) or not isinstance(case_id, str) or type(case_revision) is not int:
            check("sqlRun", "unverified", "SQL_RUN_NOT_LINKED")
        else:
            try:
                run = self._run(scope, feedback_id, case_id, case_revision, run_id)
                result = next(
                    item
                    for item in run.get("results", [])
                    if isinstance(item, Mapping) and item.get("caseId") == case_id
                )
                if result.get("status") != "passed" or result.get("reasonCode") != "ASSERTIONS_PASSED":
                    check("sqlRun", "conflict", "SQL_RUN_DID_NOT_PASS")
                    result = None
                elif (
                    not isinstance(result.get("queryId"), str)
                    or not _ID.fullmatch(result["queryId"])
                    or not isinstance(result.get("coreTraceIds"), list)
                    or not result["coreTraceIds"]
                    or any(not isinstance(trace_id, str) or not _ID.fullmatch(trace_id) for trace_id in result["coreTraceIds"])
                ):
                    check("sqlRun", "conflict", "SQL_RUN_CORE_EVIDENCE_MISSING")
                    result = None
                elif case_record is None:
                    check("sqlRun", "stale", "CASE_REVISION_UNAVAILABLE")
                    result = None
                else:
                    environment = result.get("environment")
                    dataset_version = case_record.get("datasetVersion")
                    if (
                        not isinstance(environment, Mapping)
                        or environment.get("semanticVersion") != semantic_version
                        or environment.get("datasetVersion") != dataset_version
                        or not isinstance(semantic_version, str)
                    ):
                        check("sqlRun", "conflict", "SQL_RUN_VERSION_MISMATCH")
                        result = None
                    else:
                        check("sqlRun", "passed", "SQL_RUN_PASSED_SAME_VERSION")
            except DiagnosticError as exc:
                check("sqlRun", "stale" if exc.status == 404 else "conflict", exc.code)

        if result is None or case_record is None:
            check("agentEvaluation", "unverified", "SQL_VERSION_CONTEXT_UNAVAILABLE")
        else:
            environment = result.get("environment")
            semantic = environment.get("semanticVersion") if isinstance(environment, Mapping) else None
            dataset = environment.get("datasetVersion") if isinstance(environment, Mapping) else None

            def review_of(item: Mapping[str, Any]) -> Mapping[str, Any]:
                review = item.get("review")
                return review if isinstance(review, Mapping) else {}

            matching = [
                item
                for item in evaluations
                if item.get("semanticVersion") == semantic
                and item.get("datasetVersion") == dataset
                and review_of(item).get("decision") == "approved"
            ]
            passing = [
                item
                for item in matching
                if item.get("verdictAggregates", {}).get("passed")
                == item.get("verdictAggregates", {}).get("total")
                and item.get("verdictAggregates", {}).get("failed") == 0
                and item.get("verdictAggregates", {}).get("inconclusive") == 0
                and isinstance(review_of(item).get("reviewerSubjectId"), str)
                and isinstance(review_of(item).get("reviewedAt"), str)
            ]
            if passing:
                check("agentEvaluation", "passed", "REVIEWED_AGENT_EVALUATION_SAME_VERSION")
            elif matching:
                check("agentEvaluation", "conflict", "AGENT_EVALUATION_HAS_NONPASS_VERDICTS")
            elif any(
                review_of(item).get("decision") == "approved"
                for item in evaluations
            ):
                check("agentEvaluation", "conflict", "AGENT_EVALUATION_VERSION_MISMATCH")
            else:
                check("agentEvaluation", "unverified", "REVIEWED_AGENT_EVALUATION_REQUIRED")

        statuses = {item["status"] for item in checks}
        status = "verified" if statuses == {"passed"} else (
            "conflict" if "conflict" in statuses else "stale" if "stale" in statuses else "incomplete"
        )
        return {"schemaVersion": 1, "status": status, "checks": checks}


__all__ = ["FeedbackResolutionStore"]
