import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import "../i18n";
import type { DiagnosticFeedback } from "../types";
import DiagnosticsWorkbench from "./DiagnosticsWorkbench";

const issue: DiagnosticFeedback = {
  id: "fb_auto", diagnosticId: "diag_1", traceId: "trace_1", queryId: "query_2", originalQueryId: "query_1", datasourceId: "hr",
  subjectId: "service_account_1", transport: "core-http", method: "query.run", stage: "authorization",
  semanticVersion: "semantic-v3", durationMs: 12.5, policyVersions: ["policy-1:4"], category: "runtime_failure",
  description: "Automatically captured query failure", status: "pending", duplicateOf: null,
  evidence: { source: "server", question: "Show salary", semanticSql: "SELECT salary FROM hr.compensation", nativeSql: null, error: { reasonCode: "TABLE_PERMISSION_REQUIRED", resources: [{ table: "hr.compensation" }] }, contentPurgedAt: null },
  createdAt: "2026-09-08T08:00:00Z", diagnosticCreatedAt: "2026-09-08T08:00:00Z", updatedAt: "2026-09-08T08:00:00Z",
  history: [{ id: "history_1", actorSubjectId: "admin", fromStatus: "pending", toStatus: "classified", note: "Reproduced", createdAt: "2026-09-08T09:00:00Z" }],
};

