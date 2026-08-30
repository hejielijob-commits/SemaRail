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

/** Typed REST boundary for the semantic console. Secrets are accepted only in request bodies. */
export class ApiClient {
  private readonly baseUrl: string;

  constructor(baseUrl = "") {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
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

  /** Load secret-free stdio MCP commands and readiness for the active project. */
  getMcpIntegration(): Promise<McpIntegrationResponse> {
    return this.request<McpIntegrationResponse>("/api/mcp-integration");
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
