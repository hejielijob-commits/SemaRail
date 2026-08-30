import { useEffect, useMemo, useRef, useState } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { sql } from "@codemirror/lang-sql";
import {
  ArrowClockwise,
  BracketsCurly,
  Check,
  CheckCircle,
  Code,
  Database,
  Eye,
  FloppyDisk,
  MagnifyingGlass,
  Plus,
  Table,
  Trash,
  TreeStructure,
  WarningCircle,
} from "@phosphor-icons/react";
import type {
  ProjectDiff,
  ViewDefinition,
  ViewPreviewResult,
  ViewSnapshot,
  ViewValidationResponse,
  ViewWritePayload,
} from "../types";
import { Badge, Button, EmptyState, Field, InlineNotice, LoadingRows, Modal, Select, TextArea, TextInput } from "./ui";
import "./view-workbench.css";

type Locale = "en-US" | "zh-CN";
type Tab = "definition" | "preview" | "dependencies" | "source" | "changes";
type SourceKind = "metadata" | "sql";

const COPY = {
  "en-US": {
    eyebrow: "Semantic layer",
    title: "Views",
    description: "Build reusable business datasets with SemaRail SQL, then inspect their source and runtime output before publishing.",
    viewCount: (count: number) => `${count} ${count === 1 ? "view" : "views"}`,
    draft: "Draft",
    tracked: "Tracked",
    newView: "New view",
    search: "Search views",
    noMatches: "No matching views",
    noMatchesBody: "Try a different name, description, or SQL reference.",
    empty: "No views yet",
    emptyBody: "Create a reusable SemaRail view to shape a business dataset without changing the source models.",
    select: "Select a view",
    selectBody: "Choose a view from the list to edit its definition and inspect its runtime output.",
    retry: "Retry",
    validate: "Validate",
    validating: "Validating",
    save: "Save draft",
    saving: "Saving",
    delete: "Delete",
    definition: "Definition",
    preview: "Preview",
    dependencies: "Dependencies",
    source: "Source",
    changes: "Changes",
    technicalName: "Technical name",
    sourceFile: "Source file",
    storage: "SQL storage",
    storageSql: "Separate sql.yml",
    storageMetadata: "Inline metadata.yml",
    dialect: "Dialect",
    dialectAuto: "Project default",
    descriptionLabel: "Business description",
    descriptionHint: "Explain the business meaning and intended use of this dataset.",
    tags: "Tags",
    tagsHint: "Comma-separated labels for discovery and governance.",
    statement: "View SQL",
    statementHint: "One read-only query. Reference SemaRail models directly; nested View references are not supported by the current Python runtime.",
    dirty: "Unsaved changes",
    validationPassed: "View definition passed structural validation",
    validationWarning: "Validation passed with warnings",
    validationFailed: "View definition needs attention",
    saveSuccess: "View draft saved",
    createTitle: "Create a view",
    createDescription: "Start with one read-only query. New views keep SQL in a separate sql.yml file by default.",
    createAction: "Create draft",
    createNameHint: "Letters, numbers, underscore, dollar sign, and hyphen are supported. Start with a letter or underscore.",
    createStatementHint: "Use a SemaRail model name, for example SELECT * FROM orders.",
    cancel: "Cancel",
    invalidName: "Enter a valid, unique technical name.",
    statementRequired: "View SQL is required.",
    deleteTitle: "Delete this view?",
    deleteDescription: "The metadata and SQL files will be removed from the current project draft.",
    deleteAction: "Delete view",
    deleteConfirm: (name: string) => `This removes ${name}. Publish the project before downstream queries observe the deletion.`,
    previewTitle: "Database runtime preview",
    previewBody: "SemaRail Views do not declare output columns. Run a bounded, read-only PostgreSQL preview to discover the actual fields and sample rows.",
    runPreview: "Run preview",
    runningPreview: "Running preview",
    retryPreview: "Retry preview",
    previewFailed: "Preview failed",
    previewUnavailable: "Runtime preview is unavailable",
    previewUnavailableBody: "Connect a supported database and enable the safe SemaRail query runtime. No sample rows have been fabricated.",
    noPreview: "Preview has not run",
    noPreviewBody: "Run the View through SemaRail to inspect its database-derived columns and rows.",
    rows: (count: number) => `${count} rows`,
    truncated: "Bounded result",
    nullValue: "NULL",
    dependencyTitle: "SQL references",
    dependencyBody: "References are parsed from FROM and JOIN clauses. SemaRail planning remains the authoritative semantic check.",
    noDependencies: "No references detected",
    noDependenciesBody: "Add a model reference in a FROM or JOIN clause, then validate the View.",
    model: "Model",
    view: "View",
    unknown: "Unresolved",
    nestedUnsupported: "Nested View reference is not supported in the current SemaRail Python runtime.",
    metadataFile: "metadata.yml",
    sqlFile: "sql.yml",
    sourceHint: "This read-only source is generated from the visual draft. Use the MDL editor for advanced file-level changes.",
    sourceUnavailable: "Source is unavailable",
    sourceUnavailableBody: "Save the View draft, then load its generated source file.",
    noChanges: "No unpublished changes",
    noChangesBody: "The selected source file matches its published version.",
    changesHint: "Review the bounded unified diff before publishing the project.",
    loading: "Loading Views",
  },
  "zh-CN": {
    eyebrow: "语义层",
    title: "视图",
    description: "使用 SemaRail SQL 构建可复用的业务数据集，并在发布前检查源码和真实运行结果。",
    viewCount: (count: number) => `${count} 个视图`,
    draft: "草稿",
    tracked: "已跟踪",
    newView: "新建视图",
    search: "搜索视图",
    noMatches: "没有匹配的视图",
    noMatchesBody: "请尝试其他名称、描述或 SQL 引用。",
    empty: "暂无视图",
    emptyBody: "创建可复用的 SemaRail 视图，在不改动源模型的情况下组织业务数据集。",
    select: "选择一个视图",
    selectBody: "从左侧选择视图，编辑定义并检查真实运行结果。",
    retry: "重试",
    validate: "校验",
    validating: "校验中",
    save: "保存草稿",
    saving: "保存中",
    delete: "删除",
    definition: "定义",
    preview: "预览",
    dependencies: "依赖",
    source: "源码",
    changes: "变更",
    technicalName: "技术名称",
    sourceFile: "源文件",
    storage: "SQL 存储方式",
    storageSql: "独立 sql.yml",
    storageMetadata: "内联 metadata.yml",
    dialect: "方言",
    dialectAuto: "使用项目默认值",
    descriptionLabel: "业务描述",
    descriptionHint: "说明该数据集的业务含义和适用场景。",
    tags: "标签",
    tagsHint: "使用英文逗号分隔，便于发现和治理。",
    statement: "视图 SQL",
    statementHint: "只允许一条只读查询。请直接引用 SemaRail 模型；当前 Python 运行时不支持嵌套引用 View。",
    dirty: "有未保存修改",
    validationPassed: "视图定义已通过结构校验",
    validationWarning: "校验通过，但存在警告",
    validationFailed: "视图定义需要处理",
    saveSuccess: "视图草稿已保存",
    createTitle: "创建视图",
    createDescription: "从一条只读查询开始。新视图默认将 SQL 保存在独立的 sql.yml 中。",
    createAction: "创建草稿",
    createNameHint: "支持字母、数字、下划线、美元符号和连字符，并以字母或下划线开头。",
    createStatementHint: "请使用 SemaRail 模型名称，例如 SELECT * FROM orders。",
    cancel: "取消",
    invalidName: "请输入有效且不重复的技术名称。",
    statementRequired: "必须填写视图 SQL。",
    deleteTitle: "删除该视图？",
    deleteDescription: "metadata 和 SQL 文件将从当前项目草稿中删除。",
    deleteAction: "删除视图",
    deleteConfirm: (name: string) => `将删除 ${name}。发布项目后，下游查询才会看到该删除。`,
    previewTitle: "数据库运行时预览",
    previewBody: "SemaRail View 本身不声明输出字段。运行有边界的只读 PostgreSQL 预览，才能获得真实字段与样例数据。",
    runPreview: "运行预览",
    runningPreview: "正在预览",
    retryPreview: "重试预览",
    previewFailed: "预览失败",
    previewUnavailable: "运行时预览不可用",
    previewUnavailableBody: "请连接受支持的数据库并启用安全 SemaRail 查询运行时。这里不会伪造任何样例数据。",
    noPreview: "尚未运行预览",
    noPreviewBody: "通过 SemaRail 运行该 View，检查数据库推断出的字段和数据行。",
    rows: (count: number) => `${count} 行`,
    truncated: "结果已限制",
    nullValue: "空值",
    dependencyTitle: "SQL 引用",
    dependencyBody: "引用来自 FROM 和 JOIN 子句的静态解析，SemaRail 规划结果仍是最终语义校验依据。",
    noDependencies: "未发现引用",
    noDependenciesBody: "在 FROM 或 JOIN 子句中添加模型引用，然后校验视图。",
    model: "模型",
    view: "视图",
    unknown: "未解析",
    nestedUnsupported: "当前 SemaRail Python 运行时不支持嵌套 View 引用。",
    metadataFile: "metadata.yml",
    sqlFile: "sql.yml",
    sourceHint: "这里以只读方式展示可视化草稿生成的源码。高级文件级修改请使用 MDL 编辑器。",
    sourceUnavailable: "源码不可用",
    sourceUnavailableBody: "请先保存视图草稿，再加载生成的源文件。",
    noChanges: "没有未发布变更",
    noChangesBody: "所选源文件与已发布版本一致。",
    changesHint: "发布项目前，请检查有边界限制的统一差异。",
    loading: "正在加载视图",
  },
} as const;

