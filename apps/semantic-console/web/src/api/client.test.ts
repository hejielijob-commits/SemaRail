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
});
