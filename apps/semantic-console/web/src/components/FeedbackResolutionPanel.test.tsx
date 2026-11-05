import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, api } from "../api/client";
import "../i18n";
import type { DiagnosticFeedback } from "../types";
import resolutionFixture from "./fixtures/feedback-resolution-v1.json";
import evidenceFixture from "./fixtures/business-evidence-v1.json";
import confirmedEvidenceFixture from "./fixtures/business-evidence-ui03-confirmed-v1.json";
import defaultedEvidenceFixture from "./fixtures/business-evidence-ui03-defaulted-v1.json";
import sqlObservedEvidenceFixture from "./fixtures/business-evidence-ui03-sql-observed-v1.json";
import FeedbackResolutionPanel from "./FeedbackResolutionPanel";

const feedback: DiagnosticFeedback = {
  id: "fb_business_trust_1", diagnosticId: "diag_1", traceId: "trace_1", queryId: "query-business-trust-1",
  subjectId: "subject_1", transport: "core-http", method: "query.run", stage: "complete", durationMs: 10,
  policyVersions: [], category: "sql_generation", description: "Revenue excludes voided orders.", status: "fixed",
  evidence: { source: "client", contentPurgedAt: null }, createdAt: "2026-10-12T02:00:00Z",
  diagnosticCreatedAt: "2026-10-12T02:00:00Z", updatedAt: "2026-10-12T02:00:00Z",
};

function mockPanelApis(evidence: unknown) {
  vi.spyOn(api, "getFeedbackResolution").mockResolvedValue(resolutionFixture as never);
  vi.spyOn(api, "getVersions").mockResolvedValue([]);
  vi.spyOn(api, "listRegressionCases").mockResolvedValue({ schemaVersion: 2, items: [] });
  vi.spyOn(api, "listRegressionRuns").mockResolvedValue({ schemaVersion: 1, items: [] });
  return vi.spyOn(api, "getQueryEvidence").mockResolvedValue(evidence as never);
}