type Copy = {
  [Key in keyof typeof COPY["en-US"]]: typeof COPY["en-US"][Key] extends (...args: infer Args) => string
    ? (...args: Args) => string
    : string;
};

const NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_$-]*$/;
const DIALECTS = ["postgres", "mysql", "bigquery", "snowflake", "trino", "clickhouse", "duckdb", "mssql", "oracle", "redshift", "spark"];

function descriptionOf(view: ViewDefinition): string {
  return typeof view.properties?.description === "string" ? view.properties.description : "";
}

function tagsOf(view: ViewDefinition): string[] {
  return Array.isArray(view.properties?.tags) ? view.properties.tags.filter((tag): tag is string => typeof tag === "string") : [];
}

function cloneView(view: ViewDefinition): ViewDefinition {
  return { ...view, properties: { ...(view.properties ?? {}), ...(tagsOf(view).length ? { tags: [...tagsOf(view)] } : {}) } };
}

function editableShape(view: ViewDefinition) {
  return {
    name: view.name,
    statement: view.statement,
    storage: view.storage,
    dialect: view.dialect ?? "",
    properties: view.properties ?? {},
  };
}

function sameView(left: ViewDefinition | undefined, right: ViewDefinition | undefined) {
  if (!left || !right) return false;
  return JSON.stringify(editableShape(left)) === JSON.stringify(editableShape(right));
}

