import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ArrowClockwise,
  ArrowRight,
  ArrowsClockwise,
  BookOpenText,
  BracketsCurly,
  CaretDown,
  CaretRight,
  Check,
  CheckCircle,
  ClockCounterClockwise,
  Code,
  Columns,
  Cube,
  Database,
  DotsThree,
  DownloadSimple,
  Eye,
  FloppyDisk,
  GearSix,
  GitBranch,
  House,
  Info,
  MagnifyingGlass,
  Moon,
  PencilSimple,
  Play,
  Plus,
  RocketLaunch,
  Rows,
  ShareNetwork,
  SidebarSimple,
  Stack,
  Sun,
  Table,
  TreeStructure,
  UploadSimple,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { api } from "./api/client";
import "./i18n";
import { setConsoleLocale } from "./i18n";
import type { ColumnRecord, ConsoleSection, CubeSnapshot, Datasource, DatasourceField, DatasourceType, KnowledgeRulesResponse, LocalizedText, ProjectDiff, ProjectFile, ProjectSummary, SchemaRecord, SemanticModel, SemanticProjectSnapshot, SemanticRelationship, SqlCandidatesResponse, TableRecord, Theme, ValidationIssue, VersionRecord, ViewDefinition, ViewPreviewResult, ViewSnapshot, ViewValidationResponse, ViewWritePayload } from "./types";
import { Badge, Button, EmptyState, Field, InlineNotice, LoadingRows, Modal, SectionHeading, Select, TextArea, TextInput } from "./components/ui";
import ModelEditor from "./components/ModelEditor";
import { RelationshipGraph, type RelationshipGraphLocalizedText, type RelationshipGraphRelationship } from "./components/RelationshipGraph";
import RuleWorkbench, { type KnowledgeRule, type RuleSaveAction } from "./components/RuleWorkbench";
import SqlKnowledgeWorkbench, { type SqlKnowledgeCandidate, type SqlValidation } from "./components/SqlKnowledgeWorkbench";
import CubeWorkbench, { type CubeDefinition } from "./components/CubeWorkbench";

const ViewWorkbench = lazy(() => import("./components/ViewWorkbench"));

type Notice = { tone: "info" | "success" | "warning" | "error"; title: string; body?: string } | null;

const navGroups: { labelKey: string; items: { id: ConsoleSection; labelKey: string; icon: typeof House; count?: string }[] }[] = [
  { labelKey: "nav.workspace", items: [{ id: "overview", labelKey: "nav.overview", icon: House }, { id: "datasources", labelKey: "nav.datasources", icon: Database }, { id: "schema", labelKey: "nav.schema", icon: Table }] },
  { labelKey: "nav.semanticLayer", items: [{ id: "models", labelKey: "nav.models", icon: Cube }, { id: "relationships", labelKey: "nav.relationships", icon: ShareNetwork }, { id: "views", labelKey: "nav.views", icon: Eye }, { id: "cubes", labelKey: "nav.cubes", icon: Stack }, { id: "rules", labelKey: "nav.rules", icon: BookOpenText }, { id: "sqlKnowledge", labelKey: "nav.sqlKnowledge", icon: Code }, { id: "mdl", labelKey: "nav.mdl", icon: BracketsCurly }] },
];

function formatDate(value?: string, locale = "en-US") {
  if (!value) return "No activity yet";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
}

function sourceLabel(source?: Datasource) {
  if (!source) return "No source selected";
  return `${source.name} (${datasourceTypeLabel(source.type)})`;
}

function datasourceTypeLabel(type: string) {
  if (type === "postgres" || type === "postgresql") return "PostgreSQL";
  if (type === "mysql" || type === "mariadb") return "MySQL";
  return type;
}

const endpointLabels = ["project", "datasource types", "data sources", "project files", "versions", "health"];

function isEditableProjectFile(path: string) {
  return /\.(ya?ml|md|json|sql|toml|txt)$/i.test(path);
}

function isInstructionFile(path: string) {
  return /instructions\.(ya?ml|md)$/i.test(path);
}

function localizedValue(value: string | RelationshipGraphLocalizedText | undefined, fallback: string): LocalizedText {
  if (typeof value === "string") return { "zh-CN": value, "en-US": value };
  return {
    "zh-CN": value?.["zh-CN"] ?? value?.zh ?? "",
    "en-US": value?.["en-US"] ?? value?.en ?? fallback,
  };
}

function semanticRelationships(value: RelationshipGraphRelationship[]): SemanticRelationship[] {
  return value.map((relationship) => ({
    name: relationship.name,
    models: relationship.models.slice(0, 2).map((model) => typeof model === "string" ? model : model.name) as [string, string],
    joinType: relationship.joinType ?? "ONE_TO_MANY",
    condition: relationship.condition ?? "",
    displayName: localizedValue(relationship.displayName, relationship.name),
    description: localizedValue(relationship.description, ""),
  }));
}

function knowledgeRules(snapshot: KnowledgeRulesResponse | null): KnowledgeRule[] {
  return (snapshot?.rules ?? []).map((rule) => ({
    id: rule.id, name: rule.title, content: rule.content, enabled: rule.enabled,
    sourcePath: rule.sourcePath, scope: rule.scope, tags: rule.tags,
    updatedAt: rule.updatedAt, draft: rule.draft, sourceContent: rule.sourceContent, diff: rule.diff,
  }));
}

function knowledgeCandidates(snapshot: SqlCandidatesResponse | null): SqlKnowledgeCandidate[] {
  return (snapshot?.candidates ?? []).map((candidate) => ({
    id: candidate.id, question: candidate.question, sql: candidate.sql,
    queryId: candidate.queryId, sessionId: candidate.sessionId, status: candidate.status,
    submittedAt: candidate.createdAt, reviewedAt: candidate.reviewedAt,
    reviewer: candidate.reviewer, reviewNote: candidate.reviewNote,
    stats: candidate.stats === undefined ? undefined : {
      durationMs: typeof candidate.stats.durationMs === "number" ? candidate.stats.durationMs : undefined,
      rowCount: typeof candidate.stats.returnedRows === "number" ? candidate.stats.returnedRows : undefined,
      dialect: candidate.dialect ?? (typeof candidate.stats.dialect === "string" ? candidate.stats.dialect : undefined),
    },
    sqlHistory: candidate.sqlHistory,
    sourcePath: candidate.approvedPath,
  }));
}

