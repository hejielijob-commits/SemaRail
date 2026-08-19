## Revenue and order rules

- Revenue means `order_items.line_revenue`: quantity multiplied by unit price,
  less discount. Never sum `products.list_price` for revenue.
- Exclude orders whose status is `cancelled` from revenue and completed-order
  metrics unless the question explicitly asks about cancellations.
- `ordered_at` is UTC. Preserve the requested date grain explicitly.
- Customer region is joined through `orders.customer_id`,
  `customers.region_id`, and `regions.id`.
- A null discount means zero discount; it does not mean an unknown sale.

## Privacy

- This model deliberately exposes no customer email, address, or payment data.
- Do not infer or request columns that do not appear in semantic context.
