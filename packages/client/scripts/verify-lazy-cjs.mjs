/**
 * Execute the generated rc.7 lazy-CJS artifact in a small VM sandbox.
 *
 * This catches the most common packaging regression: a browser bundle that
 * exists on disk but is not actually consumable by Harness's ModuleLoader, or
 * that accidentally inlines React/platform modules instead of ECharts.
 */
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const packageDir = resolve(fileURLToPath(new URL('..', import.meta.url)))
const artifactPath = resolve(packageDir, 'lib/client.js')
const source = readFileSync(artifactPath, 'utf8')
const require = createRequire(import.meta.url)
let loaded
const loader = {
  load(value) {
    loaded = value
  },
}
const sandbox = {
  window: { __ModuleLoader__: loader },
  navigator: { userAgent: 'Mozilla/5.0 (VM lazy-CJS probe)' },
  document: { documentElement: { style: {} } },
  console,
  setTimeout,
  clearTimeout,
}
vm.runInNewContext(source, sandbox, { filename: artifactPath })
if (loaded === undefined || loaded.id !== '@hejielijob/dsh-wren-data-agent-client' || typeof loaded.factory !== 'function') {
  throw new Error('lazy-CJS ModuleLoader payload is missing or malformed')
}

const react = require('react')
const exportsValue = loaded.factory((moduleId) => {
  if (moduleId === 'react') return react
  throw new Error(`unexpected non-external module request: ${moduleId}`)
})
for (const name of ['apply', 'DataQueryRow', 'parseDataQueryMeta', 'buildDataQueryChartOption']) {
  if (typeof exportsValue[name] !== 'function') throw new Error(`lazy-CJS export ${name} is missing`)
}
if (/require\(["']echarts(?:\/|["'])/u.test(source)) throw new Error('ECharts was not bundled into the Client artifact')
if (!/require\(["']react["']\)/u.test(source)) throw new Error('React is not external to the Client artifact')
process.stdout.write(JSON.stringify({
  ok: true,
  id: loaded.id,
  exports: ['apply', 'DataQueryRow', 'parseDataQueryMeta', 'buildDataQueryChartOption'],
  bytes: Buffer.byteLength(source, 'utf8'),
}))
