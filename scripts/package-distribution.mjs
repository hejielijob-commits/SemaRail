/** Pack one of the split SemaRail distributions and write a SHA-256 checksum. */
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { basename, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repository = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const selected = process.argv[2]
const expected = {
  core: '@hejielijob/semarail-core',
  'dsh-plugin': '@hejielijob/dsh-semarail-plugin',
}
if (!(selected in expected)) throw new Error('Choose core or dsh-plugin')
const packageDir = resolve(repository, 'packages', selected)
const destination = resolve(repository, 'dist')
const manifest = JSON.parse(readFileSync(resolve(packageDir, 'package.json'), 'utf8'))
const pnpmEntry = process.env.npm_execpath
if (pnpmEntry === undefined) throw new Error('distribution packaging must run through pnpm')
if (manifest.name !== expected[selected] || typeof manifest.version !== 'string') {
  throw new Error('Unexpected distribution package identity')
}
mkdirSync(destination, { recursive: true })
execFileSync(process.execPath, [
  pnpmEntry, 'pack', '--pack-destination', destination,
  '--config.registry=https://registry.npmjs.org',
], {
  cwd: packageDir,
  stdio: 'inherit',
  env: {
    ...process.env,
    PNPM_CONFIG_REGISTRY: 'https://registry.npmjs.org',
    npm_config_registry: 'https://registry.npmjs.org',
  },
})
const filename = `${manifest.name.slice(1).replace('/', '-')}-${manifest.version}.tgz`
const artifact = resolve(destination, filename)
const digest = createHash('sha256').update(readFileSync(artifact)).digest('hex')
writeFileSync(`${artifact}.sha256`, `${digest}  ${basename(artifact)}\n`, 'utf8')
process.stdout.write(`\nPackage: ${artifact}\nSHA-256: ${digest}\n`)
