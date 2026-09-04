# 0006: Bounded query results and temporary CSV artifacts

Status: accepted on 2026-09-04.

## Context

Putting every database row directly into an MCP tool result consumes the
Agent's context window, makes durable replay payloads large, and can expose more
data to the model than it needs. Pagination still requires the Agent to read
every page into context and is therefore not the default answer for analytical
results that the Agent can process locally.

## Decision

SemaRail Core owns a versioned `DataQueryPresentation` v2 delivery contract.
The Sidecar continues to execute at most 500 rows and chooses between two
delivery modes while it fetches:

- `inline`: no more than 50 rows and no more than 128 KiB of compact UTF-8 JSON.
- `artifact`: a maximum 20-row preview plus a streamed UTF-8 CSV, capped at
  16 MiB. Artifact results do not include a chart.

The Sidecar writes into a Core-issued reservation using a generated filename
and atomic rename. It returns only attested metadata; local paths and download
tokens never cross the Sidecar protocol. Core hashes the random token, exposes
an attachment download route over both Core HTTP and authenticated remote MCP,
sets `Cache-Control: no-store`, and deletes expired or failed output.

The URL is valid for 15 minutes at most. Every download revalidates that the
issuing subject and credential remain active and that the active datasource and
policy-version set are unchanged. A mismatch returns a terminal response and
invalidates the artifact. Audit records contain metadata, never the token, URL,
SQL text, result rows, or CSV body.

The Host sends only bounded stats, preview rows, download URL, and expiry to the
Agent. The Client renders the full metadata and a download action. Existing v1
tool-event payloads remain parseable for replay.

## Consequences

- Agents can download the CSV to local storage and use their own code or
  DuckDB without placing the full file in the conversation context.
- A download URL is a short-lived bearer capability and must be protected like
  a secret while valid.
- The first release intentionally does not provide Parquet, page-by-page result
  reading, resumable download, or exports beyond the existing 500-row limit.
- A future bulk-export service must use a separate authorization, storage,
  quota, and audit design rather than silently raising these limits.
