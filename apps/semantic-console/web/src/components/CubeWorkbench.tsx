import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  BracketsCurly,
  Check,
  CheckCircle,
  Code,
  Cube,
  ArrowClockwise,
  FloppyDisk,
  MagnifyingGlass,
  Plus,
  Trash,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { Badge, Button, EmptyState, Field, InlineNotice, LoadingRows, Modal, Select, TextArea, TextInput } from "./ui";
import "./cube-workbench.css";

export type CubeLocale = "zh-CN" | "en-US";

/** A measure, dimension, or time dimension in a Wren cube. */
export interface CubeField {
  name: string;
  expression: string;
  type: string;
  [key: string]: unknown;
}

/** The stable projection edited by the cube workbench. */
export interface CubeDefinition {
  name: string;
  sourcePath: string;
  baseObject: string;
  measures: CubeField[];
  dimensions: CubeField[];
  timeDimensions: CubeField[];
  hierarchies: Record<string, string[]>;
  refreshTime?: string;
  properties?: Record<string, unknown>;
  draft?: boolean;
  [key: string]: unknown;
}

/** The response shape returned by the semantic console cube API. */
export interface CubeProjectSnapshot {
  revision: string;
  draftCount: number;
  cubes: CubeDefinition[];
  sourceFiles: Array<{ path: string; draft?: boolean; size?: number; [key: string]: unknown }>;
  availableBaseObjects?: string[];
}

export interface CubeProjectDiff {
  path: string;
  changed: boolean;
  diff: string;
  revision: string;
}

export interface CubeValidationIssue {
  path: string;
  message: string;
  severity: "error" | "warning";
}

export interface CubeValidationResult {
  valid: boolean;
  errors: CubeValidationIssue[];
  warnings: CubeValidationIssue[];
}

type CubeTab = "details" | "measures" | "dimensions" | "timeDimensions" | "hierarchies" | "source" | "diff";

const NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_$-]*$/;

