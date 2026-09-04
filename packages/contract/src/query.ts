import {
  _boolean,
  _enum,
  _fail,
  _integer,
  _keys,
  _number,
  _optional,
  _record,
  _required,
  _string,
  _version,
  DATA_QUERY_PRESENTATION_VERSION,
  isJsonSafeScalar,
  MAX_ARTIFACT_BYTES,
  MAX_ARTIFACT_PREVIEW_ROWS,
  MAX_INLINE_PREVIEW_BYTES,
  MAX_INLINE_PREVIEW_ROWS,
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_ROWS,
  MAX_QUERY_ROWS,
  SCHEMA_VERSION,
  type JsonSafeScalar,
  type JsonSchema,
} from './json.js'
import { ERROR_JSON_SCHEMA, _parseError, type DataAgentError } from './errors.js'
import { CHART_SPEC_V1_JSON_SCHEMA, parseChartSpecV1, type ChartSpecV1 } from './chart.js'
import { parseSqlHistoryReference, type SqlHistoryReference } from './context.js'
import { _schema, type ContractSchema } from './schema.js'

/** Chart preference supplied to `data_query`. */
export type ChartIntent = 'auto' | 'table' | 'line' | 'bar' | 'pie'

/** Input to the Host `data_query` tool. */
export interface DataQueryInput {
  readonly question: string
  readonly semanticSql: string
  readonly chartIntent?: ChartIntent
}

/** Result column shown in a presentation. */
export interface DataQueryColumn {
  readonly name: string
  readonly type: string
  readonly semanticRole: 'dimension' | 'measure'
}

/** Query counters persisted by the v1 presentation. */
export interface DataQueryStatsV1 {
  /** Total rows represented by the bounded query. */
  readonly returnedRows: number
  readonly durationMs: number
  /** Whether the preview omitted one or more returned rows. */
  readonly truncated: boolean
}

/** Query counters persisted by the v2 presentation. */
export interface DataQueryStatsV2 {
  /** Total rows represented by the bounded query. */
  readonly returnedRows: number
  readonly durationMs: number
  /** Whether the preview omitted one or more returned rows. */
  readonly truncated: boolean
  /** Number of rows retained in `previewRows`. */
  readonly previewedRows: number
}

/** Query counters accepted by either presentation version. */
export type DataQueryStats = DataQueryStatsV1 | DataQueryStatsV2

/** CSV artifact metadata for a v2 large-result presentation. */
export interface DataQueryArtifact {
  /** Opaque artifact identifier; it is not a filesystem path. */
  readonly id: string
  readonly format: 'csv'
  /** Download filename shown by the Client. */
  readonly fileName: string
  /** Total rows represented by the CSV artifact. */
  readonly rowCount: number
  /** Exact artifact byte size. */
  readonly sizeBytes: number
  /** SHA-256 digest of the artifact bytes. */
  readonly sha256: string
  /** ISO-8601 expiration timestamp. */
  readonly expiresAt: string
  /** Absolute HTTP(S) download URL; it may contain a short-lived access token. */
  readonly downloadUrl: string
}

/** Successful result presentation persisted in `tool/result.meta` (v1). */
export interface DataQuerySuccessPresentationV1 {
  readonly schemaVersion: typeof SCHEMA_VERSION
  readonly queryId: string
  readonly status: 'success'
  readonly semanticSql: string
  readonly question?: string
  readonly sqlHistory?: readonly SqlHistoryReference[]
  readonly nativeSql?: string
  readonly columns: readonly DataQueryColumn[]
  readonly previewRows: readonly Readonly<Record<string, JsonSafeScalar>>[]
  readonly chart?: ChartSpecV1
  readonly stats: DataQueryStatsV1
}

/** Failed result presentation persisted in `tool/result.meta` (v1). */
export interface DataQueryErrorPresentationV1 {
  readonly schemaVersion: typeof SCHEMA_VERSION
  readonly queryId: string
  readonly status: 'error'
  readonly semanticSql: string
  readonly question?: string
  readonly sqlHistory?: readonly SqlHistoryReference[]
  readonly nativeSql?: string
  readonly columns: readonly DataQueryColumn[]
  readonly previewRows: readonly Readonly<Record<string, JsonSafeScalar>>[]
  readonly stats: DataQueryStatsV1
  readonly error: DataAgentError
}

