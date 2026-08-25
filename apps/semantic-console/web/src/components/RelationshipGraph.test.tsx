import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import RelationshipGraph, { type RelationshipGraphModel, type RelationshipGraphRelationship } from "./RelationshipGraph";

const models: RelationshipGraphModel[] = [
  {
    name: "orders",
    displayName: { "zh-CN": "订单", "en-US": "Orders" },
    table: "analytics.orders",
    primaryKey: "id",
    columns: [{ name: "id", type: "bigint", primaryKey: true }, { name: "customer_id", type: "bigint" }, { name: "amount", type: "decimal" }],
  },
  {
    name: "customers",
    displayName: { "zh-CN": "客户", "en-US": "Customers" },
    table: "crm.customers",
    primaryKey: "id",
    columns: [{ name: "id", type: "bigint", primaryKey: true }, { name: "name", type: "varchar" }],
  },
  {
    name: "regions",
    displayName: { "zh-CN": "区域", "en-US": "Regions" },
    table: "crm.regions",
    primaryKey: "id",
    columns: [{ name: "id", type: "bigint", primaryKey: true }],
  },
];

const relationship: RelationshipGraphRelationship = {
  name: "orders_to_customers",
  models: ["orders", "customers"],
  joinType: "one-to-many",
  condition: "orders.customer_id = customers.id",
  displayName: { "zh-CN": "订单客户", "en-US": "Order customer" },
  description: { "zh-CN": "订单属于客户", "en-US": "Each order belongs to a customer" },
};

beforeAll(() => {
  class ResizeObserverStub {
    observe() { /* jsdom has no layout observer. */ }
    unobserve() { /* jsdom has no layout observer. */ }
    disconnect() { /* jsdom has no layout observer. */ }
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  Object.defineProperty(HTMLElement.prototype, "getBoundingClientRect", {
    configurable: true,
    value: () => ({ x: 0, y: 0, top: 0, left: 0, right: 1024, bottom: 720, width: 1024, height: 720, toJSON: () => ({}) }),
  });
});

function renderGraph(overrides: Partial<React.ComponentProps<typeof RelationshipGraph>> = {}) {
  return render(<RelationshipGraph models={models} relationships={[relationship]} locale="en-US" {...overrides} />);
}

describe("RelationshipGraph", () => {
  it("renders model nodes, physical tables, join fields and cardinality labels", async () => {
    renderGraph();

    expect(await screen.findByText("Orders")).toBeInTheDocument();
    expect(screen.getByText("Customers")).toBeInTheDocument();
    expect(screen.getByText("analytics.orders")).toBeInTheDocument();
    expect(screen.getByText("crm.customers")).toBeInTheDocument();
    expect(screen.getByText(/customer_id/)).toBeInTheDocument();
    expect(screen.getAllByText("1 relationship").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Map controls")).toBeInTheDocument();
    expect(screen.getByText("Mini Map")).toBeInTheDocument();
  });

  it("opens the relationship drawer from an edge and saves bilingual metadata", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onChange = vi.fn();
    renderGraph({ onSave, onChange });

    fireEvent.click(document.querySelector<HTMLButtonElement>('.relationship-graph-node-edit[aria-label="Edit relationship: Orders"]')!);
    expect(await screen.findByRole("dialog", { name: "Edit relationship" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Relationship name"), { target: { value: "orders_customers" } });
    fireEvent.change(screen.getByLabelText("Chinese display name"), { target: { value: "订单客户关联" } });
    fireEvent.change(screen.getByLabelText("English display name"), { target: { value: "Order to customer" } });
    fireEvent.click(screen.getByRole("button", { name: "Save relationship" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ name: "orders_customers", models: ["orders", "customers"], displayName: { "zh-CN": "订单客户关联", "en-US": "Order to customer" } }),
      expect.arrayContaining([expect.objectContaining({ name: "orders_customers" })]),
    );
    expect(onChange).toHaveBeenCalledWith(expect.arrayContaining([expect.objectContaining({ name: "orders_customers" })]));
  });

  it("creates and deletes relationships through the editor callbacks", async () => {
    const onChange = vi.fn();
    const createView = renderGraph({ relationships: [], onChange });

    fireEvent.click(screen.getByRole("button", { name: "Add relationship" }));
    expect(await screen.findByRole("dialog", { name: "Add relationship" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Relationship name"), { target: { value: "customers_to_regions" } });
    fireEvent.change(screen.getByLabelText("Join condition"), { target: { value: "customers.region_id = regions.id" } });
    fireEvent.click(screen.getByRole("button", { name: "Save relationship" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.arrayContaining([expect.objectContaining({ name: "customers_to_regions" })])));
    createView.unmount();

    const deleteChange = vi.fn();
    const deleteView = renderGraph({ onChange: deleteChange });
    fireEvent.click(document.querySelector<HTMLButtonElement>('.relationship-graph-node-edit[aria-label="Edit relationship: Orders"]')!);
    fireEvent.click(screen.getByRole("button", { name: "Delete relationship" }));
    await waitFor(() => expect(deleteChange).toHaveBeenLastCalledWith([]));
    deleteView.unmount();
  });

  it("localizes the editor and supports model search focus", async () => {
    render(<RelationshipGraph models={models} relationships={[relationship]} locale="zh-CN" />);
    expect(await screen.findByRole("heading", { name: "关系图" })).toBeInTheDocument();
    const search = screen.getByRole("textbox", { name: "搜索模型" });
    fireEvent.change(search, { target: { value: "客户" } });
    fireEvent.click(await screen.findByRole("option", { name: /客户/ }));
    expect(await screen.findByText(/聚焦/)).toBeInTheDocument();
    expect(screen.getByText("上游")).toBeInTheDocument();
    expect(screen.getByText("下游")).toBeInTheDocument();
  });

  it("shows loading, error, and empty states", () => {
    const { rerender } = renderGraph({ models: [], relationships: [], loading: true });
    expect(screen.getByRole("status")).toHaveTextContent("Loading relationship map");
    rerender(<RelationshipGraph models={models} relationships={[]} locale="en-US" error="Project metadata could not be loaded" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Project metadata could not be loaded");
    rerender(<RelationshipGraph models={[]} relationships={[]} locale="en-US" />);
    expect(screen.getByText("No models yet")).toBeInTheDocument();
  });
});