const copy = {
  "en-US": {
    eyebrow: "Semantic layer",
    title: "Cubes",
    pageDescription: "Define governed measures and dimensions over a single Wren model or view.",
    cubeCount: (count: number) => `${count} ${count === 1 ? "cube" : "cubes"}`,
    modelCount: (count: number) => `${count} ${count === 1 ? "model" : "models"}`,
    tracked: "Tracked",
    draft: "Draft",
    emptyTitle: "No cubes yet",
    emptyBody: "Add a cube to give recurring aggregations a stable semantic interface.",
    createCube: "Create cube",
    createTitle: "Create a cube",
    createDescription: "Name the semantic interface and choose the model or view it will aggregate.",
    createNameHint: "Use a stable identifier such as sales_overview.",
    createPath: "Source path",
    cancel: "Cancel",
    creating: "Creating",
    createInvalidName: "Use letters, numbers, underscores, dollar signs, or hyphens, and start with a letter or underscore.",
    createDuplicateName: "A cube with this name already exists.",
    createBaseRequired: "Choose a base model or view.",
    search: "Search cubes",
    noMatches: "No matching cubes",
    noMatchesBody: "Try another name or clear the search.",
    details: "Basic information",
    measures: "Measures",
    dimensions: "Dimensions",
    timeDimensions: "Time dimensions",
    hierarchies: "Hierarchies",
    source: "Source",
    diff: "Diff",
    technicalName: "Technical name",
    sourceFile: "Source file",
    baseObject: "Base object",
    baseObjectHint: "Choose the model or view this cube aggregates over.",
    refreshTime: "Refresh time",
    refreshTimeHint: "Optional cache refresh interval, for example 15 minutes.",
    description: "Description",
    descriptionHint: "Optional context for people maintaining this cube.",
    noBaseObjects: "No models or views are available",
    chooseBaseObject: "Choose a model or view",
    fieldCount: (count: number) => `${count} ${count === 1 ? "field" : "fields"}`,
    fieldName: "Name",
    expression: "Expression",
    type: "Type",
    expressionHint: "Use a Wren expression, such as SUM(amount) or order_date.",
    addMeasure: "Add measure",
    addDimension: "Add dimension",
    addTimeDimension: "Add time dimension",
    addHierarchy: "Add hierarchy",
    remove: "Remove",
    hierarchyName: "Hierarchy name",
    hierarchyLevels: "Levels",
    hierarchyLevelsHint: "Comma-separated dimension or time-dimension names, in drill-down order.",
    noEntries: (label: string) => `No ${label.toLowerCase()} defined yet.`,
    noHierarchies: "No hierarchies defined yet.",
    validate: "Validate structure",
    valid: "Structure is valid",
    validationFailed: "Fix the highlighted fields before saving.",
    validationWarnings: (count: number) => `${count} warning${count === 1 ? "" : "s"}`,
    saveDraft: "Save draft",
    saving: "Saving",
    sourceHint: "Read-only view of the source file. Edit through the source workspace when needed.",
    diffHint: "Changes against the last published file.",
    sourceLoading: "Loading source",
    diffLoading: "Loading diff",
    sourceUnavailable: "Source is unavailable",
    sourceUnavailableBody: "The API did not return content for this cube file.",
    diffUnavailable: "No source changes",
    diffUnavailableBody: "This cube has no unpublished changes.",
    selectCube: "Select a cube",
    selectCubeBody: "Choose a cube from the list to inspect and edit it.",
    required: "Required",
    retry: "Retry",
    duplicate: "Duplicate name",
    unknownBase: (name: string) => `Base object '${name}' is not a model or view in this project.`,
    noMeasuresWarning: "A cube without measures can be saved as a draft, but Wren will warn during validation.",
  },
  "zh-CN": {
    eyebrow: "语义层",
    title: "指标立方体",
    pageDescription: "围绕单个 Wren 模型或视图定义受治理的度量和维度。",
    cubeCount: (count: number) => `${count} 个立方体`,
    modelCount: (count: number) => `${count} 个模型或视图`,
    tracked: "已跟踪",
    draft: "草稿",
    emptyTitle: "还没有立方体",
    emptyBody: "添加一个立方体，为常用聚合提供稳定的语义接口。",
    createCube: "创建立方体",
    createTitle: "创建立方体",
    createDescription: "先定义稳定的技术名称，并选择该立方体聚合所基于的模型或视图。",
    createNameHint: "使用稳定标识符，例如 sales_overview。",
    createPath: "源文件路径",
    cancel: "取消",
    creating: "创建中",
    createInvalidName: "仅可使用字母、数字、下划线、美元符号或连字符，且必须以字母或下划线开头。",
    createDuplicateName: "已存在同名立方体。",
    createBaseRequired: "请选择基础模型或视图。",
    search: "搜索立方体",
    noMatches: "没有匹配的立方体",
    noMatchesBody: "换一个名称，或清除搜索条件。",
    details: "基础信息",
    measures: "度量",
    dimensions: "维度",
    timeDimensions: "时间维度",
    hierarchies: "层级",
    source: "源文件",
    diff: "差异",
    technicalName: "技术名称",
    sourceFile: "源文件",
    baseObject: "基础对象",
    baseObjectHint: "选择该立方体聚合所基于的模型或视图。",
    refreshTime: "刷新时间",
    refreshTimeHint: "可选的缓存刷新间隔，例如 15 minutes。",
    description: "描述",
    descriptionHint: "给维护该立方体的团队成员提供可选上下文。",
    noBaseObjects: "当前没有可用的模型或视图",
    chooseBaseObject: "选择模型或视图",
    fieldCount: (count: number) => `${count} 个字段`,
    fieldName: "名称",
    expression: "表达式",
    type: "类型",
    expressionHint: "填写 Wren 表达式，例如 SUM(amount) 或 order_date。",
    addMeasure: "添加度量",
    addDimension: "添加维度",
    addTimeDimension: "添加时间维度",
    addHierarchy: "添加层级",
    remove: "移除",
    hierarchyName: "层级名称",
    hierarchyLevels: "层级字段",
    hierarchyLevelsHint: "用逗号分隔维度或时间维度名称，并按下钻顺序排列。",
    noEntries: (label: string) => `还没有定义${label}。`,
    noHierarchies: "还没有定义层级。",
    validate: "校验结构",
    valid: "结构有效",
    validationFailed: "请修复标记的字段后再保存。",
    validationWarnings: (count: number) => `${count} 个警告`,
    saveDraft: "保存草稿",
    saving: "保存中",
    sourceHint: "只读查看源文件。需要编辑时，请通过源文件工作区操作。",
    diffHint: "与上次发布文件相比的变化。",
    sourceLoading: "正在加载源文件",
    diffLoading: "正在加载差异",
    sourceUnavailable: "源文件不可用",
    sourceUnavailableBody: "API 没有返回该立方体文件的内容。",
    diffUnavailable: "没有源文件变化",
    diffUnavailableBody: "该立方体没有未发布的变化。",
    selectCube: "选择立方体",
    selectCubeBody: "从左侧列表选择一个立方体进行查看和编辑。",
    required: "必填",
    retry: "重试",
    duplicate: "名称重复",
    unknownBase: (name: string) => `基础对象“${name}”不是当前项目中的模型或视图。`,
    noMeasuresWarning: "没有度量的立方体仍可保存为草稿，但 Wren 校验时会给出警告。",
  },
} as const;

type Copy = (typeof copy)[CubeLocale];

