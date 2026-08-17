from __future__ import annotations

import io
import json
import struct
import unittest

from sidecar.protocol import (
    DEFAULT_MAX_FRAME_BYTES,
    FramingError,
    decode_frame,
    encode_frame,
    read_frame,
    write_frame,
)


class ProtocolTests(unittest.TestCase):
    def test_round_trip_uses_four_byte_big_endian_utf8_length(self) -> None:
        message = {"text": "你好", "number": 3, "nested": {"ok": True}}
        frame = encode_frame(message)
        self.assertEqual(frame[:4], struct.pack(">I", len(frame) - 4))
        self.assertEqual(frame[4:].decode("utf-8"), '{"text":"你好","number":3,"nested":{"ok":true}}')
        self.assertEqual(decode_frame(frame), message)

    def test_read_and_write_multiple_frames(self) -> None:
        stream = io.BytesIO()
        write_frame(stream, {"id": "1"})
        write_frame(stream, {"id": "2"})
        stream.seek(0)
        self.assertEqual(read_frame(stream), {"id": "1"})
        self.assertEqual(read_frame(stream), {"id": "2"})
        self.assertIsNone(read_frame(stream))

    def test_partial_prefix_and_payload_are_rejected(self) -> None:
        with self.assertRaises(FramingError) as prefix:
            read_frame(io.BytesIO(b"\x00\x00"))
        self.assertEqual(prefix.exception.code, "TRUNCATED_FRAME")

        frame = encode_frame({"id": "1"})
        with self.assertRaises(FramingError) as payload:
            read_frame(io.BytesIO(frame[:-1]))
        self.assertEqual(payload.exception.code, "TRUNCATED_FRAME")

    def test_invalid_json_and_non_object_are_rejected(self) -> None:
        invalid = struct.pack(">I", 3) + b"no!"
        with self.assertRaises(FramingError) as invalid_json:
            read_frame(io.BytesIO(invalid))
        self.assertEqual(invalid_json.exception.code, "PROTOCOL_ERROR")

        array = json.dumps([1, 2]).encode("utf-8")
        with self.assertRaises(FramingError) as non_object:
            read_frame(io.BytesIO(struct.pack(">I", len(array)) + array))
        self.assertEqual(non_object.exception.code, "PROTOCOL_ERROR")

    def test_frame_size_is_bounded(self) -> None:
        with self.assertRaises(FramingError) as too_large:
            encode_frame({"text": "x"}, max_frame_bytes=1)
        self.assertEqual(too_large.exception.code, "FRAME_TOO_LARGE")
        oversized_prefix = struct.pack(">I", DEFAULT_MAX_FRAME_BYTES + 1)
        with self.assertRaises(FramingError) as oversized:
            read_frame(io.BytesIO(oversized_prefix))
        self.assertEqual(oversized.exception.code, "FRAME_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()

