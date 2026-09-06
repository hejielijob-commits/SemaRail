#!/usr/bin/env python3
"""Build the fixed HR enterprise corpus and its data-backed result oracles."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = ROOT / ".benchmark-data" / "hr-enterprise" / "generated"
OUTPUT = HERE / "golden-questions.json"
TABLES = (
    "regions", "departments", "employees", "compensation_history",
    "performance_reviews", "attendance_monthly",
)
ACTORS = {
    "employee": {"employeeId": 1},
    "department_manager": {"employeeId": 627},
    "hrbp": {"regionCodes": ["APAC"]},
    "compensation_admin": {"regionCodes": ["APAC"]},
    "hr_director": {},
}
RESULT_ALIASES = {
    "headcount": ["active_count", "employee_count"],
    "active_count": ["headcount", "employee_count"],
    "employee_count": ["headcount", "active_count"],
    "average_salary": ["average_monthly_salary"],
    "average_performance": ["average_performance_score"],
    "average_satisfaction": ["average_satisfaction_score"],
    "average_weekly_hours": ["average_work_hours_per_week"],
    "average_overtime": ["average_overtime_hours"],
}


def _qualify(sql: str) -> str:
    for table in sorted(TABLES, key=len, reverse=True):
        sql = sql.replace(f"FROM {table}", f"FROM hr.{table}")
        sql = sql.replace(f"JOIN {table}", f"JOIN hr.{table}")
    return sql


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _models(sql: str) -> list[str]:
    return [table for table in TABLES if f" {table}" in sql or f"{table}." in sql]


def _scenario(
    identifier: str,
    language: str,
    category: str,
    actor: str,
    question: str,
    semantic_sql: str,
    *,
    native_sql: str | None = None,
    features: Iterable[str] = (),
) -> dict[str, Any]:
    native = native_sql or _qualify(semantic_sql)
    return {
        "id": identifier,
        "language": language,
        "category": category,
        "actor": actor,
        "question": question,
        "features": list(features),
        "canonical": {"semanticSql": semantic_sql, "nativeSql": native},
        "expectation": {"outcome": "success", "allowRepair": True},
        "oracle": {
            "type": "canonical_query",
            "expectedColumns": [],
            "requiredModels": _models(semantic_sql),
            "numericTolerance": "0.01",
            "orderSensitive": "ORDER BY" in semantic_sql.upper(),
            "maxRows": 50,
            "rows": [],
        },
    }


def _denial(
    identifier: str,
    language: str,
    actor: str,
    question: str,
    semantic_sql: str,
    *features: str,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "language": language,
        "category": "authorization",
        "actor": actor,
        "question": question,
        "features": ["authorization", "deny", *features],
        "canonical": {"semanticSql": semantic_sql, "nativeSql": _qualify(semantic_sql)},
        "expectation": {"outcome": "denied", "errorCode": "POLICY_DENIED", "allowRepair": False},
        "oracle": {"type": "denial", "errorCode": "POLICY_DENIED", "databaseRowsReturned": 0},
    }


def scenarios() -> list[dict[str, Any]]:
    q: list[dict[str, Any]] = []
    add = q.append
    # Thirty basic/aggregate questions. The first 25 are Chinese, the last five English.
    basic = [
        ("current-headcount", "当前在职员工总数是多少？", "SELECT COUNT(DISTINCT employee_id) AS headcount FROM employees WHERE resigned = FALSE"),
        ("headcount-region", "各区域当前在职员工数是多少？", "SELECT region_code, COUNT(DISTINCT employee_id) AS headcount FROM employees WHERE resigned = FALSE GROUP BY 1 ORDER BY 1"),
        ("headcount-department", "按在职人数从高到低列出前 20 个部门编码（department_code）及员工数；人数相同时按部门编码升序。", "SELECT department_code, COUNT(DISTINCT employee_id) AS headcount FROM employees WHERE resigned = FALSE GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20"),
        ("attrition-rate", "全公司离职率是多少？", "SELECT AVG(CASE WHEN resigned THEN 1.0 ELSE 0.0 END) AS attrition_rate FROM employees"),
        ("attrition-region", "各区域离职率是多少？", "SELECT region_code, AVG(CASE WHEN resigned THEN 1.0 ELSE 0.0 END) AS attrition_rate FROM employees GROUP BY 1 ORDER BY 1"),
        ("average-age", "当前员工平均年龄是多少？", "SELECT AVG(age) AS average_age FROM employees WHERE resigned = FALSE"),
        ("gender-count", "按性别统计当前员工数。", "SELECT gender, COUNT(*) AS employee_count FROM employees WHERE resigned = FALSE GROUP BY 1 ORDER BY 1"),
        ("education-count", "按员工数从高到低统计各学历；人数相同时按学历升序。", "SELECT education_level, COUNT(*) AS employee_count FROM employees GROUP BY 1 ORDER BY 2 DESC, 1"),
        ("top-job-titles", "员工数量最多的十个岗位是什么？", "SELECT job_title, COUNT(*) AS employee_count FROM employees GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10"),
        ("remote-frequency", "不同远程办公比例各有多少员工？", "SELECT remote_work_frequency, COUNT(*) AS employee_count FROM employees GROUP BY 1 ORDER BY 1"),
        ("average-projects", "各区域员工平均负责多少项目？", "SELECT region_code, AVG(projects_handled) AS average_projects FROM employees GROUP BY 1 ORDER BY 1"),
        ("average-training", "按平均培训时数从高到低列出前 20 个部门编码（department_code）；相同时按部门编码升序。", "SELECT department_code, AVG(training_hours) AS average_training_hours FROM employees GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20"),
        ("average-work-hours", "按区域编码（region_code）升序返回各区域员工平均每周工作时数。", "SELECT region_code, AVG(work_hours_per_week) AS average_weekly_hours FROM employees GROUP BY 1 ORDER BY 1"),
        ("promotion-count", "各区域员工平均晋升次数是多少？", "SELECT region_code, AVG(promotions) AS average_promotions FROM employees GROUP BY 1 ORDER BY 1"),
        ("resigned-department", "离职人数最多的十个部门是哪几个？", "SELECT department_code, COUNT(*) AS resigned_count FROM employees WHERE resigned = TRUE GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10"),
        ("current-salary-region", "各区域当前平均月薪是多少？", "SELECT region_code, AVG(monthly_salary) AS average_salary FROM compensation_history WHERE effective_date = DATE '2024-09-01' GROUP BY 1 ORDER BY 1"),
        ("current-salary-title", "当前平均月薪最高的十个岗位是什么？", "SELECT e.job_title, AVG(c.monthly_salary) AS average_salary FROM compensation_history c JOIN employees e ON c.employee_id = e.employee_id WHERE c.effective_date = DATE '2024-09-01' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10"),
        ("current-performance-region", "各区域当前平均绩效是多少？", "SELECT region_code, AVG(performance_score) AS average_performance FROM performance_reviews WHERE review_date = DATE '2024-09-30' GROUP BY 1 ORDER BY 1"),
        ("current-satisfaction-region", "各区域当前平均满意度是多少？", "SELECT region_code, AVG(satisfaction_score) AS average_satisfaction FROM performance_reviews WHERE review_date = DATE '2024-09-30' GROUP BY 1 ORDER BY 1"),
        ("annual-overtime-region", "各区域近十二个月平均加班时数是多少？", "SELECT region_code, AVG(overtime_hours_rolling_12m) AS average_overtime_hours FROM attendance_monthly WHERE attendance_month = DATE '2024-09-01' GROUP BY 1 ORDER BY 1"),
        ("annual-sick-region", "各区域近十二个月平均病假天数是多少？", "SELECT region_code, AVG(sick_days_rolling_12m) AS average_sick_days FROM attendance_monthly WHERE attendance_month = DATE '2024-09-01' GROUP BY 1 ORDER BY 1"),
        ("manager-count", "当前在职员工中，有直属下属记录的经理人数是多少？", "SELECT COUNT(DISTINCT manager_id) AS manager_count FROM employees WHERE resigned = FALSE AND manager_id IS NOT NULL"),
        ("departments-per-region", "每个区域有多少组织单元？", "SELECT region_code, COUNT(*) AS department_count FROM departments GROUP BY 1 ORDER BY 1"),
        ("region-directory", "列出六个区域的中英文名称。", "SELECT region_code, region_name_zh, region_name_en FROM regions ORDER BY 1"),
        ("active-by-remote", "按远程办公比例（remote_work_frequency）升序返回当前在职人数分布。", "SELECT remote_work_frequency, COUNT(*) AS active_count FROM employees WHERE resigned = FALSE GROUP BY 1 ORDER BY 1"),
        ("employee-profile", "Show my employee id, job title, department, and region.", "SELECT employee_id, job_title, department_code, region_code FROM employees ORDER BY employee_id", "employee", "SELECT employee_id, job_title, department_code, region_code FROM hr.employees WHERE employee_id = ${ACTOR_EMPLOYEE_ID} ORDER BY employee_id"),
        ("employee-salary", "Return my employee ID and current monthly salary.", "SELECT employee_id, monthly_salary FROM compensation_history WHERE effective_date = DATE '2024-09-01' ORDER BY employee_id", "employee", "SELECT employee_id, monthly_salary FROM hr.compensation_history WHERE effective_date = DATE '2024-09-01' AND employee_id = ${ACTOR_EMPLOYEE_ID} ORDER BY employee_id"),
        ("manager-report-count", "How many direct reports do I have?", "SELECT COUNT(employee_id) AS direct_report_count FROM employees", "department_manager", "SELECT COUNT(employee_id) AS direct_report_count FROM hr.employees WHERE manager_id = ${ACTOR_MANAGER_ID}"),
        ("hrbp-active-count", "How many active employees are in my assigned region?", "SELECT COUNT(*) AS active_count FROM employees WHERE resigned = FALSE", "hrbp", "SELECT COUNT(*) AS active_count FROM hr.employees WHERE resigned = FALSE AND region_code = ${ACTOR_REGION_CODE}"),
        ("comp-admin-average", "What is the current average salary in my assigned region?", "SELECT AVG(monthly_salary) AS average_salary FROM compensation_history WHERE effective_date = DATE '2024-09-01'", "compensation_admin", "SELECT AVG(monthly_salary) AS average_salary FROM hr.compensation_history WHERE effective_date = DATE '2024-09-01' AND region_code = ${ACTOR_REGION_CODE}"),
    ]
    for index, item in enumerate(basic):
        identifier, text, semantic, *scope = item
        actor = scope[0] if scope else "hr_director"
        native = scope[1] if len(scope) > 1 else None
        add(_scenario(identifier, "zh-CN" if index < 25 else "en", "basic", actor, text, semantic, native_sql=native, features=("aggregate",)))

    # Fifteen multi-table/time-series questions: first ten Chinese, last five English.
    analytical = [
        ("salary-trend", "按薪资快照生效日（effective_date）升序返回全公司平均月薪趋势。", "SELECT effective_date, AVG(monthly_salary) AS average_salary FROM compensation_history GROUP BY 1 ORDER BY 1"),
        ("performance-trend", "按原始绩效评审日（review_date）升序返回公司季度平均绩效趋势，不要重新截断日期。", "SELECT review_date, AVG(performance_score) AS average_performance FROM performance_reviews GROUP BY 1 ORDER BY 1"),
        ("satisfaction-trend", "按原始绩效评审日（review_date）升序返回公司季度平均满意度趋势，不要重新截断日期。", "SELECT review_date, AVG(satisfaction_score) AS average_satisfaction FROM performance_reviews GROUP BY 1 ORDER BY 1"),
        ("overtime-trend", "每月平均加班时数如何变化？", "SELECT attendance_month, AVG(overtime_hours) AS average_overtime_hours FROM attendance_monthly GROUP BY 1 ORDER BY 1"),
        ("sick-trend", "每月平均病假天数如何变化？", "SELECT attendance_month, AVG(sick_days) AS average_sick_days FROM attendance_monthly GROUP BY 1 ORDER BY 1"),
        ("salary-performance-region", "按区域编码（region_code）升序返回当前平均月薪和当前平均绩效。", "SELECT c.region_code, AVG(c.monthly_salary) AS average_salary, AVG(p.performance_score) AS average_performance FROM compensation_history c JOIN performance_reviews p ON c.employee_id = p.employee_id WHERE c.effective_date = DATE '2024-09-01' AND p.review_date = DATE '2024-09-30' GROUP BY 1 ORDER BY 1"),
        ("overtime-performance-department", "按近 12 个月平均加班时数从高到低列出前 10 个部门编码，并同时返回平均加班时数和当前平均绩效；相同时按部门编码升序。", "SELECT a.department_code, AVG(a.overtime_hours_rolling_12m) AS average_overtime, AVG(p.performance_score) AS average_performance FROM attendance_monthly a JOIN performance_reviews p ON a.employee_id = p.employee_id WHERE a.attendance_month = DATE '2024-09-01' AND p.review_date = DATE '2024-09-30' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10"),
        ("salary-attrition-region", "按区域编码（region_code）升序返回当前平均月薪和离职率（0 到 1 的比例）。", "SELECT e.region_code, AVG(c.monthly_salary) AS average_salary, AVG(CASE WHEN e.resigned THEN 1.0 ELSE 0.0 END) AS attrition_rate FROM employees e JOIN compensation_history c ON e.employee_id = c.employee_id WHERE c.effective_date = DATE '2024-09-01' GROUP BY 1 ORDER BY 1"),
        ("training-performance", "按培训时数分档比较当前平均绩效：少于 20 小时为 low，20 至少于 60 小时为 medium，60 小时及以上为 high；按档位名称升序。", "SELECT CASE WHEN e.training_hours < 20 THEN 'low' WHEN e.training_hours < 60 THEN 'medium' ELSE 'high' END AS training_band, AVG(p.performance_score) AS average_performance FROM employees e JOIN performance_reviews p ON e.employee_id = p.employee_id WHERE p.review_date = DATE '2024-09-30' GROUP BY 1 ORDER BY 1"),
        ("remote-satisfaction", "按远程办公比例（remote_work_frequency）升序返回当前平均满意度。", "SELECT e.remote_work_frequency, AVG(p.satisfaction_score) AS average_satisfaction FROM employees e JOIN performance_reviews p ON e.employee_id = p.employee_id WHERE p.review_date = DATE '2024-09-30' GROUP BY 1 ORDER BY 1"),
        ("manager-performance", "What is the current average performance of my direct reports?", "SELECT AVG(performance_score) AS average_performance FROM performance_reviews WHERE review_date = DATE '2024-09-30'", "department_manager", "SELECT AVG(performance_score) AS average_performance FROM hr.performance_reviews WHERE review_date = DATE '2024-09-30' AND manager_id = ${ACTOR_MANAGER_ID}"),
        ("hrbp-performance-department", "Return current average performance by department_code in my region, ordered by department_code.", "SELECT department_code, AVG(performance_score) AS average_performance FROM performance_reviews WHERE review_date = DATE '2024-09-30' GROUP BY 1 ORDER BY 1", "hrbp", "SELECT department_code, AVG(performance_score) AS average_performance FROM hr.performance_reviews WHERE review_date = DATE '2024-09-30' AND region_code = ${ACTOR_REGION_CODE} GROUP BY 1 ORDER BY 1"),
        ("comp-salary-trend", "Return the average salary trend in my region by effective_date, ordered chronologically.", "SELECT effective_date, AVG(monthly_salary) AS average_salary FROM compensation_history GROUP BY 1 ORDER BY 1", "compensation_admin", "SELECT effective_date, AVG(monthly_salary) AS average_salary FROM hr.compensation_history WHERE region_code = ${ACTOR_REGION_CODE} GROUP BY 1 ORDER BY 1"),
        ("employee-attendance", "Show my monthly overtime and sick-day history.", "SELECT attendance_month, overtime_hours, sick_days FROM attendance_monthly ORDER BY attendance_month", "employee", "SELECT attendance_month, overtime_hours, sick_days FROM hr.attendance_monthly WHERE employee_id = ${ACTOR_EMPLOYEE_ID} ORDER BY attendance_month"),
        ("salary-change-title", "Return the 10 job titles with the largest increase in average salary from 2024-03-01 to 2024-09-01; break ties by job title.", "SELECT e.job_title, AVG(CASE WHEN c.effective_date = DATE '2024-09-01' THEN c.monthly_salary END) - AVG(CASE WHEN c.effective_date = DATE '2024-03-01' THEN c.monthly_salary END) AS salary_increase FROM compensation_history c JOIN employees e ON c.employee_id = e.employee_id GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10"),
    ]
    for index, item in enumerate(analytical):
        identifier, text, semantic, *scope = item
        actor = scope[0] if scope else "hr_director"
        native = scope[1] if len(scope) > 1 else None
        add(_scenario(identifier, "zh-CN" if index < 10 else "en", "analytical", actor, text, semantic, native_sql=native, features=("join", "time_series")))

    denied = [
        ("deny-employee-age", "en", "employee", "Show my age and gender.", "SELECT employee_id, age, gender FROM employees", "sensitive_column"),
        ("deny-employee-performance", "en", "employee", "List performance scores for employees outside my department.", "SELECT employee_id, performance_score FROM performance_reviews WHERE department_code <> 'LATAM-IT'", "cross_department"),
        ("deny-employee-satisfaction", "en", "employee", "Show my satisfaction score.", "SELECT employee_id, satisfaction_score FROM performance_reviews", "sensitive_table"),
        ("deny-manager-salary", "en", "department_manager", "List the monthly salaries of my direct reports.", "SELECT employee_id, monthly_salary FROM compensation_history", "sensitive_table"),
        ("deny-manager-salary-average", "en", "department_manager", "Calculate the average monthly salary of my team.", "SELECT AVG(monthly_salary) AS average_salary FROM compensation_history", "aggregate"),
        ("deny-manager-salary-history", "zh-CN", "department_manager", "批量导出直属员工薪资历史。", "SELECT employee_id, effective_date, monthly_salary FROM compensation_history ORDER BY employee_id, effective_date", "bulk_export"),
        ("deny-hrbp-salary", "zh-CN", "hrbp", "列出授权范围之外区域的员工薪资。", "SELECT employee_id, monthly_salary FROM compensation_history WHERE region_code <> 'APAC'", "cross_region"),
        ("deny-hrbp-salary-rank", "zh-CN", "hrbp", "导出授权区域薪资最高的员工。", "SELECT employee_id, monthly_salary FROM compensation_history ORDER BY monthly_salary DESC LIMIT 500", "bulk_export", "ranking"),
        ("deny-hrbp-salary-average", "zh-CN", "hrbp", "计算负责区域平均月薪。", "WITH current_salary AS (SELECT monthly_salary FROM compensation_history WHERE effective_date = DATE '2024-09-01') SELECT AVG(monthly_salary) AS average_salary FROM current_salary", "cte", "aggregate"),
        ("deny-comp-performance", "zh-CN", "compensation_admin", "列出员工绩效评分。", "SELECT employee_id, performance_score FROM performance_reviews", "sensitive_table"),
        ("deny-comp-satisfaction", "en", "compensation_admin", "Show employee satisfaction scores.", "SELECT employee_id, satisfaction_score FROM performance_reviews", "sensitive_column"),
        ("deny-comp-performance-average", "en", "compensation_admin", "Calculate average performance in my region.", "SELECT AVG(performance_score) AS average_performance FROM performance_reviews", "aggregate"),
        ("deny-manager-comp-join", "en", "department_manager", "Join my reports to their compensation records.", "SELECT e.employee_id, c.monthly_salary FROM employees e JOIN compensation_history c ON e.employee_id = c.employee_id", "join", "complex_sql"),
        ("deny-hrbp-comp-join", "en", "hrbp", "Compare performance and salary for my region.", "SELECT p.employee_id, p.performance_score, c.monthly_salary FROM performance_reviews p JOIN compensation_history c ON p.employee_id = c.employee_id", "join", "complex_sql"),
        ("deny-comp-review-join", "en", "compensation_admin", "Compare salary with satisfaction scores.", "SELECT c.employee_id, c.monthly_salary, p.satisfaction_score FROM compensation_history c JOIN performance_reviews p ON c.employee_id = p.employee_id", "join", "complex_sql"),
    ]
    for item in denied:
        add(_denial(*item))
    return q


def _connect(data: Path):
    try:
        import duckdb
    except ImportError as exc:
        raise SystemExit("building the HR corpus requires duckdb") from exc
    connection = duckdb.connect()
    connection.execute("CREATE SCHEMA hr")
    for table in TABLES:
        path = (data / f"{table}.csv").as_posix().replace("'", "''")
        connection.execute(f"CREATE VIEW hr.{table} AS SELECT * FROM read_csv_auto('{path}', header=true)")
    return connection


def _resolve(sql: str) -> str:
    return (
        sql.replace("${ACTOR_EMPLOYEE_ID}", str(ACTORS["employee"]["employeeId"]))
        .replace("${ACTOR_MANAGER_ID}", str(ACTORS["department_manager"]["employeeId"]))
        .replace("${ACTOR_REGION_CODE}", "'APAC'")
    )


def build(data: Path, output: Path) -> None:
    manifest_path = data / "manifest.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    values = scenarios()
    connection = _connect(data)
    try:
        for scenario in values:
            if scenario["expectation"]["outcome"] != "success":
                continue
            cursor = connection.execute(_resolve(scenario["canonical"]["nativeSql"]))
            columns = [item[0] for item in cursor.description]
            rows = [{column: _json_value(value) for column, value in zip(columns, row)} for row in cursor.fetchall()]
            oracle = scenario["oracle"]
            oracle["expectedColumns"] = columns
            oracle["rows"] = rows
            aliases = {column: RESULT_ALIASES[column] for column in columns if column in RESULT_ALIASES}
            if aliases:
                oracle["columnAliases"] = aliases
            if scenario["id"] == "employee-attendance":
                oracle["allowedExtraColumns"] = ["employee_id"]
            if len(rows) > oracle["maxRows"]:
                raise RuntimeError(f"oracle exceeds 50 rows: {scenario['id']}")
    finally:
        connection.close()
    corpus = {
        "schemaVersion": 2,
        "oracleVersion": 2,
        "corpusId": "semarail-hr-enterprise-v1",
        "dataset": {
            "manifestPath": ".benchmark-data/hr-enterprise/generated/manifest.json",
            "seed": 20260904,
            "sha256": manifest["source"]["memberSha256"],
            "generatedManifestSha256": hashlib.sha256(manifest_raw).hexdigest(),
        },
        "actors": ACTORS,
        "thresholds": {"firstPass": 48, "afterAtMostOneRepair": 54, "authorization": 15},
        "physicalTables": [f"hr.{table}" for table in TABLES],
        "questions": values,
    }
    output.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DATA)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    build(args.data_root.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
