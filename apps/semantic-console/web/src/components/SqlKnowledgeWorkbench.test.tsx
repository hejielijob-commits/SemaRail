import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import SqlKnowledgeWorkbench, { SqlQueryKnowledgePanel, type SqlHistoryReference, type SqlKnowledgeCandidate } from "./SqlKnowledgeWorkbench";

const history: SqlHistoryReference = {
  id: "history-1",
  question: "Revenue by region",
  sql: "select region, sum(amount) from orders group by region;",
  status: "approved",
  sourcePath: "knowledge/sql/revenue-by-region.md",
  score: 0.91,
};

function candidate(overrides: Partial<SqlKnowledgeCandidate> = {}): SqlKnowledgeCandidate {
  return {
    id: "candidate-1",
    queryId: "q-20260826-01",
    question: "What is net revenue by region?",
    sql: "select region, sum(net_revenue) as revenue\nfrom orders\ngroup by region;",
    status: "pending",
    stats: { durationMs: 84, rowCount: 12, datasource: "analytics", dialect: "PostgreSQL", modelNames: ["orders"], fields: ["region", "net_revenue"] },
    sqlHistory: [history],
    sourcePath: "review/sql/candidate-1.yml",
    sessionId: "session-1",
    submittedAt: "2026-08-26T09:30:00.000Z",
    validation: { status: "not-run" },
    ...overrides,
  };
}

