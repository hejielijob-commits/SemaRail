import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { i18n } from "../i18n";
import ModelEditor from "./ModelEditor";
import type { ProjectDiff, SemanticColumn, SemanticModel, SemanticProjectSnapshot } from "../types";

function column(index: number): SemanticColumn {
  const name = index === 1 ? "customer_id" : `field_${String(index).padStart(2, "0")}_with_a_long_technical_name`;
  return {
    name,
    type: index % 2 ? "UUID" : "VARCHAR",
    primaryKey: index === 1,
    notNull: index === 1,
    calculated: false,
    expression: "",
    displayName: { "zh-CN": index === 1 ? "客户编号" : `字段 ${index}`, "en-US": index === 1 ? "Customer ID" : `Field ${index}` },
    description: { "zh-CN": "", "en-US": "" },
    semanticRole: index === 1 ? "key" : "dimension",
    format: "auto",
    visible: true,
  };
}

function makeSnapshot(count = 1): SemanticProjectSnapshot {
  const model: SemanticModel = {
    name: "orders",
    sourcePath: "models/orders/metadata.yml",
    tableReference: { schema: "public", table: "orders" },
    primaryKey: "customer_id",
    displayName: { "zh-CN": "订单", "en-US": "Orders" },
    description: { "zh-CN": "业务订单", "en-US": "Customer orders" },
    businessDomain: "commerce",
    visible: true,
    draft: false,
    columns: Array.from({ length: count }, (_, index) => column(index + 1)),
  };
  return {
    revision: "sha256:model",
    draftCount: 0,
    models: [model],
    relationships: [],
    sourceFiles: [{ path: model.sourcePath }],
  };
}

