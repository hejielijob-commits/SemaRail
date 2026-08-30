/** Build the one-tarball Harness distribution.
 *
 * The Bundle is deliberately dual-faced: its root export is the Host Cordis
 * plugin and `./client` is the generated browser plugin.  Contract code is
 * bundled into the Host so installing this package never asks npm for one of
 * SemaRail's unpublished workspace packages.
 */
import { execFileSync } from 'node:child_process'
import { copyFileSync, cpSync, mkdirSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoDir = resolve(packageDir, '..', '..')
const hostDir = resolve(repoDir, 'packages', 'host')
const clientDir = resolve(repoDir, 'packages', 'client')
const contractDir = resolve(repoDir, 'packages', 'contract')
const consoleWebDir = resolve(repoDir, 'apps', 'semantic-console', 'web')
const clientRequire = createRequire(resolve(clientDir, 'package.json'))
const { build } = clientRequire('esbuild')
const tsc = clientRequire.resolve('typescript/bin/tsc')
const consoleRequire = createRequire(resolve(consoleWebDir, 'package.json'))
const vite = resolve(dirname(consoleRequire.resolve('vite/package.json')), 'bin', 'vite.js')

function node(script, args = [], options = {}) {
  execFileSync(process.execPath, [script, ...args], {
    cwd: options.cwd ?? repoDir,
    stdio: 'inherit',
    ...(options.env === undefined ? {} : { env: options.env }),
  })
}

rmSync(resolve(packageDir, 'lib'), { recursive: true, force: true })
rmSync(resolve(packageDir, 'python'), { recursive: true, force: true })
rmSync(resolve(packageDir, 'runtime'), { recursive: true, force: true })
rmSync(resolve(packageDir, 'semantic-console-web'), { recursive: true, force: true })
rmSync(resolve(packageDir, 'LICENSE'), { force: true })
rmSync(resolve(packageDir, 'THIRD_PARTY_NOTICES.md'), { force: true })
mkdirSync(resolve(packageDir, 'lib'), { recursive: true })

// Host staging owns the reviewed Python/SPA allowlists. Reusing it prevents
// this distribution layer from silently growing a second packaging policy.
node(tsc, ['-p', resolve(contractDir, 'tsconfig.json')])
node(resolve(hostDir, 'scripts', 'stage-sidecar.mjs'))
node(vite, ['build'], { cwd: consoleWebDir })
node(resolve(hostDir, 'scripts', 'stage-semantic-console.mjs'))
node(resolve(clientDir, 'scripts', 'build.mjs'))
node(resolve(clientDir, 'scripts', 'build.mjs'), [], {
  env: {
    ...process.env,
    DSH_CLIENT_ARTIFACT_ONLY: '1',
    DSH_CLIENT_PLUGIN_ID: '@hejielijob/dsh-wren-data-agent',
    DSH_CLIENT_OUTPUT_PATH: resolve(packageDir, 'lib', 'client.js'),
  },
})

await build({
  entryPoints: [resolve(hostDir, 'src', 'index.ts')],
  outfile: resolve(packageDir, 'lib', 'index.js'),
  bundle: true,
  format: 'esm',
  platform: 'node',
  target: 'node22',
  sourcemap: false,
  legalComments: 'eof',
  external: [
    '@deepseek-ai/cordis',
    '@deepseek-ai/dsh-subprocess',
    '@deepseek-ai/dsh-system-prompt',
    '@deepseek-ai/dsh-tools',
  ],
})

cpSync(resolve(hostDir, 'python'), resolve(packageDir, 'python'), { recursive: true })
cpSync(resolve(hostDir, 'runtime'), resolve(packageDir, 'runtime'), { recursive: true })
cpSync(resolve(hostDir, 'semantic-console-web'), resolve(packageDir, 'semantic-console-web'), { recursive: true })
copyFileSync(resolve(repoDir, 'LICENSE'), resolve(packageDir, 'LICENSE'))
copyFileSync(resolve(repoDir, 'THIRD_PARTY_NOTICES.md'), resolve(packageDir, 'THIRD_PARTY_NOTICES.md'))