describe("DiagnosticsWorkbench", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("shows an automatically captured failure and saves the administrator workflow", async () => {
    vi.spyOn(api, "listDiagnosticFeedback").mockResolvedValue({ items: [issue], nextCursor: null });
    vi.spyOn(api, "getDiagnosticFeedback").mockResolvedValue(issue);
    const update = vi.spyOn(api, "updateDiagnosticFeedback").mockResolvedValue({ ...issue, status: "located" });

    const { container } = render(<DiagnosticsWorkbench locale="en-US" mode="diagnostics" authToken="admin" adminToken="admin" />);

    expect((await screen.findAllByText("Automatically captured query failure")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("TABLE_PERMISSION_REQUIRED").length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getByText("semantic-v3")).toBeInTheDocument());
    expect(screen.getByText("query_1")).toBeInTheDocument();
    expect(screen.getByText(/Reproduced/)).toBeInTheDocument();
    const statusSelect = screen.getAllByRole("combobox").find((element) => (element as HTMLSelectElement).value === "pending");
    expect(statusSelect).toBeDefined();
    fireEvent.change(statusSelect!, { target: { value: "closed_no_fix" } });
    fireEvent.change(container.querySelector(".diagnostic-workflow input")!, { target: { value: "fb_canonical" } });
    fireEvent.change(container.querySelector(".diagnostic-workflow textarea")!, { target: { value: "Duplicate; no separate fix" } });
    fireEvent.click(screen.getByRole("button", { name: "Save workflow" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("fb_auto", expect.objectContaining({
      status: "closed_no_fix", duplicateOf: "fb_canonical", note: "Duplicate; no separate fix",
    })));
  });

  it("submits the opaque-link feedback with a retry-stable idempotency key", async () => {
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: { randomUUID: vi.fn().mockReturnValueOnce("attempt-1").mockReturnValue("attempt-2") } });
    const submit = vi.spyOn(api, "submitFeedback").mockResolvedValue({ feedbackId: "fb_1", diagnosticId: "diag_1", status: "pending", duplicate: false });

    render(<DiagnosticsWorkbench locale="en-US" mode="diagnostics" authToken="owner" adminToken="" feedbackReference="opaque-query-id" />);
    const textareas = screen.getAllByRole("textbox");
    fireEvent.change(textareas[0], { target: { value: "The metric is wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith(expect.objectContaining({ reference: "opaque-query-id", idempotencyKey: "attempt-1", description: "The metric is wrong" })));
    expect(await screen.findByText("fb_1")).toBeInTheDocument();
    expect(screen.getByText(/shared service account/)).toBeInTheDocument();
  });

  it("reports a feedback failure and retries with the same idempotency key", async () => {
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: { randomUUID: vi.fn().mockReturnValueOnce("stable-attempt").mockReturnValue("next-attempt") } });
    const submit = vi.spyOn(api, "submitFeedback")
      .mockRejectedValueOnce(new Error("diagnostic storage unavailable"))
      .mockResolvedValueOnce({ feedbackId: "fb_retry", diagnosticId: "diag_retry", status: "pending", duplicate: false });

    render(<DiagnosticsWorkbench locale="en-US" mode="diagnostics" authToken="owner" adminToken="" feedbackReference="opaque-query-id" />);
    fireEvent.change(screen.getAllByRole("textbox")[0]!, { target: { value: "The result is wrong" } });
    const button = screen.getByRole("button", { name: "Submit feedback" });
    fireEvent.click(button);
    expect(await screen.findByText("diagnostic storage unavailable")).toBeInTheDocument();
    fireEvent.click(button);

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit.mock.calls[0]?.[0].idempotencyKey).toBe("stable-attempt");
    expect(submit.mock.calls[1]?.[0].idempotencyKey).toBe("stable-attempt");
    expect(await screen.findByText("fb_retry")).toBeInTheDocument();
  });

  it("applies server filters and follows the opaque pagination cursor in Chinese", async () => {
    const list = vi.spyOn(api, "listDiagnosticFeedback")
      .mockResolvedValueOnce({ items: [issue], nextCursor: "cursor-page-2" })
      .mockResolvedValueOnce({ items: [issue], nextCursor: "cursor-page-2" })
      .mockResolvedValueOnce({ items: [{ ...issue, id: "fb_page_2", description: "第二页问题" }], nextCursor: null });
    vi.spyOn(api, "getDiagnosticFeedback").mockImplementation(async (id) => ({ ...issue, id }));

    const { container } = render(<DiagnosticsWorkbench locale="zh-CN" mode="diagnostics" authToken="admin" adminToken="admin" />);
    await screen.findByText("原始查询");
    fireEvent.change(screen.getByPlaceholderText("core-http / mcp"), { target: { value: "remote-mcp" } });
    fireEvent.change(screen.getByPlaceholderText("TABLE_PERMISSION_REQUIRED"), { target: { value: "COLUMN_PERMISSION_REQUIRED" } });
    const inputs = Array.from(container.querySelectorAll("input"));
    fireEvent.change(inputs[0]!, { target: { value: "2026-09-01T00:00" } });
    fireEvent.change(inputs[1]!, { target: { value: "2026-09-09T00:00" } });
    fireEvent.change(inputs[3]!, { target: { value: "hr" } });
    const selects = screen.getAllByRole("combobox");
    fireEvent.change(selects[0]!, { target: { value: "closed_no_fix" } });
    fireEvent.change(selects[1]!, { target: { value: "permission_configuration" } });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({
      source: "remote-mcp", datasourceId: "hr", reasonCode: "COLUMN_PERMISSION_REQUIRED",
      status: "closed_no_fix", category: "permission_configuration",
      createdAfter: new Date("2026-09-01T00:00").toISOString(),
      createdBefore: new Date("2026-09-09T00:00").toISOString(),
    })));

    const next = screen.getByRole("button", { name: "下一页" });
    await waitFor(() => expect(next).toBeEnabled());
    fireEvent.click(next);
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: "cursor-page-2" })));
  });

  it("creates an enabled reviewed regression case from issue evidence", async () => {
    vi.spyOn(api, "listDiagnosticFeedback").mockResolvedValue({ items: [issue], nextCursor: null });
    vi.spyOn(api, "getDiagnosticFeedback").mockResolvedValue(issue);
    const create = vi.spyOn(api, "createRegressionCase").mockResolvedValue({
      id: "case_1", status: "enabled",
      case: {
        schemaVersion: 1, kind: "deterministic_sql", question: "Show salary",
        semanticSql: "SELECT salary FROM hr.compensation", testDatasetId: "hr-policy-fixture",
        expectedError: { reasonCode: "TABLE_PERMISSION_REQUIRED" },
      },
    });

    render(<DiagnosticsWorkbench locale="en-US" mode="diagnostics" authToken="admin" adminToken="admin" />);
    await screen.findByText("semantic-v3");
    fireEvent.click(screen.getByRole("button", { name: "Create regression case" }));
    const dialog = screen.getByRole("dialog");
    const fields = within(dialog).getAllByRole("textbox");
    fireEvent.change(fields[2]!, { target: { value: "hr-policy-fixture" } });
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Create case" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith(issue.id, expect.objectContaining({
      enable: true,
      case: expect.objectContaining({ testDatasetId: "hr-policy-fixture", question: "Show salary" }),
    })));
  });

  it("shows draft and enabled cases and exports versioned JSON", async () => {
    const exported = {
      schemaVersion: 1 as const,
      projectId: "hr-project",
      cases: [
        { id: "case_draft", feedbackId: "fb_1", status: "draft" as const, schemaVersion: 1 as const, kind: "agent_evidence" as const, question: "Draft ambiguity case" },
        { id: "case_enabled", feedbackId: "fb_2", status: "enabled" as const, schemaVersion: 1 as const, kind: "deterministic_sql" as const, question: "Enabled salary policy case", testDatasetId: "hr-fixture", expectedError: { reasonCode: "COLUMN_PERMISSION_REQUIRED" } },
      ],
    };
    vi.spyOn(api, "exportRegressionCases").mockResolvedValue(exported);
    const createObjectURL = vi.fn().mockReturnValue("blob:regression-export");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
    let downloadName = "";
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { downloadName = this.download; });

    render(<DiagnosticsWorkbench locale="en-US" mode="regressionCases" authToken="admin" adminToken="admin" />);
    expect(await screen.findByText("Draft ambiguity case")).toBeInTheDocument();
    expect(screen.getByText("Enabled salary policy case")).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByText("enabled")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Export versioned JSON" }));

    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(downloadName).toBe("semarail-regression-cases-v1.json");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:regression-export");
  });
});
