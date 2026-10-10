import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { ArrowClockwise, CheckCircle, DownloadSimple, Flask, LinkSimple, Play, Plus, Trash, WarningCircle } from "@phosphor-icons/react";
import { api, ApiClientError } from "../api/client";
import type { CoreDiagnosticRecord, RegressionCaseV2, RegressionCaseV2Record, RegressionRun, RegressionRunResult, TestDatasetRecord } from "../types";
import { Badge, Button, EmptyState, Field, InlineNotice, LoadingRows, Modal, SectionHeading, Select, TextArea, TextInput } from "./ui";
import "./diagnostics-workbench.css";
import "./regression-workbench.css";

type Locale = "en-US" | "zh-CN";
type CaseSeed = { feedbackId: string; sourceTraceId?: string };

const labels = {
  "en-US": {
    notCollected: "Not collected", coreUnavailable: "The Core diagnostic is missing or expired in this project.",
    eyebrow: "Quality loop", title: "Regression cases", description: "Register test datasets, review manually authored cases, and run deterministic checks against the active data source.",
    locked: "Console administrator authentication required", lockedBody: "Unlock the Console with a project-scoped administrator credential to manage regression cases.",
    refresh: "Refresh", export: "Export versioned JSON", newCase: "New case", noCases: "No regression cases", noCasesBody: "Create a draft with a registered test dataset and manual assertions.",
    datasets: "Registered test datasets", datasetsHint: "Datasets are registered against the active data source. Credentials and query results are not stored here.", datasetId: "Dataset ID", datasetVersion: "Dataset version", datasource: "Active data source", register: "Register dataset", updateDataset: "Update dataset", cancelEdit: "Cancel edit", noDatasource: "Select an active data source before registering a test dataset.", noDatasets: "No test datasets registered for the active data source.", delete: "Delete", edit: "Edit", matching: "Matches active data source", mismatch: "Different data source", datasetRequired: "Choose a registered dataset for the active data source.",
    draft: "Draft", enabled: "Enabled", legacy: "Legacy · view and export only", legacyBody: "This version 1 case is preserved for review/export. Upgrade by creating a new deterministic SQL case.", caseId: "Case ID", feedbackId: "Feedback ID", sourceTraceId: "Source trace ID", revision: "Revision", updated: "Updated", kind: "Kind", question: "Question", semanticSql: "Semantic SQL", conditions: "Conditions JSON", confirmedConditions: "Confirmed conditions JSON", assertions: "Expected assertions", expectedStatus: "Expected status", expectedError: "Expected error", errorCode: "Error code", reasonCode: "Reason code", phase: "Phase", rowCount: "Expected row count", cells: "Cell assertions", addCell: "Add cell assertion", removeCell: "Remove cell", rowIndex: "Row index (0-based)", columnName: "Column name", expectedValue: "Expected scalar", scalarType: "Value type", scalarString: "String", scalarNumber: "Number", scalarBoolean: "Boolean", scalarNull: "Null", tolerance: "Numeric tolerance", orderByHint: "Cell assertions require a top-level ORDER BY with observable unique keys. SQL row limits or uncertain ordering produce an inconclusive result.", caseFormHint: "Enter case data manually. Evidence bodies and current query values are never copied into this form.", saveDraft: "Save draft", saveChanges: "Save changes", enableAfterSave: "Enable after validation", create: "Create draft", createFailed: "Case could not be saved.", noDatasetsForForm: "Register a dataset for the active data source before creating an executable case.",
    selectCases: "Select enabled cases", selected: "Selected", runSelected: "Run selected", run: "Run", runHistory: "Run history", noRuns: "No regression runs", noRunsBody: "Run an enabled case to create a durable run record.", runStatus: "Run status", runId: "Run ID", subject: "Executed as current authenticated subject", queued: "Queued", running: "Running", completed: "Completed", cancelled: "Cancelled", interrupted: "Interrupted", passed: "Passed", failed: "Failed", blocked: "Blocked", inconclusive: "Inconclusive", assertionsResult: "Assertion results", duration: "Duration", environment: "Environment", datasetVersionResult: "Dataset version", semanticVersion: "Semantic version", policyVersions: "Policy versions", coreTraces: "Core trace IDs", sourceIssue: "Open source issue", openOriginal: "Open original trace", inspectCore: "Inspect Core diagnostic", cancelRun: "Cancel run", refreshRun: "Refresh run", runLimit: "Select at most 20 enabled cases.", noResults: "This run has no result records yet.", queryId: "Query ID", traceId: "Trace ID", status: "Status", reason: "Reason code", result: "Result", requestFailed: "Request failed", coreDiagnostic: "Core diagnostic", method: "Method", stage: "Stage", noCoreDetail: "Select a Core trace ID to inspect its safe diagnostic metadata.", coreExpires: "Available until", noIssue: "No linked issue", noTrace: "No source trace", sourceTraceUnavailable: "The original Agent trace is missing or expired in this project.", expectedSuccess: "Success", expectedFailure: "Failure", yes: "Yes", no: "No", draftOnly: "New cases start as drafts. Review and enable them after saving.",
  },
  "zh-CN": {
    notCollected: "未采集", coreUnavailable: "此项目中的 Core 诊断已过期或不存在。",
    eyebrow: "质量闭环", title: "回归用例", description: "注册测试数据集，审核手动编写的用例，并基于当前数据源运行确定性校验。",
    locked: "需要 Console 管理员认证", lockedBody: "请使用当前项目范围内的管理员凭证解锁 Console 后管理回归用例。",
    refresh: "刷新", export: "导出版本化 JSON", newCase: "新建用例", noCases: "暂无回归用例", noCasesBody: "使用已注册测试数据集和手动断言创建草稿。",
    datasets: "已注册测试数据集", datasetsHint: "数据集会绑定当前活动数据源。此处不会保存凭证或查询结果。", datasetId: "数据集 ID", datasetVersion: "数据集版本", datasource: "活动数据源", register: "注册数据集", updateDataset: "更新数据集", cancelEdit: "取消编辑", noDatasource: "请先选择活动数据源，再注册测试数据集。", noDatasets: "当前活动数据源没有已注册的测试数据集。", delete: "删除", edit: "编辑", matching: "匹配活动数据源", mismatch: "其他数据源", datasetRequired: "请选择当前活动数据源的已注册数据集。",
    draft: "草稿", enabled: "已启用", legacy: "旧版 · 仅查看和导出", legacyBody: "此 v1 用例保留用于查看和导出。请创建新的确定性 SQL 用例进行升级。", caseId: "用例 ID", feedbackId: "反馈 ID", sourceTraceId: "来源追踪 ID", revision: "修订版", updated: "更新时间", kind: "类型", question: "问题", semanticSql: "语义 SQL", conditions: "条件 JSON", confirmedConditions: "已确认条件 JSON", assertions: "预期断言", expectedStatus: "预期状态", expectedError: "预期错误", errorCode: "错误代码", reasonCode: "原因代码", phase: "阶段", rowCount: "预期行数", cells: "单元格断言", addCell: "添加单元格断言", removeCell: "移除断言", rowIndex: "行索引（从 0 开始）", columnName: "列名", expectedValue: "预期标量", scalarType: "值类型", scalarString: "字符串", scalarNumber: "数字", scalarBoolean: "布尔值", scalarNull: "空值", tolerance: "数值容差", orderByHint: "单元格断言要求 SQL 顶层包含 ORDER BY，并使用可观测的唯一排序键。行数限制或顺序不确定时显示“无法判定”。", caseFormHint: "请手动填写用例。表单不会复制证据正文或当前查询值。", saveDraft: "保存草稿", saveChanges: "保存修改", enableAfterSave: "通过校验后启用", create: "创建草稿", createFailed: "无法保存用例。", noDatasetsForForm: "请先为当前活动数据源注册数据集，再创建可执行用例。",
    selectCases: "选择已启用用例", selected: "已选", runSelected: "运行所选", run: "运行", runHistory: "运行历史", noRuns: "暂无回归运行记录", noRunsBody: "运行已启用用例后会生成持久化记录。", runStatus: "运行状态", runId: "运行 ID", subject: "由当前认证主体执行", queued: "排队中", running: "运行中", completed: "已完成", cancelled: "已取消", interrupted: "已中断", passed: "通过", failed: "失败", blocked: "阻塞", inconclusive: "无法判定", assertionsResult: "断言结果", duration: "耗时", environment: "运行环境", datasetVersionResult: "数据集版本", semanticVersion: "语义版本", policyVersions: "策略版本", coreTraces: "Core 追踪 ID", sourceIssue: "打开来源问题", openOriginal: "打开原始追踪", inspectCore: "查看 Core 诊断", cancelRun: "取消运行", refreshRun: "刷新运行", runLimit: "最多选择 20 个已启用用例。", noResults: "此运行尚无结果记录。", queryId: "查询 ID", traceId: "追踪 ID", status: "状态", reason: "原因代码", result: "结果", requestFailed: "请求失败", coreDiagnostic: "Core 诊断", method: "方法", stage: "阶段", noCoreDetail: "选择 Core 追踪 ID 以查看安全诊断元数据。", coreExpires: "有效期至", noIssue: "没有关联问题", noTrace: "没有来源追踪", sourceTraceUnavailable: "此项目中的原始 Agent 追踪已过期或不存在。", expectedSuccess: "成功", expectedFailure: "失败", yes: "是", no: "否", draftOnly: "新用例会以草稿保存。保存后请审核并启用。",
  },
} as const;

