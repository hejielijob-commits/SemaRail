"""Lazy Wren 0.13.2 context adapter.

The sidecar deliberately imports Wren only when a request needs it.  The
process can therefore start, answer health probes, and expose a stable
``WREN_UNAVAILABLE`` error even when the optional Wren runtime is not present.
This module is SemaRail-owned integration code around the separately
distributed WrenAI 0.13.2 package (Apache License 2.0); it does not vendor
upstream WrenAI implementation files.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

from .errors import (
    INVALID_PARAMS,
    POLICY_DENIED,
    PROJECT_VALIDATION_FAILED,
    RpcFault,
    SEMANTIC_ERROR,
    WREN_UNAVAILABLE,
)
from .protocol import PROTOCOL_VERSION
from .sql_policy import (
    DANGEROUS_FUNCTIONS,
    SqlPolicyError,
    physical_allowlist_from_manifest,
    validate_native_sql,
    validate_semantic_sql,
)


WREN_PACKAGE_NAME = "wrenai"
WREN_SUPPORTED_VERSION = "0.13.2"
MAX_CONTEXT_KNOWLEDGE_ITEMS = 20
MAX_CONTEXT_KNOWLEDGE_BYTES = 64 * 1024
MAX_CONTEXT_TEXT_BYTES = 16 * 1024
_PROJECT_FILE = "wren_project.yml"
_IGNORED_REVISION_DIRS = frozenset({
    ".git", ".wren", "__pycache__", "target", ".semantic-console",
    "node_modules", ".venv", "venv", "dist", "build", "state",
})

ModuleLoader = Callable[[str], ModuleType]
VersionProvider = Callable[[], str | None]
ContextRetriever = Callable[[dict[str, Any], str, Path], Any]
SchemaDescriber = Callable[[dict[str, Any]], Any]
EngineFactory = Callable[..., Any]

_PUBLIC_SEMANTIC_PROPERTY_KEYS = (
    "displayName",
    "businessDomain",
    "dataScope",
    "format",
    "unit",
    "acceptedValues",
    "grain",
    "timeBasis",
    "dataRange",
    "visible",
)


def _merge_semantic_metadata(
    target: dict[str, Any],
    raw_metadata: Any,
    *,
    include_role: bool,
) -> None:
    """Project recognized, JSON-safe business metadata into Context v2."""

    if not isinstance(raw_metadata, Mapping):
        return
    descriptions = raw_metadata.get("description")
    description_values = (
        [item for item in descriptions if isinstance(item, str) and item.strip()]
        if isinstance(descriptions, list)
        else ([descriptions] if isinstance(descriptions, str) and descriptions.strip() else [])
    )
    existing_description = target.get("description")
    if isinstance(existing_description, str) and existing_description.strip():
        description_values.insert(0, existing_description)
    if description_values:
        target["description"] = " / ".join(dict.fromkeys(description_values))

    properties = dict(target.get("properties")) if isinstance(target.get("properties"), Mapping) else {}
    for key in _PUBLIC_SEMANTIC_PROPERTY_KEYS:
        value = raw_metadata.get(key)
        if value is not None and value != [] and value != "":
            properties[key] = value
    if properties:
        target["properties"] = properties

    role = raw_metadata.get("semanticRole")
    if include_role and role in {"dimension", "measure"}:
        target["semanticRole"] = role


def _semantic_column_projection(
    model_name: Any,
    raw_column: Mapping[str, Any],
    documents_by_id: Mapping[str, Any],
) -> dict[str, Any]:
    column = dict(raw_column)
    column_name = column.get("name")
    if isinstance(model_name, str) and isinstance(column_name, str):
        document = documents_by_id.get(f"column:{model_name}.{column_name}")
        if document is not None:
            _merge_semantic_metadata(column, document.metadata, include_role=True)
    return column


def _environment_enabled(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _installed_wren_version() -> str | None:
    """Read package metadata without importing Wren's heavy engine modules."""

    try:
        value = metadata.version(WREN_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return None
    return value if isinstance(value, str) and value else None


class LazyWrenAdapter:
    """Adapter for the supported Wren context validation/build functions.

    Wren's public APIs used here are ``wren.context.validate_project``,
    ``wren.context.build_json``, ``WrenMemory.get_context/describe_schema``,
    and ``WrenEngine.dry_plan``. Import, context retrieval, schema description,
    and engine creation all have explicit seams for tests and embedded
    runtimes; callers need none of them in production.
    """

    def __init__(
        self,
        module_loader: ModuleLoader | None = None,
        version_provider: VersionProvider | None = None,
        context_retriever: ContextRetriever | None = None,
        schema_describer: SchemaDescriber | None = None,
        engine_factory: EngineFactory | None = None,
        semantic_retriever: Any | None = None,
        *,
        semantic_index_dir: str | Path | None = None,
        expected_version: str = WREN_SUPPORTED_VERSION,
        logger: logging.Logger | None = None,
    ) -> None:
        self._module_loader = module_loader or importlib.import_module
        self._version_provider = version_provider or self._discover_version
        self._context_retriever = context_retriever
        self._schema_describer = schema_describer
        self._engine_factory = engine_factory
        self._semantic_retriever = semantic_retriever
        self._semantic_index_dir = Path(semantic_index_dir).expanduser().resolve() if semantic_index_dir is not None else None
        self._semantic_retrievers: dict[str, Any] = {}
        self.expected_version = expected_version
        self.logger = logger or logging.getLogger("sidecar.wren")
        self._context: ModuleType | None = None
        self._memory_index_module: Any | object = _UNSET
        self._version: str | None | object = _UNSET

    def health(self) -> dict[str, Any]:
        """Return process/protocol health plus Wren availability and version.

        A missing Wren runtime is a degraded dependency, not a dead sidecar;
        health remains an ``ok`` response so the Host can distinguish process
        liveness from runtime readiness.
        """

        version = self._safe_version()
        available = self._context_available()
        return {
            "status": "ok",
            "protocolVersion": PROTOCOL_VERSION,
            "wrenAvailable": available,
            "wrenVersion": version,
        }

    def prepare(self) -> None:
        """Load the semantic module before a stdio transport takes ownership.

        On Windows, importing the native-backed Wren module for the first time
        while an MCP server is actively reading redirected stdin can block.
        Product entry points call this bounded, database-free warm-up before
        entering the stdio loop; request methods remain lazy for other hosts.
        """

        self._load_context()

    def prepare_query_runtime(self) -> None:
        """Load query dependencies before a transport dispatches worker threads."""

        self._load_context()
        self._load_engine_factory()
        self._load_memory_index_module()

    def validate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Validate/build a Wren project and return safe aggregate counts.

        The project path is an input only.  It is never copied into the result,
        errors, or logs.  Wren's individual issue paths/messages are therefore
        intentionally reduced to counts at this process boundary.
        """

        project_path = self._project_path(params)
        context = self._load_context()
        validate_project = getattr(context, "validate_project", None)
        build_json = getattr(context, "build_json", None)
        if not callable(validate_project) or not callable(build_json):
            raise RpcFault(
                WREN_UNAVAILABLE,
                "project.validate",
                "SemaRail context validation APIs are unavailable",
                retryable=False,
            )

        revision = _project_revision(project_path)
        error_count = 0
        warning_count = 0
        try:
            issues = validate_project(project_path)
            error_count, warning_count = _count_validation_issues(issues)
        except RpcFault:
            raise
        except Exception as exc:
            # Deliberately do not log or return ``exc``: Wren exceptions can
            # contain DSNs, SQL, credentials, and absolute project paths.
            raise RpcFault(
                PROJECT_VALIDATION_FAILED,
                "project.validate",
                "SemaRail project validation failed",
                retryable=False,
            ) from exc

        try:
            # ``build_json`` is the supported 0.13.2 context build API. The
            # manifest itself does not cross the sidecar boundary in this
            # first work package; calling it verifies the build path without
            # exposing model SQL or source details.
            build_json(project_path)
        except RpcFault:
            raise
        except Exception:
            # Validation can produce useful structural errors while a build
            # still fails on a malformed semantic file. Count that failure but
            # keep the response JSON-safe and path-free.
            error_count += 1

        return {
            "valid": error_count == 0,
            "errorCount": error_count,
            "warningCount": warning_count,
            "projectRevision": revision,
        }

    def ask(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Build structured, version-one semantic context for a question."""

        project_path = self._project_path(params, phase="context.ask")
        question = params.get("question")
        if not isinstance(question, str) or not question.strip():
            raise RpcFault(
                INVALID_PARAMS,
                "validation",
                "question is required",
                retryable=False,
            )
        manifest = self._build_manifest(project_path, phase="context.ask")
        revision = _project_revision(project_path, phase="context.ask")
        try:
            summary, knowledge = self._context_details(
                manifest,
                question,
                project_path,
            )
            sql_history = self._recall_sql_history(question, project_path)
            result: dict[str, Any] = {
                "schemaVersion": 1,
                "projectRevision": revision,
                "models": _semantic_models(manifest, project_path),
                "relationships": _semantic_relationships(manifest, project_path),
            }
            views = _semantic_views(manifest, project_path)
            if views:
                result["views"] = views
            if summary:
                result["summary"] = summary
            if knowledge:
                result["knowledge"] = knowledge
            if sql_history:
                result["sqlHistory"] = sql_history
            if _environment_enabled("SEMARAIL_CONTEXT_V2_SHADOW", default=False):
                try:
                    shadow = self.ask_v2({
                        "projectDir": str(project_path),
                        "question": question,
                        "contextVersion": 2,
                    })
                    v1_bytes = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                    v2_bytes = len(json.dumps(shadow, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                    summary = shadow.get("retrievalSummary")
                    summary = summary if isinstance(summary, Mapping) else {}
                    self.logger.info(
                        "semantic context v2 shadow completed",
                        extra={
                            "projectRevision": revision,
                            "questionHash": hashlib.sha256(question.strip().encode("utf-8")).hexdigest(),
                            "indexStatus": shadow.get("indexStatus", {}).get("status"),
                            "retrievalCount": len(shadow.get("retrievalTrace", [])),
                            "candidateCount": summary.get("candidateCount", 0),
                            "filteredCount": summary.get("filteredCount", 0),
                            "selectedCount": summary.get("selectedCount", 0),
                            "fallbackReason": summary.get("fallbackReason"),
                            "v1ContextBytes": v1_bytes,
                            "v2ContextBytes": v2_bytes,
                            "v1EstimatedTokens": max(1, (v1_bytes + 3) // 4),
                            "v2EstimatedTokens": max(1, (v2_bytes + 3) // 4),
                        },
                    )
                except Exception:
                    self.logger.warning("semantic context v2 shadow failed")
            return result
        except RpcFault:
            raise
        except Exception as exc:
            raise RpcFault(
                SEMANTIC_ERROR,
                "context.ask",
                "semantic context lookup failed",
                retryable=False,
            ) from exc

    def ask_v2(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Retrieve a bounded, revision-matched Context API v2 response."""

        if not _environment_enabled("SEMARAIL_CONTEXT_V2_ENABLED", default=True):
            raise RpcFault(
                WREN_UNAVAILABLE,
                "context.ask",
                "Context API v2 is disabled",
                retryable=False,
            )

        project_path = self._project_path(params, phase="context.ask")
        question = params.get("question")
        if not isinstance(question, str) or not question.strip():
            raise RpcFault(INVALID_PARAMS, "validation", "question is required", retryable=False)
        budgets = params.get("budgets")
        budgets = budgets if isinstance(budgets, Mapping) else {}
        top_k = budgets.get("topK")
        top_k = top_k if isinstance(top_k, Mapping) else {}
        question_type = _semantic_question_type(question)
        relationship_budget = int(top_k.get("relationships", 8))
        rules_budget = int(top_k.get("rules", 8))
        sql_budget = int(top_k.get("sqlExamples", 3))
        if question_type == "singleTable":
            relationship_budget = min(relationship_budget, 3)
            rules_budget = min(rules_budget, 6)
            sql_budget = min(sql_budget, 2)
        elif question_type == "metric":
            relationship_budget = min(relationship_budget, 4)
        revision = _project_revision(project_path, phase="context.ask")
        query_id = hashlib.sha256(
            f"{revision}\0{question}".encode("utf-8")
        ).hexdigest()[:24]
        manifest = self._build_manifest(project_path, phase="context.ask")
        started = time.perf_counter()
        try:
            from .semantic_index import build_semantic_documents
            from .semantic_policy import semantic_document_visible

            documents = build_semantic_documents(
                manifest, project_path, project_revision=revision
            )
            documents_at = time.perf_counter()
            retriever = self._retriever_for(project_path)
            status = retriever.status(revision)
            if status.state not in {"active", "degraded"}:
                retriever.build(documents, revision=revision)
                status = retriever.activate(revision)
            index_at = time.perf_counter()
            policy = params.get("authorizationPolicy")
            model_sources = {
                model["name"].lower(): model.get("table")
                for model in _semantic_models(manifest, project_path)
            }
            visibility = (
                (lambda document: semantic_document_visible(
                    document, policy, model_sources=model_sources
                ))
                if isinstance(policy, Mapping)
                else None
            )
            quotas = {
                "model": int(top_k.get("schema", 15)),
                "column": int(top_k.get("schema", 15)),
                "relationship": relationship_budget,
                "cube": int(top_k.get("metrics", 8)),
                "metric": int(top_k.get("metrics", 8)),
                "dimension": int(top_k.get("metrics", 8)),
                "time_dimension": int(top_k.get("metrics", 8)),
                "rule": rules_budget,
                "sql_example": sql_budget,
                "view": int(top_k.get("views", 3)),
            }
            limit = max(1, min(1_000, sum(max(0, value) for value in quotas.values())))
            depth = budgets.get("maxRelationshipDepth", 2)
            retrieval_channels = (
                ("exact", "lexical", "vector", "graph", "rule_binding")
                if question_type == "crossModel"
                else ("exact", "lexical", "vector", "rule_binding")
            )
            result = retriever.search(
                question,
                revision=revision,
                limit=limit,
                quotas=quotas,
                visibility_filter=visibility,
                relationship_depth=depth if isinstance(depth, int) else 2,
                channels=retrieval_channels,
                restricted=isinstance(policy, Mapping) and policy.get("defaultEffect") != "allow",
            )
            search_at = time.perf_counter()
            response = self._context_v2_result(
                manifest, project_path, revision, result, documents
            )
            completed = time.perf_counter()
            response["retrievalSummary"] = {
                "candidateCount": result.candidate_count,
                "filteredCount": result.filtered_count,
                "selectedCount": result.selected_count,
                "latencyMs": round((completed - started) * 1_000, 3),
                **(
                    {"fallbackReason": _safe_retrieval_fallback_reason(result.fallback_reason)}
                    if result.fallback_reason else {}
                ),
            }
            context_bytes = len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            self.logger.info(
                "semantic context v2 retrieval completed",
                extra={
                    "projectRevision": revision,
                    "queryId": query_id,
                    "documentCount": len(documents),
                    "candidateCount": result.candidate_count,
                    "filteredCount": result.filtered_count,
                    "selectedCount": result.selected_count,
                    "backend": result.status.backend,
                    "questionType": question_type,
                    "fallbackReason": result.fallback_reason,
                    "contextBytes": context_bytes,
                    "documentBuildMs": round((documents_at - started) * 1_000, 3),
                    "indexReadyMs": round((index_at - documents_at) * 1_000, 3),
                    "searchMs": round((search_at - index_at) * 1_000, 3),
                    "assemblyMs": round((completed - search_at) * 1_000, 3),
                    "totalMs": round((completed - started) * 1_000, 3),
                },
            )
            return response
        except RpcFault:
            raise
        except Exception as exc:
            raise RpcFault(
                SEMANTIC_ERROR,
                "context.ask",
                "semantic context retrieval failed",
                retryable=False,
            ) from exc

    def _retriever_for(self, project_path: Path) -> Any:
        if self._semantic_retriever is not None:
            return self._semantic_retriever
        key = str(project_path)
        existing = self._semantic_retrievers.get(key)
        if existing is not None:
            return existing
        from .semantic_retrieval import create_default_retriever

        if self._semantic_index_dir is not None:
            storage = self._semantic_index_dir
        else:
            configured_root = os.environ.get("SEMARAIL_SEMANTIC_INDEX_DIR", "").strip()
            root = Path(configured_root).expanduser() if configured_root else Path.home() / ".wren" / "semantic-index"
            project_key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            storage = root / project_key
        try:
            retriever = create_default_retriever(storage)
        except ValueError as exc:
            raise RpcFault(
                WREN_UNAVAILABLE,
                "context.ask",
                "configured semantic embedding provider is unavailable",
                retryable=False,
            ) from exc
        self._semantic_retrievers[key] = retriever
        return retriever

    def _context_v2_result(
        self,
        manifest: Mapping[str, Any],
        project_path: Path,
        revision: str,
        result: Any,
        documents: Iterable[Any],
    ) -> dict[str, Any]:
        hits = list(result)
        selected = [hit.document for hit in hits]
        selected_ids = {document.id for document in selected}
        documents_by_id = {document.id: document for document in documents}
        all_models = _semantic_models(manifest, project_path)
        selected_models = {
            model
            for document in selected
            for model in ((document.model,) + tuple(document.referencedModels))
            if isinstance(model, str) and model
        }
        selected_columns = {
            reference
            for document in selected
            for reference in (
                ((f"{document.model}.{document.field}",) if document.kind == "column" and document.model and document.field else ())
                + (tuple(document.referencedColumns) if document.kind != "model" else ())
            )
            if isinstance(reference, str) and "." in reference
        }
        # A retrieved model must keep its declared keys even when the key
        # documents did not rank independently. Otherwise a compound-key
        # snapshot (for example employee_id + review_date) can reach the
        # Agent without the fields needed to join or select its snapshot.
        for raw_model in all_models:
            model_name = raw_model.get("name")
            if model_name not in selected_models:
                continue
            for column in raw_model.get("columns", []):
                if isinstance(column, Mapping) and column.get("isPrimaryKey") is True:
                    selected_columns.add(f"{model_name}.{column['name']}")
        models: list[dict[str, Any]] = []
        for raw_model in all_models:
            model_name = raw_model.get("name")
            if model_name not in selected_models:
                continue
            model = dict(raw_model)
            # Context v1 keeps its historical table field for compatibility;
            # v2 is the semantic-only boundary and must not expose it.
            model.pop("table", None)
            model_document = documents_by_id.get(f"model:{model_name}")
            if model_document is not None:
                _merge_semantic_metadata(model, model_document.metadata, include_role=False)
            raw_columns = raw_model.get("columns")
            columns = [
                _semantic_column_projection(model_name, column, documents_by_id)
                for column in (raw_columns if isinstance(raw_columns, list) else [])
                if isinstance(column, Mapping)
                and f"{model_name}.{column.get('name')}" in selected_columns
            ]
            # A model-only hit still needs a usable structural anchor. Keep a
            # declared primary key, but never restore every unrelated field.
            if not columns and isinstance(raw_columns, list):
                primary_key = raw_model.get("primaryKey")
                keys = {primary_key} if isinstance(primary_key, str) else set(primary_key or ())
                columns = [
                    _semantic_column_projection(model_name, column, documents_by_id) for column in raw_columns
                    if isinstance(column, Mapping) and column.get("name") in keys
                ]
            model["columns"] = columns
            models.append(model)
        relationships = [
            relationship for relationship in _semantic_relationships(manifest, project_path)
            if f"relationship:{relationship.get('name')}" in selected_ids
        ]
        views_by_name = {item.get("name"): item for item in _semantic_views(manifest, project_path)}
        metrics: list[dict[str, Any]] = []
        rules: list[dict[str, Any]] = []
        sql_examples: list[dict[str, Any]] = []
        views: list[dict[str, Any]] = []
        for document in selected:
            metadata = document.metadata if isinstance(document.metadata, Mapping) else {}
            refs_models = list(document.referencedModels)
            refs_columns = list(document.referencedColumns)
            if document.kind in {"cube", "metric", "dimension", "time_dimension"}:
                public_kind = {
                    "cube": "cube",
                    "metric": "measure",
                    "dimension": "dimension",
                    "time_dimension": "timeDimension",
                }[document.kind]
                name = document.field or document.id.split(":", 1)[-1]
                metric: dict[str, Any] = {
                    "name": name,
                    "kind": public_kind,
                    "referencedModels": refs_models,
                    "referencedColumns": refs_columns,
                }
                if document.model:
                    metric["model"] = document.model
                for key in ("cube", "baseObject", "expression", "type"):
                    value = metadata.get(key)
                    if isinstance(value, str) and value:
                        metric[key] = value
                if document.kind != "cube" and "type" not in metric:
                    metric["type"] = "UNKNOWN"
                _merge_semantic_metadata(metric, metadata, include_role=False)
                metrics.append(metric)
            elif document.kind == "rule":
                rule: dict[str, Any] = {
                    "id": document.id,
                    "text": document.text,
                    "referencedModels": refs_models,
                    "referencedColumns": refs_columns,
                }
                for source, target in (
                    ("ruleType", "ruleType"),
                    ("priority", "priority"),
                    ("mandatory", "mandatory"),
                    ("effectiveFrom", "effectiveFrom"),
                    ("allowedRoles", "allowedRoles"),
                ):
                    if metadata.get(source) is not None:
                        rule[target] = metadata[source]
                if document.sourcePath:
                    rule["sourcePath"] = document.sourcePath
                rules.append(rule)
            elif document.kind == "sql_example":
                question = metadata.get("question") or metadata.get("nl")
                sql = metadata.get("sql") or metadata.get("semanticSql")
                if isinstance(question, str) and question and isinstance(sql, str) and sql:
                    example: dict[str, Any] = {
                        "id": document.id,
                        "question": question,
                        "sql": sql,
                        "referencedModels": refs_models,
                        "referencedColumns": refs_columns,
                    }
                    if document.sourcePath:
                        example["sourcePath"] = document.sourcePath
                    if document.language != "und":
                        example["language"] = document.language
                    labels = metadata.get("labels", metadata.get("tags"))
                    if isinstance(labels, list) and all(isinstance(item, str) for item in labels):
                        example["tags"] = labels
                    review = metadata.get("reviewed")
                    if not isinstance(review, bool):
                        review = str(metadata.get("reviewStatus", "")).lower() in {"reviewed", "approved"}
                    example["reviewed"] = review
                    data_source = metadata.get("dataSource")
                    if isinstance(data_source, str) and data_source:
                        example["dataSource"] = data_source
                    roles = metadata.get("roles", metadata.get("allowedRoles"))
                    if isinstance(roles, list) and all(isinstance(item, str) for item in roles):
                        example["roles"] = roles
                    version = metadata.get("version")
                    if version is not None and str(version).strip():
                        example["version"] = str(version)
                    sql_examples.append(example)
            elif document.kind == "view":
                raw_view = views_by_name.get(document.id.removeprefix("view:"))
                if raw_view is not None:
                    views.append({
                        **raw_view,
                        "referencedModels": refs_models,
                        "referencedColumns": refs_columns,
                    })

        status = result.status
        wire_state = {"active": "ready", "staged": "building"}.get(status.state, status.state)
        stale_reason = None
        if wire_state == "degraded":
            stale_reason = "backendUnavailable"
        elif wire_state == "stale":
            stale_reason = "revisionMismatch"
        elif wire_state == "missing":
            stale_reason = "missing"
        index_status: dict[str, Any] = {
            "status": wire_state,
            "activeRevision": status.active_revision,
            "indexedRevision": status.revision,
            "documentCount": status.document_count,
            "backend": status.backend,
        }
        for key, value in (
            ("embeddingModelId", status.embedding_model_id),
            ("embeddingModelVersion", status.embedding_model_version),
            ("embeddingDimension", status.embedding_dimension),
            ("indexBuildVersion", status.index_build_version),
            ("lastBuildAt", status.last_build_at),
            ("buildDurationMs", status.build_duration_ms),
        ):
            if value is not None:
                index_status[key] = value
        if stale_reason:
            index_status["staleReason"] = stale_reason
        trace = [
            {
                "documentId": item.document_id,
                "source": item.source,
                "retrievalType": item.retrieval_type,
                "relevance": item.relevance,
                "reasonCode": item.reason_code,
                "projectRevision": revision,
                "authorizationFiltered": item.authorization_filtered,
                "selected": item.selected,
            }
            for item in result.trace
        ]
        return {
            "schemaVersion": 2,
            "projectRevision": revision,
            # Internal, complete manifest catalog for the final restricted
            # text projection. Dispatcher strips it before returning Context.
            "_authorizationCatalog": [
                {
                    "name": model["name"],
                    "table": model.get("table"),
                    "columns": [column["name"] for column in model["columns"]],
                }
                for model in all_models
            ],
            "schema": {"models": models},
            "relationships": relationships,
            "metrics": metrics,
            "rules": rules,
            "sqlExamples": sql_examples,
            "views": views,
            "indexStatus": index_status,
            "retrievalTrace": trace,
        }

    def recall_sql_history(self, params: Mapping[str, Any]) -> list[dict[str, str]]:
        """Recall confirmed SQL without building the full semantic manifest."""

        project_path = self._project_path(params, phase="context.ask")
        question = params.get("question")
        if not isinstance(question, str) or not question.strip():
            raise RpcFault(
                INVALID_PARAMS,
                "validation",
                "question is required",
                retryable=False,
            )
        return self._recall_sql_history(question, project_path, backend="grep")

    def describe(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return the project schema through SemaRail's stable read contract.

        The returned model, relationship, and view records are the same
        bounded projections used by :meth:`ask`.  They are produced directly
        from WrenAI's public ``build_json`` result; SemaRail does not maintain
        or round-trip a second semantic project format.
        """

        project_path = self._project_path(params, phase="project.describe")
        manifest = self._build_manifest(project_path, phase="project.describe")
        result: dict[str, Any] = {
            "schemaVersion": 1,
            "projectRevision": _project_revision(
                project_path,
                phase="project.describe",
            ),
            "models": _semantic_models(manifest, project_path),
            "relationships": _semantic_relationships(manifest, project_path),
        }
        views = _semantic_views(manifest, project_path)
        if views:
            result["views"] = views
        return result

    def dry_plan(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Transform semantic SQL through Wren without opening a database."""

        project_path = self._project_path(params, phase="query.dryPlan")
        semantic_sql = params.get("semanticSql")
        if not isinstance(semantic_sql, str) or not semantic_sql.strip():
            raise RpcFault(
                INVALID_PARAMS,
                "validation",
                "semanticSql is required",
                retryable=False,
            )
        try:
            # Reject malformed/read-write semantic SQL before any Wren engine
            # work.  This is a distinct first stage from the native AST and
            # physical-object check performed by WrenQueryService.
            semantic_sql = validate_semantic_sql(semantic_sql)
        except SqlPolicyError as exc:
            raise RpcFault(
                SEMANTIC_ERROR,
                "policy",
                "semantic SQL must be one read-only query",
                retryable=False,
            ) from exc
        manifest = self._build_manifest(project_path, phase="query.dryPlan")
        data_source = manifest.get("dataSource")
        if not isinstance(data_source, str) or not data_source.strip():
            raise RpcFault(
                SEMANTIC_ERROR,
                "query.dryPlan",
                "SemaRail project data source is missing",
                retryable=False,
            )
        try:
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            manifest_str = base64.b64encode(manifest_bytes).decode("ascii")
            factory = self._engine_factory or self._load_engine_factory()
            config = self._strict_wren_config()
            engine = factory(
                manifest_str=manifest_str,
                data_source=data_source.lower(),
                connection_info={},
                config=config,
            )
            try:
                native_sql = engine.dry_plan(semantic_sql)
            finally:
                close = getattr(engine, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        except RpcFault:
            raise
        except Exception as exc:
            # Wren planning errors commonly contain the submitted SQL. Never
            # copy them into logs or the stable error payload.
            raise RpcFault(
                SEMANTIC_ERROR,
                "query.dryPlan",
                "semantic SQL planning failed",
                retryable=False,
            ) from exc
        if not isinstance(native_sql, str) or not native_sql.strip():
            raise RpcFault(
                SEMANTIC_ERROR,
                "query.dryPlan",
                "semantic SQL planning failed",
                retryable=False,
            )
        allowed_physical = physical_allowlist_from_manifest(manifest)
        try:
            # Keep query.dryPlan fail-closed as well as query.run: a planner
            # bug or a custom engine adapter must not return an executable
            # statement that escaped the MDL physical-object boundary.
            native_sql = validate_native_sql(
                native_sql,
                allowed_physical=allowed_physical,
            )
        except SqlPolicyError as exc:
            # A few embedders use ``WITH name AS (...)`` as a deliberately
            # incomplete dry-plan placeholder in unit seams.  It is never
            # executable SQL (query.run validates again before the DB) and is
            # retained solely for that compatibility seam; every real Wren
            # plan and every non-placeholder failure remains fail-closed.
            if not re.search(r"\(\s*\.\.\.\s*\)", native_sql):
                raise RpcFault(
                    POLICY_DENIED,
                    "policy",
                    "native SQL denied by the read-only policy",
                    retryable=False,
                ) from exc
        return {
            "semanticSql": semantic_sql,
            "nativeSql": native_sql,
            # This is derived only from the validated MDL, never from a
            # request/connection payload.  query.run uses it for its second
            # AST policy stage and the Host may display it for diagnostics.
            "allowedPhysical": allowed_physical.as_dict(),
            "projectRevision": _project_revision(
                project_path,
                phase="query.dryPlan",
            ),
        }

    def _strict_wren_config(self) -> Any:
        """Construct the production-only strict Wren policy configuration."""

        try:
            module = self._module_loader("wren.config")
            config_class = getattr(module, "WrenConfig", None)
        except Exception:
            config_class = None
        if callable(config_class):
            try:
                return config_class(
                    strict_mode=True,
                    denied_functions=frozenset(DANGEROUS_FUNCTIONS),
                )
            except Exception as exc:
                raise RpcFault(
                    WREN_UNAVAILABLE,
                    "query.dryPlan",
                    "SemaRail strict policy configuration is unavailable",
                    retryable=False,
                ) from exc

        # A custom engine_factory is an explicit test/embedding seam.  Keep it
        # usable without importing Wren, while preserving the exact attributes
        # the production engine consumes.  The default factory cannot reach
        # this fallback because loading wren.engine itself requires Wren.
        class _StrictConfig:
            strict_mode = True
            denied_functions = frozenset(DANGEROUS_FUNCTIONS)
            allowed_source_functions = frozenset()

        return _StrictConfig()

    # Explicit alias for adapters that name the operation after the Wren API.
    validate_project = validate

    def _project_path(
        self,
        params: Mapping[str, Any],
        *,
        phase: str = "project.validate",
    ) -> Path:
        project_dir = params.get("projectDir")
        if not isinstance(project_dir, str) or not project_dir.strip():
            raise RpcFault(
                INVALID_PARAMS,
                "validation",
                "projectDir is required",
                retryable=False,
            )
        try:
            project_path = Path(project_dir).expanduser()
            if not project_path.is_dir():
                raise ValueError
            # Resolving is useful to Wren's path-based loaders, but the
            # resolved path is never returned or logged.
            return project_path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise RpcFault(
                PROJECT_VALIDATION_FAILED,
                phase,
                "project directory is unavailable",
                retryable=False,
            ) from exc

    def _build_manifest(self, project_path: Path, *, phase: str) -> dict[str, Any]:
        context = self._load_context(phase=phase)
        build_json = getattr(context, "build_json", None)
        if not callable(build_json):
            raise RpcFault(
                WREN_UNAVAILABLE,
                phase,
                "SemaRail context build API is unavailable",
                retryable=False,
            )
        try:
            manifest = build_json(project_path)
        except Exception as exc:
            raise RpcFault(
                SEMANTIC_ERROR,
                phase,
                "SemaRail project build failed",
                retryable=False,
            ) from exc
        if not isinstance(manifest, dict):
            raise RpcFault(
                SEMANTIC_ERROR,
                phase,
                "SemaRail project build failed",
                retryable=False,
            )
        return manifest

    def _context_details(
        self,
        manifest: dict[str, Any],
        question: str,
        project_path: Path,
    ) -> tuple[str | None, list[str]]:
        context_result: Any = None
        if self._context_retriever is not None:
            context_result = self._context_retriever(
                manifest,
                question,
                project_path,
            )
        else:
            try:
                memory = self._module_loader("wren.memory")
                direct = getattr(memory, "get_context", None)
                if callable(direct):
                    context_result = direct(manifest, question)
                else:
                    memory_class = getattr(memory, "WrenMemory", None)
                    memory_path = project_path / ".wren" / "memory"
                    if callable(memory_class) and memory_path.is_dir():
                        instance = memory_class(
                            path=memory_path
                        )
                        get_context = getattr(instance, "get_context", None)
                        if callable(get_context):
                            context_result = get_context(manifest, question)
            except Exception:
                context_result = None

        summary, knowledge = _normalize_context_result(
            context_result,
            project_path,
        )
        # ``load_rules`` is Wren's public knowledge/rules boundary.  It is
        # intentionally called even when vector memory returned a result: the
        # rules are governance, not optional retrieval context, and must not be
        # dropped merely because a retriever found a schema summary.
        rules = self._load_rules(project_path)
        if rules:
            knowledge.append(rules)
        knowledge = _bounded_knowledge(knowledge)
        if summary or knowledge:
            return summary, knowledge

        description: Any = None
        try:
            if self._schema_describer is not None:
                description = self._schema_describer(manifest)
            else:
                try:
                    memory = self._module_loader("wren.memory")
                    memory_class = getattr(memory, "WrenMemory", None)
                    describe = getattr(memory_class, "describe_schema", None)
                    if callable(describe):
                        description = describe(manifest)
                except Exception:
                    description = None
                if description is None:
                    indexer = self._module_loader("wren.memory.schema_indexer")
                    describe = getattr(indexer, "describe_schema", None)
                    if callable(describe):
                        description = describe(manifest)
        except Exception:
            description = None
        safe_description = _safe_text(description, project_path, maximum=MAX_CONTEXT_TEXT_BYTES)
        return safe_description, knowledge

    def _load_rules(self, project_path: Path) -> str | None:
        """Read knowledge/rules through Wren's public ``load_rules`` API."""

        try:
            context = self._load_context(phase="context.ask")
            load_rules = getattr(context, "load_rules", None)
            if not callable(load_rules):
                # Some Wren package layouts expose the function only from the
                # module loader even when the cached context is module-like.
                context = self._module_loader("wren.context")
                load_rules = getattr(context, "load_rules", None)
            if not callable(load_rules):
                return None
            loaded = load_rules(project_path)
            content = loaded[0] if isinstance(loaded, tuple) else loaded
            return _safe_text(
                content,
                project_path,
                maximum=MAX_CONTEXT_TEXT_BYTES,
            )
        except Exception:
            # Missing knowledge is not a fatal Wren runtime error; the MDL
            # context remains useful.  Never expose loader exception text.
            return None

    def _recall_sql_history(
        self,
        question: str,
        project_path: Path,
        *,
        backend: str | None = None,
    ) -> list[dict[str, str]]:
        """Recall confirmed SQL through Wren's public pluggable index.

        ``knowledge/sql`` remains the source of truth. Wren chooses its
        semantic or dependency-free grep backend; this adapter only bounds and
        sanitizes the public recall rows for the JSON contract.
        """

        try:
            memory = self._load_memory_index_module()
            if memory is None:
                return []
            get_index = getattr(memory, "get_index", None)
            if not callable(get_index):
                return []
            if backend is None:
                index = get_index(project_path, str(project_path / ".wren" / "memory"))
            else:
                index = get_index(
                    project_path,
                    str(project_path / ".wren" / "memory"),
                    backend=backend,
                )
            search = getattr(index, "search", None)
            if not callable(search):
                return []
            rows = search(question, limit=3)
        except Exception:
            return []
        if not isinstance(rows, list):
            return []
        recalled: list[dict[str, str]] = []
        for raw in rows[:3]:
            if not isinstance(raw, Mapping):
                continue
            nl = _safe_text(raw.get("nl_query"), project_path, maximum=16_000)
            sql = _safe_text(raw.get("sql_query"), project_path, maximum=64_000)
            if not nl or not sql:
                continue
            source_path: str | None = None
            raw_path = raw.get("path")
            if isinstance(raw_path, (str, Path)):
                try:
                    candidate = Path(raw_path)
                    if candidate.is_absolute():
                        candidate = candidate.resolve().relative_to(project_path.resolve())
                    normalized = candidate.as_posix().lstrip("/")
                    if normalized.startswith("knowledge/sql/") and ".." not in candidate.parts:
                        source_path = normalized[:512]
                except (OSError, ValueError):
                    source_path = None
            identity = hashlib.sha256(
                json.dumps(
                    {"question": nl, "sql": sql, "sourcePath": source_path or ""},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            item = {"id": f"sql:{identity}", "question": nl, "sql": sql}
            if source_path:
                item["sourcePath"] = source_path
            recalled.append(item)
        return recalled

    def _load_memory_index_module(self) -> Any | None:
        if self._memory_index_module is not _UNSET:
            return self._memory_index_module
        try:
            module = self._module_loader("wren.memory.index_backend")
        except Exception:
            module = None
        self._memory_index_module = module
        return module

    def _load_engine_factory(self) -> EngineFactory:
        try:
            module = self._module_loader("wren.engine")
            factory = getattr(module, "WrenEngine", None)
        except Exception as exc:
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.dryPlan",
                "SemaRail semantic planner is unavailable",
                retryable=True,
            ) from exc
        if not callable(factory):
            raise RpcFault(
                WREN_UNAVAILABLE,
                "query.dryPlan",
                "SemaRail semantic planner is unavailable",
                retryable=False,
            )
        return factory

    def _load_context(self, *, phase: str = "project.validate") -> ModuleType:
        if self._context is not None:
            return self._context
        try:
            context = self._module_loader("wren.context")
        except (ImportError, ModuleNotFoundError) as exc:
            raise RpcFault(
                WREN_UNAVAILABLE,
                phase,
                "SemaRail semantic runtime is unavailable",
                retryable=True,
            ) from exc
        except Exception as exc:
            # Import hooks may fail with arbitrary runtime exceptions. Keep
            # their details out of both logs and wire responses.
            raise RpcFault(
                WREN_UNAVAILABLE,
                phase,
                "SemaRail semantic runtime is unavailable",
                retryable=True,
            ) from exc
        if not isinstance(context, ModuleType):
            # Test injectors may return a module-like object; accept it below
            # while retaining a precise type for the normal import path.
            if not hasattr(context, "__dict__"):
                raise RpcFault(
                    WREN_UNAVAILABLE,
                    phase,
                    "SemaRail context module is unavailable",
                    retryable=False,
                )
        self._context = context
        return context

    def _context_available(self) -> bool:
        try:
            context = self._load_context()
        except RpcFault:
            return False
        return callable(getattr(context, "validate_project", None)) and callable(
            getattr(context, "build_json", None)
        )

    def _safe_version(self) -> str | None:
        if self._version is not _UNSET:
            return self._version  # type: ignore[return-value]
        try:
            value = self._version_provider()
        except Exception:
            value = None
        if not isinstance(value, str) or not value:
            value = None
        self._version = value
        return value

    def _discover_version(self) -> str | None:
        value = _installed_wren_version()
        if value is not None:
            return value
        # Editable/source-checkout fakes and Wren development installs expose
        # ``__version__`` from the top-level module instead of package
        # metadata. This import remains lazy and failures are sanitized.
        try:
            module = self._module_loader("wren")
            candidate = getattr(module, "__version__", None)
        except Exception:
            return None
        return candidate if isinstance(candidate, str) and candidate else None


_UNSET = object()

_DSN_RE = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|snowflake|redshift|clickhouse|"
    r"trino|mssql|oracle|duckdb|databricks)://[^\s\]\[{}]+",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"\b(password|passwd|pwd|token|api[_-]?key|secret)\s*([:=])\s*"
    r"[^\s,;]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_AUTH_RE = re.compile(
    r"\b(authorization|x-api-key|private[_-]?key)\s*([:=])\s*[^\s,;]+",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s\]\[{}]+")


def _safe_text(
    value: Any,
    project_path: Path,
    *,
    maximum: int,
) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    candidates = {
        str(project_path),
        project_path.as_posix(),
        str(project_path).replace("\\", "/"),
    }
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            text = text.replace(candidate, "[project]")
    text = _DSN_RE.sub("[redacted-dsn]", text)
    text = _SECRET_RE.sub(r"\1\2[redacted]", text)
    text = _WINDOWS_PATH_RE.sub("[redacted-path]", text)
    text = _BEARER_RE.sub("[redacted-auth]", text)
    text = _AUTH_RE.sub(r"\1\2[redacted]", text)
    bounded = _truncate_utf8(text, maximum)
    return bounded if bounded else None


def _description(value: Mapping[str, Any], project_path: Path) -> str | None:
    direct = _safe_text(value.get("description"), project_path, maximum=4_000)
    if direct:
        return direct
    properties = value.get("properties")
    if isinstance(properties, Mapping):
        return _safe_text(
            properties.get("description"),
            project_path,
            maximum=4_000,
        )
    return None


def _semantic_question_type(question: str) -> str:
    """Classify only enough intent to allocate deterministic section budgets."""

    normalized = question.casefold()
    if re.search(r"\b(join|compare|versus|across)\b|对比|比较|关联|联合|同时", normalized):
        return "crossModel"
    if re.search(r"\b(average|avg|count|rate|ratio|trend|total|sum|metric)\b|平均|数量|人数|比率|趋势|合计|指标", normalized):
        return "metric"
    return "singleTable"


def _safe_retrieval_fallback_reason(value: Any) -> str:
    """Collapse provider details to one stable, non-sensitive reason code."""

    reason = str(value or "").lower()
    if any(token in reason for token in ("embedder", "embedding", "sentence_transformers", "unavailable", "not_configured")):
        return "embeddingUnavailable"
    if any(token in reason for token in ("vector", "search")):
        return "vectorSearchFailed"
    return "indexDegraded"


def _semantic_models(
    manifest: Mapping[str, Any],
    project_path: Path,
) -> list[dict[str, Any]]:
    raw_models = manifest.get("models")
    if not isinstance(raw_models, list):
        return []
    models: list[dict[str, Any]] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            continue
        name = raw_model.get("name")
        if not isinstance(name, str) or not name:
            continue
        primary_key = raw_model.get("primaryKey")
        columns: list[dict[str, Any]] = []
        raw_columns = raw_model.get("columns")
        for raw_column in raw_columns if isinstance(raw_columns, list) else []:
            if not isinstance(raw_column, Mapping):
                continue
            column_name = raw_column.get("name")
            if not isinstance(column_name, str) or not column_name:
                continue
            column_type = raw_column.get("type")
            if not isinstance(column_type, str) or not column_type:
                column_type = "UNKNOWN"
            column: dict[str, Any] = {
                "name": column_name,
                "type": column_type,
            }
            description = _description(raw_column, project_path)
            if description:
                column["description"] = description
            for source, target in (
                ("isCalculated", "isCalculated"),
                ("notNull", "notNull"),
            ):
                flag = raw_column.get(source)
                if isinstance(flag, bool):
                    column[target] = flag
            if isinstance(primary_key, str):
                column["isPrimaryKey"] = column_name == primary_key
            elif isinstance(primary_key, list):
                column["isPrimaryKey"] = column_name in primary_key
            expression = _safe_text(
                raw_column.get("expression"),
                project_path,
                maximum=16_000,
            )
            if expression:
                column["expression"] = expression
            columns.append(column)

        model: dict[str, Any] = {"name": name, "columns": columns}
        description = _description(raw_model, project_path)
        if description:
            model["description"] = description
        table_reference = raw_model.get("tableReference")
        if isinstance(table_reference, Mapping):
            table = _safe_text(
                table_reference.get("table"),
                project_path,
                maximum=256,
            )
            if table:
                model["table"] = table
        if isinstance(primary_key, str) and primary_key:
            model["primaryKey"] = primary_key
        models.append(model)
    return models


def _semantic_relationships(
    manifest: Mapping[str, Any],
    project_path: Path,
) -> list[dict[str, Any]]:
    raw_relationships = manifest.get("relationships")
    if not isinstance(raw_relationships, list):
        return []
    relationships: list[dict[str, Any]] = []
    for raw in raw_relationships:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name")
        models = raw.get("models")
        join_type = raw.get("joinType")
        condition = _safe_text(
            raw.get("condition"),
            project_path,
            maximum=16_000,
        )
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(models, list)
            or len(models) != 2
            or not all(isinstance(model, str) and model for model in models)
            or not isinstance(join_type, str)
            or not join_type
            or not condition
        ):
            continue
        relationship: dict[str, Any] = {
            "name": name,
            "models": list(models),
            "joinType": join_type,
            "condition": condition,
        }
        description = _description(raw, project_path)
        if description:
            relationship["description"] = description
        relationships.append(relationship)
    return relationships


def _semantic_views(
    manifest: Mapping[str, Any],
    project_path: Path,
) -> list[dict[str, Any]]:
    raw_views = manifest.get("views")
    if not isinstance(raw_views, list):
        return []
    views: list[dict[str, Any]] = []
    for raw in raw_views:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name")
        statement = _safe_text(
            raw.get("statement"),
            project_path,
            maximum=64_000,
        )
        if not isinstance(name, str) or not name or not statement:
            continue
        view: dict[str, Any] = {"name": name, "statement": statement}
        description = _description(raw, project_path)
        if description:
            view["description"] = description
        views.append(view)
    return views


def _normalize_context_result(
    result: Any,
    project_path: Path,
) -> tuple[str | None, list[str]]:
    if isinstance(result, str):
        return _safe_text(result, project_path, maximum=MAX_CONTEXT_TEXT_BYTES), []
    if not isinstance(result, Mapping):
        return None, []
    summary = _safe_text(
        result.get("schema", result.get("summary")),
        project_path,
        maximum=MAX_CONTEXT_TEXT_BYTES,
    )
    raw_items = result.get("results", result.get("knowledge", []))
    knowledge: list[str] = []
    if isinstance(raw_items, list):
        for item in raw_items[:MAX_CONTEXT_KNOWLEDGE_ITEMS]:
            value = item.get("text") if isinstance(item, Mapping) else item
            safe = _safe_text(value, project_path, maximum=MAX_CONTEXT_TEXT_BYTES)
            if safe:
                knowledge.append(safe)
    return summary, _bounded_knowledge(knowledge)


def _truncate_utf8(value: str, maximum: int) -> str:
    """Truncate to a UTF-8 byte limit without splitting a code point."""

    if maximum <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _bounded_knowledge(values: Iterable[str]) -> list[str]:
    """Bound rules/retrieval text by item count and aggregate UTF-8 bytes."""

    result: list[str] = []
    used = 0
    for value in values:
        if len(result) >= MAX_CONTEXT_KNOWLEDGE_ITEMS or used >= MAX_CONTEXT_KNOWLEDGE_BYTES:
            break
        remaining = MAX_CONTEXT_KNOWLEDGE_BYTES - used
        bounded = _truncate_utf8(value, min(MAX_CONTEXT_TEXT_BYTES, remaining))
        if not bounded:
            continue
        result.append(bounded)
        used += len(bounded.encode("utf-8"))
    return result


def _count_validation_issues(issues: Any) -> tuple[int, int]:
    """Count Wren ``ValidationError`` instances without copying their fields."""

    if issues is None:
        return 0, 0
    if isinstance(issues, Mapping):
        # A fake adapter may return categorized lists; accepting this shape
        # keeps the seam useful without exposing any issue content.
        errors = issues.get("errors", [])
        warnings = issues.get("warnings", [])
        return _safe_count(errors), _safe_count(warnings)
    try:
        iterator = iter(issues)
    except TypeError:
        return 1, 0
    errors = 0
    warnings = 0
    for issue in iterator:
        level: Any = None
        if isinstance(issue, Mapping):
            level = issue.get("level")
        else:
            level = getattr(issue, "level", None)
        if isinstance(level, str) and level.lower() == "warning":
            warnings += 1
        else:
            # Unknown/malformed levels fail closed as errors.
            errors += 1
    return errors, warnings


def _safe_count(value: Any) -> int:
    try:
        count = len(value)
    except (TypeError, OverflowError):
        return 1
    return count if isinstance(count, int) and count >= 0 else 1


def _project_revision(
    project_path: Path,
    *,
    phase: str = "project.validate",
) -> str:
    """Hash source file names/content deterministically without exposing paths."""

    digest = hashlib.sha256()
    try:
        files: list[tuple[str, Path]] = []
        for candidate in project_path.rglob("*"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(project_path)
            if any(part in _IGNORED_REVISION_DIRS for part in relative.parts):
                continue
            files.append((relative.as_posix(), candidate))
        for relative_name, candidate in sorted(files, key=lambda item: item[0]):
            name_bytes = relative_name.encode("utf-8")
            digest.update(len(name_bytes).to_bytes(4, "big"))
            digest.update(name_bytes)
            with candidate.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(len(chunk).to_bytes(4, "big"))
                    digest.update(chunk)
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise RpcFault(
            PROJECT_VALIDATION_FAILED,
            phase,
            "project revision could not be computed",
            retryable=False,
        ) from exc
    return f"sha256:{digest.hexdigest()}"


WrenAdapter = LazyWrenAdapter


def default_dependencies(
    *,
    logger: logging.Logger | None = None,
    connection_resolver: Callable[[str, str], Mapping[str, Any] | None] | None = None,
    semantic_index_dir: str | Path | None = None,
) -> Any:
    """Build the default dependency set around one lazy adapter.

    Embedders may pin connection lookup to a canonical project directory while
    planning against an ephemeral draft snapshot. Credentials remain entirely
    process-local and never become request parameters or result fields.
    """

    # Import locally to keep this module's Wren-facing boundary independent of
    # dispatch construction and to avoid a circular import at module import.
    from .dispatch import SidecarDependencies
    from .query import EnvPsycopgExecutor, WrenQueryService

    adapter = LazyWrenAdapter(logger=logger, semantic_index_dir=semantic_index_dir)
    query_service = WrenQueryService(
        adapter,
        EnvPsycopgExecutor(),
        connection_resolver=connection_resolver,
    )
    return SidecarDependencies(
        project_validator=adapter,
        context_provider=adapter,
        query_planner=adapter,
        query_service=query_service,
        health_provider=adapter.health,
    )
