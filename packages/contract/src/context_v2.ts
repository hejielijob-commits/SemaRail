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
  type JsonObject,
  type JsonSchema,
} from './json.js'
import { _schema, type ContractSchema } from './schema.js'
import {
  parseSemanticContext,
  type SemanticModel,
  type SemanticRelationship,
  type SemanticView,
} from './context.js'

/** Version of the partitioned semantic context API. */
export const SEMANTIC_CONTEXT_V2_VERSION = 2 as const

/** Stable section names returned by Context API v2. */
export type SemanticContextSection = 'schema' | 'relationships' | 'metrics' | 'rules' | 'sqlExamples' | 'views'

/** Bounded request/response limits for one context lookup. */
export interface SemanticContextBudgets {
  readonly topK?: Partial<Record<SemanticContextSection, number>>
  readonly maxBytes?: number
  readonly maxTokens?: number
  readonly maxRelationshipDepth?: number
}

/** A structured business rule that can be authorization-projected. */
export interface SemanticRule {
  readonly id: string
  readonly text: string
  readonly referencedModels: readonly string[]
  readonly referencedColumns: readonly string[]
  readonly sourcePath?: string
  readonly ruleType?: string
  readonly priority?: number
  readonly mandatory?: boolean
  readonly effectiveFrom?: string
  readonly allowedRoles?: readonly string[]
}

/** A reviewed NL-to-SQL example with explicit semantic bindings. */
export interface SemanticSqlExample {
  readonly id: string
  readonly question: string
  readonly sql: string
  readonly referencedModels: readonly string[]
  readonly referencedColumns: readonly string[]
  readonly sourcePath?: string
  readonly language?: string
  readonly tags?: readonly string[]
  readonly reviewed?: boolean
  readonly dataSource?: string
  readonly roles?: readonly string[]
  readonly version?: string
}

/** Recalled cube member shape with explicit authorization bindings. */
export interface SemanticMetricV2 {
  readonly name: string
  readonly kind: 'cube' | 'measure' | 'dimension' | 'timeDimension'
  readonly expression?: string
  readonly type?: string
  readonly model?: string
  readonly cube?: string
  readonly baseObject?: string
  readonly description?: string
  readonly properties?: JsonObject
  readonly referencedModels?: readonly string[]
  readonly referencedColumns?: readonly string[]
}

/** View shape with explicit authorization bindings for Context API v2. */
export interface SemanticViewV2 extends SemanticView {
  readonly referencedModels?: readonly string[]
  readonly referencedColumns?: readonly string[]
}

/** Semantic-only model projection; physical table names are deliberately absent. */
export type SemanticModelV2 = Omit<SemanticModel, 'table'>

/** Index readiness information safe to expose outside the sidecar. */
export interface SemanticIndexStatus {
  readonly status: 'ready' | 'missing' | 'stale' | 'building' | 'unavailable' | 'degraded'
  readonly activeRevision?: string
  readonly indexedRevision?: string
  readonly documentCount?: number
  readonly backend?: 'vector' | 'lexical' | 'hybrid' | 'none'
  readonly staleReason?: 'missing' | 'revisionMismatch' | 'backendUnavailable' | 'buildFailed' | 'unknown'
  readonly embeddingModelId?: string
  readonly embeddingModelVersion?: string
  readonly embeddingDimension?: number
  readonly indexBuildVersion?: number
  readonly lastBuildAt?: string
  readonly buildDurationMs?: number
}

/** Non-sensitive explanation for one selected or filtered retrieval result. */
export interface SemanticRetrievalTrace {
  /** Stable ID of the visible semantic document selected for context. */
  readonly documentId?: string
  readonly source: SemanticContextSection
  readonly retrievalType: 'exact' | 'lexical' | 'vector' | 'graph' | 'ruleBinding' | 'fallback'
  /** Normalized relevance, never a raw vector distance. */
  readonly relevance?: number
  readonly reasonCode: 'exactMatch' | 'lexicalMatch' | 'vectorMatch' | 'graphExpansion' | 'ruleBinding' | 'fallback' | 'permissionFiltered' | 'budgetLimited'
  readonly projectRevision: string
  readonly authorizationFiltered: boolean
  readonly selected?: boolean
}

