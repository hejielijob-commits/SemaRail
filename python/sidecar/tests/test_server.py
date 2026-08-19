from __future__ import annotations

import io
import threading
import unittest
from typing import Any

from sidecar.dispatch import Dispatcher, SidecarDependencies
from sidecar.errors import CANCELLED, RpcFault
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

    def test_query_cancel_is_read_while_query_run_is_blocked(self) -> None:
        """The protocol-level cancellation path must not wait for run."""

        class BlockingService:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.cancelled = threading.Event()
                self.finished = threading.Event()
                self.allow_finish = threading.Event()

            def run(self, _params: dict[str, Any]) -> dict[str, Any]:
                self.started.set()
                self.cancelled.wait(2)
                self.allow_finish.wait(2)
                self.finished.set()
                raise RpcFault(CANCELLED, "cancel", "query was cancelled")

            def cancel(self, _params: dict[str, Any]) -> dict[str, Any]:
                self.cancelled.set()
                return {"queryId": "q-live", "cancelled": True}

        class GateInput(io.BytesIO):
            def __init__(self, first: bytes, second: bytes, gate: threading.Event) -> None:
                super().__init__(first + second)
                self.cut = len(first)
                self.gate = gate

            def read(self, size: int = -1) -> bytes:
                if self.tell() >= self.cut and not self.gate.is_set():
                    # The query worker sets the gate after the first frame has
                    # been submitted, proving cancel is not merely queued
                    # behind a synchronous dispatch call.
                    self.gate.wait(1)
                return super().read(size)

        class ObservedOutput(io.BytesIO):
            def __init__(self) -> None:
                super().__init__()
                self.cancel_written = threading.Event()

            def write(self, data: bytes) -> int:
                if b'"id":"cancel-rpc"' in data:
                    self.cancel_written.set()
                return super().write(data)

        service = BlockingService()
        first = encode_frame({
            "protocolVersion": "1",
            "id": "run-rpc",
            "method": "query.run",
            "params": {
                "projectDir": ".",
                "question": "q",
                "semanticSql": "SELECT 1",
                "queryId": "q-live",
            },
        })
        second = encode_frame({
            "protocolVersion": "1",
            "id": "cancel-rpc",
            "method": "query.cancel",
            "params": {"queryId": "q-live"},
        })
        incoming = GateInput(first, second, service.started)
        output = ObservedOutput()
        server_thread = threading.Thread(
            target=JsonRpcServer(
                Dispatcher(SidecarDependencies(query_service=service))
            ).serve,
            args=(incoming, output),
        )
        server_thread.start()
        self.assertTrue(service.started.wait(1))
        self.assertTrue(output.cancel_written.wait(1))
        self.assertFalse(service.finished.is_set())
        service.allow_finish.set()
        server_thread.join(2)
        self.assertFalse(server_thread.is_alive())

        output.seek(0)
        responses = [read_frame(output), read_frame(output)]
        by_id = {response["id"]: response for response in responses if response is not None}
        self.assertEqual(by_id["cancel-rpc"]["result"], {"queryId": "q-live", "cancelled": True})
        self.assertEqual(by_id["run-rpc"]["error"]["code"], CANCELLED)


if __name__ == "__main__":
    unittest.main()
