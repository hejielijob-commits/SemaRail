export type Theme = "light" | "dark";

export type ConsoleSection =
  | "overview"
  | "datasources"
  | "schema"
  | "models"
  | "relationships"
  | "views"
  | "instructions"
  | "mdl";

export interface HealthResponse {
  status: string;
  ok?: boolean;
  service?: string;
  version?: string;
  [key: string]: unknown;
}

export interface ProjectSummary {
  name?: string;
  projectName?: string;
  path?: string;
  status?: string;
  revision?: string;
  updatedAt?: string;
  fileCount?: number;
  modelCount?: number;
  relationshipCount?: number;
  viewCount?: number;
  schemaVersion?: number | null;
  dataSource?: string | null;
  draftCount?: number;
  projectExists?: boolean;
  activeDatasource?: Datasource | null;
  wren?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ActivateDatasourceResponse {
  activeDatasource?: Datasource | null;
  project?: ProjectSummary;
  [key: string]: unknown;
}

export interface DatasourceField {
  name: string;
  label?: string;
  inputType?: string;
  placeholder?: string;
  hint?: string;
  required?: boolean;
  sensitive?: boolean;
  alias?: string;
  examples?: string[];
  accept?: string;
}

export interface DatasourceType {
  type: string;
  label: string;
  available?: boolean;
  module?: string | null;
  supportsSchemaBrowse?: boolean;
  supportsTest?: boolean;
  note?: string;
  fields?: DatasourceField[];
}

export interface ConnectionValues {
  [key: string]: string | number | boolean | null | undefined;
}

export interface ConnectionTest {
  ok: boolean;
  latencyMs?: number;
  driver?: string;
  message?: string;
  [key: string]: unknown;
}

export interface Datasource {
  id: string;
  name: string;
  type: string;
  connection?: ConnectionValues;
  hasPassword?: boolean;
  createdAt?: string;
  updatedAt?: string;
  lastTest?: ConnectionTest | null;
}

export interface SchemaRecord {
  name: string;
  tableCount?: number;
  [key: string]: unknown;
}

export interface TableRecord {
  name: string;
  type?: string;
  schema?: string;
  [key: string]: unknown;
}

export interface ColumnRecord {
  name: string;
  type?: string;
  dataType?: string;
  nullable?: boolean;
  ordinal?: number;
  primaryKey?: boolean;
  [key: string]: unknown;
}

export interface ProjectFile {
  path: string;
  content?: string;
  size?: number;
  sha256?: string;
  draft?: boolean;
  revision?: string;
  /** Optional client classification used by fixtures and file index views. */
  kind?: string;
}

export interface ValidationIssue {
  path?: string;
  line?: number;
  severity?: "error" | "warning" | "info" | string;
  message: string;
  code?: string;
}

export interface ValidationResponse {
  valid: boolean;
  ok?: boolean;
  errorCount?: number;
  warningCount?: number;
  revision?: string;
  draft?: boolean;
  issues?: ValidationIssue[];
  errors?: ValidationIssue[];
  warnings?: ValidationIssue[];
  [key: string]: unknown;
}

export interface VersionRecord {
  id: string;
  revision: string;
  createdAt: string;
  fileCount?: number;
  label?: string;
}

export interface PublishResponse {
  version?: VersionRecord;
  project?: ProjectSummary;
  ok?: boolean;
  revision?: string;
  publishedAt?: string;
  message?: string;
  [key: string]: unknown;
}

export interface ApiErrorPayload {
  code?: string;
  message?: string;
  details?: Record<string, unknown>;
}

export interface ProjectFilePayload {
  path: string;
  content?: string;
  delete?: boolean;
  expectedRevision?: string;
}

export interface ProjectFileImport {
  path: string;
  content: string;
}

export interface ImportProjectResponse {
  files: ProjectFile[];
  revision: string;
  draft: boolean;
}

export interface GenerateModelResponse {
  model?: Record<string, unknown>;
  file: string;
  draft: boolean;
  revision: string;
}
