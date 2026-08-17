"""Length-prefixed JSON framing for the sidecar's stdin/stdout boundary."""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from typing import Any, BinaryIO

from .errors import FRAME_TOO_LARGE, PROTOCOL_ERROR, TRUNCATED_FRAME


PROTOCOL_VERSION = "1"
LENGTH_PREFIX_BYTES = 4
# The sidecar itself does not create large result previews yet. A bounded
# frame prevents an accidental or hostile length prefix from allocating
# unbounded memory while leaving room for the Host's result limits.
DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024
_LENGTH = struct.Struct(">I")


class FramingError(Exception):
    """A malformed or incomplete length-prefixed JSON frame."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# A descriptive alias for callers that prefer protocol terminology.
ProtocolError = FramingError


def _json_bytes(message: Mapping[str, Any]) -> bytes:
    if not isinstance(message, Mapping):
        raise FramingError(PROTOCOL_ERROR, "message must be a JSON object")
    try:
        encoded = json.dumps(
            dict(message),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        # Do not expose the exception's repr: it can contain a value supplied
        # by a caller and must not become a protocol diagnostic.
        raise FramingError(PROTOCOL_ERROR, "message is not JSON serializable") from exc
    return encoded


def encode_frame(
    message: Mapping[str, Any],
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> bytes:
    """Encode a JSON object as a four-byte big-endian framed message."""

    payload = _json_bytes(message)
    if len(payload) > max_frame_bytes:
        raise FramingError(FRAME_TOO_LARGE, "message exceeds the maximum frame size")
    return _LENGTH.pack(len(payload)) + payload


def decode_payload(payload: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Decode one already-unframed UTF-8 JSON object."""

    try:
        decoded = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FramingError(PROTOCOL_ERROR, "payload is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise FramingError(PROTOCOL_ERROR, "payload must be a JSON object")
    return decoded


def decode_frame(
    frame: bytes | bytearray | memoryview,
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> dict[str, Any]:
    """Decode exactly one complete framed message.

    ``read_frame`` is more efficient for streams; this helper is convenient
    for contract tests and adapters that already have one frame in memory.
    """

    raw = bytes(frame)
    if len(raw) < LENGTH_PREFIX_BYTES:
        raise FramingError(TRUNCATED_FRAME, "frame length prefix is incomplete")
    (size,) = _LENGTH.unpack(raw[:LENGTH_PREFIX_BYTES])
    if size > max_frame_bytes:
        raise FramingError(FRAME_TOO_LARGE, "message exceeds the maximum frame size")
    payload = raw[LENGTH_PREFIX_BYTES:]
    if len(payload) != size:
        raise FramingError(TRUNCATED_FRAME, "frame payload length does not match prefix")
    return decode_payload(payload)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if chunk is None:
            chunk = b""
        if isinstance(chunk, str):
            raise FramingError(PROTOCOL_ERROR, "binary stream returned text")
        if not chunk:
            break
        chunks.append(bytes(chunk))
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(
    stream: BinaryIO,
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> dict[str, Any] | None:
    """Read one frame, returning ``None`` only for clean stream EOF."""

    prefix = _read_exact(stream, LENGTH_PREFIX_BYTES)
    if not prefix:
        return None
    if len(prefix) != LENGTH_PREFIX_BYTES:
        raise FramingError(TRUNCATED_FRAME, "frame length prefix is incomplete")
    (size,) = _LENGTH.unpack(prefix)
    if size > max_frame_bytes:
        raise FramingError(FRAME_TOO_LARGE, "message exceeds the maximum frame size")
    payload = _read_exact(stream, size)
    if len(payload) != size:
        raise FramingError(TRUNCATED_FRAME, "frame payload is incomplete")
    return decode_payload(payload)


def write_frame(
    stream: BinaryIO,
    message: Mapping[str, Any],
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> None:
    """Write one framed message and flush it immediately."""

    stream.write(encode_frame(message, max_frame_bytes=max_frame_bytes))
    stream.flush()


# Compatibility aliases make the boundary vocabulary explicit to adapters.
encode_message = encode_frame
decode_message = decode_frame
read_message = read_frame
write_message = write_frame

