import type {
  ColumnRecord,
  Datasource,
  DatasourceType,
  ProjectFile,
  ProjectSummary,
  SchemaRecord,
  TableRecord,
  VersionRecord,
} from "../types";

export const fixtureProject: ProjectSummary = {
  name: "Revenue intelligence",
  projectName: "Revenue intelligence",
  path: "./semantic-project",
  revision: "draft-18",
  status: "draft",
  updatedAt: "2026-08-24T08:30:00Z",
  fileCount: 12,
  modelCount: 8,
  relationshipCount: 11,
  viewCount: 4,
  activeDatasource: { id: "warehouse-prod", name: "Warehouse production", type: "postgres" },
};

export const fixtureDatasourceTypes: DatasourceType[] = [
  {
    type: "postgres",
    label: "PostgreSQL",
    available: true,
    supportsSchemaBrowse: true,
    supportsTest: true,
    fields: [
      { name: "host", label: "Host", inputType: "text", placeholder: "analytics-db.internal", required: true },
      { name: "port", label: "Port", inputType: "number", placeholder: "5432", required: true },
      { name: "database", label: "Database", inputType: "text", placeholder: "warehouse", required: true },
      { name: "user", label: "User", inputType: "text", placeholder: "semantic_reader", required: true },
      { name: "password", label: "Password", inputType: "password", placeholder: "Enter password", sensitive: true },
      { name: "ssl_mode", label: "SSL mode", inputType: "select", examples: ["require", "verify-full", "disable"] },
    ],
  },
  {
    type: "mysql",
    label: "MySQL",
    available: true,
    supportsSchemaBrowse: true,
    supportsTest: true,
    fields: [
      { name: "host", label: "Host", inputType: "text", placeholder: "mysql.internal", required: true },
      { name: "port", label: "Port", inputType: "number", placeholder: "3306", required: true },
      { name: "database", label: "Database", inputType: "text", placeholder: "warehouse", required: true },
      { name: "user", label: "User", inputType: "text", placeholder: "semantic_reader", required: true },
      { name: "password", label: "Password", inputType: "password", placeholder: "Enter password", sensitive: true },
      { name: "ssl_mode", label: "SSL mode", inputType: "select", examples: ["required", "preferred", "disabled"] },
    ],
  },
];

export const fixtureDatasources: Datasource[] = [
  {
    id: "warehouse-prod",
    name: "Warehouse production",
    type: "postgres",
    connection: { host: "analytics-db.internal", port: 5432, database: "warehouse", user: "semantic_reader", ssl_mode: "require" },
    hasPassword: true,
    updatedAt: "2026-08-24T08:20:00Z",
    lastTest: { ok: true, latencyMs: 84, driver: "psycopg" },
  },
  {
    id: "billing-replica",
    name: "Billing replica",
    type: "mysql",
    connection: { host: "billing-replica.internal", port: 3306, database: "billing", user: "semantic_reader", ssl_mode: "required" },
    hasPassword: true,
    updatedAt: "2026-08-23T15:10:00Z",
    lastTest: { ok: true, latencyMs: 116, driver: "mysql" },
  },
];

export const fixtureSchemas: SchemaRecord[] = [
  { name: "analytics", tableCount: 18 },
  { name: "billing", tableCount: 9 },
  { name: "public", tableCount: 24 },
];

export const fixtureTables: TableRecord[] = [
  { name: "orders", type: "BASE TABLE" },
  { name: "customers", type: "BASE TABLE" },
  { name: "order_items", type: "BASE TABLE" },
  { name: "monthly_revenue", type: "VIEW" },
  { name: "subscription_events", type: "BASE TABLE" },
];

export const fixtureColumns: ColumnRecord[] = [
  { name: "id", type: "UUID", dataType: "uuid", nullable: false, ordinal: 1, primaryKey: true },
  { name: "customer_id", type: "UUID", dataType: "uuid", nullable: false, ordinal: 2, primaryKey: false },
  { name: "status", type: "VARCHAR", dataType: "character varying", nullable: false, ordinal: 3, primaryKey: false },
  { name: "total_amount", type: "NUMERIC", dataType: "numeric", nullable: false, ordinal: 4, primaryKey: false },
  { name: "ordered_at", type: "TIMESTAMPTZ", dataType: "timestamp with time zone", nullable: false, ordinal: 5, primaryKey: false },
];

export const fixtureFiles: ProjectFile[] = [
  { path: "models/orders.model.yml", size: 1230, kind: "model" },
  { path: "models/customers.model.yml", size: 980, kind: "model" },
  { path: "relationships/orders_to_customers.relationship.yml", size: 456, kind: "relationship" },
  { path: "views/revenue.view.yml", size: 708, kind: "view" },
  { path: "instructions.yml", size: 684, kind: "instruction" },
  { path: "manifest.yml", size: 512, kind: "project" },
];

export const fixtureVersions: VersionRecord[] = [
  { id: "v18", revision: "draft-18", createdAt: "2026-08-24T08:30:00Z", fileCount: 12, label: "Current draft" },
  { id: "v17", revision: "release-17", createdAt: "2026-08-22T11:12:00Z", fileCount: 12, label: "Revenue model refresh" },
  { id: "v16", revision: "release-16", createdAt: "2026-08-16T16:45:00Z", fileCount: 11, label: "Billing source added" },
];
