#!/usr/bin/env node

/**
 * AC05 public-API replay evidence collector.
 *
 * This intentionally uses only the rc.10-compatible HTTP session.list/history/fork API and
 * the durable tool/result message metadata. It does not read JSONL files or
 * Harness internals. It proves that a fork preserves the same data_query
 * presentation metadata; browser refresh/visual rendering remains a separate
 * gate and is never claimed by this script.
 */

import { createHash } from 'node:crypto'

const args = parseArgs(process.argv.slice(2))
if (args.dryRun) {
  console.log('REPLAY_EVIDENCE_DRY_RUN_OK: public session.list/history/fork only; AC05 not evaluated')
  process.exit(0)
}

const baseUrl = required(args['base-url'], '--base-url').replace(/\/$/, '')
const sessionId = required(args['session-id'], '--session-id')
const maxMessages = positiveInt(args['max-messages'] ?? '200', '--max-messages')

const listed = await rpc('session.list', {})
const listedIds = new Set((listed.items ?? []).map(item => item?.sessionId).filter(value => typeof value === 'string'))
if (!listedIds.has(sessionId)) throw new Error('REPLAY_EVIDENCE_FAIL: session is not visible through session.list')

const sourceHistory = await loadHistory(sessionId, maxMessages)
const sourceMetas = extractDataQueryMetas(sourceHistory.events)
if (sourceMetas.length === 0) {
  throw new Error('REPLAY_EVIDENCE_BLOCKED: source session has no durable data_query tool/result.meta')
}
const sourceMeta = sourceMetas.at(-1)
const sourceHash = digest(sourceMeta)
const forkAtSeq = args['at-seq'] === undefined
  ? lastCompletedTurnSeq(sourceHistory.events)
  : positiveInt(args['at-seq'], '--at-seq', true)
const fork = await rpc('session.fork', { sessionId, atSeq: forkAtSeq })
const forkId = fork.sessionId
if (typeof forkId !== 'string' || forkId.length === 0) throw new Error('REPLAY_EVIDENCE_FAIL: session.fork returned no child id')
const forkHistory = await loadHistory(forkId, maxMessages)
const forkMetas = extractDataQueryMetas(forkHistory.events)
const forkMeta = forkMetas.find(meta => digest(meta) === sourceHash)
if (forkMeta === undefined) {
  throw new Error('REPLAY_EVIDENCE_FAIL: fork history did not preserve the source data_query presentation metadata')
}

console.log('REPLAY_EVIDENCE_PASS')
console.log(`  sourceSession=${sessionId}`)
console.log(`  forkSession=${forkId}`)
console.log(`  queryId=${String(sourceMeta.queryId)}`)
console.log(`  metaSha256=${sourceHash}`)
console.log('  publicHistory: source + fork metadata matched')
console.log('  browserRefresh: not evaluated by this API-only gate')

function parseArgs(values) {
  const result = {}
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]
    if (value === '--dry-run') result.dryRun = true
    else if (value.startsWith('--') && values[index + 1] !== undefined && !values[index + 1].startsWith('--')) {
      result[value.slice(2)] = values[++index]
    } else throw new Error(`unknown or incomplete argument: ${value}`)
  }
  return result
}

function required(value, name) {
  if (typeof value !== 'string' || value.trim().length === 0) throw new Error(`${name} is required`)
  return value.trim()
}

function positiveInt(value, name, allowZero = false) {
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed) || (allowZero ? parsed < 0 : parsed < 1)) throw new Error(`${name} must be a safe integer`)
  return parsed
}

async function rpc(method, payload) {
  const response = await fetch(`${baseUrl}/api/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ type: 'client-request', rpcId: `replay-${Date.now()}-${Math.random()}`, method, payload }),
  })
  if (!response.ok) throw new Error(`REPLAY_EVIDENCE_FAIL: ${method} HTTP ${response.status}`)
  const envelope = await response.json()
  const result = envelope?.result
  if (!result?.ok) throw new Error(`REPLAY_EVIDENCE_FAIL: ${method} returned an RPC error`)
  return result.value
}

async function loadHistory(id, pageSize) {
  const events = []
  let beforeSeq
  for (let page = 0; page < 20; page += 1) {
    const value = await rpc('session.history', {
      sessionId: id,
      maxMessages: pageSize,
      ...(beforeSeq === undefined ? {} : { beforeSeq }),
    })
    if (!Array.isArray(value.events)) throw new Error('REPLAY_EVIDENCE_FAIL: malformed session.history response')
    events.unshift(...value.events)
    if (value.hasMore !== true) break
    const firstSeq = value.events[0]?.event?.seq
    if (!Number.isSafeInteger(firstSeq)) throw new Error('REPLAY_EVIDENCE_FAIL: history page has no sequence anchor')
    beforeSeq = firstSeq
  }
  return { events }
}

function extractDataQueryMetas(entries) {
  const metas = []
  for (const entry of entries) {
    const event = entry?.event
    if (event?.type !== 'tool/result') continue
    // Harness stores presentation metadata beside the tool message in the
    // durable tool/result payload. Keep the nested form as a compatibility
    // fallback for older fixtures, but prefer the public durable shape.
    const meta = event?.data?.meta ?? event?.data?.message?.meta
    if (!meta || typeof meta !== 'object' || Array.isArray(meta)) continue
    if ((meta.schemaVersion !== 1 && meta.schemaVersion !== 2) || typeof meta.queryId !== 'string') continue
    if (typeof meta.status !== 'string' || !Array.isArray(meta.columns) || !Array.isArray(meta.previewRows)) continue
    if (meta.schemaVersion === 2) {
      if (meta.status === 'success' && meta.delivery === 'inline' && meta.artifact !== undefined) continue
      if (meta.status === 'success' && meta.delivery === 'artifact') {
        if (meta.previewRows.length > 20 || typeof meta.artifact?.downloadUrl !== 'string') continue
      } else if (meta.status === 'success' && meta.delivery !== 'inline') continue
      if (meta.status === 'error' && (meta.delivery !== undefined || meta.artifact !== undefined)) continue
    }
    metas.push(meta)
  }
  return metas
}

function lastCompletedTurnSeq(entries) {
  const completed = entries
    .map(entry => entry?.event)
    .filter(event => event?.type === 'turn/end' && Number.isSafeInteger(event.seq))
    .map(event => event.seq)
  const result = completed.at(-1)
  if (!Number.isSafeInteger(result)) throw new Error('REPLAY_EVIDENCE_BLOCKED: source has no completed turn for a safe fork anchor')
  return result
}

function digest(value) {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex')
}
