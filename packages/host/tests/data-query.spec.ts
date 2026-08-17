import { describe, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import ToolRuntime, { type ToolDefinition } from '@deepseek-ai/dsh-tools'
import { CallId } from '@deepseek-ai/dsh-llm'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import {
  SCHEMA_VERSION,
  TOOL_NAME,
  apply,
  createDataQueryTool,
  type QueryGateway,
} from '../src/index.ts'
import type { DataQueryPresentation } from '@hejielijob/dsh-wren-data-agent-contract'

const input = {
  question: 'How many orders exist?',
  semanticSql: 'SELECT COUNT(*) AS count FROM orders',
} as const

function success(queryInput: { semanticSql: string }): DataQueryPresentation {
  return {
    schemaVersion: SCHEMA_VERSION,
    queryId: 'q-1',
    status: 'success',
    semanticSql: queryInput.semanticSql,
    nativeSql: 'SELECT COUNT(*) AS count FROM orders',
    columns: [{ name: 'count', type: 'BIGINT', semanticRole: 'measure' }],
    previewRows: [{ count: '1' }],
    chart: { version: 1, type: 'bar', x: 'count', y: ['count'], tooltip: true },
    stats: { returnedRows: 1, durationMs: 12, truncated: false },
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
        value: { status: 'success', nativeSql: 'SELECT COUNT(*) AS count FROM orders' },
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
    expect(result.nativeSql).toBe('SELECT COUNT(*) AS count FROM orders')
    expect(result.chart).toMatchObject({ version: 1, type: 'bar' })
    expect(result.previewRows).toHaveLength(1)
  })

  it('fails closed when a gateway returns an over-bound presentation', async () => {
    const gateway: QueryGateway = {
      async query(queryInput): Promise<DataQueryPresentation> {
        return {
          ...success(queryInput),
          previewRows: Array.from({ length: 250 }, () => ({ count: '1' })),
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
          previewRows: [{ count: BigInt(1) as unknown as string }],
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
          previewRows: [{ count: new Date('2026-01-01') as unknown as string }],
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
        message: 'The Wren query gateway returned an internal error.',
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
