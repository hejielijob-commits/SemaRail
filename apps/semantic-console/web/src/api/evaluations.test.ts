import { afterEach, describe, expect, it, vi } from "vitest";
import { EvaluationClient } from "./evaluations";

describe("EvaluationClient", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("keeps the credential in a header and preserves versioned JSON", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ schemaVersion: 1, items: [] }) });
    vi.stubGlobal("fetch", fetch);
    await new EvaluationClient("private-token").request("/suites/import", "POST", { schemaVersion: 1, suite: { name: "test" } });
    expect(fetch).toHaveBeenCalledWith(expect.not.stringContaining("private-token"), expect.objectContaining({ headers: expect.objectContaining({ Authorization: "Bearer private-token" }), body: JSON.stringify({ schemaVersion: 1, suite: { name: "test" } }) }));
  });
  it("rejects unknown response schema versions", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ schemaVersion: 2, items: [] }) }));
    await expect(new EvaluationClient("token").list("suites")).rejects.toThrow("Unsupported evaluation schema");
  });
  it("preserves expiry status for evidence-specific UI handling", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 410, json: async () => ({ code: "EVIDENCE_UNAVAILABLE", message: "expired" }) }));
    await expect(new EvaluationClient("token").request("/attempts/a/evidence")).rejects.toMatchObject({ status: 410, code: "EVIDENCE_UNAVAILABLE" });
  });
  it("rejects malformed JSON and missing collection items before rendering", async () => {
    const fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => null }).mockResolvedValueOnce({ ok: true, json: async () => ({ schemaVersion: 1 }) });
    vi.stubGlobal("fetch", fetch);
    await expect(new EvaluationClient("token").list("suites")).rejects.toThrow("Invalid evaluation response");
    await expect(new EvaluationClient("token").list("suites")).rejects.toThrow("Invalid evaluation collection");
  });
  it("registers only a managed subject ID for the selected dataset", async () => {
    const dataset = { id: "dataset-a", version: "v1", datasourceId: "test", scorerSubject: "subject-scorer" };
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => dataset });
    vi.stubGlobal("fetch", fetch);
    await expect(new EvaluationClient("admin-token").setDatasetScorer("dataset-a", "subject-scorer")).resolves.toEqual(dataset);
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/datasets/dataset-a/scorer"), expect.objectContaining({ method: "PUT", body: JSON.stringify({ subjectId: "subject-scorer" }) }));
  });
});
