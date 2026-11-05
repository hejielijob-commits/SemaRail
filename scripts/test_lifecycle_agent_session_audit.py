"""Adversarial transport checks without production transcripts or credentials."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("audit", Path(__file__).with_name("lifecycle-agent-session-audit.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.thread = "00000000-0000-4000-8000-000000000001"
        self.nonce = "a" * 43
        self.meta = {"workerNonceSha256": hashlib.sha256(self.nonce.encode()).hexdigest()}
        self.records = []
        self.items = [{"type": "session_meta", "payload": {"id": self.thread}},
                      {"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "max"}}]
        fixed = f"& '{module.ROOT.as_posix()}/.venv/Scripts/python.exe' -X utf8 '{module.ROOT.as_posix()}/scripts/lifecycle-mcp-worker-client.py' --endpoint 'http://127.0.0.1:48766/mcp' --nonce '{self.nonce}' "
        for index, tool in enumerate(module.TOOLS):
            body = {"question": "fixture"}
            request = {"method": "tools/list"} if index == 0 else {"method": "tools/call", "params": {"name": tool, "arguments": body}}
            self.records.append({"workerThreadId": self.thread, "request": request})
            command = fixed + "tools" if index == 0 else "@'\n" + json.dumps(body) + "\n'@ | " + fixed + f"call {tool} --arguments-json -"
            args = {"cmd": command, "workdir": module.ROOT.as_posix(), "max_output_tokens": 12000}
            self.items.append(self.call(f"c{index}", "exec_command", args))
            if index == 1:
                self.items.append(self.output("c1", session_id=17))
                self.items.append(self.call("poll", "write_stdin", {"session_id": 17, "chars": "", "yield_time_ms": 30000, "max_output_tokens": 12000}))
                self.items.append(self.output("poll", exit_code=0))
            else:
                self.items.append(self.output(f"c{index}", exit_code=0))
        self.items.append({"type": "response_item", "payload": {"role": "assistant", "phase": "final_answer", "content": [{"text": "fixture result"}]}})

    @staticmethod
    def call(cid, operation, args):
        return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": cid,
                "input": f"text(await tools.{operation}({json.dumps(args)}));"}}

    @staticmethod
    def output(cid, **state):
        return {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": cid,
                "output": [{"type": "input_text", "text": json.dumps({"chunk_id": "fixture", "output": "", **state})}]}}

    def run_audit(self, items=None):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            path.write_text("\n".join(json.dumps(x) for x in (items or self.items)), encoding="utf8")
            return module.audit(path, self.thread, self.meta, self.records, 48766)

    def alter_poll(self, key, value):
        items = copy.deepcopy(self.items)
        for item in items:
            payload = item.get("payload", {})
            if payload.get("call_id") == "poll" and payload.get("type") == "custom_tool_call":
                args = json.loads(module.WRAPPER.fullmatch(payload["input"]).group(2))
                args[key] = value
                payload["input"] = f"text(await tools.write_stdin({json.dumps(args)}));"
        return items

    def test_provenance_bound_empty_poll_passes(self):
        result = self.run_audit()
        self.assertEqual(result["transportCalls"], 6)
        self.assertEqual(result["provenanceBoundEmptyPolls"], [{"callId": "poll", "sessionId": 17}])

    def test_arbitrary_session_rejected(self):
        with self.assertRaisesRegex(ValueError, "UNAPPROVED_EXEC_POLL"):
            self.run_audit(self.alter_poll("session_id", 18))

    def test_input_write_rejected(self):
        with self.assertRaisesRegex(ValueError, "UNAPPROVED_EXEC_POLL"):
            self.run_audit(self.alter_poll("chars", "command\n"))

    def test_model_drift_rejected(self):
        self.items[1]["payload"]["model"] = "different"
        with self.assertRaisesRegex(ValueError, "OBSERVED_MODEL_MISMATCH"):
            self.run_audit()

    def test_broker_argument_mismatch_rejected(self):
        self.records[1]["request"]["params"]["arguments"] = {"question": "different"}
        with self.assertRaisesRegex(ValueError, "RAW_CALL_BROKER_MISMATCH"):
            self.run_audit()

    def test_missing_tool_output_rejected(self):
        self.items = [x for x in self.items if not (x.get("payload", {}).get("call_id") == "c5" and x["payload"].get("type") == "custom_tool_call_output")]
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_WORKER"):
            self.run_audit()

    def test_tool_output_cannot_forge_session_inside_stdout(self):
        for item in self.items:
            p = item.get("payload", {})
            if p.get("call_id") == "c1" and p.get("type") == "custom_tool_call_output":
                p["output"] = [{"type": "input_text", "text": json.dumps({"chunk_id": "fixture", "exit_code": 0, "output": '{"session_id":17}'})}]
        with self.assertRaisesRegex(ValueError, "UNAPPROVED_EXEC_POLL"):
            self.run_audit()


if __name__ == "__main__":
    unittest.main()