describe("ModelEditor", () => {
  beforeEach(async () => {
    localStorage.clear();
    await i18n.changeLanguage("en-US");
  });

  it("edits one visible locale value without dropping the other locale", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onOpenSource = vi.fn();
    render(<ModelEditor snapshot={makeSnapshot()} sourceContent="name: orders\ncolumns: []\n" onSave={onSave} onOpenSource={onOpenSource} onLoadDiff={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: "Business models" })).toBeInTheDocument();
    expect(onOpenSource).toHaveBeenCalledWith("models/orders/metadata.yml");
    expect(screen.getByRole("textbox", { name: "Business name" })).toHaveValue("Orders");
    expect(screen.queryByRole("textbox", { name: "Business name English" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Business name" }), { target: { value: "Customer orders model" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0]?.[0]).toMatchObject({
      displayName: { "zh-CN": "订单", "en-US": "Customer orders model" },
    });
  });

  it("keeps an unpublished local edit while switching tabs and filtering models", async () => {
    render(<ModelEditor snapshot={makeSnapshot()} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} />);

    const nameInput = await screen.findByRole("textbox", { name: "Business name" });
    fireEvent.change(nameInput, { target: { value: "Edited locally" } });
    fireEvent.click(screen.getByRole("tab", { name: /Field dictionary/ }));
    fireEvent.click(screen.getByRole("tab", { name: "Model details" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search models" }), { target: { value: "orders" } });
    expect(screen.getByRole("textbox", { name: "Business name" })).toHaveValue("Edited locally");
  });

  it("retains a dirty draft when a project revision changes", async () => {
    const initial = makeSnapshot();
    const { rerender } = render(<ModelEditor snapshot={initial} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} />);
    const nameInput = await screen.findByRole("textbox", { name: "Business name" });
    fireEvent.change(nameInput, { target: { value: "Edited locally" } });

    const refreshed = makeSnapshot();
    refreshed.revision = "sha256:unrelated-change";
    rerender(<ModelEditor snapshot={refreshed} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} />);

    expect(screen.getByRole("textbox", { name: "Business name" })).toHaveValue("Edited locally");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("restores each model's local draft after switching models", async () => {
    const snapshot = makeSnapshot();
    const secondModel = JSON.parse(JSON.stringify(snapshot.models[0])) as SemanticModel;
    secondModel.name = "customers";
    secondModel.sourcePath = "models/customers/metadata.yml";
    secondModel.displayName = { "zh-CN": "客户", "en-US": "Customers" };
    snapshot.models.push(secondModel);
    render(<ModelEditor snapshot={snapshot} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} />);

    fireEvent.change(await screen.findByRole("textbox", { name: "Business name" }), { target: { value: "Edited orders" } });
    fireEvent.click(screen.getByRole("button", { name: /Customers/ }));
    expect(screen.getByRole("textbox", { name: "Business name" })).toHaveValue("Customers");
    fireEvent.click(screen.getByRole("button", { name: /Orders/ }));
    expect(screen.getByRole("textbox", { name: "Business name" })).toHaveValue("Edited orders");
  });

  it("keeps a dirty draft and exposes a resolution when the selected model changes remotely", async () => {
    const initial = makeSnapshot();
    const { rerender } = render(<ModelEditor snapshot={initial} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} />);
    const nameInput = await screen.findByRole("textbox", { name: "Business name" });
    fireEvent.change(nameInput, { target: { value: "Edited locally" } });

    const refreshed = makeSnapshot();
    refreshed.revision = "sha256:selected-model-change";
    refreshed.models[0] = { ...refreshed.models[0], businessDomain: "fulfillment" };
    rerender(<ModelEditor snapshot={refreshed} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} />);

    expect(screen.getByRole("textbox", { name: "Business name" })).toHaveValue("Edited locally");
    expect(screen.getByRole("alert")).toHaveTextContent("This model changed on the server");
    fireEvent.click(screen.getByRole("button", { name: "Use server version" }));
    expect(screen.getByRole("textbox", { name: "Business name" })).toHaveValue("Orders");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders and edits ordered composite primary keys without coercing them to a string", async () => {
    const snapshot = makeSnapshot(3);
    const model = snapshot.models[0]!;
    const compositeKey = [model.columns[0]!.name, model.columns[2]!.name];
    model.primaryKey = compositeKey;
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<ModelEditor snapshot={snapshot} onSave={onSave} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} />);

    const keyPicker = await screen.findByRole("button", { name: "Primary key" });
    expect(keyPicker).toHaveTextContent(compositeKey[0]!);
    expect(keyPicker).toHaveTextContent(compositeKey[1]!);
    fireEvent.click(keyPicker);
    const uncheckedKey = model.columns[1]!.name;
    fireEvent.click(screen.getByRole("checkbox", { name: uncheckedKey }));
    fireEvent.click(screen.getByRole("checkbox", { name: uncheckedKey }));
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0]?.[0]).toMatchObject({ primaryKey: compositeKey });
  });

  it("reloads the selected source after a successful save", async () => {
    const onOpenSource = vi.fn();
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<ModelEditor snapshot={makeSnapshot()} onSave={onSave} onOpenSource={onOpenSource} onLoadDiff={vi.fn()} />);

    const nameInput = await screen.findByRole("textbox", { name: "Business name" });
    fireEvent.change(nameInput, { target: { value: "Saved model" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onOpenSource).toHaveBeenCalledTimes(2);
    expect(onOpenSource).toHaveBeenLastCalledWith("models/orders/metadata.yml");
  });

  it("keeps source and diff views in the selected model workspace tabs", async () => {
    const diff: ProjectDiff = { path: "models/orders/metadata.yml", changed: true, revision: "sha256:draft", diff: "- name: orders\n+ name: sales_orders\n" };
    const onLoadDiff = vi.fn();
    render(<ModelEditor snapshot={makeSnapshot()} sourceContent="name: orders\n" diff={diff} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={onLoadDiff} />);

    expect(document.querySelector(".model-source-panel")).toBeNull();
    fireEvent.click(await screen.findByRole("tab", { name: "Source" }));
    expect(document.querySelector(".model-source-code")).toHaveTextContent("name: orders");
    fireEvent.click(screen.getByRole("tab", { name: "Diff" }));
    expect(onLoadDiff).toHaveBeenCalledWith("models/orders/metadata.yml");
    expect(screen.getByText(/sales_orders/)).toBeInTheDocument();
  });

  it("shows a compact field summary and expands one field for advanced editing", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<ModelEditor snapshot={makeSnapshot()} onSave={onSave} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} />);

    fireEvent.click(await screen.findByRole("tab", { name: /Field dictionary/ }));
    const fieldToggle = screen.getByRole("button", { name: /customer_id/ });
    expect(screen.getByText("Customer ID")).toBeInTheDocument();
    fireEvent.click(fieldToggle);
    expect(fieldToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Customer ID");
    expect(screen.getByRole("textbox", { name: "Description" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Description English/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "Buyer ID" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0]?.[0]).toMatchObject({ columns: [{ displayName: { "en-US": "Buyer ID", "zh-CN": "客户编号" } }] });
  });

  it("paginates fields at fifteen rows and keeps long technical names contained", async () => {
    render(<ModelEditor snapshot={makeSnapshot(31)} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} />);

    fireEvent.click(await screen.findByRole("tab", { name: /Field dictionary/ }));
    expect(screen.getByText("field_15_with_a_long_technical_name")).toBeInTheDocument();
    expect(screen.queryByText("field_16_with_a_long_technical_name")).not.toBeInTheDocument();
    expect(screen.getByText("1-15 of 31")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("field_16_with_a_long_technical_name")).toBeInTheDocument();
    expect(screen.queryByText("field_01_with_a_long_technical_name")).not.toBeInTheDocument();
    expect(screen.getByText("16-30 of 31")).toBeInTheDocument();
  });
});