/** Aggregate, text-free retrieval metrics intended for diagnostics. */
export interface SemanticRetrievalSummary {
  readonly candidateCount: number
  readonly filteredCount: number
  readonly selectedCount: number
  readonly latencyMs: number
  readonly fallbackReason?: 'embeddingUnavailable' | 'vectorSearchFailed' | 'indexDegraded'
}

/** Versioned input for `context.ask` API v2. */
export interface SemanticContextV2Input {
  readonly contextVersion: typeof SEMANTIC_CONTEXT_V2_VERSION
  readonly question: string
  readonly budgets?: SemanticContextBudgets
}

/** Partitioned semantic context returned by `context.ask` API v2. */
export interface SemanticContextV2 {
  readonly schemaVersion: typeof SEMANTIC_CONTEXT_V2_VERSION
  readonly projectRevision: string
  readonly schema: { readonly models: readonly SemanticModelV2[] }
  readonly relationships: readonly SemanticRelationship[]
  readonly metrics: readonly SemanticMetricV2[]
  readonly rules: readonly SemanticRule[]
  readonly sqlExamples: readonly SemanticSqlExample[]
  readonly views: readonly SemanticViewV2[]
  readonly budgets: SemanticContextBudgets
  readonly indexStatus: SemanticIndexStatus
  readonly retrievalSummary: SemanticRetrievalSummary
  readonly retrievalTrace: readonly SemanticRetrievalTrace[]
}

function parseBudgets(value: unknown, path: string): SemanticContextBudgets {
  const object = _record(value, path)
  _keys(object, ['topK', 'maxBytes', 'maxTokens', 'maxRelationshipDepth'], path)
  const rawTopK = _optional(object, 'topK')
  let topK: Partial<Record<SemanticContextSection, number>> | undefined
  if (rawTopK !== undefined) {
    const top = _record(rawTopK, `${path}.topK`)
    _keys(top, ['schema', 'relationships', 'metrics', 'rules', 'sqlExamples', 'views'], `${path}.topK`)
    topK = {}
    for (const section of ['schema', 'relationships', 'metrics', 'rules', 'sqlExamples', 'views'] as const) {
      const raw = _optional(top, section)
      if (raw !== undefined) topK[section] = _integer(raw, `${path}.topK.${section}`, 1_000)
    }
  }
  const maxBytes = _optional(object, 'maxBytes')
  const maxTokens = _optional(object, 'maxTokens')
  const maxRelationshipDepth = _optional(object, 'maxRelationshipDepth')
  return {
    ...(topK === undefined ? {} : { topK }),
    ...(maxBytes === undefined ? {} : { maxBytes: _integer(maxBytes, `${path}.maxBytes`, 4 * 1024 * 1024) }),
    ...(maxTokens === undefined ? {} : { maxTokens: _integer(maxTokens, `${path}.maxTokens`, 256_000) }),
    ...(maxRelationshipDepth === undefined ? {} : { maxRelationshipDepth: _integer(maxRelationshipDepth, `${path}.maxRelationshipDepth`, 8) }),
  }
}

function parseBoundedStrings(value: unknown, path: string, maxItems = 64): string[] {
  if (!Array.isArray(value)) _fail(path, 'expected an array')
  if (value.length > maxItems) _fail(path, `must contain at most ${maxItems} item(s)`)
  return value.map((item, index) => _string(item, `${path}[${index}]`, 1, 512))
}

