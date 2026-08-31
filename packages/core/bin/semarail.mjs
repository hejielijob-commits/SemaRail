#!/usr/bin/env node

import { randomBytes } from 'node:crypto'
import { execFileSync, spawn } from 'node:child_process'
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const DEFAULT_ENDPOINT = 'http://127.0.0.1:48763'
const VERSION = '0.1.0-alpha.3'

function usage() {
  return [
    'SemaRail Core',
    '',
    'Usage:',
    '  semarail start --project <directory> [--port 48763] [--state-dir <directory>]',
    '  semarail mcp serve --project <directory> [--port 48764] [--state-dir <directory>]',
    '  semarail status [--endpoint http://127.0.0.1:48763]',
    '  semarail auth login --provider <id> [--endpoint http://127.0.0.1:48763] [--no-open]',
    '  semarail auth status [--endpoint http://127.0.0.1:48763]',
    '  semarail auth logout [--endpoint http://127.0.0.1:48763]',
    '  semarail token create',
    '  semarail --version',
    '',
    'Authentication:',
    '  Set SEMARAIL_API_TOKEN to at least 32 characters before start/status.',
    '  Give Harness a scoped service-account key through SEMARAIL_HARNESS_TOKEN.',
  ].join('\n')
}

function parseOptions(args) {
  const result = new Map()
  for (let index = 0; index < args.length; index += 1) {
    const key = args[index]
    if (!key?.startsWith('--')) throw new Error(`unexpected argument: ${key ?? ''}`)
    if (key === '--no-open') {
      result.set('no-open', 'true')
      continue
    }
    const value = args[index + 1]
    if (value === undefined || value.startsWith('--')) throw new Error(`${key} requires a value`)
    result.set(key.slice(2), value)
    index += 1
  }
  return result
}

function endpointOption(options) {
  const endpoint = new URL(options.get('endpoint') ?? DEFAULT_ENDPOINT)
  const loopback = ['127.0.0.1', 'localhost', '::1'].includes(endpoint.hostname)
  if (endpoint.username || endpoint.password || endpoint.hash || (endpoint.protocol !== 'https:' && !(endpoint.protocol === 'http:' && loopback))) {
    throw new Error('--endpoint must be HTTPS or loopback HTTP without credentials')
  }
  return endpoint
}

function authFilePath(options) {
  return resolve(options.get('session-file') ?? process.env.SEMARAIL_AUTH_FILE?.trim() ?? resolve(homedir(), '.semarail', 'session.json'))
}

function saveSession(path, endpoint, session) {
  if (typeof session?.accessToken !== 'string' || !session.accessToken.startsWith('sr_session_') || typeof session?.expiresAt !== 'string') {
    throw new Error('SemaRail returned an invalid employee session')
  }
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
  const temporary = `${path}.${process.pid}.${randomBytes(12).toString('hex')}.tmp`
  try {
    writeFileSync(temporary, `${JSON.stringify({ endpoint: endpoint.origin, accessToken: session.accessToken, expiresAt: session.expiresAt, subject: session.subject }, null, 2)}\n`, { encoding: 'utf8', mode: 0o600, flag: 'wx' })
    if (process.platform === 'win32') {
      const username = process.env.USERNAME?.trim()
      const domain = process.env.USERDOMAIN?.trim()
      if (!username) throw new Error('Windows user identity is unavailable')
      const principal = domain ? `${domain}\\${username}` : username
      execFileSync('icacls.exe', [temporary, '/inheritance:r', '/grant:r', `${principal}:(F)`], {
        stdio: 'ignore', windowsHide: true,
      })
    } else {
      chmodSync(temporary, 0o600)
    }
    rmSync(path, { force: true })
    renameSync(temporary, path)
  } finally {
    rmSync(temporary, { force: true })
  }
}

function loadSession(path) {
  let parsed
  try { parsed = JSON.parse(readFileSync(path, 'utf8')) } catch { throw new Error('employee session is unavailable; run `semarail auth login`') }
  if (typeof parsed?.accessToken !== 'string' || !parsed.accessToken.startsWith('sr_session_') || typeof parsed?.endpoint !== 'string') {
    throw new Error('employee session is invalid; run `semarail auth login`')
  }
  return parsed
}

function openAuthorizationUrl(url) {
  let command
  let args
  if (process.platform === 'win32') {
    command = 'rundll32.exe'
    args = ['url.dll,FileProtocolHandler', url]
  } else if (process.platform === 'darwin') {
    command = 'open'
    args = [url]
  } else {
    command = 'xdg-open'
    args = [url]
  }
  const child = spawn(command, args, { detached: true, stdio: 'ignore', windowsHide: true })
  child.unref()
}

const delay = milliseconds => new Promise(resolveDelay => setTimeout(resolveDelay, milliseconds))