function isV2Case(item: RegressionCaseV2Record): item is RegressionCaseV2Record & { case: RegressionCaseV2 } {
  return item.case.schemaVersion === 2 && item.case.kind === "deterministic_sql";
}

function formatTime(value: string | undefined, locale: Locale) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function statusTone(status: string): "neutral" | "blue" | "green" | "amber" | "red" {
  if (status === "running" || status === "queued") return "blue";
  if (status === "passed" || status === "enabled" || status === "completed") return "green";
  if (status === "failed") return "red";
  if (status === "blocked" || status === "inconclusive" || status === "cancelled" || status === "interrupted" || status === "draft") return "amber";
  return "neutral";
}

function localStatus(status: string, locale: Locale) {
  const en: Record<string, string> = { queued: "Queued", running: "Running", completed: "Completed", cancelled: "Cancelled", interrupted: "Interrupted", passed: "Passed", failed: "Failed", blocked: "Blocked", inconclusive: "Inconclusive", draft: "Draft", enabled: "Enabled" };
  const zh: Record<string, string> = { queued: "排队中", running: "运行中", completed: "已完成", cancelled: "已取消", interrupted: "已中断", passed: "通过", failed: "失败", blocked: "阻塞", inconclusive: "无法判定", draft: "草稿", enabled: "已启用" };
  return (locale === "zh-CN" ? zh : en)[status] ?? status;
}

