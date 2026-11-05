"""Authenticated business evidence and feedback-resolution HTTP boundaries."""
from __future__ import annotations

import logging
import re
from typing import Any

try:
    from .access_control import AccessControlError
    from .diagnostics import DiagnosticError
    from .feedback_resolution import FeedbackResolutionStore
except ImportError:  # pragma: no cover
    from access_control import AccessControlError
    from diagnostics import DiagnosticError
    from feedback_resolution import FeedbackResolutionStore

_EVIDENCE = re.compile(r"/api/v1/diagnostics/queries/([^/]+)/evidence\Z")
_RESOLUTION = re.compile(r"/api/v1/diagnostics/feedback/([^/]+)/resolution(?:/(verify))?\Z")
_EVALUATION = re.compile(r"/api/v1/diagnostics/feedback/([^/]+)/agent-evaluations(?:/([^/]+)/review)?\Z")


class BusinessTrustApi:
    """Route only the separate v1 business-trust endpoints."""
    def __init__(self, gateway: Any, regression_store: Any) -> None:
        self.gateway = gateway
        self.store = None
        if gateway.diagnostics is not None and regression_store is not None:
            try:
                self.store = FeedbackResolutionStore(gateway.access_control, gateway.diagnostics, gateway.project, regression_store)
            except Exception:
                logging.getLogger("semarail-core.business-trust").error("feedback resolution storage initialization failed")

    def dispatch(self, method: str, path: str, body: Any, authorization: str | None) -> tuple[int, dict[str, Any]] | None:
        evidence, resolution, evaluation = _EVIDENCE.fullmatch(path), _RESOLUTION.fullmatch(path), _EVALUATION.fullmatch(path)
        if not any((evidence, resolution, evaluation)):
            return None
        try:
            auth = self.gateway.access_control.authenticate(authorization)
            project_id = str(self.gateway.project.overview().get("name") or "")
            if evidence:
                if method != "GET":
                    return 405, {"code": "METHOD_NOT_ALLOWED", "message": "query evidence is read-only"}
                return 200, self.gateway.read_query_evidence(auth, project_id, evidence.group(1), administrator=True)
            if self.store is None:
                raise DiagnosticError("FEEDBACK_RESOLUTION_STORE_UNAVAILABLE", "feedback resolution storage is unavailable", status=503)
            if resolution:
                feedback_id = resolution.group(1)
                if resolution.group(2) == "verify" and method == "POST":
                    if body not in (None, {}):
                        raise DiagnosticError("INVALID_REQUEST", "verification body must be empty")
                    return 200, self.store.verify(auth, project_id, feedback_id)
                if resolution.group(2) is None:
                    if method == "GET":
                        return 200, self.store.get(auth, project_id, feedback_id)
                    if method == "PUT":
                        return 200, self.store.save(auth, project_id, feedback_id, body)
            if evaluation and method == "POST":
                if evaluation.group(2):
                    return 200, self.store.review_evaluation(auth, project_id, evaluation.group(1), evaluation.group(2), body)
                return 201, self.store.import_evaluation(auth, project_id, evaluation.group(1), body)
            return 405, {"code": "METHOD_NOT_ALLOWED", "message": "method is not supported"}
        except (AccessControlError, DiagnosticError) as exc:
            return exc.status, {"code": exc.code, "message": exc.safe_message}
