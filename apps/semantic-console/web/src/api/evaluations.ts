import { ApiClientError } from "./client";

/** A published evaluation case is immutable within its suite revision. */
export interface EvaluationCase {
  id: string; question: string; role: string;
  expectedBehavior: "answer" | "clarify" | "deny" | "unsupported";
  referenceSql?: string; expectedResult?: { columns: string[]; rows: unknown[][] };
  assertions?: Record<string, unknown>; conditions?: Record<string, unknown>;
  clarificationReplies?: Record<string, unknown>; tags?: string[]; reviewGuidance?: string;
}
/** Editable suite head; published revisions remain immutable. */
export interface EvaluationSuite { id?: string; revision?: number; status?: string; name: string; testDatasetId: string; cases: EvaluationCase[] }
/** Versioned configuration for an explicitly selected external Agent. */
export interface AgentProfile { id?: string; revision?: number; status?: string; name: string; model: string; reasoningEffort: string; prompt: string; contextVersion: number; timeoutSeconds: number; concurrency: number; rounds: number }
/** One scheduled case and round with independent automatic and human outcomes. */
export interface EvaluationAttempt { id: string; caseId: string; round: number; status: string; score?: Record<string, unknown>; review?: { status: string; reason?: string; history?: { status: string; reason: string; reviewedBy: string; reviewedAt: string }[] }; durationMs?: number; coreTraceIds?: string[]; observedTokens?: Record<string, number>; evidenceExpiresAt?: string; agentTraceId?: string }
/** Persisted run metadata and its complete planned attempt summary. */
export interface EvaluationRun { id: string; status: string; suiteId?: string; profileId?: string; baseline?: boolean; createdAt?: string; environment?: Record<string, unknown>; suiteRevision?: number; profileRevision?: number; sourceRunId?: string; agentConfig?: AgentProfile; cases?: Pick<EvaluationCase, "id" | "question" | "role" | "tags" | "reviewGuidance" | "expectedBehavior">[]; summary: Record<string, number>; attempts?: EvaluationAttempt[] }
/** Safe runner availability and advertised test-role mappings. */
export interface EvaluationWorker { id: string; status?: string; online?: boolean; roles?: string[]; lastSeenAt?: string }
/** Registered test datasource binding, without connection credentials. */
export interface EvaluationDataset {
  id: string; version: string; datasourceId: string; scorerSubject?: string | null;
  scorerBinding?: { subjectId: string; datasetVersion: string; datasourceId: string; revision: number };
  scorerBindingValid?: boolean;
}
/** Project opt-in restricted to declared test datasets. */
export interface EvaluationSettings { enabled: boolean; testDataOnly: boolean }
/** Strict or exploratory comparison with matched case outcomes. */
export interface EvaluationComparison { mode?: string; comparable?: boolean; strict?: boolean; reasons?: string[]; items?: Record<string, unknown>[]; [key: string]: unknown }

/** Authenticated evaluation API; credentials remain in memory and HTTP headers. */
export class EvaluationClient {
  constructor(private readonly token: string) {}
  async request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
    const response = await fetch(`${(import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "")}/api/v1/evaluations${path}`, {
      method, headers: { Accept: "application/json", Authorization: `Bearer ${this.token}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const value = await response.json().catch(() => undefined);
    if (!response.ok) throw new ApiClientError(value?.message ?? `Request failed (${response.status})`, response.status, value);
    if (value === null || typeof value !== "object" || Array.isArray(value)) throw new ApiClientError("Invalid evaluation response", 502);
    if (value?.schemaVersion !== undefined && value.schemaVersion !== 1) throw new ApiClientError("Unsupported evaluation schema", 502);
    return value as T;
  }
  async list<T>(kind: string) { const value = await this.request<{ items: T[] }>(`/${kind}`); if (!Array.isArray(value.items)) throw new ApiClientError("Invalid evaluation collection", 502); return value; }
  /** Bind a registered dataset to a managed scorer identity, never a credential. */
  async setDatasetScorer(datasetId: string, subjectId: string): Promise<EvaluationDataset> {
    return this.request(`/datasets/${encodeURIComponent(datasetId)}/scorer`, "PUT", { subjectId });
  }
}
