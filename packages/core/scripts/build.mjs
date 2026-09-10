import { execFileSync } from 'node:child_process'
import { copyFileSync, lstatSync, mkdirSync, readdirSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoDir = resolve(packageDir, '..', '..')
const consoleWebDir = resolve(repoDir, 'apps', 'semantic-console', 'web')
const consoleRequire = createRequire(resolve(consoleWebDir, 'package.json'))
const vite = resolve(dirname(consoleRequire.resolve('vite/package.json')), 'bin', 'vite.js')

function node(script, args = [], options = {}) {
  execFileSync(process.execPath, [script, ...args], {
    cwd: options.cwd ?? repoDir,
    stdio: 'inherit',
  })
}

for (const name of ['python', 'semantic-console-web']) {
  rmSync(resolve(packageDir, name), { recursive: true, force: true })
}
rmSync(resolve(packageDir, 'LICENSE'), { force: true })
rmSync(resolve(packageDir, 'THIRD_PARTY_NOTICES.md'), { force: true })
node(vite, ['build'], { cwd: consoleWebDir })

function copyRegularFile(source, target) {
  const stat = lstatSync(source)
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`Expected a regular file: ${source}`)
  mkdirSync(dirname(target), { recursive: true })
  copyFileSync(source, target)
}

function copyTree(source, target) {
  const stat = lstatSync(source)
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(`Expected a regular directory: ${source}`)
  for (const entry of readdirSync(source, { withFileTypes: true })) {
    const from = resolve(source, entry.name)
    const to = resolve(target, entry.name)
    if (entry.isSymbolicLink()) throw new Error(`Symbolic links are not staged: ${from}`)
    if (entry.isDirectory()) copyTree(from, to)
    else if (entry.isFile()) copyRegularFile(from, to)
  }
}

const sidecarSource = resolve(repoDir, 'python', 'sidecar')
const sidecarTarget = resolve(packageDir, 'python', 'sidecar')
for (const name of ['pyproject.toml', 'README.md']) {
  copyRegularFile(resolve(sidecarSource, name), resolve(sidecarTarget, name))
}
for (const entry of readdirSync(resolve(sidecarSource, 'sidecar'), { withFileTypes: true })) {
  if (entry.isFile() && entry.name.endsWith('.py')) {
    copyRegularFile(resolve(sidecarSource, 'sidecar', entry.name), resolve(sidecarTarget, 'sidecar', entry.name))
  }
}

const consoleSource = resolve(repoDir, 'apps', 'semantic-console')
const consoleTarget = resolve(packageDir, 'python', 'semantic-console')
copyRegularFile(resolve(consoleSource, 'pyproject.toml'), resolve(consoleTarget, 'pyproject.toml'))
for (const entry of readdirSync(resolve(consoleSource, 'server'), { withFileTypes: true })) {
  if (entry.isFile() && (entry.name.endsWith('.py') || ['README.md', 'openapi.json'].includes(entry.name))) {
    copyRegularFile(resolve(consoleSource, 'server', entry.name), resolve(consoleTarget, 'server', entry.name))
  }
}
copyTree(resolve(consoleWebDir, 'dist'), resolve(packageDir, 'semantic-console-web'))
const reactPackage = consoleRequire.resolve('react/package.json')
const reactDomPackage = consoleRequire.resolve('react-dom/package.json')
const schedulerPackage = createRequire(reactDomPackage).resolve('scheduler/package.json')
for (const [name, packageJson] of [
  ['react', reactPackage],
  ['react-dom', reactDomPackage],
  ['scheduler', schedulerPackage],
  ['phosphor-icons-react', consoleRequire.resolve('@phosphor-icons/react/package.json')],
  ['xyflow-react', consoleRequire.resolve('@xyflow/react/package.json')],
  ['i18next', consoleRequire.resolve('i18next/package.json')],
  ['react-i18next', consoleRequire.resolve('react-i18next/package.json')],
]) {
  copyRegularFile(
    resolve(dirname(packageJson), 'LICENSE'),
    resolve(packageDir, 'semantic-console-web', 'licenses', name, 'LICENSE'),
  )
}
mkdirSync(packageDir, { recursive: true })
copyFileSync(resolve(repoDir, 'LICENSE'), resolve(packageDir, 'LICENSE'))
copyFileSync(resolve(repoDir, 'THIRD_PARTY_NOTICES.md'), resolve(packageDir, 'THIRD_PARTY_NOTICES.md'))
console.log(`Built standalone SemaRail Core package in ${packageDir}`)
