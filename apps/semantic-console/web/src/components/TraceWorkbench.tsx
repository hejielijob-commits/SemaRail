import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowClockwise, ArrowRight, ClockCounterClockwise, GitBranch, LinkSimple, MagnifyingGlass, Wrench } from "@phosphor-icons/react";
import { api } from "../api/client";
import type { AgentTraceDetail, AgentTraceEvent, AgentTraceSummary, TraceTokenUsage } from "../types";
import { Badge, Button, EmptyState, InlineNotice, LoadingRows, SectionHeading } from "./ui";
import "./trace-workbench.css";

type Locale = "en-US" | "zh-CN";
type TraceState = AgentTraceSummary["status"] | "incomplete" | "unknown" | "completed_unknown";

const labels = {
  "en-US": {
    eyebrow: "Quality loop", title: "Agent traces", description: "Rebuild sessions and tool activity from the durable event stream.",
    locked: "Console administrator authentication required", lockedBody: "Unlock the Console with a project-scoped administrator credential to inspect traces.",
    refresh: "Refresh", loadMore: "Load more", empty: "No traces yet", emptyBody: "Durable agent traces will appear here after an integration records events.",
    details: "Trace details", select: "Select a trace to inspect its event timeline.", requestFailed: "Trace request failed", session: "Session", turn: "Turn", agent: "Agent", subagent: "Subagent", unknownOwner: "Unknown owner", tool: "Tool", core: "Core trace", stage: "Stage", unknown: "Unknown", notCollected: "Not collected", incomplete: "Incomplete", running: "Running", success: "Succeeded", failure: "Failed", cancelled: "Cancelled", completedUnknown: "Completed · outcome unknown", status: "Status", source: "Source", started: "Started", ended: "Ended", duration: "Observed turn duration", observedDuration: "Observed duration", llmDuration: "LLM duration", parallelActivity: "Parallel activity from recorded timestamps", overlap: "overlap", model: "Model", tokens: "Tokens", events: "Events", issues: "Related issues", noIssues: "No linked issues", noEvents: "No events were returned for this trace.", evidence: "Core evidence", event: "Event", toolOwnerUnknown: "Tool ownership was not recorded.", parentUnavailable: "Parent agent event is unavailable.", ownerUnknown: "Ownership unavailable", parentToolUnavailable: "Parent tool event is unavailable.", input: "Input", output: "Output", total: "Total", openIssue: "Open issue", traceId: "Trace", search: "Search traces",
  },
  "zh-CN": {
    eyebrow: "质量闭环", title: "Agent 追踪", description: "根据持久化事件流重建会话与工具活动。",
    locked: "需要 Console 管理员认证", lockedBody: "请使用当前项目范围内的管理员凭证解锁 Console 后查看追踪记录。",
    refresh: "刷新", loadMore: "加载更多", empty: "暂无追踪记录", emptyBody: "集成开始记录持久化事件后，Agent 追踪会显示在此处。",
    details: "追踪详情", select: "选择一条追踪记录以查看事件时间线。", requestFailed: "追踪请求失败", session: "会话", turn: "轮次", agent: "Agent", subagent: "子 Agent", unknownOwner: "归属未知", tool: "工具", core: "Core 追踪", stage: "阶段", unknown: "未知", notCollected: "未采集", incomplete: "未完成", running: "运行中", success: "成功", failure: "失败", cancelled: "已取消", completedUnknown: "已完成 · 结果未知", status: "状态", source: "来源", started: "开始时间", ended: "结束时间", duration: "观测到的轮次耗时", observedDuration: "观测耗时", llmDuration: "LLM 耗时", parallelActivity: "根据已记录时间戳显示并行活动", overlap: "重叠", model: "模型", tokens: "Token 用量", events: "事件", issues: "关联问题", noIssues: "没有关联问题", noEvents: "此追踪记录没有返回事件。", evidence: "Core 证据", event: "事件", toolOwnerUnknown: "未记录工具归属。", parentUnavailable: "父 Agent 事件不可用。", ownerUnknown: "归属不可用", parentToolUnavailable: "父工具事件不可用。", input: "输入", output: "输出", total: "总计", openIssue: "打开问题", traceId: "追踪", search: "搜索追踪记录",
  },
} as const;

