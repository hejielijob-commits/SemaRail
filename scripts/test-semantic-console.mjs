/** Run the dependency-light Semantic Console tests with the project Python. */

import { existsSync } from 'node:fs'
import { delimiter, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'

const repository = resolve(import.meta.dirname, '..')
const appRoot = resolve(repository, 'apps', 'semantic-console')
const configuredPython = process.env.PYTHON?.trim()
const virtualPython = process.platform === 'win32'
  ? resolve(repository, '.venv', 'Scripts', 'python.exe')
  : resolve(repository, '.venv', 'bin', 'python')
const python = configuredPython || (existsSync(virtualPython) ? virtualPython : (process.platform === 'win32' ? 'python.exe' : 'python3'))
const existingPath = process.env.PYTHONPATH?.trim()
const result = spawnSync(python, [
  '-m', 'unittest', 'discover',
  '-s', resolve(appRoot, 'server', 'tests'),
  '-p', 'test_*.py',
  '-v',
], {
  cwd: repository,
  env: {
    ...process.env,
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONPATH: existingPath ? `${appRoot}${delimiter}${existingPath}` : appRoot,
  },
  encoding: 'utf8',
  windowsHide: true,
})

if (result.stdout) process.stdout.write(result.stdout)
if (result.stderr) process.stderr.write(result.stderr)
if (result.error !== undefined) throw result.error
if (result.status !== 0) process.exit(result.status ?? 1)
