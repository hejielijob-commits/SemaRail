# 0010: Revisioned, policy-safe semantic retrieval

## Status

Accepted.

## Context

Schema version 1 context returns the complete authorized model projection,
concatenates all business rules, and recalls reviewed SQL through Wren's
optional memory backend.  The default Core installation does not include the
memory extra, so reviewed SQL falls back to an ASCII-oriented token overlap
search and schema context is not selectively retrieved.  Restricted policies
must currently remove every unstructured rule and SQL example because those
records do not declare the models and columns they reference.

That behavior is safe, but it gives multilingual questions poor recall, makes
context grow with the project, and prevents useful knowledge from reaching
restricted subjects.  An index also cannot be allowed to outlive the published
MDL revision from which it was derived.

## Decision

SemaRail owns a version-two semantic retrieval contract while preserving the
version-one contract for compatibility.

MDL project files remain the only source of truth.  A build projects models,
columns, relationships, cubes and metrics, rules, reviewed SQL, and views into
immutable `SemanticDocument` records.  Every record carries a stable identity,
the published project revision, its source, referenced models and columns,
visibility metadata, language, and a content hash.  Search indexes are derived
artifacts and never become an authoring store.
The content hash deliberately excludes the revision envelope. A staged build
reuses a compatible active vector only when stable ID and content hash both
match; changed/new records are embedded, removed records disappear, and a
partial refresh is never activated.

Retrieval is hybrid and deterministic at its boundaries:

1. compile the subject policy and exclude ineligible documents before scoring;
2. exact technical and business-name matches;
3. Unicode-aware lexical retrieval;
4. optional multilingual vector retrieval;
5. reciprocal-rank fusion and explicit rule binding;
6. bounded relationship-graph expansion over the already-visible graph when
   the query is classified as cross-model;
7. repeat structural authorization filtering at the response boundary;
8. per-kind quotas and a deterministic context budget.

The public result exposes the retrieval methods and bounded scores used to
select a record, but never exposes raw embedding vectors, vector distances,
local index paths, or credentials.  An unavailable vector provider is an
explicit degraded index status rather than an invisible change in behavior.
The Context boundary contains semantic model names only. Physical
`tableReference` values remain available to the internal planner but are not
serialized into Context schema records or restricted knowledge text.

Authorization is fail closed.  Models, columns, and relationships use their
structured MDL identities.  A rule or SQL example is eligible for a restricted
subject only when all of its referenced models and columns are declared and
authorized.  Unscoped arbitrary text remains available only to an unrestricted
policy.  Retrieval may oversample before filtering, but filtering always occurs
before budgeting and serialization.

An index is usable only when its project revision and embedding configuration
match the published project and active retrieval configuration.  Publication
builds a staged index, validates it, publishes the MDL, and atomically activates
the matching index revision.  Failure leaves the previous MDL/index pair active.
Rollback activates the snapshot's matching index.  Missing or stale indexes are
reported and may use the lexical projection built from the current MDL; they
must never silently serve documents from another revision.

## Compatibility and rollout

- Context schema version 1 keeps its existing request and response shape.
- Context schema version 2 is selected explicitly until retrieval and security
  benchmarks pass and it becomes the default.
- Unknown schema versions and unknown budget fields fail closed.
- The vector implementation is accessed through SemaRail's `SemanticIndex`
  interface.  LanceDB and sentence-transformers are replaceable provider
  details, not transport or project-format dependencies.
- Shadow evaluation compares version 1 and version 2 without changing the
  answer path.  A feature switch permits an immediate version 1 fallback.

## Verification gates

The fixed HR corpus records expected models, columns, relationships, rules, and
SQL examples.  Reports separate Chinese and English and include Recall@K, MRR,
NDCG, context bytes and estimated tokens, p50/p95 retrieval latency, and policy
leaks.  Release requires zero policy leaks and revision-mismatch tests in
addition to retrieval-quality targets; query correctness alone is not evidence
that retrieval works.

## Consequences

Semantic publication performs more work and retains revisioned derived state.
In exchange, context size no longer grows directly with the full project,
Chinese and English retrieval can be measured independently, restricted users
can receive provably safe knowledge, and every recalled item can be traced to a
published MDL revision and source file.
