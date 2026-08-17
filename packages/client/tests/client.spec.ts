import { describe, expect, it, vi } from 'vitest'
import {
  apply,
  DataQueryRow,
  parseDataQueryMeta,
  type DataQueryResultBlock,
} from '../src/client/index.js'

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
    expect(register).toHaveBeenCalledWith(
      { name: 'tool.call.toolview', key: 'data_query' },
      DataQueryRow,
    )
  })
})
