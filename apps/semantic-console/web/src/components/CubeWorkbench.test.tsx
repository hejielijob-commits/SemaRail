import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { i18n } from "../i18n";
import CubeWorkbench, { type CubeDefinition, type CubeProjectSnapshot } from "./CubeWorkbench";

function makeCube(): CubeDefinition {
  return {
    name: "order_metrics",
    sourcePath: "cubes/order_metrics/metadata.yml",
    baseObject: "orders",
    measures: [{ name: "total_revenue", expression: "SUM(amount)", type: "DOUBLE" }],
    dimensions: [{ name: "status", expression: "status", type: "VARCHAR" }],
    timeDimensions: [{ name: "ordered_at", expression: "ordered_at", type: "DATE" }],
    hierarchies: { time: ["ordered_at"] },
    refreshTime: "15 minutes",
    properties: { description: "Order metrics" },
    draft: false,
  };
}

function makeSnapshot(): CubeProjectSnapshot {
  const cube = makeCube();
  return {
    revision: "revision-1",
    draftCount: 0,
    cubes: [cube],
    sourceFiles: [{ path: cube.sourcePath }],
    availableBaseObjects: ["orders", "customers"],
  };
}

function renderWorkbench(overrides: Partial<ComponentProps<typeof CubeWorkbench>> = {}) {
  return render(<CubeWorkbench snapshot={makeSnapshot()} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} {...overrides} />);
}

describe("CubeWorkbench", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en-US");
  });

  it("renders all cube tabs in the documented order and defaults to basic information", async () => {
    renderWorkbench();
    expect(await screen.findByRole("heading", { name: "Cubes" })).toBeInTheDocument();
    const tabs = screen.getAllByRole("tab").map((tab) => tab.textContent?.replace(/\d+/g, "").trim());
    expect(tabs).toEqual(["Basic information", "Measures", "Dimensions", "Time dimensions", "Hierarchies", "Source", "Diff"]);
    expect(screen.getByRole("textbox", { name: "Technical name" })).toHaveValue("order_metrics");
    expect(screen.getByRole("combobox", { name: "Base object" })).toHaveValue("orders");
  });

  it("edits measure expressions in a code-formatted field and saves a validated draft", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWorkbench({ onSave });
    fireEvent.click(screen.getByRole("tab", { name: /Measures/ }));
    const expression = screen.getByRole("textbox", { name: "Expression" });
    expect(expression).toHaveClass("cube-code-input");
    fireEvent.change(expression, { target: { value: "SUM(net_amount)" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0]?.[0]).toMatchObject({ measures: [{ expression: "SUM(net_amount)" }] });
    expect(screen.getByRole("status")).toHaveTextContent("Save draft");
  });

  it("blocks malformed entries and explains the structural error", async () => {
    renderWorkbench();
    fireEvent.click(screen.getByRole("tab", { name: /Measures/ }));
    fireEvent.click(screen.getByRole("button", { name: "Add measure" }));
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByRole("group", { name: "Validation issues" })).toHaveTextContent("measures.1.name");
  });

  it("opens source and diff content only when their tabs are selected", async () => {
    const onOpenSource = vi.fn();
    const onLoadDiff = vi.fn();
    renderWorkbench({ onOpenSource, onLoadDiff, sourceContent: "name: order_metrics\n", diff: { path: "cubes/order_metrics/metadata.yml", changed: true, diff: "- old\n+ new\n", revision: "revision-2" } });
    fireEvent.click(screen.getByRole("tab", { name: "Source" }));
    expect(onOpenSource).toHaveBeenCalledWith("cubes/order_metrics/metadata.yml");
    expect(screen.getByText("name: order_metrics")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Diff" }));
    expect(onLoadDiff).toHaveBeenCalledWith("cubes/order_metrics/metadata.yml");
    expect(screen.getByText(/new/)).toBeInTheDocument();
  });

  it("supports the empty state and create action", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    render(<CubeWorkbench snapshot={{ revision: "empty", draftCount: 0, cubes: [], sourceFiles: [], availableBaseObjects: ["orders", "customers"] }} onSave={vi.fn()} onOpenSource={vi.fn()} onLoadDiff={vi.fn()} onCreate={onCreate} />);
    expect(screen.getByRole("heading", { name: "No cubes yet" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create cube" }));
    const dialog = screen.getByRole("dialog", { name: "Create a cube" });
    fireEvent.change(screen.getByRole("textbox", { name: "Technical name" }), { target: { value: "sales_overview" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Base object" }), { target: { value: "customers" } });
    fireEvent.click(dialog.querySelector(".button-primary") as HTMLButtonElement);
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith({ name: "sales_overview", baseObject: "customers" }));
  });
});
