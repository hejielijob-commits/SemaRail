import {
  _boolean,
  _enum,
  _fail,
  _json,
  _keys,
  _optional,
  _record,
  _required,
  _string,
  _strings,
  _version,
  SCHEMA_VERSION,
  type JsonObject,
  type JsonSchema,
} from './json.js'
import { _schema, type ContractSchema } from './schema.js'

/** Semantic role used by context and result columns. */
export type SemanticRole = 'dimension' | 'measure'

/** A column exposed by Wren's semantic context. */
export interface SemanticColumn {
  readonly name: string
  readonly type: string
  readonly description?: string
  readonly isCalculated?: boolean
  readonly notNull?: boolean
  readonly isPrimaryKey?: boolean
  readonly semanticRole?: SemanticRole
  readonly expression?: string
  readonly properties?: JsonObject
}

/** A semantic model and its fields. */
export interface SemanticModel {
  readonly name: string
  readonly description?: string
  readonly table?: string
  readonly columns: readonly SemanticColumn[]
  readonly primaryKey?: string
  readonly properties?: JsonObject
}

/** A Wren relationship between two semantic models. */
export interface SemanticRelationship {
  readonly name: string
  readonly models: readonly string[]
  readonly joinType: string
  readonly condition: string
  readonly description?: string
}

/** A reusable metric/measure. */
export interface SemanticMetric {
  readonly name: string
  readonly expression: string
  readonly type: string
  readonly model?: string
  readonly description?: string
}

/** A semantic view exposed by Wren. */
export interface SemanticView {
  readonly name: string
  readonly statement: string
  readonly description?: string
}

/** Business context returned by `context.ask`. */
export interface SemanticContext {
  readonly schemaVersion: typeof SCHEMA_VERSION
  readonly projectRevision: string
  readonly models: readonly SemanticModel[]
  readonly relationships: readonly SemanticRelationship[]
  readonly metrics?: readonly SemanticMetric[]
  readonly views?: readonly SemanticView[]
  readonly summary?: string
  readonly knowledge?: readonly string[]
  readonly defaultGrain?: string
  readonly defaultFilters?: readonly string[]
}

/** Input to `context.ask`. */
export interface SemanticContextInput {
  readonly question: string
}

function optionalString(object: Record<string, unknown>, key: string, path: string, maxLength = 4_000): string | undefined {
  const value = _optional(object, key)
  return value === undefined ? undefined : _string(value, `${path}.${key}`, 1, maxLength)
}

function parseColumn(value: unknown, path: string): SemanticColumn {
  const object = _record(value, path)
  _keys(object, ['name', 'type', 'description', 'isCalculated', 'notNull', 'isPrimaryKey', 'semanticRole', 'expression', 'properties'], path)
  const description = optionalString(object, 'description', path)
  const expression = optionalString(object, 'expression', path, 16_000)
  const properties = _optional(object, 'properties')
  return {
    name: _string(_required(object, 'name', path), `${path}.name`, 1, 256),
    type: _string(_required(object, 'type', path), `${path}.type`, 1, 128),
    ...(description === undefined ? {} : { description }),
    ...(_optional(object, 'isCalculated') === undefined ? {} : { isCalculated: _boolean(_optional(object, 'isCalculated'), `${path}.isCalculated`) }),
    ...(_optional(object, 'notNull') === undefined ? {} : { notNull: _boolean(_optional(object, 'notNull'), `${path}.notNull`) }),
    ...(_optional(object, 'isPrimaryKey') === undefined ? {} : { isPrimaryKey: _boolean(_optional(object, 'isPrimaryKey'), `${path}.isPrimaryKey`) }),
    ...(_optional(object, 'semanticRole') === undefined ? {} : { semanticRole: _enum(_optional(object, 'semanticRole'), ['dimension', 'measure'] as const, `${path}.semanticRole`) }),
    ...(expression === undefined ? {} : { expression }),
    ...(properties === undefined ? {} : { properties: _record(_json(properties, `${path}.properties`), `${path}.properties`) as JsonObject }),
  }
}

function parseModel(value: unknown, path: string): SemanticModel {
  const object = _record(value, path)
  _keys(object, ['name', 'description', 'table', 'columns', 'primaryKey', 'properties'], path)
  const columns = _required(object, 'columns', path)
  if (!Array.isArray(columns)) _fail(`${path}.columns`, 'expected an array')
  const description = optionalString(object, 'description', path)
  const table = optionalString(object, 'table', path, 256)
  const primaryKey = optionalString(object, 'primaryKey', path, 256)
  const properties = _optional(object, 'properties')
  return {
    name: _string(_required(object, 'name', path), `${path}.name`, 1, 256),
    ...(description === undefined ? {} : { description }),
    ...(table === undefined ? {} : { table }),
    columns: columns.map((column, index) => parseColumn(column, `${path}.columns[${index}]`)),
    ...(primaryKey === undefined ? {} : { primaryKey }),
    ...(properties === undefined ? {} : { properties: _record(_json(properties, `${path}.properties`), `${path}.properties`) as JsonObject }),
  }
}

function parseRelationship(value: unknown, path: string): SemanticRelationship {
  const object = _record(value, path)
  _keys(object, ['name', 'models', 'joinType', 'condition', 'description'], path)
  const description = optionalString(object, 'description', path)
  return {
    name: _string(_required(object, 'name', path), `${path}.name`, 1, 256),
    models: _strings(_required(object, 'models', path), `${path}.models`, 2, 2),
    joinType: _string(_required(object, 'joinType', path), `${path}.joinType`, 1, 64),
    condition: _string(_required(object, 'condition', path), `${path}.condition`, 1, 16_000),
    ...(description === undefined ? {} : { description }),
  }
}