function payload(view: ViewDefinition, revision?: string): ViewWritePayload {
  return {
    name: view.name,
    statement: view.statement,
    storage: view.storage,
    properties: view.properties ?? {},
    ...(view.dialect ? { dialect: view.dialect } : {}),
    ...(revision ? { expectedRevision: revision } : {}),
  };
}

function sqlReferences(statement: string): string[] {
  const result: string[] = [];
  const seen = new Set<string>();
  const pattern = /\b(?:from|join)\s+(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_$.-]*))/gi;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(statement)) !== null) {
    const raw = match[1] ?? match[2] ?? match[3] ?? match[4] ?? "";
    const name = raw.split(".").at(-1)?.replace(/^['"]|['"]$/g, "") ?? "";
    if (name && !seen.has(name.toLowerCase())) {
      seen.add(name.toLowerCase());
      result.push(name);
    }
  }
  return result;
}

export interface ViewWorkbenchProps {
  snapshot: ViewSnapshot | null;
  loading?: boolean;
  error?: string | null;
  locale?: Locale;
  theme?: "light" | "dark";
  modelNames?: string[];
  sourceContent?: string;
  sourceLoading?: boolean;
  sourceError?: string | null;
  diff?: ProjectDiff | null;
  diffLoading?: boolean;
  onSave: (view: ViewDefinition) => Promise<void> | void;
  onCreate: (input: ViewWritePayload) => Promise<void> | void;
  onDelete: (view: ViewDefinition) => Promise<void> | void;
  onValidate: (view: ViewDefinition) => Promise<ViewValidationResponse>;
  onPreview: (view: ViewDefinition) => Promise<ViewPreviewResult>;
  onOpenSource: (path: string) => void;
  onLoadDiff: (path: string) => void;
  onRetry?: () => void;
}

export default function ViewWorkbench({
  snapshot,
  loading = false,
  error = null,
  locale = "en-US",
  theme = "light",
  modelNames = [],
  sourceContent,
  sourceLoading = false,
  sourceError = null,
  diff,
  diffLoading = false,
  onSave,
  onCreate,
  onDelete,
  onValidate,
  onPreview,
  onOpenSource,
  onLoadDiff,
  onRetry,
}: ViewWorkbenchProps) {
  const c: Copy = COPY[locale];
  const [activeName, setActiveName] = useState("");
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<Tab>("definition");
  const [sourceKind, setSourceKind] = useState<SourceKind>("metadata");
  const [drafts, setDrafts] = useState<Record<string, ViewDefinition>>({});
  const [busy, setBusy] = useState<"save" | "validate" | "preview" | "create" | "delete" | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "warning" | "error"; title: string; body?: string } | null>(null);
  const [validation, setValidation] = useState<ViewValidationResponse | null>(null);
  const [preview, setPreview] = useState<ViewPreviewResult | null>(null);
  const [creating, setCreating] = useState(false);
  const [createDraft, setCreateDraft] = useState({ name: "", statement: "SELECT * FROM orders", description: "" });
  const [createError, setCreateError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<ViewDefinition | null>(null);
  const tabRefs = useRef<Record<Tab, HTMLButtonElement | null>>({ definition: null, preview: null, dependencies: null, source: null, changes: null });

  useEffect(() => {
    setActiveName((current) => current && snapshot?.views.some((view) => view.name === current) ? current : snapshot?.views[0]?.name ?? "");
  }, [snapshot]);

  useEffect(() => {
    setPreview(null);
    setValidation(null);
    setNotice(null);
  }, [activeName]);

  const sourceView = snapshot?.views.find((view) => view.name === activeName);
  const activeView = activeName ? drafts[activeName] ?? sourceView : undefined;
  const isDirty = Boolean(activeView && sourceView && !sameView(activeView, sourceView));
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return snapshot?.views ?? [];
    return (snapshot?.views ?? []).filter((view) => [view.name, descriptionOf(view), view.statement, ...tagsOf(view)].join(" ").toLocaleLowerCase().includes(needle));
  }, [query, snapshot]);

  const references = useMemo(() => {
    if (!activeView) return [];
    const viewNames = new Set((snapshot?.views ?? []).map((view) => view.name.toLowerCase()));
    const models = new Set(modelNames.map((name) => name.toLowerCase()));
    return sqlReferences(activeView.statement).map((name) => ({
      name,
      kind: viewNames.has(name.toLowerCase()) ? "view" : models.has(name.toLowerCase()) ? "model" : "unknown",
    } as const));
  }, [activeView, modelNames, snapshot]);

  function patchActive(patch: Partial<ViewDefinition>) {
    if (!activeView) return;
    const next = cloneView({ ...activeView, ...patch });
    setDrafts((current) => ({ ...current, [next.name]: next }));
    setNotice(null);
    setValidation(null);
    setPreview(null);
  }

  function patchProperties(patch: Record<string, unknown>) {
    if (!activeView) return;
    patchActive({ properties: { ...(activeView.properties ?? {}), ...patch } });
  }

  function beginCreate() {
    const existing = new Set((snapshot?.views ?? []).map((view) => view.name));
    let name = "new_view";
    let index = 2;
    while (existing.has(name)) name = `new_view_${index++}`;
    setCreateDraft({ name, statement: "SELECT * FROM orders", description: "" });
    setCreateError("");
    setCreating(true);
  }

  async function confirmCreate() {
    if (busy) return;
    const name = createDraft.name.trim();
    if (!NAME_PATTERN.test(name) || snapshot?.views.some((view) => view.name === name)) { setCreateError(c.invalidName); return; }
    if (!createDraft.statement.trim()) { setCreateError(c.statementRequired); return; }
    setBusy("create");
    setCreateError("");
    try {
      await onCreate({ name, statement: createDraft.statement, storage: "sql", properties: createDraft.description.trim() ? { description: createDraft.description.trim() } : {}, expectedRevision: snapshot?.revision });
      setActiveName(name);
      setTab("definition");
      setCreating(false);
    } catch (caught) {
      setCreateError(caught instanceof Error ? caught.message : c.validationFailed);
    } finally {
      setBusy(null);
    }
  }

  async function save() {
    if (!activeView || busy) return;
    setBusy("save");
    try {
      await onSave(cloneView(activeView));
      setDrafts((current) => { const next = { ...current }; delete next[activeView.name]; return next; });
      setNotice({ tone: "success", title: c.saveSuccess });
    } catch (caught) {
      setNotice({ tone: "error", title: c.validationFailed, body: caught instanceof Error ? caught.message : undefined });
    } finally {
      setBusy(null);
    }
  }

  async function validate() {
    if (!activeView || busy) return;
    setBusy("validate");
    try {
      const result = await onValidate(cloneView(activeView));
      setValidation(result);
      setNotice({
        tone: result.valid ? (result.warningCount ? "warning" : "success") : "error",
        title: result.valid ? (result.warningCount ? c.validationWarning : c.validationPassed) : c.validationFailed,
      });
    } catch (caught) {
      setNotice({ tone: "error", title: c.validationFailed, body: caught instanceof Error ? caught.message : undefined });
    } finally {
      setBusy(null);
    }
  }

  async function runPreview() {
    if (!activeView || busy) return;
    setBusy("preview");
    setPreview(null);
    try {
      const result = await onPreview(cloneView(activeView));
      setPreview(result);
    } catch (caught) {
      setPreview({ status: "error", message: caught instanceof Error ? caught.message : c.previewUnavailableBody });
    } finally {
      setBusy(null);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget || busy) return;
    setBusy("delete");
    try {
      await onDelete(deleteTarget);
      setDrafts((current) => { const next = { ...current }; delete next[deleteTarget.name]; return next; });
      setDeleteTarget(null);
    } catch (caught) {
      setDeleteTarget(null);
      setNotice({ tone: "error", title: c.validationFailed, body: caught instanceof Error ? caught.message : undefined });
    } finally {
      setBusy(null);
    }
  }

  function selectedPath(view: ViewDefinition, kind = sourceKind) {
    return kind === "sql" && view.sqlPath ? view.sqlPath : view.sourcePath;
  }

  function chooseSource(kind: SourceKind) {
    if (!activeView) return;
    setSourceKind(kind);
    const path = selectedPath(activeView, kind);
    if (tab === "changes") onLoadDiff(path);
    else onOpenSource(path);
  }

  function selectTab(next: Tab) {
    setTab(next);
    if (!activeView) return;
    const path = selectedPath(activeView);
    if (next === "source") onOpenSource(path);
    if (next === "changes") onLoadDiff(path);
  }

  function onTabKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, current: Tab) {
    const tabs: Tab[] = ["definition", "preview", "dependencies", "source", "changes"];
    const index = tabs.indexOf(current);
    let target: Tab | undefined;
    if (event.key === "ArrowRight") target = tabs[(index + 1) % tabs.length];
    if (event.key === "ArrowLeft") target = tabs[(index - 1 + tabs.length) % tabs.length];
    if (event.key === "Home") target = tabs[0];
    if (event.key === "End") target = tabs.at(-1);
    if (!target) return;
    event.preventDefault();
    selectTab(target);
    tabRefs.current[target]?.focus();
  }

  const createModal = <Modal open={creating} title={c.createTitle} description={c.createDescription} onClose={() => { if (!busy) setCreating(false); }} footer={<><Button variant="ghost" onClick={() => setCreating(false)} disabled={Boolean(busy)}>{c.cancel}</Button><Button variant="primary" icon={Plus} loading={busy === "create"} onClick={() => void confirmCreate()}>{c.createAction}</Button></>}><div className="view-create-form">{createError ? <InlineNotice tone="error" title={createError} /> : null}<Field label={c.technicalName} hint={c.createNameHint} htmlFor="new-view-name"><TextInput id="new-view-name" value={createDraft.name} onChange={(event) => { setCreateDraft((current) => ({ ...current, name: event.target.value })); setCreateError(""); }} autoFocus /></Field><Field label={c.descriptionLabel} htmlFor="new-view-description"><TextArea id="new-view-description" rows={3} value={createDraft.description} onChange={(event) => setCreateDraft((current) => ({ ...current, description: event.target.value }))} /></Field><Field label={c.statement} hint={c.createStatementHint} htmlFor="new-view-statement"><TextArea id="new-view-statement" className="view-create-sql" rows={6} value={createDraft.statement} onChange={(event) => { setCreateDraft((current) => ({ ...current, statement: event.target.value })); setCreateError(""); }} spellCheck={false} /></Field></div></Modal>;
  const deleteModal = <Modal open={Boolean(deleteTarget)} title={c.deleteTitle} description={c.deleteDescription} onClose={() => { if (!busy) setDeleteTarget(null); }} footer={<><Button variant="ghost" onClick={() => setDeleteTarget(null)} disabled={Boolean(busy)}>{c.cancel}</Button><Button variant="danger" icon={Trash} loading={busy === "delete"} onClick={() => void confirmDelete()}>{c.deleteAction}</Button></>}><div className="view-delete-confirm"><WarningCircle size={20} weight="fill" /><div><p>{c.deleteConfirm(deleteTarget?.name ?? "")}</p><code>{deleteTarget?.sourcePath}</code></div></div></Modal>;

  if (loading) return <div className="page view-workbench-page" role="status" aria-label={c.loading}><div className="view-heading-skeleton"><LoadingRows count={2} /></div><section className="panel view-loading-panel"><LoadingRows count={7} /></section></div>;
  if (error) return <div className="page view-workbench-page"><PageHeading c={c} count={snapshot?.views.length ?? 0} draftCount={snapshot?.draftCount ?? 0} /><section className="panel view-state-panel"><InlineNotice tone="error" title={error}>{onRetry ? <Button variant="secondary" size="sm" icon={ArrowClockwise} onClick={onRetry}>{c.retry}</Button> : null}</InlineNotice></section></div>;
  if (!snapshot || snapshot.views.length === 0) return <div className="page view-workbench-page"><PageHeading c={c} count={0} draftCount={0} /><section className="panel view-state-panel"><EmptyState icon={Eye} title={c.empty} body={c.emptyBody} action={<Button variant="primary" icon={Plus} onClick={beginCreate}>{c.newView}</Button>} /></section>{createModal}</div>;

  return <div className="page view-workbench-page">
    <PageHeading c={c} count={snapshot.views.length} draftCount={snapshot.draftCount} />
    <div className="view-workbench">
      <aside className="panel view-list-panel" aria-label={c.title}>
        <div className="view-list-toolbar"><div><span className="panel-kicker">{c.title}</span><strong>{c.viewCount(filtered.length)}</strong></div><Button variant="ghost" size="sm" icon={Plus} onClick={beginCreate}>{c.newView}</Button></div>
        <label className="view-search"><MagnifyingGlass size={15} aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={c.search} aria-label={c.search} /></label>
        <div className="view-list" role="listbox" aria-label={c.title}>
          {filtered.map((view) => {
            const current = drafts[view.name] ?? view;
            const dirty = !sameView(current, view);
            return <button type="button" role="option" aria-selected={view.name === activeName} className={`view-list-item ${view.name === activeName ? "view-list-item-active" : ""}`} key={view.name} onClick={() => { setActiveName(view.name); setTab("definition"); }}><span className="view-list-icon"><Table size={17} weight="duotone" /></span><span className="view-list-copy"><strong>{view.name}</strong><small>{descriptionOf(current) || view.sourcePath}</small><em>{current.storage === "sql" ? c.sqlFile : c.metadataFile}</em></span><span className={`view-status-dot ${view.draft || dirty ? "view-status-draft" : ""}`} title={view.draft || dirty ? c.draft : c.tracked} /></button>;
          })}
          {filtered.length === 0 ? <EmptyState icon={MagnifyingGlass} title={c.noMatches} body={c.noMatchesBody} /> : null}
        </div>
      </aside>
      <section className="view-editor-main">
        {!activeView ? <section className="panel view-state-panel"><EmptyState icon={Eye} title={c.select} body={c.selectBody} /></section> : <section className="panel view-workspace-panel">
          <div className="view-panel-header"><div className="view-title-block"><p className="panel-kicker">{activeView.statementSource === "sql" ? c.sqlFile : c.metadataFile}</p><h2>{activeView.name}</h2><p>{descriptionOf(activeView) || c.description}</p></div><div className="view-editor-actions"><Badge tone={activeView.draft || isDirty ? "amber" : "neutral"}>{isDirty ? c.dirty : activeView.draft ? c.draft : c.tracked}</Badge><Button variant="ghost" size="sm" icon={Trash} onClick={() => setDeleteTarget(cloneView(activeView))} disabled={Boolean(busy)}>{c.delete}</Button><Button variant="secondary" size="sm" icon={CheckCircle} loading={busy === "validate"} onClick={() => void validate()} disabled={Boolean(busy) && busy !== "validate"}>{busy === "validate" ? c.validating : c.validate}</Button><Button variant="primary" size="sm" icon={FloppyDisk} loading={busy === "save"} onClick={() => void save()} disabled={!isDirty || Boolean(busy)}>{busy === "save" ? c.saving : c.save}</Button></div></div>
          {notice ? <div className="view-inline-notice"><InlineNotice tone={notice.tone} title={notice.title} onDismiss={() => setNotice(null)}>{notice.body}</InlineNotice></div> : null}
          {validation && (!validation.valid || validation.warningCount > 0) ? <ValidationIssues result={validation} /> : null}
          <div className="view-tabs" role="tablist" aria-label={c.title}>
            {(["definition", "preview", "dependencies", "source", "changes"] as Tab[]).map((id) => <button key={id} type="button" id={`view-tab-${id}`} role="tab" aria-selected={tab === id} aria-controls="view-tab-panel" tabIndex={tab === id ? 0 : -1} className={tab === id ? "view-tab-active" : ""} onClick={() => selectTab(id)} onKeyDown={(event) => onTabKeyDown(event, id)} ref={(element) => { tabRefs.current[id] = element; }}>{id === "definition" ? <Code size={14} /> : id === "preview" ? <Database size={14} /> : id === "dependencies" ? <TreeStructure size={14} /> : id === "source" ? <BracketsCurly size={14} /> : <Check size={14} />}{c[id]}</button>)}
          </div>
          <div className="view-tab-panel" id="view-tab-panel" role="tabpanel" aria-labelledby={`view-tab-${tab}`}>
            {tab === "definition" ? <DefinitionTab view={activeView} c={c} theme={theme} onPatch={patchActive} onPatchProperties={patchProperties} /> : null}
            {tab === "preview" ? <PreviewTab preview={preview} busy={busy === "preview"} c={c} onRun={() => void runPreview()} /> : null}
            {tab === "dependencies" ? <DependenciesTab references={references} c={c} /> : null}
            {tab === "source" ? <SourceTab view={activeView} kind={sourceKind} content={sourceContent} loading={sourceLoading} error={sourceError} c={c} onChoose={chooseSource} /> : null}
            {tab === "changes" ? <ChangesTab view={activeView} kind={sourceKind} diff={diff} loading={diffLoading} c={c} onChoose={chooseSource} /> : null}
          </div>
        </section>}
      </section>
    </div>
    {createModal}{deleteModal}
  </div>;
}

