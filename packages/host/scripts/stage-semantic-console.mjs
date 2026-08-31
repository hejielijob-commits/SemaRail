/** Stage the reviewed Semantic Console server and production SPA for packing. */

import { copyFileSync, lstatSync, mkdirSync, readdirSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repositoryConsole = resolve(packageDirectory, '..', '..', 'apps', 'semantic-console')
const packagedServer = resolve(packageDirectory, 'python', 'semantic-console')
const packagedWeb = resolve(packageDirectory, 'semantic-console-web')
const require = createRequire(import.meta.url)

function assertRegularFile(path) {
  const stat = lstatSync(path)
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`Expected a regular file: ${path}`)
}

function copyFile(source, target) {
  assertRegularFile(source)
  mkdirSync(dirname(target), { recursive: true })
  copyFileSync(source, target)
}

function copyTree(source, target, accept) {
  const sourceStat = lstatSync(source)
  if (!sourceStat.isDirectory() || sourceStat.isSymbolicLink()) {
    throw new Error(`Expected a regular directory: ${source}`)
  }
  for (const entry of readdirSync(source, { withFileTypes: true })) {
    const from = join(source, entry.name)
    const to = join(target, entry.name)
    if (entry.isSymbolicLink()) throw new Error(`Symbolic links are not staged: ${from}`)
    if (entry.isDirectory()) copyTree(from, to, accept)
    else if (entry.isFile() && accept(from)) copyFile(from, to)
  }
}

function main() {
  const sourceServer = resolve(repositoryConsole, 'server')
  const sourceWeb = resolve(repositoryConsole, 'web', 'dist')
  const webRoot = resolve(repositoryConsole, 'web')
  const reactPackage = require.resolve('react/package.json', { paths: [webRoot] })
  const reactDomPackage = require.resolve('react-dom/package.json', { paths: [webRoot] })
  const phosphorPackage = require.resolve('@phosphor-icons/react/package.json', { paths: [webRoot] })
  const xyflowPackage = require.resolve('@xyflow/react/package.json', { paths: [webRoot] })
  const i18nextPackage = require.resolve('i18next/package.json', { paths: [webRoot] })
  const reactI18nextPackage = require.resolve('react-i18next/package.json', { paths: [webRoot] })
  const schedulerPackage = createRequire(reactDomPackage).resolve('scheduler/package.json')
  assertRegularFile(resolve(repositoryConsole, 'pyproject.toml'))
  assertRegularFile(resolve(sourceWeb, 'index.html'))

  rmSync(packagedServer, { recursive: true, force: true })
  rmSync(packagedWeb, { recursive: true, force: true })
  mkdirSync(resolve(packagedServer, 'server'), { recursive: true })
  mkdirSync(packagedWeb, { recursive: true })
  copyFile(resolve(repositoryConsole, 'pyproject.toml'), resolve(packagedServer, 'pyproject.toml'))
  for (const entry of readdirSync(sourceServer, { withFileTypes: true })) {
    // Runtime server modules are a reviewed top-level allowlist. Tests,
    // caches, fixtures, and future nested directories never enter the npm
    // package accidentally.
    if (!entry.isFile() || !entry.name.endsWith('.py')) continue
    copyFile(resolve(sourceServer, entry.name), resolve(packagedServer, 'server', entry.name))
  }
  for (const name of ['README.md', 'openapi.json']) {
    copyFile(resolve(sourceServer, name), resolve(packagedServer, 'server', name))
  }
  copyTree(sourceWeb, packagedWeb, () => true)
  const licenses = [
    ['react', resolve(dirname(reactPackage), 'LICENSE')],
    ['react-dom', resolve(dirname(reactDomPackage), 'LICENSE')],
    ['scheduler', resolve(dirname(schedulerPackage), 'LICENSE')],
    ['phosphor-icons-react', resolve(dirname(phosphorPackage), 'LICENSE')],
    ['xyflow-react', resolve(dirname(xyflowPackage), 'LICENSE')],
    ['i18next', resolve(dirname(i18nextPackage), 'LICENSE')],
    ['react-i18next', resolve(dirname(reactI18nextPackage), 'LICENSE')],
  ]
  for (const [name, source] of licenses) {
    copyFile(source, resolve(packagedWeb, 'licenses', name, 'LICENSE'))
  }

  console.log(`Staged Semantic Console server and SPA in ${packageDirectory}`)
}

main()
