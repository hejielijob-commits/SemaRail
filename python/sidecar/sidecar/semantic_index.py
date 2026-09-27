"""Revisioned semantic documents and dependency-free retrieval primitives.

The semantic project (Wren's MDL manifest plus the SemaRail companion
documents) is the source of truth.  This module turns that source into small,
stable :class:`SemanticDocument` records and provides a deterministic lexical
index that can be used before an embedding runtime is installed.

The index deliberately has no LanceDB, tokenizer, or embedding dependency.
``SemanticIndex`` and ``VectorSemanticIndex`` are protocol seams so a vector
backend can be added without changing document construction or the transport
layer.  ``InMemorySemanticIndex`` is useful in tests and for the local MVP.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable

try:  # PyYAML is optional; the lexical index itself never needs it.
    import yaml as _yaml
except ImportError:  # pragma: no cover - exercised in minimal deployments
    _yaml = None


SEMANTIC_DOCUMENT_KINDS = frozenset(
    {
        "model",
        "column",
        "relationship",
        "cube",
        "metric",
        "dimension",
        "time_dimension",
        "rule",
        "sql_example",
        "view",
    }
)

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
_REVISION_IGNORED_DIRS = frozenset({
    ".git", ".wren", "__pycache__", "target", ".semantic-console",
    "node_modules", ".venv", "venv", "dist", "build", "state",
})
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ASCII_TOKEN = re.compile(r"[a-z0-9]+(?:[_.-][a-z0-9]+)*", re.IGNORECASE)
_QUALIFIED_NAME = re.compile(
    r"(?<![A-Za-z0-9_$-])([A-Za-z_][A-Za-z0-9_$-]*)\.([A-Za-z_][A-Za-z0-9_$-]*)"
)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_FRONTMATTER = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)

JSON: TypeAlias = dict[str, Any]
VisibilityFilter: TypeAlias = Callable[["SemanticDocument"], bool]


class SemanticIndexError(ValueError):
    """Raised when an index or semantic source cannot be made trustworthy."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _get(value: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return default


def _as_section(value: Any, name: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SemanticIndexError(f"manifest[{name!r}] must be a list")
    return [item for item in value if isinstance(item, Mapping)]


def _normalise(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _tokens(value: str) -> tuple[str, ...]:
    """Tokenise identifiers/ASCII words and CJK characters without deps."""

    normal = _normalise(value)
    tokens: list[str] = []
    occupied: list[tuple[int, int]] = []
    for match in _ASCII_TOKEN.finditer(normal):
        token = match.group(0)
        tokens.append(token)
        occupied.append(match.span())
    for index, char in enumerate(normal):
        if _CJK.match(char):
            tokens.append(char)
    # A mixed identifier such as ``员工 headcount`` is intentionally represented
    # by both the CJK characters and the ASCII identifier.
    return tuple(tokens)


def _language(*values: str) -> str:
    value = " ".join(v for v in values if v)
    has_cjk = bool(_CJK.search(value))
    has_ascii = bool(re.search(r"[A-Za-z]", value))
    if has_cjk and has_ascii:
        return "mixed"
    if has_cjk:
        return "zh-CN"
    if has_ascii:
        return "en-US"
    return "und"


def _clean_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(sorted({item.strip() for item in value if isinstance(item, str) and item.strip()}))
    return ()


def _json_value(value: Any) -> Any:
    """Return a JSON-safe, deterministic copy for public records."""

    if isinstance(value, Mapping):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _hash_payload(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _document_hash_values(
    *,
    identifier: str,
    kind: str,
    project_revision: str,
    model: str | None,
    field_name: str | None,
    text: str,
    language: str,
    source_path: str | None,
    referenced_models: Sequence[str],
    referenced_columns: Sequence[str],
    visibility: Mapping[str, Any] | bool | None,
    metadata: Mapping[str, Any] | None,
) -> str:
    return _hash_payload(
        {
            "id": identifier,
            "kind": kind,
            # A content hash identifies the semantic payload, not the
            # immutable revision envelope carrying it. Excluding the revision
            # lets a newly published index safely reuse embeddings for records
            # whose meaning did not change while every record still carries
            # and is searched under its own projectRevision.
            "model": model,
            "field": field_name,
            "text": text,
            "language": language,
            "sourcePath": source_path,
            "referencedModels": list(referenced_models),
            "referencedColumns": list(referenced_columns),
            "visibility": visibility,
            "metadata": metadata or {},
        }
    )


@dataclass(frozen=True, slots=True, init=False)
class SemanticDocument:
    """One searchable semantic fact derived from one project revision.

    Wire-shaped fields intentionally retain the camelCase names used by the
    semantic API.  Snake-case aliases are provided for Python callers.  The
    ``contentHash`` covers the semantic payload except its revision envelope
    and itself, which makes cross-revision incremental indexing safe and
    deterministic.
    """

    id: str
    kind: str
    projectRevision: str
    model: str | None = None
    field: str | None = None
    text: str = ""
    language: str = "und"
    sourcePath: str | None = None
    referencedModels: tuple[str, ...] = ()
    referencedColumns: tuple[str, ...] = ()
    visibility: Mapping[str, Any] | bool | None = None
    contentHash: str = ""
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def __init__(
        self,
        id: str,
        kind: str,
        projectRevision: str | None = None,
        model: str | None = None,
        field: str | None = None,
        text: str = "",
        language: str = "und",
        sourcePath: str | None = None,
        referencedModels: Sequence[str] = (),
        referencedColumns: Sequence[str] = (),
        visibility: Mapping[str, Any] | bool | None = None,
        contentHash: str = "",
        metadata: Mapping[str, Any] | None = None,
        *,
        project_revision: str | None = None,
        field_name: str | None = None,
        source_path: str | None = None,
        referenced_models: Sequence[str] | None = None,
        referenced_columns: Sequence[str] | None = None,
        content_hash: str | None = None,
    ) -> None:
        """Create a document using wire names or Python snake-case aliases."""

        object.__setattr__(self, "id", id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "projectRevision", projectRevision if projectRevision is not None else project_revision or "")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "field", field if field is not None else field_name)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "sourcePath", sourcePath if sourcePath is not None else source_path)
        object.__setattr__(self, "referencedModels", referencedModels if referenced_models is None else referenced_models)
        object.__setattr__(self, "referencedColumns", referencedColumns if referenced_columns is None else referenced_columns)
        object.__setattr__(self, "visibility", visibility)
        object.__setattr__(self, "contentHash", contentHash if content_hash is None else content_hash)
        object.__setattr__(self, "metadata", metadata or {})
        self.__post_init__()

    def __post_init__(self) -> None:
        identifier = _text(self.id)
        kind = _text(self.kind)
        revision = _text(self.projectRevision)
        if not identifier or not kind or not revision:
            raise SemanticIndexError("semantic document id, kind, and projectRevision are required")
        if kind not in SEMANTIC_DOCUMENT_KINDS:
            raise SemanticIndexError(f"unsupported semantic document kind: {kind}")
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "projectRevision", revision)
        object.__setattr__(self, "model", _text(self.model) or None)
        object.__setattr__(self, "field", _text(self.field) or None)
        object.__setattr__(self, "text", _text(self.text) or identifier)
        object.__setattr__(self, "language", _text(self.language) or "und")
        object.__setattr__(self, "sourcePath", _text(self.sourcePath) or None)
        object.__setattr__(self, "referencedModels", _clean_list(self.referencedModels))
        object.__setattr__(self, "referencedColumns", _clean_list(self.referencedColumns))
        if isinstance(self.visibility, Mapping):
            object.__setattr__(self, "visibility", _json_value(self.visibility))
        elif self.visibility is not None and not isinstance(self.visibility, bool):
            object.__setattr__(self, "visibility", bool(self.visibility))
        object.__setattr__(self, "metadata", _json_value(_mapping(self.metadata)))
        if not self.contentHash:
            object.__setattr__(
                self,
                "contentHash",
                _document_hash_values(
                    identifier=self.id,
                    kind=self.kind,
                    project_revision=self.projectRevision,
                    model=self.model,
                    field_name=self.field,
                    text=self.text,
                    language=self.language,
                    source_path=self.sourcePath,
                    referenced_models=self.referencedModels,
                    referenced_columns=self.referencedColumns,
                    visibility=self.visibility,
                    metadata=self.metadata,
                ),
            )
        elif not isinstance(self.contentHash, str) or not self.contentHash:
            raise SemanticIndexError("contentHash must be a non-empty string")

    # Python-friendly aliases.
    @property
    def project_revision(self) -> str:
        return self.projectRevision

    @property
    def source_path(self) -> str | None:
        return self.sourcePath

    @property
    def referenced_models(self) -> tuple[str, ...]:
        return self.referencedModels

    @property
    def referenced_columns(self) -> tuple[str, ...]:
        return self.referencedColumns

    @property
    def content_hash(self) -> str:
        return self.contentHash

    @property
    def is_visible(self) -> bool:
        if isinstance(self.visibility, bool):
            return self.visibility
        if isinstance(self.visibility, Mapping):
            value = self.visibility.get("visible")
            return value is not False
        return True

    def to_dict(self) -> JSON:
        """Return a JSON-safe wire representation."""

        result: JSON = {
            "id": self.id,
            "kind": self.kind,
            "projectRevision": self.projectRevision,
            "model": self.model,
            "field": self.field,
            "text": self.text,
            "language": self.language,
            "sourcePath": self.sourcePath,
            "referencedModels": list(self.referencedModels),
            "referencedColumns": list(self.referencedColumns),
            "visibility": _json_value(self.visibility),
            "contentHash": self.contentHash,
        }
        if self.metadata:
            result["metadata"] = _json_value(self.metadata)
        return result

    as_dict = to_dict
    to_mapping = to_dict

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SemanticDocument":
        """Construct a document from either wire or Python naming."""

        return cls(
            id=_get(value, "id", default=""),
            kind=_get(value, "kind", default=""),
            projectRevision=_get(value, "projectRevision", "project_revision", default=""),
            model=_get(value, "model"),
            field=_get(value, "field"),
            text=_get(value, "text", default=""),
            language=_get(value, "language", default="und"),
            sourcePath=_get(value, "sourcePath", "source_path"),
            referencedModels=_get(value, "referencedModels", "referenced_models", default=()),
            referencedColumns=_get(value, "referencedColumns", "referenced_columns", default=()),
            visibility=_get(value, "visibility"),
            contentHash=_get(value, "contentHash", "content_hash", default=""),
            metadata=_get(value, "metadata", default={}),
        )


