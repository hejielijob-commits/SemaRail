import type {
  ApiErrorPayload,
  ActivateDatasourceResponse,
  ColumnRecord,
  Datasource,
  DatasourceType,
  HealthResponse,
  ProjectFile,
  ProjectFilePayload,
  ProjectSummary,
  ImportProjectResponse,
  ProjectFileImport,
  GenerateModelResponse,
  PublishResponse,
  SchemaRecord,
  TableRecord,
  ValidationResponse,
  VersionRecord,
  ConnectionTest,
  ProjectDiff,
  SemanticModelUpdate,
  SemanticProjectSnapshot,
  SemanticRelationshipsUpdate,
  KnowledgeRulesResponse,
  KnowledgeRuleRecord,
  SqlCandidatesResponse,
  SqlCandidateRecord,
  CubeSnapshot,
  CubeRecord,
  CubeValidationResponse,
  ViewSnapshot,
  ViewWritePayload,
  ViewValidationResponse,
  ViewPreviewResult,
  McpIntegrationResponse,
  AccessCredential,
  ServiceAccount,
  UserAccount,
  AccessPolicy,
  AccessAuditEvent,
  IssuedApiKey,
  DiagnosticFeedback,
  FeedbackCategory,
  FeedbackStatus,
  FeedbackResolution,
  BusinessEvidenceV1,
  ExternalAgentEvaluationSummary,
  RegressionCaseExport,
  RegressionCaseRecord,
  AgentTraceDetail,
  AgentTraceSummary,
  TraceCollectorHealthResponse,
  TraceComparisonResponse,
  TraceDiagnosis,
  TestDatasetRecord,
  CoreDiagnosticRecord,
  RegressionCaseV2,
  RegressionCaseV2Record,
  RegressionCasesExportV2,
  RegressionCasesResponse,
  RegressionRun,
  RegressionRunsResponse,
  TestDatasetsResponse,
} from "../types";

export class ApiClientError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly details?: Record<string, unknown>;

  constructor(message: string, status: number, payload?: ApiErrorPayload) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = payload?.code;
    this.details = payload?.details;
  }
}

const BUSINESS_EVIDENCE_FIELDS = [
  "schemaVersion", "rulesVersion", "projectRevision", "queryId", "datasourceId", "outcome",
  "createdAt", "expiresAt", "references", "models", "columns", "metrics", "grain", "unit",
  "time", "defaultsApplied", "conditions", "confirmations", "checks",
] as const;
const BUSINESS_EVIDENCE_ID = /^[A-Za-z_][A-Za-z0-9_.:-]{0,255}$/;
const BUSINESS_EVIDENCE_SECRET = /bearer\s|sr_(?:live|session|key)_|(?:password|token|secret|dsn)\s*[:=]|[a-z]+:\/\/|[A-Z]:[\\/]/i;

function invalidBusinessEvidence(message: string, code = "INVALID_BUSINESS_EVIDENCE"): never {
  throw new ApiClientError(message, 502, { code, message });
}

function evidenceRecord(value: unknown, path: string, allowed?: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidBusinessEvidence(`${path} must be an object.`);
  const record = value as Record<string, unknown>;
  if (allowed && Object.keys(record).some((key) => !allowed.includes(key))) invalidBusinessEvidence(`${path} contains an unsupported field.`);
  return record;
}

function evidenceText(value: unknown, path: string, identifier = false): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 256 || (identifier && !BUSINESS_EVIDENCE_ID.test(value))) {
    invalidBusinessEvidence(`${path} is invalid.`);
  }
  return value;
}

function evidenceJson(value: unknown, depth = 0): unknown {
  if (depth > 8) invalidBusinessEvidence("Query evidence is too deeply nested.");
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "string") {
    if (value.length > 2000 || BUSINESS_EVIDENCE_SECRET.test(value) || /[\u0000-\u001f]/.test(value)) invalidBusinessEvidence("Query evidence contains invalid text.");
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) invalidBusinessEvidence("Query evidence contains a non-finite number.");
    return value;
  }
  if (Array.isArray(value)) {
    if (value.length > 256) invalidBusinessEvidence("Query evidence contains an oversized array.");
    return value.map((item) => evidenceJson(item, depth + 1));
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (Object.keys(record).length > 256) invalidBusinessEvidence("Query evidence contains an oversized object.");
    return Object.fromEntries(Object.entries(record).map(([key, item]) => {
      if (key.length > 256 || BUSINESS_EVIDENCE_SECRET.test(key) || /[\u0000-\u001f]/.test(key)) invalidBusinessEvidence("Query evidence contains an invalid field name.");
      return [key, evidenceJson(item, depth + 1)];
    }));
  }
  invalidBusinessEvidence("Query evidence contains a non-JSON value.");
}

