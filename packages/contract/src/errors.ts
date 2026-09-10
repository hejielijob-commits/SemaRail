import { _enum, _fail, _keys, _record, _required, _string, _boolean, _strings, type JsonSchema } from './json.js'

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
  'RESULT_TOO_LARGE',
  'INVALID_REQUEST',
  'PROTOCOL_ERROR',
  'UNSUPPORTED_VERSION',
  'INTERNAL_ERROR',
  'UNAUTHENTICATED',
  'CLARIFICATION_REQUIRED',
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

/** Stable reason codes carried by the detailed v2 error protocol. */
export const ERROR_REASON_CODES = [
  'AUTHENTICATION_EXPIRED',
  'ACCOUNT_DISABLED',
  'PROJECT_PERMISSION_REQUIRED',
  'DATASOURCE_PERMISSION_REQUIRED',
  'TOOL_PERMISSION_REQUIRED',
  'TABLE_PERMISSION_REQUIRED',
  'COLUMN_PERMISSION_REQUIRED',
  'EXPLICIT_DENIAL',
  'UNAUTHORIZED',
  'ROW_ATTRIBUTE_MISSING',
  'DATABASE_PERMISSION_REQUIRED',
  'SQL_SAFETY_RESTRICTION',
  'SEMANTIC_PARSE_FAILED',
  'QUERY_TIMEOUT',
  'CONNECTION_FAILED',
  'UNSUPPORTED_DATASOURCE',
  'INTERNAL_FAILURE',
  'CLARIFICATION_REQUIRED',
] as const

/** Specific reason for a v2 failure. */
export type DataAgentErrorReasonCode = typeof ERROR_REASON_CODES[number]

/** Layer that made the failure decision. */
export const ERROR_ORIGINS = [
  'authentication',
  'semarail-policy',
  'query-safety',
  'semantic-runtime',
  'database',
  'transport',
  'core',
] as const

/** Layer that made the failure decision. */
export type DataAgentErrorOrigin = typeof ERROR_ORIGINS[number]

/** A request-relevant object named by a detailed error. */
export interface DataAgentErrorResource {
  readonly kind: 'project' | 'datasource' | 'tool' | 'table' | 'column' | 'attribute'
  readonly name: string
}

/** Detailed v2 error payload. */
export interface DataAgentErrorV2 extends DataAgentError {
  readonly reasonCode: DataAgentErrorReasonCode
  readonly resources: readonly DataAgentErrorResource[]
  readonly requiredPermissions: readonly string[]
  readonly suggestion: string
  readonly origin: DataAgentErrorOrigin
  readonly traceId: string
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

/** @internal */
export function _parseErrorV2(value: unknown, path: string): DataAgentErrorV2 {
  const object = _record(value, path)
  _keys(object, [
    'code', 'phase', 'message', 'retryable', 'reasonCode', 'resources',
    'requiredPermissions', 'suggestion', 'origin', 'traceId',
  ], path)
  const resources = _required(object, 'resources', path)
  if (!Array.isArray(resources)) _fail(`${path}.resources`, 'expected an array')
  if (resources.length > 32) _fail(`${path}.resources`, 'must contain at most 32 item(s)')
  return {
    code: _enum(_required(object, 'code', path), ERROR_CODES, `${path}.code`),
    phase: _string(_required(object, 'phase', path), `${path}.phase`, 1, 64),
    message: _string(_required(object, 'message', path), `${path}.message`, 1, 4_000),
    retryable: _boolean(_required(object, 'retryable', path), `${path}.retryable`),
    reasonCode: _enum(_required(object, 'reasonCode', path), ERROR_REASON_CODES, `${path}.reasonCode`),
    resources: resources.map((value, index) => {
      const resourcePath = `${path}.resources[${index}]`
      const resource = _record(value, resourcePath)
      _keys(resource, ['kind', 'name'], resourcePath)
      return {
        kind: _enum(_required(resource, 'kind', resourcePath), ['project', 'datasource', 'tool', 'table', 'column', 'attribute'] as const, `${resourcePath}.kind`),
        name: _string(_required(resource, 'name', resourcePath), `${resourcePath}.name`, 1, 512),
      }
    }),
    requiredPermissions: _strings(_required(object, 'requiredPermissions', path), `${path}.requiredPermissions`, 0, 32)
      .map((permission, index) => _string(permission, `${path}.requiredPermissions[${index}]`, 1, 256)),
    suggestion: _string(_required(object, 'suggestion', path), `${path}.suggestion`, 1, 2_000),
    origin: _enum(_required(object, 'origin', path), ERROR_ORIGINS, `${path}.origin`),
    traceId: _string(_required(object, 'traceId', path), `${path}.traceId`, 1, 128),
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


/** JSON Schema for a detailed v2 error payload. */
export const ERROR_V2_JSON_SCHEMA: JsonSchema = {
  type: 'object',
  additionalProperties: false,
  required: [
    'code', 'phase', 'message', 'retryable', 'reasonCode', 'resources',
    'requiredPermissions', 'suggestion', 'origin', 'traceId',
  ],
  properties: {
    code: { enum: [...ERROR_CODES] },
    phase: { type: 'string', minLength: 1, maxLength: 64 },
    message: { type: 'string', minLength: 1, maxLength: 4_000 },
    retryable: { type: 'boolean' },
    reasonCode: { enum: [...ERROR_REASON_CODES] },
    resources: {
      type: 'array', maxItems: 32, items: {
        type: 'object', additionalProperties: false, required: ['kind', 'name'],
        properties: {
          kind: { enum: ['project', 'datasource', 'tool', 'table', 'column', 'attribute'] },
          name: { type: 'string', minLength: 1, maxLength: 512 },
        },
      },
    },
    requiredPermissions: { type: 'array', maxItems: 32, items: { type: 'string', minLength: 1, maxLength: 256 } },
    suggestion: { type: 'string', minLength: 1, maxLength: 2_000 },
    origin: { enum: [...ERROR_ORIGINS] },
    traceId: { type: 'string', minLength: 1, maxLength: 128 },
  },
}