@dataclass(frozen=True, slots=True)
class IndexStatus(Mapping[str, Any]):
    """Safe, JSON-friendly state for one staged or active revision."""

    state: str
    backend: str
    revision: str | None
    active_revision: str | None
    built_revisions: tuple[str, ...] = ()
    document_count: int = 0
    stale_reason: str | None = None

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
            "indexStatus": self.state,
            "backend": self.backend,
            "revision": self.revision,
            "activeRevision": self.active_revision,
            "builtRevisions": list(self.built_revisions),
            "documentCount": self.document_count,
            "staleReason": self.stale_reason,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True, slots=True)
class SearchHit(Mapping[str, Any]):
    """One ranked hit with an auditable lexical/exact match explanation."""

    document: SemanticDocument
    score: float
    rank: int
    match_type: str
    reason: str

    @property
    def matchType(self) -> str:
        return self.match_type

    def to_dict(self) -> JSON:
        return {
            "document": self.document.to_dict(),
            "score": self.score,
            "rank": self.rank,
            "matchType": self.match_type,
            "reason": self.reason,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True, slots=True)
class SearchResponse(Sequence[SearchHit]):
    """Sequence-compatible search response carrying fail-closed status."""

    hits: tuple[SearchHit, ...]
    index_status: IndexStatus
    query: str
    revision: str | None
    backend: str
    fallback_reason: str | None = None

    @property
    def results(self) -> tuple[SearchHit, ...]:
        return self.hits

    @property
    def indexStatus(self) -> IndexStatus:
        return self.index_status

    @property
    def fallbackReason(self) -> str | None:
        return self.fallback_reason

    def __len__(self) -> int:
        return len(self.hits)

    def __iter__(self) -> Iterator[SearchHit]:
        return iter(self.hits)

    def __getitem__(self, key: int | slice | str) -> SearchHit | tuple[SearchHit, ...] | Any:
        if isinstance(key, str):
            return self.to_dict()[key]
        return self.hits[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def to_dict(self) -> JSON:
        return {
            "results": [hit.to_dict() for hit in self.hits],
            "indexStatus": self.index_status.to_dict(),
            "query": self.query,
            "revision": self.revision,
            "backend": self.backend,
            "fallbackReason": self.fallback_reason,
        }

    as_dict = to_dict


@runtime_checkable
class SemanticIndex(Protocol):
    """Backend-neutral lifecycle and search contract."""

    def build(
        self,
        documents: Iterable[SemanticDocument | Mapping[str, Any]],
        revision: str | None = None,
    ) -> IndexStatus: ...

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
    ) -> SearchResponse: ...

    def status(self, revision: str | None = None) -> IndexStatus: ...

    def activate(self, revision: str) -> IndexStatus: ...

    def remove_revision(self, revision: str) -> bool: ...


