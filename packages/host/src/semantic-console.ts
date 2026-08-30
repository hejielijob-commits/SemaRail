import { fileURLToPath } from 'node:url'
import type { SubprocessHandle, SubprocessRuntime, SubprocessSpawnSpec } from '@deepseek-ai/dsh-subprocess'
import { packagedPythonBootstrap } from './sidecar.js'

/** Loopback address shared with the Client plugin's global console link. */
export const SEMANTIC_CONSOLE_HOST = '127.0.0.1' as const
/** Stable MVP port shared with the Client plugin. */
export const SEMANTIC_CONSOLE_PORT = 48_763 as const

const DEFAULT_GRACE_MS = 1_000
const MAX_DIAGNOSTIC_BYTES = 32 * 1024

/** Host configuration for the independently supervised Semantic Console. */
export interface SemanticConsoleConfig {
  /** Set false to keep the query tools while disabling the web console. */
  readonly semanticConsoleEnabled?: boolean
}

/** Directory containing the packaged dependency-free Python HTTP server. */
export function packagedSemanticConsoleDirectory(): string {
  return fileURLToPath(new URL('../python/semantic-console/', import.meta.url))
}

/** Directory containing the packaged production SPA. */
export function packagedSemanticConsoleWebDirectory(): string {
  return fileURLToPath(new URL('../semantic-console-web/', import.meta.url))
}

/** Python import root for the sibling packaged query sidecar. */
export function packagedSidecarPythonDirectory(): string {
  return fileURLToPath(new URL('../python/sidecar/', import.meta.url))
}

function requiredString(value: string, name: string): string {
  const normalized = value.trim()
  if (normalized.length === 0) throw new RangeError(`${name} must not be empty`)
  return normalized
}

/**
 * Own the console as a second Harness-managed process.
 *
 * Query RPC and web administration deliberately use separate children: a UI
 * failure cannot corrupt the framed protocol stream or an in-flight query.
 */
export class SemanticConsoleProcess {
  private handle: SubprocessHandle | undefined
  private disposed = false

  constructor(
    private readonly runtime: SubprocessRuntime,
    private readonly options: {
      readonly pythonExecutable: string
      readonly projectDir: string
      readonly pythonBootstrapEnabled?: boolean
    },
  ) {}

  /** Start once. Process creation is shell-free and its output is bounded. */
  start(): void {
    if (this.disposed || this.handle !== undefined) return
    const port = SEMANTIC_CONSOLE_PORT
    const argv = [
      requiredString(this.options.pythonExecutable, 'pythonExecutable'),
      ...(this.options.pythonBootstrapEnabled === false ? [] : [packagedPythonBootstrap(), '--']),
      '-m',
      'server',
      '--host',
      SEMANTIC_CONSOLE_HOST,
      '--port',
      String(port),
      '--project-dir',
      requiredString(this.options.projectDir, 'projectDir'),
      '--static-dir',
      packagedSemanticConsoleWebDirectory(),
    ]
    const origin = `http://${SEMANTIC_CONSOLE_HOST}:${port}`
    const localhostOrigin = `http://localhost:${port}`
    const spec: SubprocessSpawnSpec = {
      argv,
      cwd: packagedSemanticConsoleDirectory(),
      stdio: {
        stdin: 'ignore',
        stdout: { maxBytes: MAX_DIAGNOSTIC_BYTES },
        stderr: { maxBytes: MAX_DIAGNOSTIC_BYTES },
      },
      graceMs: DEFAULT_GRACE_MS,
      env: {
        SEMANTIC_CONSOLE_ORIGINS: `${origin},${localhostOrigin}`,
        PYTHONPATH: packagedSidecarPythonDirectory(),
        PYTHONDONTWRITEBYTECODE: '1',
      },
    }
    const handle = this.runtime.spawn(spec)
    this.handle = handle
    void handle.done.finally(() => {
      if (this.handle === handle) this.handle = undefined
    }).catch(() => undefined)
  }

  /** Terminate only the process tree created by this plugin instance. */
  async dispose(): Promise<void> {
    if (this.disposed) return
    this.disposed = true
    const handle = this.handle
    this.handle = undefined
    if (handle !== undefined) {
      handle.terminate()
      await handle.waitForExit()
    }
  }
}
