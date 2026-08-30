import { spawn as nodeSpawn, type ChildProcessWithoutNullStreams, type SpawnOptions } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { fileURLToPath } from 'node:url'
import type { Readable, Writable } from 'node:stream'
import { scrubbedParentEnv } from '@deepseek-ai/dsh-subprocess'
import type { SubprocessHandle, SubprocessRuntime, SubprocessSpawnSpec } from '@deepseek-ai/dsh-subprocess'
import {
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_ROWS,
  MAX_QUERY_ROWS,
  parseDataQueryInput,
  parseDataQueryPresentation,
  parseRpcRequest,
  parseRpcResponse,
  parseSemanticContext,
  parseSemanticContextInput,
  PROTOCOL_VERSION,
  type DataAgentErrorCode,
  type DataQueryInput,
  type JsonValue,
  type RpcMethod,
  type RpcResponse,
  type SemanticContextInput,
} from '@hejielijob/dsh-wren-data-agent-contract'
import { SidecarFrameError, SidecarFrameDecoder, DEFAULT_MAX_FRAME_BYTES, encodeSidecarFrame } from './framing.js'
import {
  QueryGatewayError,
  SemanticContextGatewayError,
  type QueryGateway,
  type SemanticContextGateway,
} from './types.js'
import type { SemanticConsoleConfig } from './semantic-console.js'

const DEFAULT_TIMEOUT_MS = 30_000
const DEFAULT_STARTUP_TIMEOUT_MS = 10_000
// A cold Windows install of Wren plus database drivers can take several
// minutes on a slow package mirror; subsequent starts reuse the marker.
const DEFAULT_BOOTSTRAP_STARTUP_TIMEOUT_MS = 900_000
const DEFAULT_CANCEL_GRACE_MS = 500
const DEFAULT_MAX_STDERR_BYTES = 32 * 1024
const DEFAULT_DSN_ENV = 'SEMARAIL_DATABASE_URL'

/**
 * Resolve the sidecar directory shipped beside the compiled Host module.
 *
 * Both src/sidecar.ts (tests) and lib/sidecar.js (the published package) are
 * one directory below the package root, so this URL remains valid in either
 * execution mode.  The build stages python/sidecar there before packing.
 */
export function packagedSidecarDirectory(): string {
  return fileURLToPath(new URL('../python/sidecar/', import.meta.url))
}

/** Bootstrap bundled beside both packaged Python applications. */
export function packagedPythonBootstrap(): string {
  return fileURLToPath(new URL('../runtime/bootstrap.py', import.meta.url))
}

type StableTransportCode = Extract<
  DataAgentErrorCode,
  | 'SIDECAR_UNAVAILABLE'
  | 'TIMEOUT'
  | 'CANCELLED'
  | 'FRAME_TOO_LARGE'
  | 'TRUNCATED_FRAME'
  | 'PROTOCOL_ERROR'
>

/** Stable RPC error without retaining adapter diagnostics. */
export class SidecarRpcError extends Error {
  readonly code: DataAgentErrorCode
  readonly retryable: boolean

  constructor(code: DataAgentErrorCode, retryable: boolean) {
    super('The SemaRail sidecar request failed.')
    this.name = 'SidecarRpcError'
    this.code = code
    this.retryable = retryable
  }
}

/** A safe process-boundary error. Child stderr and exception text are omitted. */
export class SidecarProcessError extends Error {
  readonly code: StableTransportCode
  readonly retryable: boolean

  constructor(code: StableTransportCode = 'SIDECAR_UNAVAILABLE', retryable = true) {
    super('The SemaRail sidecar process is unavailable.')
    this.name = 'SidecarProcessError'
    this.code = code
    this.retryable = retryable
  }
}

/** Injectable child-process shape used by focused transport tests. */
export interface SidecarChildProcess {
  readonly stdin: Writable
  readonly stdout: Readable
  readonly stderr: Readable | undefined
  readonly exitCode: number | null
  readonly killed: boolean
  on(event: string, listener: (...args: any[]) => void): this
  once(event: string, listener: (...args: any[]) => void): this
  kill(signal?: NodeJS.Signals | number): boolean
}

/** Spawn seam for tests; production uses Node's shell-free `spawn`. */
export type SidecarSpawn = (
  executable: string,
  args: readonly string[],
  options: SpawnOptions,
) => SidecarChildProcess

