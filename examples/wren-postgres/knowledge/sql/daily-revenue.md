---
nl: 最近 30 天每天的销售额是多少？
sql: |
  SELECT DATE_TRUNC('day', orders.ordered_at) AS order_day,
         SUM(order_items.line_revenue) AS revenue
  FROM order_items
  JOIN orders ON order_items.order_id = orders.id
  WHERE orders.status <> 'cancelled'
    AND orders.ordered_at >= CURRENT_DATE - INTERVAL '30 days'
  GROUP BY 1
  ORDER BY 1
---

Revenue uses line revenue after discount and excludes cancelled orders.
