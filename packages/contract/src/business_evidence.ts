import { _boolean, _enum, _fail, _json, _keys, _record, _required, _string, _version, type JsonObject, type JsonSchema, type JsonValue } from './json.js'
import { _schema } from './schema.js'

/** Core-owned business constraint status, distinct from user confirmation. */
export type BusinessCheckStatus = 'passed' | 'conflict' | 'unverified' | 'stale'

/** One published rule binding and its finite SQL verification outcome. */
export interface BusinessCheckV1 {
  readonly id: string
  readonly ruleId: string
  readonly mandatory: boolean
  readonly status: BusinessCheckStatus
  readonly reasonCode: string
}

/** Safe query evidence; contains no query/result bodies or authorization expressions. */
export interface BusinessEvidenceV1 {
  readonly schemaVersion: 1
  readonly rulesVersion: string
  readonly projectRevision: string
  readonly queryId: string
  readonly datasourceId: string
  readonly outcome: 'success' | 'failure' | 'blocked'
  readonly createdAt: string
  readonly expiresAt: string
  readonly references: readonly { readonly ruleId: string, readonly model: string, readonly column: string }[]
  readonly models: readonly JsonValue[]
  readonly columns: readonly JsonValue[]
  readonly metrics: readonly JsonValue[]
  readonly grain: readonly JsonValue[]
  readonly unit: { readonly status: 'verified' | 'unverified', readonly value?: string | null }
  readonly time: { readonly source: 'confirmed' | 'publishedDefault' | 'sql' | 'unknown', readonly field?: string | null, readonly value?: JsonValue, readonly groupedByOriginal?: boolean | null }
  readonly defaultsApplied: JsonObject
  readonly conditions: JsonObject
  readonly confirmations: Readonly<Record<string, 'confirmed' | 'defaulted' | 'missing'>>
  readonly checks: readonly BusinessCheckV1[]
}

const fields = ['schemaVersion', 'rulesVersion', 'projectRevision', 'queryId', 'datasourceId', 'outcome', 'createdAt', 'expiresAt', 'references', 'models', 'columns', 'metrics', 'grain', 'unit', 'time', 'defaultsApplied', 'conditions', 'confirmations', 'checks'] as const
const statuses = ['passed', 'conflict', 'unverified', 'stale'] as const
function list(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value) || value.length > 256) _fail(path, 'expected bounded array')
  return value
}
function text(value: unknown, path: string): string { return _string(value, path, 1, 256) }

/** Parse independent v1 evidence; unknown versions/fields fail closed. */
export function parseBusinessEvidenceV1(value: unknown): BusinessEvidenceV1 {
  const p = 'businessEvidence'
  const r = _record(value, p)
  _keys(r, fields, p)
  _version(_required(r, 'schemaVersion', p), 1, `${p}.schemaVersion`)
  for (const field of fields) _required(r, field, p)
  const u = _record(r.unit, `${p}.unit`)
  _keys(u, ['status', 'value'], `${p}.unit`)
  const t = _record(r.time, `${p}.time`)
  _keys(t, ['source', 'field', 'value', 'groupedByOriginal'], `${p}.time`)
  const confirmations = _record(r.confirmations, `${p}.confirmations`)
  const confirmationValues = Object.fromEntries(Object.entries(confirmations).map(([key, v]) => [key, _enum(v, ['confirmed', 'defaulted', 'missing'] as const, `${p}.confirmations.${key}`)]))
  const jsonObject = (v: unknown, path: string): JsonObject => _json(_record(v, path), path) as JsonObject
  return {
    schemaVersion: 1,
    rulesVersion: text(r.rulesVersion, `${p}.rulesVersion`),
    projectRevision: text(r.projectRevision, `${p}.projectRevision`),
    queryId: text(r.queryId, `${p}.queryId`), datasourceId: text(r.datasourceId, `${p}.datasourceId`),
    outcome: _enum(r.outcome, ['success', 'failure', 'blocked'] as const, `${p}.outcome`),
    createdAt: text(r.createdAt, `${p}.createdAt`), expiresAt: text(r.expiresAt, `${p}.expiresAt`),
    references: list(r.references, `${p}.references`).map((v, i) => {
      const path = `${p}.references[${i}]`, ref = _record(v, path)
      _keys(ref, ['ruleId', 'model', 'column'], path)
      return { ruleId: text(ref.ruleId, `${path}.ruleId`), model: text(ref.model, `${path}.model`), column: text(ref.column, `${path}.column`) }
    }),
    models: list(r.models, `${p}.models`).map(v => _json(v, `${p}.models`)),
    columns: list(r.columns, `${p}.columns`).map(v => _json(v, `${p}.columns`)),
    metrics: list(r.metrics, `${p}.metrics`).map(v => _json(v, `${p}.metrics`)),
    grain: list(r.grain, `${p}.grain`).map(v => _json(v, `${p}.grain`)),
    unit: { status: _enum(u.status, ['verified', 'unverified'] as const, `${p}.unit.status`), ...(u.value === undefined ? {} : { value: u.value === null ? null : text(u.value, `${p}.unit.value`) }) },
    time: { source: _enum(t.source, ['confirmed', 'publishedDefault', 'sql', 'unknown'] as const, `${p}.time.source`), ...(t.field === undefined ? {} : { field: t.field === null ? null : text(t.field, `${p}.time.field`) }), ...(t.value === undefined ? {} : { value: _json(t.value, `${p}.time.value`) }), ...(t.groupedByOriginal === undefined ? {} : { groupedByOriginal: t.groupedByOriginal === null ? null : _boolean(t.groupedByOriginal, `${p}.time.groupedByOriginal`) }) },
    defaultsApplied: jsonObject(r.defaultsApplied, `${p}.defaultsApplied`), conditions: jsonObject(r.conditions, `${p}.conditions`), confirmations: confirmationValues,
    checks: list(r.checks, `${p}.checks`).map((v, i) => {
      const path = `${p}.checks[${i}]`, c = _record(v, path)
      _keys(c, ['id', 'ruleId', 'mandatory', 'status', 'reasonCode'], path)
      return { id: text(c.id, `${path}.id`), ruleId: text(c.ruleId, `${path}.ruleId`), mandatory: _boolean(c.mandatory, `${path}.mandatory`), status: _enum(c.status, statuses, `${path}.status`), reasonCode: text(c.reasonCode, `${path}.reasonCode`) }
    }),
  }
}

