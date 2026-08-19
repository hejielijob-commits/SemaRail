const MAX_FRAME_BYTES = 16 * 1024 * 1024
let input = Buffer.alloc(0)
const waiting = new Map()

function frame(value) {
  const payload = Buffer.from(JSON.stringify(value), 'utf8')
  if (payload.length > MAX_FRAME_BYTES) throw new Error('fixture frame too large')
  const result = Buffer.alloc(4 + payload.length)
  result.writeUInt32BE(payload.length, 0)
  payload.copy(result, 4)
  return result
}

function respond(request, result, error) {
  const response = {
    protocolVersion: '1',
    id: request.id,
    ok: error === undefined,
    ...(error === undefined ? { result } : { error }),
  }
  process.stdout.write(frame(response))
}

function presentation(request) {
  const semanticSql = request.params.semanticSql
  return {
    schemaVersion: 1,
    queryId: request.params.queryId,
    status: 'success',
    semanticSql,
    nativeSql: `SELECT '${semanticSql}'`,
    columns: [{ name: 'value', type: 'VARCHAR', semanticRole: 'dimension' }],
    previewRows: [{ value: semanticSql }],
    stats: { returnedRows: 1, durationMs: 1, truncated: false },
  }
}

function handle(request) {
  if (request.method === 'health') {
    respond(request, { status: 'ok', protocolVersion: '1', wrenAvailable: true, wrenVersion: '0.13.2' })
    return
  }
  if (request.method === 'project.validate') {
    respond(request, { valid: true, errorCount: 0, warningCount: 0, projectRevision: 'sha256:fixture' })
    return
  }
  if (request.method === 'context.ask') {
    respond(request, {
      schemaVersion: 1,
      projectRevision: 'sha256:fixture',
      models: [{ name: 'orders', table: 'orders', columns: [{ name: 'id', type: 'INT', semanticRole: 'dimension' }] }],
      relationships: [],
      summary: request.params.question,
    })
    return
  }
  if (request.method === 'query.cancel') {
    const queryId = request.params.queryId
    const target = waiting.get(queryId)
    if (target !== undefined) {
      waiting.delete(queryId)
      if (target.cancelResponds) {
        respond(target.request, undefined, { code: 'CANCELLED', phase: 'run', message: 'cancelled', retryable: false })
      }
    }
    respond(request, { cancelled: target !== undefined })
    return
  }
  if (request.method === 'query.run') {
    const sql = request.params.semanticSql
    if (sql === 'EXIT') {
      process.stderr.write('dsn=postgres://user:secret@example.invalid/db\n')
      process.exit(7)
    }
    if (sql === 'BAD') {
      process.stdout.write(Buffer.from([0, 0, 0, 4, 0xff, 0xff, 0xff, 0xff]))
      return
    }
    if (sql === 'TRUNCATED') {
      const partial = frame({ protocolVersion: '1', id: request.id, ok: true }).subarray(0, 8)
      process.stdout.end(partial, () => process.exit(0))
      return
    }
    if (sql === 'WAIT') {
      waiting.set(request.params.queryId, { request, cancelResponds: true })
      return
    }
    if (sql === 'STUCK') {
      waiting.set(request.params.queryId, { request, cancelResponds: false })
      return
    }
    if (sql === 'CHECK_CANCEL') {
      setTimeout(() => respond(request, presentation({ ...request, params: { ...request.params, semanticSql: String(waiting.size) } })), 2)
      return
    }
    const delay = sql === 'LATE' ? 40 : 2
    setTimeout(() => respond(request, presentation(request)), delay)
    return
  }
  respond(request, undefined, { code: 'METHOD_NOT_FOUND', phase: 'dispatch', message: 'unsupported', retryable: false })
}

process.stdin.on('data', chunk => {
  input = input.length === 0 ? Buffer.from(chunk) : Buffer.concat([input, Buffer.from(chunk)])
  while (input.length >= 4) {
    const size = input.readUInt32BE(0)
    if (size > MAX_FRAME_BYTES || input.length < size + 4) break
    const payload = input.subarray(4, size + 4)
    input = input.subarray(size + 4)
    handle(JSON.parse(payload.toString('utf8')))
  }
})
