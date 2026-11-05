"""Project-scoped evaluation management and external-worker HTTP boundary."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from pathlib import Path
from typing import Any

try:
    from .access_control import AccessControlError, BOOTSTRAP_SUBJECT_ID
    from .diagnostics import DiagnosticError
    from .evaluations import EvaluationStore, TERMINAL, identifier, invalid, text, scrub_evidence
    from .evaluation_scoring import VERSION
    from .authorization import _matches
except ImportError:
    from access_control import AccessControlError, BOOTSTRAP_SUBJECT_ID
    from diagnostics import DiagnosticError
    from evaluations import EvaluationStore, TERMINAL, identifier, invalid, text, scrub_evidence
    from evaluation_scoring import VERSION
    from authorization import _matches

BASE = "/api/v1/evaluations"


class EvaluationApi:
    def __init__(self, gateway: Any, regression_api: Any) -> None:
        self.gateway, self.regression_api = gateway, regression_api
        source = Path(__file__).resolve().parent
        root = source.parents[2]
        sources = [("server/" + entry.name, entry) for entry in source.glob("*.py")]
        sources += [("sidecar/" + entry.name, entry) for entry in (root / "python/sidecar/sidecar").glob("*.py")]
        launcher = next((entry for entry in (root / "bin/semarail.mjs", root / "packages/core/bin/semarail.mjs") if entry.is_file()), None)
        if launcher is not None:
            sources.append(("bin/semarail.mjs", launcher))
        self.core_source_hash = hashlib.sha256(b"".join(
            name.encode() + b"\0" + entry.read_bytes()
            for name, entry in sorted(sources)
        )).hexdigest()
        self.core_version = None
        for manifest in (root / "package.json", root / "packages/core/package.json"):
            if manifest.is_file():
                package = json.loads(manifest.read_text(encoding="utf-8"))
                if package.get("name") == "@hejielijob/semarail-core":
                    self.core_version = package.get("version")
                    break
        try:
            self.store = EvaluationStore(gateway.diagnostics) if gateway.diagnostics else None
        except Exception:
            # Optional evaluation must not prevent ordinary Core startup.
            self.store = None
        self._guards: dict[tuple[str, str], Any] = {}

    def ensure_idle(self, auth: Any, project_id: str) -> None:
        if self.store:
            with self.store.transaction() as connection:
                scope = (auth.subject.organization_id, project_id)
                self.store.recover(connection, scope)
                if self.store.active(connection, scope):
                    invalid("configuration cannot change during evaluation", 409)
                if scope in self._guards:
                    self._guards.pop(scope).release()

    def dispatch(self, method: str, path: str, query: dict[str, Any], body: Any, authorization: str | None) -> tuple[int, dict[str, Any]] | None:
        if path != BASE and not path.startswith(BASE + "/"):
            return None
        if not self.store:
            return 503, {"code": "EVALUATION_UNAVAILABLE", "message": "evaluation storage unavailable"}
        try:
            auth = self.gateway.access_control.authenticate(authorization)
            project_id = str(self.gateway.project.overview().get("name") or "")
            scope = (auth.subject.organization_id, project_id)
            route = path[len(BASE):].strip("/").split("/")
            worker_call = route[:2] == ["workers", "register"] or (route[0] == "workers" and len(route) == 3) or (route[0] == "attempts" and len(route) == 3 and route[2] in {"heartbeat", "events", "complete"})
            action = "eval:worker" if worker_call else "console:admin"
            policies = [] if auth.subject.id == BOOTSTRAP_SUBJECT_ID else self.gateway.access_control.policies_for_subject(auth.subject.id)
            decision = self.gateway.policy_engine.authorize_scope(auth.subject, action, policies, project_id=project_id)
            if not decision.allowed or (worker_call and auth.subject.id == BOOTSTRAP_SUBJECT_ID):
                raise AccessControlError("FORBIDDEN", "evaluation permission required", status=403)
            if method in {"POST", "PUT"} and not isinstance(body, dict):
                invalid("request must be an object")
            payload = body or {}
            acquired = None
            if (method == "POST" and (route == ["runs"] or (len(route) == 3 and route[0] == "runs" and route[2] == "rerun"))) or (method == "PUT" and len(route) == 3 and route[0] == "datasets" and route[2] == "scorer"):
                acquired = self.regression_api.acquire_configuration_guard(auth, project_id)
            try:
                with self.store.transaction() as connection:
                    self.store.recover(connection, scope)
                    result = self._dispatch(connection, scope, auth, method, route, query, payload)
                    active = self.store.active(connection, scope)
                if acquired is not None:
                    self._guards[scope] = acquired
                    acquired = None
                if not active and scope in self._guards:
                    self._guards.pop(scope).release()
            finally:
                if acquired is not None:
                    acquired.release()
            self.gateway.access_control.record_audit(action="evaluation." + action, decision="allowed", auth=auth, resource=path)
            return result
        except (DiagnosticError, AccessControlError) as exc:
            return exc.status, {"code": exc.code, "message": exc.safe_message}
        except (TypeError, ValueError, KeyError):
            return 400, {"code": "INVALID_EVALUATION", "message": "evaluation payload is invalid"}

    def _environment(self, c: Any, scope: tuple[str, str], dataset_id: str) -> dict[str, Any]:
        row = c.execute("SELECT * FROM regression_test_datasets WHERE organization_id=? AND project_id=? AND id=?", (*scope, dataset_id)).fetchone()
        if row is None or row["datasource_id"] != self.gateway.project.active_datasource_identifier():
            invalid("registered test dataset must match active datasource", 409)
        revision = self.gateway.project.published_revision
        if not revision:
            invalid("publish the test project before evaluation", 409)
        dataset = self._dataset_record(c, scope, row)
        return {"datasetId": dataset_id, "datasetVersion": row["version"], "datasourceId": row["datasource_id"], "semanticVersion": revision, "scorerVersion": VERSION, "coreVersion": self.core_version, "coreSourceHash": self.core_source_hash, "datasetScorerBinding": dataset.get("scorerBinding"), "datasetScorerBindingValid": dataset.get("scorerBindingValid", False)}

    def _dataset_record(self, connection: Any, scope: tuple[str, str], row: Any) -> dict[str, Any]:
        result = {"id": row["id"], "version": row["version"], "datasourceId": row["datasource_id"]}
        assert self.store is not None
        binding = next((item for item in self.store.items(connection, scope, "dataset_scorers") if item["id"] == row["id"]), None)
        if binding:
            public = {key: binding[key] for key in ("subjectId", "datasetVersion", "datasourceId", "revision")}
            valid = binding["datasetVersion"] == row["version"] and binding["datasourceId"] == row["datasource_id"]
            try:
                self._validate_scorer(scope, binding["subjectId"], row["datasource_id"])
            except (DiagnosticError, AccessControlError):
                valid = False
            result.update(scorerSubject=binding["subjectId"], scorerBinding=public, scorerBindingValid=valid)
        return result

    def _validate_scorer(self, scope: tuple[str, str], subject_id: str, datasource_id: str) -> None:
        self._role_versions(scope, {"scorer": subject_id})
        subject = self.gateway.access_control.subject(subject_id)
        policies = self.gateway.access_control.policies_for_subject(subject_id)
        allowed = {"runtime:health", "project:validate", "semantic:read", "query:plan", "query:execute", "query:cancel", "trace:read", "diagnostics:read"}
        for policy in policies:
            document = policy["document"]
            if _matches(document.get("projects", []), scope[1]) and any(tool not in allowed for tool in document.get("tools", [])):
                invalid("dataset scorer must have only read/query capabilities", 403)
        decision = self.gateway.policy_engine.authorize_scope(subject, "query:execute", policies, project_id=scope[1], datasource_id=datasource_id)
        if not decision.allowed:
            invalid("dataset scorer requires query execution permission for this test datasource", 403)

    def _environment_matches(self, connection: Any, scope: tuple[str, str], environment: dict[str, Any]) -> bool:
        try:
            current = self._environment(connection, scope, environment["datasetId"])
            return all(environment.get(key) == value for key, value in current.items()) and self._identity_matches(scope, environment)
        except (DiagnosticError, AccessControlError):
            return False

    def _role_versions(self, scope: tuple[str, str], subjects: dict[str, str]) -> list[str]:
        versions = set()
        for subject_id in subjects.values():
            subject = self.gateway.access_control.subject(subject_id)
            if subject.organization_id != scope[0] or subject.status != "active" or subject.id == BOOTSTRAP_SUBJECT_ID:
                invalid("role must identify an active managed subject in this organization", 403)
            for policy in self.gateway.access_control.policies_for_subject(subject_id):
                if policy["organizationId"] == scope[0]:
                    versions.add(str(policy["id"]) + ":" + str(policy["version"]))
        return sorted(versions)

    def _role_contexts(self, scope: tuple[str, str], subjects: dict[str, str]) -> dict[str, str]:
        self._role_versions(scope, subjects)
        return {role: hashlib.sha256(json.dumps({"subject": self.gateway.access_control.subject(subject_id).as_dict(), "policies": self.gateway.access_control.policies_for_subject(subject_id)}, sort_keys=True, allow_nan=False).encode()).hexdigest() for role, subject_id in subjects.items()}

    def _identity_matches(self, scope: tuple[str, str], environment: dict[str, Any]) -> bool:
        try:
            subjects = environment["roleSubjects"]
            matches = self._role_versions(scope, subjects) == environment["policyVersions"] and ("roleContextHashes" not in environment or self._role_contexts(scope, subjects) == environment["roleContextHashes"])
            if environment.get("scorerSubject"):
                scorer = {"scorer": environment["scorerSubject"]}
                matches = matches and self._role_versions(scope, scorer) == environment["scorerPolicyVersions"] and self._role_contexts(scope, scorer)["scorer"] == environment["scorerContextHash"]
            return matches
        except (DiagnosticError, AccessControlError):
            return False

    def _dispatch(self, c: Any, scope: tuple[str, str], auth: Any, method: str, r: list[str], q: dict[str, Any], b: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        s = self.store
        assert s is not None
        get = lambda kind, id: s.get(c, scope, kind, id)
        put = lambda kind, value: s.put(c, scope, kind, value)
        items = lambda kind: s.items(c, scope, kind)
        source_run = None
        if len(r) == 3 and r[0] == "runs" and r[2] == "rerun" and method == "POST":
            if set(b) - {"caseIds"}:
                invalid("rerun accepts only case IDs")
            source_run = get("runs", identifier(r[1]))
            b = {"suiteId": source_run["suiteId"], "profileId": source_run["profileId"], **b}
            r = ["runs"]
        if r == ["preflight"] and method == "GET":
            reasons: list[str] = []
            environment = None
            total = 0
            model = None
            try:
                suite = get("suites", identifier(q.get("suiteId")))
                profile = get("profiles", identifier(q.get("profileId")))
                total, model = len(suite["cases"]) * profile["rounds"], profile["model"]
                settings = get("settings", "settings")
                if not settings.get("enabled") or not settings.get("testDataOnly"):
                    reasons.append("Enable this test project first")
                if suite["status"] != "published" or profile["status"] != "published":
                    reasons.append("Publish the suite and Agent profile")
                environment = self._environment(c, scope, suite["testDatasetId"])
                workers = [w for w in items("workers") if w["expiresAt"] > s.now()]
                roles = {case["role"] for case in suite["cases"]}
                if not roles <= {role for worker in workers for role in worker["roles"]}:
                    reasons.append("Online workers must provide every test role")
                if any(case.get("referenceSql") for case in suite["cases"]):
                    scorers = {worker.get("capabilities", {}).get("scorerSubject") for worker in workers}
                    if None in scorers or len(scorers) != 1:
                        reasons.append("Reference queries require one independent managed scorer identity")
                    binding = environment.get("datasetScorerBinding") or {}
                    if not environment.get("datasetScorerBindingValid") or scorers != {binding.get("subjectId")}:
                        reasons.append("Register the matching scorer for this dataset version and datasource")
                fingerprints = {w.get("capabilities", {}).get("datasetFingerprints", {}).get(suite["testDatasetId"]) for w in workers}
                if not workers or None in fingerprints or len(fingerprints) != 1:
                    reasons.append("Workers must report matching test data fingerprints")
                else:
                    environment["datasetFingerprint"] = next(iter(fingerprints))
                if s.active(c, scope):
                    reasons.append("Another evaluation is active")
            except (DiagnosticError, AccessControlError) as exc:
                reasons.append(exc.safe_message)
            return 200, {"schemaVersion": 1, "ready": not reasons, "reasons": reasons, "environment": environment, "total": total, "model": model}
        if r == ["settings"]:
            settings = next(iter(items("settings")), {"id": "settings", "schemaVersion": 1, "enabled": False, "testDataOnly": False})
            if method == "PUT":
                if set(b) - {"enabled", "testDataOnly"} or any(type(v) is not bool for v in b.values()):
                    invalid("settings require booleans")
                settings.update(b)
                if settings["enabled"] and settings["testDataOnly"] is not True:
                    invalid("test-data-only acknowledgement required")
                put("settings", settings)
            return 200, settings
        if r == ["datasets"] and method == "GET":
            rows = c.execute("SELECT * FROM regression_test_datasets WHERE organization_id=? AND project_id=?", scope).fetchall()
            return 200, {"schemaVersion": 1, "items": [self._dataset_record(c, scope, row) for row in rows]}
        if len(r) == 3 and r[0] == "datasets" and r[2] == "scorer" and method == "PUT":
            dataset_id = identifier(r[1])
            if set(b) != {"subjectId"} or not isinstance(b.get("subjectId"), str):
                invalid("scorer registration requires only a managed subject ID")
            row = c.execute("SELECT * FROM regression_test_datasets WHERE organization_id=? AND project_id=? AND id=?", (*scope, dataset_id)).fetchone()
            if row is None:
                invalid("registered test dataset was not found", 404)
            self._validate_scorer(scope, b["subjectId"], row["datasource_id"])
            if any(b["subjectId"] == worker["subjectId"] or b["subjectId"] in worker["capabilities"]["roleSubjects"].values() for worker in items("workers")):
                invalid("dataset scorer must be independent of worker and Agent role identities")
            old = next((value for value in items("dataset_scorers") if value["id"] == dataset_id), None)
            binding = {"id": dataset_id, "schemaVersion": 1, "subjectId": b["subjectId"], "datasetVersion": row["version"], "datasourceId": row["datasource_id"]}
            if not old or any(binding[key] != old.get(key) for key in binding):
                put("dataset_scorers", {**binding, "revision": old["revision"] + 1 if old else 1, "registeredBy": auth.subject.id, "registeredAt": s.now()})
            return 200, self._dataset_record(c, scope, row)
        if len(r) == 2 and r[0] in {"suites", "profiles"} and r[1] == "import" and method == "POST":
            if type(b.get("schemaVersion")) is not int or b.get("schemaVersion") != 1:
                invalid("unsupported import version")
            return 201, s.save_asset(c, scope, r[0], b.get("suite" if r[0] == "suites" else "profile"))
        if r[0] in {"suites", "profiles"}:
            kind = r[0]
            if len(r) == 1:
                if method == "GET":
                    return 200, {"schemaVersion": 1, "items": items(kind)}
                if method == "POST":
                    return 201, s.save_asset(c, scope, kind, b)
            if len(r) >= 2:
                id = identifier(r[1])
                if len(r) == 2:
                    if method == "GET":
                        return 200, get(kind, id)
                    if method == "PUT":
                        return 200, s.save_asset(c, scope, kind, b, id)
                    if method == "DELETE":
                        get(kind, id)
                        c.execute("DELETE FROM evaluation_records WHERE organization_id=? AND project_id=? AND kind=? AND id=?", (*scope, kind, id))
                        return 200, {"deleted": True}
                if len(r) == 3 and r[2] == "publish" and method == "POST":
                    return 200, s.publish(c, scope, kind, id)
                if len(r) == 3 and r[2] == "export" and method == "GET":
                    asset = get(kind, id)
                    return 200, {"schemaVersion": 1, "suite" if kind == "suites" else "profile": {k: v for k, v in asset.items() if k not in {"id", "revision", "status", "schemaVersion", "publishedAt", "updatedAt"}}}
                if len(r) == 3 and r[2] == "copy" and method == "POST":
                    old = get(kind, id)
                    value = {k: v for k, v in old.items() if k not in {"id", "revision", "status", "schemaVersion", "publishedAt", "updatedAt"}}
                    value["name"] += " (copy)"
                    return 201, s.save_asset(c, scope, kind, value)
        if r == ["workers", "register"] and method == "POST":
            id = identifier(b.get("id"))
            old = next((x for x in items("workers") if x["id"] == id), None)
            if old and old["subjectId"] != auth.subject.id:
                invalid("worker belongs to another subject", 403)
            roles = b.get("roles")
            if not isinstance(roles, list) or not roles or len(roles) > 100:
                invalid("worker role mappings required")
            for role in roles:
                identifier(role)
            capabilities = b.get("capabilities", {})
            if not isinstance(capabilities, dict):
                invalid("worker capabilities must be an object")
            fingerprints = capabilities.get("datasetFingerprints", {})
            if not isinstance(fingerprints, dict) or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in fingerprints.values()):
                invalid("dataset fingerprints must be SHA256")
            subjects = capabilities.get("roleSubjects", {})
            if not isinstance(subjects, dict) or set(subjects) != set(roles) or any(not isinstance(x, str) for x in subjects.values()):
                invalid("verified role subjects must match the advertised roles")
            self._role_versions(scope, subjects)
            scorer = capabilities.get("scorerSubject")
            if scorer is not None:
                if not isinstance(scorer, str) or scorer == auth.subject.id or scorer in subjects.values():
                    invalid("scorer must be independent of worker and Agent role identities")
                self._role_versions(scope, {"scorer": scorer})
            value = {"id": id, "subjectId": auth.subject.id, "version": text(b.get("version"), "version", 100), "cliVersion": text(b.get("cliVersion"), "cliVersion", 100), "roles": roles, "capabilities": {"datasetFingerprints": fingerprints, "roleSubjects": subjects, **({"scorerSubject": scorer} if scorer else {})}, "lastSeenAt": s.now(), "expiresAt": s.expires(120)}
            return 200, put("workers", value)
        if r == ["workers"] and method == "GET":
            return 200, {"schemaVersion": 1, "items": [{**w, "online": w["expiresAt"] > s.now()} for w in items("workers")]}
        if len(r) == 3 and r[0] == "workers" and method == "POST":
            worker = get("workers", identifier(r[1]))
            if worker["subjectId"] != auth.subject.id:
                invalid("worker belongs to another subject", 403)
            worker.update(lastSeenAt=s.now(), expiresAt=s.expires(120))
            put("workers", worker)
            if r[2] == "heartbeat":
                return 200, worker
            if r[2] == "claim":
                for run in items("runs"):
                    if run["status"] not in {"queued", "running"} or run.get("cancelRequested"):
                        continue
                    environment_matches = self._environment_matches(c, scope, run["environment"])
                    observed_fingerprints = b.get("datasetFingerprints", {})
                    if not isinstance(observed_fingerprints, dict):
                        invalid("dataset fingerprints must be an object")
                    fingerprint = observed_fingerprints.get(run["environment"]["datasetId"])
                    if not environment_matches or fingerprint != run["environment"].get("datasetFingerprint"):
                        run["cancelRequested"] = True
                        for attempt in s.attempts(c, scope, run["id"]):
                            if attempt["status"] == "queued":
                                attempt["status"] = "interrupted"
                                put("attempts", attempt)
                        put("runs", run)
                        continue
                    active = sum(a["status"] in {"claimed", "running"} for a in s.attempts(c, scope, run["id"]))
                    if active >= run["profile"]["concurrency"]:
                        continue
                    for attempt in s.attempts(c, scope, run["id"]):
                        if attempt["status"] != "queued":
                            continue
                        case = next(x for x in run["suite"]["cases"] if x["id"] == attempt["caseId"])
                        if case["role"] not in worker["roles"]:
                            continue
                        if worker["capabilities"]["roleSubjects"].get(case["role"]) != run["environment"]["roleSubjects"].get(case["role"]):
                            continue
                        if run["environment"].get("scorerSubject") and worker["capabilities"].get("scorerSubject") != run["environment"]["scorerSubject"]:
                            continue
                        attempt.update(status="claimed", workerId=worker["id"], workerSubjectId=auth.subject.id, leaseToken=secrets.token_urlsafe(32), expiresAt=s.expires(120), executionDeadline=s.expires(run["profile"]["timeoutSeconds"] + 120))
                        put("attempts", attempt)
                        run["status"] = "running"
                        put("runs", run)
                        return 200, {"job": {"attemptId": attempt["id"], "runId": run["id"], "leaseToken": attempt["leaseToken"], "case": case, "profile": run["profile"], "environment": run["environment"], "expiresAt": attempt["expiresAt"]}}
                return 200, {"job": None}
        if r == ["runs"]:
            if method == "GET":
                return 200, {"schemaVersion": 1, "items": [self._public_run(s.summarize(c, scope, run)) for run in items("runs")]}
            if method == "POST":
                if set(b) - {"suiteId", "profileId", "caseIds"}:
                    invalid("run fields are invalid")
                settings = get("settings", "settings")
                if not settings.get("enabled") or not settings.get("testDataOnly"):
                    invalid("enable evaluation for a test project first", 409)
                if s.active(c, scope):
                    invalid("a project evaluation is already active", 409)
                suite, profile = (source_run["suite"], source_run["profile"]) if source_run else (get("suites", identifier(b.get("suiteId"))), get("profiles", identifier(b.get("profileId"))))
                if suite["status"] != "published" or profile["status"] != "published":
                    invalid("publish suite and profile first", 409)
                selected = b.get("caseIds")
                if selected is not None:
                    if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected) or not set(selected) <= {x["id"] for x in suite["cases"]}:
                        invalid("case selection is invalid")
                    suite = {**suite, "cases": [x for x in suite["cases"] if x["id"] in selected]}
                roles = {x["role"] for x in suite["cases"]}
                workers = [w for w in items("workers") if w["expiresAt"] > s.now()]
                if not roles <= {role for w in workers for role in w["roles"]}:
                    invalid("an online worker must provide every test role", 409)
                environment = self._environment(c, scope, suite["testDatasetId"])
                fingerprints = {w.get("capabilities", {}).get("datasetFingerprints", {}).get(suite["testDatasetId"]) for w in workers}
                if None in fingerprints or len(fingerprints) != 1:
                    invalid("online workers must report one matching test dataset fingerprint", 409)
                environment["datasetFingerprint"] = fingerprints.pop()
                subjects: dict[str, str] = {}
                for worker in workers:
                    for role, subject in worker["capabilities"].get("roleSubjects", {}).items():
                        if role in roles:
                            if role in subjects and subjects[role] != subject:
                                invalid("workers disagree on role identity", 409)
                            subjects[role] = subject
                environment["roleSubjects"] = subjects
                environment["policyVersions"] = self._role_versions(scope, subjects)
                environment["roleContextHashes"] = self._role_contexts(scope, subjects)
                if any(case.get("referenceSql") for case in suite["cases"]):
                    scorers = {w.get("capabilities", {}).get("scorerSubject") for w in workers}
                    if None in scorers or len(scorers) != 1:
                        invalid("reference queries require one independent managed scorer identity", 409)
                    scorer = scorers.pop()
                    binding = environment.get("datasetScorerBinding") or {}
                    if not environment.get("datasetScorerBindingValid") or binding.get("subjectId") != scorer:
                        invalid("register the matching scorer for this dataset version and datasource", 409)
                    environment["scorerSubject"] = scorer
                    environment["scorerPolicyVersions"] = self._role_versions(scope, {"scorer": scorer})
                    environment["scorerContextHash"] = self._role_contexts(scope, {"scorer": scorer})["scorer"]
                environment["workerVersions"] = sorted({w["version"] for w in workers})
                environment["cliVersions"] = sorted({w["cliVersion"] for w in workers})
                environment["promptSha256"] = hashlib.sha256(profile["prompt"].encode()).hexdigest()
                run = {"schemaVersion": 1, "id": uuid.uuid4().hex, "status": "queued", "suiteId": suite["id"], "suiteRevision": suite["revision"], "profileId": profile["id"], "profileRevision": profile["revision"], "suite": suite, "profile": profile, "environment": environment, "createdAt": s.now(), "createdBy": auth.subject.id, "cancelRequested": False, "baseline": False}
                if source_run:
                    run["sourceRunId"] = source_run["id"]
                put("runs", run)
                for case in suite["cases"]:
                    for round in range(1, profile["rounds"] + 1):
                        put("attempts", {"id": uuid.uuid4().hex, "runId": run["id"], "caseId": case["id"], "round": round, "status": "queued", "review": {"status": "pending", "history": []}})
                return 202, self._public_run(s.summarize(c, scope, run))
        if len(r) >= 2 and r[0] == "runs":
            run = get("runs", identifier(r[1]))
            if len(r) == 2 and method == "GET":
                return 200, self._public_run(s.summarize(c, scope, run))
            if len(r) == 3 and r[2] == "export" and method == "GET":
                return 200, self._public_run(s.summarize(c, scope, run))
            if len(r) == 3 and r[2] == "cancel" and method == "POST":
                run["cancelRequested"] = True
                put("runs", run)
                for attempt in s.attempts(c, scope, run["id"]):
                    if attempt["status"] == "queued":
                        attempt["status"] = "cancelled"
                        put("attempts", attempt)
                return 202, self._public_run(s.summarize(c, scope, run))
            if len(r) == 3 and r[2] == "baseline" and method == "POST":
                full = s.summarize(c, scope, run)
                if full["status"] != "completed" or any(a["status"] != "completed" for a in full["attempts"]):
                    invalid("baseline must be fully executed", 409)
                run["baseline"] = True
                put("runs", run)
                return 200, self._public_run(s.summarize(c, scope, run))
        if len(r) == 3 and r[0] == "attempts":
            attempt = get("attempts", identifier(r[1]))
            run = get("runs", attempt["runId"])
            operation = r[2]
            if operation in {"heartbeat", "events", "complete"} and method == "POST":
                if attempt.get("workerSubjectId") != auth.subject.id or attempt.get("workerId") != b.get("workerId") or not secrets.compare_digest(attempt.get("leaseToken", ""), str(b.get("leaseToken", ""))):
                    invalid("attempt lease does not belong to this worker", 403)
                if operation == "complete":
                    case = next(x for x in run["suite"]["cases"] if x["id"] == attempt["caseId"])
                    if attempt["status"] in TERMINAL:
                        return 200, s.complete(c, scope, attempt, b, case)
                    if not self._environment_matches(c, scope, run["environment"]):
                        case = {**case, "_environmentDrift": True}
                        attempt["environmentStatus"] = "identity_drift"
                    evidence = b.get("evidence", {})
                    ids = evidence.get("coreTraceIds", []) if isinstance(evidence, dict) else []
                    if not isinstance(ids, list) or len(ids) > 200:
                        invalid("Core trace references are invalid")
                    for trace_id in ids:
                        row = c.execute("SELECT subject_id FROM query_diagnostics WHERE trace_id=? AND organization_id=? AND project_id=? AND expires_at>?", (trace_id, *scope, s.now())).fetchone()
                        if row is None or row["subject_id"] != run["environment"]["roleSubjects"][case["role"]]:
                            invalid("Core trace ownership could not be verified", 403)
                    result = s.complete(c, scope, attempt, b, case)
                    s.summarize(c, scope, run)
                    return 200, result
                if attempt["status"] in TERMINAL:
                    invalid("attempt is already terminal", 409)
                if operation == "events":
                    events = b.get("events")
                    if not isinstance(events, list) or len(events) > 200:
                        invalid("event batch must contain at most 200 events")
                    previous = c.execute("SELECT body_json FROM evaluation_evidence WHERE organization_id=? AND project_id=? AND attempt_id=?", (*scope, attempt["id"])).fetchone()
                    content = json.loads(previous["body_json"]) if previous else {"events": []}
                    content["events"].extend(scrub_evidence(events))
                    encoded = json.dumps(content, allow_nan=False)
                    if len(encoded.encode()) > 2_000_000:
                        invalid("attempt evidence exceeds limit")
                    c.execute("DELETE FROM evaluation_evidence WHERE organization_id=? AND project_id=? AND attempt_id=?", (*scope, attempt["id"]))
                    c.execute("INSERT INTO evaluation_evidence(organization_id,project_id,attempt_id,body_json,expires_at) VALUES(?,?,?,?,?)", (*scope, attempt["id"], encoded, s.expires(30 * 86400)))
                if not self._environment_matches(c, scope, run["environment"]):
                    run["cancelRequested"] = True
                    put("runs", run)
                    for queued in s.attempts(c, scope, run["id"]):
                        if queued["status"] == "queued":
                            queued["status"] = "interrupted"
                            put("attempts", queued)
                attempt["expiresAt"] = min(s.expires(120), attempt["executionDeadline"])
                if b.get("started") is True:
                    if b.get("datasetFingerprint") != run["environment"].get("datasetFingerprint"):
                        invalid("test data fingerprint changed before execution", 409)
                    attempt.setdefault("startedAt", s.now())
                    attempt["status"] = "running"
                put("attempts", attempt)
                return 200, {"cancelRequested": run["cancelRequested"], "expiresAt": attempt["expiresAt"]}
            if operation == "review" and method == "POST":
                if b.get("status") not in {"passed", "failed", "pending"}:
                    invalid("review status is invalid")
                reason = text(b.get("reason"), "reason", 4000)
                entry = {"status": b["status"], "reason": reason, "reviewedBy": auth.subject.id, "reviewedAt": s.now()}
                history = attempt["review"]["history"] + [entry]
                attempt["review"] = {**entry, "history": history}
                put("attempts", attempt)
                return 200, attempt["review"]
            if operation == "evidence":
                if method == "DELETE":
                    c.execute("DELETE FROM evaluation_evidence WHERE organization_id=? AND project_id=? AND attempt_id=?", (*scope, attempt["id"]))
                    return 200, {"deleted": True}
                if method == "GET":
                    row = c.execute("SELECT body_json,expires_at FROM evaluation_evidence WHERE organization_id=? AND project_id=? AND attempt_id=?", (*scope, attempt["id"])).fetchone()
                    return (200, {"schemaVersion": 1, "evidence": json.loads(row["body_json"]), "expiresAt": row["expires_at"]}) if row else (410, {"code": "EVIDENCE_UNAVAILABLE", "message": "evidence expired, deleted or not collected"})
        if r == ["compare"] and method == "GET":
            left, right = get("runs", identifier(q.get("baselineId"))), get("runs", identifier(q.get("candidateId")))
            same = all(left.get(k) == right.get(k) for k in ("suiteId", "suiteRevision")) and all(left["environment"].get(k) == right["environment"].get(k) for k in ("datasetId", "datasetVersion", "datasetFingerprint", "scorerVersion"))
            la = {(a["caseId"], a["round"]): a for a in s.attempts(c, scope, left["id"])}
            ra = {(a["caseId"], a["round"]): a for a in s.attempts(c, scope, right["id"])}
            same = same and set(la) == set(ra) and left["environment"].get("datasourceId") == right["environment"].get("datasourceId")
            pairs = []
            for key in sorted(set(la) | set(ra)):
                a, z = la.get(key, {}), ra.get(key, {})
                ls, rs = a.get("score", {}).get("status"), z.get("score", {}).get("status")
                category = "unmatched"
                if same and ls in {"passed", "failed"} and rs in {"passed", "failed"}:
                    category = {("passed", "passed"): "stable_pass", ("passed", "failed"): "regression", ("failed", "passed"): "fixed", ("failed", "failed"): "persistent_failure"}[(ls, rs)]
                pairs.append({"caseId": key[0], "round": key[1], "baselineAttemptId": a.get("id"), "candidateAttemptId": z.get("id"), "baselineStatus": ls, "candidateStatus": rs, "category": category, "baselineReview": a.get("review"), "candidateReview": z.get("review")})
            changes = {k: {"baseline": left["environment"].get(k), "candidate": right["environment"].get(k)} for k in set(left["environment"]) | set(right["environment"]) if left["environment"].get(k) != right["environment"].get(k)}
            for k in ("model", "prompt", "reasoningEffort", "contextVersion"):
                if left["profile"].get(k) != right["profile"].get(k):
                    changes[k] = {"baseline": left["profile"].get(k), "candidate": right["profile"].get(k)}
            return 200, {"schemaVersion": 1, "strict": same, "mode": "strict" if same else "exploratory", "changes": changes, "items": pairs}
        return 404, {"code": "NOT_FOUND", "message": "evaluation endpoint not found"}

    @staticmethod
    def _public_run(run: dict[str, Any]) -> dict[str, Any]:
        # Oracle and reference SQL remain available only to trusted workers and suite admins.
        return {**{k: v for k, v in run.items() if k not in {"suite", "profile"}}, "agentConfig": run.get("profile"), "cases": [{k: v for k, v in case.items() if k in {"id", "question", "role", "tags", "reviewGuidance", "expectedBehavior"}} for case in run.get("suite", {}).get("cases", [])]}
