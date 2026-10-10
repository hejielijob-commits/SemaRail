import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiClientError } from "../api/client";
import type { AgentTraceDetail, RegressionCaseV2, RegressionCaseV2Record, RegressionRun, TestDatasetRecord } from "../types";
import persistedTrace from "./fixtures/persisted-trace.json";
import runFixture from "./fixtures/regression-run-result.json";
import RegressionWorkbench from "./RegressionWorkbench";

const activeDataset: TestDatasetRecord = { id: "dataset-active", version: "v4", datasourceId: "warehouse-test", updatedAt: "2026-10-10T00:00:00Z" };
const otherDataset: TestDatasetRecord = { id: "dataset-other", version: "v2", datasourceId: "different-source", updatedAt: "2026-10-10T00:00:00Z" };
const editableCase: RegressionCaseV2 = {
  schemaVersion: 2, kind: "deterministic_sql", question: "Count active users", semanticSql: "SELECT count(*) AS total FROM users ORDER BY total",
  conditions: { active: true }, confirmedConditions: ["active"], testDatasetId: activeDataset.id,
  assertions: { status: "success", rowCount: 1, cells: [{ row: 0, column: "total", expected: 4, tolerance: 0.1 }] },
};
const caseRecord: RegressionCaseV2Record = {
  id: "case-42", feedbackId: "feedback-42", sourceTraceId: "atr_source_42", revision: 3, status: "enabled", case: editableCase,
  datasetVersion: "v4", createdAt: "2026-10-01T00:00:00Z", updatedAt: "2026-10-10T00:00:00Z",
};
const run = runFixture as RegressionRun;