@runtime_checkable
class VectorSemanticIndex(SemanticIndex, Protocol):
    """Optional vector implementation seam.

    A vector backend must preserve the same revision and permission semantics;
    callers should not need to know whether a result came from LanceDB or the
    built-in lexical implementation.
    """

    backend: str


class InMemorySemanticIndex:
    """Dependency-free exact/lexical index with staged revisions."""

    backend = "lexical"

    def __init__(self) -> None:
        self._revisions: dict[str, tuple[SemanticDocument, ...]] = {}
        self._active_revision: str | None = None

    def build(
        self,
        documents: Iterable[SemanticDocument | Mapping[str, Any]],
        revision: str | None = None,
    ) -> IndexStatus:
        items = tuple(
            document
            if isinstance(document, SemanticDocument)
            else SemanticDocument.from_mapping(document)
            for document in documents
        )
        if revision is None:
            revisions = {item.projectRevision for item in items}
            if len(revisions) != 1:
                raise SemanticIndexError("build requires one project revision")
            revision = next(iter(revisions), None)
        revision = _text(revision)
        if not revision:
            raise SemanticIndexError("build requires a non-empty project revision")
        if any(item.projectRevision != revision for item in items):
            raise SemanticIndexError("document projectRevision does not match build revision")
        if len({item.id for item in items}) != len(items):
            raise SemanticIndexError("duplicate semantic document id in revision")
        ordered = tuple(sorted(items, key=lambda item: (_KIND_ORDER.get(item.kind, 99), item.id)))
        self._revisions[revision] = ordered
        return self.status(revision=revision)

    def activate(self, revision: str) -> IndexStatus:
        revision = _text(revision)
        if not revision or revision not in self._revisions:
            raise SemanticIndexError("cannot activate an unbuilt semantic revision")
        self._active_revision = revision
        return self.status(revision=revision)

    def remove_revision(self, revision: str) -> bool:
        normalized_revision = _text(revision)
        removed = self._revisions.pop(normalized_revision, None) is not None
        if self._active_revision == normalized_revision:
            self._active_revision = None
        return removed

    def status(self, revision: str | None = None) -> IndexStatus:
        requested = _text(revision) or self._active_revision
        active = self._active_revision
        built = tuple(sorted(self._revisions))
        if requested is None:
            return IndexStatus("missing", self.backend, None, active, built, 0, "no_revision")
        docs = self._revisions.get(requested)
        if active == requested and docs is not None:
            return IndexStatus("active", self.backend, requested, active, built, len(docs), None)
        if docs is not None:
            return IndexStatus("staged", self.backend, requested, active, built, len(docs), "revision_not_active")
        if active is None:
            return IndexStatus("missing", self.backend, requested, None, built, 0, "revision_not_built")
        return IndexStatus("stale", self.backend, requested, active, built, 0, "revision_mismatch")

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
    ) -> SearchResponse:
        if limit < 0:
            raise SemanticIndexError("search limit must be non-negative")
        query_text = _text(query)
        status = self.status(revision=revision)
        requested = status.revision
        if status.state != "active":
            reason = status.stale_reason or "index_not_active"
            return SearchResponse((), status, query_text, requested, self.backend, reason)
        if not query_text or limit == 0:
            return SearchResponse((), status, query_text, requested, self.backend, None)

        if isinstance(kinds, str):
            selected_kinds = {kinds}
        else:
            selected_kinds = {item for item in (kinds or ()) if isinstance(item, str)}
        if selected_kinds and not selected_kinds.issubset(SEMANTIC_DOCUMENT_KINDS):
            raise SemanticIndexError("search contains an unsupported document kind")
        quota_map = dict(quotas or {})
        for alias_map in (type_quotas, kind_quotas):
            if alias_map:
                for key, value in alias_map.items():
                    if key in quota_map and quota_map[key] != value:
                        raise SemanticIndexError("conflicting type quotas")
                    quota_map[key] = value
        for kind, quota in quota_map.items():
            if kind not in SEMANTIC_DOCUMENT_KINDS or not isinstance(quota, int) or quota < 0:
                raise SemanticIndexError("quotas must contain non-negative known kinds")
        query_terms = _tokens(query_text)
        query_normal = _normalise(query_text)
        if not query_terms:
            return SearchResponse((), status, query_text, requested, self.backend, None)

        scored: list[tuple[float, int, str, str, SemanticDocument, str]] = []
        for document in self._revisions[status.revision or ""]:
            if selected_kinds and document.kind not in selected_kinds:
                continue
            if not document.is_visible:
                continue
            if visibility_filter is not None:
                try:
                    if not visibility_filter(document):
                        continue
                except Exception:
                    # A failed policy callback must not leak a document.
                    continue
            score, match_type, reason = _lexical_score(document, query_normal, query_terms)
            if score <= 0:
                continue
            scored.append((score, 0 if match_type == "exact" else 1, document.id, match_type, document, reason))
        scored.sort(key=lambda item: (-item[0], item[1], _KIND_ORDER.get(item[4].kind, 99), item[2]))

        counts: Counter[str] = Counter()
        hits: list[SearchHit] = []
        for _, _, _, match_type, document, reason in scored:
            quota = quota_map.get(document.kind)
            if quota is not None and counts[document.kind] >= quota:
                continue
            counts[document.kind] += 1
            hits.append(SearchHit(document, _score_value(document, query_normal, query_terms), len(hits) + 1, match_type, reason))
            if len(hits) >= limit:
                break
        return SearchResponse(tuple(hits), status, query_text, requested, self.backend, None)


LexicalSemanticIndex = InMemorySemanticIndex
MemorySemanticIndex = InMemorySemanticIndex
VectorIndex = VectorSemanticIndex


def _search_terms(document: SemanticDocument) -> tuple[str, ...]:
    values = [document.id, document.kind, document.model or "", document.field or "", document.text]
    values.extend(document.referencedModels)
    values.extend(document.referencedColumns)
    return _tokens(" ".join(values))


def _lexical_score(
    document: SemanticDocument, query_normal: str, query_terms: Sequence[str]
) -> tuple[float, str, str]:
    doc_normal = _normalise(" ".join([document.id, document.model or "", document.field or "", document.text]))
    terms = _search_terms(document)
    term_counts = Counter(terms)
    overlap = sum(1 for term in query_terms if term_counts.get(term, 0))
    if overlap == 0:
        # Full-phrase matching is useful for punctuation-heavy English and
        # Chinese names that are not separated by whitespace.
        if query_normal and query_normal in doc_normal:
            return 2.0, "lexical", "query phrase in document text"
        return 0.0, "lexical", ""
    identifiers = {
        _normalise(document.id),
        _normalise(document.model or ""),
        _normalise(document.field or ""),
    }
    if document.model and document.field:
        identifiers.add(_normalise(f"{document.model}.{document.field}"))
    exact = query_normal in identifiers or any(
        query_normal == _normalise(value)
        for value in (document.referencedModels + document.referencedColumns)
    )
    phrase = bool(query_normal and query_normal in doc_normal)
    coverage = overlap / max(1, len(set(query_terms)))
    score = coverage + (2.0 if phrase else 0.0) + (10.0 if exact else 0.0)
    if exact:
        return score, "exact", "exact identifier match"
    if phrase:
        return score, "lexical", "query phrase and token overlap"
    return score, "lexical", "token overlap"


