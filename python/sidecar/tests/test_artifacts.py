from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import tempfile
import threading
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sidecar.errors import CANCELLED, RESULT_TOO_LARGE
from sidecar.query import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_INLINE_BYTES,
    MAX_ARTIFACT_INLINE_ROWS,
    MAX_ARTIFACT_PREVIEW_ROWS,
    PostgresQueryExecutor,
    QueryLimits,
    artifact_request_from_mapping,
)


class CsvCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.description = [
            SimpleNamespace(name='a,"b"', type_code=25),
            SimpleNamespace(name="amount", type_code=1700),
            SimpleNamespace(name="day", type_code=1082),
        ]
        self.rows = rows
        self.executed: list[str] = []

    def execute(self, sql: str, _parameters: Any = None) -> None:
        self.executed.append(sql)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows.pop(0) if self.rows else None

    def close(self) -> None:
        return None


class CsvConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.cursors: list[CsvCursor] = []
        self.cancelled = threading.Event()
        self.closed = False

    def set_session(self, **_kwargs: Any) -> None:
        return None

    def cursor(self) -> CsvCursor:
        cursor = CsvCursor(self.rows)
        self.cursors.append(cursor)
        return cursor

    def cancel(self) -> None:
        self.cancelled.set()

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def artifact_request(directory: str, **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "id": "query-artifact-1",
        "directory": directory,
        "inlineMaxRows": MAX_ARTIFACT_INLINE_ROWS,
        "inlineMaxBytes": MAX_ARTIFACT_INLINE_BYTES,
        "previewRows": MAX_ARTIFACT_PREVIEW_ROWS,
        "maxBytes": MAX_ARTIFACT_BYTES,
        "expiresAt": "2099-01-01T00:00:00Z",
    }
    request.update(overrides)
    return request


