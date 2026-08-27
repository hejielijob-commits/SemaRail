import {
  _enum,
  _fail,
  _keys,
  _number,
  _optional,
  _record,
  _required,
  _string,
  _boolean,
  _integer,
  _version,
  isJsonSafeScalar,
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_ROWS,
  MAX_QUERY_ROWS,
  SCHEMA_VERSION,
  type JsonSafeScalar,
  type JsonSchema,
} from './json.js'
import { ERROR_JSON_SCHEMA, _parseError, type DataAgentError } from './errors.js'
import { parseChartSpecV1, CHART_SPEC_V1_JSON_SCHEMA, type ChartSpecV1 } from './chart.js'
import { _schema, type ContractSchema } from './schema.js'
import { parseSqlHistoryReference, type SqlHistoryReference } from './context.js'

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

/** Query counters; `returnedRows` is intentionally unambiguous. */
export interface DataQueryStats {
  readonly returnedRows: number
  readonly durationMs: number
  readonly truncated: boolean
}

/** Successful result presentation persisted in `tool/result.meta`. */
export interface DataQuerySuccessPresentation {
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
  readonly stats: DataQueryStats
}

/** Failed result presentation persisted in `tool/result.meta`. */
export interface DataQueryErrorPresentation {
  readonly schemaVersion: typeof SCHEMA_VERSION
  readonly queryId: string
  readonly status: 'error'
  readonly semanticSql: string
  readonly question?: string
  readonly sqlHistory?: readonly SqlHistoryReference[]
  readonly nativeSql?: string
  readonly columns: readonly DataQueryColumn[]
  readonly previewRows: readonly Readonly<Record<string, JsonSafeScalar>>[]
  readonly stats: DataQueryStats
  readonly error: DataAgentError
}

/** Union of successful and failed query presentations. */
export type DataQueryPresentation = DataQuerySuccessPresentation | DataQueryErrorPresentation

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

