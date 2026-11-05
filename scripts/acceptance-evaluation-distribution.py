"""Install packed Core and runner into a fresh owned workspace and smoke APIs.

No model is called. Package-manager output stays private because configured
indexes can contain credentials. Supply archives built from the current source.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import venv

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def command(args: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300) -> bytes:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=timeout, creationflags=HIDDEN)
    if result.returncode:
        raise RuntimeError("distribution_command_failed")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--runner-wheel", type=Path, required=True)
    parser.add_argument("--startup-timeout", type=int, default=900, help="Allow the installed Core's first private-runtime bootstrap")
    args = parser.parse_args()
    archive, wheel = args.archive.resolve(strict=True), args.runner_wheel.resolve(strict=True)
    directory = ROOT / ".benchmark-data/evaluation-distribution" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SEMARAIL_", "WREN_", "BTV_", "BUSINESS_TRUST_")) and k != "PYTHONPATH"}
    report = {"schemaVersion": 1, "status": "started", "coreSha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "runnerSha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "checks": []}
    process = None
    try:
        prefix = directory / "node-install"
        prefix.mkdir()
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        command([npm, "install", "--prefix", str(prefix), "--no-audit", "--no-fund", "--ignore-scripts", str(archive)], cwd=directory, env=env)
        installed = prefix / "node_modules/@hejielijob/semarail-core"
        cli = installed / "bin/semarail.mjs"
        for name in ("evaluation_api.py", "evaluation_scoring.py", "evaluations.py"):
            assert (installed / "python/semantic-console/server" / name).is_file()
        version = command([shutil.which("node"), str(cli), "--version"], cwd=directory, env=env).decode().strip()
        report["coreVersion"] = version
        report["checks"].append("npm_archive_installed")
        runner_venv = directory / "runner-venv"
        venv.EnvBuilder(with_pip=True).create(runner_venv)
        py = runner_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        exe = runner_venv / ("Scripts/semarail-eval-runner.exe" if os.name == "nt" else "bin/semarail-eval-runner")
        command([str(py), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", str(wheel) + "[postgres]"], cwd=directory, env=env)
        command([str(py), "-c", "import mcp, psycopg, sqlglot, semarail_eval_runner; print(semarail_eval_runner.__version__)"], cwd=directory, env=env)
        command([str(exe), "--help"], cwd=directory, env=env)
        config = directory / "eval-runner.toml"
        command([str(exe), "configure", "--config", str(config)], cwd=directory, env=env)
        assert 'database_url_env' in config.read_text(encoding="utf-8")
        report["checks"].append("runner_wheel_installed_with_postgres")
        project = directory / "project"
        shutil.copytree(ROOT / "benchmarks/hr-enterprise/project", project)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_urlsafe(40)
        env.update(SEMARAIL_API_TOKEN=token, SEMARAIL_RUNTIME_HOME=str(directory / "runtime"), WREN_HOME=str(directory / "wren-home"), SEMARAIL_EMBEDDING_PROVIDER="disabled")
        with (directory / "core.log").open("wb") as log:
            process = subprocess.Popen([shutil.which("node"), str(cli), "start", "--project", str(project), "--state-dir", str(directory / "state"), "--port", str(port), "--python", sys.executable], cwd=directory, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=HIDDEN)
        def request(path, body=None, method=None):
            req = Request(f"http://127.0.0.1:{port}" + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with urlopen(req, timeout=5) as response:
                return response.read()
        deadline = time.monotonic() + args.startup_timeout
        while True:
            if process.poll() is not None:
                report["coreExitCode"] = process.returncode
                raise RuntimeError("installed_core_exited")
            try:
                settings = json.loads(request("/api/v1/evaluations/settings"))
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("installed_core_start_timeout") from None
                time.sleep(0.25)
        assert settings["enabled"] is False
        suite = json.loads(request("/api/v1/evaluations/suites", {"name": "Installed package smoke", "testDatasetId": "unbound-test", "cases": [{"id": "one", "role": "employee", "question": "Count regions", "expectedBehavior": "answer", "expectedResult": {"columns": ["n"], "rows": [[6]]}}]}))
        assert suite["status"] == "draft"
        published = json.loads(request(f"/api/v1/evaluations/suites/{suite['id']}/publish", {}))
        assert published["status"] == "published"
        assert json.loads(request("/api/v1/evaluations/suites"))["items"][0]["id"] == suite["id"]
        report["checks"].append("installed_core_evaluation_api")
        html = request("/").decode()
        assert '<div id="root">' in html and '/assets/' in html
        report["checks"].append("installed_console_assets")
        time.sleep(0.5)
        if process.poll() is not None:
            raise RuntimeError("installed_core_launcher_did_not_wait")
        report["checks"].append("installed_core_process_supervised")
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        raise
    finally:
        if process and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=HIDDEN, timeout=30)
            else:
                process.terminate()
            process.wait(timeout=30)
        if report["status"] == "passed":
            with socket.socket() as probe:
                probe.settimeout(1)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    report.update(status="failed", error="installed_core_orphan_listener")
                else:
                    report["checks"].append("installed_process_tree_stopped")
        path = directory / "report.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(path), "status": report["status"]}))
        if report["status"] == "failed" and report.get("error") == "installed_core_orphan_listener":
            raise RuntimeError("installed_core_orphan_listener")


if __name__ == "__main__":
    main()
