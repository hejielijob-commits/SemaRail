from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from server.app import SemanticConsoleHTTPServer, create_app
from server.project import ProjectStore
from server.service import SemanticConsoleService


class SemanticConsoleProcessTests(unittest.TestCase):
    def test_module_entrypoint_real_subprocess_smoke(self):
        """Exercise the documented ``python -m server`` launch contract."""

        with tempfile.TemporaryDirectory(prefix="semantic-console-entry-") as root:
            tmp_path = Path(root)
            project = tmp_path / "project"
            project.mkdir()
            (project / "wren_project.yml").write_text(
                "schema_version: 5\nname: subprocess-smoke\ndata_source: postgres\n",
                encoding="utf-8",
            )
            state = tmp_path / "state"
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            package_root = Path(__file__).resolve().parents[2]
            environment = os.environ.copy()
            existing_pythonpath = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                item for item in (str(package_root), existing_pythonpath) if item
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--project-dir",
                    str(project),
                    "--state-dir",
                    str(state),
                ],
                cwd=str(package_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/health",
                    headers={"Host": f"127.0.0.1:{port}"},
                )
                response_body: dict[str, object] | None = None
                for _ in range(50):
                    if process.poll() is not None:
                        self.fail(f"python -m server exited early with code {process.returncode}")
                    try:
                        with urllib.request.urlopen(request, timeout=0.5) as response:
                            response_body = json.loads(response.read().decode("utf-8"))
                        break
                    except (urllib.error.URLError, TimeoutError, ConnectionRefusedError):
                        time.sleep(0.1)
                self.assertIsNotNone(response_body, "python -m server did not become ready")
                assert response_body is not None
                self.assertEqual(response_body["status"], "ok")
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def test_http_process_smoke(self):
        with tempfile.TemporaryDirectory(prefix="semantic-console-http-") as root:
            tmp_path = Path(root)
            project = tmp_path / "project"
            project.mkdir()
            (project / "wren_project.yml").write_text("schema_version: 5\nname: smoke\ndata_source: postgres\n", encoding="utf-8")
            application = create_app(SemanticConsoleService(ProjectStore(project, state_dir=tmp_path / "state")))
            server = SemanticConsoleHTTPServer(("127.0.0.1", 0), application)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/health",
                    headers={"Host": f"127.0.0.1:{server.server_port}"},
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    body = json.loads(response.read().decode("utf-8"))
                self.assertEqual(body["service"], "semantic-console")
                self.assertNotIn("data", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_http_rejects_unlisted_origin_and_serves_csp(self):
        with tempfile.TemporaryDirectory(prefix="semantic-console-static-") as root:
            tmp_path = Path(root)
            project = tmp_path / "project"
            project.mkdir()
            (project / "wren_project.yml").write_text("schema_version: 5\nname: smoke\ndata_source: postgres\n", encoding="utf-8")
            static = tmp_path / "static"
            static.mkdir()
            (static / "index.html").write_text("<html>ok</html>", encoding="utf-8")
            application = create_app(SemanticConsoleService(ProjectStore(project, state_dir=tmp_path / "state")), static_dir=static)
            server = SemanticConsoleHTTPServer(("127.0.0.1", 0), application)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                request = urllib.request.Request(base + "/", headers={"Host": f"127.0.0.1:{server.server_port}"})
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.read(), b"<html>ok</html>")
                blocked = urllib.request.Request(
                    base + "/api/health",
                    headers={"Host": f"127.0.0.1:{server.server_port}", "Origin": "http://evil.invalid"},
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(blocked, timeout=3)
                self.assertEqual(raised.exception.code, 403)
                desktop = urllib.request.Request(
                    base + "/api/health",
                    headers={"Host": f"127.0.0.1:{server.server_port}", "Origin": "http://127.0.0.1:43120"},
                )
                with urllib.request.urlopen(desktop, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://127.0.0.1:43120")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
