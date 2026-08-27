import { useEffect, useId, useMemo, useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";
import {
  ArrowClockwise,
  ArrowUUpLeft,
  BookOpenText,
  CaretRight,
  Check,
  CheckCircle,
  CircleNotch,
  Code,
  FileText,
  FloppyDisk,
  MagnifyingGlass,
  PencilSimple,
  Plus,
  RocketLaunch,
  Tag,
  Trash,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { Button, Field, InlineNotice, Modal, TextArea, TextInput } from "./ui";
import "./knowledge-workbench.css";

/** The two locales supported by the semantic-console workbenches. */
export type KnowledgeWorkbenchLocale = "en-US" | "zh-CN";

/** A single editable Wren business rule. The file itself remains the source of truth. */
export interface KnowledgeRule {
  id: string;
  name: string;
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

/** A source and optional diff view supplied by the project-file adapter. */
export interface KnowledgeRuleSource {
  path: string;
  content: string;
  diff?: string;
}

export type RuleSaveAction = "draft" | "publish";

export type NewKnowledgeRule = Pick<KnowledgeRule, "name" | "content" | "enabled"> & { scope: string[]; tags: string[] };

export interface RuleWorkbenchProps {
  rules: KnowledgeRule[];
  selectedRuleId?: string;
  locale?: KnowledgeWorkbenchLocale;
  loading?: boolean;
  error?: string | null;
  readOnly?: boolean;
  source?: KnowledgeRuleSource | null;
  scopeOptions?: string[];
  onRetry?: () => void;
  onSelectRule?: (rule: KnowledgeRule) => void;
  onDraftChange?: (rule: KnowledgeRule) => void;
  onToggleRule?: (rule: KnowledgeRule, enabled: boolean) => void | Promise<void>;
  onSave?: (rule: KnowledgeRule, action: RuleSaveAction) => void | Promise<void>;
  onCreate?: (rule: NewKnowledgeRule) => void | Promise<void>;
  onDelete?: (rule: KnowledgeRule) => void | Promise<void>;
  onDiscard?: (rule: KnowledgeRule) => void;
}

type RuleTab = "content" | "source" | "diff";
type RuleFilter = "all" | "enabled" | "disabled";

const ruleCopy = {
  "en-US": {
    eyebrow: "Semantic layer",
    title: "Business rules",
    description: "Keep the guidance that shapes natural-language queries explicit, reviewable, and easy to publish.",
    listTitle: "Rules",
    listCount: (count: number) => `${count} ${count === 1 ? "rule" : "rules"}`,
    search: "Search rules",
    filterLabel: "Rule status",
    all: "All",
    enabled: "Enabled",
    disabled: "Disabled",
    noRules: "No business rules",
    noRulesBody: "Create a rule file in knowledge/rules to make query guidance available here.",
    noMatches: "No matching rules",
    noMatchesBody: "Try a different name, scope, tag, or status filter.",
    loading: "Loading rules",
    errorTitle: "Rules could not be loaded",
    retry: "Try again",
    readOnly: "Read only",
    draft: "Draft",
    published: "Published",
    unsaved: "Unsaved changes",
    saved: "Draft saved",
    contentTab: "Content",
    sourceTab: "Source",
    diffTab: "Changes",
    contentTitle: "Rule content",
    contentHint: "Write one clear instruction. Keep the wording specific enough to review as a team.",
    contentLabel: "Rule text",
    nameLabel: "Rule name",
    sourceLabel: "Source file",
    scopeLabel: "Applies to",
    tagsLabel: "Tags",
    noScope: "No scope set",
    noTags: "No tags set",
    onLabel: "Disable rule",
    offLabel: "Enable rule",
    saveDraft: "Save draft",
    publish: "Publish",
    discard: "Discard changes",
    saving: "Saving",
    publishing: "Publishing",
    sourceHint: "This is the underlying Markdown file. Edit it in the MDL source page when the visual fields are not enough.",
    sourceUnavailable: "The source file is unavailable.",
    diffHint: "Review the unpublished rule changes before publishing.",
    noDiff: "No unpublished changes",
    noDiffBody: "This rule matches its last saved source.",
    sourceEditor: "Source editor",
    selected: "Selected rule",
    ruleEnabled: "Rule is enabled",
    ruleDisabled: "Rule is disabled",
    savedNotice: "The rule is saved as a project draft.",
    publishedNotice: "The rule is now available to query guidance.",
    discardNotice: "Local changes were discarded.",
    previous: "Previous rule",
    next: "Next rule",
    create: "New rule", createTitle: "Create a business rule", createDescription: "Add one focused instruction. It remains a project draft until you publish.",
    createAction: "Create draft", creating: "Creating", createNameRequired: "Give the rule a clear name.", createContentRequired: "Rule text is required.",
    delete: "Delete", deleteTitle: "Delete this rule?", deleteDescription: "The rule file will be removed from the project draft. Publish the project to make the deletion effective downstream.",
    deleteAction: "Delete rule", deleting: "Deleting", deleteConfirm: "This removes {{name}} and its source file from the current draft.",
    addToken: "Type and press Enter", removeToken: "Remove", operationFailed: "The operation could not be completed.",
  },
  "zh-CN": {
    eyebrow: "语义层",
    title: "业务规则",
    description: "将影响自然语言查询的业务指引明确记录，方便团队审阅和发布。",
    listTitle: "规则",
    listCount: (count: number) => `${count} 条规则`,
    search: "搜索规则",
    filterLabel: "规则状态",
    all: "全部",
    enabled: "已启用",
    disabled: "已停用",
    noRules: "暂无业务规则",
    noRulesBody: "在 knowledge/rules 中创建规则文件后，规则会显示在这里。",
    noMatches: "没有匹配的规则",
    noMatchesBody: "请更换名称、范围、标签或状态筛选条件。",
    loading: "正在加载规则",
    errorTitle: "规则加载失败",
    retry: "重试",
    readOnly: "只读",
    draft: "草稿",
    published: "已发布",
    unsaved: "有未保存修改",
    saved: "草稿已保存",
    contentTab: "内容",
    sourceTab: "源文件",
    diffTab: "变更",
    contentTitle: "规则内容",
    contentHint: "写下一条清晰的指引，内容应足够具体，便于团队审核。",
    contentLabel: "规则文本",
    nameLabel: "规则名称",
    sourceLabel: "源文件",
    scopeLabel: "适用范围",
    tagsLabel: "标签",
    noScope: "未设置范围",
    noTags: "未设置标签",
    onLabel: "停用规则",
    offLabel: "启用规则",
    saveDraft: "保存草稿",
    publish: "发布",
    discard: "放弃修改",
    saving: "保存中",
    publishing: "发布中",
    sourceHint: "这是规则对应的 Markdown 源文件。如果可视化字段无法满足需求，可前往 MDL 源文件页面编辑。",
    sourceUnavailable: "源文件不可用。",
    diffHint: "发布前请检查规则尚未发布的变更。",
    noDiff: "没有未发布变更",
    noDiffBody: "该规则与最近保存的源文件一致。",
    sourceEditor: "源文件编辑器",
    selected: "当前规则",
    ruleEnabled: "规则已启用",
    ruleDisabled: "规则已停用",
    savedNotice: "规则已保存为项目草稿。",
    publishedNotice: "规则现已用于查询指引。",
    discardNotice: "本地修改已放弃。",
    previous: "上一条规则",
    next: "下一条规则",
    create: "新建规则", createTitle: "创建业务规则", createDescription: "新增一条聚焦、可审核的指引；发布前它只会保存为项目草稿。",
    createAction: "创建草稿", creating: "创建中", createNameRequired: "请填写清晰的规则名称。", createContentRequired: "请填写规则文本。",
    delete: "删除", deleteTitle: "删除这条规则？", deleteDescription: "规则文件将从项目草稿中移除；发布项目后，删除才会影响下游查询。",
    deleteAction: "删除规则", deleting: "删除中", deleteConfirm: "这会从当前草稿中移除 {{name}} 及其源文件。",
    addToken: "输入后按 Enter 添加", removeToken: "移除", operationFailed: "操作未能完成。",
  },
} as const;

function formatRuleDate(value: string | undefined, locale: KnowledgeWorkbenchLocale) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric" }).format(date);
}

function copyRule(rule: KnowledgeRule): KnowledgeRule {
  return {
    ...rule,
    scope: rule.scope ? [...rule.scope] : [],
    tags: rule.tags ? [...rule.tags] : [],
  };
}

function sameRule(a: KnowledgeRule | undefined, b: KnowledgeRule | undefined) {
  if (!a || !b) return false;
  return a.name === b.name && a.content === b.content && a.enabled === b.enabled && JSON.stringify(a.scope ?? []) === JSON.stringify(b.scope ?? []) && JSON.stringify(a.tags ?? []) === JSON.stringify(b.tags ?? []);
}

function StatusBadge({ rule, text }: { rule: KnowledgeRule; text: string }) {
  return <span className={`kw-status-badge ${rule.enabled ? "kw-status-enabled" : "kw-status-disabled"}`}><span className="kw-status-mark" aria-hidden="true" />{text}</span>;
}

function RuleLoadingState({ label }: { label: string }) {
  return <div className="kw-loading-state" role="status" aria-label={label}><span className="kw-skeleton kw-skeleton-title" /><span className="kw-skeleton kw-skeleton-line" /><span className="kw-skeleton kw-skeleton-line kw-skeleton-short" /><span className="kw-skeleton kw-skeleton-editor" /></div>;
}

function RuleEmptyState({ title, body, icon = <BookOpenText size={22} weight="duotone" /> }: { title: string; body: string; icon?: React.ReactNode }) {
  return <div className="kw-empty-state"><span className="kw-empty-icon">{icon}</span><h3>{title}</h3><p>{body}</p></div>;
}

/**
 * Rules workbench for one-rule-per-row governance. It deliberately keeps source and
 * diff views in the same editor so a reviewer can compare the visual draft with the file.
 */
export function RuleWorkbench({
  rules,
  selectedRuleId,
  locale = "en-US",
  loading = false,
  error = null,
  readOnly = false,
  source,
  scopeOptions = [],
  onRetry,
  onSelectRule,
  onDraftChange,
  onToggleRule,
  onSave,
  onCreate,
  onDelete,
  onDiscard,
}: RuleWorkbenchProps) {
  const c = ruleCopy[locale];
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<RuleFilter>("all");
  const [internalSelectedId, setInternalSelectedId] = useState(selectedRuleId ?? rules[0]?.id ?? "");
  const [tab, setTab] = useState<RuleTab>("content");
  const [drafts, setDrafts] = useState<Record<string, KnowledgeRule>>({});
  const [committed, setCommitted] = useState<Record<string, KnowledgeRule>>({});
  const [busyAction, setBusyAction] = useState<RuleSaveAction | "toggle" | "create" | "delete" | null>(null);
  const [notice, setNotice] = useState<"saved" | "published" | "discarded" | null>(null);
  const [operationError, setOperationError] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeRule | null>(null);
  const [createDraft, setCreateDraft] = useState<NewKnowledgeRule>({ name: "", content: "", enabled: true, scope: [], tags: [] });
  const [createError, setCreateError] = useState("");
  const tabRefs = useRef<Record<RuleTab, HTMLButtonElement | null>>({ content: null, source: null, diff: null });

  useEffect(() => {
    setDrafts((current) => {
      const next = { ...current };
      for (const rule of rules) if (!next[rule.id]) next[rule.id] = copyRule(rule);
      return next;
    });
    setCommitted((current) => {
      const next = { ...current };
      for (const rule of rules) {
        const previous = next[rule.id];
        if (!previous || sameRule(previous, rule)) next[rule.id] = copyRule(rule);
      }
      return next;
    });
    if (selectedRuleId !== undefined) setInternalSelectedId(selectedRuleId);
    else if (!rules.some((rule) => rule.id === internalSelectedId)) setInternalSelectedId(rules[0]?.id ?? "");
  }, [rules, selectedRuleId, internalSelectedId]);

  const filteredRules = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase();
    return rules.filter((rule) => {
      if (filter === "enabled" && !rule.enabled) return false;
      if (filter === "disabled" && rule.enabled) return false;
      if (!normalized) return true;
      return [rule.name, rule.content, rule.sourcePath, ...(rule.scope ?? []), ...(rule.tags ?? [])].join(" ").toLocaleLowerCase().includes(normalized);
    });
  }, [filter, rules, search]);

  const selectedSourceRule = rules.find((rule) => rule.id === internalSelectedId);
  const selected = selectedSourceRule ? drafts[selectedSourceRule.id] ?? selectedSourceRule : undefined;
  const selectedCommitted = selected ? committed[selected.id] ?? selectedSourceRule : undefined;
  const isDirty = Boolean(selected && selectedCommitted && !sameRule(selected, selectedCommitted));
  const sourceMatches = Boolean(source && selected && source.path === selected.sourcePath);
  const sourceContent = sourceMatches ? source?.content ?? "" : selected?.sourceContent ?? "";
  const diffContent = sourceMatches ? source?.diff ?? "" : selected?.diff ?? "";

  function selectRule(rule: KnowledgeRule) {
    setInternalSelectedId(rule.id);
    setTab("content");
    onSelectRule?.(rule);
    setNotice(null);
    setOperationError("");
  }

  function updateSelected(patch: Partial<KnowledgeRule>) {
    if (!selected || readOnly) return;
    const next = { ...selected, ...patch, draft: true };
    setDrafts((current) => ({ ...current, [selected.id]: next }));
    onDraftChange?.(next);
    setNotice(null);
    setOperationError("");
  }

  async function save(action: RuleSaveAction) {
    if (!selected || !onSave || readOnly || busyAction) return;
    setBusyAction(action);
    try {
      await onSave(selected, action);
      setCommitted((current) => ({ ...current, [selected.id]: copyRule(selected) }));
      setNotice(action === "publish" ? "published" : "saved");
      setOperationError("");
    } catch (caught) {
      setOperationError(caught instanceof Error ? caught.message : c.errorTitle);
    } finally {
      setBusyAction(null);
    }
  }

  function discard() {
    if (!selected || readOnly) return;
    const baseline = selectedCommitted ?? selectedSourceRule;
    if (!baseline) return;
    const next = copyRule(baseline);
    setDrafts((current) => ({ ...current, [selected.id]: next }));
    onDiscard?.(next);
    setNotice("discarded");
  }

  async function toggleRule(rule: KnowledgeRule, event: React.MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    if (readOnly || busyAction) return;
    const current = drafts[rule.id] ?? rule;
    const next = { ...current, enabled: !current.enabled, draft: true };
    setDrafts((value) => ({ ...value, [rule.id]: next }));
    onDraftChange?.(next);
    setBusyAction("toggle");
    try {
      await onToggleRule?.(next, next.enabled);
      setOperationError("");
    } catch (caught) {
      setDrafts((value) => ({ ...value, [rule.id]: current }));
      onDraftChange?.(current);
      setOperationError(caught instanceof Error ? caught.message : c.errorTitle);
    } finally {
      setBusyAction(null);
    }
  }

  function beginCreate() {
    setCreateDraft({ name: "", content: "", enabled: true, scope: [], tags: [] });
    setCreateError("");
    setCreating(true);
  }

  async function confirmCreate() {
    if (!onCreate || busyAction) return;
    const name = createDraft.name.trim();
    const content = createDraft.content.trim();
    if (!name) { setCreateError(c.createNameRequired); return; }
    if (!content) { setCreateError(c.createContentRequired); return; }
    setBusyAction("create"); setCreateError("");
    try {
      await onCreate({ ...createDraft, name, content });
      setCreating(false);
      setOperationError("");
    } catch (caught) {
      setCreateError(caught instanceof Error ? caught.message : c.operationFailed);
    } finally {
      setBusyAction(null);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget || !onDelete || busyAction) return;
    setBusyAction("delete");
    try {
      await onDelete(deleteTarget);
      setDeleteTarget(null);
      setOperationError("");
    } catch (caught) {
      setOperationError(caught instanceof Error ? caught.message : c.operationFailed);
      setDeleteTarget(null);
    } finally {
      setBusyAction(null);
    }
  }

  function handleTabKey(event: KeyboardEvent<HTMLButtonElement>, current: RuleTab) {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    event.preventDefault();
    const tabs: RuleTab[] = ["content", "source", "diff"];
    const offset = event.key === "ArrowRight" ? 1 : -1;
    const next = tabs[(tabs.indexOf(current) + offset + tabs.length) % tabs.length]!;
    setTab(next);
    tabRefs.current[next]?.focus();
  }

  function handleSelectKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const offset = event.key === "ArrowDown" ? 1 : -1;
    const next = filteredRules[(index + offset + filteredRules.length) % filteredRules.length];
    if (next) selectRule(next);
  }

  const noticeText = notice === "saved" ? c.savedNotice : notice === "published" ? c.publishedNotice : notice === "discarded" ? c.discardNotice : "";

  const createModal = <Modal open={creating} title={c.createTitle} description={c.createDescription} onClose={() => { if (!busyAction) setCreating(false); }} footer={<><Button variant="ghost" onClick={() => setCreating(false)} disabled={Boolean(busyAction)}>{locale === "zh-CN" ? "取消" : "Cancel"}</Button><Button variant="primary" icon={Plus} loading={busyAction === "create"} onClick={() => void confirmCreate()} disabled={Boolean(busyAction)}>{busyAction === "create" ? c.creating : c.createAction}</Button></>}><div className="kw-create-rule-form">{createError ? <InlineNotice tone="error" title={createError} /> : null}<Field label={c.nameLabel} htmlFor="new-rule-name"><TextInput id="new-rule-name" value={createDraft.name} onChange={(event) => { setCreateDraft((current) => ({ ...current, name: event.target.value })); setCreateError(""); }} autoFocus /></Field><Field label={c.contentLabel} htmlFor="new-rule-content"><TextArea id="new-rule-content" rows={5} value={createDraft.content} onChange={(event) => { setCreateDraft((current) => ({ ...current, content: event.target.value })); setCreateError(""); }} /></Field><div className="kw-form-grid"><TokenEditor label={c.scopeLabel} values={createDraft.scope} options={scopeOptions} placeholder={c.addToken} removeLabel={c.removeToken} onChange={(scope) => setCreateDraft((current) => ({ ...current, scope }))} /><TokenEditor label={c.tagsLabel} values={createDraft.tags} placeholder={c.addToken} removeLabel={c.removeToken} onChange={(tags) => setCreateDraft((current) => ({ ...current, tags }))} /></div></div></Modal>;
  const deleteModal = <Modal open={Boolean(deleteTarget)} title={c.deleteTitle} description={c.deleteDescription} onClose={() => { if (!busyAction) setDeleteTarget(null); }} footer={<><Button variant="ghost" onClick={() => setDeleteTarget(null)} disabled={Boolean(busyAction)}>{locale === "zh-CN" ? "取消" : "Cancel"}</Button><Button variant="danger" icon={Trash} loading={busyAction === "delete"} onClick={() => void confirmDelete()} disabled={Boolean(busyAction)}>{busyAction === "delete" ? c.deleting : c.deleteAction}</Button></>}><div className="kw-delete-confirm"><WarningCircle size={20} weight="fill" /><p>{c.deleteConfirm.replace("{{name}}", deleteTarget?.name ?? "")}</p>{deleteTarget ? <code>{deleteTarget.sourcePath}</code> : null}</div></Modal>;

  return <div className="page kw-page kw-rules-page">
    <div className="kw-page-heading">
      <div><p className="kw-eyebrow">{c.eyebrow}</p><h1>{c.title}</h1><p>{c.description}</p></div>
      <div className="kw-heading-actions">{readOnly ? <span className="kw-readonly-badge"><Code size={14} />{c.readOnly}</span> : null}{onCreate && !readOnly ? <Button variant="primary" icon={Plus} onClick={beginCreate}>{c.create}</Button> : null}</div>
    </div>
    {error || operationError ? <div className="kw-error" role="alert"><WarningCircle size={18} weight="fill" /><span>{operationError || error || c.errorTitle}</span>{error && onRetry ? <button type="button" className="kw-text-button" onClick={onRetry}><ArrowClockwise size={15} />{c.retry}</button> : null}</div> : null}
    {noticeText ? <div className="kw-notice" role="status"><CheckCircle size={18} weight="fill" /><span>{noticeText}</span><button type="button" className="kw-dismiss" onClick={() => setNotice(null)} aria-label={locale === "zh-CN" ? "关闭提示" : "Dismiss notice"}><X size={15} /></button></div> : null}
    {loading ? <RuleLoadingState label={c.loading} /> : <div className="kw-rules-workbench">
      <aside className="kw-panel kw-rule-list-panel" aria-label={c.listTitle}>
        <div className="kw-list-header"><div><span className="kw-panel-kicker">{c.listTitle}</span><strong>{c.listCount(rules.length)}</strong></div><BookOpenText size={18} className="kw-header-icon" aria-hidden="true" /></div>
        <label className="kw-search"><MagnifyingGlass size={16} aria-hidden="true" /><span className="sr-only">{c.search}</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={c.search} aria-label={c.search} /></label>
        <div className="kw-filter" role="group" aria-label={c.filterLabel}>{(["all", "enabled", "disabled"] as RuleFilter[]).map((value) => <button type="button" key={value} className={filter === value ? "kw-filter-active" : ""} aria-pressed={filter === value} onClick={() => setFilter(value)}>{c[value]}</button>)}</div>
        <div className="kw-rule-list" role="list" aria-label={c.listTitle}>
          {filteredRules.map((rule, index) => { const draft = drafts[rule.id] ?? rule; const active = rule.id === internalSelectedId; return <div className={`kw-rule-row ${active ? "kw-rule-row-active" : ""}`} role="listitem" key={rule.id}>
            <button type="button" className="kw-rule-select" aria-current={active ? "true" : undefined} onClick={() => selectRule(rule)} onKeyDown={(event) => handleSelectKey(event, index)}>
              <span className="kw-rule-leading"><FileText size={16} weight={active ? "fill" : "regular"} aria-hidden="true" /></span>
              <span className="kw-rule-copy"><strong title={rule.name}>{rule.name}</strong><small>{rule.scope?.join(", ") || c.noScope}</small><span className="kw-rule-meta">{draft.draft || !sameRule(draft, committed[rule.id] ?? rule) ? c.draft : c.published}{formatRuleDate(rule.updatedAt, locale) ? `  ${formatRuleDate(rule.updatedAt, locale)}` : ""}</span></span>
              <CaretRight size={15} className="kw-rule-arrow" aria-hidden="true" />
            </button>
            <button type="button" role="switch" aria-checked={draft.enabled} aria-label={draft.enabled ? `${c.onLabel}: ${rule.name}` : `${c.offLabel}: ${rule.name}`} className={`kw-rule-toggle ${draft.enabled ? "kw-rule-toggle-on" : ""}`} onClick={(event) => void toggleRule(rule, event)} disabled={readOnly || busyAction === "toggle"}><span aria-hidden="true" /></button>
          </div>; })}
          {filteredRules.length === 0 ? <RuleEmptyState title={rules.length ? c.noMatches : c.noRules} body={rules.length ? c.noMatchesBody : c.noRulesBody} /> : null}
        </div>
      </aside>
      <section className="kw-panel kw-rule-editor-panel" aria-label={selected ? `${c.selected}: ${selected.name}` : c.selected}>
        {!selected ? <RuleEmptyState title={c.noRules} body={c.noRulesBody} /> : <>
          <div className="kw-editor-header"><div className="kw-editor-title"><span className="kw-editor-icon"><PencilSimple size={17} /></span><div><span className="kw-panel-kicker">{c.selected}</span><h2 title={selected.name}>{selected.name}</h2><div className="kw-editor-status"><StatusBadge rule={selected} text={selected.enabled ? c.ruleEnabled : c.ruleDisabled} />{isDirty ? <span className="kw-draft-state kw-draft-state-dirty">{c.unsaved}</span> : selected.draft ? <span className="kw-draft-state">{c.draft}</span> : <span className="kw-draft-state">{c.published}</span>}</div></div></div><div className="kw-editor-actions">{onDelete ? <button type="button" className="button button-ghost button-sm kw-delete-button" onClick={() => setDeleteTarget(copyRule(selected))} disabled={readOnly || Boolean(busyAction)}><Trash size={15} />{c.delete}</button> : null}<button type="button" className="button button-ghost button-sm" onClick={discard} disabled={readOnly || !isDirty || Boolean(busyAction)}><ArrowUUpLeft size={15} />{c.discard}</button><button type="button" className="button button-secondary button-sm" onClick={() => void save("draft")} disabled={readOnly || !isDirty || !onSave || Boolean(busyAction)}>{busyAction === "draft" ? <CircleNotch className="spin" size={15} /> : <FloppyDisk size={15} />}{busyAction === "draft" ? c.saving : c.saveDraft}</button><button type="button" className="button button-primary button-sm" onClick={() => void save("publish")} disabled={readOnly || !isDirty || !onSave || Boolean(busyAction)}>{busyAction === "publish" ? <CircleNotch className="spin" size={15} /> : <RocketLaunch size={15} />}{busyAction === "publish" ? c.publishing : c.publish}</button></div></div>
          <div className="kw-tabs" role="tablist" aria-label={`${c.selected} views`}><button type="button" role="tab" id="kw-rule-tab-content" aria-controls="kw-rule-tabpanel" aria-selected={tab === "content"} className={tab === "content" ? "kw-tab-active" : ""} onClick={() => setTab("content")} onKeyDown={(event) => handleTabKey(event, "content")} ref={(element) => { tabRefs.current.content = element; }}>{c.contentTab}</button><button type="button" role="tab" id="kw-rule-tab-source" aria-controls="kw-rule-tabpanel" aria-selected={tab === "source"} className={tab === "source" ? "kw-tab-active" : ""} onClick={() => setTab("source")} onKeyDown={(event) => handleTabKey(event, "source")} ref={(element) => { tabRefs.current.source = element; }}>{c.sourceTab}</button><button type="button" role="tab" id="kw-rule-tab-diff" aria-controls="kw-rule-tabpanel" aria-selected={tab === "diff"} className={tab === "diff" ? "kw-tab-active" : ""} onClick={() => setTab("diff")} onKeyDown={(event) => handleTabKey(event, "diff")} ref={(element) => { tabRefs.current.diff = element; }}>{c.diffTab}{isDirty ? <span className="kw-tab-count" aria-label={c.unsaved}>1</span> : null}</button></div>
          <div className="kw-tab-panel" id="kw-rule-tabpanel" role="tabpanel" aria-labelledby={`kw-rule-tab-${tab}`}>
            {tab === "content" ? <div className="kw-rule-content"><div className="kw-content-heading"><div><h3>{c.contentTitle}</h3><p>{c.contentHint}</p></div><StatusBadge rule={selected} text={selected.enabled ? c.enabled : c.disabled} /></div><div className="kw-form-grid"><label className="kw-field kw-field-wide"><span>{c.nameLabel}</span><input className="input" value={selected.name} onChange={(event) => updateSelected({ name: event.target.value })} disabled={readOnly} /></label><label className="kw-field kw-field-wide"><span>{c.contentLabel}</span><textarea className="input kw-rule-textarea" value={selected.content} onChange={(event) => updateSelected({ content: event.target.value })} disabled={readOnly} /></label><TokenEditor label={c.scopeLabel} values={selected.scope ?? []} options={scopeOptions} placeholder={c.addToken} removeLabel={c.removeToken} disabled={readOnly} onChange={(scope) => updateSelected({ scope })} /><TokenEditor label={c.tagsLabel} values={selected.tags ?? []} placeholder={c.addToken} removeLabel={c.removeToken} disabled={readOnly} onChange={(tags) => updateSelected({ tags })} /></div><div className="kw-rule-toggle-row"><div><strong>{selected.enabled ? c.ruleEnabled : c.ruleDisabled}</strong><small>{selected.enabled ? c.enabled : c.disabled}</small></div><button type="button" role="switch" aria-checked={selected.enabled} aria-label={selected.enabled ? `${c.onLabel}: ${selected.name}` : `${c.offLabel}: ${selected.name}`} className={`kw-large-toggle ${selected.enabled ? "kw-large-toggle-on" : ""}`} onClick={(event) => void toggleRule(selected, event)} disabled={readOnly || busyAction === "toggle"}><span aria-hidden="true" /></button></div></div> : null}
            {tab === "source" ? <div className="kw-source-view"><div className="kw-source-heading"><div><h3>{c.sourceTab}</h3><p>{c.sourceHint}</p></div><code title={selected.sourcePath}>{selected.sourcePath}</code></div>{sourceContent ? <pre className="kw-code"><code>{sourceContent}</code></pre> : <RuleEmptyState title={c.sourceUnavailable} body={c.sourceLabel} icon={<Code size={22} weight="duotone" />} />}</div> : null}
            {tab === "diff" ? <div className="kw-source-view"><div className="kw-source-heading"><div><h3>{c.diffTab}</h3><p>{c.diffHint}</p></div><code title={selected.sourcePath}>{selected.sourcePath}</code></div>{diffContent ? <pre className="kw-code kw-diff-code"><code>{diffContent}</code></pre> : <RuleEmptyState title={c.noDiff} body={c.noDiffBody} icon={<Check size={22} weight="bold" />} />}</div> : null}
          </div>
        </>}
      </section>
    </div>}{createModal}{deleteModal}
  </div>;
}

