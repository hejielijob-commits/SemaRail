import { describe, expect, it, vi } from 'vitest'
import type {
  SubprocessHandle,
  SubprocessRuntime,
  SubprocessSpawnSpec,
} from '@deepseek-ai/dsh-subprocess'
import {
  SEMANTIC_CONSOLE_PORT,
  SemanticConsoleProcess,
  packagedSemanticConsoleDirectory,
  packagedSemanticConsoleWebDirectory,
} from '../src/semantic-console.ts'

function fakeHandle(): SubprocessHandle & {
  terminate: ReturnType<typeof vi.fn>
  waitForExit: ReturnType<typeof vi.fn>
} {
  return {
    pid: 41,
    stdin: undefined,
    stdout: undefined,
    stderr: undefined,
    collected: {},
    done: new Promise(() => undefined),
    terminate: vi.fn(),
    waitForExit: vi.fn(async () => true),
  }
}

describe('Semantic Console managed process', () => {
  it('spawns the packaged server shell-free on loopback and awaits teardown', async () => {
    const specs: SubprocessSpawnSpec[] = []
    const handle = fakeHandle()
    const runtime = {
      spawn(spec: SubprocessSpawnSpec) {
        specs.push(spec)
        return handle
      },
    } as unknown as SubprocessRuntime
    const process = new SemanticConsoleProcess(runtime, {
      pythonExecutable: 'C:\\Python311\\python.exe',
      projectDir: 'D:\\projects\\retail',
    })

    process.start()
    process.start()

    expect(specs).toHaveLength(1)
    expect(specs[0]).toMatchObject({
      cwd: packagedSemanticConsoleDirectory(),
      stdio: {
        stdin: 'ignore',
        stdout: { maxBytes: 32 * 1024 },
        stderr: { maxBytes: 32 * 1024 },
      },
      env: {
        SEMANTIC_CONSOLE_ORIGINS: `http://127.0.0.1:${SEMANTIC_CONSOLE_PORT},http://localhost:${SEMANTIC_CONSOLE_PORT}`,
        PYTHONDONTWRITEBYTECODE: '1',
      },
    })
    expect(specs[0]?.argv).toEqual([
      'C:\\Python311\\python.exe',
      '-m', 'server',
      '--host', '127.0.0.1',
      '--port', String(SEMANTIC_CONSOLE_PORT),
      '--project-dir', 'D:\\projects\\retail',
      '--static-dir', packagedSemanticConsoleWebDirectory(),
    ])

    await process.dispose()
    expect(handle.terminate).toHaveBeenCalledOnce()
    expect(handle.waitForExit).toHaveBeenCalledOnce()
  })
})