function toExportFile(data: unknown, filename: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = filename; anchor.click(); URL.revokeObjectURL(url);
}

export default function RegressionWorkbench({ locale, adminToken, activeDatasourceId, initialSeed, onSeedConsumed, onOpenIssue, onOpenTrace }: {
  locale: Locale;
  adminToken: string;
  activeDatasourceId: string;
  initialSeed?: CaseSeed | null;
  onSeedConsumed?: () => void;
  onOpenIssue?: (feedbackId: string) => void;
  onOpenTrace?: (traceId: string) => void;
}) {
  const c = labels[locale];
  const [datasets, setDatasets] = useState<TestDatasetRecord[]>([]);
  const [cases, setCases] = useState<RegressionCaseV2Record[]>([]);
  const [runs, setRuns] = useState<RegressionRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<RegressionRun | null>(null);
  const [selectedCaseIds, setSelectedCaseIds] = useState<string[]>([]);
  const [editingDataset, setEditingDataset] = useState<TestDatasetRecord | null>(null);
  const [datasetId, setDatasetId] = useState("");
  const [datasetVersion, setDatasetVersion] = useState("");
  const [editingCase, setEditingCase] = useState<RegressionCaseV2Record | null>(null);
  const [caseSeed, setCaseSeed] = useState<CaseSeed | null>(null);
  const [showCaseEditor, setShowCaseEditor] = useState(false);
  const [coreDiagnostic, setCoreDiagnostic] = useState<CoreDiagnosticRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const loadAll = useCallback(async () => {
    if (!adminToken) return;
    setLoading(true); setError("");
    try {
      const [datasetResponse, caseResponse, runResponse] = await Promise.all([api.listTestDatasets(), api.listRegressionCases(), api.listRegressionRuns()]);
      setDatasets(datasetResponse.items);
      setCases(caseResponse.items);
      setRuns(runResponse.items);
    } catch (cause) { setError(cause instanceof Error ? cause.message : c.requestFailed); }
    finally { setLoading(false); }
  }, [adminToken, c.requestFailed]);

  useEffect(() => { void loadAll(); }, [loadAll]);
  useEffect(() => {
    if (!initialSeed) return;
    setCaseSeed(initialSeed); setEditingCase(null); setShowCaseEditor(true); onSeedConsumed?.();
  }, [initialSeed, onSeedConsumed]);

  const matchedDatasets = useMemo(() => datasets.filter((item) => activeDatasourceId && item.datasourceId === activeDatasourceId), [activeDatasourceId, datasets]);
  const runnableCaseIds = useMemo(() => new Set(cases.filter((item) => isV2Case(item) && item.status === "enabled" && matchedDatasets.some((dataset) => dataset.id === item.case.testDatasetId)).map((item) => item.id)), [cases, matchedDatasets]);

  async function saveDataset(event: FormEvent) {
    event.preventDefault();
    if (!activeDatasourceId || !datasetId.trim() || !datasetVersion.trim()) return;
    setSaving(true); setError("");
    try {
      if (editingDataset) await api.updateTestDataset(editingDataset.id, { version: datasetVersion.trim(), datasourceId: activeDatasourceId });
      else await api.createTestDataset({ id: datasetId.trim(), version: datasetVersion.trim(), datasourceId: activeDatasourceId });
      setEditingDataset(null); setDatasetId(""); setDatasetVersion(""); await loadAll();
    } catch (cause) { setError(cause instanceof Error ? cause.message : c.requestFailed); }
    finally { setSaving(false); }
  }

  function editDataset(dataset: TestDatasetRecord) { setEditingDataset(dataset); setDatasetId(dataset.id); setDatasetVersion(dataset.version); }

  function beginNewCase(seed: CaseSeed | null = null) { setEditingCase(null); setCaseSeed(seed); setShowCaseEditor(true); }

  async function runCases(caseIds: string[]) {
    if (!caseIds.length || caseIds.length > 20) { setError(c.runLimit); return; }
    setSaving(true); setError("");
    try {
      const run = await api.createRegressionRun(caseIds);
      setSelectedRun(run); setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]); setSelectedCaseIds([]);
      await loadAll();
      const freshRun = await api.getRegressionRun(run.id).catch(() => run);
      setSelectedRun(freshRun);
    } catch (cause) { setError(cause instanceof Error ? cause.message : c.requestFailed); }
    finally { setSaving(false); }
  }

  async function openRun(run: RegressionRun) {
    setSelectedRun(run); setCoreDiagnostic(null); setError("");
    try { setSelectedRun(await api.getRegressionRun(run.id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : c.requestFailed); }
  }

  async function cancelRun() {
    if (!selectedRun) return;
    setSaving(true); setError("");
    try {
      const updated = await api.cancelRegressionRun(selectedRun.id); setSelectedRun(updated); await loadAll();
    } catch (cause) { setError(cause instanceof Error ? cause.message : c.requestFailed); }
    finally { setSaving(false); }
  }

  async function inspectCore(id: string) {
    setCoreDiagnostic(null); setSaving(true); setError("");
    try { setCoreDiagnostic(await api.getCoreDiagnostic(id)); }
    catch (cause) { setError(cause instanceof ApiClientError && cause.code === "DIAGNOSTIC_NOT_FOUND"
      ? c.coreUnavailable : cause instanceof Error ? cause.message : c.requestFailed); }
    finally { setSaving(false); }
  }

  async function openOriginalTrace(id: string) {
    if (!onOpenTrace) return;
    setError("");
    try { await api.getTrace(id); onOpenTrace(id); }
    catch { setError(c.sourceTraceUnavailable); }
  }

  async function exportCases() {
    setSaving(true); setError("");
    try { const data = await api.exportRegressionCasesV2(); toExportFile(data, `semarail-regression-cases-v${data.schemaVersion}.json`); }
    catch (cause) { setError(cause instanceof Error ? cause.message : c.requestFailed); }
    finally { setSaving(false); }
  }

  if (!adminToken) return <div className="page regression-page"><SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} /><InlineNotice tone="warning" title={c.locked}>{c.lockedBody}</InlineNotice></div>;

  return <div className="page regression-page">
    <SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} action={<div className="heading-actions"><Button variant="secondary" icon={ArrowClockwise} loading={loading} onClick={() => void loadAll()}>{c.refresh}</Button><Button variant="secondary" icon={DownloadSimple} loading={saving} onClick={() => void exportCases()}>{c.export}</Button><Button variant="primary" icon={Plus} onClick={() => beginNewCase()}>{c.newCase}</Button></div>} />
    {error ? <InlineNotice tone="error" title={c.requestFailed}>{error}</InlineNotice> : null}
    <section className="panel regression-dataset-panel">
      <header><div><p className="panel-kicker">{c.datasets}</p><h2>{c.datasets}</h2></div><small>{c.datasetsHint}</small></header>
      {!activeDatasourceId ? <InlineNotice tone="warning" title={c.datasource}>{c.noDatasource}</InlineNotice> : null}
      <form className="regression-dataset-form" onSubmit={(event) => void saveDataset(event)}>
        <Field label={c.datasetId} htmlFor="test-dataset-id"><TextInput id="test-dataset-id" value={datasetId} disabled={Boolean(editingDataset)} required onChange={(event) => setDatasetId(event.target.value)} /></Field>
        <Field label={c.datasetVersion} htmlFor="test-dataset-version"><TextInput id="test-dataset-version" value={datasetVersion} required onChange={(event) => setDatasetVersion(event.target.value)} /></Field>
        <Field label={c.datasource}><TextInput value={activeDatasourceId || c.noDatasource} readOnly /></Field>
        <div className="regression-dataset-actions"><Button type="submit" variant="primary" size="sm" loading={saving} disabled={!activeDatasourceId || !datasetId.trim() || !datasetVersion.trim()}>{editingDataset ? c.updateDataset : c.register}</Button>{editingDataset ? <Button type="button" variant="ghost" size="sm" onClick={() => { setEditingDataset(null); setDatasetId(""); setDatasetVersion(""); }}>{c.cancelEdit}</Button> : null}</div>
      </form>
      {datasets.length ? <div className="regression-dataset-list">{datasets.map((dataset) => <article key={dataset.id}><span><strong>{dataset.id}</strong><small>{dataset.version} · {dataset.datasourceId}</small></span><Badge tone={dataset.datasourceId === activeDatasourceId ? "green" : "neutral"}>{dataset.datasourceId === activeDatasourceId ? c.matching : c.mismatch}</Badge><Button size="sm" variant="ghost" icon={Plus} disabled={dataset.datasourceId !== activeDatasourceId} onClick={() => editDataset(dataset)}>{c.edit}</Button></article>)}</div> : <p className="regression-muted">{c.noDatasets}</p>}
    </section>

    <section className="panel regression-cases-panel">
      <header><div><p className="panel-kicker">{c.selectCases}</p><h2>{c.title}</h2></div><div className="regression-selection-count"><small>{c.selected}: {selectedCaseIds.length}/20</small><Button size="sm" variant="primary" icon={Play} loading={saving} disabled={!selectedCaseIds.length || selectedCaseIds.length > 20} onClick={() => void runCases(selectedCaseIds)}>{c.runSelected}</Button></div></header>
      {loading && !cases.length ? <LoadingRows count={4} /> : cases.length ? <div className="regression-v2-list">{cases.map((item) => {
        const v2 = isV2Case(item);
        const runnable = runnableCaseIds.has(item.id);
        const question = v2 ? item.case.question : typeof item.case.question === "string" ? item.case.question : "—";
        return <article className="regression-v2-card" key={item.id}>
          <label className="regression-case-select"><input aria-label={`${c.selectCases} ${item.id}`} type="checkbox" checked={selectedCaseIds.includes(item.id)} disabled={!runnable || (!selectedCaseIds.includes(item.id) && selectedCaseIds.length >= 20)} onChange={(event) => setSelectedCaseIds((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))} /><span><Badge tone={statusTone(item.status)} dot>{localStatus(item.status, locale)}</Badge>{!v2 ? <Badge tone="neutral">{c.legacy}</Badge> : null}</span></label>
          <div className="regression-case-copy"><h3>{question}</h3><code>{item.id}</code><dl><div><dt>{c.feedbackId}</dt><dd>{item.feedbackId}</dd></div><div><dt>{c.sourceTraceId}</dt><dd>{item.sourceTraceId ?? "—"}</dd></div><div><dt>{c.revision}</dt><dd>{item.revision}</dd></div><div><dt>{c.datasetVersion}</dt><dd>{item.datasetVersion ?? (v2 ? item.case.testDatasetId : "—")}</dd></div></dl>
            {item.sourceTraceId && onOpenTrace ? <Button size="sm" variant="ghost" icon={LinkSimple} onClick={() => void openOriginalTrace(item.sourceTraceId!)}>{c.openOriginal}</Button> : null}
          </div>
          <div className="regression-case-actions">{v2 ? <><Button size="sm" variant="secondary" onClick={() => { setEditingCase(item); setCaseSeed(null); setShowCaseEditor(true); }}>{c.edit}</Button>{item.status === "enabled" && runnable ? <Button size="sm" variant="primary" icon={Play} loading={saving} onClick={() => void runCases([item.id])}>{c.run}</Button> : null}</> : <span className="regression-legacy-note">{c.legacyBody}</span>}</div>
        </article>;
      })}</div> : <EmptyState icon={Flask} title={c.noCases} body={c.noCasesBody} />}
    </section>

    <section className="regression-run-layout">
      <section className="panel regression-run-history"><header><div><p className="panel-kicker">{c.runHistory}</p><h2>{c.runHistory}</h2></div><Button size="sm" variant="ghost" icon={ArrowClockwise} loading={loading} onClick={() => void loadAll()}>{c.refresh}</Button></header>
        {runs.length ? <div className="regression-run-list">{runs.map((run) => <button className={selectedRun?.id === run.id ? "active" : ""} key={run.id} onClick={() => void openRun(run)}><span><Badge tone={statusTone(run.status)} dot>{localStatus(run.status, locale)}</Badge><small>{formatTime(run.createdAt, locale)}</small></span><code>{run.id}</code><small>{run.caseIds.length} cases · {run.subjectId}</small></button>)}</div> : <EmptyState icon={Play} title={c.noRuns} body={c.noRunsBody} />}
      </section>
      <section className="panel regression-run-detail"><header><div><p className="panel-kicker">{c.runStatus}</p><h2>{selectedRun?.id ?? c.runHistory}</h2></div>{selectedRun ? <div className="heading-actions"><Badge tone={statusTone(selectedRun.status)} dot>{localStatus(selectedRun.status, locale)}</Badge><Button size="sm" variant="secondary" icon={ArrowClockwise} loading={saving} onClick={() => void openRun(selectedRun)}>{c.refreshRun}</Button>{selectedRun.status === "queued" || selectedRun.status === "running" ? <Button size="sm" variant="danger" icon={Trash} loading={saving} onClick={() => void cancelRun()}>{c.cancelRun}</Button> : null}</div> : null}</header>
        {selectedRun ? <RunDetail run={selectedRun} locale={locale} onOpenIssue={onOpenIssue} onOpenTrace={(id) => void openOriginalTrace(id)} onInspectCore={inspectCore} /> : <EmptyState icon={CheckCircle} title={c.runHistory} body={c.noRunsBody} />}
        {coreDiagnostic ? <CoreDiagnosticPanel item={coreDiagnostic} locale={locale} onClose={() => setCoreDiagnostic(null)} /> : null}
        {selectedRun && !selectedRun.results.length ? <p className="regression-muted">{c.noResults}</p> : null}
      </section>
    </section>
    <CaseEditorModal open={showCaseEditor} record={editingCase} seed={caseSeed} datasets={matchedDatasets} activeDatasourceId={activeDatasourceId} locale={locale} busy={saving} onClose={() => { setShowCaseEditor(false); setEditingCase(null); setCaseSeed(null); }} onSave={async (payload) => {
      setSaving(true); setError("");
      try {
        if (editingCase) await api.updateRegressionCase(editingCase.id, { expectedRevision: editingCase.revision, case: payload.case, enable: payload.enable });
        else await api.createRegressionCaseV2({ feedbackId: payload.feedbackId, sourceTraceId: payload.sourceTraceId || undefined, case: payload.case, enable: false });
        setShowCaseEditor(false); setEditingCase(null); setCaseSeed(null); await loadAll();
      } catch (cause) { setError(cause instanceof Error ? cause.message : c.createFailed); }
      finally { setSaving(false); }
    }} />
  </div>;
}