function PageHeading({ c, count, draftCount }: { c: Copy; count: number; draftCount: number }) {
  return <div className="view-page-heading"><div><p className="eyebrow">{c.eyebrow}</p><h1>{c.title}</h1><p>{c.description}</p></div><div className="view-heading-meta"><Badge tone="neutral">{c.viewCount(count)}</Badge><Badge tone={draftCount ? "amber" : "green"} dot>{draftCount ? c.draft : c.tracked}</Badge></div></div>;
}

function DefinitionTab({ view, c, theme, onPatch, onPatchProperties }: { view: ViewDefinition; c: Copy; theme: "light" | "dark"; onPatch: (patch: Partial<ViewDefinition>) => void; onPatchProperties: (patch: Record<string, unknown>) => void }) {
  const tags = tagsOf(view);
  return <div className="view-definition-tab">
    <div className="view-definition-grid">
      <Field label={c.technicalName} htmlFor="view-name"><TextInput id="view-name" value={view.name} readOnly /></Field>
      <Field label={c.sourceFile} htmlFor="view-source-path"><TextInput id="view-source-path" value={view.sourcePath} readOnly /></Field>
      <Field label={c.storage} htmlFor="view-storage"><Select id="view-storage" value={view.storage} onChange={(event) => onPatch({ storage: event.target.value as ViewDefinition["storage"] })}><option value="sql">{c.storageSql}</option><option value="metadata">{c.storageMetadata}</option></Select></Field>
      <Field label={c.dialect} htmlFor="view-dialect"><Select id="view-dialect" value={view.dialect ?? ""} onChange={(event) => onPatch({ dialect: event.target.value || undefined })}><option value="">{c.dialectAuto}</option>{DIALECTS.map((dialect) => <option key={dialect} value={dialect}>{dialect}</option>)}</Select></Field>
      <Field label={c.descriptionLabel} hint={c.descriptionHint} htmlFor="view-description"><TextArea id="view-description" rows={3} value={descriptionOf(view)} onChange={(event) => onPatchProperties({ description: event.target.value })} /></Field>
      <Field label={c.tags} hint={c.tagsHint} htmlFor="view-tags"><TextInput id="view-tags" value={tags.join(", ")} onChange={(event) => onPatchProperties({ tags: event.target.value.split(",").map((tag) => tag.trim()).filter(Boolean) })} /></Field>
    </div>
    <div className="view-sql-section"><div className="view-section-heading"><div><p className="panel-kicker">{c.statement}</p><h3>{c.statement}</h3></div><Badge tone="blue">SQL</Badge></div><p className="view-section-hint">{c.statementHint}</p><div className="view-code-editor"><CodeMirror value={view.statement} height="360px" theme={theme} extensions={[sql()]} onChange={(statement) => onPatch({ statement })} basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: true, autocompletion: true, bracketMatching: true }} aria-label={c.statement} /></div></div>
  </div>;
}