/** Adapt the rc.7 Harness-managed subprocess Service Definition to this transport. */
class HarnessSubprocessChild extends EventEmitter implements SidecarChildProcess {
  readonly stdin: Writable
  readonly stdout: Readable
  readonly stderr = undefined
  private terminated = false
  private outcome: { readonly exitCode: number | null } | undefined

  constructor(private readonly handle: SubprocessHandle) {
    super()
    if (handle.stdin === undefined || handle.stdout === undefined) {
      throw new SidecarProcessError('SIDECAR_UNAVAILABLE', true)
    }
    this.stdin = handle.stdin
    this.stdout = handle.stdout
    void handle.done.then(
      value => {
        this.outcome = { exitCode: value.exitCode }
        this.emit('close')
      },
      () => {
        this.emit('error', new SidecarProcessError('SIDECAR_UNAVAILABLE', true))
        this.emit('close')
      },
    )
  }

  get exitCode(): number | null {
    return this.outcome?.exitCode ?? null
  }

  get killed(): boolean {
    return this.terminated
  }

  kill(): boolean {
    if (!this.terminated) {
      this.terminated = true
      this.handle.terminate()
    }
    return true
  }
}

/**
 * Build a production spawner from `ctx.subprocess`. The Service Definition
 * owns process-tree teardown and credential-scrubbed inherited environment;
 * this package only owns the framed protocol on its pipe streams.
 */
export function createSubprocessSidecarSpawn(
  runtime: SubprocessRuntime,
  options: Pick<SidecarGatewayConfig, 'workingDirectory' | 'cancelGraceMs' | 'maxStderrBytes'> = {},
): SidecarSpawn {
  return (executable, args, spawnOptions) => {
    const spec: SubprocessSpawnSpec = {
      argv: [executable, ...args],
      cwd: options.workingDirectory ?? packagedSidecarDirectory(),
      stdio: {
        stdin: 'pipe',
        stdout: 'pipe',
        stderr: { maxBytes: options.maxStderrBytes ?? DEFAULT_MAX_STDERR_BYTES },
      },
      graceMs: options.cancelGraceMs ?? DEFAULT_CANCEL_GRACE_MS,
      ...(spawnOptions.env === undefined ? {} : { env: spawnOptions.env }),
    }
    return new HarnessSubprocessChild(runtime.spawn(spec))
  }
}

/** Options for the supervised Python sidecar and its non-secret boundary. */
export interface SidecarGatewayConfig extends SemanticConsoleConfig {
  /** Python executable, defaulting to `python`. */
  readonly pythonExecutable?: string
  /** Set false only when the configured Python already owns all dependencies. */
  readonly pythonBootstrapEnabled?: boolean
  /** Python module passed to `python -m`, defaulting to `sidecar`. */
  readonly sidecarModule?: string
  /** Working directory used for the child process. */
  readonly workingDirectory?: string
  /** Wren project directory sent only to the sidecar. */
  readonly projectDir?: string
  /** Per-request query/context timeout. */
  readonly timeoutMs?: number
  /** Startup health/validation timeout. */
  readonly startupTimeoutMs?: number
  /** Grace period after query.cancel before terminating the child. */
  readonly cancelGraceMs?: number
  /** Maximum accepted or emitted framed payload. */
  readonly maxFrameBytes?: number
  /** Maximum stderr bytes counted from the child; bytes are never exposed. */
  readonly maxStderrBytes?: number
  /** Name of the environment variable from which the sidecar reads the DSN. */
  readonly databaseDsnEnv?: string
  /** Optional test-only spawn seam. */
  readonly spawn?: SidecarSpawn
}

export interface SidecarRequestOptions {
  readonly signal?: AbortSignal
  readonly timeoutMs?: number
  readonly cancelOnAbort?: boolean
}

interface PendingRequest {
  readonly id: string
  readonly method: RpcMethod
  /** Sidecar query identity. This is deliberately distinct from the RPC id. */
  readonly queryId?: string
  readonly resolve: (value: JsonValue) => void
  readonly reject: (reason: unknown) => void
  readonly signal?: AbortSignal
  readonly cancelOnAbort: boolean
  settled: boolean
  timer?: ReturnType<typeof setTimeout>
  abortHandler?: () => void
  /** Grace timer while a cancelled query.run is still draining in the child. */
  cancelGraceTimer?: ReturnType<typeof setTimeout>
}

