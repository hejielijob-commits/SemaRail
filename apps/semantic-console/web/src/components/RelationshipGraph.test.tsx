import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import RelationshipGraph, { getRelationshipFieldPairs, parseRelationshipFieldPairs, relationshipUsesAdvancedCondition, selectRelationshipFieldPair, type RelationshipGraphModel, type RelationshipGraphRelationship } from "./RelationshipGraph";

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
  fieldPairs: [{ sourceModel: "orders", sourceField: "customer_id", targetModel: "customers", targetField: "id" }],
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
    expect(screen.getAllByText(/customer_id/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("1 relationship").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Map controls")).toBeInTheDocument();
    expect(screen.getByText("Mini Map")).toBeInTheDocument();
  });

  it("opens the relationship drawer, saves one locale, and keeps the field-level condition", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onChange = vi.fn();
    renderGraph({ onSave, onChange });

    fireEvent.click(document.querySelector<HTMLButtonElement>('.relationship-graph-node-edit[aria-label="Edit relationship: Orders"]')!);
    expect(await screen.findByRole("dialog", { name: "Edit relationship" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Relationship name"), { target: { value: "orders_customers" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Order to customer link" } });
    fireEvent.click(screen.getByRole("button", { name: "Save relationship" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ name: "orders_customers", models: ["orders", "customers"], condition: "orders.customer_id = customers.id", displayName: { "zh-CN": "订单客户", "en-US": "Order to customer link" } }),
      expect.arrayContaining([expect.objectContaining({ name: "orders_customers" })]),
    );
    expect(onSave.mock.calls[0][0]).not.toHaveProperty("fieldPairs");
    expect(onChange).toHaveBeenCalledWith(expect.arrayContaining([expect.objectContaining({ name: "orders_customers" })]));
  });

  it("preserves advanced condition source text exactly when it is saved unchanged", async () => {
    const condition = "\n  LOWER(orders.customer_id) = CAST(customers.id AS TEXT)\n";
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderGraph({ relationships: [{ ...relationship, condition, fieldPairs: [] }], onSave });

    fireEvent.click(document.querySelector<HTMLButtonElement>('.relationship-graph-node-edit[aria-label="Edit relationship: Orders"]')!);
    expect(await screen.findByRole("checkbox", { name: "Advanced join condition" })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Save relationship" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0].condition).toBe(condition);
  });

  it("clears a generated condition and rejects save when one structured field is cleared", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderGraph({ onSave });

    fireEvent.click(document.querySelector<HTMLButtonElement>('.relationship-graph-node-edit[aria-label="Edit relationship: Orders"]')!);
    fireEvent.change(await screen.findByLabelText("Source field"), { target: { value: "" } });
    expect(screen.getByRole("textbox", { name: /join condition/i })).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "Save relationship" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Complete the required relationship fields");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("locks the relationship draft while an asynchronous save is in flight", async () => {
    let finishSave!: () => void;
    const pendingSave = new Promise<void>((resolve) => { finishSave = resolve; });
    const onSave = vi.fn(() => pendingSave);
    renderGraph({ onSave });

    fireEvent.click(document.querySelector<HTMLButtonElement>('.relationship-graph-node-edit[aria-label="Edit relationship: Orders"]')!);
    const nameInput = await screen.findByLabelText("Relationship name");
    fireEvent.click(screen.getByRole("button", { name: "Save relationship" }));

    await waitFor(() => expect(nameInput).toBeDisabled());
    expect(screen.getByRole("button", { name: "Delete relationship" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Close relationship editor" })).toBeDisabled();

    finishSave();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit relationship" })).not.toBeInTheDocument());
  });

  it("creates and deletes relationships through the editor callbacks", async () => {
    const onChange = vi.fn();
    const createView = renderGraph({ relationships: [], onChange });

    fireEvent.click(screen.getByRole("button", { name: "Add relationship" }));
    expect(await screen.findByRole("dialog", { name: "Add relationship" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Relationship name"), { target: { value: "customers_to_regions" } });
    fireEvent.change(screen.getByLabelText("Source model"), { target: { value: "customers" } });
    fireEvent.change(screen.getByLabelText("Target model"), { target: { value: "regions" } });
    fireEvent.change(screen.getByLabelText("Source field"), { target: { value: "id" } });
    fireEvent.change(screen.getByLabelText("Target field"), { target: { value: "id" } });
    fireEvent.click(screen.getByRole("button", { name: "Save relationship" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.arrayContaining([expect.objectContaining({ name: "customers_to_regions" })])));
    expect(onChange).toHaveBeenLastCalledWith(expect.arrayContaining([expect.objectContaining({ name: "customers_to_regions", condition: "customers.id = regions.id" })]));
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

  it("parses simple and composite field predicates and rejects unsafe expressions", () => {
    expect(parseRelationshipFieldPairs(relationship, models)).toEqual([{ sourceField: "customer_id", targetField: "id" }]);
    expect(parseRelationshipFieldPairs({ ...relationship, condition: "orders.customer_id = customers.id AND orders.id = customers.id" }, models)).toEqual([
      { sourceField: "customer_id", targetField: "id" },
      { sourceField: "id", targetField: "id" },
    ]);
    expect(parseRelationshipFieldPairs({ ...relationship, condition: "o.customer_id = c.id" }, models)).toEqual([]);
    const composite = { ...relationship, fieldPairs: undefined, condition: "orders.customer_id = customers.id AND orders.id = customers.id" };
    expect(selectRelationshipFieldPair(composite, models, { sourceField: "id", targetField: "id" })).toEqual({ sourceField: "id", targetField: "id" });
  });

  it("keeps all fields usable through expansion and six-row pagination", async () => {
    const wideOrders: RelationshipGraphModel = {
      ...models[0],
      columns: [
        ...models[0].columns,
        ...Array.from({ length: 7 }, (_, index) => ({ name: `very_long_order_field_name_${index}`, type: "varchar" })),
      ],
    };
    render(<RelationshipGraph models={[wideOrders, models[1]]} relationships={[relationship]} locale="en-US" />);
    expect(await screen.findByText("Orders")).toBeInTheDocument();
    fireEvent.click(document.querySelector<HTMLButtonElement>(".relationship-graph-expand-button")!);
    expect(screen.getByRole("textbox", { name: "Search models" })).toHaveValue("");
    expect(screen.queryByText(/Focus:/)).not.toBeInTheDocument();
    expect(screen.getByText("1-6 / 9")).toBeInTheDocument();
    expect(screen.getAllByTitle("very_long_order_field_name_0").length).toBeGreaterThan(0);
    fireEvent.click(document.querySelector<HTMLButtonElement>(".relationship-graph-page-button:not(:disabled)")!);
    expect(screen.getByText("7-9 / 9")).toBeInTheDocument();
    expect(screen.getAllByTitle("very_long_order_field_name_6").length).toBeGreaterThan(0);
    expect(document.querySelector('[data-handleid="source-field-customer_id"]')).toBeInTheDocument();
    expect(document.querySelector('[data-handleid="target-field-id"]')).toBeInTheDocument();
  });

  it("uses validated server field pairs before parsing the condition", () => {
    const serverRelationship = {
      ...relationship,
      condition: "orders.customer_id = customers.id AND orders.id = customers.id",
      fieldPairs: [{ sourceModel: "orders", sourceField: "customer_id", targetModel: "customers", targetField: "id" }],
    };
    expect(getRelationshipFieldPairs(serverRelationship, models)).toEqual([{ sourceField: "customer_id", targetField: "id" }]);
    render(<RelationshipGraph models={models} relationships={[serverRelationship]} locale="en-US" />);
    expect(document.querySelector('[data-handleid="source-field-customer_id"]')).toBeInTheDocument();
    expect(getRelationshipFieldPairs({
      ...relationship,
      condition: "orders.customer_id = customers.id AND (orders.id = customers.id)",
      fieldPairs: [],
    }, models)).toEqual([]);
  });

  it("keeps a server-rejected field projection in advanced condition mode", () => {
    const complexRelationship = {
      ...relationship,
      condition: "orders.customer_id = customers.id AND (orders.id = customers.id)",
      fieldPairs: [],
    };
    expect(relationshipUsesAdvancedCondition(complexRelationship, models)).toBe(true);
    expect(relationshipUsesAdvancedCondition(relationship, models)).toBe(false);
  });
});
