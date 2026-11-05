import { cloneElement, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { EvaluationClient } from "../api/evaluations";
import { api, ApiClientError } from "../api/client";
import type { AgentProfile, EvaluationAttempt, EvaluationComparison, EvaluationDataset, EvaluationRun, EvaluationSettings, EvaluationSuite, EvaluationWorker } from "../api/evaluations";
import { Badge, Button, EmptyState, Field as BaseField, InlineNotice, SectionHeading, Select, TextArea, TextInput } from "./ui";
import "./evaluation-workbench.css";

import type { ReactElement, ReactNode } from "react";

function Field({ label, children, hint, required }: { label: ReactNode; children: ReactElement<{ id?: string }>; hint?: ReactNode; required?: boolean }) { const id = useId(); return <BaseField label={label} htmlFor={id} hint={hint} required={required}>{cloneElement(children, { id })}</BaseField>; }

type Locale = "en-US" | "zh-CN";
const newSuite = (): EvaluationSuite => ({ name: "", testDatasetId: "", cases: [] });
const newProfile = (): AgentProfile => ({ name: "", model: "", reasoningEffort: "medium", prompt: "Use SemaRail MCP tools to answer the question. Ask for clarification when required. Never guess results.", contextVersion: 1, timeoutSeconds: 300, concurrency: 2, rounds: 1 });
const json = (value: unknown) => JSON.stringify(value, null, 2);
const active = (status: string) => ["queued", "running", "cancelling"].includes(status);
function download(value: unknown, name: string) {
  const url = URL.createObjectURL(new Blob([json(value)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url);
}

/** Manage immutable evaluation inputs, real Agent runs and separate human reviews. */
export default function EvaluationWorkbench({ locale, adminToken, onOpenTrace, onOpenCore }: {
  locale: Locale; adminToken: string; onOpenTrace?: (id: string) => void; onOpenCore?: (id: string) => void;
}) {
  const zh = locale === "zh-CN";
  const t = useCallback((en: string, cn: string) => zh ? cn : en, [zh]);
  const client = useMemo(() => new EvaluationClient(adminToken), [adminToken]);
  const [tab, setTab] = useState("runs");
  const [settings, setSettings] = useState<EvaluationSettings>({ enabled: false, testDataOnly: true });
  const [suites, setSuites] = useState<EvaluationSuite[]>([]);
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [runs, setRuns] = useState<EvaluationRun[]>([]);
  const [datasets, setDatasets] = useState<EvaluationDataset[]>([]);
  const [scorerDatasetId, setScorerDatasetId] = useState("");
  const [scorerSubject, setScorerSubject] = useState("");
  const [workers, setWorkers] = useState<EvaluationWorker[]>([]);
  const [suite, setSuite] = useState<EvaluationSuite>(newSuite);
  const [profile, setProfile] = useState<AgentProfile>(newProfile);
  const [suiteId, setSuiteId] = useState(""); const [profileId, setProfileId] = useState("");
  const [run, setRun] = useState<EvaluationRun | null>(null);
  const [attempt, setAttempt] = useState<EvaluationAttempt | null>(null);
  const [evidence, setEvidence] = useState<unknown>(null);
  const [coreDetail, setCoreDetail] = useState<unknown>(null);
  const runSelection = useRef(0); const attemptSelection = useRef(0);
  const [reviewStatus, setReviewStatus] = useState("pending"); const [reviewReason, setReviewReason] = useState("");
  const [baselineId, setBaselineId] = useState(""); const [candidateId, setCandidateId] = useState("");
  const [comparison, setComparison] = useState<EvaluationComparison | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  const [profileImportText, setProfileImportText] = useState("");
  const [importText, setImportText] = useState(""); const [advanced, setAdvanced] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState(false);
  const [preflight, setPreflight] = useState<{ ready: boolean; reasons: string[]; environment: unknown; total: number; model: string | null } | null>(null);

  const load = useCallback(async () => {
    const results = await Promise.all([client.list<EvaluationSuite>("suites"), client.list<AgentProfile>("profiles"), client.list<EvaluationRun>("runs"), client.list<EvaluationDataset>("datasets"), client.list<EvaluationWorker>("workers"), client.request<EvaluationSettings>("/settings")]);
    setSuites(results[0].items); setProfiles(results[1].items); setRuns(results[2].items); setDatasets(results[3].items); setWorkers(results[4].items); setSettings(results[5]);
  }, [client]);
  const perform = useCallback(async (action: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await action(); } catch (err) { setError(err instanceof Error ? err.message : t("Request failed", "请求失败")); } finally { setBusy(false); }
  }, [t]);
  useEffect(() => { if (adminToken) void perform(load); }, [adminToken, load, perform]);
  useEffect(() => {
    if (!run || !active(run.status)) return;
    const selection = runSelection.current; let cancelled = false;
    const timer = window.setInterval(() => { void client.request<EvaluationRun>(`/runs/${encodeURIComponent(run.id)}`).then(next => { if (!cancelled && selection === runSelection.current) setRun(next); }).catch(err => { if (!cancelled && selection === runSelection.current) setError(String(err)); }); }, 2500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [client, run]);
  const tone = (status: string) => status === "passed" || status === "completed" ? "green" as const : status === "failed" || status === "error" ? "red" as const : active(status) ? "blue" as const : "neutral" as const;
  const status = (value: string) => zh ? ({ passed: "通过", failed: "失败", pending: "待复核", queued: "排队中", running: "运行中", completed: "已完成", cancelled: "已取消", interrupted: "已中断", inconclusive: "无法判定", draft: "草稿", published: "已发布", cancelling: "取消中", claimed: "已领取", timeout: "超时", process_error: "运行错误", unsupported_interaction: "不支持的交互" } as Record<string, string>)[value] ?? value : value;
  const publishedSuite = suites.find(item => item.id === suiteId && item.status === "published");
  const publishedProfile = profiles.find(item => item.id === profileId && item.status === "published");
  const onlineWorkers = workers.filter(item => item.online !== false);
  const availableRoles = new Set(onlineWorkers.flatMap(item => item.roles ?? []));
  const missingRoles = publishedSuite?.cases.map(item => item.role).filter(role => !availableRoles.has(role)) ?? [];
  const runnable = settings.enabled && onlineWorkers.length > 0 && !missingRoles.length && !!publishedSuite && !!publishedProfile;
  const openRun = async (id: string) => {
    const selection = ++runSelection.current; ++attemptSelection.current;
    setAttempt(null); setEvidence(null); setCoreDetail(null);
    const next = await client.request<EvaluationRun>(`/runs/${encodeURIComponent(id)}`);
    if (selection === runSelection.current) setRun(next);
  };
  const inspectAttempt = async (item: EvaluationAttempt) => {
    const selection = ++attemptSelection.current;
    setAttempt(item); setEvidence(null); setCoreDetail(null);
    setReviewStatus(item.review?.status ?? "pending"); setReviewReason(item.review?.reason ?? "");
    try { const next = await client.request(`/attempts/${item.id}/evidence`); if (selection === attemptSelection.current) setEvidence(next); }
    catch (err) { if (!(err instanceof ApiClientError && err.status === 410)) throw err; }
  };
  const saveSuite = async () => {
    const cases = suite.cases.map(item => {
      const text = advanced[item.id];
      if (!text) return item;
      const value = JSON.parse(text) as Record<string, unknown>;
      return { ...item, ...value, id: item.id, question: item.question, role: item.role, expectedBehavior: item.expectedBehavior };
    });
    const id = suite.id;
    const body = { name: suite.name, testDatasetId: suite.testDatasetId, cases };
    const saved = await client.request<EvaluationSuite>(id ? `/suites/${encodeURIComponent(id)}` : "/suites", id ? "PUT" : "POST", body);
    setSuite(saved); setAdvanced({}); await load(); setNotice(t("Draft saved. Review and publish before running.", "草稿已保存，请审核后发布。"));
  };
  const saveProfile = async () => {
    const id = profile.id;
    const body = { name: profile.name, model: profile.model, reasoningEffort: profile.reasoningEffort, prompt: profile.prompt, contextVersion: profile.contextVersion, timeoutSeconds: profile.timeoutSeconds, concurrency: profile.concurrency, rounds: profile.rounds };
    setProfile(await client.request(id ? `/profiles/${encodeURIComponent(id)}` : "/profiles", id ? "PUT" : "POST", body)); await load();
  };
  const detail = evidence && typeof evidence === "object" ? ((evidence as { evidence?: Record<string, unknown> }).evidence ?? evidence) as Record<string, unknown> : null;
  const observedToolCalls = Array.isArray(detail?.toolCalls) ? detail.toolCalls.length : null;
  const scorerDataset = datasets.find(item => item.id === scorerDatasetId);
  if (!adminToken) return <EmptyState title={t("Console administrator authentication required", "需要 Console 管理员认证")} body={t("Unlock the current project to manage evaluations.", "请解锁当前项目后管理评测。")} />;

  return <div className="evaluation-workbench">
    <SectionHeading eyebrow={t("Quality loop", "质量闭环")} title={t("Evaluation center", "评测中心")} description={t("Run real Agents against versioned test suites and compare changes with a historical baseline.", "用固定题集评测真实 Agent，与历史基线对比修改带来的变化。")} action={<Button disabled={busy} onClick={() => void perform(load)}>{t("Refresh", "刷新")}</Button>} />
    {error && <InlineNotice tone="error" title={t("Request failed", "请求失败")}>{error}</InlineNotice>}
    {notice && <InlineNotice tone="success" title={notice} />}
    <div className="evaluation-toolbar panel">
      <label><input type="checkbox" checked={settings.enabled} disabled={busy} onChange={e => { const enabled = e.target.checked; void perform(async () => { setSettings(await client.request("/settings", "PUT", { enabled, testDataOnly: true })); }); }} /> {t("Enable evaluations for this test project", "为此测试项目启用评测")}</label>
      <span>{t("Registered runners", "已注册运行器")}: {onlineWorkers.length}/{workers.length}</span>
      <small>{t("Detailed test evidence is retained for 30 days. Scores and versions remain available.", "详细测试证据保留 30 天，评分与版本摘要长期保留。")}</small>
    </div>
    <section className="panel evaluation-panel" aria-label={t("Reference scoring identity", "参考查询评分身份")}>
      <h3>{t("Reference scoring identity", "参考查询评分身份")}</h3>
      <p>{t("Register a managed scorer subject for the test dataset before running reference SQL. The runner's host-side scoring credential must belong to this subject and remain separate from test roles.", "运行参考 SQL 前，请为测试数据集登记受管评分主体。运行器主机上的评分凭证必须属于该主体，并与被测角色使用不同身份。")}</p>
      <div className="evaluation-grid">
        <Field label={t("Dataset for reference scoring", "参考查询使用的数据集")}>
          <Select value={scorerDatasetId} disabled={busy} onChange={e => { const id = e.target.value; setScorerDatasetId(id); setScorerSubject(datasets.find(item => item.id === id)?.scorerSubject ?? ""); }}>
            <option value="">{t("Select a registered dataset", "选择已注册的测试数据集")}</option>
            {datasets.map(item => <option key={item.id} value={item.id}>{item.id} · {item.version}</option>)}
          </Select>
        </Field>
        <Field label={t("Managed scorer subject ID", "受管评分主体 ID")} hint={t("Use the managed account's subject ID from Access control.", "填写“访问控制”中受管账号的主体 ID。") }>
          <TextInput value={scorerSubject} disabled={busy || !scorerDatasetId} onChange={e => setScorerSubject(e.target.value)} autoComplete="off" />
        </Field>
      </div>
      {!datasets.length && <p>{t("Register a test dataset in Regression cases first.", "请先在“回归用例”中注册测试数据集。")}</p>}
      {scorerDataset && <p>{t("Registered scorer", "已登记评分主体")}: {scorerDataset.scorerSubject ?? t("Not registered", "未登记")}</p>}
      {scorerDataset?.scorerBindingValid === false && <InlineNotice tone="warning" title={t("Scorer registration is out of date", "评分身份登记已过期")}>{t("Register the scorer again for the dataset's current version and data source.", "请针对数据集的当前版本和数据源重新登记评分主体。")}</InlineNotice>}
      <Button disabled={busy || !scorerDatasetId || !scorerSubject.trim()} onClick={() => void perform(async () => {
        await client.setDatasetScorer(scorerDatasetId, scorerSubject.trim());
        setPreflight(null); setPreview(false); await load(); setNotice(t("Scorer identity registered", "评分主体已登记"));
      })}>{t("Register scorer identity", "登记评分主体")}</Button>
    </section>
    <nav className="evaluation-tabs" aria-label={t("Evaluation sections", "评测栏目")}>
      {[["runs", t("Runs", "评测任务")], ["suites", t("Test suites", "评测集")], ["profiles", t("Agent profiles", "Agent 配置")], ["compare", t("Baseline comparison", "基线对比")]].map(([id, label]) => <Button key={id} variant={tab === id ? "primary" : "secondary"} onClick={() => setTab(id)}>{label}</Button>)}
    </nav>
    {tab === "suites" && <div className="evaluation-columns">
      <section className="panel evaluation-panel"><h3>{t("Test suites", "评测集")}</h3><Button onClick={() => { setSuite(newSuite()); setAdvanced({}); }}>{t("New suite", "新建评测集")}</Button>
        {suites.map(item => <button className="evaluation-list-item" key={item.id} onClick={() => { setSuite(structuredClone(item)); setAdvanced({}); }}><strong>{item.name}</strong><span>v{item.revision} · {status(item.status ?? "draft")} · {item.cases.length} {t("cases", "题")}</span></button>)}
        <details><summary>{t("Import suite", "导入评测集")}</summary><Field label={t("Suite JSON", "评测集 JSON")}><TextArea value={importText} onChange={e => setImportText(e.target.value)} /></Field><input type="file" accept=".json,application/json" aria-label={t("Import JSON file", "导入 JSON 文件")} onChange={e => { const file = e.target.files?.[0]; if (file) void perform(async () => { if (file.size > 2_000_000) throw new Error(t("Import exceeds 2 MB", "导入文件超过 2 MB")); setImportText(await file.text()); }); }} /><Button disabled={busy || !importText} onClick={() => void perform(async () => { const saved = await client.request<EvaluationSuite>("/suites/import", "POST", JSON.parse(importText)); setSuite(saved); setImportText(""); await load(); })}>{t("Import as draft", "导入为草稿")}</Button></details>
      </section>
      <section className="panel evaluation-panel"><h3>{suite.name || t("Suite draft", "评测集草稿")}</h3>
        <Field label={t("Suite name", "评测集名称")} required><TextInput value={suite.name} onChange={e => setSuite({ ...suite, name: e.target.value })} /></Field>
        <Field label={t("Test dataset", "测试数据集")} required><Select value={suite.testDatasetId} onChange={e => setSuite({ ...suite, testDatasetId: e.target.value })}><option value="">{t("Select a registered dataset", "选择已注册的测试数据集")}</option>{datasets.map(item => <option key={item.id} value={item.id}>{item.id} · {item.version}</option>)}</Select></Field>
        {!datasets.length && <p>{t("Register a test dataset in Regression cases first.", "请先在“回归用例”中注册测试数据集。")}</p>}
        {suite.status === "published" && <p>{t("Saving changes creates a new draft revision. Existing runs keep their published snapshot.", "保存修改会创建新的草稿修订版，历史运行保留原发布快照。")}</p>}
        {suite.cases.map((item, index) => <fieldset key={index} className="evaluation-case"><legend>{t("Case", "用例")} {index + 1}</legend>
          <div className="evaluation-grid"><Field label={t("Case ID", "用例 ID")}><TextInput value={item.id} onChange={e => { const next = e.target.value; if (advanced[item.id] !== undefined) { const { [item.id]: text, ...rest } = advanced; setAdvanced({ ...rest, [next]: text }); } setSuite({ ...suite, cases: suite.cases.map((c, i) => i === index ? { ...c, id: next } : c) }); }} /></Field><Field label={t("Test role", "测试角色")}><TextInput value={item.role} onChange={e => setSuite({ ...suite, cases: suite.cases.map((c, i) => i === index ? { ...c, role: e.target.value } : c) })} /></Field></div>
          <Field label={t("Question", "问题")}><TextArea value={item.question} onChange={e => setSuite({ ...suite, cases: suite.cases.map((c, i) => i === index ? { ...c, question: e.target.value } : c) })} /></Field>
          <Field label={t("Expected behavior", "预期行为")}><Select value={item.expectedBehavior} onChange={e => setSuite({ ...suite, cases: suite.cases.map((c, i) => i === index ? { ...c, expectedBehavior: e.target.value as typeof item.expectedBehavior } : c) })}>{[["answer", t("Answer using a query", "查询回答")], ["clarify", t("Clarify first", "先澄清")], ["deny", t("Deny access", "拒绝访问")], ["unsupported", t("Unsupported question", "不支持的问题")]].map(([key, label]) => <option key={key} value={key}>{label}</option>)}</Select></Field>
          <Field label={t("Reference SQL (grader only)", "参考 SQL（仅评分器可见）")}><TextArea value={item.referenceSql ?? ""} onChange={e => setSuite({ ...suite, cases: suite.cases.map((c, i) => i === index ? { ...c, referenceSql: e.target.value || undefined } : c) })} /></Field>
          <Field label={t("Review guidance", "人工复核要点")}><TextInput value={item.reviewGuidance ?? ""} onChange={e => setSuite({ ...suite, cases: suite.cases.map((c, i) => i === index ? { ...c, reviewGuidance: e.target.value } : c) })} /></Field>
          <details><summary>{t("Result assertions, clarification replies and tags", "结果断言、澄清回复和标签")}</summary><Field label={t("Advanced case settings JSON", "用例高级设置 JSON")} hint={t("Use expectedResult, assertions, conditions, clarificationReplies and tags. These are not sent as answers to the Agent.", "可设置 expectedResult、assertions、conditions、clarificationReplies 和 tags，预期答案不会提供给 Agent。 ")}><TextArea rows={9} value={advanced[item.id] ?? json({ ...(item.expectedResult ? { expectedResult: item.expectedResult } : {}), assertions: item.assertions ?? {}, conditions: item.conditions ?? {}, clarificationReplies: item.clarificationReplies ?? {}, tags: item.tags ?? [] })} onChange={e => setAdvanced({ ...advanced, [item.id]: e.target.value })} /></Field></details>
          <Button onClick={() => setSuite({ ...suite, cases: suite.cases.filter((_, i) => i !== index) })}>{t("Remove case", "移除用例")}</Button>
        </fieldset>)}
        <div className="evaluation-actions"><Button onClick={() => setSuite({ ...suite, cases: [...suite.cases, { id: `case-${Array.from({ length: suite.cases.length + 1 }, (_, i) => i + 1).find(i => !suite.cases.some(c => c.id === `case-${i}`))}`, question: "", role: "", expectedBehavior: "answer" }] })}>{t("Add case", "添加用例")}</Button><Button disabled={busy || !suite.name || !suite.testDatasetId} onClick={() => void perform(saveSuite)}>{t("Save suite draft", "保存评测集草稿")}</Button>
          <Button disabled={busy || !suite.id || suite.status !== "draft"} onClick={() => void perform(async () => { setSuite(await client.request(`/suites/${suite.id}/publish`, "POST", {})); await load(); })}>{t("Publish saved suite", "发布已保存题集")}</Button>
          <Button disabled={!suite.id || busy} onClick={() => void perform(async () => download(await client.request(`/suites/${suite.id}/export`), "evaluation-suite.json"))}>{t("Export suite", "导出题集")}</Button>
          <Button disabled={!suite.id || busy} onClick={() => void perform(async () => { await client.request(`/suites/${suite.id}`, "DELETE"); setSuite(newSuite()); await load(); })}>{t("Delete suite", "删除题集")}</Button><Button disabled={!suite.id} onClick={() => { setSuite({ ...suite, id: undefined, revision: undefined, status: "draft", name: `${suite.name} (${t("copy", "副本")})` }); }}>{t("Copy suite", "复制题集")}</Button></div>
      </section>
    </div>}
    {tab === "profiles" && <div className="evaluation-columns"><section className="panel evaluation-panel"><h3>{t("Agent profiles", "Agent 配置")}</h3><Button onClick={() => setProfile(newProfile())}>{t("New profile", "新建配置")}</Button>{profiles.map(item => <button className="evaluation-list-item" key={item.id} onClick={() => setProfile({ ...item })}><strong>{item.name}</strong><span>{item.model} · v{item.revision} · {status(item.status ?? "draft")}</span></button>)}<details><summary>{t("Import Agent profile", "导入 Agent 配置")}</summary><Field label={t("Profile JSON", "配置 JSON")}><TextArea value={profileImportText} onChange={e => setProfileImportText(e.target.value)} /></Field><input type="file" accept=".json,application/json" aria-label={t("Import profile JSON file", "导入配置 JSON 文件")} onChange={e => { const file = e.target.files?.[0]; if (file) void perform(async () => { if (file.size > 2_000_000) throw new Error(t("Import exceeds 2 MB", "导入文件超过 2 MB")); setProfileImportText(await file.text()); }); }} /><Button disabled={busy || !profileImportText} onClick={() => void perform(async () => { setProfile(await client.request<AgentProfile>("/profiles/import", "POST", JSON.parse(profileImportText))); setProfileImportText(""); await load(); })}>{t("Import profile as draft", "导入配置为草稿")}</Button></details></section>
      <section className="panel evaluation-panel"><h3>{t("Profile draft", "配置草稿")}</h3><Field label={t("Profile name", "配置名称")}><TextInput value={profile.name} onChange={e => setProfile({ ...profile, name: e.target.value })} /></Field><Field label={t("Model (explicit selection)", "模型（必须指定）")}><TextInput value={profile.model} onChange={e => setProfile({ ...profile, model: e.target.value })} /></Field>
        <Field label={t("Reasoning effort", "推理强度")}><Select value={profile.reasoningEffort} onChange={e => setProfile({ ...profile, reasoningEffort: e.target.value })}>{["low", "medium", "high", "xhigh", "max"].map(value => <option key={value}>{value}</option>)}</Select></Field><Field label={t("Task instructions", "任务指令")}><TextArea rows={5} value={profile.prompt} onChange={e => setProfile({ ...profile, prompt: e.target.value })} /></Field>
        <div className="evaluation-grid">{([ ["contextVersion", t("Context version", "Context 版本"), 1, 2], ["timeoutSeconds", t("Timeout seconds", "超时秒数"), 1, 1800], ["concurrency", t("Concurrency", "并发数"), 1, 4], ["rounds", t("Rounds", "重复轮数"), 1, 3] ] as const).map(([key, label, min, max]) => <Field key={key} label={label}><TextInput type="number" min={min} max={max} value={profile[key]} onChange={e => setProfile({ ...profile, [key]: Number(e.target.value) })} /></Field>)}</div>
        <div className="evaluation-actions"><Button disabled={busy || !profile.name || !profile.model} onClick={() => void perform(saveProfile)}>{t("Save profile draft", "保存配置草稿")}</Button><Button disabled={busy || !profile.id || profile.status !== "draft"} onClick={() => void perform(async () => { setProfile(await client.request(`/profiles/${profile.id}/publish`, "POST", {})); await load(); })}>{t("Publish saved profile", "发布已保存配置")}</Button><Button disabled={busy || !profile.id} onClick={() => void perform(async () => download(await client.request(`/profiles/${profile.id}/export`), "evaluation-profile.json"))}>{t("Export Agent profile", "导出 Agent 配置")}</Button><Button disabled={busy || !profile.id} onClick={() => void perform(async () => { await client.request(`/profiles/${profile.id}`, "DELETE"); setProfile(newProfile()); await load(); })}>{t("Delete profile", "删除配置")}</Button></div>
      </section></div>}
    {tab === "runs" && <>
      <section className="panel evaluation-panel"><h3>{t("New evaluation", "新建评测")}</h3><div className="evaluation-grid"><Field label={t("Published suite", "已发布评测集")}><Select disabled={busy} value={suiteId} onChange={e => { setSuiteId(e.target.value); setPreview(false); }}><option value="">{t("Select suite", "选择题集")}</option>{suites.filter(item => item.status === "published").map(item => <option key={item.id} value={item.id}>{item.name} · v{item.revision}</option>)}</Select></Field><Field label={t("Published Agent profile", "已发布 Agent 配置")}><Select disabled={busy} value={profileId} onChange={e => { setProfileId(e.target.value); setPreview(false); }}><option value="">{t("Select profile", "选择配置")}</option>{profiles.filter(item => item.status === "published").map(item => <option key={item.id} value={item.id}>{item.name} · {item.model}</option>)}</Select></Field></div>
        {!settings.enabled && <p>{t("Enable this test project before running.", "运行前请启用此测试项目的评测。")}</p>}{!!missingRoles.length && <p>{t("Runner role mapping missing", "运行器缺少角色映射")}: {[...new Set(missingRoles)].join(", ")}</p>}{!onlineWorkers.length && <p>{t("No runner registered. Install and start semarail-eval-runner on the host.", "尚无运行器，请在主机安装并启动 semarail-eval-runner。")}</p>}
        <Button disabled={busy || !runnable} onClick={() => void perform(async () => { setPreflight(await client.request(`/preflight?suiteId=${encodeURIComponent(suiteId)}&profileId=${encodeURIComponent(profileId)}`)); setPreview(true); })}>{t("Preview evaluation", "预览评测任务")}</Button>
        {preview && publishedSuite && publishedProfile && <div className="evaluation-preview"><h4>{t("Review execution plan", "核对运行计划")}</h4><p>{publishedSuite.cases.length} {t("questions", "题")} × {publishedProfile.rounds} {t("rounds", "轮")} = <strong>{publishedSuite.cases.length * publishedProfile.rounds}</strong> {t("attempts", "次执行")}</p><p>{t("Dataset", "数据集")}: {publishedSuite.testDatasetId} · {t("Model", "模型")}: {publishedProfile.model} · {t("Concurrency", "并发")}: {publishedProfile.concurrency} · {publishedProfile.timeoutSeconds}s</p><p>{t("The server verifies and freezes the current published semantic version, dataset and permissions before dispatch.", "服务器在派发前校验并固定当前已发布语义版本、测试数据和权限。")}</p><details><summary>{t("Environment before execution", "执行前环境")}</summary><pre>{json(preflight?.environment)}</pre></details>{preflight?.reasons.map(reason => <p key={reason}>{reason}</p>)}<Button disabled={busy || !runnable || !preflight?.ready} onClick={() => void perform(async () => { const next = await client.request<EvaluationRun>("/runs", "POST", { suiteId, profileId }); setRun(next); setPreview(false); await load(); })}>{t("Run evaluation", "开始评测")}</Button></div>}
      </section>
      <section className="panel evaluation-panel"><h3>{t("Run history", "运行历史")}</h3>{!runs.length && <p>{t("No evaluations yet.", "暂无评测任务。")}</p>}{runs.map(item => <button key={item.id} className="evaluation-list-item" onClick={() => void perform(() => openRun(item.id))}><strong>{item.id}</strong><span>{status(item.status)} · {item.createdAt} · {item.summary?.passed ?? 0}/{item.summary?.total ?? 0}</span></button>)}</section>
      {run && <section className="panel evaluation-panel"><h3>{t("Run detail", "任务详情")} <Badge tone={tone(run.status)}>{status(run.status)}</Badge></h3><code>{run.id}</code><div className="evaluation-metrics">{["total", "started", "passed", "failed", "inconclusive", "errors", "cancelled", "passRate"].map(key => <div key={key}><span>{t(key, ({ total: "计划总数", started: "已执行", passed: "通过", failed: "失败", inconclusive: "无法判定", errors: "运行异常", cancelled: "已取消", passRate: "通过率" } as Record<string, string>)[key])}</span><strong>{key === "passRate" && run.summary?.[key] != null ? `${(run.summary[key] * 100).toFixed(1)}%` : run.summary?.[key] ?? "—"}</strong></div>)}</div><p>{t("Pass rate includes every planned attempt. Human review is separate from automatic scores.", "通过率以全部计划执行次数为分母，人工复核与自动评分分别保留。")}</p>
        <details><summary>{t("Frozen environment", "固定的运行环境")}</summary><pre>{json(run.environment)}</pre></details><details><summary>{t("Frozen Agent configuration and test cases", "固定的 Agent 配置和用例")}</summary><p>{t("Suite revision", "题集版本")}: {run.suiteRevision ?? "—"} · {t("Profile revision", "配置版本")}: {run.profileRevision ?? "—"}</p><pre>{json({ agentConfig: run.agentConfig, cases: run.cases })}</pre></details><p>{t("Not started", "未开始执行")}: {Math.max(0, (run.summary.total ?? 0) - (run.summary.started ?? 0))}</p><div className="evaluation-actions"><Button disabled={busy} onClick={() => void perform(() => openRun(run.id))}>{t("Refresh run", "刷新任务")}</Button><Button disabled={busy || !active(run.status)} onClick={() => void perform(async () => { await client.request(`/runs/${run.id}/cancel`, "POST", {}); await openRun(run.id); })}>{t("Cancel run", "取消任务")}</Button><Button disabled={busy || run.status !== "completed"} onClick={() => void perform(async () => { await client.request(`/runs/${run.id}/baseline`, "POST", {}); setBaselineId(run.id); setNotice(t("Baseline saved", "基线已设置")); await load(); })}>{t("Set baseline", "设为基线")}</Button><Button onClick={() => download(run, `${run.id}.json`)}>{t("Export report", "导出报告")}</Button><Button disabled={busy || active(run.status) || !run.attempts?.some(item => item.score?.status === "failed" || ["failed", "error", "process_error", "timeout", "interrupted"].includes(item.status))} onClick={() => void perform(async () => { const ids = [...new Set(run.attempts?.filter(item => item.score?.status === "failed" || ["failed", "error", "process_error", "timeout", "interrupted"].includes(item.status)).map(item => item.caseId))]; const next = await client.request<EvaluationRun>(`/runs/${run.id}/rerun`, "POST", { caseIds: ids }); ++runSelection.current; ++attemptSelection.current; setAttempt(null); setEvidence(null); setCoreDetail(null); setRun(next); await load(); })}>{t("Rerun failed cases as new run", "失败题另建任务重跑")}</Button></div>
        <div className="evaluation-table"><table><thead><tr>{[t("Case", "用例"), t("Round", "轮次"), t("Execution", "执行状态"), t("Automatic score", "自动评分"), t("Human review", "人工复核"), t("Duration", "耗时"), t("Evidence", "证据")].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{run.attempts?.map(item => <tr key={item.id}><td>{item.caseId}<small className="evaluation-question">{run.cases?.find(c => c.id === item.caseId)?.question}</small></td><td>{item.round}</td><td>{status(item.status)}</td><td>{status(String(item.score?.status ?? "inconclusive"))}</td><td>{status(item.review?.status ?? "pending")}</td><td>{item.durationMs == null ? t("Not collected", "未采集") : `${item.durationMs} ms`}</td><td><Button onClick={() => void perform(async () => { await inspectAttempt(item); })}>{t("Inspect", "查看")}</Button></td></tr>)}</tbody></table></div>
      </section>}
      {attempt && <section className="panel evaluation-panel"><h3>{attempt.caseId} · {t("Evidence and review", "证据与复核")}</h3><p>{run?.cases?.find(c => c.id === attempt.caseId)?.question}</p><p>{run?.cases?.find(c => c.id === attempt.caseId)?.reviewGuidance}</p><dl className="evaluation-dimensions" aria-label={t("Observed execution metrics", "实际采集的执行指标")}><div><dt>{t("Observed tool calls", "实际工具调用次数")}</dt><dd>{observedToolCalls === null ? t("Unavailable", "不可用") : observedToolCalls}</dd></div><div><dt>{t("Observed duration", "实际耗时")}</dt><dd>{attempt.durationMs == null ? t("Not collected", "未采集") : `${attempt.durationMs} ms`}</dd></div><div><dt>{t("Observed token usage", "实际采集的 Token 用量")}</dt><dd>{attempt.observedTokens ? json(attempt.observedTokens) : t("Not collected", "未采集")}</dd></div></dl><dl className="evaluation-dimensions">{Object.entries((attempt.score?.dimensions ?? {}) as Record<string, unknown>).map(([key, value]) => <div key={key}><dt>{t(key, ({ result: "查询结果", firstSql: "首次 SQL", permission: "权限行为", clarification: "澄清", businessConditions: "业务条件", finalAnswer: "最终回答" } as Record<string, string>)[key] ?? key)}</dt><dd><Badge tone={tone(String(value))}>{status(String(value))}</Badge></dd></div>)}</dl>{evidence ? <pre>{json(evidence)}</pre> : <p>{t("Evidence unavailable or expired. Scores remain available.", "详细证据不可用或已过期，评分仍然保留。")}</p>}<div className="evaluation-actions">{attempt.coreTraceIds?.map(id => <Button key={id} onClick={() => void perform(async () => { const selection = attemptSelection.current; const detail = await api.getCoreDiagnostic(id); if (selection === attemptSelection.current) { setCoreDetail(detail); onOpenCore?.(id); } })}>{t("Core diagnostic", "Core 诊断")}: {id}</Button>)}{attempt.agentTraceId ? <Button onClick={() => onOpenTrace?.(attempt.agentTraceId!)}>{t("Agent trace", "Agent 追踪")}</Button> : <span>{t("Full Agent trace not collected", "未采集完整 Agent 追踪")}</span>}<Button disabled={busy || !evidence} onClick={() => void perform(async () => { await client.request(`/attempts/${attempt.id}/evidence`, "DELETE"); setEvidence(null); })}>{t("Delete detailed evidence", "删除详细证据")}</Button></div>
        {attempt.review?.history?.length ? <details><summary>{t("Review history", "复核历史")}</summary><ol>{attempt.review.history.map((entry, index) => <li key={index}>{status(entry.status)} · {entry.reason} · {entry.reviewedBy} · {entry.reviewedAt}</li>)}</ol></details> : null}{coreDetail ? <section aria-label={t("Core diagnostic detail", "Core 诊断详情")}><h4>{t("Core diagnostic detail", "Core 诊断详情")}</h4><pre>{json(coreDetail)}</pre></section> : null}<Field label={t("Human review", "人工复核")}><Select value={reviewStatus} onChange={e => setReviewStatus(e.target.value)}>{["pending", "passed", "failed"].map(value => <option value={value} key={value}>{status(value)}</option>)}</Select></Field><Field label={t("Review reason", "复核理由")}><TextArea value={reviewReason} onChange={e => setReviewReason(e.target.value)} /></Field><Button disabled={busy || !reviewReason.trim()} onClick={() => void perform(async () => { const review = await client.request<NonNullable<EvaluationAttempt["review"]>>(`/attempts/${attempt.id}/review`, "POST", { status: reviewStatus, reason: reviewReason }); setAttempt({ ...attempt, review }); if (run) setRun(await client.request<EvaluationRun>(`/runs/${run.id}`)); setNotice(t("Review saved with history", "复核已保存并保留历史")); })}>{t("Save review", "保存复核")}</Button>
      </section>}
    </>}
    {tab === "compare" && <section className="panel evaluation-panel"><h3>{t("Historical baseline comparison", "历史基线对比")}</h3><div className="evaluation-grid"><Field label={t("Baseline run", "基线任务")}><Select value={baselineId} onChange={e => { setBaselineId(e.target.value); setComparison(null); }}><option value="">{t("Select baseline", "选择基线")}</option>{runs.map(item => <option key={item.id}>{item.id}</option>)}</Select></Field><Field label={t("Candidate run", "候选任务")}><Select value={candidateId} onChange={e => { setCandidateId(e.target.value); setComparison(null); }}><option value="">{t("Select candidate", "选择候选")}</option>{runs.map(item => <option key={item.id}>{item.id}</option>)}</Select></Field></div><Button disabled={busy || !baselineId || !candidateId || baselineId === candidateId} onClick={() => void perform(async () => setComparison(await client.request(`/compare?baselineId=${encodeURIComponent(baselineId)}&candidateId=${encodeURIComponent(candidateId)}`)))}>{t("Compare runs", "对比任务")}</Button>
      {comparison && <><h4>{comparison.strict === true && comparison.mode === "strict" ? t("Strict comparison", "严格对比") : t("Exploratory comparison — conditions differ", "探索性对比：运行条件存在差异")}</h4>{comparison.reasons?.map(reason => <p key={reason}>{reason}</p>)}<details><summary>{t("Changed environment and Agent configuration", "环境及 Agent 配置变化")}</summary><pre>{json(comparison.changes)}</pre></details><div className="evaluation-table"><table><thead><tr>{[t("Case", "用例"), t("Round", "轮次"), t("Change", "变化"), t("Baseline score", "基线评分"), t("Candidate score", "候选评分"), t("Baseline review", "基线复核"), t("Candidate review", "候选复核")].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{comparison.items?.map((item, i) => <tr key={i}><td>{String(item.caseId)}</td><td>{String(item.round)}</td><td>{t(String(item.category), ({ regression: "新增退化", fixed: "修复成功", persistent_failure: "持续失败", stable_pass: "稳定通过", unmatched: "无法比较" } as Record<string, string>)[String(item.category)] ?? String(item.category))}</td><td>{status(String(item.baselineStatus ?? "inconclusive"))}</td><td>{status(String(item.candidateStatus ?? "inconclusive"))}</td><td>{status(String((item.baselineReview as {status?: string} | undefined)?.status ?? "pending"))}</td><td>{status(String((item.candidateReview as {status?: string} | undefined)?.status ?? "pending"))}</td></tr>)}</tbody></table></div><Button onClick={() => download(comparison, "evaluation-comparison.json")}>{t("Export comparison", "导出对比")}</Button></>}
    </section>}
  </div>;
}
