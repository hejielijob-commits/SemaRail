import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import {
  apply,
  buildDataQueryChartOption,
  compareDataQueryCells,
  DATA_QUERY_TAB_ORDER,
  DEFAULT_DATA_QUERY_TAB,
  DataQueryRow,
  DataQuerySqlView,
  DEFAULT_SEMANTIC_CONSOLE_URL,
  parseSemanticConsoleUrl,
  resolveSemanticConsoleUrl,
  SemanticConsoleLink,
  SemanticConsoleSidebarAction,
  SEMANTIC_CONSOLE_URL_STORAGE_KEY,
  parseDataQueryMeta,
  tokenizeSql,
  type DataQueryResultBlock,
} from '../src/client/index.js'

const replayFixture = JSON.parse(readFileSync(new URL('./fixtures/query-result-v1.json', import.meta.url), 'utf8')) as Record<string, any>

function collectElementTypes(value: unknown, types: string[] = []): string[] {
  if (typeof value !== 'object' || value === null) return types
  const candidate = value as { type?: unknown; props?: { children?: unknown } }
  if (typeof candidate.type === 'string') types.push(candidate.type)
  const children = candidate.props?.children
  if (Array.isArray(children)) children.forEach(child => collectElementTypes(child, types))
  else collectElementTypes(children, types)
  return types
}

const validMeta = {
  schemaVersion: 1,
  queryId: 'q-1',
  status: 'success',
  semanticSql: 'select day, revenue from orders',
  columns: [
    { name: 'day', type: 'DATE', semanticRole: 'dimension' },
    { name: 'revenue', type: 'DECIMAL', semanticRole: 'measure' },
  ],
  previewRows: [{ day: '2026-08-17', revenue: '12.50' }],
  stats: { returnedRows: 1, durationMs: 12.4, truncated: false },
} as const

function settled(meta?: unknown): DataQueryResultBlock {
  return {
    kind: 'tool-result',
    callId: 'call-1',
    content: [{ type: 'text', text: 'fallback result' }],
    isError: false,
    ...(meta === undefined ? {} : { meta }),
  }
}

