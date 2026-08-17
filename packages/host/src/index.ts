/**
 * Host-side Wren `data_query` boundary for DeepSeek Harness.
 *
 * The default Loader entry registers a safe, unavailable gateway. A deployed
 * adapter can inject a real QueryGateway into `createDataQueryTool` without
 * changing the tool wire name or the Bundle patch.
 */

import { defineTool } from '@deepseek-ai/dsh-tools'
import type { Context } from '@deepseek-ai/cordis'
import {
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_ROWS,
  MAX_QUERY_ROWS,
  parseDataQueryInput,
  parseDataQueryPresentation,
  SCHEMA_VERSION,
} from '@hejielijob/dsh-wren-data-agent-contract'
import type {
  DataAgentErrorCode,
  DataQueryInput,
  DataQueryPresentation,
} from '@hejielijob/dsh-wren-data-agent-contract'
import type { ToolDefinition, ToolRunContext } from '@deepseek-ai/dsh-tools'
import type { JsonValue } from '@deepseek-ai/dsh-session'

export const TOOL_NAME = 'data_query' as const
export { MAX_PREVIEW_BYTES, MAX_PREVIEW_ROWS, MAX_QUERY_ROWS, SCHEMA_VERSION }

/** Stable bounds shared with the version-one contract. */
export const MAX_QUERY_TEXT_LENGTH = 64_000 as const

/** Gateway boundary used by the registered Tool. */
export interface QueryGateway {
  /** Execute one already-validated query and observe Host cancellation. */
  query(input: DataQueryInput, signal: AbortSignal): Promise<DataQueryPresentation>
}

/** A stable failure raised by an unavailable or rejected gateway. */
export class QueryGatewayError extends Error {
  readonly code: DataAgentErrorCode
  readonly retryable: boolean

  constructor(
    code: DataAgentErrorCode = 'WREN_UNAVAILABLE',
    retryable = true,
  ) {
    super('Wren query gateway is unavailable.')
    this.name = 'QueryGatewayError'
    this.code = code
    this.retryable = retryable
  }
}

/** Default gateway: it never fabricates a successful query result. */
export const unavailableQueryGateway: QueryGateway = {
  async query(): Promise<DataQueryPresentation> {
    throw new QueryGatewayError()
  },
}

function safeErrorMessage(code: DataAgentErrorCode): string {
  switch (code) {
    case 'SEMANTIC_ERROR': return 'Wren rejected the semantic query.'
    case 'POLICY_DENIED': return 'The query was denied by policy.'
    case 'DATABASE_ERROR': return 'Wren could not read the data source.'
    case 'TIMEOUT': return 'The Wren query timed out.'
    case 'CANCELLED': return 'The Wren query was cancelled.'
    case 'SIDECAR_UNAVAILABLE': return 'The Wren sidecar is unavailable.'
    case 'INVALID_PARAMS': return 'The Wren query parameters were invalid.'
    case 'METHOD_NOT_FOUND': return 'The Wren sidecar does not support this query.'
    case 'PROJECT_VALIDATION_FAILED': return 'The Wren project failed validation.'
    case 'HEALTHCHECK_FAILED': return 'The Wren sidecar health check failed.'
    case 'FRAME_TOO_LARGE': return 'The Wren sidecar response was too large.'
    case 'TRUNCATED_FRAME': return 'The Wren sidecar response was truncated.'
    case 'INVALID_REQUEST': return 'The Wren sidecar rejected the request.'
    case 'PROTOCOL_ERROR': return 'The Wren sidecar returned an invalid response.'
    case 'UNSUPPORTED_PROTOCOL': return 'The Wren sidecar protocol is unsupported.'
    case 'UNSUPPORTED_VERSION': return 'The Wren data contract version is unsupported.'
    case 'INTERNAL_ERROR': return 'The Wren query gateway returned an internal error.'
    case 'WREN_UNAVAILABLE': return 'Wren query gateway is unavailable.'
  }
  return 'The Wren query gateway returned an internal error.'
}