/** Union of successful and failed v1 query presentations. */
export type DataQueryPresentationV1 = DataQuerySuccessPresentationV1 | DataQueryErrorPresentationV1

interface DataQuerySuccessPresentationV2Base {
  readonly schemaVersion: typeof DATA_QUERY_PRESENTATION_VERSION
  readonly queryId: string
  readonly status: 'success'
  readonly semanticSql: string
  readonly question?: string
  readonly sqlHistory?: readonly SqlHistoryReference[]
  readonly nativeSql?: string
  readonly columns: readonly DataQueryColumn[]
  readonly previewRows: readonly Readonly<Record<string, JsonSafeScalar>>[]
  readonly stats: DataQueryStatsV2
}

/** Successful small-result presentation in the v2 inline delivery mode. */
export interface DataQueryInlineSuccessPresentationV2 extends DataQuerySuccessPresentationV2Base {
  readonly delivery: 'inline'
  readonly artifact?: never
  readonly chart?: ChartSpecV1
}

/** Successful large-result presentation in the v2 artifact delivery mode. */
export interface DataQueryArtifactSuccessPresentationV2 extends DataQuerySuccessPresentationV2Base {
  readonly delivery: 'artifact'
  readonly artifact: DataQueryArtifact
  /** Artifact previews are table-only; a chart could imply the full CSV is in context. */
  readonly chart?: never
}

/** Successful v2 result presentation. */
export type DataQuerySuccessPresentationV2 =
  | DataQueryInlineSuccessPresentationV2
  | DataQueryArtifactSuccessPresentationV2

/** Failed result presentation persisted in `tool/result.meta` (v2). */
export interface DataQueryErrorPresentationV2 {
  readonly schemaVersion: typeof DATA_QUERY_PRESENTATION_VERSION
  readonly queryId: string
  readonly status: 'error'
  readonly semanticSql: string
  readonly question?: string
  readonly sqlHistory?: readonly SqlHistoryReference[]
  readonly nativeSql?: string
  readonly columns: readonly DataQueryColumn[]
  readonly previewRows: readonly Readonly<Record<string, JsonSafeScalar>>[]
  readonly chart?: never
  readonly delivery?: never
  readonly artifact?: never
  readonly stats: DataQueryStatsV2
  readonly error: DataAgentError
}

/** Union of successful and failed v2 query presentations. */
export type DataQueryPresentationV2 = DataQuerySuccessPresentationV2 | DataQueryErrorPresentationV2

/** Backwards-compatible success alias spanning both presentation versions. */
export type DataQuerySuccessPresentation = DataQuerySuccessPresentationV1 | DataQuerySuccessPresentationV2

/** Backwards-compatible error alias spanning both presentation versions. */
export type DataQueryErrorPresentation = DataQueryErrorPresentationV1 | DataQueryErrorPresentationV2

/** Union of v1 and v2 query presentations accepted at the boundary. */
export type DataQueryPresentation = DataQueryPresentationV1 | DataQueryPresentationV2

/** Alias for consumers that call the presentation a result. */
export type DataQueryResult = DataQueryPresentation

/** Parse DataQueryInput. */
export function parseDataQueryInput(value: unknown): DataQueryInput {
  const object = _record(value, 'dataQueryInput')
  _keys(object, ['question', 'semanticSql', 'chartIntent'], 'dataQueryInput')
  const chartIntent = _optional(object, 'chartIntent')
  return {
    question: _string(_required(object, 'question', 'dataQueryInput'), 'dataQueryInput.question', 1, 16_000),
    semanticSql: _string(_required(object, 'semanticSql', 'dataQueryInput'), 'dataQueryInput.semanticSql', 1, 64_000),
    ...(chartIntent === undefined ? {} : { chartIntent: _enum(chartIntent, ['auto', 'table', 'line', 'bar', 'pie'] as const, 'dataQueryInput.chartIntent') }),
  }
}

function parseColumn(value: unknown, path: string): DataQueryColumn {
  const object = _record(value, path)
  _keys(object, ['name', 'type', 'semanticRole'], path)
  return {
    name: _string(_required(object, 'name', path), `${path}.name`, 1, 256),
    type: _string(_required(object, 'type', path), `${path}.type`, 1, 128),
    semanticRole: _enum(_required(object, 'semanticRole', path), ['dimension', 'measure'] as const, `${path}.semanticRole`),
  }
}

