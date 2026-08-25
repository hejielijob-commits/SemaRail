import { useEffect, useMemo, useRef, useState } from "react";
import { BracketsCurly, Check, Code, Eye, FloppyDisk, Key, MagnifyingGlass, Table, WarningCircle } from "@phosphor-icons/react";
import { useTranslation } from "react-i18next";
import type { ProjectDiff, SemanticColumn, SemanticModel, SemanticProjectSnapshot } from "../types";
import { Badge, Button, EmptyState, Field, Select, TextArea, TextInput, Toggle } from "./ui";

type EditorTab = "details" | "fields";
type SourceTab = "source" | "diff";

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

const emptyLocalized = { "zh-CN": "", "en-US": "" } as const;

function cloneModel(model: SemanticModel): SemanticModel {
  return {
    ...model,
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

function updateColumn(model: SemanticModel, name: string, patch: Partial<SemanticColumn>): SemanticModel {
  return { ...model, columns: model.columns.map((column) => column.name === name ? { ...column, ...patch } : column) };
}

/**
 * Dense, keyboard-friendly visual editor for one semantic business model.
 * Source and diff panels are intentionally part of this surface so business
 * edits remain traceable to the underlying Wren files.
 */
export function ModelEditor({ snapshot, sourceContent, sourceLoading = false, diff, diffLoading = false, onSave, onOpenSource, onLoadDiff }: ModelEditorProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [selectedName, setSelectedName] = useState("");
  const [model, setModel] = useState<SemanticModel | null>(null);
  const [editorTab, setEditorTab] = useState<EditorTab>("details");
  const [sourceTab, setSourceTab] = useState<SourceTab>("source");
  const [fieldQuery, setFieldQuery] = useState("");
  const [saving, setSaving] = useState(false);
  const loadedSourceRef = useRef("");

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
    if (target && target.name !== selectedName) setSelectedName(target.name);
    if (target && (!model || model.name !== target.name || !model.draft)) setModel(cloneModel(target));
    // The snapshot revision changes after a save. Refresh the selected model
    // only when the server projection still represents the same model.
    if (target && model && model.name === target.name && snapshot?.revision !== undefined && target.draft !== model.draft) setModel(cloneModel(target));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models, filteredModels, selectedName, snapshot?.revision]);

  useEffect(() => {
    const target = models.find((item) => item.name === selectedName);
    if (target && target.sourcePath !== loadedSourceRef.current) {
      loadedSourceRef.current = target.sourcePath;
      onOpenSource(target.sourcePath);
    }
  }, [models, onOpenSource, selectedName]);

  function selectModel(name: string) {
    const next = models.find((item) => item.name === name);
    setSelectedName(name);
    setModel(next ? cloneModel(next) : null);
    setEditorTab("details");
    setSourceTab("source");
    setFieldQuery("");
  }

  function update(patch: Partial<SemanticModel>) {
    setModel((current) => current ? { ...current, ...patch } : current);
  }

  function updateLocale(field: "displayName" | "description", locale: "zh-CN" | "en-US", value: string) {
    setModel((current) => current ? { ...current, [field]: { ...current[field], [locale]: value } } : current);
  }

  function updateColumnLocale(columnName: string, field: "displayName" | "description", locale: "zh-CN" | "en-US", value: string) {
    setModel((current) => current ? updateColumn(current, columnName, { [field]: { ...current.columns.find((column) => column.name === columnName)?.[field], [locale]: value } }) : current);
  }

  async function save() {
    if (!model) return;
    setSaving(true);
    try { await onSave(model); } finally { setSaving(false); }
  }

  const columns = useMemo(() => {
    const needle = fieldQuery.trim().toLowerCase();
    if (!model || !needle) return model?.columns ?? [];
    return model.columns.filter((column) => [column.name, column.type, column.semanticRole, column.displayName["zh-CN"], column.displayName["en-US"]].some((value) => value.toLowerCase().includes(needle)));
  }, [fieldQuery, model]);

  if (!snapshot || snapshot.models.length === 0) {
    return <div className="page"><div className="model-editor-heading"><div><p className="eyebrow">{t("model.eyebrow")}</p><h1>{t("model.title")}</h1><p>{t("model.pageDescription")}</p></div></div><section className="panel model-empty-panel"><EmptyState icon={Table} title={t("model.noModels")} body={t("model.noModelsBody")} /></section></div>;
  }

  const activeModel = model;
  return <div className="page model-editor-page">
    <div className="model-editor-heading"><div><p className="eyebrow">{t("model.eyebrow")}</p><h1>{t("model.title")}</h1><p>{t("model.pageDescription")}</p></div><div className="model-heading-meta"><Badge tone="neutral">{t("model.modelCount", { count: snapshot.models.length })}</Badge><Badge tone={snapshot.draftCount ? "amber" : "green"} dot>{snapshot.draftCount ? t("common.draft") : t("common.published")}</Badge></div></div>
    <div className="model-workbench">
      <aside className="panel model-list-panel"><div className="model-list-toolbar"><div><span className="panel-kicker">{t("model.title")}</span><strong>{t("model.modelCount", { count: filteredModels.length })}</strong></div><div className="model-search"><MagnifyingGlass size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("model.searchPlaceholder")} aria-label={t("model.searchPlaceholder")} /></div></div><div className="model-list">{filteredModels.map((item) => <button className={`model-list-item ${item.name === activeModel?.name ? "model-list-item-active" : ""}`} key={item.name} onClick={() => selectModel(item.name)}><span className="model-list-icon"><Table size={16} /></span><span className="model-list-copy"><strong>{item.displayName["zh-CN"] || item.displayName["en-US"] || item.name}</strong><small>{item.name}</small><em>{item.tableReference.schema ? `${item.tableReference.schema}.` : ""}{item.tableReference.table || t("common.unknown")}</em></span><span className={`model-status-dot ${item.draft ? "model-status-draft" : ""}`} title={item.draft ? t("common.draft") : t("common.tracked")} /></button>)}{filteredModels.length === 0 ? <EmptyState icon={MagnifyingGlass} title={t("model.noModels")} body={t("model.searchPlaceholder")} /> : null}</div></aside>
      <section className="model-editor-main">
        {activeModel ? <>
          <div className="panel model-details-panel"><div className="model-panel-header"><div><p className="panel-kicker">{activeModel.name}</p><h2>{activeModel.displayName["zh-CN"] || activeModel.displayName["en-US"] || activeModel.name}</h2><p>{t("model.modelDetailsHint")}</p></div><div className="model-editor-actions"><Badge tone={activeModel.draft ? "amber" : "neutral"}>{activeModel.draft ? t("common.draft") : t("common.tracked")}</Badge><Button variant="primary" size="sm" icon={FloppyDisk} loading={saving} onClick={() => void save()}>{t("model.saveDraft")}</Button></div></div><div className="editor-tabs" role="tablist"><button className={editorTab === "details" ? "editor-tab-active" : ""} onClick={() => setEditorTab("details")} role="tab" aria-selected={editorTab === "details"}>{t("model.modelDetails")}</button><button className={editorTab === "fields" ? "editor-tab-active" : ""} onClick={() => setEditorTab("fields")} role="tab" aria-selected={editorTab === "fields"}>{t("model.fields")} <span>{activeModel.columns.length}</span></button></div>{editorTab === "details" ? <div className="model-form-grid"><Field label={t("model.technicalName")} htmlFor="model-technical-name"><TextInput id="model-technical-name" value={activeModel.name} readOnly /></Field><Field label={t("model.sourceFile")} htmlFor="model-source-file"><div className="source-path-control"><TextInput id="model-source-file" value={activeModel.sourcePath} readOnly /><button type="button" title={t("model.sourceView")} onClick={() => onOpenSource(activeModel.sourcePath)}><Code size={15} /></button></div></Field><Field label={t("model.schema")} htmlFor="model-schema"><TextInput id="model-schema" value={activeModel.tableReference.schema} onChange={(event) => update({ tableReference: { ...activeModel.tableReference, schema: event.target.value } })} /></Field><Field label={t("model.table")} htmlFor="model-table"><TextInput id="model-table" value={activeModel.tableReference.table} onChange={(event) => update({ tableReference: { ...activeModel.tableReference, table: event.target.value } })} /></Field><Field label={t("model.businessDomain")} htmlFor="model-domain"><TextInput id="model-domain" value={activeModel.businessDomain} onChange={(event) => update({ businessDomain: event.target.value })} placeholder="Finance / 财务" /></Field><Field label={t("model.primaryKey")} htmlFor="model-primary-key"><Select id="model-primary-key" value={activeModel.primaryKey} onChange={(event) => update({ primaryKey: event.target.value })}><option value="">{t("common.none")}</option>{activeModel.columns.map((column) => <option key={column.name} value={column.name}>{column.name}</option>)}</Select></Field><div className="model-form-toggle"><Toggle checked={activeModel.visible} onChange={(visible) => update({ visible })} label={t("model.modelVisible")} /><span><strong>{t("model.modelVisible")}</strong><small>{activeModel.visible ? t("common.visible") : t("common.hidden")}</small></span></div><div className="localized-field"><div className="localized-field-heading"><span>{t("model.bilingualName")}</span><small>{t("model.chinese")} / {t("model.english")}</small></div><div className="localized-input"><span>中</span><TextInput aria-label={`${t("model.bilingualName")} ${t("model.chinese")}`} value={activeModel.displayName["zh-CN"]} onChange={(event) => updateLocale("displayName", "zh-CN", event.target.value)} placeholder={activeModel.name} /></div><div className="localized-input"><span>EN</span><TextInput aria-label={`${t("model.bilingualName")} ${t("model.english")}`} value={activeModel.displayName["en-US"]} onChange={(event) => updateLocale("displayName", "en-US", event.target.value)} placeholder={activeModel.name} /></div></div><div className="localized-field localized-field-wide"><div className="localized-field-heading"><span>{t("model.bilingualDescription")}</span></div><div className="localized-input"><span>中</span><TextArea aria-label={`${t("model.bilingualDescription")} ${t("model.chinese")}`} value={activeModel.description["zh-CN"]} onChange={(event) => updateLocale("description", "zh-CN", event.target.value)} rows={2} /></div><div className="localized-input"><span>EN</span><TextArea aria-label={`${t("model.bilingualDescription")} ${t("model.english")}`} value={activeModel.description["en-US"]} onChange={(event) => updateLocale("description", "en-US", event.target.value)} rows={2} /></div></div></div> : <FieldDictionary model={activeModel} columns={columns} fieldQuery={fieldQuery} setFieldQuery={setFieldQuery} onColumnPatch={(name, patch) => setModel((current) => current ? updateColumn(current, name, patch) : current)} onColumnLocale={updateColumnLocale} />}</div>
          <div className="panel model-source-panel"><div className="source-panel-header"><div><span className="panel-kicker">{t("model.sourceFile")}</span><strong>{activeModel.sourcePath}</strong></div><div className="source-view-tabs" role="tablist"><button className={sourceTab === "source" ? "source-view-active" : ""} onClick={() => setSourceTab("source")}><Code size={14} />{t("model.sourceView")}</button><button className={sourceTab === "diff" ? "source-view-active" : ""} onClick={() => { setSourceTab("diff"); onLoadDiff(activeModel.sourcePath); }}><BracketsCurly size={14} />{t("model.diffView")}</button></div></div><div className="source-panel-hint"><Eye size={14} />{sourceTab === "source" ? t("model.sourceHint") : t("model.diffHint")}<button type="button" onClick={() => onOpenSource(activeModel.sourcePath)}>{t("nav.mdl")}</button></div>{sourceTab === "source" ? sourceLoading ? <div className="source-loading">{t("common.loading")}</div> : <pre className="model-source-code">{sourceContent || t("model.sourceUnavailable")}</pre> : diffLoading ? <div className="source-loading">{t("common.loading")}</div> : diff?.changed ? <pre className="model-source-code model-diff-code">{diff.diff}</pre> : <div className="model-no-diff"><Check size={18} /><strong>{t("model.noDiff")}</strong><span>{t("model.noDiffBody")}</span></div>}</div>
        </> : <section className="panel model-empty-panel"><EmptyState icon={WarningCircle} title={t("model.noModels")} body={t("model.noModelsBody")} /></section>}
      </section>
    </div>
  </div>;
}

