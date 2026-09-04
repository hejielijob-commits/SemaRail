import { describe, expect, it } from 'vitest'
import {
  ContractValidationError,
  DATA_QUERY_PRESENTATION_V2_JSON_SCHEMA,
  MAX_ARTIFACT_BYTES,
  DATA_QUERY_PRESENTATION_VERSION,
  MAX_ARTIFACT_PREVIEW_ROWS,
  MAX_INLINE_PREVIEW_BYTES,
  MAX_INLINE_PREVIEW_ROWS,
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_ROWS,
  MAX_QUERY_ROWS,
  parseChartSpecV1,
  parseDataQueryPresentation,
  parseRpcRequest,
  parseRpcResponse,
  parseSemanticContext,
  safeParseDataQueryInput,
  safeParseDataQueryPresentationV2,
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
    expect(parseSemanticContext({
      schemaVersion: 1, projectRevision: 'rev-2', models: [], relationships: [],
      sqlHistory: [{ id: 'sql:one', question: 'Revenue?', sql: 'SELECT SUM(amount) FROM orders', sourcePath: 'knowledge/sql/revenue.md' }],
    }).sqlHistory?.[0]?.sourcePath).toBe('knowledge/sql/revenue.md')
    expect(parseChartSpecV1({ version: 1, type: 'line', x: 'day', y: ['total'], tooltip: true }).tooltip).toBe(true)
    expect(() => parseChartSpecV1({ version: 2, type: 'line', x: 'day', y: ['total'], tooltip: true })).toThrow(/unsupported version/)
  })
})

