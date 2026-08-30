import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import {
  ArrowClockwise,
  Check,
  CheckCircle,
  CircleNotch,
  ClipboardText,
  Clock,
  Code,
  FileText,
  Info,
  MagnifyingGlass,
  NotePencil,
  Play,
  PlusCircle,
  SealCheck,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import type { KnowledgeWorkbenchLocale } from "./RuleWorkbench";
import "./knowledge-workbench.css";

export type SqlKnowledgeStatus = "pending" | "approved" | "rejected";
export type SqlValidationStatus = "not-run" | "running" | "passed" | "failed";

/** Safe query execution facts shown to reviewers. Do not place credentials here. */
export interface SqlQueryStats {
  durationMs?: number;
  rowCount?: number;
  datasource?: string;
  dialect?: string;
  modelNames?: string[];
  fields?: string[];
}

/** A historical SQL example that was actually referenced by the current query. */
export interface SqlHistoryReference {
  id: string;
  question: string;
  sql: string;
  status?: "pending" | "approved" | "rejected";
  sourcePath?: string;
  score?: number;
}

/** Validation state returned by the SQL adapter. */
export interface SqlValidation {
  status: SqlValidationStatus;
  message?: string;
  checkedAt?: string;
}

/** A candidate that can become a knowledge/sql Markdown record after review. */
export interface SqlKnowledgeCandidate {
  id: string;
  question: string;
  sql: string;
  queryId?: string;
  status: SqlKnowledgeStatus;
  stats?: SqlQueryStats;
  sqlHistory?: SqlHistoryReference[];
  sourcePath?: string;
  sessionId?: string;
  submittedAt?: string;
  reviewedAt?: string;
  reviewer?: string;
  reviewNote?: string;
  validation?: SqlValidation;
}

/** A live Harness query preview shown before a user submits it for review. */
export interface SqlQueryCapture {
  queryId: string;
  question: string;
  sql: string;
  stats?: SqlQueryStats;
  sqlHistory?: SqlHistoryReference[];
  sourcePath?: string;
  sessionId?: string;
  recorded?: boolean;
}

export interface SqlKnowledgeWorkbenchProps {
  candidates: SqlKnowledgeCandidate[];
  activeQuery?: SqlQueryCapture | null;
  locale?: KnowledgeWorkbenchLocale;
  loading?: boolean;
  error?: string | null;
  readOnly?: boolean;
  onRetry?: () => void;
  onSelectCandidate?: (candidate: SqlKnowledgeCandidate) => void;
  onValidate?: (candidate: SqlKnowledgeCandidate) => SqlValidation | void | Promise<SqlValidation | void>;
  onApprove?: (candidate: SqlKnowledgeCandidate, sql: string) => void | Promise<void>;
  onReject?: (candidate: SqlKnowledgeCandidate, note: string) => void | Promise<void>;
  onSaveSql?: (candidate: SqlKnowledgeCandidate, sql: string) => void | Promise<void>;
  onRecordQuery?: (query: SqlQueryCapture) => void | Promise<void>;
}

type SqlCopy = {
  eyebrow: string;
  title: string;
  description: string;
  queueTitle: string;
  queueDescription: string;
  pending: string;
  approved: string;
  rejected: string;
  search: string;
  noCandidates: string;
  noCandidatesBody: string;
  noMatches: string;
  noMatchesBody: string;
  loading: string;
  errorTitle: string;
  retry: string;
  readOnly: string;
  currentQuery: string;
  currentQueryHint: string;
  queryId: string;
  recordForReview: string;
  submitting: string;
  submitted: string;
  reviewGate: string;
  detail: string;
  sql: string;
  question: string;
  source: string;
  sourcePath: string;
  session: string;
  datasource: string;
  dialect: string;
  models: string;
  fields: string;
  duration: string;
  rows: string;
  submittedAt: string;
  reviewer: string;
  validate: string;
  validating: string;
  validationNotRun: string;
  validationPassed: string;
  validationFailed: string;
  validationRunning: string;
  approve: string;
  reject: string;
  confirmReject: string;
  cancel: string;
  rejectionReason: string;
  rejectionReasonHint: string;
  editSql: string;
  saveSql: string;
  discard: string;
  copy: string;
  copied: string;
  history: string;
  historyUsed: (count: number) => string;
  noHistory: string;
  noHistoryBody: string;
  historyScore: string;
  statusPending: string;
  statusApproved: string;
  statusRejected: string;
  reviewNote: string;
  notAvailable: string;
  passedBeforeApprove: string;
  approvedNotice: string;
  rejectedNotice: string;
};

const sqlCopy: Record<KnowledgeWorkbenchLocale, SqlCopy> = {
  "en-US": {
    eyebrow: "Semantic layer",
    title: "SQL knowledge",
    description: "Turn trusted query examples into reusable guidance only after a reviewer checks the question, SQL, and evidence.",
    queueTitle: "Review queue",
    queueDescription: "Candidate SQL stays out of SemaRail knowledge until it is approved.",
    pending: "Pending",
    approved: "Approved",
    rejected: "Rejected",
    search: "Search questions",
    noCandidates: "No SQL candidates",
    noCandidatesBody: "A query can be submitted from the result panel when it is useful to the team.",
    noMatches: "No matching candidates",
    noMatchesBody: "Try a different question or query identifier.",
    loading: "Loading SQL knowledge",
    errorTitle: "SQL knowledge could not be loaded",
    retry: "Try again",
    readOnly: "Read only",
    currentQuery: "Current query",
    currentQueryHint: "Review the generated SQL and any examples actually used before submitting it for review.",
    queryId: "Query ID",
    recordForReview: "Record for review",
    submitting: "Submitting",
    submitted: "Awaiting review",
    reviewGate: "Recording creates a candidate. It does not publish knowledge.",
    detail: "Candidate detail",
    sql: "SQL",
    question: "Question",
    source: "Source",
    sourcePath: "Source file",
    session: "Session",
    datasource: "Data source",
    dialect: "Dialect",
    models: "Models",
    fields: "Fields",
    duration: "Duration",
    rows: "Rows returned",
    submittedAt: "Submitted",
    reviewer: "Reviewer",
    validate: "Validate SQL",
    validating: "Validating",
    validationNotRun: "Validation has not run",
    validationPassed: "Validation passed",
    validationFailed: "Validation failed",
    validationRunning: "Validation in progress",
    approve: "Approve",
    reject: "Reject",
    confirmReject: "Confirm rejection",
    cancel: "Cancel",
    rejectionReason: "Review note",
    rejectionReasonHint: "Explain what should change before this example is reconsidered.",
    editSql: "Edit SQL",
    saveSql: "Save SQL",
    discard: "Discard",
    copy: "Copy SQL",
    copied: "Copied",
    history: "History used",
    historyUsed: (count) => `${count} referenced ${count === 1 ? "example" : "examples"}`,
    noHistory: "No historical SQL was used",
    noHistoryBody: "This query was generated without a recorded SQL example in its context.",
    historyScore: "match",
    statusPending: "Pending review",
    statusApproved: "Approved",
    statusRejected: "Rejected",
    reviewNote: "Review note",
    notAvailable: "Not available",
    passedBeforeApprove: "Validate this SQL successfully before approving it.",
    approvedNotice: "The SQL example was approved for the knowledge library.",
    rejectedNotice: "The SQL example was rejected and remains outside the knowledge library.",
  },
  "zh-CN": {
    eyebrow: "语义层",
    title: "SQL 知识库",
    description: "只有在审核问题、SQL 和证据后，才将可信的查询案例沉淀为可复用知识。",
    queueTitle: "审核队列",
    queueDescription: "候选 SQL 在批准前不会进入 SemaRail 知识库。",
    pending: "待审核",
    approved: "已批准",
    rejected: "已拒绝",
    search: "搜索问题",
    noCandidates: "暂无 SQL 候选",
    noCandidatesBody: "在查询结果面板中提交有复用价值的查询后，候选会显示在这里。",
    noMatches: "没有匹配的候选",
    noMatchesBody: "请尝试其他问题或查询 ID。",
    loading: "正在加载 SQL 知识",
    errorTitle: "SQL 知识加载失败",
    retry: "重试",
    readOnly: "只读",
    currentQuery: "当前查询",
    currentQueryHint: "提交审核前，请检查生成的 SQL 以及实际使用的历史案例。",
    queryId: "查询 ID",
    recordForReview: "记录并提交审核",
    submitting: "提交中",
    submitted: "等待审核",
    reviewGate: "记录只会创建候选，不会直接发布知识。",
    detail: "候选详情",
    sql: "SQL",
    question: "问题",
    source: "来源",
    sourcePath: "源文件",
    session: "会话",
    datasource: "数据源",
    dialect: "方言",
    models: "模型",
    fields: "字段",
    duration: "耗时",
    rows: "返回行数",
    submittedAt: "提交时间",
    reviewer: "审核人",
    validate: "校验 SQL",
    validating: "校验中",
    validationNotRun: "尚未校验",
    validationPassed: "校验通过",
    validationFailed: "校验失败",
    validationRunning: "正在校验",
    approve: "批准",
    reject: "拒绝",
    confirmReject: "确认拒绝",
    cancel: "取消",
    rejectionReason: "审核备注",
    rejectionReasonHint: "说明再次提交前需要修改的内容。",
    editSql: "编辑 SQL",
    saveSql: "保存 SQL",
    discard: "放弃",
    copy: "复制 SQL",
    copied: "已复制",
    history: "使用的历史 SQL",
    historyUsed: (count) => `引用了 ${count} 条历史案例`,
    noHistory: "本次未使用历史 SQL",
    noHistoryBody: "本次查询上下文中没有记录过的历史 SQL 案例。",
    historyScore: "匹配",
    statusPending: "待审核",
    statusApproved: "已批准",
    statusRejected: "已拒绝",
    reviewNote: "审核备注",
    notAvailable: "暂无",
    passedBeforeApprove: "请先成功校验 SQL，再进行批准。",
    approvedNotice: "SQL 案例已批准，可进入知识库。",
    rejectedNotice: "SQL 案例已拒绝，仍不会进入知识库。",
  },
};

function formatSqlDate(value: string | undefined, locale: KnowledgeWorkbenchLocale) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
}