function numberOption(value: number | undefined, fallback: number, name: string, maximum = 2_147_483_647): number {
  const resolved = value ?? fallback
  if (!Number.isSafeInteger(resolved) || resolved < 1 || resolved > maximum) {
    throw new RangeError(`${name} must be a positive integer`)
  }
  return resolved
}

function optionalString(value: string | undefined, fallback: string, name: string): string {
  const resolved = value ?? fallback
  if (typeof resolved !== 'string' || resolved.trim().length === 0) throw new RangeError(`${name} must be a non-empty string`)
  return resolved
}

function safeTransportError(error: unknown, fallback: StableTransportCode = 'SIDECAR_UNAVAILABLE'): SidecarProcessError | SidecarRpcError {
  if (error instanceof SidecarProcessError || error instanceof SidecarRpcError) return error
  if (error instanceof SidecarFrameError) return new SidecarProcessError(error.code, false)
  return new SidecarProcessError(fallback, true)
}

/** Read the sidecar query identity without confusing it with the RPC id. */
function queryIdFromParams(params: JsonValue): string | undefined {
  if (typeof params !== 'object' || params === null || Array.isArray(params)) return undefined
  const candidate = (params as Record<string, JsonValue>).queryId
  return typeof candidate === 'string' && candidate.length > 0 ? candidate : undefined
}

/** Supervised concurrent framed-RPC client for one sidecar process. */
export class SidecarRpcClient {
  private readonly pythonExecutable: string
  private readonly pythonBootstrapEnabled: boolean
  private readonly sidecarModule: string
  private readonly workingDirectory: string | undefined
  private readonly timeoutMs: number
  private readonly cancelGraceMs: number
  private readonly maxFrameBytes: number
  private readonly maxStderrBytes: number
  private readonly spawn: SidecarSpawn
  private readonly decoder: SidecarFrameDecoder
  private child: SidecarChildProcess | undefined
  private starting: Promise<void> | undefined
  private disposed = false
  private stdoutEnded = false
  private stderrBytes = 0
  private nextIdValue = 0
  private readonly pending = new Map<string, PendingRequest>()
  private readonly tombstones = new Set<string>()

  constructor(options: SidecarGatewayConfig = {}) {
    this.pythonExecutable = optionalString(options.pythonExecutable, 'python', 'pythonExecutable')
    this.pythonBootstrapEnabled = options.pythonBootstrapEnabled !== false
    this.sidecarModule = optionalString(options.sidecarModule, 'sidecar', 'sidecarModule')
    this.workingDirectory = options.workingDirectory === undefined
      ? packagedSidecarDirectory()
      : optionalString(options.workingDirectory, '', 'workingDirectory')
    this.timeoutMs = numberOption(options.timeoutMs, DEFAULT_TIMEOUT_MS, 'timeoutMs')
    this.cancelGraceMs = numberOption(options.cancelGraceMs, DEFAULT_CANCEL_GRACE_MS, 'cancelGraceMs')
    this.maxFrameBytes = numberOption(options.maxFrameBytes, DEFAULT_MAX_FRAME_BYTES, 'maxFrameBytes', 0xffff_ffff)
    this.maxStderrBytes = numberOption(options.maxStderrBytes, DEFAULT_MAX_STDERR_BYTES, 'maxStderrBytes')
    this.spawn = options.spawn ?? ((executable, args, spawnOptions) => nodeSpawn(executable, [...args], spawnOptions) as unknown as SidecarChildProcess)
    this.decoder = new SidecarFrameDecoder(this.maxFrameBytes)
  }

  /** Start the child lazily; repeated callers share the same start promise. */
  async start(): Promise<void> {
    if (this.disposed) throw new SidecarProcessError('SIDECAR_UNAVAILABLE', false)
    if (this.isLive()) return
    if (this.starting !== undefined) return this.starting
    const start = Promise.resolve().then(() => this.spawnChild())
    const wrapped = start.finally(() => {
      if (this.starting === wrapped) this.starting = undefined
    })
    this.starting = wrapped
    return wrapped
  }

