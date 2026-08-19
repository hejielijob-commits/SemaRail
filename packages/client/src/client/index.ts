/**
 * Browser half of the Wren data-agent plugin.
 *
 * The view is deliberately a pure projection of the durable `tool/result.meta`
 * payload. It does not read tool text, running arguments, session state, or an
 * in-memory query cache when a result is settled. A replay or hard refresh is
 * therefore equivalent to the first render of the same tool result.
 */
import { createElement, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

/** Wire tool name registered by the Host half. */
export const DATA_QUERY_TOOL_NAME = 'data_query' as const

/** Harness slot surface used by a Harness slot plugin. */
export interface DataQuerySlots {
  inject(name: string, callback: () => unknown): unknown
  register(options: { name: string; key?: string; locale?: string }, component: unknown): () => void
}

/** Minimal Client context needed by a Harness slot plugin. */
export interface DataQueryClientContext {
  slots: DataQuerySlots
}

/** Settled ToolResult subset consumed by this view. */
export interface DataQueryResultBlock {
  readonly kind: 'tool-result'
  readonly callId: string
  readonly content: readonly unknown[]
  readonly isError: boolean
  readonly error?: { readonly name: string; readonly code: string }
  /** Durable presentation; this is the sole source for a settled view. */
  readonly meta?: unknown
}

/** Running ToolCall subset consumed by the safe fallback. */
export interface DataQueryRunningBlock {
  readonly kind?: never
  readonly callId: string
  readonly name: string
  readonly argsRaw: string
}

/** Props supplied by Harness's keyed `tool.call.toolview` slot. */
export interface DataQueryViewProps {
  readonly callId: string
  readonly toolName: string
  readonly block: DataQueryResultBlock | DataQueryRunningBlock
  readonly cwd?: string
  readonly openFile?: (path: string) => void
  readonly inspect?: () => void
}

/** Version-one result metadata shape accepted by the fail-closed guard. */
export interface DataQueryMeta {
  readonly schemaVersion: 1
  readonly queryId: string
  readonly status: 'success' | 'error'
  readonly semanticSql: string
  readonly nativeSql?: string
  readonly columns: readonly DataQueryMetaColumn[]
  readonly previewRows: readonly Readonly<Record<string, DataQueryScalar>>[]
  readonly chart?: DataQueryChart
  readonly stats: DataQueryStats
  readonly error?: DataQueryError
}

/** Result column metadata. */
export interface DataQueryMetaColumn {
  readonly name: string
  readonly type: string
  readonly semanticRole: 'dimension' | 'measure'
}

/** JSON-safe scalar shown in the result table and chart. */
export type DataQueryScalar = string | number | boolean | null

/** Bounded query statistics. */
export interface DataQueryStats {
  readonly returnedRows: number
  readonly durationMs: number
  readonly truncated: boolean
}

/** Validated ChartSpecV1 persisted inside a successful presentation. */
export interface DataQueryChart {
  readonly version: 1
  readonly type: 'line' | 'bar' | 'pie'
  readonly title?: string
  readonly x: string
  readonly y: readonly string[]
  readonly series?: string
  readonly tooltip: true
}

/** Stable error projection in an error result. */
export interface DataQueryError {
  readonly code: DataQueryErrorCode
  readonly phase: string
  readonly message: string
  readonly retryable: boolean
}

/** Stable error codes from the shared Contract package. */
export type DataQueryErrorCode = typeof DATA_QUERY_ERROR_CODES[number]

/**
 * Keep this list local so the browser package remains independently loadable.
 * It mirrors the version-pinned Contract package and rejects unknown codes.
 */
export const DATA_QUERY_ERROR_CODES = [
  'SEMANTIC_ERROR', 'POLICY_DENIED', 'DATABASE_ERROR', 'TIMEOUT', 'CANCELLED',
  'SIDECAR_UNAVAILABLE', 'UNSUPPORTED_PROTOCOL', 'INVALID_PARAMS', 'METHOD_NOT_FOUND',
  'WREN_UNAVAILABLE', 'PROJECT_VALIDATION_FAILED', 'HEALTHCHECK_FAILED', 'FRAME_TOO_LARGE',
  'TRUNCATED_FRAME', 'INVALID_REQUEST', 'PROTOCOL_ERROR', 'UNSUPPORTED_VERSION', 'INTERNAL_ERROR',
] as const

const MAX_COLUMNS = 64
const MAX_ROWS = 200
const MAX_ROW_KEYS = 64
const MAX_STRING = 4_000
const MAX_SQL = 64_000
const MAX_QUERY_ROWS = 500
const MAX_PREVIEW_BYTES = 1_048_576

/** Return true for a JSON/plain object, excluding arrays and null. */
function isRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function hasOnlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  const set = new Set(allowed)
  return Object.keys(value).every(key => set.has(key))
}

