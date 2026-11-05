from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
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
    def test_launcher_waits_for_runtime_and_preserves_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "ready"
            (root / "runtime_probe.py").write_text(
                "from pathlib import Path\nimport sys,time\n"
                "Path(sys.argv[1]).write_text('ready')\ntime.sleep(1)\nraise SystemExit(7)\n",
                encoding="utf-8",
            )
            script = (
                "import importlib.util,sys\nfrom pathlib import Path\n"
                f"spec=importlib.util.spec_from_file_location('bootstrap', {str(Path(bootstrap.__file__).resolve())!r})\n"
                "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
                "module.ensure_runtime=lambda _:Path(sys.executable)\n"
                f"sys.argv=['bootstrap.py','--','-m','runtime_probe',{str(ready)!r}]\n"
                "raise SystemExit(module.main())\n"
            )
            env = {**os.environ, "PYTHONPATH": str(root)}
            process = subprocess.Popen(
                [sys.executable, "-c", script], cwd=root, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "runtime must actually start")
                stayed_alive = process.poll() is None
                exit_code = process.wait(timeout=10)
                if not stayed_alive:
                    # The pre-fix Windows launcher orphaned this short probe.
                    time.sleep(1.1)
                self.assertTrue(stayed_alive, "launcher must remain alive while runtime runs")
                self.assertEqual(exit_code, 7)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_stale_lock_threshold_exceeds_initializer_wait(self) -> None:
        self.assertGreater(bootstrap.LOCK_STALE_SECONDS, bootstrap.LOCK_WAIT_SECONDS)

    def _package_root(self, root: Path) -> Path:
        for relative in ("runtime/constraints.txt", "python/sidecar/pyproject.toml", "python/semantic-console/pyproject.toml"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        for relative in ("python/sidecar/sidecar/__init__.py", "python/semantic-console/server/__init__.py"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        return root

    def test_python_source_change_invalidates_runtime_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package_root = self._package_root(Path(temporary) / "package")
            before = bootstrap._fingerprint(package_root)
            source = package_root / "python" / "semantic-console" / "server" / "__init__.py"
            source.write_text("changed runtime source", encoding="utf-8")

            self.assertNotEqual(before, bootstrap._fingerprint(package_root))

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

    def test_install_builds_from_short_private_copy_not_installed_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._package_root(root / ("long-install-path-" * 5) / "package")
            target = root / "venv"
            class FakeBuilder:
                def __init__(self, **_kwargs): pass
                def create(self, destination):
                    bootstrap._python_in(destination).parent.mkdir(parents=True)
                    bootstrap._python_in(destination).touch()
            def install(command, **kwargs):
                for argument in command[-2:]:
                    source = Path(argument.split("[", 1)[0])
                    self.assertFalse(source.is_relative_to(package))
                    self.assertLess(len(str(source)), len(str(package)))
                    self.assertTrue((source / "pyproject.toml").is_file())
                return type("Installed", (), {"returncode": 0})()
            with patch("venv.EnvBuilder", FakeBuilder), patch.object(bootstrap.subprocess, "run", side_effect=install):
                bootstrap._install(package, target, "test")
            self.assertTrue(bootstrap._marker_valid(target, "test"))
            self.assertFalse(any(package.rglob("build")))

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