function cloneCube(cube: CubeDefinition): CubeDefinition {
  return {
    ...cube,
    measures: cube.measures.map((entry) => ({ ...entry })),
    dimensions: cube.dimensions.map((entry) => ({ ...entry })),
    timeDimensions: cube.timeDimensions.map((entry) => ({ ...entry })),
    hierarchies: Object.fromEntries(Object.entries(cube.hierarchies).map(([name, levels]) => [name, [...levels]])),
    properties: cube.properties ? { ...cube.properties } : undefined,
  };
}

function fieldValues(value: unknown): CubeField[] {
  return Array.isArray(value) ? value.filter((entry): entry is CubeField => Boolean(entry && typeof entry === "object")).map((entry) => ({
    ...entry,
    name: typeof entry.name === "string" ? entry.name : "",
    expression: typeof entry.expression === "string" ? entry.expression : "",
    type: typeof entry.type === "string" ? entry.type : "",
  })) : [];
}

/** Perform client-side structural checks before an API save. */
export function validateCubeDraft(cube: CubeDefinition, availableBaseObjects?: string[]): CubeValidationResult {
  const errors: CubeValidationIssue[] = [];
  const warnings: CubeValidationIssue[] = [];
  if (!cube.name.trim() || !NAME_PATTERN.test(cube.name.trim())) {
    errors.push({ path: "name", message: "Cube name must be a valid identifier.", severity: "error" });
  }
  if (!cube.baseObject.trim()) {
    errors.push({ path: "baseObject", message: "Base object is required.", severity: "error" });
  } else if (availableBaseObjects && availableBaseObjects.length > 0 && !availableBaseObjects.includes(cube.baseObject.trim())) {
    errors.push({ path: "baseObject", message: `Base object '${cube.baseObject.trim()}' is not defined.`, severity: "error" });
  }
  const checkEntries = (entries: CubeField[], field: string) => {
    const seen = new Set<string>();
    entries.forEach((entry, index) => {
      const path = `${field}.${index}`;
      const name = entry.name.trim();
      if (!name) errors.push({ path: `${path}.name`, message: "Name is required.", severity: "error" });
      else if (seen.has(name)) errors.push({ path: `${path}.name`, message: "Name is duplicated.", severity: "error" });
      else seen.add(name);
      if (!entry.expression.trim()) errors.push({ path: `${path}.expression`, message: "Expression is required.", severity: "error" });
      if (!entry.type.trim()) errors.push({ path: `${path}.type`, message: "Type is required.", severity: "error" });
    });
    return seen;
  };
  const measureNames = checkEntries(cube.measures, "measures");
  const dimensionNames = checkEntries(cube.dimensions, "dimensions");
  const timeNames = checkEntries(cube.timeDimensions, "timeDimensions");
  for (const name of dimensionNames) {
    if (timeNames.has(name)) errors.push({ path: "timeDimensions", message: `Name '${name}' is duplicated across dimensions.`, severity: "error" });
  }
  if (measureNames.size === 0) warnings.push({ path: "measures", message: "Cube has no measures.", severity: "warning" });
  const levelNames = new Set([...dimensionNames, ...timeNames]);
  Object.entries(cube.hierarchies).forEach(([name, levels]) => {
    if (!name.trim()) errors.push({ path: "hierarchies", message: "Hierarchy name is required.", severity: "error" });
    levels.forEach((level, index) => {
      if (!level.trim()) errors.push({ path: `hierarchies.${name}.${index}`, message: "Hierarchy level is required.", severity: "error" });
      else if (!levelNames.has(level.trim())) errors.push({ path: `hierarchies.${name}.${index}`, message: `Unknown dimension '${level.trim()}'.`, severity: "error" });
    });
  });
  return { valid: errors.length === 0, errors, warnings };
}

export interface CubeWorkbenchProps {
  snapshot: CubeProjectSnapshot | null;
  loading?: boolean;
  error?: string | null;
  sourceContent?: string;
  sourceLoading?: boolean;
  sourceError?: string | null;
  diff?: CubeProjectDiff | null;
  diffLoading?: boolean;
  locale?: CubeLocale;
  onSave: (cube: CubeDefinition) => Promise<void> | void;
  onOpenSource: (path: string) => void;
  onLoadDiff: (path: string) => void;
  onCreate?: (input: { name: string; baseObject: string }) => Promise<void> | void;
  onRetry?: () => void;
}

