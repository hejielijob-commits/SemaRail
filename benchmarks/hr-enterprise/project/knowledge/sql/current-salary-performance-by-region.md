---
nl: 按区域编码返回当前平均薪资和当前平均绩效。
sql: |
  SELECT c.region_code,
         AVG(c.monthly_salary) AS average_salary,
         AVG(p.performance_score) AS average_performance
  FROM compensation_history c
  JOIN performance_reviews p ON c.employee_id = p.employee_id
  WHERE c.effective_date = DATE '2024-09-01'
    AND p.review_date = DATE '2024-09-30'
  GROUP BY 1
  ORDER BY 1
---

Filter both facts to one snapshot before joining so each employee contributes
once to each regional average.
