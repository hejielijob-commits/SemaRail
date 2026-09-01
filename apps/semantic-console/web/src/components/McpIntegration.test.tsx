import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import McpIntegration from "./McpIntegration";
import type { McpIntegrationResponse } from "../types";

const integration: McpIntegrationResponse = {
  schemaVersion: 2,
  transport: "streamable-http",
  endpoint: { url: "https://mcp.example.test/mcp", authentication: "bearer" },
  authentication: { type: "bearer", acceptedCredentials: ["service_account_key", "employee_session"], tokenPlacement: "authorization_header" },
  readiness: { status: "ready", semanticContext: "ready", governedQuery: "ready", datasourceType: "postgres", endpointConfiguration: "ready" },
  tools: ["semarail_validate_project", "semarail_list_models", "semarail_get_context", "semarail_plan_query", "semarail_governed_query"],
  clientConfig: { mcpServers: { semarail: { url: "https://mcp.example.test/mcp", transport: "streamable-http", headers: { Authorization: "Bearer ${SEMARAIL_TOKEN}" } } } },
  trustedLocalOperator: { status: "compatibility_only", transport: "stdio", audience: "trusted_local_operator", userIsolation: false },
};

describe("McpIntegration", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
  });

  it("shows authenticated remote MCP and copies a secret-free token-placeholder config", async () => {
    render(<McpIntegration integration={integration} loading={false} error={null} locale="en-US" onRetry={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Authenticated remote MCP" })).toBeInTheDocument();
    expect(screen.getAllByText("https://mcp.example.test/mcp")).toHaveLength(2);
    expect(screen.getByText("Bearer authentication is required")).toBeInTheDocument();
    expect(screen.getByText("Service-account key")).toBeInTheDocument();
    expect(screen.getByText("Employee login session")).toBeInTheDocument();
    expect(screen.getByText("Show stdio compatibility notes")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy configuration" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(expect.stringContaining("${SEMARAIL_TOKEN}")));
    expect(navigator.clipboard.writeText).not.toHaveBeenCalledWith(expect.stringContaining("DATABASE_URL"));
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("explains the MySQL governed-query boundary in Chinese", () => {
    render(<McpIntegration integration={{ ...integration, readiness: { ...integration.readiness, status: "setup_required", governedQuery: "setup_required", datasourceType: "mysql" } }} loading={false} error={null} locale="zh-CN" onRetry={vi.fn()} />);

    expect(screen.getByText("受控执行目前仅支持 PostgreSQL")).toBeInTheDocument();
    expect(screen.getByText(/SemaRail Core 受控查询服务/)).toBeInTheDocument();
    expect(screen.getByText("必须使用 Bearer 认证")).toBeInTheDocument();
    expect(screen.getByText("查看 stdio 兼容说明")).toBeInTheDocument();
    expect(screen.getAllByText("需要配置").length).toBeGreaterThan(0);
  });
});
