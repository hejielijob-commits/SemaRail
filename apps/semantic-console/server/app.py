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
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

try:  # Package import when started with ``python -m server``.
    from .access_control import AccessControlError, AuthContext, BOOTSTRAP_SUBJECT_ID
    from .access_api import AccessControlAdminApi
    from .artifact_store import ArtifactDownload, ArtifactError
    from .identity_api import IdentityApi
    from .service import SemanticConsoleService
    from .project import ProjectStore
    from .runtime_rpc import RuntimeRpcGateway
except ImportError:  # Direct ``python app.py`` / test loading by file path.
    from access_control import AccessControlError, AuthContext, BOOTSTRAP_SUBJECT_ID  # type: ignore[no-redef]
    from access_api import AccessControlAdminApi  # type: ignore[no-redef]
    from artifact_store import ArtifactDownload, ArtifactError  # type: ignore[no-redef]
    from identity_api import IdentityApi  # type: ignore[no-redef]
    from service import SemanticConsoleService  # type: ignore[no-redef]
    from project import ProjectStore  # type: ignore[no-redef]
    from runtime_rpc import RuntimeRpcGateway  # type: ignore[no-redef]


MAX_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48763
_PUBLIC_CONSOLE_ROUTES = frozenset(
    {
        ("GET", "/api/health"),
        ("GET", "/api/datasource-types"),
    }
)
_ARTIFACT_DOWNLOAD_ROUTE = re.compile(r"/api/v1/artifacts/([^/]+)/download\Z")


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

    def __init__(
        self,
        service: SemanticConsoleService | None = None,
        *,
        static_dir: str | Path | None = None,
        runtime_rpc: RuntimeRpcGateway | None = None,
        access_api: AccessControlAdminApi | None = None,
        identity_api: IdentityApi | None = None,
    ) -> None:
        self.service = service or SemanticConsoleService()
        self.static_dir = Path(static_dir).expanduser().resolve() if static_dir else None
        self.runtime_rpc = runtime_rpc or RuntimeRpcGateway(self.service.project)
        project_id = str(self.runtime_rpc.project.overview().get("name") or "")
        self.access_api = access_api or AccessControlAdminApi(
            self.runtime_rpc.access_control,
            self.runtime_rpc.policy_engine,
            project_id=project_id,
        )
        self.identity_api = identity_api or IdentityApi(self.runtime_rpc.access_control)

    def request(
        self,
        method: str,
        target: str,
        body: Any = None,
        *,
        authorization: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        parsed = urlsplit(target)
        query: dict[str, Any] = {}
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
            query[key] = values[-1] if values else ""
        if method.upper() == "POST" and parsed.path == "/api/v1/runtime/rpc":
            return self.runtime_rpc.dispatch(body, authorization)
        if method.upper() == "GET" and _ARTIFACT_DOWNLOAD_ROUTE.fullmatch(parsed.path):
            result = self.resolve_artifact_download(target, authorization=authorization)
            if isinstance(result, ArtifactDownload):
                # The embeddable adapter remains JSON-shaped for callers that
                # do not use the HTTP handler.  The real HTTP path streams the
                # same immutable file with attachment headers.
                return 200, {"artifact": result.metadata.as_dict()}
            if result is not None:
                return result
        if method.upper() == "GET" and parsed.path == "/api/v1/auth/capabilities":
            try:
                auth = self.runtime_rpc.access_control.authenticate(authorization)
                policies = (
                    []
                    if auth.subject.id == BOOTSTRAP_SUBJECT_ID
                    else self.runtime_rpc.access_control.policies_for_subject(auth.subject.id)
                )
                project_id = str(self.runtime_rpc.project.overview().get("name") or "")
                capabilities = {
                    scope: self.runtime_rpc.policy_engine.authorize_scope(
                        auth.subject, scope, policies, project_id=project_id
                    ).allowed
                    for scope in ("console:admin", "access:admin")
                }
                return 200, {
                    "subject": auth.subject.as_dict(),
                    "projectId": project_id,
                    "capabilities": capabilities,
                }
            except AccessControlError as exc:
                return exc.status, {"code": exc.code, "message": exc.safe_message}
        identity_response = self.identity_api.dispatch(method.upper(), parsed.path, query, body, authorization)
        if identity_response is not None:
            return identity_response
        access_response = self.access_api.dispatch(method.upper(), parsed.path, body, authorization)
        if access_response is not None:
            return access_response
        auth: AuthContext | None = None
        normalized_method = method.upper()
        if parsed.path.startswith("/api/") and (normalized_method, parsed.path) not in _PUBLIC_CONSOLE_ROUTES:
            try:
                auth = self.runtime_rpc.access_control.authenticate(authorization)
                policies = (
                    []
                    if auth.subject.id == BOOTSTRAP_SUBJECT_ID
                    else self.runtime_rpc.access_control.policies_for_subject(auth.subject.id)
                )
                project_id = str(self.runtime_rpc.project.overview().get("name") or "")
                decision = self.runtime_rpc.policy_engine.authorize_scope(
                    auth.subject, "console:admin", policies, project_id=project_id
                )
                if not decision.allowed:
                    self.runtime_rpc.access_control.record_audit(
                        action="console.access", decision="denied", auth=auth, resource=parsed.path
                    )
                    return 403, {"code": "FORBIDDEN", "message": "console administrator permission is required"}
            except AccessControlError as exc:
                return exc.status, {"code": exc.code, "message": exc.safe_message}
        status, result = self.service.dispatch(method.upper(), parsed.path, query, body)
        if auth is not None:
            try:
                self.runtime_rpc.access_control.record_audit(
                    action=f"console.{normalized_method.lower()}",
                    decision="allowed" if status < 400 else "error",
                    auth=auth,
                    resource=parsed.path,
                )
            except AccessControlError:
                _LOGGER.error("console audit write failed")
        if "__error__" in result:
            return status, result["__error__"]
        return status, result

    def resolve_artifact_download(
        self,
        target: str,
        *,
        authorization: str | None = None,
    ) -> ArtifactDownload | tuple[int, dict[str, Any]] | None:
        """Resolve the binary artifact route for the HTTP adapter.

        A non-artifact target returns ``None``.  Errors are returned as the
        ordinary ``(status, JSON)`` shape so both embedded and real HTTP
        callers get stable, credential-free messages.
        """

        parsed = urlsplit(target)
        match = _ARTIFACT_DOWNLOAD_ROUTE.fullmatch(parsed.path)
        if match is None:
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        token_values = query.get("token", [])
        token = token_values[-1] if token_values else ""
        try:
            return self.runtime_rpc.download_artifact(
                match.group(1), token, authorization, transport="core-http"
            )
        except ArtifactError as exc:
            return exc.status, {"code": exc.code, "message": exc.safe_message}
        except AccessControlError as exc:
            return exc.status, {"code": exc.code, "message": exc.safe_message}


def create_app(
    service: SemanticConsoleService | None = None,
    *,
    static_dir: str | Path | None = None,
    runtime_rpc: RuntimeRpcGateway | None = None,
    access_api: AccessControlAdminApi | None = None,
    identity_api: IdentityApi | None = None,
) -> SemanticConsoleApplication:
    """Create an embeddable Semantic Console application."""

    return SemanticConsoleApplication(
        service,
        static_dir=static_dir,
        runtime_rpc=runtime_rpc,
        access_api=access_api,
        identity_api=identity_api,
    )


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
            if method == "GET" and _ARTIFACT_DOWNLOAD_ROUTE.fullmatch(urlsplit(self.path).path):
                result = self.application.resolve_artifact_download(
                    self.path,
                    authorization=self.headers.get("Authorization"),
                )
                if isinstance(result, ArtifactDownload):
                    self._send_artifact(result)
                elif isinstance(result, tuple):
                    self._send_json(result[0], result[1])
                else:  # pragma: no cover - route matcher and resolver agree
                    self._send_json(404, {"code": "NOT_FOUND", "message": "artifact was not found"})
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
            status, response = self.application.request(
                method,
                self.path,
                body,
                authorization=self.headers.get("Authorization"),
            )
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

    def _send_artifact(self, download: ArtifactDownload) -> None:
        """Stream an already-authorized immutable artifact without redirecting."""

        metadata = download.metadata
        self.send_response(200)
        self._headers(content_type=metadata.content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{metadata.filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(metadata.size or 0))
        self.end_headers()
        try:
            with download.path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError:
            # Headers may already be on the wire; there is no safe JSON error
            # shape to append after a partial binary body.
            return

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
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
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
        # A real Core HTTP server knows its bound port only after bind (not
        # from a request Host header).  Publish that trusted authority for
        # descriptors generated after startup; explicitly configured bases
        # (for a reverse proxy or TLS terminator) remain untouched.
        setter = getattr(self.application.runtime_rpc, "set_artifact_base_url", None)
        explicit = bool(getattr(self.application.runtime_rpc, "artifact_base_url_explicit", False))
        if callable(setter) and not explicit:
            bound_host = str(self.server_address[0])
            if bound_host in {"0.0.0.0", "::", ""}:
                bound_host = "127.0.0.1"
            authority = f"[{bound_host}]" if ":" in bound_host and not bound_host.startswith("[") else bound_host
            setter(f"http://{authority}:{int(self.server_address[1])}")


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