describe("SqlKnowledgeWorkbench", () => {
  it("shows the review queue and the pending candidate detail", async () => {
    render(<SqlKnowledgeWorkbench candidates={[candidate()]} />);
    expect(await screen.findByRole("heading", { name: "SQL knowledge" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Pending/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByText("What is net revenue by region?")).toHaveLength(2);
    expect(screen.getByRole("heading", { name: "SQL", level: 3 })).toBeInTheDocument();
    expect(screen.getByText("PostgreSQL")).toBeInTheDocument();
    expect(screen.getByText("review/sql/candidate-1.yml")).toBeInTheDocument();
    expect(screen.getByText("session-1")).toBeInTheDocument();
    expect(screen.getByText("Validate SQL")).toBeInTheDocument();
  });

  it("shows referenced historical SQL and an explicit no-history state", async () => {
    const { rerender } = render(<SqlKnowledgeWorkbench candidates={[candidate()]} />);
    fireEvent.click(await screen.findByRole("button", { name: /History used/ }));
    expect(screen.getByText("Revenue by region")).toBeInTheDocument();
    expect(screen.getByText(/91% match/)).toBeInTheDocument();
    rerender(<SqlKnowledgeWorkbench candidates={[candidate({ sqlHistory: [] })]} />);
    expect(screen.getAllByText("No historical SQL was used").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/This query was generated without/)).toBeInTheDocument();
  });

  it("requires successful validation before approval", async () => {
    const onValidate = vi.fn().mockResolvedValue({ status: "passed", message: "Query plan is safe." });
    const onApprove = vi.fn().mockResolvedValue(undefined);
    render(<SqlKnowledgeWorkbench candidates={[candidate()]} onValidate={onValidate} onApprove={onApprove} />);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect(approve).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Validate SQL" }));
    await waitFor(() => expect(onValidate).toHaveBeenCalledWith(expect.objectContaining({ queryId: "q-20260826-01" })));
    await waitFor(() => expect(approve).not.toBeDisabled());
    fireEvent.click(approve);
    await waitFor(() => expect(onApprove).toHaveBeenCalledWith(expect.objectContaining({ sql: expect.stringContaining("net_revenue") }), expect.stringContaining("net_revenue")));
    expect(screen.getByText("The SQL example was approved for the knowledge library.")).toBeInTheDocument();
  });

  it("requires a review note and sends it when rejecting", async () => {
    const onReject = vi.fn().mockResolvedValue(undefined);
    render(<SqlKnowledgeWorkbench candidates={[candidate()]} onReject={onReject} />);
    fireEvent.click(await screen.findByRole("button", { name: "Reject" }));
    const note = screen.getByRole("textbox", { name: "Review note" });
    expect(screen.getByRole("button", { name: "Confirm rejection" })).toBeDisabled();
    fireEvent.change(note, { target: { value: "The metric definition needs to be clarified." } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm rejection" }));
    await waitFor(() => expect(onReject).toHaveBeenCalledWith(expect.objectContaining({ id: "candidate-1" }), "The metric definition needs to be clarified."));
  });

  it("allows editing SQL only through an explicit save callback", async () => {
    const onSaveSql = vi.fn().mockResolvedValue(undefined);
    render(<SqlKnowledgeWorkbench candidates={[candidate()]} onSaveSql={onSaveSql} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit SQL" }));
    const editor = screen.getByRole("textbox", { name: "SQL" });
    fireEvent.change(editor, { target: { value: "select 1;" } });
    fireEvent.click(screen.getByRole("button", { name: "Save SQL" }));
    await waitFor(() => expect(onSaveSql).toHaveBeenCalledWith(expect.objectContaining({ id: "candidate-1" }), "select 1;"));
  });

  it("switches review statuses and keeps each tab count accurate", async () => {
    render(<SqlKnowledgeWorkbench candidates={[candidate(), candidate({ id: "approved-1", status: "approved", question: "Approved example" }), candidate({ id: "rejected-1", status: "rejected", question: "Rejected example" })]} />);
    expect(await screen.findByRole("button", { name: /What is net revenue by region\?/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /Approved/ }));
    expect(screen.getAllByText("Approved example")).toHaveLength(2);
    expect(screen.queryByText("What is net revenue by region?")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /Rejected/ }));
    expect(screen.getAllByText("Rejected example")).toHaveLength(2);
  });

  it("renders the current query capture and submits it as a review candidate", async () => {
    const onRecordQuery = vi.fn().mockResolvedValue(undefined);
    render(<SqlKnowledgeWorkbench candidates={[]} activeQuery={{ queryId: "q-live", question: "How many orders?", sql: "select count(*) from orders;", sqlHistory: [history] }} onRecordQuery={onRecordQuery} />);
    expect(await screen.findByRole("heading", { name: "Current query" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Record for review" }));
    await waitFor(() => expect(onRecordQuery).toHaveBeenCalledWith(expect.objectContaining({ queryId: "q-live" })));
    expect(screen.getByRole("button", { name: "Awaiting review" })).toBeDisabled();
  });

  it("exposes the compact query panel alias with an explicit no-history empty state", async () => {
    const onRecordQuery = vi.fn();
    render(<SqlQueryKnowledgePanel query={{ queryId: "q-empty", question: "No history", sql: "select 1;", sqlHistory: [] }} onRecordQuery={onRecordQuery} />);
    fireEvent.click(await screen.findByRole("button", { name: /History used/ }));
    expect(screen.getAllByText("No historical SQL was used").length).toBeGreaterThanOrEqual(1);
  });

  it("supports read-only and localized modes", async () => {
    render(<SqlKnowledgeWorkbench candidates={[candidate()]} locale="zh-CN" readOnly onValidate={vi.fn()} onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "SQL 知识库" })).toBeInTheDocument();
    expect(screen.getByText("只读")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "校验 SQL" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "批准" })).toBeDisabled();
  });

  it("renders loading and retryable error states", async () => {
    const retry = vi.fn();
    const { rerender } = render(<SqlKnowledgeWorkbench candidates={[]} loading />);
    expect(screen.getByRole("status", { name: "Loading SQL knowledge" })).toBeInTheDocument();
    rerender(<SqlKnowledgeWorkbench candidates={[]} error="Server unavailable" onRetry={retry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Server unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("keeps a candidate pending and surfaces approval failures", async () => {
    const onValidate = vi.fn().mockResolvedValue({ status: "passed" });
    const onApprove = vi.fn().mockRejectedValue(new Error("Approval conflict"));
    render(<SqlKnowledgeWorkbench candidates={[candidate()]} onValidate={onValidate} onApprove={onApprove} />);
    fireEvent.click(await screen.findByRole("button", { name: "Validate SQL" }));
    const approve = screen.getByRole("button", { name: "Approve" });
    await waitFor(() => expect(approve).toBeEnabled());
    fireEvent.click(approve);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Approval conflict"));
    expect(screen.getAllByText("Pending review").length).toBeGreaterThan(0);
  });
});
