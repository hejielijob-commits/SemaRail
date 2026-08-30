/** Run the upstream-compatibility and stable SemaRail MCP acceptance gate. */

import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'

const repository = resolve(import.meta.dirname, '..')
const configuredPython = process.env.PYTHON?.trim()
const virtualPython = process.platform === 'win32'
  ? resolve(repository, '.venv', 'Scripts', 'python.exe')
  : resolve(repository, '.venv', 'bin', 'python')
const fallbackPython = process.platform === 'win32' ? 'python.exe' : 'python3'
const python = configuredPython || (existsSync(virtualPython) ? virtualPython : fallbackPython)

const result = spawnSync(
  python,
  [resolve(repository, 'scripts', 'acceptance-wren-mcp.py'), ...process.argv.slice(2)],
  {
    cwd: repository,
    env: {
      ...process.env,
      PYTHONDONTWRITEBYTECODE: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
    },
    encoding: 'utf8',
    windowsHide: true,
  },
)

if (result.stdout) process.stdout.write(result.stdout)
if (result.stderr) process.stderr.write(result.stderr)
if (result.error !== undefined) throw result.error
if (result.status !== 0) process.exit(result.status ?? 1)