function ValidationIssues({ result }: { result: ViewValidationResponse }) {
  const issues = [...result.errors, ...result.warnings];
  return <div className="view-validation-issues" role="group" aria-label="Validation issues"><WarningCircle size={17} weight="fill" /><div>{issues.map((issue, index) => <span key={`${issue.path}-${issue.code}-${index}`}><strong>{issue.path || issue.code || "view"}</strong>{issue.message}</span>)}</div></div>;
}

function PreviewTab({ preview, busy, c, onRun }: { preview: ViewPreviewResult | null; busy: boolean; c: Copy; onRun: () => void }) {
  const columns = preview?.columns ?? [];
  const rows = preview?.previewRows ?? [];
  const failed = preview?.status === "error";
  const unavailable = preview?.status === "PREVIEW_UNAVAILABLE";
  const actionLabel = failed || unavailable ? c.retryPreview : c.runPreview;
  return <div className="view-preview-tab"><div className="view-tab-intro"><div><p className="panel-kicker">{c.previewTitle}</p><h3>{c.previewTitle}</h3><p>{c.previewBody}</p></div><Button variant="secondary" icon={ArrowClockwise} loading={busy} onClick={onRun}>{busy ? c.runningPreview : actionLabel}</Button></div>{failed ? <InlineNotice tone="error" title={c.previewFailed}>{preview.message}</InlineNotice> : unavailable ? <InlineNotice tone="warning" title={c.previewUnavailable}>{preview.message || c.previewUnavailableBody}</InlineNotice> : preview?.status === "success" ? <><div className="view-preview-stats"><Badge tone="neutral">{c.rows(preview.stats?.returnedRows ?? rows.length)}</Badge>{preview.stats?.truncated ? <Badge tone="amber">{c.truncated}</Badge> : null}<span>{Math.round(preview.stats?.durationMs ?? 0)} ms</span></div><div className="view-preview-table-wrap"><table className="view-preview-table"><caption className="sr-only">{c.previewTitle}</caption><thead><tr>{columns.map((column) => <th key={column.name} scope="col"><strong>{column.name}</strong><small>{column.type || column.semanticRole || ""}</small></th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{columns.map((column) => <td key={column.name}>{row[column.name] === null || row[column.name] === undefined ? <span className="view-null">{c.nullValue}</span> : String(row[column.name])}</td>)}</tr>)}</tbody></table></div></> : <EmptyState icon={Database} title={c.noPreview} body={c.noPreviewBody} action={<Button variant="ghost" size="sm" icon={Database} onClick={onRun}>{c.runPreview}</Button>} />}</div>;
}

