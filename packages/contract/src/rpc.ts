import {
  _boolean,
  _enum,
  _fail,
  _integer,
  _json,
  _keys,
  _optional,
  _record,
  _required,
  _string,
  LEGACY_PROTOCOL_VERSION,
  PROTOCOL_VERSION,
  type JsonSchema,
  type JsonValue,
} from './json.js'
import {
  ERROR_JSON_SCHEMA,
  ERROR_V2_JSON_SCHEMA,
  _parseError,
  _parseErrorV2,
  type DataAgentError,
  type DataAgentErrorV2,
} from './errors.js'
import { _schema, type ContractSchema } from './schema.js'

/** Supported sidecar RPC methods. */
export const RPC_METHODS = ['health', 'project.validate', 'project.describe', 'context.ask', 'query.prepare', 'query.dryPlan', 'query.run', 'query.cancel'] as const

/** Supported sidecar RPC method. */
export type RpcMethod = typeof RPC_METHODS[number]

/** Public RPC protocol versions accepted during the v2 migration. */
export type RpcProtocolVersion = typeof LEGACY_PROTOCOL_VERSION | typeof PROTOCOL_VERSION

function parseProtocolVersion(value: unknown, path: string): RpcProtocolVersion {
  if (value === LEGACY_PROTOCOL_VERSION || value === PROTOCOL_VERSION) return value
  _fail(path, `unsupported version; expected ${JSON.stringify(PROTOCOL_VERSION)}`, 'UNSUPPORTED_VERSION')
}

/** Request envelope sent to the sidecar. */
export interface RpcRequest<Params extends JsonValue = JsonValue> {
  readonly protocolVersion: RpcProtocolVersion
  readonly id: string
  readonly method: RpcMethod
  readonly params: Params
  readonly deadlineMs?: number
}

/** Successful RPC response. */
export interface RpcSuccess<Result extends JsonValue = JsonValue> {
  readonly protocolVersion: RpcProtocolVersion
  readonly id: string
  readonly ok: true
  readonly result: Result
}

/** Failed RPC response. */
export interface RpcFailureV1 {
  readonly protocolVersion: typeof LEGACY_PROTOCOL_VERSION
  readonly id: string
  readonly ok: false
  readonly error: DataAgentError
}

/** Failed current RPC response carrying structured diagnostic details. */
export interface RpcFailureV2 {
  readonly protocolVersion: typeof PROTOCOL_VERSION
  readonly id: string
  readonly ok: false
  readonly error: DataAgentErrorV2
}

/** Failed RPC response for either supported protocol version. */
export type RpcFailure = RpcFailureV1 | RpcFailureV2

/** RPC response envelope. */
export type RpcResponse<Result extends JsonValue = JsonValue> = RpcSuccess<Result> | RpcFailure

/** Parse an RPC request and fail closed on unknown versions/fields. */
export function parseRpcRequest(value: unknown): RpcRequest {
  const object = _record(value, 'request')
  _keys(object, ['protocolVersion', 'id', 'method', 'params', 'deadlineMs'], 'request')
  const protocolVersion = parseProtocolVersion(_required(object, 'protocolVersion', 'request'), 'request.protocolVersion')
  const deadlineMs = _optional(object, 'deadlineMs')
  return {
    protocolVersion,
    id: _string(_required(object, 'id', 'request'), 'request.id', 1, 128),
    method: _enum(_required(object, 'method', 'request'), RPC_METHODS, 'request.method'),
    params: _json(_required(object, 'params', 'request'), 'request.params'),
    ...(deadlineMs === undefined ? {} : { deadlineMs: _integer(deadlineMs, 'request.deadlineMs') }),
  }
}

