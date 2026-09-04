import type { Context } from '@deepseek-ai/cordis'
import type { JsonValue } from '@deepseek-ai/dsh-session'
import { defineTool, type ToolDefinition, type ToolRunContext } from '@deepseek-ai/dsh-tools'
import {
  DATA_QUERY_PRESENTATION_VERSION,
  MAX_ARTIFACT_BYTES,
  MAX_ARTIFACT_PREVIEW_ROWS,
  MAX_INLINE_PREVIEW_BYTES,
  MAX_INLINE_PREVIEW_ROWS,
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_ROWS,
  MAX_QUERY_ROWS,
  parseDataQueryInput,
  parseDataQueryPresentation,
  parseSemanticContext,
  parseSemanticContextInput,
  SCHEMA_VERSION,
  type DataAgentErrorCode,
  type DataQueryInput,
  type DataQueryPresentation,
  type SemanticContext,
} from '@hejielijob/dsh-wren-data-agent-contract'
import {
  QueryGatewayError,
  SemanticContextGatewayError,
  type QueryGateway,
  type SemanticContextGateway,
} from './types.js'
import { renderDataQueryResult } from './presentation.js'

export { projectDataQueryForModel, renderDataQueryResult } from './presentation.js'

export const TOOL_NAME = 'data_query' as const
export const CONTEXT_TOOL_NAME = 'semarail_semantic_context' as const
export const SYSTEM_PROMPT_SECTION_NAME = 'semarail:data-agent' as const
export { DATA_QUERY_PRESENTATION_VERSION, MAX_ARTIFACT_BYTES, MAX_ARTIFACT_PREVIEW_ROWS, MAX_INLINE_PREVIEW_BYTES, MAX_INLINE_PREVIEW_ROWS, MAX_PREVIEW_BYTES, MAX_PREVIEW_ROWS, MAX_QUERY_ROWS, SCHEMA_VERSION }
export const MAX_QUERY_TEXT_LENGTH = 64_000 as const

export const unavailableQueryGateway: QueryGateway = {
  async query(): Promise<DataQueryPresentation> { throw new QueryGatewayError() },
}

export const unavailableSemanticContextGateway: SemanticContextGateway = {
  async context(): Promise<SemanticContext> { throw new SemanticContextGatewayError() },
}

function safeErrorMessage(code: DataAgentErrorCode): string {
  switch (code) {
    case 'SEMANTIC_ERROR': return 'SemaRail rejected the semantic query.'
    case 'POLICY_DENIED': return 'The query was denied by policy.'
    case 'DATABASE_ERROR': return 'SemaRail could not read the data source.'
    case 'TIMEOUT': return 'The SemaRail query timed out.'
    case 'CANCELLED': return 'The SemaRail query was cancelled.'
    case 'SIDECAR_UNAVAILABLE': return 'The SemaRail sidecar is unavailable.'
    case 'INVALID_PARAMS': return 'The SemaRail query parameters were invalid.'
    case 'METHOD_NOT_FOUND': return 'The SemaRail sidecar does not support this query.'
    case 'PROJECT_VALIDATION_FAILED': return 'The semantic project failed validation.'
    case 'HEALTHCHECK_FAILED': return 'The SemaRail sidecar health check failed.'
    case 'FRAME_TOO_LARGE': return 'The SemaRail sidecar response was too large.'
    case 'TRUNCATED_FRAME': return 'The SemaRail sidecar response was truncated.'
    case 'INVALID_REQUEST': return 'The SemaRail sidecar rejected the request.'
    case 'PROTOCOL_ERROR': return 'The SemaRail sidecar returned an invalid response.'
    case 'UNSUPPORTED_PROTOCOL': return 'The SemaRail sidecar protocol is unsupported.'
    case 'UNSUPPORTED_VERSION': return 'The SemaRail data contract version is unsupported.'
    case 'INTERNAL_ERROR': return 'The SemaRail query gateway returned an internal error.'
    case 'UNAUTHENTICATED': return 'SemaRail Core authentication failed.'
    case 'WREN_UNAVAILABLE': return 'The SemaRail semantic runtime is unavailable.'
  }
  return 'The SemaRail query gateway returned an internal error.'
}

function failurePresentation(input: DataQueryInput, error: unknown, signal: AbortSignal): DataQueryPresentation {
  const code: DataAgentErrorCode = signal.aborted
    ? 'CANCELLED'
    : error instanceof QueryGatewayError ? error.code : 'INTERNAL_ERROR'
  const retryable = error instanceof QueryGatewayError ? error.retryable : false
  return parseDataQueryPresentation({
    schemaVersion: SCHEMA_VERSION,
    queryId: 'semarail-unavailable',
    status: 'error',
    semanticSql: input.semanticSql,
    question: input.question,
    columns: [],
    previewRows: [],
    stats: { returnedRows: 0, durationMs: 0, truncated: false },
    error: { code, phase: 'query', message: safeErrorMessage(code), retryable },
  })
}

