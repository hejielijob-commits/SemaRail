import type { JsonValue } from './json.js'

/** Test-only question and private grading inputs; never an Agent job payload. */
export interface EvaluationCase {
  id: string
  question: string
  role: string
  expectedBehavior: 'answer' | 'clarify' | 'deny' | 'unsupported'
  referenceSql?: string
  expectedResult?: { columns: string[]; rows: JsonValue[][] }
  assertions?: Record<string, JsonValue>
  conditions?: Record<string, JsonValue>
  clarificationReplies?: Record<string, JsonValue>
  tags?: string[]
  reviewGuidance?: string
}

/** A draft or immutable published suite revision. */
export interface EvaluationSuite {
  schemaVersion: 1
  id: string
  revision: number
  status: 'draft' | 'published'
  name: string
  testDatasetId: string
  cases: EvaluationCase[]
}

/** Versioned Agent inputs; executable paths and credentials are host-owned. */
export interface AgentProfile {
  schemaVersion: 1
  id: string
  revision: number
  status: 'draft' | 'published'
  name: string
  model: string
  reasoningEffort: string
  prompt: string
  contextVersion: 1 | 2
  timeoutSeconds: number
  concurrency: number
  rounds: number
}

/** A human decision recorded independently of the automatic result. */
export interface EvaluationReview {
  status: 'passed' | 'failed' | 'pending'
  reason: string
  reviewedBy: string
  reviewedAt: string
}

/** Metadata-only projection of one case/repetition execution. */
export interface EvaluationAttempt {
  id: string
  caseId: string
  round: number
  status: string
}

/** Frozen run identity with independently addressable attempts. */
export interface EvaluationRun {
  schemaVersion: 1
  id: string
  suiteId: string
  profileId: string
  status: string
  attempts: EvaluationAttempt[]
}

/** Comparison summary; unmatched conditions cannot assert strict regression. */
export interface EvaluationComparison {
  schemaVersion: 1
  strict: boolean
  mode: 'strict' | 'exploratory'
  changes: Record<string, { baseline: JsonValue; candidate: JsonValue }>
  items: { caseId: string; round: number; category: string }[]
}
