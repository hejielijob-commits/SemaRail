import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const repoDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const defaultPython = process.platform === 'win32'
  ? resolve(repoDir, '.venv', 'Scripts', 'python.exe')
  : resolve(repoDir, '.venv', 'bin', 'python')
const pythonExecutable = resolve(process.argv[2] ?? defaultPython)
const projectDir = resolve(process.argv[3] ?? resolve(repoDir, 'examples', 'wren-postgres'))
const sidecarDir = resolve(process.argv[4] ?? resolve(repoDir, 'python', 'sidecar'))
const hostModule = resolve(process.argv[5] ?? resolve(repoDir, 'packages', 'host', 'lib', 'sidecar.js'))

for (const [label, path] of [
  ['Python executable', pythonExecutable],
  ['Wren project', projectDir],
  ['Sidecar directory', sidecarDir],
  ['Host module', hostModule],
]) {
  if (!existsSync(path)) throw new Error(`${label} does not exist: ${path}`)
}

const { SidecarRpcClient } = await import(pathToFileURL(hostModule).href)
const client = new SidecarRpcClient({
  pythonExecutable,
  sidecarModule: 'sidecar',
  workingDirectory: sidecarDir,
  timeoutMs: 30_000,
  startupTimeoutMs: 10_000,
})

function assertRecord(value, label) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${label} was not an object`)
  }
  return value
}

try {
  const health = assertRecord(await client.request('health', {}, { timeoutMs: 10_000 }), 'health')
  if (health.protocolVersion !== '1' || health.status !== 'ok' || health.wrenAvailable !== true) {
    throw new Error('health did not report protocol v1 with Wren available')
  }

  const validation = assertRecord(
    await client.request('project.validate', { projectDir }, { timeoutMs: 10_000 }),
    'project.validate',
  )
  if (validation.valid !== true || validation.errorCount !== 0) {
    throw new Error('example Wren project validation failed')
  }

  const question = '最近 30 天每天的销售额是多少？'
  const context = assertRecord(
    await client.request('context.ask', { projectDir, question }, { timeoutMs: 30_000 }),
    'context.ask',
  )
  if (context.schemaVersion !== 1 || !Array.isArray(context.models) || context.models.length !== 5) {
    throw new Error('semantic context did not contain the expected five models')
  }

  const semanticSql = "SELECT DATE_TRUNC('day', orders.ordered_at) AS order_day, COUNT(*) AS order_count FROM orders GROUP BY 1 ORDER BY 1"
  const plan = assertRecord(
    await client.request('query.dryPlan', { projectDir, semanticSql }, { timeoutMs: 30_000 }),
    'query.dryPlan',
  )
  const allowed = assertRecord(plan.allowedPhysical, 'query.dryPlan.allowedPhysical')
  if (typeof plan.nativeSql !== 'string' || plan.nativeSql.length === 0) {
    throw new Error('dry-plan did not return native SQL')
  }
  if (!Array.isArray(allowed.tables) || allowed.tables.length !== 5) {
    throw new Error('dry-plan did not return the expected physical object allowlist')
  }

  console.log('SIDECAR_PROBE_PASS')
  console.log(`  protocol: ${health.protocolVersion}`)
  console.log(`  Wren: ${health.wrenVersion ?? 'available'}`)
  console.log(`  models: ${context.models.length}`)
  console.log(`  physical tables: ${allowed.tables.length}`)
} finally {
  client.dispose()
}
