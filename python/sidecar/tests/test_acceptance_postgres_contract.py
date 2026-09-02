from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path


class PostgreSQLAcceptanceContractTests(unittest.TestCase):
    def test_runtime_dsn_is_normalized_to_semarail_environment(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        script = runpy.run_path(str(repository / "scripts" / "acceptance-postgres.py"))

        normalize = script["_runtime_process_env"]
        environment = normalize(
            {"WREN_DATABASE_URL": "postgresql://legacy.invalid/old"},
            "postgresql://runtime.invalid/current",
        )

        self.assertEqual(
            environment["SEMARAIL_DATABASE_URL"],
            "postgresql://runtime.invalid/current",
        )
        self.assertEqual(
            environment["WREN_DATABASE_URL"],
            "postgresql://legacy.invalid/old",
        )

    def test_runtime_project_is_an_isolated_copy(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        script = runpy.run_path(str(repository / "scripts" / "acceptance-postgres.py"))

        prepare = script["_prepare_project_fixture"]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "wren_project.yml").write_text("name: fixture\n", encoding="utf-8")

            prepared = prepare(source, root / "run")

            self.assertEqual(prepared, (root / "run" / "project").resolve())
            self.assertEqual(
                (prepared / "wren_project.yml").read_text(encoding="utf-8"),
                "name: fixture\n",
            )
            self.assertNotEqual(prepared, source.resolve())


if __name__ == "__main__":
    unittest.main()
