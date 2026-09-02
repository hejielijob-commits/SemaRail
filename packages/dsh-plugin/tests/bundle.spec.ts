import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const packageDir = join(dirname(fileURLToPath(import.meta.url)), '..')

describe('SemaRail DeepSeek Harness plugin', () => {
  it('registers one thin Host/Client package', () => {
    const patch = readFileSync(join(packageDir, 'cordis.patch.yml'), 'utf8')
    expect(patch).toContain('id: semarail-harness-host')
    expect(patch).toContain("name: '@hejielijob/dsh-semarail-plugin'")
  })

  it('does not require the Harness subprocess service', () => {
    const manifest = JSON.parse(readFileSync(join(packageDir, 'package.json'), 'utf8'))
    expect(manifest.peerDependencies?.['@deepseek-ai/dsh-subprocess']).toBeUndefined()
    expect(manifest.dsh?.client?.platform).toBe('web')
  })

  it('builds the Client intermediate before requesting an artifact-only bundle', () => {
    const buildScript = readFileSync(join(packageDir, 'scripts', 'build.mjs'), 'utf8')
    const clientBuild = "node(resolve(clientDir, 'scripts', 'build.mjs'))"
    const compileIndex = buildScript.indexOf(clientBuild)
    const artifactOnlyIndex = buildScript.indexOf("DSH_CLIENT_ARTIFACT_ONLY: '1'")

    expect(compileIndex).toBeGreaterThan(-1)
    expect(artifactOnlyIndex).toBeGreaterThan(compileIndex)
  })
})
