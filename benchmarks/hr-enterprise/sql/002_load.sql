\set ON_ERROR_STOP on

BEGIN;
SET CONSTRAINTS ALL DEFERRED;

\copy hr.regions FROM '/benchmark-data/regions.csv' WITH (FORMAT csv, HEADER true)
\copy hr.departments FROM '/benchmark-data/departments.csv' WITH (FORMAT csv, HEADER true)
\copy hr.employees FROM '/benchmark-data/employees.csv' WITH (FORMAT csv, HEADER true)
\copy hr.compensation_history FROM '/benchmark-data/compensation_history.csv' WITH (FORMAT csv, HEADER true)
\copy hr.performance_reviews FROM '/benchmark-data/performance_reviews.csv' WITH (FORMAT csv, HEADER true)
\copy hr.attendance_monthly FROM '/benchmark-data/attendance_monthly.csv' WITH (FORMAT csv, HEADER true)

CREATE ROLE hr_benchmark_reader LOGIN PASSWORD 'hr_benchmark_readonly';
ALTER ROLE hr_benchmark_reader SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE hr_enterprise TO hr_benchmark_reader;
GRANT USAGE ON SCHEMA hr TO hr_benchmark_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA hr TO hr_benchmark_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA hr GRANT SELECT ON TABLES TO hr_benchmark_reader;

ANALYZE hr.regions;
ANALYZE hr.departments;
ANALYZE hr.employees;
ANALYZE hr.compensation_history;
ANALYZE hr.performance_reviews;
ANALYZE hr.attendance_monthly;

COMMIT;
