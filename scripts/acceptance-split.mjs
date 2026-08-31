/** Real HTTP acceptance: thin Host gateway -> packaged SemaRail Core server. */
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'node:net'

const repository = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const core = resolve(repository, 'packages', 'core')
const python = process.env.SEMARAIL_TEST_PYTHON?.trim()
  || resolve(repository, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const token = 'semarail-split-acceptance-token-0123456789abcdef'
const state = mkdtempSync(resolve(tmpdir(), 'semarail-split-acceptance-'))

async function freePort() {
  return await new Promise((resolvePort, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (typeof address !== 'object' || address === null) return reject(new Error('could not allocate port'))
      server.close(error => error ? reject(error) : resolvePort(address.port))
    })
  })
}

async function waitForConsole(endpoint, child) {
  const deadline = Date.now() + 20_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error('packaged Core server exited before readiness')
    try {
      const response = await fetch(new URL('/api/health', endpoint), { signal: AbortSignal.timeout(1000) })
      if (response.ok) return
    } catch {}
    await new Promise(resolveWait => setTimeout(resolveWait, 100))
  }
  throw new Error('packaged Core server did not become ready')
}

async function adminJson(endpoint, path, init = {}) {
  const response = await fetch(new URL(path, endpoint), {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      ...init.headers,
    },
  })
  if (!response.ok) throw new Error(`bootstrap request failed: ${path} (${response.status})`)
  return await response.json()
}

let child
try {
  const port = await freePort()
  const endpoint = `http://127.0.0.1:${port}`
  child = spawn(python, [
    '-m', 'server', '--host', '127.0.0.1', '--port', String(port),
    '--project-dir', resolve(repository, 'examples', 'wren-postgres'),
    '--state-dir', state,
    '--static-dir', resolve(core, 'semantic-console-web'),
  ], {
    cwd: resolve(core, 'python', 'semantic-console'),
    stdio: ['ignore', 'ignore', 'ignore'],
    windowsHide: true,
    env: {
      ...process.env,
      PYTHONPATH: resolve(core, 'python', 'sidecar'),
      PYTHONDONTWRITEBYTECODE: '1',
      SEMARAIL_API_TOKEN: token,
    },
  })
  await waitForConsole(endpoint, child)

  const account = await adminJson(endpoint, '/api/v1/access/service-accounts', {
    method: 'POST',
    body: JSON.stringify({ name: 'Split acceptance Harness' }),
  })
  const policy = await adminJson(endpoint, '/api/v1/access/policies', {
    method: 'POST',
    body: JSON.stringify({
      name: 'Split acceptance semantic read',
      document: {
        schemaVersion: 1,
        projects: ['semarail_sales'],
        tools: ['runtime:health', 'project:validate', 'semantic:read'],
        tables: {},
      },
    }),
  })
  await adminJson(endpoint, '/api/v1/access/policy-bindings', {
    method: 'POST',
    body: JSON.stringify({ subjectId: account.id, policyId: policy.id }),
  })
  const issued = await adminJson(endpoint, `/api/v1/access/service-accounts/${account.id}/keys`, {
    method: 'POST',
    body: JSON.stringify({ label: 'acceptance' }),
  })
  if (typeof issued.apiKey !== 'string' || !issued.apiKey.startsWith('sr_live_')) {
    throw new Error('bootstrap did not issue a scoped service-account key')
  }

  const clientRequire = createRequire(resolve(repository, 'packages', 'client', 'package.json'))
  const { build } = clientRequire('esbuild')
  const built = await build({
    entryPoints: [resolve(repository, 'packages', 'host', 'src', 'core-http.ts')],
    bundle: true,
    format: 'esm',
    platform: 'node',
    target: 'node22',
    write: false,
  })
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(built.outputFiles[0].contents).toString('base64')}`
  process.env.SEMARAIL_HARNESS_TOKEN = issued.apiKey
  const { createCoreHttpGateway } = await import(moduleUrl)
  const gateway = createCoreHttpGateway({ semarailEndpoint: endpoint })
  await gateway.start()
  const context = await gateway.context({ question: 'What is the daily order revenue?' }, new AbortController().signal)
  if (context.schemaVersion !== 1 || !Array.isArray(context.models)) {
    throw new Error('thin gateway returned an invalid semantic context')
  }
  gateway.dispose()
  process.stdout.write(`Split acceptance passed: thin gateway reached packaged Core and resolved ${context.models.length} model(s).\n`)
} finally {
  delete process.env.SEMARAIL_HARNESS_TOKEN
  if (child !== undefined && child.exitCode === null) {
    child.kill('SIGTERM')
    await new Promise(resolveExit => child.once('exit', resolveExit))
  }
  rmSync(state, { recursive: true, force: true })
}