export default function CubeWorkbench({
  snapshot,
  loading = false,
  error = null,
  sourceContent,
  sourceLoading = false,
  sourceError = null,
  diff,
  diffLoading = false,
  locale,
  onSave,
  onOpenSource,
  onLoadDiff,
  onCreate,
  onRetry,
}: CubeWorkbenchProps) {
  const { i18n } = useTranslation();
  const activeLocale: CubeLocale = locale ?? (i18n.language === "zh-CN" ? "zh-CN" : "en-US");
  const t: Copy = copy[activeLocale];
  const [activeName, setActiveName] = useState("");
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<CubeTab>("details");
  const [drafts, setDrafts] = useState<Record<string, CubeDefinition>>({});
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [creatingBusy, setCreatingBusy] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createBaseObject, setCreateBaseObject] = useState("");
  const [createError, setCreateError] = useState("");
  const [validation, setValidation] = useState<CubeValidationResult | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "error" | "warning"; title: string; body?: string } | null>(null);

  useEffect(() => {
    setActiveName((current) => current && snapshot?.cubes.some((cube) => cube.name === current) ? current : snapshot?.cubes[0]?.name ?? "");
  }, [snapshot]);

  const filteredCubes = useMemo(() => {
    const cubes = snapshot?.cubes ?? [];
    const needle = query.trim().toLowerCase();
    return needle ? cubes.filter((cube) => cube.name.toLowerCase().includes(needle) || cube.baseObject.toLowerCase().includes(needle)) : cubes;
  }, [query, snapshot]);

  const sourceCube = snapshot?.cubes.find((cube) => cube.name === activeName);
  const activeCube = activeName ? drafts[activeName] ?? sourceCube : undefined;
  const baseObjects = snapshot?.availableBaseObjects ?? [];

  function beginCreate() {
    const existing = new Set((snapshot?.cubes ?? []).map((cube) => cube.name));
    let suffix = 1;
    let name = "new_cube";
    while (existing.has(name)) name = `new_cube_${++suffix}`;
    setCreateName(name);
    setCreateBaseObject(baseObjects[0] ?? "");
    setCreateError("");
    setCreating(true);
  }

  async function confirmCreate() {
    if (!onCreate || creatingBusy) return;
    const name = createName.trim();
    const baseObject = createBaseObject.trim();
    if (!NAME_PATTERN.test(name)) { setCreateError(t.createInvalidName); return; }
    if (snapshot?.cubes.some((cube) => cube.name === name)) { setCreateError(t.createDuplicateName); return; }
    if (!baseObject) { setCreateError(t.createBaseRequired); return; }
    setCreatingBusy(true);
    setCreateError("");
    try {
      await onCreate({ name, baseObject });
      setActiveName(name);
      setTab("details");
      setCreating(false);
    } catch (caught) {
      setCreateError(caught instanceof Error ? caught.message : t.validationFailed);
    } finally {
      setCreatingBusy(false);
    }
  }

  const createModal = <Modal open={creating} title={t.createTitle} description={t.createDescription} onClose={() => { if (!creatingBusy) setCreating(false); }} footer={<><Button variant="ghost" onClick={() => setCreating(false)} disabled={creatingBusy}>{t.cancel}</Button><Button variant="primary" icon={Plus} loading={creatingBusy} onClick={() => void confirmCreate()} disabled={!createName.trim() || !createBaseObject}>{creatingBusy ? t.creating : t.createCube}</Button></>}><div className="cube-create-form">{createError ? <InlineNotice tone="error" title={createError} /> : null}<Field label={t.technicalName} hint={t.createNameHint} htmlFor="new-cube-name"><TextInput id="new-cube-name" value={createName} onChange={(event) => { setCreateName(event.target.value); setCreateError(""); }} autoFocus /></Field><Field label={t.baseObject} hint={t.baseObjectHint} htmlFor="new-cube-base"><Select id="new-cube-base" value={createBaseObject} onChange={(event) => { setCreateBaseObject(event.target.value); setCreateError(""); }}><option value="">{t.chooseBaseObject}</option>{baseObjects.map((name) => <option value={name} key={name}>{name}</option>)}</Select></Field><div className="cube-create-path"><span>{t.createPath}</span><code>{createName.trim() ? `cubes/${createName.trim()}/metadata.yml` : "cubes/…/metadata.yml"}</code></div></div></Modal>;

  function patchActive(patch: Partial<CubeDefinition>) {
    if (!activeCube) return;
    const next = cloneCube({ ...activeCube, ...patch });
    setDrafts((current) => ({ ...current, [next.name]: next }));
    setValidation(null);
    setNotice(null);
  }

  function patchField(collection: "measures" | "dimensions" | "timeDimensions", index: number, patch: Partial<CubeField>) {
    if (!activeCube) return;
    const entries = activeCube[collection].map((entry, entryIndex) => entryIndex === index ? { ...entry, ...patch } : entry);
    patchActive({ [collection]: entries });
  }

  function addField(collection: "measures" | "dimensions" | "timeDimensions") {
    if (!activeCube) return;
    const entries = [...activeCube[collection], { name: "", expression: "", type: "DOUBLE" }];
    patchActive({ [collection]: entries });
  }

  function removeField(collection: "measures" | "dimensions" | "timeDimensions", index: number) {
    if (!activeCube) return;
    patchActive({ [collection]: activeCube[collection].filter((_, entryIndex) => entryIndex !== index) });
  }

  function runValidation() {
    if (!activeCube) return null;
    const result = validateCubeDraft(activeCube, snapshot?.availableBaseObjects);
    setValidation(result);
    setNotice(result.valid
      ? { tone: result.warnings.length ? "warning" : "success", title: t.valid, body: result.warnings.length ? t.noMeasuresWarning : undefined }
      : { tone: "error", title: t.validationFailed });
    return result;
  }

  async function save() {
    if (!activeCube || saving) return;
    const result = runValidation();
    if (!result?.valid) return;
    setSaving(true);
    try {
      await onSave(cloneCube(activeCube));
      setDrafts((current) => {
        const next = { ...current };
        delete next[activeCube.name];
        return next;
      });
      setNotice({ tone: "success", title: t.saveDraft });
      onOpenSource(activeCube.sourcePath);
    } catch (caught) {
      setNotice({ tone: "error", title: t.validationFailed, body: caught instanceof Error ? caught.message : undefined });
    } finally {
      setSaving(false);
    }
  }

  function selectTab(next: CubeTab) {
    setTab(next);
    if (!activeCube) return;
    if (next === "source") onOpenSource(activeCube.sourcePath);
    if (next === "diff") onLoadDiff(activeCube.sourcePath);
  }

  if (loading) {
    return <div className="page cube-workbench-page"><div className="cube-heading-skeleton"><LoadingRows count={2} /></div><section className="panel cube-loading-panel"><LoadingRows count={6} /></section></div>;
  }

  if (error) {
    return <div className="page cube-workbench-page"><div className="cube-page-heading"><div><p className="eyebrow">{t.eyebrow}</p><h1>{t.title}</h1><p>{t.pageDescription}</p></div></div><section className="panel cube-state-panel"><InlineNotice tone="error" title={error}>{onRetry ? <Button variant="secondary" size="sm" icon={ArrowClockwise} onClick={onRetry}>{t.retry}</Button> : null}</InlineNotice></section></div>;
  }

  if (!snapshot || snapshot.cubes.length === 0) {
    return <div className="page cube-workbench-page"><div className="cube-page-heading"><div><p className="eyebrow">{t.eyebrow}</p><h1>{t.title}</h1><p>{t.pageDescription}</p></div></div><section className="panel cube-state-panel"><EmptyState icon={Cube} title={t.emptyTitle} body={t.emptyBody} action={onCreate ? <Button variant="primary" icon={Plus} onClick={beginCreate}>{t.createCube}</Button> : undefined} /></section>{createModal}</div>;
  }

  return <div className="page cube-workbench-page">
    <div className="cube-page-heading">
      <div><p className="eyebrow">{t.eyebrow}</p><h1>{t.title}</h1><p>{t.pageDescription}</p></div>
      <div className="cube-heading-meta"><Badge tone="neutral">{t.cubeCount(snapshot.cubes.length)}</Badge><Badge tone={snapshot.draftCount ? "amber" : "green"} dot>{snapshot.draftCount ? t.draft : t.tracked}</Badge></div>
    </div>
    <div className="cube-workbench">
      <aside className="panel cube-list-panel">
        <div className="cube-list-toolbar"><div><span className="panel-kicker">{t.title}</span><strong>{t.cubeCount(filteredCubes.length)}</strong></div>{onCreate ? <Button variant="ghost" size="sm" icon={Plus} onClick={beginCreate}>{t.createCube}</Button> : null}</div>
        <label className="cube-search"><MagnifyingGlass size={15} aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t.search} aria-label={t.search} /></label>
        <div className="cube-list" role="listbox" aria-label={t.title}>
          {filteredCubes.map((cube) => <button type="button" role="option" aria-selected={cube.name === activeName} className={`cube-list-item ${cube.name === activeName ? "cube-list-item-active" : ""}`} key={cube.name} onClick={() => { setActiveName(cube.name); setTab("details"); setValidation(null); setNotice(null); }}><span className="cube-list-icon"><Cube size={17} weight="duotone" /></span><span className="cube-list-copy"><strong>{cube.name}</strong><small>{cube.baseObject}</small><em>{t.fieldCount(cube.measures.length + cube.dimensions.length + cube.timeDimensions.length)}</em></span><span className={`cube-status-dot ${cube.draft ? "cube-status-draft" : ""}`} title={cube.draft ? t.draft : t.tracked} /></button>)}
          {filteredCubes.length === 0 ? <EmptyState icon={MagnifyingGlass} title={t.noMatches} body={t.noMatchesBody} /> : null}
        </div>
      </aside>
      <section className="cube-editor-main">
        {!activeCube ? <section className="panel cube-state-panel"><EmptyState icon={Cube} title={t.selectCube} body={t.selectCubeBody} /></section> : <section className="panel cube-workspace-panel">
          <div className="cube-panel-header"><div><p className="panel-kicker">{activeCube.baseObject}</p><h2>{activeCube.name}</h2><p>{t.pageDescription}</p></div><div className="cube-editor-actions"><Badge tone={activeCube.draft || Boolean(drafts[activeCube.name]) ? "amber" : "neutral"}>{activeCube.draft || drafts[activeCube.name] ? t.draft : t.tracked}</Badge><Button variant="secondary" size="sm" icon={CheckCircle} onClick={runValidation}>{t.validate}</Button><Button variant="primary" size="sm" icon={FloppyDisk} loading={saving} onClick={() => void save()}>{t.saveDraft}</Button></div></div>
          {notice ? <InlineNotice tone={notice.tone} title={notice.title} onDismiss={() => setNotice(null)}>{notice.body}</InlineNotice> : null}
          {validation && !validation.valid ? <ValidationSummary result={validation} /> : null}
          <div className="cube-workspace-tabs" role="tablist" aria-label={t.title}>
            <WorkspaceTab id="details" active={tab} label={t.details} onClick={selectTab} />
            <WorkspaceTab id="measures" active={tab} label={t.measures} count={activeCube.measures.length} onClick={selectTab} />
            <WorkspaceTab id="dimensions" active={tab} label={t.dimensions} count={activeCube.dimensions.length} onClick={selectTab} />
            <WorkspaceTab id="timeDimensions" active={tab} label={t.timeDimensions} count={activeCube.timeDimensions.length} onClick={selectTab} />
            <WorkspaceTab id="hierarchies" active={tab} label={t.hierarchies} count={Object.keys(activeCube.hierarchies).length} onClick={selectTab} />
            <WorkspaceTab id="source" active={tab} label={t.source} icon={<Code size={14} />} onClick={selectTab} />
            <WorkspaceTab id="diff" active={tab} label={t.diff} icon={<BracketsCurly size={14} />} onClick={selectTab} />
          </div>
          <div className="cube-workspace-content" role="tabpanel" aria-label={tabLabel(tab, t)}>
            {tab === "details" ? <DetailsTab cube={activeCube} baseObjects={baseObjects} t={t} onChange={patchActive} /> : null}
            {tab === "measures" ? <EntriesTab collection="measures" entries={fieldValues(activeCube.measures)} title={t.measures} emptyLabel={t.measures} addLabel={t.addMeasure} t={t} onAdd={() => addField("measures")} onRemove={(index) => removeField("measures", index)} onChange={(index, patch) => patchField("measures", index, patch)} /> : null}
            {tab === "dimensions" ? <EntriesTab collection="dimensions" entries={fieldValues(activeCube.dimensions)} title={t.dimensions} emptyLabel={t.dimensions} addLabel={t.addDimension} t={t} onAdd={() => addField("dimensions")} onRemove={(index) => removeField("dimensions", index)} onChange={(index, patch) => patchField("dimensions", index, patch)} /> : null}
            {tab === "timeDimensions" ? <EntriesTab collection="timeDimensions" entries={fieldValues(activeCube.timeDimensions)} title={t.timeDimensions} emptyLabel={t.timeDimensions} addLabel={t.addTimeDimension} t={t} onAdd={() => addField("timeDimensions")} onRemove={(index) => removeField("timeDimensions", index)} onChange={(index, patch) => patchField("timeDimensions", index, patch)} /> : null}
            {tab === "hierarchies" ? <HierarchiesTab hierarchies={activeCube.hierarchies} dimensionNames={[...activeCube.dimensions, ...activeCube.timeDimensions].map((entry) => entry.name).filter(Boolean)} t={t} onChange={(hierarchies) => patchActive({ hierarchies })} /> : null}
            {tab === "source" ? <SourceTab content={sourceContent} loading={sourceLoading} error={sourceError} cube={activeCube} t={t} /> : null}
            {tab === "diff" ? <DiffTab diff={diff} loading={diffLoading} cube={activeCube} t={t} /> : null}
          </div>
        </section>}
      </section>
    </div>
    {createModal}
  </div>;
}

