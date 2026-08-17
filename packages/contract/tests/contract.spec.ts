import { describe, expect, it } from 'vitest'
import {
  ContractValidationError,
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_ROWS,
  parseChartSpecV1,
  parseDataQueryPresentation,
  parseRpcRequest,
  parseRpcResponse,
  parseSemanticContext,
  safeParseDataQueryInput,
} from '../src/index.js'

const column = { name: 'total', type: 'DECIMAL(18,2)', semanticRole: 'measure' as const }

function success(rows: Readonly<Record<string, unknown>>[] = [{ total: '12.50' }]) {
  return {
    schemaVersion: 1,
    queryId: 'q-1',
    status: 'success' as const,
    semanticSql: 'SELECT SUM(amount) AS total FROM orders',
    columns: [column],
    previewRows: rows,
    stats: { returnedRows: rows.length, durationMs: 12, truncated: false },
  }
}

describe('versioned RPC contracts', () => {
  it('accepts valid envelopes and rejects unknown fields', () => {
    expect(parseRpcRequest({ protocolVersion: '1', id: '1', method: 'health', params: {} })).toMatchObject({ id: '1' })
    expect(parseRpcRequest({ protocolVersion: '1', id: '2', method: 'project.validate', params: {} }).method).toBe('project.validate')
    expect(() => parseRpcRequest({ protocolVersion: '1', id: '1', method: 'health', params: {}, extra: true })).toThrow(ContractValidationError)
    expect(parseRpcResponse({ protocolVersion: '1', id: '1', ok: true, result: { ready: true } })).toMatchObject({ ok: true })
    expect(() => parseRpcResponse({ protocolVersion: '2', id: '1', ok: true, result: null })).toThrow(/unsupported version/)
    expect(parseRpcResponse({
      protocolVersion: '1', id: 'python-1', ok: false,
      error: { code: 'PROJECT_VALIDATION_FAILED', phase: 'project.validate', message: 'project validation failed', retryable: false },
    })).toMatchObject({ ok: false, error: { code: 'PROJECT_VALIDATION_FAILED' } })
  })
})

describe('semantic context and ChartSpecV1', () => {
  it('accepts the minimal context and chart recommendation', () => {
    expect(parseSemanticContext({
      schemaVersion: 1,
      projectRevision: 'rev-1',
      models: [{ name: 'orders', columns: [{ name: 'amount', type: 'DECIMAL(18,2)' }] }],
      relationships: [],
    }).models[0]?.name).toBe('orders')
    expect(parseChartSpecV1({ version: 1, type: 'line', x: 'day', y: ['total'], tooltip: true }).tooltip).toBe(true)
    expect(() => parseChartSpecV1({ version: 2, type: 'line', x: 'day', y: ['total'], tooltip: true })).toThrow(/unsupported version/)
  })
})

describe('DataQuery presentation limits and scalar policy', () => {
  it('uses returnedRows and accepts exact decimal strings', () => {
    expect(parseDataQueryPresentation(success())).toMatchObject({ stats: { returnedRows: 1 } })
    expect(() => parseDataQueryPresentation({ ...success([{ total: 12.5 }]), stats: { returnedRows: 1, durationMs: 1, truncated: false } })).toThrow(/exact strings/)
  })

  it('rejects BigInt/Date previews and enforces row and byte caps', () => {
    expect(() => parseDataQueryPresentation(success([{ total: BigInt(1) }]))).toThrow(ContractValidationError)
    expect(() => parseDataQueryPresentation(success([{ total: new Date('2026-01-01') }]))).toThrow(ContractValidationError)
    const rows = Array.from({ length: MAX_PREVIEW_ROWS + 1 }, () => ({ total: '1.00' }))
    expect(() => parseDataQueryPresentation(success(rows))).toThrow(/at most 200/)
    const large = [{ total: 'x'.repeat(MAX_PREVIEW_BYTES) }]
    expect(() => parseDataQueryPresentation(success(large))).toThrow(/1,048,576|1048576/)
  })

  it('returns a discriminated safe-parse failure', () => {
    const result = safeParseDataQueryInput({ question: '', semanticSql: 'SELECT 1' })
    expect(result.success).toBe(false)
  })
})
