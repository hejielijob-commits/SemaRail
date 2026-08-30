import { fileURLToPath } from 'node:url'
import { spawn as nodeSpawn } from 'node:child_process'
import { PassThrough } from 'node:stream'
import { afterEach, describe, expect, it } from 'vitest'
import {
  QueryGatewayError,
} from '../src/index.ts'
import { SidecarFrameDecoder, encodeSidecarFrame, SidecarFrameError } from '../src/framing.ts'
import {
  createSidecarQueryGateway,
  createSubprocessSidecarSpawn,
  packagedSidecarDirectory,
  packagedPythonBootstrap,
  type SidecarChildProcess,
  type SidecarGatewayConfig,
  type SidecarSpawn,
  SidecarQueryGateway,
  SidecarRpcClient,
} from '../src/sidecar.ts'
import type { SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'

const fixture = fileURLToPath(new URL('./fixtures/fake-sidecar.mjs', import.meta.url))
const gateways: SidecarQueryGateway[] = []
let spawnCount = 0

const fakeSpawn: SidecarSpawn = (_executable, _args, options) => {
  spawnCount += 1
  return nodeSpawn(
    process.execPath,
    [fixture],
    options,
  ) as unknown as SidecarChildProcess
}

function gateway(overrides: SidecarGatewayConfig = {}): SidecarQueryGateway {
  const value = createSidecarQueryGateway({
    projectDir: 'C:\\fixture-project',
    spawn: fakeSpawn,
    ...overrides,
  })
  gateways.push(value)
  return value
}

function input(semanticSql: string) {
  return { question: 'fixture question', semanticSql } as const
}

afterEach(() => {
  for (const value of gateways.splice(0)) value.dispose()
  spawnCount = 0
})

describe('sidecar framing', () => {
  it('uses a four-byte big-endian prefix and accepts arbitrary chunk boundaries', () => {
    const frame = encodeSidecarFrame({ hello: '世界' })
    expect(frame.readUInt32BE(0)).toBe(frame.length - 4)
    const decoder = new SidecarFrameDecoder()
    const values = [
      ...decoder.push(frame.subarray(0, 1)),
      ...decoder.push(frame.subarray(1, 4)),
      ...decoder.push(frame.subarray(4)),
    ]
    expect(values).toEqual([{ hello: '世界' }])
    decoder.finish()
  })

  it('rejects an oversized declared payload before allocation', () => {
    const decoder = new SidecarFrameDecoder(8)
    const prefix = Buffer.alloc(4)
    prefix.writeUInt32BE(9, 0)
    expect(() => decoder.push(prefix)).toThrow(SidecarFrameError)
  })
})

describe('supervised sidecar gateway', () => {
  it('defaults the managed subprocess cwd to the packaged Python runtime', () => {
    const specs: Array<{ argv?: string[] }> = []
    const runtime = {
      spawn(spec: unknown) {
        specs.push(spec)
        const stdin = new PassThrough()
        const stdout = new PassThrough()
        return {
          pid: 1,
          stdin,
          stdout,
          stderr: undefined,
          collected: {},
          done: Promise.resolve({ exitCode: 0, signal: null }),
          terminate() {},
          waitForExit: async () => true,
        }
      },
    } as unknown as SubprocessRuntime
    const spawn = createSubprocessSidecarSpawn(runtime)
    const child = spawn('python', ['-m', 'sidecar'], {
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    expect(specs[0]).toMatchObject({ cwd: packagedSidecarDirectory() })
    expect(specs[0]?.argv).toEqual(['python', '-m', 'sidecar'])
    child.kill()
  })

  it('starts the sidecar through the private-runtime bootstrap by default', () => {
    const calls: Array<{ executable: string, args: readonly string[] }> = []
    const spawn: SidecarSpawn = (executable, args) => {
      calls.push({ executable, args })
      const stdin = new PassThrough()
      const stdout = new PassThrough()
      return {
        stdin, stdout, stderr: new PassThrough(), exitCode: null, killed: false,
        on() { return this }, once() { return this }, kill() { return true },
      }
    }
    const client = new SidecarRpcClient({ spawn })
    return client.start().then(() => {
      expect(calls[0]).toEqual({ executable: 'python', args: [packagedPythonBootstrap(), '--', '-m', 'sidecar'] })
      client.dispose()
    })
  })

  it('allows an explicitly managed Python runtime to bypass bootstrap', () => {
    const calls: Array<{ executable: string, args: readonly string[] }> = []
    const spawn: SidecarSpawn = (executable, args) => {
      calls.push({ executable, args })
      const stdin = new PassThrough()
      const stdout = new PassThrough()
      return {
        stdin, stdout, stderr: new PassThrough(), exitCode: null, killed: false,
        on() { return this }, once() { return this }, kill() { return true },
      }
    }
    const client = new SidecarRpcClient({ pythonBootstrapEnabled: false, spawn })
    return client.start().then(() => {
      expect(calls[0]?.args).toEqual(['-m', 'sidecar'])
      client.dispose()
    })
  })

  it('adapts production startup to the rc.7 managed subprocess spec', () => {
    const specs: unknown[] = []
    let terminated = false
    const runtime = {
      spawn(spec: unknown) {
        specs.push(spec)
        const stdin = new PassThrough()
        const stdout = new PassThrough()
        const handle = {
          pid: 1,
          stdin,
          stdout,
          stderr: undefined,
          collected: {},
          done: Promise.resolve({ exitCode: 0, signal: null }),
          terminate() { terminated = true },
          waitForExit: async () => true,
        }
        return handle
      },
    } as unknown as SubprocessRuntime
    const spawn = createSubprocessSidecarSpawn(runtime, {
      workingDirectory: 'C:\\fixture-project',
      cancelGraceMs: 42,
      maxStderrBytes: 77,
    })
    const child = spawn('python', ['-m', 'sidecar'], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { WREN_DATABASE_URL: 'secret' },
    })
    expect(specs[0]).toMatchObject({
      argv: ['python', '-m', 'sidecar'],
      cwd: 'C:\\fixture-project',
      stdio: {
        stdin: 'pipe',
        stdout: 'pipe',
        stderr: { maxBytes: 77 },
      },
      graceMs: 42,
      env: { WREN_DATABASE_URL: 'secret' },
    })
    child.kill()
    expect(terminated).toBe(true)
  })

  it('runs health then project validation and correlates concurrent query responses', async () => {
    const value = gateway()
    const [late, fast] = await Promise.all([
      value.query(input('LATE'), new AbortController().signal),
      value.query(input('FAST'), new AbortController().signal),
    ])
    expect(late.status).toBe('success')
    expect(late.previewRows[0]?.value).toBe('LATE')
    expect(fast.previewRows[0]?.value).toBe('FAST')
  })

  it('returns a shared-contract SemanticContext from context.ask', async () => {
    const value = gateway()
    const context = await value.context({ question: 'orders' }, new AbortController().signal)
    expect(context.schemaVersion).toBe(1)
    expect(context.models[0]?.name).toBe('orders')
    expect(context.projectRevision).toBe('sha256:fixture')
  })

  it('sends query.cancel on timeout and keeps a healthy process when cancellation is acknowledged', async () => {
    const value = gateway({ timeoutMs: 30, cancelGraceMs: 500 })
    await expect(value.query(input('WAIT'), new AbortController().signal)).rejects.toMatchObject({
      code: 'TIMEOUT',
    })
    const followUp = await value.query(input('FAST'), new AbortController().signal)
    expect(followUp.status).toBe('success')
  })

  it('uses the query.run queryId, not its transport RPC id, for query.cancel', async () => {
    const value = gateway({ timeoutMs: 30, cancelGraceMs: 500 })
    await expect(value.query(input('WAIT'), new AbortController().signal)).rejects.toMatchObject({
      code: 'TIMEOUT',
    })
    const check = await value.query(input('CHECK_CANCEL'), new AbortController().signal)
    expect(check.previewRows[0]?.value).toBe('0')
  })

  it('terminates and restarts the child when cancel acknowledgement does not end query.run', async () => {
    const value = gateway({ timeoutMs: 20, cancelGraceMs: 20 })
    await expect(value.query(input('STUCK'), new AbortController().signal)).rejects.toMatchObject({
      code: 'TIMEOUT',
    })
    await new Promise(resolve => setTimeout(resolve, 60))
    const followUp = await value.query(input('FAST'), new AbortController().signal)
    expect(followUp.status).toBe('success')
    expect(spawnCount).toBe(2)
  })

  it('sends query.cancel on AbortSignal and maps it to CANCELLED', async () => {
    const value = gateway({ timeoutMs: 2_000 })
    const controller = new AbortController()
    const request = value.query(input('WAIT'), controller.signal)
    setTimeout(() => controller.abort(), 10)
    await expect(request).rejects.toMatchObject({ code: 'CANCELLED' })
  })

  it('fails all pending work on child exit without exposing stderr', async () => {
    const value = gateway()
    const error = await value.query(input('EXIT'), new AbortController().signal).catch(reason => reason)
    expect(error).toBeInstanceOf(QueryGatewayError)
    expect(error).toMatchObject({ code: 'SIDECAR_UNAVAILABLE' })
    expect(String(error)).not.toContain('secret')
  })

  it('reruns health and project validation after a crash before the next query', async () => {
    const value = gateway()
    await expect(value.query(input('EXIT'), new AbortController().signal)).rejects.toMatchObject({
      code: 'SIDECAR_UNAVAILABLE',
    })
    const followUp = await value.query(input('FAST'), new AbortController().signal)
    expect(followUp.status).toBe('success')
    expect(spawnCount).toBe(2)
  })

  it('fails closed on malformed framed responses', async () => {
    const value = gateway()
    await expect(value.query(input('BAD'), new AbortController().signal)).rejects.toMatchObject({
      code: 'PROTOCOL_ERROR',
    })
  })

  it('fails closed on a truncated response frame', async () => {
    const value = gateway()
    await expect(value.query(input('TRUNCATED'), new AbortController().signal)).rejects.toMatchObject({
      code: 'TRUNCATED_FRAME',
    })
  })
})
