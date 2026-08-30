import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = join(dirname(fileURLToPath(import.meta.url)), '..')

describe('Wren Data Agent Bundle', () => {
  it('ships one dual-face Loader row so Host apply runs only once', () => {
    const patch = readFileSync(join(packageDir, 'cordis.patch.yml'), 'utf8')
    expect(patch).toContain("id: wren-data-agent-host")
    expect(patch).toContain("name: '@hejielijob/dsh-wren-data-agent'")
    expect(patch).not.toContain('id: wren-data-agent-client')
    expect(patch.match(/name: '@hejielijob\/dsh-wren-data-agent'/g)).toHaveLength(1)
  })

  it('is a dual-face bundle without unpublished runtime dependencies', () => {
    const manifest = JSON.parse(readFileSync(join(packageDir, 'package.json'), 'utf8')) as {
      dsh?: { bundle?: { patch?: string }, client?: { platform?: string } }
      dependencies?: Record<string, string>
      exports?: Record<string, unknown>
    }
    expect(manifest.dsh?.bundle?.patch).toBe('./cordis.patch.yml')
    expect(manifest.dsh?.client?.platform).toBe('web')
    expect(manifest.exports?.['./client']).toBeDefined()
    expect(manifest.dependencies?.['@hejielijob/dsh-wren-data-agent-host']).toBeUndefined()
    expect(manifest.dependencies?.['@hejielijob/dsh-wren-data-agent-client']).toBeUndefined()
    expect(manifest.dependencies?.['@hejielijob/dsh-wren-data-agent-contract']).toBeUndefined()
  })
})