function failurePresentation(
  input: DataQueryInput,
  error: unknown,
  signal: AbortSignal,
): DataQueryPresentation {
  const code: DataAgentErrorCode = signal.aborted
    ? 'CANCELLED'
    : error instanceof QueryGatewayError ? error.code : 'INTERNAL_ERROR'
  const retryable = error instanceof QueryGatewayError
    ? error.retryable
    : false
  return parseDataQueryPresentation({
    schemaVersion: SCHEMA_VERSION,
    queryId: 'wren-unavailable',
    status: 'error',
    semanticSql: input.semanticSql,
    columns: [],
    previewRows: [],
    stats: { returnedRows: 0, durationMs: 0, truncated: false },
    error: {
      code,
      phase: 'query',
      message: safeErrorMessage(code),
      retryable,
    },
  })
}

function normalizeGatewayResult(input: DataQueryInput, value: DataQueryPresentation): DataQueryPresentation {
  // The contract parser is the runtime boundary: it preserves nativeSql/chart,
  // rejects unsupported versions, and rejects lossy values such as BigInt/Date.
  const parsed = parseDataQueryPresentation(value)
  const normalized = {
    ...parsed,
    semanticSql: input.semanticSql,
  }
  return parseDataQueryPresentation(parsed.status === 'error'
    ? {
      ...normalized,
      error: {
        ...parsed.error,
        // Keep the contract's stable code/phase/retryability, but never carry
        // gateway diagnostics (which may contain DSNs or credentials) across
        // the durable tool/result.meta boundary.
        message: safeErrorMessage(parsed.error.code),
      },
    }
    : normalized)
}

function renderResult(value: unknown): Array<{ type: 'text'; text: string }> {
  const result = parseDataQueryPresentation(value)
  if (result.status === 'error') {
    return [{ type: 'text', text: `Wren query failed (${result.error.code}).` }]
  }
  return [{ type: 'text', text: `Wren query returned ${result.stats.returnedRows} row(s).` }]
}

/** Build a registry-ready `data_query` definition around an injected gateway. */
export function createDataQueryTool(gateway: QueryGateway): ToolDefinition {
  return defineTool({
    name: TOOL_NAME,
    description: 'Query Wren through the configured semantic layer and return a bounded JSON preview.',
    parameters: {
      question: {
        type: 'string',
        required: true,
        description: 'The natural-language question represented by this query.',
      },
      semanticSql: {
        type: 'string',
        required: true,
        description: 'Model-generated semantic SQL. Treat it as untrusted input.',
      },
      chartIntent: {
        type: 'string',
        enum: ['auto', 'table', 'line', 'bar', 'pie'],
        description: 'Optional presentation preference.',
      },
    },
    output: {
      // `type: json` is compiled by defineTool into an open JSON schema. The
      // contract parser below remains the authoritative runtime validator.
      schema: { type: 'json' },
      render: (_args, value) => renderResult(value),
      presentationMeta: (_args, value) => parseDataQueryPresentation(value) as unknown as JsonValue,
    },
    async execute(args, exec: ToolRunContext): Promise<JsonValue> {
      const input = parseDataQueryInput(args)
      try {
        const result = await gateway.query(input, exec.signal)
        return normalizeGatewayResult(input, result) as unknown as JsonValue
      } catch (error: unknown) {
        return failurePresentation(input, error, exec.signal) as unknown as JsonValue
      }
    },
  })
}

/** Install one gateway-backed `data_query` Tool into a Harness context. */
export function installDataQueryTool(ctx: Context, gateway: QueryGateway): () => void {
  return ctx.tools.register(createDataQueryTool(gateway))
}

/** Loader entry name used by the Bundle patch. */
export const name = 'wren-data-agent-host'

/** Wait for the public Harness Tool registry before installing the Tool. */
export const inject = ['tools'] as const

/**
 * Default Cordis plugin entry. It is deliberately unavailable until a real
 * QueryGateway adapter replaces this registration boundary.
 */
export function apply(ctx: Context): void {
  installDataQueryTool(ctx, unavailableQueryGateway)
}
