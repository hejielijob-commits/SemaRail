"""Authenticated regression endpoints; all execution enters the public Core boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

try:
    from .access_control import AccessControlError, BOOTSTRAP_SUBJECT_ID
    from .diagnostics import DiagnosticError
    from .regression import RegressionRunner, RegressionStore, _id
except ImportError:  # pragma: no cover
    from access_control import AccessControlError, BOOTSTRAP_SUBJECT_ID
    from diagnostics import DiagnosticError
    from regression import RegressionRunner, RegressionStore, _id


_BASE = "/api/v1/diagnostics"


class RegressionApi:
    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway
        self.store = None
        self.runner = None
        try:
            self.store = RegressionStore(gateway.diagnostics) if gateway.diagnostics else None
            self.runner = RegressionRunner(self.store, gateway) if self.store else None
        except Exception:
            # Regression storage must not prevent ordinary Core queries.
            self.store = None
            self.runner = None

    def acquire_configuration_guard(self, auth: Any, project_id: str) -> Any:
        """Hold the same project lock as a regression run for one Console write."""

        if self.runner is None:
            raise DiagnosticError(
                "REGRESSION_STORE_UNAVAILABLE", "regression storage is unavailable", status=503
            )
        return self.runner.acquire_configuration_guard(auth, project_id)

    def dispatch(self, method: str, path: str, query: Mapping[str, Any], body: Any, authorization: str | None) -> tuple[int, dict[str, Any]] | None:
        if not any(path == _BASE + "/" + name or path.startswith(_BASE + "/" + name + "/")
                   for name in ("test-datasets", "regression-cases", "regression-runs")):
            return None
        if path == _BASE + "/regression-cases/export":
            if query.get("schemaVersion") in (None, "1"):
                return None  # Existing v1 export contract stays intact.
            if query.get("schemaVersion") != "2" or set(query) != {"schemaVersion"}:
                return 400, {"code": "INVALID_REQUEST", "message": "export schema version is unsupported"}
        if self.store is None or self.runner is None:
            return 503, {"code": "REGRESSION_STORE_UNAVAILABLE", "message": "regression storage is unavailable"}
        try:
            auth = self.gateway.access_control.authenticate(authorization)
            project_id = str(self.gateway.project.overview().get("name") or "")
            policies = [] if auth.subject.id == BOOTSTRAP_SUBJECT_ID else self.gateway.access_control.policies_for_subject(auth.subject.id)
            decision = self.gateway.policy_engine.authorize_scope(auth.subject, "console:admin", policies, project_id=project_id)
            if not decision.allowed:
                raise AccessControlError("FORBIDDEN", "console administrator permission is required", status=403)
            scope = self.store.scope(auth, project_id)
            self.gateway.access_control.record_audit(action="regression.admin", decision="allowed", auth=auth, resource=path)
            if path == _BASE + "/regression-cases/export" and method == "GET":
                return 200, {"schemaVersion": 2, "projectId": project_id, "cases": self.store.cases(scope)["items"]}
            if path == _BASE + "/test-datasets":
                if method == "GET":
                    return 200, self.store.datasets(scope)
                if method == "POST":
                    return self._guarded_dataset_mutation(
                        auth,
                        project_id,
                        lambda: self._save_dataset(scope, body, status=201),
                    )
            dataset = re.fullmatch(_BASE + r"/test-datasets/([^/]+)", path)
            if dataset:
                if method == "PUT":
                    return self._guarded_dataset_mutation(
                        auth,
                        project_id,
                        lambda: self._save_dataset(
                            scope, body, dataset_id=dataset.group(1), status=200
                        ),
                    )
                if method == "DELETE":
                    return self._guarded_dataset_mutation(
                        auth,
                        project_id,
                        lambda: self._delete_dataset(scope, dataset.group(1)),
                    )
            if path == _BASE + "/regression-cases":
                if method == "GET":
                    return 200, self.store.cases(scope)
                if method == "POST":
                    if not isinstance(body, Mapping) or body.get("enable", False) is not False:
                        raise DiagnosticError("INVALID_REQUEST", "new regression cases must be created as drafts")
                    return 201, self.store.save_case(auth, project_id, body)
            case = re.fullmatch(_BASE + r"/regression-cases/([^/]+)", path)
            if case:
                if method == "GET":
                    return 200, self.store.case(scope, case.group(1))
                if method == "PUT":
                    return 200, self.store.save_case(auth, project_id, body, identifier=case.group(1))
                if method == "DELETE":
                    self.store.delete_case(scope, _id(case.group(1)))
                    return 200, {"deleted": True}
            if path == _BASE + "/regression-runs":
                if method == "GET":
                    return 200, self.store.runs(scope)
                if method == "POST":
                    if not isinstance(body, Mapping) or set(body) != {"caseIds"}:
                        raise DiagnosticError("INVALID_REQUEST", "run request must contain only caseIds")
                    return 202, self.runner.start(auth, project_id, authorization, body["caseIds"])
            run = re.fullmatch(_BASE + r"/regression-runs/([^/]+)(/cancel)?", path)
            if run:
                if method == "GET" and not run.group(2):
                    return 200, self.store.run(scope, run.group(1))
                if method == "POST" and run.group(2):
                    if body not in (None, {}):
                        raise DiagnosticError("INVALID_REQUEST", "cancel request must be empty")
                    return 202, self.runner.cancel(auth, project_id, run.group(1), authorization)
            return 404, {"code": "NOT_FOUND", "message": "regression endpoint was not found"}
        except (AccessControlError, DiagnosticError) as exc:
            return exc.status, {"code": exc.code, "message": exc.safe_message}

    def _dataset_exists(self, body: Any) -> None:
        if not isinstance(body, Mapping) or body.get("datasourceId") not in self.gateway.project.datasource_records():
            raise DiagnosticError("DATASOURCE_NOT_FOUND", "select a datasource registered in this project")

    def _guarded_dataset_mutation(
        self, auth: Any, project_id: str, operation: Any
    ) -> tuple[int, dict[str, Any]]:
        guard = self.acquire_configuration_guard(auth, project_id)
        try:
            return operation()
        finally:
            guard.release()

    def _save_dataset(
        self,
        scope: tuple[str, str],
        body: Any,
        *,
        status: int,
        dataset_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        # Validate the current project datasource and write the registry while
        # holding the same project guard used by regression execution.
        self._dataset_exists(body)
        return status, self.store.save_dataset(scope, body, dataset_id=dataset_id)

    def _delete_dataset(
        self, scope: tuple[str, str], dataset_id: str
    ) -> tuple[int, dict[str, Any]]:
        self.store.delete_dataset(scope, dataset_id)
        return 200, {"deleted": True}