/** Schema of the separate evidence RPC result. */
export const BUSINESS_EVIDENCE_V1_JSON_SCHEMA: JsonSchema = {
  type: 'object', additionalProperties: false, required: [...fields],
  properties: {
    schemaVersion: { type: 'integer', const: 1 },
    ...Object.fromEntries(['rulesVersion', 'projectRevision', 'queryId', 'datasourceId', 'createdAt', 'expiresAt'].map(field => [field, { type: 'string', minLength: 1, maxLength: 256 }])),
    outcome: { type: 'string', enum: ['success', 'failure', 'blocked'] },
    references: {
      type: 'array', maxItems: 256,
      items: { type: 'object', additionalProperties: false, required: ['ruleId', 'model', 'column'], properties: Object.fromEntries(['ruleId', 'model', 'column'].map(field => [field, { type: 'string', minLength: 1, maxLength: 256 }])) },
    },
    ...Object.fromEntries(['models', 'columns', 'metrics', 'grain'].map(field => [field, { type: 'array', maxItems: 256 }])),
    unit: { type: 'object', additionalProperties: false, required: ['status'], properties: { status: { type: 'string', enum: ['verified', 'unverified'] }, value: { type: ['string', 'null'], minLength: 1, maxLength: 256 } } },
    time: { type: 'object', additionalProperties: false, required: ['source'], properties: { source: { type: 'string', enum: ['confirmed', 'publishedDefault', 'sql', 'unknown'] }, field: { type: ['string', 'null'], minLength: 1, maxLength: 256 }, value: {}, groupedByOriginal: { type: ['boolean', 'null'] } } },
    defaultsApplied: { type: 'object' }, conditions: { type: 'object' },
    confirmations: { type: 'object', additionalProperties: { type: 'string', enum: ['confirmed', 'defaulted', 'missing'] } },
    checks: {
      type: 'array', maxItems: 256,
      items: { type: 'object', additionalProperties: false, required: ['id', 'ruleId', 'mandatory', 'status', 'reasonCode'], properties: { ...Object.fromEntries(['id', 'ruleId', 'reasonCode'].map(field => [field, { type: 'string', minLength: 1, maxLength: 256 }])), mandatory: { type: 'boolean' }, status: { type: 'string', enum: [...statuses] } } },
    },
  },
}
/** Runtime parser and JSON Schema for BusinessEvidenceV1. */
export const businessEvidenceV1Schema = _schema(BUSINESS_EVIDENCE_V1_JSON_SCHEMA, parseBusinessEvidenceV1)
