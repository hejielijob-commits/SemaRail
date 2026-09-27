"""Policy-safe, revisioned hybrid semantic retrieval.

The module deliberately owns the retrieval implementation instead of making
the sidecar transport aware of a particular vector database.  It provides a
small dependency-free index with an optional ``sentence-transformers``
adapter.  The latter is imported only when an embedding is requested, so the
normal sidecar installation remains usable without the optional package.

There are three useful seams in this module:

* :class:`Embedder` is the provider-neutral embedding protocol.  Tests and
  applications can inject a deterministic provider without installing a
  model runtime.
* :class:`SentenceTransformerEmbedder` is an optional multilingual adapter.
  Missing dependencies are represented as an explicit degraded status and do
  not turn into an implicit vector result.
* :class:`HybridSemanticRetriever` is an in-memory or JSON-persisted,
  revision-partitioned index.  A staged partition is never searchable until
  its active pointer is atomically published.

No raw vectors or vector distances are exposed through the public response.
Only bounded, normalized relevance and retrieval-method traces are returned.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable

from .semantic_index import (
    SEMANTIC_DOCUMENT_KINDS,
    SemanticDocument,
    SemanticIndexError,
)


JSON: TypeAlias = dict[str, Any]
VisibilityFilter: TypeAlias = Callable[[SemanticDocument], bool]
Vector: TypeAlias = tuple[float, ...]

_KIND_ORDER = {
    "model": 0,
    "column": 1,
    "relationship": 2,
    "cube": 3,
    "metric": 4,
    "dimension": 5,
    "time_dimension": 6,
    "rule": 7,
    "sql_example": 8,
    "view": 9,
}
_ASCII_TOKEN = re.compile(r"[a-z0-9]+(?:[_.-][a-z0-9]+)*", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD = re.compile(r"[\w]+", re.UNICODE)


class EmbedderUnavailable(RuntimeError):
    """Raised when an optional embedding provider cannot be loaded."""


@runtime_checkable
class Embedder(Protocol):
    """Provider-neutral embedding seam.

    ``embed`` receives a batch of texts and returns one finite numeric vector
    per text.  Implementations may expose a different native method (for
    example ``encode``); the retriever accepts those conventional aliases as
    a convenience, while this protocol remains the stable integration point.
    """

    def embed(
        self,
        texts: Sequence[str],
        *,
        batch_size: int | None = None,
    ) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True, slots=True)
class EmbedderStatus(Mapping[str, Any]):
    """JSON-safe health information for an embedding provider."""

    available: bool
    degraded: bool
    provider: str
    model: str | None = None
    device: str | None = None
    batch_size: int | None = None
    reason: str | None = None
    dimension: int | None = None

    @property
    def state(self) -> str:
        return "ready" if self.available and not self.degraded else "degraded"

    def to_dict(self) -> JSON:
        return {
            "available": self.available,
            "degraded": self.degraded,
            "provider": self.provider,
            "model": self.model,
            "device": self.device,
            "batchSize": self.batch_size,
            "reason": self.reason,
            "dimension": self.dimension,
            "state": self.state,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


class SentenceTransformerEmbedder:
    """Lazy multilingual ``sentence-transformers`` adapter.

    Importing this module never imports ``sentence_transformers``.  The
    optional package is loaded on the first call to :meth:`embed` (or an
    explicit :meth:`status` call), and an unavailable package is reported as
    ``degraded`` with a stable reason.  ``model_name``, ``device``, and
    ``batch_size`` are part of the provider configuration and therefore of a
    persisted partition's compatibility identity.
    """

    DEFAULT_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        model: str | None = None,
        device: str = "cpu",
        batch_size: int = 32,
        normalize_embeddings: bool = True,
        trust_remote_code: bool = False,
        model_revision: str = "main",
    ) -> None:
        # ``model`` is accepted as a friendly alias used by a few embedding
        # wrappers.  A blank model is rejected before any optional import.
        selected_model = model_name if model is None else model
        if not isinstance(selected_model, str) or not selected_model.strip():
            raise ValueError("model_name must be a non-empty string")
        if not isinstance(device, str) or not device.strip():
            raise ValueError("device must be a non-empty string")
        if not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self.model_name = selected_model.strip()
        self.device = device.strip()
        self.batch_size = batch_size
        self.normalize_embeddings = bool(normalize_embeddings)
        self.trust_remote_code = bool(trust_remote_code)
        if not isinstance(model_revision, str) or not model_revision.strip():
            raise ValueError("model_revision must be a non-empty string")
        self.model_revision = model_revision.strip()
        self._model: Any = None
        self._load_attempted = False
        self._degraded_reason: str | None = None
        self._dimension: int | None = None

    @property
    def config(self) -> JSON:
        return {
            "provider": "sentence-transformers",
            "model": self.model_name,
            "modelRevision": self.model_revision,
            "device": self.device,
            "batchSize": self.batch_size,
            "normalizeEmbeddings": self.normalize_embeddings,
            "trustRemoteCode": self.trust_remote_code,
        }

    @property
    def degraded(self) -> bool:
        return self._load_attempted and self._model is None

    @property
    def available(self) -> bool:
        return self._ensure_model() is not None

    @property
    def degraded_reason(self) -> str | None:
        if not self._load_attempted:
            return None
        return self._degraded_reason

    @property
    def dimension(self) -> int | None:
        return self._dimension

    def _ensure_model(self) -> Any | None:
        if self._load_attempted:
            return self._model
        self._load_attempted = True
        try:
            # Deliberately delayed: this import is the optional dependency
            # boundary and must not run during sidecar startup.
            from sentence_transformers import SentenceTransformer  # type: ignore

            kwargs: dict[str, Any] = {"device": self.device}
            kwargs["revision"] = self.model_revision
            if self.trust_remote_code:
                kwargs["trust_remote_code"] = True
            self._model = SentenceTransformer(self.model_name, **kwargs)
            dimension = getattr(self._model, "get_embedding_dimension", None)
            if not callable(dimension):
                dimension = getattr(self._model, "get_sentence_embedding_dimension", None)
            if callable(dimension):
                raw_dimension = dimension()
                if isinstance(raw_dimension, int) and raw_dimension > 0:
                    self._dimension = raw_dimension
        except Exception as exc:  # optional runtime failures are degraded state
            self._model = None
            self._degraded_reason = f"sentence_transformers_unavailable:{type(exc).__name__}"
        return self._model

    def status(self) -> EmbedderStatus:
        model = self._ensure_model()
        return EmbedderStatus(
            available=model is not None,
            degraded=model is None,
            provider="sentence-transformers",
            model=self.model_name,
            device=self.device,
            batch_size=self.batch_size,
            reason=self._degraded_reason,
            dimension=self._dimension,
        )

    def embed(
        self,
        texts: Sequence[str],
        *,
        batch_size: int | None = None,
    ) -> Sequence[Sequence[float]]:
        model = self._ensure_model()
        if model is None:
            raise EmbedderUnavailable(self._degraded_reason or "sentence_transformers_unavailable")
        values = list(texts)
        if not values:
            return []
        effective_batch_size = self.batch_size if batch_size is None else batch_size
        if not isinstance(effective_batch_size, int) or effective_batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        try:
            encoded = model.encode(
                values,
                batch_size=effective_batch_size,
                convert_to_numpy=False,
                normalize_embeddings=self.normalize_embeddings,
                show_progress_bar=False,
            )
        except TypeError:
            # Small test doubles and older sentence-transformers versions may
            # not accept every keyword, while preserving the same semantics.
            encoded = model.encode(
                values,
                batch_size=effective_batch_size,
                show_progress_bar=False,
            )
        rows = _coerce_vectors(encoded, expected_count=len(values))
        if rows:
            self._dimension = len(rows[0])
        return rows

    # Familiar aliases make the adapter usable by generic provider code.
    encode = embed
    embed_documents = embed


@dataclass(frozen=True, slots=True)
class RetrievalIndexStatus(Mapping[str, Any]):
    """Safe status for a staged, active, stale, or degraded revision."""

    state: str
    backend: str
    revision: str | None
    active_revision: str | None
    built_revisions: tuple[str, ...] = ()
    document_count: int = 0
    stale_reason: str | None = None
    degraded_reason: str | None = None
    vector_available: bool = False
    embedding_model_id: str | None = None
    embedding_model_version: str | None = None
    embedding_dimension: int | None = None
    index_build_version: int = 1
    last_build_at: str | None = None
    build_duration_ms: float | None = None

    @property
    def degraded(self) -> bool:
        return self.state == "degraded" or bool(self.degraded_reason)

    @property
    def ready(self) -> bool:
        return self.state in {"active", "degraded"}

    @property
    def status(self) -> str:
        if self.state == "active":
            return "ready"
        if self.state == "degraded":
            return "degraded"
        if self.state == "stale":
            return "stale"
        if self.state == "missing":
            return "missing"
        if self.state == "staged":
            return "building"
        return self.state

    @property
    def indexStatus(self) -> str:
        return self.state

    @property
    def activeRevision(self) -> str | None:
        return self.active_revision

    @property
    def builtRevisions(self) -> tuple[str, ...]:
        return self.built_revisions

    @property
    def documentCount(self) -> int:
        return self.document_count

    @property
    def staleReason(self) -> str | None:
        return self.stale_reason

    def to_dict(self) -> JSON:
        return {
            "state": self.state,
            "status": self.status,
            "indexStatus": self.state,
            "backend": self.backend,
            "revision": self.revision,
            "activeRevision": self.active_revision,
            "indexedRevision": self.revision,
            "builtRevisions": list(self.built_revisions),
            "documentCount": self.document_count,
            "staleReason": self.stale_reason,
            "degradedReason": self.degraded_reason,
            "vectorAvailable": self.vector_available,
            "embeddingModelId": self.embedding_model_id,
            "embeddingModelVersion": self.embedding_model_version,
            "embeddingDimension": self.embedding_dimension,
            "indexBuildVersion": self.index_build_version,
            "lastBuildAt": self.last_build_at,
            "buildDurationMs": self.build_duration_ms,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True, slots=True)
class RetrievalTrace(Mapping[str, Any]):
    """One bounded retrieval explanation for a visible candidate."""

    document_id: str
    source: str
    retrieval_type: str
    relevance: float
    reason_code: str
    project_revision: str
    authorization_filtered: bool = False
    selected: bool = True

    @property
    def documentId(self) -> str:
        return self.document_id

    @property
    def retrievalType(self) -> str:
        return self.retrieval_type

    @property
    def reasonCode(self) -> str:
        return self.reason_code

    @property
    def projectRevision(self) -> str:
        return self.project_revision

    @property
    def authorizationFiltered(self) -> bool:
        return self.authorization_filtered

    def to_dict(self) -> JSON:
        # ``documentId`` is intentionally included for callers that need to
        # join a trace to a result.  Transport adapters may project the
        # contract-safe subset without exposing identifiers from a denied doc.
        return {
            "documentId": self.document_id,
            "source": self.source,
            "retrievalType": self.retrieval_type,
            "relevance": max(0.0, min(1.0, float(self.relevance))),
            "reasonCode": self.reason_code,
            "projectRevision": self.project_revision,
            "authorizationFiltered": self.authorization_filtered,
            "selected": self.selected,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True, slots=True)
class HybridSearchHit(Mapping[str, Any]):
    """One fused, policy-eligible result."""

    document: SemanticDocument
    score: float
    rank: int
    match_type: str
    reason: str
    retrieval_types: tuple[str, ...] = ()

    @property
    def matchType(self) -> str:
        return self.match_type

    @property
    def retrievalTypes(self) -> tuple[str, ...]:
        return self.retrieval_types

    def to_dict(self) -> JSON:
        return {
            "document": self.document.to_dict(),
            "score": max(0.0, min(1.0, float(self.score))),
            "rank": self.rank,
            "matchType": self.match_type,
            "reason": self.reason,
            "retrievalTypes": list(self.retrieval_types),
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True, slots=True)
class HybridSearchResponse(Sequence[HybridSearchHit], Mapping[str, Any]):
    """Sequence-compatible result with status and auditable traces."""

    hits: tuple[HybridSearchHit, ...]
    index_status: RetrievalIndexStatus
    query: str
    revision: str | None
    backend: str
    traces: tuple[RetrievalTrace, ...] = ()
    fallback_reason: str | None = None
    candidate_count: int = 0
    filtered_count: int = 0
    selected_count: int = 0

    @property
    def results(self) -> tuple[HybridSearchHit, ...]:
        return self.hits

    @property
    def documents(self) -> tuple[SemanticDocument, ...]:
        return tuple(hit.document for hit in self.hits)

    @property
    def status(self) -> RetrievalIndexStatus:
        return self.index_status

    @property
    def trace(self) -> tuple[RetrievalTrace, ...]:
        return self.traces

    @property
    def retrieval_trace(self) -> tuple[RetrievalTrace, ...]:
        return self.traces

    @property
    def indexStatus(self) -> RetrievalIndexStatus:
        return self.index_status

    @property
    def fallbackReason(self) -> str | None:
        return self.fallback_reason

    def __len__(self) -> int:
        return len(self.hits)

    def __iter__(self) -> Iterator[HybridSearchHit]:
        return iter(self.hits)

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            return self.to_dict()[key]
        return self.hits[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def to_dict(self) -> JSON:
        traces = [trace.to_dict() for trace in self.traces]
        results = [hit.to_dict() for hit in self.hits]
        return {
            "results": results,
            "hits": results,
            "indexStatus": self.index_status.to_dict(),
            "query": self.query,
            "revision": self.revision,
            "backend": self.backend,
            "retrievalTrace": traces,
            "trace": traces,
            "fallbackReason": self.fallback_reason,
            "candidateCount": self.candidate_count,
            "filteredCount": self.filtered_count,
            "selectedCount": self.selected_count,
        }

    as_dict = to_dict


@dataclass(frozen=True, slots=True)
class _Partition:
    revision: str
    documents: tuple[SemanticDocument, ...]
    vectors: Mapping[str, Vector]
    embedding_config: JSON
    embedding_available: bool
    degraded_reason: str | None = None
    built_at: str | None = None
    build_duration_ms: float | None = None
    embedding_dimension: int | None = None


def _partition_status_metadata(partition: _Partition) -> dict[str, Any]:
    config = partition.embedding_config if isinstance(partition.embedding_config, Mapping) else {}
    return {
        "embedding_model_id": str(config.get("model")) if config.get("model") else None,
        "embedding_model_version": str(config.get("modelRevision")) if config.get("modelRevision") else None,
        "embedding_dimension": partition.embedding_dimension,
        "index_build_version": 1,
        "last_build_at": partition.built_at,
        "build_duration_ms": partition.build_duration_ms,
    }


@dataclass(frozen=True, slots=True)
class _Candidate:
    document: SemanticDocument
    fused_score: float
    exact_rank: int | None = None
    lexical_rank: int | None = None
    vector_rank: int | None = None
    graph_rank: int | None = None
    rule_binding_rank: int | None = None
    graph_depth: int | None = None
    lexical_score: float = 0.0
    vector_score: float = 0.0

    @property
    def retrieval_types(self) -> tuple[str, ...]:
        values: list[str] = []
        if self.exact_rank is not None:
            values.append("exact")
        if self.lexical_rank is not None:
            values.append("lexical")
        if self.vector_rank is not None:
            values.append("vector")
        if self.graph_rank is not None:
            values.append("graph")
        if self.rule_binding_rank is not None:
            values.append("ruleBinding")
        return tuple(values)

    @property
    def primary_type(self) -> str:
        if self.exact_rank is not None:
            return "exact"
        if self.rule_binding_rank is not None:
            return "ruleBinding"
        if self.vector_rank is not None and self.lexical_rank is None:
            return "vector"
        if self.lexical_rank is not None:
            return "lexical"
        if self.vector_rank is not None:
            return "vector"
        return "graph"


def _normalise(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _unicode_tokens(value: str) -> tuple[str, ...]:
    """Tokenize identifiers, Unicode words, and CJK characters deterministically."""

    normal = _normalise(value)
    tokens: list[str] = []
    occupied: list[tuple[int, int]] = []
    for match in _ASCII_TOKEN.finditer(normal):
        token = match.group(0)
        tokens.append(token)
        # Technical identifiers remain exact tokens while their components
        # make natural-language forms such as "job title" match job_title.
        tokens.extend(
            part for part in re.split(r"[._-]+", token)
            if part and part != token
        )
        occupied.append(match.span())
    for match in _WORD.finditer(normal):
        if any(match.start() >= start and match.end() <= end for start, end in occupied):
            continue
        token = match.group(0).strip("_-")
        if token:
            tokens.append(token)
    for char in normal:
        if _CJK.match(char):
            tokens.append(char)
    return tuple(tokens)


def _safe_vector(value: Any) -> Vector | None:
    if isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not vector or any(not math.isfinite(item) for item in vector):
        return None
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 0.0 or not math.isfinite(norm):
        return None
    return tuple(item / norm for item in vector)


def _coerce_vectors(value: Any, *, expected_count: int) -> list[Vector]:
    if expected_count == 0:
        return []
    # A single-vector result for one text is common in small provider stubs.
    if expected_count == 1:
        single = _safe_vector(value)
        if single is not None:
            return [single]
    if isinstance(value, Mapping):
        rows = list(value.values())
    else:
        try:
            rows = list(value)
        except TypeError:
            return []
    if len(rows) != expected_count:
        return []
    vectors = [_safe_vector(row) for row in rows]
    if any(row is None for row in vectors):
        return []
    concrete = [row for row in vectors if row is not None]
    if len({len(row) for row in concrete}) != 1:
        return []
    return concrete


def _document_text(document: SemanticDocument) -> str:
    # Curated aliases are semantic metadata, not prompt text. They enrich both
    # embedding and lexical recall without leaking implementation-only helper
    # text into the Context response.
    aliases = document.metadata.get("aliases") if isinstance(document.metadata, Mapping) else None
    alias_text = " ".join(str(item) for item in aliases) if isinstance(aliases, list) else ""
    return " ".join(part for part in (document.text or document.id, alias_text) if part)


def _document_tokens(document: SemanticDocument) -> tuple[str, ...]:
    values = [document.id, document.kind, document.model or "", document.field or "", _document_text(document)]
    values.extend(document.referencedModels)
    values.extend(document.referencedColumns)
    return _unicode_tokens(" ".join(values))


def _config_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _config_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_config_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _embedder_config(embedder: Any | None) -> JSON:
    if embedder is None:
        return {"provider": "none"}
    config = getattr(embedder, "config", None)
    if callable(config):
        config = config()
    if isinstance(config, Mapping):
        return _config_value(config)
    result: JSON = {
        "provider": f"{type(embedder).__module__}.{type(embedder).__qualname__}"
    }
    for name in ("model_name", "model", "device", "batch_size", "dimension"):
        if hasattr(embedder, name):
            result[name] = _config_value(getattr(embedder, name))
    return result


def _provider_status(embedder: Any | None) -> EmbedderStatus:
    if embedder is None:
        return EmbedderStatus(
            available=False,
            degraded=True,
            provider="none",
            reason="embedder_not_configured",
        )
    status = getattr(embedder, "status", None)
    try:
        value = status() if callable(status) else None
    except Exception as exc:
        return EmbedderStatus(
            available=False,
            degraded=True,
            provider=type(embedder).__name__,
            reason=f"embedder_status_failed:{type(exc).__name__}",
        )
    if isinstance(value, EmbedderStatus):
        return value
    if isinstance(value, Mapping):
        return EmbedderStatus(
            available=bool(value.get("available", not bool(value.get("degraded", False)))),
            degraded=bool(value.get("degraded", False)),
            provider=str(value.get("provider", type(embedder).__name__)),
            model=value.get("model") if isinstance(value.get("model"), str) else None,
            device=value.get("device") if isinstance(value.get("device"), str) else None,
            batch_size=value.get("batchSize") if isinstance(value.get("batchSize"), int) else None,
            reason=value.get("reason") if isinstance(value.get("reason"), str) else None,
            dimension=value.get("dimension") if isinstance(value.get("dimension"), int) else None,
        )
    # A deterministic fake generally has no status method; it is considered
    # available until an embedding call proves otherwise.
    dimension = getattr(embedder, "dimension", None)
    return EmbedderStatus(
        True,
        False,
        type(embedder).__name__,
        dimension=dimension if isinstance(dimension, int) and dimension > 0 else None,
    )


def _embed_many(embedder: Any, texts: Sequence[str], *, batch_size: int | None = None) -> list[Vector]:
    if not texts:
        return []
    method: Callable[..., Any] | None = None
    method_name: str | None = None
    for name in ("embed", "embed_documents", "encode", "embed_query"):
        candidate = getattr(embedder, name, None)
        if callable(candidate):
            method = candidate
            method_name = name
            break
    if method is None:
        raise EmbedderUnavailable("embedder_missing_embed_method")
    try:
        if method_name == "embed_query" and len(texts) > 1:
            rows = []
            for text in texts:
                rows.append(method(text))
            raw = rows
            vectors = _coerce_vectors(raw, expected_count=len(texts))
            if len(vectors) != len(texts):
                raise EmbedderUnavailable("embedder_returned_invalid_vectors")
            return vectors
        if batch_size is not None:
            try:
                raw = method(texts, batch_size=batch_size)
            except TypeError:
                raw = method(texts)
        else:
            raw = method(texts)
    except EmbedderUnavailable:
        raise
    except Exception as exc:
        raise EmbedderUnavailable(f"embedder_failed:{type(exc).__name__}") from exc
    vectors = _coerce_vectors(raw, expected_count=len(texts))
    # A tiny provider often implements ``embed(text: str)`` rather than the
    # batch protocol.  Keep the stable batch seam, but make one-text queries
    # interoperable with that common shape without hiding provider failures.
    if len(vectors) != len(texts) and len(texts) == 1:
        try:
            raw = method(texts[0])
            vectors = _coerce_vectors(raw, expected_count=1)
        except Exception as exc:
            raise EmbedderUnavailable(f"embedder_returned_invalid_vectors:{type(exc).__name__}") from exc
    if len(vectors) != len(texts):
        raise EmbedderUnavailable("embedder_returned_invalid_vectors")
    return vectors


def _revision_key(revision: str) -> str:
    return hashlib.sha256(revision.encode("utf-8")).hexdigest()[:32]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


class HybridSemanticRetriever:
    """Revisioned exact/BM25/vector/RRF retrieval with graph expansion.

    ``storage_path`` is optional.  When supplied it is a directory containing
    JSON revision partitions and an ``active.json`` pointer.  Builds first
    write a complete staged partition; :meth:`activate` then atomically
    replaces the pointer with ``os.replace``.  In-memory operation follows the
    exact same staged/active lifecycle.
    """

    backend = "hybrid"

    def __init__(
        self,
        embedder: Embedder | Any | None = None,
        storage_path: str | Path | None = None,
        *,
        index_dir: str | Path | None = None,
        storage_dir: str | Path | None = None,
        persist_path: str | Path | None = None,
        path: str | Path | None = None,
        rrf_k: int = 60,
        batch_size: int | None = None,
        graph_depth: int = 1,
        vector_top_k: int | None = None,
        lexical_top_k: int | None = None,
    ) -> None:
        if not isinstance(rrf_k, int) or rrf_k < 1:
            raise ValueError("rrf_k must be a positive integer")
        if not isinstance(graph_depth, int) or graph_depth < 0:
            raise ValueError("graph_depth must be non-negative")
        if batch_size is not None and (not isinstance(batch_size, int) or batch_size < 1):
            raise ValueError("batch_size must be a positive integer")
        selected_path = storage_path if storage_path is not None else index_dir
        if selected_path is None:
            selected_path = storage_dir
        if selected_path is None:
            selected_path = persist_path
        if selected_path is None:
            selected_path = path
        self.storage_path = Path(selected_path).expanduser().resolve() if selected_path is not None else None
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.batch_size = batch_size
        self.graph_depth = graph_depth
        self.vector_top_k = vector_top_k
        self.lexical_top_k = lexical_top_k
        self._revisions: dict[str, _Partition] = {}
        self._active_revision: str | None = None
        self._active_partition_file: str | None = None
        self._pointer_error: str | None = None
        if self.storage_path is not None:
            self._load_storage()

    @property
    def active_revision(self) -> str | None:
        return self._active_revision

    @property
    def embedder_status(self) -> EmbedderStatus:
        return _provider_status(self.embedder)

    def status(self, revision: str | None = None) -> RetrievalIndexStatus:
        requested = _clean_revision(revision) or self._active_revision
        active = self._active_revision
        built = tuple(sorted(self._revisions))
        if requested is None:
            return RetrievalIndexStatus(
                "stale" if self._pointer_error else "missing",
                self._backend_for(None),
                None,
                active,
                built,
                0,
                self._pointer_error or "no_revision",
            )
        partition = self._revisions.get(requested)
        if partition is None:
            if active is None:
                reason = self._pointer_error or "revision_not_built"
                return RetrievalIndexStatus("missing", self._backend_for(None), requested, None, built, 0, reason)
            return RetrievalIndexStatus(
                "stale",
                self._backend_for(None),
                requested,
                active,
                built,
                0,
                self._pointer_error or "revision_mismatch",
            )
        if requested == active and self._pointer_error:
            return RetrievalIndexStatus(
                "stale",
                self._backend_for(partition),
                requested,
                active,
                built,
                0,
                self._pointer_error,
                partition.degraded_reason,
                partition.embedding_available,
                **_partition_status_metadata(partition),
            )
        compatible, compatibility_reason = self._partition_compatible(partition)
        if active != requested:
            return RetrievalIndexStatus(
                "staged",
                self._backend_for(partition),
                requested,
                active,
                built,
                len(partition.documents),
                "revision_not_active",
                partition.degraded_reason,
                partition.embedding_available,
                **_partition_status_metadata(partition),
            )
        if not compatible:
            return RetrievalIndexStatus(
                "stale",
                self._backend_for(partition),
                requested,
                active,
                built,
                0,
                compatibility_reason,
                partition.degraded_reason,
                partition.embedding_available,
                **_partition_status_metadata(partition),
            )
        if not partition.embedding_available:
            return RetrievalIndexStatus(
                "degraded",
                "lexical",
                requested,
                active,
                built,
                len(partition.documents),
                "backend_unavailable",
                partition.degraded_reason or "embedder_unavailable",
                False,
                **_partition_status_metadata(partition),
            )
        return RetrievalIndexStatus(
            "active",
            "hybrid",
            requested,
            active,
            built,
            len(partition.documents),
            None,
            None,
            True,
            **_partition_status_metadata(partition),
        )

    def build(
        self,
        documents: Iterable[SemanticDocument | Mapping[str, Any]],
        revision: str | None = None,
    ) -> RetrievalIndexStatus:
        build_started = time.perf_counter()
        items = tuple(
            item if isinstance(item, SemanticDocument) else SemanticDocument.from_mapping(item)
            for item in documents
        )
        if revision is None:
            revisions = {item.projectRevision for item in items}
            if len(revisions) != 1:
                raise SemanticIndexError("build requires one project revision")
            revision = next(iter(revisions), None)
        revision = _clean_revision(revision)
        if revision is None:
            raise SemanticIndexError("build requires a non-empty project revision")
        if any(item.projectRevision != revision for item in items):
            raise SemanticIndexError("document projectRevision does not match build revision")
        if len({item.id for item in items}) != len(items):
            raise SemanticIndexError("duplicate semantic document id in revision")
        ordered = tuple(sorted(items, key=lambda item: (_KIND_ORDER.get(item.kind, 99), item.id)))

        embedding_config = _embedder_config(self.embedder)
        vectors: dict[str, Vector] = {}
        embedding_available = False
        degraded_reason: str | None = None
        if self.embedder is None:
            degraded_reason = "embedder_not_configured"
        elif ordered:
            try:
                # Prefer the same revision during an explicit rebuild, then
                # the active revision during normal publish. Stable IDs plus
                # revision-independent content hashes make reuse exact: a
                # deleted record is omitted and only new/changed records are
                # sent to the embedding provider.
                base = self._revisions.get(revision)
                if base is None and self._active_revision is not None:
                    base = self._revisions.get(self._active_revision)
                previous_documents = {
                    item.id: item for item in base.documents
                } if (
                    base is not None
                    and base.embedding_available
                    and _json_safe(base.embedding_config) == _json_safe(embedding_config)
                ) else {}
                pending: list[SemanticDocument] = []
                for item in ordered:
                    previous = previous_documents.get(item.id)
                    previous_vector = base.vectors.get(item.id) if base is not None else None
                    if (
                        previous is not None
                        and previous.contentHash == item.contentHash
                        and previous_vector is not None
                    ):
                        vectors[item.id] = previous_vector
                    else:
                        pending.append(item)
                rows = _embed_many(
                    self.embedder,
                    [_document_text(item) for item in pending],
                    batch_size=self.batch_size,
                )
                vectors.update({item.id: row for item, row in zip(pending, rows)})
                if len({len(vector) for vector in vectors.values()}) > 1:
                    raise EmbedderUnavailable("embedding_dimension_mismatch")
                embedding_available = bool(vectors) or not ordered
            except EmbedderUnavailable as exc:
                # A partially refreshed vector partition is never searchable.
                vectors = {}
                degraded_reason = str(exc)
        else:
            # An empty partition can still be a valid staged revision.  It is
            # vector-capable if its provider is configured; no model call is
            # needed to establish that fact.
            provider = _provider_status(self.embedder)
            embedding_available = provider.available and not provider.degraded
            degraded_reason = provider.reason

        partition = _Partition(
            revision=revision,
            documents=ordered,
            vectors=vectors,
            embedding_config=embedding_config,
            embedding_available=embedding_available,
            degraded_reason=degraded_reason,
            built_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            build_duration_ms=round((time.perf_counter() - build_started) * 1_000, 3),
            embedding_dimension=(len(next(iter(vectors.values()))) if vectors else _provider_status(self.embedder).dimension),
        )
        if self.storage_path is not None:
            self._persist_partition(partition)
        self._revisions[revision] = partition
        return self.status(revision)

    def activate(self, revision: str) -> RetrievalIndexStatus:
        selected = _clean_revision(revision)
        if selected is None or selected not in self._revisions:
            raise SemanticIndexError("cannot activate an unbuilt semantic revision")
        partition = self._revisions[selected]
        compatible, reason = self._partition_compatible(partition)
        if not compatible:
            raise SemanticIndexError(f"cannot activate incompatible semantic revision: {reason}")
        if self.storage_path is not None:
            self._write_active_pointer(selected)
        self._active_revision = selected
        self._active_partition_file = self._partition_filename(selected)
        self._pointer_error = None
        return self.status(selected)

    def remove_revision(self, revision: str) -> bool:
        selected = _clean_revision(revision)
        if selected is None:
            return False
        removed = self._revisions.pop(selected, None) is not None
        if self._active_revision == selected:
            self._active_revision = None
            self._active_partition_file = None
            if self.storage_path is not None:
                self._remove_active_pointer()
        if self.storage_path is not None:
            partition_path = self.storage_path / self._partition_filename(selected)
            staged_path = self.storage_path / self._staged_partition_filename(selected)
            for candidate in (partition_path, staged_path):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    # The in-memory removal still has deterministic semantics;
                    # callers can inspect storage separately if cleanup fails.
                    pass
        return removed

    def search(
        self,
        query: str,
        revision: str | None = None,
        limit: int = 10,
        kinds: Iterable[str] | None = None,
        quotas: Mapping[str, int] | None = None,
        type_quotas: Mapping[str, int] | None = None,
        kind_quotas: Mapping[str, int] | None = None,
        visibility_filter: VisibilityFilter | None = None,
        *,
        relationship_depth: int | None = None,
        graph_depth: int | None = None,
        trace: bool = True,
        channels: Iterable[str] | None = None,
        restricted: bool = False,
        rerank: bool = False,
    ) -> HybridSearchResponse:
        if not isinstance(limit, int) or limit < 0:
            raise SemanticIndexError("search limit must be non-negative")
        if not isinstance(rerank, bool):
            raise SemanticIndexError("rerank must be a boolean")
        query_text = query.strip() if isinstance(query, str) else ""
        status = self.status(revision)
        requested = status.revision
        searchable = status.state in {"active", "degraded"}
        if not searchable:
            return HybridSearchResponse((), status, query_text, requested, status.backend, (), status.stale_reason)
        if not query_text or limit == 0:
            return HybridSearchResponse((), status, query_text, requested, status.backend, (), None)
        partition = self._revisions.get(requested or "")
        if partition is None:
            # This should only be possible after an external storage change.
            stale = RetrievalIndexStatus(
                "stale", status.backend, requested, status.active_revision,
                status.built_revisions, 0, "revision_mismatch",
            )
            return HybridSearchResponse((), stale, query_text, requested, status.backend, (), "revision_mismatch")

        selected_kinds = _selected_kinds(kinds)
        enabled_channels = (
            {"exact", "lexical", "vector", "graph", "rule_binding"}
            if channels is None
            else {str(channel).strip().lower() for channel in channels}
        )
        unsupported_channels = enabled_channels - {"exact", "lexical", "vector", "graph", "rule_binding"}
        if unsupported_channels or not enabled_channels:
            raise SemanticIndexError("search channels must select exact, lexical, vector, graph, or rule_binding")
        quota_map = _quotas(quotas, type_quotas, kind_quotas)
        if selected_kinds and not selected_kinds.issubset(SEMANTIC_DOCUMENT_KINDS):
            raise SemanticIndexError("search contains an unsupported document kind")
        eligible_count = sum(
            1
            for document in partition.documents
            if not selected_kinds or document.kind in selected_kinds
        )
        visible = self._visible_documents(partition, selected_kinds, visibility_filter)
        filtered_count = eligible_count - len(visible)
        if not visible:
            return HybridSearchResponse(
                (), status, query_text, requested, status.backend, (), None,
                0, filtered_count, 0,
            )

        by_id = {item.id: item for item in visible}
        exact = self._exact_rank(query_text, visible) if "exact" in enabled_channels else []
        lexical_scores = self._bm25(query_text, visible) if "lexical" in enabled_channels else {}
        lexical = _rank_scores(lexical_scores, self.lexical_top_k or max(limit * 8, 64))
        vector_scores: dict[str, float] = {}
        vector_reason: str | None = None
        if "vector" in enabled_channels and partition.embedding_available and partition.vectors and self.embedder is not None:
            try:
                query_vectors = _embed_many(self.embedder, [query_text], batch_size=self.batch_size)
                query_vector = query_vectors[0]
                for item in visible:
                    vector = partition.vectors.get(item.id)
                    if vector is None or len(vector) != len(query_vector):
                        continue
                    vector_scores[item.id] = _cosine(query_vector, vector)
            except EmbedderUnavailable as exc:
                vector_reason = str(exc)
        vector = _rank_scores(vector_scores, self.vector_top_k or max(limit * 8, 64))
        raw_rule_binding_scores = (
            self._rule_binding_scores(query_text, visible, restricted=restricted)
            if "rule_binding" in enabled_channels
            else {}
        )
        rule_binding_seeds = _rank_scores(
            raw_rule_binding_scores,
            max(limit * 2, 16),
        )
        bound_relationships = self._rule_relationship_scores(
            rule_binding_seeds,
            visible,
        )
        bound_columns = self._rule_column_scores(rule_binding_seeds, visible)
        rule_binding = rule_binding_seeds

        # The graph is expanded from strong lexical/vector/exact seeds before
        # quotas.  It never sees a filtered document because ``visible`` is
        # the only input used to build the adjacency map.
        depth_limit = self.graph_depth if relationship_depth is None and graph_depth is None else (
            relationship_depth if relationship_depth is not None else graph_depth
        )
        if not isinstance(depth_limit, int) or depth_limit < 0:
            raise SemanticIndexError("relationship depth must be a non-negative integer")
        seed_limit = max(4, min(8, limit))
        graph_scores, graph_depths = (
            self._graph_expand(
                visible,
                tuple(item_id for item_id, _ in _top_items(exact, seed_limit))
                + tuple(item_id for item_id, _ in _top_items(lexical, seed_limit))
                + tuple(item_id for item_id, _ in _top_items(vector, seed_limit))
                + tuple(item_id for item_id, _ in _top_items(rule_binding_seeds, seed_limit)),
                depth_limit,
            )
            if "graph" in enabled_channels and depth_limit > 0
            else ({}, {})
        )
        if "graph" in enabled_channels and bound_relationships:
            for item_id, score in bound_relationships.items():
                graph_scores[item_id] = max(graph_scores.get(item_id, 0.0), score)
                graph_depths[item_id] = min(graph_depths.get(item_id, 2), 2)
        if "graph" in enabled_channels and bound_columns:
            for item_id, score in bound_columns.items():
                graph_scores[item_id] = max(graph_scores.get(item_id, 0.0), score)
                graph_depths[item_id] = min(graph_depths.get(item_id, 1), 1)
        graph = _rank_scores(graph_scores, max(limit * 8, 64))

        ranks = {
            "exact": {item_id: index for index, (item_id, _) in enumerate(exact, 1)},
            "lexical": {item_id: index for index, (item_id, _) in enumerate(lexical, 1)},
            "vector": {item_id: index for index, (item_id, _) in enumerate(vector, 1)},
            "graph": {item_id: index for index, (item_id, _) in enumerate(graph, 1)},
            "ruleBinding": {item_id: index for index, (item_id, _) in enumerate(rule_binding, 1)},
        }
        score_lookup = {
            "lexical": lexical_scores,
            "vector": vector_scores,
        }
        fused: dict[str, _Candidate] = {}
        for item_id, document in by_id.items():
            components = [
                ("exact", ranks["exact"].get(item_id)),
                ("lexical", ranks["lexical"].get(item_id)),
                ("vector", ranks["vector"].get(item_id)),
                ("graph", ranks["graph"].get(item_id)),
                ("ruleBinding", ranks["ruleBinding"].get(item_id)),
            ]
            if not any(rank is not None for _, rank in components):
                continue
            fused_score = sum(
                1.0 / (self.rrf_k + rank)
                for _, rank in components
                if rank is not None
            )
            fused[item_id] = _Candidate(
                document=document,
                fused_score=fused_score,
                exact_rank=ranks["exact"].get(item_id),
                lexical_rank=ranks["lexical"].get(item_id),
                vector_rank=ranks["vector"].get(item_id),
                graph_rank=ranks["graph"].get(item_id),
                rule_binding_rank=ranks["ruleBinding"].get(item_id),
                graph_depth=graph_depths.get(item_id),
                lexical_score=score_lookup["lexical"].get(item_id, 0.0),
                vector_score=score_lookup["vector"].get(item_id, 0.0),
            )
        max_fused_score = max(
            (candidate.fused_score for candidate in fused.values()),
            default=1.0,
        )
        ranking_scores = {
            item_id: (
                _lightweight_rerank_score(query_text, candidate, max_fused_score)
                if rerank
                else candidate.fused_score
            )
            for item_id, candidate in fused.items()
        }
        ordered = sorted(
            fused.values(),
            key=lambda item: (
                -ranking_scores[item.document.id],
                _candidate_priority(item),
                _KIND_ORDER.get(item.document.kind, 99),
                item.document.id,
            ),
        )
        max_score = ranking_scores[ordered[0].document.id] if ordered else 1.0
        counts: Counter[str] = Counter()
        hits: list[HybridSearchHit] = []
        traces: list[RetrievalTrace] = []
        # Reservation must never exceed either a kind quota or the total
        # response limit.  Otherwise a small custom budget can reserve more
        # records than it is able to emit and leave the response under-filled.
        reservation_candidates: list[tuple[float, int, str, str]] = []
        for kind, scores in (
            ("relationship", bound_relationships),
            ("column", bound_columns),
        ):
            kind_quota = quota_map.get(kind)
            if kind_quota is not None and kind_quota <= 0:
                continue
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            if kind_quota is not None:
                ranked = ranked[:kind_quota]
            reservation_candidates.extend(
                (score, _KIND_ORDER.get(kind, 99), kind, item_id)
                for item_id, score in ranked
                if item_id in fused
            )
        reservation_candidates.sort(key=lambda item: (-item[0], item[1], item[3]))
        reserved_by_kind: dict[str, set[str]] = {
            "relationship": set(),
            "column": set(),
        }
        for _, _, kind, item_id in reservation_candidates[:limit]:
            reserved_by_kind[kind].add(item_id)
        for candidate in ordered:
            kind = candidate.document.kind
            quota = quota_map.get(kind)
            reserved = reserved_by_kind.get(kind, set())
            is_reserved = candidate.document.id in reserved
            if reserved and candidate.document.id not in reserved:
                remaining_reserved = len(reserved)
                if quota is not None and counts[kind] >= max(0, quota - remaining_reserved):
                    continue
            # Keep total-budget space for rule-bound join records even when
            # their mixed-channel RRF score is lower than unrelated records.
            total_reserved = sum(len(values) for values in reserved_by_kind.values())
            if not is_reserved and len(hits) >= max(0, limit - total_reserved):
                continue
            if quota is not None and counts[kind] >= quota:
                continue
            counts[kind] += 1
            reserved.discard(candidate.document.id)
            relevance = ranking_scores[candidate.document.id] / max_score if max_score > 0 else 0.0
            match_type = candidate.primary_type
            reason = _candidate_reason(candidate, vector_reason)
            if rerank:
                reason += " after deterministic lightweight reranking"
            hit = HybridSearchHit(
                document=candidate.document,
                score=max(0.0, min(1.0, relevance)),
                rank=len(hits) + 1,
                match_type=match_type,
                reason=reason,
                retrieval_types=candidate.retrieval_types,
            )
            hits.append(hit)
            if trace:
                traces.append(
                    RetrievalTrace(
                        document_id=candidate.document.id,
                        source=_trace_source(candidate.document.kind),
                        retrieval_type=match_type,
                        relevance=hit.score,
                        reason_code=_reason_code(match_type),
                        project_revision=partition.revision,
                        authorization_filtered=False,
                        selected=True,
                    )
                )
            if len(hits) >= limit:
                break
        fallback_reason = (
            vector_reason or partition.degraded_reason or "embedder_unavailable"
            if not partition.embedding_available
            else vector_reason
        )
        return HybridSearchResponse(
            tuple(hits),
            status,
            query_text,
            requested,
            status.backend,
            tuple(traces),
            fallback_reason,
            len(fused),
            filtered_count,
            len(hits),
        )

    # Friendly aliases for callers that use retrieval terminology.
    retrieve = search
    query = search

    def _backend_for(self, partition: _Partition | None) -> str:
        if partition is not None and partition.embedding_available:
            return "hybrid"
        return "lexical"

    def _partition_compatible(self, partition: _Partition) -> tuple[bool, str | None]:
        if partition.revision not in self._revisions:
            return False, "revision_not_built"
        expected = _embedder_config(self.embedder)
        if _json_safe(partition.embedding_config) != _json_safe(expected):
            return False, "embedding_config_mismatch"
        provider = _provider_status(self.embedder)
        if (
            partition.embedding_available
            and partition.embedding_dimension is not None
            and provider.dimension is not None
            and partition.embedding_dimension != provider.dimension
        ):
            return False, "embedding_dimension_mismatch"
        return True, None

    def _visible_documents(
        self,
        partition: _Partition,
        selected_kinds: set[str],
        visibility_filter: VisibilityFilter | None,
    ) -> tuple[SemanticDocument, ...]:
        visible: list[SemanticDocument] = []
        for document in partition.documents:
            if selected_kinds and document.kind not in selected_kinds:
                continue
            # Both structural visibility and the injected policy callback are
            # applied before any candidate list, graph edge, or trace exists.
            if not document.is_visible:
                continue
            if visibility_filter is not None:
                try:
                    allowed = bool(visibility_filter(document))
                except Exception:
                    allowed = False
                if not allowed:
                    continue
            visible.append(document)
        return tuple(visible)

    def _exact_rank(self, query: str, documents: Sequence[SemanticDocument]) -> list[tuple[str, float]]:
        normal = _normalise(query)
        matches: list[tuple[str, float]] = []
        for document in documents:
            identifiers = {
                _normalise(document.id),
                _normalise(document.model or ""),
                _normalise(document.field or ""),
            }
            if document.model and document.field:
                identifiers.add(_normalise(f"{document.model}.{document.field}"))
            identifiers.update(_normalise(value) for value in document.referencedModels)
            identifiers.update(_normalise(value) for value in document.referencedColumns)
            if normal and normal in identifiers:
                matches.append((document.id, 1.0))
        matches.sort(key=lambda item: (_KIND_ORDER.get(next((doc.kind for doc in documents if doc.id == item[0]), ""), 99), item[0]))
        return matches

    def _bm25(self, query: str, documents: Sequence[SemanticDocument]) -> dict[str, float]:
        query_terms = _unicode_tokens(query)
        if not query_terms:
            return {}
        token_map = {document.id: _document_tokens(document) for document in documents}
        document_frequency: Counter[str] = Counter()
        for tokens in token_map.values():
            document_frequency.update(set(tokens))
        average_length = sum(len(tokens) for tokens in token_map.values()) / max(1, len(token_map))
        scores: dict[str, float] = {}
        k1 = 1.2
        b = 0.75
        for document in documents:
            tokens = token_map[document.id]
            length = len(tokens)
            counts = Counter(tokens)
            score = 0.0
            for term in query_terms:
                frequency = counts.get(term, 0)
                if frequency <= 0:
                    continue
                df = document_frequency.get(term, 0)
                idf = math.log(1.0 + (len(documents) - df + 0.5) / (df + 0.5))
                denominator = frequency + k1 * (1.0 - b + b * length / max(1.0, average_length))
                score += idf * (frequency * (k1 + 1.0)) / max(1e-12, denominator)
            phrase = _normalise(query) in _normalise(" ".join((document.id, _document_text(document))))
            if phrase:
                score += 0.25
            aliases = document.metadata.get("aliases") if isinstance(document.metadata, Mapping) else None
            if isinstance(aliases, list):
                normalized_query = _normalise(query)
                query_token_set = set(query_terms)
                for alias in aliases:
                    if not isinstance(alias, str) or not alias.strip():
                        continue
                    normalized_alias = _normalise(alias)
                    alias_terms = set(_unicode_tokens(alias))
                    if normalized_alias and normalized_alias in normalized_query:
                        score += 8.0
                    elif alias_terms:
                        coverage = len(alias_terms & query_token_set) / len(alias_terms)
                        if coverage >= 0.6:
                            score += 2.0 * coverage
            if score > 0.0:
                scores[document.id] = score
        return scores

    def _rule_binding_scores(
        self,
        query: str,
        documents: Sequence[SemanticDocument],
        *,
        restricted: bool,
    ) -> dict[str, float]:
        """Bind curated business phrases to structured rule documents."""

        normalized_query = _normalise(query)
        query_terms = set(_unicode_tokens(query))
        scores: dict[str, float] = {}
        for document in documents:
            if document.kind != "rule" or not isinstance(document.metadata, Mapping):
                continue
            if document.metadata.get("ruleType") == "security" and not restricted:
                continue
            aliases = document.metadata.get("aliases")
            if not isinstance(aliases, list):
                continue
            score = 0.0
            for alias in aliases:
                if not isinstance(alias, str) or not alias.strip():
                    continue
                normalized_alias = _normalise(alias)
                alias_terms = set(_unicode_tokens(alias))
                if normalized_alias and normalized_alias in normalized_query:
                    score = max(score, 1.0 + min(1.0, len(normalized_alias) / 32.0))
                elif alias_terms:
                    coverage = len(alias_terms & query_terms) / len(alias_terms)
                    if coverage >= 0.8:
                        score = max(score, coverage)
            if score > 0:
                if document.metadata.get("ruleType") == "security" and restricted:
                    score += 2.0
                if document.metadata.get("mandatory") is True:
                    score += 0.05
                priority = document.metadata.get("priority")
                if isinstance(priority, (int, float)) and not isinstance(priority, bool):
                    score += max(0.0, min(0.1, float(priority) / 10_000.0))
                scores[document.id] = score
        return scores

    def _rule_relationship_scores(
        self,
        ranked_rules: Sequence[tuple[str, float]],
        documents: Sequence[SemanticDocument],
    ) -> dict[str, float]:
        """Prioritize joins explicitly sharing a matched rule's bound fields."""

        by_id = {document.id: document for document in documents}
        relationships = [document for document in documents if document.kind == "relationship"]
        scores: dict[str, float] = {}
        for rank, (rule_id, _) in enumerate(ranked_rules, 1):
            rule = by_id.get(rule_id)
            if rule is None or rule.kind != "rule":
                continue
            rule_type = rule.metadata.get("ruleType") if isinstance(rule.metadata, Mapping) else None
            if len(rule.referencedModels) < 2 or rule_type not in {
                "join_semantics", "time_semantics", "metric_definition", "result_shape"
            }:
                continue
            columns = {_normalise(value) for value in rule.referencedColumns}
            if not columns:
                continue
            matched = False
            for relationship in relationships:
                overlap = columns & {
                    _normalise(value) for value in relationship.referencedColumns
                }
                if overlap:
                    matched = True
                    # Stronger than ordinary one-hop graph expansion while
                    # remaining bounded by the rule-binding rank.
                    scores[relationship.id] = max(
                        scores.get(relationship.id, 0.0),
                        2.0 + 1.0 / rank,
                    )
            # One best structured rule supplies the join path. Lower-ranked
            # business rules must not turn every schema relationship into a
            # rule-binding match.
            if matched:
                break
        return scores

    def _rule_column_scores(
        self,
        ranked_rules: Sequence[tuple[str, float]],
        documents: Sequence[SemanticDocument],
    ) -> dict[str, float]:
        """Reserve explicitly bound columns from the strongest matched rules."""

        by_id = {document.id: document for document in documents}
        column_ids = {
            _normalise(document.id.partition(":")[2]): document.id
            for document in documents
            if document.kind == "column"
        }
        scores: dict[str, float] = {}
        matched_rules = 0
        for rank, (rule_id, _) in enumerate(ranked_rules, 1):
            rule = by_id.get(rule_id)
            if rule is None or rule.kind != "rule" or not rule.referencedColumns:
                continue
            matched_rules += 1
            for reference in rule.referencedColumns:
                target = column_ids.get(_normalise(reference))
                if target:
                    scores[target] = max(scores.get(target, 0.0), 2.0 + 1.0 / rank)
            if matched_rules >= 5 or len(scores) >= 12:
                break
        return dict(
            sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:12]
        )

    def _graph_expand(
        self,
        documents: Sequence[SemanticDocument],
        seeds: Sequence[str],
        depth_limit: int,
    ) -> tuple[dict[str, float], dict[str, int]]:
        if depth_limit <= 0:
            return {}, {}
        by_id = {document.id: document for document in documents}
        adjacency: dict[str, set[str]] = {document.id: set() for document in documents}
        model_ids = {
            _normalise(document.model or document.id.partition(":")[2]): document.id
            for document in documents
            if document.kind == "model"
        }
        column_ids = {
            _normalise(document.id.partition(":")[2]): document.id
            for document in documents
            if document.kind == "column" and "." in document.id.partition(":")[2]
        }

        def connect(source: str, target: str, *, reverse: bool = False) -> None:
            if source in adjacency and target in adjacency and source != target:
                adjacency[source].add(target)
                if reverse:
                    adjacency[target].add(source)

        # Relationship edges are the only cross-model expansion route. A
        # shared model or field name never creates an implicit clique.
        for document in documents:
            if document.kind != "relationship":
                continue
            for model in document.referencedModels:
                model_id = model_ids.get(_normalise(model))
                if model_id:
                    connect(model_id, document.id, reverse=True)
            for reference in document.referencedColumns:
                column_id = column_ids.get(_normalise(reference))
                if column_id:
                    connect(document.id, column_id, reverse=True)

        for document in documents:
            if document.kind in {"model", "relationship"}:
                continue
            if document.kind == "column" and document.model:
                model_id = model_ids.get(_normalise(document.model))
                if model_id:
                    # Field -> owner is required for structural completion;
                    # the reverse edge would expand one model to every field.
                    connect(document.id, model_id)
                continue
            referenced_models = tuple(document.referencedModels) + (
                (document.model,) if document.model else ()
            )
            for model in referenced_models:
                model_id = model_ids.get(_normalise(model))
                if model_id:
                    connect(document.id, model_id)
            for reference in document.referencedColumns:
                column_id = column_ids.get(_normalise(reference))
                if column_id:
                    connect(document.id, column_id)
        queue: deque[tuple[str, int]] = deque()
        seen: set[str] = set()
        for seed in seeds:
            if seed in by_id and seed not in seen:
                seen.add(seed)
                queue.append((seed, 0))
        scores: dict[str, float] = {}
        depths: dict[str, int] = {}
        while queue:
            current, depth = queue.popleft()
            if depth >= depth_limit:
                continue
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                next_depth = depth + 1
                queue.append((neighbor, next_depth))
                scores[neighbor] = max(scores.get(neighbor, 0.0), 1.0 / next_depth)
                depths[neighbor] = min(depths.get(neighbor, next_depth), next_depth)
        return scores, depths

    def _partition_filename(self, revision: str) -> str:
        return f"partition-{_revision_key(revision)}.json"

    def _staged_partition_filename(self, revision: str) -> str:
        return f"partition-{_revision_key(revision)}.staged.json"

    def _persist_partition(self, partition: _Partition) -> None:
        assert self.storage_path is not None
        self.storage_path.mkdir(parents=True, exist_ok=True)
        payload = _partition_to_dict(partition)
        staged = self.storage_path / self._staged_partition_filename(partition.revision)
        final = self.storage_path / self._partition_filename(partition.revision)
        _atomic_write_json(staged, payload)
        os.replace(staged, final)

    def _write_active_pointer(self, revision: str) -> None:
        assert self.storage_path is not None
        self.storage_path.mkdir(parents=True, exist_ok=True)
        payload = {
            "schemaVersion": 1,
            "revision": revision,
            "partition": self._partition_filename(revision),
            "embeddingConfig": _embedder_config(self.embedder),
        }
        _atomic_write_json(self.storage_path / "active.json", payload)

    def _remove_active_pointer(self) -> None:
        assert self.storage_path is not None
        try:
            (self.storage_path / "active.json").unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _load_storage(self) -> None:
        assert self.storage_path is not None
        try:
            self.storage_path.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.storage_path.glob("partition-*.json")):
                if path.name.endswith(".staged.json"):
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    partition = _partition_from_dict(payload)
                except (OSError, ValueError, TypeError, SemanticIndexError, json.JSONDecodeError):
                    continue
                self._revisions[partition.revision] = partition
            pointer = self.storage_path / "active.json"
            if not pointer.exists():
                return
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            revision = _clean_revision(payload.get("revision")) if isinstance(payload, Mapping) else None
            partition_name = payload.get("partition") if isinstance(payload, Mapping) else None
            self._active_revision = revision
            self._active_partition_file = partition_name if isinstance(partition_name, str) else None
            if revision is None or revision not in self._revisions:
                self._pointer_error = "active_partition_missing"
            elif self._active_partition_file != self._partition_filename(revision):
                self._pointer_error = "active_pointer_invalid"
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._pointer_error = f"active_pointer_unreadable:{type(exc).__name__}"


