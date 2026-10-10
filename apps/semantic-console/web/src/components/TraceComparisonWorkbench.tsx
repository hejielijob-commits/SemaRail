import { useState } from "react";
import { ArrowRight, GitBranch, MagnifyingGlass } from "@phosphor-icons/react";
import { api } from "../api/client";
import type { AgentTraceDetail, CoreTraceDiagnostic, TraceComparisonNode, TraceComparisonResponse, TraceComparisonUnmatchedNode } from "../types";
import { Badge, Button, EmptyState, Field, InlineNotice, SectionHeading, TextInput } from "./ui";
import "./trace-comparison.css";

type Locale = "en-US" | "zh-CN";

const labels = {
  "en-US": {
    eyebrow: "Quality loop", title: "Trace comparison", description: "Compare two trace IDs in the current project. Matching uses recorded hierarchy and tool order; unmatched events stay visible on each side.",
    locked: "Console administrator authentication required", lockedBody: "Unlock the Console with a project-scoped administrator credential to compare traces.",
    project: "Current project", leftId: "Left trace ID", rightId: "Right trace ID", compare: "Compare traces", required: "Enter two different trace IDs.", requestFailed: "Trace comparison failed", same: "Same", changed: "Changed", aligned: "Matched activity", leftOnly: "Only in left trace", rightOnly: "Only in right trace", noAligned: "No matching activity", noUnmatched: "No unmatched activity", identity: "Trace identity", status: "Status", model: "Model", semanticVersion: "Semantic version", policyVersions: "Policy versions", versionComparison: "Version changes", changedValues: "Updated", unchangedValues: "Unchanged", activity: "Activity", agentPath: "Agent path", tool: "Tool", duration: "Observation interval", durationChange: "Observation interval change", phaseDurations: "Core phase durations", phase: "Phase", left: "Left", right: "Right", delta: "Difference", notCollected: "Not collected", coreEvidence: "Core evidence by trace", noCore: "No Core diagnostic evidence for this trace.", method: "Method", stage: "Stage", code: "Code", reasonCode: "Reason code", errorPhase: "Error phase", failedDetails: "Comparison completed, but Core detail could not be loaded for one or both IDs.", stateChanged: "State changed", unchangedState: "State matched", request: "Compare",
  },
  "zh-CN": {
    eyebrow: "质量闭环", title: "追踪对比", description: "在当前项目中对比两条追踪 ID。匹配依据是已记录的层级和工具顺序；未匹配事件会分别显示。",
    locked: "需要 Console 管理员认证", lockedBody: "请使用当前项目范围内的管理员凭证解锁 Console 后对比追踪记录。",
    project: "当前项目", leftId: "左侧追踪 ID", rightId: "右侧追踪 ID", compare: "对比追踪", required: "请输入两条不同的追踪 ID。", requestFailed: "追踪对比失败", same: "一致", changed: "已变化", aligned: "匹配的活动", leftOnly: "仅左侧追踪有", rightOnly: "仅右侧追踪有", noAligned: "没有匹配的活动", noUnmatched: "没有未匹配的活动", identity: "追踪标识", status: "状态", model: "模型", semanticVersion: "语义版本", policyVersions: "策略版本", versionComparison: "版本变化", changedValues: "已更新", unchangedValues: "一致", activity: "活动", agentPath: "Agent 路径", tool: "工具", duration: "观察间隔", durationChange: "观察间隔变化", phaseDurations: "Core 阶段耗时", phase: "阶段", left: "左侧", right: "右侧", delta: "差值", notCollected: "未采集", coreEvidence: "两条追踪的 Core 证据", noCore: "此追踪没有 Core 诊断证据。", method: "方法", stage: "阶段", code: "代码", reasonCode: "原因代码", errorPhase: "错误阶段", failedDetails: "对比已完成，但无法加载其中一条或两条追踪的 Core 详情。", stateChanged: "状态有变化", unchangedState: "状态一致", request: "开始对比",
  },
} as const;

function formatDuration(value: number | null, locale: Locale) {
  if (value === null || !Number.isFinite(value)) return labels[locale].notCollected;
  return `${value.toFixed(1)} ms`;
}

