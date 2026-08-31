import {
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_ROWS,
  MAX_QUERY_ROWS,
  PROTOCOL_VERSION,
  parseDataQueryInput,
  parseDataQueryPresentation,
  parseRpcResponse,
  parseSemanticContext,
  parseSemanticContextInput,
  type DataAgentErrorCode,
  type DataQueryInput,
  type DataQueryPresentation,
  type JsonValue,
  type RpcMethod,
  type SemanticContext,
  type SemanticContextInput,
} from '@hejielijob/dsh-wren-data-agent-contract'
import {
  QueryGatewayError,
  SemanticContextGatewayError,
  type QueryGateway,
  type SemanticContextGateway,
} from './types.js'

export const DEFAULT_SEMARAIL_ENDPOINT = 'http://127.0.0.1:48763' as const
const DEFAULT_TIMEOUT_MS = 30_000
const STARTUP_TIMEOUT_MS = 10_000

type RequestFunction = (input: string | URL, init?: RequestInit) => Promise<Response>

export interface CoreHttpGatewayConfig {
  readonly semarailEndpoint?: string
  /** Environment variable containing the Core bearer token. */
  readonly authTokenEnv?: string
  readonly timeoutMs?: number
  readonly request?: RequestFunction
}

function endpointUrl(value: string | undefined): URL {
  let parsed: URL
  try {
    parsed = new URL(value?.trim() || DEFAULT_SEMARAIL_ENDPOINT)
  } catch {
    throw new RangeError('semarailEndpoint must be an absolute HTTP URL')
  }
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new RangeError('semarailEndpoint must be a credential-free HTTP URL')
  }
  const loopback = ['127.0.0.1', 'localhost', '::1'].includes(parsed.hostname)
  if (parsed.protocol === 'http:' && !loopback) {
    throw new RangeError('non-loopback SemaRail endpoints require HTTPS')
  }
  parsed.pathname = parsed.pathname.replace(/\/+$/, '')
  parsed.search = ''
  parsed.hash = ''
  return parsed
}

function timeout(value: number | undefined): number {
  if (value === undefined) return DEFAULT_TIMEOUT_MS
  if (!Number.isInteger(value) || value < 100 || value > 120_000) throw new RangeError('timeoutMs is outside the supported range')
  return value
}

function asCode(value: string): DataAgentErrorCode {
  const codes: readonly DataAgentErrorCode[] = [
    'SEMANTIC_ERROR', 'POLICY_DENIED', 'DATABASE_ERROR', 'TIMEOUT', 'CANCELLED',
    'SIDECAR_UNAVAILABLE', 'INVALID_PARAMS', 'METHOD_NOT_FOUND', 'PROJECT_VALIDATION_FAILED',
    'HEALTHCHECK_FAILED', 'FRAME_TOO_LARGE', 'TRUNCATED_FRAME', 'INVALID_REQUEST',
    'PROTOCOL_ERROR', 'UNSUPPORTED_PROTOCOL', 'UNSUPPORTED_VERSION', 'INTERNAL_ERROR', 'WREN_UNAVAILABLE',
    'UNAUTHENTICATED',
  ]
  return codes.includes(value as DataAgentErrorCode) ? value as DataAgentErrorCode : 'PROTOCOL_ERROR'
}

function jsonObject(value: JsonValue): { readonly [key: string]: JsonValue } | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as { readonly [key: string]: JsonValue }
    : undefined
}

function linkedSignal(signal: AbortSignal | undefined, timeoutMs: number): { signal: AbortSignal; dispose: () => void } {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const abort = () => controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  return {
    signal: controller.signal,
    dispose: () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', abort)
    },
  }
}

/** Thin HTTP client for a separately running SemaRail Core. */
export class CoreHttpGateway implements QueryGateway, SemanticContextGateway {
  private readonly endpoint: URL
  private readonly timeoutMs: number
  private readonly request: RequestFunction
  private readonly authToken: string
  private nextIdValue = 0
  private queryCounter = 0
  private ready: Promise<void> | undefined