function tabLabel(tab: CubeTab, t: Copy) {
  return ({ details: t.details, measures: t.measures, dimensions: t.dimensions, timeDimensions: t.timeDimensions, hierarchies: t.hierarchies, source: t.source, diff: t.diff })[tab];
}

function WorkspaceTab({ id, active, label, count, icon, onClick }: { id: CubeTab; active: CubeTab; label: string; count?: number; icon?: ReactNode; onClick: (tab: CubeTab) => void }) {
  return <button type="button" id={`cube-tab-${id}`} className={`cube-workspace-tab ${active === id ? "cube-workspace-tab-active" : ""}`} onClick={() => onClick(id)} role="tab" aria-selected={active === id} aria-controls="cube-workspace-panel">{icon}{label}{count !== undefined ? <span>{count}</span> : null}</button>;
}

function DetailsTab({ cube, baseObjects, t, onChange }: { cube: CubeDefinition; baseObjects: string[]; t: Copy; onChange: (patch: Partial<CubeDefinition>) => void }) {
  const description = typeof cube.properties?.description === "string" ? cube.properties.description : "";
  const baseOptionExists = baseObjects.includes(cube.baseObject);
  return <div className="cube-details-grid">
    <Field label={t.technicalName} htmlFor="cube-technical-name"><TextInput id="cube-technical-name" value={cube.name} readOnly /></Field>
    <Field label={t.sourceFile} htmlFor="cube-source-file"><TextInput id="cube-source-file" value={cube.sourcePath} readOnly /></Field>
    <Field label={t.baseObject} hint={t.baseObjectHint} htmlFor="cube-base-object"><Select id="cube-base-object" value={cube.baseObject} onChange={(event) => onChange({ baseObject: event.target.value })}><option value="">{t.chooseBaseObject}</option>{!baseOptionExists && cube.baseObject ? <option value={cube.baseObject}>{cube.baseObject}</option> : null}{baseObjects.map((name) => <option value={name} key={name}>{name}</option>)}</Select>{baseObjects.length === 0 ? <span className="cube-inline-warning"><WarningCircle size={14} />{t.noBaseObjects}</span> : null}</Field>
    <Field label={t.refreshTime} hint={t.refreshTimeHint} htmlFor="cube-refresh-time"><TextInput id="cube-refresh-time" value={cube.refreshTime ?? ""} onChange={(event) => onChange({ refreshTime: event.target.value })} placeholder="15 minutes" /></Field>
    <Field label={t.description} hint={t.descriptionHint} htmlFor="cube-description"><TextArea id="cube-description" rows={4} value={description} onChange={(event) => onChange({ properties: { ...(cube.properties ?? {}), description: event.target.value } })} /></Field>
  </div>;
}

