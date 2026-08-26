import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent, type KeyboardEvent } from "react";
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
  useUpdateNodeInternals,
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
  /** Optional server-derived field pairs. These are read-only and never written back. */
  fieldPairs?: readonly RelationshipGraphFieldPair[];
}

/** A parsed equality between the source and target model columns. */
export interface RelationshipGraphFieldPair {
  sourceField: string;
  targetField: string;
  sourceModel?: string;
  targetModel?: string;
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
  /** Hide the outer heading when the graph is embedded in a page-level tab. */
  showHeading?: boolean;
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
  sourceField: string;
  targetField: string;
  relationshipName: string;
  displayName: string;
  relationshipDescription: string;
  cardinality: string;
  condition: string;
  advancedCondition: string;
  structuredCondition: string;
  sourceFieldHint: string;
  targetFieldHint: string;
  fields: string;
  expandFields: string;
  collapseFields: string;
  previousPage: string;
  nextPage: string;
  fieldPage: (from: number, to: number, total: number) => string;
  noFields: string;
  fieldIncoming: string;
  fieldOutgoing: string;
  fallbackRelationship: string;
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
    sourceField: "起点字段",
    targetField: "终点字段",
    relationshipName: "关系名称",
    displayName: "显示名称",
    relationshipDescription: "描述",
    cardinality: "基数",
    condition: "关联条件",
    advancedCondition: "高级关联条件",
    structuredCondition: "已按字段生成标准条件",
    sourceFieldHint: "选择起点字段",
    targetFieldHint: "选择终点字段",
    fields: "字段",
    expandFields: "展开字段",
    collapseFields: "收起字段",
    previousPage: "上一页",
    nextPage: "下一页",
    fieldPage: (from, to, total) => `${from}-${to} / ${total}`,
    noFields: "暂无字段",
    fieldIncoming: "进入关系",
    fieldOutgoing: "发出关系",
    fallbackRelationship: "未解析字段，显示模型级关系",
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
    validation: "请完整填写关系名称、模型和字段或高级条件。",
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
    sourceField: "Source field",
    targetField: "Target field",
    relationshipName: "Relationship name",
    displayName: "Display name",
    relationshipDescription: "Description",
    cardinality: "Cardinality",
    condition: "Join condition",
    advancedCondition: "Advanced join condition",
    structuredCondition: "Standard condition generated from fields",
    sourceFieldHint: "Choose source field",
    targetFieldHint: "Choose target field",
    fields: "Fields",
    expandFields: "Expand fields",
    collapseFields: "Collapse fields",
    previousPage: "Previous page",
    nextPage: "Next page",
    fieldPage: (from, to, total) => `${from}-${to} / ${total}`,
    noFields: "No fields",
    fieldIncoming: "Incoming relationship",
    fieldOutgoing: "Outgoing relationship",
    fallbackRelationship: "Fields could not be parsed; showing model-level link",
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
    validation: "Complete the required relationship fields.",
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

function localizedForLocale(value: string | RelationshipGraphLocalizedText | undefined, locale: RelationshipGraphLocale | undefined) {
  return localizedText(value, locale, "");
}

function localeKey(locale: RelationshipGraphLocale | undefined): "zh-CN" | "en-US" {
  return languageFor(locale) === "zh" ? "zh-CN" : "en-US";
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

function relationshipEdgeId(relationship: RelationshipGraphRelationship, index: number, pairIndex = 0) {
  return `relationship-edge-${index}-${pairIndex}-${encodeURIComponent(relationship.name || "unnamed")}`;
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

function cleanIdentifier(value: string) {
  return value.trim().replace(/^(["'`])|(["'`])$/g, "");
}

function modelQualifiers(model: RelationshipGraphModel) {
  const table = model.table ?? model.tableName ?? model.physicalName ?? "";
  const tableBase = table.split(".").pop() ?? table;
  const schemaTable = model.schema && tableBase ? `${model.schema}.${tableBase}` : "";
  return new Set([model.name, table, tableBase, schemaTable, model.physicalName, model.tableName].filter(Boolean).map((item) => String(item).toLowerCase()));
}

function columnExists(model: RelationshipGraphModel, name: string) {
  return model.columns.some((column) => column.name.toLowerCase() === name.toLowerCase());
}

function resolveConditionSide(
  side: string,
  model: RelationshipGraphModel,
): string | null {
  const normalized = side.trim().replace(/^\(+|\)+$/g, "");
  const match = normalized.match(/^(?:(.+)\.)?(["'`]?[^.\s"'`]+["'`]?)$/);
  if (!match) return null;
  const qualifier = match[1] ? cleanIdentifier(match[1]).toLowerCase() : "";
  const field = cleanIdentifier(match[2]);
  if (qualifier && !modelQualifiers(model).has(qualifier)) return null;
  return columnExists(model, field) ? model.columns.find((column) => column.name.toLowerCase() === field.toLowerCase())?.name ?? null : null;
}

/**
 * Parse simple equality predicates into field pairs. The parser deliberately
 * rejects aliases, casts, functions and non-equality expressions so the graph
 * can safely fall back to model-level handles without inventing a connection.
 */
export function parseRelationshipFieldPairs(
  relationship: RelationshipGraphRelationship,
  models: readonly RelationshipGraphModel[],
): RelationshipGraphFieldPair[] {
  const endpoints = relationshipModels(relationship);
  if (!endpoints || !relationship.condition?.trim()) return [];
  const [sourceModelName, targetModelName] = endpoints;
  const sourceModel = models.find((model) => model.name === sourceModelName);
  const targetModel = models.find((model) => model.name === targetModelName);
  if (!sourceModel || !targetModel) return [];
  const pairs: RelationshipGraphFieldPair[] = [];
  for (const predicate of relationship.condition.split(/\s+AND\s+/i)) {
    const equality = predicate.trim().match(/^\(?\s*(.+?)\s*=\s*(.+?)\s*\)?$/);
    if (!equality) return [];
    const left = resolveConditionSide(equality[1], sourceModel);
    const right = resolveConditionSide(equality[2], targetModel);
    const reverseLeft = resolveConditionSide(equality[1], targetModel);
    const reverseRight = resolveConditionSide(equality[2], sourceModel);
    if (left && right) pairs.push({ sourceField: left, targetField: right });
    else if (reverseLeft && reverseRight) pairs.push({ sourceField: reverseRight, targetField: reverseLeft });
    else return [];
  }
  return pairs;
}

function relationFieldPairs(
  relationship: RelationshipGraphRelationship,
  models: readonly RelationshipGraphModel[],
): RelationshipGraphFieldPair[] {
  const endpoints = relationshipModels(relationship);
  if (endpoints && Array.isArray(relationship.fieldPairs)) {
    const [sourceModelName, targetModelName] = endpoints;
    const sourceModel = models.find((model) => model.name === sourceModelName);
    const targetModel = models.find((model) => model.name === targetModelName);
    const explicit = relationship.fieldPairs.filter((pair) =>
      (!pair.sourceModel || pair.sourceModel === sourceModelName)
      && (!pair.targetModel || pair.targetModel === targetModelName)
      && Boolean(sourceModel && targetModel && columnExists(sourceModel, pair.sourceField) && columnExists(targetModel, pair.targetField)),
    ).map((pair) => ({ sourceField: sourceModel?.columns.find((column) => column.name.toLowerCase() === pair.sourceField.toLowerCase())?.name ?? pair.sourceField, targetField: targetModel?.columns.find((column) => column.name.toLowerCase() === pair.targetField.toLowerCase())?.name ?? pair.targetField }));
    // A present array is an authoritative server projection. In particular,
    // an empty array means the condition was intentionally judged unsafe for
    // field-level rendering; do not reinterpret it with the legacy fallback.
    return explicit;
  }
  const parsed = parseRelationshipFieldPairs(relationship, models);
  if (parsed.length) return parsed;
  if (!endpoints) return [];
  const [source, target] = endpoints;
  const sourceNames = relationship.joinColumns?.[source];
  const targetNames = relationship.joinColumns?.[target];
  const sourceFields = sourceNames ? (Array.isArray(sourceNames) ? sourceNames : [sourceNames]) : [];
  const targetFields = targetNames ? (Array.isArray(targetNames) ? targetNames : [targetNames]) : [];
  return sourceFields.length === targetFields.length && sourceFields.length > 0
    ? sourceFields.map((sourceField, index) => ({ sourceField, targetField: targetFields[index] }))
    : [];
}

/** Resolve server-provided pairs first, then the safe condition parser. */
export function getRelationshipFieldPairs(
  relationship: RelationshipGraphRelationship,
  models: readonly RelationshipGraphModel[],
) {
  return relationFieldPairs(relationship, models);
}

/** Select the pair represented by a clicked edge, preserving composite joins. */
export function selectRelationshipFieldPair(
  relationship: RelationshipGraphRelationship,
  models: readonly RelationshipGraphModel[],
  selected?: RelationshipGraphFieldPair,
) {
  const pairs = relationFieldPairs(relationship, models);
  if (!selected) return pairs[0];
  return pairs.find((pair) => pair.sourceField === selected.sourceField && pair.targetField === selected.targetField) ?? selected;
}

/** Complex or server-rejected joins stay in the lossless condition editor. */
export function relationshipUsesAdvancedCondition(
  relationship: RelationshipGraphRelationship,
  models: readonly RelationshipGraphModel[],
) {
  return relationFieldPairs(relationship, models).length !== 1;
}

function fieldHandleId(type: "source" | "target", field: string) {
  return `${type}-field-${encodeURIComponent(field)}`;
}

function modelHandleId(type: "source" | "target") {
  return `${type}-model`;
}

function relationColumnNames(modelName: string, model: RelationshipGraphModel, relationships: readonly RelationshipGraphRelationship[], models: readonly RelationshipGraphModel[]) {
  const names = new Set<string>();
  relationships.forEach((relationship) => {
    const endpoints = relationshipModels(relationship);
    if (!endpoints || !endpoints.includes(modelName)) return;
    const pairs = relationFieldPairs(relationship, models);
    const endpointIndex = endpoints[0] === modelName ? 0 : 1;
    pairs.forEach((pair) => names.add(endpointIndex === 0 ? pair.sourceField : pair.targetField));
    const explicit = relationship.joinColumns?.[modelName];
    if (explicit) (Array.isArray(explicit) ? explicit : [explicit]).forEach((name) => names.add(name));
  });
  return model.columns.filter((column) => names.has(column.name));
}

function defaultPositions(models: readonly RelationshipGraphModel[], relationships: readonly RelationshipGraphRelationship[] = []) {
  const modelNames = new Set(models.map((model) => model.name));
  const rank = new Map(models.map((model) => [model.name, 0]));
  const indegree = new Map(models.map((model) => [model.name, 0]));
  const outgoing = new Map(models.map((model) => [model.name, [] as string[]]));
  relationships.forEach((relationship) => {
    const endpoints = relationshipModels(relationship);
    if (!endpoints || !modelNames.has(endpoints[0]) || !modelNames.has(endpoints[1]) || endpoints[0] === endpoints[1]) return;
    outgoing.get(endpoints[0])?.push(endpoints[1]);
    indegree.set(endpoints[1], (indegree.get(endpoints[1]) ?? 0) + 1);
  });
  const queue = models.filter((model) => (indegree.get(model.name) ?? 0) === 0).map((model) => model.name);
  for (let index = 0; index < queue.length; index += 1) {
    const source = queue[index];
    (outgoing.get(source) ?? []).forEach((target) => {
      rank.set(target, Math.max(rank.get(target) ?? 0, (rank.get(source) ?? 0) + 1));
      const nextIndegree = (indegree.get(target) ?? 1) - 1;
      indegree.set(target, nextIndegree);
      if (nextIndegree === 0) queue.push(target);
    });
  }
  const layerOffsets = new Map<number, number>();
  return models.reduce<Record<string, { x: number; y: number }>>((positions, model) => {
    const layer = rank.get(model.name) ?? 0;
    const layerIndex = layerOffsets.get(layer) ?? 0;
    layerOffsets.set(layer, layerIndex + 1);
    positions[modelNodeId(model.name)] = { x: layer * 390 + 40, y: layerIndex * 260 + 40 };
    return positions;
  }, {});
}

type GraphNodeData = {
  model: RelationshipGraphModel;
  displayName: string;
  tableName: string;
  primaryKeys: string[];
  relatedColumns: RelationshipGraphColumn[];
  otherColumns: RelationshipGraphColumn[];
  activeFieldNames: string[];
  locale?: RelationshipGraphLocale;
  copy: RelationshipGraphCopy;
  isDimmed: boolean;
  isActive: boolean;
  onFieldFocus: (fieldName: string) => void;
  onOpen: () => void;
};

type GraphNode = Node<GraphNodeData, "model">;

type GraphEdgeData = {
  relationship: RelationshipGraphRelationship;
  label: string;
  fieldPair?: RelationshipGraphFieldPair;
  labelOffset: number;
  onOpen: () => void;
  isDimmed: boolean;
};

type GraphEdge = Edge<GraphEdgeData, "relationship">;

function primaryKeyNames(model: RelationshipGraphModel) {
  const explicit = model.primaryKey ? (Array.isArray(model.primaryKey) ? model.primaryKey : [model.primaryKey]) : [];
  const columns = model.columns.filter((column) => column.primaryKey || column.isPrimaryKey).map((column) => column.name);
  return Array.from(new Set([...explicit, ...columns]));
}

function ModelNode({ id, data }: NodeProps<GraphNode>) {
  const [expanded, setExpanded] = useState(false);
  const [page, setPage] = useState(0);
  const updateNodeInternals = useUpdateNodeInternals();
  const pageSize = 6;
  const copy = data.copy;
  const tableName = [data.model.schema, data.tableName].filter(Boolean).join(".");
  const pages = Math.max(1, Math.ceil(data.otherColumns.length / pageSize));
  const currentPage = Math.min(page, pages - 1);
  const visibleColumns = data.otherColumns.slice(currentPage * pageSize, (currentPage + 1) * pageSize);
  const visibleFieldSignature = [...data.relatedColumns, ...(expanded ? visibleColumns : [])].map((column) => column.name).join("\u0000");
  useEffect(() => {
    updateNodeInternals(id);
  }, [currentPage, expanded, id, updateNodeInternals, visibleFieldSignature]);
  const fieldRow = (column: RelationshipGraphColumn, related: boolean) => {
    const active = data.activeFieldNames.includes(column.name);
    const label = column.displayName ? localizedText(column.displayName, data.locale, column.name) : column.name;
    return (
      <div className={`relationship-graph-column-row ${related ? "relationship-graph-column-row-related" : ""} ${active ? "relationship-graph-column-row-active" : ""}`} key={column.name} title={column.name} role="button" tabIndex={0} aria-label={column.name} onClick={(event) => { event.stopPropagation(); data.onFieldFocus(column.name); }} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); data.onFieldFocus(column.name); } }}>
        <Handle type="target" position={Position.Left} id={fieldHandleId("target", column.name)} className="relationship-graph-field-handle" aria-label={`${copy.fieldIncoming}: ${column.name}`} />
        <span className="relationship-graph-column-name" title={column.name}><code>{column.name}</code>{column.primaryKey || column.isPrimaryKey ? <span className="relationship-graph-column-key" title={copy.primaryKey}>PK</span> : null}</span>
        <span className="relationship-graph-column-display" title={label}>{label}</span>
        <span className="relationship-graph-column-type" title={column.type ?? column.dataType ?? ""}>{column.type ?? column.dataType ?? "-"}</span>
        <Handle type="source" position={Position.Right} id={fieldHandleId("source", column.name)} className="relationship-graph-field-handle" aria-label={`${copy.fieldOutgoing}: ${column.name}`} />
      </div>
    );
  };
  return (
    <div className={`relationship-graph-node ${data.isDimmed ? "relationship-graph-node-dimmed" : ""} ${data.isActive ? "relationship-graph-node-active" : ""}`}>
      <Handle type="target" position={Position.Left} id={modelHandleId("target")} className="relationship-graph-model-handle" aria-label={`${copy.fieldIncoming}: ${data.displayName}`} />
      <Handle type="source" position={Position.Right} id={modelHandleId("source")} className="relationship-graph-model-handle" aria-label={`${copy.fieldOutgoing}: ${data.displayName}`} />
      <div className="relationship-graph-node-header">
        <span className="relationship-graph-node-glyph" aria-hidden="true"><MapTrifold size={16} weight="duotone" /></span>
        <div className="relationship-graph-node-title">
          <strong title={data.displayName}>{data.displayName}</strong>
          <small title={data.model.name}>{data.model.name}</small>
        </div>
        <button type="button" className="relationship-graph-node-edit nodrag nopan" aria-label={`${copy.edit}: ${data.displayName}`} onClick={(event) => { event.stopPropagation(); data.onOpen(); }}>
          <PencilSimple size={15} aria-hidden="true" />
        </button>
      </div>
      <div className="relationship-graph-node-table"><span>{copy.table}</span><code>{tableName || data.model.name}</code></div>
      <div className="relationship-graph-node-fields">
        <div className="relationship-graph-column-heading"><span>{copy.fields}</span><span>{data.model.columns.length}</span></div>
        {data.relatedColumns.length ? <div className="relationship-graph-column-section"><small>{copy.relatedFields}</small>{data.relatedColumns.map((column) => fieldRow(column, true))}</div> : null}
        {expanded ? <div className="relationship-graph-column-section"><small>{copy.fields}</small>{visibleColumns.length ? visibleColumns.map((column) => fieldRow(column, false)) : <span className="relationship-graph-column-empty">{copy.noFields}</span>}<div className="relationship-graph-column-pagination"><button type="button" className="relationship-graph-page-button nodrag nopan" onClick={(event) => { event.stopPropagation(); setPage(Math.max(0, currentPage - 1)); }} disabled={currentPage === 0} aria-label={copy.previousPage}><ArrowLeft size={12} /></button><span>{copy.fieldPage(data.otherColumns.length ? currentPage * pageSize + 1 : 0, Math.min((currentPage + 1) * pageSize, data.otherColumns.length), data.otherColumns.length)}</span><button type="button" className="relationship-graph-page-button nodrag nopan" onClick={(event) => { event.stopPropagation(); setPage(Math.min(pages - 1, currentPage + 1)); }} disabled={currentPage >= pages - 1} aria-label={copy.nextPage}><ArrowRight size={12} /></button></div></div> : null}
        <button type="button" className="relationship-graph-expand-button nodrag nopan" onClick={(event) => { event.stopPropagation(); setExpanded((value) => !value); setPage(0); }} aria-expanded={expanded}>{expanded ? copy.collapseFields : copy.expandFields}<span>{expanded ? "−" : "+"}</span></button>
      </div>
    </div>
  );
}

function RelationshipEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected }: EdgeProps<GraphEdge>) {
  const [edgePath, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  const label = data?.label ?? "1:N";
  const fieldLabel = data?.fieldPair ? `${data.fieldPair.sourceField} → ${data.fieldPair.targetField}` : "";
  return (
    <>
      <BaseEdge id={id} path={edgePath} interactionWidth={28} className={data?.isDimmed ? "relationship-graph-edge-dimmed" : ""} style={{ stroke: selected ? "var(--accent)" : "var(--relationship-edge, var(--border-strong))", strokeWidth: selected ? 2.5 : 1.7 }} />
      <EdgeLabelRenderer>
        <button
          type="button"
          className={`relationship-graph-edge-label nodrag nopan ${selected ? "relationship-graph-edge-label-selected" : ""} ${data?.isDimmed ? "relationship-graph-edge-label-dimmed" : ""}`}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px,${labelY + (data?.labelOffset ?? 0)}px)` }}
          aria-label={`${data?.relationship.name ?? label}${fieldLabel ? `: ${fieldLabel}` : ""} (${label})`}
          onClick={(event) => { event.stopPropagation(); data?.onOpen(); }}
          onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); data?.onOpen(); } }}
        >
          {fieldLabel ? <><span>{label}</span><small>{fieldLabel}</small></> : label}
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
  sourceField: string;
  targetField: string;
  cardinality: string;
  condition: string;
  displayName: string;
  description: string;
  advancedCondition: boolean;
};

function draftFromRelationship(relationship: RelationshipGraphRelationship, models: readonly RelationshipGraphModel[], locale?: RelationshipGraphLocale, selectedPair?: RelationshipGraphFieldPair): RelationshipDraft {
  const endpoints = relationshipModels(relationship) ?? ["", ""];
  const pairs = relationFieldPairs(relationship, models);
  const selected = selectedPair ?? pairs[0];
  return {
    existingName: relationship.name,
    name: relationship.name,
    sourceModel: endpoints[0],
    targetModel: endpoints[1],
    sourceField: selected?.sourceField ?? "",
    targetField: selected?.targetField ?? "",
    cardinality: cardinalityValue(relationship.cardinality ?? relationship.joinType),
    condition: relationship.condition ?? "",
    displayName: localizedForLocale(relationship.displayName, locale),
    description: localizedForLocale(relationship.description, locale),
    advancedCondition: relationshipUsesAdvancedCondition(relationship, models),
  };
}

function blankDraft(models: readonly RelationshipGraphModel[]): RelationshipDraft {
  return {
    name: "",
    sourceModel: models[0]?.name ?? "",
    targetModel: models[1]?.name ?? "",
    sourceField: "",
    targetField: "",
    cardinality: "one-to-many",
    condition: "",
    displayName: "",
    description: "",
    advancedCondition: false,
  };
}

function mergeLocalizedValue(existing: string | RelationshipGraphLocalizedText | undefined, value: string, locale?: RelationshipGraphLocale) {
  const key = localeKey(locale);
  const trimmed = value.trim();
  if (typeof existing === "string") return trimmed || existing ? { [key]: trimmed || existing } : undefined;
  const next = { ...(existing ?? {}) };
  if (trimmed || Object.keys(next).length) next[key] = trimmed;
  return Object.keys(next).length ? next : undefined;
}

function relationshipFromDraft(draft: RelationshipDraft, original?: RelationshipGraphRelationship, locale?: RelationshipGraphLocale): RelationshipGraphRelationship {
  const generatedCondition = draft.sourceField && draft.targetField
    ? `${draft.sourceModel}.${draft.sourceField} = ${draft.targetModel}.${draft.targetField}`
    : draft.condition;
  const { fieldPairs: _readOnlyFieldPairs, ...editableOriginal } = original ?? {};
  const next: RelationshipGraphRelationship = {
    ...editableOriginal,
    name: draft.name.trim(),
    models: [draft.sourceModel, draft.targetModel],
    joinType: wrenJoinType(draft.cardinality),
    cardinality: cardinalityValue(draft.cardinality),
    // Advanced conditions may contain intentional leading/trailing newlines or
    // indentation. Treat the editor value as source text and preserve it byte
    // for byte; structured mode still generates the canonical equality.
    condition: draft.advancedCondition ? draft.condition : generatedCondition,
    displayName: mergeLocalizedValue(original?.displayName, draft.displayName, locale),
    description: mergeLocalizedValue(original?.description, draft.description, locale),
  };
  return next;
}

function RelationshipDrawer({
  draft,
  models,
  locale,
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
  locale?: RelationshipGraphLocale;
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
        <button type="button" className="relationship-graph-icon-button" onClick={onClose} aria-label={copy.close} disabled={isSaving}><X size={17} /></button>
      </div>
      <form className="relationship-graph-form" onSubmit={onSave} aria-busy={isSaving}>
        <fieldset className="relationship-graph-form-fields" disabled={isSaving || readOnly}>
        {error ? <div className="relationship-graph-form-error" role="alert"><WarningCircle size={16} weight="fill" />{error}</div> : null}
        <label className="relationship-graph-field"><span>{copy.relationshipName}</span><input autoFocus className="relationship-graph-input" value={draft.name} onChange={(event) => onChange({ ...draft, name: event.target.value })} required /></label>
        <div className="relationship-graph-form-grid">
          <label className="relationship-graph-field"><span>{copy.sourceModel}</span><select className="relationship-graph-input" value={draft.sourceModel} onChange={(event) => onChange({ ...draft, sourceModel: event.target.value, sourceField: "", targetField: "", condition: "" })} disabled={!models.length} required><option value="">-</option>{models.map((model) => <option value={model.name} key={model.name}>{localizedText(model.displayName, locale, model.name)} ({model.name})</option>)}</select></label>
          <label className="relationship-graph-field"><span>{copy.targetModel}</span><select className="relationship-graph-input" value={draft.targetModel} onChange={(event) => onChange({ ...draft, targetModel: event.target.value, sourceField: "", targetField: "", condition: "" })} disabled={!models.length} required><option value="">-</option>{models.map((model) => <option value={model.name} key={model.name}>{localizedText(model.displayName, locale, model.name)} ({model.name})</option>)}</select></label>
        </div>
        <div className="relationship-graph-form-grid">
          <label className="relationship-graph-field"><span>{copy.sourceField}</span><select className="relationship-graph-input" aria-label={copy.sourceField} value={draft.sourceField} onChange={(event) => { const sourceField = event.target.value; onChange({ ...draft, sourceField, condition: sourceField && draft.targetField ? `${draft.sourceModel}.${sourceField} = ${draft.targetModel}.${draft.targetField}` : "", advancedCondition: false }); }} disabled={!draft.sourceModel || draft.advancedCondition}><option value="">{copy.sourceFieldHint}</option>{(models.find((model) => model.name === draft.sourceModel)?.columns ?? []).map((column) => <option value={column.name} key={column.name}>{column.name}{column.type || column.dataType ? ` (${column.type ?? column.dataType})` : ""}</option>)}</select></label>
          <label className="relationship-graph-field"><span>{copy.targetField}</span><select className="relationship-graph-input" aria-label={copy.targetField} value={draft.targetField} onChange={(event) => { const targetField = event.target.value; onChange({ ...draft, targetField, condition: draft.sourceField && targetField ? `${draft.sourceModel}.${draft.sourceField} = ${draft.targetModel}.${targetField}` : "", advancedCondition: false }); }} disabled={!draft.targetModel || draft.advancedCondition}><option value="">{copy.targetFieldHint}</option>{(models.find((model) => model.name === draft.targetModel)?.columns ?? []).map((column) => <option value={column.name} key={column.name}>{column.name}{column.type || column.dataType ? ` (${column.type ?? column.dataType})` : ""}</option>)}</select></label>
        </div>
        <label className="relationship-graph-field"><span>{copy.cardinality}</span><select className="relationship-graph-input" value={draft.cardinality} onChange={(event) => onChange({ ...draft, cardinality: event.target.value })}><option value="one-to-one">1:1</option><option value="one-to-many">1:N</option><option value="many-to-one">N:1</option><option value="many-to-many">N:N</option></select></label>
        <label className="relationship-graph-checkbox"><input type="checkbox" checked={draft.advancedCondition} onChange={(event) => onChange({ ...draft, advancedCondition: event.target.checked })} /><span>{copy.advancedCondition}</span></label>
        <label className="relationship-graph-field"><span>{copy.condition}</span><textarea className="relationship-graph-input relationship-graph-textarea relationship-graph-condition" value={draft.condition} onChange={(event) => onChange({ ...draft, condition: event.target.value, advancedCondition: true })} placeholder="orders.customer_id = customers.id" aria-describedby="relationship-graph-condition-hint" /><small id="relationship-graph-condition-hint" className="relationship-graph-form-hint">{draft.advancedCondition ? copy.condition : copy.structuredCondition}</small></label>
        <div className="relationship-graph-form-divider" />
        <label className="relationship-graph-field"><span>{copy.displayName}</span><input className="relationship-graph-input" value={draft.displayName} onChange={(event) => onChange({ ...draft, displayName: event.target.value })} /></label>
        <label className="relationship-graph-field"><span>{copy.relationshipDescription}</span><textarea className="relationship-graph-input relationship-graph-textarea" value={draft.description} onChange={(event) => onChange({ ...draft, description: event.target.value })} /></label>
        <div className="relationship-graph-drawer-actions">
          {!isNew && !readOnly ? <button type="button" className="relationship-graph-button relationship-graph-button-danger" onClick={onDelete} disabled={isSaving}><Trash size={15} />{copy.delete}</button> : null}
          <span className="relationship-graph-action-spacer" />
          <button type="button" className="relationship-graph-button relationship-graph-button-secondary" onClick={onClose}>{copy.cancel}</button>
          {!readOnly ? <button type="submit" className="relationship-graph-button relationship-graph-button-primary" disabled={isSaving}><span>{isSaving ? <CircleNotch className="relationship-graph-spin" size={15} /> : null}</span>{isSaving ? copy.saving : copy.save}</button> : null}
        </div>
        </fieldset>
      </form>
    </aside>
  );
}

function RelationshipGraphCanvas({ props, copy }: { props: RelationshipGraphProps; copy: RelationshipGraphCopy }) {
  const { models, locale, relationships, readOnly = false } = props;
  const reactFlow = useReactFlow<GraphNode, GraphEdge>();
  const initialFitTimerRef = useRef<number | undefined>(undefined);
  const [localRelationships, setLocalRelationships] = useState<RelationshipGraphRelationship[]>(() => [...relationships]);
  const [nodePositions, setNodePositions] = useState<Record<string, { x: number; y: number }>>(() => defaultPositions(models, relationships));
  const [search, setSearch] = useState("");
  const [focusedModel, setFocusedModel] = useState<string | null>(null);
  const [focusScope, setFocusScope] = useState<"all" | "upstream" | "downstream">("all");
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [highlightedFields, setHighlightedFields] = useState<Record<string, string[]>>({});
  const [draft, setDraft] = useState<RelationshipDraft | null>(null);
  const [isNewDraft, setIsNewDraft] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [localSaving, setLocalSaving] = useState(false);

  useEffect(() => () => {
    if (initialFitTimerRef.current !== undefined) window.clearTimeout(initialFitTimerRef.current);
  }, []);

  useEffect(() => {
    setLocalRelationships([...relationships]);
  }, [relationships]);

  useEffect(() => {
    setNodePositions((current) => {
      const next = { ...current };
      const defaults = defaultPositions(models, localRelationships);
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
  }, [localRelationships, models]);

  const openRelationship = useCallback((relationship: RelationshipGraphRelationship, pair?: RelationshipGraphFieldPair) => {
    const index = localRelationships.findIndex((item) => item === relationship || item.name === relationship.name);
    const pairs = relationFieldPairs(relationship, models);
    const selectedPairIndex = pair ? Math.max(0, pairs.findIndex((item) => item.sourceField === pair.sourceField && item.targetField === pair.targetField)) : 0;
    const highlightedPairs = pair ? [pair] : pairs;
    const selectedPair = selectRelationshipFieldPair(relationship, models, pair);
    setSelectedEdgeId(index >= 0 ? relationshipEdgeId(relationship, index, selectedPairIndex) : null);
    const endpoints = relationshipModels(relationship);
    setHighlightedFields(endpoints && highlightedPairs.length ? {
      [endpoints[0]]: Array.from(new Set(highlightedPairs.map((item) => item.sourceField))),
      [endpoints[1]]: Array.from(new Set(highlightedPairs.map((item) => item.targetField))),
    } : {});
    setDraft(draftFromRelationship(relationship, models, locale, selectedPair));
    setIsNewDraft(false);
    setFormError(null);
    props.onEdit?.(relationship);
  }, [localRelationships, locale, models, props]);

  const openNewRelationship = useCallback(() => {
    setSelectedEdgeId(null);
    setHighlightedFields({});
    setDraft(blankDraft(models));
    setIsNewDraft(true);
    setFormError(null);
    props.onEdit?.(null);
  }, [models, props]);

  const closeDrawer = useCallback(() => {
    setDraft(null);
    setSelectedEdgeId(null);
    setHighlightedFields({});
    setFormError(null);
    props.onEdit?.(null);
  }, [props]);

  const openNewOrExistingForModel = useCallback((modelName: string) => {
    const relation = localRelationships.find((candidate) => relationshipModels(candidate)?.includes(modelName));
    if (relation) openRelationship(relation);
    else openNewRelationship();
  }, [localRelationships, openNewRelationship, openRelationship]);

  const focusField = useCallback((modelName: string, fieldName: string) => {
    setFocusedModel(modelName);
    setFocusScope("all");
    setHighlightedFields({ [modelName]: [fieldName] });
  }, []);

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
      const relatedColumns = relationColumnNames(model.name, model, localRelationships, models);
      const relatedNames = new Set(relatedColumns.map((column) => column.name));
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
          otherColumns: model.columns.filter((column) => !relatedNames.has(column.name)),
          activeFieldNames: highlightedFields[model.name] ?? [],
          locale,
          copy,
          isDimmed,
          isActive: focusedModel === model.name,
          onFieldFocus: (fieldName: string) => focusField(model.name, fieldName),
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
  }, [copy, focusedModel, focusField, focusScope, highlightedFields, localRelationships, locale, models, nodePositions, openNewOrExistingForModel]);

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
      const pairs = relationFieldPairs(relationship, models);
      const fieldHighlightActive = Object.keys(highlightedFields).length > 0;
      const fieldMatches = !fieldHighlightActive || pairs.some((pair) => highlightedFields[endpoints[0]]?.includes(pair.sourceField) || highlightedFields[endpoints[1]]?.includes(pair.targetField));
      const isDimmed = Boolean((focusedModel && focusScope !== "all" && !relatedVisible(relationship) && !endpoints.includes(focusedModel)) || !fieldMatches);
      const edgePairs: Array<RelationshipGraphFieldPair | undefined> = pairs.length ? pairs : [undefined];
      return edgePairs.map((fieldPair, pairIndex) => {
        const edgeId = relationshipEdgeId(relationship, index, pairIndex);
        return {
          id: edgeId,
          type: "relationship",
          source: modelNodeId(endpoints[0]),
          target: modelNodeId(endpoints[1]),
          sourceHandle: fieldPair ? fieldHandleId("source", fieldPair.sourceField) : modelHandleId("source"),
          targetHandle: fieldPair ? fieldHandleId("target", fieldPair.targetField) : modelHandleId("target"),
          data: { relationship, fieldPair, label: relationshipLabel(relationship), labelOffset: pairs.length > 1 ? (pairIndex - (pairs.length - 1) / 2) * 30 : 0, onOpen: () => openRelationship(relationship, fieldPair), isDimmed },
          selected: selectedEdgeId === edgeId,
          focusable: true,
          ariaLabel: `${relationship.name}${fieldPair ? ` ${fieldPair.sourceField} to ${fieldPair.targetField}` : ""} ${relationshipLabel(relationship)}`,
        };
      });
    });
  }, [focusedModel, focusScope, highlightedFields, localRelationships, models, openRelationship, selectedEdgeId]);

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
    setNodePositions(defaultPositions(models, localRelationships));
    if (initialFitTimerRef.current !== undefined) window.clearTimeout(initialFitTimerRef.current);
    initialFitTimerRef.current = window.setTimeout(() => {
      initialFitTimerRef.current = undefined;
      void reactFlow.fitView({ padding: 0.2, duration: 280 });
    }, 0);
  }, [localRelationships, models, reactFlow]);

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
    if (!draft || localSaving || props.isSaving) return;
    if (!draft.name.trim() || !draft.sourceModel || !draft.targetModel) { setFormError(copy.validation); return; }
    if (draft.sourceModel === draft.targetModel) { setFormError(copy.sameModel); return; }
    if (draft.advancedCondition ? !draft.condition.trim() : (!draft.sourceField || !draft.targetField)) { setFormError(copy.validation); return; }
    const original = draft.existingName ? localRelationships.find((relationship) => relationship.name === draft.existingName) : undefined;
    const nextRelationship = relationshipFromDraft(draft, original, locale);
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
  }, [closeDrawer, copy, draft, localRelationships, localSaving, locale, props]);

  const handleDelete = useCallback(async () => {
    if (!draft?.existingName || localSaving || props.isSaving) return;
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
  }, [closeDrawer, copy.saveFailed, draft, localRelationships, localSaving, props]);

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
            onInit={(instance) => {
              if (initialFitTimerRef.current !== undefined) window.clearTimeout(initialFitTimerRef.current);
              // A tab can initialize React Flow before its final layout box is
              // measurable. Delay the first fit so it cannot clamp to maxZoom.
              initialFitTimerRef.current = window.setTimeout(() => {
                initialFitTimerRef.current = undefined;
                void instance.fitView({ padding: 0.2, duration: 0 });
              }, 120);
            }}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodesChange={handleNodesChange}
            onNodeClick={(_, node) => focusModel(node.data.model.name)}
            onEdgeClick={(_, edge) => { if (edge.data) openRelationship(edge.data.relationship, edge.data.fieldPair); }}
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
      {draft ? <RelationshipDrawer draft={draft} models={models} locale={locale} copy={copy} isNew={isNewDraft} isSaving={Boolean(props.isSaving || localSaving)} readOnly={readOnly} error={formError} onChange={updateDraft} onSave={handleSave} onDelete={handleDelete} onClose={closeDrawer} /> : null}
    </div>
  );
}