function TokenEditor({ label, values, options = [], placeholder, removeLabel, disabled = false, onChange }: { label: string; values: string[]; options?: string[]; placeholder: string; removeLabel: string; disabled?: boolean; onChange: (values: string[]) => void }) {
  const [input, setInput] = useState("");
  const listId = useId();
  const normalizedValues = new Set(values.map((value) => value.toLocaleLowerCase()));
  const suggestions = options.filter((option) => !normalizedValues.has(option.toLocaleLowerCase()));
  function commit(raw = input) {
    const additions = raw.split(/[,，]/).map((value) => value.trim()).filter(Boolean);
    if (!additions.length) return;
    const next = [...values];
    for (const addition of additions) if (!next.some((value) => value.toLocaleLowerCase() === addition.toLocaleLowerCase())) next.push(addition);
    onChange(next); setInput("");
  }
  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" || event.key === ",") { event.preventDefault(); commit(); }
    if (event.key === "Backspace" && !input && values.length) onChange(values.slice(0, -1));
  }
  return <label className="kw-field kw-token-field"><span>{label}</span><div className={`kw-token-editor ${disabled ? "kw-token-editor-disabled" : ""}`}><Tag size={15} aria-hidden="true" />{values.map((item) => <span className="kw-token" key={item}>{item}{!disabled ? <button type="button" onClick={() => onChange(values.filter((value) => value !== item))} aria-label={`${removeLabel} ${item}`}><X size={12} /></button> : null}</span>)}<input value={input} onChange={(event: ChangeEvent<HTMLInputElement>) => setInput(event.target.value)} onKeyDown={handleKeyDown} onBlur={() => commit()} placeholder={values.length ? "" : placeholder} list={suggestions.length ? listId : undefined} disabled={disabled} aria-label={label} /></div>{suggestions.length ? <datalist id={listId}>{suggestions.map((option) => <option value={option} key={option} />)}</datalist> : null}</label>;
}

export default RuleWorkbench;