/** Return a bounded non-empty string or undefined. */
function boundedString(value: unknown, max = MAX_STRING): string | undefined {
  return typeof value === 'string' && value.length > 0 && value.length <= max ? value : undefined
}

/** Return a scalar that can safely cross the React text boundary. */
function scalar(value: unknown): DataQueryScalar | undefined {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number' && Number.isFinite(value)) return value
  return undefined
}

function parseColumn(value: unknown): DataQueryMetaColumn | undefined {
  if (!isRecord(value) || !hasOnlyKeys(value, ['name', 'type', 'semanticRole'])) return undefined
  const name = boundedString(value.name, 256)
  const type = boundedString(value.type, 128)
  const semanticRole = value.semanticRole
  if (name === undefined || type === undefined || (semanticRole !== 'dimension' && semanticRole !== 'measure')) return undefined
  return { name, type, semanticRole }
}

function parseStats(value: unknown): DataQueryStats | undefined {
  if (!isRecord(value) || !hasOnlyKeys(value, ['returnedRows', 'durationMs', 'truncated'])) return undefined
  const returnedRows = value.returnedRows
  const durationMs = value.durationMs
  const truncated = value.truncated
  if (typeof returnedRows !== 'number' || !Number.isSafeInteger(returnedRows) || returnedRows < 0 || returnedRows > MAX_QUERY_ROWS) return undefined
  if (typeof durationMs !== 'number' || !Number.isFinite(durationMs) || durationMs < 0 || durationMs > 86_400_000) return undefined
  if (typeof truncated !== 'boolean') return undefined
  return { returnedRows, durationMs, truncated }
}