function formatTime(value: string | null | undefined, locale: Locale) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function tokenUsageLabel(usage: TraceTokenUsage | null | undefined, locale: Locale) {
  if (!usage) return labels[locale].notCollected;
  const c = labels[locale];
  const values = [[c.input, usage.input], [c.output, usage.output], [c.total, usage.total]] as const;
  const available = values.filter(([, value]) => typeof value === "number").map(([label, value]) => `${label} ${value}`);
  return available.length ? available.join(" · ") : c.notCollected;
}

function stateLabel(state: TraceState, locale: Locale) {
  const c = labels[locale];
  if (state === "running") return c.running;
  if (state === "success") return c.success;
  if (state === "failure") return c.failure;
  if (state === "cancelled") return c.cancelled;
  if (state === "incomplete") return c.incomplete;
  if (state === "completed_unknown") return c.completedUnknown;
  return c.unknown;
}

function stateTone(state: TraceState): "neutral" | "blue" | "green" | "amber" | "red" {
  if (state === "running") return "blue";
  if (state === "success") return "green";
  if (state === "failure") return "red";
  if (state === "cancelled" || state === "incomplete" || state === "completed_unknown") return "amber";
  return "neutral";
}

function isToolEvent(event: AgentTraceEvent) {
  return event.type === "tool_started" || event.type === "tool_completed";
}

function isTurnEvent(event: AgentTraceEvent) {
  return event.type === "turn_started" || event.type === "turn_completed" || event.type === "turn_interrupted";
}

function sortedEvents(events: AgentTraceEvent[]) {
  return [...events].sort((left, right) => {
    const delta = Date.parse(left.occurredAt) - Date.parse(right.occurredAt);
    return (Number.isNaN(delta) ? 0 : delta) || left.eventId.localeCompare(right.eventId);
  });
}

function deriveState(events: AgentTraceEvent[], fallback: TraceState = "unknown"): TraceState {
  const ordered = sortedEvents(events);
  const terminal = [...ordered].reverse().find((event) => event.type === "turn_completed" || event.type === "turn_interrupted" || event.type === "tool_completed" || event.type === "subagent_completed");
  if (terminal?.status === "unknown") return terminal.type === "subagent_completed" ? "completed_unknown" : "unknown";
  if (terminal?.status) return terminal.status;
  if (terminal?.type === "turn_interrupted") return "cancelled";
  if (terminal?.type === "subagent_completed") return "completed_unknown";
  if (terminal?.type === "tool_completed") return "unknown";
  if (terminal) return "success";
  const explicit = [...ordered].reverse().find((event) => event.status && event.status !== "running")?.status;
  if (explicit) return explicit;
  if (ordered.some((event) => event.type === "turn_started" || event.type === "tool_started" || event.type === "subagent_started")) {
    return fallback === "running" ? "running" : "incomplete";
  }
  return fallback;
}

function typeLabel(event: AgentTraceEvent, locale: Locale) {
  const names: Record<AgentTraceEvent["type"], [string, string]> = {
    turn_started: ["Turn started", "轮次开始"], turn_completed: ["Turn completed", "轮次完成"], turn_interrupted: ["Turn interrupted", "轮次中断"],
    tool_started: ["Tool started", "工具开始"], tool_completed: ["Tool completed", "工具完成"],
    subagent_started: ["Subagent started", "子 Agent 开始"], subagent_completed: ["Subagent completed", "子 Agent 完成"], output: ["Agent output", "Agent 输出"],
  };
  return names[event.type][locale === "zh-CN" ? 1 : 0];
}

function eventState(event: AgentTraceEvent): TraceState {
  if (event.status === "unknown") return event.type === "subagent_completed" ? "completed_unknown" : "unknown";
  if (event.status) return event.status;
  if (event.type === "turn_interrupted") return "cancelled";
  if (event.type === "subagent_completed") return "completed_unknown";
  if (event.type === "tool_completed") return "unknown";
  if (event.type === "turn_completed") return "success";
  if (event.type === "turn_started" || event.type === "tool_started" || event.type === "subagent_started") return "running";
  return "unknown";
}

interface AgentGroup {
  id: string;
  parentAgentId?: string;
  events: AgentTraceEvent[];
  children: AgentGroup[];
}

interface ToolGroup {
  id: string;
  parentToolUseId?: string;
  events: AgentTraceEvent[];
  children: ToolGroup[];
}

