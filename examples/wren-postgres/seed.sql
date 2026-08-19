BEGIN;

CREATE TABLE regions (
  id integer PRIMARY KEY,
  name text NOT NULL,
  country text NOT NULL
);

CREATE TABLE customers (
  id integer PRIMARY KEY,
  display_name text NOT NULL,
  email text NOT NULL,
  region_id integer NOT NULL REFERENCES regions(id),
  segment text NOT NULL
);

CREATE TABLE products (
  id integer PRIMARY KEY,
  sku text NOT NULL UNIQUE,
  name text NOT NULL,
  category text NOT NULL,
  list_price numeric(12, 2) NOT NULL CHECK (list_price >= 0)
);

CREATE TABLE orders (
  id integer PRIMARY KEY,
  customer_id integer NOT NULL REFERENCES customers(id),
  ordered_at timestamp with time zone NOT NULL,
  status text NOT NULL CHECK (status IN ('pending', 'paid', 'shipped', 'cancelled'))
);

CREATE TABLE order_items (
  id integer PRIMARY KEY,
  order_id integer NOT NULL REFERENCES orders(id),
  product_id integer NOT NULL REFERENCES products(id),
  quantity integer NOT NULL CHECK (quantity > 0),
  unit_price numeric(12, 2) NOT NULL CHECK (unit_price >= 0),
  discount_amount numeric(12, 2)
);

INSERT INTO regions VALUES
  (1, '华东', '中国'),
  (2, '华南', '中国'),
  (3, '华北', '中国');

INSERT INTO customers VALUES
  (1, '星河零售', 'private-1@example.invalid', 1, 'enterprise'),
  (2, '海风商贸', 'private-2@example.invalid', 2, 'mid_market'),
  (3, '北辰小店', 'private-3@example.invalid', 3, 'small_business'),
  (4, '匿名访客', 'private-4@example.invalid', 1, 'consumer');

INSERT INTO products VALUES
  (1, 'NOTE-01', '轻薄笔记本', '电脑', 6999.00),
  (2, 'MON-27', '27 寸显示器', '配件', 1899.00),
  (3, 'KEY-87', '机械键盘', '配件', 699.00),
  (4, 'CHAIR-1', '人体工学椅', '办公家具', 2499.00);

INSERT INTO orders VALUES
  (1, 1, CURRENT_DATE - INTERVAL '3 days' + TIME '09:15', 'shipped'),
  (2, 2, CURRENT_DATE - INTERVAL '2 days' + TIME '10:30', 'paid'),
  (3, 3, CURRENT_DATE - INTERVAL '1 day' + TIME '11:45', 'cancelled'),
  (4, 1, CURRENT_DATE - INTERVAL '1 day' + TIME '15:20', 'paid'),
  (5, 4, CURRENT_DATE + TIME '08:05', 'pending');

INSERT INTO order_items VALUES
  (1, 1, 1, 2, 6699.00, 200.00),
  (2, 1, 2, 2, 1799.00, NULL),
  (3, 2, 3, 5, 649.00, 100.00),
  (4, 3, 4, 1, 2499.00, NULL),
  (5, 4, 2, 3, 1699.00, 150.00),
  (6, 5, 3, 1, 699.00, NULL);

COMMIT;
