import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ArrowClockwise, CheckCircle, GitBranch, ShieldCheck } from "@phosphor-icons/react";
import { ApiClientError, api } from "../api/client";
import type {
  BusinessEvidenceV1,
  DiagnosticFeedback,
  ExternalAgentEvaluationSummary,
  FeedbackResolution,
  RegressionCaseV2Record,
  RegressionRun,
  VersionRecord,
} from "../types";
import { Badge, Button, Field, InlineNotice, Select, TextInput } from "./ui";
import "./feedback-resolution.css";

type Locale = "en-US" | "zh-CN";

const copy = {
  "en-US": {
    title: "Resolution evidence",
    body: "Link the edited semantic asset, its published version, a versioned SQL case and run, and a reviewed external Agent evaluation.",
    refresh: "Refresh evidence",
    save: "Save links",
    asset: "Semantic asset path",
    assetHint: "Use a model, rule, view, or cube source file.",
    baseVersion: "Before edit",
    publishedVersion: "Published after edit",
    caseRevision: "Regression case revision",
    run: "SQL regression run",
    noVersions: "Publish two project versions to link an edited asset.",
    noCases: "Create and enable a regression case for this feedback first.",
    noRuns: "Run the selected case to create SQL evidence.",
    verify: "Verify chain",
    verified: "Verified",
    incomplete: "Incomplete",
    conflict: "Evidence conflict",
    stale: "Evidence expired or unavailable",
    checkNames: { feedbackEvidence: "Feedback evidence", semanticAsset: "Published semantic asset", regressionCase: "Case revision", sqlRun: "Passing SQL run", agentEvaluation: "External Agent evaluation" },
    passed: "Passed", unverified: "Unverified", staleCheck: "Stale", conflictCheck: "Conflict",
    versions: "Open version history", rules: "Open semantic editor", regressions: "Open regression cases",
    evaluations: "External Agent summaries",
    imported: "Imported", review: "Review summary", approve: "Approve", reject: "Reject",
    yes: "Yes", no: "No",
    campaign: "Campaign ID", reportHash: "Report SHA-256", source: "Evaluation source", model: "Model", reasoning: "Reasoning mode", dataset: "Dataset version", semantic: "Semantic version",
    passedCount: "Passed", failedCount: "Failed", inconclusiveCount: "Inconclusive", total: "Total verdicts", import: "Import summary",
    noEvaluations: "No external summary has been imported.",
    noQuery: "No query identifier is linked to this feedback.",
    queryEvidence: "Query evidence",
    evidenceUnavailable: "Query evidence is expired or unavailable.",
    evidenceUnsupported: "Query evidence uses an unsupported or invalid schema.",
    versionsUnavailable: "Published versions could not be loaded.", casesUnavailable: "Regression case revisions could not be loaded.", runsUnavailable: "SQL regression runs could not be loaded.",
    sqlOnly: "A passing SQL regression is one check. Full verification also requires the source feedback, changed published asset, enabled case, and an approved passing external evaluation to match.",
    manualVerified: "This feedback has a workflow status of Verified. That historical label does not establish this evidence chain.",
    rulesVersion: "Rules version", projectRevision: "Project revision", references: "Semantic references", models: "Models", columns: "Columns", metrics: "Metrics", unit: "Unit", grain: "Grain", timeField: "Time field", timeSource: "Time source", groupedTime: "Original time grouping", defaults: "Defaults applied", conditions: "Conditions", confirmations: "Confirmations", checks: "Evidence checks", queryId: "Query ID", outcome: "Execution outcome", datasource: "Data source", created: "Execution time", expires: "Evidence expires",
    timeValue: "Time value", undefined: "Undefined", valueNotDisplayed: "Value not displayed", condition: "Condition", unknown: "Unknown", required: "Required", notApplicable: "Not applicable",
    timeSources: { confirmed: "Confirmed by user", publishedDefault: "Published default", sql: "Observed in SQL", unknown: "Unknown" },
    confirmationStates: { confirmed: "Confirmed", defaulted: "Defaulted", missing: "Missing", unknown: "Unknown" },
    checkStatuses: { passed: "Passed", conflict: "Conflict", unverified: "Unverified", stale: "Stale", unknown: "Unknown" },
    unitStatuses: { verified: "Verified", unverified: "Unverified" },
    aggregation: { count: "Count", avg: "Average", unknown: "Aggregation not displayed" }, distinct: "Distinct", notDistinct: "Not distinct",
    saved: "Resolution links saved.", importedNotice: "External summary imported. A Console administrator must review it.", error: "The operation could not be completed.",
  },
  "zh-CN": {
    title: "修复证据",
    body: "关联已编辑的语义资产、实际发布版本、带版本号的 SQL 用例与运行，以及经审核的外部 Agent 评测摘要。",
    refresh: "刷新证据",
    save: "保存关联",
    asset: "语义资产路径",
    assetHint: "填写模型、规则、视图或立方体源文件路径。",
    baseVersion: "编辑前版本",
    publishedVersion: "编辑后发布版本",
    caseRevision: "回归用例版本",
    run: "SQL 回归运行",
    noVersions: "请先发布两个项目版本，再关联已编辑资产。",
    noCases: "请先为此反馈创建并启用回归用例。",
    noRuns: "运行所选用例后才能关联 SQL 证据。",
    verify: "验证闭环",
    verified: "已验证",
    incomplete: "未完成",
    conflict: "证据冲突",
    stale: "证据过期或不可用",
    checkNames: { feedbackEvidence: "反馈证据", semanticAsset: "已发布语义资产", regressionCase: "用例版本", sqlRun: "通过的 SQL 运行", agentEvaluation: "外部 Agent 评测" },
    passed: "通过", unverified: "未验证", staleCheck: "过期", conflictCheck: "冲突",
    versions: "打开版本历史", rules: "打开语义编辑器", regressions: "打开回归用例",
    evaluations: "外部 Agent 评测摘要",
    imported: "导入时间", review: "审核摘要", approve: "批准", reject: "拒绝",
    yes: "是", no: "否",
    campaign: "评测批次 ID", reportHash: "报告 SHA-256", source: "评测来源", model: "模型", reasoning: "推理模式", dataset: "数据集版本", semantic: "语义版本",
    passedCount: "通过", failedCount: "失败", inconclusiveCount: "无法判定", total: "评测总数", import: "导入摘要",
    noEvaluations: "尚未导入外部评测摘要。",
    noQuery: "此反馈没有关联查询标识。",
    queryEvidence: "查询证据",
    evidenceUnavailable: "查询证据已过期或不可用。",
    evidenceUnsupported: "查询证据使用了不支持或无效的架构版本。",
    versionsUnavailable: "无法加载已发布版本。", casesUnavailable: "无法加载回归用例版本。", runsUnavailable: "无法加载 SQL 回归运行。",
    sqlOnly: "SQL 回归通过只是其中一项检查。完整验证还要求源反馈、已变更发布资产、启用用例和获批且全通过的外部评测版本相互匹配。",
    manualVerified: "此反馈的处理状态为“已验证”；这个历史状态标签不代表证据链已完成验证。",
    rulesVersion: "规则版本", projectRevision: "项目修订", references: "语义引用", models: "模型", columns: "字段", metrics: "指标", unit: "单位", grain: "粒度", timeField: "时间字段", timeSource: "时间来源", groupedTime: "原始时间分组", defaults: "应用默认值", conditions: "条件", confirmations: "确认项", checks: "证据检查", queryId: "查询 ID", outcome: "执行结果", datasource: "数据源", created: "执行时间", expires: "证据到期时间",
    timeValue: "时间值", undefined: "未定义", valueNotDisplayed: "不显示此值", condition: "条件", unknown: "未知", required: "强制验证", notApplicable: "不适用",
    timeSources: { confirmed: "用户已确认", publishedDefault: "已发布默认值", sql: "SQL 中观察到", unknown: "未知" },
    confirmationStates: { confirmed: "已确认", defaulted: "已应用默认值", missing: "缺失", unknown: "未知" },
    checkStatuses: { passed: "通过", conflict: "冲突", unverified: "未验证", stale: "版本失效", unknown: "未知" },
    unitStatuses: { verified: "已验证", unverified: "未验证" },
    aggregation: { count: "计数", avg: "平均值", unknown: "不显示聚合方式" }, distinct: "去重", notDistinct: "不去重",
    saved: "修复关联已保存。", importedNotice: "外部摘要已导入，请由 Console 管理员审核。", error: "操作未能完成。",
  },
} as const;

