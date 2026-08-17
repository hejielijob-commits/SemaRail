import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const tsc = resolve(packageDir, 'node_modules/typescript/bin/tsc')

rmSync(resolve(packageDir, 'lib'), { recursive: true, force: true })
rmSync(resolve(packageDir, '.build'), { recursive: true, force: true })

execFileSync(process.execPath, [tsc, '-p', 'tsconfig.json'], { cwd: packageDir, stdio: 'inherit' })
execFileSync(process.execPath, [tsc, '-p', 'tsconfig.client.json'], { cwd: packageDir, stdio: 'inherit' })

const body = readFileSync(resolve(packageDir, '.build/client-cjs/index.js'), 'utf8')
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