function parseStatsV1(value: unknown, path: string): DataQueryStatsV1 {
  const object = _record(value, path)
  _keys(object, ['returnedRows', 'durationMs', 'truncated'], path)
  const durationMs = _number(_required(object, 'durationMs', path), `${path}.durationMs`)
  if (durationMs < 0) _fail(`${path}.durationMs`, 'must be non-negative')
  return {
    returnedRows: _integer(_required(object, 'returnedRows', path), `${path}.returnedRows`, MAX_QUERY_ROWS),
    durationMs,
    truncated: _boolean(_required(object, 'truncated', path), `${path}.truncated`),
  }
}

function parseStatsV2(value: unknown, path: string, maxPreviewRows: number = MAX_PREVIEW_ROWS): DataQueryStatsV2 {
  const object = _record(value, path)
  _keys(object, ['returnedRows', 'durationMs', 'truncated', 'previewedRows'], path)
  const durationMs = _number(_required(object, 'durationMs', path), `${path}.durationMs`)
  if (durationMs < 0) _fail(`${path}.durationMs`, 'must be non-negative')
  return {
    returnedRows: _integer(_required(object, 'returnedRows', path), `${path}.returnedRows`, MAX_QUERY_ROWS),
    durationMs,
    truncated: _boolean(_required(object, 'truncated', path), `${path}.truncated`),
    previewedRows: _integer(_required(object, 'previewedRows', path), `${path}.previewedRows`, maxPreviewRows),
  }
}

