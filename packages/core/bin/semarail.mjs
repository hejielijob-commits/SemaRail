#!/usr/bin/env node

import { randomBytes } from 'node:crypto'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const DEFAULT_ENDPOINT = 'http://127.0.0.1:48763'
const VERSION = '0.1.0-alpha.2'

function usage() {
  return [
    'SemaRail Core',
    '',
    'Usage:',
    '  semarail start --project <directory> [--port 48763] [--state-dir <directory>]',
    '  semarail status [--endpoint http://127.0.0.1:48763]',
    '  semarail token create',
    '  semarail --version',
    '',
    'Authentication:',
    '  Set SEMARAIL_API_TOKEN to at least 32 characters before start/status.',
    '  Harness reads the same variable; the token is never stored in plugin config.',
  ].join('\n')
}

function parseOptions(args) {
  const result = new Map()
  for (let index = 0; index < args.length; index += 1) {
    const key = args[index]
    if (!key?.startsWith('--')) throw new Error(`unexpected argument: ${key ?? ''}`)
    const value = args[index + 1]
    if (value === undefined || value.startsWith('--')) throw new Error(`${key} requires a value`)
    result.set(key.slice(2), value)
    index += 1
  }
  return result
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
  if (command === 'status') return status(args)
  throw new Error(`unknown command: ${command}`)
}

main().catch(() => {
  process.stderr.write('SemaRail command failed. Run `semarail --help` and check the local configuration.\n')
  process.exitCode = 1
})