function isSafeFieldName(value: string): boolean {
  // A field name is only used as an exact object key. Reject common URL and
  // expression markers anyway so it cannot become an accidental executable
  // configuration if the chart option evolves later.
  return !(/[\u0000-\u001f<>]|:\/\/|\$\{|=>|(?:javascript|data|vbscript):/iu.test(value))
}

/** Parse exactly the versioned ChartSpecV1 subset. */
export function parseChartSpecV1(value: unknown): DataQueryChart | undefined {
  if (!isRecord(value) || !hasOnlyKeys(value, ['version', 'type', 'title', 'x', 'y', 'series', 'tooltip'])) return undefined
  if (value.version !== 1 || value.tooltip !== true) return undefined
  const type = value.type
  const x = boundedString(value.x, 256)
  const y = value.y
  if ((type !== 'line' && type !== 'bar' && type !== 'pie') || x === undefined || !isSafeFieldName(x)) return undefined
  if (!Array.isArray(y) || y.length === 0 || y.length > 8) return undefined
  const parsedY = y.map(item => boundedString(item, 256))
  if (parsedY.some(item => item === undefined || !isSafeFieldName(item))) return undefined
  const yNames = parsedY as string[]
  if (new Set(yNames).size !== yNames.length) return undefined
  const title = value.title === undefined ? undefined : boundedString(value.title, 200)
  const series = value.series === undefined ? undefined : boundedString(value.series, 256)
  if (value.title !== undefined && (title === undefined || !isSafeFieldName(title))) return undefined
  if (value.series !== undefined && (series === undefined || !isSafeFieldName(series))) return undefined
  return {
    version: 1,
    type,
    x,
    y: yNames,
    tooltip: true,
    ...(title === undefined ? {} : { title }),
    ...(series === undefined ? {} : { series }),
  }
}

function parseError(value: unknown): DataQueryError | undefined {
  if (!isRecord(value) || !hasOnlyKeys(value, ['code', 'phase', 'message', 'retryable'])) return undefined
  const code = value.code
  const phase = boundedString(value.phase, 64)
  const message = boundedString(value.message, MAX_STRING)
  if (typeof code !== 'string' || !(DATA_QUERY_ERROR_CODES as readonly string[]).includes(code)) return undefined
  if (phase === undefined || message === undefined || typeof value.retryable !== 'boolean') return undefined
  return { code: code as DataQueryErrorCode, phase, message, retryable: value.retryable }
}

/**
 * Parse the durable `tool/result.meta` value for this view.
 *
 * The guard deliberately accepts exactly schema version 1 and the same
 * bounded fields as the Contract package. Future, incomplete, oversized, or
 * malformed metadata returns null; no untrusted value reaches a chart option
 * or HTML attribute without this check.
 */
export function parseDataQueryMeta(value: unknown): DataQueryMeta | null {
  if (!isRecord(value) || !hasOnlyKeys(value, ['schemaVersion', 'queryId', 'status', 'semanticSql', 'nativeSql', 'columns', 'previewRows', 'chart', 'stats', 'error'])) return null
  if (value.schemaVersion !== 1 || (value.status !== 'success' && value.status !== 'error')) return null
  const queryId = boundedString(value.queryId, 128)
  const semanticSql = boundedString(value.semanticSql, MAX_SQL)
  if (queryId === undefined || semanticSql === undefined) return null
  if (!Array.isArray(value.columns) || value.columns.length > MAX_COLUMNS) return null
  const columns = value.columns.map(parseColumn)
  if (columns.some(column => column === undefined)) return null
  const parsedColumns = columns as DataQueryMetaColumn[]
  const names = new Set(parsedColumns.map(column => column.name))
  if (names.size !== parsedColumns.length || !Array.isArray(value.previewRows) || value.previewRows.length > MAX_ROWS) return null
  const previewRows: Readonly<Record<string, DataQueryScalar>>[] = []
  for (const candidate of value.previewRows) {
    if (!isRecord(candidate) || Object.keys(candidate).length !== parsedColumns.length || Object.keys(candidate).length > MAX_ROW_KEYS) return null
    const row: Record<string, DataQueryScalar> = {}
    for (const [key, raw] of Object.entries(candidate)) {
      if (!names.has(key)) return null
      const item = scalar(raw)
      if (item === undefined) return null
      row[key] = item
    }
    for (const column of parsedColumns) if (!Object.prototype.hasOwnProperty.call(row, column.name)) return null
    previewRows.push(row)
  }
  try {
    if (new TextEncoder().encode(JSON.stringify(previewRows)).byteLength > MAX_PREVIEW_BYTES) return null
  } catch {
    return null
  }
  const stats = parseStats(value.stats)
  if (stats === undefined || previewRows.length > stats.returnedRows || (!stats.truncated && previewRows.length !== stats.returnedRows)) return null
  const nativeSql = value.nativeSql === undefined ? undefined : boundedString(value.nativeSql, MAX_SQL)
  if (value.nativeSql !== undefined && nativeSql === undefined) return null
  const chart = value.chart === undefined ? undefined : parseChartSpecV1(value.chart)
  if (value.chart !== undefined && chart === undefined) return null
  const error = value.error === undefined ? undefined : parseError(value.error)
  if (value.error !== undefined && error === undefined) return null
  if (value.status === 'success' && error !== undefined) return null
  if (value.status === 'error' && (error === undefined || chart !== undefined)) return null
  return {
    schemaVersion: 1,
    queryId,
    status: value.status,
    semanticSql,
    ...(nativeSql === undefined ? {} : { nativeSql }),
    columns: parsedColumns,
    previewRows,
    ...(chart === undefined ? {} : { chart }),
    stats,
    ...(error === undefined ? {} : { error }),
  }
}

function isSettled(block: DataQueryViewProps['block']): block is DataQueryResultBlock {
  return isRecord(block) && block.kind === 'tool-result'
}

function cellText(value: DataQueryScalar): string {
  return value === null ? 'null' : String(value)
}

/** Stable scalar comparison used by the table's basic sort controls. */
export function compareDataQueryCells(left: DataQueryScalar, right: DataQueryScalar): number {
  if (left === right) return 0
  if (left === null) return 1
  if (right === null) return -1
  if (typeof left === 'number' && typeof right === 'number') return left - right
  if (typeof left === 'boolean' && typeof right === 'boolean') return left === right ? 0 : left ? 1 : -1
  return String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: 'base' })
}

