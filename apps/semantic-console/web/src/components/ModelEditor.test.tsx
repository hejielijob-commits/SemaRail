import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import "../i18n";
import ModelEditor from "./ModelEditor";
import type { ProjectDiff, SemanticProjectSnapshot } from "../types";

const snapshot: SemanticProjectSnapshot = {
  revision: "sha256:model",
  draftCount: 0,
  models: [{
    name: "orders",
    sourcePath: "models/orders/metadata.yml",
    tableReference: { schema: "public", table: "orders" },
    primaryKey: "id",
    displayName: { "zh-CN": "订单", "en-US": "Orders" },
    description: { "zh-CN": "业务订单", "en-US": "Customer orders" },
    businessDomain: "commerce",
    visible: true,
    draft: false,
    columns: [{
      name: "customer_id",
      type: "UUID",
      primaryKey: false,
      notNull: true,
      calculated: false,
      expression: "",
      displayName: { "zh-CN": "客户编号", "en-US": "Customer ID" },
      description: { "zh-CN": "", "en-US": "" },
      semanticRole: "dimension",
      format: "auto",
      visible: true,
    }],
  }],
  relationships: [],
  sourceFiles: [{ path: "models/orders/metadata.yml" }],
};

describe("ModelEditor", () => {
  it("edits bilingual model metadata and field semantics", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onOpenSource = vi.fn();
    render(<ModelEditor snapshot={snapshot} sourceContent="name: orders\ncolumns: []\n" onSave={onSave} onOpenSource={onOpenSource} onLoadDiff={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: "Business models" })).toBeInTheDocument();
    expect(onOpenSource).toHaveBeenCalledWith("models/orders/metadata.yml");
    fireEvent.change(screen.getByRole("textbox", { name: "Business name English" }), { target: { value: "Customer orders model" } });
    fireEvent.click(screen.getByRole("tab", { name: /Field dictionary/ }));
    fireEvent.change(screen.getByRole("combobox", { name: "customer_id Semantic role" }), { target: { value: "key" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0]?.[0]).toMatchObject({
      displayName: { "en-US": "Customer orders model" },
      columns: [{ semanticRole: "key" }],
    });
  });

  it("keeps source and diff views reachable from the selected model", async () => {
    const diff: ProjectDiff = { path: "models/orders/metadata.yml", changed: true, revision: "sha256:draft", diff: "- name: orders\n+ name: sales_orders\n", };
    const onLoadDiff = vi.fn();
    render(<ModelEditor snapshot={snapshot} sourceContent="name: orders\n" diff={diff} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={onLoadDiff} />);

    fireEvent.click(await screen.findByRole("button", { name: /Diff/ }));
    expect(onLoadDiff).toHaveBeenCalledWith("models/orders/metadata.yml");
    expect(screen.getByText(/sales_orders/)).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: /Source/ })[1]!);
    expect(document.querySelector(".model-source-code")).toHaveTextContent("name: orders");
  });
});