# Public aliases chosen to keep integrations provider-neutral while allowing
# callers to describe the object as an index or a retriever.
HybridSemanticIndex = HybridSemanticRetriever
PersistentHybridSemanticIndex = HybridSemanticRetriever
PersistentHybridIndex = HybridSemanticRetriever
SemanticRetriever = HybridSemanticRetriever
SentenceTransformerAdapter = SentenceTransformerEmbedder
MultilingualSentenceTransformerEmbedder = SentenceTransformerEmbedder
SentenceTransformerMultilingualAdapter = SentenceTransformerEmbedder


def create_default_retriever(storage_path: str | Path) -> HybridSemanticRetriever:
    """Create the configured retriever, preserving explicit degraded state."""

    provider = os.environ.get("SEMARAIL_EMBEDDING_PROVIDER", "sentence-transformers").strip().lower()
    embedder: Any | None = None
    if provider not in {"", "none", "disabled", "off"}:
        if provider != "sentence-transformers":
            raise ValueError("configured semantic embedding provider is unavailable")
        raw_batch = os.environ.get("SEMARAIL_EMBEDDING_BATCH_SIZE", "32")
        try:
            batch_size = int(raw_batch)
        except ValueError:
            batch_size = 32
        embedder = SentenceTransformerEmbedder(
            model_name=os.environ.get(
                "SEMARAIL_EMBEDDING_MODEL", SentenceTransformerEmbedder.DEFAULT_MODEL
            ),
            device=os.environ.get("SEMARAIL_EMBEDDING_DEVICE", "cpu"),
            batch_size=max(1, min(batch_size, 1_024)),
            model_revision=os.environ.get("SEMARAIL_EMBEDDING_MODEL_REVISION", "main"),
        )
    return HybridSemanticRetriever(embedder=embedder, storage_path=storage_path)


