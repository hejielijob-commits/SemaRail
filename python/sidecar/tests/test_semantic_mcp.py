from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from sidecar.semantic_mcp import main


class SemanticMcpEntrypointTests(unittest.TestCase):
    def test_delegates_to_pinned_semantic_runtime_without_database_connection(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_app(**kwargs: object) -> None:
            calls.append(kwargs)

        wren_package = types.ModuleType("wren")
        wren_cli = types.ModuleType("wren.cli")
        wren_cli.app = fake_app  # type: ignore[attr-defined]

        with patch.dict(sys.modules, {"wren": wren_package, "wren.cli": wren_cli}):
            result = main(["--project", "C:/semantic/project"])

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [{
                "args": ["serve", "mcp", "--project", "C:/semantic/project", "--no-connect"],
                "prog_name": "semarail-mcp",
                "standalone_mode": False,
            }],
        )


if __name__ == "__main__":
    unittest.main()
