import type { JsonSchema, SafeParseResult } from './json.js'
import { ContractValidationError } from './json.js'

/** Runtime parser plus its machine-readable JSON Schema document. */
export interface ContractSchema<T> {
  /** Draft-2020-12 JSON Schema document. */
  readonly jsonSchema: JsonSchema
  /** Parse and validate unknown input. */
  parse(value: unknown): T
  /** Parse without throwing on a contract failure. */
  safeParse(value: unknown): SafeParseResult<T>
  /** Type guard backed by the same parser. */
  check(value: unknown): value is T
}

/** @internal */
export function _schema<T>(jsonSchema: JsonSchema, parser: (value: unknown) => T): ContractSchema<T> {
  return {
    jsonSchema,
    parse: parser,
    safeParse(value: unknown): SafeParseResult<T> {
      try {
        return { success: true, data: parser(value) }
      } catch (error) {
        if (error instanceof ContractValidationError) return { success: false, error }
        throw error
      }
    },
    check(value: unknown): value is T {
      try {
        parser(value)
        return true
      } catch (error) {
        if (error instanceof ContractValidationError) return false
        throw error
      }
    },
  }
}