function parseRule(value: unknown, path: string): SemanticRule {
  const object = _record(value, path)
  _keys(object, ['id', 'text', 'referencedModels', 'referencedColumns', 'sourcePath', 'ruleType', 'priority', 'mandatory', 'effectiveFrom', 'allowedRoles'], path)
  const sourcePath = _optional(object, 'sourcePath')
  const ruleType = _optional(object, 'ruleType')
  const priority = _optional(object, 'priority')
  const mandatory = _optional(object, 'mandatory')
  const effectiveFrom = _optional(object, 'effectiveFrom')
  const allowedRoles = _optional(object, 'allowedRoles')
  return {
    id: _string(_required(object, 'id', path), `${path}.id`, 1, 256),
    text: _string(_required(object, 'text', path), `${path}.text`, 1, 16_000),
    referencedModels: parseBoundedStrings(_required(object, 'referencedModels', path), `${path}.referencedModels`),
    referencedColumns: parseBoundedStrings(_required(object, 'referencedColumns', path), `${path}.referencedColumns`),
    ...(sourcePath === undefined ? {} : { sourcePath: _string(sourcePath, `${path}.sourcePath`, 1, 512) }),
    ...(ruleType === undefined ? {} : { ruleType: _string(ruleType, `${path}.ruleType`, 1, 128) }),
    ...(priority === undefined ? {} : { priority: _integer(priority, `${path}.priority`, 1_000) }),
    ...(mandatory === undefined ? {} : { mandatory: _boolean(mandatory, `${path}.mandatory`) }),
    ...(effectiveFrom === undefined ? {} : { effectiveFrom: _string(effectiveFrom, `${path}.effectiveFrom`, 1, 128) }),
    ...(allowedRoles === undefined ? {} : { allowedRoles: parseBoundedStrings(allowedRoles, `${path}.allowedRoles`, 64) }),
  }
}

function parseSqlExample(value: unknown, path: string): SemanticSqlExample {
  const object = _record(value, path)
  _keys(object, ['id', 'question', 'sql', 'referencedModels', 'referencedColumns', 'sourcePath', 'language', 'tags', 'reviewed', 'dataSource', 'roles', 'version'], path)
  const sourcePath = _optional(object, 'sourcePath')
  const language = _optional(object, 'language')
  const tags = _optional(object, 'tags')
  const reviewed = _optional(object, 'reviewed')
  const dataSource = _optional(object, 'dataSource')
  const roles = _optional(object, 'roles')
  const version = _optional(object, 'version')
  return {
    id: _string(_required(object, 'id', path), `${path}.id`, 1, 256),
    question: _string(_required(object, 'question', path), `${path}.question`, 1, 16_000),
    sql: _string(_required(object, 'sql', path), `${path}.sql`, 1, 64_000),
    referencedModels: parseBoundedStrings(_required(object, 'referencedModels', path), `${path}.referencedModels`),
    referencedColumns: parseBoundedStrings(_required(object, 'referencedColumns', path), `${path}.referencedColumns`),
    ...(sourcePath === undefined ? {} : { sourcePath: _string(sourcePath, `${path}.sourcePath`, 1, 512) }),
    ...(language === undefined ? {} : { language: _string(language, `${path}.language`, 1, 32) }),
    ...(tags === undefined ? {} : { tags: parseBoundedStrings(tags, `${path}.tags`, 32) }),
    ...(reviewed === undefined ? {} : { reviewed: _boolean(reviewed, `${path}.reviewed`) }),
    ...(dataSource === undefined ? {} : { dataSource: _string(dataSource, `${path}.dataSource`, 1, 256) }),
    ...(roles === undefined ? {} : { roles: parseBoundedStrings(roles, `${path}.roles`, 64) }),
    ...(version === undefined ? {} : { version: _string(version, `${path}.version`, 1, 128) }),
  }
}

