/** Verify the package file set includes only the reviewed sidecar runtime. */

import { spawnSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const npmExecutable = process.platform === 'win32' ? (process.env.ComSpec ?? 'cmd.exe') : 'npm'
const npmArguments = process.platform === 'win32'
  ? ['/d', '/s', '/c', 'npm.cmd pack --dry-run --json --ignore-scripts']
  : ['pack', '--dry-run', '--json', '--ignore-scripts']
const result = spawnSync(npmExecutable, npmArguments, {
  cwd: packageDirectory,
  encoding: 'utf8',
  windowsHide: true,
})

if (result.error !== undefined || result.status !== 0) {
  throw result.error ?? new Error(`npm pack --dry-run failed with exit code ${result.status}`)
}

let report
try {
  report = JSON.parse(result.stdout)
} catch (error) {
  throw new Error(`npm pack --dry-run returned invalid JSON: ${error instanceof Error ? error.message : 'unknown error'}`)
}

const files = Array.isArray(report) && Array.isArray(report[0]?.files)
  ? report[0].files.map(file => file?.path).filter(path => typeof path === 'string')
  : []
const required = [
  'python/sidecar/pyproject.toml',
  'python/sidecar/sidecar/__main__.py',
  'python/sidecar/sidecar/semantic_mcp.py',
  'python/sidecar/sidecar/semantic_service.py',
  'python/sidecar/sidecar/mcp_gateway.py',
  'python/sidecar/sidecar/wren_adapter.py',
  'python/sidecar/sidecar/protocol.py',
  'python/sidecar/sidecar/query.py',
  'python/semantic-console/server/app.py',
  'python/semantic-console/server/service.py',
  'python/semantic-console/server/semantic_models.py',
  'semantic-console-web/index.html',
  'semantic-console-web/licenses/react/LICENSE',
  'semantic-console-web/licenses/react-dom/LICENSE',
  'semantic-console-web/licenses/scheduler/LICENSE',
  'semantic-console-web/licenses/phosphor-icons-react/LICENSE',
  'semantic-console-web/licenses/xyflow-react/LICENSE',
  'semantic-console-web/licenses/i18next/LICENSE',
  'semantic-console-web/licenses/react-i18next/LICENSE',
]
for (const path of required) {
  if (!files.includes(path)) throw new Error(`npm pack omitted required sidecar file: ${path}`)
}

const forbidden = files.filter(path =>
  path.startsWith('python/sidecar/tests/')
  || path.startsWith('python/semantic-console/tests/')
  || path.includes('/__pycache__/')
  || path.includes('/.pytest_cache/')
  || path.endsWith('.pyc')
  || path.endsWith('.pyo')
  || path.endsWith('.env')
  || path.includes('.env.'),
)
if (forbidden.length > 0) throw new Error(`npm pack included forbidden sidecar files: ${forbidden.join(', ')}`)

console.log(`Packaged sidecar file verification passed (${files.length} package files).`)
