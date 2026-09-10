import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { ArrowClockwise, Bug, CheckCircle, DownloadSimple, Flask, MagnifyingGlass, PaperPlaneTilt, Plus } from "@phosphor-icons/react";
import { api } from "../api/client";
import type { DiagnosticFeedback, FeedbackCategory, FeedbackStatus, RegressionCaseExport } from "../types";
import { Badge, Button, EmptyState, Field, InlineNotice, LoadingRows, Modal, SectionHeading, Select, TextArea, TextInput } from "./ui";
import "./diagnostics-workbench.css";

type Locale = "en-US" | "zh-CN";
type Mode = "diagnostics" | "regressionCases";

const categories: FeedbackCategory[] = ["ambiguity", "knowledge_gap", "agent_understanding", "sql_generation", "permission_configuration", "runtime_failure", "evaluation", "other"];
const statuses: FeedbackStatus[] = ["pending", "classified", "located", "fixed", "verified", "closed_no_fix"];

const labels = {
  "en-US": {
    eyebrow: "Quality loop", diagnostics: "Issues & feedback", diagnosticsBody: "Triage server-captured failures and explicit user feedback without mixing evidence into the audit log.", regressions: "Regression cases", regressionsBody: "Review draft and enabled cases. Exported JSON is versioned and does not execute against production.", locked: "Console administrator authentication required", lockedBody: "Unlock the Console with a project-scoped administrator credential to inspect diagnostic evidence.",
    submitTitle: "Report a problem with this query", submitBody: "The URL contains only an opaque query reference. Your authenticated identity is checked before the report is accepted.", category: "Category", description: "What went wrong?", expected: "Expected behavior", question: "Question (optional evidence)", semanticSql: "Semantic SQL (optional evidence)", submit: "Submit feedback", submitted: "Feedback submitted", retry: "Submission failed; retrying will reuse the same idempotency key.", shared: "If this login is a shared service account, the report is attributed to that service account, not to an inferred employee.",
    filters: "Filters", after: "From", before: "To", source: "Source", datasource: "Data source", reason: "Error reason", status: "Status", apply: "Apply filters", clear: "Clear", noIssues: "No matching issues", noIssuesBody: "No server-captured failure or user feedback matches these filters.", loadMore: "Next page", previous: "Previous page", detail: "Issue details", evidence: "Diagnostic evidence", history: "Processing history", update: "Save workflow", duplicate: "Duplicate of", originalQuery: "Original query", note: "Processing note", createCase: "Create regression case", caseKind: "Case kind", dataset: "Test dataset ID", assertion: "Expected error JSON", enable: "Enable after review", draftHint: "Cases without a reproducible dataset and assertion must remain drafts.", create: "Create case", export: "Export versioned JSON", noCases: "No regression cases", noCasesBody: "Create a reviewed case from an issue first.", client: "User supplied", server: "Server captured", purged: "Evidence content expired after the retention window.",
  },
  "zh-CN": {
    eyebrow: "质量闭环", diagnostics: "问题与反馈", diagnosticsBody: "统一处理服务端自动采集的失败和用户主动反馈，诊断证据不会混入审计日志。", regressions: "回归用例", regressionsBody: "审核草稿和已启用用例；导出的 JSON 带版本号，且不会自动在生产环境执行。", locked: "需要 Console 管理员认证", lockedBody: "请使用当前项目范围内的管理员凭证解锁 Console 后查看诊断证据。",
    submitTitle: "反馈本次查询的问题", submitBody: "链接只包含不透明查询标识；服务端会在提交前校验当前登录身份与查询归属。", category: "问题分类", description: "问题说明", expected: "预期行为", question: "原问题（可选证据）", semanticSql: "语义 SQL（可选证据）", submit: "提交反馈", submitted: "反馈已提交", retry: "提交失败；重试会复用同一个幂等键。", shared: "如果当前使用共享服务账号，反馈将归属该服务账号，不会推断或声称识别了具体员工。",
    filters: "筛选", after: "开始时间", before: "结束时间", source: "来源", datasource: "数据源", reason: "错误原因", status: "处理状态", apply: "应用筛选", clear: "清空", noIssues: "没有匹配的问题", noIssuesBody: "没有服务端自动失败或用户反馈符合当前筛选条件。", loadMore: "下一页", previous: "上一页", detail: "问题详情", evidence: "诊断证据", history: "处理历史", update: "保存处理结果", duplicate: "重复问题编号", originalQuery: "原始查询", note: "处理备注", createCase: "创建回归用例", caseKind: "用例类型", dataset: "测试数据集标识", assertion: "预期错误 JSON", enable: "审核后立即启用", draftHint: "缺少可复现数据集或断言的记录必须保留为草稿。", create: "创建用例", export: "导出版本化 JSON", noCases: "暂无回归用例", noCasesBody: "请先从问题详情创建并审核用例。", client: "用户提供", server: "服务端采集", purged: "诊断正文已按留存期限清理。",
  },
} as const;