function requiresString(type: string): boolean {
  return /^(BIGINT|INT64|LONG|DECIMAL|NUMERIC|DATE|TIME|TIMESTAMP|DATETIME)(?:\b|\()/u.test(type.trim().toUpperCase())
}

function parsePreviewRows(
  value: unknown,
  columns: readonly DataQueryColumn[],
  maxRows: number = MAX_PREVIEW_ROWS,
  maxBytes: number = MAX_PREVIEW_BYTES,
  path = 'dataQueryPresentation.previewRows',
): Readonly<Record<string, JsonSafeScalar>>[] {
  if (!Array.isArray(value)) _fail(path, 'expected an array')
  if (value.length > maxRows) _fail(path, `must contain at most ${maxRows} row(s)`)
  const names = new Set(columns.map(column => column.name))
  const rows = value.map((row, rowIndex) => {
    const object = _record(row, `${path}[${rowIndex}]`)
    for (const key of Object.keys(object)) if (!names.has(key)) _fail(`${path}[${rowIndex}]`, `unknown column ${JSON.stringify(key)}`)
    const parsed: Record<string, JsonSafeScalar> = {}
    for (const column of columns) {
      if (!Object.prototype.hasOwnProperty.call(object, column.name)) _fail(`${path}[${rowIndex}].${column.name}`, 'field is required')
      const scalar = object[column.name]
      if (!isJsonSafeScalar(scalar)) _fail(`${path}[${rowIndex}].${column.name}`, 'expected a JSON-safe scalar')
      if (scalar !== null && requiresString(column.type) && typeof scalar !== 'string') _fail(`${path}[${rowIndex}].${column.name}`, `${column.type} values must be exact strings`)
      parsed[column.name] = scalar
    }
    return parsed
  })
  if (new TextEncoder().encode(JSON.stringify(rows)).byteLength > maxBytes) _fail(path, `UTF-8 JSON size must be at most ${maxBytes} bytes`)
  return rows
}

function parseCommonPresentationFields(
  object: Record<string, unknown>,
  parsedColumns: readonly DataQueryColumn[],
  previewRows: readonly Readonly<Record<string, JsonSafeScalar>>[],
  path = 'dataQueryPresentation',
) {
  const nativeSql = _optional(object, 'nativeSql')
  const question = _optional(object, 'question')
  const sqlHistory = _optional(object, 'sqlHistory')
  if (sqlHistory !== undefined && !Array.isArray(sqlHistory)) _fail(`${path}.sqlHistory`, 'expected an array')
  if (Array.isArray(sqlHistory) && sqlHistory.length > 5) _fail(`${path}.sqlHistory`, 'must contain at most 5 item(s)')
  return {
    queryId: _string(_required(object, 'queryId', path), `${path}.queryId`, 1, 128),
    semanticSql: _string(_required(object, 'semanticSql', path), `${path}.semanticSql`, 1, 64_000),
    ...(question === undefined ? {} : { question: _string(question, `${path}.question`, 1, 16_000) }),
    ...(sqlHistory === undefined ? {} : { sqlHistory: sqlHistory.map((item, index) => parseSqlHistoryReference(item, `${path}.sqlHistory[${index}]`)) }),
    ...(nativeSql === undefined ? {} : { nativeSql: _string(nativeSql, `${path}.nativeSql`, 1, 64_000) }),
    columns: parsedColumns,
    previewRows,
  }
}

function parseColumnsAndPreview(
  object: Record<string, unknown>,
  maxPreviewRows: number = MAX_PREVIEW_ROWS,
  maxPreviewBytes: number = MAX_PREVIEW_BYTES,
) {
  const columns = _required(object, 'columns', 'dataQueryPresentation')
  if (!Array.isArray(columns)) _fail('dataQueryPresentation.columns', 'expected an array')
  const parsedColumns = columns.map((column, index) => parseColumn(column, `dataQueryPresentation.columns[${index}]`))
  const previewRows = parsePreviewRows(_required(object, 'previewRows', 'dataQueryPresentation'), parsedColumns, maxPreviewRows, maxPreviewBytes)
  return { parsedColumns, previewRows }
}

/** Parse a v1 DataQueryPresentation. */
export function parseDataQueryPresentationV1(value: unknown): DataQueryPresentationV1 {
  const object = _record(value, 'dataQueryPresentation')
  _keys(object, ['schemaVersion', 'queryId', 'status', 'semanticSql', 'question', 'sqlHistory', 'nativeSql', 'columns', 'previewRows', 'chart', 'stats', 'error'], 'dataQueryPresentation')
  _version(_required(object, 'schemaVersion', 'dataQueryPresentation'), SCHEMA_VERSION, 'dataQueryPresentation.schemaVersion')
  const status = _enum(_required(object, 'status', 'dataQueryPresentation'), ['success', 'error'] as const, 'dataQueryPresentation.status')
  const { parsedColumns, previewRows } = parseColumnsAndPreview(object)
  const stats = parseStatsV1(_required(object, 'stats', 'dataQueryPresentation'), 'dataQueryPresentation.stats')
  if (previewRows.length > stats.returnedRows) _fail('dataQueryPresentation.previewRows', 'cannot contain more rows than returnedRows')
  if (!stats.truncated && previewRows.length !== stats.returnedRows) _fail('dataQueryPresentation.stats.truncated', 'must be true when preview rows omit returned rows')
  const base = {
    schemaVersion: SCHEMA_VERSION,
    ...parseCommonPresentationFields(object, parsedColumns, previewRows),
    stats,
  }
  const chart = _optional(object, 'chart')
  const error = _optional(object, 'error')
  if (status === 'success') {
    if (error !== undefined) _fail('dataQueryPresentation.error', 'must be omitted for success')
    return { ...base, status: 'success', ...(chart === undefined ? {} : { chart: parseChartSpecV1(chart) }) }
  }
  if (chart !== undefined) _fail('dataQueryPresentation.chart', 'must be omitted for an error')
  if (error === undefined) _fail('dataQueryPresentation.error', 'field is required for an error')
  return { ...base, status: 'error', error: _parseError(error, 'dataQueryPresentation.error') }
}

function parseArtifact(value: unknown): DataQueryArtifact {
  const path = 'dataQueryPresentation.artifact'
  const object = _record(value, path)
  _keys(object, ['id', 'format', 'fileName', 'rowCount', 'sizeBytes', 'sha256', 'expiresAt', 'downloadUrl'], path)
  const fileName = _string(_required(object, 'fileName', path), `${path}.fileName`, 1, 256)
  if (!/^[^\\/\u0000-\u001f]+\.csv$/iu.test(fileName) || fileName === '.' || fileName === '..') {
    _fail(`${path}.fileName`, 'must be a safe CSV filename')
  }
  const sha256 = _string(_required(object, 'sha256', path), `${path}.sha256`, 64, 72)
  if (!/^(?:sha256:)?[a-f0-9]{64}$/iu.test(sha256)) _fail(`${path}.sha256`, 'must be a SHA-256 hex digest')
  const expiresAt = _string(_required(object, 'expiresAt', path), `${path}.expiresAt`, 1, 64)
  if (!/^\d{4}-\d{2}-\d{2}T[^\s]+(?:Z|[+-]\d{2}:?\d{2})$/u.test(expiresAt) || !Number.isFinite(Date.parse(expiresAt))) {
    _fail(`${path}.expiresAt`, 'must be an ISO-8601 timestamp')
  }
  const downloadUrl = _string(_required(object, 'downloadUrl', path), `${path}.downloadUrl`, 1, 2_048)
  let parsedUrl: URL
  try {
    parsedUrl = new URL(downloadUrl)
  } catch {
    _fail(`${path}.downloadUrl`, 'must be an absolute HTTP(S) URL')
  }
  if ((parsedUrl.protocol !== 'http:' && parsedUrl.protocol !== 'https:') || parsedUrl.username !== '' || parsedUrl.password !== '' || parsedUrl.hash !== '') {
    _fail(`${path}.downloadUrl`, 'must be an absolute HTTP(S) URL without embedded userinfo or a fragment')
  }
  return {
    id: _string(_required(object, 'id', path), `${path}.id`, 1, 128),
    format: _enum(_required(object, 'format', path), ['csv'] as const, `${path}.format`),
    fileName,
    rowCount: _integer(_required(object, 'rowCount', path), `${path}.rowCount`, MAX_QUERY_ROWS),
    sizeBytes: _integer(_required(object, 'sizeBytes', path), `${path}.sizeBytes`, MAX_ARTIFACT_BYTES),
    sha256,
    expiresAt,
    downloadUrl: parsedUrl.href,
  }
}

/** Parse a v2 DataQueryPresentation with inline or artifact delivery. */
export function parseDataQueryPresentationV2(value: unknown): DataQueryPresentationV2 {
  const object = _record(value, 'dataQueryPresentation')
  _keys(object, ['schemaVersion', 'queryId', 'status', 'semanticSql', 'question', 'sqlHistory', 'nativeSql', 'columns', 'previewRows', 'chart', 'stats', 'error', 'delivery', 'artifact'], 'dataQueryPresentation')
  _version(_required(object, 'schemaVersion', 'dataQueryPresentation'), DATA_QUERY_PRESENTATION_VERSION, 'dataQueryPresentation.schemaVersion')
  const status = _enum(_required(object, 'status', 'dataQueryPresentation'), ['success', 'error'] as const, 'dataQueryPresentation.status')
  const delivery = _optional(object, 'delivery')
  const artifact = _optional(object, 'artifact')
  // Delivery is intentionally parsed before setting the preview cap. An
  // artifact is never allowed to smuggle more than the fixed 20-row preview.
  const artifactDelivery = status === 'success' && delivery === 'artifact'
  const inlineDelivery = status === 'success' && delivery === 'inline'
  const maxPreviewRows = artifactDelivery ? MAX_ARTIFACT_PREVIEW_ROWS : inlineDelivery ? MAX_INLINE_PREVIEW_ROWS : MAX_PREVIEW_ROWS
  const maxPreviewBytes = inlineDelivery ? MAX_INLINE_PREVIEW_BYTES : MAX_PREVIEW_BYTES
  const { parsedColumns, previewRows } = parseColumnsAndPreview(object, maxPreviewRows, maxPreviewBytes)
  const stats = parseStatsV2(_required(object, 'stats', 'dataQueryPresentation'), 'dataQueryPresentation.stats', maxPreviewRows)
  if (stats.previewedRows !== previewRows.length) _fail('dataQueryPresentation.stats.previewedRows', 'must equal previewRows.length')
  if (previewRows.length > stats.returnedRows) _fail('dataQueryPresentation.previewRows', 'cannot contain more rows than returnedRows')
  if (!stats.truncated && previewRows.length !== stats.returnedRows) _fail('dataQueryPresentation.stats.truncated', 'must be true when preview rows omit returned rows')
  const chart = _optional(object, 'chart')
  const error = _optional(object, 'error')
  const base = {
    schemaVersion: DATA_QUERY_PRESENTATION_VERSION,
    ...parseCommonPresentationFields(object, parsedColumns, previewRows),
    stats,
  }
  if (status === 'error') {
    if (delivery !== undefined) _fail('dataQueryPresentation.delivery', 'must be omitted for an error')
    if (artifact !== undefined) _fail('dataQueryPresentation.artifact', 'must be omitted for an error')
    if (chart !== undefined) _fail('dataQueryPresentation.chart', 'must be omitted for an error')
    if (error === undefined) _fail('dataQueryPresentation.error', 'field is required for an error')
    return { ...base, status: 'error', error: _parseError(error, 'dataQueryPresentation.error') }
  }
  if (error !== undefined) _fail('dataQueryPresentation.error', 'must be omitted for success')
  const parsedDelivery = _enum(delivery, ['inline', 'artifact'] as const, 'dataQueryPresentation.delivery')
  if (parsedDelivery === 'inline') {
    if (artifact !== undefined) _fail('dataQueryPresentation.artifact', 'must be omitted for inline delivery')
    const chart = _optional(object, 'chart')
    return { ...base, status: 'success', delivery: parsedDelivery, ...(chart === undefined ? {} : { chart: parseChartSpecV1(chart) }) }
  }
  if (artifact === undefined) _fail('dataQueryPresentation.artifact', 'field is required for artifact delivery')
  if (_optional(object, 'chart') !== undefined) _fail('dataQueryPresentation.chart', 'must be omitted for artifact delivery')
  const parsedArtifact = parseArtifact(artifact)
  if (parsedArtifact.rowCount !== stats.returnedRows) _fail('dataQueryPresentation.artifact.rowCount', 'must equal stats.returnedRows')
  return {
    ...base,
    status: 'success',
    delivery: parsedDelivery,
    artifact: parsedArtifact,
  }
}

/** Parse either the replay-compatible v1 or current v2 presentation. */
export function parseDataQueryPresentation(value: unknown): DataQueryPresentation {
  const object = _record(value, 'dataQueryPresentation')
  if (object.schemaVersion === SCHEMA_VERSION) return parseDataQueryPresentationV1(value)
  if (object.schemaVersion === DATA_QUERY_PRESENTATION_VERSION) return parseDataQueryPresentationV2(value)
  return _fail('dataQueryPresentation.schemaVersion', `unsupported version; expected ${JSON.stringify(DATA_QUERY_PRESENTATION_VERSION)}`, 'UNSUPPORTED_VERSION')
}

/** JSON Schema for DataQueryInput. */
export const DATA_QUERY_INPUT_JSON_SCHEMA: JsonSchema = {
  $schema: 'https://json-schema.org/draft/2020-12/schema', type: 'object', additionalProperties: false,
  required: ['question', 'semanticSql'], properties: {
    question: { type: 'string', minLength: 1, maxLength: 16_000 },
    semanticSql: { type: 'string', minLength: 1, maxLength: 64_000 },
    chartIntent: { enum: ['auto', 'table', 'line', 'bar', 'pie'] },
  },
}

const DATA_QUERY_PRESENTATION_BASE_JSON_SCHEMA: JsonSchema = {
  type: 'object', additionalProperties: false,
  required: ['schemaVersion', 'queryId', 'status', 'semanticSql', 'columns', 'previewRows', 'stats'],
  properties: {
    queryId: { type: 'string', minLength: 1, maxLength: 128 },
    status: { enum: ['success', 'error'] }, semanticSql: { type: 'string', minLength: 1 },
    question: { type: 'string', minLength: 1 }, sqlHistory: { type: 'array', maxItems: 5 }, nativeSql: { type: 'string' },
    columns: { type: 'array' }, previewRows: { type: 'array', maxItems: MAX_PREVIEW_ROWS },
    chart: CHART_SPEC_V1_JSON_SCHEMA, stats: { type: 'object' }, error: ERROR_JSON_SCHEMA,
  },
  $defs: { error: ERROR_JSON_SCHEMA },
}

const DATA_QUERY_STATS_V1_JSON_SCHEMA: JsonSchema = {
  type: 'object', additionalProperties: false,
  required: ['returnedRows', 'durationMs', 'truncated'],
  properties: {
    returnedRows: { type: 'integer', minimum: 0, maximum: MAX_QUERY_ROWS },
    durationMs: { type: 'number', minimum: 0 },
    truncated: { type: 'boolean' },
  },
}

function dataQueryStatsV2JsonSchema(maxPreviewRows: number): JsonSchema {
  return {
  type: 'object', additionalProperties: false,
  required: ['returnedRows', 'durationMs', 'truncated', 'previewedRows'],
  properties: {
    returnedRows: { type: 'integer', minimum: 0, maximum: MAX_QUERY_ROWS },
    durationMs: { type: 'number', minimum: 0 },
    truncated: { type: 'boolean' },
    previewedRows: { type: 'integer', minimum: 0, maximum: maxPreviewRows },
  },
  }
}

/** JSON Schema for replay-compatible DataQueryPresentation v1. */
export const DATA_QUERY_PRESENTATION_V1_JSON_SCHEMA: JsonSchema = {
  ...DATA_QUERY_PRESENTATION_BASE_JSON_SCHEMA,
  properties: { ...DATA_QUERY_PRESENTATION_BASE_JSON_SCHEMA.properties, schemaVersion: { const: SCHEMA_VERSION }, stats: DATA_QUERY_STATS_V1_JSON_SCHEMA },
}

const DATA_QUERY_ARTIFACT_JSON_SCHEMA: JsonSchema = {
  type: 'object', additionalProperties: false,
  required: ['id', 'format', 'fileName', 'rowCount', 'sizeBytes', 'sha256', 'expiresAt', 'downloadUrl'],
  properties: {
    id: { type: 'string', minLength: 1, maxLength: 128 }, format: { const: 'csv' },
    fileName: { type: 'string', minLength: 1, maxLength: 256, pattern: '^[^\\\\/\\u0000-\\u001f]+\\.csv$' }, rowCount: { type: 'integer', minimum: 0, maximum: MAX_QUERY_ROWS },
    sizeBytes: { type: 'integer', minimum: 0, maximum: MAX_ARTIFACT_BYTES }, sha256: { type: 'string', pattern: '^(?:sha256:)?[a-fA-F0-9]{64}$' },
    expiresAt: { type: 'string', format: 'date-time' }, downloadUrl: { type: 'string', format: 'uri', maxLength: 2_048 },
  },
}

const DATA_QUERY_PRESENTATION_V2_INLINE_JSON_SCHEMA: JsonSchema = {
  ...DATA_QUERY_PRESENTATION_BASE_JSON_SCHEMA,
  required: [...(DATA_QUERY_PRESENTATION_BASE_JSON_SCHEMA.required ?? []), 'delivery'],
  properties: {
    schemaVersion: { const: DATA_QUERY_PRESENTATION_VERSION }, queryId: { type: 'string', minLength: 1, maxLength: 128 },
    status: { const: 'success' }, semanticSql: { type: 'string', minLength: 1 }, question: { type: 'string', minLength: 1 },
    sqlHistory: { type: 'array', maxItems: 5 }, nativeSql: { type: 'string' }, columns: { type: 'array' },
    previewRows: { type: 'array', maxItems: MAX_INLINE_PREVIEW_ROWS }, chart: CHART_SPEC_V1_JSON_SCHEMA,
    stats: dataQueryStatsV2JsonSchema(MAX_INLINE_PREVIEW_ROWS), delivery: { const: 'inline' },
  },
}

const DATA_QUERY_PRESENTATION_V2_ARTIFACT_JSON_SCHEMA: JsonSchema = {
  ...DATA_QUERY_PRESENTATION_BASE_JSON_SCHEMA,
  required: [...(DATA_QUERY_PRESENTATION_BASE_JSON_SCHEMA.required ?? []), 'delivery', 'artifact'],
  properties: {
    schemaVersion: { const: DATA_QUERY_PRESENTATION_VERSION }, queryId: { type: 'string', minLength: 1, maxLength: 128 },
    status: { const: 'success' }, semanticSql: { type: 'string', minLength: 1 }, question: { type: 'string', minLength: 1 },
    sqlHistory: { type: 'array', maxItems: 5 }, nativeSql: { type: 'string' }, columns: { type: 'array' },
    previewRows: { type: 'array', maxItems: MAX_ARTIFACT_PREVIEW_ROWS }, stats: dataQueryStatsV2JsonSchema(MAX_ARTIFACT_PREVIEW_ROWS),
    delivery: { const: 'artifact' }, artifact: DATA_QUERY_ARTIFACT_JSON_SCHEMA,
  },
}

const DATA_QUERY_PRESENTATION_V2_ERROR_JSON_SCHEMA: JsonSchema = {
  ...DATA_QUERY_PRESENTATION_BASE_JSON_SCHEMA,
  required: [...(DATA_QUERY_PRESENTATION_BASE_JSON_SCHEMA.required ?? []), 'error'],
  properties: {
    schemaVersion: { const: DATA_QUERY_PRESENTATION_VERSION }, queryId: { type: 'string', minLength: 1, maxLength: 128 },
    status: { const: 'error' }, semanticSql: { type: 'string', minLength: 1 }, question: { type: 'string', minLength: 1 },
    sqlHistory: { type: 'array', maxItems: 5 }, nativeSql: { type: 'string' }, columns: { type: 'array' },
    previewRows: { type: 'array', maxItems: MAX_PREVIEW_ROWS }, stats: dataQueryStatsV2JsonSchema(MAX_PREVIEW_ROWS),
    error: ERROR_JSON_SCHEMA,
  },
}

/** JSON Schema for DataQueryPresentation v2. */
export const DATA_QUERY_PRESENTATION_V2_JSON_SCHEMA: JsonSchema = {
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  anyOf: [
    DATA_QUERY_PRESENTATION_V2_INLINE_JSON_SCHEMA,
    DATA_QUERY_PRESENTATION_V2_ARTIFACT_JSON_SCHEMA,
    DATA_QUERY_PRESENTATION_V2_ERROR_JSON_SCHEMA,
  ],
}

/** JSON Schema for both accepted DataQueryPresentation versions. */
export const DATA_QUERY_PRESENTATION_JSON_SCHEMA: JsonSchema = {
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  anyOf: [DATA_QUERY_PRESENTATION_V1_JSON_SCHEMA, DATA_QUERY_PRESENTATION_V2_JSON_SCHEMA],
}

/** DataQuery input parser/schema pair. */
export const dataQueryInputSchema: ContractSchema<DataQueryInput> = _schema(DATA_QUERY_INPUT_JSON_SCHEMA, parseDataQueryInput)

/** DataQuery v1 presentation parser/schema pair. */
export const dataQueryPresentationV1Schema: ContractSchema<DataQueryPresentationV1> = _schema(DATA_QUERY_PRESENTATION_V1_JSON_SCHEMA, parseDataQueryPresentationV1)

/** DataQuery v2 presentation parser/schema pair. */
export const dataQueryPresentationV2Schema: ContractSchema<DataQueryPresentationV2> = _schema(DATA_QUERY_PRESENTATION_V2_JSON_SCHEMA, parseDataQueryPresentationV2)

/** DataQuery presentation parser/schema pair accepting v1 and v2. */
export const dataQueryPresentationSchema: ContractSchema<DataQueryPresentation> = _schema(DATA_QUERY_PRESENTATION_JSON_SCHEMA, parseDataQueryPresentation)

/** Pascal-case schema aliases. */
export const DataQueryInputSchema = dataQueryInputSchema
export const DataQueryPresentationV1Schema = dataQueryPresentationV1Schema
export const DataQueryPresentationV2Schema = dataQueryPresentationV2Schema
export const DataQueryPresentationSchema = dataQueryPresentationSchema

/** Type guards for query contracts. */
export const isDataQueryInput = (value: unknown): value is DataQueryInput => dataQueryInputSchema.check(value)
export const isDataQueryPresentationV1 = (value: unknown): value is DataQueryPresentationV1 => dataQueryPresentationV1Schema.check(value)
export const isDataQueryPresentationV2 = (value: unknown): value is DataQueryPresentationV2 => dataQueryPresentationV2Schema.check(value)
export const isDataQueryPresentation = (value: unknown): value is DataQueryPresentation => dataQueryPresentationSchema.check(value)
