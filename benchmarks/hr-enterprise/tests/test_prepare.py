from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "prepare.py"
SPEC = importlib.util.spec_from_file_location("hr_benchmark_prepare", MODULE_PATH)
assert SPEC and SPEC.loader
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class PrepareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.csv"
        rows = [
            (1, "IT", "Female", 40, "Manager", "2020-01-02 00:00:00", 4, "Master", 5, "9000.0", 40, 8, 17, 5, 50, 8, 20, 1, "4.50", "False"),
            (2, "IT", "Male", 30, "Developer", "2021-02-03 00:00:00", 3, "Bachelor", 3, "6500.0", 42, 7, 11, 3, 25, 8, 10, 0, "3.25", "False"),
            (3, "Finance", "Other", 35, "Analyst", "2019-03-04 00:00:00", 5, "PhD", 4, "7000.0", 38, 9, 13, 4, 75, 5, 15, 1, "4.00", "True"),
            (4, "Finance", "Female", 45, "Manager", "2018-04-05 00:00:00", 6, "Master", 2, "8500.0", 45, 12, 19, 2, 0, 5, 25, 2, "2.75", "False"),
            (5, "Sales", "Male", 28, "Specialist", "2022-05-06 00:00:00", 2, "Bachelor", 1, "5000.0", 36, 5, 7, 1, 100, 6, 8, 0, "1.50", "False"),
            (6, "Sales", "Female", 50, "Manager", "2017-06-07 00:00:00", 7, "High School", 4, "8000.0", 41, 10, 23, 6, 50, 6, 30, 3, "3.75", "False"),
        ]
        with self.source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(prepare.SOURCE_HEADERS)
            writer.writerows(rows)
        self.spec = prepare._load_spec()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_transform_is_deterministic_and_preserves_latest_values(self) -> None:
        generated = prepare.transform(self.source, self.root / "first", self.spec, expected_rows=6)
        second = prepare.transform(self.source, self.root / "second", self.spec, expected_rows=6)
        first_manifest = json.loads((generated / "manifest.json").read_text(encoding="utf-8"))
        second_manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(first_manifest["outputs"], second_manifest["outputs"])
        self.assertEqual(6, first_manifest["outputs"]["employees"]["rows"])
        self.assertEqual(12, first_manifest["outputs"]["compensation_history"]["rows"])
        self.assertEqual(24, first_manifest["outputs"]["performance_reviews"]["rows"])
        self.assertEqual(72, first_manifest["outputs"]["attendance_monthly"]["rows"])

        with (generated / "compensation_history.csv").open(encoding="utf-8", newline="") as handle:
            salary_rows = list(csv.DictReader(handle))
        latest = {int(row["employee_id"]): row["monthly_salary"] for row in salary_rows if row["effective_date"] == "2024-09-01"}
        self.assertEqual("9000.00", latest[1])
        self.assertEqual("5000.00", latest[5])

        with (generated / "performance_reviews.csv").open(encoding="utf-8", newline="") as handle:
            review_rows = list(csv.DictReader(handle))
        employee_one = [row for row in review_rows if row["employee_id"] == "1" and row["review_date"] == "2024-09-30"][0]
        self.assertEqual("5.00", employee_one["performance_score"])
        self.assertEqual("4.50", employee_one["satisfaction_score"])

    def test_attendance_totals_and_management_boundaries(self) -> None:
        generated = prepare.transform(self.source, self.root / "output", self.spec, expected_rows=6)
        with (generated / "attendance_monthly.csv").open(encoding="utf-8", newline="") as handle:
            attendance = list(csv.DictReader(handle))
        employee_one = [row for row in attendance if row["employee_id"] == "1"]
        self.assertEqual(17, sum(int(row["overtime_hours"]) for row in employee_one))
        self.assertEqual(5, sum(int(row["sick_days"]) for row in employee_one))
        self.assertEqual("17", employee_one[-1]["overtime_hours_rolling_12m"])
        self.assertEqual("5", employee_one[-1]["sick_days_rolling_12m"])

        with (generated / "employees.csv").open(encoding="utf-8", newline="") as handle:
            employees = list(csv.DictReader(handle))
        by_id = {row["employee_id"]: row for row in employees}
        for employee in employees:
            manager_id = employee["manager_id"]
            if manager_id:
                manager = by_id[manager_id]
                self.assertEqual(employee["region_code"], manager["region_code"])
                self.assertEqual(employee["department_code"], manager["department_code"])

    def test_verify_detects_tampering(self) -> None:
        generated = prepare.transform(self.source, self.root / "output", self.spec, expected_rows=6)
        with (generated / "employees.csv").open("a", encoding="utf-8") as handle:
            handle.write("tampered\n")
        with self.assertRaisesRegex(prepare.PreparationError, "checksum mismatch"):
            prepare.verify(generated)

    def test_kaggle_download_authentication_is_required_and_not_returned_in_errors(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(prepare.PreparationError, "Kaggle credentials are required"):
                prepare._kaggle_headers()
        with patch.dict("os.environ", {"KAGGLE_API_TOKEN": "private-token"}, clear=True):
            headers = prepare._kaggle_headers()
        self.assertEqual(headers["Authorization"], "Bearer private-token")


if __name__ == "__main__":
    unittest.main()