function App() {
  const { t, i18n: activeI18n } = useTranslation();
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem("semantic-console-theme") as Theme) || "light");
  const [section, setSection] = useState<ConsoleSection>("overview");
  const [project, setProject] = useState<ProjectSummary>({});
  const [datasourceTypes, setDatasourceTypes] = useState<DatasourceType[]>([]);
  const [datasources, setDatasources] = useState<Datasource[]>([]);
  const [schemas, setSchemas] = useState<SchemaRecord[]>([]);
  const [tables, setTables] = useState<TableRecord[]>([]);
  const [columns, setColumns] = useState<ColumnRecord[]>([]);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [versions, setVersions] = useState<VersionRecord[]>([]);
  const [instructions, setInstructions] = useState("");
  const [mdlSource, setMdlSource] = useState("");
  const [selectedDatasourceId, setSelectedDatasourceId] = useState("");
  const [selectedSchema, setSelectedSchema] = useState("");
  const [selectedTable, setSelectedTable] = useState("");
  const [schemaSearch, setSchemaSearch] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [apiOnline, setApiOnline] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [showMobileNav, setShowMobileNav] = useState(false);
  const [showDatasourceForm, setShowDatasourceForm] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [draftSavedAt, setDraftSavedAt] = useState<string | null>(null);
  const [selectedFilePath, setSelectedFilePath] = useState("");
  const [fileLoading, setFileLoading] = useState(false);
  const [semanticProject, setSemanticProject] = useState<SemanticProjectSnapshot | null>(null);
  const [semanticLoading, setSemanticLoading] = useState(false);
  const [semanticDiff, setSemanticDiff] = useState<ProjectDiff | null>(null);
  const [semanticDiffLoading, setSemanticDiffLoading] = useState(false);
  const [rulesSnapshot, setRulesSnapshot] = useState<KnowledgeRulesResponse | null>(null);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [selectedRuleId, setSelectedRuleId] = useState("");
  const [sqlCandidates, setSqlCandidates] = useState<SqlCandidatesResponse | null>(null);
  const [sqlLoading, setSqlLoading] = useState(false);
  const [cubeSnapshot, setCubeSnapshot] = useState<CubeSnapshot | null>(null);
  const [cubeLoading, setCubeLoading] = useState(false);
  const [viewSnapshot, setViewSnapshot] = useState<ViewSnapshot | null>(null);
  const [viewLoading, setViewLoading] = useState(false);
  const [viewError, setViewError] = useState<string | null>(null);
  const fileRequestRef = useRef(0);
  const diffRequestRef = useRef(0);

  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem("semantic-console-theme", theme); }, [theme]);

  useEffect(() => {
    document.documentElement.lang = activeI18n.language === "zh-CN" ? "zh-CN" : "en-US";
  }, [activeI18n.language]);

  async function loadSemanticProject() {
    setSemanticLoading(true);
    const result = await api.getSemanticProject().catch((error: unknown) => {
      setNotice({ tone: "error", title: t("model.noModels"), body: error instanceof Error ? error.message : t("model.noModelsBody") });
      return null;
    });
    if (result) setSemanticProject(result);
    setSemanticLoading(false);
  }

  async function loadSemanticDiff(path: string) {
    if (!path) return;
    const requestId = ++diffRequestRef.current;
    setSemanticDiffLoading(true);
    setSemanticDiff(null);
    const result = await api.getProjectDiff(path).catch(() => null);
    if (requestId === diffRequestRef.current) {
      setSemanticDiff(result);
      setSemanticDiffLoading(false);
    }
  }

  async function loadRules() {
    setRulesLoading(true);
    const result = await api.getRules().catch((error: unknown) => {
      setNotice({ tone: "error", title: "Rules could not be loaded", body: error instanceof Error ? error.message : undefined });
      return null;
    });
    if (result) {
      setRulesSnapshot(result);
      const selected = result.rules.find((rule) => rule.id === selectedRuleId) ?? result.rules[0];
      setSelectedRuleId(selected?.id ?? "");
      if (selected) {
        setSelectedFilePath(selected.sourcePath);
        void loadProjectFile(selected.sourcePath);
        void loadSemanticDiff(selected.sourcePath);
      }
    }
    setRulesLoading(false);
  }

  async function loadSqlCandidates() {
    setSqlLoading(true);
    const result = await api.getSqlCandidates().catch((error: unknown) => {
      setNotice({ tone: "error", title: "SQL review queue could not be loaded", body: error instanceof Error ? error.message : undefined });
      return null;
    });
    if (result) setSqlCandidates(result);
    setSqlLoading(false);
  }

  async function loadCubes() {
    setCubeLoading(true);
    const result = await api.getCubes().catch((error: unknown) => {
      setNotice({ tone: "error", title: "Cubes could not be loaded", body: error instanceof Error ? error.message : undefined });
      return null;
    });
    if (result) setCubeSnapshot(result);
    setCubeLoading(false);
  }

  async function loadViews() {
    setViewLoading(true);
    setViewError(null);
    const result = await api.getViews().catch((error: unknown) => {
      setViewError(error instanceof Error ? error.message : "Views could not be loaded.");
      return null;
    });
    if (result) setViewSnapshot(result);
    setViewLoading(false);
  }

  async function loadProjectFile(path: string, fileList = files) {
    if (!path || !isEditableProjectFile(path)) return;
    const requestId = ++fileRequestRef.current;
    setFileLoading(true);
    setSelectedFilePath(path);
    if (isInstructionFile(path)) setInstructions("");
    else setMdlSource("");
    const result = await api.getProjectFile(path).catch((error: unknown) => {
      if (requestId !== fileRequestRef.current) return null;
      const message = error instanceof Error ? error.message : "The project file could not be read.";
      setNotice({ tone: "error", title: "Could not read project file", body: message });
      return null;
    });
    if (requestId !== fileRequestRef.current) return;
    if (result?.content !== undefined) {
      if (isInstructionFile(path)) setInstructions(result.content);
      else setMdlSource(result.content);
      setFiles((current) => current.map((file) => file.path === path ? { ...file, ...result } : file));
    } else if (!fileList.some((file) => file.path === path)) {
      setNotice({ tone: "error", title: "Project file is unavailable", body: `No file named ${path} was returned by the API.` });
    }
    setFileLoading(false);
  }

  const refreshWorkspace = async (): Promise<boolean> => {
    setLoadError(null);
    setRefreshing(true);
    const results = await Promise.allSettled([api.getProject(), api.getDatasourceTypes(), api.getDatasources(), api.getProjectFiles(), api.getVersions(), api.health()]);
    const [projectResult, typesResult, datasourcesResult, filesResult, versionsResult, healthResult] = results;
    if (projectResult.status === "fulfilled") setProject(projectResult.value);
    if (typesResult.status === "fulfilled") setDatasourceTypes(typesResult.value);
    if (datasourcesResult.status === "fulfilled") {
      setDatasources(datasourcesResult.value);
      const activeId = projectResult.status === "fulfilled" ? projectResult.value.activeDatasource?.id : undefined;
      setSelectedDatasourceId((current) => current || activeId || datasourcesResult.value[0]?.id || "");
    }
    if (filesResult.status === "fulfilled") {
      setFiles(filesResult.value);
      const firstFile = filesResult.value.find((file) => isEditableProjectFile(file.path) && !isInstructionFile(file.path));
      if (firstFile) {
        setSelectedFilePath((current) => current && filesResult.value.some((file) => file.path === current) && !isInstructionFile(current) ? current : firstFile.path);
        void loadProjectFile(firstFile.path, filesResult.value);
      } else { setSelectedFilePath(""); setMdlSource(""); }
    }
    if (versionsResult.status === "fulfilled") setVersions(versionsResult.value);
    const healthOk = healthResult.status === "fulfilled" && healthResult.value.status === "ok";
    const failures = results
      .map((result, index) => result.status === "rejected" ? endpointLabels[index] : healthResult.status === "fulfilled" && !healthOk && index === 5 ? "health" : null)
      .filter((label): label is string => Boolean(label));
    setApiOnline(healthOk);
    if (failures.length) {
      setLoadError(`Could not load ${failures.join(", ")}. Check the API and retry.`);
      setNotice({ tone: "error", title: "Semantic Console is offline", body: `Could not load ${failures.join(", ")}. No local project data was substituted.` });
    }
    setLoading(false); setRefreshing(false);
    return failures.length === 0;
  };
  useEffect(() => { void refreshWorkspace(); }, []);

  const selectedDatasource = datasources.find((item) => item.id === selectedDatasourceId);
  const activeDatasourceId = selectedDatasource?.id ?? "";
  const selectedType = datasourceTypes.find((item) => item.type === selectedDatasource?.type) ?? datasourceTypes.find((item) => item.available !== false);
  const filteredTables = useMemo(() => tables.filter((table) => table.name.toLowerCase().includes(schemaSearch.toLowerCase())), [schemaSearch, tables]);

  async function handleLoadSchema(schema = selectedSchema, datasourceId = activeDatasourceId) {
    if (!datasourceId) {
      setSchemas([]); setTables([]); setColumns([]);
      setNotice({ tone: "warning", title: "No data source selected", body: "Connect a data source before browsing schema metadata." });
      return;
    }
    setBusyAction("schema");
    const schemaResult = await api.getSchemas(datasourceId).catch((error: unknown) => ({ error: error instanceof Error ? error.message : "Schema metadata could not be loaded." }));
    if ("error" in schemaResult) {
      setSchemas([]); setTables([]); setColumns([]); setBusyAction(null);
      setNotice({ tone: "error", title: "Schema metadata unavailable", body: schemaResult.error });
      return;
    }
    setSchemas(schemaResult);
    const targetSchema = schema || schemaResult[0]?.name || "";
    setSelectedSchema(targetSchema);
    if (!targetSchema) { setTables([]); setColumns([]); setBusyAction(null); return; }
    const tableResult = await api.getTables(datasourceId, targetSchema).catch((error: unknown) => ({ error: error instanceof Error ? error.message : "Table metadata could not be loaded." }));
    if ("error" in tableResult) {
      setTables([]); setColumns([]); setBusyAction(null);
      setNotice({ tone: "error", title: "Table metadata unavailable", body: tableResult.error });
      return;
    }
    setTables(tableResult);
    const firstTable = tableResult[0]?.name;
    if (firstTable && !tableResult.some((table) => table.name === selectedTable)) await handleSelectTable(firstTable, datasourceId, targetSchema);
    setBusyAction(null);
  }
  async function handleSelectTable(table: string, datasourceId = activeDatasourceId, schemaName = selectedSchema) {
    if (!datasourceId || !schemaName || !table) return;
    setSelectedTable(table); setBusyAction("columns");
    const result = await api.getColumns(datasourceId, schemaName, table).catch((error: unknown) => ({ error: error instanceof Error ? error.message : "Column metadata could not be loaded." }));
    if ("error" in result) {
      setColumns([]); setNotice({ tone: "error", title: "Column metadata unavailable", body: result.error });
    } else setColumns(result);
    setBusyAction(null);
  }
  async function handleValidate() {
    setBusyAction("validate");
    const result = await api.validateProject().catch((error: unknown) => { setNotice({ tone: "error", title: "Validation request failed", body: error instanceof Error ? error.message : "The validation API could not be reached." }); return null; });
    setBusyAction(null);
    if (!result) return;
    const issues: ValidationIssue[] = result.errors ?? result.warnings ?? [];
    if (result.valid) setNotice({ tone: "success", title: "Validation passed", body: `${result.warningCount ?? issues.length} warning(s) found in the current project.` });
    else setNotice({ tone: "error", title: "Validation needs attention", body: `${(result.errorCount ?? issues.length) || 1} error(s) need review before publishing.` });
  }
  async function handlePublish() {
    setBusyAction("publish");
    const result = await api.publishProject().catch((error: unknown) => { setNotice({ tone: "error", title: "Publish failed", body: error instanceof Error ? error.message : "The project could not be published." }); return null; });
    setBusyAction(null);
    if (!result) return;
    const refreshed = await refreshWorkspace();
    if (!refreshed) return;
    setNotice({ tone: "success", title: "Project published", body: result.version?.revision ? `Revision ${result.version.revision} is ready for downstream queries.` : "The project was published successfully." });
  }
  async function saveRelationshipDraft(relationships: RelationshipGraphRelationship[]) {
    if (!semanticProject) return;
    setBusyAction("relationships");
    try {
      const result = await api.updateSemanticRelationships({
        relationships: semanticRelationships(relationships),
        expectedRevision: semanticProject.revision,
      });
      setSemanticProject(result);
      markSaved();
      setNotice({ tone: "success", title: t("model.saved"), body: t("model.modelSaveBody") });
    } catch (error) {
      setNotice({ tone: "error", title: t("model.saveError"), body: error instanceof Error ? error.message : t("model.saveError") });
      throw error;
    } finally {
      setBusyAction(null);
    }
  }
  async function toggleRule(rule: KnowledgeRule, enabled: boolean) {
    const result = await api.setRuleEnabled(rule.id, enabled, rulesSnapshot?.revision);
    setRulesSnapshot((current) => current ? { ...current, revision: result.revision, rules: result.rules } : current);
    markSaved();
  }
  async function saveRule(rule: KnowledgeRule, action: RuleSaveAction) {
    const result = await api.updateRule(rule.id, {
      title: rule.name, content: rule.content, enabled: rule.enabled,
      scope: rule.scope, tags: rule.tags, expectedRevision: rulesSnapshot?.revision,
    });
    setRulesSnapshot((current) => current ? { ...current, revision: result.revision, rules: result.rules } : current);
    markSaved();
    if (action === "publish") {
      await api.publishProject();
      await Promise.all([refreshWorkspace(), loadRules()]);
    }
  }
  async function createRule(input: { name: string; content: string; enabled: boolean; scope: string[]; tags: string[] }) {
    const result = await api.createRule({
      title: input.name, content: input.content, enabled: input.enabled,
      scope: input.scope, tags: input.tags, expectedRevision: rulesSnapshot?.revision,
    });
    setRulesSnapshot((current) => current ? { ...current, revision: result.revision, rules: result.rules } : current);
    setSelectedRuleId(result.rule.id);
    setSelectedFilePath(result.rule.sourcePath);
    await Promise.all([loadProjectFile(result.rule.sourcePath), loadSemanticDiff(result.rule.sourcePath)]);
    markSaved();
  }
  async function deleteRule(rule: KnowledgeRule) {
    const currentRules = rulesSnapshot?.rules ?? [];
    const currentIndex = currentRules.findIndex((item) => item.id === rule.id);
    const result = await api.deleteRule(rule.id, rulesSnapshot?.revision);
    setRulesSnapshot((current) => current ? { ...current, revision: result.revision, rules: result.rules } : current);
    const next = result.rules[Math.min(Math.max(currentIndex, 0), Math.max(result.rules.length - 1, 0))];
    setSelectedRuleId(next?.id ?? "");
    if (next) {
      setSelectedFilePath(next.sourcePath);
      await Promise.all([loadProjectFile(next.sourcePath), loadSemanticDiff(next.sourcePath)]);
    } else {
      setSelectedFilePath(""); setMdlSource(""); setSemanticDiff(null);
    }
    markSaved();
  }
  async function approveCandidate(candidate: SqlKnowledgeCandidate, sql: string) {
    await api.approveSqlCandidate(candidate.id, { sql });
    await loadSqlCandidates();
    markSaved();
  }
  async function rejectCandidate(candidate: SqlKnowledgeCandidate, note: string) {
    await api.rejectSqlCandidate(candidate.id, note);
    await loadSqlCandidates();
  }
  async function validateCandidate(candidate: SqlKnowledgeCandidate): Promise<SqlValidation> {
    const result = await api.validateSqlCandidate(candidate.id, candidate.sql);
    return { status: result.valid ? "passed" : "failed", message: result.message, checkedAt: new Date().toISOString() };
  }
  async function saveCube(cube: CubeDefinition) {
    const result = await api.saveCube(cube.name, { ...cube, expectedRevision: cubeSnapshot?.revision });
    setCubeSnapshot(result);
    markSaved();
  }
  async function createCubeDraft(input: { name: string; baseObject: string }) {
    if (!input.baseObject || !cubeSnapshot) {
      setNotice({ tone: "warning", title: "A base object is required", body: "Create a model or view before adding a cube." });
      return;
    }
    const result = await api.createCube({
      name: input.name, sourcePath: `cubes/${input.name}/metadata.yml`, baseObject: input.baseObject,
      measures: [], dimensions: [], timeDimensions: [], hierarchies: {}, expectedRevision: cubeSnapshot.revision,
    });
    setCubeSnapshot(result);
    markSaved();
  }
  async function deleteCubeDraft(cube: CubeDefinition) {
    const result = await api.deleteCube(cube.name, cubeSnapshot?.revision);
    setCubeSnapshot(result);
    const next = result.cubes[0];
    if (next) {
      setSelectedFilePath(next.sourcePath);
      await Promise.all([loadProjectFile(next.sourcePath), loadSemanticDiff(next.sourcePath)]);
    } else {
      setSelectedFilePath(""); setMdlSource(""); setSemanticDiff(null);
    }
    markSaved();
  }
  async function saveViewDraft(view: ViewDefinition) {
    const result = await api.saveView(view.name, {
      name: view.name,
      statement: view.statement,
      storage: view.storage,
      properties: view.properties ?? {},
      ...(view.dialect ? { dialect: view.dialect } : {}),
      expectedRevision: viewSnapshot?.revision,
    });
    setViewSnapshot(result);
    const saved = result.views.find((item) => item.name === view.name);
    if (saved) {
      setSelectedFilePath(saved.storage === "sql" && saved.sqlPath ? saved.sqlPath : saved.sourcePath);
      await loadProjectFile(saved.storage === "sql" && saved.sqlPath ? saved.sqlPath : saved.sourcePath);
    }
    markSaved();
  }
  async function createViewDraft(input: ViewWritePayload) {
    const result = await api.createView({ ...input, expectedRevision: viewSnapshot?.revision });
    setViewSnapshot(result);
    const created = result.views.find((view) => view.name === input.name);
    if (created) {
      const path = created.storage === "sql" && created.sqlPath ? created.sqlPath : created.sourcePath;
      setSelectedFilePath(path);
      await loadProjectFile(path);
    }
    markSaved();
  }
  async function deleteViewDraft(view: ViewDefinition) {
    const result = await api.deleteView(view.name, viewSnapshot?.revision);
    setViewSnapshot(result);
    const next = result.views[0];
    if (next) {
      const path = next.storage === "sql" && next.sqlPath ? next.sqlPath : next.sourcePath;
      setSelectedFilePath(path);
      await Promise.all([loadProjectFile(path), loadSemanticDiff(path)]);
    } else {
      setSelectedFilePath(""); setMdlSource(""); setSemanticDiff(null);
    }
    markSaved();
  }
  async function validateViewDraft(view: ViewDefinition): Promise<ViewValidationResponse> {
    return api.validateView(view.name, {
      name: view.name,
      statement: view.statement,
      storage: view.storage,
      properties: view.properties ?? {},
      ...(view.dialect ? { dialect: view.dialect } : {}),
      expectedRevision: viewSnapshot?.revision,
    });
  }
  async function previewViewDraft(view: ViewDefinition): Promise<ViewPreviewResult> {
    const current = viewSnapshot?.views.find((item) => item.name === view.name);
    const currentShape = current ? JSON.stringify({ statement: current.statement, storage: current.storage, dialect: current.dialect ?? "", properties: current.properties ?? {} }) : "";
    const draftShape = JSON.stringify({ statement: view.statement, storage: view.storage, dialect: view.dialect ?? "", properties: view.properties ?? {} });
    if (currentShape !== draftShape) await saveViewDraft(view);
    return api.previewView(view.name, { limit: 100, maxBytes: 524_288 });
  }
  function openProjectFile(path: string) { setSection("mdl"); setShowMobileNav(false); setNotice(null); void loadProjectFile(path); }
  function navigate(next: ConsoleSection) {
    setSection(next); setShowMobileNav(false); setNotice(null);
    if (next === "models" || next === "relationships" || next === "views") void loadSemanticProject();
    if (next === "rules") void loadRules();
    if (next === "sqlKnowledge") void loadSqlCandidates();
    if (next === "cubes") void loadCubes();
    if (next === "views") void loadViews();
    if (next === "schema" && activeDatasourceId) void handleLoadSchema();
    if (next === "instructions") {
      const instructionFile = files.find((file) => isInstructionFile(file.path));
      if (instructionFile) void loadProjectFile(instructionFile.path);
      else setInstructions("");
    }
    if (next === "mdl") {
      const sourceFile = selectedFilePath && files.some((file) => file.path === selectedFilePath) && !isInstructionFile(selectedFilePath)
        ? selectedFilePath
        : files.find((file) => isEditableProjectFile(file.path) && !isInstructionFile(file.path))?.path;
      if (sourceFile) void loadProjectFile(sourceFile);
      else { setSelectedFilePath(""); setMdlSource(""); }
    }
  }
  function markSaved() { const saved = new Date().toISOString(); setDraftSavedAt(saved); setProject((current) => ({ ...current, status: "draft", updatedAt: saved })); setNotice({ tone: "success", title: t("status.draftSaved"), body: t("status.draftSafe") }); }
  async function handleSaveFile(path: string, content: string) {
    if (!path) {
      setNotice({ tone: "error", title: "Draft save failed", body: "Choose a project file before saving." });
      return;
    }
    setBusyAction("save-file");
    const expectedRevision = files.find((file) => file.path === path)?.revision;
    const result = await api.updateProjectFile({ path, content, ...(expectedRevision ? { expectedRevision } : {}) }).catch((error: unknown) => { setNotice({ tone: "error", title: "Draft save failed", body: error instanceof Error ? error.message : "The project file could not be saved." }); return null; });
    setBusyAction(null);
    if (result) {
      setFiles((current) => current.some((file) => file.path === path) ? current.map((file) => file.path === path ? { ...file, ...result } : file) : [...current, result]);
      markSaved();
    }
  }
  function updateSelectedDatasource(patch: Partial<Datasource>) { if (selectedDatasource) setDatasources((current) => current.map((item) => item.id === selectedDatasource.id ? { ...item, ...patch } : item)); }
  async function handleSaveDatasource() {
    if (!selectedDatasource) return;
    setBusyAction("save-datasource"); const payload = { name: selectedDatasource.name, type: selectedDatasource.type, connection: selectedDatasource.connection ?? {} };
    const result = await (selectedDatasource.id.startsWith("new-") ? api.createDatasource(payload) : api.updateDatasource(selectedDatasource.id, payload)).catch((error: unknown) => { setNotice({ tone: "error", title: "Data source save failed", body: error instanceof Error ? error.message : "The data source could not be saved." }); return null; });
    if (!result) { setBusyAction(null); return; }
    setDatasources((current) => selectedDatasource.id.startsWith("new-") ? [...current.filter((item) => item.id !== selectedDatasource.id), result] : current.map((item) => item.id === selectedDatasource.id ? result : item));
    setSelectedDatasourceId(result.id);
    setBusyAction(null); setShowDatasourceForm(false); setNotice({ tone: "success", title: "Data source saved", body: "Connection details are stored with secrets redacted from responses." });
  }
  async function handleTestDatasource() {
    if (!selectedDatasource) return;
    if (selectedDatasource.id.startsWith("new-")) { setNotice({ tone: "warning", title: "Save the data source first", body: "A connection test requires a server-side datasource profile." }); return; }
    setBusyAction("test-datasource"); const result = await api.testDatasource(selectedDatasource.id, { connection: selectedDatasource.connection }).catch((error: unknown) => { setNotice({ tone: "error", title: "Connection test failed", body: error instanceof Error ? error.message : "The connection test API could not be reached." }); return null; });
    if (!result) { setBusyAction(null); return; }
    updateSelectedDatasource({ lastTest: result }); setBusyAction(null);
    setNotice(result.ok ? { tone: "success", title: "Connection successful", body: `${result.driver ?? selectedDatasource.type} responded in ${result.latencyMs ?? "-"} ms.` } : { tone: "error", title: "Connection failed", body: result.message ?? "Check the connection details and try again." });
  }
  async function handleActivateDatasource(id: string) {
    setBusyAction(`activate-${id}`);
    const result = await api.activateDatasource(id).catch((error: unknown) => { setNotice({ tone: "error", title: "Could not set current data source", body: error instanceof Error ? error.message : "The activation API could not be reached." }); return null; });
    if (!result) { setBusyAction(null); return; }
    if (result.project) setProject(result.project);
    else if (result.activeDatasource) setProject((current) => ({ ...current, activeDatasource: result.activeDatasource }));
    setBusyAction(null);
    setNotice({ tone: "success", title: "Current data source updated", body: "New query sessions will use this connection." });
  }
  function addDatasource() { const type = datasourceTypes.find((item) => item.available !== false); if (!type) { setNotice({ tone: "error", title: "No available datasource types", body: "The API did not return a datasource type that can be configured." }); return; } const id = `new-${Date.now()}`; setDatasources((current) => [...current, { id, name: "New data source", type: type.type, connection: {} }]); setSelectedDatasourceId(id); setShowDatasourceForm(true); }
  async function importSelectedTable() {
    if (!activeDatasourceId || !selectedSchema || !selectedTable) {
      setNotice({ tone: "error", title: "Nothing to import", body: "Select a datasource, schema, and table first." });
      return;
    }
    setBusyAction("import-model");
    const result = await api.generateModel(activeDatasourceId, selectedSchema, selectedTable, { name: selectedTable }).catch((error: unknown) => { setNotice({ tone: "error", title: "Model import failed", body: error instanceof Error ? error.message : "The model generation API could not be reached." }); return null; });
    setBusyAction(null);
    if (!result) return;
    setShowImportModal(false);
    const refreshed = await refreshWorkspace();
    if (!refreshed) return;
    setSelectedFilePath(result.file);
    await loadProjectFile(result.file);
    setSection("mdl");
    setNotice({ tone: "success", title: `${selectedTable} imported`, body: `Draft model written to ${result.file}.` });
  }
  async function handleImportProject(fileList: FileList | null) {
    const selectedFiles = fileList ? Array.from(fileList) : [];
    if (!selectedFiles.length) return;
    setBusyAction("import-project");
    const imported = await Promise.all(selectedFiles.map(async (file) => ({ path: file.webkitRelativePath || file.name, content: await file.text() })));
    const result = await api.importProject({ files: imported }).catch((error: unknown) => { setNotice({ tone: "error", title: "Project import failed", body: error instanceof Error ? error.message : "The project import API could not be reached." }); return null; });
    setBusyAction(null);
    if (!result) return;
    setFiles(result.files);
    setProject((current) => ({ ...current, revision: result.revision, status: result.draft ? "draft" : current.status }));
    const refreshed = await refreshWorkspace();
    if (!refreshed) return;
    setSection("mdl");
    setNotice({ tone: "success", title: "Project imported", body: `${result.files.length} file${result.files.length === 1 ? "" : "s"} loaded from the API.` });
  }
  function rollback(version: VersionRecord) {
    setBusyAction(`rollback-${version.id}`);
    void api.rollbackVersion(version.id).then(async (result) => {
      const refreshed = await refreshWorkspace();
      setShowHistory(false);
      if (!refreshed) return;
      setNotice({ tone: "success", title: `Restored ${version.revision}`, body: result.project?.revision ? `Revision ${result.project.revision} is now active.` : "The selected version was restored." });
    }).catch((error: unknown) => {
      setNotice({ tone: "error", title: "Rollback failed", body: error instanceof Error ? error.message : "The selected version could not be restored." });
    }).finally(() => setBusyAction(null));
  }

  return <div className="app-shell">
    <Sidebar projectName={project.name || project.projectName || "Semantic project"} section={section} onNavigate={navigate} open={showMobileNav} onClose={() => setShowMobileNav(false)} />
    <div className="app-main">
      <header className="topbar"><div className="topbar-left"><button className="icon-button mobile-menu" onClick={() => setShowMobileNav(true)} aria-label={t("nav.open")}><SidebarSimple size={19} /></button><div className="breadcrumbs"><span>{t("nav.workspace")}</span><CaretRight size={13} /><strong>{pageTitle(section, t)}</strong></div></div><div className="topbar-actions"><span className={`connection-state ${apiOnline ? "online" : "offline"}`}><span className="connection-dot" />{apiOnline ? t("common.connected") : t("common.unavailable")}</span><label className="locale-switcher"><span className="sr-only">{t("language.label")}</span><select aria-label={t("language.switchTo")} value={activeI18n.language === "zh-CN" ? "zh-CN" : "en-US"} onChange={(event) => setConsoleLocale(event.target.value as "zh-CN" | "en-US")}><option value="en-US">EN</option><option value="zh-CN">中</option></select></label><button className="icon-button" onClick={() => setTheme(theme === "light" ? "dark" : "light")} aria-label={t(theme === "light" ? "common.switchToDark" : "common.switchToLight")}>{theme === "light" ? <Moon size={18} /> : <Sun size={18} />}</button><button className="icon-button" onClick={() => setShowHistory(true)} aria-label={t("common.openHistory")}><ClockCounterClockwise size={18} /></button><div className="avatar" aria-label={t("common.signedIn")}>WS</div></div></header>
      <main className="content">
        {notice ? <InlineNotice tone={notice.tone} title={notice.title} onDismiss={() => { setNotice(null); setLoadError(null); }}>{notice.body}</InlineNotice> : null}
        {loading ? <LoadingWorkspace /> : <>
          {section === "overview" ? <Overview project={project} datasources={datasources} versions={versions} files={files} onNavigate={navigate} onHistory={() => setShowHistory(true)} /> : null}
          {section === "datasources" ? <DatasourcesPage datasources={datasources} types={datasourceTypes} selectedId={activeDatasourceId} activeId={project.activeDatasource?.id ?? ""} setSelectedId={setSelectedDatasourceId} showForm={showDatasourceForm} setShowForm={setShowDatasourceForm} onAdd={addDatasource} selected={selectedDatasource} selectedType={selectedType} onUpdate={updateSelectedDatasource} onSave={handleSaveDatasource} onTest={handleTestDatasource} onActivate={handleActivateDatasource} busyAction={busyAction} /> : null}
          {section === "schema" ? <SchemaPage datasources={datasources} selectedDatasource={selectedDatasource} selectedSchema={selectedSchema} setSelectedSchema={(value) => { setSelectedSchema(value); void handleLoadSchema(value); }} schemas={schemas} tables={filteredTables} search={schemaSearch} setSearch={setSchemaSearch} selectedTable={selectedTable} onSelectTable={handleSelectTable} columns={columns} onImport={() => setShowImportModal(true)} busyAction={busyAction} onRefresh={() => void handleLoadSchema()} onDatasourceChange={(id: string) => { setSelectedDatasourceId(id); setSelectedSchema(""); setSelectedTable(""); void handleLoadSchema("", id); }} /> : null}
          {section === "models" ? <ModelsPage files={files} onOpenFile={openProjectFile} snapshot={semanticProject} loading={semanticLoading} sourceContent={mdlSource} sourceLoading={fileLoading} diff={semanticDiff} diffLoading={semanticDiffLoading} onSave={async (model) => { const result = await api.updateSemanticModel(model.name, { ...model, expectedRevision: semanticProject?.revision }); if (result) { setSemanticProject(result); markSaved(); } }} onOpenSource={(path) => { setSelectedFilePath(path); void loadProjectFile(path); }} onLoadDiff={(path) => void loadSemanticDiff(path)} /> : null}
          {section === "relationships" ? <RelationshipsPage snapshot={semanticProject} loading={semanticLoading} locale={activeI18n.language} theme={theme} saving={busyAction === "relationships"} sourceContent={mdlSource} sourceLoading={fileLoading} diff={semanticDiff} diffLoading={semanticDiffLoading} onOpenSource={(path) => { setSelectedFilePath(path); void loadProjectFile(path); }} onLoadDiff={(path) => void loadSemanticDiff(path)} onOpenEditor={(path) => { setSelectedFilePath(path); void loadProjectFile(path); setSection("mdl"); }} onSave={saveRelationshipDraft} /> : null}
          {section === "views" ? <Suspense fallback={<div className="panel" role="status" aria-label={t("nav.views")}><LoadingRows /></div>}><ViewWorkbench snapshot={viewSnapshot} loading={viewLoading} error={viewError} locale={activeI18n.language === "zh-CN" ? "zh-CN" : "en-US"} theme={theme} modelNames={(semanticProject?.models ?? []).map((model) => model.name)} sourceContent={mdlSource} sourceLoading={fileLoading} diff={semanticDiff} diffLoading={semanticDiffLoading} onSave={saveViewDraft} onCreate={createViewDraft} onDelete={deleteViewDraft} onValidate={validateViewDraft} onPreview={previewViewDraft} onRetry={() => void loadViews()} onOpenSource={(path) => { setSelectedFilePath(path); void loadProjectFile(path); }} onLoadDiff={(path) => void loadSemanticDiff(path)} /></Suspense> : null}
          {section === "cubes" ? <CubeWorkbench snapshot={cubeSnapshot} loading={cubeLoading} sourceContent={mdlSource} sourceLoading={fileLoading} diff={semanticDiff} diffLoading={semanticDiffLoading} locale={activeI18n.language === "zh-CN" ? "zh-CN" : "en-US"} onSave={saveCube} onCreate={createCubeDraft} onDelete={deleteCubeDraft} onRetry={() => void loadCubes()} onOpenSource={(path) => { setSelectedFilePath(path); void loadProjectFile(path); }} onLoadDiff={(path) => void loadSemanticDiff(path)} /> : null}
          {section === "rules" ? <RuleWorkbench rules={knowledgeRules(rulesSnapshot)} selectedRuleId={selectedRuleId} loading={rulesLoading} locale={activeI18n.language === "zh-CN" ? "zh-CN" : "en-US"} source={selectedFilePath ? { path: selectedFilePath, content: mdlSource, diff: semanticDiff?.diff } : null} onRetry={() => void loadRules()} onSelectRule={(rule) => { setSelectedRuleId(rule.id); setSelectedFilePath(rule.sourcePath); void loadProjectFile(rule.sourcePath); void loadSemanticDiff(rule.sourcePath); }} onToggleRule={toggleRule} onSave={saveRule} onCreate={createRule} onDelete={deleteRule} /> : null}
          {section === "sqlKnowledge" ? <SqlKnowledgeWorkbench candidates={knowledgeCandidates(sqlCandidates)} loading={sqlLoading} locale={activeI18n.language === "zh-CN" ? "zh-CN" : "en-US"} onRetry={() => void loadSqlCandidates()} onValidate={validateCandidate} onApprove={approveCandidate} onReject={rejectCandidate} /> : null}
          {section === "instructions" ? <InstructionsPage value={instructions} onChange={setInstructions} onSave={() => { const path = files.find((file) => isInstructionFile(file.path))?.path; if (path) void handleSaveFile(path, instructions); else setNotice({ tone: "error", title: "Draft save failed", body: "The project API did not return an instructions file." }); }} savedAt={draftSavedAt} loading={fileLoading} /> : null}
          {section === "mdl" ? <MdlPage value={mdlSource} onChange={setMdlSource} files={files} selectedFile={selectedFilePath} onSelectFile={(path: string) => void loadProjectFile(path)} onSave={(path?: string) => void handleSaveFile(path ?? selectedFilePath, mdlSource)} onImportProject={handleImportProject} savedAt={draftSavedAt} loading={fileLoading} /> : null}
        </>}
      </main>
      <footer className="command-bar"><div className="command-context"><span className="draft-indicator" /><span>{project.status === "published" ? t("common.published") : t("common.draft")}</span>{draftSavedAt ? <span className="saved-time">{t("status.draftSaved")} {formatDate(draftSavedAt, activeI18n.language)}</span> : null}</div><div className="command-actions"><Button variant="ghost" size="sm" icon={ArrowsClockwise} onClick={() => void refreshWorkspace()} loading={refreshing}>{t("common.refresh")}</Button><Button variant="secondary" size="sm" icon={CheckCircle} onClick={handleValidate} loading={busyAction === "validate"}>{t("action.validate")}</Button><Button variant="primary" size="sm" icon={RocketLaunch} onClick={handlePublish} loading={busyAction === "publish"}>{t("action.publish")}</Button></div></footer>
    </div>
    <Modal open={showHistory} title="Version history" description="Restore a previous project snapshot as a draft." onClose={() => setShowHistory(false)} footer={<Button variant="ghost" onClick={() => setShowHistory(false)}>Close</Button>}><VersionHistory versions={versions} onRollback={rollback} busyAction={busyAction} /></Modal>
    <Modal open={showImportModal} title={`Import ${selectedTable}`} description={`${selectedSchema}.${selectedTable} will become a semantic model draft.`} onClose={() => setShowImportModal(false)} footer={<><Button variant="ghost" onClick={() => setShowImportModal(false)}>Cancel</Button><Button variant="primary" icon={DownloadSimple} onClick={importSelectedTable}>Import table</Button></>}><div className="import-preview"><div className="import-preview-row"><span>Source</span><strong>{sourceLabel(selectedDatasource)}</strong></div><div className="import-preview-row"><span>Columns</span><strong>{columns.length} detected</strong></div><div className="import-preview-row"><span>Primary key</span><strong>{columns.find((column) => column.primaryKey)?.name ?? "Not detected"}</strong></div><InlineNotice tone="info" title="Review after import">The generated model uses source column names and keeps measures empty until you define them.</InlineNotice></div></Modal>
  </div>;
}

