from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sidecar.dispatch import SidecarDependencies
from sidecar.main import run


class MainTests(unittest.TestCase):
    def test_run_prepares_query_service_before_starting_protocol_workers(self) -> None:
        events: list[str] = []

        class QueryService:
            def prepare_for_worker_threads(self) -> None:
                events.append("prepared")

        dependencies = SidecarDependencies(query_service=QueryService())
        with patch("sidecar.main.serve", side_effect=lambda *_args, **_kwargs: events.append("served")):
            run(io.BytesIO(), io.BytesIO(), dependencies=dependencies)

        self.assertEqual(events, ["prepared", "served"])

    def test_run_prepares_legacy_query_runner_once_when_it_is_the_same_service(self) -> None:
        service = SimpleNamespace(prepare_for_worker_threads=lambda: None)
        calls: list[object] = []
        service.prepare_for_worker_threads = lambda: calls.append(service)
        dependencies = SidecarDependencies(query_service=service, query_runner=service)

        with patch("sidecar.main.serve"):
            run(io.BytesIO(), io.BytesIO(), dependencies=dependencies)

        self.assertEqual(calls, [service])


if __name__ == "__main__":
    unittest.main()