class ArtifactTests(unittest.TestCase):
    def execute(
        self,
        rows: list[tuple[Any, ...]],
        directory: str,
        **request_overrides: Any,
    ) -> dict[str, Any]:
        request = artifact_request(directory, **request_overrides)
        parsed = artifact_request_from_mapping(request)
        return PostgresQueryExecutor(
            connection_factory=lambda _info: CsvConnection(rows),
        ).execute(
            query_id="q-artifact",
            semantic_sql="SELECT 1",
            native_sql="SELECT 1",
            project_dir=".",
            connection_info={},
            limits=QueryLimits(max_rows=500),
            artifact_request=parsed,
        )

    def test_rows_at_or_below_adaptive_boundary_stay_inline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.execute(
                [("small", Decimal("12.50"), date(2026, 1, 1)) for _ in range(50)],
                directory,
            )
            self.assertNotIn("artifact", result)
            self.assertEqual(len(result["previewRows"]), 50)
            self.assertFalse(result["stats"]["truncated"])
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

    def test_row_boundary_switches_to_streamed_artifact_and_limits_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.execute(
                [("small", Decimal("12.50"), date(2026, 1, 1)) for _ in range(51)],
                directory,
            )
            metadata = result["artifact"]
            self.assertEqual(set(metadata), {"id", "format", "fileName", "rowCount", "sizeBytes", "sha256", "expiresAt"})
            self.assertEqual(metadata["rowCount"], 51)
            self.assertEqual(metadata["format"], "csv")
            self.assertEqual(len(result["previewRows"]), 20)
            self.assertTrue(result["stats"]["truncated"])
            self.assertNotIn("path", metadata)
            self.assertNotIn("token", metadata)
            output = pathlib.Path(directory) / metadata["fileName"]
            self.assertTrue(output.is_file())
            raw = output.read_bytes()
            self.assertEqual(metadata["sizeBytes"], len(raw))
            self.assertEqual(metadata["sha256"], hashlib.sha256(raw).hexdigest())
            with output.open("r", encoding="utf-8", newline="") as stream:
                parsed = list(csv.reader(stream))
            self.assertEqual(parsed[0], ['a,"b"', "amount", "day"])
            self.assertEqual(parsed[1], ["small", "12.50", "2026-01-01"])
            self.assertEqual(len(parsed), 52)

    def test_core_filename_is_used_but_path_fragments_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.execute(
                [("small", Decimal("12.50"), date(2026, 1, 1)) for _ in range(51)],
                directory,
                filename="artifact-deadbeef.csv",
            )
            self.assertEqual(result["artifact"]["fileName"], "artifact-deadbeef.csv")
            self.assertTrue((pathlib.Path(directory) / "artifact-deadbeef.csv").is_file())
            for filename in ("../escape.csv", "nested/escape.csv", "artifact.csv.tmp", ""):
                with self.subTest(filename=filename):
                    with self.assertRaises(Exception):
                        artifact_request_from_mapping(artifact_request(directory, filename=filename))

    def test_versioned_core_attestation_fields_are_accepted_only_as_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = artifact_request(
                directory,
                schemaVersion=1,
                filename="artifact-deadbeef.csv",
                contentType="text/csv; charset=utf-8",
                format="csv",
                createdAt="2026-01-01T00:00:00Z",
            )
            parsed = artifact_request_from_mapping(request)
            self.assertEqual(parsed.file_name, "artifact-deadbeef.csv")
            for field, value in (
                ("schemaVersion", 2),
                ("format", "json"),
                ("contentType", "application/octet-stream"),
                ("createdAt", "not-a-time"),
            ):
                with self.subTest(field=field):
                    bad = dict(request)
                    bad[field] = value
                    with self.assertRaises(Exception):
                        artifact_request_from_mapping(bad)

    def test_json_boundary_switches_to_artifact_without_retaining_large_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.execute(
                [("x" * 140_000, Decimal("1.00"), date(2026, 1, 1))],
                directory,
            )
            self.assertIn("artifact", result)
            self.assertEqual(result["artifact"]["rowCount"], 1)
            self.assertEqual(len(result["previewRows"]), 1)
            self.assertNotIn("chart", result)

    def test_exact_json_byte_boundary_stays_inline_and_one_more_byte_uses_artifact(self) -> None:
        base = {'a,"b"': "", "amount": "1.00", "day": "2026-01-01"}
        base_bytes = len(
            json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        payload_size = MAX_ARTIFACT_INLINE_BYTES - 2 - base_bytes
        with tempfile.TemporaryDirectory() as directory:
            inline = self.execute(
                [("x" * payload_size, Decimal("1.00"), date(2026, 1, 1))],
                directory,
            )
            self.assertNotIn("artifact", inline)
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.execute(
                [("x" * (payload_size + 1), Decimal("1.00"), date(2026, 1, 1))],
                directory,
            )
            self.assertIn("artifact", artifact)

    def test_csv_round_trips_unicode_quotes_newlines_null_decimal_and_times(self) -> None:
        descriptions = [
            SimpleNamespace(name="text", type_code=25),
            SimpleNamespace(name="missing", type_code=25),
            SimpleNamespace(name="amount", type_code=1700),
            SimpleNamespace(name="day", type_code=1082),
            SimpleNamespace(name="moment", type_code=1184),
            SimpleNamespace(name="clock", type_code=1083),
        ]

        class RichCursor(CsvCursor):
            def __init__(self, rows: list[tuple[Any, ...]]) -> None:
                super().__init__(rows)
                self.description = descriptions

        class RichConnection(CsvConnection):
            def cursor(self) -> RichCursor:
                cursor = RichCursor(self.rows)
                self.cursors.append(cursor)
                return cursor

        value = (
            '中文, "quoted"\nnext line',
            None,
            Decimal("12.50"),
            date(2026, 9, 4),
            datetime(2026, 9, 4, 8, 30, tzinfo=timezone(timedelta(hours=8))),
            time(9, 45, 1),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = PostgresQueryExecutor(
                connection_factory=lambda _info: RichConnection([value for _ in range(51)]),
            ).execute(
                query_id="q-rich-csv",
                semantic_sql="SELECT 1",
                native_sql="SELECT 1",
                project_dir=".",
                connection_info={},
                limits=QueryLimits(max_rows=500),
                artifact_request=artifact_request_from_mapping(artifact_request(directory)),
            )
            output = pathlib.Path(directory) / result["artifact"]["fileName"]
            with output.open("r", encoding="utf-8", newline="") as stream:
                parsed = list(csv.reader(stream))
            self.assertEqual(
                parsed[1],
                [
                    '中文, "quoted"\nnext line',
                    "",
                    "12.50",
                    "2026-09-04",
                    "2026-09-04T08:30:00+08:00",
                    "09:45:01",
                ],
            )

    def test_csv_max_size_is_stable_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Exception) as caught:
                self.execute(
                    [("x" * 10_000, Decimal("1.00"), date(2026, 1, 1))],
                    directory,
                    inlineMaxBytes=1,
                    maxBytes=64,
                )
            self.assertEqual(caught.exception.error.code, RESULT_TOO_LARGE)
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

    def test_artifact_directory_and_id_are_strictly_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for bad in (
                {"id": "../escape", "directory": directory, "expiresAt": "2099-01-01T00:00:00Z"},
                {"id": "escape", "directory": ".", "expiresAt": "2099-01-01T00:00:00Z"},
                {"id": "escape", "directory": directory, "expiresAt": "not-a-time"},
            ):
                with self.subTest(bad=bad):
                    with self.assertRaises(Exception):
                        artifact_request_from_mapping(bad)

    def test_cancel_after_csv_started_removes_unpublished_file(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingCursor(CsvCursor):
            def fetchone(self) -> tuple[Any, ...] | None:
                if len(self.rows) == 1:
                    started.set()
                    release.wait(2)
                    raise RuntimeError("cancelled")
                return super().fetchone()

        class BlockingConnection(CsvConnection):
            def cursor(self) -> BlockingCursor:
                cursor = BlockingCursor(self.rows)
                self.cursors.append(cursor)
                return cursor

        with tempfile.TemporaryDirectory() as directory:
            rows = [("small", Decimal("12.50"), date(2026, 1, 1)) for _ in range(51)] + [
                ("late", Decimal("13.50"), date(2026, 1, 2))
            ]
            connection = BlockingConnection(rows)
            executor = PostgresQueryExecutor(connection_factory=lambda _info: connection)
            outcome: list[BaseException] = []

            def run() -> None:
                try:
                    executor.execute(
                        query_id="q-cancel-artifact",
                        semantic_sql="SELECT 1",
                        native_sql="SELECT 1",
                        project_dir=".",
                        connection_info={},
                        limits=QueryLimits(),
                        artifact_request=artifact_request_from_mapping(artifact_request(directory)),
                    )
                except BaseException as exc:
                    outcome.append(exc)

            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertTrue(executor.cancel("q-cancel-artifact"))
            release.set()
            worker.join(2)
            self.assertEqual(len(outcome), 1)
            self.assertEqual(outcome[0].error.code, CANCELLED)
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