function statusText(candidate: SqlKnowledgeCandidate, c: SqlCopy) {
  return candidate.status === "approved" ? c.statusApproved : candidate.status === "rejected" ? c.statusRejected : c.statusPending;
}

function historyStatusText(status: NonNullable<SqlHistoryReference["status"]>, c: SqlCopy) {
  return status === "approved" ? c.statusApproved : status === "rejected" ? c.statusRejected : c.statusPending;
}

function statusClass(status: SqlKnowledgeStatus) {
  return status === "approved" ? "kw-status-enabled" : status === "rejected" ? "kw-status-disabled kw-sql-status-rejected" : "kw-draft-state-dirty";
}

function validationCopy(validation: SqlValidation | undefined, c: SqlCopy) {
  if (!validation || validation.status === "not-run") return { label: c.validationNotRun, className: "kw-sql-validation-pending" };
  if (validation.status === "running") return { label: c.validationRunning, className: "kw-sql-validation-pending" };
  if (validation.status === "passed") return { label: validation.message || c.validationPassed, className: "kw-sql-validation-passed" };
  return { label: validation.message || c.validationFailed, className: "kw-sql-validation-failed" };
}

function safeStats(candidate: SqlKnowledgeCandidate | SqlQueryCapture, c: SqlCopy) {
  const stats = candidate.stats;
  return [
    [c.queryId, candidate.queryId],
    [c.sourcePath, candidate.sourcePath],
    [c.session, candidate.sessionId],
    [c.datasource, stats?.datasource],
    [c.dialect, stats?.dialect],
    [c.models, stats?.modelNames?.join(", ")],
    [c.fields, stats?.fields?.join(", ")],
    [c.duration, stats?.durationMs === undefined ? undefined : `${stats.durationMs} ms`],
    [c.rows, stats?.rowCount === undefined ? undefined : String(stats.rowCount)],
  ] as const;
}