interface CellDraft { row: string; column: string; type: "string" | "number" | "boolean" | "null"; expected: string; tolerance: string }
interface CaseFormValues {
  feedbackId: string; sourceTraceId: string; question: string; semanticSql: string; conditions: string; confirmedConditions: string;
  testDatasetId: string; status: "success" | "failure"; errorCode: string; reasonCode: string; phase: string; rowCount: string; cells: CellDraft[]; enable: boolean;
}

function makeCaseForm(record: RegressionCaseV2Record | null, seed: CaseSeed | null): CaseFormValues {
  const data = record && isV2Case(record) ? record.case : null;
  return {
    feedbackId: record?.feedbackId ?? seed?.feedbackId ?? "", sourceTraceId: record?.sourceTraceId ?? seed?.sourceTraceId ?? "",
    question: data?.question ?? "", semanticSql: data?.semanticSql ?? "", conditions: JSON.stringify(data?.conditions ?? {}, null, 2), confirmedConditions: JSON.stringify(data?.confirmedConditions ?? [], null, 2),
    testDatasetId: data?.testDatasetId ?? "", status: data?.assertions.status ?? "success", errorCode: data?.assertions.error?.code ?? "", reasonCode: data?.assertions.error?.reasonCode ?? "", phase: data?.assertions.error?.phase ?? "", rowCount: data?.assertions.rowCount === undefined ? "" : String(data.assertions.rowCount),
    cells: data?.assertions.cells?.map((cell) => ({ row: String(cell.row), column: cell.column, type: cell.expected === null ? "null" : typeof cell.expected === "boolean" ? "boolean" : typeof cell.expected === "number" ? "number" : "string", expected: cell.expected === null ? "" : String(cell.expected), tolerance: cell.tolerance === undefined ? "" : String(cell.tolerance) })) ?? [],
    enable: record?.status === "enabled",
  };
}

