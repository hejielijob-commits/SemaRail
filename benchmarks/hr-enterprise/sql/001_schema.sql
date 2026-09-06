\set ON_ERROR_STOP on

CREATE SCHEMA hr;

CREATE TABLE hr.regions (
    region_code varchar(8) PRIMARY KEY,
    region_name_en text NOT NULL,
    region_name_zh text NOT NULL
);

CREATE TABLE hr.departments (
    department_code varchar(64) PRIMARY KEY,
    department_name text NOT NULL,
    region_code varchar(8) NOT NULL REFERENCES hr.regions(region_code),
    cost_center varchar(16) NOT NULL UNIQUE
);

CREATE TABLE hr.employees (
    employee_id bigint PRIMARY KEY,
    department_code varchar(64) NOT NULL REFERENCES hr.departments(department_code),
    region_code varchar(8) NOT NULL REFERENCES hr.regions(region_code),
    manager_id bigint REFERENCES hr.employees(employee_id) DEFERRABLE INITIALLY DEFERRED,
    gender text NOT NULL,
    age smallint NOT NULL CHECK (age BETWEEN 16 AND 100),
    job_title text NOT NULL,
    hire_date date NOT NULL,
    years_at_company smallint NOT NULL CHECK (years_at_company >= 0),
    education_level text NOT NULL,
    work_hours_per_week numeric(5,2) NOT NULL CHECK (work_hours_per_week >= 0),
    projects_handled integer NOT NULL CHECK (projects_handled >= 0),
    remote_work_frequency smallint NOT NULL CHECK (remote_work_frequency BETWEEN 0 AND 100),
    team_size smallint NOT NULL CHECK (team_size >= 0),
    training_hours numeric(7,2) NOT NULL CHECK (training_hours >= 0),
    promotions smallint NOT NULL CHECK (promotions >= 0),
    resigned boolean NOT NULL
);

CREATE TABLE hr.compensation_history (
    employee_id bigint NOT NULL REFERENCES hr.employees(employee_id),
    effective_date date NOT NULL,
    manager_id bigint REFERENCES hr.employees(employee_id),
    department_code varchar(64) NOT NULL REFERENCES hr.departments(department_code),
    region_code varchar(8) NOT NULL REFERENCES hr.regions(region_code),
    monthly_salary numeric(12,2) NOT NULL CHECK (monthly_salary >= 0),
    PRIMARY KEY (employee_id, effective_date)
);

CREATE TABLE hr.performance_reviews (
    employee_id bigint NOT NULL REFERENCES hr.employees(employee_id),
    review_date date NOT NULL,
    manager_id bigint REFERENCES hr.employees(employee_id),
    department_code varchar(64) NOT NULL REFERENCES hr.departments(department_code),
    region_code varchar(8) NOT NULL REFERENCES hr.regions(region_code),
    performance_score numeric(3,2) NOT NULL CHECK (performance_score BETWEEN 1 AND 5),
    satisfaction_score numeric(3,2) NOT NULL CHECK (satisfaction_score BETWEEN 1 AND 5),
    PRIMARY KEY (employee_id, review_date)
);

CREATE TABLE hr.attendance_monthly (
    employee_id bigint NOT NULL REFERENCES hr.employees(employee_id),
    attendance_month date NOT NULL,
    manager_id bigint REFERENCES hr.employees(employee_id),
    department_code varchar(64) NOT NULL REFERENCES hr.departments(department_code),
    region_code varchar(8) NOT NULL REFERENCES hr.regions(region_code),
    overtime_hours numeric(7,2) NOT NULL CHECK (overtime_hours >= 0),
    sick_days numeric(5,2) NOT NULL CHECK (sick_days >= 0),
    overtime_hours_rolling_12m numeric(7,2) NOT NULL CHECK (overtime_hours_rolling_12m >= 0),
    sick_days_rolling_12m numeric(5,2) NOT NULL CHECK (sick_days_rolling_12m >= 0),
    PRIMARY KEY (employee_id, attendance_month)
);

CREATE INDEX employees_scope_idx ON hr.employees (region_code, department_code, manager_id);
CREATE INDEX compensation_scope_date_idx ON hr.compensation_history (region_code, department_code, manager_id, effective_date);
CREATE INDEX performance_scope_date_idx ON hr.performance_reviews (region_code, department_code, manager_id, review_date);
CREATE INDEX attendance_scope_date_idx ON hr.attendance_monthly (region_code, department_code, manager_id, attendance_month);