def _clean_revision(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _selected_kinds(kinds: Iterable[str] | None) -> set[str]:
    if isinstance(kinds, str):
        return {kinds}
    return {item for item in (kinds or ()) if isinstance(item, str)}


def _quotas(
    quotas: Mapping[str, int] | None,
    type_quotas: Mapping[str, int] | None,
    kind_quotas: Mapping[str, int] | None,
) -> dict[str, int]:
    result = dict(quotas or {})
    for alias in (type_quotas, kind_quotas):
        if alias:
            for kind, value in alias.items():
                if kind in result and result[kind] != value:
                    raise SemanticIndexError("conflicting type quotas")
                result[kind] = value
    for kind, value in result.items():
        if kind not in SEMANTIC_DOCUMENT_KINDS or not isinstance(value, int) or value < 0:
            raise SemanticIndexError("quotas must contain non-negative known kinds")
    return result


def _rank_scores(scores: Mapping[str, float], limit: int) -> list[tuple[str, float]]:
    positive = [(item_id, float(score)) for item_id, score in scores.items() if math.isfinite(float(score)) and float(score) > 0.0]
    positive.sort(key=lambda item: (-item[1], item[0]))
    return positive[: max(0, limit)]


def _top_items(items: Sequence[tuple[str, float]], limit: int) -> Sequence[tuple[str, float]]:
    return items[: max(0, limit)]


def _cosine(left: Vector, right: Vector) -> float:
    if len(left) != len(right):
        return -1.0
    value = sum(a * b for a, b in zip(left, right))
    return max(-1.0, min(1.0, value))


def _graph_keys(document: SemanticDocument) -> set[str]:
    keys: set[str] = {"id:" + _normalise(document.id)}
    model = _normalise(document.model or "")
    field = _normalise(document.field or "")
    if model:
        keys.add("model:" + model)
    if model and field:
        keys.add("column:" + model + "." + field)
    for value in document.referencedModels:
        normal = _normalise(value)
        if normal:
            keys.add("model:" + normal)
    for value in document.referencedColumns:
        normal = _normalise(value)
        if normal:
            keys.add("column:" + normal)
    if document.kind == "model" and document.id.partition(":")[2]:
        keys.add("model:" + _normalise(document.id.partition(":")[2]))
    if document.kind == "column" and document.id.partition(":")[2]:
        qualified = _normalise(document.id.partition(":")[2])
        if "." in qualified:
            model_name, _field_name = qualified.split(".", 1)
            # Never connect columns in unrelated models merely because their
            # unqualified names happen to match (for example ``id`` or
            # ``team_size``). Cross-model edges must come from an explicit
            # Relationship or qualified reference.
            keys.update({"model:" + model_name, "column:" + qualified})
    return keys


def _candidate_priority(candidate: _Candidate) -> int:
    if candidate.exact_rank is not None:
        return 0
    if candidate.rule_binding_rank is not None:
        return 1
    if candidate.vector_rank is not None and candidate.lexical_rank is None:
        return 2
    if candidate.lexical_rank is not None:
        return 3
    if candidate.vector_rank is not None:
        return 4
    return 5


def _lightweight_rerank_score(
    query: str,
    candidate: _Candidate,
    max_fused_score: float,
) -> float:
    """Reorder a fused candidate set without another model dependency.

    This second stage deliberately uses features that are independent of raw
    vector distance: query-token coverage, normalized phrase containment, and
    agreement across retrieval channels.  It is opt-in so benchmark evidence
    can decide whether its extra work improves a corpus before production use.
    """

    query_terms = set(_unicode_tokens(query))
    document_terms = set(_document_tokens(candidate.document))
    coverage = (
        len(query_terms & document_terms) / len(query_terms)
        if query_terms
        else 0.0
    )
    normalized_query = _normalise(query)
    normalized_document = _normalise(_document_text(candidate.document))
    phrase = 1.0 if normalized_query and normalized_query in normalized_document else 0.0
    agreement = min(1.0, len(candidate.retrieval_types) / 3.0)
    fused = candidate.fused_score / max_fused_score if max_fused_score > 0 else 0.0
    return 0.55 * fused + 0.35 * coverage + 0.05 * phrase + 0.05 * agreement


def _candidate_reason(candidate: _Candidate, vector_reason: str | None) -> str:
    if candidate.primary_type == "exact":
        return "exact identifier or business-name match"
    if candidate.primary_type == "ruleBinding":
        return "structured business-rule alias binding"
    if candidate.primary_type == "vector":
        return "vector cosine similarity fused with reciprocal rank"
    if candidate.primary_type == "lexical":
        return "Unicode-aware BM25 lexical match fused with reciprocal rank"
    if candidate.primary_type == "graph":
        return f"relationship graph expansion at depth {candidate.graph_depth or 1}"
    return vector_reason or "hybrid reciprocal-rank fusion"


def _trace_source(kind: str) -> str:
    return {
        "model": "schema",
        "column": "schema",
        "relationship": "relationships",
        "cube": "metrics",
        "metric": "metrics",
        "dimension": "metrics",
        "time_dimension": "metrics",
        "rule": "rules",
        "sql_example": "sqlExamples",
        "view": "views",
    }.get(kind, "schema")


def _reason_code(retrieval_type: str) -> str:
    return {
        "exact": "exactMatch",
        "lexical": "lexicalMatch",
        "vector": "vectorMatch",
        "graph": "graphExpansion",
        "ruleBinding": "ruleBinding",
    }.get(retrieval_type, "fallback")


def _partition_to_dict(partition: _Partition) -> JSON:
    return {
        "schemaVersion": 1,
        "revision": partition.revision,
        "documents": [document.to_dict() for document in partition.documents],
        "vectors": {key: list(value) for key, value in sorted(partition.vectors.items())},
        "embeddingConfig": _json_safe(partition.embedding_config),
        "embeddingAvailable": partition.embedding_available,
        "degradedReason": partition.degraded_reason,
        "builtAt": partition.built_at,
        "buildDurationMs": partition.build_duration_ms,
        "embeddingDimension": partition.embedding_dimension,
    }


def _partition_from_dict(value: Mapping[str, Any]) -> _Partition:
    if value.get("schemaVersion") != 1:
        raise SemanticIndexError("unsupported semantic partition schema")
    revision = _clean_revision(value.get("revision"))
    if revision is None or not isinstance(value.get("documents"), list):
        raise SemanticIndexError("invalid semantic partition")
    documents = tuple(SemanticDocument.from_mapping(item) for item in value["documents"])
    if any(document.projectRevision != revision for document in documents):
        raise SemanticIndexError("partition document revision mismatch")
    if len({document.id for document in documents}) != len(documents):
        raise SemanticIndexError("partition contains duplicate document ids")
    raw_vectors = value.get("vectors", {})
    vectors: dict[str, Vector] = {}
    if isinstance(raw_vectors, Mapping):
        for key, raw in raw_vectors.items():
            vector = _safe_vector(raw)
            if vector is not None:
                vectors[str(key)] = vector
    config = value.get("embeddingConfig")
    if not isinstance(config, Mapping):
        config = {"provider": "none"}
    return _Partition(
        revision=revision,
        documents=documents,
        vectors=vectors,
        embedding_config=_json_safe(config),
        embedding_available=bool(value.get("embeddingAvailable", bool(vectors))),
        degraded_reason=value.get("degradedReason") if isinstance(value.get("degradedReason"), str) else None,
        built_at=value.get("builtAt") if isinstance(value.get("builtAt"), str) else None,
        build_duration_ms=(
            float(value["buildDurationMs"])
            if isinstance(value.get("buildDurationMs"), (int, float))
            else None
        ),
        embedding_dimension=(
            int(value["embeddingDimension"])
            if isinstance(value.get("embeddingDimension"), int)
            else (len(next(iter(vectors.values()))) if vectors else None)
        ),
    )


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


__all__ = [
    "Embedder",
    "EmbedderStatus",
    "EmbedderUnavailable",
    "SentenceTransformerEmbedder",
    "SentenceTransformerAdapter",
    "MultilingualSentenceTransformerEmbedder",
    "SentenceTransformerMultilingualAdapter",
    "RetrievalIndexStatus",
    "RetrievalTrace",
    "HybridSearchHit",
    "HybridSearchResponse",
    "HybridSemanticRetriever",
    "HybridSemanticIndex",
    "PersistentHybridSemanticIndex",
    "PersistentHybridIndex",
    "SemanticRetriever",
    "create_default_retriever",
]
