"""Supervised stdin/stdout loop for the sidecar RPC protocol."""

from __future__ import annotations

import logging
from typing import BinaryIO

from .dispatch import Dispatcher
from .errors import FRAME_TOO_LARGE, RpcError, TRUNCATED_FRAME
from .protocol import PROTOCOL_VERSION, FramingError, read_frame, write_frame


class JsonRpcServer:
    """Serve framed requests until clean EOF or an output failure."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        max_frame_bytes: int | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.max_frame_bytes = max_frame_bytes
        self.logger = logger or logging.getLogger("sidecar.server")

    def serve(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        """Process requests from ``stdin`` and write only frames to ``stdout``."""

        while True:
            try:
                if self.max_frame_bytes is None:
                    request = read_frame(stdin)
                else:
                    request = read_frame(stdin, max_frame_bytes=self.max_frame_bytes)
            except FramingError as exc:
                self.logger.warning("invalid sidecar frame: %s", exc.code)
                self._write_error(stdout, exc)
                # The whole declared payload has been consumed for malformed
                # JSON/object frames. For a truncated frame, read_frame has
                # reached EOF. A size violation cannot safely be resynchronized
                # because its declared payload was intentionally not consumed.
                if exc.code in {FRAME_TOO_LARGE, TRUNCATED_FRAME}:
                    return
                continue

            if request is None:
                return

            response = self.dispatcher.dispatch(request)
            try:
                if self.max_frame_bytes is None:
                    write_frame(stdout, response)
                else:
                    write_frame(stdout, response, max_frame_bytes=self.max_frame_bytes)
            except (BrokenPipeError, OSError):
                self.logger.info("sidecar output closed")
                return

    def _write_error(self, stdout: BinaryIO, exc: FramingError) -> None:
        response = {
            "protocolVersion": PROTOCOL_VERSION,
            "id": "",
            "ok": False,
            "error": RpcError(
                code=exc.code,
                phase="protocol",
                message=exc.message,
                retryable=False,
            ).normalized().as_dict(),
        }
        try:
            if self.max_frame_bytes is None:
                write_frame(stdout, response)
            else:
                write_frame(stdout, response, max_frame_bytes=self.max_frame_bytes)
        except (BrokenPipeError, OSError):
            self.logger.info("sidecar output closed")


def serve(
    stdin: BinaryIO,
    stdout: BinaryIO,
    dispatcher: Dispatcher | None = None,
    *,
    max_frame_bytes: int | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Run a server with an optional injected dispatcher."""

    JsonRpcServer(
        dispatcher or Dispatcher(logger=logger),
        max_frame_bytes=max_frame_bytes,
        logger=logger,
    ).serve(stdin, stdout)
