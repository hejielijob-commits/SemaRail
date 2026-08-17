/**
 * Browser half of the Wren data-agent plugin.
 *
 * This file intentionally keeps the first Client implementation self-contained
 * and imports only React, which is a Harness web platform module. The shared
 * contract package can replace the local runtime guard after its API settles;
 * no cross-plugin value import is needed for this adapter.
 */
import { createElement, type ReactNode } from 'react'

/** Wire tool name registered by the Host half. */
export const DATA_QUERY_TOOL_NAME = 'data_query' as const

/** Harness slot surface used by this out-of-tree plugin. */
export interface DataQuerySlots {
  inject(name: string, callback: () => unknown): unknown
  register(options: { name: string; key?: string; locale?: string }, component: unknown): () => void
}

/** Minimal Client context needed by a Harness slot plugin. */
export interface DataQueryClientContext {
  slots: DataQuerySlots
}

/** The settled ToolResultNode subset consumed by this view. */
export interface DataQueryResultBlock {
  readonly kind: 'tool-result'
  readonly callId: string
  readonly content: readonly unknown[]
  readonly isError: boolean
  readonly error?: { readonly name: string; readonly code: string }
  readonly meta?: unknown
}

/** The running ToolCall subset consumed by the safe fallback. */
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

/** Version-one result metadata shape accepted by the local fail-closed guard. */
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

/** JSON-safe scalar shown in the result table. */
export type DataQueryScalar = string | number | boolean | null

/** Bounded query statistics. */
export interface DataQueryStats {
  readonly returnedRows: number
  readonly durationMs: number
  readonly truncated: boolean
}

/** Chart recommendation rendered as a non-interactive hint in the MVP. */
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
  readonly code: string
  readonly phase: string
  readonly message: string
  readonly retryable: boolean
}

const MAX_META_TEXT = 4_000
const MAX_COLUMNS = 64
const MAX_ROWS = 200
const MAX_ROW_KEYS = 64
const MAX_STRING = 4_000

/** Return true for a plain JSON object, excluding arrays and null. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
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
  if (!isRecord(value)) return undefined
  const name = boundedString(value.name, 256)
  const type = boundedString(value.type, 128)
  const semanticRole = value.semanticRole
  if (name === undefined || type === undefined || (semanticRole !== 'dimension' && semanticRole !== 'measure')) return undefined
  return { name, type, semanticRole }
}

function parseStats(value: unknown): DataQueryStats | undefined {
  if (!isRecord(value)) return undefined
  const returnedRows = value.returnedRows
  const durationMs = value.durationMs
  const truncated = value.truncated
  if (typeof returnedRows !== 'number' || !Number.isSafeInteger(returnedRows) || returnedRows < 0 || returnedRows > 1_000_000) return undefined
  if (typeof durationMs !== 'number' || !Number.isFinite(durationMs) || durationMs < 0 || durationMs > 86_400_000) return undefined
  if (typeof truncated !== 'boolean') return undefined
  return { returnedRows, durationMs, truncated }
}

function parseChart(value: unknown): DataQueryChart | undefined {
  if (!isRecord(value) || value.version !== 1 || value.tooltip !== true) return undefined
  const type = value.type
  const x = boundedString(value.x, 256)
  const y = value.y
  if ((type !== 'line' && type !== 'bar' && type !== 'pie') || x === undefined || !Array.isArray(y) || y.length === 0 || y.length > 8) return undefined
  const parsedY = y.map(item => boundedString(item, 256))
  if (parsedY.some(item => item === undefined)) return undefined
  const title = value.title === undefined ? undefined : boundedString(value.title, 200)
  const series = value.series === undefined ? undefined : boundedString(value.series, 256)
  if (value.title !== undefined && title === undefined) return undefined
  if (value.series !== undefined && series === undefined) return undefined
  return {
    version: 1,
    type,
    x,
    y: parsedY as string[],
    tooltip: true,
    ...(title === undefined ? {} : { title }),
    ...(series === undefined ? {} : { series }),
  }
}

function parseError(value: unknown): DataQueryError | undefined {
  if (!isRecord(value)) return undefined
  const code = boundedString(value.code, 128)
  const phase = boundedString(value.phase, 64)
  const message = boundedString(value.message, MAX_STRING)
  if (code === undefined || phase === undefined || message === undefined || typeof value.retryable !== 'boolean') return undefined
  return { code, phase, message, retryable: value.retryable }
}

/**
 * Parse the durable `tool/result.meta` value for this view.
 *
 * The guard deliberately accepts exactly schema version 1 and returns null for
 * future, incomplete, oversized, or malformed metadata. That keeps replay of
 * old conversations safe when the Host and Client versions do not match.
 */