describe('DataQuery presentation limits and scalar policy', () => {
  it('uses returnedRows and accepts exact decimal strings', () => {
    expect(parseDataQueryPresentation(success())).toMatchObject({ stats: { returnedRows: 1 } })
    expect(() => parseDataQueryPresentation({ ...success([{ total: 12.5 }]), stats: { returnedRows: 1, durationMs: 1, truncated: false } })).toThrow(/exact strings/)
  })

  it('preserves optional question and bounded SQL history for durable replay', () => {
    expect(parseDataQueryPresentation({
      ...success(),
      question: 'What is revenue?',
      sqlHistory: [{ id: 'sql:revenue', question: 'Revenue by day', sql: 'SELECT day, SUM(amount) FROM orders GROUP BY day' }],
    })).toMatchObject({ question: 'What is revenue?', sqlHistory: [{ id: 'sql:revenue' }] })
    expect(() => parseDataQueryPresentation({
      ...success(),
      sqlHistory: Array.from({ length: 6 }, (_, index) => ({ id: `sql:${index}`, question: 'q', sql: 'SELECT 1' })),
    })).toThrow(/at most 5/)
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

  it('parses v2 inline delivery while preserving v1 replay shape', () => {
    const parsedV1 = parseDataQueryPresentation(success())
    expect(parsedV1.schemaVersion).toBe(1)
    expect(parsedV1).not.toHaveProperty('delivery')
    const parsedV2 = parseDataQueryPresentation({
      ...success(),
      schemaVersion: DATA_QUERY_PRESENTATION_VERSION,
      delivery: 'inline',
      stats: { returnedRows: 1, previewedRows: 1, durationMs: 12, truncated: false },
    })
    expect(parsedV2).toMatchObject({ schemaVersion: 2, status: 'success', delivery: 'inline', stats: { previewedRows: 1 } })

    const tooManyRows = Array.from({ length: MAX_INLINE_PREVIEW_ROWS + 1 }, () => ({ total: '1.00' }))
    expect(() => parseDataQueryPresentation({
      ...success(tooManyRows), schemaVersion: DATA_QUERY_PRESENTATION_VERSION, delivery: 'inline',
      stats: { returnedRows: tooManyRows.length, previewedRows: tooManyRows.length, durationMs: 12, truncated: false },
    })).toThrow(/at most 50/)

    const emptyEnvelopeBytes = new TextEncoder().encode(JSON.stringify([{ total: '' }])).byteLength
    const boundaryValue = 'x'.repeat(MAX_INLINE_PREVIEW_BYTES - emptyEnvelopeBytes)
    const inlineAtBoundary = {
      ...success([{ total: boundaryValue }]), schemaVersion: DATA_QUERY_PRESENTATION_VERSION, delivery: 'inline',
      columns: [{ ...column, type: 'VARCHAR' }],
      stats: { returnedRows: 1, previewedRows: 1, durationMs: 12, truncated: false },
    }
    expect(parseDataQueryPresentation(inlineAtBoundary)).toMatchObject({ delivery: 'inline' })
    expect(() => parseDataQueryPresentation({ ...inlineAtBoundary, previewRows: [{ total: `${boundaryValue}x` }] })).toThrow(/131072/)
  })

  it('bounds v2 artifact previews at 20 rows and keeps artifact metadata strict', () => {
    const previewRows = Array.from({ length: MAX_ARTIFACT_PREVIEW_ROWS }, (_, index) => ({ total: String(index) }))
    const artifact = {
      id: 'artifact-1',
      format: 'csv',
      fileName: 'orders.csv',
      rowCount: 40,
      sizeBytes: 1_024,
      sha256: 'a'.repeat(64),
      expiresAt: '2026-09-04T12:00:00Z',
      downloadUrl: 'https://console.example.test/api/artifacts/artifact-1',
    }
    const result = parseDataQueryPresentation({
      ...success(previewRows),
      schemaVersion: DATA_QUERY_PRESENTATION_VERSION,
      delivery: 'artifact',
      artifact,
      stats: { returnedRows: 40, previewedRows: previewRows.length, durationMs: 12, truncated: true },
    })
    expect(result).toMatchObject({ delivery: 'artifact', artifact: { format: 'csv', rowCount: 40 } })
    expect(parseDataQueryPresentation({
      ...success(previewRows), schemaVersion: DATA_QUERY_PRESENTATION_VERSION, delivery: 'artifact', artifact: { ...artifact, downloadUrl: `${artifact.downloadUrl}?token=short-lived` },
      stats: { returnedRows: 40, previewedRows: 20, durationMs: 12, truncated: true },
    })).toMatchObject({ artifact: { downloadUrl: `${artifact.downloadUrl}?token=short-lived` } })
    expect(() => parseDataQueryPresentation({
      ...success([...previewRows, { total: '20' }]),
      schemaVersion: DATA_QUERY_PRESENTATION_VERSION,
      delivery: 'artifact',
      artifact,
      stats: { returnedRows: 40, previewedRows: 21, durationMs: 12, truncated: true },
    })).toThrow(/at most 20/)
    expect(() => parseDataQueryPresentation({
      ...success(previewRows), schemaVersion: DATA_QUERY_PRESENTATION_VERSION, delivery: 'artifact', artifact: { ...artifact, downloadUrl: 'javascript:alert(1)' },
      stats: { returnedRows: 40, previewedRows: 20, durationMs: 12, truncated: true },
    })).toThrow(/downloadUrl/)
    expect(() => parseDataQueryPresentation({
      ...success(previewRows), schemaVersion: DATA_QUERY_PRESENTATION_VERSION, delivery: 'artifact', artifact: { ...artifact, sizeBytes: MAX_ARTIFACT_BYTES + 1 },
      stats: { returnedRows: 40, previewedRows: 20, durationMs: 12, truncated: true },
    })).toThrow(/sizeBytes/)
    expect(() => parseDataQueryPresentation({
      ...success(previewRows), schemaVersion: DATA_QUERY_PRESENTATION_VERSION, delivery: 'artifact', artifact: { ...artifact, downloadUrl: `${artifact.downloadUrl}#fragment` },
      stats: { returnedRows: 40, previewedRows: 20, durationMs: 12, truncated: true },
    })).toThrow(/downloadUrl/)
    expect(() => parseDataQueryPresentation({
      ...success(previewRows), schemaVersion: DATA_QUERY_PRESENTATION_VERSION, delivery: 'artifact', artifact: { ...artifact, expiresAt: '2026-09-04T12:00:00' },
      stats: { returnedRows: 40, previewedRows: 20, durationMs: 12, truncated: true },
    })).toThrow(/expiresAt/)
    expect(() => parseDataQueryPresentation({
      ...success(previewRows), schemaVersion: DATA_QUERY_PRESENTATION_VERSION, delivery: 'artifact', artifact: { ...artifact, rowCount: MAX_QUERY_ROWS + 1 },
      stats: { returnedRows: MAX_QUERY_ROWS + 1, previewedRows: 20, durationMs: 12, truncated: true },
    })).toThrow(/returnedRows|rowCount/)
    expect(() => parseDataQueryPresentation({
      ...success(previewRows), schemaVersion: DATA_QUERY_PRESENTATION_VERSION, delivery: 'artifact', artifact,
      chart: { version: 1, type: 'line', x: 'total', y: ['total'] },
      stats: { returnedRows: 40, previewedRows: 20, durationMs: 12, truncated: true },
    })).toThrow(/chart/)
  })

  it('fails closed for unsafe v2 error or delivery combinations', () => {
    const base = {
      ...success(),
      schemaVersion: DATA_QUERY_PRESENTATION_VERSION,
      stats: { returnedRows: 1, previewedRows: 1, durationMs: 1, truncated: false },
    }
    expect(parseDataQueryPresentation({
      ...base,
      status: 'error',
      error: { code: 'DATABASE_ERROR', phase: 'query', message: 'safe error', retryable: false },
    })).toMatchObject({ schemaVersion: 2, status: 'error', stats: { previewedRows: 1 } })
    expect(() => parseDataQueryPresentation({
      ...base,
      status: 'error', delivery: 'artifact',
      error: { code: 'DATABASE_ERROR', phase: 'query', message: 'safe error', retryable: false },
    })).toThrow(/delivery/)
    expect(() => parseDataQueryPresentation({
      ...base, delivery: 'inline', stats: { returnedRows: 1, previewedRows: 0, durationMs: 1, truncated: false },
    })).toThrow(/previewedRows/)
    expect(safeParseDataQueryPresentationV2({ ...base, schemaVersion: 3 }).success).toBe(false)
  })

  it('emits a valid schema shape without impossible empty enums', () => {
    expect(JSON.stringify(DATA_QUERY_PRESENTATION_V2_JSON_SCHEMA)).not.toContain('"enum":[]')
  })
})