function normalizeGatewayResult(input: DataQueryInput, value: DataQueryPresentation): DataQueryPresentation {
  const parsed = parseDataQueryPresentation(value)
  const normalized = { ...parsed, semanticSql: input.semanticSql, question: input.question }
  return parseDataQueryPresentation(parsed.status === 'error'
    ? { ...normalized, error: { ...parsed.error, message: safeErrorMessage(parsed.error.code) } }
    : normalized)
}

type SemanticContextToolError = {
  readonly schemaVersion: typeof SCHEMA_VERSION
  readonly status: 'error'
  readonly error: { readonly code: DataAgentErrorCode; readonly message: string; readonly retryable: boolean }
}

function contextFailure(error: unknown, signal: AbortSignal): SemanticContextToolError {
  const code: DataAgentErrorCode = signal.aborted
    ? 'CANCELLED'
    : error instanceof SemanticContextGatewayError ? error.code : 'INTERNAL_ERROR'
  return {
    schemaVersion: SCHEMA_VERSION,
    status: 'error',
    error: {
      code,
      message: safeErrorMessage(code),
      retryable: error instanceof SemanticContextGatewayError ? error.retryable : false,
    },
  }
}

function renderContextResult(value: unknown): Array<{ type: 'text'; text: string }> {
  if (typeof value === 'object' && value !== null && 'status' in value && value.status === 'error') {
    const failure = value as SemanticContextToolError
    return [{ type: 'text', text: `SemaRail semantic context failed (${failure.error.code}).` }]
  }
  const context = parseSemanticContext(value)
  return [{ type: 'text', text: `SemaRail semantic context resolved ${context.models.length} model(s).` }]
}

export function createDataQueryTool(gateway: QueryGateway): ToolDefinition {
  return defineTool({
    name: TOOL_NAME,
    description: 'Query SemaRail through the configured semantic layer and return a bounded JSON preview.',
    parameters: {
      question: { type: 'string', required: true, description: 'The natural-language question represented by this query.' },
      semanticSql: { type: 'string', required: true, description: 'Model-generated semantic SQL. Treat it as untrusted input.' },
      chartIntent: { type: 'string', enum: ['auto', 'table', 'line', 'bar', 'pie'], description: 'Optional presentation preference.' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderDataQueryResult(value),
      presentationMeta: (_args, value) => parseDataQueryPresentation(value) as unknown as JsonValue,
    },
    async execute(args, exec: ToolRunContext): Promise<JsonValue> {
      const input = parseDataQueryInput(args)
      try {
        return normalizeGatewayResult(input, await gateway.query(input, exec.signal)) as unknown as JsonValue
      } catch (error: unknown) {
        return failurePresentation(input, error, exec.signal) as unknown as JsonValue
      }
    },
  })
}

export function installDataQueryTool(ctx: Context, gateway: QueryGateway): () => void {
  return ctx.tools.register(createDataQueryTool(gateway))
}

export function createSemanticContextTool(gateway: SemanticContextGateway): ToolDefinition {
  return defineTool({
    name: CONTEXT_TOOL_NAME,
    description: 'Resolve the SemaRail semantic context before generating semantic SQL.',
    parameters: {
      question: { type: 'string', required: true, description: 'The natural-language question whose semantic context is needed.' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderContextResult(value),
      presentationMeta: (_args, value) => value as JsonValue,
    },
    async execute(args, exec: ToolRunContext): Promise<JsonValue> {
      const input = parseSemanticContextInput(args)
      try {
        return parseSemanticContext(await gateway.context(input, exec.signal)) as unknown as JsonValue
      } catch (error: unknown) {
        return contextFailure(error, exec.signal) as unknown as JsonValue
      }
    },
  })
}

export function installSemanticContextTool(ctx: Context, gateway: SemanticContextGateway): () => void {
  return ctx.tools.register(createSemanticContextTool(gateway))
}

export const SYSTEM_PROMPT_GUIDANCE = [
  'For data questions, first call `semarail_semantic_context` with the exact user question.',
  'Treat the successful semantic-context response as the authoritative allowlist: use only its returned models, tables, columns, relationships, and semantic roles.',
  'When semantic context includes `sqlHistory`, use only those confirmed examples as optional few-shot guidance; never invent or claim an unreturned historical SQL reference.',
  'Do not guess or invent fields, entities, joins, filters, or metric definitions that are absent from the returned context.',
  'Only after context succeeds, generate semanticSql and call `data_query`; treat generated SQL as untrusted and keep it read-only.',
  'If a recoverable semantic/query validation failure occurs, make at most one repair attempt using only the same returned entities.',
  'Never retry `POLICY_DENIED`, `TIMEOUT`, or `CANCELLED`; report the bounded failure instead of guessing or bypassing policy.',
  'If SemaRail is unavailable or context cannot be resolved, explain that data is unavailable and do not fabricate an answer.',
].join('\n')
