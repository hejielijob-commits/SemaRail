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
  _version,
  PROTOCOL_VERSION,
  type JsonSchema,
  type JsonValue,
} from './json.js'
import { ERROR_JSON_SCHEMA, _parseError, type DataAgentError } from './errors.js'
import { _schema, type ContractSchema } from './schema.js'

/** Supported sidecar RPC methods. */
export const RPC_METHODS = ['health', 'project.validate', 'project.describe', 'context.ask', 'query.dryPlan', 'query.run', 'query.cancel'] as const

/** Supported sidecar RPC method. */
export type RpcMethod = typeof RPC_METHODS[number]

/** Request envelope sent to the sidecar. */
export interface RpcRequest<Params extends JsonValue = JsonValue> {
  readonly protocolVersion: typeof PROTOCOL_VERSION
  readonly id: string
  readonly method: RpcMethod
  readonly params: Params
  readonly deadlineMs?: number
}

/** Successful RPC response. */
export interface RpcSuccess<Result extends JsonValue = JsonValue> {
  readonly protocolVersion: typeof PROTOCOL_VERSION
  readonly id: string
  readonly ok: true
  readonly result: Result
}

/** Failed RPC response. */
export interface RpcFailure {
  readonly protocolVersion: typeof PROTOCOL_VERSION
  readonly id: string
  readonly ok: false
  readonly error: DataAgentError
}

/** RPC response envelope. */
export type RpcResponse<Result extends JsonValue = JsonValue> = RpcSuccess<Result> | RpcFailure

/** Parse an RPC request and fail closed on unknown versions/fields. */
export function parseRpcRequest(value: unknown): RpcRequest {
  const object = _record(value, 'request')
  _keys(object, ['protocolVersion', 'id', 'method', 'params', 'deadlineMs'], 'request')
  _version(_required(object, 'protocolVersion', 'request'), PROTOCOL_VERSION, 'request.protocolVersion')
  const deadlineMs = _optional(object, 'deadlineMs')
  return {
    protocolVersion: PROTOCOL_VERSION,
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
  _version(_required(object, 'protocolVersion', 'response'), PROTOCOL_VERSION, 'response.protocolVersion')
  const id = _string(_required(object, 'id', 'response'), 'response.id', 1, 128)
  const ok = _boolean(_required(object, 'ok', 'response'), 'response.ok')
  const hasResult = Object.prototype.hasOwnProperty.call(object, 'result')
  const hasError = Object.prototype.hasOwnProperty.call(object, 'error')
  if (ok) {
    if (!hasResult) _fail('response.result', 'field is required when response.ok is true')
    if (hasError) _fail('response.error', 'must be omitted when response.ok is true')
    return { protocolVersion: PROTOCOL_VERSION, id, ok: true, result: _json(object.result, 'response.result') }
  }
  if (!hasError) _fail('response.error', 'field is required when response.ok is false')
  if (hasResult) _fail('response.result', 'must be omitted when response.ok is false')
  return { protocolVersion: PROTOCOL_VERSION, id, ok: false, error: _parseError(object.error, 'response.error') }
}

const SCHEMA = 'https://json-schema.org/draft/2020-12/schema'

/** JSON Schema for an RPC request envelope. */
export const RPC_REQUEST_JSON_SCHEMA: JsonSchema = {
  $schema: SCHEMA,
  type: 'object',
  additionalProperties: false,
  required: ['protocolVersion', 'id', 'method', 'params'],
  properties: {
    protocolVersion: { const: PROTOCOL_VERSION },
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
    { required: ['protocolVersion', 'id', 'ok', 'error'], properties: { ok: { const: false }, error: ERROR_JSON_SCHEMA } },
  ],
  properties: {
    protocolVersion: { const: PROTOCOL_VERSION },
    id: { type: 'string', minLength: 1, maxLength: 128 },
    ok: { type: 'boolean' },
    result: {},
    error: ERROR_JSON_SCHEMA,
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
