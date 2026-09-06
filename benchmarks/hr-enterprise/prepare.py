#!/usr/bin/env python3
"""Download, normalize, and verify the reproducible HR benchmark dataset."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from urllib.parse import urlsplit
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_DATA_ROOT = REPO_ROOT / ".benchmark-data" / "hr-enterprise"
SOURCE_HEADERS = (
    "Employee_ID", "Department", "Gender", "Age", "Job_Title", "Hire_Date",
    "Years_At_Company", "Education_Level", "Performance_Score", "Monthly_Salary",
    "Work_Hours_Per_Week", "Projects_Handled", "Overtime_Hours", "Sick_Days",
    "Remote_Work_Frequency", "Team_Size", "Training_Hours", "Promotions",
    "Employee_Satisfaction_Score", "Resigned",
)
REGIONS = (
    ("NAM", "North America", "北美"),
    ("LATAM", "Latin America", "拉丁美洲"),
    ("EMEA", "Europe", "欧洲"),
    ("APAC", "Asia Pacific", "亚太"),
    ("ANZ", "Australia and New Zealand", "澳大利亚及新西兰"),
    ("MENA", "Middle East and North Africa", "中东及北非"),
)
OUTPUT_HEADERS: dict[str, tuple[str, ...]] = {
    "regions": ("region_code", "region_name_en", "region_name_zh"),
    "departments": ("department_code", "department_name", "region_code", "cost_center"),
    "employees": (
        "employee_id", "department_code", "region_code", "manager_id", "gender", "age",
        "job_title", "hire_date", "years_at_company", "education_level",
        "work_hours_per_week", "projects_handled", "remote_work_frequency", "team_size",
        "training_hours", "promotions", "resigned",
    ),
    "compensation_history": (
        "employee_id", "effective_date", "manager_id", "department_code", "region_code",
        "monthly_salary",
    ),
    "performance_reviews": (
        "employee_id", "review_date", "manager_id", "department_code", "region_code",
        "performance_score", "satisfaction_score",
    ),
    "attendance_monthly": (
        "employee_id", "attendance_month", "manager_id", "department_code", "region_code",
        "overtime_hours", "sick_days", "overtime_hours_rolling_12m", "sick_days_rolling_12m",
    ),
}
SALARY_DATES = ("2024-03-01", "2024-09-01")
REVIEW_DATES = ("2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30")
ATTENDANCE_MONTHS = (
    "2023-10-01", "2023-11-01", "2023-12-01", "2024-01-01", "2024-02-01", "2024-03-01",
    "2024-04-01", "2024-05-01", "2024-06-01", "2024-07-01", "2024-08-01", "2024-09-01",
)


class PreparationError(RuntimeError):
    """A stable, credential-free benchmark preparation error."""


def _load_spec(path: Path = HERE / "dataset.json") -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_int(seed: int, *parts: object) -> int:
    value = ":".join((str(seed), *(str(part) for part in parts))).encode()
    return int.from_bytes(hashlib.sha256(value).digest()[:8], "big")


def _decimal(value: str, places: str = "0.01") -> Decimal:
    return Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise PreparationError("department name cannot be normalized")
    return slug.upper()


def _region_for(seed: int, employee_id: int) -> str:
    return REGIONS[_stable_int(seed, "region", employee_id) % len(REGIONS)][0]


def _department_code(region_code: str, department: str) -> str:
    return f"{region_code}-{_slug(department)}"


def _kaggle_headers() -> dict[str, str]:
    """Build authentication headers without returning or persisting credentials elsewhere."""

    headers = {"User-Agent": "semarail-benchmark/1"}
    api_token = os.environ.get("KAGGLE_API_TOKEN")
    username = os.environ.get("KAGGLE_USERNAME")
    api_key = os.environ.get("KAGGLE_KEY")
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
        return headers
    if username and api_key:
        encoded = base64.b64encode(f"{username}:{api_key}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
        return headers
    raise PreparationError(
        "Kaggle credentials are required for the first download; configure KAGGLE_API_TOKEN "
        "or both KAGGLE_USERNAME and KAGGLE_KEY"
    )


def download(data_root: Path, spec: Mapping[str, object]) -> Path:
    dataset = spec["dataset"]
    assert isinstance(dataset, Mapping)
    raw_dir = data_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    archive = raw_dir / "dataset-v1.zip"
    member = str(dataset["member"])
    source = raw_dir / member

    if not archive.exists() or _sha256(archive) != dataset["archiveSha256"]:
        download_url = str(dataset["downloadUrl"])
        parsed_url = urlsplit(download_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "www.kaggle.com" or not parsed_url.path.startswith("/api/"):
            raise PreparationError("Kaggle download URL is not an approved HTTPS API endpoint")
        request = urllib.request.Request(download_url, headers=_kaggle_headers())
        with tempfile.NamedTemporaryFile(dir=raw_dir, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    shutil.copyfileobj(response, temporary)
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
        if _sha256(temporary_path) != dataset["archiveSha256"]:
            temporary_path.unlink(missing_ok=True)
            raise PreparationError("downloaded Kaggle archive checksum mismatch")
        os.replace(temporary_path, archive)
    if archive.stat().st_size != dataset["archiveBytes"]:
        raise PreparationError("Kaggle archive size differs from the pinned manifest")

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if names != [member]:
            raise PreparationError("Kaggle archive member list differs from the pinned manifest")
        with bundle.open(member) as source_handle, tempfile.NamedTemporaryFile(dir=raw_dir, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(source_handle, temporary)
        if _sha256(temporary_path) != dataset["memberSha256"]:
            temporary_path.unlink(missing_ok=True)
            raise PreparationError("extracted Kaggle CSV checksum mismatch")
        if temporary_path.stat().st_size != dataset["memberBytes"]:
            temporary_path.unlink(missing_ok=True)
            raise PreparationError("extracted Kaggle CSV size differs from the pinned manifest")
        os.replace(temporary_path, source)
    return source


def _read_source(source: Path, expected_rows: int | None) -> tuple[list[dict[str, str]], list[str]]:
    records: list[dict[str, str]] = []
    departments: set[str] = set()
    employee_ids: set[int] = set()
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != SOURCE_HEADERS:
            raise PreparationError("source CSV columns differ from the pinned schema")
        for line_number, row in enumerate(reader, start=2):
            try:
                employee_id = int(row["Employee_ID"])
                if employee_id in employee_ids:
                    raise ValueError("duplicate employee id")
                employee_ids.add(employee_id)
                if not row["Department"].strip():
                    raise ValueError("empty department")
                if row["Resigned"].lower() not in {"true", "false"}:
                    raise ValueError("invalid resigned value")
                int(row["Overtime_Hours"])
                int(row["Sick_Days"])
                _decimal(row["Monthly_Salary"])
                _decimal(row["Performance_Score"])
                _decimal(row["Employee_Satisfaction_Score"])
                date.fromisoformat(row["Hire_Date"][:10])
            except (ValueError, KeyError, ArithmeticError) as error:
                raise PreparationError(f"invalid source row {line_number}: {error}") from error
            records.append(dict(row))
            departments.add(row["Department"].strip())
    if expected_rows is not None and len(records) != expected_rows:
        raise PreparationError(f"expected {expected_rows} source rows, found {len(records)}")
    return records, sorted(departments)


def _manager_assignments(records: Sequence[Mapping[str, str]], seed: int) -> dict[int, int | None]:
    groups: dict[tuple[str, str], list[tuple[int, bool]]] = defaultdict(list)
    for row in records:
        employee_id = int(row["Employee_ID"])
        region = _region_for(seed, employee_id)
        groups[(region, row["Department"].strip())].append((employee_id, row["Job_Title"].strip().lower() == "manager"))
    result: dict[int, int | None] = {}
    for group, members in groups.items():
        members.sort()
        candidates = [employee_id for employee_id, is_manager in members if is_manager]
        if not candidates:
            candidates = [members[0][0]]
        head = candidates[0]
        for employee_id, is_manager in members:
            if employee_id == head:
                result[employee_id] = None
            elif is_manager:
                result[employee_id] = head
            else:
                result[employee_id] = candidates[_stable_int(seed, "manager", employee_id, *group) % len(candidates)]
    return result


def _bounded_score(base: Decimal, seed: int, employee_id: int, period: int, label: str) -> Decimal:
    delta = Decimal((_stable_int(seed, label, employee_id, period) % 5) - 2) * Decimal("0.25")
    return min(Decimal("5.00"), max(Decimal("1.00"), base + delta)).quantize(Decimal("0.01"))


def _allocate_integer(total: int, seed: int, employee_id: int, label: str) -> list[int]:
    weights = [1 + _stable_int(seed, label, employee_id, month) % 100 for month in range(12)]
    weight_total = sum(weights)
    allocation = [(total * weight) // weight_total for weight in weights]
    remainder = total - sum(allocation)
    order = sorted(range(12), key=lambda month: _stable_int(seed, label, employee_id, "remainder", month))
    for month in order[:remainder]:
        allocation[month] += 1
    return allocation


def _write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def transform(source: Path, data_root: Path, spec: Mapping[str, object], expected_rows: int | None = None) -> Path:
    transformation = spec["transformation"]
    dataset = spec["dataset"]
    assert isinstance(transformation, Mapping) and isinstance(dataset, Mapping)
    seed = int(transformation["seed"])
    if expected_rows is None:
        expected_rows = int(dataset["rows"])
    records, department_names = _read_source(source, expected_rows)
    managers = _manager_assignments(records, seed)
    generated = data_root / "generated"
    generated.mkdir(parents=True, exist_ok=True)

    department_pairs = sorted({(_region_for(seed, int(row["Employee_ID"])), row["Department"].strip()) for row in records})
    row_counts: dict[str, int] = {}
    row_counts["regions"] = _write_csv(generated / "regions.csv", OUTPUT_HEADERS["regions"], REGIONS)
    row_counts["departments"] = _write_csv(
        generated / "departments.csv",
        OUTPUT_HEADERS["departments"],
        (
            (_department_code(region, department), department, region, f"CC-{_stable_int(seed, region, department) % 100000:05d}")
            for region, department in department_pairs
        ),
    )

    def common(row: Mapping[str, str]) -> tuple[int, str, str, str]:
        employee_id = int(row["Employee_ID"])
        region = _region_for(seed, employee_id)
        manager = managers[employee_id]
        return employee_id, _department_code(region, row["Department"].strip()), region, "" if manager is None else str(manager)

    row_counts["employees"] = _write_csv(
        generated / "employees.csv",
        OUTPUT_HEADERS["employees"],
        (
            (*common(row), row["Gender"], row["Age"], row["Job_Title"], row["Hire_Date"][:10],
             row["Years_At_Company"], row["Education_Level"], row["Work_Hours_Per_Week"],
             row["Projects_Handled"], row["Remote_Work_Frequency"], row["Team_Size"],
             row["Training_Hours"], row["Promotions"], row["Resigned"].lower())
            for row in records
        ),
    )

    def compensation_rows() -> Iterable[Sequence[object]]:
        for row in records:
            employee_id, department, region, manager = common(row)
            latest = _decimal(row["Monthly_Salary"])
            reduction = Decimal(2 + _stable_int(seed, "salary", employee_id) % 11) / Decimal(100)
            previous = (latest * (Decimal(1) - reduction)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            yield employee_id, SALARY_DATES[0], manager, department, region, previous
            yield employee_id, SALARY_DATES[1], manager, department, region, latest

    row_counts["compensation_history"] = _write_csv(
        generated / "compensation_history.csv", OUTPUT_HEADERS["compensation_history"], compensation_rows()
    )

    def performance_rows() -> Iterable[Sequence[object]]:
        for row in records:
            employee_id, department, region, manager = common(row)
            performance = _decimal(row["Performance_Score"])
            satisfaction = _decimal(row["Employee_Satisfaction_Score"])
            for period, review_date in enumerate(REVIEW_DATES):
                if period == len(REVIEW_DATES) - 1:
                    score, sentiment = performance, satisfaction
                else:
                    score = _bounded_score(performance, seed, employee_id, period, "performance")
                    sentiment = _bounded_score(satisfaction, seed, employee_id, period, "satisfaction")
                yield employee_id, review_date, manager, department, region, score, sentiment

    row_counts["performance_reviews"] = _write_csv(
        generated / "performance_reviews.csv", OUTPUT_HEADERS["performance_reviews"], performance_rows()
    )

    def attendance_rows() -> Iterable[Sequence[object]]:
        for row in records:
            employee_id, department, region, manager = common(row)
            overtime = _allocate_integer(int(row["Overtime_Hours"]), seed, employee_id, "overtime")
            sick = _allocate_integer(int(row["Sick_Days"]), seed, employee_id, "sick")
            overtime_running = 0
            sick_running = 0
            for month, attendance_month in enumerate(ATTENDANCE_MONTHS):
                overtime_running += overtime[month]
                sick_running += sick[month]
                yield (
                    employee_id, attendance_month, manager, department, region, overtime[month], sick[month],
                    overtime_running, sick_running,
                )

    row_counts["attendance_monthly"] = _write_csv(
        generated / "attendance_monthly.csv", OUTPUT_HEADERS["attendance_monthly"], attendance_rows()
    )

    output_manifest = {
        "schemaVersion": 1,
        "source": {
            "datasetRef": dataset["ref"],
            "datasetVersion": dataset["version"],
            "memberSha256": _sha256(source),
        },
        "transformation": dict(transformation),
        "departmentsInSource": department_names,
        "outputs": {
            name: {"file": f"{name}.csv", "rows": row_counts[name], "sha256": _sha256(generated / f"{name}.csv")}
            for name in OUTPUT_HEADERS
        },
    }
    with (generated / "manifest.json").open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(output_manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    verify(generated)
    return generated


def verify(generated: Path) -> None:
    manifest_path = generated / "manifest.json"
    if not manifest_path.is_file():
        raise PreparationError("generated manifest is missing")
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    outputs = manifest.get("outputs", {})
    row_counts: dict[str, int] = {}
    for name, headers in OUTPUT_HEADERS.items():
        metadata = outputs.get(name)
        path = generated / f"{name}.csv"
        if not isinstance(metadata, dict) or not path.is_file():
            raise PreparationError(f"generated output is missing: {name}")
        if _sha256(path) != metadata.get("sha256"):
            raise PreparationError(f"generated output checksum mismatch: {name}")
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            if tuple(next(reader, ())) != headers:
                raise PreparationError(f"generated output columns mismatch: {name}")
            rows = sum(1 for _ in reader)
        if rows != metadata.get("rows"):
            raise PreparationError(f"generated output row count mismatch: {name}")
        row_counts[name] = rows
    transformation = manifest.get("transformation", {})
    employee_rows = row_counts["employees"]
    expected_counts = {
        "regions": transformation.get("regions"),
        "compensation_history": employee_rows * int(transformation.get("salaryRowsPerEmployee", 0)),
        "performance_reviews": employee_rows * int(transformation.get("performanceRowsPerEmployee", 0)),
        "attendance_monthly": employee_rows * int(transformation.get("attendanceRowsPerEmployee", 0)),
    }
    if any(row_counts[name] != expected for name, expected in expected_counts.items()):
        raise PreparationError("generated output cardinalities conflict with the transformation manifest")

    employee_scope: dict[str, tuple[str, str, str]] = {}
    with (generated / "employees.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            employee_scope[row["employee_id"]] = (row["department_code"], row["region_code"], row["manager_id"])
    if row_counts["departments"] != len({scope[0] for scope in employee_scope.values()}):
        raise PreparationError("generated departments do not match employee department scopes")
    for employee_id, (department, region, manager) in employee_scope.items():
        if manager:
            manager_scope = employee_scope.get(manager)
            if manager == employee_id or manager_scope is None or manager_scope[:2] != (department, region):
                raise PreparationError("employee manager boundary is invalid")
        visited = {employee_id}
        ancestor = manager
        while ancestor:
            if ancestor in visited:
                raise PreparationError("employee manager hierarchy contains a cycle")
            visited.add(ancestor)
            ancestor_scope = employee_scope.get(ancestor)
            if ancestor_scope is None:
                raise PreparationError("employee manager hierarchy references an unknown employee")
            ancestor = ancestor_scope[2]
    for name in ("compensation_history", "performance_reviews", "attendance_monthly"):
        with (generated / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                scope = employee_scope.get(row["employee_id"])
                if scope is None or (row["department_code"], row["region_code"], row["manager_id"]) != scope:
                    raise PreparationError(f"generated history scope mismatch: {name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("download", "transform", "verify", "all"))
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--source", type=Path, help="source CSV (transform only)")
    parser.add_argument("--expected-rows", type=int, help="override pinned row count for fixture tests")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    spec = _load_spec()
    try:
        source = args.source or args.data_root / "raw" / str(spec["dataset"]["member"])
        if args.command in {"download", "all"}:
            source = download(args.data_root, spec)
        if args.command in {"transform", "all"}:
            transform(source, args.data_root, spec, args.expected_rows)
        if args.command == "verify":
            verify(args.data_root / "generated")
    except (OSError, ValueError, zipfile.BadZipFile, PreparationError) as error:
        print(f"HR benchmark preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