/** Render an interactive model relationship map with a synchronized relationship editor drawer. */
export function RelationshipGraph(props: RelationshipGraphProps) {
  const copy = COPY[languageFor(props.locale)];
  const className = ["relationship-graph", props.className].filter(Boolean).join(" ");
  return (
    <section className={className} style={props.style}>
      {props.showHeading !== false ? <div className="relationship-graph-heading">
        <div><p className="relationship-graph-panel-kicker">{copy.eyebrow}</p><h2>{copy.title}</h2><p>{copy.description}</p></div>
        <div className="relationship-graph-heading-meta"><span>{copy.modelCount(props.models.length)}</span><span>{copy.relationCount(props.relationships.length)}</span></div>
      </div> : null}
      {props.error ? <div className="relationship-graph-error" role="alert"><WarningCircle size={17} weight="fill" /><span>{props.error}</span></div> : null}
      {props.loading ? <div className="relationship-graph-state" role="status" aria-live="polite"><CircleNotch className="relationship-graph-spin" size={24} /><strong>{copy.loading}</strong></div> : props.models.length === 0 ? <div className="relationship-graph-state relationship-graph-state-empty"><MapTrifold size={30} weight="duotone" /><strong>{copy.noModels}</strong><span>{copy.noModelsBody}</span></div> : <ReactFlowProvider><RelationshipGraphCanvas props={props} copy={copy} /></ReactFlowProvider>}
    </section>
  );
}

export default RelationshipGraph;
