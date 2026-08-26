import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { BracketsCurly, CaretDown, CaretLeft, CaretRight, Check, Code, Eye, FloppyDisk, Key, MagnifyingGlass, Table, WarningCircle } from "@phosphor-icons/react";
import { useTranslation } from "react-i18next";
import type { LocalizedText, ProjectDiff, SemanticColumn, SemanticModel, SemanticProjectSnapshot } from "../types";
import { Badge, Button, EmptyState, Field, Select, TextArea, TextInput, Toggle } from "./ui";

type EditorTab = "details" | "fields" | "source" | "diff";
type EditorLocale = "zh-CN" | "en-US";

const FIELD_PAGE_SIZE = 15;

function editorLocale(language: string): EditorLocale {
  return language === "zh-CN" ? "zh-CN" : "en-US";
}

/**
 * The API keeps both locale slots for backwards compatibility. The workbench
 * exposes one value at a time, using the active UI locale and falling back to
 * the other value when a legacy project has only one translation.
 */
function localizedValue(value: LocalizedText, locale: EditorLocale): string {
  return value[locale] || value["en-US"] || value["zh-CN"];
}

function updateLocalized(value: LocalizedText, locale: EditorLocale, next: string): LocalizedText {
  return { ...value, [locale]: next };
}

function primaryKeyNames(primaryKey: SemanticModel["primaryKey"]): string[] {
  return Array.isArray(primaryKey) ? [...primaryKey] : primaryKey ? [primaryKey] : [];
}

function primaryKeyValue(names: string[]): string | string[] {
  if (names.length === 0) return "";
  return names.length === 1 ? names[0] : names;
}

function orderedPrimaryKeyValue(current: SemanticModel["primaryKey"], selected: string[]): string | string[] {
  const currentNames = primaryKeyNames(current);
  const selectedSet = new Set(selected);
  const retained = currentNames.filter((name) => selectedSet.has(name));
  const added = selected.filter((name) => !currentNames.includes(name));
  return primaryKeyValue([...retained, ...added]);
}

function PrimaryKeyPicker({ model, label, noneLabel, onChange }: { model: SemanticModel; label: string; noneLabel: string; onChange: (value: string | string[]) => void }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = primaryKeyNames(model.primaryKey);
  const options = [...model.columns.map((column) => column.name), ...selected.filter((name) => !model.columns.some((column) => column.name === name))];

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return <div className="primary-key-picker" ref={rootRef}>
    <button id="model-primary-key" type="button" className="primary-key-trigger" aria-label={label} aria-haspopup="true" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <span className={selected.length ? "primary-key-values" : "primary-key-placeholder"}>
        {selected.length ? selected.map((name) => <span className="primary-key-chip" key={name}><Key size={12} aria-hidden="true" />{name}</span>) : noneLabel}
      </span>
      <CaretDown className={open ? "primary-key-caret-open" : ""} size={15} aria-hidden="true" />
    </button>
    {open ? <div className="primary-key-menu" role="group" aria-label={label}>
      {options.map((name) => {
        const checked = selected.includes(name);
        return <label className="primary-key-option" key={name}>
          <input type="checkbox" checked={checked} onChange={(event) => {
            const next = event.target.checked ? [...selected, name] : selected.filter((item) => item !== name);
            onChange(orderedPrimaryKeyValue(model.primaryKey, next));
          }} />
          <span title={name}>{name}</span>
          {checked ? <Check size={14} weight="bold" aria-hidden="true" /> : null}
        </label>;
      })}
    </div> : null}
  </div>;
}

function cloneModel(model: SemanticModel): SemanticModel {
  return {
    ...model,
    primaryKey: Array.isArray(model.primaryKey) ? [...model.primaryKey] : model.primaryKey,
    displayName: { ...model.displayName },
    description: { ...model.description },
    tableReference: { ...model.tableReference },
    columns: model.columns.map((column) => ({
      ...column,
      displayName: { ...column.displayName },
      description: { ...column.description },
    })),
  };
}

/** Compare editable model state while ignoring the server-maintained draft flag. */
function sameModel(a: SemanticModel, b: SemanticModel): boolean {
  const { draft: _aDraft, ...aEditable } = a;
  const { draft: _bDraft, ...bEditable } = b;
  return JSON.stringify(aEditable) === JSON.stringify(bEditable);
}