function parseMetricV2(value: unknown, path: string): SemanticMetricV2 {
  const object = _record(value, path)
  _keys(object, ['name', 'kind', 'expression', 'type', 'model', 'cube', 'baseObject', 'description', 'properties', 'referencedModels', 'referencedColumns'], path)
  const rawKind = _optional(object, 'kind')
  const expression = _optional(object, 'expression')
  const type = _optional(object, 'type')
  const model = _optional(object, 'model')
  const cube = _optional(object, 'cube')
  const baseObject = _optional(object, 'baseObject')
  const description = _optional(object, 'description')
  const properties = _optional(object, 'properties')
  const rawModels = _optional(object, 'referencedModels')
  const rawColumns = _optional(object, 'referencedColumns')
  return {
    name: _string(_required(object, 'name', path), `${path}.name`, 1, 256),
    kind: rawKind === undefined ? 'measure' : _enum(rawKind, ['cube', 'measure', 'dimension', 'timeDimension'] as const, `${path}.kind`),
    ...(expression === undefined ? {} : { expression: _string(expression, `${path}.expression`, 1, 16_000) }),
    ...(type === undefined ? {} : { type: _string(type, `${path}.type`, 1, 128) }),
    ...(model === undefined ? {} : { model: _string(model, `${path}.model`, 1, 256) }),
    ...(cube === undefined ? {} : { cube: _string(cube, `${path}.cube`, 1, 256) }),
    ...(baseObject === undefined ? {} : { baseObject: _string(baseObject, `${path}.baseObject`, 1, 256) }),
    ...(description === undefined ? {} : { description: _string(description, `${path}.description`, 1, 4_000) }),
    ...(properties === undefined ? {} : { properties: _record(_json(properties, `${path}.properties`), `${path}.properties`) as JsonObject }),
    ...(rawModels === undefined ? {} : { referencedModels: parseBoundedStrings(rawModels, `${path}.referencedModels`) }),
    ...(rawColumns === undefined ? {} : { referencedColumns: parseBoundedStrings(rawColumns, `${path}.referencedColumns`) }),
  }
}

function parseViewV2(value: unknown, path: string): SemanticViewV2 {
  const object = _record(value, path)
  _keys(object, ['name', 'statement', 'description', 'referencedModels', 'referencedColumns'], path)
  const baseObject = { ...object }
  delete baseObject.referencedModels
  delete baseObject.referencedColumns
  const base = parseSemanticContext({
    schemaVersion: 1,
    projectRevision: 'v2',
    models: [],
    relationships: [],
    views: [baseObject],
  }).views?.[0]
  if (!base) _fail(path, 'invalid view')
  const rawModels = _optional(object, 'referencedModels')
  const rawColumns = _optional(object, 'referencedColumns')
  return {
    ...base,
    ...(rawModels === undefined ? {} : { referencedModels: parseBoundedStrings(rawModels, `${path}.referencedModels`) }),
    ...(rawColumns === undefined ? {} : { referencedColumns: parseBoundedStrings(rawColumns, `${path}.referencedColumns`) }),
  }
}

