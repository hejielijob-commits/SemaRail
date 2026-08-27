import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiClientError } from "./client";

describe("ApiClient", () => {
  it("keeps REST paths and query parameters typed at one boundary", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify([{ name: "analytics" }]), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await new ApiClient("http://console.test").getTables("warehouse prod", "analytics/reporting");

    expect(result).toEqual([{ name: "analytics" }]);
    expect(fetchMock).toHaveBeenCalledWith("http://console.test/api/datasources/warehouse%20prod/tables?schema=analytics%2Freporting", expect.objectContaining({ headers: { Accept: "application/json" } }));
  });

  it("normalizes safe API errors without exposing response internals", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "INVALID_CONNECTION", message: "database connection failed" }), { status: 400 })));

    await expect(new ApiClient().testDatasource("warehouse")).rejects.toMatchObject({ status: 400, code: "INVALID_CONNECTION", message: "database connection failed" } satisfies Partial<ApiClientError>);
  });

  it("activates a datasource through the explicit current-connection endpoint", async () => {
    const response = { activeDatasource: { id: "billing", name: "Billing", type: "mysql" }, project: { activeDatasource: { id: "billing", name: "Billing", type: "mysql" } } };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(new ApiClient().activateDatasource("billing replica")).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith("/api/datasources/billing%20replica/activate", expect.objectContaining({ method: "POST", body: "{}" }));
  });

  it("generates a model and keeps file paths encoded in the REST boundary", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ file: "models/orders/metadata.yml", draft: true, revision: "sha256:1" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient().generateModel("warehouse prod", "analytics/reporting", "order items", { name: "Order items" });
    expect(fetchMock).toHaveBeenCalledWith("/api/datasources/warehouse%20prod/models?schema=analytics%2Freporting&table=order%20items", expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "Order items" }) }));
  });

  it("puts project file content in the body and path in the query string", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ path: "models/orders/metadata.yml", content: "name: Orders", revision: "sha256:2" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient().updateProjectFile({ path: "models/orders/metadata.yml", content: "name: Orders", expectedRevision: "sha256:1" });
    expect(fetchMock).toHaveBeenCalledWith("/api/project/file?path=models%2Forders%2Fmetadata.yml", expect.objectContaining({ method: "PUT", body: JSON.stringify({ content: "name: Orders", expectedRevision: "sha256:1" }) }));
  });

  it("loads and updates the structured semantic project", async () => {
    const snapshot = { revision: "sha256:1", draftCount: 0, models: [], relationships: [], sourceFiles: [] };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(snapshot), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(new ApiClient().getSemanticProject()).resolves.toEqual(snapshot);
    await new ApiClient().updateSemanticModel("order items", {
      name: "order_items",
      primaryKey: "id",
      displayName: { "zh-CN": "订单明细", "en-US": "Order items" },
      description: { "zh-CN": "", "en-US": "" },
      businessDomain: "commerce",
      visible: true,
      tableReference: { schema: "public", table: "order_items" },
      columns: [],
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/semantic-project");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/semantic-models/order%20items");
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toMatchObject({ name: "order_items" });
  });

  it("saves relationships and reads a file diff with encoded paths", async () => {
    const response = { revision: "sha256:2", draftCount: 1, models: [], relationships: [], sourceFiles: [] };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient();

    await client.updateSemanticRelationships({ relationships: [], expectedRevision: "sha256:1" });
    await client.getProjectDiff("models/order items/metadata.yml");

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/semantic-relationships");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ relationships: [], expectedRevision: "sha256:1" });
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/project/diff?path=models%2Forder%20items%2Fmetadata.yml");
  });

  it("deletes governed rules and cubes with revision protection", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ revision: "sha256:2", rules: [], cubes: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient();
    await client.deleteRule("rule/a", "sha256:1");
    await client.deleteCube("sales cube", "sha256:2");
    await client.deleteCube("empty-revision", "");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/knowledge/rules/rule%2Fa");
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: "DELETE", body: JSON.stringify({ expectedRevision: "sha256:1" }) });
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/cubes/sales%20cube");
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({ method: "DELETE", body: JSON.stringify({ expectedRevision: "sha256:2" }) });
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({ method: "DELETE", body: JSON.stringify({ expectedRevision: "" }) });
  });
});