function DependenciesTab({ references, c }: { references: Array<{ name: string; kind: "model" | "view" | "unknown" }>; c: Copy }) {
  return <div className="view-dependencies-tab"><div className="view-tab-intro"><div><p className="panel-kicker">{c.dependencyTitle}</p><h3>{c.dependencyTitle}</h3><p>{c.dependencyBody}</p></div><TreeStructure size={21} aria-hidden="true" /></div>{references.length ? <div className="view-reference-list">{references.map((reference) => <article key={`${reference.kind}-${reference.name}`} className={`view-reference-row view-reference-${reference.kind}`}><span className="view-reference-icon">{reference.kind === "model" ? <Table size={17} /> : reference.kind === "view" ? <Eye size={17} /> : <WarningCircle size={17} />}</span><span><strong>{reference.name}</strong><small>{reference.kind === "model" ? c.model : reference.kind === "view" ? c.view : c.unknown}</small>{reference.kind === "view" ? <em>{c.nestedUnsupported}</em> : null}</span></article>)}</div> : <EmptyState icon={TreeStructure} title={c.noDependencies} body={c.noDependenciesBody} />}</div>;
}

function SourceSelector({ view, kind, c, onChoose }: { view: ViewDefinition; kind: SourceKind; c: Copy; onChoose: (kind: SourceKind) => void }) {
  return <div className="view-source-selector" role="group" aria-label={c.sourceFile}><button type="button" className={kind === "metadata" ? "view-source-choice-active" : ""} onClick={() => onChoose("metadata")}><BracketsCurly size={14} />{c.metadataFile}</button>{view.sqlPath ? <button type="button" className={kind === "sql" ? "view-source-choice-active" : ""} onClick={() => onChoose("sql")}><Code size={14} />{c.sqlFile}</button> : null}</div>;
}

