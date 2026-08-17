/** JSON primitives and boundary validation shared by every contract. */

/** The only sidecar protocol version supported by this release. */
export const PROTOCOL_VERSION = '1' as const

/** The only Host/Client presentation schema version supported by this release. */
export const SCHEMA_VERSION = 1 as const

/** Maximum rows fetched by one MVP query. */
export const MAX_QUERY_ROWS = 500 as const

/** Maximum rows persisted in a tool presentation preview. */
export const MAX_PREVIEW_ROWS = 200 as const

/** Maximum UTF-8 JSON bytes persisted for preview rows. */
export const MAX_PREVIEW_BYTES = 1_048_576 as const

/** JSON primitive accepted at a wire boundary. */
export type JsonPrimitive = string | number | boolean | null

/** JSON value accepted at a wire boundary. */
export type JsonValue = JsonPrimitive | readonly JsonValue[] | { readonly [key: string]: JsonValue }

/** JSON object accepted at a wire boundary. */
export type JsonObject = { readonly [key: string]: JsonValue }

/** Scalar allowed in a query preview. */
export type JsonSafeScalar = string | number | boolean | null

/** Draft-2020-12 JSON Schema subset emitted by this package. */
export interface JsonSchema {
  readonly $schema?: string
  readonly $ref?: string
  readonly $defs?: Readonly<Record<string, JsonSchema>>
  readonly anyOf?: readonly JsonSchema[]
  readonly oneOf?: readonly JsonSchema[]
  readonly type?: string | readonly string[]
  readonly const?: JsonValue
  readonly enum?: readonly JsonValue[]
  readonly required?: readonly string[]
  readonly properties?: Readonly<Record<string, JsonSchema>>
  readonly items?: JsonSchema
  readonly additionalProperties?: boolean | JsonSchema
  readonly minItems?: number
  readonly maxItems?: number
  readonly minLength?: number
  readonly maxLength?: number
  readonly minimum?: number
  readonly maximum?: number
}

/** Error category raised by a contract parser. */
export type ContractValidationCode = 'INVALID_CONTRACT' | 'UNSUPPORTED_VERSION'

/** Error thrown when unknown input does not satisfy a contract. */
export class ContractValidationError extends Error {
  /** Machine-readable validation category. */
  readonly code: ContractValidationCode
  /** Dot path to the rejected value. */
  readonly path: string

  /**
   * Create a validation error.
   * @param message - Human-readable reason.
   * @param path - Rejected value path.
   * @param code - Validation category.
   */
  constructor(message: string, path: string, code: ContractValidationCode = 'INVALID_CONTRACT') {
    super(message)
    this.name = 'ContractValidationError'
    this.code = code
    this.path = path
  }
}

/** Successful parser result. */
export interface SafeParseSuccess<T> {
  readonly success: true
  readonly data: T
}

/** Failed parser result. */
export interface SafeParseFailure {
  readonly success: false
  readonly error: ContractValidationError
}

/** Discriminated result returned by safe parser helpers. */
export type SafeParseResult<T> = SafeParseSuccess<T> | SafeParseFailure

/** @internal */
export type UnknownRecord = Record<string, unknown>

