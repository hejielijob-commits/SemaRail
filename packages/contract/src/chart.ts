import { _enum, _fail, _keys, _optional, _record, _required, _strings, _string, _version, type JsonSchema } from './json.js'
import { _schema, type ContractSchema } from './schema.js'

/** Maximum chart title length. */
export const MAX_CHART_TITLE_LENGTH = 200 as const

/** Chart family accepted by the MVP Client adapter. */
export type ChartType = 'line' | 'bar' | 'pie'

/** Version-one chart recommendation consumed by the Client. */
export interface ChartSpecV1 {
  readonly version: 1
  readonly type: ChartType
  readonly title?: string
  readonly x: string
  readonly y: readonly string[]
  readonly series?: string
  readonly tooltip: true
}

/** Parse ChartSpecV1 and reject future versions. */
export function parseChartSpecV1(value: unknown): ChartSpecV1 {
  const object = _record(value, 'chart')
  _keys(object, ['version', 'type', 'title', 'x', 'y', 'series', 'tooltip'], 'chart')
  _version(_required(object, 'version', 'chart'), 1, 'chart.version')
  const title = _optional(object, 'title')
  const series = _optional(object, 'series')
  return {
    version: 1,
    type: _enum(_required(object, 'type', 'chart'), ['line', 'bar', 'pie'] as const, 'chart.type'),
    ...(title === undefined ? {} : { title: _string(title, 'chart.title', 1, MAX_CHART_TITLE_LENGTH) }),
    x: _string(_required(object, 'x', 'chart'), 'chart.x', 1, 256),
    y: _strings(_required(object, 'y', 'chart'), 'chart.y', 1, 8),
    ...(series === undefined ? {} : { series: _string(series, 'chart.series', 1, 256) }),
    tooltip: _required(object, 'tooltip', 'chart') === true ? true : _fail('chart.tooltip', 'expected true'),
  }
}

/** JSON Schema for ChartSpecV1. */
export const CHART_SPEC_V1_JSON_SCHEMA: JsonSchema = {
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  additionalProperties: false,
  required: ['version', 'type', 'x', 'y', 'tooltip'],
  properties: {
    version: { const: 1 },
    type: { enum: ['line', 'bar', 'pie'] },
    title: { type: 'string', minLength: 1, maxLength: MAX_CHART_TITLE_LENGTH },
    x: { type: 'string', minLength: 1, maxLength: 256 },
    y: { type: 'array', minItems: 1, maxItems: 8, items: { type: 'string', minLength: 1, maxLength: 256 } },
    series: { type: 'string', minLength: 1, maxLength: 256 },
    tooltip: { const: true },
  },
}

/** ChartSpecV1 parser/schema pair. */
export const chartSpecV1Schema: ContractSchema<ChartSpecV1> = _schema(CHART_SPEC_V1_JSON_SCHEMA, parseChartSpecV1)

/** Pascal-case schema alias. */
export const ChartSpecV1Schema = chartSpecV1Schema

/** Type guard for ChartSpecV1 values. */
export const isChartSpecV1 = (value: unknown): value is ChartSpecV1 => chartSpecV1Schema.check(value)