function formatDelta(value: number | null, locale: Locale) {
  if (value === null || !Number.isFinite(value)) return labels[locale].notCollected;
  return `${value > 0 ? "+" : ""}${value.toFixed(1)} ms`;
}

function statusTone(status: string): "neutral" | "blue" | "green" | "amber" | "red" {
  if (status === "running") return "blue";
  if (status === "success") return "green";
  if (status === "failure") return "red";
  if (status === "cancelled") return "amber";
  return "neutral";
}

function statusText(status: string, locale: Locale) {
  if (locale === "zh-CN") return ({ running: "运行中", success: "成功", failure: "失败", cancelled: "已取消", unknown: "未知" } as Record<string, string>)[status] ?? status;
  return ({ running: "Running", success: "Succeeded", failure: "Failed", cancelled: "Cancelled", unknown: "Unknown" } as Record<string, string>)[status] ?? status;
}

function versionValue(side: TraceComparisonResponse["left"], key: "model" | "semanticVersion" | "policyVersions", locale: Locale) {
  if (key === "policyVersions") return side.policyVersions.join(", ") || labels[locale].notCollected;
  if (key === "semanticVersion") return side.semanticVersions?.join(", ") || side.semanticVersion || labels[locale].notCollected;
  return side.model || labels[locale].notCollected;
}

function SideSummary({ side, label, locale }: { side: TraceComparisonResponse["left"]; label: string; locale: Locale }) {
  const c = labels[locale];
  return <article className="panel trace-compare-side">
    <header><span>{label}</span><Badge tone={statusTone(side.status)} dot>{statusText(side.status, locale)}</Badge></header>
    <code>{side.traceId}</code>
    <dl><div><dt>{c.model}</dt><dd>{side.model ?? c.notCollected}</dd></div><div><dt>{c.semanticVersion}</dt><dd>{versionValue(side, "semanticVersion", locale)}</dd></div><div><dt>{c.policyVersions}</dt><dd>{side.policyVersions.length ? side.policyVersions.join(", ") : c.notCollected}</dd></div></dl>
  </article>;
}

function NodeLabel({ node, locale }: { node: TraceComparisonUnmatchedNode; locale: Locale }) {
  const c = labels[locale];
  const path = node.agentPath.length ? node.agentPath.join(" / ") : c.notCollected;
  const kindLabel = locale === "zh-CN" ? ({ turn: "轮次", agent: "Agent", tool: "工具" } as const)[node.kind] : node.kind;
  return <span className="trace-compare-node-label"><Badge tone="neutral">{kindLabel}</Badge><strong>{node.toolName || node.id}</strong><code>{node.id}</code><small>{c.agentPath}: {path}</small></span>;
}

function ActivityLabel({ kind, toolName, keyValue, agentPath, locale }: { kind: string; toolName: string | null; keyValue: string; agentPath: string[]; locale: Locale }) {
  const c = labels[locale];
  const kindLabel = locale === "zh-CN" ? ({ turn: "轮次", agent: "Agent", tool: "工具" } as Record<string, string>)[kind] ?? kind : kind;
  return <span className="trace-compare-node-label"><Badge tone="neutral">{kindLabel}</Badge><strong>{toolName || keyValue}</strong><small>{c.agentPath}: {agentPath.length ? agentPath.join(" / ") : c.notCollected}</small></span>;
}

function NodeResult({ node, locale }: { node: TraceComparisonNode; locale: Locale }) {
  const c = labels[locale];
  return <span className="trace-compare-node-result"><Badge tone={statusTone(node.status)}>{statusText(node.status, locale)}</Badge><small>{c.duration}: {formatDuration(node.durationMs, locale)}</small></span>;
}

function UnmatchedList({ title, nodes, locale }: { title: string; nodes: TraceComparisonUnmatchedNode[]; locale: Locale }) {
  const c = labels[locale];
  return <section className="trace-compare-unmatched panel">
    <header><h3>{title}</h3><Badge tone={nodes.length ? "amber" : "green"}>{nodes.length}</Badge></header>
    {nodes.length ? <div>{nodes.map((node) => <article key={node.key}><NodeLabel node={node} locale={locale} /><NodeResult node={node} locale={locale} /></article>)}</div> : <p>{c.noUnmatched}</p>}
  </section>;
}