function pageTitle(section: ConsoleSection, t: (key: string) => string) { return t(({ overview: "page.overview", datasources: "page.datasources", schema: "page.schema", models: "page.models", relationships: "page.relationships", views: "page.views", cubes: "page.cubes", rules: "page.rules", sqlKnowledge: "page.sqlKnowledge", instructions: "page.instructions", mdl: "page.mdl" })[section]); }

function Sidebar({ projectName, section, onNavigate, open, onClose }: { projectName: string; section: ConsoleSection; onNavigate: (section: ConsoleSection) => void; open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  return <aside className={`sidebar ${open ? "sidebar-open" : ""}`}><div className="brand"><span className="brand-mark"><Stack size={19} weight="bold" /></span><span><strong>{t("brand.name")}</strong><small>{t("brand.subtitle")}</small></span><button className="icon-button sidebar-close" onClick={onClose} aria-label={t("nav.close")}><X size={17} /></button></div><div className="project-switcher"><span className="project-icon"><BracketsCurly size={16} /></span><span><small>{t("nav.project")}</small><strong>{projectName}</strong></span><CaretDown size={14} /></div><nav aria-label={t("nav.workspace")}>{navGroups.map((group) => <div className="nav-group" key={group.labelKey}><p className="nav-label">{t(group.labelKey)}</p>{group.items.map((item) => <button aria-label={t(item.labelKey)} className={`nav-item ${section === item.id ? "nav-item-active" : ""}`} key={item.id} onClick={() => onNavigate(item.id)}><item.icon size={18} weight={section === item.id ? "fill" : "regular"} /><span>{t(item.labelKey)}</span>{item.count ? <span className="nav-count">{item.count}</span> : null}</button>)}</div>)}</nav><div className="sidebar-bottom"><button className="nav-item"><GearSix size={18} /><span>{t("nav.settings")}</span></button><div className="sidebar-help"><Info size={16} /><span><strong>{t("nav.helpTitle")}</strong><small>{t("nav.helpBody")}</small></span><ArrowRight size={14} /></div></div></aside>;
}

function LoadingWorkspace() {
  return <div className="loading-workspace"><span className="skeleton skeleton-title" /><span className="skeleton skeleton-subtitle" /><div className="loading-grid"><span className="skeleton skeleton-card" /><span className="skeleton skeleton-card" /><span className="skeleton skeleton-card" /></div><span className="skeleton skeleton-panel" /></div>;
}

function Overview({ project, datasources, versions, files, onNavigate, onHistory }: { project: ProjectSummary; datasources: Datasource[]; versions: VersionRecord[]; files: ProjectFile[]; onNavigate: (section: ConsoleSection) => void; onHistory: () => void }) {
  const projectName = project.name || project.projectName || "Semantic project";
  const modelCount = project.modelCount ?? files.filter((file) => /^models\/[^/]+\/metadata\.ya?ml$/i.test(file.path)).length;
  const relationshipCount = project.relationshipCount ?? files.filter((file) => /relationship/i.test(file.path)).length;
  const viewCount = project.viewCount ?? files.filter((file) => /view/i.test(file.path)).length;
  const fileCount = project.fileCount ?? files.length;
  const testedSources = datasources.filter((source) => source.lastTest?.ok).length;
  const projectLoaded = Object.keys(project).length > 0;
  return <div className="page page-overview"><SectionHeading eyebrow="Workspace" title={projectName} description="Keep the business language close to the data, then publish it as one reliable semantic layer." action={<div className="heading-actions"><Button variant="secondary" icon={UploadSimple} onClick={() => onNavigate("schema")}>Import schema</Button><Button variant="primary" icon={Plus} onClick={() => onNavigate("datasources")}>Add data source</Button></div>} /><div className="overview-grid"><section className="panel health-panel"><div className="panel-header"><div><p className="panel-kicker">Project health</p><h2>{projectLoaded ? "Awaiting validation" : "Not available"}</h2></div><Badge tone="neutral">Not verified</Badge></div><div className="health-score"><span className="health-ring"><WarningCircle size={28} /></span><div><strong>--</strong><span> score</span><p>Run Validate to check the current project.</p></div></div><div className="health-list"><div><WarningCircle size={16} /><span>{testedSources} data source{testedSources === 1 ? "" : "s"} tested</span><small>{testedSources ? "API result" : "Not verified"}</small></div><div><Code size={16} /><span>{modelCount} model file{modelCount === 1 ? "" : "s"} indexed</span><small>{fileCount ? "Project files" : "No files"}</small></div><div><GitBranch size={16} /><span>{versions.length} version{versions.length === 1 ? "" : "s"} available</span><small>{versions.length ? "Project history" : "No history"}</small></div></div></section><section className="panel metrics-panel"><div className="panel-header"><div><p className="panel-kicker">Semantic layer</p><h2>Coverage at a glance</h2></div><Button variant="ghost" size="sm" onClick={() => onNavigate("models")}>View models <ArrowRight size={15} /></Button></div><div className="metric-grid"><Metric label="Models" value={modelCount} detail={modelCount ? "Indexed from files" : "No model files"} icon={Cube} /><Metric label="Relationships" value={relationshipCount} detail={relationshipCount ? "Indexed from files" : "No relationship files"} icon={ShareNetwork} /><Metric label="Views" value={viewCount} detail={viewCount ? "Indexed from files" : "No view files"} icon={Eye} /><Metric label="Project files" value={fileCount} detail={fileCount ? "Loaded from API" : "No files"} icon={Code} /></div></section></div><div className="overview-lower"><section className="panel source-panel"><div className="panel-header"><div><p className="panel-kicker">Connections</p><h2>Data sources</h2></div><Button variant="ghost" size="sm" onClick={() => onNavigate("datasources")}>Manage <ArrowRight size={15} /></Button></div>{datasources.length ? <div className="source-list">{datasources.slice(0, 3).map((source) => <button className="source-row" key={source.id} onClick={() => onNavigate("datasources")}><span className="source-logo"><Database size={17} /></span><span className="source-info"><strong>{source.name}</strong><small>{source.type === "postgres" ? "PostgreSQL" : source.type} <span className="source-separator">/</span> {String(source.connection?.database ?? "Database not set")}</small></span><Badge tone={source.lastTest?.ok ? "green" : "amber"} dot>{source.lastTest?.ok ? "Connected" : "Needs test"}</Badge><ArrowRight className="row-arrow" size={15} /></button>)}</div> : <EmptyState icon={Database} title="No data sources" body="Connect a data source to start browsing metadata." action={<Button variant="secondary" onClick={() => onNavigate("datasources")}>Manage data sources</Button>} />}</section><section className="panel activity-panel"><div className="panel-header"><div><p className="panel-kicker">Project history</p><h2>Recent activity</h2></div><Button variant="ghost" size="sm" onClick={onHistory}>All versions <ArrowRight size={15} /></Button></div>{versions.length ? <div className="activity-list">{versions.slice(0, 3).map((version, index) => <div className="activity-row" key={version.id}><span className={`activity-icon ${index === 0 ? "activity-current" : ""}`}>{index === 0 ? <Check size={15} /> : <GitBranch size={15} />}</span><span><strong>{version.label ?? `Revision ${version.revision}`}</strong><small>{version.revision} <span className="source-separator">/</span> {formatDate(version.createdAt)}</small></span><span className="activity-files">{version.fileCount ?? fileCount} files</span></div>)}</div> : <EmptyState icon={ClockCounterClockwise} title="No versions yet" body="Published revisions will appear here after the project API creates one." />}</section></div><div className="overview-footer"><div><span className="footer-icon"><FloppyDisk size={16} /></span><span><strong>Last saved {formatDate(project.updatedAt)}</strong><small>Current revision {project.revision ?? "Not available"}</small></span></div><Button variant="secondary" size="sm" onClick={onHistory} icon={ClockCounterClockwise}>Review history</Button></div></div>;
}

function Metric({ label, value, detail, icon: Icon }: { label: string; value: number | string; detail: string; icon: typeof Cube }) {
  return <div className="metric"><span className="metric-icon"><Icon size={17} /></span><span className="metric-copy"><strong>{value}</strong><small>{label}</small><em>{detail}</em></span></div>;
}

function DatasourcesPage({ datasources, types, selectedId, activeId, setSelectedId, showForm, setShowForm, onAdd, selected, selectedType, onUpdate, onSave, onTest, onActivate, busyAction }: { datasources: Datasource[]; types: DatasourceType[]; selectedId: string; activeId: string; setSelectedId: (id: string) => void; showForm: boolean; setShowForm: (show: boolean) => void; onAdd: () => void; selected?: Datasource; selectedType?: DatasourceType; onUpdate: (patch: Partial<Datasource>) => void; onSave: () => void; onTest: () => void; onActivate: (id: string) => void; busyAction: string | null }) {
  return <div className="page"><SectionHeading eyebrow="Workspace" title="Data sources" description="Connect the systems that power your semantic models. Credentials stay private to the server." action={<Button variant="primary" icon={Plus} onClick={onAdd}>Add data source</Button>} /><div className="datasources-layout"><section className="panel datasource-list-panel"><div className="list-toolbar"><div><strong>{datasources.length} sources</strong><span>configured for this project</span></div><button className="icon-button" aria-label="Refresh data sources"><ArrowClockwise size={17} /></button></div><div className="datasource-list">{datasources.map((source) => <button className={`datasource-item ${source.id === selectedId ? "datasource-item-active" : ""}`} key={source.id} onClick={() => { setSelectedId(source.id); setShowForm(false); }}><span className="datasource-icon"><Database size={18} /></span><span className="datasource-copy"><strong>{source.name}</strong><small>{source.type === "postgres" ? "PostgreSQL" : source.type} <span>/</span> {String(source.connection?.database ?? "Not configured")}</small></span>{source.id === activeId ? <Badge tone="blue">Current</Badge> : null}<span className={`source-status ${source.lastTest?.ok ? "source-status-ok" : "source-status-pending"}`} aria-label={source.lastTest?.ok ? "Connected" : "Needs test"} /><CaretRight size={15} /></button>)}</div>{datasources.length === 0 ? <EmptyState icon={Database} title="No data sources" body="Connect a warehouse to start building your semantic layer." action={<Button variant="secondary" onClick={onAdd}>Add the first source</Button>} /> : null}</section><section className="panel datasource-detail-panel">{selected && !showForm ? <DatasourceDetail source={selected} active={selected.id === activeId} onEdit={() => setShowForm(true)} onTest={onTest} onActivate={() => onActivate(selected.id)} busyAction={busyAction} /> : selected ? <DatasourceForm source={selected} type={selectedType} types={types} onUpdate={onUpdate} onCancel={() => setShowForm(false)} onSave={onSave} onTest={onTest} busyAction={busyAction} /> : <EmptyState icon={Database} title="Select a data source" body="Choose a connection from the list to inspect its configuration." />}</section></div></div>;
}

function DatasourceDetail({ source, active, onEdit, onTest, onActivate, busyAction }: { source: Datasource; active: boolean; onEdit: () => void; onTest: () => void; onActivate: () => void; busyAction: string | null }) {
  const connectionEntries = Object.entries(source.connection ?? {}).filter(([key]) => key !== "password" && key !== "secret");
  const tested = source.lastTest;
  return <div className="detail-content"><div className="detail-heading"><div className="datasource-hero-icon"><Database size={23} /></div><div><div className="detail-title-line"><h2>{source.name}</h2><Badge tone={tested?.ok ? "green" : "amber"} dot>{tested?.ok ? "Connected" : tested ? "Failed" : "Not tested"}</Badge>{active ? <Badge tone="blue">Current</Badge> : null}</div><p>{source.type === "postgres" ? "PostgreSQL" : source.type} connection</p></div><div className="detail-actions"><Button variant="secondary" size="sm" icon={PencilSimple} onClick={onEdit}>Edit</Button><Button variant="ghost" size="sm" icon={Play} onClick={onTest} loading={busyAction === "test-datasource"}>Test</Button><button className="icon-button" aria-label="More data source actions"><DotsThree size={19} /></button></div></div><div className="connection-summary"><div className="summary-status">{tested?.ok ? <CheckCircle size={18} weight="fill" /> : <WarningCircle size={18} />}<span><strong>{tested?.ok ? "Connection healthy" : tested ? "Connection test failed" : "Connection not tested"}</strong><small>{tested ? `Last tested ${tested.latencyMs ?? "-"} ms response time` : "Run a test to verify this connection."}</small></span></div><div className="summary-security"><span className="security-lock">✓</span><span><strong>Secrets protected</strong><small>Password is never returned by the API</small></span></div></div><div className="details-section"><div className="section-label-row"><h3>Connection details</h3><span>Read only</span></div><dl className="detail-grid">{connectionEntries.map(([key, value]) => <div key={key}><dt>{fieldLabel(key)}</dt><dd>{String(value ?? "Not set")}</dd></div>)}<div><dt>Password</dt><dd className="redacted">•••••••• <span>Stored securely</span></dd></div></dl></div><div className="detail-note"><Info size={17} /><p>Schema browsing uses a read-only connection. We only request metadata and never write to the source.</p></div><div className="current-source-action">{active ? <InlineNotice tone="success" title="This is the current connection">New query sessions will use this data source.</InlineNotice> : <Button variant="secondary" icon={CheckCircle} onClick={onActivate} loading={busyAction === `activate-${source.id}`}>Set as current</Button>}</div></div>;
}

function DatasourceForm({ source, type, types, onUpdate, onCancel, onSave, onTest, busyAction }: { source: Datasource; type?: DatasourceType; types: DatasourceType[]; onUpdate: (patch: Partial<Datasource>) => void; onCancel: () => void; onSave: () => void; onTest: () => void; busyAction: string | null }) {
  const fields = type?.fields ?? [];
  types = types.filter((item) => item.available !== false);
  const connection = source.connection ?? {};
  function updateConnection(name: string, value: string) { onUpdate({ connection: { ...connection, [name]: value } }); }
  function switchType(value: string) { onUpdate({ type: value, connection: {} }); }
  return <div className="detail-content form-content"><div className="detail-heading"><div className="datasource-hero-icon"><Database size={23} /></div><div><h2>{source.id.startsWith("new-") ? "Add data source" : "Edit data source"}</h2><p>Configure a connection profile for schema browsing.</p></div></div><div className="form-grid"><Field label="Display name" required htmlFor="datasource-name"><TextInput id="datasource-name" value={source.name} onChange={(event) => onUpdate({ name: event.target.value })} placeholder="Analytics warehouse" /></Field><Field label="Data source type" required htmlFor="datasource-type"><Select id="datasource-type" value={source.type} onChange={(event) => switchType(event.target.value)}>{types.map((item) => <option value={item.type} key={item.type} disabled={item.available === false}>{item.label}{item.available === false ? " (unavailable)" : ""}</option>)}</Select></Field>{fields.map((field) => <DynamicField field={field} value={connection[field.name]} onChange={(value) => updateConnection(field.name, value)} key={field.name} />)}</div><div className="form-footer"><div><Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button><Button variant="secondary" size="sm" icon={Play} onClick={onTest} loading={busyAction === "test-datasource"}>Test connection</Button></div><Button variant="primary" size="sm" icon={FloppyDisk} onClick={onSave} loading={busyAction === "save-datasource"}>Save data source</Button></div></div>;
}

function DynamicField({ field, value, onChange }: { field: DatasourceField; value: string | number | boolean | null | undefined; onChange: (value: string) => void }) {
  const id = `field-${field.name}`;
  const inputType = field.inputType === "password" || field.sensitive ? "password" : field.inputType === "number" ? "number" : "text";
  return <Field label={field.label ?? field.name} required={field.required} hint={field.hint} htmlFor={id}>{field.inputType === "select" || field.examples?.length ? <Select id={id} value={String(value ?? "")} onChange={(event) => onChange(event.target.value)}><option value="">Choose an option</option>{(field.examples ?? []).map((example) => <option value={example} key={example}>{example}</option>)}</Select> : <TextInput id={id} type={inputType} value={String(value ?? "")} placeholder={field.placeholder} onChange={(event) => onChange(event.target.value)} autoComplete={field.sensitive ? "new-password" : undefined} />}</Field>;
}

function fieldLabel(key: string) { return key.replace(/([A-Z])/g, " $1").replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }

function SchemaPage({ datasources, selectedDatasource, selectedSchema, setSelectedSchema, schemas, tables, search, setSearch, selectedTable, onSelectTable, columns, onImport, busyAction, onRefresh, onDatasourceChange }: { datasources: Datasource[]; selectedDatasource?: Datasource; selectedSchema: string; setSelectedSchema: (value: string) => void; schemas: SchemaRecord[]; tables: TableRecord[]; search: string; setSearch: (value: string) => void; selectedTable: string; onSelectTable: (value: string) => void; columns: ColumnRecord[]; onImport: () => void; busyAction: string | null; onRefresh: () => void; onDatasourceChange: (id: string) => void }) {
  return <div className="page"><SectionHeading eyebrow="Workspace" title="Schema browser" description="Explore source metadata, then import only the tables that belong in your semantic layer." action={<div className="heading-actions"><Button variant="ghost" size="sm" icon={ArrowClockwise} onClick={onRefresh} loading={busyAction === "schema"}>Refresh metadata</Button><Button variant="primary" icon={DownloadSimple} onClick={onImport} disabled={!selectedTable}>Import selected table</Button></div>} /><div className="schema-toolbar"><Field label="Data source" htmlFor="schema-source"><Select id="schema-source" value={selectedDatasource?.id ?? ""} onChange={(event) => onDatasourceChange(event.target.value)}><option value="">Choose a data source</option>{datasources.map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}</Select></Field><div className="schema-toolbar-meta"><span><span className={`source-status ${selectedDatasource ? "source-status-ok" : "source-status-pending"}`} />Metadata is read only</span><button className="icon-button" aria-label="More schema options"><DotsThree size={18} /></button></div></div><div className="schema-browser"><aside className="schema-sidebar"><div className="schema-sidebar-heading"><h3>Schemas</h3><span>{schemas.length}</span></div><div className="schema-tree">{schemas.map((schema) => <button className={`schema-tree-item ${schema.name === selectedSchema ? "schema-tree-item-active" : ""}`} key={schema.name} onClick={() => setSelectedSchema(schema.name)}><CaretRight size={14} /><span className="schema-glyph"><Database size={15} /></span><span>{schema.name}</span><small>{schema.tableCount ?? "-"}</small></button>)}{schemas.length === 0 ? <EmptyState icon={Database} title="No schema metadata" body={selectedDatasource ? "Refresh this connection to load its schemas." : "Choose a data source to begin browsing."} /> : null}</div></aside><section className="table-browser"><div className="table-browser-heading"><div><p className="panel-kicker">{selectedSchema || "Select a schema"}</p><h2>Tables and views</h2></div><div className="table-search"><MagnifyingGlass size={16} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Filter tables" aria-label="Filter tables" /></div></div><div className="table-list">{tables.map((table) => <button className={`table-row ${table.name === selectedTable ? "table-row-active" : ""}`} key={table.name} onClick={() => onSelectTable(table.name)}><span className="table-row-icon">{table.type === "VIEW" ? <Eye size={16} /> : <Table size={16} />}</span><span><strong>{table.name}</strong><small>{table.type === "VIEW" ? "View" : "Table"}</small></span><CaretRight size={15} /></button>)}{tables.length === 0 ? <EmptyState icon={Table} title="No tables found" body={selectedSchema ? "Try a different filter or refresh metadata." : "Choose a schema to load its tables."} /> : null}</div></section><section className="column-browser"><div className="column-browser-heading"><div><p className="panel-kicker">{selectedSchema && selectedTable ? `${selectedSchema}.${selectedTable}` : "Select a table"}</p><h2>Columns</h2></div><Badge tone="neutral">{columns.length} columns</Badge></div><div className="columns-table"><div className="columns-head"><span>Column</span><span>Type</span><span>Nullable</span></div>{busyAction === "columns" ? <LoadingRows count={3} /> : columns.map((column) => <div className="columns-row" key={column.name}><span className="column-name">{column.primaryKey ? <span className="key-marker" title="Primary key">PK</span> : null}{column.name}</span><span className="column-type">{column.type ?? column.dataType ?? "Unknown"}</span><span>{column.nullable ? "Yes" : "No"}</span></div>)}</div><div className="column-footer"><span>Selected source table</span><Button variant="secondary" size="sm" onClick={onImport} icon={DownloadSimple} disabled={!selectedTable}>Import table</Button></div></section></div></div>;
}

function fileName(path: string) {
  return path.split("/").pop() || path;
}

function FileIndexPage({ title, description, files, pattern, icon: Icon, emptyTitle, emptyBody, onOpenFile }: {
  title: string;
  description: string;
  files: ProjectFile[];
  pattern: RegExp;
  icon: typeof Cube;
  emptyTitle: string;
  emptyBody: string;
  onOpenFile: (path: string) => void;
}) {
  const matches = files.filter((file) => pattern.test(file.path));
  return <div className="page"><SectionHeading eyebrow="Semantic layer" title={title} description={description} /><section className="panel table-panel"><div className="list-toolbar"><div><strong>{matches.length} files</strong><span>indexed from the project API</span></div><Badge tone="neutral">Read only</Badge></div>{matches.length === 0 ? <EmptyState icon={Icon} title={emptyTitle} body={emptyBody} /> : <div className="data-table"><div className="data-table-head"><span>Name</span><span>Path</span><span>Size</span><span>Status</span><span aria-hidden="true" /></div>{matches.map((file) => <div className="data-table-row" key={file.path}><span className="name-cell"><span className="row-icon"><Icon size={16} /></span><span><strong>{fileName(file.path)}</strong><small>Source file</small></span></span><span className="muted-code">{file.path}</span><span>{file.size ? `${Math.max(1, Math.round(file.size / 1024))} KB` : "Not available"}</span><span><Badge tone={file.draft ? "amber" : "neutral"} dot>{file.draft ? "Draft" : "Tracked"}</Badge></span><Button variant="ghost" size="sm" onClick={() => onOpenFile(file.path)}>Open source <ArrowRight size={14} /></Button></div>)}</div>}</section><div className="info-strip"><Info size={17} /><span>Structure is indexed from real project files. Use MDL source to edit and save a file through the project API.</span></div></div>;
}

function ModelsPage({ files, onOpenFile, snapshot, loading, sourceContent, sourceLoading, diff, diffLoading, onSave, onOpenSource, onLoadDiff }: { files: ProjectFile[]; onOpenFile: (path: string) => void; snapshot: SemanticProjectSnapshot | null; loading: boolean; sourceContent: string; sourceLoading: boolean; diff: ProjectDiff | null; diffLoading: boolean; onSave: (model: SemanticModel) => Promise<void>; onOpenSource: (path: string) => void; onLoadDiff: (path: string) => void }) {
  if (loading && !snapshot) return <div className="page"><LoadingRows count={8} /></div>;
  if (!snapshot) return <FileIndexPage title="Models" description="Inspect model metadata files generated from the semantic project." files={files} pattern={/^models\/[^/]+\/metadata\.ya?ml$/i} icon={Cube} emptyTitle="No model files" emptyBody="Import a table from Schema browser or import an existing project to create model files." onOpenFile={onOpenFile} />;
  return <ModelEditor snapshot={snapshot} sourceContent={sourceContent} sourceLoading={sourceLoading} diff={diff} diffLoading={diffLoading} onSave={onSave} onOpenSource={onOpenSource} onLoadDiff={onLoadDiff} />;
}

function RelationshipsPage({ snapshot, loading, locale, theme, saving, sourceContent, sourceLoading, diff, diffLoading, onOpenSource, onLoadDiff, onOpenEditor, onSave }: { snapshot: SemanticProjectSnapshot | null; loading: boolean; locale: string; theme: Theme; saving: boolean; sourceContent: string; sourceLoading: boolean; diff: ProjectDiff | null; diffLoading: boolean; onOpenSource: (path: string) => void; onLoadDiff: (path: string) => void; onOpenEditor: (path: string) => void; onSave: (relationships: RelationshipGraphRelationship[]) => Promise<void> }) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<"graph" | "source" | "diff">("graph");
  const sourcePath = snapshot?.sourceFiles.find((file) => /relationships?\.ya?ml$/i.test(file.path))?.path ?? "relationships.yml";
  useEffect(() => {
    if (snapshot) onOpenSource(sourcePath);
    // Reload when the semantic revision changes; the callback identity belongs
    // to the parent render and is intentionally not a trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot?.revision, sourcePath]);
  const models = (snapshot?.models ?? []).map((model) => ({
    name: model.name,
    displayName: model.displayName,
    table: model.tableReference.table,
    schema: model.tableReference.schema,
    primaryKey: model.primaryKey,
    columns: model.columns,
  }));
  function selectTab(tab: "graph" | "source" | "diff") {
    setActiveTab(tab);
    if (tab === "source") onOpenSource(sourcePath);
    if (tab === "diff") onLoadDiff(sourcePath);
  }
  return <div className="page relationship-page">
    <div className="relationship-page-heading"><div><p className="eyebrow">{t("model.eyebrow")}</p><h1>{t("relationship.title")}</h1><p>{t("relationship.description")}</p></div><div className="relationship-page-meta"><Badge tone="neutral">{t("relationship.modelCount", { count: models.length })}</Badge><Badge tone="neutral">{t("relationship.relationCount", { count: snapshot?.relationships.length ?? 0 })}</Badge></div></div>
    <section className="relationship-workbench">
      <div className="relationship-page-tabs" role="tablist" aria-label={t("relationship.views")}>
        <button id="relationship-tab-graph" type="button" role="tab" aria-controls="relationship-panel" aria-selected={activeTab === "graph"} className={activeTab === "graph" ? "relationship-page-tab-active" : ""} onClick={() => selectTab("graph")}><ShareNetwork size={15} />{t("relationship.graphTab")}</button>
        <button id="relationship-tab-source" type="button" role="tab" aria-controls="relationship-panel" aria-selected={activeTab === "source"} className={activeTab === "source" ? "relationship-page-tab-active" : ""} onClick={() => selectTab("source")}><Code size={15} />{t("relationship.sourceTab")}</button>
        <button id="relationship-tab-diff" type="button" role="tab" aria-controls="relationship-panel" aria-selected={activeTab === "diff"} className={activeTab === "diff" ? "relationship-page-tab-active" : ""} onClick={() => selectTab("diff")}><BracketsCurly size={15} />{t("relationship.diffTab")}</button>
      </div>
      <div id="relationship-panel" className="relationship-page-tab-panel" role="tabpanel" aria-labelledby={`relationship-tab-${activeTab}`}>
        {activeTab === "graph" ? <RelationshipGraph showHeading={false} models={models} relationships={snapshot?.relationships ?? []} locale={locale} theme={theme} loading={loading} isSaving={saving} onSave={(_relationship, relationships) => onSave(relationships)} onDelete={(_relationship, relationships) => onSave(relationships)} /> : null}
        {activeTab === "source" ? <div className="relationship-source-view"><div className="relationship-source-toolbar"><span><strong>{t("relationshipSource.file")}</strong><code>{sourcePath}</code></span><Button variant="ghost" size="sm" icon={Code} onClick={() => onOpenEditor(sourcePath)}>{t("relationshipSource.openEditor")}</Button></div><div className="source-panel-hint"><Eye size={14} />{t("relationshipSource.sourceHint")}</div>{sourceLoading ? <div className="source-loading">{t("common.loading")}</div> : <pre className="model-source-code relationship-source-code">{sourceContent || t("relationshipSource.unavailable")}</pre>}</div> : null}
        {activeTab === "diff" ? <div className="relationship-source-view"><div className="relationship-source-toolbar"><span><strong>{t("relationshipSource.diff")}</strong><code>{sourcePath}</code></span></div><div className="source-panel-hint"><Eye size={14} />{t("relationshipSource.diffHint")}</div>{diffLoading ? <div className="source-loading">{t("common.loading")}</div> : diff?.changed ? <pre className="model-source-code model-diff-code relationship-source-code">{diff.diff}</pre> : <div className="model-no-diff"><Check size={18} /><strong>{t("relationshipSource.noDiff")}</strong></div>}</div> : null}
      </div>
    </section>
  </div>;
}

function InstructionsPage({ value, onChange, onSave, savedAt, loading }: { value: string; onChange: (value: string) => void; onSave: () => void; savedAt: string | null; loading: boolean }) {
  const hasContent = Boolean(value);
  return <div className="page"><SectionHeading eyebrow="Semantic layer" title="Instructions" description="Set clear guidance for how the query engine should interpret business terms and choose semantic fields." action={<Button variant="primary" icon={FloppyDisk} onClick={onSave} loading={loading} disabled={!hasContent}>Save instructions</Button>} /><div className="editor-layout"><section className="panel editor-panel"><div className="editor-toolbar"><div><span className="editor-file-icon"><BookOpenText size={16} /></span><strong>Instructions file</strong><Badge tone="blue">Markdown/YAML</Badge></div><span className="editor-save-state">{savedAt ? `Saved ${formatDate(savedAt)}` : "Unsaved changes"}</span></div>{loading ? <LoadingRows count={5} /> : hasContent ? <TextArea className="code-area prose-area" value={value} onChange={(event) => onChange(event.target.value)} spellCheck={false} aria-label="Semantic layer instructions" /> : <EmptyState icon={BookOpenText} title="No instruction file" body="The project API did not return an instructions file." />}</section><aside className="panel guide-panel"><div className="guide-header"><Info size={18} /><h2>Instruction guide</h2></div><p>Keep guidance specific and testable. Good instructions describe intent, priority, and the expected fallback.</p><div className="guide-example"><span>Example</span><p>When a question asks for revenue, use <code>Model.measure</code> and state the filters that apply.</p></div></aside></div></div>;
}

function MdlPage({ value, onChange, files, selectedFile, onSelectFile, onSave, onImportProject, savedAt, loading }: { value: string; onChange: (value: string) => void; files: ProjectFile[]; selectedFile: string; onSelectFile: (path: string) => void; onSave: (path?: string) => void; onImportProject: (files: FileList | null) => void; savedAt: string | null; loading: boolean }) {
  const mdlFiles = files.filter((file) => isEditableProjectFile(file.path) && !isInstructionFile(file.path));
  const selected = mdlFiles.find((file) => file.path === selectedFile);
  const canEdit = Boolean(selected && !loading);
  const importInputRef = useRef<HTMLInputElement>(null);
  return <div className="page"><SectionHeading eyebrow="Semantic layer" title="MDL source" description="Inspect and edit the source representation when the visual editors are not enough." action={<div className="heading-actions"><Badge tone="neutral"><Code size={14} /> Plain text editor</Badge><Button variant="primary" icon={FloppyDisk} onClick={() => onSave(selectedFile)} loading={loading} disabled={!canEdit}>Save source</Button></div>} /><div className="mdl-layout"><aside className="panel file-panel"><div className="file-panel-heading"><div><p className="panel-kicker">Project files</p><h2>Semantic source</h2></div><Badge tone="neutral">{mdlFiles.length}</Badge></div><div className="file-list">{mdlFiles.map((file) => <button className={`file-item ${file.path === selectedFile ? "file-item-active" : ""}`} key={file.path} onClick={() => onSelectFile(file.path)}><span className="file-icon"><Code size={15} /></span><span><strong>{fileName(file.path)}</strong><small>{file.path}</small></span><span className="file-size">{file.size ? `${Math.max(1, Math.round(file.size / 1024))} KB` : "Not available"}</span></button>)}{mdlFiles.length === 0 ? <EmptyState icon={Code} title="No project files" body="Import a project or generate a model from Schema browser to open source here." /> : null}</div><Button variant="secondary" size="sm" icon={UploadSimple} onClick={() => importInputRef.current?.click()} loading={loading}>Import project</Button><input ref={importInputRef} style={{ display: "none" }} type="file" multiple onChange={(event) => { onImportProject(event.target.files); event.currentTarget.value = ""; }} /></aside><section className="panel mdl-editor-panel"><div className="editor-toolbar"><div><span className="editor-file-icon"><BracketsCurly size={16} /></span><strong>{selectedFile || "No file selected"}</strong>{selected ? <Badge tone={selected.draft ? "amber" : "green"}>{selected.draft ? "Draft" : "Tracked"}</Badge> : null}</div><div className="editor-toolbar-actions"><span className="editor-save-state">{savedAt ? `Saved ${formatDate(savedAt)}` : canEdit ? "Unsaved changes" : "No file loaded"}</span><button className="icon-button" aria-label="Download source" disabled={!canEdit}><DownloadSimple size={17} /></button></div></div><div className="editor-notice"><Info size={15} /><span>Monaco is intentionally kept behind this editor boundary. This lightweight fallback preserves keyboard editing and uses the same project file API.</span></div>{loading ? <LoadingRows count={8} /> : selected ? <><div className="code-editor"><div className="line-numbers" aria-hidden="true">{value.split("\n").map((_, index) => <span key={index}>{index + 1}</span>)}</div><textarea value={value} onChange={(event) => onChange(event.target.value)} spellCheck={false} aria-label="MDL source editor" /></div><div className="editor-statusbar"><span><span className="statusbar-dot" />Source file</span><span>UTF-8</span><span>Ln 1, Col 1</span></div></> : <EmptyState icon={Code} title="No file selected" body="Choose a project file to load its current contents from the API." />}</section></div></div>;
}

function VersionHistory({ versions, onRollback, busyAction }: { versions: VersionRecord[]; onRollback: (version: VersionRecord) => void; busyAction: string | null }) {
  return <div className="version-list">{versions.map((version, index) => <div className="version-row" key={version.id}><span className={`version-marker ${index === 0 ? "version-marker-current" : ""}`}>{index === 0 ? <Check size={13} /> : <GitBranch size={13} />}</span><span className="version-copy"><strong>{version.label ?? version.revision}</strong><small>{version.revision} <span>/</span> {formatDate(version.createdAt)} <span>/</span> {version.fileCount ?? 0} files</small></span>{index === 0 ? <Badge tone="blue">Current</Badge> : <Button variant="ghost" size="sm" onClick={() => onRollback(version)} loading={busyAction === `rollback-${version.id}`}>Restore</Button>}</div>)}</div>;
}


export default App;
