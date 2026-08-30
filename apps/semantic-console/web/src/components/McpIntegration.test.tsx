import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import McpIntegration from "./McpIntegration";
import type { McpIntegrationResponse } from "../types";

const integration: McpIntegrationResponse = {
  schemaVersion: 1,
  transport: "stdio",
  projectPath: "C:\\semantic project",
  semantic: { status: "ready", command: "semarail-mcp", args: ["--project", "C:\\semantic project"], toolMode: "semantic_only" },
  governedQuery: { status: "ready", command: "semarail-query-mcp", args: ["--project", "C:\\semantic project", "--database-dsn-env", "SEMARAIL_DATABASE_URL"], databaseDsnEnv: "SEMARAIL_DATABASE_URL", datasourceType: "postgres" },
  clientConfig: { mcpServers: { "semarail-semantic": { command: "semarail-mcp", args: ["--project", "C:\\semantic project"] }, "semarail-query": { command: "semarail-query-mcp", args: [], env: { SEMARAIL_DATABASE_URL: "<POSTGRESQL_DSN>" } } } },
};

describe("McpIntegration", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
  });

  it("shows both agent-neutral MCP servers and copies the secret-free config", async () => {
    render(<McpIntegration integration={integration} loading={false} error={null} locale="en-US" onRetry={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Semantic context" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Governed query" })).toBeInTheDocument();
    expect(screen.getByText("validate · models · context · plan")).toBeInTheDocument();
    expect(screen.getByText("semarail_governed_query")).toBeInTheDocument();
    expect(screen.getByText(/SemaRail Core query limits/)).toBeInTheDocument();
    expect(screen.getAllByText(/semarail-mcp/).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Copy configuration" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(expect.stringContaining("<POSTGRESQL_DSN>")));
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("explains the MySQL governed-query boundary in Chinese", () => {
    render(<McpIntegration integration={{ ...integration, governedQuery: { ...integration.governedQuery, status: "setup_required", datasourceType: "mysql" } }} loading={false} error={null} locale="zh-CN" onRetry={vi.fn()} />);

    expect(screen.getByText("受控执行目前仅支持 PostgreSQL")).toBeInTheDocument();
    expect(screen.getByText(/SemaRail Core 受控查询服务/)).toBeInTheDocument();
    expect(screen.getAllByText("需要配置").length).toBeGreaterThan(0);
  });
});