function SqlHistory({ history, c }: { history: SqlHistoryReference[]; c: SqlCopy }) {
  const [open, setOpen] = useState(false);
  return <section className="kw-sql-history" aria-label={c.history}>
    <button type="button" className="kw-sql-history-toggle" aria-expanded={open} onClick={() => setOpen((current) => !current)}><span><FileText size={16} aria-hidden="true" /><strong>{c.history}</strong><small>{history.length ? c.historyUsed(history.length) : c.noHistory}</small></span><span aria-hidden="true">{open ? "−" : "+"}</span></button>
    {open ? history.length ? <div className="kw-sql-history-list">{history.map((item) => <details className="kw-sql-history-item" key={item.id}><summary><span>{item.question}</span>{item.status ? <span className={`kw-status-badge ${statusClass(item.status)}`}>{historyStatusText(item.status, c)}</span> : null}</summary><div className="kw-sql-history-content"><p>{item.sourcePath || c.source}: {item.score === undefined ? c.notAvailable : `${Math.round(item.score * 100)}% ${c.historyScore}`}</p><code>{item.sql}</code></div></details>)}</div> : <div className="kw-sql-no-history"><Info size={16} aria-hidden="true" /><span><strong>{c.noHistory}</strong><br />{c.noHistoryBody}</span></div> : null}
  </section>;
}

