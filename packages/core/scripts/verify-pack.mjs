import { spawnSync } from 'node:child_process'
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
const files = report[0]?.files?.map(file => file.path) ?? []
const required = [
  'bin/semarail.mjs', 'runtime/bootstrap.py', 'runtime/constraints.txt',
  'python/sidecar/sidecar/query.py', 'python/sidecar/sidecar/semantic_mcp.py',
  'python/semantic-console/server/app.py', 'python/semantic-console/server/runtime_rpc.py',
  'semantic-console-web/index.html', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
]
for (const file of required) if (!files.includes(file)) throw new Error(`Core package omitted ${file}`)
const forbidden = files.filter(file => file.startsWith('lib/client') || file === 'cordis.patch.yml' || file.includes('/tests/'))
if (forbidden.length) throw new Error(`Core package included Harness/test files: ${forbidden.join(', ')}`)
console.log(`Standalone SemaRail Core package verification passed (${files.length} files).`)