async function authLogin(args) {
  const options = parseOptions(args)
  const provider = options.get('provider')
  if (!provider) throw new Error('--provider is required')
  const endpoint = endpointOption(options)
  const startedResponse = await fetch(new URL('/api/v1/auth/device/start', endpoint), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider }),
    signal: AbortSignal.timeout(10_000),
  })
  const started = await startedResponse.json()
  if (!startedResponse.ok || typeof started?.verificationUriComplete !== 'string' || typeof started?.deviceCode !== 'string') {
    throw new Error('employee login could not be started')
  }
  if (options.get('no-open') === 'true') {
    process.stdout.write(`Open this URL to continue:\n${started.verificationUriComplete}\n`)
  } else {
    openAuthorizationUrl(started.verificationUriComplete)
    process.stdout.write('Complete employee authorization in your browser.\n')
  }
  const deadline = Date.parse(started.expiresAt)
  const interval = Math.max(1, Math.min(Number(started.interval) || 2, 10)) * 1000
  while (Number.isFinite(deadline) && Date.now() < deadline) {
    await delay(interval)
    const response = await fetch(new URL('/api/v1/auth/device/token', endpoint), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ deviceCode: started.deviceCode }),
      signal: AbortSignal.timeout(10_000),
    })
    const body = await response.json()
    if (response.status === 202 && body?.status === 'authorization_pending') continue
    if (!response.ok) throw new Error('employee authorization failed')
    const path = authFilePath(options)
    saveSession(path, endpoint, body)
    process.stdout.write(`Signed in as ${body?.subject?.name ?? 'employee'}; session saved to ${path}\n`)
    return
  }
  throw new Error('employee authorization expired')
}

async function authStatus(args) {
  const options = parseOptions(args)
  const path = authFilePath(options)
  const session = loadSession(path)
  const endpoint = options.has('endpoint') ? endpointOption(options) : endpointOption(new Map([['endpoint', session.endpoint]]))
  const response = await fetch(new URL('/api/v1/auth/me', endpoint), {
    headers: { Authorization: `Bearer ${session.accessToken}` },
    signal: AbortSignal.timeout(10_000),
  })
  const body = await response.json()
  if (!response.ok || body?.authenticationMethod !== 'oauth_session') throw new Error('employee session is no longer valid')
  process.stdout.write(`Signed in as ${body.subject?.name ?? body.subject?.id} until ${session.expiresAt}\n`)
}

async function authLogout(args) {
  const options = parseOptions(args)
  const path = authFilePath(options)
  const session = loadSession(path)
  const endpoint = options.has('endpoint') ? endpointOption(options) : endpointOption(new Map([['endpoint', session.endpoint]]))
  let revoked = false
  try {
    const response = await fetch(new URL('/api/v1/auth/logout', endpoint), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${session.accessToken}` },
      body: '{}',
      signal: AbortSignal.timeout(10_000),
    })
    revoked = response.ok
  } finally {
    rmSync(path, { force: true })
  }
  if (!revoked) throw new Error('local employee session was removed; server revocation could not be confirmed')
  process.stdout.write('Signed out.\n')
}

function requiredToken(envName = 'SEMARAIL_API_TOKEN') {
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(envName)) throw new Error('auth-token-env is invalid')
  const token = process.env[envName]?.trim() ?? ''
  if (token.length < 32) throw new Error(`${envName} must contain at least 32 characters`)
  return token
}

async function start(args) {
  const options = parseOptions(args)
  const project = options.get('project')
  if (!project) throw new Error('--project is required')
  const projectDir = resolve(project)
  if (!existsSync(projectDir)) throw new Error('project directory does not exist')
  const host = options.get('host') ?? '127.0.0.1'
  if (!['127.0.0.1', 'localhost', '::1'].includes(host)) throw new Error('SemaRail Core currently supports loopback binding only')
  const portText = options.get('port') ?? '48763'
  const port = Number(portText)
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('--port is invalid')
  const authTokenEnv = options.get('auth-token-env') ?? 'SEMARAIL_API_TOKEN'
  const token = requiredToken(authTokenEnv)
  const python = options.get('python') ?? process.env.SEMARAIL_PYTHON?.trim() ?? (process.platform === 'win32' ? 'python.exe' : 'python3')
  const bootstrap = resolve(packageRoot, 'runtime', 'bootstrap.py')
  const consoleRoot = resolve(packageRoot, 'python', 'semantic-console')
  const sidecarRoot = resolve(packageRoot, 'python', 'sidecar')
  const staticRoot = resolve(packageRoot, 'semantic-console-web')
  const childArgs = [
    bootstrap, '--', '-m', 'server', '--host', host, '--port', String(port),
    '--project-dir', projectDir, '--static-dir', staticRoot,
  ]
  const stateDir = options.get('state-dir')
  if (stateDir) childArgs.push('--state-dir', resolve(stateDir))
  const child = spawn(python, childArgs, {
    cwd: consoleRoot,
    stdio: 'inherit',
    windowsHide: true,
    env: {
      ...process.env,
      SEMARAIL_API_TOKEN: token,
      PYTHONPATH: sidecarRoot,
      PYTHONDONTWRITEBYTECODE: '1',
      SEMANTIC_CONSOLE_ORIGINS: `http://${host}:${port},http://localhost:${port}`,
    },
  })
  const forward = signal => {
    try { child.kill(signal) } catch {}
  }
  process.once('SIGINT', forward)
  process.once('SIGTERM', forward)
  const exitCode = await new Promise((resolveExit, reject) => {
    child.once('error', reject)
    child.once('exit', code => resolveExit(code ?? 1))
  })
  process.exitCode = exitCode
}

