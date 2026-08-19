import { execFileSync } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const tsc = resolve(packageDir, 'node_modules/typescript/bin/tsc')

rmSync(resolve(packageDir, 'lib'), { recursive: true, force: true })
rmSync(resolve(packageDir, '.build'), { recursive: true, force: true })

execFileSync(process.execPath, [tsc, '-p', 'tsconfig.json'], { cwd: packageDir, stdio: 'inherit' })
execFileSync(process.execPath, [tsc, '-p', 'tsconfig.client.json'], { cwd: packageDir, stdio: 'inherit' })
// The package is ESM, while this intermediate tree is intentionally CJS for
// the rc.7 lazy factory. Mark only the temporary tree as CommonJS so esbuild
// does not reinterpret TypeScript's `exports` assignments as ESM globals.
writeFileSync(resolve(packageDir, '.build/client-cjs/package.json'), '{"type":"commonjs"}\n')

const { build } = await import('esbuild')
const bundled = await build({
  entryPoints: [resolve(packageDir, '.build/client-cjs/index.js')],
  bundle: true,
  format: 'cjs',
  platform: 'browser',
  target: 'es2022',
  minify: true,
  write: false,
  sourcemap: false,
  legalComments: 'eof',
  // Harness supplies React and platform modules through the lazy-CJS factory.
  // ECharts is intentionally bundled into this one browser artifact.
  external: ['react', 'react/*', '@deepseek-ai/dsh-client-ui-tool'],
})
const body = bundled.outputFiles[0]?.text
if (body === undefined) throw new Error('esbuild did not emit a Client artifact')
const pluginId = '@hejielijob/dsh-wren-data-agent-client'
const artifact = [
  '// Generated lazy-CJS Client artifact for DeepSeek Harness 0.1.0-rc.7.',
  '// The loader supplies the platform module table through the factory require.',
  'window.__ModuleLoader__.load({',
  `  id: ${JSON.stringify(pluginId)},`,
  '  factory: (require) => {',
  '    var module = { exports: {} };',
  '    var exports = module.exports;',
  body,
  '    return module.exports;',
  '  },',
  '});',
  '',
].join('\n')

mkdirSync(resolve(packageDir, 'lib'), { recursive: true })
writeFileSync(resolve(packageDir, 'lib/client.js'), artifact)
