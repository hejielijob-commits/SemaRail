import type { DataAgentErrorCode, JsonValue } from '@hejielijob/dsh-wren-data-agent-contract'

/** Default sidecar frame bound; it matches the Python sidecar's limit. */
export const DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024

/** A transport framing failure with a stable contract code. */
export class SidecarFrameError extends Error {
  readonly code: Extract<DataAgentErrorCode, 'FRAME_TOO_LARGE' | 'TRUNCATED_FRAME' | 'PROTOCOL_ERROR'>

  constructor(
    code: Extract<DataAgentErrorCode, 'FRAME_TOO_LARGE' | 'TRUNCATED_FRAME' | 'PROTOCOL_ERROR'>,
  ) {
    super('The Wren sidecar returned an invalid frame.')
    this.name = 'SidecarFrameError'
    this.code = code
  }
}

function validateMaxFrameBytes(value: number): number {
  if (!Number.isSafeInteger(value) || value < 1 || value > 0xffff_ffff) {
    throw new RangeError('maxFrameBytes must be an integer between 1 and 4294967295')
  }
  return value
}

/** Encode one JSON object with a four-byte unsigned big-endian length prefix. */
export function encodeSidecarFrame(value: JsonValue, maxFrameBytes = DEFAULT_MAX_FRAME_BYTES): Buffer {
  const max = validateMaxFrameBytes(maxFrameBytes)
  let json: string | undefined
  try {
    json = JSON.stringify(value)
  } catch {
    throw new SidecarFrameError('PROTOCOL_ERROR')
  }
  if (json === undefined) throw new SidecarFrameError('PROTOCOL_ERROR')
  const payload = Buffer.from(json, 'utf8')
  if (payload.byteLength > max) throw new SidecarFrameError('FRAME_TOO_LARGE')
  const frame = Buffer.allocUnsafe(4 + payload.byteLength)
  frame.writeUInt32BE(payload.byteLength, 0)
  payload.copy(frame, 4)
  return frame
}

/** Incremental decoder for arbitrary stdout chunk boundaries. */
export class SidecarFrameDecoder {
  private readonly maxFrameBytes: number
  private buffer = Buffer.alloc(0)

  constructor(maxFrameBytes = DEFAULT_MAX_FRAME_BYTES) {
    this.maxFrameBytes = validateMaxFrameBytes(maxFrameBytes)
  }

  /** Push a child stdout chunk and return every complete decoded JSON object. */
  push(chunk: Uint8Array): unknown[] {
    if (chunk.byteLength === 0) return []
    this.buffer = this.buffer.length === 0
      ? Buffer.from(chunk)
      : Buffer.concat([this.buffer, Buffer.from(chunk)])
    const messages: unknown[] = []
    while (this.buffer.length >= 4) {
      const payloadBytes = this.buffer.readUInt32BE(0)
      if (payloadBytes > this.maxFrameBytes) throw new SidecarFrameError('FRAME_TOO_LARGE')
      const frameBytes = 4 + payloadBytes
      if (this.buffer.length < frameBytes) break
      const payload = this.buffer.subarray(4, frameBytes)
      this.buffer = this.buffer.subarray(frameBytes)
      let decoded: unknown
      try {
        decoded = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(payload)) as unknown
      } catch {
        throw new SidecarFrameError('PROTOCOL_ERROR')
      }
      if (typeof decoded !== 'object' || decoded === null || Array.isArray(decoded)) {
        throw new SidecarFrameError('PROTOCOL_ERROR')
      }
      messages.push(decoded)
    }
    return messages
  }

  /** Reject a cleanly closed stream that ends halfway through a frame. */
  finish(): void {
    if (this.buffer.length !== 0) throw new SidecarFrameError('TRUNCATED_FRAME')
  }

  /** Clear buffered bytes after a child restart. */
  reset(): void {
    this.buffer = Buffer.alloc(0)
  }
}