function groupAgents(events: AgentTraceEvent[]) {
  const groups = new Map<string, AgentGroup>([["__main__", { id: "__main__", events: [], children: [] }]]);
  const turnEvents: AgentTraceEvent[] = [];
  for (const event of events) {
    if (isTurnEvent(event)) {
      turnEvents.push(event);
      continue;
    }
    const id = isToolEvent(event) ? event.parentAgentId ?? "__unknown_owner__" : event.agentId ?? event.parentAgentId ?? (event.type === "output" ? "__main__" : undefined);
    if (!id) {
      turnEvents.push(event);
      continue;
    }
    const group = groups.get(id) ?? { id, events: [], children: [] };
    if (event.parentAgentId && event.parentAgentId !== id) group.parentAgentId = event.parentAgentId;
    group.events.push(event);
    groups.set(id, group);
  }
  for (const group of groups.values()) group.children = [];
  const roots: AgentGroup[] = [];
  for (const group of groups.values()) {
    const parent = group.parentAgentId ? groups.get(group.parentAgentId) : undefined;
    if (parent && parent.id !== group.id) parent.children.push(group);
    else roots.push(group);
  }
  for (const group of groups.values()) {
    group.events = sortedEvents(group.events);
    group.children.sort((left, right) => firstTime(left.events).localeCompare(firstTime(right.events)) || left.id.localeCompare(right.id));
  }
  roots.sort((left, right) => (left.id === "__main__" ? -1 : right.id === "__main__" ? 1 : firstTime(left.events).localeCompare(firstTime(right.events)) || left.id.localeCompare(right.id)));
  return { roots, turnEvents: sortedEvents(turnEvents) };
}

function groupTools(events: AgentTraceEvent[]) {
  const groups = new Map<string, ToolGroup>();
  const ungrouped: AgentTraceEvent[] = [];
  for (const event of events) {
    if (!event.toolUseId) {
      ungrouped.push(event);
      continue;
    }
    const group = groups.get(event.toolUseId) ?? { id: event.toolUseId, events: [], children: [] };
    if (event.parentToolUseId && event.parentToolUseId !== event.toolUseId) group.parentToolUseId = event.parentToolUseId;
    group.events.push(event);
    groups.set(event.toolUseId, group);
  }
  const roots: ToolGroup[] = [];
  for (const group of groups.values()) {
    const parent = group.parentToolUseId ? groups.get(group.parentToolUseId) : undefined;
    if (parent && parent.id !== group.id) parent.children.push(group);
    else roots.push(group);
  }
  for (const group of groups.values()) {
    group.events = sortedEvents(group.events);
    group.children.sort((left, right) => firstTime(left.events).localeCompare(firstTime(right.events)) || left.id.localeCompare(right.id));
  }
  roots.sort((left, right) => firstTime(left.events).localeCompare(firstTime(right.events)) || left.id.localeCompare(right.id));
  return { roots, ungrouped: sortedEvents(ungrouped) };
}

function firstTime(events: AgentTraceEvent[]) {
  return events[0]?.occurredAt ?? "";
}

interface ActivitySpan {
  id: string;
  kind: "agent" | "tool";
  label: string;
  state: TraceState;
  startMs: number | null;
  endMs: number | null;
  durationMs: number | null;
  overlapCount: number;
}

function recordedSpan(id: string, kind: ActivitySpan["kind"], label: string, events: AgentTraceEvent[], startType: AgentTraceEvent["type"], endTypes: AgentTraceEvent["type"][], fallback: TraceState): ActivitySpan {
  const ordered = sortedEvents(events);
  const start = ordered.find((event) => event.type === startType);
  const end = [...ordered].reverse().find((event) => endTypes.includes(event.type));
  const startValue = start ? Date.parse(start.occurredAt) : Number.NaN;
  const endValue = end ? Date.parse(end.occurredAt) : Number.NaN;
  const startMs = Number.isFinite(startValue) ? startValue : null;
  const endMs = Number.isFinite(endValue) ? endValue : null;
  const durationMs = startMs !== null && endMs !== null && endMs >= startMs ? endMs - startMs : null;
  const state = end ? eventState(end) : start ? (fallback === "running" ? "running" : "incomplete") : fallback;
  return { id, kind, label, state, startMs, endMs, durationMs, overlapCount: 0 };
}

