import { spawnSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const python = process.env.PYTHON || (process.platform === 'win32' ? 'python.exe' : 'python3')
const result = spawnSync(python, ['-m', 'unittest', 'runtime/test_bootstrap.py', '-v'], {
  cwd: packageDirectory,
  stdio: 'inherit',
  windowsHide: true,
})
if (result.error !== undefined || result.status !== 0) {
  throw result.error ?? new Error(`runtime bootstrap tests failed with exit code ${result.status}`)
}
