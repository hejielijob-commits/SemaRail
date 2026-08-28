/**
 * Browser half of the Wren data-agent plugin.
 *
 * The view is deliberately a pure projection of the durable `tool/result.meta`
 * payload. It does not read tool text, running arguments, session state, or an
 * in-memory query cache when a result is settled. A replay or hard refresh is
 * therefore equivalent to the first render of the same tool result.
 */
import { createElement, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
import type { SidebarFooterActionOwnerProps } from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type { ToolCallOwnerProps } from '@deepseek-ai/dsh-client-ui-tool/client'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

/** Wire tool name registered by the Host half. */
export const DATA_QUERY_TOOL_NAME = 'data_query' as const

/** Public sidebar slot owner share used by the rc.10-compatible action. */
export type SemanticConsoleSidebarActionProps = SidebarFooterActionOwnerProps

/** Declare the public sidebar child slot without importing Harness UI code. */
declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface SlotMap {
    'sidebar.footer.action': {
      kind: 'list'
      scope: 'root'
      owner: SemanticConsoleSidebarActionProps
    }
  }
}

/** rc.10-compatible Client context: the public runtime owns the SlotRegistry. */
export type DataQueryClientContext = Pick<ClientContext, 'slots'>

/** Default local semantic console endpoint used when no browser override exists. */
export const DEFAULT_SEMANTIC_CONSOLE_URL = 'http://127.0.0.1:48763' as const

/** Browser-only override key; the value is validated before it is used as a URL. */
export const SEMANTIC_CONSOLE_URL_STORAGE_KEY = 'dsh-wren-data-agent.semantic-console-url' as const

const MAX_SEMANTIC_CONSOLE_URL = 2_048

/**
 * Validate a configurable console URL as a navigable HTTP(S) origin.
 * Credentials and malformed/oversized values are rejected to keep links from
 * becoming a credential-bearing or script URL sink.
 *
 * @param value - candidate URL from plugin config or browser storage.
 * @returns canonical URL, or undefined when the candidate is unsafe.
 */
export function parseSemanticConsoleUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  if (trimmed.length === 0 || trimmed.length > MAX_SEMANTIC_CONSOLE_URL) return undefined
  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    return undefined
  }
  if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:') || parsed.hostname.length === 0) return undefined
  if (parsed.username !== '' || parsed.password !== '') return undefined
  return parsed.href
}

function readSemanticConsoleStorage(): string | undefined {
  if (typeof globalThis === 'undefined' || !('localStorage' in globalThis)) return undefined
  try {
    return parseSemanticConsoleUrl(globalThis.localStorage.getItem(SEMANTIC_CONSOLE_URL_STORAGE_KEY))
  } catch {
    // Storage can throw in privacy mode or for an opaque origin. Keep the
    // loopback fallback deterministic and do not make rendering fail.
    return undefined
  }
}

/**
 * Resolve the explicit value, browser-local override, or loopback default.
 * The Host has no public rc.10 Client config injection, so this intentionally
 * does not pretend that a server-side `consoleUrl` reached the browser.
 *
 * @param explicit - optional plugin-local value supplied by an embedding host.
 * @returns a validated HTTP(S) URL.
 */