function CoreEvidence({ detail, title, locale }: { detail: AgentTraceDetail | null; title: string; locale: Locale }) {
  const c = labels[locale];
  const diagnostics = detail?.coreDiagnostics ?? [];
  return <section className="trace-compare-core-side">
    <h4>{title}</h4>
    {diagnostics.length ? diagnostics.map((item) => <CoreDiagnostic key={item.traceId} item={item} locale={locale} />) : <p>{c.noCore}</p>}
  </section>;
}

function CoreDiagnostic({ item, locale }: { item: CoreTraceDiagnostic; locale: Locale }) {
  const c = labels[locale];
  return <article className="trace-compare-core-item">
    <header><code>{item.traceId}</code><Badge tone={statusTone(item.status)}>{statusText(item.status, locale)}</Badge></header>
    <dl><div><dt>{c.method}</dt><dd>{item.method}</dd></div><div><dt>{c.stage}</dt><dd>{item.stage}</dd></div><div><dt>{c.semanticVersion}</dt><dd>{item.semanticVersion ?? c.notCollected}</dd></div><div><dt>{c.policyVersions}</dt><dd>{item.policyVersions?.join(", ") || c.notCollected}</dd></div><div><dt>{c.code}</dt><dd>{item.code ?? c.notCollected}</dd></div><div><dt>{c.reasonCode}</dt><dd>{item.reasonCode ?? c.notCollected}</dd></div><div><dt>{c.errorPhase}</dt><dd>{item.phase ?? c.notCollected}</dd></div></dl>
    {item.phaseSpans.length ? <div className="trace-compare-phases">{item.phaseSpans.map((phase, index) => <span key={`${phase.name}:${index}`}><strong>{phase.name}</strong><small>{formatDuration(phase.durationMs, locale)} · {statusText(phase.status, locale)}</small></span>)}</div> : null}
  </article>;
}

function ComparisonResult({ data, leftDetail, rightDetail, detailError, locale }: { data: TraceComparisonResponse; leftDetail: AgentTraceDetail | null; rightDetail: AgentTraceDetail | null; detailError: boolean; locale: Locale }) {
  const c = labels[locale];
  return <div className="trace-compare-results">
    {detailError ? <InlineNotice tone="warning" title={c.failedDetails}>{c.failedDetails}</InlineNotice> : null}
    <section className="trace-compare-sides"><SideSummary side={data.left} label={c.left} locale={locale} /><SideSummary side={data.right} label={c.right} locale={locale} /></section>
    <section className="panel trace-compare-versions"><header><h3>{c.versionComparison}</h3></header><div>{(["model", "semanticVersion", "policyVersions"] as const).map((key) => <article key={key}><strong>{c[key]}</strong><Badge tone={data.versionChanges[key] === null ? "neutral" : data.versionChanges[key] ? "amber" : "green"}>{data.versionChanges[key] === null ? c.notCollected : data.versionChanges[key] ? c.changedValues : c.unchangedValues}</Badge><small>{versionValue(data.left, key, locale)}</small><ArrowRight size={14} /><small>{versionValue(data.right, key, locale)}</small></article>)}</div></section>
    <section className="panel trace-compare-aligned"><header><h3>{c.aligned}</h3><Badge>{data.aligned.length}</Badge></header>
      {data.aligned.length ? <div className="trace-compare-table-wrap"><table><thead><tr><th>{c.activity}</th><th>{c.left}</th><th>{c.right}</th><th>{c.durationChange}</th></tr></thead><tbody>{data.aligned.map((entry) => <tr key={entry.key}>
        <td><ActivityLabel kind={entry.kind} keyValue={entry.key} toolName={entry.toolName} agentPath={entry.agentPath} locale={locale} /><Badge tone={entry.stateChanged ? "amber" : "green"}>{entry.stateChanged ? c.stateChanged : c.unchangedState}</Badge></td>
        <td><NodeResult node={entry.left} locale={locale} /><small className="trace-compare-node-id">{entry.left.id}</small></td>
        <td><NodeResult node={entry.right} locale={locale} /><small className="trace-compare-node-id">{entry.right.id}</small></td>
        <td>{formatDelta(entry.durationDeltaMs, locale)}{entry.phaseDurations.length ? <details><summary>{c.phaseDurations}</summary><div className="trace-compare-phase-deltas">{entry.phaseDurations.map((phase) => <span key={phase.name}><strong>{phase.name}</strong><small>{c.left}: {formatDuration(phase.leftMs, locale)}</small><small>{c.right}: {formatDuration(phase.rightMs, locale)}</small><small>{c.delta}: {formatDelta(phase.deltaMs, locale)}</small></span>)}</div></details> : null}</td>
      </tr>)}</tbody></table></div> : <EmptyState icon={GitBranch} title={c.noAligned} body={c.noAligned} />}
    </section>
    <section className="trace-compare-unmatched-grid"><UnmatchedList title={c.leftOnly} nodes={data.leftOnly} locale={locale} /><UnmatchedList title={c.rightOnly} nodes={data.rightOnly} locale={locale} /></section>
    <section className="panel trace-compare-core"><header><h3>{c.coreEvidence}</h3></header><div><CoreEvidence detail={leftDetail} title={`${c.left} · ${data.left.traceId}`} locale={locale} /><CoreEvidence detail={rightDetail} title={`${c.right} · ${data.right.traceId}`} locale={locale} /></div></section>
  </div>;
}