async function status(args) {
  const options = parseOptions(args)
  const endpoint = new URL(options.get('endpoint') ?? DEFAULT_ENDPOINT)
  const authTokenEnv = options.get('auth-token-env') ?? 'SEMARAIL_API_TOKEN'
  const token = requiredToken(authTokenEnv)
  const response = await fetch(new URL('/api/v1/runtime/rpc', endpoint), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ protocolVersion: '1', id: 'semarail-cli-status', method: 'health', params: {} }),
    signal: AbortSignal.timeout(10_000),
  })
  const body = await response.json()
  if (!response.ok || body?.ok !== true || body?.result?.service !== 'semarail-core') {
    throw new Error('SemaRail Core is unavailable or incompatible')
  }
  process.stdout.write(`SemaRail Core ${body.result.apiVersion} is ready at ${endpoint.origin}\n`)
}

async function serveMcp(args) {
  const options = parseOptions(args)
  const project = options.get('project')
  if (!project) throw new Error('--project is required')
  const projectDir = resolve(project)
  if (!existsSync(projectDir)) throw new Error('project directory does not exist')
  const host = options.get('host') ?? '127.0.0.1'
  const allowedHost = options.get('allowed-host')
  if (!['127.0.0.1', 'localhost', '::1'].includes(host) && !allowedHost) {
    throw new Error('non-loopback MCP requires --allowed-host')
  }
  const port = Number(options.get('port') ?? '48764')
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('--port is invalid')
  const authTokenEnv = options.get('auth-token-env') ?? 'SEMARAIL_API_TOKEN'
  const token = requiredToken(authTokenEnv)
  const python = options.get('python') ?? process.env.SEMARAIL_PYTHON?.trim() ?? (process.platform === 'win32' ? 'python.exe' : 'python3')
  const bootstrap = resolve(packageRoot, 'runtime', 'bootstrap.py')
  const consoleRoot = resolve(packageRoot, 'python', 'semantic-console')
  const sidecarRoot = resolve(packageRoot, 'python', 'sidecar')
  const childArgs = [
    bootstrap, '--', '-m', 'server.remote_mcp', '--host', host, '--port', String(port), '--project', projectDir,
  ]
  const stateDir = options.get('state-dir')
  if (stateDir) childArgs.push('--state-dir', resolve(stateDir))
  if (allowedHost) childArgs.push('--allowed-host', allowedHost)
  const child = spawn(python, childArgs, {
    cwd: consoleRoot,
    stdio: 'inherit',
    windowsHide: true,
    env: {
      ...process.env,
      SEMARAIL_API_TOKEN: token,
      PYTHONPATH: sidecarRoot,
      PYTHONDONTWRITEBYTECODE: '1',
    },
  })
  const forward = signal => {
    try { child.kill(signal) } catch {}
  }
  process.once('SIGINT', forward)
  process.once('SIGTERM', forward)
  process.exitCode = await new Promise((resolveExit, reject) => {
    child.once('error', reject)
    child.once('exit', code => resolveExit(code ?? 1))
  })
}

async function main(argv = process.argv.slice(2)) {
  const [command, ...args] = argv
  if (command === '--version' || command === '-v') {
    process.stdout.write(`${VERSION}\n`)
    return
  }
  if (command === '--help' || command === '-h' || command === undefined) {
    process.stdout.write(`${usage()}\n`)
    return
  }
  if (command === 'token' && args[0] === 'create' && args.length === 1) {
    process.stdout.write(`${randomBytes(32).toString('hex')}\n`)
    return
  }
  if (command === 'start') return start(args)
  if (command === 'auth' && args[0] === 'login') return authLogin(args.slice(1))
  if (command === 'auth' && args[0] === 'status') return authStatus(args.slice(1))
  if (command === 'auth' && args[0] === 'logout') return authLogout(args.slice(1))
  if (command === 'mcp' && args[0] === 'serve') return serveMcp(args.slice(1))
  if (command === 'status') return status(args)
  throw new Error(`unknown command: ${command}`)
}

main().catch(() => {
  process.stderr.write('SemaRail command failed. Run `semarail --help` and check the local configuration.\n')
  process.exitCode = 1
})
