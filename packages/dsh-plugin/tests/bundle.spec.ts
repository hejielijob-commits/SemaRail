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
})