describe('data_query Client adapter', () => {
  it('accepts the version-one result presentation and rejects future versions', () => {
    expect(parseDataQueryMeta(validMeta)?.queryId).toBe('q-1')
    expect(parseDataQueryMeta({ ...validMeta, schemaVersion: 2 })).toBeNull()
  })

  it('fails closed for malformed rows and unknown fields', () => {
    expect(parseDataQueryMeta({ ...validMeta, previewRows: [{ day: 'today', extra: 1 }] })).toBeNull()
    expect(parseDataQueryMeta({ ...validMeta, columns: [{ name: 'day', type: 'DATE', semanticRole: 'dimension' }] })).toBeNull()
  })

  it('renders safe fallback for running and missing metadata', () => {
    const running = DataQueryRow({
      callId: 'call-1', toolName: 'data_query', block: { callId: 'call-1', name: 'data_query', argsRaw: '{}' },
    }) as { props?: Record<string, unknown> }
    expect(running.props?.['data-dsh-wren-data-query']).toBe('fallback')
    const missing = DataQueryRow({ callId: 'call-1', toolName: 'data_query', block: settled() }) as { props?: Record<string, unknown> }
    expect(missing.props?.['data-dsh-wren-data-query']).toBe('fallback')
  })

  it('registers the keyed slot and does not require a cross-plugin value import', () => {
    const register = vi.fn(() => () => undefined)
    const inject = vi.fn((_name: string, callback: () => unknown) => callback())
    apply({ slots: { inject, register } })
    expect(inject).toHaveBeenCalledWith('tool.call.toolview', expect.any(Function))
    expect(inject).toHaveBeenCalledWith('sidebar.footer.action', expect.any(Function))
    expect(register).toHaveBeenCalledWith(
      { name: 'tool.call.toolview', key: 'data_query' },
      DataQueryRow,
    )
    expect(register).toHaveBeenCalledWith(
      { name: 'sidebar.footer.action', id: 'wren-semantic-console' },
      SemanticConsoleSidebarAction,
    )
  })

  it('accepts only absolute HTTP(S) console URLs and falls back to loopback', () => {
    expect(parseSemanticConsoleUrl('https://console.example.test/wren')).toBe('https://console.example.test/wren')
    expect(parseSemanticConsoleUrl('javascript:alert(1)')).toBeUndefined()
    expect(parseSemanticConsoleUrl('data:text/html,owned')).toBeUndefined()
    expect(parseSemanticConsoleUrl('https://user:password@example.test')).toBeUndefined()
    expect(resolveSemanticConsoleUrl('not a URL')).toBe(DEFAULT_SEMANTIC_CONSOLE_URL)
  })

  it('uses the validated browser-local URL override without leaking credentials', () => {
    const values = new Map<string, string>()
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
    }
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
    storage.setItem(SEMANTIC_CONSOLE_URL_STORAGE_KEY, 'https://console.example.test/project')
    expect(resolveSemanticConsoleUrl()).toBe('https://console.example.test/project')
    storage.setItem(SEMANTIC_CONSOLE_URL_STORAGE_KEY, 'javascript:alert(1)')
    expect(resolveSemanticConsoleUrl()).toBe(DEFAULT_SEMANTIC_CONSOLE_URL)
    storage.removeItem(SEMANTIC_CONSOLE_URL_STORAGE_KEY)
  })

  it('renders an external link with opener isolation and collapsed tooltip', () => {
    const wide = SemanticConsoleLink({ wide: true, consoleUrl: 'https://console.example.test', surface: 'card' }) as { props?: Record<string, unknown> }
    expect(wide.props?.href).toBe('https://console.example.test/')
    expect(wide.props?.target).toBe('_blank')
    expect(wide.props?.rel).toBe('noopener noreferrer')
    expect(wide.props?.referrerPolicy).toBe('no-referrer')
    const rail = SemanticConsoleSidebarAction({ wide: false }) as { props?: { children?: unknown } }
    const children = rail.props?.children
    expect(Array.isArray(children)).toBe(true)
    const style = (children as unknown[])[0] as { props?: Record<string, unknown> }
    expect(style.props?.['data-wren-semantic-console-style']).toBe(true)
    expect(style.props?.children).not.toContain('[data-query-semantic-console]')
    const link = SemanticConsoleLink({ wide: false, surface: 'sidebar' }) as { props?: Record<string, unknown> }
    expect(link.props?.['data-sidebar-wide']).toBe('false')
    expect(link.props?.title).toBe('语义层管理')
    expect(link.props?.['aria-label']).toBe('语义层管理')
    const expanded = SemanticConsoleLink({ wide: true, surface: 'sidebar' }) as { props?: Record<string, unknown> }
    expect(expanded.props?.children).toBe('语义层管理')
    expect(expanded.props?.title).toBeUndefined()
  })

  it('rebuilds a fixed replay fixture deterministically from durable metadata', () => {
    const first = parseDataQueryMeta(replayFixture)
    const refreshed = parseDataQueryMeta(JSON.parse(JSON.stringify(replayFixture)))
    expect(first).not.toBeNull()
    expect(refreshed).toEqual(first)
    expect(buildDataQueryChartOption(first!)).toEqual(buildDataQueryChartOption(refreshed!))
  })

  it('limits only previewRows bytes, allowing a legal 1 MiB preview plus full SQL', () => {
    const maxPreviewBytes = 1_048_576
    const emptyEnvelopeBytes = new TextEncoder().encode(JSON.stringify([{ blob: '' }])).byteLength
    const blob = 'x'.repeat(maxPreviewBytes - emptyEnvelopeBytes)
    const boundaryMeta = {
      schemaVersion: 1,
      queryId: 'preview-byte-boundary',
      status: 'success',
      semanticSql: 's'.repeat(64_000),
      nativeSql: 'n'.repeat(64_000),
      columns: [{ name: 'blob', type: 'VARCHAR', semanticRole: 'dimension' }],
      previewRows: [{ blob }],
      stats: { returnedRows: 1, durationMs: 1, truncated: false },
    }
    expect(new TextEncoder().encode(JSON.stringify(boundaryMeta.previewRows)).byteLength).toBe(maxPreviewBytes)
    expect(parseDataQueryMeta(boundaryMeta)).not.toBeNull()
    expect(parseDataQueryMeta({ ...boundaryMeta, previewRows: [{ blob: `${blob}x` }] })).toBeNull()
  })

  it('maps only validated chart fields and keeps raw dimension/value tooltip data', () => {
    const meta = parseDataQueryMeta(replayFixture)
    const option = buildDataQueryChartOption(meta!)
    expect(option?.tooltip).toEqual({ trigger: 'axis' })
    expect(option?.legend.data).toEqual(['revenue'])
    expect(option?.series[0]?.data[0]).toEqual({ name: '2026-08-16', value: '12.50' })
    expect(option).not.toHaveProperty('formatter')
    expect(JSON.stringify(option)).not.toMatch(/javascript:|https?:\/\//iu)

    for (const type of ['line', 'bar', 'pie'] as const) {
      const typed = JSON.parse(JSON.stringify(replayFixture)) as Record<string, any>
      typed.chart.type = type
      const typedMeta = parseDataQueryMeta(typed)
      expect(buildDataQueryChartOption(typedMeta!)?.series[0]?.type).toBe(type)
    }

    const grouped = JSON.parse(JSON.stringify(replayFixture)) as Record<string, any>
    grouped.chart.series = 'region'
    const groupedOption = buildDataQueryChartOption(parseDataQueryMeta(grouped)!)
    expect(groupedOption?.xAxis?.data).toEqual(['2026-08-16', '2026-08-17', '2026-08-18'])
    expect(groupedOption?.series[0]?.data).toEqual([
      { name: '2026-08-16', value: '12.50' },
      { name: '2026-08-17', value: null },
      { name: '2026-08-18', value: '21.00' },
    ])
    expect(groupedOption?.series[1]?.data).toEqual([
      { name: '2026-08-16', value: null },
      { name: '2026-08-17', value: '18.75' },
      { name: '2026-08-18', value: null },
    ])

    const booleanMeasure = JSON.parse(JSON.stringify(replayFixture)) as Record<string, any>
    booleanMeasure.columns[2] = { name: 'revenue', type: 'BOOLEAN', semanticRole: 'measure' }
    booleanMeasure.previewRows = booleanMeasure.previewRows.map((row: Record<string, unknown>, index: number) => ({ ...row, revenue: index % 2 === 0 }))
    const booleanMeta = parseDataQueryMeta(booleanMeasure)
    expect(booleanMeta).not.toBeNull()
    expect(buildDataQueryChartOption(booleanMeta!)).toBeNull()
  })

  it('fails closed for old, malformed, or unmapped chart metadata', () => {
    expect(parseDataQueryMeta({ ...replayFixture, schemaVersion: 2 })).toBeNull()
    expect(parseDataQueryMeta({ ...replayFixture, chart: { ...replayFixture.chart, formatter: 'return value' } })).toBeNull()
    expect(parseDataQueryMeta({ ...replayFixture, error: null })).toBeNull()
    expect(parseDataQueryMeta({
      ...replayFixture,
      chart: { ...replayFixture.chart, title: 'javascript:alert(1)' },
    })).toBeNull()
    expect(parseDataQueryMeta({
      ...replayFixture,
      status: 'error',
      error: { code: 'DATABASE_ERROR', phase: 'run', message: 'safe error', retryable: false },
    })).toBeNull()
    const unmapped = JSON.parse(JSON.stringify(replayFixture)) as Record<string, any>
    unmapped.chart.x = 'missing_dimension'
    const unmappedMeta = parseDataQueryMeta(unmapped)
    expect(unmappedMeta).not.toBeNull()
    expect(buildDataQueryChartOption(unmappedMeta!)).toBeNull()
  })

  it('provides stable basic sorting and text-only SQL highlighting', () => {
    expect(compareDataQueryCells(null, 'a')).toBeGreaterThan(0)
    expect(compareDataQueryCells('10', '2')).toBeGreaterThan(0)
    expect(tokenizeSql("SELECT revenue -- raw\nFROM orders WHERE id = 2")).toEqual(expect.arrayContaining([
      { kind: 'keyword', text: 'SELECT' },
      { kind: 'comment', text: '-- raw' },
      { kind: 'number', text: '2' },
    ]))
  })

  it('uses the analysis-first Chart / Table / SQL order and chart default', () => {
    expect(DATA_QUERY_TAB_ORDER).toEqual(['chart', 'table', 'sql'])
    expect(DEFAULT_DATA_QUERY_TAB).toBe('chart')
  })

  it('renders SQL content directly after selecting the SQL tab', () => {
    const meta = parseDataQueryMeta(replayFixture)!
    const view = DataQuerySqlView({
      meta,
      mode: 'semantic',
      copied: false,
      onModeChange: vi.fn(),
      onCopy: vi.fn(),
    }) as { type?: unknown; props?: Record<string, unknown> }
    expect(view.type).toBe('section')
    expect(view.props?.['data-query-sql']).toBe(true)
    expect(collectElementTypes(view)).toContain('pre')
    expect(collectElementTypes(view)).not.toContain('details')
    expect(collectElementTypes(view)).not.toContain('summary')
  })
})