function element(type: string | ((props: any) => ReactNode), props: Record<string, unknown> | null, ...children: ReactNode[]): ReactNode {
  return createElement(type, props, ...children)
}

function fallback(props: DataQueryViewProps, reason: string, detail?: string): ReactNode {
  const text = detail === undefined ? reason : `${reason}: ${detail}`
  return element('div', { 'data-dsh-wren-data-query': 'fallback', 'data-tool': props.toolName, role: 'status' }, text)
}

/** Return a safe display label for a chart datum without evaluating content. */
function chartLabel(value: DataQueryScalar): string {
  return cellText(value)
}

function chartValue(value: DataQueryScalar): string | number | null | undefined {
  // Preserve an exact numeric string (for example DECIMAL/BIGINT) in the
  // option so the stock tooltip displays the original value. Boolean and
  // non-numeric strings are not drawable measure values and fail closed.
  if (value === null || typeof value === 'number') return value
  if (typeof value === 'string' && value.trim().length > 0 && Number.isFinite(Number(value))) return value
  return undefined
}

interface ChartDataPoint {
  readonly name: string
  readonly value: DataQueryScalar
}

interface ChartSeriesOption {
  readonly name: string
  readonly type: 'line' | 'bar' | 'pie'
  readonly data: readonly ChartDataPoint[]
}

/** ECharts option subset generated only from validated metadata and fields. */
export interface DataQueryChartOption {
  readonly title?: { readonly text: string }
  readonly tooltip: { readonly trigger: 'axis' | 'item' }
  readonly legend: { readonly data: readonly string[] }
  readonly xAxis?: { readonly type: 'category'; readonly data: readonly string[] }
  readonly yAxis?: { readonly type: 'value' }
  readonly series: readonly ChartSeriesOption[]
}

interface ChartGroup {
  readonly name: string
  readonly rows: readonly Readonly<Record<string, DataQueryScalar>>[]
}

function columnMap(meta: DataQueryMeta): ReadonlyMap<string, DataQueryMetaColumn> {
  return new Map(meta.columns.map(column => [column.name, column]))
}

/**
 * Build a minimal, non-executable ECharts option from ChartSpecV1.
 *
 * No formatter, callback, URL, expression, or arbitrary option object is
 * accepted. Unknown/mismatched fields return null and the UI stays on its
 * table/unsupported fallback. ECharts' built-in tooltip is intentionally used
 * so its item name/value are the original row dimension/value.
 */
export function buildDataQueryChartOption(meta: DataQueryMeta): DataQueryChartOption | null {
  const chart = meta.chart
  if (chart === undefined) return null
  const columns = columnMap(meta)
  const xColumn = columns.get(chart.x)
  if (xColumn === undefined || xColumn.semanticRole !== 'dimension') return null
  const yColumns = chart.y.map(name => columns.get(name))
  if (yColumns.some(column => column === undefined || column.semanticRole !== 'measure')) return null
  if (chart.series !== undefined) {
    const seriesColumn = columns.get(chart.series)
    if (seriesColumn === undefined || seriesColumn.semanticRole !== 'dimension') return null
  }

  const groups: ChartGroup[] = []
  if (chart.series === undefined) {
    groups.push({ name: '', rows: meta.previewRows })
  } else {
    const grouped = new Map<string, Readonly<Record<string, DataQueryScalar>>[]>()
    for (const row of meta.previewRows) {
      const groupName = chartLabel(row[chart.series])
      const existing = grouped.get(groupName)
      if (existing === undefined) grouped.set(groupName, [row])
      else existing.push(row)
    }
    for (const [name, rows] of grouped) groups.push({ name, rows })
  }

  const categories: string[] = []
  const categorySet = new Set<string>()
  if (chart.type !== 'pie') {
    for (const row of meta.previewRows) {
      const category = chartLabel(row[chart.x])
      if (!categorySet.has(category)) {
        categorySet.add(category)
        categories.push(category)
      }
    }
  }

  const series: ChartSeriesOption[] = []
  for (const group of groups) {
    for (const yName of chart.y) {
      let data: ChartDataPoint[]
      if (chart.type === 'pie') {
        data = []
        for (const row of group.rows) {
          const value = chartValue(row[yName])
          if (value === undefined) return null
          data.push({ name: chartLabel(row[chart.x]), value })
        }
      } else {
        // Every line/bar series shares the same unique category axis. Missing
        // group/category combinations become null so ECharts does not shift a
        // point into a neighbouring category. Existing values retain their
        // original scalar for the stock tooltip.
        const rowsByCategory = new Map<string, Readonly<Record<string, DataQueryScalar>>>()
        for (const row of group.rows) rowsByCategory.set(chartLabel(row[chart.x]), row)
        data = []
        for (const category of categories) {
          const row = rowsByCategory.get(category)
          if (row === undefined) {
            data.push({ name: category, value: null })
            continue
          }
          const value = chartValue(row[yName])
          if (value === undefined) return null
          data.push({ name: category, value })
        }
      }
      series.push({
        name: group.name === '' ? yName : `${group.name} · ${yName}`,
        type: chart.type,
        data,
      })
    }
  }
  if (series.length === 0) return null
  const legend = series.map(item => item.name)
  return {
    ...(chart.title === undefined ? {} : { title: { text: chart.title } }),
    tooltip: { trigger: chart.type === 'pie' ? 'item' : 'axis' },
    legend: { data: legend },
    ...(chart.type === 'pie' ? {} : {
      xAxis: { type: 'category', data: categories },
      yAxis: { type: 'value' },
    }),
    series,
  }
}

