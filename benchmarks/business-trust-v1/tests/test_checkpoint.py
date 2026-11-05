from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import checkpoint
import experiment


class RecoveryCheckpointTests(unittest.TestCase):
    def test_protocol_is_an_exact_opt_in(self):
        protocol = dict(checkpoint.RECOVERY_PROTOCOL)
        self.assertTrue(checkpoint.protocol_supported({"recoveryProtocol": protocol}))
        checkpoint.require_protocol({"recoveryProtocol": protocol})
        for manifest in (
            {},
            {"recoveryProtocol": {**protocol, "futureField": True}},
            {"recoveryProtocol": {**protocol, "schemaVersion": True}},
            {"recoveryProtocol": {**protocol, "mode": "unknown"}},
        ):
            with self.subTest(manifest=manifest), self.assertRaises(checkpoint.CheckpointError):
                checkpoint.require_protocol(manifest)

    def test_private_record_reader_allows_sql_but_public_reader_rejects_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "record.json"
            path.write_text(json.dumps({"semanticSql": "SELECT 1", "status": "running"}),
                            encoding="utf-8")
            self.assertEqual(checkpoint.read_private_json(path)["semanticSql"], "SELECT 1")
            path.write_text('{"apiKey":"redacted"}', encoding="utf-8")
            with self.assertRaises(checkpoint.CheckpointError):
                checkpoint.read_json(path)

    def test_json_readers_reject_duplicate_keys_and_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.json"
            for source in ('{"revision":1,"revision":2}', '{"clock":NaN}'):
                path.write_text(source, encoding="utf-8")
                with self.subTest(source=source), self.assertRaises(checkpoint.CheckpointError):
                    checkpoint.read_json(path)
                with self.subTest(private_source=source), self.assertRaises(checkpoint.CheckpointError):
                    checkpoint.read_private_json(path)

    def test_create_only_evidence_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "attempt.json"
            checkpoint.write_public_json_new(path, {"passed": True})
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                checkpoint.write_public_json_new(path, {"passed": False})
            self.assertEqual(path.read_bytes(), original)

    def test_dpapi_store_round_trips_without_plaintext_ciphertext(self):
        if os.name != "nt":
            self.skipTest("Windows current-user DPAPI is required")
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            secret_value = secrets.token_hex(32)
            run_id = "recovery-test"
            manifest_sha = hashlib.sha256(b"manifest").hexdigest()
            payload = {"opaqueValue": secret_value}
            ciphertext_sha = checkpoint.write_secret_store(
                run_dir, run_id=run_id, manifest_sha256=manifest_sha,
                secrets_payload=payload)
            ciphertext = checkpoint.secret_path(run_dir).read_bytes()
            self.assertNotIn(secret_value.encode("ascii"), ciphertext)
            restored = checkpoint.read_secret_store(
                run_dir, run_id=run_id, manifest_sha256=manifest_sha,
                expected_ciphertext_sha256=ciphertext_sha)
            self.assertEqual(restored, payload)

    def test_ledger_schema_and_hmac_reject_mutation(self):
        ledger = self._ledger()
        key = b"k" * 32
        signed = checkpoint.seal_ledger(ledger, key)
        checkpoint.validate_worker_ready_ledger(
            signed, run_id="run-1", manifest_sha256="a" * 64)
        checkpoint.verify_ledger_mac(signed, key)

        unknown = copy.deepcopy(signed)
        unknown["futureField"] = True
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.validate_worker_ready_ledger(
                unknown, run_id="run-1", manifest_sha256="a" * 64)

        bool_version = copy.deepcopy(signed)
        bool_version["schemaVersion"] = True
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.validate_worker_ready_ledger(
                bool_version, run_id="run-1", manifest_sha256="a" * 64)

        changed = copy.deepcopy(signed)
        changed["lastWallEpoch"] += 1
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.verify_ledger_mac(changed, key)

    def test_wall_clock_rejects_any_backwards_step_including_half_second(self):
        ledger = {"lastWallEpoch": 100.0}
        self.assertEqual(checkpoint.check_wall_clock(ledger, 100.0), 100.0)
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.check_wall_clock(ledger, 99.5)
        for bad in (float("nan"), float("inf"), True):
            with self.subTest(value=bad), self.assertRaises(checkpoint.CheckpointError):
                checkpoint.check_wall_clock(ledger, bad)

    def test_one_ahead_capture_is_reconciled_without_resetting_original_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            host, ledger = self._record_host(Path(temp), records_ahead=1)
            host.recovery_ledger = ledger
            host._recovery_mac_key = b"m" * 32
            checkpoint.write_public_json_new(checkpoint.ledger_path(host.directory), ledger)
            with patch("experiment.time.time", return_value=1200.0), \
                 patch("experiment.time.monotonic", return_value=500.0):
                host._load_recovery_records()
            self.assertEqual(host.records["job-001"]["startedAtEpoch"], 1000.0)
            self.assertEqual(host.records["job-001"]["deadlineAtEpoch"], 1300.0)
            self.assertEqual(host.deadline_monotonic["job-001"], 600.0)
            self.assertEqual(host.recovery_ledger["consumedJobs"]["job-001"], {
                "schedulePosition": 0, "startedAtEpoch": 1000.0,
                "deadlineAtEpoch": 1300.0, "status": "running",
            })

    def test_two_ahead_records_fail_closed_instead_of_replaying_or_skipping(self):
        with tempfile.TemporaryDirectory() as temp:
            host, ledger = self._record_host(Path(temp), records_ahead=2)
            host.recovery_ledger = ledger
            host._recovery_mac_key = b"m" * 32
            before = checkpoint.ledger_path(host.directory)
            checkpoint.write_public_json_new(before, ledger)
            original = before.read_bytes()
            with patch("experiment.time.time", return_value=1200.0), \
                 patch("experiment.time.monotonic", return_value=500.0), \
                 self.assertRaises(ValueError):
                host._load_recovery_records()
            self.assertEqual(before.read_bytes(), original)

    def test_tampered_committed_head_cannot_be_rechained_as_one_ahead_append(self):
        with tempfile.TemporaryDirectory() as temp:
            host, ledger = self._record_host(Path(temp), records_ahead=1)
            host.recovery_ledger = ledger
            host._recovery_mac_key = b"m" * 32
            ledger_path = checkpoint.ledger_path(host.directory)
            checkpoint.write_public_json_new(ledger_path, ledger)
            with patch("experiment.time.time", return_value=1200.0), \
                 patch("experiment.time.monotonic", return_value=500.0):
                host._load_recovery_records()

            record_dir = host.directory / "records" / "job-001"
            committed_head_path = record_dir / "0001.json"
            rewritten_head = checkpoint.read_private_json(committed_head_path)
            rewritten_head["events"] = [{"kind": "tampered-after-ledger-commit"}]
            committed_head_path.write_text(json.dumps(rewritten_head), encoding="utf-8")
            rewritten_head_sha = checkpoint.sha256_file(committed_head_path)
            appended = {
                "id": "job-001", "questionId": "BTV-01", "group": "baseline",
                "round": 1, "schemaVersion": 1, "status": "complete",
                "startedAtEpoch": 1000.0, "deadlineAtEpoch": 1300.0,
                "events": [],
                "_checkpoint": {"schemaVersion": 1, "revision": 2,
                                 "previousSha256": rewritten_head_sha},
            }
            checkpoint.write_json_new(record_dir / "0002.json", appended)

            before = ledger_path.read_bytes()
            recovered = object.__new__(experiment.Host)
            recovered.directory = host.directory
            recovered.schedule = host.schedule
            recovered.recovery_ledger = host.recovery_ledger
            recovered._recovery_mac_key = host._recovery_mac_key
            with self.assertRaisesRegex(ValueError, "record history changed before"):
                recovered._load_recovery_records()
            self.assertEqual(ledger_path.read_bytes(), before)

    def test_preaccept_manifest_cannot_begin_a_worker_job(self):
        host = object.__new__(experiment.Host)
        host.guard = threading.RLock()
        host.manifest = {"workerDispatchAllowed": False}
        host.schedule = {"job-001": {}}
        host.records = {}
        with self.assertRaisesRegex(ValueError, "does not allow worker dispatch"):
            host.begin("job-001", "unused")

    def test_resume_requires_new_explicit_protocol(self):
        host = object.__new__(experiment.Host)
        host.manifest = {"runId": "legacy-run"}
        with self.assertRaises(checkpoint.CheckpointError):
            host.resume()
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.require_protocol({"runId": "legacy-run"})

    def test_changed_or_missing_semantic_index_files_fail_resume_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            host = object.__new__(experiment.Host)
            host.directory = Path(temp).resolve()
            index_dir = host.directory / "state" / "business" / "candidate" / "semantic-index"
            index_dir.mkdir(parents=True)
            active = index_dir / "active.json"
            partition = index_dir / "partition.json"
            active.write_text('{"revision":"one"}', encoding="utf-8")
            partition.write_text('{"documents":1}', encoding="utf-8")
            expected = host._semantic_index_file_hashes("candidate")
            host._assert_semantic_index_state("candidate", expected)

            active.write_text('{"revision":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "semantic index state changed after worker-ready checkpoint"
            ):
                host._assert_semantic_index_state("candidate", expected)

            active.unlink()
            partition.unlink()
            with self.assertRaisesRegex(
                ValueError, "semantic index state changed after worker-ready checkpoint"
            ):
                host._assert_semantic_index_state("candidate", expected)

    def test_cli_dispatch_marker_binds_frozen_client_and_transport_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            candidate = root / "candidate"
            candidate.mkdir()
            worker_root = root / "workers"
            worker_root.mkdir()
            worker_dir = worker_root / "job-001"
            worker_dir.mkdir()
            client = candidate / "benchmarks" / "business-trust-v1" / "worker_client.py"
            host = object.__new__(experiment.Host)
            host.directory = run_dir
            host.manifest = {
                "runId": "run-1", "scheduleSha256": "a" * 64,
                "promptTemplateSha256": "b" * 64,
                "ports": {"broker": 48785},
                "candidate": {"path": str(candidate)},
            }
            host.schedule_list = [{"id": "job-001"}]
            host.model = "gpt-6-luna"
            host.reasoning = "max"
            marker = {
                "schemaVersion": 1, "runId": "run-1",
                "scheduleSha256": "a" * 64,
                "promptTemplateSha256": "b" * 64,
                "brokerPort": 48785, "maxConcurrent": experiment.MAX_CONCURRENT,
                "deadlineSeconds": experiment.DEADLINE_SECONDS,
                "model": "gpt-6-luna", "reasoning": "max",
                "workerClientPath": str(client.resolve()),
                "transportPath": client.resolve().as_posix(),
                "pythonPath": str(Path(sys.executable).resolve()),
                "workerRoot": str(worker_root.resolve()),
                "workerDirectories": {"job-001": str(worker_dir.resolve())},
            }
            marker_path = run_dir / "runner-start.json"
            checkpoint.write_json_new(marker_path, marker)
            observed = host._validated_dispatch_marker()
            self.assertEqual(observed["file"], "runner-start.json")
            self.assertEqual(observed["selectedJobIds"], ["job-001"])

            marker["transportPath"] = "unbound-client-path"
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "CLI dispatch client binding"):
                host._validated_dispatch_marker()

    def test_recovery_mcp_evidence_is_hash_only_and_role_complete(self):
        host = object.__new__(experiment.Host)
        actors = host._expected_actor_attributes()
        names = ["semarail_list_models", "semarail_get_context"]
        host.expected_tool_capabilities = {
            group: {actor: list(names) for actor in actors}
            for group in experiment.GROUPS
        }
        host.group_tools = {
            group: {actor: [{"name": name} for name in names] for actor in actors}
            for group in experiment.GROUPS
        }
        evidence = host._recovery_tool_capability_evidence()
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["observedRoleCount"], 10)
        self.assertEqual(evidence["requiredRoleCount"], 10)
        self.assertEqual(evidence["groups"]["baseline"]["employee"]["toolCount"], 2)
        self.assertNotIn("semarail_list_models", json.dumps(evidence))
        host.group_tools["candidate"]["employee"] = [{"name": "unexpected"}]
        self.assertFalse(host._recovery_tool_capability_evidence()["passed"])

    @staticmethod
    def _ledger() -> dict:
        sha = "b" * 64
        return checkpoint.make_worker_ready_ledger(
            run_id="run-1", manifest_sha256="a" * 64,
            secret_sha256="c" * 64, schedule_sha256="d" * 64,
            dispatch_nonces_sha256="e" * 64, dispatch_file="", dispatch_sha256="",
            gate_hashes={"gate.json": sha}, binding_hashes={"baseline": sha},
            record_hashes={"job-001": sha}, created_at_epoch=100.0)

    @staticmethod
    def _record_host(directory: Path, *, records_ahead: int):
        entry = {"id": "job-001", "questionId": "BTV-01",
                 "group": "baseline", "round": 1}
        folder = directory / "records" / "job-001"
        folder.mkdir(parents=True)
        baseline = {**entry, "schemaVersion": 1, "status": "missing",
                    "reason": "not_started", "events": [],
                    "audit": {"status": "unverified"}}
        checkpoint.write_json_new(folder / "0000.json", baseline)
        previous = checkpoint.sha256_file(folder / "0000.json")
        latest = baseline
        for revision in range(1, records_ahead + 1):
            latest = {**entry, "schemaVersion": 1,
                      "status": "running" if revision == 1 else "complete",
                      "startedAtEpoch": 1000.0, "deadlineAtEpoch": 1300.0,
                      "events": [],
                      "_checkpoint": {"schemaVersion": 1, "revision": revision,
                                      "previousSha256": previous}}
            target = folder / f"{revision:04d}.json"
            checkpoint.write_json_new(target, latest)
            previous = checkpoint.sha256_file(target)
        ledger = RecoveryCheckpointTests._ledger()
        ledger["records"] = {"job-001": {"revision": 0,
                                         "sha256": checkpoint.sha256_file(folder / "0000.json")}}
        if records_ahead:
            ledger["dispatch"] = {
                "file": "runner-start.json", "sha256": "f" * 64,
                "mode": "codex-cli-runner-v1", "selectedJobIds": ["job-001"],
            }
        key = b"m" * 32
        ledger = checkpoint.seal_ledger(ledger, key)
        host = object.__new__(experiment.Host)
        host.directory = directory
        host.schedule = {"job-001": entry}
        host.recovery_ledger = ledger
        host._recovery_mac_key = key
        host.records = {}
        return host, ledger


if __name__ == "__main__":
    unittest.main()