function formatObservedDuration(milliseconds: number | null, locale: Locale) {
  if (milliseconds === null || !Number.isFinite(milliseconds) || milliseconds < 0) return labels[locale].notCollected;
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(2)} s`;
}

function collectActivitySpans(roots: AgentGroup[], turnEvents: AgentTraceEvent[], traceStatus: TraceState, locale: Locale) {
  const spans: ActivitySpan[] = [recordedSpan("__main__", "agent", labels[locale].agent, turnEvents, "turn_started", ["turn_completed", "turn_interrupted"], traceStatus)];
  const addToolTree = (group: ToolGroup) => {
    const name = group.events.find((event) => event.toolName)?.toolName ?? labels[locale].tool;
    spans.push(recordedSpan(`tool:${group.id}`, "tool", `${name} · ${group.id}`, group.events, "tool_started", ["tool_completed"], traceStatus === "running" ? "running" : "unknown"));
    for (const child of group.children) addToolTree(child);
  };
  const visitAgent = (group: AgentGroup) => {
    if (group.id !== "__main__" && group.id !== "__unknown_owner__" && group.events.some((event) => event.type === "subagent_started" || event.type === "subagent_completed")) {
      const lifecycle = group.events.filter((event) => event.type === "subagent_started" || event.type === "subagent_completed");
      spans.push(recordedSpan(`agent:${group.id}`, "agent", `${labels[locale].subagent} · ${group.id}`, lifecycle, "subagent_started", ["subagent_completed"], traceStatus));
    }
    const toolTree = groupTools(group.events.filter(isToolEvent));
    for (const tool of toolTree.roots) addToolTree(tool);
    for (const child of group.children) visitAgent(child);
  };
  for (const root of roots) visitAgent(root);

  return spans.map((span) => {
    if (span.kind !== "agent" || span.id === "__main__" || span.startMs === null || span.endMs === null || span.endMs <= span.startMs) return span;
    const overlapCount = spans.filter((other) => other.kind === "agent" && other.id !== span.id && other.id !== "__main__" && other.startMs !== null && other.endMs !== null && other.endMs > other.startMs && span.startMs! < other.endMs && other.startMs < span.endMs!).length;
    return { ...span, overlapCount };
  }).map((span) => {
    if (span.kind !== "tool" || span.startMs === null || span.endMs === null || span.endMs <= span.startMs) return span;
    const overlapCount = spans.filter((other) => other.kind === "tool" && other.id !== span.id && other.startMs !== null && other.endMs !== null && other.endMs > other.startMs && span.startMs! < other.endMs && other.startMs < span.endMs!).length;
    return { ...span, overlapCount };
  });
}

function clockTime(value: number, locale: Locale) {
  return new Intl.DateTimeFormat(locale, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(value);
}

export default function TraceWorkbench({ locale, adminToken, focusCoreTraceId, onOpenIssue }: { locale: Locale; adminToken: string; focusCoreTraceId?: string; onOpenIssue: (id: string) => void }) {
  const c = labels[locale];
  const [items, setItems] = useState<AgentTraceSummary[]>([]);
  const [detail, setDetail] = useState<AgentTraceDetail | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const detailRequestRef = useRef(0);

  const loadTraces = useCallback(async (cursor?: string, append = false) => {
    if (!adminToken) return;
    setLoading(true);
    setError("");
    try {
      const response = await api.listTraces({ limit: 50, cursor });
      setItems((current) => append ? [...current, ...response.items.filter((trace) => !current.some((item) => item.id === trace.id))] : response.items);
      setNextCursor(response.nextCursor ?? null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : c.requestFailed);
    } finally {
      setLoading(false);
    }
  }, [adminToken, c.requestFailed]);

  const openTrace = useCallback(async (id: string) => {
    if (!adminToken || !id) return;
    const requestId = ++detailRequestRef.current;
    setSelectedId(id);
    setDetailLoading(true);
    setError("");
    try {
      const resolved = await api.getTrace(id);
      if (detailRequestRef.current === requestId) setDetail(resolved);
    } catch (cause) {
      if (detailRequestRef.current === requestId) {
        setDetail(null);
        setError(cause instanceof Error ? cause.message : c.requestFailed);
      }
    } finally {
      if (detailRequestRef.current === requestId) setDetailLoading(false);
    }
  }, [adminToken, c.requestFailed]);

  useEffect(() => { void loadTraces(); }, [loadTraces]);
  useEffect(() => {
    if (!focusCoreTraceId || !adminToken) {
      setDetailLoading(false);
      return;
    }
    const requestId = ++detailRequestRef.current;
    setDetailLoading(true);
    void api.getTraceByCore(focusCoreTraceId).then((resolved) => {
      if (detailRequestRef.current === requestId) {
        setSelectedId(resolved.id);
        setDetail(resolved);
        setError("");
      }
    }).catch((cause: unknown) => {
      if (detailRequestRef.current === requestId) {
        setDetail(null);
        setError(cause instanceof Error ? cause.message : c.requestFailed);
      }
    }).finally(() => {
      if (detailRequestRef.current === requestId) setDetailLoading(false);
    });
    return () => {
      if (detailRequestRef.current === requestId) detailRequestRef.current += 1;
    };
  }, [focusCoreTraceId, adminToken, c.requestFailed]);

  useEffect(() => () => { detailRequestRef.current += 1; }, []);

  const filteredItems = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return items;
    return items.filter((trace) => [trace.id, trace.source, trace.sourceSessionId, trace.sourceTurnId, trace.model ?? ""].some((value) => value.toLowerCase().includes(query)));
  }, [items, search]);

  if (!adminToken) return <div className="page trace-page"><SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} /><InlineNotice tone="warning" title={c.locked}>{c.lockedBody}</InlineNotice></div>;

  return <div className="page trace-page">
    <SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} action={<Button variant="secondary" icon={ArrowClockwise} loading={loading} onClick={() => void loadTraces()}>{c.refresh}</Button>} />
    {error ? <InlineNotice tone="error" title={c.requestFailed}>{error}</InlineNotice> : null}
    <div className="trace-layout">
      <section className="panel trace-list" aria-label={c.title}>
        <label className="trace-search"><MagnifyingGlass size={16} /><input aria-label={c.search} placeholder={c.search} value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        {loading && !items.length ? <LoadingRows count={5} /> : filteredItems.length ? <div className="trace-list-items">{filteredItems.map((trace) => <button type="button" key={trace.id} className={`trace-list-item ${selectedId === trace.id ? "active" : ""}`} aria-pressed={selectedId === trace.id} onClick={() => void openTrace(trace.id)}>
          <span className="trace-list-top"><Badge tone={stateTone(trace.status)} dot>{stateLabel(trace.status, locale)}</Badge><small>{formatTime(trace.startedAt ?? trace.createdAt, locale)}</small></span>
          <strong>{trace.sourceSessionId} <ArrowRight size={13} /> {trace.sourceTurnId}</strong>
          <code>{trace.id}</code>
          <span className="trace-list-meta"><span>{trace.model ?? c.unknown}</span><span>{tokenUsageLabel(trace.tokenUsage, locale)}</span><span>{trace.issueCount} {c.issues.toLowerCase()}</span></span>
        </button>)}</div> : <EmptyState icon={GitBranch} title={c.empty} body={c.emptyBody} />}
        {nextCursor ? <footer className="trace-list-footer"><Button size="sm" variant="ghost" loading={loading} onClick={() => void loadTraces(nextCursor, true)}>{c.loadMore}</Button></footer> : null}
      </section>
      <section className="panel trace-detail" aria-label={c.details}>
        {detailLoading ? <LoadingRows count={7} /> : detail ? <TraceDetail detail={detail} locale={locale} onOpenIssue={onOpenIssue} /> : <EmptyState icon={ClockCounterClockwise} title={c.details} body={c.select} />}
      </section>
    </div>
  </div>;
}

function TraceDetail({ detail, locale, onOpenIssue }: { detail: AgentTraceDetail; locale: Locale; onOpenIssue: (id: string) => void }) {
  const c = labels[locale];
  const events = useMemo(() => sortedEvents(detail.events ?? []), [detail.events]);
  const { roots, turnEvents } = useMemo(() => groupAgents(events), [events]);
  const activitySpans = useMemo(() => collectActivitySpans(roots, turnEvents, detail.status, locale), [roots, turnEvents, detail.status, locale]);
  const eventTimes = useMemo(() => events.map((event) => Date.parse(event.occurredAt)).filter(Number.isFinite), [events]);
  const rangeStart = eventTimes.length ? Math.min(...eventTimes) : null;
  const rangeEnd = eventTimes.length ? Math.max(...eventTimes) : null;
  return <div className="trace-detail-content">
    <header className="trace-detail-header"><div><p className="panel-kicker">{c.details}</p><h2>{detail.sourceSessionId} <ArrowRight size={16} /> {detail.sourceTurnId}</h2><code>{detail.id}</code></div><Badge tone={stateTone(detail.status)} dot>{stateLabel(detail.status, locale)}</Badge></header>
    <dl className="trace-meta">
      <Meta label={c.source} value={detail.source} /><Meta label={c.started} value={formatTime(detail.startedAt, locale)} /><Meta label={c.ended} value={formatTime(detail.endedAt, locale)} />
      <Meta label={c.duration} value={formatObservedDuration(activitySpans.find((span) => span.id === "__main__")?.durationMs ?? null, locale)} />
      <Meta label={c.llmDuration} value={c.notCollected} /><Meta label={c.model} value={detail.model ?? c.unknown} /><Meta label={c.tokens} value={tokenUsageLabel(detail.tokenUsage, locale)} /><Meta label={c.events} value={String(detail.eventCount)} />
    </dl>
    {detail.issueIds.length ? <section className="trace-evidence"><h3>{c.issues}</h3><div>{detail.issueIds.map((id) => <button key={id} type="button" className="trace-evidence-link" onClick={() => onOpenIssue(id)}><LinkSimple size={14} />{c.openIssue}<code>{id}</code></button>)}</div></section> : null}
    {detail.coreTraceIds.length ? <section className="trace-evidence" id="trace-core-evidence"><h3>{c.evidence}</h3><div>{detail.coreTraceIds.map((id) => {
      const event = events.find((item) => item.coreTraceId === id);
      const diagnostic = detail.coreDiagnostics?.find((item) => item.traceId === id);
      const summary = diagnostic ? `${diagnostic.method} · ${stateLabel(diagnostic.status, locale)} · ${diagnostic.durationMs.toFixed(1)} ms` : "";
      return event ? <a className="trace-evidence-link" key={id} href={`#trace-event-${encodeURIComponent(event.eventId)}`}><LinkSimple size={14} />{c.core}<code>{id}</code>{summary ? <small>{summary}</small> : null}{diagnostic?.phaseSpans.map((phase) => <small key={phase.name}>{phase.name}: {phase.durationMs.toFixed(1)} ms · {stateLabel(phase.status, locale)}</small>)}</a> : <span className="trace-evidence-link" key={id}><LinkSimple size={14} />{c.core}<code>{id}</code>{summary ? <small>{summary}</small> : null}</span>;
    })}</div></section> : null}
    <section className="trace-timeline-section"><h3>{c.session} <span className="trace-chevron">›</span> {c.turn}</h3>
      {activitySpans.length ? <ActivityLanes spans={activitySpans} rangeStart={rangeStart} rangeEnd={rangeEnd} locale={locale} /> : null}
      <div className="trace-timeline">
        <div className="trace-node trace-session-node"><div className="trace-node-heading"><span className="trace-node-icon"><GitBranch size={15} /></span><strong>{c.session}</strong><code>{detail.sourceSessionId}</code><Badge tone={stateTone(detail.status)}>{stateLabel(detail.status, locale)}</Badge></div>
          <div className="trace-node trace-turn-node"><div className="trace-node-heading"><span className="trace-node-icon"><ClockCounterClockwise size={15} /></span><strong>{c.turn}</strong><code>{detail.sourceTurnId}</code><Badge tone={stateTone(deriveState(events, detail.status))}>{stateLabel(deriveState(events, detail.status), locale)}</Badge></div>
            {turnEvents.length ? <div className="trace-event-list">{turnEvents.map((event) => <EventCard event={event} key={event.eventId} locale={locale} />)}</div> : null}
            {roots.map((group) => <AgentNode group={group} key={group.id} locale={locale} traceStatus={detail.status} coreDiagnostics={detail.coreDiagnostics ?? []} onOpenIssue={onOpenIssue} />)}
            {!events.length ? <p className="trace-no-events">{c.noEvents}</p> : null}
          </div>
        </div>
      </div>
    </section>
  </div>;
}

