import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CoreHttpGateway, QueryGatewayError } from '../src/index.ts'

const TOKEN = 'test-token-that-is-at-least-thirty-two-characters'

function rpcResponse(id: string, result: unknown, protocolVersion = '1'): Response {
  return new Response(JSON.stringify({ protocolVersion, id, ok: true, result }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function success(semanticSql: string) {
  return {
    schemaVersion: 1,
    queryId: 'semarail-query-1',
    status: 'success',
    semanticSql,
    nativeSql: 'SELECT 1 AS revenue',
    columns: [{ name: 'revenue', type: 'INTEGER', semanticRole: 'measure' }],
    previewRows: [{ revenue: 1 }],
    stats: { returnedRows: 1, durationMs: 2, truncated: false },
  }
}

describe('standalone SemaRail Core HTTP gateway', () => {
  beforeEach(() => {
    process.env.SEMARAIL_TEST_TOKEN = TOKEN
  })

  afterEach(() => {
    delete process.env.SEMARAIL_TEST_TOKEN
    delete process.env.SEMARAIL_HARNESS_TOKEN
  })

  it('uses the dedicated Harness token variable by default', async () => {
    process.env.SEMARAIL_HARNESS_TOKEN = TOKEN
    const authorizations: Array<string | null> = []
    const request = async (_input: string | URL, init?: RequestInit): Promise<Response> => {
      const body = JSON.parse(String(init?.body)) as { id: string; method: string }
      authorizations.push(new Headers(init?.headers).get('Authorization'))
      if (body.method === 'health') {
        return rpcResponse(body.id, { service: 'semarail-core', apiVersion: '1', protocolVersion: '1' })
      }
      return rpcResponse(body.id, { valid: true, projectRevision: 'sha256:test' })
    }

    await new CoreHttpGateway({ request }).start()

    expect(authorizations).toEqual([`Bearer ${TOKEN}`, `Bearer ${TOKEN}`])
  })

  it('handshakes, authenticates, and queries without sending project or credentials', async () => {
    const calls: Array<{ method: string; params: Record<string, unknown>; authorization: string | null }> = []
    const request = async (_input: string | URL, init?: RequestInit): Promise<Response> => {
      const body = JSON.parse(String(init?.body)) as { id: string; method: string; params: Record<string, unknown> }
      const headers = new Headers(init?.headers)
      calls.push({ method: body.method, params: body.params, authorization: headers.get('Authorization') })
      if (body.method === 'health') {
        return rpcResponse(body.id, { service: 'semarail-core', apiVersion: '1', protocolVersion: '1' })
      }
      if (body.method === 'project.validate') {
        return rpcResponse(body.id, { valid: true, projectRevision: 'sha256:test' })
      }
      return rpcResponse(body.id, success(String(body.params.semanticSql)))
    }
    const gateway = new CoreHttpGateway({ request, authTokenEnv: 'SEMARAIL_TEST_TOKEN' })

    const result = await gateway.query(
      { question: 'Revenue?', semanticSql: 'SELECT 1 AS revenue', chartIntent: 'table' },
      new AbortController().signal,
    )

    expect(result.status).toBe('success')
    expect(calls.map(call => call.method)).toEqual(['health', 'project.validate', 'query.run'])
    expect(calls.every(call => call.authorization === `Bearer ${TOKEN}`)).toBe(true)
    expect(calls[2]?.params).toMatchObject({ question: 'Revenue?', semanticSql: 'SELECT 1 AS revenue' })
    expect(calls[2]?.params).not.toHaveProperty('projectDir')
    expect(calls[2]?.params).not.toHaveProperty('databaseDsnEnv')
    expect(calls[2]?.params).not.toHaveProperty('maxRows')
  })

  it('fails closed when Core negotiates an unsupported protocol', async () => {
    const request = async (_input: string | URL, init?: RequestInit): Promise<Response> => {
      const body = JSON.parse(String(init?.body)) as { id: string }
      return rpcResponse(body.id, { service: 'semarail-core', apiVersion: '2', protocolVersion: '2' })
    }
    const gateway = new CoreHttpGateway({ request, authTokenEnv: 'SEMARAIL_TEST_TOKEN' })

    await expect(gateway.start()).rejects.toMatchObject({ code: 'UNSUPPORTED_PROTOCOL', retryable: false })
  })

  it('requires token configuration and rejects credential-bearing or insecure remote endpoints', async () => {
    delete process.env.SEMARAIL_TEST_TOKEN
    const gateway = new CoreHttpGateway({ authTokenEnv: 'SEMARAIL_TEST_TOKEN' })
    await expect(gateway.start()).rejects.toMatchObject({ code: 'UNAUTHENTICATED' })
    expect(() => new CoreHttpGateway({ semarailEndpoint: 'http://user:secret@127.0.0.1:48763' })).toThrow(/credential-free/)
    expect(() => new CoreHttpGateway({ semarailEndpoint: 'http://example.com:48763' })).toThrow(/require HTTPS/)
  })

  it('sends best-effort query cancellation with the same server-visible query id', async () => {
    const methods: Array<{ method: string; queryId?: unknown }> = []
    const request = async (_input: string | URL, init?: RequestInit): Promise<Response> => {
      const body = JSON.parse(String(init?.body)) as { id: string; method: string; params: { queryId?: unknown } }
      methods.push({ method: body.method, queryId: body.params.queryId })
      if (body.method === 'health') return rpcResponse(body.id, { service: 'semarail-core', apiVersion: '1', protocolVersion: '1' })
      if (body.method === 'project.validate') return rpcResponse(body.id, { valid: true, projectRevision: 'sha256:test' })
      if (body.method === 'query.cancel') return rpcResponse(body.id, { queryId: body.params.queryId, cancelled: true })
      return await new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
      })
    }
    const gateway = new CoreHttpGateway({ request, authTokenEnv: 'SEMARAIL_TEST_TOKEN' })
    const controller = new AbortController()
    const running = gateway.query({ question: 'Q', semanticSql: 'SELECT 1' }, controller.signal)
    await new Promise(resolve => setTimeout(resolve, 0))
    controller.abort()

    await expect(running).rejects.toMatchObject<QueryGatewayError>({ code: 'CANCELLED' })
    await new Promise(resolve => setTimeout(resolve, 0))
    const run = methods.find(call => call.method === 'query.run')
    const cancel = methods.find(call => call.method === 'query.cancel')
    expect(cancel?.queryId).toBe(run?.queryId)
  })
})