  constructor(config: CoreHttpGatewayConfig = {}) {
    this.endpoint = endpointUrl(config.semarailEndpoint)
    this.timeoutMs = timeout(config.timeoutMs)
    this.request = config.request ?? fetch
    const tokenEnv = config.authTokenEnv?.trim() || 'SEMARAIL_HARNESS_TOKEN'
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(tokenEnv)) throw new RangeError('authTokenEnv is invalid')
    this.authToken = process.env[tokenEnv]?.trim() || ''
  }

  async start(): Promise<void> {
    if (this.authToken.length < 32) throw new QueryGatewayError('UNAUTHENTICATED', false)
    if (this.ready !== undefined) return this.ready
    const startup = this.initialize()
    this.ready = startup.catch(error => {
      this.ready = undefined
      throw error
    })
    return this.ready
  }

  async context(input: SemanticContextInput, signal: AbortSignal): Promise<SemanticContext> {
    const parsed = parseSemanticContextInput(input)
    if (signal.aborted) throw new SemanticContextGatewayError('CANCELLED', false)
    try {
      await this.start()
      return parseSemanticContext(await this.rpc('context.ask', { question: parsed.question }, signal))
    } catch (error: unknown) {
      if (error instanceof SemanticContextGatewayError) throw error
      if (error instanceof QueryGatewayError) throw new SemanticContextGatewayError(error.code, error.retryable)
      throw new SemanticContextGatewayError(signal.aborted ? 'CANCELLED' : 'SIDECAR_UNAVAILABLE', !signal.aborted)
    }
  }

  async query(input: DataQueryInput, signal: AbortSignal): Promise<DataQueryPresentation> {
    const parsed = parseDataQueryInput(input)
    if (signal.aborted) throw new QueryGatewayError('CANCELLED', false)
    await this.start()
    const queryId = this.nextQueryId()
    const cancel = () => {
      void this.rpc('query.cancel', { queryId }).catch(() => undefined)
    }
    signal.addEventListener('abort', cancel, { once: true })
    try {
      const params: Record<string, JsonValue> = {
        question: parsed.question,
        semanticSql: parsed.semanticSql,
        queryId,
        ...(parsed.chartIntent === undefined ? {} : { chartIntent: parsed.chartIntent }),
      }
      return parseDataQueryPresentation(await this.rpc('query.run', params, signal))
    } catch (error: unknown) {
      if (error instanceof QueryGatewayError) throw error
      throw new QueryGatewayError(signal.aborted ? 'CANCELLED' : 'SIDECAR_UNAVAILABLE', !signal.aborted)
    } finally {
      signal.removeEventListener('abort', cancel)
    }
  }

  dispose(): void {
    this.ready = undefined
  }

  private async initialize(): Promise<void> {
    const health = await this.rpc('health', {}, undefined, STARTUP_TIMEOUT_MS)
    const healthObject = jsonObject(health)
    if (
      healthObject === undefined
      || healthObject.service !== 'semarail-core'
      || healthObject.apiVersion !== PROTOCOL_VERSION
      || healthObject.protocolVersion !== PROTOCOL_VERSION
    ) {
      throw new QueryGatewayError('UNSUPPORTED_PROTOCOL', false)
    }
    const validation = await this.rpc('project.validate', {}, undefined, STARTUP_TIMEOUT_MS)
    const validationObject = jsonObject(validation)
    if (validationObject === undefined || validationObject.valid !== true) {
      throw new QueryGatewayError('PROJECT_VALIDATION_FAILED', false)
    }
  }

  private async rpc(
    method: RpcMethod,
    params: Record<string, JsonValue>,
    callerSignal?: AbortSignal,
    requestTimeout = this.timeoutMs,
  ): Promise<JsonValue> {
    const id = this.nextId(method)
    const endpoint = new URL('/api/v1/runtime/rpc', this.endpoint)
    const linked = linkedSignal(callerSignal, requestTimeout)
    try {
      const response = await this.request(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.authToken}` },
        body: JSON.stringify({ protocolVersion: PROTOCOL_VERSION, id, method, params, deadlineMs: requestTimeout }),
        signal: linked.signal,
      })
      const body: unknown = await response.json()
      const parsed = parseRpcResponse(body)
      if (parsed.id !== id) throw new QueryGatewayError('PROTOCOL_ERROR', false)
      if (!parsed.ok) throw new QueryGatewayError(asCode(parsed.error.code), parsed.error.retryable)
      if (!response.ok) throw new QueryGatewayError('SIDECAR_UNAVAILABLE', response.status >= 500)
      return parsed.result
    } catch (error: unknown) {
      if (error instanceof QueryGatewayError) throw error
      if (callerSignal?.aborted) throw new QueryGatewayError('CANCELLED', false)
      if (linked.signal.aborted) throw new QueryGatewayError('TIMEOUT', true)
      throw new QueryGatewayError('SIDECAR_UNAVAILABLE', true)
    } finally {
      linked.dispose()
    }
  }

  private nextId(method: string): string {
    this.nextIdValue = (this.nextIdValue + 1) % 0x3fff_ffff
    return `semarail-http-${method.replace(/[^a-zA-Z0-9]/g, '-')}-${this.nextIdValue.toString(36)}`
  }

  private nextQueryId(): string {
    this.queryCounter = (this.queryCounter + 1) % 0x3fff_ffff
    return `semarail-query-${this.queryCounter.toString(36)}`
  }
}

export function createCoreHttpGateway(config: CoreHttpGatewayConfig = {}): CoreHttpGateway {
  return new CoreHttpGateway(config)
}

export { MAX_PREVIEW_BYTES, MAX_PREVIEW_ROWS, MAX_QUERY_ROWS }
