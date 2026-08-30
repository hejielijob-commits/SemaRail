/** Prove that npm pack emits a self-contained Harness plugin package. */
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const pnpmEntry = process.env.npm_execpath
if (pnpmEntry === undefined) throw new Error('verify-pack must run through pnpm')

const packDir = mkdtempSync(resolve(tmpdir(), 'semarail-pack-'))
let files
try {
  execFileSync(process.execPath, [pnpmEntry, 'pack', '--pack-destination', packDir], {
    cwd: packageDir,
    stdio: 'ignore',
    env: {
      ...process.env,
      npm_lifecycle_event: 'verify-pack',
      PNPM_CONFIG_REGISTRY: 'https://registry.npmjs.org',
      npm_config_registry: 'https://registry.npmjs.org',
    },
  })
  const tarball = readdirSync(packDir).find(file => file.endsWith('.tgz'))
  if (tarball === undefined) throw new Error('pnpm pack did not create a tarball')
  files = execFileSync('tar', ['-tf', resolve(packDir, tarball)], { encoding: 'utf8' })
    .split(/\r?\n/u)
    .filter(Boolean)
    .map(path => path.replace(/^package\//u, ''))
} finally {
  rmSync(packDir, { recursive: true, force: true })
}

for (const required of [
  'cordis.patch.yml',
  'LICENSE',
  'THIRD_PARTY_NOTICES.md',
  'lib/index.js',
  'lib/client.js',
  'runtime/bootstrap.py',
  'runtime/constraints.txt',
  'python/sidecar/sidecar/server.py',
  'python/semantic-console/server/app.py',
  'semantic-console-web/index.html',
]) {
  if (!files.includes(required)) throw new Error(`single-package tarball omitted ${required}`)
}

const manifest = JSON.parse(readFileSync(resolve(packageDir, 'package.json'), 'utf8'))
for (const internal of [
  '@hejielijob/dsh-wren-data-agent-host',
  '@hejielijob/dsh-wren-data-agent-client',
  '@hejielijob/dsh-wren-data-agent-contract',
]) {
  if (manifest.dependencies?.[internal] !== undefined) {
    throw new Error(`single-package tarball still depends on unpublished ${internal}`)
  }
}

const client = readFileSync(resolve(packageDir, 'lib', 'client.js'), 'utf8')
if (!client.includes('id: "@hejielijob/dsh-wren-data-agent"')) {
  throw new Error('Client artifact does not register under the installed Bundle package name')
}

const unpublishedPattern = /@hejielijob\/dsh-wren-data-agent-(?:host|client|contract)/u
for (const file of files.filter(path => /\.(?:js|d\.ts)$/u.test(path))) {
  const body = readFileSync(resolve(packageDir, file), 'utf8')
  if (unpublishedPattern.test(body)) {
    throw new Error(`${file} still references an unpublished SemaRail workspace package`)
  }
}

process.stdout.write('single-package tarball contains Host, Client, Sidecar, and Semantic Console assets\n')