function parseIndexStatus(value: unknown, path: string): SemanticIndexStatus {
  const object = _record(value, path)
  _keys(object, ['status', 'activeRevision', 'indexedRevision', 'documentCount', 'backend', 'staleReason', 'embeddingModelId', 'embeddingModelVersion', 'embeddingDimension', 'indexBuildVersion', 'lastBuildAt', 'buildDurationMs'], path)
  const activeRevision = _optional(object, 'activeRevision')
  const indexedRevision = _optional(object, 'indexedRevision')
  const documentCount = _optional(object, 'documentCount')
  const backend = _optional(object, 'backend')
  const staleReason = _optional(object, 'staleReason')
  const embeddingModelId = _optional(object, 'embeddingModelId')
  const embeddingModelVersion = _optional(object, 'embeddingModelVersion')
  const embeddingDimension = _optional(object, 'embeddingDimension')
  const indexBuildVersion = _optional(object, 'indexBuildVersion')
  const lastBuildAt = _optional(object, 'lastBuildAt')
  const buildDurationMs = _optional(object, 'buildDurationMs')
  if (buildDurationMs !== undefined && (typeof buildDurationMs !== 'number' || !Number.isFinite(buildDurationMs) || buildDurationMs < 0 || buildDurationMs > 86_400_000)) _fail(`${path}.buildDurationMs`, 'expected a bounded non-negative number')
  return {
    status: _enum(_required(object, 'status', path), ['ready', 'missing', 'stale', 'building', 'unavailable', 'degraded'] as const, `${path}.status`),
    ...(activeRevision === undefined ? {} : { activeRevision: _string(activeRevision, `${path}.activeRevision`, 1, 256) }),
    ...(indexedRevision === undefined ? {} : { indexedRevision: _string(indexedRevision, `${path}.indexedRevision`, 1, 256) }),
    ...(documentCount === undefined ? {} : { documentCount: _integer(documentCount, `${path}.documentCount`, 10_000_000) }),
    ...(backend === undefined ? {} : { backend: _enum(backend, ['vector', 'lexical', 'hybrid', 'none'] as const, `${path}.backend`) }),
    ...(staleReason === undefined ? {} : { staleReason: _enum(staleReason, ['missing', 'revisionMismatch', 'backendUnavailable', 'buildFailed', 'unknown'] as const, `${path}.staleReason`) }),
    ...(embeddingModelId === undefined ? {} : { embeddingModelId: _string(embeddingModelId, `${path}.embeddingModelId`, 1, 256) }),
    ...(embeddingModelVersion === undefined ? {} : { embeddingModelVersion: _string(embeddingModelVersion, `${path}.embeddingModelVersion`, 1, 256) }),
    ...(embeddingDimension === undefined ? {} : { embeddingDimension: _integer(embeddingDimension, `${path}.embeddingDimension`, 1_000_000) }),
    ...(indexBuildVersion === undefined ? {} : { indexBuildVersion: _integer(indexBuildVersion, `${path}.indexBuildVersion`, 1_000_000) }),
    ...(lastBuildAt === undefined ? {} : { lastBuildAt: _string(lastBuildAt, `${path}.lastBuildAt`, 1, 64) }),
    ...(buildDurationMs === undefined ? {} : { buildDurationMs: buildDurationMs as number }),
  }
}

function parseRetrievalTrace(value: unknown, path: string): SemanticRetrievalTrace {
  const object = _record(value, path)
  _keys(object, ['documentId', 'source', 'retrievalType', 'relevance', 'reasonCode', 'projectRevision', 'authorizationFiltered', 'selected'], path)
  const documentId = _optional(object, 'documentId')
  const relevance = _optional(object, 'relevance')
  const selected = _optional(object, 'selected')
  const normalizedRelevance = relevance === undefined ? undefined : relevance
  if (normalizedRelevance !== undefined && (typeof normalizedRelevance !== 'number' || !Number.isFinite(normalizedRelevance) || normalizedRelevance < 0 || normalizedRelevance > 1)) {
    _fail(`${path}.relevance`, 'expected a finite number between 0 and 1')
  }
  return {
    ...(documentId === undefined ? {} : { documentId: _string(documentId, `${path}.documentId`, 1, 512) }),
    source: _enum(_required(object, 'source', path), ['schema', 'relationships', 'metrics', 'rules', 'sqlExamples', 'views'] as const, `${path}.source`),
    retrievalType: _enum(_required(object, 'retrievalType', path), ['exact', 'lexical', 'vector', 'graph', 'ruleBinding', 'fallback'] as const, `${path}.retrievalType`),
    ...(normalizedRelevance === undefined ? {} : { relevance: normalizedRelevance as number }),
    reasonCode: _enum(_required(object, 'reasonCode', path), ['exactMatch', 'lexicalMatch', 'vectorMatch', 'graphExpansion', 'ruleBinding', 'fallback', 'permissionFiltered', 'budgetLimited'] as const, `${path}.reasonCode`),
    projectRevision: _string(_required(object, 'projectRevision', path), `${path}.projectRevision`, 1, 256),
    authorizationFiltered: _boolean(_required(object, 'authorizationFiltered', path), `${path}.authorizationFiltered`),
    ...(selected === undefined ? {} : { selected: _boolean(selected, `${path}.selected`) }),
  }
}

