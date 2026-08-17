from __future__ import annotations

import io
import unittest

from sidecar.dispatch import Dispatcher, SidecarDependencies
from sidecar.protocol import encode_frame, read_frame
from sidecar.server import JsonRpcServer


class ServerTests(unittest.TestCase):
    def test_server_emits_only_framed_protocol_on_stdout(self) -> None:
        validator = lambda params: {"valid": True, "project": params["projectDir"]}
        incoming = b"".join([
            encode_frame({
                "protocolVersion": "1",
                "id": "h",
                "method": "health",
                "params": {},
            }),
            encode_frame({
                "protocolVersion": "1",
                "id": "v",
                "method": "project.validate",
                "params": {"projectDir": "example"},
            }),
        ])
        output = io.BytesIO()
        JsonRpcServer(
            Dispatcher(SidecarDependencies(project_validator=validator))
        ).serve(io.BytesIO(incoming), output)

        output.seek(0)
        health = read_frame(output)
        validation = read_frame(output)
        self.assertEqual(health["id"], "h")
        self.assertEqual(validation["result"], {"valid": True, "project": "example"})
        self.assertIsNone(read_frame(output))

    def test_malformed_frame_gets_structured_protocol_error(self) -> None:
        output = io.BytesIO()
        JsonRpcServer(Dispatcher()).serve(io.BytesIO(b"\x00\x00"), output)
        output.seek(0)
        response = read_frame(output)
        self.assertEqual(response["protocolVersion"], "1")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "TRUNCATED_FRAME")
        # No request id can be recovered from a malformed transport frame.
        # id="" marks an uncorrelated transport fault and is intentionally not
        # passed to the ordinary TS parseRpcResponse correlation path.
        self.assertEqual(response["id"], "")


if __name__ == "__main__":
    unittest.main()
