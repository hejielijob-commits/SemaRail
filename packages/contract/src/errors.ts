import { _enum, _keys, _record, _required, _string, _boolean, type JsonSchema } from './json.js'

/** Stable application and transport error codes. */
export const ERROR_CODES = [
  'SEMANTIC_ERROR',
  'POLICY_DENIED',
  'DATABASE_ERROR',
  'TIMEOUT',
  'CANCELLED',
  'SIDECAR_UNAVAILABLE',
  'UNSUPPORTED_PROTOCOL',
  'INVALID_PARAMS',
  'METHOD_NOT_FOUND',
  'WREN_UNAVAILABLE',
  'PROJECT_VALIDATION_FAILED',
  'HEALTHCHECK_FAILED',
  'FRAME_TOO_LARGE',
  'TRUNCATED_FRAME',
  'INVALID_REQUEST',
  'PROTOCOL_ERROR',
  'UNSUPPORTED_VERSION',
  'INTERNAL_ERROR',
  'UNAUTHENTICATED',
] as const

/** Stable error code emitted by Host, sidecar, or presentation validation. */
export type DataAgentErrorCode = typeof ERROR_CODES[number]

/** Alias for consumers that call these RPC error codes. */
export type StableErrorCode = DataAgentErrorCode

/** Error payload shared by RPC failures and query presentations. */
export interface DataAgentError {
  /** Stable machine-readable code. */
  readonly code: DataAgentErrorCode
  /** Short phase label, for example `context` or `run`. */
  readonly phase: string
  /** Safe diagnostic without credentials or DSNs. */
  readonly message: string
  /** Whether retrying may succeed. */
  readonly retryable: boolean
}

/** @internal */
export function _parseError(value: unknown, path: string): DataAgentError {
  const object = _record(value, path)
  _keys(object, ['code', 'phase', 'message', 'retryable'], path)
  return {
    code: _enum(_required(object, 'code', path), ERROR_CODES, `${path}.code`),
    phase: _string(_required(object, 'phase', path), `${path}.phase`, 1, 64),
    message: _string(_required(object, 'message', path), `${path}.message`, 1, 4_000),
    retryable: _boolean(_required(object, 'retryable', path), `${path}.retryable`),
  }
}

/** JSON Schema for a stable error payload. */
export const ERROR_JSON_SCHEMA: JsonSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['code', 'phase', 'message', 'retryable'],
  properties: {
    code: { enum: [...ERROR_CODES] },
    phase: { type: 'string', minLength: 1, maxLength: 64 },
    message: { type: 'string', minLength: 1, maxLength: 4_000 },
    retryable: { type: 'boolean' },
  },
}
