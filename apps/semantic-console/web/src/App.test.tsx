import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function emptyWorkspaceFetch() {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/health")) return Promise.resolve(jsonResponse({ status: "ok" }));
    if (url.endsWith("/api/project") && init?.method !== "POST") return Promise.resolve(jsonResponse({ name: "", projectExists: false, activeDatasource: null }));
    if (url.endsWith("/api/datasource-types")) return Promise.resolve(jsonResponse([]));
    if (url.endsWith("/api/datasources")) return Promise.resolve(jsonResponse([]));
    if (url.endsWith("/api/project/files")) return Promise.resolve(jsonResponse([]));
    if (url.endsWith("/api/versions")) return Promise.resolve(jsonResponse([]));
    return Promise.resolve(jsonResponse({ code: "NOT_FOUND", message: `Unhandled test request: ${url}` }, 404));
  });
}

describe("Semantic Console interactions", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("does not show fixture data when the API is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<App />);

    expect(await screen.findByText("Semantic Console is offline")).toBeInTheDocument();
    expect(screen.getByText("API unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Revenue intelligence")).not.toBeInTheDocument();
    expect(screen.queryByText("Orders")).not.toBeInTheDocument();
    expect(screen.queryByText("Healthy")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Models" }));
    expect(await screen.findByRole("heading", { name: "Models" })).toBeInTheDocument();
    expect(screen.getByText("No model files")).toBeInTheDocument();
  });

  it("surfaces publish failures without claiming a publish", async () => {
    const fetchMock = emptyWorkspaceFetch();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/project/publish")) return Promise.resolve(jsonResponse({ code: "INVALID_PROJECT", message: "project validation failed" }, 400));
      return emptyWorkspaceFetch()(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    await screen.findByRole("heading", { name: "Semantic project" });
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByText("Publish failed")).toBeInTheDocument();
    expect(screen.queryByText("Project published")).not.toBeInTheDocument();
  });

  it("loads a real project file and sends its content to the file endpoint", async () => {
    const file = { path: "models/orders/metadata.yml", size: 42, draft: true, revision: "sha256:old" };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return Promise.resolve(jsonResponse({ status: "ok" }));
      if (url.endsWith("/api/project") && init?.method !== "POST") return Promise.resolve(jsonResponse({ name: "Warehouse project", projectExists: true, activeDatasource: null }));
      if (url.endsWith("/api/datasource-types")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/datasources")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/project/files")) return Promise.resolve(jsonResponse([file]));
      if (url.endsWith("/api/versions")) return Promise.resolve(jsonResponse([]));
      if (url.includes("/api/project/file?") && init?.method === "PUT") return Promise.resolve(jsonResponse({ ...file, content: "name: Orders\ncolumns: []\n", revision: "sha256:new" }));
      if (url.includes("/api/project/file?")) return Promise.resolve(jsonResponse({ ...file, content: "name: Orders\ncolumns: []\n" }));
      return Promise.resolve(jsonResponse({ code: "NOT_FOUND", message: `Unhandled test request: ${url}` }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "MDL source" }));
    const editor = await screen.findByRole("textbox", { name: "MDL source editor" });
    expect(editor).toHaveValue("name: Orders\ncolumns: []\n");
    fireEvent.change(editor, { target: { value: "name: Orders\ncolumns: [id]\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Save source" }));

    await screen.findByText("Draft saved");
    const putCall = fetchMock.mock.calls.find(([input, init]) => String(input).includes("/api/project/file?") && init?.method === "PUT");
    expect(putCall).toBeTruthy();
    expect(String(putCall?.[0])).toContain("path=models%2Forders%2Fmetadata.yml");
    expect(JSON.parse(String(putCall?.[1]?.body))).toMatchObject({ content: "name: Orders\ncolumns: [id]\n", expectedRevision: "sha256:old" });
  });

  it("uses the selected datasource and schema when loading columns", async () => {
    const datasources = [{ id: "source-a", name: "Warehouse A", type: "postgres" }, { id: "source-b", name: "Warehouse B", type: "postgres" }];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return Promise.resolve(jsonResponse({ status: "ok" }));
      if (url.endsWith("/api/project") && init?.method !== "POST") return Promise.resolve(jsonResponse({ name: "Warehouse project", projectExists: true, activeDatasource: datasources[0] }));
      if (url.endsWith("/api/datasource-types")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/datasources")) return Promise.resolve(jsonResponse(datasources));
      if (url.endsWith("/api/project/files")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/versions")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/schemas")) return Promise.resolve(jsonResponse([{ name: url.includes("source-b") ? "analytics_b" : "analytics_a" }]));
      if (url.endsWith("/tables?schema=analytics_b")) return Promise.resolve(jsonResponse([{ name: "orders", type: "TABLE" }]));
      if (url.endsWith("/tables?schema=analytics_a")) return Promise.resolve(jsonResponse([{ name: "orders", type: "TABLE" }]));
      if (url.includes("/columns?schema=analytics_b&table=orders")) return Promise.resolve(jsonResponse([{ name: "id", type: "BIGINT" }]));
      if (url.includes("/columns?schema=analytics_a&table=orders")) return Promise.resolve(jsonResponse([{ name: "id", type: "INTEGER" }]));
      return Promise.resolve(jsonResponse({ code: "NOT_FOUND", message: `Unhandled test request: ${url}` }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Schema browser" }));
    await screen.findByRole("heading", { name: "Tables and views" });
    fireEvent.change(screen.getByLabelText("Data source"), { target: { value: "source-b" } });

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/datasources/source-b/columns?schema=analytics_b&table=orders"))).toBe(true));
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/datasources/source-a/columns?schema=analytics_b"))).toBe(false);
  });
});
