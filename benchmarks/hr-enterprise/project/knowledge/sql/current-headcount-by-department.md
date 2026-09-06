---
nl: 按在职人数从高到低列出前 20 个部门编码。
sql: |
  SELECT department_code,
         COUNT(DISTINCT employee_id) AS headcount
  FROM employees
  WHERE resigned = FALSE
  GROUP BY 1
  ORDER BY 2 DESC, 1
  LIMIT 20
---

Current headcount excludes resigned employees and counts employee ids once.
