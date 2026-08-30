"""Local HTTP server for the SemaRail Semantic Console.

The implementation intentionally uses the Python standard library.  It can be
embedded by a host process (``create_app``/``SemanticConsoleApplication``) or
started directly with ``python -m server``.  The default bind address is
127.0.0.1; callers must opt in explicitly to any other interface.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

try:  # Package import when started with ``python -m server``.
    from .service import SemanticConsoleService
    from .project import ProjectStore
except ImportError:  # Direct ``python app.py`` / test loading by file path.
    from service import SemanticConsoleService  # type: ignore[no-redef]
    from project import ProjectStore  # type: ignore[no-redef]


MAX_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48763


def _allowed_browser_origin(origin: str | None) -> bool:
    """Allow the console itself, configured origins, and local Desktop UI.

    DSH Desktop serves its renderer from an ephemeral loopback port.  Matching
    the parsed hostname (not a string prefix) keeps arbitrary web origins out
    while allowing the out-of-tree Client plugin to submit review candidates.
    """

    if not origin:
        return True
    allowed = {
        item.strip()
        for item in os.environ.get("SEMANTIC_CONSOLE_ORIGINS", "").split(",")
        if item.strip()
    }
    if origin in allowed:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
_LOGGER = logging.getLogger("semantic-console")


class SemanticConsoleApplication:
    """Embeddable request adapter with one service instance."""

    def __init__(self, service: SemanticConsoleService | None = None, *, static_dir: str | Path | None = None) -> None:
        self.service = service or SemanticConsoleService()
        self.static_dir = Path(static_dir).expanduser().resolve() if static_dir else None

    def request(self, method: str, target: str, body: Any = None) -> tuple[int, dict[str, Any]]:
        parsed = urlsplit(target)
        query: dict[str, Any] = {}
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
            query[key] = values[-1] if values else ""
        status, result = self.service.dispatch(method.upper(), parsed.path, query, body)
        if "__error__" in result:
            return status, result["__error__"]
        return status, result


def create_app(
    service: SemanticConsoleService | None = None,
    *,
    static_dir: str | Path | None = None,
) -> SemanticConsoleApplication:
    """Create an embeddable Semantic Console application."""

    return SemanticConsoleApplication(service, static_dir=static_dir)


class _Handler(BaseHTTPRequestHandler):
    server_version = "SemanticConsole/1"

    @property
    def application(self) -> SemanticConsoleApplication:
        return self.server.application  # type: ignore[attr-defined,no-any-return]

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._host_allowed() or not self._origin_allowed():
            self._send_json(403, {"code": "ORIGIN_NOT_ALLOWED", "message": "request origin is not allowed"})
            return
        self.send_response(204)
        self._headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            if not self._host_allowed():
                self._send_json(403, {"code": "HOST_NOT_ALLOWED", "message": "request host is not allowed"})
                return
            if not self._origin_allowed():
                self._send_json(403, {"code": "ORIGIN_NOT_ALLOWED", "message": "request origin is not allowed"})
                return
            if method == "GET" and not urlsplit(self.path).path.startswith("/api/"):
                if self._serve_static():
                    return
            if method in {"POST", "PUT", "PATCH"} and urlsplit(self.path).path.startswith("/api/"):
                length = int(self.headers.get("Content-Length", "0") or "0")
                content_type = self.headers.get("Content-Type", "")
                if length and not content_type.lower().split(";", 1)[0].strip() == "application/json":
                    self._send_json(415, {"code": "UNSUPPORTED_MEDIA_TYPE", "message": "write requests require application/json"})
                    return
            body = self._read_body() if method in {"POST", "PUT", "PATCH"} else None
            status, response = self.application.request(method, self.path, body)
        except ValueError:
            status, response = 400, {"code": "INVALID_JSON", "message": "request body must be valid JSON"}
        except Exception:
            # Keep handler diagnostics out of the response.  In particular, a
            # DB-API exception can include a DSN in its string representation.
            _LOGGER.error("semantic console request failed")
            status, response = 500, {"code": "INTERNAL_ERROR", "message": "semantic console request failed"}
        self._send_json(status, response)

    def _send_json(self, status: int, response: dict[str, Any]) -> None:
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._headers()
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            pass

    def _host_allowed(self) -> bool:
        host_header = self.headers.get("Host", "")
        if not host_header:
            # The console is a local browser-facing API.  Reject HTTP/1.0
            # requests without Host instead of treating an absent authority
            # as an implicit allow-all origin.
            return False
        host_header = host_header.strip()
        if host_header.startswith("["):
            host = host_header[1:].split("]", 1)[0]
        elif ":" in host_header:
            host = host_header.rsplit(":", 1)[0]
        else:
            host = host_header
        return host.lower() in {"localhost", "127.0.0.1", "::1"}

    def _origin_allowed(self) -> bool:
        return _allowed_browser_origin(self.headers.get("Origin"))

    def _serve_static(self) -> bool:
        root = self.application.static_dir
        if root is None or not root.is_dir():
            return False
        path = unquote(urlsplit(self.path).path)
        relative = path.lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self._send_json(403, {"code": "INVALID_PATH", "message": "static path is not allowed"})
            return True
        if not candidate.is_file():
            candidate = root / "index.html"
        if not candidate.is_file():
            return False
        try:
            content = candidate.read_bytes()
        except OSError:
            self._send_json(500, {"code": "STATIC_READ_FAILED", "message": "static asset could not be read"})
            return True
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
        }
        self.send_response(200)
        self._headers(content_type=content_types.get(candidate.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        try:
            self.wfile.write(content)
        except OSError:
            pass
        return True

    def _read_body(self) -> Any:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _headers(self, *, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_header("Content-Type", content_type)
        origin = self.headers.get("Origin")
        allowed = {
            item.strip()
            for item in os.environ.get("SEMANTIC_CONSOLE_ORIGINS", "").split(",")
            if item.strip()
        }
        if origin and _allowed_browser_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        connect_sources = " ".join(["'self'", *sorted(allowed)])
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src {connect_sources}; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log request targets or bodies: query strings and future routes
        # may contain user-provided identifiers or secrets.
        _LOGGER.info("semantic console request completed")


class SemanticConsoleHTTPServer(ThreadingHTTPServer):
    """Threaded local server carrying an explicitly injected application."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], application: SemanticConsoleApplication) -> None:
        self.application = application
        super().__init__(address, _Handler)


def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    application: SemanticConsoleApplication | None = None,
    project_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    static_dir: str | Path | None = None,
) -> None:
    """Run the HTTP server until interrupted."""

    if application is None:
        project = ProjectStore(project_dir, state_dir=state_dir)
        application = create_app(SemanticConsoleService(project), static_dir=static_dir)
    app = application
    server = SemanticConsoleHTTPServer((host, port), app)
    _LOGGER.info("semantic console listening on %s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SemaRail Semantic Console local server")
    parser.add_argument("--host", default=os.environ.get("SEMANTIC_CONSOLE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SEMANTIC_CONSOLE_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--project-dir", default=os.environ.get("WREN_PROJECT_HOME"))
    parser.add_argument("--state-dir", default=os.environ.get("SEMANTIC_CONSOLE_STATE_DIR"))
    parser.add_argument("--static-dir", default=os.environ.get("SEMANTIC_CONSOLE_STATIC_DIR"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    serve(host=args.host, port=args.port, project_dir=args.project_dir, state_dir=args.state_dir, static_dir=args.static_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by process smoke tests
    raise SystemExit(main())


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "SemanticConsoleApplication",
    "SemanticConsoleHTTPServer",
    "create_app",
    "serve",
]