/** Token kind used by the intentionally small SQL highlighter. */
export type SqlTokenKind = 'keyword' | 'number' | 'string' | 'comment' | 'identifier' | 'punctuation' | 'whitespace'

/** A safe SQL token; rendering uses React text nodes, never HTML injection. */
export interface SqlToken {
  readonly kind: SqlTokenKind
  readonly text: string
}

const SQL_KEYWORDS = new Set([
  'select', 'from', 'where', 'group', 'by', 'order', 'limit', 'offset', 'join', 'left', 'right', 'inner', 'outer',
  'on', 'as', 'and', 'or', 'not', 'null', 'is', 'in', 'having', 'union', 'all', 'distinct', 'case', 'when', 'then',
  'else', 'end', 'asc', 'desc', 'with', 'cast', 'date', 'interval', 'true', 'false',
])

/** Lightweight, non-evaluating tokenization for SQL display. */
export function tokenizeSql(sql: string): readonly SqlToken[] {
  const tokenPattern = /(--[^\n]*|\/\*[\s\S]*?\*\/|'(?:''|[^'])*'|"(?:""|[^"])*"|`[^`]*`|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_$]*\b|\s+|.)/gu
  const tokens: SqlToken[] = []
  for (const match of sql.matchAll(tokenPattern)) {
    const text = match[0]
    let kind: SqlTokenKind = 'punctuation'
    if (/^\s+$/u.test(text)) kind = 'whitespace'
    else if (/^--|^\/\*/u.test(text)) kind = 'comment'
    else if (/^['"`]/u.test(text)) kind = 'string'
    else if (/^\d/u.test(text)) kind = 'number'
    else if (SQL_KEYWORDS.has(text.toLowerCase())) kind = 'keyword'
    else if (/^[A-Za-z_]/u.test(text)) kind = 'identifier'
    tokens.push({ kind, text })
  }
  return tokens
}

function HighlightedSql({ sql }: { readonly sql: string }): ReactNode {
  return element('code', { 'data-query-sql-code': true }, ...tokenizeSql(sql).map((token, index) => element(
    'span',
    { key: `${index}-${token.kind}`, className: `sql-${token.kind}` },
    token.text,
  )))
}

/** Pure visible SQL panel props, separated for replay/shallow verification. */
export interface DataQuerySqlViewProps {
  readonly meta: DataQueryMeta
  readonly mode: 'semantic' | 'native'
  readonly copied: boolean
  readonly onModeChange: (mode: 'semantic' | 'native') => void
  readonly onCopy: () => void
}

/** Render the selected SQL directly; the SQL tab is the only disclosure. */
export function DataQuerySqlView({ meta, mode, copied, onModeChange, onCopy }: DataQuerySqlViewProps): ReactNode {
  const nativeAvailable = meta.nativeSql !== undefined
  const selectedMode = mode === 'native' && nativeAvailable ? 'native' : 'semantic'
  const sql = selectedMode === 'native' ? meta.nativeSql as string : meta.semanticSql
  return element('section', { 'data-query-sql': true, role: 'region', 'aria-label': 'SQL' },
    element('div', { 'data-query-sql-heading': true }, 'SQL'),
    element('div', { className: 'data-query-sql-toolbar' },
      element('button', { type: 'button', 'data-query-sql-mode': 'semantic', 'aria-pressed': selectedMode === 'semantic', onClick: () => onModeChange('semantic') }, 'Semantic SQL'),
      element('button', { type: 'button', 'data-query-sql-mode': 'native', 'aria-pressed': selectedMode === 'native', disabled: !nativeAvailable, onClick: () => { if (nativeAvailable) onModeChange('native') } }, 'Native SQL'),
      element('button', { type: 'button', 'data-query-sql-copy': true, onClick: onCopy }, copied ? 'Copied' : 'Copy'),
    ),
    element('pre', { 'data-query-sql-code-block': true, 'data-query-sql-current': selectedMode }, element(HighlightedSql, { sql })),
  )
}

function DataQuerySql({ meta }: { readonly meta: DataQueryMeta }): ReactNode {
  const [mode, setMode] = useState<'semantic' | 'native'>('semantic')
  const [copied, setCopied] = useState(false)
  const nativeAvailable = meta.nativeSql !== undefined
  const sql = mode === 'native' && nativeAvailable ? meta.nativeSql as string : meta.semanticSql
  const copySql = async (): Promise<void> => {
    try {
      if (typeof navigator === 'undefined' || navigator.clipboard === undefined) return
      await navigator.clipboard.writeText(sql)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return element(DataQuerySqlView, {
    meta,
    mode,
    copied,
    onModeChange: setMode,
    onCopy: () => { void copySql() },
  })
}

function DataQueryTable({ meta }: { readonly meta: DataQueryMeta }): ReactNode {
  const [sort, setSort] = useState<{ readonly column: string; readonly descending: boolean } | null>(null)
  const rows = useMemo(() => {
    if (sort === null) return meta.previewRows
    return meta.previewRows
      .map((row, index) => ({ row, index }))
      .sort((left, right) => {
        const result = compareDataQueryCells(left.row[sort.column], right.row[sort.column])
        return result === 0 ? left.index - right.index : sort.descending ? -result : result
      })
      .map(item => item.row)
  }, [meta.previewRows, sort])
  const toggleSort = (column: string): void => {
    setSort(current => current?.column === column
      ? { column, descending: !current.descending }
      : { column, descending: false })
  }
  return element('div', { 'data-query-table-scroll': true, style: { overflowX: 'auto', maxWidth: '100%' } },
    element('table', { 'data-query-table': true, style: { minWidth: 'max-content', borderCollapse: 'collapse' } },
      element('thead', { style: { position: 'sticky', top: 0, zIndex: 1 } }, element('tr', null,
        ...meta.columns.map(column => element('th', {
          key: column.name,
          scope: 'col',
          'aria-sort': sort?.column === column.name ? (sort.descending ? 'descending' : 'ascending') : 'none',
          style: { position: 'sticky', top: 0, background: 'var(--color-bg, Canvas)', cursor: 'pointer' },
          onClick: () => toggleSort(column.name),
          onKeyDown: (event: { key?: string; preventDefault?: () => void }) => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault?.(); toggleSort(column.name) }
          },
          tabIndex: 0,
        }, column.name)),
      )),
      element('tbody', null, ...rows.map((row, index) => element('tr', { key: `${meta.queryId}-${index}` },
        ...meta.columns.map(column => element('td', { key: column.name }, cellText(row[column.name]))),
      ))),
    ),
  )
}

function DataQueryChartPanel({ meta }: { readonly meta: DataQueryMeta }): ReactNode {
  const ref = useRef<HTMLDivElement | null>(null)
  const replayKey = useMemo(() => JSON.stringify(meta), [meta])
  const option = buildDataQueryChartOption(meta)
  useEffect(() => {
    const elementRef = ref.current
    if (elementRef === null || option === null) return undefined
    const instance = echarts.init(elementRef)
    instance.setOption(option as unknown as Record<string, unknown>, { notMerge: true, lazyUpdate: false })
    const resize = (): void => instance.resize()
    if (typeof window !== 'undefined') window.addEventListener('resize', resize)
    return () => {
      if (typeof window !== 'undefined') window.removeEventListener('resize', resize)
      instance.dispose()
    }
    // replayKey is the durable payload identity; option is derived solely from it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replayKey])
  if (option === null) return element('div', { 'data-query-chart-fallback': true, role: 'status' }, 'Chart unavailable for this result')
  return element('div', {
    ref,
    role: 'img',
    'data-query-chart-canvas': meta.chart?.type,
    'aria-label': meta.chart?.title ?? `${meta.chart?.type ?? 'data'} chart`,
    style: { width: '100%', minHeight: 280 },
  })
}

/** Stable review-first tab order used by the MVP query card. */
export const DATA_QUERY_TAB_ORDER = ['table', 'chart', 'sql'] as const

/** The table is the deterministic default view for a replayed result. */
export const DEFAULT_DATA_QUERY_TAB = 'table' as const

type QueryTab = typeof DATA_QUERY_TAB_ORDER[number]

function DataQueryTabs({ meta }: { readonly meta: DataQueryMeta }): ReactNode {
  const chartAvailable = buildDataQueryChartOption(meta) !== null
  const [tab, setTab] = useState<QueryTab>(DEFAULT_DATA_QUERY_TAB)
  const panel = tab === 'chart'
    ? (chartAvailable ? element(DataQueryChartPanel, { meta }) : element('div', { role: 'status' }, 'Chart unavailable for this result'))
    : tab === 'table' ? element(DataQueryTable, { meta }) : element(DataQuerySql, { meta })
  return element('div', { 'data-query-tabs': true },
    element('nav', { role: 'tablist', 'aria-label': 'Data query views' },
      element('button', { type: 'button', role: 'tab', 'aria-selected': tab === 'table', onClick: () => setTab('table') }, 'Table'),
      element('button', { type: 'button', role: 'tab', 'aria-selected': tab === 'chart', disabled: !chartAvailable, onClick: () => setTab('chart') }, 'Chart'),
      element('button', { type: 'button', role: 'tab', 'aria-selected': tab === 'sql', onClick: () => setTab('sql') }, 'SQL'),
    ),
    element('div', { 'data-query-tab-panel': tab }, panel),
  )
}

/**
 * Keyed `data_query` view. All settled data is rebuilt from `block.meta` on
 * every render; the only Client state is view preference (tab, sort, copy).
 */
export function DataQueryRow(props: DataQueryViewProps): ReactNode {
  if (!isSettled(props.block)) return fallback(props, 'Data query is running')
  const meta = parseDataQueryMeta(props.block.meta)
  if (meta === null) {
    const detail = props.block.meta === undefined ? 'result metadata is unavailable' : 'unsupported or invalid result metadata'
    return fallback(props, 'Data query result is not renderable', detail)
  }
  const status = meta.status === 'success' ? 'success' : 'error'
  const stats = `${meta.stats.returnedRows} row(s) · ${Math.round(meta.stats.durationMs)} ms${meta.stats.truncated ? ' · preview truncated' : ''}`
  const error = meta.error === undefined ? null : element('p', { 'data-query-error': meta.error.code }, `${meta.error.code}: ${meta.error.message}`)
  return element('section', { 'data-dsh-wren-data-query': status, 'data-query-id': meta.queryId },
    element('header', { 'data-query-header': true },
      element('strong', { 'data-query-status': status }, status === 'success' ? 'Success' : 'Error'),
      element('span', { 'data-query-stats': true }, stats),
    ),
    error,
    element(DataQueryTabs, { meta }),
  )
}

/** Required Harness service for the keyed slot. */
export const inject = ['slots'] as const

/** Register the data-query result view without modifying Harness core. */
export function apply(ctx: DataQueryClientContext): void {
  ctx.slots.inject('tool.call.toolview', () =>
    ctx.slots.register({ name: 'tool.call.toolview', key: DATA_QUERY_TOOL_NAME }, DataQueryRow))
}

// Register only the chart families/components used by this MVP. This keeps the
// generated lazy-CJS artifact smaller than importing the all-in-one ECharts
// bundle while retaining ECharts' normal resize/dispose lifecycle.
echarts.use([LineChart, BarChart, PieChart, TitleComponent, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])