  /** Send one strict version-one request and correlate its response by id. */
  async request(method: RpcMethod, params: JsonValue, options: SidecarRequestOptions = {}): Promise<JsonValue> {
    return this.requestInternal(method, params, options)
  }

  /** Whether the current child is still usable for a request. */
  isLive(): boolean {
    return this.isChildLive()
  }

  /** Dispose the child and fail all still-pending calls safely. */
  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.failAll(new SidecarProcessError('SIDECAR_UNAVAILABLE', false))
    this.terminateChild()
  }

  private isChildLive(): boolean {
    const child = this.child
    return child !== undefined && child.exitCode === null && !child.killed
  }

  private spawnChild(): void {
    if (this.disposed) throw new SidecarProcessError('SIDECAR_UNAVAILABLE', false)
    if (this.isChildLive()) return
    const spawnOptions: SpawnOptions = {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      // Runtime configuration may name the DSN variable but cannot inject
      // arbitrary environment values (and therefore cannot embed credentials
      // in a Cordis/profile config). The value is inherited only from the
      // already-scrubbed process environment.
      env: scrubbedParentEnv(),
    }
    spawnOptions.cwd = this.workingDirectory
    let child: SidecarChildProcess
    try {
      const args = this.pythonBootstrapEnabled
        ? [packagedPythonBootstrap(), '--', '-m', this.sidecarModule]
        : ['-m', this.sidecarModule]
      child = this.spawn(this.pythonExecutable, args, spawnOptions)
    } catch {
      throw new SidecarProcessError('SIDECAR_UNAVAILABLE', true)
    }
    this.child = child
    this.stdoutEnded = false
    this.stderrBytes = 0
    child.stdout.on('data', (chunk: Buffer | Uint8Array | string) => {
      if (typeof chunk === 'string') this.handleStdout(Buffer.from(chunk, 'utf8'))
      else this.handleStdout(chunk)
    })
    child.stdout.once('end', () => {
      this.stdoutEnded = true
    })
    child.stderr?.on('data', (chunk: Buffer | Uint8Array | string) => {
      const bytes = typeof chunk === 'string' ? Buffer.byteLength(chunk, 'utf8') : chunk.byteLength
      this.stderrBytes = Math.min(this.maxStderrBytes, this.stderrBytes + bytes)
    })
    child.once('error', () => {
      this.failChild(child, 'SIDECAR_UNAVAILABLE')
    })
    child.once('close', () => {
      const code: StableTransportCode = this.stdoutEnded ? 'SIDECAR_UNAVAILABLE' : 'TRUNCATED_FRAME'
      this.failChild(child, code)
    })
  }

  private handleStdout(chunk: Uint8Array): void {
    try {
      for (const value of this.decoder.push(chunk)) this.handleResponse(value)
    } catch (error: unknown) {
      const safe = safeTransportError(error, 'PROTOCOL_ERROR')
      this.failAll(safe)
      this.terminateChild()
    }
  }

  private handleResponse(value: unknown): void {
    let response: RpcResponse
    try {
      response = parseRpcResponse(value)
    } catch {
      throw new SidecarFrameError('PROTOCOL_ERROR')
    }
    const pending = this.pending.get(response.id)
    if (pending === undefined) {
      if (this.tombstones.has(response.id)) return
      throw new SidecarFrameError('PROTOCOL_ERROR')
    }
    this.pending.delete(response.id)
    this.clearPending(pending)
    if (pending.settled) return
    pending.settled = true
    if (response.ok) pending.resolve(response.result)
    else pending.reject(new SidecarRpcError(response.error.code, response.error.retryable))
  }

  private async requestInternal(
    method: RpcMethod,
    params: JsonValue,
    options: SidecarRequestOptions,
    ensureStarted = true,
  ): Promise<JsonValue> {
    if (this.disposed) throw new SidecarProcessError('SIDECAR_UNAVAILABLE', false)
    if (options.signal?.aborted) throw new SidecarRpcError('CANCELLED', false)
    if (ensureStarted) await this.start()
    if (!this.isChildLive()) throw new SidecarProcessError('SIDECAR_UNAVAILABLE', true)
    const id = this.nextId(method)
    const timeoutMs = numberOption(options.timeoutMs, this.timeoutMs, 'request timeoutMs')
    const cancelOnAbort = options.cancelOnAbort ?? (method === 'query.run' || method === 'context.ask')
    const rawRequest: Record<string, unknown> = {
      protocolVersion: PROTOCOL_VERSION,
      id,
      method,
      params,
      deadlineMs: timeoutMs,
    }
    let request
    try {
      request = parseRpcRequest(rawRequest)
    } catch {
      throw new SidecarRpcError('INVALID_PARAMS', false)
    }
    return new Promise<JsonValue>((resolve, reject) => {
      const queryId = method === 'query.run' ? queryIdFromParams(params) : undefined
      const pending: PendingRequest = {
        id,
        method,
        ...(queryId === undefined ? {} : { queryId }),
        resolve,
        reject,
        cancelOnAbort,
        settled: false,
        ...(options.signal === undefined ? {} : { signal: options.signal }),
      }
      if (options.signal !== undefined) {
        const abortHandler = () => this.abortPending(pending, 'CANCELLED')
        pending.abortHandler = abortHandler
        options.signal.addEventListener('abort', abortHandler, { once: true })
      }
      pending.timer = setTimeout(() => this.abortPending(pending, 'TIMEOUT'), timeoutMs)
      this.pending.set(id, pending)
      // A signal may be aborted between the preflight check and listener
      // installation. Keep cancellation deterministic in that race.
      if (options.signal?.aborted) this.abortPending(pending, 'CANCELLED')
      void this.writeRequest(request).catch(() => {
        const current = this.pending.get(id)
        if (current !== undefined) {
          this.pending.delete(id)
          this.clearPending(current)
          if (!current.settled) {
            current.settled = true
            current.reject(new SidecarProcessError('SIDECAR_UNAVAILABLE', true))
          }
        }
        this.terminateChild()
      })
    })
  }

  private async writeRequest(request: { readonly protocolVersion: typeof PROTOCOL_VERSION; readonly id: string; readonly method: RpcMethod; readonly params: JsonValue; readonly deadlineMs?: number }): Promise<void> {
    const child = this.child
    if (child === undefined || !this.isLive()) throw new SidecarProcessError('SIDECAR_UNAVAILABLE', true)
    const frame = encodeSidecarFrame(request as unknown as JsonValue, this.maxFrameBytes)
    await new Promise<void>((resolve, reject) => {
      try {
        child.stdin.write(frame, error => error == null ? resolve() : reject(new Error('write failed')))
      } catch {
        reject(new Error('write failed'))
      }
    })
  }

  private abortPending(pending: PendingRequest, code: 'CANCELLED' | 'TIMEOUT'): void {
    if (pending.settled) return
    pending.settled = true
    this.clearCallerPending(pending)
    pending.reject(new SidecarRpcError(code, code === 'TIMEOUT'))
    // Only query.run has a sidecar-level cancellation identity. In
    // particular, never send query.cancel with a transport RPC id or with an
    // undefined query id for health/context/startup requests.
    if (!pending.cancelOnAbort || pending.method !== 'query.run' || pending.queryId === undefined) {
      this.pending.delete(pending.id)
      this.tombstones.add(pending.id)
      this.terminateChild()
      return
    }

    // Keep the original query.run entry until its response arrives. A
    // query.cancel acknowledgement only says that the cancellation request
    // was accepted; it does not prove the running query has stopped. If the
    // original response does not arrive during the grace period, terminate
    // this managed child so the next request starts a clean process.
    pending.cancelGraceTimer = setTimeout(() => {
      if (this.pending.get(pending.id) === pending) this.terminateChild()
    }, this.cancelGraceMs)
    void this.requestInternal('query.cancel', { queryId: pending.queryId }, {
      timeoutMs: this.cancelGraceMs,
      cancelOnAbort: false,
    }, false).catch(() => {
      // The grace timer above owns the final decision. A failed cancel request
      // must not accidentally spawn a replacement child for this old query.
    })
  }

  private clearPending(pending: PendingRequest): void {
    if (pending.timer !== undefined) clearTimeout(pending.timer)
    if (pending.cancelGraceTimer !== undefined) clearTimeout(pending.cancelGraceTimer)
    if (pending.abortHandler !== undefined && pending.signal !== undefined) {
      pending.signal.removeEventListener('abort', pending.abortHandler)
    }
  }

  /** Clear only the caller deadline/cancellation hooks, retaining the RPC. */
  private clearCallerPending(pending: PendingRequest): void {
    if (pending.timer !== undefined) {
      clearTimeout(pending.timer)
    }
    if (pending.abortHandler !== undefined && pending.signal !== undefined) {
      pending.signal.removeEventListener('abort', pending.abortHandler)
    }
  }

  private failChild(child: SidecarChildProcess, code: StableTransportCode): void {
    if (this.child !== child) return
    this.child = undefined
    let effectiveCode = code
    try {
      this.decoder.finish()
    } catch {
      effectiveCode = 'TRUNCATED_FRAME'
    }
    const error = new SidecarProcessError(effectiveCode, effectiveCode === 'SIDECAR_UNAVAILABLE')
    this.failAll(error)
    this.tombstones.clear()
    this.decoder.reset()
  }

  private failAll(error: SidecarProcessError | SidecarRpcError): void {
    for (const pending of this.pending.values()) {
      this.clearPending(pending)
      if (!pending.settled) {
        pending.settled = true
        pending.reject(error)
      }
    }
    this.pending.clear()
  }

  private terminateChild(): void {
    const child = this.child
    if (child === undefined) return
    this.failAll(new SidecarProcessError('SIDECAR_UNAVAILABLE', true))
    this.child = undefined
    this.tombstones.clear()
    this.decoder.reset()
    try {
      if (!child.killed && child.exitCode === null) child.kill('SIGTERM')
    } catch {
      // Process termination is best-effort; callers already receive a stable error.
    }
  }

  private nextId(prefix: string): string {
    this.nextIdValue = (this.nextIdValue + 1) % 0x3fff_ffff
    return `wren-${prefix.replace(/[^a-zA-Z0-9]/g, '-')}-${this.nextIdValue.toString(36)}`
  }
}