/** Parse safe query evidence at the REST boundary; unknown versions and fields fail closed. */
function parseBusinessEvidenceV1(value: unknown): BusinessEvidenceV1 {
  const raw = evidenceRecord(value, "businessEvidence", BUSINESS_EVIDENCE_FIELDS);
  if (raw.schemaVersion !== 1) invalidBusinessEvidence("Query evidence uses an unsupported schema version.", "UNSUPPORTED_BUSINESS_EVIDENCE_VERSION");
  if (Object.keys(raw).length !== BUSINESS_EVIDENCE_FIELDS.length || BUSINESS_EVIDENCE_FIELDS.some((key) => !Object.prototype.hasOwnProperty.call(raw, key))) {
    invalidBusinessEvidence("Query evidence is missing required fields.");
  }
  evidenceJson(raw);
  for (const key of ["rulesVersion", "projectRevision", "queryId", "datasourceId"] as const) evidenceText(raw[key], `businessEvidence.${key}`, true);
  for (const key of ["createdAt", "expiresAt"] as const) evidenceText(raw[key], `businessEvidence.${key}`);
  if (!["success", "failure", "blocked"].includes(String(raw.outcome))) invalidBusinessEvidence("Query evidence outcome is invalid.");

  for (const field of ["models", "columns", "metrics", "grain"] as const) {
    if (!Array.isArray(raw[field]) || raw[field].length > 256) invalidBusinessEvidence(`businessEvidence.${field} is invalid.`);
  }
  if (!Array.isArray(raw.references) || raw.references.length > 256) invalidBusinessEvidence("businessEvidence.references is invalid.");
  const references = raw.references.map((item, index) => {
    const reference = evidenceRecord(item, `businessEvidence.references[${index}]`, ["ruleId", "model", "column"]);
    if (Object.keys(reference).length !== 3) invalidBusinessEvidence("Query evidence reference is incomplete.");
    return {
      ruleId: evidenceText(reference.ruleId, "businessEvidence.references.ruleId", true),
      model: evidenceText(reference.model, "businessEvidence.references.model", true),
      column: evidenceText(reference.column, "businessEvidence.references.column", true),
    };
  });
  const unit = evidenceRecord(raw.unit, "businessEvidence.unit", ["status", "value"]);
  if (unit.status !== "verified" && unit.status !== "unverified") invalidBusinessEvidence("Query evidence unit status is invalid.");
  if (Object.keys(unit).some((key) => key !== "status" && key !== "value")) invalidBusinessEvidence("Query evidence unit fields are invalid.");
  if (Object.prototype.hasOwnProperty.call(unit, "value") && unit.value !== null) evidenceText(unit.value, "businessEvidence.unit.value");
  const time = evidenceRecord(raw.time, "businessEvidence.time", ["source", "field", "value", "groupedByOriginal"]);
  if (!["confirmed", "publishedDefault", "sql", "unknown"].includes(String(time.source))) invalidBusinessEvidence("Query evidence time source is invalid.");
  if (Object.prototype.hasOwnProperty.call(time, "field") && time.field !== null) evidenceText(time.field, "businessEvidence.time.field");
  if (Object.prototype.hasOwnProperty.call(time, "value")) evidenceJson(time.value);
  if (Object.prototype.hasOwnProperty.call(time, "groupedByOriginal") && time.groupedByOriginal !== null && typeof time.groupedByOriginal !== "boolean") invalidBusinessEvidence("Query evidence time grouping is invalid.");
  if (Object.keys(time).some((key) => !["source", "field", "value", "groupedByOriginal"].includes(key))) invalidBusinessEvidence("Query evidence time fields are invalid.");

  for (const key of ["defaultsApplied", "conditions"] as const) evidenceRecord(raw[key], `businessEvidence.${key}`);
  const confirmations = evidenceRecord(raw.confirmations, "businessEvidence.confirmations");
  if (Object.keys(confirmations).length > 256 || Object.values(confirmations).some((item) => !["confirmed", "defaulted", "missing"].includes(String(item)))) invalidBusinessEvidence("Query evidence confirmations are invalid.");
  if (!Array.isArray(raw.checks) || raw.checks.length > 256) invalidBusinessEvidence("Query evidence checks are invalid.");
  const checks = raw.checks.map((item, index) => {
    const check = evidenceRecord(item, `businessEvidence.checks[${index}]`, ["id", "ruleId", "mandatory", "status", "reasonCode"]);
    if (Object.keys(check).length !== 5 || typeof check.mandatory !== "boolean" || !["passed", "conflict", "unverified", "stale"].includes(String(check.status))) invalidBusinessEvidence("Query evidence check fields are invalid.");
    return {
      id: evidenceText(check.id, "businessEvidence.checks.id", true),
      ruleId: evidenceText(check.ruleId, "businessEvidence.checks.ruleId", true),
      mandatory: check.mandatory,
      status: check.status as BusinessEvidenceV1["checks"][number]["status"],
      reasonCode: evidenceText(check.reasonCode, "businessEvidence.checks.reasonCode", true),
    };
  });

  let encoded: string;
  try { encoded = JSON.stringify(raw); } catch { invalidBusinessEvidence("Query evidence is invalid."); }
  if (new TextEncoder().encode(encoded!).length > 128 * 1024) invalidBusinessEvidence("Query evidence exceeds the size limit.");
  return {
    schemaVersion: 1,
    rulesVersion: raw.rulesVersion as string,
    projectRevision: raw.projectRevision as string,
    queryId: raw.queryId as string,
    datasourceId: raw.datasourceId as string,
    outcome: raw.outcome as BusinessEvidenceV1["outcome"],
    createdAt: raw.createdAt as string,
    expiresAt: raw.expiresAt as string,
    references,
    models: raw.models as BusinessEvidenceV1["models"],
    columns: raw.columns as BusinessEvidenceV1["columns"],
    metrics: raw.metrics as BusinessEvidenceV1["metrics"],
    grain: raw.grain as BusinessEvidenceV1["grain"],
    unit: unit as BusinessEvidenceV1["unit"],
    time: time as BusinessEvidenceV1["time"],
    defaultsApplied: raw.defaultsApplied as BusinessEvidenceV1["defaultsApplied"],
    conditions: raw.conditions as BusinessEvidenceV1["conditions"],
    confirmations: confirmations as BusinessEvidenceV1["confirmations"],
    checks,
  };
}

