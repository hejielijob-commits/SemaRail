import { describe, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import ToolRuntime, { type ToolDefinition } from '@deepseek-ai/dsh-tools'
import { CallId } from '@deepseek-ai/dsh-llm'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import {
  SCHEMA_VERSION,
  TOOL_NAME,
  CONTEXT_TOOL_NAME,
  apply,
  createDataQueryTool,
  renderDataQueryResult,
  type QueryGateway,
} from '../src/index.ts'
import type { DataQueryPresentation } from '@hejielijob/dsh-wren-data-agent-contract'

const input = {
  question: 'How many orders exist?',
  semanticSql: "SELECT 'all' AS day, COUNT(*) AS revenue FROM orders",
} as const

function success(queryInput: { semanticSql: string }): DataQueryPresentation {
  return {
    schemaVersion: SCHEMA_VERSION,
    queryId: 'q-1',
    status: 'success',
    semanticSql: queryInput.semanticSql,
    nativeSql: "SELECT 'all' AS day, COUNT(*) AS revenue FROM orders",
    columns: [
      { name: 'day', type: 'TEXT', semanticRole: 'dimension' },
      { name: 'revenue', type: 'BIGINT', semanticRole: 'measure' },
    ],
    previewRows: [{ day: 'all', revenue: '1' }],
    chart: { version: 1, type: 'bar', x: 'day', y: ['revenue'], tooltip: true },
    stats: { returnedRows: 1, durationMs: 12, truncated: false },
  }
}

function artifactSuccess(queryInput: { semanticSql: string }): DataQueryPresentation {
  const previewRows = Array.from({ length: 20 }, (_, index) => ({ day: `day-${index}`, revenue: String(index) }))
  return {
    schemaVersion: 2,
    queryId: 'q-artifact-1',
    status: 'success',
    delivery: 'artifact',
    semanticSql: queryInput.semanticSql,
    columns: [
      { name: 'day', type: 'TEXT', semanticRole: 'dimension' },
      { name: 'revenue', type: 'BIGINT', semanticRole: 'measure' },
    ],
    previewRows,
    stats: { returnedRows: 40, previewedRows: previewRows.length, durationMs: 18, truncated: true },
    artifact: {
      id: 'artifact-q-artifact-1', format: 'csv', fileName: 'orders.csv', rowCount: 40, sizeBytes: 4096,
      sha256: 'a'.repeat(64), expiresAt: '2099-01-01T00:00:00Z',
      downloadUrl: 'https://console.example.test/api/artifacts/artifact-q-artifact-1',
    },
  }
}

function v2Error(queryInput: { semanticSql: string }): DataQueryPresentation {
  return {
    schemaVersion: 2,
    queryId: 'q-error-v2',
    status: 'error',
    semanticSql: queryInput.semanticSql,
    columns: [],
    previewRows: [],
    stats: { returnedRows: 0, previewedRows: 0, durationMs: 7, truncated: false },
    error: {
      code: 'DATABASE_ERROR',
      phase: 'query',
      message: 'dsn=postgres://user:secret@example.invalid/db',
      retryable: true,
    },
  }
}

async function withRealToolContext<T>(run: (ctx: Context) => Promise<T>): Promise<T> {
  const ctx = new Context()
  try {
    await ctx.plugin(SystemPrompt)
    await ctx.plugin(ToolRuntime)
    return await run(ctx)
  } finally {
    await ctx.fiber.dispose()
  }
}

describe('Wren data_query Host boundary', () => {
  it('is accepted by the real rc.7 ToolRuntime registry', async () => {
    await withRealToolContext(async ctx => {
      const disposer = ctx.tools.register(createDataQueryTool({
        async query(queryInput) {
          return success(queryInput)
        },
      }))
      expect(ctx.tools.get(TOOL_NAME)?.name).toBe(TOOL_NAME)
      const result = await ctx.tools.execute({
        signal: new AbortController().signal,
        callId: CallId('wren-success'),
        name: TOOL_NAME,
        arguments: input,
      })
      expect(result).toMatchObject({
        isError: false,
        value: { status: 'success', nativeSql: "SELECT 'all' AS day, COUNT(*) AS revenue FROM orders" },
        meta: { status: 'success', chart: { type: 'bar' } },
      })
      disposer()
    })
  })

  it('registers the exact wire name and fails closed without a gateway', async () => {
    await withRealToolContext(async ctx => {
      apply(ctx)
      const tool = ctx.tools.get(TOOL_NAME)
      expect(tool?.name).toBe(TOOL_NAME)
      expect(ctx.tools.get(CONTEXT_TOOL_NAME)?.name).toBe(CONTEXT_TOOL_NAME)
      const result = await ctx.tools.execute({
        signal: new AbortController().signal,
        callId: CallId('wren-unavailable'),
        name: TOOL_NAME,
        arguments: input,
      })
      expect(result).toMatchObject({
        isError: false,
        value: {
          schemaVersion: SCHEMA_VERSION,
          status: 'error',
          error: { code: 'WREN_UNAVAILABLE' },
        },
      })
      const contextResult = await ctx.tools.execute({
        signal: new AbortController().signal,
        callId: CallId('wren-context-unavailable'),
        name: CONTEXT_TOOL_NAME,
        arguments: { question: 'How many orders exist?' },
      })
      expect(contextResult).toMatchObject({
        isError: false,
        value: { schemaVersion: SCHEMA_VERSION, status: 'error', error: { code: 'WREN_UNAVAILABLE' } },
      })
    })
  })

  it('assembles the semantic-first, entity-allowlist and bounded-retry guidance', async () => {
    await withRealToolContext(async ctx => {
      apply(ctx)
      const assembly = await ctx.systemPrompt.assemble()
      const section = assembly.sections.find(candidate => candidate.name === 'semarail:data-agent')
      expect(section?.text).toContain('semarail_semantic_context')
      expect(section?.text).toContain('authoritative allowlist')
      expect(section?.text).toContain('Do not guess or invent')
      expect(section?.text).toContain('at most one repair attempt')
      expect(section?.text).toContain('POLICY_DENIED')
      expect(section?.text).toContain('TIMEOUT')
      expect(section?.text).toContain('CANCELLED')
    })
  })

  it('preserves contract nativeSql/chart on a valid bounded presentation', async () => {
    const gateway: QueryGateway = {
      async query(queryInput): Promise<DataQueryPresentation> {
        return success(queryInput)
      },
    }
    const tool = createDataQueryTool(gateway)
    const result = await tool.execute(input, { signal: new AbortController().signal })
    expect(result.status).toBe('success')
    if (result.status !== 'success') return
    expect(result.nativeSql).toBe("SELECT 'all' AS day, COUNT(*) AS revenue FROM orders")
    expect(result.chart).toMatchObject({ version: 1, type: 'bar' })
    expect(result.previewRows).toHaveLength(1)
  })

  it('keeps the full artifact presentation in presentationMeta but bounds model content', async () => {
    const gateway: QueryGateway = { async query(queryInput) { return artifactSuccess(queryInput) } }
    const tool = createDataQueryTool(gateway)
    const result = await tool.execute(input, { signal: new AbortController().signal })
    const content = renderDataQueryResult(result)
    const modelResult = JSON.parse(content[0]?.text ?? '{}') as Record<string, unknown>
    expect(modelResult).toMatchObject({
      status: 'success',
      delivery: 'artifact',
      summary: expect.stringContaining('40 row(s)'),
      previewHint: expect.stringContaining('first 20'),
      stats: { returnedRows: 40, previewedRows: 20, durationMs: 18, truncated: true },
    })
    expect(modelResult).toHaveProperty('downloadUrl', 'https://console.example.test/api/artifacts/artifact-q-artifact-1')
    expect(modelResult).toHaveProperty('previewHint', expect.stringContaining('do not paste the full CSV into the chat context'))
    expect(modelResult).toHaveProperty('expiresAt', '2099-01-01T00:00:00Z')
    expect(modelResult).not.toHaveProperty('artifact.sha256')
    expect(JSON.stringify(modelResult)).not.toContain('nativeSql')
    const meta = tool.output.presentationMeta?.(input, result)
    expect(meta).toMatchObject({ schemaVersion: 2, delivery: 'artifact', artifact: { sha256: 'a'.repeat(64), rowCount: 40 } })
  })

  it('renders v2 errors as a safe code/retryability summary', () => {
    const content = renderDataQueryResult(v2Error(input))
    const modelResult = JSON.parse(content[0]?.text ?? '{}') as Record<string, unknown>
    expect(modelResult).toMatchObject({
      schemaVersion: 2,
      status: 'error',
      summary: 'SemaRail query failed (DATABASE_ERROR).',
      error: { code: 'DATABASE_ERROR', retryable: true },
    })
    expect(JSON.stringify(modelResult)).not.toContain('secret')
    expect(JSON.stringify(modelResult)).not.toContain('semanticSql')
  })

  it('sanitizes v2 gateway error diagnostics before durable presentation metadata', async () => {
    const tool = createDataQueryTool({ async query() { return v2Error(input) } })
    const result = await tool.execute(input, { signal: new AbortController().signal })
    expect(result).toMatchObject({ status: 'error', error: { code: 'DATABASE_ERROR', message: 'SemaRail could not read the data source.' } })
    expect(JSON.stringify(result)).not.toContain('secret')
  })

  it('fails closed when a gateway returns an over-bound presentation', async () => {
    const gateway: QueryGateway = {
      async query(queryInput): Promise<DataQueryPresentation> {
        return {
          ...success(queryInput),
          previewRows: Array.from({ length: 250 }, () => ({ day: 'all', revenue: '1' })),
          stats: { returnedRows: 250, durationMs: 12, truncated: true },
        }
      },
    }
    const result = await createDataQueryTool(gateway).execute(input, { signal: new AbortController().signal })
    expect(result).toMatchObject({ status: 'error', error: { code: 'INTERNAL_ERROR' } })
  })

  it('fails closed instead of accepting BigInt/Date preview cells', async () => {
    const gateway: QueryGateway = {
      async query(queryInput) {
        return {
          ...success(queryInput),
          previewRows: [{ day: 'all', revenue: BigInt(1) as unknown as string }],
        }
      },
    }
    const result = await createDataQueryTool(gateway).execute(input, { signal: new AbortController().signal })
    expect(result).toMatchObject({ status: 'error', error: { code: 'INTERNAL_ERROR' } })
    expect(() => JSON.stringify(result)).not.toThrow()
  })

  it('fails closed instead of accepting Date preview cells', async () => {
    const gateway: QueryGateway = {
      async query(queryInput) {
        return {
          ...success(queryInput),
          previewRows: [{ day: 'all', revenue: new Date('2026-01-01') as unknown as string }],
        }
      },
    }
    const result = await createDataQueryTool(gateway).execute(input, { signal: new AbortController().signal })
    expect(result).toMatchObject({ status: 'error', error: { code: 'INTERNAL_ERROR' } })
    expect(() => JSON.stringify(result)).not.toThrow()
  })

  it('converts gateway exceptions to a stable, credential-free failure', async () => {
    const gateway: QueryGateway = {
      async query() {
        throw new Error('dsn=postgres://user:secret@example.invalid/db')
      },
    }
    const result = await createDataQueryTool(gateway).execute(input, { signal: new AbortController().signal })
    expect(result).toMatchObject({
      status: 'error',
      error: {
        code: 'INTERNAL_ERROR',
        message: 'The SemaRail query gateway returned an internal error.',
      },
    })
    expect(JSON.stringify(result)).not.toContain('secret')
  })

  it('rejects malformed model input before the gateway boundary', async () => {
    const calls: unknown[] = []
    const gateway: QueryGateway = {
      async query(value) {
        calls.push(value)
        throw new Error('should not execute')
      },
    }
    await expect(createDataQueryTool(gateway).execute({ semanticSql: 'SELECT 1' }, { signal: new AbortController().signal }))
      .rejects.toThrow(/question/)
    expect(calls).toHaveLength(0)
  })

  it('keeps the public definition compatible with the ToolDefinition surface', () => {
    const definition: ToolDefinition = createDataQueryTool({
      async query(queryInput) {
        return success(queryInput)
      },
    })
    expect(definition.name).toBe(TOOL_NAME)
    expect(definition.output).toHaveProperty('presentationMeta')
  })
})
