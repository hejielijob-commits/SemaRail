import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import "../i18n";
import type { DiagnosticFeedback } from "../types";
import resolutionFixture from "./fixtures/feedback-resolution-v1.json";
import salaryDefaulted from "./fixtures/business-evidence-ui03-hr-salary-defaulted-v1.json";
import salaryConfirmed from "./fixtures/business-evidence-ui03-hr-salary-confirmed-v1.json";
import performanceConfirmed from "./fixtures/business-evidence-ui03-hr-performance-confirmed-v1.json";
import performanceSqlObserved from "./fixtures/business-evidence-ui03-hr-performance-sql-observed-v1.json";
import FeedbackResolutionPanel from "./FeedbackResolutionPanel";

const feedback: DiagnosticFeedback = {
  id: "fb_hr_fixture", diagnosticId: "diag_hr", traceId: "trace_hr", queryId: "query-business-trust-1",
  subjectId: "subject_hr", transport: "core-http", method: "query.run", stage: "complete", durationMs: 10,
  policyVersions: [], category: "knowledge_gap", description: "Synthetic HR snapshot presentation fixture.", status: "fixed",
  evidence: { source: "client", contentPurgedAt: null }, createdAt: "2026-10-12T02:00:00Z",
  diagnosticCreatedAt: "2026-10-12T02:00:00Z", updatedAt: "2026-10-12T02:00:00Z",
};

function useEvidenceResponse(evidence: unknown) {
  vi.spyOn(api, "getFeedbackResolution").mockResolvedValue(resolutionFixture as never);
  vi.spyOn(api, "getVersions").mockResolvedValue([]);
  vi.spyOn(api, "listRegressionCases").mockResolvedValue({ schemaVersion: 2, items: [] });
  vi.spyOn(api, "listRegressionRuns").mockResolvedValue({ schemaVersion: 1, items: [] });
  // Keep getQueryEvidence real: exercise its request, schema parser and query-ID check.
  const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify(evidence), {
    status: 200, headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const cases = [
  { name: "defaulted salary", fixture: salaryDefaulted, model: "compensation_history", column: "monthly_salary", value: "2024-09-01", source: ["Published default", "已发布默认值"], state: ["Defaulted", "已应用默认值"] },
  { name: "confirmed salary", fixture: salaryConfirmed, model: "compensation_history", column: "monthly_salary", value: "2024-09-01", source: ["Confirmed by user", "用户已确认"], state: ["Confirmed", "已确认"] },
  { name: "confirmed performance", fixture: performanceConfirmed, model: "performance_reviews", column: "performance_score", value: "2024-07-01 – 2024-09-30", source: ["Confirmed by user", "用户已确认"], state: ["Confirmed", "已确认"] },
  { name: "SQL-observed performance", fixture: performanceSqlObserved, model: "performance_reviews", column: "performance_score", value: "2024-07-01 – 2024-09-30", source: ["Observed in SQL", "SQL 中观察到"], state: ["Missing", "缺失"] },
];

describe("Core-generated HR query evidence through the REST parser", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  for (const locale of ["en-US", "zh-CN"] as const) {
    const language = locale === "en-US" ? 0 : 1;
    it.each(cases)(`renders $name without inventing a unit in ${locale}`, async (testCase) => {
      const fetchMock = useEvidenceResponse(testCase.fixture);
      const { container } = render(<FeedbackResolutionPanel locale={locale} feedback={feedback} adminToken="fixture-admin" />);
      expect(await screen.findByText(testCase.source[language])).toBeInTheDocument();
      const section = container.querySelector(".business-query-evidence");
      expect(section).toHaveTextContent(testCase.value);
      expect(section).toHaveTextContent(locale === "en-US" ? "Undefined · Unverified" : "未定义 · 未验证");
      expect(section).toHaveTextContent(`${testCase.model}.${testCase.column}`);
      expect(section).toHaveTextContent(locale === "en-US" ? "Average · Not distinct" : "平均值 · 不去重");
      expect(section).toHaveTextContent(`${testCase.model}.${testCase.column} · employee`);
      expect(section).toHaveTextContent(locale === "en-US" ? "Not applicable" : "不适用");
      expect(section?.querySelector(".business-check-list")).toHaveTextContent(locale === "en-US" ? "Required" : "强制验证");
      const confirmations = [...(section?.querySelectorAll(".business-evidence-meta > div") ?? [])]
        .find((item) => item.querySelector("dt")?.textContent === (locale === "en-US" ? "Confirmations" : "确认项"));
      expect(confirmations).toHaveTextContent(testCase.state[language]);
      if (testCase.fixture === performanceSqlObserved) {
        expect(confirmations).toHaveTextContent("reviewPeriod");
        expect(section?.querySelector(".business-check-list")).toHaveTextContent(locale === "en-US" ? "Unverified" : "未验证");
        expect(screen.queryByText(locale === "en-US" ? "Confirmed by user" : "用户已确认")).not.toBeInTheDocument();
      } else {
        const conditions = [...(section?.querySelectorAll(".business-evidence-meta > div") ?? [])]
          .find((item) => item.querySelector("dt")?.textContent === (locale === "en-US" ? "Conditions" : "条件"));
        const trend = [...(conditions?.querySelectorAll(".business-evidence-items > span") ?? [])]
          .find((item) => item.querySelector("code")?.textContent === "trend");
        expect(trend).toHaveTextContent(locale === "en-US" ? "trend: No Confirmed" : "trend: 否 已确认");
      }
      expect(container.textContent).not.toMatch(/\b(?:USD|CNY|RMB)\b|人民币|美元|SELECT AVG/);
      expect(fetchMock).toHaveBeenCalledWith("/api/v1/diagnostics/queries/query-business-trust-1/evidence", expect.anything());
    });
  }

  it("reconstructs the same Core-generated default after refresh and remount through real parsing", async () => {
    const fetchMock = useEvidenceResponse(salaryDefaulted);
    const mount = render(<FeedbackResolutionPanel locale="zh-CN" feedback={feedback} adminToken="fixture-admin" />);
    expect(await screen.findByText("已发布默认值")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新证据" }));
    expect(await screen.findByText("已发布默认值")).toBeInTheDocument();
    mount.unmount();
    render(<FeedbackResolutionPanel locale="zh-CN" feedback={feedback} adminToken="fixture-admin" />);
    expect(await screen.findByText("已发布默认值")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