export function resolveSemanticConsoleUrl(explicit?: unknown): string {
  return parseSemanticConsoleUrl(explicit)
    ?? readSemanticConsoleStorage()
    ?? DEFAULT_SEMANTIC_CONSOLE_URL
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
export interface DataQueryViewProps extends Pick<ToolCallOwnerProps, 'callId' | 'toolName' | 'cwd'> {
  readonly block: DataQueryResultBlock | DataQueryRunningBlock
  readonly openFile?: ToolCallOwnerProps['openFile']
  readonly inspect?: ToolCallOwnerProps['inspect']
  /** Optional embedding override; normal Harness owners do not provide it. */
  readonly semanticConsoleUrl?: string
}

/** Version-one result metadata shape accepted by the fail-closed guard. */
export interface DataQueryMeta {
  readonly schemaVersion: 1
  readonly queryId: string
  readonly status: 'success' | 'error'
  readonly semanticSql: string
  readonly question?: string
  readonly sqlHistory?: readonly DataQuerySqlHistoryReference[]
  readonly nativeSql?: string
  readonly columns: readonly DataQueryMetaColumn[]
  readonly previewRows: readonly Readonly<Record<string, DataQueryScalar>>[]
  readonly chart?: DataQueryChart
  readonly stats: DataQueryStats
  readonly error?: DataQueryError
}

/** Confirmed Wren NL-to-SQL example actually recalled for this query. */
export interface DataQuerySqlHistoryReference {
  readonly id: string
  readonly question: string
  readonly sql: string
  readonly sourcePath?: string
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
export const DATA_QUERY_PAGE_SIZE = 20

/** Card-only styles; every selector is scoped below this plugin's root. */
const SEMANTIC_CONSOLE_CARD_STYLES = `
[data-dsh-wren-data-query] [data-query-header] { display: flex; min-width: 0; align-items: center; gap: 10px; padding: 13px 16px 11px; }
[data-dsh-wren-data-query] [data-query-header] [data-query-semantic-console] { margin-left: auto; }
[data-dsh-wren-data-query] [data-query-semantic-console] { display: inline-flex; min-height: 30px; align-items: center; justify-content: center; gap: 6px; padding: 5px 10px; border: 1px solid var(--wren-border, var(--dsw-alias-border-l2, color-mix(in srgb, CanvasText 14%, transparent))); border-radius: 7px; background: var(--wren-bg, var(--dsw-alias-bg-layer-1, Canvas)); color: var(--wren-text-secondary, var(--dsw-alias-label-secondary, color-mix(in srgb, CanvasText 72%, transparent))); font: inherit; font-size: 12px; text-decoration: none; cursor: pointer; transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease; }
[data-dsh-wren-data-query] [data-query-semantic-console]:hover { border-color: var(--wren-border-strong, var(--dsw-alias-border-l3, color-mix(in srgb, CanvasText 22%, transparent))); background: var(--wren-bg-hover, var(--dsw-alias-interactive-bg-hover, color-mix(in srgb, CanvasText 7%, transparent))); color: var(--wren-text, var(--dsw-alias-label-primary, CanvasText)); }
[data-dsh-wren-data-query] [data-query-semantic-console]:focus-visible { outline: 2px solid var(--wren-accent, var(--dsw-alias-brand-primary-new-colorprimary-new-color, #4176e6)); outline-offset: 2px; }
`

/** Sidebar-only styles carried by the action so it works before any card exists. */
const SEMANTIC_CONSOLE_SIDEBAR_STYLES = `
[data-wren-semantic-console-action] { display: inline-flex; width: 100%; min-height: 36px; align-items: center; justify-content: flex-start; gap: 6px; padding: 5px 10px; border: 1px solid transparent; border-radius: 7px; background: transparent; color: var(--wren-text-secondary, var(--dsw-alias-label-secondary, color-mix(in srgb, CanvasText 72%, transparent))); font: inherit; font-size: 12px; text-decoration: none; cursor: pointer; transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease; }
[data-wren-semantic-console-action]:hover { border-color: var(--wren-border-strong, var(--dsw-alias-border-l3, color-mix(in srgb, CanvasText 22%, transparent))); background: var(--wren-bg-hover, var(--dsw-alias-interactive-bg-hover, color-mix(in srgb, CanvasText 7%, transparent))); color: var(--wren-text, var(--dsw-alias-label-primary, CanvasText)); }
[data-wren-semantic-console-action]:focus-visible { outline: 2px solid var(--wren-accent, var(--dsw-alias-brand-primary-new-colorprimary-new-color, #4176e6)); outline-offset: 2px; }
[data-wren-semantic-console-action][data-sidebar-wide="false"] { width: 36px; margin: 0 auto; padding: 5px; }
[data-wren-semantic-console-icon] { display: inline-grid; width: 18px; height: 18px; place-items: center; font-size: 15px; line-height: 1; }
`

/**
 * The Client artifact has no CSS entry point in Harness rc.10. Keep the
 * styles scoped to this tool view so the plugin remains portable and cannot
 * affect Harness core. Values prefer Harness theme tokens and retain system
 * fallbacks.
 */
const DATA_QUERY_STYLES = `
[data-dsh-wren-data-query] {
  --wren-bg: var(--dsw-alias-bg-layer-1, Canvas);
  --wren-bg-subtle: var(--dsw-alias-bg-module-platform, color-mix(in srgb, CanvasText 5%, Canvas));
  --wren-bg-hover: var(--dsw-alias-interactive-bg-hover, color-mix(in srgb, CanvasText 7%, transparent));
  --wren-border: var(--dsw-alias-border-l2, color-mix(in srgb, CanvasText 14%, transparent));
  --wren-border-strong: var(--dsw-alias-border-l3, color-mix(in srgb, CanvasText 22%, transparent));
  --wren-text: var(--dsw-alias-label-primary, CanvasText);
  --wren-text-secondary: var(--dsw-alias-label-secondary, color-mix(in srgb, CanvasText 72%, transparent));
  --wren-text-tertiary: var(--dsw-alias-label-tertiary, color-mix(in srgb, CanvasText 58%, transparent));
  --wren-accent: var(--dsw-alias-brand-primary-new-colorprimary-new-color, #4176e6);
  --wren-success: var(--dsw-alias-state-success-primary, #22875a);
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  margin: 12px 0;
  overflow: hidden;
  border: 1px solid var(--wren-border);
  border-radius: 12px;
  background: var(--wren-bg);
  color: var(--wren-text);
  font: var(--dsw-font-s-14, 14px/1.5 system-ui, sans-serif);
}

[data-dsh-wren-data-query] *, [data-dsh-wren-data-query] *::before, [data-dsh-wren-data-query] *::after { box-sizing: border-box; }
${SEMANTIC_CONSOLE_CARD_STYLES}
[data-query-status] { display: inline-flex; align-items: center; min-height: 24px; padding: 2px 9px; border-radius: 6px; background: color-mix(in srgb, var(--wren-success) 12%, transparent); color: var(--wren-success); font-weight: 600; font-size: 12px; line-height: 18px; }
[data-dsh-wren-data-query="error"] [data-query-status] { background: color-mix(in srgb, var(--dsw-alias-state-error-primary, #c23b3b) 12%, transparent); color: var(--dsw-alias-state-error-primary, #c23b3b); }
[data-query-stats] { display: flex; min-width: 0; flex-wrap: wrap; align-items: center; gap: 4px 12px; color: var(--wren-text-secondary); font-size: 12px; }
[data-query-stats] span + span { position: relative; }
[data-query-stats] span + span::before { position: absolute; left: -7px; top: 50%; width: 2px; height: 2px; border-radius: 50%; background: var(--wren-text-tertiary); content: ""; }
[data-query-error] { margin: 0 16px 12px; padding: 10px 12px; border-radius: 8px; background: color-mix(in srgb, var(--dsw-alias-state-error-primary, #c23b3b) 9%, transparent); color: var(--wren-text); overflow-wrap: anywhere; }
[data-query-tabs] { min-width: 0; }
[data-query-tabs] [role="tablist"] { display: flex; gap: 4px; padding: 0 12px; border-bottom: 1px solid var(--wren-border); }
[data-query-tabs] [role="tab"] { position: relative; min-width: 72px; min-height: 38px; padding: 8px 12px; border: 0; border-radius: 8px 8px 0 0; background: transparent; color: var(--wren-text-secondary); font: inherit; font-weight: 500; cursor: pointer; transition: background-color 120ms ease, color 120ms ease; }
[data-query-tabs] [role="tab"]::after { position: absolute; right: 12px; bottom: -1px; left: 12px; height: 2px; border-radius: 2px 2px 0 0; background: transparent; content: ""; }
[data-query-tabs] [role="tab"]:hover:not(:disabled) { background: var(--wren-bg-hover); color: var(--wren-text); }
[data-query-tabs] [role="tab"][aria-selected="true"] { color: var(--wren-text); }
[data-query-tabs] [role="tab"][aria-selected="true"]::after { background: var(--wren-accent); }
[data-query-tabs] [role="tab"]:disabled { cursor: not-allowed; opacity: .42; }
[data-dsh-wren-data-query] button:focus-visible, [data-dsh-wren-data-query] [tabindex="0"]:focus-visible { outline: 2px solid var(--wren-accent); outline-offset: 2px; }
[data-query-tab-panel] { min-width: 0; padding: 14px 16px 16px; }
[data-query-chart-shell] { min-width: 0; overflow: hidden; border-radius: 8px; background: var(--wren-bg-subtle); }
[data-query-chart-canvas] { width: 100%; min-height: 320px; }
[data-query-chart-fallback], [data-query-empty] { display: grid; min-height: 180px; place-items: center; padding: 24px; color: var(--wren-text-tertiary); text-align: center; }
[data-query-table-shell] { min-width: 0; overflow: hidden; border: 1px solid var(--wren-border); border-radius: 8px; }
[data-query-table-scroll] { max-width: 100%; overflow: auto; }
[data-query-table] { width: 100%; min-width: max-content; border-collapse: separate; border-spacing: 0; font-variant-numeric: tabular-nums; }
[data-query-table] th { position: sticky; top: 0; z-index: 1; padding: 0; border-bottom: 1px solid var(--wren-border-strong); background: var(--wren-bg-subtle); color: var(--wren-text-secondary); text-align: left; }
[data-query-column-sort] { display: flex; width: 100%; min-width: 120px; align-items: center; justify-content: space-between; gap: 10px; padding: 9px 12px; border: 0; background: transparent; color: inherit; font: inherit; font-weight: 600; text-align: left; cursor: pointer; }
[data-query-column-sort]:hover { background: var(--wren-bg-hover); color: var(--wren-text); }
[data-query-sort-indicator] { width: 12px; color: var(--wren-text-tertiary); font-size: 10px; text-align: center; }
[data-query-table] td { max-width: 360px; padding: 9px 12px; border-bottom: 1px solid var(--wren-border); color: var(--wren-text); overflow-wrap: anywhere; vertical-align: top; }
[data-query-table] tbody tr:last-child td { border-bottom: 0; }
[data-query-table] tbody tr:hover td { background: var(--wren-bg-hover); }
[data-query-null] { color: var(--wren-text-tertiary); font-style: italic; }
[data-query-pagination] { display: flex; min-height: 48px; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; padding: 9px 10px 9px 12px; border-top: 1px solid var(--wren-border); color: var(--wren-text-secondary); font-size: 12px; }
[data-query-pagination-actions] { display: flex; align-items: center; gap: 8px; }
[data-query-page-label] { min-width: 88px; color: var(--wren-text); text-align: center; font-variant-numeric: tabular-nums; }
[data-query-pagination] button, .data-query-sql-toolbar button { min-height: 30px; padding: 5px 10px; border: 1px solid var(--wren-border); border-radius: 7px; background: var(--wren-bg); color: var(--wren-text); font: inherit; font-size: 12px; cursor: pointer; transition: background-color 120ms ease, border-color 120ms ease; }
[data-query-pagination] button:hover:not(:disabled), .data-query-sql-toolbar button:hover:not(:disabled) { border-color: var(--wren-border-strong); background: var(--wren-bg-hover); }
[data-query-pagination] button:active:not(:disabled), .data-query-sql-toolbar button:active:not(:disabled) { transform: translateY(1px); }
[data-query-pagination] button:disabled, .data-query-sql-toolbar button:disabled { cursor: not-allowed; opacity: .42; }
[data-query-sql] { min-width: 0; overflow: hidden; border: 1px solid var(--wren-border); border-radius: 8px; background: var(--dsw-alias-markdown-code-block, var(--wren-bg-subtle)); }
[data-query-sql-heading] { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.data-query-sql-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px; border-bottom: 1px solid var(--wren-border); }
.data-query-sql-toolbar [aria-pressed="true"] { border-color: var(--wren-accent); background: color-mix(in srgb, var(--wren-accent) 12%, transparent); color: var(--wren-text); }
.data-query-sql-modes, .data-query-sql-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.data-query-sql-actions { margin-left: auto; }
[data-query-sql-history] { padding: 14px 16px; border-bottom: 1px solid var(--wren-border); background: var(--wren-bg); }
[data-query-sql-history] h4 { margin: 0 0 4px; color: var(--wren-text); font-size: 13px; }
[data-query-sql-history] > p { margin: 0; color: var(--wren-text-tertiary); font-size: 12px; }
[data-query-sql-history] details { margin-top: 8px; border: 1px solid var(--wren-border); border-radius: 7px; background: var(--wren-bg-subtle); }
[data-query-sql-history] summary { display: flex; min-width: 0; cursor: pointer; gap: 8px; padding: 9px 10px; color: var(--wren-text-secondary); font-size: 12px; }
[data-query-sql-history] summary span:first-child { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
[data-query-sql-history] summary code { color: var(--wren-text-tertiary); font-size: 11px; }
[data-query-sql-history] pre { max-height: 220px; margin: 0; overflow: auto; border-top: 1px solid var(--wren-border); padding: 12px; color: var(--wren-text); font: 12px/1.65 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
[data-query-sql-submit-state] { color: var(--wren-text-tertiary); font-size: 12px; }
[data-query-sql-submit-state="pending"] { color: var(--dsw-alias-state-success-primary, #22875a); }
[data-query-sql-submit-state="error"] { color: var(--dsw-alias-state-error-primary, #c33b43); }
[data-query-sql-code-block] { width: 100%; max-width: 100%; max-height: 440px; margin: 0; overflow: auto; padding: 16px; color: var(--wren-text); font: var(--dsw-font-markdown-code-block, 13px/1.7 ui-monospace, SFMono-Regular, Consolas, monospace); tab-size: 2; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
[data-query-sql-code] { display: block; min-width: 0; white-space: inherit; overflow-wrap: inherit; word-break: inherit; }
.sql-keyword { color: var(--dsw-static-deepseek-500, #356ac3); font-weight: 600; }
.sql-number { color: var(--dsw-alias-state-warn-label, #9a5b13); }
.sql-string { color: var(--dsw-alias-state-success-primary, #22875a); }
.sql-comment { color: var(--wren-text-tertiary); font-style: italic; }
.sql-identifier { color: var(--wren-text); }
@media (max-width: 640px) {
  [data-query-header] { align-items: flex-start; flex-direction: column; gap: 6px; }
  [data-query-tabs] [role="tablist"] { padding: 0 6px; }
  [data-query-tabs] [role="tab"] { min-width: 0; flex: 1 1 0; }
  [data-query-tab-panel] { padding: 10px; }
  [data-query-chart-canvas] { min-height: 260px; }
  [data-query-pagination] { align-items: stretch; flex-direction: column; }
  [data-query-pagination-actions] { justify-content: space-between; }
}
@media (prefers-reduced-motion: reduce) {
  [data-dsh-wren-data-query] button { transition: none !important; }
}
`

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
  if (!isRecord(value) || !hasOnlyKeys(value, ['schemaVersion', 'queryId', 'status', 'semanticSql', 'question', 'sqlHistory', 'nativeSql', 'columns', 'previewRows', 'chart', 'stats', 'error'])) return null
  if (value.schemaVersion !== 1 || (value.status !== 'success' && value.status !== 'error')) return null
  const queryId = boundedString(value.queryId, 128)
  const semanticSql = boundedString(value.semanticSql, MAX_SQL)
  if (queryId === undefined || semanticSql === undefined) return null
  const question = value.question === undefined ? undefined : boundedString(value.question, 16_000)
  if (value.question !== undefined && question === undefined) return null
  let sqlHistory: DataQuerySqlHistoryReference[] | undefined
  if (value.sqlHistory !== undefined) {
    if (!Array.isArray(value.sqlHistory) || value.sqlHistory.length > 5) return null
    sqlHistory = []
    for (const raw of value.sqlHistory) {
      if (!isRecord(raw) || !hasOnlyKeys(raw, ['id', 'question', 'sql', 'sourcePath'])) return null
      const id = boundedString(raw.id, 128)
      const historicalQuestion = boundedString(raw.question, 16_000)
      const sql = boundedString(raw.sql, MAX_SQL)
      const sourcePath = raw.sourcePath === undefined ? undefined : boundedString(raw.sourcePath, 512)
      if (id === undefined || historicalQuestion === undefined || sql === undefined || (raw.sourcePath !== undefined && sourcePath === undefined)) return null
      sqlHistory.push({ id, question: historicalQuestion, sql, ...(sourcePath === undefined ? {} : { sourcePath }) })
    }
  }
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
    ...(question === undefined ? {} : { question }),
    ...(sqlHistory === undefined ? {} : { sqlHistory }),
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

/** Props for the shared semantic-console auxiliary link. */
export interface SemanticConsoleLinkProps {
  /** Expanded sidebar state; false renders only the icon and tooltip. */
  readonly wide?: boolean
  /** Optional embedding override; otherwise storage/default resolution applies. */
  readonly consoleUrl?: string
  /** Link surface, used only for stable test/accessibility hooks and styles. */
  readonly surface?: 'card' | 'sidebar'
}

/**
 * Render a safe external link to the semantic console.
 *
 * `target` plus `rel` keeps an external page from receiving an opener handle;
 * the URL is canonicalized by {@link resolveSemanticConsoleUrl} and therefore
 * cannot become a `javascript:`, `data:`, or credential-bearing navigation.
 *
 * @param props - surface and optional URL configuration.
 * @returns an accessible external anchor.
 */
export function SemanticConsoleLink({ wide = true, consoleUrl, surface = 'card' }: SemanticConsoleLinkProps): ReactNode {
  const sidebar = surface === 'sidebar'
  const compact = sidebar && !wide
  const label = '语义层管理'
  return element('a', {
    href: resolveSemanticConsoleUrl(consoleUrl),
    target: '_blank',
    rel: 'noopener noreferrer',
    referrerPolicy: 'no-referrer',
    ...(sidebar ? {
      'data-wren-semantic-console-action': true,
      'data-sidebar-wide': String(wide),
      ...(compact ? { 'aria-label': label, title: label } : {}),
    } : {
      'data-query-semantic-console': true,
      'aria-label': label,
    }),
  }, compact
    ? element('span', { 'data-wren-semantic-console-icon': true, 'aria-hidden': true }, '↗')
    : label)
}

/** Sidebar footer action registered through the public list slot. */
export function SemanticConsoleSidebarAction(props: SemanticConsoleSidebarActionProps): ReactNode {
  return element('div', { 'data-wren-semantic-console-root': true },
    element('style', { 'data-wren-semantic-console-style': true }, SEMANTIC_CONSOLE_SIDEBAR_STYLES),
    element(SemanticConsoleLink, { wide: props.wide, surface: 'sidebar' }))
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
  readonly submissionState?: 'idle' | 'submitting' | 'pending' | 'error'
  readonly onSubmit?: () => void
}

/** Render the selected SQL directly; the SQL tab is the only disclosure. */
export function DataQuerySqlView({ meta, mode, copied, onModeChange, onCopy, submissionState = 'idle', onSubmit }: DataQuerySqlViewProps): ReactNode {
  const nativeAvailable = meta.nativeSql !== undefined
  const selectedMode = mode === 'native' && nativeAvailable ? 'native' : 'semantic'
  const sql = selectedMode === 'native' ? meta.nativeSql as string : meta.semanticSql
  return element('section', { 'data-query-sql': true, role: 'region', 'aria-label': 'SQL' },
    element('div', { 'data-query-sql-heading': true }, 'SQL'),
    element('div', { className: 'data-query-sql-toolbar' },
      element('div', { className: 'data-query-sql-modes', role: 'group', 'aria-label': 'SQL source' },
        element('button', { type: 'button', 'data-query-sql-mode': 'semantic', 'aria-pressed': selectedMode === 'semantic', onClick: () => onModeChange('semantic') }, 'Semantic SQL'),
        element('button', { type: 'button', 'data-query-sql-mode': 'native', 'aria-pressed': selectedMode === 'native', disabled: !nativeAvailable, onClick: () => { if (nativeAvailable) onModeChange('native') } }, 'Native SQL'),
      ),
      element('div', { className: 'data-query-sql-actions', role: 'group', 'aria-label': 'SQL actions' },
        element('button', {
          type: 'button',
          'data-query-sql-submit': true,
          disabled: meta.status !== 'success' || submissionState === 'submitting' || submissionState === 'pending' || onSubmit === undefined,
          onClick: onSubmit,
        }, submissionState === 'submitting' ? 'Submitting…' : submissionState === 'pending' ? 'Pending review' : submissionState === 'error' ? 'Retry submission' : 'Record for review'),
        element('button', { type: 'button', 'data-query-sql-copy': true, onClick: onCopy }, copied ? 'Copied' : 'Copy'),
      ),
    ),
    element('div', { 'data-query-sql-submit-state': submissionState, role: 'status', 'aria-live': 'polite' },
      submissionState === 'pending' ? 'Saved to the review queue. It is not active Wren memory until approved.'
        : submissionState === 'error' ? 'Could not reach the semantic console. Nothing was recorded.' : ''),
    element('section', { 'data-query-sql-history': true, 'aria-label': 'Confirmed SQL history used' },
      element('h4', null, 'Confirmed SQL references'),
      meta.sqlHistory === undefined || meta.sqlHistory.length === 0
        ? element('p', null, 'No confirmed historical SQL was recalled for this query.')
        : element('div', null, ...meta.sqlHistory.map(reference => element('details', { key: reference.id },
          element('summary', null,
            element('span', null, reference.question),
            ...(reference.sourcePath === undefined ? [] : [element('code', { key: 'path' }, reference.sourcePath)]),
          ),
          element('pre', null, element(HighlightedSql, { sql: reference.sql })),
        ))),
    ),
    element('pre', { 'data-query-sql-code-block': true, 'data-query-sql-current': selectedMode }, element(HighlightedSql, { sql })),
  )
}

/** Submit one successful query as a pending candidate; approval remains server-side. */
export async function submitSqlCandidate(
  meta: DataQueryMeta,
  consoleUrl?: unknown,
  request: typeof fetch = fetch,
): Promise<{ readonly candidate?: { readonly status?: string }; readonly created?: boolean; readonly duplicate?: boolean }> {
  if (meta.status !== 'success' || meta.question === undefined) throw new Error('query is not eligible for review')
  const endpoint = new URL('/api/knowledge/sql-candidates', resolveSemanticConsoleUrl(consoleUrl)).toString()
  const response = await request(endpoint, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({
      question: meta.question,
      sql: meta.semanticSql,
      dialect: 'wren-semantic',
      queryId: meta.queryId,
      status: 'pending',
      stats: meta.stats,
      sqlHistory: meta.sqlHistory ?? [],
      executionStatus: meta.status,
    }),
  })
  const body = await response.json().catch(() => undefined) as { candidate?: { status?: string }; created?: boolean; duplicate?: boolean; message?: string } | undefined
  if (!response.ok) throw new Error(body?.message ?? `review submission failed (${response.status})`)
  return body ?? {}
}

function DataQuerySql({ meta, consoleUrl }: { readonly meta: DataQueryMeta; readonly consoleUrl?: unknown }): ReactNode {
  const [mode, setMode] = useState<'semantic' | 'native'>('semantic')
  const [copied, setCopied] = useState(false)
  const [submissionState, setSubmissionState] = useState<'idle' | 'submitting' | 'pending' | 'error'>('idle')
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
  const submit = async (): Promise<void> => {
    setSubmissionState('submitting')
    try {
      await submitSqlCandidate(meta, consoleUrl)
      setSubmissionState('pending')
    } catch {
      setSubmissionState('error')
    }
  }
  useEffect(() => setSubmissionState('idle'), [meta.queryId])
  return element(DataQuerySqlView, {
    meta,
    mode,
    copied,
    onModeChange: setMode,
    onCopy: () => { void copySql() },
    submissionState,
    onSubmit: meta.question === undefined ? undefined : () => { void submit() },
  })
}

function DataQueryTable({ meta }: { readonly meta: DataQueryMeta }): ReactNode {
  const [sort, setSort] = useState<{ readonly column: string; readonly descending: boolean } | null>(null)
  const [page, setPage] = useState(0)
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
  const pageCount = Math.max(1, Math.ceil(rows.length / DATA_QUERY_PAGE_SIZE))
  const currentPage = Math.min(page, pageCount - 1)
  const pageStart = currentPage * DATA_QUERY_PAGE_SIZE
  const visibleRows = rows.slice(pageStart, pageStart + DATA_QUERY_PAGE_SIZE)
  useEffect(() => {
    setPage(0)
    setSort(null)
  }, [meta.queryId, meta.previewRows])
  const toggleSort = (column: string): void => {
    setPage(0)
    setSort(current => current?.column === column
      ? { column, descending: !current.descending }
      : { column, descending: false })
  }
  if (rows.length === 0) return element('div', { 'data-query-empty': true, role: 'status' }, 'No rows returned')
  const rangeEnd = Math.min(pageStart + DATA_QUERY_PAGE_SIZE, rows.length)
  return element('div', { 'data-query-table-shell': true },
    element('div', { 'data-query-table-scroll': true },
      element('table', { 'data-query-table': true },
        element('thead', null, element('tr', null,
        ...meta.columns.map(column => element('th', {
          key: column.name,
          scope: 'col',
          'aria-sort': sort?.column === column.name ? (sort.descending ? 'descending' : 'ascending') : 'none',
        }, element('button', {
          type: 'button',
          'data-query-column-sort': column.name,
          onClick: () => toggleSort(column.name),
          'aria-label': `Sort by ${column.name}`,
        },
        element('span', null, column.name),
        element('span', { 'data-query-sort-indicator': true, 'aria-hidden': true }, sort?.column === column.name ? (sort.descending ? '▼' : '▲') : '↕'),
        ))),
        )),
        element('tbody', null, ...visibleRows.map((row, index) => element('tr', { key: `${meta.queryId}-${pageStart + index}` },
          ...meta.columns.map(column => element('td', { key: column.name }, row[column.name] === null
            ? element('span', { 'data-query-null': true }, 'null')
            : cellText(row[column.name]))),
        ))),
      ),
    ),
    element('div', { 'data-query-pagination': true, role: 'navigation', 'aria-label': 'Table pagination' },
      element('span', { 'data-query-row-range': true }, `${pageStart + 1}-${rangeEnd} of ${rows.length} rows`),
      element('div', { 'data-query-pagination-actions': true },
        element('button', { type: 'button', disabled: currentPage === 0, onClick: () => setPage(value => Math.max(0, value - 1)), 'aria-label': 'Previous page' }, 'Previous'),
        element('span', { 'data-query-page-label': true, 'aria-live': 'polite' }, `Page ${currentPage + 1} of ${pageCount}`),
        element('button', { type: 'button', disabled: currentPage >= pageCount - 1, onClick: () => setPage(value => Math.min(pageCount - 1, value + 1)), 'aria-label': 'Next page' }, 'Next'),
      ),
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
  return element('div', { 'data-query-chart-shell': true }, element('div', {
      ref,
      role: 'img',
      'data-query-chart-canvas': meta.chart?.type,
      'aria-label': meta.chart?.title ?? `${meta.chart?.type ?? 'data'} chart`,
    }),
  )
}

/** Stable analysis-first tab order used by the query card. */
export const DATA_QUERY_TAB_ORDER = ['chart', 'table', 'sql'] as const

/** Prefer the visual summary when the result includes a valid chart. */
export const DEFAULT_DATA_QUERY_TAB = 'chart' as const

type QueryTab = typeof DATA_QUERY_TAB_ORDER[number]

function DataQueryTabs({ meta, consoleUrl }: { readonly meta: DataQueryMeta; readonly consoleUrl?: unknown }): ReactNode {
  const chartAvailable = buildDataQueryChartOption(meta) !== null
  const tabIdPrefix = `wren-query-${useId().replace(/:/gu, '')}`
  const tabPanelId = `${tabIdPrefix}-panel`
  const [tab, setTab] = useState<QueryTab>(chartAvailable ? DEFAULT_DATA_QUERY_TAB : 'table')
  const panel = tab === 'chart'
    ? (chartAvailable ? element(DataQueryChartPanel, { meta }) : element('div', { role: 'status' }, 'Chart unavailable for this result'))
    : tab === 'table' ? element(DataQueryTable, { meta }) : element(DataQuerySql, { meta, consoleUrl })
  return element('div', { 'data-query-tabs': true },
    element('nav', { role: 'tablist', 'aria-label': 'Data query views' },
      ...DATA_QUERY_TAB_ORDER.map(item => element('button', {
        key: item,
        type: 'button',
        role: 'tab',
        'data-query-tab': item,
        id: `${tabIdPrefix}-${item}`,
        'aria-controls': tabPanelId,
        'aria-selected': tab === item,
        disabled: item === 'chart' && !chartAvailable,
        tabIndex: tab === item ? 0 : -1,
        onClick: () => setTab(item),
      }, item === 'sql' ? 'SQL' : `${item[0]?.toUpperCase() ?? ''}${item.slice(1)}`)),
    ),
    element('div', { id: tabPanelId, role: 'tabpanel', 'aria-labelledby': `${tabIdPrefix}-${tab}`, 'data-query-tab-panel': tab }, panel),
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
  const error = meta.error === undefined ? null : element('p', { 'data-query-error': meta.error.code }, `${meta.error.code}: ${meta.error.message}`)
  return element('section', { 'data-dsh-wren-data-query': status, 'data-query-id': meta.queryId },
    element('style', null, DATA_QUERY_STYLES),
    element('header', { 'data-query-header': true },
      element('strong', { 'data-query-status': status }, status === 'success' ? 'Success' : 'Error'),
      element('div', { 'data-query-stats': true },
        element('span', null, `${meta.stats.returnedRows} ${meta.stats.returnedRows === 1 ? 'row' : 'rows'}`),
        element('span', null, `${Math.round(meta.stats.durationMs)} ms`),
        ...(meta.stats.truncated ? [element('span', { key: 'truncated' }, 'Preview limited')] : []),
      ),
      element(SemanticConsoleLink, { consoleUrl: props.semanticConsoleUrl, surface: 'card' }),
    ),
    error,
    element(DataQueryTabs, { meta, consoleUrl: props.semanticConsoleUrl }),
  )
}

/** Required Harness service for the keyed Tool and sidebar action slots. */
export const inject = ['slots'] as const

/**
 * Register the data-query result view and additive semantic-console action
 * through public rc.10-compatible slots; Harness core remains untouched.
 *
 * @param ctx - public Client context containing the SlotRegistry.
 */
export function apply(ctx: DataQueryClientContext): void {
  ctx.slots.inject('tool.call.toolview', () =>
    ctx.slots.register({ name: 'tool.call.toolview', key: DATA_QUERY_TOOL_NAME }, DataQueryRow))
  ctx.slots.inject('sidebar.footer.action', () =>
    ctx.slots.register({ name: 'sidebar.footer.action', id: 'wren-semantic-console' }, SemanticConsoleSidebarAction))
}

// Register only the chart families/components used by this MVP. This keeps the
// generated lazy-CJS artifact smaller than importing the all-in-one ECharts
// bundle while retaining ECharts' normal resize/dispose lifecycle.
echarts.use([LineChart, BarChart, PieChart, TitleComponent, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])
