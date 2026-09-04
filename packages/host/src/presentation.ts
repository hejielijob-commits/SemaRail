import {
  parseDataQueryPresentation,
  type DataQueryColumn,
  type DataQueryPresentation,
  type DataQueryPresentationV2,
  type DataQueryStats,
  type DataQueryStatsV2,
  type JsonSafeScalar,
} from '@hejielijob/dsh-wren-data-agent-contract'

/** Text content emitted to the model for one bounded inline result. */
export interface DataQueryInlineModelResult {
  readonly schemaVersion: 1 | 2
  readonly queryId: string
  readonly status: 'success'
  readonly delivery?: 'inline'
  readonly summary: string
  readonly columns: readonly DataQueryColumn[]
  readonly previewRows: readonly Readonly<Record<string, JsonSafeScalar>>[]
  readonly stats: DataQueryStats
}

/** Text content emitted to the model for an artifact result. */
export interface DataQueryArtifactModelResult {
  readonly schemaVersion: 2
  readonly queryId: string
  readonly status: 'success'
  readonly delivery: 'artifact'
  readonly summary: string
  readonly columns: readonly DataQueryColumn[]
  readonly previewRows: readonly Readonly<Record<string, JsonSafeScalar>>[]
  readonly previewHint: string
  readonly stats: DataQueryStatsV2
  readonly downloadUrl: string
  readonly expiresAt: string
}

/** Text content emitted to the model for a failed result. */
export interface DataQueryErrorModelResult {
  readonly schemaVersion: 1 | 2
  readonly queryId: string
  readonly status: 'error'
  readonly summary: string
  readonly error: {
    readonly code: string
    readonly retryable: boolean
  }
}

/** Model-facing data query projection; never includes SQL or full artifact bytes. */
export type DataQueryModelResult =
  | DataQueryInlineModelResult
  | DataQueryArtifactModelResult
  | DataQueryErrorModelResult

function errorProjection(result: Extract<DataQueryPresentation, { status: 'error' }>): DataQueryErrorModelResult {
  return {
    schemaVersion: result.schemaVersion,
    queryId: result.queryId,
    status: 'error',
    summary: `SemaRail query failed (${result.error.code}).`,
    error: { code: result.error.code, retryable: result.error.retryable },
  }
}

function inlineProjection(result: Extract<DataQueryPresentation, { status: 'success' }>): DataQueryInlineModelResult {
  const delivery = result.schemaVersion === 2 && result.delivery === 'inline' ? 'inline' : undefined
  return {
    schemaVersion: result.schemaVersion,
    queryId: result.queryId,
    status: 'success',
    ...(delivery === undefined ? {} : { delivery }),
    summary: `SemaRail query returned ${result.stats.returnedRows} row(s).`,
    columns: result.columns,
    previewRows: result.previewRows,
    stats: result.stats,
  }
}

function artifactProjection(result: DataQueryPresentationV2 & { status: 'success'; delivery: 'artifact' }): DataQueryArtifactModelResult {
  const previewedRows = result.stats.previewedRows
  const totalRows = result.artifact.rowCount
  return {
    schemaVersion: 2,
    queryId: result.queryId,
    status: 'success',
    delivery: 'artifact',
    summary: `SemaRail query returned ${totalRows} row(s) as a CSV artifact.`,
    columns: result.columns,
    // The v2 contract caps this array at 20 rows before it reaches this
    // projector. It is deliberately never replaced with artifact contents.
    previewRows: result.previewRows,
    previewHint: `Only the first ${previewedRows} row(s) are shown here. Download the CSV to local storage and analyze it with DuckDB, Python/pandas, or another local tool; do not paste the full CSV into the chat context.`,
    stats: result.stats,
    downloadUrl: result.artifact.downloadUrl,
    expiresAt: result.artifact.expiresAt,
  }
}

/** Project a parsed presentation into the bounded model-facing shape. */
export function projectDataQueryForModel(value: unknown): DataQueryModelResult {
  const result = parseDataQueryPresentation(value)
  if (result.status === 'error') return errorProjection(result)
  if (result.schemaVersion === 2 && result.delivery === 'artifact') return artifactProjection(result)
  return inlineProjection(result)
}

/** Render the bounded model-facing projection as JSON text for Harness. */
export function renderDataQueryResult(value: unknown): Array<{ type: 'text'; text: string }> {
  return [{ type: 'text', text: JSON.stringify(projectDataQueryForModel(value)) }]
}
