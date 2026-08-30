export type Theme = "light" | "dark";

export type ConsoleSection =
  | "overview"
  | "datasources"
  | "schema"
  | "models"
  | "relationships"
  | "views"
  | "cubes"
  | "rules"
  | "sqlKnowledge"
  | "mcp"
  | "access"
  | "instructions"
  | "mdl";

export interface McpServerProfile {
  status: "ready" | "setup_required";
  command: string;
  args: string[];
  toolMode?: string;
  databaseDsnEnv?: string;
  datasourceType?: string | null;
}

export interface McpIntegrationResponse {
  schemaVersion: number;
  transport: "stdio";
  projectPath: string;
  semantic: McpServerProfile;
  governedQuery: McpServerProfile;
  clientConfig: { mcpServers: Record<string, { command: string; args: string[]; env?: Record<string, string> }> };
}

export interface AccessCredential {
  id: string;
  subjectId: string;
  label: string;
  createdAt: string;
  expiresAt?: string | null;
  revokedAt?: string | null;
  lastUsedAt?: string | null;
}

export interface ServiceAccount {
  id: string;
  organizationId: string;
  type: "service_account";
  name: string;
  attributes: Record<string, unknown>;
  status: "active" | "disabled";
  credentials: AccessCredential[];
  policyIds: string[];
}

export interface AccessPolicy {
  id: string;
  organizationId: string;
  name: string;
  version: number;
  document: Record<string, unknown>;
}

export interface AccessAuditEvent {
  id: string;
  occurredAt: string;
  organizationId?: string | null;
  subjectId?: string | null;
  credentialId?: string | null;
  action: string;
  decision: "allowed" | "denied" | "error";
  resource?: string | null;
  policyVersion?: string | null;
  details: Record<string, unknown>;
}

export interface IssuedApiKey {
  apiKey: string;
  credential: AccessCredential;
  replacedCredentialId?: string;
}

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
  [key: string]: unknown;
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

/** A pair of human-facing values used by the semantic model editor. */
export interface LocalizedText {
  "zh-CN": string;
  "en-US": string;
}

/** A column projected from Wren metadata into the business-model editor. */
export interface SemanticColumn {
  name: string;
  type: string;
  primaryKey: boolean;
  notNull: boolean;
  relationship?: string | null;
  calculated: boolean;
  expression: string;
  displayName: LocalizedText;
  description: LocalizedText;
  semanticRole: string;
  format: string;
  visible: boolean;
}

/** A model with both source identity and editable business semantics. */
export interface SemanticModel {
  name: string;
  sourcePath: string;
  tableReference: { schema: string; table: string };
  /** Wren accepts either one column name or an ordered composite key. */
  primaryKey: string | string[];
  displayName: LocalizedText;
  description: LocalizedText;
  businessDomain: string;
  visible: boolean;
  columns: SemanticColumn[];
  draft: boolean;
}

/** A relationship projected from relationships.yml and its locale companion. */
export interface SemanticRelationship {
  name: string;
  models: [string, string];
  joinType: "ONE_TO_ONE" | "ONE_TO_MANY" | "MANY_TO_ONE" | "MANY_TO_MANY" | string;
  condition: string;
  displayName: LocalizedText;
  description: LocalizedText;
  /** Read-only projection derived from simple equality terms in condition. */
  fieldPairs?: SemanticRelationshipFieldPair[];
}

export interface SemanticRelationshipFieldPair {
  sourceModel: string;
  sourceField: string;
  targetModel: string;
  targetField: string;
}

/** The read model used by the visual semantic console. */
export interface SemanticProjectSnapshot {
  revision: string;
  draftCount: number;
  models: SemanticModel[];
  relationships: SemanticRelationship[];
  relationshipErrors?: Array<{ name: string; message: string }>;
  sourceFiles: ProjectFile[];
}

export type SemanticModelUpdate = Omit<SemanticModel, "sourcePath" | "draft"> & {
  expectedRevision?: string;
};

export interface SemanticRelationshipsUpdate {
  relationships: SemanticRelationship[];
  expectedRevision?: string;
}