function SqlStats({ candidate, c }: { candidate: SqlKnowledgeCandidate | SqlQueryCapture; c: SqlCopy }) {
  return <dl className="kw-sql-metadata">{safeStats(candidate, c).map(([label, value]) => { const displayValue = value === undefined || value === "" ? c.notAvailable : value; return <div className="kw-sql-meta-cell" key={label}><dt>{label}</dt><dd title={displayValue}>{displayValue}</dd></div>; })}</dl>;
}

function SqlCodeBlock({ sql, c, className = "" }: { sql: string; c: SqlCopy; className?: string }) {
  const [copied, setCopied] = useState(false);
  async function copySql() {
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(sql);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  }
  return <div className="kw-sql-code-wrap"><pre className={`kw-sql-code ${className}`}><code>{sql}</code></pre><div className="kw-sql-code-actions"><button type="button" className="kw-sql-copy" onClick={() => void copySql()}><ClipboardText size={13} />{copied ? c.copied : c.copy}</button></div></div>;
}

function QueryCaptureCard({ query, c, readOnly, onRecordQuery }: { query: SqlQueryCapture; c: SqlCopy; readOnly: boolean; onRecordQuery?: (query: SqlQueryCapture) => void | Promise<void> }) {
  const [state, setState] = useState<"idle" | "submitting" | "submitted">(query.recorded ? "submitted" : "idle");
  useEffect(() => { if (query.recorded) setState("submitted"); }, [query.recorded]);
  async function record() {
    if (!onRecordQuery || readOnly || state !== "idle") return;
    setState("submitting");
    try {
      await onRecordQuery(query);
      setState("submitted");
    } catch {
      setState("idle");
    }
  }
  const history = query.sqlHistory ?? [];
  return <section className="kw-sql-record-panel" aria-label={c.currentQuery}><div className="kw-sql-record-heading"><div><h2>{c.currentQuery}</h2><p>{c.currentQueryHint}</p></div><span className="kw-readonly-badge"><Code size={14} />{c.queryId}: <code>{query.queryId}</code></span></div><div><div className="kw-sql-section-heading"><h3>{c.question}</h3></div><p className="kw-sql-question-copy">{query.question}</p></div><div><div className="kw-sql-section-heading"><h3>{c.sql}</h3></div><SqlCodeBlock sql={query.sql} c={c} className="kw-sql-record-code" /></div><SqlStats candidate={query} c={c} /><SqlHistory history={history} c={c} /><div className="kw-sql-record-footer"><small>{c.reviewGate}</small><button type="button" className="kw-sql-record-action" onClick={() => void record()} disabled={readOnly || !onRecordQuery || state !== "idle"}>{state === "submitting" ? <CircleNotch className="spin" size={15} /> : state === "submitted" ? <Check size={15} /> : <PlusCircle size={15} />}{state === "submitting" ? c.submitting : state === "submitted" ? c.submitted : c.recordForReview}</button></div></section>;
}

