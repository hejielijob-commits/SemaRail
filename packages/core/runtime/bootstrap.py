"""Create and reuse SemaRail's private Python runtime, then exec a module.

Diagnostics use stable text on stderr; child command output and environment
variables are never emitted because either may contain package-index or data
source credentials. Sidecar stdout therefore remains framed-protocol only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

BOOTSTRAP_VERSION = "4"
LOCK_WAIT_SECONDS = 900
LOCK_STALE_SECONDS = 3600
LOCK_POLL_SECONDS = 0.2


def _runtime_home() -> Path:
    configured = os.environ.get("SEMARAIL_RUNTIME_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "SemaRail" / "runtime"
    if os.environ.get("XDG_CACHE_HOME"):
        return Path(os.environ["XDG_CACHE_HOME"]) / "semarail" / "runtime"
    return Path.home() / ".cache" / "semarail" / "runtime"


def _python_in(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _fingerprint(package_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(BOOTSTRAP_VERSION.encode())
    digest.update(sys.implementation.cache_tag.encode())
    inputs = [
        package_root / "runtime" / "constraints.txt",
        package_root / "python" / "sidecar" / "pyproject.toml",
        package_root / "python" / "semantic-console" / "pyproject.toml",
        *sorted((package_root / "python" / "sidecar").rglob("*.py")),
        *sorted((package_root / "python" / "semantic-console").rglob("*.py")),
    ]
    for path in inputs:
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:16]


def _marker_valid(venv: Path, fingerprint: str) -> bool:
    try:
        marker = json.loads((venv / ".semarail-runtime.json").read_text(encoding="utf-8"))
        return marker == {"bootstrapVersion": BOOTSTRAP_VERSION, "fingerprint": fingerprint} and _python_in(venv).is_file()
    except (OSError, ValueError, TypeError):
        return False


def _acquire_lock(lock: Path) -> None:
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            lock.mkdir(parents=True)
            (lock / "owner.json").write_text(json.dumps({"pid": os.getpid(), "created": int(time.time())}), encoding="utf-8")
            return
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            # Never classify a legitimate slow package installation as stale
            # merely because another Host process exhausted its own wait.
            if age > LOCK_STALE_SECONDS:
                shutil.rmtree(lock, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError("runtime initialization timed out")
            time.sleep(LOCK_POLL_SECONDS)


def _install(package_root: Path, venv: Path, fingerprint: str) -> None:
    import venv as venv_module

    if venv.exists():
        shutil.rmtree(venv)
    venv_module.EnvBuilder(with_pip=True, clear=True).create(venv)
    python = _python_in(venv)
    # setuptools builds local projects in-place. Deep npm prefixes can exceed
    # Windows MAX_PATH, and globally installed sources may be read-only. Build
    # public package sources in a short private directory instead.
    with tempfile.TemporaryDirectory(prefix="sr-") as temporary:
        stage = Path(temporary)
        for name in ("sidecar", "semantic-console"):
            shutil.copytree(package_root / "python" / name, stage / name,
                            ignore=shutil.ignore_patterns("build", "dist", "*.egg-info", "__pycache__"))
        command = [
            str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "--constraint", str(package_root / "runtime" / "constraints.txt"),
            str(stage / "sidecar") + "[wren,mcp]",
            str(stage / "semantic-console") + "[wren]",
        ]
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=os.environ.copy(), check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Python dependencies could not be installed; check network and package-index settings")
    (venv / ".semarail-runtime.json").write_text(
        json.dumps({"bootstrapVersion": BOOTSTRAP_VERSION, "fingerprint": fingerprint}), encoding="utf-8"
    )


def ensure_runtime(package_root: Path) -> Path:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    fingerprint = _fingerprint(package_root)
    runtime_root = _runtime_home() / fingerprint
    venv = runtime_root / "venv"
    if _marker_valid(venv, fingerprint):
        return _python_in(venv)
    lock = runtime_root.with_name(runtime_root.name + ".install.lock")
    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    _acquire_lock(lock)
    try:
        if not _marker_valid(venv, fingerprint):
            print("SemaRail is initializing its private Python runtime...", file=sys.stderr)
            _install(package_root, venv, fingerprint)
        return _python_in(venv)
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def main() -> int:
    separator = sys.argv.index("--") if "--" in sys.argv else -1
    module_args = sys.argv[separator + 1 :] if separator >= 0 else []
    if len(module_args) < 2 or module_args[0] != "-m":
        print("SemaRail bootstrap requires '-- -m <module> [args]'", file=sys.stderr)
        return 2
    package_root = Path(__file__).resolve().parent.parent
    try:
        python = ensure_runtime(package_root)
    except Exception:
        print("SemaRail runtime initialization failed. Check Python, network, and package-index settings.", file=sys.stderr)
        return 1
    if os.name == "nt":
        # Windows execv starts a replacement PID and exits this launcher early.
        # Keep the supervised process alive and propagate the runtime exit code.
        return subprocess.call([str(python), *module_args])
    os.execv(str(python), [str(python), *module_args])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
