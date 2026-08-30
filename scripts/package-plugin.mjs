/** Build one self-contained DeepSeek Harness plugin tarball for distribution. */
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { basename, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repository = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const bundle = resolve(repository, 'packages', 'bundle')
const destination = resolve(repository, 'dist')
const manifest = JSON.parse(readFileSync(resolve(bundle, 'package.json'), 'utf8'))
const pnpmEntry = process.env.npm_execpath

if (pnpmEntry === undefined) {
  throw new Error('package:plugin must run through pnpm')
}
if (manifest.name !== '@hejielijob/dsh-wren-data-agent' || typeof manifest.version !== 'string') {
  throw new Error('Unexpected SemaRail distribution package identity')
}

mkdirSync(destination, { recursive: true })
execFileSync(process.execPath, [
  pnpmEntry,
  'pack',
  '--pack-destination',
  destination,
  '--config.registry=https://registry.npmjs.org',
], {
  cwd: bundle,
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
const checksum = `${artifact}.sha256`
writeFileSync(checksum, `${digest}  ${basename(artifact)}\n`, 'utf8')
process.stdout.write(`\nSemaRail plugin package: ${artifact}\n`)
process.stdout.write(`SHA-256: ${digest}\n`)
process.stdout.write(`Checksum file: ${checksum}\n`)
process.stdout.write(`Install: dsh plugin --profile web add ${artifact}\n`)