function SourceTab({ view, kind, content, loading, error, c, onChoose }: { view: ViewDefinition; kind: SourceKind; content?: string; loading: boolean; error?: string | null; c: Copy; onChoose: (kind: SourceKind) => void }) {
  const path = kind === "sql" && view.sqlPath ? view.sqlPath : view.sourcePath;
  return <div className="view-source-tab"><div className="view-source-header"><div><p className="panel-kicker">{c.sourceFile}</p><strong>{path}</strong></div><SourceSelector view={view} kind={kind} c={c} onChoose={onChoose} /></div><div className="view-source-notice"><Eye size={15} /><span>{c.sourceHint}</span></div>{loading ? <LoadingRows count={8} /> : error ? <InlineNotice tone="error" title={c.sourceUnavailable}>{error}</InlineNotice> : content ? <pre className="view-code-block"><code>{content}</code></pre> : <EmptyState icon={Code} title={c.sourceUnavailable} body={c.sourceUnavailableBody} />}</div>;
}

function ChangesTab({ view, kind, diff, loading, c, onChoose }: { view: ViewDefinition; kind: SourceKind; diff?: ProjectDiff | null; loading: boolean; c: Copy; onChoose: (kind: SourceKind) => void }) {
  const path = kind === "sql" && view.sqlPath ? view.sqlPath : view.sourcePath;
  return <div className="view-source-tab"><div className="view-source-header"><div><p className="panel-kicker">{c.changes}</p><strong>{path}</strong></div><SourceSelector view={view} kind={kind} c={c} onChoose={onChoose} /></div><div className="view-source-notice"><BracketsCurly size={15} /><span>{c.changesHint}</span></div>{loading ? <LoadingRows count={8} /> : diff?.changed ? <pre className="view-code-block view-diff-block"><code>{diff.diff}</code></pre> : <EmptyState icon={Check} title={c.noChanges} body={c.noChangesBody} />}</div>;
}