function asGatewayError(error: unknown, context: boolean): QueryGatewayError | SemanticContextGatewayError {
  if (context) {
    if (error instanceof SemanticContextGatewayError) return error
    if (error instanceof SidecarRpcError || error instanceof SidecarProcessError) {
      return new SemanticContextGatewayError(error.code, error.retryable)
    }
    return new SemanticContextGatewayError('INTERNAL_ERROR', false)
  }
  if (error instanceof QueryGatewayError) return error
  if (error instanceof SidecarRpcError || error instanceof SidecarProcessError) {
    return new QueryGatewayError(error.code, error.retryable)
  }
  return new QueryGatewayError('INTERNAL_ERROR', false)
}

function objectResult(value: JsonValue): Record<string, JsonValue> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new SidecarRpcError('PROTOCOL_ERROR', false)
  }
  return value as Record<string, JsonValue>
}

function validateHealth(value: JsonValue): void {
  const object = objectResult(value)
  if (object.status !== 'ok' || object.protocolVersion !== PROTOCOL_VERSION) {
    throw new SidecarRpcError('HEALTHCHECK_FAILED', true)
  }
  if (object.wrenAvailable !== undefined && typeof object.wrenAvailable !== 'boolean') {
    throw new SidecarRpcError('PROTOCOL_ERROR', false)
  }
  if (object.wrenVersion !== undefined && object.wrenVersion !== null && typeof object.wrenVersion !== 'string') {
    throw new SidecarRpcError('PROTOCOL_ERROR', false)
  }
}

