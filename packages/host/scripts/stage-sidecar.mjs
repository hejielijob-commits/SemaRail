/**
 * Stage the Python sidecar into this package before TypeScript build/pack.
 *
 * npm package file lists cannot include paths outside the package root.  The
 * source sidecar remains at the repository root, so the build copies only the
 * runtime allowlist into packages/host/python/sidecar.  Tests, caches, bytecode,
 * VCS metadata, and arbitrary files are intentionally never copied.
 */

import { copyFileSync, lstatSync, mkdirSync, readdirSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDirectory = dirname(fileURLToPath(import.meta.url))
const repositorySidecar = resolve(scriptDirectory, '..', '..', '..', 'python', 'sidecar')
const packagedSidecar = resolve(scriptDirectory, '..', 'python', 'sidecar')

function assertRegularFile(path, label) {
  const stat = lstatSync(path)
  if (!stat.isFile()) throw new Error(`${label} must be a regular file`)
}

function copyRegularFile(source, target) {
  assertRegularFile(source, source)
  mkdirSync(dirname(target), { recursive: true })
  copyFileSync(source, target)
}

function copyRuntimeSources() {
  const sourcePackage = join(repositorySidecar, 'sidecar')
  const targetPackage = join(packagedSidecar, 'sidecar')
  const entries = readdirSync(sourcePackage, { withFileTypes: true })

  mkdirSync(targetPackage, { recursive: true })
  for (const entry of entries) {
    // The runtime is deliberately an explicit Python-source allowlist.  This
    // excludes tests, __pycache__, .env files, VCS metadata, and future files
    // that have not been reviewed for package distribution.
    if (!entry.isFile() || !entry.name.endsWith('.py')) continue
    copyRegularFile(join(sourcePackage, entry.name), join(targetPackage, entry.name))
  }
}

function main() {
  assertRegularFile(join(repositorySidecar, 'pyproject.toml'), 'pyproject.toml')
  assertRegularFile(join(repositorySidecar, 'README.md'), 'README.md')
  const sourceStat = lstatSync(repositorySidecar)
  if (!sourceStat.isDirectory()) throw new Error(`Sidecar source is not a directory: ${repositorySidecar}`)

  // The destination is package-owned and deterministic.  It is ignored by
  // git and recreated on every build so stale runtime files cannot enter a
  // tarball after a source file is removed.
  rmSync(packagedSidecar, { recursive: true, force: true })
  mkdirSync(packagedSidecar, { recursive: true })
  copyRegularFile(join(repositorySidecar, 'pyproject.toml'), join(packagedSidecar, 'pyproject.toml'))
  copyRegularFile(join(repositorySidecar, 'README.md'), join(packagedSidecar, 'README.md'))
  copyRuntimeSources()

  console.log(`Staged Python sidecar runtime at ${packagedSidecar}`)
}

main()