describe("RegressionWorkbench", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  function mockEmptyLists() {
    vi.spyOn(api, "listTestDatasets").mockResolvedValue({ schemaVersion: 1, items: [activeDataset, otherDataset] });
    vi.spyOn(api, "listRegressionCases").mockResolvedValue({ schemaVersion: 2, items: [] });
    vi.spyOn(api, "listRegressionRuns").mockResolvedValue({ schemaVersion: 1, items: [] });
  }

  it("registers datasets for the active datasource and creates a manual draft without copying evidence", async () => {
    mockEmptyLists();
    const createDataset = vi.spyOn(api, "createTestDataset").mockResolvedValue(activeDataset);
    const createCase = vi.spyOn(api, "createRegressionCaseV2").mockResolvedValue(caseRecord);
    const listCases = vi.spyOn(api, "listRegressionCases").mockResolvedValueOnce({ schemaVersion: 2, items: [] }).mockResolvedValue({ schemaVersion: 2, items: [caseRecord] });
    const consumed = vi.fn();

    render(<RegressionWorkbench locale="en-US" adminToken="admin" activeDatasourceId="warehouse-test" initialSeed={{ feedbackId: "feedback-42", sourceTraceId: "atr_source_42" }} onSeedConsumed={consumed} />);
    const dialog = await screen.findByRole("dialog");
    expect(consumed).toHaveBeenCalled();
    expect(within(dialog).getByLabelText(/^Question/)).toHaveValue("");
    expect(within(dialog).getByLabelText(/^Semantic SQL/)).toHaveValue("");
    expect(within(dialog).getByLabelText("Conditions JSON")).toHaveValue("{}");
    expect(within(dialog).getByLabelText("Confirmed conditions JSON")).toHaveValue("[]");
    expect(within(dialog).getByLabelText("Dataset ID")).toHaveValue("");
    expect(screen.getByText(/never copied into this form/i)).toBeInTheDocument();
    expect(within(dialog).queryByRole("option", { name: /dataset-other/ })).not.toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText(/^Question/), { target: { value: "Count active users" } });
    fireEvent.change(within(dialog).getByLabelText(/^Semantic SQL/), { target: { value: "SELECT count(*) AS total FROM users ORDER BY total" } });
    fireEvent.change(within(dialog).getByLabelText("Dataset ID"), { target: { value: activeDataset.id } });
    fireEvent.click(screen.getByRole("button", { name: "Create draft" }));
    await waitFor(() => expect(createCase).toHaveBeenCalledWith(expect.objectContaining({
      feedbackId: "feedback-42", sourceTraceId: "atr_source_42", enable: false,
      case: expect.objectContaining({ question: "Count active users", testDatasetId: "dataset-active", assertions: { status: "success" } }),
    })));
    await waitFor(() => expect(listCases).toHaveBeenCalledTimes(2));
    expect(createDataset).not.toHaveBeenCalled();
  });

  it("registers a dataset only against the active data source", async () => {
    mockEmptyLists();
    const createDataset = vi.spyOn(api, "createTestDataset").mockResolvedValue(activeDataset);
    render(<RegressionWorkbench locale="en-US" adminToken="admin" activeDatasourceId="warehouse-test" />);
    await screen.findByText("dataset-active");

    fireEvent.change(screen.getByLabelText("Dataset ID"), { target: { value: "manual-fixture" } });
    fireEvent.change(screen.getByLabelText("Dataset version"), { target: { value: "v7" } });
    fireEvent.click(screen.getByRole("button", { name: "Register dataset" }));
    await waitFor(() => expect(createDataset).toHaveBeenCalledWith({ id: "manual-fixture", version: "v7", datasourceId: "warehouse-test" }));
  });

  it("edits and enables versioned cases, then reviews run evidence without result values", async () => {
    const draftCase = { ...caseRecord, status: "draft" as const };
    const updateCase = vi.spyOn(api, "updateRegressionCase").mockResolvedValue({ ...caseRecord, revision: 4, status: "enabled" });
    const listDatasets = vi.spyOn(api, "listTestDatasets").mockResolvedValue({ schemaVersion: 1, items: [activeDataset] });
    vi.spyOn(api, "listRegressionCases").mockResolvedValueOnce({ schemaVersion: 2, items: [draftCase] }).mockResolvedValue({ schemaVersion: 2, items: [caseRecord] });
    vi.spyOn(api, "listRegressionRuns").mockResolvedValue({ schemaVersion: 1, items: [run] });
    const getTrace = vi.spyOn(api, "getTrace").mockImplementation(async (id) => ({ ...(persistedTrace as AgentTraceDetail), id }));
    const createRun = vi.spyOn(api, "createRegressionRun").mockResolvedValue(run);
    vi.spyOn(api, "getRegressionRun").mockResolvedValue(run);
    const coreDiagnostic = vi.spyOn(api, "getCoreDiagnostic").mockResolvedValue({
      schemaVersion: 1, traceId: "trace_core_regression_42", method: "query.run", status: "failure", stage: "execution", durationMs: 28,
      phaseSpans: [{ name: "execution", status: "failure", durationMs: 28 }], code: "QUERY_FAILED", reasonCode: "ROW_COUNT_MISMATCH", phase: "execution",
      semanticVersion: "semantic-13", policyVersions: [], issueIds: ["feedback_42"], createdAt: run.createdAt, expiresAt: run.expiresAt,
    });
    const onOpenIssue = vi.fn(); const onOpenTrace = vi.fn();

    render(<RegressionWorkbench locale="en-US" adminToken="admin" activeDatasourceId="warehouse-test" onOpenIssue={onOpenIssue} onOpenTrace={onOpenTrace} />);
    await screen.findByText("Count active users");
    const caseCard = screen.getByText("Count active users").closest<HTMLElement>(".regression-v2-card");
    expect(caseCard).not.toBeNull();
    fireEvent.click(within(caseCard!).getByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(updateCase).toHaveBeenCalledWith("case-42", expect.objectContaining({ expectedRevision: 3, enable: true })));

    const selection = screen.getByRole("checkbox", { name: /Select enabled cases case-42/ });
    fireEvent.click(selection);
    fireEvent.click(screen.getByRole("button", { name: "Run selected" }));
    await waitFor(() => expect(createRun).toHaveBeenCalledWith(["case-42"]));
    expect(await screen.findAllByText("ROW_COUNT_MISMATCH")).toHaveLength(2);
    expect(screen.getByText("Not collected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open source issue" }));
    const originalTraceButtons = screen.getAllByRole("button", { name: "Open original trace" });
    fireEvent.click(originalTraceButtons[originalTraceButtons.length - 1]!);
    expect(onOpenIssue).toHaveBeenCalledWith("feedback_42");
    await waitFor(() => {
      expect(onOpenTrace).toHaveBeenCalledWith("atr_fixture");
      expect(getTrace).toHaveBeenCalledWith("atr_fixture");
    });
    expect(screen.queryByText(/actual cell value/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "trace_core_regression_42" }));
    expect(await screen.findByText("QUERY_FAILED")).toBeInTheDocument();
    await waitFor(() => expect(coreDiagnostic).toHaveBeenCalledWith("trace_core_regression_42"));
    expect(listDatasets).toHaveBeenCalled();
    coreDiagnostic.mockRejectedValueOnce(new ApiClientError("Core diagnostic was not found", 404,
      { code: "DIAGNOSTIC_NOT_FOUND" }));
    fireEvent.click(screen.getByRole("button", { name: "trace_core_regression_42" }));
    expect(await screen.findByText("The Core diagnostic is missing or expired in this project.")).toBeInTheDocument();
  });
});