const categoryLabels: Record<Locale, Record<FeedbackCategory, string>> = {
  "en-US": { ambiguity: "Requirement ambiguity", knowledge_gap: "Knowledge gap", agent_understanding: "Agent understanding", sql_generation: "SQL generation", permission_configuration: "Permission configuration", runtime_failure: "Runtime failure", evaluation: "Evaluation", other: "Other" },
  "zh-CN": { ambiguity: "需求歧义", knowledge_gap: "知识缺失", agent_understanding: "Agent 理解", sql_generation: "SQL 生成", permission_configuration: "权限配置", runtime_failure: "运行故障", evaluation: "评测问题", other: "其他" },
};
const statusLabels: Record<Locale, Record<FeedbackStatus, string>> = {
  "en-US": { pending: "Pending", classified: "Classified", located: "Located", fixed: "Fixed", verified: "Verified", closed_no_fix: "Closed · no fix" },
  "zh-CN": { pending: "待处理", classified: "已分类", located: "已定位", fixed: "已修复", verified: "已验证", closed_no_fix: "无需修复关闭" },
};

function formatTime(value: string, locale: Locale) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function reasonCode(item: DiagnosticFeedback) {
  const reason = item.evidence.error?.reasonCode;
  return typeof reason === "string" ? reason : "—";
}

function toIso(value: string) {
  return value ? new Date(value).toISOString() : "";
}