function EntriesTab({ collection, entries, title, emptyLabel, addLabel, t, onAdd, onRemove, onChange }: { collection: string; entries: CubeField[]; title: string; emptyLabel: string; addLabel: string; t: Copy; onAdd: () => void; onRemove: (index: number) => void; onChange: (index: number, patch: Partial<CubeField>) => void }) {
  return <div className="cube-entries-tab"><div className="cube-tab-intro"><div><p className="panel-kicker">{title}</p><h3>{t.fieldCount(entries.length)}</h3></div><Button variant="secondary" size="sm" icon={Plus} onClick={onAdd}>{addLabel}</Button></div>{entries.length === 0 ? <div className="cube-empty-collection"><EmptyState icon={collection === "timeDimensions" ? CheckCircle : Cube} title={t.noEntries(emptyLabel)} body={t.expressionHint} action={<Button variant="ghost" size="sm" icon={Plus} onClick={onAdd}>{addLabel}</Button>} /></div> : <div className="cube-entry-list">{entries.map((entry, index) => <EntryRow key={`${collection}-${index}`} entry={entry} index={index} t={t} onRemove={() => onRemove(index)} onChange={(patch) => onChange(index, patch)} />)}</div>}</div>;
}

function EntryRow({ entry, index, t, onRemove, onChange }: { entry: CubeField; index: number; t: Copy; onRemove: () => void; onChange: (patch: Partial<CubeField>) => void }) {
  const rowId = `${entry.name || "field"}-${index}`;
  return <article className="cube-entry-row"><div className="cube-entry-header"><span className="cube-entry-index">{String(index + 1).padStart(2, "0")}</span><strong>{entry.name || t.fieldName}</strong><button type="button" className="cube-remove-button" onClick={onRemove} aria-label={`${t.remove} ${entry.name || t.fieldName}`}><Trash size={15} /></button></div><div className="cube-entry-fields"><Field label={t.fieldName} htmlFor={`${rowId}-name`}><TextInput id={`${rowId}-name`} value={entry.name} onChange={(event) => onChange({ name: event.target.value })} placeholder="total_revenue" /></Field><Field label={t.type} htmlFor={`${rowId}-type`}><TextInput id={`${rowId}-type`} value={entry.type} onChange={(event) => onChange({ type: event.target.value })} placeholder="DOUBLE" /></Field><Field label={t.expression} hint={t.expressionHint} htmlFor={`${rowId}-expression`}><TextArea id={`${rowId}-expression`} className="cube-code-input" rows={2} value={entry.expression} onChange={(event) => onChange({ expression: event.target.value })} placeholder="SUM(amount)" spellCheck={false} /></Field></div></article>;
}

