import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const npmExecutable = process.platform === 'win32' ? (process.env.ComSpec ?? 'cmd.exe') : 'npm'
const args = process.platform === 'win32'
  ? ['/d', '/s', '/c', 'npm.cmd pack --dry-run --json --ignore-scripts']
  : ['pack', '--dry-run', '--json', '--ignore-scripts']
const result = spawnSync(npmExecutable, args, { cwd: packageDir, encoding: 'utf8', windowsHide: true })
if (result.error !== undefined || result.status !== 0) throw result.error ?? new Error('npm pack failed')
const report = JSON.parse(result.stdout)
const manifest = JSON.parse(readFileSync(resolve(packageDir, 'package.json'), 'utf8'))
if (report[0]?.name !== manifest.name || report[0]?.version !== manifest.version) {
  throw new Error('Core pack identity does not match package.json')
}
const files = report[0]?.files?.map(file => file.path) ?? []
const required = [
  'bin/semarail.mjs', 'runtime/bootstrap.py', 'runtime/constraints.txt',
  'python/sidecar/sidecar/query.py', 'python/sidecar/sidecar/row_policy.py', 'python/sidecar/sidecar/semantic_mcp.py',
  'python/sidecar/sidecar/semantic_policy.py',
  'python/semantic-console/server/app.py', 'python/semantic-console/server/runtime_rpc.py',
  'python/semantic-console/server/access_control.py', 'python/semantic-console/server/authorization.py',
  'python/semantic-console/server/identity.py', 'python/semantic-console/server/identity_api.py',
  'python/semantic-console/server/remote_mcp.py', 'python/semantic-console/server/stdio_mcp.py',
  'python/semantic-console/server/README.md', 'python/semantic-console/server/openapi.json',
  'semantic-console-web/index.html', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
]
for (const file of required) if (!files.includes(file)) throw new Error(`Core package omitted ${file}`)
const forbidden = files.filter(file => file.startsWith('lib/client') || file === 'cordis.patch.yml' || file.includes('/tests/'))
if (forbidden.length) throw new Error(`Core package included Harness/test files: ${forbidden.join(', ')}`)
for (const [file, marker] of [
  ['python/semantic-console/server/access_api.py', 'unbind_policy'],
  ['python/semantic-console/server/app.py', '/api/v1/auth/capabilities'],
  ['python/semantic-console/server/runtime_rpc.py', 'project_id=project_id'],
  ['python/semantic-console/server/authorization.py', 'datasource binding is required'],
  ['python/semantic-console/server/access_control.py', 'confirmation_hash'],
  ['python/sidecar/sidecar/row_policy.py', 'traverse_scope'],
  ['python/sidecar/sidecar/semantic_policy.py', '_safe_relationship_condition'],
  ['bin/semarail.mjs', 'Enter the confirmation code shown in your browser'],
]) {
  if (!readFileSync(resolve(packageDir, file), 'utf8').includes(marker)) {
    throw new Error(`Core package has stale generated content: ${file}`)
  }
}
console.log(`Standalone SemaRail Core package verification passed (${files.length} files).`)
