from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import experiment
import app_dispatch
from runner import render_prompt
from review import (APP_STORED_TRANSPORT_DISPATCH_MODE, APP_STORED_TRANSPORT_MODE,
                    _app_bootstrap_requirement, audit_trace,
                    audit_wrapper,
                    public_prompt_stored_transport_launcher,
                    public_prompt_stored_transport_message,
                    stored_transport_config, stored_transport_prompt,
                    stored_transport_wrapper)
import transport_smoke


class AppBootstrapAuditTests(unittest.TestCase):
    thread_id = "01a12bd8-80ec-7c53-aea3-2dea074af449"
    nonce = "a" * 32

    def _make_trace(self, root: Path, *, bootstrap: bool = True,
                    bootstrap_output: str | None = None,
                    bootstrap_exit_code: int = 0,
                    bootstrap_session_id: int | None = None,
                    bootstrap_output_missing: bool = False,
                    duplicate_bootstrap: bool = False,
                    bootstrap_after_begin: bool = False,
                    bootstrap_path: Path | None = None,
                    setup_prompt_path: Path | None = None,
                    setup_prompt_sha256: str | None = None,
                    setup_launcher_sha256: str | None = None,
                    trace_client_override: Path | None = None,
                    template_override: str | None = None,
                    setup_schema_version: int = 2,
                    setup_bootstrap_mode: str | None = None,
                    setup_dispatch_mode: str | None = None
                    ) -> tuple[Path, Path, Path]:
        run_dir = root / "run"
        run_dir.mkdir(parents=True)
        worker_root = root / "external-worker"
        worker_root.mkdir(parents=True)
        worker_dir = worker_root / "job-001"
        worker_dir.mkdir()
        prompts_dir = worker_root / "prompts"
        prompts_dir.mkdir()
        prompt_path = prompts_dir / "job-001.md"
        template_path = HERE / "worker-prompt.md"
        template_bytes = (template_override.encode("utf-8") if template_override is not None
            else template_path.read_bytes())
        template = template_bytes.decode("utf-8")
        python = (root / "python.exe").resolve()
        client = (experiment.REPO_ROOT
            / "benchmarks/business-trust-v1/worker_client.py").resolve()
        broker_port = 48785
        base_prompt = render_prompt(template, worker_dir=worker_dir,
            job_id="job-001", broker_port=broker_port, python=str(python),
            client=client, nonce=self.nonce).replace("\r\n", "\n").replace("\r", "\n")
        stored_transport = setup_schema_version == 3
        config = (stored_transport_config(run_id="bootstrap-audit", job_id="job-001",
            nonce=self.nonce, python=python, worker_client=client,
            worker_dir=worker_dir, broker_port=broker_port) if stored_transport else None)
        self.public_prompt = (stored_transport_prompt(base_prompt, config)
            if stored_transport else base_prompt)
        prompt_bytes = self.public_prompt.encode("utf-8")
        prompt_path.write_bytes(prompt_bytes)

        launcher_dir = run_dir / "app-prompts"
        launcher_dir.mkdir()
        launcher_path = launcher_dir / "job-001.md"
        actual_bootstrap_source = (public_prompt_stored_transport_launcher(
            prompt_path, worker_dir, config) if stored_transport
            else self._bootstrap_source(prompt_path, worker_dir))
        launcher_text = (public_prompt_stored_transport_message(
            prompt_path, worker_dir, config) if stored_transport
            else self._launcher(actual_bootstrap_source))
        launcher_path.write_bytes(launcher_text.encode("utf-8"))

        actual_prompt_path = setup_prompt_path or prompt_path
        actual_prompt_hash = setup_prompt_sha256 or hashlib.sha256(prompt_bytes).hexdigest()
        actual_launcher_hash = setup_launcher_sha256 or hashlib.sha256(
            launcher_text.encode("utf-8")).hexdigest()
        trace_events: list[dict] = [
            {"type": "session_meta", "payload": {"id": self.thread_id}},
            {"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "max"}},
        ]
        call_index = 0
        bootstrap_events: list[dict] = []

        def append_source(source: str, *, operation: str, output: str = "",
                          exit_code: int = 0, session_id: int | None = None,
                          omit_output: bool = False) -> None:
            nonlocal call_index
            call_index += 1
            call_id = f"exec-{call_index}"
            trace_events.append({"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": call_id,
                "input": source,
            }})
            if omit_output:
                return
            metadata = {"chunk_id": f"chunk-{call_index}", "wall_time_seconds": 0.01,
                "original_token_count": 0, "output": output}
            if session_id is not None:
                metadata["session_id"] = session_id
            else:
                metadata["exit_code"] = exit_code
            trace_events.append({"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": call_id,
                "output": [{"type": "input_text", "text": json.dumps(metadata)}],
            }})

        if bootstrap:
            chosen_path = bootstrap_path or prompt_path
            source = (public_prompt_stored_transport_launcher(
                chosen_path, worker_dir, config) if stored_transport
                else self._bootstrap_source(chosen_path, worker_dir)) + "\n"
            for _ in range(2 if duplicate_bootstrap else 1):
                append_source(source, operation="bootstrap",
                    output=self.public_prompt,
                    exit_code=bootstrap_exit_code, session_id=bootstrap_session_id,
                    omit_output=bootstrap_output_missing)
            bootstrap_events = trace_events[2:]
            del trace_events[2:]

        def transport(operation: str, request: dict, *, output: str = "",
                      session_id: int | None = None) -> None:
            if stored_transport:
                assert config is not None
                source = stored_transport_wrapper(config, operation, request)
                append_source(source, operation=operation, output=output,
                    session_id=session_id)
                return
            command_client = trace_client_override or client
            command = (f"@'\n{json.dumps(request, separators=(',', ':'))}\n'@ | & "
                f"'{python}' -X utf8 '{command_client}' job-001 {operation} - --port {broker_port}")
            source = "text(await tools.exec_command(" + json.dumps({
                "cmd": command, "workdir": str(worker_dir), "max_output_tokens": 12000,
            }) + "));"
            append_source(source, operation=operation, output=output,
                session_id=session_id)

        transport("begin", {"nonce": self.nonce})
        if bootstrap_after_begin:
            trace_events[4:4] = bootstrap_events
        elif bootstrap:
            trace_events[2:2] = bootstrap_events
        transport("tool", {"name": "semarail_get_context", "arguments": {
            "question": "smoke question", "context_version": 2}}, session_id=12345)
        poll_id = "poll-1"
        trace_events.append({"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": poll_id,
            "input": "text(await tools.write_stdin(" + json.dumps({
                "session_id": 12345, "chars": "", "yield_time_ms": 30000,
                "max_output_tokens": 12000,
            }) + "));",
        }})
        poll_metadata = {"chunk_id": "chunk-poll", "wall_time_seconds": 0.02,
            "exit_code": 0, "original_token_count": 0, "output": ""}
        trace_events.append({"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": poll_id,
            "output": [{"type": "input_text", "text": json.dumps(poll_metadata)}],
        }})
        transport("final", {"answer": "Done", "rows": [], "columns": [],
            "scope": "authorized", "conditions": [], "uncertain": False})

        trace = run_dir / "traces" / "job-001.jsonl"
        trace.parent.mkdir()
        trace_bytes = b"\n".join(json.dumps(event, ensure_ascii=False).encode("utf-8")
            for event in trace_events) + b"\n"
        trace.write_bytes(trace_bytes)
        (run_dir / "worker-prompt.md").write_bytes(template_bytes)
        (run_dir / "app-dispatch-start.json").write_text(json.dumps({
            "schemaVersion": setup_schema_version,
            "dispatchMode": setup_dispatch_mode or (APP_STORED_TRANSPORT_DISPATCH_MODE
                if stored_transport else "Codex app fresh-session worker prompts via public-file bootstrap"),
            "bootstrapMode": setup_bootstrap_mode or (APP_STORED_TRANSPORT_MODE
                if stored_transport else "public-file-one-read-v1"),
            "workerDirectories": {"job-001": str(worker_dir)},
            "workerRoot": str(worker_root),
            "publicPromptDirectory": str(prompts_dir),
            "launcherDirectory": str(launcher_dir),
            "pythonPath": str(python),
            "workerClientPath": str(client),
            "brokerPort": broker_port,
            "promptTemplateSha256": hashlib.sha256(template_bytes).hexdigest(),
            "selectedJobIds": ["job-001"],
            "publicPromptFiles": {"job-001": {
                "path": str(actual_prompt_path), "sha256": actual_prompt_hash}},
            "launcherFiles": {"job-001": {
                "path": str(launcher_path), "sha256": actual_launcher_hash}},
            "promptHashes": {"job-001": actual_prompt_hash},
            "launcherHashes": {"job-001": actual_launcher_hash},
            **({"storedTransportConfigs": {"job-001": config}}
                if stored_transport else {}),
        }), encoding="utf-8")
        (run_dir / "dispatch-nonces.json").write_text(json.dumps({"job-001": self.nonce}),
            encoding="utf-8")
        (run_dir / "app-imports").mkdir()
        (run_dir / "app-imports/job-001.json").write_text(json.dumps({
            "threadId": self.thread_id,
            "traceSha256": hashlib.sha256(trace_bytes).hexdigest(),
        }), encoding="utf-8")
        (run_dir / "records/job-001").mkdir(parents=True)
        (run_dir / "records/job-001/0000.json").write_text(json.dumps({
            "workerThreadId": self.thread_id, "status": "complete",
        }), encoding="utf-8")
        (run_dir / "manifest.json").write_text(json.dumps({
            "runId": "bootstrap-audit",
            "model": {"requested": "gpt-6-luna", "reasoningRequested": "max"},
            "ports": {"broker": broker_port}, "maxToolCalls": 12,
            "candidate": {"path": str(experiment.REPO_ROOT)},
            "promptTemplateSha256": hashlib.sha256(template_bytes).hexdigest(),
        }), encoding="utf-8")
        (run_dir / "schedule.json").write_text(json.dumps([{"id": "job-001"}]),
            encoding="utf-8")
        return run_dir, trace, prompt_path

    @staticmethod
    def _bootstrap_source(prompt_path: Path, worker_dir: Path) -> str:
        quoted_path = prompt_path.resolve().as_posix().replace("'", "''")
        command = (
            "[Console]::Write([IO.File]::ReadAllText('" + quoted_path
            + "',[Text.Encoding]::UTF8))"
        )
        return "text(await tools.exec_command(" + json.dumps({
            "cmd": command, "workdir": str(worker_dir.resolve()),
            "max_output_tokens": 12000,
        }) + "));"

    @staticmethod
    def _launcher(source: str) -> str:
        return (
            "Before reading the participant prompt, use no other tools, files, environment, or network. "
            "Make exactly this one functions.exec call to read the public prompt:\n\n"
            "```js\n" + source + "\n```\n\n"
            "Then follow the returned prompt exactly and use only its specified transport. "
            "Do not repeat the read or access another file.\n"
        )

    def _audit(self, root: Path, **kwargs) -> tuple[dict, Path, Path]:
        bootstrap_output = kwargs.pop("bootstrap_output", None)
        run_dir, trace, prompt_path = self._make_trace(root, **kwargs)
        if bootstrap_output is not None:
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            output_event = next(event for event in events
                if event.get("type") == "response_item"
                and event.get("payload", {}).get("type") == "custom_tool_call_output")
            block = output_event["payload"]["output"][0]
            metadata = json.loads(block["text"])
            metadata["output"] = bootstrap_output
            block["text"] = json.dumps(metadata)
            trace_bytes = b"\n".join(json.dumps(event, ensure_ascii=False).encode("utf-8")
                for event in events) + b"\n"
            trace.write_bytes(trace_bytes)
            imported = run_dir / "app-imports/job-001.json"
            evidence = json.loads(imported.read_text(encoding="utf-8"))
            evidence["traceSha256"] = hashlib.sha256(trace_bytes).hexdigest()
            imported.write_text(json.dumps(evidence), encoding="utf-8")
        return audit_trace(trace, run_dir=run_dir, job_id="job-001"), run_dir, prompt_path

    def test_bootstrap_is_required_first_and_valid_sequence_keeps_poll_audit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-valid-") as temp:
            result, _, _ = self._audit(Path(temp))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["operations"], ["begin", "tool", "final"])
        self.assertEqual(result["toolCalls"], ["semarail_get_context"])
        self.assertTrue(result["publicPromptReceiptVerified"])
        self.assertFalse(result["sentInputIndependentlyByteVerified"])
        self.assertEqual(result["publicPromptSha256"], result["publicPromptReceiptSha256"])
        self.assertNotEqual(result["publicPromptSha256"], result["bootstrapLauncherSha256"])

    def test_real_app_bootstrap_exec_allows_trailing_newline_but_rejects_extra_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-outer-newline-") as temp:
            run_dir, trace, _ = self._make_trace(Path(temp))
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            bootstrap_call = next(event["payload"] for event in events
                if event.get("type") == "response_item"
                and event.get("payload", {}).get("type") == "custom_tool_call")
            source = bootstrap_call["input"]
            self.assertTrue(source.endswith("));\n"))
            self.assertEqual(audit_trace(trace, run_dir=run_dir, job_id="job-001")["status"], "passed")

            bootstrap_call["input"] = source + "unapproved"
            trace_bytes = b"\n".join(json.dumps(event, ensure_ascii=False).encode("utf-8")
                for event in events) + b"\n"
            trace.write_bytes(trace_bytes)
            import_path = run_dir / "app-imports/job-001.json"
            evidence = json.loads(import_path.read_text(encoding="utf-8"))
            evidence["traceSha256"] = hashlib.sha256(trace_bytes).hexdigest()
            import_path.write_text(json.dumps(evidence), encoding="utf-8")
            result = audit_trace(trace, run_dir=run_dir, job_id="job-001")
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("unrecognized transport wrapper" in reason
                for reason in result["reasons"]))

    def test_bootstrap_after_begin_fails_and_only_known_setup_versions_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-order-") as temp:
            result, _, _ = self._audit(Path(temp), bootstrap_after_begin=True)
        self.assertEqual(result["status"], "failed")
        for options in (
            {"setup_schema_version": 4},
            {"setup_schema_version": None},
            {"setup_schema_version": True},
            {"setup_schema_version": 2.0},
            {"setup_schema_version": 1.0},
            {"setup_bootstrap_mode": "arbitrary-file-read"},
            {"setup_dispatch_mode": "legacy-full-prompt"},
        ):
            with self.subTest(options=options), tempfile.TemporaryDirectory(prefix="btv-bootstrap-version-") as temp:
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    self._audit(Path(temp), **options)

    def test_missing_bootstrap_fails_before_the_existing_sequence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-missing-") as temp:
            result, _, _ = self._audit(Path(temp), bootstrap=False)
        self.assertEqual(result["status"], "failed")

    def test_schema3_stored_transport_valid_receipt_and_sequence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-stored-transport-valid-") as temp:
            result, _run_dir, prompt_path = self._audit(
                Path(temp), setup_schema_version=3)
            public_prompt = prompt_path.read_text(encoding="utf-8")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["operations"], ["begin", "tool", "final"])
        self.assertEqual(result["toolCalls"], ["semarail_get_context"])
        self.assertTrue(result["publicPromptReceiptVerified"])
        self.assertEqual(result["publicPromptSha256"], result["publicPromptReceiptSha256"])
        self.assertRegex(result["storedTransportConfigSha256"], r"^[0-9a-f]{64}$")
        self.assertIn("semarail_list_models", public_prompt)
        self.assertIn("exact original public condition tag strings", public_prompt)
        self.assertIn("const c=load(\"", public_prompt)

    def test_schema3_read_receipt_requires_flat_successful_exec_metadata(self) -> None:
        cases = [
            ({"bootstrap_exit_code": 1}, "bootstrap read command failed"),
            ({"bootstrap_session_id": 712}, "unexpectedly started an executor session"),
            ({"bootstrap_output": "different public prompt"}, "differs from the public prompt"),
            ({"bootstrap_output_missing": True}, "executor output"),
            ({"bootstrap_output": json.dumps({"chunk_id": "nested", "exit_code": 0,
                "wall_time_seconds": 0.01, "output": "nested receipt"})},
                "differs from the public prompt"),
        ]
        for options, message in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory(
                    prefix="btv-stored-receipt-reject-") as temp:
                result, _run_dir, _prompt_path = self._audit(
                    Path(temp), setup_schema_version=3, **options)
                self.assertEqual(result["status"], "failed")
                self.assertTrue(any(message in reason for reason in result["reasons"]))

    def test_schema3_rejects_bootstrap_or_transport_source_mutations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-stored-js-tamper-") as temp:
            run_dir, trace, _ = self._make_trace(Path(temp), setup_schema_version=3)
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            call = next(event["payload"] for event in events
                if event.get("type") == "response_item"
                and event.get("payload", {}).get("type") == "custom_tool_call")
            call["input"] += "store('extra',{});"
            trace_bytes = b"\n".join(json.dumps(event, ensure_ascii=False).encode("utf-8")
                for event in events) + b"\n"
            trace.write_bytes(trace_bytes)
            import_path = run_dir / "app-imports/job-001.json"
            imported = json.loads(import_path.read_text(encoding="utf-8"))
            imported["traceSha256"] = hashlib.sha256(trace_bytes).hexdigest()
            import_path.write_text(json.dumps(imported), encoding="utf-8")
            result = audit_trace(trace, run_dir=run_dir, job_id="job-001")
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("unrecognized transport wrapper" in reason
                or "stored transport wrapper" in reason for reason in result["reasons"]))

    def test_schema3_stored_config_is_reconstructed_and_legacy_schema_rejects_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-stored-config-tamper-") as temp:
            run_dir, trace, _ = self._make_trace(Path(temp), setup_schema_version=3)
            setup_path = run_dir / "app-dispatch-start.json"
            setup = json.loads(setup_path.read_text(encoding="utf-8"))
            setup["storedTransportConfigs"]["job-001"]["brokerPort"] += 1
            setup_path.write_text(json.dumps(setup), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "configuration differs"):
                audit_trace(trace, run_dir=run_dir, job_id="job-001")

        with tempfile.TemporaryDirectory(prefix="btv-stored-schema-confusion-") as temp:
            run_dir, trace, _ = self._make_trace(Path(temp))
            setup_path = run_dir / "app-dispatch-start.json"
            setup = json.loads(setup_path.read_text(encoding="utf-8"))
            setup["storedTransportConfigs"] = {"job-001": {}}
            setup_path.write_text(json.dumps(setup), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "legacy app setup"):
                audit_trace(trace, run_dir=run_dir, job_id="job-001")

    def test_schema3_accepts_equivalent_json_encoding_but_rejects_js_additions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-stored-json-syntax-") as temp:
            root = Path(temp)
            worker_dir = root / "job-001"
            worker_dir.mkdir()
            python = root / "python.exe"
            client = (experiment.REPO_ROOT
                / "benchmarks/business-trust-v1/worker_client.py").resolve()
            config = stored_transport_config(run_id="json-syntax", job_id="job-001",
                nonce=self.nonce, python=python, worker_client=client,
                worker_dir=worker_dir, broker_port=48785)
            request = {"answer": "结果", "rows": [{"n": 1.0, "label": "中"}],
                "columns": [], "scope": "authorized", "conditions": [],
                "uncertain": False}
            source = stored_transport_wrapper(config, "final", request)
            canonical = json.dumps(request, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False)
            encoded = (r'{ "answer" : "\u7ed3\u679c", "rows" : '
                r'[ { "n" : 1.0, "label" : "\u4e2d" } ], "columns" : [], '
                r'"scope" : "authorized", "conditions" : [], "uncertain" : false }')
            source = source.replace(canonical, encoded, 1)
            args = {"job_id": "job-001", "worker_dir": worker_dir,
                "python": python, "worker_client": client, "broker_port": 48785,
                "expected_nonce": self.nonce, "stored_transport_config": config}
            self.assertEqual(audit_wrapper(source, **args), ("final", None))
            with self.assertRaisesRegex(ValueError, "stored transport wrapper"):
                audit_wrapper(source + ";store('unexpected',{});", **args)
            proto_request = source.replace('"scope"', '"__proto__":{"x":1},"scope"', 1)
            with self.assertRaisesRegex(ValueError, "prototype-mutating"):
                audit_wrapper(proto_request, **args)

    def test_schema3_rejects_wrong_store_key_and_missing_final_or_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-stored-key-tamper-") as temp:
            run_dir, trace, _ = self._make_trace(Path(temp), setup_schema_version=3)
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            tool_call = next(event["payload"] for event in events
                if event.get("type") == "response_item"
                and event.get("payload", {}).get("type") == "custom_tool_call"
                and "const request={\"name\"" in event["payload"].get("input", ""))
            tool_call["input"] = tool_call["input"].replace(
                "btv-stored-v1-", "btv-other-v1-", 1)
            trace_bytes = b"\n".join(json.dumps(event, ensure_ascii=False).encode("utf-8")
                for event in events) + b"\n"
            trace.write_bytes(trace_bytes)
            import_path = run_dir / "app-imports/job-001.json"
            imported = json.loads(import_path.read_text(encoding="utf-8"))
            imported["traceSha256"] = hashlib.sha256(trace_bytes).hexdigest()
            import_path.write_text(json.dumps(imported), encoding="utf-8")
            result = audit_trace(trace, run_dir=run_dir, job_id="job-001")
            self.assertEqual(result["status"], "failed")

        for options in ({"bootstrap": False}, {"duplicate_bootstrap": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory(
                    prefix="btv-stored-bootstrap-count-") as temp:
                result, _, _ = self._audit(Path(temp), setup_schema_version=3, **options)
                self.assertEqual(result["status"], "failed")

        with tempfile.TemporaryDirectory(prefix="btv-stored-final-missing-") as temp:
            run_dir, trace, _ = self._make_trace(Path(temp), setup_schema_version=3)
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            final_call = next(event["payload"] for event in events
                if event.get("type") == "response_item"
                and event.get("payload", {}).get("type") == "custom_tool_call"
                and '"answer":"Done"' in event["payload"].get("input", ""))
            final_call_id = final_call["call_id"]
            events = [event for event in events
                if not (event.get("type") == "response_item"
                    and event.get("payload", {}).get("call_id") == final_call_id)]
            trace_bytes = b"\n".join(json.dumps(event, ensure_ascii=False).encode("utf-8")
                for event in events) + b"\n"
            trace.write_bytes(trace_bytes)
            import_path = run_dir / "app-imports/job-001.json"
            imported = json.loads(import_path.read_text(encoding="utf-8"))
            imported["traceSha256"] = hashlib.sha256(trace_bytes).hexdigest()
            import_path.write_text(json.dumps(imported), encoding="utf-8")
            result = audit_trace(trace, run_dir=run_dir, job_id="job-001")
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("final" in reason for reason in result["reasons"]))

    def test_wrong_public_file_path_or_hash_fails_setup_audit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-path-") as temp:
            root = Path(temp)
            private = root / "private-source.txt"
            private.write_text("private source", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "public prompt"):
                self._audit(root, setup_prompt_path=private)
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-hash-") as temp:
            with self.assertRaisesRegex(ValueError, "hash"):
                self._audit(Path(temp), setup_prompt_sha256="0" * 64)

    def test_changed_prompt_and_matching_setup_hashes_still_fail_frozen_rendering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-mutual-tamper-") as temp:
            run_dir, trace, prompt_path = self._make_trace(Path(temp))
            changed = prompt_path.read_bytes() + b"coordinated prompt and setup tamper\n"
            prompt_path.write_bytes(changed)
            setup_path = run_dir / "app-dispatch-start.json"
            setup = json.loads(setup_path.read_text(encoding="utf-8"))
            digest = hashlib.sha256(changed).hexdigest()
            setup["publicPromptFiles"]["job-001"]["sha256"] = digest
            setup["promptHashes"]["job-001"] = digest
            setup_path.write_text(json.dumps(setup), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen rendering"):
                audit_trace(trace, run_dir=run_dir, job_id="job-001")

    def test_extra_public_prompt_or_launcher_is_rejected_even_when_known_files_are_valid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-extra-file-") as temp:
            run_dir, trace, prompt_path = self._make_trace(Path(temp))
            (prompt_path.parent / "unexpected.md").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "directory contents differ"):
                audit_trace(trace, run_dir=run_dir, job_id="job-001")

        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-extra-launcher-") as temp:
            run_dir, trace, _ = self._make_trace(Path(temp))
            (run_dir / "app-prompts/unexpected.md").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "directory contents differ"):
                audit_trace(trace, run_dir=run_dir, job_id="job-001")

    def test_changed_launcher_and_matching_setup_hashes_still_fail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-launcher-tamper-") as temp:
            run_dir, trace, _ = self._make_trace(Path(temp))
            launcher_path = run_dir / "app-prompts/job-001.md"
            changed = launcher_path.read_bytes() + b"coordinated launcher and setup tamper\n"
            launcher_path.write_bytes(changed)
            setup_path = run_dir / "app-dispatch-start.json"
            setup = json.loads(setup_path.read_text(encoding="utf-8"))
            digest = hashlib.sha256(changed).hexdigest()
            setup["launcherFiles"]["job-001"]["sha256"] = digest
            setup["launcherHashes"]["job-001"] = digest
            setup_path.write_text(json.dumps(setup), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "launcher content or hash"):
                audit_trace(trace, run_dir=run_dir, job_id="job-001")

    def test_public_prompt_symlink_cannot_escape_into_candidate_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-symlink-") as temp:
            run_dir, trace, prompt_path = self._make_trace(Path(temp))
            prompt_path.unlink()
            try:
                prompt_path.symlink_to(experiment.REPO_ROOT / "AGENTS.md")
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
                audit_trace(trace, run_dir=run_dir, job_id="job-001")

    def test_wrong_launcher_hash_fails_setup_audit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-launcher-") as temp:
            with self.assertRaisesRegex(ValueError, "launcher"):
                self._audit(Path(temp), setup_launcher_sha256="0" * 64)

    def test_crlf_transport_newlines_are_normalized_but_content_changes_fail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-crlf-") as temp:
            root = Path(temp)
            run_dir, trace, _ = self._make_trace(root)
            public_prompt_path = run_dir.parent / "external-worker/prompts/job-001.md"
            result, _, _ = self._audit_existing(root, trace,
                bootstrap_output=public_prompt_path.read_text(encoding="utf-8")
                    .replace("\n", "\r\n"))
        self.assertEqual(result["status"], "passed")
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-text-change-") as temp:
            root = Path(temp)
            run_dir, trace, _ = self._make_trace(root)
            public_prompt_path = run_dir.parent / "external-worker/prompts/job-001.md"
            changed_output = (public_prompt_path.read_text(encoding="utf-8")
                .replace("\n", "\r\n") + "changed")
            result, _, _ = self._audit_existing(root, trace,
                bootstrap_output=changed_output)
        self.assertEqual(result["status"], "failed")

    def _audit_existing(self, root: Path, trace: Path, *,
                        bootstrap_output: str | None = None) -> tuple[dict, Path, Path]:
        if bootstrap_output is not None:
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            output_event = next(event for event in events
                if event.get("type") == "response_item"
                and event.get("payload", {}).get("type") == "custom_tool_call_output")
            metadata = json.loads(output_event["payload"]["output"][0]["text"])
            metadata["output"] = bootstrap_output
            output_event["payload"]["output"][0]["text"] = json.dumps(metadata)
            trace_bytes = b"\n".join(json.dumps(event, ensure_ascii=False).encode("utf-8")
                for event in events) + b"\n"
            trace.write_bytes(trace_bytes)
            imported = root / "run/app-imports/job-001.json"
            evidence = json.loads(imported.read_text(encoding="utf-8"))
            evidence["traceSha256"] = hashlib.sha256(trace_bytes).hexdigest()
            imported.write_text(json.dumps(evidence), encoding="utf-8")
        return audit_trace(trace, run_dir=root / "run", job_id="job-001"), root / "run", (
            root / "external-worker/prompts/job-001.md")

    def test_private_file_read_and_wrong_path_transport_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-private-read-") as temp:
            root = Path(temp)
            private = root / "private-source.json"
            private.write_text("private", encoding="utf-8")
            # Even output matching the approved prompt cannot authorize another file path.
            result, _, _ = self._audit(root, bootstrap_path=private)
        self.assertEqual(result["status"], "failed")

        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-client-path-") as temp:
            changed_client = Path(temp) / "semarail-trace-v1" / "worker_client.py"
            result, _, _ = self._audit(Path(temp), trace_client_override=changed_client)
        self.assertEqual(result["status"], "failed")

    def test_altered_executor_output_duplicate_missing_output_and_nonzero_exit_fail(self) -> None:
        cases = (
            {"bootstrap_output": "supervisor retyped prompt"},
            {"duplicate_bootstrap": True},
            {"bootstrap_output_missing": True},
            {"bootstrap_exit_code": 1},
            {"bootstrap_session_id": 12345},
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix="btv-bootstrap-reject-") as temp:
                result, _, _ = self._audit(Path(temp), **case)
                self.assertEqual(result["status"], "failed")

    def test_bootstrap_source_file_is_not_mutated_by_audit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-bootstrap-readonly-") as temp:
            run_dir, trace, prompt_path = self._make_trace(Path(temp))
            before = prompt_path.read_bytes()
            result = audit_trace(trace, run_dir=run_dir, job_id="job-001")
            self.assertEqual(prompt_path.read_bytes(), before)
            self.assertEqual(result["status"], "passed")

    def test_legacy_schema2_review_uses_the_run_owned_prompt_template(self) -> None:
        current_template = (HERE / "worker-prompt.md").read_text(encoding="utf-8")
        historical_template = current_template.replace(
            "You are an independent participant in the synthetic HR business-trust-v1 benchmark.",
            "You are an archived schema-two participant in the synthetic HR business-trust-v1 benchmark.",
            1)
        self.assertNotEqual(historical_template, current_template)
        with tempfile.TemporaryDirectory(prefix="btv-schema2-run-template-") as temp:
            result, _run_dir, _prompt_path = self._audit(
                Path(temp), template_override=historical_template)
        self.assertEqual(result["status"], "passed")


