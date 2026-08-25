import { useCallback, useEffect, useMemo, useState, type CSSProperties, type FormEvent, type KeyboardEvent } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  ArrowsOut,
  CircleNotch,
  Crosshair,
  MagnifyingGlass,
  MapTrifold,
  PencilSimple,
  Plus,
  Trash,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import {
  Background,
  BaseEdge,
  ConnectionLineType,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  getBezierPath,
  useReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./relationship-graph.css";

/** A localized value accepted by model and relationship metadata. */
export type RelationshipGraphLocalizedText = Partial<Record<"zh-CN" | "en-US" | "zh" | "en", string>>;

/** A model reference used by a relationship. Object references are accepted for API adapters. */
export type RelationshipGraphModelReference = string | { name: string };

/** A column shown inside a model node. */
export interface RelationshipGraphColumn {
  name: string;
  displayName?: string | RelationshipGraphLocalizedText;
  type?: string;
  dataType?: string;
  primaryKey?: boolean;
  isPrimaryKey?: boolean;
}

/** A semantic model represented as a node in the relationship map. */
export interface RelationshipGraphModel {
  name: string;
  displayName?: string | RelationshipGraphLocalizedText;
  table?: string;
  tableName?: string;
  physicalName?: string;
  schema?: string;
  primaryKey?: string | string[];
  columns: RelationshipGraphColumn[];
}

/** A semantic relationship represented as an edge in the relationship map. */
export interface RelationshipGraphRelationship {
  name: string;
  models: readonly [RelationshipGraphModelReference, RelationshipGraphModelReference] | readonly RelationshipGraphModelReference[];
  joinType?: string;
  cardinality?: string;
  condition?: string;
  displayName?: string | RelationshipGraphLocalizedText;
  description?: string | RelationshipGraphLocalizedText;
  /** Optional explicit join columns keyed by model name. */
  joinColumns?: Record<string, string | string[]>;
}

/** The locale used for labels and localized metadata fallbacks. */
export type RelationshipGraphLocale = string;

/** Props for the self-contained visual relationship editor. */
export interface RelationshipGraphProps {
  models: readonly RelationshipGraphModel[];
  relationships: readonly RelationshipGraphRelationship[];
  locale?: RelationshipGraphLocale;
  theme?: "light" | "dark" | "system";
  loading?: boolean;
  error?: string | null;
  isSaving?: boolean;
  readOnly?: boolean;
  className?: string;
  style?: CSSProperties;
  /** Called with the full draft relationship list after a create, edit, or delete. */
  onChange?: (relationships: RelationshipGraphRelationship[]) => void;
  /** Called after a relationship is saved through the drawer. */
  onSave?: (relationship: RelationshipGraphRelationship, relationships: RelationshipGraphRelationship[]) => void | Promise<void>;
  /** Called after a relationship is deleted through the drawer. */
  onDelete?: (relationship: RelationshipGraphRelationship, relationships: RelationshipGraphRelationship[]) => void | Promise<void>;
  /** Called when the drawer opens or closes for a relationship. */
  onEdit?: (relationship: RelationshipGraphRelationship | null) => void;
}

type RelationshipGraphCopy = {
  eyebrow: string;
  title: string;
  description: string;
  search: string;
  searchHint: string;
  add: string;
  layout: string;
  reset: string;
  upstream: string;
  downstream: string;
  all: string;
  focus: string;
  edit: string;
  addRelationship: string;
  editRelationship: string;
  sourceModel: string;
  targetModel: string;
  relationshipName: string;
  cardinality: string;
  condition: string;
  displayNameZh: string;
  displayNameEn: string;
  descriptionZh: string;
  descriptionEn: string;
  save: string;
  cancel: string;
  delete: string;
  close: string;
  noModels: string;
  noModelsBody: string;
  noRelationships: string;
  noRelationshipsBody: string;
  loading: string;
  saving: string;
  saveFailed: string;
  validation: string;
  sameModel: string;
  chooseModels: string;
  modelCount: (count: number) => string;
  relationCount: (count: number) => string;
  primaryKey: string;
  relatedFields: string;
  table: string;
  noRelatedFields: string;
};

const COPY: Record<"zh" | "en", RelationshipGraphCopy> = {
  zh: {
    eyebrow: "语义层",
    title: "关系图",
    description: "查看模型之间的业务关联，并在同一处维护关系定义。",
    search: "搜索模型",
    searchHint: "按业务名、模型名或底层表搜索",
    add: "新增关系",
    layout: "自动布局",
    reset: "重置视图",
    upstream: "上游",
    downstream: "下游",
    all: "全部关系",
    focus: "聚焦",
    edit: "编辑关系",
    addRelationship: "新增关系",
    editRelationship: "编辑关系",
    sourceModel: "起点模型",
    targetModel: "终点模型",
    relationshipName: "关系名称",
    cardinality: "基数",
    condition: "关联条件",
    displayNameZh: "中文显示名",
    displayNameEn: "英文显示名",
    descriptionZh: "中文描述",
    descriptionEn: "英文描述",
    save: "保存关系",
    cancel: "取消",
    delete: "删除关系",
    close: "关闭编辑面板",
    noModels: "暂无模型",
    noModelsBody: "导入模型后，模型之间的业务关联会显示在这里。",
    noRelationships: "暂无关系",
    noRelationshipsBody: "点击“新增关系”创建第一条模型关联。",
    loading: "正在加载关系图...",
    saving: "正在保存...",
    saveFailed: "关系保存失败，请检查后重试。",
    validation: "请填写关系名称和两个不同的模型。",
    sameModel: "关系两端必须是不同的模型。",
    chooseModels: "请先添加至少两个模型。",
    modelCount: (count) => `${count} 个模型`,
    relationCount: (count) => `${count} 条关系`,
    primaryKey: "主键",
    relatedFields: "参与关联字段",
    table: "底层表",
    noRelatedFields: "未识别到关联字段",
  },
  en: {
    eyebrow: "Semantic layer",
    title: "Relationship map",
    description: "Explore business links between models and maintain their definitions in one place.",
    search: "Search models",
    searchHint: "Search by business name, model name, or source table",
    add: "Add relationship",
    layout: "Auto layout",
    reset: "Reset view",
    upstream: "Upstream",
    downstream: "Downstream",
    all: "All links",
    focus: "Focus",
    edit: "Edit relationship",
    addRelationship: "Add relationship",
    editRelationship: "Edit relationship",
    sourceModel: "Source model",
    targetModel: "Target model",
    relationshipName: "Relationship name",
    cardinality: "Cardinality",
    condition: "Join condition",
    displayNameZh: "Chinese display name",
    displayNameEn: "English display name",
    descriptionZh: "Chinese description",
    descriptionEn: "English description",
    save: "Save relationship",
    cancel: "Cancel",
    delete: "Delete relationship",
    close: "Close relationship editor",
    noModels: "No models yet",
    noModelsBody: "Import models to see their business relationships here.",
    noRelationships: "No relationships yet",
    noRelationshipsBody: "Click “Add relationship” to create the first model link.",
    loading: "Loading relationship map...",
    saving: "Saving...",
    saveFailed: "Could not save this relationship. Check the fields and try again.",
    validation: "Enter a relationship name and choose two different models.",
    sameModel: "The two ends of a relationship must be different models.",
    chooseModels: "Add at least two models first.",
    modelCount: (count) => `${count} model${count === 1 ? "" : "s"}`,
    relationCount: (count) => `${count} relationship${count === 1 ? "" : "s"}`,
    primaryKey: "Primary key",
    relatedFields: "Join fields",
    table: "Source table",
    noRelatedFields: "No join fields detected",
  },
};

function languageFor(locale: RelationshipGraphLocale | undefined): "zh" | "en" {
  return String(locale ?? "en-US").toLowerCase().startsWith("zh") ? "zh" : "en";
}

function localizedText(value: string | RelationshipGraphLocalizedText | undefined, locale: RelationshipGraphLocale | undefined, fallback = "") {
  if (typeof value === "string") return value;
  if (!value) return fallback;
  const isZh = languageFor(locale) === "zh";
  const preferred = isZh ? ["zh-CN", "zh", "en-US", "en"] : ["en-US", "en", "zh-CN", "zh"];
  return preferred.map((key) => value[key as keyof RelationshipGraphLocalizedText]).find((item) => Boolean(item)) ?? fallback;
}

function localizedPair(value: string | RelationshipGraphLocalizedText | undefined) {
  if (typeof value === "string") return { zh: value, en: value };
  return { zh: value?.["zh-CN"] ?? value?.zh ?? "", en: value?.["en-US"] ?? value?.en ?? "" };
}

function modelReferenceName(reference: RelationshipGraphModelReference | undefined) {
  return typeof reference === "string" ? reference : reference?.name ?? "";
}

function relationshipModels(relationship: RelationshipGraphRelationship): [string, string] | null {
  const names = relationship.models.map(modelReferenceName).filter(Boolean);
  return names.length >= 2 ? [names[0], names[1]] : null;
}

function modelNodeId(name: string) {
  return `relationship-model-${encodeURIComponent(name)}`;
}

function relationshipEdgeId(relationship: RelationshipGraphRelationship, index: number) {
  return `relationship-edge-${index}-${encodeURIComponent(relationship.name || "unnamed")}`;
}

function normalizedJoinType(value: string | undefined) {
  const normalized = String(value ?? "").trim().toLowerCase().replace(/[_\s-]/g, "");
  return ({
    onetoone: "1:1",
    one2one: "1:1",
    "1:1": "1:1",
    onetomany: "1:N",
    one2many: "1:N",
    "1:n": "1:N",
    manytoone: "N:1",
    many2one: "N:1",
    "n:1": "N:1",
    manytomany: "N:N",
    many2many: "N:N",
    "n:n": "N:N",
  } as Record<string, string>)[normalized] ?? value ?? "1:N";
}

function relationshipLabel(relationship: RelationshipGraphRelationship) {
  return normalizedJoinType(relationship.cardinality ?? relationship.joinType);
}

function cardinalityValue(value?: string) {
  const normalized = String(value ?? "").toLowerCase().replace(/_/g, "-");
  if (normalized === "one-to-one" || normalized === "1:1") return "one-to-one";
  if (normalized === "many-to-one" || normalized === "n:1") return "many-to-one";
  if (normalized === "many-to-many" || normalized === "n:n") return "many-to-many";
  return "one-to-many";
}

function wrenJoinType(value: string) {
  return ({
    "one-to-one": "ONE_TO_ONE",
    "one-to-many": "ONE_TO_MANY",
    "many-to-one": "MANY_TO_ONE",
    "many-to-many": "MANY_TO_MANY",
  } as const)[cardinalityValue(value)];
}

function relationColumnNames(modelName: string, model: RelationshipGraphModel, relationships: readonly RelationshipGraphRelationship[]) {
  const names = new Set<string>();
  relationships.forEach((relationship) => {
    const endpoints = relationshipModels(relationship);
    if (!endpoints || !endpoints.includes(modelName)) return;
    const explicit = relationship.joinColumns?.[modelName];
    if (explicit) (Array.isArray(explicit) ? explicit : [explicit]).forEach((name) => names.add(name));
    const condition = relationship.condition ?? "";
    model.columns.forEach((column) => {
      if (condition.toLowerCase().includes(column.name.toLowerCase())) names.add(column.name);
    });
  });
  return model.columns.filter((column) => names.has(column.name));
}

function defaultPositions(models: readonly RelationshipGraphModel[]) {
  const columns = Math.max(1, Math.ceil(Math.sqrt(Math.max(1, models.length))));
  return models.reduce<Record<string, { x: number; y: number }>>((positions, model, index) => {
    positions[modelNodeId(model.name)] = { x: (index % columns) * 350 + 40, y: Math.floor(index / columns) * 250 + 40 };
    return positions;
  }, {});
}

type GraphNodeData = {
  model: RelationshipGraphModel;
  displayName: string;
  tableName: string;
  primaryKeys: string[];
  relatedColumns: RelationshipGraphColumn[];
  locale?: RelationshipGraphLocale;
  isDimmed: boolean;
  isActive: boolean;
  onOpen: () => void;
};

type GraphNode = Node<GraphNodeData, "model">;

type GraphEdgeData = {
  relationship: RelationshipGraphRelationship;
  label: string;
  onOpen: () => void;
  isDimmed: boolean;
};

type GraphEdge = Edge<GraphEdgeData, "relationship">;

function primaryKeyNames(model: RelationshipGraphModel) {
  const explicit = model.primaryKey ? (Array.isArray(model.primaryKey) ? model.primaryKey : [model.primaryKey]) : [];
  const columns = model.columns.filter((column) => column.primaryKey || column.isPrimaryKey).map((column) => column.name);
  return Array.from(new Set([...explicit, ...columns]));
}

function ModelNode({ data }: NodeProps<GraphNode>) {
  const copy = COPY[languageFor(data.locale)];
  const tableName = [data.model.schema, data.tableName].filter(Boolean).join(".");
  return (
    <div className={`relationship-graph-node ${data.isDimmed ? "relationship-graph-node-dimmed" : ""} ${data.isActive ? "relationship-graph-node-active" : ""}`}>
      <Handle type="target" position={Position.Left} aria-label={`Incoming relationship to ${data.displayName}`} />
      <Handle type="source" position={Position.Right} aria-label={`Outgoing relationship from ${data.displayName}`} />
      <div className="relationship-graph-node-header">
        <span className="relationship-graph-node-glyph" aria-hidden="true"><MapTrifold size={16} weight="duotone" /></span>
        <div className="relationship-graph-node-title">
          <strong title={data.displayName}>{data.displayName}</strong>
          <small title={data.model.name}>{data.model.name}</small>
        </div>
        <button type="button" className="relationship-graph-node-edit nodrag nopan" aria-label={`${copy.edit}: ${data.displayName}`} onClick={data.onOpen}>
          <PencilSimple size={15} aria-hidden="true" />
        </button>
      </div>
      <div className="relationship-graph-node-table"><span>{copy.table}</span><code>{tableName || data.model.name}</code></div>
      <div className="relationship-graph-node-fields">
        <div className="relationship-graph-node-field-heading"><span>{copy.primaryKey}</span><span>{copy.relatedFields}</span></div>
        <div className="relationship-graph-node-field-row">
          <span className="relationship-graph-node-pk">{data.primaryKeys.length ? data.primaryKeys.join(", ") : "-"}</span>
          <span className="relationship-graph-node-joins" title={data.relatedColumns.map((column) => column.name).join(", ") || copy.noRelatedFields}>
            {data.relatedColumns.length ? data.relatedColumns.map((column) => column.displayName ? localizedText(column.displayName, data.locale, column.name) : column.name).join(", ") : "-"}
          </span>
        </div>
      </div>
    </div>
  );
}

function RelationshipEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected }: EdgeProps<GraphEdge>) {
  const [edgePath, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  const label = data?.label ?? "1:N";
  return (
    <>
      <BaseEdge id={id} path={edgePath} interactionWidth={28} className={data?.isDimmed ? "relationship-graph-edge-dimmed" : ""} style={{ stroke: selected ? "var(--accent)" : "var(--relationship-edge, var(--border-strong))", strokeWidth: selected ? 2.5 : 1.7 }} />
      <EdgeLabelRenderer>
        <button
          type="button"
          className={`relationship-graph-edge-label nodrag nopan ${selected ? "relationship-graph-edge-label-selected" : ""} ${data?.isDimmed ? "relationship-graph-edge-label-dimmed" : ""}`}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)` }}
          aria-label={`${data?.relationship.name ?? label} (${label})`}
          onClick={(event) => { event.stopPropagation(); data?.onOpen(); }}
          onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); data?.onOpen(); } }}
        >
          {label}
        </button>
      </EdgeLabelRenderer>
    </>
  );
}

const nodeTypes = { model: ModelNode };
const edgeTypes = { relationship: RelationshipEdge };

type RelationshipDraft = {
  existingName?: string;
  name: string;
  sourceModel: string;
  targetModel: string;
  cardinality: string;
  condition: string;
  displayNameZh: string;
  displayNameEn: string;
  descriptionZh: string;
  descriptionEn: string;
};

function draftFromRelationship(relationship: RelationshipGraphRelationship): RelationshipDraft {
  const endpoints = relationshipModels(relationship) ?? ["", ""];
  const display = localizedPair(relationship.displayName);
  const description = localizedPair(relationship.description);
  return {
    existingName: relationship.name,
    name: relationship.name,
    sourceModel: endpoints[0],
    targetModel: endpoints[1],
    cardinality: cardinalityValue(relationship.cardinality ?? relationship.joinType),
    condition: relationship.condition ?? "",
    displayNameZh: display.zh,
    displayNameEn: display.en,
    descriptionZh: description.zh,
    descriptionEn: description.en,
  };
}

function blankDraft(models: readonly RelationshipGraphModel[]): RelationshipDraft {
  return {
    name: "",
    sourceModel: models[0]?.name ?? "",
    targetModel: models[1]?.name ?? "",
    cardinality: "one-to-many",
    condition: "",
    displayNameZh: "",
    displayNameEn: "",
    descriptionZh: "",
    descriptionEn: "",
  };
}

function relationshipFromDraft(draft: RelationshipDraft, original?: RelationshipGraphRelationship): RelationshipGraphRelationship {
  const next: RelationshipGraphRelationship = {
    ...(original ?? {}),
    name: draft.name.trim(),
    models: [draft.sourceModel, draft.targetModel],
    joinType: wrenJoinType(draft.cardinality),
    cardinality: cardinalityValue(draft.cardinality),
    condition: draft.condition.trim(),
    displayName: { "zh-CN": draft.displayNameZh.trim(), "en-US": draft.displayNameEn.trim() },
    description: { "zh-CN": draft.descriptionZh.trim(), "en-US": draft.descriptionEn.trim() },
  };
  return next;
}

function RelationshipDrawer({
  draft,
  models,
  copy,
  isNew,
  isSaving,
  readOnly,
  error,
  onChange,
  onSave,
  onDelete,
  onClose,
}: {
  draft: RelationshipDraft;
  models: readonly RelationshipGraphModel[];
  copy: RelationshipGraphCopy;
  isNew: boolean;
  isSaving: boolean;
  readOnly: boolean;
  error: string | null;
  onChange: (draft: RelationshipDraft) => void;
  onSave: (event: FormEvent<HTMLFormElement>) => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  return (
    <aside className="relationship-graph-drawer" role="dialog" aria-modal="false" aria-label={isNew ? copy.addRelationship : copy.editRelationship}>
      <div className="relationship-graph-drawer-header">
        <div><p className="relationship-graph-panel-kicker">{isNew ? copy.add : copy.edit}</p><h3>{isNew ? copy.addRelationship : copy.editRelationship}</h3></div>
        <button type="button" className="relationship-graph-icon-button" onClick={onClose} aria-label={copy.close}><X size={17} /></button>
      </div>
      <form className="relationship-graph-form" onSubmit={onSave}>
        {error ? <div className="relationship-graph-form-error" role="alert"><WarningCircle size={16} weight="fill" />{error}</div> : null}
        <label className="relationship-graph-field"><span>{copy.relationshipName}</span><input autoFocus className="relationship-graph-input" value={draft.name} onChange={(event) => onChange({ ...draft, name: event.target.value })} required /></label>
        <div className="relationship-graph-form-grid">
          <label className="relationship-graph-field"><span>{copy.sourceModel}</span><select className="relationship-graph-input" value={draft.sourceModel} onChange={(event) => onChange({ ...draft, sourceModel: event.target.value })} disabled={!models.length} required><option value="">-</option>{models.map((model) => <option value={model.name} key={model.name}>{localizedText(model.displayName, undefined, model.name)} ({model.name})</option>)}</select></label>
          <label className="relationship-graph-field"><span>{copy.targetModel}</span><select className="relationship-graph-input" value={draft.targetModel} onChange={(event) => onChange({ ...draft, targetModel: event.target.value })} disabled={!models.length} required><option value="">-</option>{models.map((model) => <option value={model.name} key={model.name}>{localizedText(model.displayName, undefined, model.name)} ({model.name})</option>)}</select></label>
        </div>
        <label className="relationship-graph-field"><span>{copy.cardinality}</span><select className="relationship-graph-input" value={draft.cardinality} onChange={(event) => onChange({ ...draft, cardinality: event.target.value })}><option value="one-to-one">1:1</option><option value="one-to-many">1:N</option><option value="many-to-one">N:1</option><option value="many-to-many">N:N</option></select></label>
        <label className="relationship-graph-field"><span>{copy.condition}</span><textarea className="relationship-graph-input relationship-graph-textarea relationship-graph-condition" value={draft.condition} onChange={(event) => onChange({ ...draft, condition: event.target.value })} placeholder="orders.customer_id = customers.id" /></label>
        <div className="relationship-graph-form-divider" />
        <label className="relationship-graph-field"><span>{copy.displayNameZh}</span><input className="relationship-graph-input" value={draft.displayNameZh} onChange={(event) => onChange({ ...draft, displayNameZh: event.target.value })} /></label>
        <label className="relationship-graph-field"><span>{copy.displayNameEn}</span><input className="relationship-graph-input" value={draft.displayNameEn} onChange={(event) => onChange({ ...draft, displayNameEn: event.target.value })} /></label>
        <label className="relationship-graph-field"><span>{copy.descriptionZh}</span><textarea className="relationship-graph-input relationship-graph-textarea" value={draft.descriptionZh} onChange={(event) => onChange({ ...draft, descriptionZh: event.target.value })} /></label>
        <label className="relationship-graph-field"><span>{copy.descriptionEn}</span><textarea className="relationship-graph-input relationship-graph-textarea" value={draft.descriptionEn} onChange={(event) => onChange({ ...draft, descriptionEn: event.target.value })} /></label>
        <div className="relationship-graph-drawer-actions">
          {!isNew && !readOnly ? <button type="button" className="relationship-graph-button relationship-graph-button-danger" onClick={onDelete}><Trash size={15} />{copy.delete}</button> : null}
          <span className="relationship-graph-action-spacer" />
          <button type="button" className="relationship-graph-button relationship-graph-button-secondary" onClick={onClose}>{copy.cancel}</button>
          {!readOnly ? <button type="submit" className="relationship-graph-button relationship-graph-button-primary" disabled={isSaving}><span>{isSaving ? <CircleNotch className="relationship-graph-spin" size={15} /> : null}</span>{isSaving ? copy.saving : copy.save}</button> : null}
        </div>
      </form>
    </aside>
  );
}

function RelationshipGraphCanvas({ props, copy }: { props: RelationshipGraphProps; copy: RelationshipGraphCopy }) {
  const { models, locale, relationships, readOnly = false } = props;
  const reactFlow = useReactFlow<GraphNode, GraphEdge>();
  const [localRelationships, setLocalRelationships] = useState<RelationshipGraphRelationship[]>(() => [...relationships]);
  const [nodePositions, setNodePositions] = useState<Record<string, { x: number; y: number }>>(() => defaultPositions(models));
  const [search, setSearch] = useState("");
  const [focusedModel, setFocusedModel] = useState<string | null>(null);
  const [focusScope, setFocusScope] = useState<"all" | "upstream" | "downstream">("all");
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [draft, setDraft] = useState<RelationshipDraft | null>(null);
  const [isNewDraft, setIsNewDraft] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [localSaving, setLocalSaving] = useState(false);

  useEffect(() => {
    setLocalRelationships([...relationships]);
  }, [relationships]);

  useEffect(() => {
    setNodePositions((current) => {
      const next = { ...current };
      const defaults = defaultPositions(models);
      let changed = false;
      models.forEach((model) => {
        const id = modelNodeId(model.name);
        if (!next[id]) { next[id] = defaults[id]; changed = true; }
      });
      Object.keys(next).forEach((id) => {
        if (!models.some((model) => modelNodeId(model.name) === id)) { delete next[id]; changed = true; }
      });
      return changed ? next : current;
    });
  }, [models]);

  const openRelationship = useCallback((relationship: RelationshipGraphRelationship) => {
    const index = localRelationships.findIndex((item) => item === relationship || item.name === relationship.name);
    setSelectedEdgeId(index >= 0 ? relationshipEdgeId(relationship, index) : null);
    setDraft(draftFromRelationship(relationship));
    setIsNewDraft(false);
    setFormError(null);
    props.onEdit?.(relationship);
  }, [localRelationships, props]);

  const openNewRelationship = useCallback(() => {
    setSelectedEdgeId(null);
    setDraft(blankDraft(models));
    setIsNewDraft(true);
    setFormError(null);
    props.onEdit?.(null);
  }, [models, props]);

  const closeDrawer = useCallback(() => {
    setDraft(null);
    setSelectedEdgeId(null);
    setFormError(null);
    props.onEdit?.(null);
  }, [props]);

  const openNewOrExistingForModel = useCallback((modelName: string) => {
    const relation = localRelationships.find((candidate) => relationshipModels(candidate)?.includes(modelName));
    if (relation) openRelationship(relation);
    else openNewRelationship();
  }, [localRelationships, openNewRelationship, openRelationship]);

  const graphNodes = useMemo<GraphNode[]>(() => {
    const focusNames = new Set<string>();
    if (focusedModel) {
      focusNames.add(focusedModel);
      if (focusScope !== "all") {
        localRelationships.forEach((relationship) => {
          const endpoints = relationshipModels(relationship);
          if (!endpoints) return;
          const [source, target] = endpoints;
          if (focusScope === "upstream" && target === focusedModel) focusNames.add(source);
          if (focusScope === "downstream" && source === focusedModel) focusNames.add(target);
        });
      } else {
        models.forEach((model) => focusNames.add(model.name));
      }
    }
    return models.map((model) => {
      const primaryKeys = primaryKeyNames(model);
      const relatedColumns = relationColumnNames(model.name, model, localRelationships);
      const isDimmed = Boolean(focusedModel && !focusNames.has(model.name));
      return {
        id: modelNodeId(model.name),
        type: "model",
        position: nodePositions[modelNodeId(model.name)] ?? { x: 40, y: 40 },
        data: {
          model,
          displayName: localizedText(model.displayName, locale, model.name),
          tableName: model.table ?? model.tableName ?? model.physicalName ?? model.name,
          primaryKeys,
          relatedColumns,
          locale,
          isDimmed,
          isActive: focusedModel === model.name,
          onOpen: () => {
            setFocusedModel(model.name);
            setFocusScope("all");
            openNewOrExistingForModel(model.name);
          },
        },
        ariaLabel: `${localizedText(model.displayName, locale, model.name)} model`,
        focusable: true,
      };
    });
  }, [focusedModel, focusScope, localRelationships, locale, models, nodePositions, openNewOrExistingForModel]);

  const graphEdges = useMemo<GraphEdge[]>(() => {
    const relatedVisible = (relationship: RelationshipGraphRelationship) => {
      if (!focusedModel || focusScope === "all") return false;
      const endpoints = relationshipModels(relationship);
      if (!endpoints) return false;
      const [source, target] = endpoints;
      return focusScope === "upstream" ? target === focusedModel : source === focusedModel;
    };
    return localRelationships.flatMap((relationship, index) => {
      const endpoints = relationshipModels(relationship);
      if (!endpoints || !models.some((model) => model.name === endpoints[0]) || !models.some((model) => model.name === endpoints[1])) return [];
      const isDimmed = Boolean(focusedModel && focusScope !== "all" && !relatedVisible(relationship) && !endpoints.includes(focusedModel));
      return [{
        id: relationshipEdgeId(relationship, index),
        type: "relationship",
        source: modelNodeId(endpoints[0]),
        target: modelNodeId(endpoints[1]),
        data: { relationship, label: relationshipLabel(relationship), onOpen: () => openRelationship(relationship), isDimmed },
        selected: selectedEdgeId === relationshipEdgeId(relationship, index),
        focusable: true,
        ariaLabel: `${relationship.name} ${relationshipLabel(relationship)}`,
      }];
    });
  }, [focusedModel, focusScope, localRelationships, models, openRelationship, selectedEdgeId]);

  const searchMatches = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return [];
    return models.filter((model) => [model.name, model.table, model.tableName, model.physicalName, localizedText(model.displayName, locale, "")].filter(Boolean).some((value) => String(value).toLowerCase().includes(query))).slice(0, 8);
  }, [locale, models, search]);

  const focusModel = useCallback((modelName: string, scope: "all" | "upstream" | "downstream" = "all") => {
    setFocusedModel(modelName);
    setFocusScope(scope);
    setSearch(localizedText(models.find((model) => model.name === modelName)?.displayName, locale, modelName));
    const node = graphNodes.find((candidate) => candidate.id === modelNodeId(modelName));
    if (node) void reactFlow.setCenter(node.position.x + 135, node.position.y + 90, { zoom: 1.1, duration: 260 });
  }, [graphNodes, locale, models, reactFlow]);

  const focusNeighbors = useCallback((scope: "upstream" | "downstream") => {
    if (!focusedModel) return;
    const names = new Set<string>([focusedModel]);
    localRelationships.forEach((relationship) => {
      const endpoints = relationshipModels(relationship);
      if (!endpoints) return;
      if (scope === "upstream" && endpoints[1] === focusedModel) names.add(endpoints[0]);
      if (scope === "downstream" && endpoints[0] === focusedModel) names.add(endpoints[1]);
    });
    setFocusScope(scope);
    void reactFlow.fitView({ nodes: graphNodes.filter((node) => names.has(node.data.model.name)), padding: 0.24, duration: 280 });
  }, [focusedModel, graphNodes, localRelationships, reactFlow]);

  const resetView = useCallback(() => {
    setFocusedModel(null);
    setFocusScope("all");
    setSearch("");
    void reactFlow.fitView({ padding: 0.2, duration: 280 });
  }, [reactFlow]);

  const autoLayout = useCallback(() => {
    setNodePositions(defaultPositions(models));
    window.setTimeout(() => { void reactFlow.fitView({ padding: 0.2, duration: 280 }); }, 0);
  }, [models, reactFlow]);

  const handleNodesChange = useCallback((changes: NodeChange<GraphNode>[]) => {
    setNodePositions((current) => {
      let next = current;
      changes.forEach((change) => {
        if (change.type === "position" && change.position) {
          if (next === current) next = { ...current };
          next[change.id] = change.position;
        }
      });
      return next;
    });
  }, []);

  const updateDraft = useCallback((next: RelationshipDraft) => {
    setDraft(next);
    setFormError(null);
  }, []);

  const handleSave = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!draft) return;
    if (!draft.name.trim() || !draft.sourceModel || !draft.targetModel) { setFormError(copy.validation); return; }
    if (draft.sourceModel === draft.targetModel) { setFormError(copy.sameModel); return; }
    const original = draft.existingName ? localRelationships.find((relationship) => relationship.name === draft.existingName) : undefined;
    const nextRelationship = relationshipFromDraft(draft, original);
    const nextRelationships = original ? localRelationships.map((relationship) => relationship === original ? nextRelationship : relationship) : [...localRelationships, nextRelationship];
    setLocalRelationships(nextRelationships);
    props.onChange?.(nextRelationships);
    setLocalSaving(true);
    setFormError(null);
    try {
      await props.onSave?.(nextRelationship, nextRelationships);
      closeDrawer();
    } catch {
      setLocalRelationships(localRelationships);
      props.onChange?.(localRelationships);
      setFormError(copy.saveFailed);
    } finally {
      setLocalSaving(false);
    }
  }, [closeDrawer, copy, draft, localRelationships, props]);

  const handleDelete = useCallback(async () => {
    if (!draft?.existingName) return;
    const relationship = localRelationships.find((candidate) => candidate.name === draft.existingName);
    if (!relationship) return;
    const nextRelationships = localRelationships.filter((candidate) => candidate !== relationship);
    setLocalRelationships(nextRelationships);
    props.onChange?.(nextRelationships);
    setLocalSaving(true);
    try {
      await props.onDelete?.(relationship, nextRelationships);
      closeDrawer();
    } catch {
      setLocalRelationships(localRelationships);
      props.onChange?.(localRelationships);
      setFormError(copy.saveFailed);
    } finally {
      setLocalSaving(false);
    }
  }, [closeDrawer, copy.saveFailed, draft, localRelationships, props]);

  const onSearchKeyDown = useCallback((event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" && searchMatches[0]) {
      event.preventDefault();
      focusModel(searchMatches[0].name);
    }
    if (event.key === "Escape") setSearch("");
  }, [focusModel, searchMatches]);

  const activeTheme = props.theme ?? (typeof document !== "undefined" && document.documentElement.dataset.theme === "dark" ? "dark" : "light");

  return (
    <div className="relationship-graph-workspace">
      <section className="relationship-graph-canvas-panel" aria-label={copy.title}>
        <div className="relationship-graph-toolbar">
          <div className="relationship-graph-search-wrap">
            <MagnifyingGlass size={16} aria-hidden="true" />
            <input className="relationship-graph-search" aria-label={copy.search} placeholder={copy.searchHint} value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={onSearchKeyDown} />
            {searchMatches.length ? <div className="relationship-graph-search-results" role="listbox" aria-label={copy.search}>
              {searchMatches.map((model) => <button type="button" role="option" className="relationship-graph-search-result" key={model.name} onClick={() => focusModel(model.name)}><span><strong>{localizedText(model.displayName, locale, model.name)}</strong><small>{model.name}</small></span><ArrowRight size={14} /></button>)}
            </div> : null}
          </div>
          <div className="relationship-graph-toolbar-actions">
            <button type="button" className="relationship-graph-toolbar-button" onClick={autoLayout} title={copy.layout}><ArrowsOut size={15} />{copy.layout}</button>
            <button type="button" className="relationship-graph-toolbar-button" onClick={resetView} title={copy.reset}><Crosshair size={15} />{copy.reset}</button>
            {!readOnly ? <button type="button" className="relationship-graph-button relationship-graph-button-primary relationship-graph-button-small" onClick={openNewRelationship} disabled={models.length < 2} title={models.length < 2 ? copy.chooseModels : copy.add}><Plus size={15} />{copy.add}</button> : null}
          </div>
        </div>
        {focusedModel ? <div className="relationship-graph-focusbar"><span><Crosshair size={15} />{copy.focus}: <strong>{localizedText(models.find((model) => model.name === focusedModel)?.displayName, locale, focusedModel)}</strong></span><div><button type="button" className={focusScope === "upstream" ? "active" : ""} onClick={() => focusNeighbors("upstream")}><ArrowUp size={14} />{copy.upstream}</button><button type="button" className={focusScope === "downstream" ? "active" : ""} onClick={() => focusNeighbors("downstream")}><ArrowDown size={14} />{copy.downstream}</button><button type="button" className={focusScope === "all" ? "active" : ""} onClick={() => { setFocusScope("all"); resetView(); }}>{copy.all}</button></div></div> : null}
        <div className="relationship-graph-flow" aria-label={copy.title}>
          <ReactFlow<GraphNode, GraphEdge>
            nodes={graphNodes}
            edges={graphEdges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodesChange={handleNodesChange}
            onNodeClick={(_, node) => focusModel(node.data.model.name)}
            onEdgeClick={(_, edge) => { if (edge.data) openRelationship(edge.data.relationship); }}
            onPaneClick={() => { setFocusedModel(null); setFocusScope("all"); }}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            minZoom={0.35}
            maxZoom={1.8}
            panOnDrag
            zoomOnScroll
            zoomOnPinch
            connectionLineType={ConnectionLineType.Bezier}
            nodesDraggable={!readOnly}
            nodesConnectable={false}
            nodesFocusable
            edgesFocusable
            colorMode={activeTheme}
            proOptions={{ hideAttribution: true }}
            aria-label={copy.title}
          >
            <Background gap={22} size={1} color="var(--relationship-grid, var(--border))" />
            <Controls showInteractive={false} position="bottom-left" aria-label="Map controls" />
            <MiniMap pannable zoomable nodeColor={() => "var(--accent)"} maskColor="color-mix(in srgb, var(--surface) 76%, transparent)" position="bottom-right" aria-label="Relationship map overview" />
          </ReactFlow>
          {!localRelationships.length && models.length ? <div className="relationship-graph-flow-empty"><MapTrifold size={24} weight="duotone" /><strong>{copy.noRelationships}</strong><span>{copy.noRelationshipsBody}</span></div> : null}
        </div>
        <div className="relationship-graph-legend"><span><i className="relationship-graph-legend-dot" />{copy.modelCount(models.length)}</span><span><i className="relationship-graph-legend-line" />{copy.relationCount(localRelationships.length)}</span><span className="relationship-graph-legend-hint"><ArrowLeft size={13} />{copy.searchHint}</span></div>
      </section>
      {draft ? <RelationshipDrawer draft={draft} models={models} copy={copy} isNew={isNewDraft} isSaving={Boolean(props.isSaving || localSaving)} readOnly={readOnly} error={formError} onChange={updateDraft} onSave={handleSave} onDelete={handleDelete} onClose={closeDrawer} /> : null}
    </div>
  );
}

/** Render an interactive model relationship map with a synchronized relationship editor drawer. */
export function RelationshipGraph(props: RelationshipGraphProps) {
  const copy = COPY[languageFor(props.locale)];
  const className = ["relationship-graph", props.className].filter(Boolean).join(" ");
  return (
    <section className={className} style={props.style}>
      <div className="relationship-graph-heading">
        <div><p className="relationship-graph-panel-kicker">{copy.eyebrow}</p><h2>{copy.title}</h2><p>{copy.description}</p></div>
        <div className="relationship-graph-heading-meta"><span>{copy.modelCount(props.models.length)}</span><span>{copy.relationCount(props.relationships.length)}</span></div>
      </div>
      {props.error ? <div className="relationship-graph-error" role="alert"><WarningCircle size={17} weight="fill" /><span>{props.error}</span></div> : null}
      {props.loading ? <div className="relationship-graph-state" role="status" aria-live="polite"><CircleNotch className="relationship-graph-spin" size={24} /><strong>{copy.loading}</strong></div> : props.models.length === 0 ? <div className="relationship-graph-state relationship-graph-state-empty"><MapTrifold size={30} weight="duotone" /><strong>{copy.noModels}</strong><span>{copy.noModelsBody}</span></div> : <ReactFlowProvider><RelationshipGraphCanvas props={props} copy={copy} /></ReactFlowProvider>}
    </section>
  );
}

export default RelationshipGraph;