function updateColumn(model: SemanticModel, name: string, patch: Partial<SemanticColumn>): SemanticModel {
  return { ...model, columns: model.columns.map((column) => column.name === name ? { ...column, ...patch } : column) };
}

/**
 * Dense, keyboard-friendly visual editor for one semantic business model.
 * Source and diff are workspace tabs rather than a second panel below the
 * editor, keeping the selected model and its traceability in one surface.
 */
export function ModelEditor({ snapshot, sourceContent, sourceLoading = false, diff, diffLoading = false, onSave, onOpenSource, onLoadDiff }: ModelEditorProps) {
  const { t, i18n } = useTranslation();
  const locale = editorLocale(i18n.language);
  const [query, setQuery] = useState("");
  const [selectedName, setSelectedName] = useState("");
  const [model, setModel] = useState<SemanticModel | null>(null);
  const [editorTab, setEditorTab] = useState<EditorTab>("details");
  const [fieldQuery, setFieldQuery] = useState("");
  const [saving, setSaving] = useState(false);
  const loadedSourceRef = useRef("");
  // The snapshot revision covers the entire project. Keep a server baseline
  // per model so a change to an unrelated model does not overwrite this
  // model's local draft, and so a genuine same-model change can be surfaced.
  const serverModelsRef = useRef(new Map<string, SemanticModel>());
  const localDraftsRef = useRef(new Map<string, SemanticModel>());
  const localDraftBasesRef = useRef(new Map<string, SemanticModel>());
  const modelRef = useRef<SemanticModel | null>(null);
  const [conflictName, setConflictName] = useState<string | null>(null);
  modelRef.current = model;

  const models = snapshot?.models ?? [];
  const filteredModels = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return models;
    return models.filter((item) => [item.name, item.displayName["zh-CN"], item.displayName["en-US"], item.businessDomain].some((value) => value.toLowerCase().includes(needle)));
  }, [models, query]);

  useEffect(() => {
    if (!models.length) {
      setSelectedName("");
      setModel(null);
      return;
    }
    const target = models.find((item) => item.name === selectedName) ?? filteredModels[0] ?? models[0];
    if (!target) return;

    const current = modelRef.current;
    if (current && current.name !== target.name) rememberDraft(current, serverModelsRef.current);
    if (target.name !== selectedName) setSelectedName(target.name);

    const previousServer = serverModelsRef.current.get(target.name);
    const localDraft = localDraftsRef.current.get(target.name);
    const localDraftBase = localDraftBasesRef.current.get(target.name);
    const serverChanged = Boolean(
      (previousServer && !sameModel(target, previousServer))
      || (localDraftBase && !sameModel(target, localDraftBase)),
    );
    const draftMatchesServer = Boolean(localDraft && sameModel(localDraft, target));

    if (draftMatchesServer) {
      // A successful save is reflected by the next snapshot. Keep the draft
      // until that happens, then let the server projection become canonical.
      localDraftsRef.current.delete(target.name);
      localDraftBasesRef.current.delete(target.name);
    }

    if (localDraft && !draftMatchesServer) {
      // Never replace a dirty local draft merely because the global revision
      // advanced. If the selected model itself changed, make that conflict
      // explicit while retaining the user's complete local draft.
      setModel(cloneModel(localDraft));
      if (serverChanged) setConflictName(target.name);
    } else {
      setModel(cloneModel(target));
      if (serverChanged || conflictName === target.name) setConflictName(null);
    }

    const nextServerModels = new Map<string, SemanticModel>();
    for (const item of models) nextServerModels.set(item.name, cloneModel(item));
    serverModelsRef.current = nextServerModels;
    // Deliberately omit model state: edits are recorded in localDraftsRef and
    // must not trigger this synchronization effect on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models, filteredModels, selectedName, snapshot?.revision]);

  useEffect(() => {
    const target = models.find((item) => item.name === selectedName);
    if (target && target.sourcePath !== loadedSourceRef.current) {
      loadedSourceRef.current = target.sourcePath;
      onOpenSource(target.sourcePath);
    }
  }, [models, onOpenSource, selectedName]);

  function rememberDraft(candidate: SemanticModel | null, serverModels = serverModelsRef.current) {
    if (!candidate) return;
    const serverModel = serverModels.get(candidate.name);
    if (serverModel && sameModel(candidate, serverModel)) {
      localDraftsRef.current.delete(candidate.name);
      localDraftBasesRef.current.delete(candidate.name);
    } else {
      if (!localDraftBasesRef.current.has(candidate.name)) {
        localDraftBasesRef.current.set(candidate.name, cloneModel(serverModel ?? candidate));
      }
      localDraftsRef.current.set(candidate.name, cloneModel(candidate));
    }
  }

  function selectModel(name: string) {
    const next = models.find((item) => item.name === name);
    rememberDraft(modelRef.current);
    const localDraft = next ? localDraftsRef.current.get(next.name) : undefined;
    setSelectedName(name);
    setModel(localDraft ? cloneModel(localDraft) : next ? cloneModel(next) : null);
    setConflictName(null);
    setEditorTab("details");
    setFieldQuery("");
  }

  function updateModel(mutator: (current: SemanticModel) => SemanticModel) {
    setModel((current) => {
      if (!current) return current;
      const next = mutator(current);
      if (!localDraftBasesRef.current.has(next.name)) {
        const serverModel = serverModelsRef.current.get(next.name);
        localDraftBasesRef.current.set(next.name, cloneModel(serverModel ?? current));
      }
      localDraftsRef.current.set(next.name, cloneModel(next));
      return next;
    });
  }

  function update(patch: Partial<SemanticModel>) {
    updateModel((current) => ({ ...current, ...patch }));
  }

  function updateLocalizedModel(field: "displayName" | "description", value: string) {
    updateModel((current) => ({ ...current, [field]: updateLocalized(current[field], locale, value) }));
  }

  function updateColumnLocalized(columnName: string, field: "displayName" | "description", value: string) {
    updateModel((current) => updateColumn(current, columnName, { [field]: updateLocalized(current.columns.find((column) => column.name === columnName)?.[field] ?? { "zh-CN": "", "en-US": "" }, locale, value) }));
  }

  async function save() {
    if (!model || saving) return;
    const draft = cloneModel(model);
    setSaving(true);
    try {
      await onSave(draft);
      // App-level saving updates the semantic snapshot, but the source file
      // is loaded through a separate endpoint/state. Trigger a fresh read so
      // switching to Source never shows the pre-save content.
      onOpenSource(draft.sourcePath);
    } finally { setSaving(false); }
  }

  const activeModel = model;
  const columns = useMemo(() => {
    const needle = fieldQuery.trim().toLowerCase();
    if (!activeModel || !needle) return activeModel?.columns ?? [];
    return activeModel.columns.filter((column) => [column.name, column.type, column.semanticRole, column.displayName["zh-CN"], column.displayName["en-US"], column.description["zh-CN"], column.description["en-US"]].some((value) => value.toLowerCase().includes(needle)));
  }, [activeModel, fieldQuery]);

  function selectEditorTab(tab: EditorTab) {
    setEditorTab(tab);
    if (!activeModel) return;
    if (tab === "source") onOpenSource(activeModel.sourcePath);
    if (tab === "diff") onLoadDiff(activeModel.sourcePath);
  }

  function useServerVersion() {
    if (!activeModel) return;
    const serverModel = models.find((item) => item.name === activeModel.name);
    if (!serverModel) return;
    localDraftsRef.current.delete(serverModel.name);
    localDraftBasesRef.current.delete(serverModel.name);
    setModel(cloneModel(serverModel));
    setConflictName(null);
  }

  if (!snapshot || snapshot.models.length === 0) {
    return <div className="page"><div className="model-editor-heading"><div><p className="eyebrow">{t("model.eyebrow")}</p><h1>{t("model.title")}</h1><p>{t("model.pageDescription")}</p></div></div><section className="panel model-empty-panel"><EmptyState icon={Table} title={t("model.noModels")} body={t("model.noModelsBody")} /></section></div>;
  }

  return <div className="page model-editor-page">
    <div className="model-editor-heading"><div><p className="eyebrow">{t("model.eyebrow")}</p><h1>{t("model.title")}</h1><p>{t("model.pageDescription")}</p></div><div className="model-heading-meta"><Badge tone="neutral">{t("model.modelCount", { count: snapshot.models.length })}</Badge><Badge tone={snapshot.draftCount ? "amber" : "green"} dot>{snapshot.draftCount ? t("common.draft") : t("common.published")}</Badge></div></div>
    <div className="model-workbench">
      <aside className="panel model-list-panel"><div className="model-list-toolbar"><div><span className="panel-kicker">{t("model.title")}</span><strong>{t("model.modelCount", { count: filteredModels.length })}</strong></div><div className="model-search"><MagnifyingGlass size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("model.searchPlaceholder")} aria-label={t("model.searchPlaceholder")} /></div></div><div className="model-list">{filteredModels.map((item) => <button type="button" className={`model-list-item ${item.name === activeModel?.name ? "model-list-item-active" : ""}`} key={item.name} onClick={() => selectModel(item.name)}><span className="model-list-icon"><Table size={16} /></span><span className="model-list-copy"><strong>{localizedValue(item.displayName, locale) || item.name}</strong><small>{item.name}</small><em>{item.tableReference.schema ? `${item.tableReference.schema}.` : ""}{item.tableReference.table || t("common.unknown")}</em></span><span className={`model-status-dot ${item.draft ? "model-status-draft" : ""}`} title={item.draft ? t("common.draft") : t("common.tracked")} /></button>)}{filteredModels.length === 0 ? <EmptyState icon={MagnifyingGlass} title={t("model.noModels")} body={t("model.searchPlaceholder")} /> : null}</div></aside>
      <section className="model-editor-main">
        {activeModel ? <section className="panel model-workspace-panel">
          <div className="model-panel-header"><div><p className="panel-kicker">{activeModel.name}</p><h2>{localizedValue(activeModel.displayName, locale) || activeModel.name}</h2><p>{t("model.modelDetailsHint")}</p></div><div className="model-editor-actions"><Badge tone={activeModel.draft ? "amber" : "neutral"}>{activeModel.draft ? t("common.draft") : t("common.tracked")}</Badge><Button variant="primary" size="sm" icon={FloppyDisk} loading={saving} onClick={() => void save()}>{t("model.saveDraft")}</Button></div></div>
          {conflictName === activeModel.name ? <div className="notice notice-warning model-editor-conflict" role="alert"><WarningCircle size={18} weight="fill" aria-hidden="true" /><div className="notice-content"><strong>{t("model.externalChangeTitle")}</strong><span>{t("model.externalChangeBody")}</span><Button variant="secondary" size="sm" onClick={useServerVersion}>{t("model.useServerVersion")}</Button></div></div> : null}
          <div className="model-workspace-tabs" role="tablist" aria-label={t("model.title")}>
            <WorkspaceTab id="details" active={editorTab} label={t("model.modelDetails")} onClick={selectEditorTab} />
            <WorkspaceTab id="fields" active={editorTab} label={t("model.fields")} count={activeModel.columns.length} onClick={selectEditorTab} />
            <WorkspaceTab id="source" active={editorTab} label={t("model.sourceView")} icon={<Code size={14} />} onClick={selectEditorTab} />
            <WorkspaceTab id="diff" active={editorTab} label={t("model.diffView")} icon={<BracketsCurly size={14} />} onClick={selectEditorTab} />
          </div>
          <div className="model-workspace-content" id="model-workspace-panel" role="tabpanel" aria-label={editorTab === "details" ? t("model.modelDetails") : editorTab === "fields" ? t("model.fields") : editorTab === "source" ? t("model.sourceView") : t("model.diffView")}>
            {editorTab === "details" ? <div className="model-form-grid"><Field label={t("model.technicalName")} htmlFor="model-technical-name"><TextInput id="model-technical-name" value={activeModel.name} readOnly /></Field><Field label={t("model.sourceFile")} htmlFor="model-source-file"><div className="source-path-control"><TextInput id="model-source-file" value={activeModel.sourcePath} readOnly /><button type="button" title={t("model.sourceView")} onClick={() => onOpenSource(activeModel.sourcePath)}><Code size={15} /></button></div></Field><Field label={t("model.schema")} htmlFor="model-schema"><TextInput id="model-schema" value={activeModel.tableReference.schema} onChange={(event) => update({ tableReference: { ...activeModel.tableReference, schema: event.target.value } })} /></Field><Field label={t("model.table")} htmlFor="model-table"><TextInput id="model-table" value={activeModel.tableReference.table} onChange={(event) => update({ tableReference: { ...activeModel.tableReference, table: event.target.value } })} /></Field><Field label={t("model.businessDomain")} htmlFor="model-domain"><TextInput id="model-domain" value={activeModel.businessDomain} onChange={(event) => update({ businessDomain: event.target.value })} placeholder="Finance / 财务" /></Field><Field label={t("model.primaryKey")} htmlFor="model-primary-key"><PrimaryKeyPicker model={activeModel} label={t("model.primaryKey")} noneLabel={t("common.none")} onChange={(primaryKey) => update({ primaryKey })} /></Field><div className="model-form-toggle"><Toggle checked={activeModel.visible} onChange={(visible) => update({ visible })} label={t("model.modelVisible")} /><span><strong>{t("model.modelVisible")}</strong><small>{activeModel.visible ? t("common.visible") : t("common.hidden")}</small></span></div><Field label={t("model.bilingualName")} htmlFor="model-business-name"><TextInput id="model-business-name" aria-label={t("model.bilingualName")} value={localizedValue(activeModel.displayName, locale)} onChange={(event) => updateLocalizedModel("displayName", event.target.value)} placeholder={activeModel.name} /></Field><Field label={t("model.bilingualDescription")} htmlFor="model-business-description"><TextArea id="model-business-description" aria-label={t("model.bilingualDescription")} value={localizedValue(activeModel.description, locale)} onChange={(event) => updateLocalizedModel("description", event.target.value)} rows={3} /></Field></div> : null}
            {editorTab === "fields" ? <FieldDictionary key={activeModel.name} model={activeModel} columns={columns} fieldQuery={fieldQuery} setFieldQuery={setFieldQuery} locale={locale} onColumnPatch={(name, patch) => updateModel((current) => updateColumn(current, name, patch))} onColumnLocalized={updateColumnLocalized} /> : null}
            {editorTab === "source" ? <SourceView sourceContent={sourceContent} sourceLoading={sourceLoading} diff={null} diffLoading={false} mode="source" model={activeModel} onOpenSource={onOpenSource} t={t} /> : null}
            {editorTab === "diff" ? <SourceView sourceContent={sourceContent} sourceLoading={false} diff={diff ?? null} diffLoading={diffLoading} mode="diff" model={activeModel} onOpenSource={onOpenSource} t={t} /> : null}
          </div>
        </section> : <section className="panel model-empty-panel"><EmptyState icon={WarningCircle} title={t("model.noModels")} body={t("model.noModelsBody")} /></section>}
      </section>
    </div>
  </div>;
}