/** A bounded unified diff for a project file. */
export interface ProjectDiff {
  path: string;
  changed: boolean;
  diff: string;
  revision: string;
}

export interface KnowledgeRuleRecord {
  id: string;
  title: string;
  content: string;
  enabled: boolean;
  sourcePath: string;
  scope?: string[];
  tags?: string[];
  updatedAt?: string;
  draft?: boolean;
  sourceContent?: string;
  diff?: string;
}

export interface KnowledgeRulesResponse {
  schemaVersion: number;
  revision: string;
  rules: KnowledgeRuleRecord[];
  enabledCount: number;
  disabledCount: number;
}

export interface SqlCandidateRecord {
  id: string;
  question: string;
  sql: string;
  dialect?: string;
  queryId?: string;
  sessionId?: string;
  status: "pending" | "approved" | "rejected";
  stats?: Record<string, unknown>;
  sqlHistory?: Array<{ id: string; question: string; sql: string; sourcePath?: string }>;
  createdAt?: string;
  updatedAt?: string;
  reviewedAt?: string;
  reviewer?: string;
  reviewNote?: string;
  approvedPath?: string;
}

export interface SqlCandidatesResponse {
  schemaVersion: number;
  candidates: SqlCandidateRecord[];
  pendingCount: number;
  approvedCount: number;
  rejectedCount: number;
}

export interface CubeRecord {
  name: string;
  sourcePath: string;
  baseObject: string;
  measures: Array<{ name: string; expression: string; type: string }>;
  dimensions: Array<{ name: string; expression: string; type: string }>;
  timeDimensions: Array<{ name: string; expression: string; type: string }>;
  hierarchies: Record<string, string[]>;
  refreshTime?: string;
  properties?: Record<string, unknown>;
  draft?: boolean;
  [key: string]: unknown;
}

export interface CubeSnapshot {
  revision: string;
  draftCount: number;
  cubes: CubeRecord[];
  sourceFiles: ProjectFile[];
  availableBaseObjects?: string[];
}

export interface CubeValidationResponse {
  valid: boolean;
  errors: Array<{ path: string; message: string; severity: "error" }>;
  warnings: Array<{ path: string; message: string; severity: "warning" }>;
}

/** The actual Wren v5 view record exposed by the semantic-console server. */
export interface ViewDefinition {
  name: string;
  sourcePath: string;
  sqlPath?: string | null;
  statement: string;
  statementSource: "metadata" | "sql" | string;
  storage: "metadata" | "sql" | string;
  dialect?: string;
  properties: Record<string, unknown>;
  draft?: boolean;
  [key: string]: unknown;
}

/** A read model returned by GET /api/views. */
export interface ViewSnapshot {
  schemaVersion: number;
  revision: string;
  draftCount: number;
  views: ViewDefinition[];
  sourceFiles: ProjectFile[];
}

export type ViewWritePayload = Pick<ViewDefinition, "name" | "statement" | "storage" | "properties"> & {
  dialect?: string;
  expectedRevision?: string;
};

export interface ViewValidationIssue {
  path?: string;
  line?: number;
  severity?: "error" | "warning" | "info" | string;
  message: string;
  code?: string;
}

export interface ViewValidationResponse {
  valid: boolean;
  ok?: boolean;
  errorCount: number;
  warningCount: number;
  errors: ViewValidationIssue[];
  warnings: ViewValidationIssue[];
  [key: string]: unknown;
}

export interface ViewPreviewColumn {
  name: string;
  type?: string;
  semanticRole?: string;
}

export interface ViewPreviewStats {
  returnedRows: number;
  durationMs: number;
  truncated: boolean;
  [key: string]: unknown;
}

/** A bounded data_query-style SQL preview. The unavailable status is
 * intentional when the configured backend has no safe preview endpoint. */
export interface ViewPreviewResult {
  schemaVersion?: number;
  queryId?: string;
  status: "success" | "PREVIEW_UNAVAILABLE" | "error";
  semanticSql?: string;
  nativeSql?: string;
  columns?: ViewPreviewColumn[];
  previewRows?: Array<Record<string, unknown>>;
  stats?: ViewPreviewStats;
  message?: string;
  [key: string]: unknown;
}
