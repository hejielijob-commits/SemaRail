---
nl: 按原始评审日返回季度平均绩效趋势。
sql: |
  SELECT review_date,
         AVG(performance_score) AS average_performance
  FROM performance_reviews
  GROUP BY 1
  ORDER BY 1
---

Review rows already use quarter-end snapshot dates. Preserve `review_date`
instead of applying another calendar transformation.