function SqlLoadingState({ label }: { label: string }) {
  return <div className="kw-loading-state" role="status" aria-label={label}><span className="kw-skeleton kw-skeleton-title" /><span className="kw-skeleton kw-skeleton-line" /><span className="kw-skeleton kw-skeleton-line kw-skeleton-short" /><span className="kw-skeleton kw-skeleton-editor" /></div>;
}

function SqlEmptyState({ title, body }: { title: string; body: string }) {
  return <div className="kw-empty-state"><span className="kw-empty-icon"><Code size={22} weight="duotone" /></span><h3>{title}</h3><p>{body}</p></div>;
}

/**
 * Review-first SQL knowledge workspace. Candidate records remain separate from
 * approved Wren knowledge until an explicit validation and approval action.
 */
export function SqlKnowledgeWorkbench({
  candidates,
  activeQuery,
  locale = "en-US",
  loading = false,
  error = null,
  readOnly = false,
  onRetry,
  onSelectCandidate,
  onValidate,
  onApprove,
  onReject,
  onSaveSql,
  onRecordQuery,
}: SqlKnowledgeWorkbenchProps) {
  const c = sqlCopy[locale];
  const [status, setStatus] = useState<SqlKnowledgeStatus>("pending");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState(candidates.find((candidate) => candidate.status === "pending")?.id ?? candidates[0]?.id ?? "");
  const [validationMap, setValidationMap] = useState<Record<string, SqlValidation>>({});
  const [localStatusMap, setLocalStatusMap] = useState<Record<string, SqlKnowledgeStatus>>({});
  const [localSqlMap, setLocalSqlMap] = useState<Record<string, string>>({});
  const [editingSql, setEditingSql] = useState(false);
  const [editSql, setEditSql] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [rejectNote, setRejectNote] = useState("");
  const [busy, setBusy] = useState<"validate" | "approve" | "reject" | "save" | null>(null);
  const [notice, setNotice] = useState<"approved" | "rejected" | null>(null);
  const [operationError, setOperationError] = useState("");
  const statusTabRefs = useRef<Record<SqlKnowledgeStatus, HTMLButtonElement | null>>({ pending: null, approved: null, rejected: null });

  useEffect(() => {
    if (!candidates.some((candidate) => candidate.id === selectedId)) setSelectedId(candidates.find((candidate) => candidate.status === "pending")?.id ?? candidates[0]?.id ?? "");
    setValidationMap((current) => {
      let changed = false;
      const next = { ...current };
      for (const candidate of candidates) if (!next[candidate.id] && candidate.validation) { next[candidate.id] = candidate.validation; changed = true; }
      return changed ? next : current;
    });
  }, [candidates, selectedId]);

  const statusOf = (candidate: SqlKnowledgeCandidate) => localStatusMap[candidate.id] ?? candidate.status;
  const visibleCandidates = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase();
    return candidates.filter((candidate) => {
      if (statusOf(candidate) !== status) return false;
      if (!normalized) return true;
      return `${candidate.question} ${candidate.queryId ?? ""} ${candidate.sql}`.toLocaleLowerCase().includes(normalized);
    });
  }, [candidates, localStatusMap, search, status]);
  const selectedCandidate = visibleCandidates.find((candidate) => candidate.id === selectedId) ?? visibleCandidates[0];
  const selectedValidation = selectedCandidate ? validationMap[selectedCandidate.id] ?? selectedCandidate.validation : undefined;
  const selectedSql = selectedCandidate ? localSqlMap[selectedCandidate.id] ?? selectedCandidate.sql : "";
  const validation = validationCopy(selectedValidation, c);
  const canApprove = selectedCandidate && statusOf(selectedCandidate) === "pending" && selectedValidation?.status === "passed";

  function selectCandidate(candidate: SqlKnowledgeCandidate) {
    setSelectedId(candidate.id);
    setEditingSql(false);
    setRejecting(false);
    setRejectNote("");
    setNotice(null);
    setOperationError("");
    onSelectCandidate?.(candidate);
  }

  function handleQueueKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const offset = event.key === "ArrowDown" ? 1 : -1;
    const next = visibleCandidates[(index + offset + visibleCandidates.length) % visibleCandidates.length];
    if (next) selectCandidate(next);
  }

  function handleStatusTabKey(event: KeyboardEvent<HTMLButtonElement>, current: SqlKnowledgeStatus) {
    const tabs: SqlKnowledgeStatus[] = ["pending", "approved", "rejected"];
    let next: SqlKnowledgeStatus | undefined;
    if (event.key === "Home") next = tabs[0];
    else if (event.key === "End") next = tabs[tabs.length - 1];
    else if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      const offset = event.key === "ArrowRight" ? 1 : -1;
      next = tabs[(tabs.indexOf(current) + offset + tabs.length) % tabs.length];
    }
    if (!next) return;
    event.preventDefault();
    setStatus(next);
    setNotice(null);
    statusTabRefs.current[next]?.focus();
  }

  async function validate() {
    if (!selectedCandidate || !onValidate || busy || readOnly) return;
    setBusy("validate");
    setValidationMap((current) => ({ ...current, [selectedCandidate.id]: { status: "running" } }));
    try {
      const result = await onValidate({ ...selectedCandidate, sql: selectedSql });
      setValidationMap((current) => ({ ...current, [selectedCandidate.id]: result ?? { status: "passed" } }));
    } catch (error: unknown) {
      setValidationMap((current) => ({ ...current, [selectedCandidate.id]: { status: "failed", message: error instanceof Error ? error.message : c.validationFailed } }));
    } finally {
      setBusy(null);
    }
  }

  async function approve() {
    if (!selectedCandidate || !onApprove || !canApprove || busy || readOnly) return;
    setBusy("approve");
    try {
      await onApprove({ ...selectedCandidate, sql: selectedSql }, selectedSql);
      setLocalStatusMap((current) => ({ ...current, [selectedCandidate.id]: "approved" }));
      setNotice("approved");
      setOperationError("");
    } catch (caught) {
      setOperationError(caught instanceof Error ? caught.message : c.errorTitle);
    } finally {
      setBusy(null);
    }
  }

  async function reject() {
    if (!selectedCandidate || !onReject || !rejectNote.trim() || busy || readOnly) return;
    setBusy("reject");
    try {
      await onReject({ ...selectedCandidate, sql: selectedSql }, rejectNote.trim());
      setLocalStatusMap((current) => ({ ...current, [selectedCandidate.id]: "rejected" }));
      setRejecting(false);
      setNotice("rejected");
      setOperationError("");
    } catch (caught) {
      setOperationError(caught instanceof Error ? caught.message : c.errorTitle);
    } finally {
      setBusy(null);
    }
  }

  async function saveSql() {
    if (!selectedCandidate || !onSaveSql || !editSql.trim() || busy || readOnly) return;
    setBusy("save");
    try {
      await onSaveSql({ ...selectedCandidate, sql: editSql }, editSql);
      setLocalSqlMap((current) => ({ ...current, [selectedCandidate.id]: editSql }));
      setEditingSql(false);
      setOperationError("");
    } catch (caught) {
      setOperationError(caught instanceof Error ? caught.message : c.errorTitle);
    } finally {
      setBusy(null);
    }
  }

  const statusTabs: Array<[SqlKnowledgeStatus, string]> = [["pending", c.pending], ["approved", c.approved], ["rejected", c.rejected]];
  const noticeText = notice === "approved" ? c.approvedNotice : notice === "rejected" ? c.rejectedNotice : "";

  return <div className="page kw-page kw-sql-page">
    <div className="kw-page-heading"><div><p className="kw-eyebrow">{c.eyebrow}</p><h1>{c.title}</h1><p>{c.description}</p></div>{readOnly ? <span className="kw-readonly-badge"><Code size={14} />{c.readOnly}</span> : null}</div>
    {activeQuery ? <QueryCaptureCard query={activeQuery} c={c} readOnly={readOnly} onRecordQuery={onRecordQuery} /> : null}
    {error || operationError ? <div className="kw-error" role="alert"><WarningCircle size={18} weight="fill" /><span>{operationError || error || c.errorTitle}</span>{error && onRetry ? <button type="button" className="kw-text-button" onClick={onRetry}><ArrowClockwise size={15} />{c.retry}</button> : null}</div> : null}
    {noticeText ? <div className="kw-notice" role="status"><CheckCircle size={18} weight="fill" /><span>{noticeText}</span><button type="button" className="kw-dismiss" onClick={() => setNotice(null)} aria-label={locale === "zh-CN" ? "关闭提示" : "Dismiss notice"}><X size={15} /></button></div> : null}
    {loading ? <SqlLoadingState label={c.loading} /> : <div className="kw-sql-workbench">
      <aside className="kw-sql-queue" aria-label={c.queueTitle}><div className="kw-sql-queue-header"><div><h2>{c.queueTitle}</h2><span className="kw-sql-count">{candidates.length}</span></div><p>{c.queueDescription}</p></div><div className="kw-sql-tabs" role="tablist" aria-label={c.queueTitle}>{statusTabs.map(([value, label]) => { const count = candidates.filter((candidate) => statusOf(candidate) === value).length; return <button type="button" role="tab" id={`kw-sql-tab-${value}`} key={value} aria-selected={status === value} aria-controls="kw-sql-queue-panel" tabIndex={status === value ? 0 : -1} ref={(element) => { statusTabRefs.current[value] = element; }} className={status === value ? "kw-sql-tab-active" : ""} onKeyDown={(event) => handleStatusTabKey(event, value)} onClick={() => { setStatus(value); setNotice(null); }}>{label}<span>{count}</span></button>; })}</div><label className="kw-search"><MagnifyingGlass size={16} aria-hidden="true" /><span className="sr-only">{c.search}</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={c.search} aria-label={c.search} /></label><div id="kw-sql-queue-panel" className="kw-sql-list" role="tabpanel" aria-labelledby={`kw-sql-tab-${status}`}><div role="list" aria-label={c.queueTitle}>{visibleCandidates.map((candidate, index) => { const active = candidate.id === selectedId; const candidateStatus = statusOf(candidate); return <div className={`kw-sql-list-row ${active ? "kw-sql-list-row-active" : ""}`} role="listitem" key={candidate.id}><button type="button" className="kw-sql-list-select" aria-current={active ? "true" : undefined} onClick={() => selectCandidate(candidate)} onKeyDown={(event) => handleQueueKey(event, index)}><strong>{candidate.question}</strong><span className="kw-sql-list-meta"><span>{candidate.queryId || candidate.id}</span><span>{formatSqlDate(candidate.submittedAt, locale)}</span></span></button><span className={`kw-status-badge ${statusClass(candidateStatus)}`}>{statusText({ ...candidate, status: candidateStatus }, c)}</span></div>; })}{visibleCandidates.length === 0 ? <SqlEmptyState title={candidates.length ? c.noMatches : c.noCandidates} body={candidates.length ? c.noMatchesBody : c.noCandidatesBody} /> : null}</div></div></aside>
      <section className="kw-sql-detail" aria-label={selectedCandidate ? `${c.detail}: ${selectedCandidate.question}` : c.detail}>
        {!selectedCandidate ? <SqlEmptyState title={c.noCandidates} body={c.noCandidatesBody} /> : <><div className="kw-sql-detail-header"><div className="kw-sql-detail-title"><span className="kw-panel-kicker">{c.detail}</span><h2>{selectedCandidate.question}</h2><p>{selectedCandidate.queryId ? `${c.queryId}: ${selectedCandidate.queryId}` : c.notAvailable}</p><span className={`kw-status-badge ${statusClass(statusOf(selectedCandidate))}`}>{statusText({ ...selectedCandidate, status: statusOf(selectedCandidate) }, c)}</span></div><div className="kw-sql-detail-actions">{statusOf(selectedCandidate) === "pending" ? <><button type="button" className="kw-sql-review-action" onClick={() => void validate()} disabled={readOnly || !onValidate || Boolean(busy)}>{busy === "validate" ? <CircleNotch className="spin" size={14} /> : <Play size={14} />}{busy === "validate" ? c.validating : c.validate}</button><button type="button" className="kw-sql-review-action kw-sql-review-action-primary" onClick={() => void approve()} disabled={readOnly || !onApprove || !canApprove || Boolean(busy)}><SealCheck size={14} />{c.approve}</button><button type="button" className="kw-sql-review-action kw-sql-review-action-danger" onClick={() => setRejecting(true)} disabled={readOnly || !onReject || Boolean(busy)}><WarningCircle size={14} />{c.reject}</button></> : null}</div></div><div className="kw-sql-detail-body"><section><div className="kw-sql-section-heading"><h3>{c.sql}</h3>{statusOf(selectedCandidate) === "pending" && onSaveSql && !editingSql ? <button type="button" className="kw-sql-edit-toggle" onClick={() => { setEditSql(selectedSql); setEditingSql(true); }} disabled={readOnly || Boolean(busy)}><NotePencil size={14} />{c.editSql}</button> : null}</div>{editingSql ? <><textarea className="kw-sql-edit-area" value={editSql} onChange={(event) => setEditSql(event.target.value)} aria-label={c.sql} disabled={readOnly || busy === "save"} /><div className="kw-sql-edit-actions"><button type="button" className="kw-sql-review-action" onClick={() => { setEditingSql(false); setEditSql(""); }} disabled={Boolean(busy)}>{c.discard}</button><button type="button" className="kw-sql-review-action kw-sql-review-action-primary" onClick={() => void saveSql()} disabled={readOnly || !editSql.trim() || Boolean(busy)}>{busy === "save" ? <CircleNotch className="spin" size={14} /> : <Check size={14} />}{c.saveSql}</button></div></> : <SqlCodeBlock sql={selectedSql} c={c} />}</section><section><div className="kw-sql-section-heading"><h3>{c.source}</h3><small>{selectedCandidate.submittedAt ? `${c.submittedAt}: ${formatSqlDate(selectedCandidate.submittedAt, locale)}` : c.notAvailable}</small></div><SqlStats candidate={selectedCandidate} c={c} /></section><div className={`kw-sql-validation ${validation.className}`} role="status"><CheckCircle size={16} weight="fill" aria-hidden="true" /><span>{validation.label}{selectedValidation?.message && selectedValidation.status !== "passed" ? `: ${selectedValidation.message}` : ""}</span></div><SqlHistory history={selectedCandidate.sqlHistory ?? []} c={c} />{rejecting ? <section className="kw-sql-review-note"><label htmlFor="kw-reject-note">{c.rejectionReason}</label><textarea id="kw-reject-note" value={rejectNote} onChange={(event) => setRejectNote(event.target.value)} placeholder={c.rejectionReasonHint} disabled={readOnly || busy === "reject"} /><div className="kw-sql-edit-actions"><button type="button" className="kw-sql-review-action" onClick={() => { setRejecting(false); setRejectNote(""); }} disabled={Boolean(busy)}>{c.cancel}</button><button type="button" className="kw-sql-review-action kw-sql-review-action-danger" onClick={() => void reject()} disabled={readOnly || !rejectNote.trim() || Boolean(busy)}>{busy === "reject" ? <CircleNotch className="spin" size={14} /> : <WarningCircle size={14} />}{c.confirmReject}</button></div></section> : null}{statusOf(selectedCandidate) === "pending" && !canApprove ? <div className="kw-sql-validation kw-sql-validation-pending"><Info size={16} aria-hidden="true" /><span>{c.passedBeforeApprove}</span></div> : null}{selectedCandidate.reviewNote ? <div className="kw-sql-review-note"><label>{c.reviewNote}</label><p>{selectedCandidate.reviewNote}</p></div> : null}</div></>}
      </section>
    </div>}
  </div>;
}

/** Compact alias for Harness result-tab integrations. */
export function SqlQueryKnowledgePanel(props: { query: SqlQueryCapture; locale?: KnowledgeWorkbenchLocale; readOnly?: boolean; onRecordQuery?: (query: SqlQueryCapture) => void | Promise<void> }) {
  const c = sqlCopy[props.locale ?? "en-US"];
  return <QueryCaptureCard query={props.query} c={c} readOnly={props.readOnly ?? false} onRecordQuery={props.onRecordQuery} />;
}

export default SqlKnowledgeWorkbench;