function parseMetric(value: unknown, path: string): SemanticMetric {
  const object = _record(value, path)
  _keys(object, ['name', 'expression', 'type', 'model', 'description'], path)
  const model = optionalString(object, 'model', path, 256)
  const description = optionalString(object, 'description', path)
  return {
    name: _string(_required(object, 'name', path), `${path}.name`, 1, 256),
    expression: _string(_required(object, 'expression', path), `${path}.expression`, 1, 16_000),
    type: _string(_required(object, 'type', path), `${path}.type`, 1, 128),
    ...(model === undefined ? {} : { model }),
    ...(description === undefined ? {} : { description }),
  }
}

function parseView(value: unknown, path: string): SemanticView {
  const object = _record(value, path)
  _keys(object, ['name', 'statement', 'description'], path)
  const description = optionalString(object, 'description', path)
  return {
    name: _string(_required(object, 'name', path), `${path}.name`, 1, 256),
    statement: _string(_required(object, 'statement', path), `${path}.statement`, 1, 64_000),
    ...(description === undefined ? {} : { description }),
  }
}

/** Parse input for `context.ask`. */
export function parseSemanticContextInput(value: unknown): SemanticContextInput {
  const object = _record(value, 'contextInput')
  _keys(object, ['question'], 'contextInput')
  return { question: _string(_required(object, 'question', 'contextInput'), 'contextInput.question', 1, 16_000) }
}

/** Parse semantic context and fail closed on unknown schema versions. */
export function parseSemanticContext(value: unknown): SemanticContext {
  const object = _record(value, 'semanticContext')
  _keys(object, ['schemaVersion', 'projectRevision', 'models', 'relationships', 'metrics', 'views', 'summary', 'knowledge', 'defaultGrain', 'defaultFilters'], 'semanticContext')
  _version(_required(object, 'schemaVersion', 'semanticContext'), SCHEMA_VERSION, 'semanticContext.schemaVersion')
  const models = _required(object, 'models', 'semanticContext')
  const relationships = _required(object, 'relationships', 'semanticContext')
  if (!Array.isArray(models)) _fail('semanticContext.models', 'expected an array')
  if (!Array.isArray(relationships)) _fail('semanticContext.relationships', 'expected an array')
  const metrics = _optional(object, 'metrics')
  const views = _optional(object, 'views')
  const knowledge = _optional(object, 'knowledge')
  const defaultFilters = _optional(object, 'defaultFilters')
  const summary = optionalString(object, 'summary', 'semanticContext', 16_000)
  const defaultGrain = optionalString(object, 'defaultGrain', 'semanticContext', 128)
  return {
    schemaVersion: SCHEMA_VERSION,
    projectRevision: _string(_required(object, 'projectRevision', 'semanticContext'), 'semanticContext.projectRevision', 1, 256),
    models: models.map((model, index) => parseModel(model, `semanticContext.models[${index}]`)),
    relationships: relationships.map((relationship, index) => parseRelationship(relationship, `semanticContext.relationships[${index}]`)),
    ...(metrics === undefined ? {} : (() => {
      if (!Array.isArray(metrics)) _fail('semanticContext.metrics', 'expected an array')
      return { metrics: metrics.map((metric, index) => parseMetric(metric, `semanticContext.metrics[${index}]`)) }
    })()),
    ...(views === undefined ? {} : (() => {
      if (!Array.isArray(views)) _fail('semanticContext.views', 'expected an array')
      return { views: views.map((view, index) => parseView(view, `semanticContext.views[${index}]`)) }
    })()),
    ...(summary === undefined ? {} : { summary }),
    ...(knowledge === undefined ? {} : { knowledge: _strings(knowledge, 'semanticContext.knowledge') }),
    ...(defaultGrain === undefined ? {} : { defaultGrain }),
    ...(defaultFilters === undefined ? {} : { defaultFilters: _strings(defaultFilters, 'semanticContext.defaultFilters') }),
  }
}

/** JSON Schema for SemanticContext. */
export const SEMANTIC_CONTEXT_JSON_SCHEMA: JsonSchema = {
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  additionalProperties: false,
  required: ['schemaVersion', 'projectRevision', 'models', 'relationships'],
  properties: {
    schemaVersion: { const: SCHEMA_VERSION },
    projectRevision: { type: 'string', minLength: 1, maxLength: 256 },
    models: { type: 'array' },
    relationships: { type: 'array' },
    metrics: { type: 'array' },
    views: { type: 'array' },
    summary: { type: 'string', minLength: 1 },
    knowledge: { type: 'array', items: { type: 'string', minLength: 1 } },
    defaultGrain: { type: 'string', minLength: 1 },
    defaultFilters: { type: 'array', items: { type: 'string', minLength: 1 } },
  },
}

/** Semantic context parser/schema pair. */
export const semanticContextSchema: ContractSchema<SemanticContext> = _schema(SEMANTIC_CONTEXT_JSON_SCHEMA, parseSemanticContext)

/** Pascal-case schema alias. */
export const SemanticContextSchema = semanticContextSchema

/** Type guard for semantic contexts. */
export const isSemanticContext = (value: unknown): value is SemanticContext => semanticContextSchema.check(value)