export default function DiagnosticsWorkbench({ locale, mode, authToken, adminToken, feedbackReference = "" }: { locale: Locale; mode: Mode; authToken: string; adminToken: string; feedbackReference?: string }) {
  const c = labels[locale];
  const [items, setItems] = useState<DiagnosticFeedback[]>([]);
  const [selected, setSelected] = useState<DiagnosticFeedback | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [cursor, setCursor] = useState("");
  const [cursorHistory, setCursorHistory] = useState<string[]>([]);
  const [filters, setFilters] = useState({ category: "", status: "", source: "", datasourceId: "", reasonCode: "", createdAfter: "", createdBefore: "" });
  const [cases, setCases] = useState<RegressionCaseExport | null>(null);

  const load = useCallback(async (pageCursor = "") => {
    if (!adminToken) return;
    setLoading(true); setError("");
    try {
      if (mode === "regressionCases") {
        setCases(await api.exportRegressionCases());
      } else {
        const response = await api.listDiagnosticFeedback({
          limit: 50, cursor: pageCursor, category: filters.category as FeedbackCategory | "", status: filters.status as FeedbackStatus | "",
          source: filters.source, datasourceId: filters.datasourceId, reasonCode: filters.reasonCode,
          createdAfter: toIso(filters.createdAfter), createdBefore: toIso(filters.createdBefore),
        });
        setItems(response.items); setNextCursor(response.nextCursor ?? null); setCursor(pageCursor);
        const first = response.items[0];
        setSelected(first ? await api.getDiagnosticFeedback(first.id) : null);
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Request failed"); }
    finally { setLoading(false); }
  }, [adminToken, filters, mode]);

  useEffect(() => { void load(); }, [adminToken, mode]);

  async function selectIssue(item: DiagnosticFeedback) {
    setError("");
    try { setSelected(await api.getDiagnosticFeedback(item.id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Request failed"); }
  }

  async function updateIssue(status: FeedbackStatus, category: FeedbackCategory, duplicateOf: string, note: string) {
    if (!selected) return;
    setLoading(true); setError("");
    try {
      const updated = await api.updateDiagnosticFeedback(selected.id, { status, category, duplicateOf: duplicateOf || undefined, note: note || undefined });
      setSelected(updated); setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Request failed"); }
    finally { setLoading(false); }
  }

  if (mode === "regressionCases") return <RegressionCases locale={locale} token={adminToken} data={cases} loading={loading} error={error} onReload={() => void load()} />;

  return <div className="page diagnostics-page">
    <SectionHeading eyebrow={c.eyebrow} title={c.diagnostics} description={c.diagnosticsBody} />
    {feedbackReference ? <FeedbackForm locale={locale} reference={feedbackReference} authenticated={Boolean(authToken)} /> : null}
    {!adminToken ? <InlineNotice tone="warning" title={c.locked}>{c.lockedBody}</InlineNotice> : <>
      {error ? <InlineNotice tone="error" title="Request failed">{error}</InlineNotice> : null}
      <section className="panel diagnostic-filters"><header><strong>{c.filters}</strong><Button size="sm" variant="ghost" icon={ArrowClockwise} onClick={() => void load(cursor)} loading={loading}>{locale === "zh-CN" ? "刷新" : "Refresh"}</Button></header><div>
        <Field label={c.after}><TextInput type="datetime-local" value={filters.createdAfter} onChange={(event) => setFilters({ ...filters, createdAfter: event.target.value })} /></Field>
        <Field label={c.before}><TextInput type="datetime-local" value={filters.createdBefore} onChange={(event) => setFilters({ ...filters, createdBefore: event.target.value })} /></Field>
        <Field label={c.source}><TextInput value={filters.source} placeholder="core-http / mcp" onChange={(event) => setFilters({ ...filters, source: event.target.value })} /></Field>
        <Field label={c.datasource}><TextInput value={filters.datasourceId} onChange={(event) => setFilters({ ...filters, datasourceId: event.target.value })} /></Field>
        <Field label={c.reason}><TextInput value={filters.reasonCode} placeholder="TABLE_PERMISSION_REQUIRED" onChange={(event) => setFilters({ ...filters, reasonCode: event.target.value })} /></Field>
        <Field label={c.status}><Select value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}><option value="">—</option>{statuses.map((status) => <option value={status} key={status}>{statusLabels[locale][status]}</option>)}</Select></Field>
        <Field label={c.category}><Select value={filters.category} onChange={(event) => setFilters({ ...filters, category: event.target.value })}><option value="">—</option>{categories.map((category) => <option value={category} key={category}>{categoryLabels[locale][category]}</option>)}</Select></Field>
      </div><footer><Button size="sm" variant="ghost" onClick={() => { setFilters({ category: "", status: "", source: "", datasourceId: "", reasonCode: "", createdAfter: "", createdBefore: "" }); setCursorHistory([]); }}>{c.clear}</Button><Button size="sm" variant="primary" icon={MagnifyingGlass} onClick={() => { setCursorHistory([]); void load(); }}>{c.apply}</Button></footer></section>
      <div className="diagnostic-layout">
        <section className="panel diagnostic-list">{loading && !items.length ? <LoadingRows count={6} /> : items.length ? items.map((item) => <button key={item.id} className={selected?.id === item.id ? "active" : ""} onClick={() => void selectIssue(item)}><span><Badge tone={item.status === "verified" ? "green" : item.status === "pending" ? "amber" : "blue"} dot>{statusLabels[locale][item.status]}</Badge><small>{item.evidence.source === "server" ? c.server : c.client}</small></span><strong>{item.description}</strong><code>{reasonCode(item)}</code><small>{formatTime(item.createdAt, locale)} · {item.transport} · {item.datasourceId ?? "—"}</small></button>) : <EmptyState icon={Bug} title={c.noIssues} body={c.noIssuesBody} />}
          <footer className="diagnostic-pagination"><Button size="sm" variant="ghost" disabled={!cursorHistory.length || loading} onClick={() => { const history = [...cursorHistory]; const previous = history.pop() ?? ""; setCursorHistory(history); void load(previous); }}>{c.previous}</Button><Button size="sm" variant="secondary" disabled={!nextCursor || loading} onClick={() => { setCursorHistory((history) => [...history, cursor]); void load(nextCursor ?? ""); }}>{c.loadMore}</Button></footer>
        </section>
        {selected ? <IssueDetail key={selected.id} locale={locale} item={selected} loading={loading} onUpdate={updateIssue} onCaseCreated={() => void load(cursor)} /> : <section className="panel"><EmptyState icon={Bug} title={c.detail} body={c.noIssuesBody} /></section>}
      </div>
    </>}
  </div>;
}

function FeedbackForm({ locale, reference, authenticated }: { locale: Locale; reference: string; authenticated: boolean }) {
  const c = labels[locale];
  const [category, setCategory] = useState<FeedbackCategory>("other");
  const [description, setDescription] = useState("");
  const [expectedBehavior, setExpectedBehavior] = useState("");
  const [question, setQuestion] = useState("");
  const [semanticSql, setSemanticSql] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const [state, setState] = useState<{ busy: boolean; result?: string; error?: string }>({ busy: false });
  async function submit(event: FormEvent) {
    event.preventDefault(); setState({ busy: true });
    try {
      const result = await api.submitFeedback({ reference, idempotencyKey, category, description, expectedBehavior: expectedBehavior || undefined, question: question || undefined, semanticSql: semanticSql || undefined });
      setState({ busy: false, result: result.feedbackId }); setDescription(""); setExpectedBehavior(""); setQuestion(""); setSemanticSql(""); setIdempotencyKey(crypto.randomUUID());
    } catch (cause) { setState({ busy: false, error: cause instanceof Error ? cause.message : "Request failed" }); }
  }
  return <section className="panel feedback-submit"><header><PaperPlaneTilt size={24} weight="duotone" /><div><h2>{c.submitTitle}</h2><p>{c.submitBody}</p></div></header>{!authenticated ? <InlineNotice tone="warning" title={c.locked}>{locale === "zh-CN" ? "请先点击右上角解锁并完成认证。" : "Unlock and authenticate from the top-right control first."}</InlineNotice> : <form onSubmit={(event) => void submit(event)}><div className="feedback-form-grid"><Field label={c.category} required><Select value={category} onChange={(event) => setCategory(event.target.value as FeedbackCategory)}>{categories.map((item) => <option value={item} key={item}>{categoryLabels[locale][item]}</option>)}</Select></Field><Field label={c.description} required><TextArea required maxLength={8000} value={description} onChange={(event) => setDescription(event.target.value)} /></Field><Field label={c.expected}><TextArea maxLength={8000} value={expectedBehavior} onChange={(event) => setExpectedBehavior(event.target.value)} /></Field><Field label={c.question}><TextArea value={question} onChange={(event) => setQuestion(event.target.value)} /></Field><Field label={c.semanticSql}><TextArea className="input textarea code-input" value={semanticSql} onChange={(event) => setSemanticSql(event.target.value)} /></Field></div><p className="feedback-attribution">{c.shared}</p><Button type="submit" variant="primary" icon={PaperPlaneTilt} loading={state.busy} disabled={!description.trim()}>{c.submit}</Button></form>}{state.result ? <InlineNotice tone="success" title={c.submitted}>{state.result}</InlineNotice> : null}{state.error ? <InlineNotice tone="error" title={c.retry}>{state.error}</InlineNotice> : null}</section>;
}

function IssueDetail({ locale, item, loading, onUpdate, onCaseCreated }: { locale: Locale; item: DiagnosticFeedback; loading: boolean; onUpdate: (status: FeedbackStatus, category: FeedbackCategory, duplicate: string, note: string) => Promise<void>; onCaseCreated: () => void }) {
  const c = labels[locale];
  const [status, setStatus] = useState(item.status); const [category, setCategory] = useState(item.category); const [duplicate, setDuplicate] = useState(item.duplicateOf ?? ""); const [note, setNote] = useState(""); const [showCase, setShowCase] = useState(false);
  return <section className="panel diagnostic-detail">
    <header><div><p className="panel-kicker">{c.detail}</p><h2>{item.description}</h2><code>{item.id}</code></div><Button size="sm" icon={Plus} onClick={() => setShowCase(true)}>{c.createCase}</Button></header>
    <dl className="diagnostic-meta">
      <div><dt>Trace</dt><dd><code>{item.traceId}</code></dd></div>
      <div><dt>Query</dt><dd><code>{item.queryId ?? "—"}</code></dd></div>
      <div><dt>{c.originalQuery}</dt><dd><code>{item.originalQueryId ?? "—"}</code></dd></div>
      <div><dt>{c.source}</dt><dd>{item.transport} · {item.method} · {item.stage}</dd></div>
      <div><dt>Duration</dt><dd>{Math.round(item.durationMs)} ms</dd></div>
      <div><dt>{c.datasource}</dt><dd><code>{item.datasourceId ?? "—"}</code></dd></div>
      <div><dt>Semantic version</dt><dd><code>{item.semanticVersion ?? "—"}</code></dd></div>
      <div><dt>Policy versions</dt><dd><code>{item.policyVersions?.join(", ") || "—"}</code></dd></div>
    </dl>
    <div className="diagnostic-workflow"><Field label={c.status}><Select value={status} onChange={(event) => setStatus(event.target.value as FeedbackStatus)}>{statuses.map((value) => <option value={value} key={value}>{statusLabels[locale][value]}</option>)}</Select></Field><Field label={c.category}><Select value={category} onChange={(event) => setCategory(event.target.value as FeedbackCategory)}>{categories.map((value) => <option value={value} key={value}>{categoryLabels[locale][value]}</option>)}</Select></Field><Field label={c.duplicate}><TextInput value={duplicate} onChange={(event) => setDuplicate(event.target.value)} /></Field><Field label={c.note}><TextArea value={note} onChange={(event) => setNote(event.target.value)} /></Field><Button size="sm" variant="primary" loading={loading} onClick={() => void onUpdate(status, category, duplicate, note)}>{c.update}</Button></div>
    <h3>{c.evidence}</h3>{item.evidence.contentPurgedAt ? <InlineNotice tone="warning" title={c.purged} /> : <div className="diagnostic-evidence"><Evidence label={c.question} value={item.evidence.question} /><Evidence label={c.semanticSql} value={item.evidence.semanticSql} code /><Evidence label="Native SQL" value={item.evidence.nativeSql} code /><Evidence label={c.reason} value={item.evidence.error ? JSON.stringify(item.evidence.error, null, 2) : null} code /></div>}
    <h3>{c.history}</h3><div className="diagnostic-history">{item.history?.length ? item.history.map((entry) => <div key={entry.id}><CheckCircle size={16} /><span><strong>{statusLabels[locale][entry.toStatus]}</strong><small>{entry.note || "—"} · {formatTime(entry.createdAt, locale)}</small></span></div>) : <small>—</small>}</div>
    <RegressionModal locale={locale} item={item} open={showCase} onClose={() => setShowCase(false)} onCreated={() => { setShowCase(false); onCaseCreated(); }} />
  </section>;
}

function Evidence({ label, value, code = false }: { label: string; value?: string | null; code?: boolean }) { return value ? <div><strong>{label}</strong>{code ? <pre>{value}</pre> : <p>{value}</p>}</div> : null; }

function RegressionModal({ locale, item, open, onClose, onCreated }: { locale: Locale; item: DiagnosticFeedback; open: boolean; onClose: () => void; onCreated: () => void }) {
  const c = labels[locale]; const [kind, setKind] = useState<"deterministic_sql" | "agent_evidence">("deterministic_sql"); const [question, setQuestion] = useState(item.evidence.question ?? ""); const [sql, setSql] = useState(item.evidence.semanticSql ?? ""); const [dataset, setDataset] = useState(""); const [assertion, setAssertion] = useState(() => JSON.stringify(item.evidence.error ? { reasonCode: item.evidence.error.reasonCode, resources: item.evidence.error.resources } : {}, null, 2)); const [enable, setEnable] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function create() { setBusy(true); setError(""); try { const expectedError = assertion.trim() ? JSON.parse(assertion) as Record<string, unknown> : undefined; await api.createRegressionCase(item.id, { enable, case: { kind, question, semanticSql: sql || undefined, testDatasetId: dataset || undefined, expectedError } }); onCreated(); } catch (cause) { setError(cause instanceof Error ? cause.message : "Request failed"); } finally { setBusy(false); } }
  return <Modal open={open} title={c.createCase} description={c.draftHint} onClose={onClose} footer={<><Button variant="ghost" onClick={onClose}>{locale === "zh-CN" ? "取消" : "Cancel"}</Button><Button variant="primary" loading={busy} onClick={() => void create()}>{c.create}</Button></>}><div className="regression-form"><Field label={c.caseKind}><Select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="deterministic_sql">Deterministic SQL</option><option value="agent_evidence">Agent evidence</option></Select></Field><Field label={c.question} required><TextArea value={question} onChange={(event) => setQuestion(event.target.value)} /></Field>{kind === "deterministic_sql" ? <Field label={c.semanticSql}><TextArea className="input textarea code-input" value={sql} onChange={(event) => setSql(event.target.value)} /></Field> : null}<Field label={c.dataset}><TextInput value={dataset} onChange={(event) => setDataset(event.target.value)} /></Field><Field label={c.assertion}><TextArea className="input textarea code-input" value={assertion} onChange={(event) => setAssertion(event.target.value)} /></Field><label className="regression-enable"><input type="checkbox" checked={enable} onChange={(event) => setEnable(event.target.checked)} />{c.enable}</label>{error ? <InlineNotice tone="error" title="Request failed">{error}</InlineNotice> : null}</div></Modal>;
}

function RegressionCases({ locale, token, data, loading, error, onReload }: { locale: Locale; token: string; data: RegressionCaseExport | null; loading: boolean; error: string; onReload: () => void }) {
  const c = labels[locale]; const cases = data?.cases ?? [];
  const download = useMemo(() => () => { if (!data) return; const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `semarail-regression-cases-v${data.schemaVersion}.json`; anchor.click(); URL.revokeObjectURL(url); }, [data]);
  return <div className="page regression-page"><SectionHeading eyebrow={c.eyebrow} title={c.regressions} description={c.regressionsBody} action={token ? <div className="heading-actions"><Button variant="secondary" icon={ArrowClockwise} loading={loading} onClick={onReload}>{locale === "zh-CN" ? "刷新" : "Refresh"}</Button><Button variant="primary" icon={DownloadSimple} disabled={!data} onClick={download}>{c.export}</Button></div> : null} />{!token ? <InlineNotice tone="warning" title={c.locked}>{c.lockedBody}</InlineNotice> : error ? <InlineNotice tone="error" title="Request failed">{error}</InlineNotice> : loading && !data ? <section className="panel"><LoadingRows count={5} /></section> : cases.length ? <section className="regression-grid">{cases.map((item) => <article className="panel regression-card" key={item.id}><header><Flask size={20} weight="duotone" /><Badge tone={item.status === "enabled" ? "green" : "amber"} dot>{item.status}</Badge></header><h2>{item.question}</h2><code>{item.id}</code><dl><div><dt>Kind</dt><dd>{item.kind}</dd></div><div><dt>{c.dataset}</dt><dd>{item.testDatasetId ?? "—"}</dd></div><div><dt>Feedback</dt><dd>{item.feedbackId}</dd></div></dl></article>)}</section> : <section className="panel"><EmptyState icon={Flask} title={c.noCases} body={c.noCasesBody} /></section>}</div>;
}