function Meta({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd>{value}</dd></div>; }

function ActivityLanes({ spans, rangeStart, rangeEnd, locale }: { spans: ActivitySpan[]; rangeStart: number | null; rangeEnd: number | null; locale: Locale }) {
  const c = labels[locale];
  const range = rangeStart !== null && rangeEnd !== null ? Math.max(1, rangeEnd - rangeStart) : 0;
  return <section className="trace-activity" aria-label={c.parallelActivity}>
    <header className="trace-activity-heading"><h4>{c.parallelActivity}</h4>{rangeStart !== null && rangeEnd !== null ? <small>{clockTime(rangeStart, locale)} – {clockTime(rangeEnd, locale)}</small> : null}</header>
    <div className="trace-activity-rows">
      {spans.map((span) => {
        const left = span.startMs !== null && range > 0 ? Math.max(0, Math.min(100, ((span.startMs - (rangeStart ?? span.startMs)) / range) * 100)) : null;
        const width = left !== null && span.durationMs !== null && range > 0 ? Math.max(1, Math.min(100 - left, (span.durationMs / range) * 100)) : null;
        const observedInterval = span.startMs === null ? c.notCollected : `${clockTime(span.startMs, locale)}${span.endMs === null ? "" : ` – ${clockTime(span.endMs, locale)}`}`;
        return <div className="trace-activity-row" role="group" key={`${span.kind}:${span.id}`} data-kind={span.kind} aria-label={`${span.label}; ${c.observedDuration} ${formatObservedDuration(span.durationMs, locale)}; ${stateLabel(span.state, locale)}; ${observedInterval}`}>
          <span className="trace-activity-label"><strong>{span.label}</strong><Badge tone={stateTone(span.state)}>{stateLabel(span.state, locale)}</Badge></span>
          <div className="trace-activity-track" aria-hidden="true">
            {left !== null ? <span className="trace-activity-start" style={{ left: `${left}%` }} /> : null}
            {left !== null && width !== null ? <span className={`trace-activity-bar trace-activity-${span.kind}`} data-status={span.state} style={{ left: `${left}%`, width: `${width}%` }} /> : null}
          </div>
          <small className="trace-activity-meta">{c.observedDuration}: {formatObservedDuration(span.durationMs, locale)}{span.overlapCount ? ` · ${span.overlapCount} ${c.overlap}` : ""} · {observedInterval}</small>
        </div>;
      })}
    </div>
  </section>;
}

function AgentNode({ group, locale, traceStatus, coreDiagnostics, onOpenIssue }: { group: AgentGroup; locale: Locale; traceStatus: TraceState; coreDiagnostics: NonNullable<AgentTraceDetail["coreDiagnostics"]>; onOpenIssue: (id: string) => void }) {
  const c = labels[locale];
  const tools = useMemo(() => groupTools(group.events.filter(isToolEvent)), [group.events]);
  const directEvents = group.events.filter((event) => !isToolEvent(event) || !event.toolUseId);
  const isUnknown = group.id === "__unknown_owner__";
  const isMain = group.id === "__main__";
  const lifecycleEvents = group.events.filter((event) => event.type === "subagent_started" || event.type === "subagent_completed");
  const state = isMain ? traceStatus : isUnknown ? deriveState(group.events, traceStatus) : deriveState(lifecycleEvents, traceStatus);
  const durationEvents = isMain ? [] : lifecycleEvents;
  const observedDuration = durationEvents.length ? formatObservedDuration(recordedSpan(`agent:${group.id}`, "agent", "", durationEvents, "subagent_started", ["subagent_completed"], traceStatus).durationMs, locale) : "";
  const parentUnavailable = Boolean(group.parentAgentId);
  return <div className={`trace-node trace-agent-node ${isUnknown ? "trace-unknown-owner" : ""}`}>
    <div className="trace-node-heading"><span className="trace-node-icon"><GitBranch size={15} /></span><strong>{isUnknown ? c.unknownOwner : isMain ? c.agent : c.subagent}</strong>{!isUnknown && !isMain ? <code>{group.id}</code> : null}<Badge tone={stateTone(state)}>{stateLabel(state, locale)}</Badge>{!isMain && !isUnknown ? <small>{c.observedDuration}: {observedDuration || c.notCollected}</small> : null}{parentUnavailable && !isUnknown ? <small>{c.parentUnavailable}</small> : null}</div>
    {directEvents.length ? <div className="trace-event-list">{directEvents.map((event) => <EventCard event={event} key={event.eventId} locale={locale} />)}</div> : null}
    {tools.ungrouped.length ? <div className="trace-event-list">{tools.ungrouped.map((event) => <EventCard event={event} key={event.eventId} locale={locale} />)}</div> : null}
    {tools.roots.map((tool) => <ToolNode group={tool} key={tool.id} locale={locale} traceStatus={traceStatus} coreDiagnostics={coreDiagnostics} onOpenIssue={onOpenIssue} />)}
    {isUnknown && group.events.some(isToolEvent) ? <small className="trace-owner-note">{c.toolOwnerUnknown}</small> : null}
    {group.children.map((child) => <AgentNode group={child} key={child.id} locale={locale} traceStatus={traceStatus} coreDiagnostics={coreDiagnostics} onOpenIssue={onOpenIssue} />)}
  </div>;
}

function ToolNode({ group, locale, traceStatus, coreDiagnostics, onOpenIssue }: { group: ToolGroup; locale: Locale; traceStatus: TraceState; coreDiagnostics: NonNullable<AgentTraceDetail["coreDiagnostics"]>; onOpenIssue: (id: string) => void }) {
  const c = labels[locale];
  const first = group.events[0];
  const name = first?.toolName ?? c.tool;
  const state = deriveState(group.events, traceStatus === "running" ? "running" : "unknown");
  const observedDuration = formatObservedDuration(recordedSpan(`tool:${group.id}`, "tool", name, group.events, "tool_started", ["tool_completed"], traceStatus === "running" ? "running" : "unknown").durationMs, locale);
  const parentUnavailable = Boolean(group.parentToolUseId);
  return <div className="trace-node trace-tool-node">
    <div className="trace-node-heading"><span className="trace-node-icon"><Wrench size={14} /></span><strong>{c.tool}</strong><span>{name}</span><code>{group.id}</code><Badge tone={stateTone(state)}>{stateLabel(state, locale)}</Badge><small>{c.observedDuration}: {observedDuration}</small>{parentUnavailable ? <small>{c.parentToolUnavailable}</small> : null}</div>
    {group.events.map((event) => <EventCard event={event} key={event.eventId} locale={locale} />)}
    {[...new Set(group.events.map((event) => event.coreTraceId).filter((id): id is string => Boolean(id)))].map((id) => {
      const diagnostic = coreDiagnostics.find((item) => item.traceId === id);
      return <div className="trace-node trace-core-node" key={id}><div className="trace-node-heading"><strong>{c.core}</strong><code>{id}</code>{diagnostic ? <Badge tone={stateTone(diagnostic.status)}>{stateLabel(diagnostic.status, locale)}</Badge> : null}{diagnostic?.issueIds?.map((issueId) => <button key={issueId} type="button" className="trace-evidence-link" onClick={() => onOpenIssue(issueId)}><LinkSimple size={14} />{c.openIssue}<code>{issueId}</code></button>)}</div>{diagnostic ? <div className="trace-core-phases"><span>{diagnostic.method} · {diagnostic.durationMs.toFixed(1)} ms</span><span>{c.stage}: {diagnostic.stage}</span>{diagnostic.phaseSpans.map((phase) => <span key={phase.name}>{phase.name}: {phase.durationMs.toFixed(1)} ms · {stateLabel(phase.status, locale)}</span>)}</div> : null}</div>;
    })}
    {group.children.map((child) => <ToolNode group={child} key={child.id} locale={locale} traceStatus={traceStatus} coreDiagnostics={coreDiagnostics} onOpenIssue={onOpenIssue} />)}
  </div>;
}

function EventCard({ event, locale }: { event: AgentTraceEvent; locale: Locale }) {
  const c = labels[locale];
  const id = `trace-event-${encodeURIComponent(event.eventId)}`;
  return <article className="trace-event" id={id}>
    <div className="trace-event-heading"><span>{typeLabel(event, locale)}</span><Badge tone={stateTone(eventState(event))}>{stateLabel(eventState(event), locale)}</Badge><time dateTime={event.occurredAt}>{formatTime(event.occurredAt, locale)}</time></div>
    <div className="trace-event-meta"><span>{c.event} <a href={`#${id}`}><code>{event.eventId}</code></a></span>{event.model ? <span>{c.model}: {event.model}</span> : null}{event.tokenUsage ? <span>{c.tokens}: {tokenUsageLabel(event.tokenUsage, locale)}</span> : null}{event.coreTraceId ? <a href="#trace-core-evidence">{c.core}: <code>{event.coreTraceId}</code></a> : null}</div>
  </article>;
}