function checkTone(status: string): "green" | "amber" | "red" | "neutral" {
  return status === "passed" || status === "verified" ? "green" : status === "conflict" ? "red" : status === "stale" ? "amber" : "neutral";
}

type BusinessCopy = typeof copy["en-US"] | typeof copy["zh-CN"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function safeIdentifier(value: unknown): string | null {
  return typeof value === "string" && /^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$/.test(value) ? value : null;
}

function simpleName(value: unknown): string | null {
  const direct = safeIdentifier(value);
  if (direct) return direct;
  if (!isRecord(value)) return null;
  for (const key of ["name", "column", "model", "id"]) {
    const candidate = safeIdentifier(value[key]);
    if (candidate) return candidate;
  }
  return null;
}

function names(values: unknown[] | undefined): string[] {
  return (values ?? []).map(simpleName).filter((item): item is string => Boolean(item)).slice(0, 20);
}

function isDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function dateRange(value: unknown): { start: string; end: string } | null {
  if (!isRecord(value) || Object.keys(value).length !== 2 || !isDate(value.start) || !isDate(value.end) || value.start > value.end) return null;
  return { start: value.start, end: value.end };
}

function displayBusinessValue(value: unknown, c: BusinessCopy): string {
  if (isDate(value)) return value;
  const range = dateRange(value);
  if (range) return `${range.start} – ${range.end}`;
  if (typeof value === "boolean") return value ? c.yes : c.no;
  return c.valueNotDisplayed;
}

function displayTimeValue(value: unknown, c: BusinessCopy): string {
  if (isDate(value)) return value;
  const range = dateRange(value);
  return range ? `${range.start} – ${range.end}` : c.valueNotDisplayed;
}

function evidenceEntries(value: unknown): Array<[string, unknown]> {
  return isRecord(value) ? Object.entries(value).slice(0, 40) : [];
}

function conditionName(key: string, index: number, c: BusinessCopy): string {
  return /^[A-Za-z][A-Za-z0-9_.:-]{0,127}$/.test(key) ? key : `${c.condition} ${index + 1}`;
}

function confirmationState(value: unknown, c: BusinessCopy): { label: string; tone: "green" | "amber" | "neutral" | "blue" } {
  if (value === "confirmed") return { label: c.confirmationStates.confirmed, tone: "green" };
  if (value === "defaulted") return { label: c.confirmationStates.defaulted, tone: "blue" };
  if (value === "missing") return { label: c.confirmationStates.missing, tone: "amber" };
  return { label: c.confirmationStates.unknown, tone: "neutral" };
}

function evidenceStatusLabel(value: unknown, c: BusinessCopy): string {
  if (value === "passed") return c.checkStatuses.passed;
  if (value === "conflict") return c.checkStatuses.conflict;
  if (value === "unverified") return c.checkStatuses.unverified;
  if (value === "stale") return c.checkStatuses.stale;
  return c.checkStatuses.unknown;
}

function timeSourceLabel(value: unknown, c: BusinessCopy): string {
  if (value === "confirmed") return c.timeSources.confirmed;
  if (value === "publishedDefault") return c.timeSources.publishedDefault;
  if (value === "sql") return c.timeSources.sql;
  return c.timeSources.unknown;
}

function displayGrain(value: unknown, c: BusinessCopy): string {
  const legacy = safeIdentifier(value);
  if (legacy) return legacy;
  if (!isRecord(value)) return c.valueNotDisplayed;
  const model = safeIdentifier(value.model);
  const column = safeIdentifier(value.column);
  const grain = safeIdentifier(value.value);
  const reference = [model, column].filter(Boolean).join(".");
  const detail = [reference || null, grain].filter(Boolean).join(" · ") || c.valueNotDisplayed;
  if (value.status === "not_applicable") return `${detail} · ${c.notApplicable}`;
  if (value.status === "unverified" || value.status === "conflict" || value.status === "stale") return `${detail} · ${evidenceStatusLabel(value.status, c)}`;
  return detail;
}

function displayMetric(value: unknown, c: BusinessCopy): string {
  if (!isRecord(value)) return c.valueNotDisplayed;
  const parts: string[] = [];
  const definition = safeIdentifier(value.ruleId) ?? safeIdentifier(value.checkId) ?? safeIdentifier(value.id);
  const model = safeIdentifier(value.model);
  const column = safeIdentifier(value.column);
  const reference = [model, column].filter(Boolean).join(".");
  if (definition) parts.push(definition);
  if (reference) parts.push(reference);
  if (value.aggregation !== undefined) {
    parts.push(value.aggregation === "count" ? c.aggregation.count : value.aggregation === "avg" ? c.aggregation.avg : c.aggregation.unknown);
  }
  if (typeof value.distinct === "boolean") parts.push(value.distinct ? c.distinct : c.notDistinct);
  return parts.join(" · ") || c.valueNotDisplayed;
}

function revisionLabel(version: VersionRecord): string {
  return `${version.label ?? version.id} · ${version.revision}`;
}

export default function FeedbackResolutionPanel({
  locale,
  feedback,
  adminToken,
  onOpenAsset,
  onOpenVersions,
  onOpenRegressions,
}: {
  locale: Locale;
  feedback: DiagnosticFeedback;
  adminToken: string;
  onOpenAsset?: (path: string) => void;
  onOpenVersions?: () => void;
  onOpenRegressions?: () => void;
}) {
  const c = copy[locale];
  const [resolution, setResolution] = useState<FeedbackResolution | null>(null);
  const [versions, setVersions] = useState<VersionRecord[]>([]);
  const [cases, setCases] = useState<RegressionCaseV2Record[]>([]);
  const [runs, setRuns] = useState<RegressionRun[]>([]);
  const [queryEvidence, setQueryEvidence] = useState<BusinessEvidenceV1 | null>(null);
  const [queryEvidenceError, setQueryEvidenceError] = useState("");
  const [referenceLoadErrors, setReferenceLoadErrors] = useState<string[]>([]);
  const [assetPath, setAssetPath] = useState("");
  const [baseVersionId, setBaseVersionId] = useState("");
  const [publishedVersionId, setPublishedVersionId] = useState("");
  const [caseSelection, setCaseSelection] = useState("");
  const [runId, setRunId] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [summary, setSummary] = useState({ campaignId: "", reportSha256: "", source: "", model: "", reasoning: "", datasetVersion: "", semanticVersion: "", passed: "", failed: "", inconclusive: "", total: "" });
  const loadSequence = useRef(0);

  const load = useCallback(async () => {
    if (!adminToken) return;
    const request = ++loadSequence.current;
    setLoading(true);
    setError("");
    setNotice("");
    setReferenceLoadErrors([]);
    setResolution(null);
    setVersions([]);
    setCases([]);
    setRuns([]);
    setQueryEvidence(null);
    setQueryEvidenceError("");
    setAssetPath("");
    setBaseVersionId("");
    setPublishedVersionId("");
    setCaseSelection("");
    setRunId("");
    const [resolutionResult, versionsResult, casesResult, runsResult] = await Promise.allSettled([
      api.getFeedbackResolution(feedback.id),
      api.getVersions(),
      api.listRegressionCases(),
      api.listRegressionRuns(),
    ]);
    if (request !== loadSequence.current) return;
    if (resolutionResult.status === "rejected") {
      setError(resolutionResult.reason instanceof Error ? resolutionResult.reason.message : c.error);
      setQueryEvidenceError(feedback.queryId ? c.evidenceUnavailable : c.noQuery);
      setLoading(false);
      return;
    }
    const referenceErrors: string[] = [];
    if (versionsResult.status === "rejected") referenceErrors.push(c.versionsUnavailable);
    if (casesResult.status === "rejected") referenceErrors.push(c.casesUnavailable);
    if (runsResult.status === "rejected") referenceErrors.push(c.runsUnavailable);
    setReferenceLoadErrors(referenceErrors);
    const saved = resolutionResult.value;
    setResolution(saved);
    setAssetPath(saved.assetPath ?? "");
    setBaseVersionId(saved.baseVersionId ?? "");
    setPublishedVersionId(saved.publishedVersionId ?? "");
    setCaseSelection(saved.caseId && saved.caseRevision !== null ? `${saved.caseId}@${saved.caseRevision}` : "");
    setRunId(saved.runId ?? "");
    setVersions(versionsResult.status === "fulfilled" ? versionsResult.value : []);
    setCases(casesResult.status === "fulfilled" ? casesResult.value.items.filter((item) => item.feedbackId === feedback.id && item.case.schemaVersion === 2) : []);
    setRuns(runsResult.status === "fulfilled" ? runsResult.value.items : []);
    if (feedback.queryId) {
      try {
        const evidence = await api.getQueryEvidence(feedback.queryId);
        if (request !== loadSequence.current) return;
        if (evidence.queryId !== feedback.queryId) throw new Error("Query evidence reference mismatch");
        setQueryEvidence(evidence);
        setQueryEvidenceError("");
      } catch (cause) {
        if (request !== loadSequence.current) return;
        setQueryEvidence(null);
        setQueryEvidenceError(cause instanceof ApiClientError && cause.code === "UNSUPPORTED_BUSINESS_EVIDENCE_VERSION" ? c.evidenceUnsupported : c.evidenceUnavailable);
      }
    } else {
      setQueryEvidence(null);
      setQueryEvidenceError(c.noQuery);
    }
    setLoading(false);
  }, [adminToken, c.casesUnavailable, c.error, c.evidenceUnavailable, c.evidenceUnsupported, c.noQuery, c.runsUnavailable, c.versionsUnavailable, feedback.id, feedback.queryId]);

  useEffect(() => {
    void load();
    return () => { loadSequence.current += 1; };
  }, [load]);

  const selectedCase = useMemo(() => {
    if (!caseSelection) return null;
    const split = caseSelection.lastIndexOf("@");
    return cases.find((item) => item.id === caseSelection.slice(0, split) && item.revision === Number(caseSelection.slice(split + 1))) ?? null;
  }, [caseSelection, cases]);
  const runOptions = useMemo(() => runs.flatMap((run) => run.results
    .filter((result) => result.feedbackId === feedback.id && (!selectedCase || (result.caseId === selectedCase.id && result.caseRevision === selectedCase.revision)))
    .map((result) => ({ run, result }))), [feedback.id, runs, selectedCase]);
  const selectedPublishedVersion = versions.find((item) => item.id === publishedVersionId);

  async function saveLinks() {
    if (!resolution) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const saved = await api.saveFeedbackResolution(feedback.id, {
        expectedRevision: resolution.revision,
        assetPath: assetPath.trim() || null,
        baseVersionId: baseVersionId || null,
        publishedVersionId: publishedVersionId || null,
        caseId: selectedCase?.id ?? null,
        caseRevision: selectedCase?.revision ?? null,
        runId: runId || null,
      });
      setResolution(saved);
      setNotice(c.saved);
    } catch (cause) { setError(cause instanceof Error ? cause.message : c.error); }
    finally { setBusy(false); }
  }

  async function verify() {
    setBusy(true); setError(""); setNotice("");
    try { setResolution(await api.verifyFeedbackResolution(feedback.id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : c.error); }
    finally { setBusy(false); }
  }

  async function importEvaluation(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setNotice("");
    try {
      await api.importAgentEvaluation(feedback.id, {
        schemaVersion: 1,
        campaignId: summary.campaignId.trim(), reportSha256: summary.reportSha256.trim().toLowerCase(),
        source: summary.source.trim(), model: summary.model.trim(), reasoning: summary.reasoning.trim(),
        datasetVersion: summary.datasetVersion.trim(), semanticVersion: summary.semanticVersion.trim(),
        verdictAggregates: { passed: Number(summary.passed), failed: Number(summary.failed), inconclusive: Number(summary.inconclusive), total: Number(summary.total) },
      });
      setSummary({ ...summary, campaignId: "", reportSha256: "", passed: "", failed: "", inconclusive: "", total: "" });
      await load();
      setNotice(c.importedNotice);
    } catch (cause) { setError(cause instanceof Error ? cause.message : c.error); }
    finally { setBusy(false); }
  }

  async function review(evaluation: ExternalAgentEvaluationSummary, decision: "approved" | "rejected") {
    setBusy(true); setError(""); setNotice("");
    try {
      await api.reviewAgentEvaluation(feedback.id, evaluation.id, { expectedRevision: evaluation.revision, decision });
      await load();
    } catch (cause) { setError(cause instanceof Error ? cause.message : c.error); }
    finally { setBusy(false); }
  }

  const evidence = queryEvidence;
  const execution = evidence ? {
    queryId: evidence.queryId,
    datasourceId: evidence.datasourceId,
    outcome: evidence.outcome,
    createdAt: evidence.createdAt,
    expiresAt: evidence.expiresAt,
  } : null;
  const modelNames = names(evidence?.models);
  const columnNames = names(evidence?.columns);
  const metricDetails = (evidence?.metrics ?? []).slice(0, 20).map((item) => displayMetric(item, c));
  const grainDetails = (evidence?.grain ?? []).slice(0, 40).map((item) => displayGrain(item, c));
  const defaultEntries = evidenceEntries(evidence?.defaultsApplied);
  const conditionEntries = evidenceEntries(evidence?.conditions);
  const confirmationEntries = evidenceEntries(evidence?.confirmations);
  const confirmationByKey = new Map(confirmationEntries);
  const verification = resolution?.verification;
  const verificationTitle = verification ? c[verification.status] : c.incomplete;

  return <section className="panel feedback-resolution-panel">
    <header className="feedback-resolution-heading">
      <div><p className="panel-kicker">{c.title}</p><h3>{c.title}</h3><p>{c.body}</p></div>
      <div className="heading-actions">
        {onOpenVersions ? <Button size="sm" variant="ghost" icon={GitBranch} onClick={onOpenVersions}>{c.versions}</Button> : null}
        <Button size="sm" variant="ghost" icon={ArrowClockwise} loading={loading} onClick={() => void load()}>{c.refresh}</Button>
      </div>
    </header>
    {error ? <InlineNotice tone="error" title={c.error}>{error}</InlineNotice> : null}
    {referenceLoadErrors.map((message) => <InlineNotice key={message} tone="warning">{message}</InlineNotice>)}
    {feedback.status === "verified" ? <InlineNotice tone="info">{c.manualVerified}</InlineNotice> : null}
    {notice ? <InlineNotice tone="success">{notice}</InlineNotice> : null}
    <div className="feedback-resolution-grid">
      <Field label={c.asset} hint={c.assetHint}><TextInput value={assetPath} onChange={(event) => setAssetPath(event.target.value)} placeholder="knowledge/rules/revenue.md" /></Field>
      <Field label={c.baseVersion}><Select value={baseVersionId} onChange={(event) => setBaseVersionId(event.target.value)}><option value="">—</option>{versions.map((version) => <option key={version.id} value={version.id}>{revisionLabel(version)}</option>)}</Select></Field>
      <Field label={c.publishedVersion}><Select value={publishedVersionId} onChange={(event) => setPublishedVersionId(event.target.value)}><option value="">—</option>{versions.map((version) => <option key={version.id} value={version.id}>{revisionLabel(version)}</option>)}</Select></Field>
      <Field label={c.caseRevision}><Select value={caseSelection} onChange={(event) => { setCaseSelection(event.target.value); setRunId(""); }}><option value="">—</option>{resolution?.caseId && resolution.caseRevision !== null && !cases.some((item) => `${item.id}@${item.revision}` === `${resolution.caseId}@${resolution.caseRevision}`) ? <option value={`${resolution.caseId}@${resolution.caseRevision}`}>{resolution.caseId} · r{resolution.caseRevision}</option> : null}{cases.map((item) => <option key={`${item.id}@${item.revision}`} value={`${item.id}@${item.revision}`}>{item.id} · r{item.revision} · {item.datasetVersion ?? "—"}</option>)}</Select></Field>
      <Field label={c.run}><Select value={runId} onChange={(event) => setRunId(event.target.value)}><option value="">—</option>{resolution?.runId && !runOptions.some(({ run }) => run.id === resolution.runId) ? <option value={resolution.runId}>{resolution.runId} · saved evidence</option> : null}{runOptions.map(({ run, result }) => <option key={`${run.id}:${result.caseId}`} value={run.id}>{run.id} · {result.status} · r{result.caseRevision}</option>)}</Select></Field>
      {versions.length < 2 ? <p className="feedback-resolution-hint">{c.noVersions}</p> : null}
      {!cases.length ? <p className="feedback-resolution-hint">{c.noCases}</p> : null}
      {!runOptions.length ? <p className="feedback-resolution-hint">{c.noRuns}</p> : null}
    </div>
    <div className="feedback-resolution-actions">
      <div className="heading-actions">
        {assetPath.trim() && onOpenAsset ? <Button size="sm" variant="secondary" onClick={() => onOpenAsset(assetPath.trim())}>{c.rules}</Button> : null}
        {onOpenRegressions ? <Button size="sm" variant="secondary" onClick={onOpenRegressions}>{c.regressions}</Button> : null}
      </div>
      <div className="heading-actions">
        <Button size="sm" variant="secondary" loading={busy} disabled={!resolution} onClick={() => void saveLinks()}>{c.save}</Button>
        <Button size="sm" variant="primary" icon={ShieldCheck} loading={busy} onClick={() => void verify()}>{c.verify}</Button>
      </div>
    </div>
    {verification ? <div className="feedback-verification" aria-live="polite">
      <div className="feedback-verification-title"><strong>{c.verify}</strong><Badge tone={checkTone(verification.status)} dot>{verificationTitle}</Badge></div>
      <p className="feedback-resolution-hint">{c.sqlOnly}</p>
      <div className="feedback-verification-checks">{verification.checks.map((check) => <div key={check.key}><span>{c.checkNames[check.key as keyof typeof c.checkNames] ?? check.key}</span><Badge tone={checkTone(check.status)}>{check.status === "passed" ? c.passed : check.status === "stale" ? c.staleCheck : check.status === "conflict" ? c.conflictCheck : c.unverified}</Badge><code>{check.reasonCode}</code></div>)}</div>
    </div> : null}

    <section className="feedback-agent-evaluations">
      <div className="feedback-subheading"><h4>{c.evaluations}</h4><CheckCircle size={18} weight="duotone" /></div>
      {resolution?.agentEvaluations.length ? resolution.agentEvaluations.map((evaluation) => <article className="feedback-agent-evaluation" key={evaluation.id}>
        <div className="feedback-evaluation-meta"><strong>{evaluation.campaignId}</strong><Badge tone={evaluation.review?.decision === "approved" ? "green" : evaluation.review?.decision === "rejected" ? "red" : "amber"}>{evaluation.review?.decision ?? c.imported}</Badge></div>
        <dl><div><dt>{c.source}</dt><dd>{evaluation.source}</dd></div><div><dt>{c.model}</dt><dd>{evaluation.model} · {evaluation.reasoning}</dd></div><div><dt>{c.dataset}</dt><dd>{evaluation.datasetVersion}</dd></div><div><dt>{c.semantic}</dt><dd>{evaluation.semanticVersion}</dd></div><div><dt>{c.passedCount} / {c.failedCount} / {c.inconclusiveCount} / {c.total}</dt><dd>{evaluation.verdictAggregates.passed} / {evaluation.verdictAggregates.failed} / {evaluation.verdictAggregates.inconclusive} / {evaluation.verdictAggregates.total}</dd></div><div><dt>{c.reportHash}</dt><dd><code>{evaluation.reportSha256}</code></dd></div></dl>
        <p className="feedback-review-meta">{evaluation.review ? `${evaluation.review.reviewerSubjectId} · ${new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(evaluation.review.reviewedAt))}` : `${c.imported} · ${new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(evaluation.importedAt))}`}</p>
        <div className="heading-actions"><Button size="sm" variant="primary" loading={busy} onClick={() => void review(evaluation, "approved")}>{c.approve}</Button><Button size="sm" variant="secondary" loading={busy} onClick={() => void review(evaluation, "rejected")}>{c.reject}</Button></div>
      </article>) : <p className="feedback-resolution-hint">{c.noEvaluations}</p>}
      <form className="feedback-evaluation-form" onSubmit={(event) => void importEvaluation(event)}>
        <Field label={c.campaign}><TextInput required value={summary.campaignId} onChange={(event) => setSummary({ ...summary, campaignId: event.target.value })} /></Field>
        <Field label={c.reportHash}><TextInput required pattern="[a-fA-F0-9]{64}" value={summary.reportSha256} onChange={(event) => setSummary({ ...summary, reportSha256: event.target.value })} /></Field>
        <Field label={c.source}><TextInput required value={summary.source} onChange={(event) => setSummary({ ...summary, source: event.target.value })} /></Field>
        <Field label={c.model}><TextInput required value={summary.model} onChange={(event) => setSummary({ ...summary, model: event.target.value })} /></Field>
        <Field label={c.reasoning}><TextInput required value={summary.reasoning} onChange={(event) => setSummary({ ...summary, reasoning: event.target.value })} /></Field>
        <Field label={c.dataset}><TextInput required value={summary.datasetVersion || selectedCase?.datasetVersion || ""} onChange={(event) => setSummary({ ...summary, datasetVersion: event.target.value })} /></Field>
        <Field label={c.semantic}><TextInput required value={summary.semanticVersion || selectedPublishedVersion?.revision || ""} onChange={(event) => setSummary({ ...summary, semanticVersion: event.target.value })} /></Field>
        <Field label={c.passedCount}><TextInput required type="number" min={0} value={summary.passed} onChange={(event) => setSummary({ ...summary, passed: event.target.value })} /></Field>
        <Field label={c.failedCount}><TextInput required type="number" min={0} value={summary.failed} onChange={(event) => setSummary({ ...summary, failed: event.target.value })} /></Field>
        <Field label={c.inconclusiveCount}><TextInput required type="number" min={0} value={summary.inconclusive} onChange={(event) => setSummary({ ...summary, inconclusive: event.target.value })} /></Field>
        <Field label={c.total}><TextInput required type="number" min={1} value={summary.total} onChange={(event) => setSummary({ ...summary, total: event.target.value })} /></Field>
        <Button size="sm" variant="secondary" loading={busy} type="submit">{c.import}</Button>
      </form>
    </section>

    <section className="business-query-evidence">
      <h4>{c.queryEvidence}</h4>
      {queryEvidenceError ? <InlineNotice tone="warning">{queryEvidenceError}</InlineNotice> : null}
      {evidence ? <>
        <dl className="business-evidence-meta">
          <div><dt>{c.rulesVersion}</dt><dd>{evidence.rulesVersion ?? "—"}</dd></div>
          <div><dt>{c.projectRevision}</dt><dd>{evidence.projectRevision ?? "—"}</dd></div>
          <div><dt>{c.unit}</dt><dd>{typeof evidence.unit?.value === "string" && evidence.unit.value.trim() ? evidence.unit.value : c.undefined} · {evidence.unit?.status === "verified" ? c.unitStatuses.verified : c.unitStatuses.unverified}</dd></div>
          <div><dt>{c.grain}</dt><dd>{grainDetails.join(", ") || "—"}</dd></div>
          <div><dt>{c.timeField}</dt><dd>{safeIdentifier(evidence.time?.field) ?? "—"}</dd></div>
          <div><dt>{c.timeValue}</dt><dd>{displayTimeValue(evidence.time?.value, c)}</dd></div>
          <div><dt>{c.timeSource}</dt><dd>{timeSourceLabel(evidence.time?.source, c)}</dd></div>
          <div><dt>{c.groupedTime}</dt><dd>{evidence.time?.groupedByOriginal === true ? c.yes : evidence.time?.groupedByOriginal === false ? c.no : "—"}</dd></div>
          <div><dt>{c.models}</dt><dd>{modelNames.join(", ") || "—"}</dd></div>
          <div><dt>{c.columns}</dt><dd>{columnNames.join(", ") || "—"}</dd></div>
          <div><dt>{c.metrics}</dt><dd>{metricDetails.join("; ") || "—"}</dd></div>
          <div><dt>{c.defaults}</dt><dd><div className="business-evidence-items">{defaultEntries.length ? defaultEntries.map(([key, value], index) => {
            const state = confirmationState(confirmationByKey.get(key) ?? "defaulted", c);
            return <span key={`${conditionName(key, index, c)}:${index}`}><code>{conditionName(key, index, c)}</code>: {displayBusinessValue(value, c)} <Badge tone={state.tone}>{state.label}</Badge></span>;
          }) : "—"}</div></dd></div>
          <div><dt>{c.conditions}</dt><dd><div className="business-evidence-items">{conditionEntries.length ? conditionEntries.map(([key, value], index) => {
            const state = confirmationState(confirmationByKey.get(key), c);
            return <span key={`${conditionName(key, index, c)}:${index}`}><code>{conditionName(key, index, c)}</code>: {displayBusinessValue(value, c)} <Badge tone={state.tone}>{state.label}</Badge></span>;
          }) : "—"}</div></dd></div>
          <div><dt>{c.confirmations}</dt><dd><div className="business-evidence-items">{confirmationEntries.length ? confirmationEntries.map(([key, value], index) => {
            const state = confirmationState(value, c);
            return <span key={`${conditionName(key, index, c)}:${index}`}><code>{conditionName(key, index, c)}</code> <Badge tone={state.tone}>{state.label}</Badge></span>;
          }) : "—"}</div></dd></div>
          {execution ? <><div><dt>{c.queryId}</dt><dd>{execution.queryId}</dd></div><div><dt>{c.outcome}</dt><dd>{execution.outcome ?? "—"}</dd></div><div><dt>{c.datasource}</dt><dd>{execution.datasourceId ?? "—"}</dd></div><div><dt>{c.created}</dt><dd>{execution.createdAt ?? "—"}</dd></div><div><dt>{c.expires}</dt><dd>{execution.expiresAt ?? "—"}</dd></div></> : null}
        </dl>
        <div className="business-reference-list"><strong>{c.references}</strong>{evidence.references?.length ? evidence.references.map((reference, index) => <span key={`${reference.ruleId ?? "rule"}:${index}`}>{[reference.ruleId, reference.model, reference.column].filter(Boolean).join(" · ")}</span>) : <span>—</span>}</div>
        <div className="business-check-list"><strong>{c.checks}</strong>{evidence.checks?.length ? evidence.checks.map((check, index) => <div key={safeIdentifier(check.id) ?? `check:${index}`}><span>{safeIdentifier(check.id) ?? c.condition}{safeIdentifier(check.ruleId) ? ` · ${safeIdentifier(check.ruleId)}` : ""}{check.mandatory ? ` · ${c.required}` : ""}</span><Badge tone={checkTone(check.status)}>{evidenceStatusLabel(check.status, c)}</Badge><code>{safeIdentifier(check.reasonCode) ?? "—"}</code></div>) : <span>—</span>}</div>
      </> : null}
    </section>
  </section>;
}