/** Parse an RPC response and enforce its success/error discriminant. */
export function parseRpcResponse(value: unknown): RpcResponse {
  const object = _record(value, 'response')
  _keys(object, ['protocolVersion', 'id', 'ok', 'result', 'error'], 'response')
  const protocolVersion = parseProtocolVersion(_required(object, 'protocolVersion', 'response'), 'response.protocolVersion')
  const id = _string(_required(object, 'id', 'response'), 'response.id', 1, 128)
  const ok = _boolean(_required(object, 'ok', 'response'), 'response.ok')
  const hasResult = Object.prototype.hasOwnProperty.call(object, 'result')
  const hasError = Object.prototype.hasOwnProperty.call(object, 'error')
  if (ok) {
    if (!hasResult) _fail('response.result', 'field is required when response.ok is true')
    if (hasError) _fail('response.error', 'must be omitted when response.ok is true')
    return { protocolVersion, id, ok: true, result: _json(object.result, 'response.result') }
  }
  if (!hasError) _fail('response.error', 'field is required when response.ok is false')
  if (hasResult) _fail('response.result', 'must be omitted when response.ok is false')
  return protocolVersion === LEGACY_PROTOCOL_VERSION
    ? { protocolVersion, id, ok: false, error: _parseError(object.error, 'response.error') }
    : { protocolVersion, id, ok: false, error: _parseErrorV2(object.error, 'response.error') }
}

const SCHEMA = 'https://json-schema.org/draft/2020-12/schema'

/** JSON Schema for an RPC request envelope. */
export const RPC_REQUEST_JSON_SCHEMA: JsonSchema = {
  $schema: SCHEMA,
  type: 'object',
  additionalProperties: false,
  required: ['protocolVersion', 'id', 'method', 'params'],
  properties: {
    protocolVersion: { enum: [LEGACY_PROTOCOL_VERSION, PROTOCOL_VERSION] },
    id: { type: 'string', minLength: 1, maxLength: 128 },
    method: { enum: [...RPC_METHODS] },
    params: {},
    deadlineMs: { type: 'integer', minimum: 0 },
  },
}

/** JSON Schema for an RPC response envelope. */
export const RPC_RESPONSE_JSON_SCHEMA: JsonSchema = {
  $schema: SCHEMA,
  type: 'object',
  additionalProperties: false,
  oneOf: [
    { required: ['protocolVersion', 'id', 'ok', 'result'], properties: { ok: { const: true } } },
    {
      required: ['protocolVersion', 'id', 'ok', 'error'],
      properties: { protocolVersion: { const: LEGACY_PROTOCOL_VERSION }, ok: { const: false }, error: ERROR_JSON_SCHEMA },
    },
    {
      required: ['protocolVersion', 'id', 'ok', 'error'],
      properties: { protocolVersion: { const: PROTOCOL_VERSION }, ok: { const: false }, error: ERROR_V2_JSON_SCHEMA },
    },
  ],
  properties: {
    protocolVersion: { enum: [LEGACY_PROTOCOL_VERSION, PROTOCOL_VERSION] },
    id: { type: 'string', minLength: 1, maxLength: 128 },
    ok: { type: 'boolean' },
    result: {},
    error: { anyOf: [ERROR_JSON_SCHEMA, ERROR_V2_JSON_SCHEMA] },
  },
}

/** RPC request parser/schema pair. */
export const rpcRequestSchema: ContractSchema<RpcRequest> = _schema(RPC_REQUEST_JSON_SCHEMA, parseRpcRequest)

/** RPC response parser/schema pair. */
export const rpcResponseSchema: ContractSchema<RpcResponse> = _schema(RPC_RESPONSE_JSON_SCHEMA, parseRpcResponse)

/** Pascal-case schema alias. */
export const RpcRequestSchema = rpcRequestSchema

/** Pascal-case schema alias. */
export const RpcResponseSchema = rpcResponseSchema

/** Type guard for RPC request values. */
export const isRpcRequest = (value: unknown): value is RpcRequest => rpcRequestSchema.check(value)

/** Type guard for RPC response values. */
export const isRpcResponse = (value: unknown): value is RpcResponse => rpcResponseSchema.check(value)
