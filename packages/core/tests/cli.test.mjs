import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { test } from 'node:test'
import { resolve } from 'node:path'

const cli = resolve(import.meta.dirname, '..', 'bin', 'semarail.mjs')

test('CLI exposes branded help and version', () => {
  const help = spawnSync(process.execPath, [cli, '--help'], { encoding: 'utf8' })
  assert.equal(help.status, 0)
  assert.match(help.stdout, /SemaRail Core/)
  assert.match(help.stdout, /semarail start --project/)
  assert.match(help.stdout, /semarail mcp serve --project/)
  assert.match(help.stdout, /semarail auth login --provider/)
  const version = spawnSync(process.execPath, [cli, '--version'], { encoding: 'utf8' })
  assert.equal(version.stdout.trim(), '0.1.0-alpha.3')
})

test('employee auth commands fail closed without configuration or session data', () => {
  const missingProvider = spawnSync(process.execPath, [cli, 'auth', 'login'], { encoding: 'utf8' })
  assert.equal(missingProvider.status, 1)
  assert.doesNotMatch(missingProvider.stderr, /Bearer|sr_session_/)

  const missingSession = spawnSync(process.execPath, [cli, 'auth', 'status', '--session-file', resolve(import.meta.dirname, 'missing-session.json')], { encoding: 'utf8' })
  assert.equal(missingSession.status, 1)
  assert.doesNotMatch(missingSession.stderr, /Bearer|sr_session_/)
})

test('CLI generates a strong token without decorating its value', () => {
  const result = spawnSync(process.execPath, [cli, 'token', 'create'], { encoding: 'utf8' })
  assert.equal(result.status, 0)
  assert.match(result.stdout.trim(), /^[a-f0-9]{64}$/)
})

test('start fails closed without authentication configuration', () => {
  const result = spawnSync(process.execPath, [cli, 'start', '--project', resolve(import.meta.dirname, '..')], {
    encoding: 'utf8',
    env: { ...process.env, SEMARAIL_API_TOKEN: '' },
  })
  assert.equal(result.status, 1)
  assert.doesNotMatch(result.stderr, /token-that|Bearer|[a-f0-9]{64}/)
})

test('remote MCP fails closed without authentication configuration', () => {
  const result = spawnSync(process.execPath, [cli, 'mcp', 'serve', '--project', resolve(import.meta.dirname, '..')], {
    encoding: 'utf8',
    env: { ...process.env, SEMARAIL_API_TOKEN: '' },
  })
  assert.equal(result.status, 1)
  assert.doesNotMatch(result.stderr, /Bearer|[a-f0-9]{64}/)
})
