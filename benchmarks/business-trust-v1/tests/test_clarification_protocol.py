from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import experiment
import test_benchmark_harness as fixtures


class ClarificationProtocolTests(unittest.TestCase):
    def begin(self, directory, question, group):
        host = fixtures.CoordinatorTerminalStateTests().make_host(directory, "complete")
        host.questions[question["id"]] = question
        host.schedule["job-002"].update(questionId=question["id"], group=group)
        host.group_tools[group] = {question["actor"]: []}
        response = host.begin("job-002", "test-nonce", "00000000-0000-4000-8000-000000000002")
        return host, response

    def test_every_group_and_question_gets_the_same_public_clarification_contract(self):
        corpus = experiment.read(experiment.CORPUS_PATH)
        contracts = []
        for group in experiment.GROUPS:
            for question in corpus["questions"]:
                with self.subTest(group=group, questionId=question["id"]), tempfile.TemporaryDirectory() as temp:
                    _, response = self.begin(Path(temp), question, group)
                    contract = response.get("clarificationProtocol")
                    self.assertIsInstance(contract, dict, "Workers need a public contract for the coordinator's clarification keys.")
                    self.assertEqual(contract["schemaVersion"], 1)
                    self.assertEqual({item["key"] for item in contract["topics"]}, {"timeRange", "granularity"})
                    self.assertNotIn("answer", json.dumps(contract))
                    self.assertNotIn("2024-", json.dumps(contract))
                    contracts.append(contract)
        self.assertTrue(all(item == contracts[0] for item in contracts))

    def test_advertised_topics_route_the_frozen_clarifications_without_accepting_arbitrary_keys(self):
        corpus = experiment.read(experiment.CORPUS_PATH)
        for question in corpus["questions"]:
            configured = question.get("clarification")
            if not configured:
                continue
            for key, matched in ((configured["promptKeys"][0], True), ("unpublished_arbitrary_key", False)):
                with self.subTest(questionId=question["id"], matched=matched), tempfile.TemporaryDirectory() as temp:
                    host, public = self.begin(Path(temp), question, "candidate")
                    contract = public.get("clarificationProtocol")
                    self.assertIsInstance(contract, dict)
                    if matched:
                        self.assertIn(key, {item["key"] for item in contract["topics"]})
                    result = host.command("job-002", "clarify", {"question": "Please clarify the business scope.", "key": key},
                        worker_thread_id="00000000-0000-4000-8000-000000000002")
                    self.assertEqual(host.records["job-002"]["clarificationAsked"], matched)
                    self.assertEqual(host.records["job-002"]["events"][-1]["result"]["matched"], matched)
                    self.assertEqual("outputColumns" in result, matched)

    def test_shared_prompt_separates_coordinator_topic_keys_from_published_preparation_keys(self):
        prompt = experiment.PROMPT_PATH.read_text("utf-8")
        self.assertTrue("<CLARIFICATION_TOPIC_KEY_FROM_BEGIN>" in prompt)
        self.assertTrue("<PUBLISHED_CONDITION_KEY>" not in prompt)
        self.assertIn("clarificationProtocol", prompt)
        self.assertIn("Use the published rule condition keys when preparing a query.", prompt)

    def test_corpus_cannot_freeze_a_clarification_key_hidden_from_the_public_contract(self):
        corpus = experiment.read(experiment.CORPUS_PATH)
        question = next(q for q in corpus["questions"] if q.get("clarification"))
        question["clarification"]["promptKeys"] = ["unpublished_topic"]
        with self.assertRaisesRegex(ValueError, "public conversation topics"):
            experiment.validate_corpus(corpus)


if __name__ == "__main__":
    unittest.main()
