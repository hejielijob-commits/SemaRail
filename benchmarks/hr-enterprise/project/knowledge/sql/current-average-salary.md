---
nl: 按区域编码返回当前平均月薪。
sql: |
  SELECT region_code,
         AVG(monthly_salary) AS average_salary
  FROM compensation_history
  WHERE effective_date = DATE '2024-09-01'
  GROUP BY 1
  ORDER BY 1
---

Current salary means the latest fixed benchmark snapshot, not both salary rows.