function HierarchiesTab({ hierarchies, dimensionNames, t, onChange }: { hierarchies: Record<string, string[]>; dimensionNames: string[]; t: Copy; onChange: (value: Record<string, string[]>) => void }) {
  const entries = Object.entries(hierarchies);
  function add() {
    let name = "hierarchy";
    let suffix = 1;
    while (Object.prototype.hasOwnProperty.call(hierarchies, name)) name = `hierarchy_${suffix++}`;
    onChange({ ...hierarchies, [name]: [] });
  }
  function patch(name: string, nextName: string, levels: string[]) {
    if (nextName !== name && nextName && Object.prototype.hasOwnProperty.call(hierarchies, nextName)) return;
    const next: Record<string, string[]> = {};
    Object.entries(hierarchies).forEach(([key, value]) => { next[key === name ? nextName : key] = key === name ? levels : value; });
    onChange(next);
  }
  function remove(name: string) {
    const next = { ...hierarchies };
    delete next[name];
    onChange(next);
  }
  return <div className="cube-hierarchies-tab"><div className="cube-tab-intro"><div><p className="panel-kicker">{t.hierarchies}</p><h3>{entries.length} {entries.length === 1 ? "hierarchy" : "hierarchies"}</h3></div><Button variant="secondary" size="sm" icon={Plus} onClick={add}>{t.addHierarchy}</Button></div>{entries.length === 0 ? <div className="cube-empty-collection"><EmptyState icon={BracketsCurly} title={t.noHierarchies} body={t.hierarchyLevelsHint} action={<Button variant="ghost" size="sm" icon={Plus} onClick={add}>{t.addHierarchy}</Button>} /></div> : <div className="cube-hierarchy-list">{entries.map(([name, levels], index) => <article className="cube-hierarchy-row" key={`${name}-${index}`}><div className="cube-entry-header"><span className="cube-entry-index">{String(index + 1).padStart(2, "0")}</span><strong>{name || t.hierarchyName}</strong><button type="button" className="cube-remove-button" onClick={() => remove(name)} aria-label={`${t.remove} ${name || t.hierarchyName}`}><Trash size={15} /></button></div><div className="cube-hierarchy-fields"><Field label={t.hierarchyName} htmlFor={`cube-hierarchy-name-${index}`}><TextInput id={`cube-hierarchy-name-${index}`} value={name} onChange={(event) => patch(name, event.target.value, levels)} placeholder="time" /></Field><Field label={t.hierarchyLevels} hint={t.hierarchyLevelsHint} htmlFor={`cube-hierarchy-levels-${index}`}><TextInput id={`cube-hierarchy-levels-${index}`} value={levels.join(", ")} list={`cube-dimension-options-${index}`} onChange={(event) => patch(name, name, event.target.value.split(",").map((level) => level.trim()).filter(Boolean))} placeholder={dimensionNames.slice(0, 2).join(", ") || "order_date"} /><datalist id={`cube-dimension-options-${index}`}>{dimensionNames.map((dimension) => <option value={dimension} key={dimension} />)}</datalist></Field></div></article>)}</div>}</div>;
}