/** Typed REST boundary for the semantic console. Secrets are accepted only in request bodies. */
export class ApiClient {
  private readonly baseUrl: string;
  private bearerToken = "";

  constructor(baseUrl = "") {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  /** Keep the Console administrator credential in memory only. */
  setBearerToken(token: string): void {
    this.bearerToken = token.trim();
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(this.bearerToken ? { Authorization: `Bearer ${this.bearerToken}` } : {}),
        ...init.headers,
      },
    });

    const body = await response.json().catch(() => undefined);
    if (!response.ok) {
      const payload = body as ApiErrorPayload | undefined;
      throw new ApiClientError(
        payload?.message || `Request failed with status ${response.status}`,
        response.status,
        payload,
      );
    }
    return body as T;
  }

  health(): Promise<HealthResponse> {
    return this.request<HealthResponse>("/api/health");
  }

  getProject(): Promise<ProjectSummary> {
    return this.request<ProjectSummary>("/api/project");
  }

  /** Load the secret-free authenticated remote MCP configuration and readiness. */
  getMcpIntegration(): Promise<McpIntegrationResponse> {
    return this.request<McpIntegrationResponse>("/api/mcp-integration");
  }

  private adminHeaders(token: string): HeadersInit {
    return { Authorization: `Bearer ${token}` };
  }

  getCapabilities(token: string): Promise<{ subject: { id: string; name: string }; projectId: string; capabilities: { "console:admin": boolean; "access:admin": boolean } }> {
    return this.request("/api/v1/auth/capabilities", { headers: this.adminHeaders(token) });
  }

  getServiceAccounts(token: string): Promise<{ items: ServiceAccount[] }> {
    return this.request("/api/v1/access/service-accounts", { headers: this.adminHeaders(token) });
  }

  getUsers(token: string): Promise<{ items: UserAccount[] }> {
    return this.request("/api/v1/access/users", { headers: this.adminHeaders(token) });
  }

  updateUser(token: string, id: string, payload: { name?: string; attributes?: Record<string, unknown> }): Promise<UserAccount> {
    return this.request(`/api/v1/access/users/${encodeURIComponent(id)}`, {
      method: "PUT", headers: this.adminHeaders(token), body: JSON.stringify(payload),
    });
  }

  setUserStatus(token: string, id: string, status: "active" | "disabled"): Promise<UserAccount> {
    return this.request(`/api/v1/access/users/${encodeURIComponent(id)}/status`, {
      method: "PUT", headers: this.adminHeaders(token), body: JSON.stringify({ status }),
    });
  }

  createServiceAccount(token: string, payload: { name: string; attributes?: Record<string, unknown> }): Promise<ServiceAccount> {
    return this.request("/api/v1/access/service-accounts", {
      method: "POST", headers: this.adminHeaders(token), body: JSON.stringify(payload),
    });
  }

  updateServiceAccount(token: string, id: string, payload: { name?: string; attributes?: Record<string, unknown> }): Promise<ServiceAccount> {
    return this.request(`/api/v1/access/service-accounts/${encodeURIComponent(id)}`, {
      method: "PUT", headers: this.adminHeaders(token), body: JSON.stringify(payload),
    });
  }

  setServiceAccountStatus(token: string, id: string, status: "active" | "disabled"): Promise<ServiceAccount> {
    return this.request(`/api/v1/access/service-accounts/${encodeURIComponent(id)}/status`, {
      method: "PUT", headers: this.adminHeaders(token), body: JSON.stringify({ status }),
    });
  }

  issueServiceAccountKey(token: string, id: string, label: string): Promise<IssuedApiKey> {
    return this.request(`/api/v1/access/service-accounts/${encodeURIComponent(id)}/keys`, {
      method: "POST", headers: this.adminHeaders(token), body: JSON.stringify({ label }),
    });
  }

  rotateCredential(token: string, id: string): Promise<IssuedApiKey> {
    return this.request(`/api/v1/access/credentials/${encodeURIComponent(id)}/rotate`, {
      method: "POST", headers: this.adminHeaders(token), body: "{}",
    });
  }

  revokeCredential(token: string, id: string): Promise<AccessCredential> {
    return this.request(`/api/v1/access/credentials/${encodeURIComponent(id)}/revoke`, {
      method: "POST", headers: this.adminHeaders(token), body: "{}",
    });
  }

  getAccessPolicies(token: string): Promise<{ items: AccessPolicy[] }> {
    return this.request("/api/v1/access/policies", { headers: this.adminHeaders(token) });
  }

  createAccessPolicy(token: string, payload: { name: string; document: Record<string, unknown> }): Promise<AccessPolicy> {
    return this.request("/api/v1/access/policies", {
      method: "POST", headers: this.adminHeaders(token), body: JSON.stringify(payload),
    });
  }

  updateAccessPolicy(token: string, id: string, document: Record<string, unknown>): Promise<AccessPolicy> {
    return this.request(`/api/v1/access/policies/${encodeURIComponent(id)}`, {
      method: "PUT", headers: this.adminHeaders(token), body: JSON.stringify({ document }),
    });
  }

  bindAccessPolicy(token: string, subjectId: string, policyId: string): Promise<{ subjectId: string; policyId: string }> {
    return this.request("/api/v1/access/policy-bindings", {
      method: "POST", headers: this.adminHeaders(token), body: JSON.stringify({ subjectId, policyId }),
    });
  }

  unbindAccessPolicy(token: string, subjectId: string, policyId: string): Promise<{ subjectId: string; policyId: string; status: "unbound" }> {
    return this.request(`/api/v1/access/policy-bindings/${encodeURIComponent(subjectId)}/${encodeURIComponent(policyId)}`, {
      method: "DELETE", headers: this.adminHeaders(token),
    });
  }

  getAccessAudit(token: string): Promise<{ items: AccessAuditEvent[] }> {
    return this.request("/api/v1/access/audit?limit=500", { headers: this.adminHeaders(token) });
  }

  /** Submit explicit feedback for a query owned by the authenticated caller. */
  submitFeedback(payload: {
    reference: string;
    idempotencyKey: string;
    category: FeedbackCategory;
    description: string;
    expectedBehavior?: string;
    question?: string;
    semanticSql?: string;
    nativeSql?: string;
  }): Promise<{ feedbackId: string; diagnosticId: string; status: FeedbackStatus; duplicate: boolean }> {
    return this.request("/api/v1/feedback", { method: "POST", body: JSON.stringify(payload) });
  }

  listDiagnosticFeedback(filters: {
    limit?: number;
    cursor?: string;
    category?: FeedbackCategory | "";
    status?: FeedbackStatus | "";
    source?: string;
    datasourceId?: string;
    reasonCode?: string;
    createdAfter?: string;
    createdBefore?: string;
  } = {}): Promise<{ items: DiagnosticFeedback[]; nextCursor?: string | null }> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    const suffix = query.size ? `?${query.toString()}` : "";
    return this.request(`/api/v1/diagnostics/feedback${suffix}`);
  }

  getDiagnosticFeedback(id: string): Promise<DiagnosticFeedback> {
    return this.request(`/api/v1/diagnostics/feedback/${encodeURIComponent(id)}`);
  }

  getFeedbackResolution(id: string): Promise<FeedbackResolution> {
    return this.request(`/api/v1/diagnostics/feedback/${encodeURIComponent(id)}/resolution`);
  }

  saveFeedbackResolution(id: string, payload: {
    expectedRevision: number;
    assetPath: string | null;
    baseVersionId: string | null;
    publishedVersionId: string | null;
    caseId: string | null;
    caseRevision: number | null;
    runId: string | null;
  }): Promise<FeedbackResolution> {
    return this.request(`/api/v1/diagnostics/feedback/${encodeURIComponent(id)}/resolution`, {
      method: "PUT", body: JSON.stringify(payload),
    });
  }

  verifyFeedbackResolution(id: string): Promise<FeedbackResolution> {
    return this.request(`/api/v1/diagnostics/feedback/${encodeURIComponent(id)}/resolution/verify`, {
      method: "POST", body: "{}",
    });
  }

  importAgentEvaluation(id: string, payload: {
    schemaVersion: 1;
    campaignId: string;
    reportSha256: string;
    source: string;
    model: string;
    reasoning: string;
    datasetVersion: string;
    semanticVersion: string;
    verdictAggregates: { passed: number; failed: number; inconclusive: number; total: number };
  }): Promise<ExternalAgentEvaluationSummary> {
    return this.request(`/api/v1/diagnostics/feedback/${encodeURIComponent(id)}/agent-evaluations`, {
      method: "POST", body: JSON.stringify(payload),
    });
  }

  reviewAgentEvaluation(id: string, evaluationId: string, payload: {
    expectedRevision: number;
    decision: "approved" | "rejected";
  }): Promise<ExternalAgentEvaluationSummary> {
    return this.request(`/api/v1/diagnostics/feedback/${encodeURIComponent(id)}/agent-evaluations/${encodeURIComponent(evaluationId)}/review`, {
      method: "POST", body: JSON.stringify(payload),
    });
  }

  async getQueryEvidence(queryId: string): Promise<BusinessEvidenceV1> {
    const value = await this.request<unknown>(`/api/v1/diagnostics/queries/${encodeURIComponent(queryId)}/evidence`);
    const evidence = parseBusinessEvidenceV1(value);
    if (evidence.queryId !== queryId) invalidBusinessEvidence("Query evidence does not match the requested query.");
    return evidence;
  }

  updateDiagnosticFeedback(id: string, payload: {
    status: FeedbackStatus;
    category?: FeedbackCategory;
    duplicateOf?: string;
    note?: string;
  }): Promise<DiagnosticFeedback> {
    return this.request(`/api/v1/diagnostics/feedback/${encodeURIComponent(id)}`, {
      method: "PUT", body: JSON.stringify(payload),
    });
  }

  createRegressionCase(feedbackId: string, payload: {
    case: Omit<RegressionCaseRecord, "id" | "feedbackId" | "status" | "schemaVersion">;
    enable: boolean;
  }): Promise<{ id: string; status: "draft" | "enabled"; case: Omit<RegressionCaseRecord, "id" | "feedbackId" | "status"> }> {
    return this.request(`/api/v1/diagnostics/feedback/${encodeURIComponent(feedbackId)}/regression-cases`, {
      method: "POST", body: JSON.stringify(payload),
    });
  }

  exportRegressionCases(): Promise<RegressionCaseExport> {
    return this.request("/api/v1/diagnostics/regression-cases/export");
  }

  listTestDatasets(): Promise<TestDatasetsResponse> {
    return this.request("/api/v1/diagnostics/test-datasets");
  }

  createTestDataset(payload: { id: string; version: string; datasourceId: string }): Promise<TestDatasetRecord> {
    return this.request("/api/v1/diagnostics/test-datasets", { method: "POST", body: JSON.stringify(payload) });
  }

  updateTestDataset(id: string, payload: { version: string; datasourceId: string }): Promise<TestDatasetRecord> {
    return this.request(`/api/v1/diagnostics/test-datasets/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });
  }

  deleteTestDataset(id: string): Promise<{ deleted: true }> {
    return this.request(`/api/v1/diagnostics/test-datasets/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  listRegressionCases(): Promise<RegressionCasesResponse> {
    return this.request("/api/v1/diagnostics/regression-cases");
  }

  exportRegressionCasesV2(): Promise<RegressionCasesExportV2> {
    return this.request("/api/v1/diagnostics/regression-cases/export?schemaVersion=2");
  }

  createRegressionCaseV2(payload: { feedbackId: string; sourceTraceId?: string; case: RegressionCaseV2; enable: false }): Promise<RegressionCaseV2Record> {
    return this.request("/api/v1/diagnostics/regression-cases", { method: "POST", body: JSON.stringify(payload) });
  }

  updateRegressionCase(id: string, payload: { expectedRevision: number; case: RegressionCaseV2; enable: boolean }): Promise<RegressionCaseV2Record> {
    return this.request(`/api/v1/diagnostics/regression-cases/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });
  }

  deleteRegressionCase(id: string): Promise<{ deleted: true }> {
    return this.request(`/api/v1/diagnostics/regression-cases/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  listRegressionRuns(): Promise<RegressionRunsResponse> {
    return this.request("/api/v1/diagnostics/regression-runs");
  }

  createRegressionRun(caseIds: string[]): Promise<RegressionRun> {
    return this.request("/api/v1/diagnostics/regression-runs", { method: "POST", body: JSON.stringify({ caseIds }) });
  }

  getRegressionRun(id: string): Promise<RegressionRun> {
    return this.request(`/api/v1/diagnostics/regression-runs/${encodeURIComponent(id)}`);
  }

  cancelRegressionRun(id: string): Promise<RegressionRun> {
    return this.request(`/api/v1/diagnostics/regression-runs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: "{}" });
  }

  getCoreDiagnostic(id: string): Promise<CoreDiagnosticRecord> {
    return this.request(`/api/v1/diagnostics/core/${encodeURIComponent(id)}`);
  }

  /** List durable, project-scoped agent trace summaries. */
  listTraces(filters: {
    limit?: number;
    cursor?: string;
    createdAfter?: string;
    createdBefore?: string;
    sourceSessionId?: string;
    subjectId?: string;
    status?: AgentTraceSummary["status"] | "";
    hasIssues?: boolean;
  } = {}): Promise<{ items: AgentTraceSummary[]; nextCursor?: string | null }> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    const suffix = query.size ? `?${query.toString()}` : "";
    return this.request(`/api/v1/traces${suffix}`);
  }

  /** Read one durable trace with events in server-provided time order. */
  getTrace(id: string): Promise<AgentTraceDetail> {
    return this.request(`/api/v1/traces/${encodeURIComponent(id)}`);
  }

  getTraceByCore(coreTraceId: string): Promise<AgentTraceDetail> {
    return this.request(`/api/v1/traces/by-core/${encodeURIComponent(coreTraceId)}`);
  }

  /** Read findings derived from the selected trace's persisted events and current Core evidence. */
  getTraceDiagnosis(id: string): Promise<TraceDiagnosis> {
    return this.request(`/api/v1/traces/${encodeURIComponent(id)}/diagnosis`);
  }

  /** Read collector counters with their observation timestamps and stale marker. */
  getTraceCollectorStatus(): Promise<TraceCollectorHealthResponse> {
    return this.request("/api/v1/traces/collector-status");
  }

  /** Compare two manually selected traces within the current project. */
  compareTraces(leftTraceId: string, rightTraceId: string): Promise<TraceComparisonResponse> {
    const query = new URLSearchParams({ leftTraceId, rightTraceId });
    return this.request(`/api/v1/traces/compare?${query.toString()}`);
  }

  /** Load the structured business-model projection used by the visual editor. */
  getSemanticProject(): Promise<SemanticProjectSnapshot> {
    return this.request<SemanticProjectSnapshot>("/api/semantic-project");
  }

  /** Save one business model and its bilingual metadata as a project draft. */
  updateSemanticModel(name: string, payload: SemanticModelUpdate): Promise<SemanticProjectSnapshot> {
    return this.request<SemanticProjectSnapshot>(`/api/semantic-models/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  }

  /** Save all relationship definitions as one atomic project draft. */
  updateSemanticRelationships(payload: SemanticRelationshipsUpdate): Promise<SemanticProjectSnapshot> {
    return this.request<SemanticProjectSnapshot>("/api/semantic-relationships", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  }

  getRules(): Promise<KnowledgeRulesResponse> {
    return this.request<KnowledgeRulesResponse>("/api/knowledge/rules");
  }

  createRule(payload: Partial<KnowledgeRuleRecord> & { content: string; expectedRevision?: string }): Promise<{ rule: KnowledgeRuleRecord; rules: KnowledgeRuleRecord[]; revision: string }> {
    return this.request("/api/knowledge/rules", { method: "POST", body: JSON.stringify(payload) });
  }

  updateRule(id: string, payload: Partial<KnowledgeRuleRecord> & { expectedRevision?: string }): Promise<{ rule: KnowledgeRuleRecord; rules: KnowledgeRuleRecord[]; revision: string }> {
    return this.request(`/api/knowledge/rules/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });
  }

  setRuleEnabled(id: string, enabled: boolean, expectedRevision?: string): Promise<{ rule: KnowledgeRuleRecord; rules: KnowledgeRuleRecord[]; revision: string }> {
    return this.request(`/api/knowledge/rules/${encodeURIComponent(id)}/${enabled ? "enable" : "disable"}`, {
      method: "POST", body: JSON.stringify(expectedRevision ? { expectedRevision } : {}),
    });
  }

  deleteRule(id: string, expectedRevision?: string): Promise<{ deleted: boolean; id: string; rules: KnowledgeRuleRecord[]; revision: string }> {
    return this.request(`/api/knowledge/rules/${encodeURIComponent(id)}`, {
      method: "DELETE", body: JSON.stringify(expectedRevision !== undefined ? { expectedRevision } : {}),
    });
  }

  getSqlCandidates(status?: string): Promise<SqlCandidatesResponse> {
    return this.request(`/api/knowledge/sql-candidates${status ? `?status=${encodeURIComponent(status)}` : ""}`);
  }

  updateSqlCandidate(id: string, payload: { sql: string }): Promise<{ candidate: SqlCandidateRecord }> {
    return this.request(`/api/knowledge/sql-candidates/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });
  }

  approveSqlCandidate(id: string, payload: { sql?: string; reviewer?: string; reviewNote?: string } = {}): Promise<{ candidate: SqlCandidateRecord; approved: boolean; path?: string }> {
    return this.request(`/api/knowledge/sql-candidates/${encodeURIComponent(id)}/approve`, { method: "POST", body: JSON.stringify(payload) });
  }

  validateSqlCandidate(id: string, sql?: string): Promise<{ valid: boolean; status: "passed"; message?: string }> {
    return this.request(`/api/knowledge/sql-candidates/${encodeURIComponent(id)}/validate`, {
      method: "POST", body: JSON.stringify(sql === undefined ? {} : { sql }),
    });
  }

  rejectSqlCandidate(id: string, reviewNote: string): Promise<{ candidate: SqlCandidateRecord; rejected: boolean }> {
    return this.request(`/api/knowledge/sql-candidates/${encodeURIComponent(id)}/reject`, { method: "POST", body: JSON.stringify({ reviewNote }) });
  }

  resubmitSqlCandidate(id: string): Promise<{ candidate: SqlCandidateRecord; resubmitted: boolean }> {
    return this.request(`/api/knowledge/sql-candidates/${encodeURIComponent(id)}/resubmit`, { method: "POST", body: "{}" });
  }

  getCubes(): Promise<CubeSnapshot> {
    return this.request<CubeSnapshot>("/api/cubes");
  }

  createCube(payload: CubeRecord & { expectedRevision?: string }): Promise<CubeSnapshot> {
    return this.request<CubeSnapshot>("/api/cubes", { method: "POST", body: JSON.stringify(payload) });
  }

  saveCube(name: string, payload: CubeRecord & { expectedRevision?: string }): Promise<CubeSnapshot> {
    return this.request<CubeSnapshot>(`/api/cubes/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(payload) });
  }

  deleteCube(name: string, expectedRevision?: string): Promise<CubeSnapshot> {
    return this.request<CubeSnapshot>(`/api/cubes/${encodeURIComponent(name)}`, {
      method: "DELETE", body: JSON.stringify(expectedRevision !== undefined ? { expectedRevision } : {}),
    });
  }

  validateCube(name: string, payload: CubeRecord): Promise<CubeValidationResponse> {
    return this.request<CubeValidationResponse>(`/api/cubes/${encodeURIComponent(name)}/validate`, { method: "POST", body: JSON.stringify(payload) });
  }

  /** Load the structured view projection used by the View Workbench. */
  getViews(): Promise<ViewSnapshot> {
    return this.request<ViewSnapshot>("/api/views");
  }

  /** Create one view as a project draft. The server owns YAML serialization. */
  createView(payload: ViewWritePayload): Promise<ViewSnapshot> {
    return this.request<ViewSnapshot>("/api/views", { method: "POST", body: JSON.stringify(payload) });
  }

  /** Save one view with optimistic revision protection. */
  saveView(name: string, payload: ViewWritePayload): Promise<ViewSnapshot> {
    return this.request<ViewSnapshot>(`/api/views/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(payload) });
  }

  /** Alias for callers that use the REST verb in their adapter naming. */
  updateView(name: string, payload: ViewWritePayload): Promise<ViewSnapshot> {
    return this.saveView(name, payload);
  }

  /** Remove one view from the current project draft. */
  deleteView(name: string, expectedRevision?: string): Promise<ViewSnapshot> {
    return this.request<ViewSnapshot>(`/api/views/${encodeURIComponent(name)}`, {
      method: "DELETE",
      body: JSON.stringify(expectedRevision !== undefined ? { expectedRevision } : {}),
    });
  }

  /** Validate a view without writing it to the project. */
  validateView(name: string, payload: ViewWritePayload): Promise<ViewValidationResponse> {
    return this.request<ViewValidationResponse>(`/api/views/${encodeURIComponent(name)}/validate`, { method: "POST", body: JSON.stringify(payload) });
  }

  /**
   * Request a bounded read-only preview when a server provides one. A missing
   * endpoint is represented explicitly so the workbench never invents rows.
   */
  async previewView(name: string, payload: { limit?: number; maxBytes?: number } = {}): Promise<ViewPreviewResult> {
    try {
      return await this.request<ViewPreviewResult>(`/api/views/${encodeURIComponent(name)}/preview`, { method: "POST", body: JSON.stringify(payload) });
    } catch (error) {
      if (error instanceof ApiClientError && (error.status === 404 || error.status === 501)) {
        return { status: "PREVIEW_UNAVAILABLE", message: error.message };
      }
      throw error;
    }
  }

  /** Read the unified diff between a published file and its current draft. */
  getProjectDiff(path: string): Promise<ProjectDiff> {
    return this.request<ProjectDiff>(`/api/project/diff?path=${encodeURIComponent(path)}`);
  }

  getDatasourceTypes(): Promise<DatasourceType[]> {
    return this.request<DatasourceType[]>("/api/datasource-types");
  }

  getDatasources(): Promise<Datasource[]> {
    return this.request<Datasource[]>("/api/datasources");
  }

  createDatasource(payload: Omit<Datasource, "id">): Promise<Datasource> {
    return this.request<Datasource>("/api/datasources", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  updateDatasource(id: string, payload: Partial<Omit<Datasource, "id">>): Promise<Datasource> {
    return this.request<Datasource>(`/api/datasources/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  }

  activateDatasource(id: string): Promise<ActivateDatasourceResponse> {
    return this.request<ActivateDatasourceResponse>(`/api/datasources/${encodeURIComponent(id)}/activate`, {
      method: "POST",
      body: "{}",
    });
  }

  deleteDatasource(id: string): Promise<{ id: string; deleted: boolean }> {
    return this.request<{ id: string; deleted: boolean }>(`/api/datasources/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  testDatasource(id: string, payload?: { connection?: Datasource["connection"] }): Promise<ConnectionTest> {
    return this.request<ConnectionTest>(`/api/datasources/${encodeURIComponent(id)}/test`, {
      method: "POST",
      body: JSON.stringify(payload ?? {}),
    });
  }

  getSchemas(id: string): Promise<SchemaRecord[]> {
    return this.request<SchemaRecord[]>(`/api/datasources/${encodeURIComponent(id)}/schemas`);
  }

  getTables(id: string, schema: string): Promise<TableRecord[]> {
    return this.request<TableRecord[]>(
      `/api/datasources/${encodeURIComponent(id)}/tables?schema=${encodeURIComponent(schema)}`,
    );
  }

  getColumns(id: string, schema: string, table: string): Promise<ColumnRecord[]> {
    return this.request<ColumnRecord[]>(
      `/api/datasources/${encodeURIComponent(id)}/columns?schema=${encodeURIComponent(schema)}&table=${encodeURIComponent(table)}`,
    );
  }

  generateModel(id: string, schema: string, table: string, payload: { name?: string; overwrite?: boolean } = {}): Promise<GenerateModelResponse> {
    return this.request<GenerateModelResponse>(
      `/api/datasources/${encodeURIComponent(id)}/models?schema=${encodeURIComponent(schema)}&table=${encodeURIComponent(table)}`,
      { method: "POST", body: JSON.stringify(payload) },
    );
  }

  importProject(payload: { files?: ProjectFileImport[]; path?: string; projectDir?: string }): Promise<ImportProjectResponse> {
    return this.request<ImportProjectResponse>("/api/project/import", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  getProjectFiles(): Promise<ProjectFile[]> {
    return this.request<ProjectFile[]>("/api/project/files");
  }

  getProjectFile(path: string): Promise<ProjectFile> {
    return this.request<ProjectFile>(`/api/project/file?path=${encodeURIComponent(path)}`);
  }

  updateProjectFile(payload: ProjectFilePayload): Promise<ProjectFile> {
    const { path, content, delete: remove, expectedRevision } = payload;
    return this.request<ProjectFile>(`/api/project/file?path=${encodeURIComponent(path)}`, {
      method: "PUT",
      body: JSON.stringify({ ...(content !== undefined ? { content } : {}), ...(remove ? { delete: true } : {}), ...(expectedRevision ? { expectedRevision } : {}) }),
    });
  }

  validateProject(): Promise<ValidationResponse> {
    return this.request<ValidationResponse>("/api/project/validate", { method: "POST", body: "{}" });
  }

  publishProject(): Promise<PublishResponse> {
    return this.request<PublishResponse>("/api/project/publish", { method: "POST", body: "{}" });
  }

  getVersions(): Promise<VersionRecord[]> {
    return this.request<VersionRecord[]>("/api/versions");
  }

  rollbackVersion(id: string): Promise<PublishResponse> {
    return this.request<PublishResponse>(`/api/versions/${encodeURIComponent(id)}/rollback`, {
      method: "POST",
      body: "{}",
    });
  }
}

export const api = new ApiClient(import.meta.env.VITE_API_BASE_URL ?? "");
