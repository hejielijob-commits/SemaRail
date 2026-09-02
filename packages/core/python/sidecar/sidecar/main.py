"""Executable entry point for the dependency-free sidecar process."""

from __future__ import annotations

import logging
import sys
from typing import BinaryIO, Sequence

from .dispatch import Dispatcher, SidecarDependencies
from .server import serve
from .wren_adapter import default_dependencies


def _configure_logging(level: str = "WARNING") -> logging.Logger:
    logger = logging.getLogger("sidecar")
    logger.setLevel(getattr(logging, level.upper(), logging.WARNING))
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _prepare_query_workers(dependencies: SidecarDependencies) -> None:
    """Load native-backed query dependencies before worker threads exist."""

    prepared: set[int] = set()
    for service in (dependencies.query_service, dependencies.query_runner):
        if service is None or id(service) in prepared:
            continue
        prepare = getattr(service, "prepare_for_worker_threads", None)
        if callable(prepare):
            prepare()
        prepared.add(id(service))


def run(
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    dependencies: SidecarDependencies | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Run the protocol loop with explicit streams and optional dependencies."""

    active_logger = logger or logging.getLogger("sidecar")
    if dependencies is None:
        # One adapter instance backs both methods so context import/version
        # discovery is lazy and cached for the process lifetime.
        dependencies = default_dependencies(logger=active_logger)
    _prepare_query_workers(dependencies)
    serve(
        stdin,
        stdout,
        Dispatcher(dependencies, logger=active_logger),
        logger=active_logger,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Start the sidecar using the process's binary stdin/stdout streams."""

    # Keep CLI parsing intentionally tiny: stdout remains reserved for frames.
    level = "WARNING"
    args = list(argv if argv is not None else sys.argv[1:])
    if args:
        if len(args) == 2 and args[0] == "--log-level":
            level = args[1]
        else:
            print("usage: python -m sidecar [--log-level LEVEL]", file=sys.stderr)
            return 2
    logger = _configure_logging(level)
    stdin = getattr(sys.stdin, "buffer", sys.stdin)
    stdout = getattr(sys.stdout, "buffer", sys.stdout)
    run(stdin, stdout, logger=logger)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by process tests
    raise SystemExit(main())
