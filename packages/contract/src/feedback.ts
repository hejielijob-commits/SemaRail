/** Versioned feedback payload exchanged by MCP clients and SemaRail Core. */

import {
  _boolean,
  _enum,
  _keys,
  _literal,
  _optional,
  _record,
  _required,
  _string,
} from './json.js'

/** Feedback classifications shared by MCP and the Console. */
export const FEEDBACK_CATEGORIES = [
  'ambiguity',
  'knowledge_gap',
  'agent_understanding',
  'sql_generation',
  'permission_configuration',
  'runtime_failure',
  'evaluation',
  'other',
] as const

export type FeedbackCategory = typeof FEEDBACK_CATEGORIES[number]

/** Strict, bounded request sent from a result card to its Host process. */
export interface FeedbackSubmissionRequestV1 {
  readonly schemaVersion: 1
  readonly reference: string
  readonly idempotencyKey: string
  readonly category: FeedbackCategory
  readonly description: string
  readonly expectedBehavior?: string
  /** Client-provided evidence; Core still determines query ownership. */
  readonly question?: string
  readonly semanticSql?: string
  readonly nativeSql?: string
}

/** Accepted feedback receipt returned by Core through the Host. */
export interface FeedbackAcceptedV1 {
  readonly schemaVersion: 1
  readonly status: 'accepted'
  readonly feedbackId: string
  readonly diagnosticId: string
  readonly workflowStatus: string
  readonly duplicate: boolean
}

/** Safe rejection returned without exposing Host credentials or Core internals. */
export interface FeedbackRejectedV1 {
  readonly schemaVersion: 1
  readonly status: 'rejected'
  readonly code: string
  readonly message: string
  readonly retryable: boolean
}

export type FeedbackSubmissionResponseV1 = FeedbackAcceptedV1 | FeedbackRejectedV1

function optionalText(value: Record<string, unknown>, key: string, limit: number): string | undefined {
  const candidate = _optional(value, key)
  return candidate === undefined ? undefined : _string(candidate, `feedback.${key}`, 1, limit)
}

/** Parse and reject unknown or oversized Client feedback fields. */
export function parseFeedbackSubmissionRequest(value: unknown): FeedbackSubmissionRequestV1 {
  const record = _record(value, 'feedback')
  _keys(record, [
    'schemaVersion', 'reference', 'idempotencyKey', 'category', 'description',
    'expectedBehavior', 'question', 'semanticSql', 'nativeSql',
  ], 'feedback')
  _literal(_required(record, 'schemaVersion', 'feedback'), 1, 'feedback.schemaVersion')
  const expectedBehavior = optionalText(record, 'expectedBehavior', 8_000)
  const question = optionalText(record, 'question', 64_000)
  const semanticSql = optionalText(record, 'semanticSql', 64_000)
  const nativeSql = optionalText(record, 'nativeSql', 64_000)
  return {
    schemaVersion: 1,
    reference: _string(_required(record, 'reference', 'feedback'), 'feedback.reference', 1, 128),
    idempotencyKey: _string(_required(record, 'idempotencyKey', 'feedback'), 'feedback.idempotencyKey', 1, 128),
    category: _enum(_required(record, 'category', 'feedback'), FEEDBACK_CATEGORIES, 'feedback.category'),
    description: _string(_required(record, 'description', 'feedback'), 'feedback.description', 1, 8_000),
    ...(expectedBehavior === undefined ? {} : { expectedBehavior }),
    ...(question === undefined ? {} : { question }),
    ...(semanticSql === undefined ? {} : { semanticSql }),
    ...(nativeSql === undefined ? {} : { nativeSql }),
  }
}

/** Parse a Host receipt before the browser renders it. */
export function parseFeedbackSubmissionResponse(value: unknown): FeedbackSubmissionResponseV1 {
  const record = _record(value, 'feedbackResponse')
  const status = _enum(_required(record, 'status', 'feedbackResponse'), ['accepted', 'rejected'] as const, 'feedbackResponse.status')
  _literal(_required(record, 'schemaVersion', 'feedbackResponse'), 1, 'feedbackResponse.schemaVersion')
  if (status === 'accepted') {
    _keys(record, ['schemaVersion', 'status', 'feedbackId', 'diagnosticId', 'workflowStatus', 'duplicate'], 'feedbackResponse')
    return {
      schemaVersion: 1,
      status,
      feedbackId: _string(_required(record, 'feedbackId', 'feedbackResponse'), 'feedbackResponse.feedbackId', 1, 128),
      diagnosticId: _string(_required(record, 'diagnosticId', 'feedbackResponse'), 'feedbackResponse.diagnosticId', 1, 128),
      workflowStatus: _string(_required(record, 'workflowStatus', 'feedbackResponse'), 'feedbackResponse.workflowStatus', 1, 64),
      duplicate: _boolean(_required(record, 'duplicate', 'feedbackResponse'), 'feedbackResponse.duplicate'),
    }
  }
  _keys(record, ['schemaVersion', 'status', 'code', 'message', 'retryable'], 'feedbackResponse')
  return {
    schemaVersion: 1,
    status,
    code: _string(_required(record, 'code', 'feedbackResponse'), 'feedbackResponse.code', 1, 128),
    message: _string(_required(record, 'message', 'feedbackResponse'), 'feedbackResponse.message', 1, 500),
    retryable: _boolean(_required(record, 'retryable', 'feedbackResponse'), 'feedbackResponse.retryable'),
  }
}