export interface ModelEditorProps {
  snapshot: SemanticProjectSnapshot | null;
  sourceContent?: string;
  sourceLoading?: boolean;
  diff?: ProjectDiff | null;
  diffLoading?: boolean;
  onSave: (model: SemanticModel) => Promise<void> | void;
  onOpenSource: (path: string) => void;
  onLoadDiff: (path: string) => void;
}

function WorkspaceTab({ id, active, label, count, icon, onClick }: { id: EditorTab; active: EditorTab; label: string; count?: number; icon?: ReactNode; onClick: (tab: EditorTab) => void }) {
  return <button type="button" id={`model-tab-${id}`} className={active === id ? "model-workspace-tab-active" : ""} onClick={() => onClick(id)} role="tab" aria-selected={active === id} aria-controls="model-workspace-panel">{icon}{label}{count !== undefined ? <span>{count}</span> : null}</button>;
}

function FieldDictionary({ model, columns, fieldQuery, setFieldQuery, locale, onColumnPatch, onColumnLocalized }: { model: SemanticModel; columns: SemanticColumn[]; fieldQuery: string; setFieldQuery: (value: string) => void; locale: EditorLocale; onColumnPatch: (name: string, patch: Partial<SemanticColumn>) => void; onColumnLocalized: (name: string, field: "displayName" | "description", value: string) => void }) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const [expandedFields, setExpandedFields] = useState<Set<string>>(new Set());
  const pageCount = Math.max(1, Math.ceil(columns.length / FIELD_PAGE_SIZE));
  const pageColumns = columns.slice((page - 1) * FIELD_PAGE_SIZE, page * FIELD_PAGE_SIZE);

  useEffect(() => {
    setPage(1);
    setExpandedFields(new Set());
  }, [fieldQuery, model.name]);

  useEffect(() => {
    setPage((current) => Math.min(current, pageCount));
  }, [pageCount]);

  function toggleField(name: string) {
    setExpandedFields((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  }

  const rangeStart = columns.length === 0 ? 0 : (page - 1) * FIELD_PAGE_SIZE + 1;
  const rangeEnd = Math.min(page * FIELD_PAGE_SIZE, columns.length);
  const range = locale === "zh-CN" ? `${rangeStart}-${rangeEnd} / ${columns.length}` : `${rangeStart}-${rangeEnd} of ${columns.length}`;
  const previousLabel = locale === "zh-CN" ? "上一页" : "Previous page";
  const nextLabel = locale === "zh-CN" ? "下一页" : "Next page";
  const pageLabel = locale === "zh-CN" ? `第 ${page} / ${pageCount} 页` : `Page ${page} of ${pageCount}`;

  return <div className="field-dictionary"><div className="field-dictionary-toolbar"><div><strong>{t("model.fields")}</strong><span>{t("model.fieldCount", { count: model.columns.length })}{columns.length !== model.columns.length ? ` · ${range}` : ""}</span></div><div className="model-search field-search"><MagnifyingGlass size={15} /><input value={fieldQuery} onChange={(event) => setFieldQuery(event.target.value)} placeholder={t("model.fieldSearch")} aria-label={t("model.fieldSearch")} /></div></div>{columns.length === 0 ? <div className="field-table-empty"><EmptyState icon={MagnifyingGlass} title={t("model.noModels")} body={t("model.fieldSearch")} /></div> : <><div className="field-table-wrap"><div className="field-table field-table-head"><span>{t("model.field")}</span><span>{t("model.displayName")}</span><span>{t("model.type")}</span><span>{t("model.role")}</span><span>{t("model.visible")}</span><span aria-hidden="true" /></div>{pageColumns.map((column) => <FieldRow key={column.name} column={column} expanded={expandedFields.has(column.name)} locale={locale} onToggle={() => toggleField(column.name)} onColumnPatch={onColumnPatch} onColumnLocalized={onColumnLocalized} />)}</div><div className="field-pagination"><span>{range}</span><div><button type="button" className="field-page-button" aria-label={previousLabel} disabled={page <= 1} onClick={() => setPage((current) => Math.max(1, current - 1))}><CaretLeft size={15} /></button><span className="field-page-current" aria-live="polite">{pageLabel}</span><button type="button" className="field-page-button" aria-label={nextLabel} disabled={page >= pageCount} onClick={() => setPage((current) => Math.min(pageCount, current + 1))}><CaretRight size={15} /></button></div></div></>}</div>;
}

function FieldRow({ column, expanded, locale, onToggle, onColumnPatch, onColumnLocalized }: { column: SemanticColumn; expanded: boolean; locale: EditorLocale; onToggle: () => void; onColumnPatch: (name: string, patch: Partial<SemanticColumn>) => void; onColumnLocalized: (name: string, field: "displayName" | "description", value: string) => void }) {
  const { t } = useTranslation();
  const displayName = localizedValue(column.displayName, locale) || column.name;
  const description = localizedValue(column.description, locale);
  const role = column.semanticRole === "dimension" ? t("model.dimension") : column.semanticRole === "measure" ? t("model.measure") : column.semanticRole === "time" ? t("model.time") : column.semanticRole === "key" ? t("model.key") : column.semanticRole || t("common.none");
  const editorId = `field-editor-${column.name.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  const visibleLabel = `${column.name} ${t("model.visible")}`;
  const keyLabel = `${column.name} ${t("model.primaryKey")}`;
  const notNullLabel = `${column.name} ${t("model.notNull")}`;

  return <div className={`field-table field-table-row ${expanded ? "field-row-expanded" : ""}`}><div className="field-summary-primary"><button type="button" className="field-summary-toggle" aria-expanded={expanded} aria-controls={editorId} onClick={onToggle}><span className="field-icon">{column.primaryKey ? <Key size={14} /> : <Table size={14} />}</span><span className="field-identity-copy"><strong title={column.name}>{column.name}</strong><small title={description || t("common.none")}>{description || (column.calculated ? t("model.calculated") : t("common.none"))}</small></span><CaretDown className={`field-expand-icon ${expanded ? "field-expand-icon-open" : ""}`} size={15} /></button></div><span className="field-summary-display" title={displayName}>{displayName}</span><span className="field-type" title={column.type}>{column.type}</span><span className="field-summary-role">{role}</span><span className={`field-summary-visible ${column.visible ? "field-visible" : "field-hidden"}`}>{column.visible ? t("common.visible") : t("common.hidden")}</span><span className="field-summary-action" aria-hidden="true">{expanded ? "−" : "+"}</span>{expanded ? <div className="field-expanded-editor" id={editorId}><div className="field-advanced-grid"><Field label={t("model.displayName")} htmlFor={`${editorId}-name`}><TextInput id={`${editorId}-name`} value={localizedValue(column.displayName, locale)} onChange={(event) => onColumnLocalized(column.name, "displayName", event.target.value)} placeholder={column.name} /></Field><Field label={t("model.format")} htmlFor={`${editorId}-format`}><Select id={`${editorId}-format`} value={column.format} onChange={(event) => onColumnPatch(column.name, { format: event.target.value })}><option value="auto">{t("model.auto")}</option><option value="number">{t("model.number")}</option><option value="currency">{t("model.currency")}</option><option value="percentage">{t("model.percentage")}</option><option value="date">{t("model.date")}</option></Select></Field><Field label={t("model.description")} htmlFor={`${editorId}-description`}><TextArea id={`${editorId}-description`} value={localizedValue(column.description, locale)} onChange={(event) => onColumnLocalized(column.name, "description", event.target.value)} rows={2} /></Field><Field label={t("model.expression")} htmlFor={`${editorId}-expression`}><TextInput id={`${editorId}-expression`} value={column.expression} onChange={(event) => onColumnPatch(column.name, { expression: event.target.value, calculated: Boolean(event.target.value) })} placeholder="SUM(amount)" /></Field></div><div className="field-advanced-toggles"><div className="field-toggle-option"><Toggle checked={column.primaryKey} onChange={(primaryKey) => onColumnPatch(column.name, { primaryKey })} label={keyLabel} /><span>{t("model.primaryKey")}</span></div><div className="field-toggle-option"><Toggle checked={column.notNull} onChange={(notNull) => onColumnPatch(column.name, { notNull })} label={notNullLabel} /><span>{t("model.notNull")}</span></div><div className="field-toggle-option"><Toggle checked={column.visible} onChange={(visible) => onColumnPatch(column.name, { visible })} label={visibleLabel} /><span>{t("model.visible")}</span></div></div></div> : null}</div>;
}

function SourceView({ sourceContent, sourceLoading, diff, diffLoading, mode, model, onOpenSource, t }: { sourceContent?: string; sourceLoading: boolean; diff: ProjectDiff | null; diffLoading: boolean; mode: "source" | "diff"; model: SemanticModel; onOpenSource: (path: string) => void; t: (key: string) => string }) {
  const sourceMode = mode === "source";
  return <div className="model-workspace-source"><div className="model-source-meta"><div><span className="panel-kicker">{t("model.sourceFile")}</span><strong title={model.sourcePath}>{model.sourcePath}</strong></div><button type="button" className="model-source-open" onClick={() => onOpenSource(model.sourcePath)}><Code size={14} />{t("nav.mdl")}</button></div><div className="source-panel-hint"><Eye size={14} />{sourceMode ? t("model.sourceHint") : t("model.diffHint")}</div>{sourceMode ? sourceLoading ? <div className="source-loading" role="status">{t("common.loading")}</div> : <pre className="model-source-code">{sourceContent || t("model.sourceUnavailable")}</pre> : diffLoading ? <div className="source-loading" role="status">{t("common.loading")}</div> : diff?.changed ? <pre className="model-source-code model-diff-code">{diff.diff}</pre> : <div className="model-no-diff"><Check size={18} /><strong>{t("model.noDiff")}</strong><span>{t("model.noDiffBody")}</span></div>}</div>;
}

export default ModelEditor;
