import type {
  DataAgentErrorCode,
  DataQueryInput,
  DataQueryPresentation,
  SemanticContext,
  SemanticContextInput,
} from '@hejielijob/dsh-wren-data-agent-contract'

/** Gateway boundary used by the registered `data_query` Tool. */
export interface QueryGateway {
  /** Execute one already-validated query and observe Host cancellation. */
  query(input: DataQueryInput, signal: AbortSignal): Promise<DataQueryPresentation>
}

/** Gateway boundary used by the semantic context Tool. */
export interface SemanticContextGateway {
  /** Resolve the Wren semantic context for one user question. */
  context(input: SemanticContextInput, signal: AbortSignal): Promise<SemanticContext>
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

/** A stable semantic-context failure that never carries adapter diagnostics. */
export class SemanticContextGatewayError extends Error {
  readonly code: DataAgentErrorCode
  readonly retryable: boolean

  constructor(
    code: DataAgentErrorCode = 'WREN_UNAVAILABLE',
    retryable = true,
  ) {
    super('Wren semantic context is unavailable.')
    this.name = 'SemanticContextGatewayError'
    this.code = code
    this.retryable = retryable
  }
}