export function parseDataQueryMeta(value: unknown): DataQueryMeta | null {
  if (!isRecord(value) || value.schemaVersion !== 1) return null
  const queryId = boundedString(value.queryId, 128)
  const semanticSql = boundedString(value.semanticSql, 64_000)
  if (queryId === undefined || semanticSql === undefined) return null
  if (value.status !== 'success' && value.status !== 'error') return null
  if (!Array.isArray(value.columns) || value.columns.length > MAX_COLUMNS) return null
  const columns = value.columns.map(parseColumn)
  if (columns.some(column => column === undefined)) return null
  const parsedColumns = columns as DataQueryMetaColumn[]
  const names = new Set(parsedColumns.map(column => column.name))
  if (names.size !== parsedColumns.length || !Array.isArray(value.previewRows) || value.previewRows.length > MAX_ROWS) return null
  const previewRows: Readonly<Record<string, DataQueryScalar>>[] = []
  for (const candidate of value.previewRows) {
    if (!isRecord(candidate) || Object.keys(candidate).length > MAX_ROW_KEYS) return null
    const row: Record<string, DataQueryScalar> = {}
    for (const [key, raw] of Object.entries(candidate)) {
      if (!names.has(key)) return null
      const item = scalar(raw)
      if (item === undefined) return null
      row[key] = item
    }
    if (Object.keys(row).length !== parsedColumns.length) return null
    previewRows.push(row)
  }
  const stats = parseStats(value.stats)
  if (stats === undefined || previewRows.length > stats.returnedRows || (!stats.truncated && previewRows.length !== stats.returnedRows)) return null
  const nativeSql = value.nativeSql === undefined ? undefined : boundedString(value.nativeSql, 64_000)
  if (value.nativeSql !== undefined && nativeSql === undefined) return null
  const chart = value.chart === undefined ? undefined : parseChart(value.chart)
  if (value.chart !== undefined && chart === undefined) return null
  const error = value.error === undefined ? undefined : parseError(value.error)
  if (value.status === 'success' && error !== undefined) return null
  if (value.status === 'error' && error === undefined) return null
  const encodedSize = JSON.stringify(value).length
  if (encodedSize > 1_100_000) return null
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

function textContent(block: DataQueryResultBlock): string {
  const values: string[] = []
  for (const item of block.content) {
    if (isRecord(item) && item.type === 'text' && typeof item.text === 'string') values.push(item.text)
    else {
      try { values.push(JSON.stringify(item)) } catch { values.push('[unavailable result]') }
    }
  }
  return values.join('\n').slice(0, MAX_META_TEXT)
}

function isSettled(block: DataQueryViewProps['block']): block is DataQueryResultBlock {
  return isRecord(block) && block.kind === 'tool-result'
}

function cellText(value: DataQueryScalar): string {
  return value === null ? 'null' : String(value)
}

function element(type: string, props: Record<string, unknown> | null, ...children: ReactNode[]): ReactNode {
  return createElement(type, props, ...children)
}

function fallback(props: DataQueryViewProps, reason: string, detail?: string): ReactNode {
  const text = detail === undefined ? reason : `${reason}: ${detail}`
  return element('div', { 'data-dsh-wren-data-query': 'fallback', 'data-tool': props.toolName, role: 'status' }, text)
}

function resultTable(meta: DataQueryMeta): ReactNode {
  const headers = meta.columns.map(column => element('th', { key: column.name, scope: 'col' }, column.name))
  const rows = meta.previewRows.map((row, index) => element(
    'tr',
    { key: `${meta.queryId}-${index}` },
    ...meta.columns.map(column => element('td', { key: column.name }, cellText(row[column.name]))),
  ))
  return element(
    'table',
    { 'data-query-table': true },
    element('thead', null, element('tr', null, ...headers)),
    element('tbody', null, ...rows),
  )
}

/**
 * Keyed `data_query` view. The table is intentionally plain HTML so it does
 * not depend on another Harness UI package and can be loaded independently.
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
  const error = meta.error === undefined ? null : element(
    'p',
    { 'data-query-error': meta.error.code },
    `${meta.error.code}: ${meta.error.message}`,
  )
  const chart = meta.chart === undefined ? null : element(
    'p',
    { 'data-query-chart': meta.chart.type },
    `Chart hint: ${meta.chart.type} (${meta.chart.x} → ${meta.chart.y.join(', ')})`,
  )
  return element(
    'section',
    { 'data-dsh-wren-data-query': status, 'data-query-id': meta.queryId },
    element('header', null,
      element('strong', null, 'Data query'),
      element('span', { 'data-query-stats': true }, stats),
    ),
    error,
    chart,
    resultTable(meta),
    element('details', null,
      element('summary', null, 'Query details'),
      element('pre', { 'data-query-semantic-sql': true }, meta.semanticSql),
    ),
  )
}

/** Required Harness service for the keyed slot. */
export const inject = ['slots'] as const

/** Register the data-query result view without modifying Harness core. */
export function apply(ctx: DataQueryClientContext): void {
  ctx.slots.inject('tool.call.toolview', () =>
    ctx.slots.register({ name: 'tool.call.toolview', key: DATA_QUERY_TOOL_NAME }, DataQueryRow))
}
