import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const command = process.platform === 'win32' ? (process.env.ComSpec ?? 'cmd.exe') : 'npm'
const args = process.platform === 'win32'
  ? ['/d', '/s', '/c', 'npm.cmd pack --dry-run --json --ignore-scripts']
  : ['pack', '--dry-run', '--json', '--ignore-scripts']
const result = spawnSync(command, args, { cwd: packageDir, encoding: 'utf8', windowsHide: true })
if (result.error !== undefined || result.status !== 0) throw result.error ?? new Error('npm pack failed')
const report = JSON.parse(result.stdout)
const files = report[0]?.files?.map(file => file.path) ?? []
for (const file of ['lib/index.js', 'lib/client.js', 'cordis.patch.yml', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md']) {
  if (!files.includes(file)) throw new Error(`Harness plugin omitted ${file}`)
}
const forbidden = files.filter(file => /^(?:python|runtime|semantic-console-web)\//u.test(file) || file.includes('/tests/'))
if (forbidden.length) throw new Error(`Harness plugin embedded Core/test files: ${forbidden.join(', ')}`)
const manifest = JSON.parse(readFileSync(resolve(packageDir, 'package.json'), 'utf8'))
if (manifest.peerDependencies?.['@deepseek-ai/dsh-subprocess'] !== undefined) {
  throw new Error('Thin Harness plugin must not depend on dsh-subprocess')
}
const host = readFileSync(resolve(packageDir, 'lib', 'index.js'), 'utf8')
if (/(?:bootstrap\.py|semantic-console-web|sidecar\/server\.py|dsh-subprocess)/u.test(host)) {
  throw new Error('Thin Host bundle still embeds Core bootstrap, server, or subprocess code')
}
console.log(`Thin DeepSeek Harness plugin verification passed (${files.length} files).`)
