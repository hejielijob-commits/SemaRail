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
import time

BOOTSTRAP_VERSION = "1"
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
    for relative in ("runtime/constraints.txt", "python/sidecar/pyproject.toml", "python/semantic-console/pyproject.toml"):
        digest.update((package_root / relative).read_bytes())
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
    command = [
        str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
        "--constraint", str(package_root / "runtime" / "constraints.txt"),
        str(package_root / "python" / "sidecar") + "[wren,mcp]",
        str(package_root / "python" / "semantic-console") + "[wren]",
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
    os.execv(str(python), [str(python), *module_args])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