function CaseEditorModal({ open, record, seed, datasets, activeDatasourceId, locale, busy, onClose, onSave }: {
  open: boolean; record: RegressionCaseV2Record | null; seed: CaseSeed | null; datasets: TestDatasetRecord[]; activeDatasourceId: string; locale: Locale; busy: boolean; onClose: () => void;
  onSave: (payload: { feedbackId: string; sourceTraceId: string; case: RegressionCaseV2; enable: boolean }) => Promise<void>;
}) {
  const c = labels[locale];
  const [form, setForm] = useState(() => makeCaseForm(record, seed));
  const [formError, setFormError] = useState("");
  useEffect(() => { setForm(makeCaseForm(record, seed)); setFormError(""); }, [record, seed, open]);

  async function submit(event: FormEvent) {
    event.preventDefault(); setFormError("");
    try {
      const conditions = JSON.parse(form.conditions) as unknown;
      const confirmedConditions = JSON.parse(form.confirmedConditions) as unknown;
      if (!conditions || typeof conditions !== "object" || Array.isArray(conditions)) throw new Error("Conditions must be a JSON object.");
      if (!Array.isArray(confirmedConditions) || confirmedConditions.some((value) => typeof value !== "string")) throw new Error("Confirmed conditions must be a JSON array of strings.");
      if (form.rowCount.trim() && (!Number.isInteger(Number(form.rowCount)) || Number(form.rowCount) < 0)) throw new Error("Expected row count must be a non-negative integer.");
      const cells = form.cells.map((cell) => {
        const row = Number(cell.row);
        if (!Number.isInteger(row) || row < 0 || !cell.column.trim()) throw new Error("Each cell assertion needs a 0-based row index and column name.");
        let expected: string | number | boolean | null;
        if (cell.type === "null") expected = null;
        else if (cell.type === "number") {
          expected = Number(cell.expected);
          if (!cell.expected.trim() || !Number.isFinite(expected)) throw new Error("Numeric expected values must be finite numbers.");
        } else if (cell.type === "boolean") expected = cell.expected === "true";
        else expected = cell.expected;
        const tolerance = cell.tolerance.trim() ? Number(cell.tolerance) : undefined;
        if (tolerance !== undefined && (!Number.isFinite(tolerance) || tolerance < 0)) throw new Error("Numeric tolerance must be a non-negative number.");
        if (tolerance !== undefined && cell.type !== "number") throw new Error("Numeric tolerance can only be used for numeric expected values.");
        return { row, column: cell.column.trim(), expected, ...(tolerance === undefined ? {} : { tolerance }) };
      });
      const error = form.errorCode || form.reasonCode || form.phase ? { ...(form.errorCode ? { code: form.errorCode } : {}), ...(form.reasonCode ? { reasonCode: form.reasonCode } : {}), ...(form.phase ? { phase: form.phase } : {}) } : undefined;
      const testDatasetId = form.testDatasetId.trim();
      if (!datasets.some((dataset) => dataset.id === testDatasetId && dataset.datasourceId === activeDatasourceId)) throw new Error(c.datasetRequired);
      if (!form.feedbackId.trim() || !form.question.trim() || !form.semanticSql.trim()) throw new Error("Feedback ID, question, and semantic SQL are required.");
      const caseRecord: RegressionCaseV2 = {
        schemaVersion: 2, kind: "deterministic_sql", question: form.question.trim(), semanticSql: form.semanticSql.trim(),
        conditions: conditions as Record<string, unknown>, confirmedConditions: confirmedConditions as string[], testDatasetId,
        assertions: { status: form.status, ...(error ? { error } : {}), ...(form.rowCount.trim() ? { rowCount: Number(form.rowCount) } : {}), ...(cells.length ? { cells } : {}) },
      };
      await onSave({ feedbackId: form.feedbackId.trim(), sourceTraceId: form.sourceTraceId.trim(), case: caseRecord, enable: record ? form.enable : false });
    } catch (cause) { setFormError(cause instanceof Error ? cause.message : c.createFailed); }
  }

  const title = record ? `${c.edit} · ${record.id}` : c.newCase;
  return <Modal open={open} title={title} description={c.caseFormHint} onClose={onClose} footer={<><Button variant="ghost" onClick={onClose}>{locale === "zh-CN" ? "取消" : "Cancel"}</Button><Button type="submit" form="regression-case-form" variant="primary" loading={busy} disabled={!datasets.length}>{record ? c.saveChanges : c.create}</Button></>}>
    <form id="regression-case-form" className="regression-case-form" onSubmit={(event) => void submit(event)}>
      {!datasets.length ? <InlineNotice tone="warning" title={c.datasetId}>{c.noDatasetsForForm}</InlineNotice> : null}
      <div className="regression-case-form-grid">
        <Field label={c.feedbackId} htmlFor="regression-feedback-id" required><TextInput id="regression-feedback-id" value={form.feedbackId} onChange={(event) => setForm({ ...form, feedbackId: event.target.value })} /></Field>
        <Field label={c.sourceTraceId} htmlFor="regression-source-trace"><TextInput id="regression-source-trace" value={form.sourceTraceId} onChange={(event) => setForm({ ...form, sourceTraceId: event.target.value })} /></Field>
        <Field label={c.question} htmlFor="regression-question" required><TextArea id="regression-question" value={form.question} onChange={(event) => setForm({ ...form, question: event.target.value })} /></Field>
        <Field label={c.semanticSql} htmlFor="regression-semantic-sql" required><TextArea id="regression-semantic-sql" className="input textarea code-input" value={form.semanticSql} onChange={(event) => setForm({ ...form, semanticSql: event.target.value })} /></Field>
        <Field label={c.conditions} htmlFor="regression-conditions"><TextArea id="regression-conditions" className="input textarea code-input" value={form.conditions} onChange={(event) => setForm({ ...form, conditions: event.target.value })} /></Field>
        <Field label={c.confirmedConditions} htmlFor="regression-confirmed"><TextArea id="regression-confirmed" className="input textarea code-input" value={form.confirmedConditions} onChange={(event) => setForm({ ...form, confirmedConditions: event.target.value })} /></Field>
        <Field label={c.datasetId} htmlFor="regression-dataset"><Select id="regression-dataset" value={form.testDatasetId} onChange={(event) => setForm({ ...form, testDatasetId: event.target.value })}><option value="">{c.datasetRequired}</option>{datasets.filter((dataset) => dataset.datasourceId === activeDatasourceId).map((dataset) => <option key={dataset.id} value={dataset.id}>{dataset.id} · {dataset.version}</option>)}</Select></Field>
        <Field label={c.expectedStatus} htmlFor="regression-expected-status"><Select id="regression-expected-status" value={form.status} onChange={(event) => setForm({ ...form, status: event.target.value as CaseFormValues["status"] })}><option value="success">{c.expectedSuccess}</option><option value="failure">{c.expectedFailure}</option></Select></Field>
        <Field label={c.errorCode} htmlFor="regression-error-code"><TextInput id="regression-error-code" value={form.errorCode} onChange={(event) => setForm({ ...form, errorCode: event.target.value })} /></Field>
        <Field label={c.reasonCode} htmlFor="regression-reason-code"><TextInput id="regression-reason-code" value={form.reasonCode} onChange={(event) => setForm({ ...form, reasonCode: event.target.value })} /></Field>
        <Field label={c.phase} htmlFor="regression-phase"><TextInput id="regression-phase" value={form.phase} onChange={(event) => setForm({ ...form, phase: event.target.value })} /></Field>
        <Field label={c.rowCount} htmlFor="regression-row-count"><TextInput id="regression-row-count" inputMode="numeric" value={form.rowCount} onChange={(event) => setForm({ ...form, rowCount: event.target.value })} /></Field>
      </div>
      <div className="regression-cell-section"><header><div><strong>{c.cells}</strong>{form.cells.length ? <small>{c.orderByHint}</small> : null}</div><Button type="button" size="sm" variant="secondary" icon={Plus} onClick={() => setForm({ ...form, cells: [...form.cells, { row: "0", column: "", type: "string", expected: "", tolerance: "" }] })}>{c.addCell}</Button></header>
        {form.cells.map((cell, index) => <article className="regression-cell-row" key={index}>
          <Field label={c.rowIndex}><TextInput type="number" min="0" step="1" value={cell.row} onChange={(event) => setForm({ ...form, cells: form.cells.map((item, i) => i === index ? { ...item, row: event.target.value } : item) })} /></Field>
          <Field label={c.columnName}><TextInput value={cell.column} onChange={(event) => setForm({ ...form, cells: form.cells.map((item, i) => i === index ? { ...item, column: event.target.value } : item) })} /></Field>
          <Field label={c.scalarType}><Select value={cell.type} onChange={(event) => setForm({ ...form, cells: form.cells.map((item, i) => i === index ? { ...item, type: event.target.value as CellDraft["type"] } : item) })}><option value="string">{c.scalarString}</option><option value="number">{c.scalarNumber}</option><option value="boolean">{c.scalarBoolean}</option><option value="null">{c.scalarNull}</option></Select></Field>
          <Field label={c.expectedValue}>{cell.type === "boolean" ? <Select value={cell.expected} onChange={(event) => setForm({ ...form, cells: form.cells.map((item, i) => i === index ? { ...item, expected: event.target.value } : item) })}><option value="true">{c.yes}</option><option value="false">{c.no}</option></Select> : <TextInput disabled={cell.type === "null"} value={cell.expected} onChange={(event) => setForm({ ...form, cells: form.cells.map((item, i) => i === index ? { ...item, expected: event.target.value } : item) })} />}</Field>
          <Field label={c.tolerance}><TextInput inputMode="decimal" disabled={cell.type !== "number"} value={cell.tolerance} onChange={(event) => setForm({ ...form, cells: form.cells.map((item, i) => i === index ? { ...item, tolerance: event.target.value } : item) })} /></Field>
          <Button type="button" variant="ghost" size="sm" icon={Trash} aria-label={`${c.removeCell} ${index + 1}`} onClick={() => setForm({ ...form, cells: form.cells.filter((_, i) => i !== index) })}>{c.removeCell}</Button>
        </article>)}
      </div>
      {record ? <label className="regression-enable"><input type="checkbox" checked={form.enable} onChange={(event) => setForm({ ...form, enable: event.target.checked })} />{c.enableAfterSave}</label> : <p className="regression-muted">{c.draftOnly}</p>}
      {formError ? <InlineNotice tone="error" title={c.createFailed}>{formError}</InlineNotice> : null}
    </form>
  </Modal>;
}