def _score_value(document: SemanticDocument, query_normal: str, query_terms: Sequence[str]) -> float:
    return _lexical_score(document, query_normal, query_terms)[0]


def _locale_record(locales: Mapping[str, Any], group: str, name: str) -> Mapping[str, Any]:
    raw_records = locales.get(group)
    if isinstance(raw_records, list):
        records: Mapping[str, Any] = {}
        value: Any = None
        for item in raw_records:
            if isinstance(item, Mapping) and _text(_get(item, "name", "id")) == name:
                value = item
                break
    else:
        records = _mapping(raw_records)
        value = records.get(name)
    if isinstance(records, list):  # pragma: no cover - retained for defensive typing
        for item in records:
            if isinstance(item, Mapping) and _text(_get(item, "name", "id")) == name:
                value = item
                break
    return _mapping(value)


def _localized(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, Mapping):
        return ()
    result: list[str] = []
    for locale in ("zh-CN", "zh", "en-US", "en"):
        item = value.get(locale)
        if isinstance(item, str) and item.strip() and item.strip() not in result:
            result.append(item.strip())
    return tuple(result)


def _description(record: Mapping[str, Any], locale: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    properties = _mapping(_get(record, "properties", default={}))
    values: list[str] = []
    for value in (_get(record, "displayName", "display_name"), _get(record, "description")):
        values.extend(_localized(value) or ((_text(value),) if _text(value) else ()))
    values.extend(_localized(properties.get("description")))
    if locale:
        values.extend(_localized(_get(locale, "displayName", "display_name")))
        values.extend(_localized(_get(locale, "description")))
        values.append(_text(_get(locale, "businessDomain", "business_domain")))
    return tuple(dict.fromkeys(value for value in values if value))


def _visible(*records: Mapping[str, Any] | None) -> tuple[bool, dict[str, Any]]:
    visible = True
    metadata: dict[str, Any] = {}
    for record in records:
        if not record:
            continue
        properties = _mapping(_get(record, "properties", default={}))
        for source in (record, properties):
            value = _get(source, "visible")
            if isinstance(value, bool):
                visible = visible and value
            roles = _get(source, "roles", "allowedRoles", "allowed_roles")
            if roles:
                metadata["allowedRoles"] = list(_clean_list(roles))
    metadata["visible"] = visible
    return visible, metadata


def _qualified_references(text: str, known_models: set[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    models: set[str] = set()
    columns: set[str] = set()
    known_by_normal = {_normalise(model): model for model in known_models}
    for model, column in _QUALIFIED_NAME.findall(text):
        canonical_model = known_by_normal.get(_normalise(model), model)
        if not known_models or _normalise(model) in known_by_normal:
            models.add(canonical_model)
            columns.add(f"{canonical_model}.{column}")
    return tuple(sorted(models)), tuple(sorted(columns))


def _semantic_references(text: str, known_models: set[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Find qualified columns and bare model identifiers in free-form text."""

    models, columns = _qualified_references(text, known_models)
    normal_tokens = set(_tokens(text))
    model_refs = set(models)
    for model in known_models:
        normal_model = _normalise(model)
        if normal_model in normal_tokens or (
            normal_model and re.search(rf"(?<![A-Za-z0-9_$-]){re.escape(normal_model)}(?![A-Za-z0-9_$-])", _normalise(text))
        ):
            model_refs.add(model)
    return tuple(sorted(model_refs)), columns


def _source_path(record: Mapping[str, Any], default: str) -> str:
    path = _get(record, "sourcePath", "source_path", "_source_path")
    normalized = _text(path).replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.startswith("../")
        or re.match(r"^[A-Za-z]:/", normalized)
    ):
        return default
    return normalized


def _make_document(
    *,
    identifier: str,
    kind: str,
    revision: str,
    text: str,
    model: str | None = None,
    field_name: str | None = None,
    language: str | None = None,
    source_path: str | None = None,
    referenced_models: Iterable[str] = (),
    referenced_columns: Iterable[str] = (),
    visibility: Mapping[str, Any] | bool | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SemanticDocument:
    return SemanticDocument(
        id=identifier,
        kind=kind,
        projectRevision=revision,
        model=model,
        field=field_name,
        text=" ".join(part.strip() for part in (text,) if part and part.strip()),
        language=language or _language(text),
        sourcePath=source_path,
        referencedModels=tuple(referenced_models),
        referencedColumns=tuple(referenced_columns),
        visibility=visibility,
        metadata=metadata or {},
    )


def _manifest_revision(manifest: Mapping[str, Any], project_revision: str | None) -> str:
    supplied = project_revision or _get(manifest, "projectRevision", "project_revision", "revision")
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()
    return _hash_payload(manifest)


def _model_documents(manifest: Mapping[str, Any], revision: str, locales: Mapping[str, Any]) -> list[SemanticDocument]:
    documents: list[SemanticDocument] = []
    models = _as_section(_get(manifest, "models", default=[]), "models")
    known_models = {_text(_get(model, "name")) for model in models if _text(_get(model, "name"))}
    for model_record in sorted(models, key=lambda item: _text(_get(item, "name"))):
        name = _text(_get(model_record, "name"))
        if not name:
            continue
        locale = _locale_record(locales, "models", name)
        visible, visibility = _visible(model_record, locale)
        description = _description(model_record, locale)
        columns = _as_section(_get(model_record, "columns", default=[]), f"model:{name}.columns")
        column_names = [
            f"{name}.{_text(_get(column, 'name'))}"
            for column in columns
            if _text(_get(column, "name"))
        ]
        properties = _mapping(_get(model_record, "properties", default={}))
        data_scope = _get(
            locale,
            "dataScope",
            "data_scope",
            default=_get(properties, "dataScope", "data_scope"),
        )
        model_text = " ".join(
            part
            for part in (
                f"Model {name}",
                *_localized(_get(locale, "displayName", "display_name")),
                *description,
                f"business domain {_text(_get(locale, 'businessDomain', 'business_domain'))}",
                f"primary key {_get(model_record, 'primaryKey', 'primary_key', default='')}",
                "columns " + ", ".join(column_names),
                _text(data_scope),
            )
            if part
        )
        documents.append(
            _make_document(
                identifier=f"model:{name}",
                kind="model",
                revision=revision,
                model=name,
                text=model_text,
                source_path=_source_path(model_record, f"models/{name}/metadata.yml"),
                referenced_models=(name,),
                referenced_columns=column_names,
                visibility=visibility,
                metadata={
                    "displayName": _json_value(_get(locale, "displayName", "display_name")),
                    "description": list(description),
                    "businessDomain": _text(_get(locale, "businessDomain", "business_domain")),
                    "dataScope": _json_value(data_scope),
                    "visible": visible,
                },
            )
        )
        for column in sorted(columns, key=lambda item: _text(_get(item, "name"))):
            column_name = _text(_get(column, "name"))
            if not column_name:
                continue
            column_locale = _mapping(_get(locale, "columns", default={}))
            column_locale = _mapping(column_locale.get(column_name))
            column_visible, column_visibility = _visible(model_record, column, column_locale)
            column_description = _description(column, column_locale)
            properties = _mapping(_get(column, "properties", default={}))
            accepted_values = _get(
                column_locale,
                "acceptedValues",
                "accepted_values",
                default=_get(
                    column,
                    "acceptedValues",
                    "accepted_values",
                    default=_get(properties, "acceptedValues", "accepted_values", default=[]),
                ),
            )
            grain = _get(column_locale, "grain", "granularity", default=_get(column, "grain", "granularity", default=_get(properties, "grain", "granularity")))
            time_basis = _get(column_locale, "timeBasis", "time_basis", default=_get(column, "timeBasis", "time_basis", default=_get(properties, "timeBasis", "time_basis")))
            data_range = _get(column_locale, "dataRange", "data_range", default=_get(column, "dataRange", "data_range", default=_get(properties, "dataRange", "data_range")))
            details = [
                f"Column {name}.{column_name}",
                _text(_get(column, "type")),
                *_localized(_get(column_locale, "displayName", "display_name")),
                *column_description,
                _text(_get(column_locale, "semanticRole", "semantic_role")),
                _text(_get(column_locale, "format")),
                _text(_get(column_locale, "unit")),
                _text(_get(column, "expression")),
                _text(_get(column, "relationship")),
                "accepted values " + ", ".join(str(v) for v in (accepted_values or [])),
                f"grain {_text(grain)}" if _text(grain) else "",
                f"time basis {_text(time_basis)}" if _text(time_basis) else "",
                f"data range {_text(data_range)}" if _text(data_range) else "",
            ]
            documents.append(
                _make_document(
                    identifier=f"column:{name}.{column_name}",
                    kind="column",
                    revision=revision,
                    model=name,
                    field_name=column_name,
                    text=" ".join(part for part in details if part),
                    source_path=_source_path(model_record, f"models/{name}/metadata.yml"),
                    referenced_models=(name,),
                    referenced_columns=(f"{name}.{column_name}",),
                    visibility=column_visibility,
                    metadata={
                        "type": _get(column, "type"),
                        "displayName": _json_value(_get(column_locale, "displayName", "display_name")),
                        "description": list(column_description),
                        "semanticRole": _get(column_locale, "semanticRole", "semantic_role", default=_get(column, "semanticRole", "semantic_role")),
                        "format": _get(column_locale, "format"),
                        "unit": _get(column_locale, "unit"),
                        "acceptedValues": _json_value(accepted_values),
                        "grain": _json_value(grain),
                        "timeBasis": _json_value(time_basis),
                        "dataRange": _json_value(data_range),
                        "visible": column_visible,
                    },
                )
            )
    return documents


def _relationship_documents(manifest: Mapping[str, Any], revision: str, locales: Mapping[str, Any]) -> list[SemanticDocument]:
    documents: list[SemanticDocument] = []
    relationships = _as_section(_get(manifest, "relationships", default=[]), "relationships")
    known_models = {
        _text(_get(model, "name"))
        for model in _as_section(_get(manifest, "models", default=[]), "models")
        if _text(_get(model, "name"))
    }
    for relationship in sorted(relationships, key=lambda item: _text(_get(item, "name"))):
        name = _text(_get(relationship, "name"))
        if not name:
            continue
        models = _clean_list(_get(relationship, "models", default=[]))
        condition = _text(_get(relationship, "condition"))
        refs_models, refs_columns = _qualified_references(condition, known_models)
        refs_models = tuple(sorted(set(refs_models) | set(models)))
        locale = _locale_record(locales, "relationships", name)
        visible, visibility = _visible(relationship, locale)
        description = _description(relationship, locale)
        text = " ".join(
            part
            for part in (
                f"Relationship {name}",
                "models " + ", ".join(models),
                _text(_get(relationship, "joinType", "join_type")),
                condition,
                *_localized(_get(locale, "displayName", "display_name")),
                *description,
            )
            if part
        )
        documents.append(
            _make_document(
                identifier=f"relationship:{name}",
                kind="relationship",
                revision=revision,
                model=models[0] if models else None,
                text=text,
                source_path=_source_path(relationship, "relationships.yml"),
                referenced_models=refs_models,
                referenced_columns=refs_columns,
                visibility=visibility,
                metadata={"joinType": _get(relationship, "joinType", "join_type"), "visible": visible},
            )
        )
    return documents


def _cube_documents(manifest: Mapping[str, Any], revision: str, locales: Mapping[str, Any]) -> list[SemanticDocument]:
    documents: list[SemanticDocument] = []
    cubes = _as_section(_get(manifest, "cubes", default=[]), "cubes")
    known_models = {
        _text(_get(model, "name"))
        for model in _as_section(_get(manifest, "models", default=[]), "models")
        if _text(_get(model, "name"))
    }
    for cube in sorted(cubes, key=lambda item: _text(_get(item, "name"))):
        name = _text(_get(cube, "name"))
        if not name:
            continue
        base = _text(_get(cube, "baseObject", "base_object"))
        locale = _locale_record(locales, "cubes", name)
        visible, visibility = _visible(cube, locale)
        cube_description = _description(cube, locale)
        measures = _as_section(_get(cube, "measures", default=[]), f"cube:{name}.measures")
        dimensions = _as_section(_get(cube, "dimensions", default=[]), f"cube:{name}.dimensions")
        time_dimensions = _as_section(_get(cube, "timeDimensions", "time_dimensions", default=[]), f"cube:{name}.timeDimensions")
        pieces = [f"Cube {name}", f"base object {base}"]
        pieces.extend(f"measure {_text(_get(item, 'name'))} {_text(_get(item, 'expression'))}" for item in measures)
        pieces.extend(f"dimension {_text(_get(item, 'name'))} {_text(_get(item, 'expression'))}" for item in dimensions)
        pieces.extend(f"time dimension {_text(_get(item, 'name'))} {_text(_get(item, 'expression'))}" for item in time_dimensions)
        pieces.extend(cube_description)
        documents.append(
            _make_document(
                identifier=f"cube:{name}",
                kind="cube",
                revision=revision,
                model=base or None,
                text=" ".join(piece for piece in pieces if piece),
                source_path=_source_path(cube, f"cubes/{name}/metadata.yml"),
                referenced_models=(base,) if base in known_models else (),
                visibility=visibility,
                metadata={
                    "baseObject": base,
                    "displayName": _json_value(_get(locale, "displayName", "display_name")),
                    "description": list(cube_description),
                    "visible": visible,
                },
            )
        )
        for kind, entries, prefix in (("metric", measures, "measure"), ("dimension", dimensions, "dimension"), ("time_dimension", time_dimensions, "timeDimension")):
            for entry in sorted(entries, key=lambda item: _text(_get(item, "name"))):
                entry_name = _text(_get(entry, "name"))
                if not entry_name:
                    continue
                entry_visible, entry_visibility = _visible(cube, entry)
                expression = _text(_get(entry, "expression"))
                entry_locales = _mapping(_get(locale, f"{prefix}s", default={}))
                entry_locale = _mapping(entry_locales.get(entry_name))
                entry_description = _description(entry, entry_locale)
                entry_properties = _mapping(_get(entry, "properties", default={}))
                grain = _get(entry_locale, "grain", "granularity", default=_get(entry, "grain", "granularity", default=_get(entry_properties, "grain", "granularity")))
                time_basis = _get(entry_locale, "timeBasis", "time_basis", default=_get(entry, "timeBasis", "time_basis", default=_get(entry_properties, "timeBasis", "time_basis")))
                data_range = _get(entry_locale, "dataRange", "data_range", default=_get(entry, "dataRange", "data_range", default=_get(entry_properties, "dataRange", "data_range")))
                entry_refs_models, entry_refs_columns = _qualified_references(expression, known_models)
                if base and base in known_models:
                    entry_refs_models = tuple(sorted(set(entry_refs_models) | {base}))
                documents.append(
                    _make_document(
                        identifier=f"{kind}:{name}.{entry_name}",
                        kind=kind,
                        revision=revision,
                        model=base or name,
                        field_name=entry_name,
                        text=" ".join(part for part in (
                            f"{prefix} {entry_name} in cube {name}",
                            _text(_get(entry, "type")),
                            expression,
                            *_localized(_get(entry_locale, "displayName", "display_name")),
                            *entry_description,
                            f"grain {_text(grain)}" if _text(grain) else "",
                            f"time basis {_text(time_basis)}" if _text(time_basis) else "",
                            f"data range {_text(data_range)}" if _text(data_range) else "",
                        ) if part),
                        source_path=_source_path(cube, f"cubes/{name}/metadata.yml"),
                        referenced_models=entry_refs_models,
                        referenced_columns=entry_refs_columns,
                        visibility=entry_visibility,
                        metadata={
                            "cube": name,
                            "expression": expression,
                            "type": _get(entry, "type"),
                            "displayName": _json_value(_get(entry_locale, "displayName", "display_name")),
                            "description": list(entry_description),
                            "grain": _json_value(grain),
                            "timeBasis": _json_value(time_basis),
                            "dataRange": _json_value(data_range),
                            "visible": entry_visible,
                        },
                    )
                )
    return documents


def _view_documents(manifest: Mapping[str, Any], revision: str) -> list[SemanticDocument]:
    documents: list[SemanticDocument] = []
    views = _as_section(_get(manifest, "views", default=[]), "views")
    known_models = {
        _text(_get(model, "name"))
        for model in _as_section(_get(manifest, "models", default=[]), "models")
        if _text(_get(model, "name"))
    }
    for view in sorted(views, key=lambda item: _text(_get(item, "name"))):
        name = _text(_get(view, "name"))
        if not name:
            continue
        statement = _text(_get(view, "statement", "sql"))
        refs_models, refs_columns = _semantic_references(statement, known_models)
        visible, visibility = _visible(view)
        documents.append(
            _make_document(
                identifier=f"view:{name}",
                kind="view",
                revision=revision,
                text=f"View {name} {statement}",
                language=_language(statement),
                source_path=_source_path(view, f"views/{name}/metadata.yml"),
                referenced_models=refs_models,
                referenced_columns=refs_columns,
                visibility=visibility,
                metadata={"statement": statement, "visible": visible},
            )
        )
    return documents


def _read_structured(path: Path) -> Mapping[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SemanticIndexError(f"cannot read semantic source {path.name}") from exc
    if _yaml is not None:
        try:
            value = _yaml.safe_load(raw) or {}
        except Exception as exc:  # yaml.YAMLError without importing yaml
            raise SemanticIndexError(f"invalid YAML semantic source {path.name}") from exc
    else:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise SemanticIndexError("YAML parsing is unavailable in this runtime") from exc
    if not isinstance(value, Mapping):
        raise SemanticIndexError(f"semantic source {path.name} must contain an object")
    return value


def _load_locales(project_path: Path) -> Mapping[str, Any]:
    for relative in ("semantic-console/locales.yml", "semantic-console/locales.yaml", "locales.yml", "locales.yaml"):
        path = project_path / relative
        if path.is_file():
            return _read_structured(path)
    return {}


def _load_rule_metadata(project_path: Path) -> Mapping[str, Any]:
    """Load the optional rule companion used during frontmatter migration."""

    for relative in (
        "semantic-console/rule-metadata.yml",
        "semantic-console/rule-metadata.yaml",
        "semantic-console/rule-metadata.json",
    ):
        path = project_path / relative
        if path.is_file():
            value = _read_structured(path)
            nested = _get(value, "ruleMetadata", "rule_metadata", "rules", default=value)
            if not isinstance(nested, Mapping):
                raise SemanticIndexError("rule metadata companion must contain an object")
            return nested
    return {}


def _read_frontmatter(path: Path) -> tuple[Mapping[str, Any], str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SemanticIndexError(f"cannot read semantic source {path.name}") from exc
    match = _FRONTMATTER.match(raw)
    if not match:
        return {}, raw
    header = match.group(1)
    if _yaml is not None:
        try:
            parsed = _yaml.safe_load(header) or {}
        except Exception as exc:
            raise SemanticIndexError(f"invalid front matter in {path.name}") from exc
    else:
        parsed = {}
        for line in header.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                parsed[key.strip()] = value.strip().strip("'\"")
    if not isinstance(parsed, Mapping):
        raise SemanticIndexError(f"front matter in {path.name} must be an object")
    return parsed, raw[match.end() :]


def _markdown_chunks(raw: str) -> list[tuple[str, str]]:
    lines = raw.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    if not headings:
        text = raw.strip()
        return [("document", text)] if text else []
    chunks: list[tuple[str, str]] = []
    preamble = "\n".join(lines[: headings[0][0]]).strip()
    if preamble:
        chunks.append(("document", preamble))
    for pos, (start, _, heading) in enumerate(headings):
        end = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        if body:
            chunks.append((heading, body))
    return chunks


def _atomic_rule_chunks(raw: str) -> list[tuple[str, str]]:
    """Split heading sections with bullet lists into stable atomic rules."""

    atomic: list[tuple[str, str]] = []
    for heading, chunk in _markdown_chunks(raw):
        lines = chunk.splitlines()
        if lines and _HEADING.match(lines[0]):
            lines = lines[1:]
        bullets: list[list[str]] = []
        current: list[str] | None = None
        for line in lines:
            if re.match(r"^\s*[-*]\s+", line):
                if current:
                    bullets.append(current)
                current = [re.sub(r"^\s*[-*]\s+", "", line).strip()]
            elif current is not None:
                if line.strip():
                    current.append(line.strip())
        if current:
            bullets.append(current)
        if len(bullets) <= 1:
            atomic.append((heading, chunk))
            continue
        for index, bullet in enumerate(bullets, 1):
            atomic.append((f"{heading}-{index:02d}", f"{heading}\n" + " ".join(bullet)))
    return atomic


def _slug(value: str) -> str:
    value = _normalise(value)
    value = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "-", value).strip("-")
    return value or "document"


def _knowledge_documents(
    project_path: Path,
    revision: str,
    known_models: set[str],
    companion_rule_metadata: Mapping[str, Any] | None = None,
) -> list[SemanticDocument]:
    documents: list[SemanticDocument] = []
    rules_root = project_path / "knowledge" / "rules"
    if rules_root.is_dir():
        for path in sorted(rules_root.rglob("*.md"), key=lambda candidate: candidate.as_posix()):
            frontmatter, body = _read_frontmatter(path)
            relative = path.relative_to(project_path).as_posix()
            base = path.stem
            for heading, chunk in _atomic_rule_chunks(body):
                slug = _slug(heading)
                identifier = f"rule:{base}#{slug}"
                companion = companion_rule_metadata or {}
                companion_candidate = companion.get(identifier)
                if companion_candidate is None:
                    companion_candidate = companion.get(heading)
                if companion_candidate is None:
                    companion_candidate = companion.get(slug)
                rule_metadata: dict[str, Any] = (
                    dict(companion_candidate)
                    if isinstance(companion_candidate, Mapping)
                    else {}
                )
                raw_rule_metadata = _get(frontmatter, "ruleMetadata", "rule_metadata", "rules", default={})
                if isinstance(raw_rule_metadata, Mapping):
                    candidate = raw_rule_metadata.get(heading)
                    if candidate is None:
                        candidate = raw_rule_metadata.get(slug)
                    if isinstance(candidate, Mapping):
                        # In-document metadata is the final authority when both
                        # migration companion and frontmatter declare a key.
                        rule_metadata.update(candidate)
                refs_models, refs_columns = _semantic_references(chunk, known_models)
                front_models = _clean_list(_get(rule_metadata, "models", "referencedModels", "referenced_models", default=_get(frontmatter, "models", "referencedModels", "referenced_models", default=[])))
                refs_models = tuple(sorted(set(refs_models) | set(front_models)))
                front_columns = _clean_list(_get(rule_metadata, "fields", "columns", "referencedColumns", "referenced_columns", default=_get(frontmatter, "fields", "columns", "referencedColumns", "referenced_columns", default=[])))
                refs_columns = tuple(sorted(set(refs_columns) | set(front_columns)))
                visible, visibility = _visible(frontmatter, rule_metadata)
                aliases = _clean_list(
                    _get(
                        rule_metadata,
                        "aliases",
                        "keywords",
                        default=_get(frontmatter, "aliases", "keywords", default=[]),
                    )
                )
                metadata = {
                    "ruleType": _get(rule_metadata, "rule_type", "ruleType", "type", default=_get(frontmatter, "rule_type", "ruleType", "type")),
                    "priority": _get(rule_metadata, "priority", default=_get(frontmatter, "priority")),
                    "mandatory": _get(rule_metadata, "mandatory", "required", default=_get(frontmatter, "mandatory", "required")),
                    "effectiveFrom": _get(rule_metadata, "effective_from", "effectiveFrom", default=_get(frontmatter, "effective_from", "effectiveFrom")),
                    "allowedRoles": list(_clean_list(_get(rule_metadata, "roles", "allowedRoles", default=_get(frontmatter, "roles", "allowedRoles", default=[])))),
                    "aliases": list(aliases),
                    "visible": visible,
                }
                documents.append(
                    _make_document(
                        identifier=identifier,
                        kind="rule",
                        revision=revision,
                        text=chunk,
                        language=_language(chunk),
                        source_path=relative,
                        referenced_models=refs_models,
                        referenced_columns=refs_columns,
                        visibility=visibility,
                        metadata=metadata,
                    )
                )

    sql_root = project_path / "knowledge" / "sql"
    if sql_root.is_dir():
        for path in sorted(sql_root.rglob("*.md"), key=lambda candidate: candidate.as_posix()):
            frontmatter, body = _read_frontmatter(path)
            relative = path.relative_to(project_path).as_posix()
            nl = _text(_get(frontmatter, "nl", "question", "prompt"))
            sql = _text(_get(frontmatter, "sql", "semanticSql", "semantic_sql"))
            chunk = "\n".join(part for part in (nl, sql, body.strip()) if part)
            refs_models, refs_columns = _semantic_references(chunk, known_models)
            front_models = _clean_list(_get(frontmatter, "models", "referencedModels", "referenced_models", default=[]))
            refs_models = tuple(sorted(set(refs_models) | set(front_models)))
            front_columns = _clean_list(_get(frontmatter, "fields", "columns", "referencedColumns", "referenced_columns", default=[]))
            refs_columns = tuple(sorted(set(refs_columns) | set(front_columns)))
            visible, visibility = _visible(frontmatter)
            metadata = {
                "question": nl or path.stem,
                "sql": sql,
                "labels": list(_clean_list(_get(frontmatter, "labels", "tags", default=[]))),
                "dataSource": _get(frontmatter, "dataSource", "data_source"),
                "reviewStatus": _get(frontmatter, "reviewStatus", "review_status", "status"),
                "roles": list(_clean_list(_get(frontmatter, "roles", "allowedRoles", default=[]))),
                "version": _get(frontmatter, "version"),
                "visible": visible,
            }
            documents.append(
                _make_document(
                    identifier=f"sql_example:{relative[:-3]}",
                    kind="sql_example",
                    revision=revision,
                    text=chunk or path.stem,
                    language=_text(_get(frontmatter, "language")) or _language(nl, body),
                    source_path=relative,
                    referenced_models=refs_models,
                    referenced_columns=refs_columns,
                    visibility=visibility,
                    metadata=metadata,
                )
            )
    return documents


def _inline_knowledge_documents(manifest: Mapping[str, Any], revision: str) -> list[SemanticDocument]:
    """Accept already-materialised knowledge when a caller has no project dir."""

    documents: list[SemanticDocument] = []
    for group, kind, prefix in (("knowledge", "rule", "rule"), ("rules", "rule", "rule"), ("sqlExamples", "sql_example", "sql_example"), ("sqlHistory", "sql_example", "sql_example")):
        entries = _get(manifest, group, default=[])
        if not isinstance(entries, list):
            continue
        for index, item in enumerate(entries):
            if not isinstance(item, Mapping):
                continue
            identifier = _text(_get(item, "id")) or f"{prefix}:{index}"
            if not identifier.startswith(f"{prefix}:"):
                identifier = f"{prefix}:{identifier}"
            text = _text(_get(item, "text", "content", "description", "sql"))
            refs_models = _clean_list(_get(item, "referencedModels", "referenced_models", "models", default=[]))
            refs_columns = _clean_list(_get(item, "referencedColumns", "referenced_columns", "fields", default=[]))
            documents.append(
                _make_document(
                    identifier=identifier,
                    kind=kind,
                    revision=revision,
                    text=text or identifier,
                    language=_text(_get(item, "language")) or _language(text),
                    source_path=_source_path(item, "") or None,
                    referenced_models=refs_models,
                    referenced_columns=refs_columns,
                    visibility=_get(item, "visibility"),
                    metadata=item,
                )
            )
    return documents


def build_semantic_documents(
    manifest: Mapping[str, Any],
    project_path: str | Path | None = None,
    *,
    project_revision: str | None = None,
    locales: Mapping[str, Any] | None = None,
    rule_metadata: Mapping[str, Any] | None = None,
) -> tuple[SemanticDocument, ...]:
    """Build stable documents from an MDL manifest and companion knowledge.

    ``project_path`` is optional: callers with an already materialised Wren
    manifest can still index schema and inline ``knowledge``/``sqlExamples``.
    When supplied, ``semantic-console/locales.yml``, ``knowledge/rules`` and
    ``knowledge/sql`` are read using forward-slash source paths.
    """

    if not isinstance(manifest, Mapping):
        raise SemanticIndexError("manifest must be an object")
    root: Path | None = None
    if project_path is not None:
        root = Path(project_path).expanduser().resolve()
        if not root.is_dir():
            raise SemanticIndexError("project directory is unavailable")
    revision = _manifest_revision(manifest, project_revision)
    if root is not None and project_revision is None and not _text(
        _get(manifest, "projectRevision", "project_revision", "revision")
    ):
        revision = _compute_project_revision(root)
    locale_data = dict(locales) if isinstance(locales, Mapping) else (_load_locales(root) if root else {})
    rule_metadata_data = (
        dict(rule_metadata)
        if isinstance(rule_metadata, Mapping)
        else (_load_rule_metadata(root) if root else {})
    )
    documents: list[SemanticDocument] = []
    documents.extend(_model_documents(manifest, revision, locale_data))
    documents.extend(_relationship_documents(manifest, revision, locale_data))
    documents.extend(_cube_documents(manifest, revision, locale_data))
    documents.extend(_view_documents(manifest, revision))
    documents.extend(_inline_knowledge_documents(manifest, revision))
    if root:
        known_models = {
            _text(_get(model, "name"))
            for model in _as_section(_get(manifest, "models", default=[]), "models")
            if _text(_get(model, "name"))
        }
        documents.extend(_knowledge_documents(root, revision, known_models, rule_metadata_data))
    deduped: dict[str, SemanticDocument] = {}
    for document in documents:
        deduped[document.id] = document
    return tuple(sorted(deduped.values(), key=lambda item: (_KIND_ORDER.get(item.kind, 99), item.id)))


def load_manifest(project_path: str | Path) -> JSON:
    """Load the Wren manifest already produced in ``target/mdl.json``."""

    root = Path(project_path).expanduser().resolve()
    for relative in ("target/mdl.json", "mdl.json"):
        path = root / relative
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SemanticIndexError(f"invalid Wren manifest {relative}") from exc
            if not isinstance(value, Mapping):
                raise SemanticIndexError("Wren manifest must contain an object")
            return dict(value)
    raise SemanticIndexError("Wren manifest target/mdl.json was not found")


def build_project_documents(
    project_path: str | Path,
    manifest: Mapping[str, Any] | None = None,
    *,
    project_revision: str | None = None,
    locales: Mapping[str, Any] | None = None,
) -> tuple[SemanticDocument, ...]:
    """Load a project's target manifest and build its semantic documents."""

    root = Path(project_path).expanduser().resolve()
    return build_semantic_documents(
        manifest if manifest is not None else load_manifest(root),
        root,
        project_revision=project_revision or _compute_project_revision(root),
        locales=locales,
    )


def _compute_project_revision(project_path: str | Path) -> str:
    """Compute the same path/content based revision style as the sidecar."""

    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        raise SemanticIndexError("project directory is unavailable")
    digest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    try:
        for candidate in root.rglob("*"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(root)
            if any(part in _REVISION_IGNORED_DIRS for part in relative.parts):
                continue
            files.append((relative.as_posix(), candidate))
        for name, candidate in sorted(files):
            name_bytes = name.encode("utf-8")
            digest.update(len(name_bytes).to_bytes(4, "big"))
            digest.update(name_bytes)
            data = candidate.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise SemanticIndexError("project revision could not be computed") from exc
    return f"sha256:{digest.hexdigest()}"


def project_revision(project_path: str | Path) -> str:
    """Compute the same path/content based revision style as the sidecar."""

    return _compute_project_revision(project_path)


# Friendly aliases for callers migrating from an earlier prototype name.
build_documents = build_semantic_documents
documents_from_project = build_project_documents
compute_project_revision = project_revision
InMemoryLexicalIndex = InMemorySemanticIndex


__all__ = [
    "SEMANTIC_DOCUMENT_KINDS",
    "SemanticIndexError",
    "SemanticDocument",
    "IndexStatus",
    "SearchHit",
    "SearchResponse",
    "SemanticIndex",
    "VectorSemanticIndex",
    "VectorIndex",
    "InMemorySemanticIndex",
    "MemorySemanticIndex",
    "InMemoryLexicalIndex",
    "LexicalSemanticIndex",
    "build_semantic_documents",
    "build_documents",
    "build_project_documents",
    "documents_from_project",
    "load_manifest",
    "project_revision",
    "compute_project_revision",
]