function FieldDictionary({ model, columns, fieldQuery, setFieldQuery, onColumnPatch, onColumnLocale }: { model: SemanticModel; columns: SemanticColumn[]; fieldQuery: string; setFieldQuery: (value: string) => void; onColumnPatch: (name: string, patch: Partial<SemanticColumn>) => void; onColumnLocale: (name: string, field: "displayName" | "description", locale: "zh-CN" | "en-US", value: string) => void }) {
  const { t } = useTranslation();
  return <div className="field-dictionary"><div className="field-dictionary-toolbar"><div><strong>{t("model.fields")}</strong><span>{t("model.fieldCount", { count: model.columns.length })}</span></div><div className="model-search field-search"><MagnifyingGlass size={15} /><input value={fieldQuery} onChange={(event) => setFieldQuery(event.target.value)} placeholder={t("model.fieldSearch")} aria-label={t("model.fieldSearch")} /></div></div><div className="field-table-wrap"><div className="field-table field-table-head"><span>{t("model.field")}</span><span>{t("model.displayName")}</span><span>{t("model.type")}</span><span>{t("model.role")}</span><span>{t("model.format")}</span><span>{t("model.visible")}</span><span>{t("model.primaryKey")}</span><span>{t("model.notNull")}</span></div>{columns.map((column) => <div className="field-table field-table-row" key={column.name}><div className="field-identity"><span className="field-icon">{column.primaryKey ? <Key size={14} /> : <Table size={14} />}</span><span><strong>{column.name}</strong><small>{column.calculated ? t("model.calculated") : column.expression ? t("model.expression") : column.description["en-US"] || column.description["zh-CN"] || t("common.none")}</small></span></div><div className="field-localized"><TextInput value={column.displayName["zh-CN"]} onChange={(event) => onColumnLocale(column.name, "displayName", "zh-CN", event.target.value)} placeholder={column.name} /><TextInput value={column.displayName["en-US"]} onChange={(event) => onColumnLocale(column.name, "displayName", "en-US", event.target.value)} placeholder={column.name} /></div><span className="field-type">{column.type}</span><Select aria-label={`${column.name} ${t("model.role")}`} value={column.semanticRole} onChange={(event) => onColumnPatch(column.name, { semanticRole: event.target.value })}><option value="dimension">{t("model.dimension")}</option><option value="measure">{t("model.measure")}</option><option value="time">{t("model.time")}</option><option value="key">{t("model.key")}</option></Select><Select aria-label={`${column.name} ${t("model.format")}`} value={column.format} onChange={(event) => onColumnPatch(column.name, { format: event.target.value })}><option value="auto">{t("model.auto")}</option><option value="number">{t("model.number")}</option><option value="currency">{t("model.currency")}</option><option value="percentage">{t("model.percentage")}</option><option value="date">{t("model.date")}</option></Select><Toggle checked={column.visible} onChange={(visible) => onColumnPatch(column.name, { visible })} label={`${column.name} ${t("model.visible")}`} /><Toggle checked={column.primaryKey} onChange={(primaryKey) => onColumnPatch(column.name, { primaryKey })} label={`${column.name} ${t("model.primaryKey")}`} /><Toggle checked={column.notNull} onChange={(notNull) => onColumnPatch(column.name, { notNull })} label={`${column.name} ${t("model.notNull")}`} /><div className="field-expression"><label>{t("model.expression")}</label><TextInput value={column.expression} onChange={(event) => onColumnPatch(column.name, { expression: event.target.value, calculated: Boolean(event.target.value) })} placeholder="SUM(amount)" /><div className="field-descriptions"><TextArea value={column.description["zh-CN"]} onChange={(event) => onColumnLocale(column.name, "description", "zh-CN", event.target.value)} placeholder={t("model.chinese")} rows={1} /><TextArea value={column.description["en-US"]} onChange={(event) => onColumnLocale(column.name, "description", "en-US", event.target.value)} placeholder={t("model.english")} rows={1} /></div></div></div>)}{columns.length === 0 ? <EmptyState icon={MagnifyingGlass} title={t("model.noModels")} body={t("model.fieldSearch")} /> : null}</div></div>;
}

export default ModelEditor;