function ValidationSummary({ result }: { result: CubeValidationResult }) {
  return <div className="cube-validation-summary" role="group" aria-label="Validation issues" aria-live="polite"><WarningCircle size={17} weight="fill" aria-hidden="true" /><div>{result.errors.map((issue) => <span key={`${issue.path}-${issue.message}`}><strong>{issue.path}</strong> {issue.message}</span>)}</div></div>;
}

function SourceTab({ content, loading, error, cube, t }: { content?: string; loading: boolean; error?: string | null; cube: CubeDefinition; t: Copy }) {
  return <div className="cube-source-tab"><div className="cube-source-header"><div><p className="panel-kicker">{t.sourceFile}</p><strong>{cube.sourcePath}</strong></div><Code size={18} aria-hidden="true" /></div><div className="cube-source-notice"><Code size={15} aria-hidden="true" /><span>{t.sourceHint}</span></div>{loading ? <LoadingRows count={8} /> : error ? <InlineNotice tone="error" title={t.sourceUnavailable}>{error}</InlineNotice> : content ? <pre className="cube-code-block"><code>{content}</code></pre> : <EmptyState icon={Code} title={t.sourceUnavailable} body={t.sourceUnavailableBody} />}</div>;
}

function DiffTab({ diff, loading, cube, t }: { diff?: CubeProjectDiff | null; loading: boolean; cube: CubeDefinition; t: Copy }) {
  return <div className="cube-source-tab"><div className="cube-source-header"><div><p className="panel-kicker">{t.diff}</p><strong>{cube.sourcePath}</strong></div><BracketsCurly size={18} aria-hidden="true" /></div><div className="cube-source-notice"><BracketsCurly size={15} aria-hidden="true" /><span>{t.diffHint}</span></div>{loading ? <LoadingRows count={8} /> : diff?.changed ? <pre className="cube-code-block cube-diff-block"><code>{diff.diff}</code></pre> : <EmptyState icon={Check} title={t.diffUnavailable} body={t.diffUnavailableBody} />}</div>;
}