class SmokeBootstrapAuditTests(unittest.TestCase):
    def _make_smoke_state(self, root: Path) -> tuple[dict, Path, Path, Path, Path, Path, int]:
        target = root / "smoke"
        target.mkdir()
        worker_root = root / "external-worker"
        worker_root.mkdir()
        worker_dir = worker_root / "job-001"
        worker_dir.mkdir()
        prompts = worker_root / "prompts"
        prompts.mkdir()
        python = Path(sys.executable).resolve()
        client = (HERE / "worker_client.py").resolve()
        port = 48786
        nonce = "b" * 32
        prompt = transport_smoke._prompt(python=python, client=client,
            worker_dir=worker_dir, port=port, nonce=nonce)
        prompt_bytes = prompt.encode("utf-8")
        prompt_path = prompts / "job-001.md"
        prompt_path.write_bytes(prompt_bytes)
        launcher_path = target / "one-shot-prompt.md"
        launcher_bytes = transport_smoke.public_prompt_launcher(
            prompt_path, worker_dir).encode("utf-8")
        launcher_path.write_bytes(launcher_bytes)
        prompt_hash = hashlib.sha256(prompt_bytes).hexdigest()
        state = {"schemaVersion": 2, "bootstrapMode": "public-file-one-read-v1",
            "workerDir": str(worker_dir), "pythonPath": str(python),
            "workerClientPath": str(client), "port": port, "nonce": nonce,
            "promptSha256": prompt_hash,
            "publicPromptPath": str(prompt_path),
            "publicPromptSha256": prompt_hash,
            "launcherSha256": hashlib.sha256(launcher_bytes).hexdigest()}
        return state, target, worker_dir, python, client, prompt_path, port

    def test_smoke_rebuilds_prompt_and_rejects_coordinated_prompt_or_launcher_tampering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-smoke-bootstrap-verify-") as temp:
            root = Path(temp)
            state, target, worker_dir, python, client, prompt_path, port = self._make_smoke_state(root)
            expected = transport_smoke._smoke_bootstrap_requirement(state, target,
                worker_dir=worker_dir, python=python, client=client, port=port)
            self.assertEqual(expected[0], prompt_path)
            self.assertEqual(expected[1], state["publicPromptSha256"])

            changed = prompt_path.read_bytes() + b"coordinated prompt and state tamper\n"
            prompt_path.write_bytes(changed)
            changed_hash = hashlib.sha256(changed).hexdigest()
            state["publicPromptSha256"] = changed_hash
            state["promptSha256"] = changed_hash
            with self.assertRaisesRegex(ValueError, "prepared rendering"):
                transport_smoke._smoke_bootstrap_requirement(state, target,
                    worker_dir=worker_dir, python=python, client=client, port=port)

        with tempfile.TemporaryDirectory(prefix="btv-smoke-bootstrap-extra-file-") as temp:
            root = Path(temp)
            state, target, worker_dir, python, client, prompt_path, port = self._make_smoke_state(root)
            (prompt_path.parent / "unexpected.md").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "directory contents differ"):
                transport_smoke._smoke_bootstrap_requirement(state, target,
                    worker_dir=worker_dir, python=python, client=client, port=port)

        with tempfile.TemporaryDirectory(prefix="btv-smoke-bootstrap-launcher-") as temp:
            root = Path(temp)
            state, target, worker_dir, python, client, _prompt_path, port = self._make_smoke_state(root)
            launcher_path = target / "one-shot-prompt.md"
            changed_launcher = launcher_path.read_bytes() + b"changed\n"
            launcher_path.write_bytes(changed_launcher)
            state["launcherSha256"] = hashlib.sha256(changed_launcher).hexdigest()
            with self.assertRaisesRegex(ValueError, "launcher content or hash"):
                transport_smoke._smoke_bootstrap_requirement(state, target,
                    worker_dir=worker_dir, python=python, client=client, port=port)

    def test_smoke_schema_versions_require_exact_integer_versions(self) -> None:
        self.assertEqual(transport_smoke._smoke_schema_version({"schemaVersion": 1}), 1)
        self.assertEqual(transport_smoke._smoke_schema_version({"schemaVersion": 2}), 2)
        self.assertEqual(transport_smoke._smoke_schema_version({"schemaVersion": 3}), 3)
        for version in (None, True, False, 1.0, 2.0, 4, "2"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "schema"):
                transport_smoke._smoke_schema_version({"schemaVersion": version})

    def test_smoke_reconstructs_schema3_stored_transport(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-smoke-stored-transport-") as temp:
            root = Path(temp)
            state, target, worker_dir, python, client, old_prompt_path, port = (
                SmokeBootstrapAuditTests()._make_smoke_state(root))
            state["smokeId"] = "smoke-reconstruct"
            config = transport_smoke.stored_transport_config(
                run_id=state["smokeId"], job_id=transport_smoke.SMOKE_JOB,
                nonce=state["nonce"], python=python, worker_client=client,
                worker_dir=worker_dir, broker_port=port)
            public_prompt_path = worker_dir.parent / "prompts" / "job-001.md"
            base_prompt = transport_smoke._prompt(python=python, client=client,
                worker_dir=worker_dir, port=port, nonce=state["nonce"])
            prompt = transport_smoke.stored_transport_prompt(base_prompt, config,
                expected_operations=("begin", "tool", "final"),
                adapt_instructions=False)
            prompt_bytes = prompt.encode("utf-8")
            public_prompt_path.write_bytes(prompt_bytes)
            launcher_path = target / "one-shot-prompt.md"
            launcher = public_prompt_stored_transport_message(
                public_prompt_path, worker_dir, config).encode("utf-8")
            launcher_path.write_bytes(launcher)
            state.update({"schemaVersion": 3,
                "bootstrapMode": APP_STORED_TRANSPORT_MODE,
                "publicPromptPath": str(public_prompt_path),
                "publicPromptSha256": hashlib.sha256(prompt_bytes).hexdigest(),
                "promptSha256": hashlib.sha256(prompt_bytes).hexdigest(),
                "launcherSha256": hashlib.sha256(launcher).hexdigest(),
                "storedTransportConfig": config})
            receipt = transport_smoke._smoke_bootstrap_requirement(state, target,
                worker_dir=worker_dir, python=python, client=client, port=port)
            self.assertEqual(receipt[0], public_prompt_path)
            self.assertEqual(receipt[3], config)
            state["storedTransportConfig"]["pythonArgument"] = "'wrong-python'"
            with self.assertRaisesRegex(ValueError, "configuration differs"):
                transport_smoke._smoke_bootstrap_requirement(state, target,
                    worker_dir=worker_dir, python=python, client=client, port=port)

    def test_smoke_prepare_emits_reconstructable_schema3_launcher(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-smoke-prepare-schema3-") as temp:
            root = Path(temp)
            smoke_root = root / "smokes"
            smoke_root.mkdir()
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            with patch.object(transport_smoke, "SMOKE_ROOT", smoke_root):
                prepared = transport_smoke.prepare("schema3-prepare-test", port,
                    bootstrap_stored_transport=True)
            target = smoke_root / "schema3-prepare-test"
            state = json.loads(Path(prepared["state"]).read_text(encoding="utf-8"))
            worker_dir = Path(state["workerDir"])
            self.addCleanup(shutil.rmtree, worker_dir.parent, ignore_errors=True)
            self.assertEqual(state["schemaVersion"], 3)
            self.assertEqual(state["bootstrapMode"], APP_STORED_TRANSPORT_MODE)
            receipt = transport_smoke._smoke_bootstrap_requirement(state, target,
                worker_dir=worker_dir, python=Path(state["pythonPath"]),
                client=Path(state["workerClientPath"]), port=port)
            self.assertEqual(receipt[3], state["storedTransportConfig"])
            self.assertIn("text(result);", Path(prepared["prompt"]).read_text(encoding="utf-8"))


class AppBootstrapExportIntegrationTests(unittest.TestCase):
    def test_real_export_setup_passes_review_requirement_for_selected_prompts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-export-review-integration-") as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            job_ids = [f"job-{index:03d}" for index in range(1, 181)]
            template_bytes = (HERE / "worker-prompt.md").read_bytes()
            template_hash = hashlib.sha256(template_bytes).hexdigest()
            (run_dir / "worker-prompt.md").write_bytes(template_bytes)
            (run_dir / "schedule.json").write_text(json.dumps([
                {"id": job_id} for job_id in job_ids]), encoding="utf-8")
            nonces = {job_id: hashlib.sha256(job_id.encode("ascii")).hexdigest()[:32]
                for job_id in job_ids}
            (run_dir / "dispatch-nonces.json").write_text(json.dumps(nonces), encoding="utf-8")
            candidate = experiment.REPO_ROOT.resolve()
            broker_port = 48787
            manifest = {
                "runId": "bootstrap-export-integration",
                "workerDispatchAllowed": True,
                "ports": {"broker": broker_port},
                "candidate": {"path": str(candidate)},
                "scheduleSha256": "schedule-hash",
                "dispatchNoncesSha256": "nonce-hash",
                "model": {"requested": "gpt-6-luna", "reasoningRequested": "max"},
                "maxConcurrent": 2,
                "deadlineSeconds": 300,
                "promptTemplateSha256": template_hash,
            }
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            selected = ["job-002", "job-007"]
            with patch.object(app_dispatch, "verify_run_freeze", return_value=manifest), \
                 patch.object(app_dispatch, "broker_ready", return_value=True):
                app_dispatch.export_app_jobs(run_dir, selected,
                    bootstrap_file_prompts=True)

            setup = experiment.read(run_dir / "app-dispatch-start.json")
            external_root = Path(setup["workerRoot"])
            self.addCleanup(shutil.rmtree, external_root, ignore_errors=True)
            self.assertEqual(setup["selectedJobIds"], selected)
            worker_directories = setup["workerDirectories"]
            self.assertEqual(set(worker_directories), set(job_ids))
            self.assertTrue(all(not any(Path(path).iterdir())
                for path in worker_directories.values()))
            self.assertEqual(set(setup["publicPromptFiles"]), set(selected))
            self.assertEqual(set(setup["launcherFiles"]), set(selected))
            self.assertEqual(setup["publicPromptDirectory"], str(external_root / "prompts"))
            self.assertTrue((external_root / "prompts/job-002.md").is_file())
            self.assertTrue((external_root / "prompts/job-007.md").is_file())
            self.assertFalse((external_root / "prompts/job-001.md").exists())

            worker_dir = Path(worker_directories["job-002"])
            prompt_path, _expected_hash = _app_bootstrap_requirement(setup,
                run_dir=run_dir, candidate=candidate, job_id="job-002",
                worker_dir=worker_dir)
            self.assertEqual(prompt_path, external_root / "prompts/job-002.md")

    def test_stored_transport_export_reconstructs_prompt_config_and_launcher(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btv-stored-export-integration-") as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            job_ids = [f"job-{index:03d}" for index in range(1, 181)]
            template_bytes = (HERE / "worker-prompt.md").read_bytes()
            template_hash = hashlib.sha256(template_bytes).hexdigest()
            (run_dir / "worker-prompt.md").write_bytes(template_bytes)
            (run_dir / "schedule.json").write_text(json.dumps([
                {"id": job_id} for job_id in job_ids]), encoding="utf-8")
            nonces = {job_id: hashlib.sha256(job_id.encode("ascii")).hexdigest()[:32]
                for job_id in job_ids}
            (run_dir / "dispatch-nonces.json").write_text(json.dumps(nonces), encoding="utf-8")
            candidate = experiment.REPO_ROOT.resolve()
            broker_port = 48788
            manifest = {
                "runId": "stored-export-integration",
                "workerDispatchAllowed": True,
                "ports": {"broker": broker_port},
                "candidate": {"path": str(candidate)},
                "scheduleSha256": "schedule-hash",
                "dispatchNoncesSha256": "nonce-hash",
                "model": {"requested": "gpt-6-luna", "reasoningRequested": "max"},
                "maxConcurrent": 2,
                "deadlineSeconds": 300,
                "promptTemplateSha256": template_hash,
            }
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            selected = ["job-002"]
            with patch.object(app_dispatch, "verify_run_freeze", return_value=manifest), \
                 patch.object(app_dispatch, "broker_ready", return_value=True):
                app_dispatch.export_app_jobs(run_dir, selected,
                    bootstrap_stored_transport=True)

            setup = experiment.read(run_dir / "app-dispatch-start.json")
            self.assertEqual(setup["schemaVersion"], 3)
            self.assertEqual(setup["bootstrapMode"], APP_STORED_TRANSPORT_MODE)
            self.assertEqual(set(setup["storedTransportConfigs"]), set(selected))
            worker_root = Path(setup["workerRoot"])
            self.addCleanup(shutil.rmtree, worker_root, ignore_errors=True)
            prompt_path, prompt_hash = _app_bootstrap_requirement(setup,
                run_dir=run_dir, candidate=candidate, job_id="job-002",
                worker_dir=Path(setup["workerDirectories"]["job-002"]))
            self.assertEqual(prompt_path, worker_root / "prompts/job-002.md")
            self.assertEqual(prompt_hash, setup["publicPromptFiles"]["job-002"]["sha256"])
            launcher = Path(setup["launcherFiles"]["job-002"]["path"]).read_text(
                encoding="utf-8")
            self.assertIn("store(\"", launcher)
            self.assertIn("text(result);", launcher)


if __name__ == "__main__":
    unittest.main()
