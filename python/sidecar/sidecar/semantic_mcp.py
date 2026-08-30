"""SemaRail-branded entry point for the upstream semantic MCP runtime.

The public command belongs to SemaRail while the implementation continues to
delegate to the pinned WrenAI CLI.  Keeping that delegation behind one module
lets SemaRail evolve the semantic surface without changing every agent config.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the SemaRail semantic context over MCP stdio"
    )
    parser.add_argument("--project", required=True, help="fixed semantic project directory")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        from wren.cli import app
    except ImportError:
        parser.error("the semantic runtime is unavailable; install the MCP extra")

    app(
        args=["serve", "mcp", "--project", args.project, "--no-connect"],
        prog_name="semarail-mcp",
        standalone_mode=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by MCP clients
    raise SystemExit(main())
