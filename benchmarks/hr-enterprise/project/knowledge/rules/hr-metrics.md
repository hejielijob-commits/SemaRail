## HR metric definitions / 人力指标口径

- Headcount / 员工数 means `COUNT(DISTINCT employees.employee_id)`. Add
  `employees.resigned = FALSE` only when the question asks for current or active
  employees / 只有“当前、在职”员工数排除已离职员工。
- Resignation or attrition rate / 离职率 is
  `AVG(CASE WHEN employees.resigned THEN 1.0 ELSE 0.0 END)` over the requested
  population. Return a 0-to-1 ratio, not a percentage, unless explicitly asked.
- Average salary / 平均薪资 means `AVG(compensation_history.monthly_salary)`
  at the latest effective date (`2024-09-01`) unless the question explicitly
  requests salary history.
- Current performance and satisfaction / 当前绩效与满意度 use the latest review
  date (`2024-09-30`). Use all four review dates only for trends.
- Annual overtime and sick leave / 年度加班和病假 use
  `overtime_hours_rolling_12m` and `sick_days_rolling_12m` from the latest
  attendance month (`2024-09-01`). Use monthly fields for monthly trends.
- Organization names are joined by `department_code` and `region_code`.
  Fact tables deliberately retain these keys so row policies can be enforced
  before or after joins.

## Query shape / 查询结果形状

- Group by and return `region_code` or `department_code` by default. Join the
  `regions` or `departments` dimensions only when the question explicitly asks
  for a display name / 默认按编码分组并返回编码；只有问题明确要求名称时才连接维表。
- Prefer `headcount` for distinct employee headcount, `employee_count` for
  category distributions, and `active_count` for a policy-scoped active count.
  Equivalent descriptive aliases do not change the business meaning. Prefer
  `average_salary`, `average_performance`, `average_satisfaction`,
  `average_training_hours`, and `average_weekly_hours` for averages.
- Preserve snapshot columns (`effective_date`, `review_date`,
  `attendance_month`) as stored for trends. Do not apply `DATE_TRUNC` to data
  that is already one row per benchmark snapshot.
- When a question specifies top N, order by the metric descending, add the
  grouping key ascending as a deterministic tie-breaker, and apply `LIMIT N`.
- For self-scoped employee detail, project only the fields requested. Include
  `employee_id` when the question asks for it; do not add an explicit actor id
  predicate because the policy engine supplies the row boundary.

## Cross-model analysis / 跨模型分析

- Join employee-grain fact models on `employee_id`; do not add a dimension join
  merely to replace a requested code with a name.
- Before joining snapshot fact tables, filter each side to the requested or
  latest snapshot: salary `2024-09-01`, reviews `2024-09-30`, and attendance
  `2024-09-01`. This prevents many-to-many multiplication across time.
- For current annual overtime use `AVG(overtime_hours_rolling_12m)` from the
  latest attendance snapshot. "Departments with the most overtime" in this
  benchmark ranks that per-employee department average, not a workforce-size
  dependent sum.
- Training bands are fixed as `low` below 20 hours, `medium` from 20 to below
  60, and `high` from 60 hours upward.

## Security semantics / 安全语义

- Salary, performance, satisfaction, sick leave, and employee-level details
  are sensitive. Never replace a policy denial with a differently shaped query.
- The ordinary employee sees self only. A department manager sees direct reports
  only and no salary. HRBP scope is its authorized region and excludes salary.
  Compensation admin scope is its authorized region and excludes performance
  and satisfaction. HR director has company-wide HR access.
- Missing identity attributes, an unbound policy, cross-region access, and any
  forbidden sensitive column must fail closed with `POLICY_DENIED`.
- `manager_id` represents only direct reports. Do not infer recursive reporting
  lines from it.
