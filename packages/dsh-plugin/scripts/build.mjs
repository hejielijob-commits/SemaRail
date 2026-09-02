import { execFileSync } from 'node:child_process'
import { copyFileSync, mkdirSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoDir = resolve(packageDir, '..', '..')
const hostDir = resolve(repoDir, 'packages', 'host')
const clientDir = resolve(repoDir, 'packages', 'client')
const contractDir = resolve(repoDir, 'packages', 'contract')
const clientRequire = createRequire(resolve(clientDir, 'package.json'))
const { build } = clientRequire('esbuild')
const tsc = clientRequire.resolve('typescript/bin/tsc')

function node(script, args = [], options = {}) {
  execFileSync(process.execPath, [script, ...args], {
    cwd: options.cwd ?? repoDir,
    stdio: 'inherit',
    ...(options.env === undefined ? {} : { env: options.env }),
  })
}

rmSync(resolve(packageDir, 'lib'), { recursive: true, force: true })
for (const name of ['python', 'runtime', 'semantic-console-web']) {
  rmSync(resolve(packageDir, name), { recursive: true, force: true })
}
mkdirSync(resolve(packageDir, 'lib'), { recursive: true })
node(tsc, ['-p', resolve(contractDir, 'tsconfig.json')])
// A clean checkout has no Client CommonJS intermediate. Build it explicitly
// before the artifact-only pass instead of relying on a previous workspace
// build or a developer's stale .build directory.
node(resolve(clientDir, 'scripts', 'build.mjs'))
node(resolve(clientDir, 'scripts', 'build.mjs'), [], {
  env: {
    ...process.env,
    DSH_CLIENT_ARTIFACT_ONLY: '1',
    DSH_CLIENT_PLUGIN_ID: '@hejielijob/dsh-semarail-plugin',
    DSH_CLIENT_OUTPUT_PATH: resolve(packageDir, 'lib', 'client.js'),
  },
})
await build({
  entryPoints: [resolve(hostDir, 'src', 'remote-entry.ts')],
  outfile: resolve(packageDir, 'lib', 'index.js'),
  bundle: true,
  format: 'esm',
  platform: 'node',
  target: 'node22',
  sourcemap: false,
  legalComments: 'eof',
  external: [
    '@deepseek-ai/cordis',
    '@deepseek-ai/dsh-system-prompt',
    '@deepseek-ai/dsh-tools',
  ],
})
copyFileSync(resolve(repoDir, 'LICENSE'), resolve(packageDir, 'LICENSE'))
copyFileSync(resolve(repoDir, 'THIRD_PARTY_NOTICES.md'), resolve(packageDir, 'THIRD_PARTY_NOTICES.md'))
console.log(`Built thin DeepSeek Harness plugin in ${packageDir}`)