function parseRetrievalSummary(value: unknown, path: string): SemanticRetrievalSummary {
  const object = _record(value, path)
  _keys(object, ['candidateCount', 'filteredCount', 'selectedCount', 'latencyMs', 'fallbackReason'], path)
  const latencyMs = _required(object, 'latencyMs', path)
  if (typeof latencyMs !== 'number' || !Number.isFinite(latencyMs) || latencyMs < 0 || latencyMs > 86_400_000) {
    _fail(`${path}.latencyMs`, 'expected a bounded non-negative number')
  }
  const fallbackReason = _optional(object, 'fallbackReason')
  return {
    candidateCount: _integer(_required(object, 'candidateCount', path), `${path}.candidateCount`, 10_000_000),
    filteredCount: _integer(_required(object, 'filteredCount', path), `${path}.filteredCount`, 10_000_000),
    selectedCount: _integer(_required(object, 'selectedCount', path), `${path}.selectedCount`, 10_000_000),
    latencyMs,
    ...(fallbackReason === undefined ? {} : {
      fallbackReason: _enum(fallbackReason, ['embeddingUnavailable', 'vectorSearchFailed', 'indexDegraded'] as const, `${path}.fallbackReason`),
    }),
  }
}

/** Parse versioned input for Context API v2. */
export function parseSemanticContextV2Input(value: unknown): SemanticContextV2Input {
  const object = _record(value, 'contextInputV2')
  _keys(object, ['contextVersion', 'question', 'budgets'], 'contextInputV2')
  _version(_required(object, 'contextVersion', 'contextInputV2'), SEMANTIC_CONTEXT_V2_VERSION, 'contextInputV2.contextVersion')
  const budgets = _optional(object, 'budgets')
  return {
    contextVersion: SEMANTIC_CONTEXT_V2_VERSION,
    question: _string(_required(object, 'question', 'contextInputV2'), 'contextInputV2.question', 1, 16_000),
    ...(budgets === undefined ? {} : { budgets: parseBudgets(budgets, 'contextInputV2.budgets') }),
  }
}

