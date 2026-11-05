import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import EvaluationWorkbench from "./EvaluationWorkbench";
import { EvaluationClient } from "../api/evaluations";
import { api, ApiClientError } from "../api/client";
import persistedRun from "./fixtures/evaluation-run-v1.json";

const suite = { id: "suite-hr", revision: 1, status: "published", name: "HR checks", testDatasetId: "hr", cases: [{ id: "headcount", question: "How many employees?", role: "hr", expectedBehavior: "answer" }] };
const profile = { id: "profile-test", revision: 1, status: "published", name: "Test Agent", model: "configured-model", reasoningEffort: "medium", prompt: "Use tools", contextVersion: 1, timeoutSeconds: 300, rounds: 1, concurrency: 2 };

describe("EvaluationWorkbench", () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
  function setup(overrides: Record<string, unknown> = {}) {
    return vi.spyOn(EvaluationClient.prototype, "request").mockImplementation(async function(path: string, method = "GET") {
      const values: Record<string, unknown> = {
        "/settings": { enabled: true, testDataOnly: true }, "/suites": { items: [suite] }, "/profiles": { items: [profile] }, "/workers": { items: [{ id: "runner-1", online: true, roles: ["hr"] }] }, "/datasets": { items: [{ id: "hr", version: "hr-v1", datasourceId: "test" }] }, "/runs": { items: [persistedRun] }, "/runs/run-persisted": persistedRun, "/preflight?suiteId=suite-hr&profileId=profile-test": { ready: true, reasons: [], environment: { semanticVersion: "v1" }, total: 1, model: "configured-model" },
        "/attempts/attempt-a/evidence": { finalAnswer: "Observed answer", finalResult: { columns: ["count"], rows: [[42]] } }, ...overrides,
      };
      if (method === "POST" && path === "/runs") return persistedRun;
      if (method === "POST" && path === "/suites") return { ...suite, status: "draft" };
      if (method === "POST" && path.endsWith("/review")) return { status: "passed" };
      if (!(path in values)) throw new Error(`Unexpected ${method} ${path}`);
      return values[path];
    });
  }
  it("requires project administration and does not fetch when locked", () => {
    const request = setup(); render(<EvaluationWorkbench locale="en-US" adminToken="" />);
    expect(screen.getByText("Console administrator authentication required")).toBeInTheDocument(); expect(request).not.toHaveBeenCalled();
  });
  it("previews exact attempt count before starting a real run", async () => {
    const request = setup(); render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByRole("option", { name: "HR checks · v1" });
    fireEvent.change(screen.getByLabelText("Published suite"), { target: { value: "suite-hr" } });
    fireEvent.change(screen.getByLabelText("Published Agent profile"), { target: { value: "profile-test" } });
    expect(screen.queryByRole("button", { name: "Run evaluation" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Preview evaluation" }));
    expect(await screen.findByText("Review execution plan")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run evaluation" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/runs", "POST", { suiteId: "suite-hr", profileId: "profile-test" }));
    expect(await screen.findByText("Frozen environment")).toBeInTheDocument();
  });
  it("keeps run disabled when no runner is registered", async () => {
    setup({ "/workers": { items: [] } }); render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByText(/No runner registered/);
    expect(screen.getByRole("button", { name: "Preview evaluation" })).toBeDisabled();
  });
  it("does not dispatch when server preflight detects environment drift", async () => {
    const request = setup({ "/preflight?suiteId=suite-hr&profileId=profile-test": { ready: false, reasons: ["Test dataset no longer matches"], environment: null, total: 1, model: "configured-model" } });
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByRole("option", { name: "HR checks · v1" });
    fireEvent.change(screen.getByLabelText("Published suite"), { target: { value: "suite-hr" } });
    fireEvent.change(screen.getByLabelText("Published Agent profile"), { target: { value: "profile-test" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview evaluation" }));
    expect(await screen.findByText("Test dataset no longer matches")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run evaluation" })).toBeDisabled();
    expect(request).not.toHaveBeenCalledWith("/runs", "POST", expect.anything());
  });
  it("does not count stale runners as available", async () => {
    setup({ "/workers": { items: [{ id: "stale", online: false, roles: ["hr"] }] } });
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByText(/No runner registered/);
    expect(screen.getByRole("button", { name: "Preview evaluation" })).toBeDisabled();
  });
  it("reconstructs persisted scores after remount and saves manual review separately", async () => {
    const request = setup(); const core = vi.fn();
    vi.spyOn(api, "getCoreDiagnostic").mockResolvedValue({ traceId: "trace-core-a" } as Awaited<ReturnType<typeof api.getCoreDiagnostic>>);
    const view = render(<EvaluationWorkbench locale="en-US" adminToken="admin" onOpenCore={core} />);
    fireEvent.click(await screen.findByRole("button", { name: /run-persisted/ }));
    await screen.findByText("Frozen environment"); view.unmount();
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" onOpenCore={core} />);
    fireEvent.click(await screen.findByRole("button", { name: /run-persisted/ }));
    fireEvent.click((await screen.findAllByRole("button", { name: "Inspect" }))[0]);
    expect(await screen.findByText(/Observed answer/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Core diagnostic: trace-core-a/ })); await waitFor(() => expect(core).toHaveBeenCalledWith("trace-core-a"));
    fireEvent.change(screen.getByLabelText("Human review"), { target: { value: "passed" } });
    fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Answer includes required date" } });
    fireEvent.click(screen.getByRole("button", { name: "Save review" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/attempts/attempt-a/review", "POST", { status: "passed", reason: "Answer includes required date" }));
    expect(persistedRun.attempts[0].score.dimensions?.finalAnswer).toBe("pending");
  });
  it("creates a suite draft using editable case fields", async () => {
    const request = setup(); request.mockImplementationOnce(async () => ({ items: [suite] }));
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByRole("option", { name: "HR checks · v1" }); fireEvent.click(screen.getByRole("button", { name: "Test suites" }));
    fireEvent.change(screen.getByLabelText(/^Suite name/), { target: { value: "New HR" } });
    fireEvent.change(screen.getByLabelText(/^Test dataset/), { target: { value: "hr" } });
    fireEvent.click(screen.getByRole("button", { name: "Add case" }));
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Count staff" } });
    fireEvent.change(screen.getByLabelText("Test role"), { target: { value: "employee" } });
    fireEvent.click(screen.getByRole("button", { name: "Save suite draft" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/suites", "POST", expect.objectContaining({ name: "New HR", cases: [expect.objectContaining({ question: "Count staff", role: "employee" })] })));
  });
  it("labels differing environments as exploratory", async () => {
    const request = setup({ "/runs": { items: [persistedRun, { ...persistedRun, id: "candidate" }] } });
    request.mockImplementation(async (path: string) => {
      const values: Record<string, unknown> = { "/settings": { enabled: true, testDataOnly: true }, "/suites": { items: [suite] }, "/profiles": { items: [profile] }, "/workers": { items: [] }, "/datasets": { items: [] }, "/runs": { items: [persistedRun, { ...persistedRun, id: "candidate" }] }, "/compare?baselineId=run-persisted&candidateId=candidate": { mode: "exploratory", reasons: ["datasetVersion changed"] } }; return values[path];
    });
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByRole("button", { name: /run-persisted/ }); fireEvent.click(screen.getByRole("button", { name: "Baseline comparison" }));
    fireEvent.change(screen.getByLabelText("Baseline run"), { target: { value: "run-persisted" } }); fireEvent.change(screen.getByLabelText("Candidate run"), { target: { value: "candidate" } }); fireEvent.click(screen.getByRole("button", { name: "Compare runs" }));
    expect(await screen.findByText("Exploratory comparison — conditions differ")).toBeInTheDocument();
  });
  it("reruns failed cases in a new task bound to the historical snapshots", async () => {
    const request = setup({ "/runs/run-persisted/rerun": { ...persistedRun, id: "new-run", sourceRunId: "run-persisted" } });
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    fireEvent.click(await screen.findByRole("button", { name: /run-persisted/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Rerun failed cases as new run" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/runs/run-persisted/rerun", "POST", { caseIds: ["salary"] }));
    expect(await screen.findByText("new-run")).toBeInTheDocument();
    expect(request).not.toHaveBeenCalledWith("/runs", "POST", expect.anything());
  });
  it("retains scores and review guidance when detailed evidence has expired", async () => {
    const request = setup();
    const original = request.getMockImplementation()!;
    request.mockImplementation(async function(this: EvaluationClient, path, ...rest) {
      if (path === "/attempts/attempt-a/evidence") throw new ApiClientError("expired", 410, { code: "EVIDENCE_UNAVAILABLE" });
      return original.call(this, path, ...rest);
    });
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    fireEvent.click(await screen.findByRole("button", { name: /run-persisted/ }));
    expect(await screen.findByText(/Original task instructions/)).toBeInTheDocument();
    fireEvent.click((await screen.findAllByRole("button", { name: "Inspect" }))[0]);
    expect(await screen.findByText("Mention the original snapshot date")).toBeInTheDocument();
    expect(screen.getByText("Evidence unavailable or expired. Scores remain available.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("Observed tool calls").nextElementSibling).toHaveTextContent("Unavailable");
    expect(screen.getByRole("button", { name: "Save review" })).toBeDisabled();
    expect(screen.getByText("50.0%")).toBeInTheDocument();
  });
  it("keeps review history and clears a different case's Core diagnostic", async () => {
    const request = setup({ "/attempts/attempt-b/evidence": { evidence: { finalAnswer: "Second answer" } } });
    const original = request.getMockImplementation()!;
    request.mockImplementation(async function(this: EvaluationClient, path, method, body) {
      if (path.endsWith("/review")) return { status: "passed", reason: "Reviewed", history: [{ status: "passed", reason: "Reviewed", reviewedBy: "reviewer", reviewedAt: "2026-11-05" }] };
      return original.call(this, path, method, body);
    });
    vi.spyOn(api, "getCoreDiagnostic").mockResolvedValue({ traceId: "UNIQUE-CORE-DETAIL" } as Awaited<ReturnType<typeof api.getCoreDiagnostic>>);
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    fireEvent.click(await screen.findByRole("button", { name: /run-persisted/ }));
    fireEvent.click((await screen.findAllByRole("button", { name: "Inspect" }))[0]);
    fireEvent.click(await screen.findByRole("button", { name: /Core diagnostic: trace-core-a/ }));
    expect(await screen.findByText(/UNIQUE-CORE-DETAIL/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Reviewed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save review" }));
    expect(await screen.findByText("Review history")).toBeInTheDocument();
    expect(screen.getByText(/Reviewed · reviewer/)).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Inspect" })[1]);
    expect(await screen.findByText(/Second answer/)).toBeInTheDocument();
    expect(screen.queryByText(/UNIQUE-CORE-DETAIL/)).not.toBeInTheDocument();
  });
  it("imports versioned suites as drafts and exports the saved server version", async () => {
    const bundle = { schemaVersion: 1, suite: { name: "Imported", testDatasetId: "hr", cases: suite.cases } };
    const request = setup({ "/suites/import": { ...suite, name: "Imported", status: "draft" }, "/suites/suite-hr/export": bundle });
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:test"), revokeObjectURL: vi.fn() }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByRole("option", { name: "HR checks · v1" }); fireEvent.click(screen.getByRole("button", { name: "Test suites" }));
    fireEvent.change(screen.getByLabelText("Suite JSON"), { target: { value: JSON.stringify(bundle) } });
    fireEvent.click(screen.getByRole("button", { name: "Import as draft" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/suites/import", "POST", bundle));
    expect(await screen.findByLabelText(/^Suite name/)).toHaveValue("Imported");
    expect(screen.getByRole("button", { name: "Publish saved suite" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Export suite" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/suites/suite-hr/export"));
    expect(URL.createObjectURL).toHaveBeenCalled();
  });
  it("ignores an older in-flight poll after a different run is selected", async () => {
    const running = { ...persistedRun, status: "running" };
    const candidate = { ...persistedRun, id: "candidate" };
    const request = setup({ "/runs": { items: [running, candidate] }, "/runs/run-persisted": running, "/runs/candidate": candidate });
    const original = request.getMockImplementation()!;
    let calls = 0;
    let resolvePoll!: (run: typeof running) => void;
    request.mockImplementation(async function(this: EvaluationClient, path, method, body) {
      if (path === "/runs/run-persisted" && ++calls > 1) return await new Promise<typeof running>(resolve => { resolvePoll = resolve; });
      return original.call(this, path, method, body);
    });
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    const button = await screen.findByRole("button", { name: /run-persisted/ });
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(button); });
    expect(screen.getByText("run-persisted", { selector: "code" })).toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(2500); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /candidate/ })); });
    expect(screen.getByText("candidate", { selector: "code" })).toBeInTheDocument();
    await act(async () => { resolvePoll(running); });
    expect(screen.getByText("candidate", { selector: "code" })).toBeInTheDocument();
    expect(screen.queryByText("run-persisted", { selector: "code" })).not.toBeInTheDocument();
  });
  it("imports and exports an Agent profile as a versioned draft envelope", async () => {
    const bundle = { schemaVersion: 1, profile: { name: "Imported Agent", model: "explicit-new-model", prompt: "Use MCP", reasoningEffort: "high", contextVersion: 2, timeoutSeconds: 300, concurrency: 2, rounds: 3 } };
    const request = setup({ "/profiles/import": { ...profile, ...bundle.profile, status: "draft" }, "/profiles/profile-test/export": bundle });
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:profile"), revokeObjectURL: vi.fn() }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByRole("option", { name: "HR checks · v1" });
    fireEvent.click(screen.getByRole("button", { name: "Agent profiles" }));
    fireEvent.change(screen.getByLabelText("Profile JSON"), { target: { value: JSON.stringify(bundle) } });
    fireEvent.click(screen.getByRole("button", { name: "Import profile as draft" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/profiles/import", "POST", bundle));
    expect(await screen.findByLabelText("Profile name")).toHaveValue("Imported Agent");
    expect(screen.getByLabelText("Model (explicit selection)")).toHaveValue("explicit-new-model");
    expect(screen.getByRole("button", { name: "Publish saved profile" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Export Agent profile" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/profiles/profile-test/export"));
    expect(URL.createObjectURL).toHaveBeenCalled();
  });
  it("shows zero only for observed empty tool calls and clears metrics when unavailable", async () => {
    setup({ "/attempts/attempt-a/evidence": { evidence: { toolCalls: [], finalAnswer: "No tools observed" } }, "/attempts/attempt-b/evidence": { evidence: { finalAnswer: "Calls not captured" } } });
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    fireEvent.click(await screen.findByRole("button", { name: /run-persisted/ }));
    fireEvent.click((await screen.findAllByRole("button", { name: "Inspect" }))[0]);
    await screen.findByText(/No tools observed/);
    expect(screen.getByText("Observed tool calls").nextElementSibling).toHaveTextContent("0");
    expect(screen.getByText("Observed duration").nextElementSibling).toHaveTextContent("100 ms");
    expect(screen.getByText("Observed token usage").nextElementSibling).toHaveTextContent("Not collected");
    fireEvent.click(screen.getAllByRole("button", { name: "Inspect" })[1]);
    await screen.findByText(/Calls not captured/);
    expect(screen.getByText("Observed tool calls").nextElementSibling).toHaveTextContent("Unavailable");
    expect(screen.getByText("Observed duration").nextElementSibling).toHaveTextContent("210 ms");
  });
  it("registers a dataset scorer identity and reconstructs its binding after reload", async () => {
    const request = setup();
    const original = request.getMockImplementation()!;
    let dataset = { id: "hr", version: "hr-v1", datasourceId: "test", scorerSubject: "old-subject", scorerBindingValid: true };
    request.mockImplementation(async function(this: EvaluationClient, path, method, body) {
      if (path === "/datasets") return { items: [dataset] };
      if (path === "/datasets/hr/scorer" && method === "PUT") {
        dataset = { ...dataset, scorerSubject: (body as { subjectId: string }).subjectId };
        return dataset;
      }
      return original.call(this, path, method, body);
    });
    const view = render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByRole("option", { name: "HR checks · v1" });
    fireEvent.change(screen.getByLabelText("Dataset for reference scoring"), { target: { value: "hr" } });
    expect(screen.getByLabelText("Managed scorer subject ID")).toHaveValue("old-subject");
    fireEvent.change(screen.getByLabelText("Managed scorer subject ID"), { target: { value: "  subject-scorer  " } });
    fireEvent.click(screen.getByRole("button", { name: "Register scorer identity" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/datasets/hr/scorer", "PUT", { subjectId: "subject-scorer" }));
    expect(await screen.findByText("Scorer identity registered")).toBeInTheDocument();
    expect(screen.getByText("Registered scorer: subject-scorer")).toBeInTheDocument();
    view.unmount();
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByRole("option", { name: "HR checks · v1" });
    fireEvent.change(screen.getByLabelText("Dataset for reference scoring"), { target: { value: "hr" } });
    expect(screen.getByLabelText("Managed scorer subject ID")).toHaveValue("subject-scorer");
    expect(screen.queryByLabelText(/password|bearer|credential/i)).not.toBeInTheDocument();
  });
  it("identifies a stale scorer binding and requires an identity before saving", async () => {
    setup({ "/datasets": { items: [{ id: "hr", version: "hr-v2", datasourceId: "test", scorerSubject: "old-subject", scorerBindingValid: false }] } });
    render(<EvaluationWorkbench locale="en-US" adminToken="admin" />);
    await screen.findByRole("option", { name: "HR checks · v1" });
    expect(screen.getByRole("button", { name: "Register scorer identity" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Dataset for reference scoring"), { target: { value: "hr" } });
    expect(screen.getByText("Scorer registration is out of date")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Managed scorer subject ID"), { target: { value: " " } });
    expect(screen.getByRole("button", { name: "Register scorer identity" })).toBeDisabled();
  });
});