function RunDetail({ run, locale, onOpenIssue, onOpenTrace, onInspectCore }: { run: RegressionRun; locale: Locale; onOpenIssue?: (id: string) => void; onOpenTrace?: (id: string) => void; onInspectCore: (id: string) => void }) {
  const c = labels[locale];
  return <div className="regression-run-content">
    <dl className="regression-run-meta"><div><dt>{c.runId}</dt><dd>{run.id}</dd></div><div><dt>{c.subject}</dt><dd>{run.subjectId}</dd></div><div><dt>{c.updated}</dt><dd>{formatTime(run.updatedAt, locale)}</dd></div><div><dt>{c.coreExpires}</dt><dd>{formatTime(run.expiresAt, locale)}</dd></div></dl>
    {run.results.length ? <div className="regression-result-list">{run.results.map((result) => <RunResult key={`${result.caseId}:${result.caseRevision}`} result={result} locale={locale} onOpenIssue={onOpenIssue} onOpenTrace={onOpenTrace} onInspectCore={onInspectCore} />)}</div> : null}
  </div>;
}

function RunResult({ result, locale, onOpenIssue, onOpenTrace, onInspectCore }: { result: RegressionRunResult; locale: Locale; onOpenIssue?: (id: string) => void; onOpenTrace?: (id: string) => void; onInspectCore: (id: string) => void }) {
  const c = labels[locale];
  return <article className="regression-run-result">
    <header><div><p className="panel-kicker">{c.caseId} {result.caseId} · {c.revision} {result.caseRevision}</p><Badge tone={statusTone(result.status)} dot>{localStatus(result.status, locale)}</Badge></div><span>{result.durationMs === null ? c.notCollected : `${result.durationMs.toFixed(1)} ms`}</span></header>
    <dl><div><dt>{c.reason}</dt><dd>{result.reasonCode ?? "—"}</dd></div><div><dt>{c.queryId}</dt><dd>{result.queryId ?? "—"}</dd></div><div><dt>{c.datasource}</dt><dd>{result.environment.datasourceId}</dd></div><div><dt>{c.datasetVersionResult}</dt><dd>{result.environment.datasetVersion}</dd></div><div><dt>{c.semanticVersion}</dt><dd>{result.environment.semanticVersion ?? c.notCollected}</dd></div><div><dt>{c.policyVersions}</dt><dd>{result.environment.policyVersions?.join(", ") || c.notCollected}</dd></div></dl>
    {result.assertions.length ? <div className="regression-assertions"><strong>{c.assertionsResult}</strong>{result.assertions.map((assertion, index) => <span key={`${assertion.kind}:${index}`}><Badge tone={statusTone(assertion.status)}>{localStatus(assertion.status, locale)}</Badge><code>{assertion.kind}</code><small>{assertion.reasonCode}</small></span>)}</div> : null}
    <div className="regression-result-links"><span><strong>{c.coreTraces}:</strong>{result.coreTraceIds.length ? result.coreTraceIds.map((id) => <Button key={id} size="sm" variant="ghost" icon={LinkSimple} onClick={() => onInspectCore(id)}>{id}</Button>) : <small>—</small>}</span><div>
      {onOpenIssue ? <Button size="sm" variant="secondary" onClick={() => onOpenIssue(result.feedbackId)}>{c.sourceIssue}</Button> : <small>{c.noIssue}</small>}
      {result.sourceTraceId && onOpenTrace ? <Button size="sm" variant="secondary" onClick={() => onOpenTrace(result.sourceTraceId!)}>{c.openOriginal}</Button> : <small>{c.noTrace}</small>}
    </div></div>
  </article>;
}

