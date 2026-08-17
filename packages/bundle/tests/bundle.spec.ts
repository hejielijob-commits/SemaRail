import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = join(dirname(fileURLToPath(import.meta.url)), '..')

describe('Wren Data Agent Bundle', () => {
  it('ships the two public Loader rows', () => {
    const patch = readFileSync(join(packageDir, 'cordis.patch.yml'), 'utf8')
    expect(patch).toContain("id: wren-data-agent-host")
    expect(patch).toContain("name: '@hejielijob/dsh-wren-data-agent-host'")
    expect(patch).toContain("id: wren-data-agent-client")
    expect(patch).toContain("name: '@hejielijob/dsh-wren-data-agent-client'")
  })

  it('declares the Host/Client dependency closure and bundle manifest', () => {
    const manifest = JSON.parse(readFileSync(join(packageDir, 'package.json'), 'utf8')) as {
      dsh?: { bundle?: { patch?: string } }
      dependencies?: Record<string, string>
    }
    expect(manifest.dsh?.bundle?.patch).toBe('./cordis.patch.yml')
    expect(manifest.dependencies?.['@hejielijob/dsh-wren-data-agent-host']).toBe('0.1.0')
    expect(manifest.dependencies?.['@hejielijob/dsh-wren-data-agent-client']).toBe('0.1.0')

    const hostManifest = JSON.parse(readFileSync(join(packageDir, '..', 'host', 'package.json'), 'utf8')) as {
      dependencies?: Record<string, string>
    }
    expect(hostManifest.dependencies?.['@hejielijob/dsh-wren-data-agent-contract']).toBe('workspace:*')
  })
})
