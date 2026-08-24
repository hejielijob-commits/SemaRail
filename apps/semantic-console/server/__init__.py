"""Wren Semantic Console MVP server package."""

from .app import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    SemanticConsoleApplication,
    SemanticConsoleHTTPServer,
    create_app,
    serve,
)
from .project import ProjectError, ProjectStore, WrenProjectAdapter
from .service import ApiServiceError, SemanticConsoleService

__all__ = [
    "ApiServiceError",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ProjectError",
    "ProjectStore",
    "SemanticConsoleApplication",
    "SemanticConsoleHTTPServer",
    "SemanticConsoleService",
    "WrenProjectAdapter",
    "create_app",
    "serve",
]