/** @internal */
export function _isRecord(value: unknown): value is UnknownRecord {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

/** @internal */
export function _fail(path: string, message: string, code: ContractValidationCode = 'INVALID_CONTRACT'): never {
  throw new ContractValidationError(`${path}: ${message}`, path, code)
}

/** @internal */
export function _record(value: unknown, path: string): UnknownRecord {
  if (!_isRecord(value)) _fail(path, 'expected a JSON object')
  return value
}

/** @internal */
export function _keys(value: UnknownRecord, allowed: readonly string[], path: string): void {
  const allowedSet = new Set(allowed)
  for (const key of Object.keys(value)) if (!allowedSet.has(key)) _fail(path, `unknown field ${JSON.stringify(key)}`)
}

/** @internal */
export function _required(value: UnknownRecord, key: string, path: string): unknown {
  if (!Object.prototype.hasOwnProperty.call(value, key)) _fail(`${path}.${key}`, 'field is required')
  return value[key]
}

/** @internal */
export function _optional(value: UnknownRecord, key: string): unknown {
  return Object.prototype.hasOwnProperty.call(value, key) ? value[key] : undefined
}

/** @internal */
export function _string(value: unknown, path: string, minLength = 0, maxLength?: number): string {
  if (typeof value !== 'string') _fail(path, 'expected a string')
  if (value.length < minLength) _fail(path, `must contain at least ${minLength} character(s)`)
  if (maxLength !== undefined && value.length > maxLength) _fail(path, `must contain at most ${maxLength} character(s)`)
  return value
}

/** @internal */
export function _boolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') _fail(path, 'expected a boolean')
  return value
}

/** @internal */
export function _number(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) _fail(path, 'expected a finite number')
  return value
}

/** @internal */
export function _integer(value: unknown, path: string, maximum?: number): number {
  const number = _number(value, path)
  if (!Number.isInteger(number) || number < 0) _fail(path, 'expected a non-negative integer')
  if (maximum !== undefined && number > maximum) _fail(path, `must be at most ${maximum}`)
  return number
}

/** @internal */
export function _enum<T extends string>(value: unknown, values: readonly T[], path: string): T {
  if (typeof value !== 'string' || !values.includes(value as T)) _fail(path, `expected one of ${values.join(', ')}`)
  return value as T
}

/** @internal */
export function _literal<T extends string | number | boolean>(value: unknown, expected: T, path: string): T {
  if (value !== expected) _fail(path, `expected ${JSON.stringify(expected)}`)
  return expected
}

/** @internal */
export function _version(value: unknown, expected: string | number, path: string): void {
  if (value !== expected) _fail(path, `unsupported version; expected ${JSON.stringify(expected)}`, 'UNSUPPORTED_VERSION')
}

/** @internal */
export function _json(value: unknown, path: string, depth = 0): JsonValue {
  if (depth > 64) _fail(path, 'maximum JSON nesting depth is 64')
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) _fail(path, 'numbers must be finite JSON numbers')
    return value
  }
  if (Array.isArray(value)) return value.map((item, index) => _json(item, `${path}[${index}]`, depth + 1))
  if (_isRecord(value)) {
    const result: Record<string, JsonValue> = {}
    for (const [key, item] of Object.entries(value)) result[key] = _json(item, `${path}.${key}`, depth + 1)
    return result
  }
  _fail(path, 'value is not JSON-safe')
}

/** Return whether a value is a finite JSON scalar. */
export function isJsonSafeScalar(value: unknown): value is JsonSafeScalar {
  return value === null || typeof value === 'string' || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))
}

/**
 * Convert driver-native bigint/date values at the explicit JSON boundary.
 * Decimal drivers must provide exact decimal text as a string; Numbers are
 * never used for decimal transport because they lose precision.
 *
 * @param value - Driver scalar.
 * @returns JSON-safe scalar.
 */
export function encodeDatabaseScalar(value: unknown): JsonSafeScalar {
  if (isJsonSafeScalar(value)) return value
  if (typeof value === 'bigint') return value.toString(10)
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) _fail('value', 'invalid Date cannot cross the JSON boundary')
    return value.toISOString()
  }
  _fail('value', 'decimal values must be exact strings; unsupported scalar is not JSON-safe')
}

/** @internal */
export function _strings(value: unknown, path: string, minItems = 0, maxItems?: number): string[] {
  if (!Array.isArray(value)) _fail(path, 'expected an array')
  if (value.length < minItems) _fail(path, `must contain at least ${minItems} item(s)`)
  if (maxItems !== undefined && value.length > maxItems) _fail(path, `must contain at most ${maxItems} item(s)`)
  return value.map((item, index) => _string(item, `${path}[${index}]`, 1))
}
