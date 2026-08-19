---
nl: 各地区销售额是多少？
sql: |
  SELECT regions.name AS region,
         SUM(order_items.line_revenue) AS revenue
  FROM order_items
  JOIN orders ON order_items.order_id = orders.id
  JOIN customers ON orders.customer_id = customers.id
  JOIN regions ON customers.region_id = regions.id
  WHERE orders.status <> 'cancelled'
  GROUP BY 1
  ORDER BY 2 DESC
---

This example exercises two relationship hops from items to customer region.