/** Parse the partitioned, bounded Context API v2 response. */
export function parseSemanticContextV2(value: unknown): SemanticContextV2 {
  const object = _record(value, 'semanticContextV2')
  _keys(object, ['schemaVersion', 'projectRevision', 'schema', 'relationships', 'metrics', 'rules', 'sqlExamples', 'views', 'budgets', 'indexStatus', 'retrievalSummary', 'retrievalTrace'], 'semanticContextV2')
  _version(_required(object, 'schemaVersion', 'semanticContextV2'), SEMANTIC_CONTEXT_V2_VERSION, 'semanticContextV2.schemaVersion')
  const revision = _string(_required(object, 'projectRevision', 'semanticContextV2'), 'semanticContextV2.projectRevision', 1, 256)
  const schema = _record(_required(object, 'schema', 'semanticContextV2'), 'semanticContextV2.schema')
  _keys(schema, ['models'], 'semanticContextV2.schema')
  const models = _required(schema, 'models', 'semanticContextV2.schema')
  if (!Array.isArray(models)) _fail('semanticContextV2.schema.models', 'expected an array')
  for (const [index, model] of models.entries()) {
    const object = _record(model, `semanticContextV2.schema.models[${index}]`)
    if ('table' in object) _fail(`semanticContextV2.schema.models[${index}].table`, 'physical table names are not allowed in Context API v2')
  }
  const relationships = _required(object, 'relationships', 'semanticContextV2')
  const metrics = _required(object, 'metrics', 'semanticContextV2')
  const rules = _required(object, 'rules', 'semanticContextV2')
  const sqlExamples = _required(object, 'sqlExamples', 'semanticContextV2')
  const views = _required(object, 'views', 'semanticContextV2')
  const retrievalTrace = _required(object, 'retrievalTrace', 'semanticContextV2')
  if (!Array.isArray(relationships) || !Array.isArray(metrics) || !Array.isArray(rules) || !Array.isArray(sqlExamples) || !Array.isArray(views) || !Array.isArray(retrievalTrace)) _fail('semanticContextV2', 'sections must be arrays')
  // Reuse the v1 structural parsers for the stable schema/relationship/metric/view shapes.
  const parsed = parseSemanticContext({ schemaVersion: 1, projectRevision: revision, models, relationships })
  if (rules.length > 1_000 || sqlExamples.length > 1_000 || retrievalTrace.length > 2_000) _fail('semanticContextV2', 'section exceeds the permitted item count')
  return {
    schemaVersion: SEMANTIC_CONTEXT_V2_VERSION,
    projectRevision: revision,
    schema: { models: parsed.models as SemanticModelV2[] },
    relationships: parsed.relationships,
    metrics: metrics.map((metric, index) => parseMetricV2(metric, `semanticContextV2.metrics[${index}]`)),
    rules: rules.map((rule, index) => parseRule(rule, `semanticContextV2.rules[${index}]`)),
    sqlExamples: sqlExamples.map((example, index) => parseSqlExample(example, `semanticContextV2.sqlExamples[${index}]`)),
    views: views.map((view, index) => parseViewV2(view, `semanticContextV2.views[${index}]`)),
    budgets: parseBudgets(_required(object, 'budgets', 'semanticContextV2'), 'semanticContextV2.budgets'),
    indexStatus: parseIndexStatus(_required(object, 'indexStatus', 'semanticContextV2'), 'semanticContextV2.indexStatus'),
    retrievalSummary: parseRetrievalSummary(_required(object, 'retrievalSummary', 'semanticContextV2'), 'semanticContextV2.retrievalSummary'),
    retrievalTrace: retrievalTrace.map((trace, index) => parseRetrievalTrace(trace, `semanticContextV2.retrievalTrace[${index}]`)),
  }
}

/** JSON Schema for the Context API v2 response. */
export const SEMANTIC_CONTEXT_V2_JSON_SCHEMA: JsonSchema = {
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  additionalProperties: false,
  required: ['schemaVersion', 'projectRevision', 'schema', 'relationships', 'metrics', 'rules', 'sqlExamples', 'views', 'budgets', 'indexStatus', 'retrievalSummary', 'retrievalTrace'],
  properties: {
    schemaVersion: { const: SEMANTIC_CONTEXT_V2_VERSION },
    projectRevision: { type: 'string', minLength: 1, maxLength: 256 },
    schema: { type: 'object', additionalProperties: false, required: ['models'], properties: { models: { type: 'array' } } },
    relationships: { type: 'array' }, metrics: { type: 'array' }, rules: { type: 'array' }, sqlExamples: { type: 'array' }, views: { type: 'array' },
    budgets: { type: 'object', additionalProperties: false },
    indexStatus: { type: 'object', additionalProperties: false },
    retrievalSummary: { type: 'object', additionalProperties: false },
    retrievalTrace: { type: 'array' },
  },
}

/** Contract schema/parser pair for Context API v2. */
export const semanticContextV2Schema: ContractSchema<SemanticContextV2> = _schema(SEMANTIC_CONTEXT_V2_JSON_SCHEMA, parseSemanticContextV2)

/** Pascal-case schema alias. */
export const SemanticContextV2Schema = semanticContextV2Schema