function validateProject(value: JsonValue): void {
  const object = objectResult(value)
  if (typeof object.valid !== 'boolean' || typeof object.projectRevision !== 'string') {
    throw new SidecarRpcError('PROTOCOL_ERROR', false)
  }
  if (!object.valid) throw new SidecarRpcError('PROJECT_VALIDATION_FAILED', false)
}

/** Real Host-side gateway backed by the Python sidecar process. */
export class SidecarQueryGateway implements QueryGateway, SemanticContextGateway {
  private readonly client: SidecarRpcClient
  private readonly projectDir: string | undefined
  private readonly timeoutMs: number
  private readonly startupTimeoutMs: number
  private readonly databaseDsnEnv: string
  private ready: Promise<void> | undefined
  private queryCounter = 0

  constructor(
    private readonly options: SidecarGatewayConfig = {},
    spawnOverride?: SidecarSpawn,
  ) {
    this.projectDir = typeof options.projectDir === 'string' ? options.projectDir.trim() || undefined : undefined
    this.timeoutMs = numberOption(options.timeoutMs, DEFAULT_TIMEOUT_MS, 'timeoutMs')
    this.startupTimeoutMs = numberOption(
      options.startupTimeoutMs,
      options.pythonBootstrapEnabled === false ? DEFAULT_STARTUP_TIMEOUT_MS : DEFAULT_BOOTSTRAP_STARTUP_TIMEOUT_MS,
      'startupTimeoutMs',
    )
    this.databaseDsnEnv = optionalString(options.databaseDsnEnv, DEFAULT_DSN_ENV, 'databaseDsnEnv')
    this.client = new SidecarRpcClient(spawnOverride === undefined ? options : { ...options, spawn: spawnOverride })
  }