function CoreDiagnosticPanel({ item, locale, onClose }: { item: CoreDiagnosticRecord; locale: Locale; onClose: () => void }) {
  const c = labels[locale];
  return <section className="regression-core-diagnostic"><header><div><p className="panel-kicker">{c.coreDiagnostic}</p><h3>{item.traceId}</h3></div><Button size="sm" variant="ghost" onClick={onClose}>{locale === "zh-CN" ? "关闭" : "Close"}</Button></header><Badge tone={statusTone(item.status)}>{localStatus(item.status, locale)}</Badge><dl><div><dt>{c.method}</dt><dd>{item.method}</dd></div><div><dt>{c.stage}</dt><dd>{item.stage}</dd></div><div><dt>{c.duration}</dt><dd>{item.durationMs.toFixed(1)} ms</dd></div><div><dt>{c.errorCode}</dt><dd>{item.code ?? "—"}</dd></div><div><dt>{c.reasonCode}</dt><dd>{item.reasonCode ?? "—"}</dd></div><div><dt>{c.phase}</dt><dd>{item.phase ?? "—"}</dd></div><div><dt>{c.semanticVersion}</dt><dd>{item.semanticVersion ?? c.notCollected}</dd></div><div><dt>{c.policyVersions}</dt><dd>{item.policyVersions.join(", ") || c.notCollected}</dd></div><div><dt>{c.feedbackId}</dt><dd>{item.issueIds.join(", ") || "—"}</dd></div><div><dt>{c.coreExpires}</dt><dd>{formatTime(item.expiresAt, locale)}</dd></div></dl><div className="trace-core-phases">{item.phaseSpans.map((phase, index) => <span key={`${phase.name}:${index}`}>{phase.name}: {phase.durationMs.toFixed(1)} ms · {localStatus(phase.status, locale)}</span>)}</div></section>;
}
