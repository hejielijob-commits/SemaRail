import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { AgentTraceDetail } from "../types";
import persisted from "./fixtures/persisted-trace.json";
import TraceWorkbench from "./TraceWorkbench";

const fixture = persisted as AgentTraceDetail;

describe("TraceWorkbench durable replay", () => {
  afterEach(() => vi.restoreAllMocks());

  it("rebuilds parallel subagents, a failed Core tool, and an unfinished tool after remount", async () => {
    vi.spyOn(api, "listTraces").mockResolvedValue({ items: [fixture], nextCursor: null });
    const getTrace = vi.spyOn(api, "getTrace").mockResolvedValue(fixture);
    const openIssue = vi.fn();

    const first = render(<TraceWorkbench locale="en-US" adminToken="admin" onOpenIssue={openIssue} />);
    fireEvent.click(await screen.findByRole("button", { name: /thr_fixture.*turn_fixture/ }));
    await waitFor(() => expect(getTrace).toHaveBeenCalledWith("atr_fixture"));
    const detail = first.container.querySelector(".trace-detail-content")!;
    expect(within(detail as HTMLElement).getAllByText("Subagent")).toHaveLength(2);
    expect(within(detail as HTMLElement).getByText("agent-a")).toBeInTheDocument();
    expect(within(detail as HTMLElement).getByText("agent-b")).toBeInTheDocument();
    expect(within(detail as HTMLElement).getByText("call-failed")).toBeInTheDocument();
    expect(within(detail as HTMLElement).getByText("call-missing-end")).toBeInTheDocument();
    expect(within(detail as HTMLElement).getByText("Unknown owner")).toBeInTheDocument();
    expect(within(detail as HTMLElement).getAllByText("Failed").length).toBeGreaterThan(0);
    expect(within(detail as HTMLElement).getAllByText("Incomplete").length).toBeGreaterThan(0);
    expect(within(detail as HTMLElement).getAllByText("Completed · outcome unknown").length).toBeGreaterThan(0);
    const activity = detail.querySelector('[aria-label="Parallel activity from recorded timestamps"]')!;
    const agentA = activity.querySelector('[aria-label^="Subagent · agent-a"]')!;
    const agentB = activity.querySelector('[aria-label^="Subagent · agent-b"]')!;
    expect(agentA.getAttribute("aria-label")).toContain("5.00 s");
    expect(agentB.getAttribute("aria-label")).toContain("5.00 s");
    const failedTool = activity.querySelector('[data-kind="tool"][aria-label*="call-failed"]');
    expect(failedTool?.getAttribute("aria-label")).toContain("Observed duration 2.00 s");
    expect(within(activity as HTMLElement).getAllByText(/1 overlap/)).toHaveLength(2);
    expect(within(detail as HTMLElement).getAllByText(/Stage: execution/)).toHaveLength(2);
    expect(within(detail as HTMLElement).getAllByText(/runtime: 3\.0 ms · Failed/).length).toBeGreaterThan(0);
    const llmDuration = within(detail.querySelector(".trace-meta") as HTMLElement).getByText("LLM duration");
    expect(llmDuration.parentElement?.querySelector("dd")).toHaveTextContent("Not collected");
    const toolNodes = [...detail.querySelectorAll<HTMLElement>(".trace-tool-node")];
    const failedToolNode = toolNodes.find((node) => node.querySelector(".trace-node-heading > code")?.textContent === "call-failed")!;
    const secondTool = toolNodes.find((node) => node.querySelector(".trace-node-heading > code")?.textContent === "call-second")!;
    const failedCoreIssue = within(failedToolNode).getByRole("button", { name: /Open issue fb_core_failure/ });
    expect(within(secondTool).queryByRole("button", { name: /Open issue fb_core_failure/ })).not.toBeInTheDocument();
    expect(within(secondTool).getByRole("button", { name: /Open issue fb_core_second/ })).toBeInTheDocument();
    fireEvent.click(failedCoreIssue);
    expect(openIssue).toHaveBeenCalledWith("fb_core_failure");

    first.unmount();
    const second = render(<TraceWorkbench locale="en-US" adminToken="admin" onOpenIssue={openIssue} />);
    fireEvent.click(await screen.findByRole("button", { name: /thr_fixture.*turn_fixture/ }));
    await waitFor(() => expect(second.container.querySelectorAll(".trace-agent-node").length).toBeGreaterThanOrEqual(4));
    expect(getTrace).toHaveBeenCalledTimes(2);
  });

  it("resolves a Core trace link from an issue without treating it as an Agent Trace ID", async () => {
    vi.spyOn(api, "listTraces").mockResolvedValue({ items: [fixture], nextCursor: null });
    const byCore = vi.spyOn(api, "getTraceByCore").mockResolvedValue(fixture);
    const direct = vi.spyOn(api, "getTrace");
    render(<TraceWorkbench locale="zh-CN" adminToken="admin" focusCoreTraceId="trace_core_failure" onOpenIssue={vi.fn()} />);
    await waitFor(() => expect(byCore).toHaveBeenCalledWith("trace_core_failure"));
    expect(direct).not.toHaveBeenCalled();
    expect(await screen.findByText("agent-a")).toBeInTheDocument();
  });

  it("uses interrupted turn timestamps for the observed turn duration", async () => {
    const interrupted: AgentTraceDetail = {
      ...fixture,
      status: "cancelled",
      endedAt: "2026-09-30T08:00:09Z",
      events: fixture.events.map((event) => event.type === "turn_completed"
        ? { ...event, eventId: "turn-interrupt", occurredAt: "2026-09-30T08:00:09Z", type: "turn_interrupted", status: "cancelled" }
        : event),
    };
    vi.spyOn(api, "listTraces").mockResolvedValue({ items: [interrupted], nextCursor: null });
    vi.spyOn(api, "getTrace").mockResolvedValue(interrupted);
    const { container } = render(<TraceWorkbench locale="en-US" adminToken="admin" onOpenIssue={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /thr_fixture.*turn_fixture/ }));
    const meta = await screen.findByText("Observed turn duration");
    expect(meta.parentElement?.querySelector("dd")).toHaveTextContent("9.00 s");
  });

  it("does not calculate hook duration from reversed persisted timestamps", async () => {
    const delayedHooks: AgentTraceDetail = {
      ...fixture,
      events: fixture.events.map((event) => event.eventId === "tool-start:call-failed"
        ? { ...event, occurredAt: "2026-09-30T08:00:06Z" }
        : event.eventId === "tool-end:call-failed"
          ? { ...event, occurredAt: "2026-09-30T08:00:05Z" }
          : event),
    };
    vi.spyOn(api, "listTraces").mockResolvedValue({ items: [delayedHooks], nextCursor: null });
    vi.spyOn(api, "getTrace").mockResolvedValue(delayedHooks);
    const { container } = render(<TraceWorkbench locale="en-US" adminToken="admin" onOpenIssue={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /thr_fixture.*turn_fixture/ }));
    const activity = await screen.findByRole("region", { name: "Parallel activity from recorded timestamps" });
    const failedTool = activity.querySelector('[data-kind="tool"][aria-label*="call-failed"]');
    expect(failedTool?.getAttribute("aria-label")).toContain("Observed duration Not collected");
    expect(failedTool?.querySelector(".trace-activity-bar")).toBeNull();
    expect(container.querySelector(".trace-detail-header code")).toHaveTextContent("atr_fixture");
  });

  it("does not let an older trace request replace the latest selection", async () => {
    const traceA = { ...fixture, id: "atr_a", sourceSessionId: "thr_a", sourceTurnId: "turn_a" };
    const traceB = { ...fixture, id: "atr_b", sourceSessionId: "thr_b", sourceTurnId: "turn_b" };
    let resolveA!: (value: AgentTraceDetail) => void;
    let resolveB!: (value: AgentTraceDetail) => void;
    vi.spyOn(api, "listTraces").mockResolvedValue({ items: [traceA, traceB], nextCursor: null });
    const getTrace = vi.spyOn(api, "getTrace").mockImplementation((id) => new Promise((resolve) => {
      if (id === "atr_a") resolveA = resolve;
      else resolveB = resolve;
    }));
    const { container } = render(<TraceWorkbench locale="en-US" adminToken="admin" onOpenIssue={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /thr_a.*turn_a/ }));
    fireEvent.click(screen.getByRole("button", { name: /thr_b.*turn_b/ }));
    await waitFor(() => expect(getTrace).toHaveBeenCalledTimes(2));
    resolveB(traceB);
    await waitFor(() => expect(container.querySelector(".trace-detail-header code")).toHaveTextContent("atr_b"));
    resolveA(traceA);
    await waitFor(() => expect(container.querySelector(".trace-detail-header code")).toHaveTextContent("atr_b"));
  });
});
