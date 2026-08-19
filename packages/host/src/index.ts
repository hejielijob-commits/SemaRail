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
  parseSemanticContext,
  parseSemanticContextInput,
  SCHEMA_VERSION,
} from '@hejielijob/dsh-wren-data-agent-contract'
import type {
  DataAgentErrorCode,
  DataQueryInput,
  DataQueryPresentation,
  SemanticContext,
} from '@hejielijob/dsh-wren-data-agent-contract'
import type { ToolDefinition, ToolRunContext } from '@deepseek-ai/dsh-tools'
import type { JsonValue } from '@deepseek-ai/dsh-session'
import type {} from '@deepseek-ai/dsh-system-prompt'
import {
  createSidecarQueryGateway,
  createSubprocessSidecarSpawn,
  type SidecarGatewayConfig,
} from './sidecar.js'
import type { SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'
import {
  QueryGatewayError,
  SemanticContextGatewayError,
  type QueryGateway,
  type SemanticContextGateway,
} from './types.js'

export type { QueryGateway, SemanticContextGateway } from './types.js'
export { QueryGatewayError, SemanticContextGatewayError } from './types.js'
export {
  createSidecarQueryGateway,
  createSubprocessSidecarSpawn,
  SidecarQueryGateway,
  SidecarRpcClient,
  SidecarRpcError,
  SidecarProcessError,
} from './sidecar.js'
export type {
  SidecarChildProcess,
  SidecarGatewayConfig,
  SidecarRequestOptions,
  SidecarSpawn,
} from './sidecar.js'
export { DEFAULT_MAX_FRAME_BYTES, SidecarFrameDecoder, SidecarFrameError, encodeSidecarFrame } from './framing.js'

export const TOOL_NAME = 'data_query' as const
/** Stable wire name of the semantic-context Tool. */
export const CONTEXT_TOOL_NAME = 'wren_semantic_context' as const
/** Stable SystemPrompt section name for the Wren data-agent operating rules. */
export const SYSTEM_PROMPT_SECTION_NAME = 'wren:data-agent' as const
export { MAX_PREVIEW_BYTES, MAX_PREVIEW_ROWS, MAX_QUERY_ROWS, SCHEMA_VERSION }

/** Stable bounds shared with the version-one contract. */
export const MAX_QUERY_TEXT_LENGTH = 64_000 as const

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

type SemanticContextToolError = {
  readonly schemaVersion: typeof SCHEMA_VERSION
  readonly status: 'error'
  readonly error: {
    readonly code: DataAgentErrorCode
    readonly message: string
    readonly retryable: boolean
  }
}

type SemanticContextToolResult = SemanticContext | SemanticContextToolError

function contextFailure(error: unknown, signal: AbortSignal): SemanticContextToolError {
  const code: DataAgentErrorCode = signal.aborted
    ? 'CANCELLED'
    : error instanceof SemanticContextGatewayError ? error.code : 'INTERNAL_ERROR'
  const retryable = error instanceof SemanticContextGatewayError
    ? error.retryable
    : false
  return {
    schemaVersion: SCHEMA_VERSION,
    status: 'error',
    error: { code, message: safeErrorMessage(code), retryable },
  }
}

function normalizeContextResult(value: SemanticContext): SemanticContext {
  return parseSemanticContext(value)
}

function renderContextResult(value: unknown): Array<{ type: 'text'; text: string }> {
  if (typeof value === 'object' && value !== null && 'status' in value && value.status === 'error') {
    const failure = value as SemanticContextToolError
    return [{ type: 'text', text: `Wren semantic context failed (${failure.error.code}).` }]
  }
  const context = parseSemanticContext(value)
  return [{ type: 'text', text: `Wren semantic context resolved ${context.models.length} model(s).` }]
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

/** Build the registry-ready `wren_semantic_context` definition. */
export function createSemanticContextTool(gateway: SemanticContextGateway): ToolDefinition {
  return defineTool({
    name: CONTEXT_TOOL_NAME,
    description: 'Resolve the Wren semantic context before generating semantic SQL.',
    parameters: {
      question: {
        type: 'string',
        required: true,
        description: 'The natural-language question whose semantic context is needed.',
      },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderContextResult(value),
      presentationMeta: (_args, value) => value as JsonValue,
    },
    async execute(args, exec: ToolRunContext): Promise<JsonValue> {
      const input = parseSemanticContextInput(args)
      try {
        const result = await gateway.context(input, exec.signal)
        return normalizeContextResult(result) as unknown as JsonValue
      } catch (error: unknown) {
        return contextFailure(error, exec.signal) as unknown as JsonValue
      }
    },
  })
}

/** Install one gateway-backed semantic-context Tool into a Harness context. */
export function installSemanticContextTool(ctx: Context, gateway: SemanticContextGateway): () => void {
  return ctx.tools.register(createSemanticContextTool(gateway))
}

/** Loader entry name used by the Bundle patch. */
export const name = 'wren-data-agent-host'

/** Wait for the public Harness registries used by this Host plugin. */
export const inject = ['tools', 'subprocess', 'systemPrompt'] as const

/**
 * Model-facing rules for the semantic query loop. This is deliberately
 * credential-free and does not reveal the sidecar project path or DSN name.
 */
export const SYSTEM_PROMPT_GUIDANCE = [
  'For data questions, first call `wren_semantic_context` with the exact user question.',
  'Treat the successful semantic-context response as the authoritative allowlist: use only its returned models, tables, columns, relationships, and semantic roles.',
  'Do not guess or invent fields, entities, joins, filters, or metric definitions that are absent from the returned context.',
  'Only after context succeeds, generate semanticSql and call `data_query`; treat generated SQL as untrusted and keep it read-only.',
  'If a recoverable semantic/query validation failure occurs, make at most one repair attempt using only the same returned entities.',
  'Never retry `POLICY_DENIED`, `TIMEOUT`, or `CANCELLED`; report the bounded failure instead of guessing or bypassing policy.',
  'If Wren is unavailable or context cannot be resolved, explain that data is unavailable and do not fabricate an answer.',
].join('\n')

function configuredProjectDir(config: SidecarGatewayConfig | undefined): string | undefined {
  const value = config?.projectDir
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : undefined
}

/**
 * Default Cordis plugin entry. A configured plugin starts the supervised
 * sidecar in the order health → project.validate and then serves both tools.
 * Without a project directory the tools remain registered but fail closed.
 */
export function apply(ctx: Context, config?: SidecarGatewayConfig): void {
  const runtime = (ctx as Context & { subprocess?: SubprocessRuntime }).subprocess
  const spawn = config?.spawn
    ?? (runtime === undefined ? undefined : createSubprocessSidecarSpawn(runtime, config))
  const projectDir = configuredProjectDir(config)
  let gateway: ReturnType<typeof createSidecarQueryGateway> | undefined
  if (config !== undefined && projectDir !== undefined && spawn !== undefined) {
    try {
      gateway = createSidecarQueryGateway({ ...config, projectDir }, spawn)
    } catch {
      // Invalid deployment configuration is an unavailable data boundary,
      // never a reason to expose a startup exception or a credential-bearing
      // diagnostic through Harness.
      gateway = undefined
    }
  }
  const queryGateway = gateway ?? unavailableQueryGateway
  const contextGateway: SemanticContextGateway = gateway ?? unavailableSemanticContextGateway
  installDataQueryTool(ctx, queryGateway)
  installSemanticContextTool(ctx, contextGateway)
  ctx.effect(
    () => ctx.systemPrompt.section({
      name: SYSTEM_PROMPT_SECTION_NAME,
      order: 125,
      text: SYSTEM_PROMPT_GUIDANCE,
    }),
    'wren-data-agent-host.system-prompt',
  )
  if (gateway !== undefined) {
    ctx.effect(() => () => gateway.dispose(), 'wren-data-agent-host.sidecar()')
    // Cordis apply hooks are synchronous. Startup is deterministic and lazy
    // requests await the same health → project.validate promise.
    void gateway.start().catch(() => undefined)
  }
}

/** Default context gateway: it never fabricates semantic entities. */
export const unavailableSemanticContextGateway: SemanticContextGateway = {
  async context(): Promise<SemanticContext> {
    throw new SemanticContextGatewayError()
  },
}