export default function TraceComparisonWorkbench({ locale, adminToken, projectName }: { locale: Locale; adminToken: string; projectName: string }) {
  const c = labels[locale];
  const [leftTraceId, setLeftTraceId] = useState("");
  const [rightTraceId, setRightTraceId] = useState("");
  const [data, setData] = useState<TraceComparisonResponse | null>(null);
  const [leftDetail, setLeftDetail] = useState<AgentTraceDetail | null>(null);
  const [rightDetail, setRightDetail] = useState<AgentTraceDetail | null>(null);
  const [detailError, setDetailError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function compare() {
    const left = leftTraceId.trim();
    const right = rightTraceId.trim();
    if (!left || !right || left === right) { setError(c.required); return; }
    setLoading(true); setError(""); setData(null); setLeftDetail(null); setRightDetail(null); setDetailError(false);
    try {
      const result = await api.compareTraces(left, right);
      setData(result);
      const [leftResult, rightResult] = await Promise.allSettled([api.getTrace(left), api.getTrace(right)]);
      if (leftResult.status === "fulfilled") setLeftDetail(leftResult.value);
      if (rightResult.status === "fulfilled") setRightDetail(rightResult.value);
      setDetailError(leftResult.status === "rejected" || rightResult.status === "rejected");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : c.requestFailed);
    } finally {
      setLoading(false);
    }
  }

  if (!adminToken) return <div className="page trace-comparison-page"><SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} /><InlineNotice tone="warning" title={c.locked}>{c.lockedBody}</InlineNotice></div>;

  return <div className="page trace-comparison-page">
    <SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} />
    <section className="panel trace-compare-form">
      <p className="panel-kicker">{c.project}</p><strong className="trace-compare-project-name">{projectName || c.project}</strong>
      <div><Field label={c.leftId} htmlFor="trace-compare-left" required><TextInput id="trace-compare-left" autoComplete="off" spellCheck={false} value={leftTraceId} onChange={(event) => setLeftTraceId(event.target.value)} /></Field><Field label={c.rightId} htmlFor="trace-compare-right" required><TextInput id="trace-compare-right" autoComplete="off" spellCheck={false} value={rightTraceId} onChange={(event) => setRightTraceId(event.target.value)} /></Field></div>
      {error ? <InlineNotice tone="error" title={c.requestFailed}>{error}</InlineNotice> : null}
      <Button variant="primary" icon={MagnifyingGlass} loading={loading} disabled={!leftTraceId.trim() || !rightTraceId.trim()} onClick={() => void compare()}>{c.compare}</Button>
    </section>
    {data ? <ComparisonResult data={data} leftDetail={leftDetail} rightDetail={rightDetail} detailError={detailError} locale={locale} /> : <section className="panel trace-compare-placeholder"><EmptyState icon={GitBranch} title={c.title} body={c.description} /></section>}
  </div>;
}
