from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location("semarail_bootstrap", Path(__file__).with_name("bootstrap.py"))
assert _SPEC is not None and _SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_stale_lock_threshold_exceeds_initializer_wait(self) -> None:
        self.assertGreater(bootstrap.LOCK_STALE_SECONDS, bootstrap.LOCK_WAIT_SECONDS)

    def _package_root(self, root: Path) -> Path:
        for relative in ("runtime/constraints.txt", "python/sidecar/pyproject.toml", "python/semantic-console/pyproject.toml"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        return root

    def test_concurrent_initializers_share_one_install_and_reuse_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = self._package_root(root / "package")
            runtime_home = root / "runtime"
            installs = 0
            installs_lock = threading.Lock()

            def fake_install(_package_root: Path, venv: Path, fingerprint: str) -> None:
                nonlocal installs
                with installs_lock:
                    installs += 1
                time.sleep(0.1)
                python = bootstrap._python_in(venv)
                python.parent.mkdir(parents=True)
                python.touch()
                (venv / ".semarail-runtime.json").write_text(
                    bootstrap.json.dumps({"bootstrapVersion": bootstrap.BOOTSTRAP_VERSION, "fingerprint": fingerprint}),
                    encoding="utf-8",
                )

            results: list[Path] = []
            with patch.object(bootstrap, "_runtime_home", return_value=runtime_home), patch.object(bootstrap, "_install", side_effect=fake_install):
                threads = [threading.Thread(target=lambda: results.append(bootstrap.ensure_runtime(package_root))) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
                results.append(bootstrap.ensure_runtime(package_root))

            self.assertEqual(installs, 1)
            self.assertEqual(len(set(results)), 1)

    def test_install_suppresses_child_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = self._package_root(root / "package")
            venv = root / "venv"

            class FakeBuilder:
                def __init__(self, **_kwargs: object) -> None: pass
                def create(self, target: Path) -> None:
                    bootstrap._python_in(Path(target)).parent.mkdir(parents=True)
                    bootstrap._python_in(Path(target)).touch()

            with patch("venv.EnvBuilder", FakeBuilder), patch.object(bootstrap.subprocess, "run") as run:
                run.return_value.returncode = 1
                with self.assertRaisesRegex(RuntimeError, "could not be installed"):
                    bootstrap._install(package_root, venv, "fingerprint")
            kwargs = run.call_args.kwargs
            self.assertIs(kwargs["stdout"], bootstrap.subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], bootstrap.subprocess.DEVNULL)
            self.assertIs(kwargs["stdin"], bootstrap.subprocess.DEVNULL)

    def test_main_never_echoes_exception_or_environment_secret(self) -> None:
        secret = "https://token@example.invalid/simple"
        stream = io.StringIO()
        with patch.object(bootstrap.sys, "argv", ["bootstrap.py", "--", "-m", "sidecar"]), patch.object(
            bootstrap, "ensure_runtime", side_effect=RuntimeError(secret)
        ), patch.object(bootstrap.sys, "stderr", stream):
            self.assertEqual(bootstrap.main(), 1)
        self.assertNotIn(secret, stream.getvalue())
        self.assertIn("runtime initialization failed", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
