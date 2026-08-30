import { execFileSync } from 'node:child_process'
import { copyFileSync, cpSync, mkdirSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoDir = resolve(packageDir, '..', '..')
const hostDir = resolve(repoDir, 'packages', 'host')
const consoleWebDir = resolve(repoDir, 'apps', 'semantic-console', 'web')
const consoleRequire = createRequire(resolve(consoleWebDir, 'package.json'))
const vite = resolve(dirname(consoleRequire.resolve('vite/package.json')), 'bin', 'vite.js')

function node(script, args = [], options = {}) {
  execFileSync(process.execPath, [script, ...args], {
    cwd: options.cwd ?? repoDir,
    stdio: 'inherit',
  })
}

for (const name of ['python', 'runtime', 'semantic-console-web']) {
  rmSync(resolve(packageDir, name), { recursive: true, force: true })
}
rmSync(resolve(packageDir, 'LICENSE'), { force: true })
rmSync(resolve(packageDir, 'THIRD_PARTY_NOTICES.md'), { force: true })
node(resolve(hostDir, 'scripts', 'stage-sidecar.mjs'))
node(vite, ['build'], { cwd: consoleWebDir })
node(resolve(hostDir, 'scripts', 'stage-semantic-console.mjs'))
for (const name of ['python', 'runtime', 'semantic-console-web']) {
  cpSync(resolve(hostDir, name), resolve(packageDir, name), { recursive: true })
}
mkdirSync(packageDir, { recursive: true })
copyFileSync(resolve(repoDir, 'LICENSE'), resolve(packageDir, 'LICENSE'))
copyFileSync(resolve(repoDir, 'THIRD_PARTY_NOTICES.md'), resolve(packageDir, 'THIRD_PARTY_NOTICES.md'))
console.log(`Built standalone SemaRail Core package in ${packageDir}`)