  /** Ensure process health and project validity before any business request. */
  async start(): Promise<void> {
    if (this.projectDir === undefined) throw new QueryGatewayError('WREN_UNAVAILABLE', true)
    // A previous startup promise is valid only for the child that passed both
    // checks. If that managed child crashed, force the next request through
    // health → project.validate before sending business traffic.
    if (this.ready !== undefined && this.client.isLive()) return this.ready
    if (this.ready !== undefined && !this.client.isLive()) this.ready = undefined
    const startup = this.initialize()
    this.ready = startup.catch(error => {
      this.ready = undefined
      throw error
    })
    return this.ready
  }

  async query(input: DataQueryInput, signal: AbortSignal): Promise<ReturnType<typeof parseDataQueryPresentation>> {
    const parsed = parseDataQueryInput(input)
    if (signal.aborted) throw new QueryGatewayError('CANCELLED', false)
    try {
      await this.start()
      const queryId = this.nextQueryId()
      const params: Record<string, JsonValue> = {
        projectDir: this.projectDir as string,
        question: parsed.question,
        semanticSql: parsed.semanticSql,
        queryId,
        maxRows: MAX_QUERY_ROWS,
        previewRows: MAX_PREVIEW_ROWS,
        maxPreviewBytes: MAX_PREVIEW_BYTES,
        databaseDsnEnv: this.databaseDsnEnv,
      }
      if (parsed.chartIntent !== undefined) params.chartIntent = parsed.chartIntent
      const result = await this.client.request('query.run', params, {
        signal,
        timeoutMs: this.timeoutMs,
        cancelOnAbort: true,
      })
      return parseDataQueryPresentation(result)
    } catch (error: unknown) {
      throw asGatewayError(error, false)
    }
  }

  async context(input: SemanticContextInput, signal: AbortSignal): Promise<ReturnType<typeof parseSemanticContext>> {
    const parsed = parseSemanticContextInput(input)
    if (signal.aborted) throw new SemanticContextGatewayError('CANCELLED', false)
    try {
      await this.start()
      const result = await this.client.request('context.ask', {
        projectDir: this.projectDir as string,
        question: parsed.question,
      }, {
        signal,
        timeoutMs: this.timeoutMs,
        cancelOnAbort: true,
      })
      return parseSemanticContext(result)
    } catch (error: unknown) {
      throw asGatewayError(error, true)
    }
  }

  /** Stop the supervised child and release all pending requests. */
  dispose(): void {
    this.client.dispose()
    this.ready = undefined
  }

  private async initialize(): Promise<void> {
    await this.client.request('health', {}, { timeoutMs: this.startupTimeoutMs, cancelOnAbort: false })
      .then(validateHealth)
    await this.client.request('project.validate', { projectDir: this.projectDir as string }, {
      timeoutMs: this.startupTimeoutMs,
      cancelOnAbort: false,
    }).then(validateProject)
  }

  private nextQueryId(): string {
    this.queryCounter = (this.queryCounter + 1) % 0x3fff_ffff
    return `wren-query-${this.queryCounter.toString(36)}`
  }
}

/** Factory kept separate so Cordis `apply` can remain a small plugin entry. */
export function createSidecarQueryGateway(options: SidecarGatewayConfig, spawnOverride?: SidecarSpawn): SidecarQueryGateway {
  return new SidecarQueryGateway(options, spawnOverride)
}