function parseStats(value: unknown, path: string): DataQueryStats {
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

function requiresString(type: string): boolean {
  return /^(BIGINT|INT64|LONG|DECIMAL|NUMERIC|DATE|TIME|TIMESTAMP|DATETIME)(?:\b|\()/u.test(type.trim().toUpperCase())
}

function parsePreviewRows(value: unknown, columns: readonly DataQueryColumn[]): Readonly<Record<string, JsonSafeScalar>>[] {
  const path = 'dataQueryPresentation.previewRows'
  if (!Array.isArray(value)) _fail(path, 'expected an array')
  if (value.length > MAX_PREVIEW_ROWS) _fail(path, `must contain at most ${MAX_PREVIEW_ROWS} row(s)`)
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
  if (new TextEncoder().encode(JSON.stringify(rows)).byteLength > MAX_PREVIEW_BYTES) _fail(path, `UTF-8 JSON size must be at most ${MAX_PREVIEW_BYTES} bytes`)
  return rows
}

/** Parse a bounded DataQueryPresentation. */
export function parseDataQueryPresentation(value: unknown): DataQueryPresentation {
  const object = _record(value, 'dataQueryPresentation')
  _keys(object, ['schemaVersion', 'queryId', 'status', 'semanticSql', 'question', 'sqlHistory', 'nativeSql', 'columns', 'previewRows', 'chart', 'stats', 'error'], 'dataQueryPresentation')
  _version(_required(object, 'schemaVersion', 'dataQueryPresentation'), SCHEMA_VERSION, 'dataQueryPresentation.schemaVersion')
  const status = _enum(_required(object, 'status', 'dataQueryPresentation'), ['success', 'error'] as const, 'dataQueryPresentation.status')
  const columns = _required(object, 'columns', 'dataQueryPresentation')
  if (!Array.isArray(columns)) _fail('dataQueryPresentation.columns', 'expected an array')
  const parsedColumns = columns.map((column, index) => parseColumn(column, `dataQueryPresentation.columns[${index}]`))
  const previewRows = parsePreviewRows(_required(object, 'previewRows', 'dataQueryPresentation'), parsedColumns)
  const stats = parseStats(_required(object, 'stats', 'dataQueryPresentation'), 'dataQueryPresentation.stats')
  if (previewRows.length > stats.returnedRows) _fail('dataQueryPresentation.previewRows', 'cannot contain more rows than returnedRows')
  if (!stats.truncated && previewRows.length !== stats.returnedRows) _fail('dataQueryPresentation.stats.truncated', 'must be true when preview rows omit returned rows')
  const nativeSql = _optional(object, 'nativeSql')
  const question = _optional(object, 'question')
  const sqlHistory = _optional(object, 'sqlHistory')
  if (sqlHistory !== undefined && !Array.isArray(sqlHistory)) _fail('dataQueryPresentation.sqlHistory', 'expected an array')
  if (Array.isArray(sqlHistory) && sqlHistory.length > 5) _fail('dataQueryPresentation.sqlHistory', 'must contain at most 5 item(s)')
  const base = {
    schemaVersion: SCHEMA_VERSION,
    queryId: _string(_required(object, 'queryId', 'dataQueryPresentation'), 'dataQueryPresentation.queryId', 1, 128),
    semanticSql: _string(_required(object, 'semanticSql', 'dataQueryPresentation'), 'dataQueryPresentation.semanticSql', 1, 64_000),
    ...(question === undefined ? {} : { question: _string(question, 'dataQueryPresentation.question', 1, 16_000) }),
    ...(sqlHistory === undefined ? {} : { sqlHistory: sqlHistory.map((item, index) => parseSqlHistoryReference(item, `dataQueryPresentation.sqlHistory[${index}]`)) }),
    ...(nativeSql === undefined ? {} : { nativeSql: _string(nativeSql, 'dataQueryPresentation.nativeSql', 1, 64_000) }),
    columns: parsedColumns,
    previewRows,
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

/** JSON Schema for DataQueryInput. */
export const DATA_QUERY_INPUT_JSON_SCHEMA: JsonSchema = {
  $schema: 'https://json-schema.org/draft/2020-12/schema', type: 'object', additionalProperties: false,
  required: ['question', 'semanticSql'], properties: {
    question: { type: 'string', minLength: 1, maxLength: 16_000 },
    semanticSql: { type: 'string', minLength: 1, maxLength: 64_000 },
    chartIntent: { enum: ['auto', 'table', 'line', 'bar', 'pie'] },
  },
}

/** JSON Schema for DataQueryPresentation. */
export const DATA_QUERY_PRESENTATION_JSON_SCHEMA: JsonSchema = {
  $schema: 'https://json-schema.org/draft/2020-12/schema', type: 'object', additionalProperties: false,
  required: ['schemaVersion', 'queryId', 'status', 'semanticSql', 'columns', 'previewRows', 'stats'],
  properties: {
    schemaVersion: { const: SCHEMA_VERSION }, queryId: { type: 'string', minLength: 1, maxLength: 128 },
    status: { enum: ['success', 'error'] }, semanticSql: { type: 'string', minLength: 1 }, question: { type: 'string', minLength: 1 },
    sqlHistory: { type: 'array', maxItems: 5 }, nativeSql: { type: 'string' },
    columns: { type: 'array' }, previewRows: { type: 'array', maxItems: MAX_PREVIEW_ROWS },
    chart: CHART_SPEC_V1_JSON_SCHEMA, stats: { type: 'object' }, error: ERROR_JSON_SCHEMA,
  },
  $defs: { error: ERROR_JSON_SCHEMA },
}

/** DataQuery input parser/schema pair. */
export const dataQueryInputSchema: ContractSchema<DataQueryInput> = _schema(DATA_QUERY_INPUT_JSON_SCHEMA, parseDataQueryInput)

/** DataQuery presentation parser/schema pair. */
export const dataQueryPresentationSchema: ContractSchema<DataQueryPresentation> = _schema(DATA_QUERY_PRESENTATION_JSON_SCHEMA, parseDataQueryPresentation)

/** Pascal-case schema aliases. */
export const DataQueryInputSchema = dataQueryInputSchema
export const DataQueryPresentationSchema = dataQueryPresentationSchema

/** Type guards for query contracts. */
export const isDataQueryInput = (value: unknown): value is DataQueryInput => dataQueryInputSchema.check(value)
export const isDataQueryPresentation = (value: unknown): value is DataQueryPresentation => dataQueryPresentationSchema.check(value)
