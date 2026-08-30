"""Supervised stdin/stdout loop for the sidecar RPC protocol."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import BinaryIO

from .dispatch import Dispatcher
from .errors import DATABASE_ERROR, FRAME_TOO_LARGE, RpcError, TRUNCATED_FRAME
from .protocol import PROTOCOL_VERSION, FramingError, read_frame, write_frame
from .query import MAX_QUERY_CONCURRENCY


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
        # ``query.run`` is the one potentially blocking operation.  It is
        # dispatched to a worker so a following ``query.cancel`` frame can be
        # read and handled concurrently.  All other methods remain inline,
        # preserving deterministic ordering for health/validation probes.
        output_lock = threading.Lock()
        pending: list[Future[dict[str, object]]] = []

        def write_response(response: dict[str, object]) -> None:
            with output_lock:
                try:
                    if self.max_frame_bytes is None:
                        write_frame(stdout, response)
                    else:
                        write_frame(stdout, response, max_frame_bytes=self.max_frame_bytes)
                except (BrokenPipeError, OSError):
                    self.logger.info("sidecar output closed")

        def finish(future: Future[dict[str, object]]) -> None:
            try:
                response = future.result()
            except Exception:
                # Dispatcher is expected to normalize all failures.  Keep a
                # final process-level guard in case an injected dispatcher
                # violates that contract.
                response = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "id": "",
                    "ok": False,
                    "error": RpcError(
                        code="INTERNAL_ERROR",
                        phase="dispatch",
                        message="internal sidecar error",
                        retryable=False,
                    ).as_dict(),
                }
            write_response(response)

        # This is intentionally the same hard limit as the DB executor.  The
        # server rejects a third run instead of queueing it behind two active
        # database calls, keeping advertised and actual concurrency equal.
        executor = ThreadPoolExecutor(
            max_workers=MAX_QUERY_CONCURRENCY,
            thread_name_prefix="sidecar-query",
        )
        active_lock = threading.Lock()
        active_runs = 0

        def reserve_run() -> bool:
            nonlocal active_runs
            with active_lock:
                if active_runs >= MAX_QUERY_CONCURRENCY:
                    return False
                active_runs += 1
                return True

        def release_run() -> None:
            nonlocal active_runs
            with active_lock:
                active_runs = max(0, active_runs - 1)

        def concurrency_error(request: dict[str, object]) -> dict[str, object]:
            request_id = request.get("id") if isinstance(request.get("id"), str) else ""
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": RpcError(
                    code=DATABASE_ERROR,
                    phase="concurrency",
                    message="query concurrency limit reached",
                    retryable=True,
                ).as_dict(),
            }

        try:
            while True:
                try:
                    if self.max_frame_bytes is None:
                        request = read_frame(stdin)
                    else:
                        request = read_frame(stdin, max_frame_bytes=self.max_frame_bytes)
                except FramingError as exc:
                    self.logger.warning("invalid sidecar frame: %s", exc.code)
                    self._write_error(stdout, exc, output_lock=output_lock)
                    # The whole declared payload has been consumed for malformed
                    # JSON/object frames. For a truncated frame, read_frame has
                    # reached EOF. A size violation cannot safely be resynchronized
                    # because its declared payload was intentionally not consumed.
                    if exc.code in {FRAME_TOO_LARGE, TRUNCATED_FRAME}:
                        break
                    continue

                if request is None:
                    break

                if request.get("method") == "query.run":
                    if not reserve_run():
                        write_response(concurrency_error(request))
                        continue
                    try:
                        future = executor.submit(self.dispatcher.dispatch, request)
                    except Exception:
                        release_run()
                        write_response(
                            {
                                "protocolVersion": PROTOCOL_VERSION,
                                "id": request.get("id")
                                if isinstance(request.get("id"), str)
                                else "",
                                "ok": False,
                                "error": RpcError(
                                    code="INTERNAL_ERROR",
                                    phase="dispatch",
                                    message="internal sidecar error",
                                    retryable=False,
                                ).as_dict(),
                            }
                        )
                        continue
                    pending.append(future)

                    def done(completed: Future[dict[str, object]]) -> None:
                        try:
                            finish(completed)
                        finally:
                            release_run()

                    future.add_done_callback(done)
                else:
                    write_response(self.dispatcher.dispatch(request))
        finally:
            # Do not let the process close stdout before a query result has
            # been emitted.  Cancellation has already been serviced inline.
            for future in pending:
                try:
                    future.result()
                except Exception:
                    pass
            # All workers are joined before the process returns; callbacks
            # take the same output lock, so no response can interleave with a
            # shutdown write or be emitted after stdout is torn down.
            executor.shutdown(wait=True)

    def _write_error(
        self,
        stdout: BinaryIO,
        exc: FramingError,
        *,
        output_lock: threading.Lock | None = None,
    ) -> None:
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
        lock = output_lock or threading.Lock()
        with lock:
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