describe("FeedbackResolutionPanel", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it("reconstructs saved resolution and safe query context from durable API responses", async () => {
    vi.spyOn(api, "getFeedbackResolution").mockResolvedValue(resolutionFixture as never);
    vi.spyOn(api, "getVersions").mockResolvedValue([
      { id: "version-after", revision: "sha256:published-v4", createdAt: "2026-10-12T02:00:00Z" },
      { id: "version-before", revision: "sha256:published-v3", createdAt: "2026-10-11T02:00:00Z" },
    ]);
    vi.spyOn(api, "listRegressionCases").mockResolvedValue({ schemaVersion: 2, items: [] });
    vi.spyOn(api, "listRegressionRuns").mockResolvedValue({ schemaVersion: 1, items: [] });
    const getEvidence = vi.spyOn(api, "getQueryEvidence").mockResolvedValue(evidenceFixture as never);

    const { container } = render(<FeedbackResolutionPanel locale="en-US" feedback={feedback} adminToken="console-admin" />);

    expect(await screen.findByText("Query evidence")).toBeInTheDocument();
    expect(screen.getByText("Verified")).toBeInTheDocument();
    expect(screen.getAllByText("sha256:published-v4").length).toBeGreaterThan(0);
    expect(screen.getByText("order, day")).toBeInTheDocument();
    expect(screen.getByText("Undefined · Verified")).toBeInTheDocument();
    expect(screen.getByText("rule-trusted-revenue · orders · net_amount")).toBeInTheDocument();
    expect(screen.queryByText(/sensitive prompt answer|SELECT secret_column/)).not.toBeInTheDocument();
    expect(container.textContent).not.toContain("sensitive prompt answer");
    expect(getEvidence).toHaveBeenCalledWith("query-business-trust-1");
  });

  it("renders the same evidence workbench in Chinese", async () => {
    vi.spyOn(api, "getFeedbackResolution").mockResolvedValue(resolutionFixture as never);
    vi.spyOn(api, "getVersions").mockResolvedValue([]);
    vi.spyOn(api, "listRegressionCases").mockResolvedValue({ schemaVersion: 2, items: [] });
    vi.spyOn(api, "listRegressionRuns").mockResolvedValue({ schemaVersion: 1, items: [] });
    vi.spyOn(api, "getQueryEvidence").mockResolvedValue(evidenceFixture as never);

    render(<FeedbackResolutionPanel locale="zh-CN" feedback={feedback} adminToken="console-admin" />);

    expect(await screen.findByText("查询证据")).toBeInTheDocument();
    expect(screen.getByText("已验证")).toBeInTheDocument();
    expect(screen.getByText("评测批次 ID")).toBeInTheDocument();
  });

  it("shows unsupported evidence and reference-load failures and does not treat the legacy verified label as proof", async () => {
    vi.spyOn(api, "getFeedbackResolution").mockResolvedValue(resolutionFixture as never);
    vi.spyOn(api, "getVersions").mockRejectedValue(new Error("version API unavailable"));
    vi.spyOn(api, "listRegressionCases").mockRejectedValue(new Error("case API unavailable"));
    vi.spyOn(api, "listRegressionRuns").mockRejectedValue(new Error("run API unavailable"));
    vi.spyOn(api, "getQueryEvidence").mockRejectedValue(new ApiClientError("unsupported", 502, { code: "UNSUPPORTED_BUSINESS_EVIDENCE_VERSION" }));

    render(<FeedbackResolutionPanel locale="en-US" feedback={{ ...feedback, status: "verified" }} adminToken="console-admin" />);

    expect(await screen.findByText("Query evidence uses an unsupported or invalid schema.")).toBeInTheDocument();
    expect(screen.getByText("Published versions could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("Regression case revisions could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("SQL regression runs could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/historical label does not establish this evidence chain/)).toBeInTheDocument();
  });

  it("clears previously loaded evidence when the selected feedback changes and its resolution cannot load", async () => {
    vi.spyOn(api, "getFeedbackResolution")
      .mockResolvedValueOnce(resolutionFixture as never)
      .mockRejectedValueOnce(new Error("resolution unavailable"));
    vi.spyOn(api, "getVersions").mockResolvedValue([]);
    vi.spyOn(api, "listRegressionCases").mockResolvedValue({ schemaVersion: 2, items: [] });
    vi.spyOn(api, "listRegressionRuns").mockResolvedValue({ schemaVersion: 1, items: [] });
    vi.spyOn(api, "getQueryEvidence").mockResolvedValue(evidenceFixture as never);

    const { rerender } = render(<FeedbackResolutionPanel locale="en-US" feedback={feedback} adminToken="console-admin" />);
    expect(await screen.findByText("query-business-trust-1")).toBeInTheDocument();

    rerender(<FeedbackResolutionPanel locale="en-US" feedback={{ ...feedback, id: "fb_business_trust_2", queryId: "query-business-trust-2" }} adminToken="console-admin" />);

    expect(await screen.findByText("Query evidence is expired or unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("query-business-trust-1")).not.toBeInTheDocument();
  });

  it.each([
    {
      locale: "en-US" as const,
      title: "Query evidence",
      source: "Confirmed by user",
      unit: "USD · Verified",
      metric: "rule-revenue · orders.net_amount · Average · Not distinct",
      grain: "orders.created_at · day",
      confirmed: "Confirmed",
      defaulted: "Defaulted",
      missing: "Missing",
      required: "Required",
      statuses: ["Passed", "Conflict", "Unverified", "Stale"],
      range: "2026-10-01 – 2026-10-31",
      booleanValue: "Yes",
    },
    {
      locale: "zh-CN" as const,
      title: "查询证据",
      source: "用户已确认",
      unit: "USD · 已验证",
      metric: "rule-revenue · orders.net_amount · 平均值 · 不去重",
      grain: "orders.created_at · day",
      confirmed: "已确认",
      defaulted: "已应用默认值",
      missing: "缺失",
      required: "强制验证",
      statuses: ["通过", "冲突", "未验证", "版本失效"],
      range: "2026-10-01 – 2026-10-31",
      booleanValue: "是",
    },
  ])("renders bounded date, unit, metric, grain and check evidence in $locale", async (expected) => {
    mockPanelApis(confirmedEvidenceFixture);

    const { container } = render(<FeedbackResolutionPanel locale={expected.locale} feedback={feedback} adminToken="console-admin" />);
    const evidence = await screen.findByText(expected.title).then((title) => title.closest(".business-query-evidence"));

    expect(evidence).not.toBeNull();
    expect(screen.getByText(expected.source)).toBeInTheDocument();
    expect(screen.getByText(expected.unit)).toBeInTheDocument();
    expect(screen.getByText("2026-10-12")).toBeInTheDocument();
    expect(screen.getAllByText(/2026-10-01.*2026-10-31/).length).toBeGreaterThan(0);
    expect(screen.getByText(expected.metric)).toBeInTheDocument();
    expect(screen.getByText(expected.grain)).toBeInTheDocument();
    expect(evidence?.querySelector(".business-evidence-meta")).toHaveTextContent(expected.booleanValue);
    expect(screen.getAllByText(expected.confirmed).length).toBeGreaterThan(0);
    expect(screen.getAllByText(expected.defaulted).length).toBeGreaterThan(0);
    expect(screen.getAllByText(expected.missing).length).toBeGreaterThan(0);
    const evidenceChecks = evidence?.querySelector(".business-check-list");
    expect(evidenceChecks).toHaveTextContent(expected.required);
    for (const status of expected.statuses) expect(evidenceChecks).toHaveTextContent(status);
    expect(evidence?.querySelector(".business-evidence-meta")).toHaveTextContent(expected.locale === "en-US" ? "Value not displayed" : "不显示此值");
    expect(container.textContent).not.toContain("sensitive prompt answer");
    expect(container.textContent).not.toContain("SELECT secret_column");
  });

  it.each([
    { fixture: defaultedEvidenceFixture, locale: "en-US" as const, source: "Published default", value: "2026-10-01 – 2026-10-31", state: "Defaulted" },
    { fixture: defaultedEvidenceFixture, locale: "zh-CN" as const, source: "已发布默认值", value: "2026-10-01 – 2026-10-31", state: "已应用默认值" },
    { fixture: sqlObservedEvidenceFixture, locale: "en-US" as const, source: "Observed in SQL", value: "2026-10-15", state: "Missing" },
    { fixture: sqlObservedEvidenceFixture, locale: "zh-CN" as const, source: "SQL 中观察到", value: "2026-10-15", state: "缺失" },
  ])("labels $source as a separate source and confirmation state in $locale", async ({ fixture, locale, source, value, state }) => {
    mockPanelApis(fixture);

    render(<FeedbackResolutionPanel locale={locale} feedback={feedback} adminToken="console-admin" />);

    expect(await screen.findByText(source)).toBeInTheDocument();
    expect((await screen.findAllByText(value.includes(" – ") ? /2026-10-01.*2026-10-31/ : value)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(state).length).toBeGreaterThan(0);
    if (fixture === sqlObservedEvidenceFixture) {
      expect(screen.queryByText(locale === "en-US" ? "Confirmed by user" : "用户已确认")).not.toBeInTheDocument();
    }
  });

  it("keeps published default evidence visible after refresh and remount from durable API responses", async () => {
    const getEvidence = mockPanelApis(defaultedEvidenceFixture);

    const firstMount = render(<FeedbackResolutionPanel locale="en-US" feedback={feedback} adminToken="console-admin" />);
    expect(await screen.findByText("Published default")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh evidence" }));
    expect((await screen.findAllByText("Published default")).length).toBeGreaterThan(0);
    expect(getEvidence).toHaveBeenCalledTimes(2);

    firstMount.unmount();
    render(<FeedbackResolutionPanel locale="en-US" feedback={feedback} adminToken="console-admin" />);
    expect(await screen.findByText("Published default")).toBeInTheDocument();
    expect((await screen.findAllByText(/2026-10-01.*2026-10-31/)).length).toBeGreaterThan(0);
    expect(getEvidence).toHaveBeenCalledTimes(3);
  });
});
