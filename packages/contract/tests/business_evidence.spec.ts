import { describe, expect, it } from 'vitest'
import { businessEvidenceV1Schema, parseBusinessEvidenceV1, parseRpcRequest } from '../src/index.js'

const evidence = {
  schemaVersion: 1, rulesVersion: 'business-trust-v1', projectRevision: 'sha256:revision', queryId: 'q-1', datasourceId: 'hr',
  outcome: 'success', createdAt: '2026-10-12T00:00:00Z', expiresAt: '2026-11-11T00:00:00Z',
  references: [{ ruleId: 'rule_12345678', model: 'employees', column: 'employee_id' }], models: ['employees'], columns: [], metrics: [], grain: [],
  unit: { status: 'unverified', value: null }, time: { source: 'unknown', field: null, value: null, groupedByOriginal: null },
  defaultsApplied: {}, conditions: {}, confirmations: {}, checks: [{ id: 'headcount', ruleId: 'rule_12345678', mandatory: true, status: 'passed', reasonCode: 'metric_matches' }],
}

describe('independent business evidence contract', () => {
  it('accepts undefined units and times without inventing a value', () => {
    expect(parseBusinessEvidenceV1(evidence).unit.value).toBeNull()
    expect(parseBusinessEvidenceV1(evidence).time.source).toBe('unknown')
    expect(parseRpcRequest({ protocolVersion: '3', id: 'read', method: 'query.evidence', params: { queryId: 'q-1' } }).method).toBe('query.evidence')
  })
  it('rejects unknown versions and caller-supplied query bodies', () => {
    expect(businessEvidenceV1Schema.safeParse({ ...evidence, schemaVersion: 2 }).success).toBe(false)
    expect(businessEvidenceV1Schema.safeParse({ ...evidence, previewRows: [] }).success).toBe(false)
    expect(businessEvidenceV1Schema.safeParse({ ...evidence, checks: [{ ...evidence.checks[0], status: 'verified' }] }).success).toBe(false)
    expect(businessEvidenceV1Schema.safeParse({ ...evidence, time: { ...evidence.time, rawSql: 'SELECT 1' } }).success).toBe(false)
  })
  it('keeps confirmation distinct from SQL check status', () => {
    const value = parseBusinessEvidenceV1({ ...evidence, confirmations: { snapshot: 'confirmed' }, checks: [{ ...evidence.checks[0], status: 'conflict' }] })
    expect(value.confirmations.snapshot).toBe('confirmed')
    expect(value.checks[0]?.status).toBe('conflict')
  })
})
