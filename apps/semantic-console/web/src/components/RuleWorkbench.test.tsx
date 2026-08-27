import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import RuleWorkbench, { type KnowledgeRule } from "./RuleWorkbench";

function rule(overrides: Partial<KnowledgeRule> = {}): KnowledgeRule {
  return {
    id: "sales",
    name: "sales guidance",
    content: "Use net revenue after approved returns.",
    enabled: true,
    sourcePath: "knowledge/rules/sales.md",
    scope: ["revenue"],
    tags: ["finance"],
    updatedAt: "2026-08-26T09:30:00.000Z",
    ...overrides,
  };
}

describe("RuleWorkbench", () => {
  it("shows one rule per row and exposes an accessible enable switch", async () => {
    const onToggleRule = vi.fn().mockResolvedValue(undefined);
    render(<RuleWorkbench rules={[rule(), rule({ id: "orders", name: "orders guidance", enabled: false })]} onToggleRule={onToggleRule} />);

    expect(await screen.findByRole("heading", { name: "Business rules" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("button", { name: /sales guidance/ })).toBeInTheDocument();
    const disabledSwitch = screen.getByRole("switch", { name: "Enable rule: orders guidance" });
    expect(disabledSwitch).toHaveAttribute("aria-checked", "false");
    fireEvent.click(disabledSwitch);
    await waitFor(() => expect(onToggleRule).toHaveBeenCalledWith(expect.objectContaining({ id: "orders", enabled: true }), true));
  });

  it("filters rows by search text and status", async () => {
    render(<RuleWorkbench rules={[rule(), rule({ id: "orders", name: "orders guidance", content: "Use order date", enabled: false })]} />);

    expect(await screen.findByRole("button", { name: /sales guidance/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Disabled" }));
    expect(screen.queryByRole("button", { name: /sales guidance/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /orders guidance/ })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Search rules" }), { target: { value: "unknown" } });
    expect(screen.getByText("No matching rules")).toBeInTheDocument();
  });

  it("edits the selected rule and sends explicit draft and publish actions", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<RuleWorkbench rules={[rule()]} onSave={onSave} />);

    const content = await screen.findByRole("textbox", { name: "Rule text" });
    fireEvent.change(content, { target: { value: "Use approved net revenue only." } });
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ content: "Use approved net revenue only." }), "draft"));
    expect(screen.getByText("The rule is saved as a project draft.")).toBeInTheDocument();

    fireEvent.change(content, { target: { value: "Use the approved net revenue metric." } });
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(onSave).toHaveBeenLastCalledWith(expect.objectContaining({ content: "Use the approved net revenue metric." }), "publish"));
  });

  it("discards local edits back to the committed rule", async () => {
    const onDiscard = vi.fn();
    render(<RuleWorkbench rules={[rule()]} onDiscard={onDiscard} />);
    const content = await screen.findByRole("textbox", { name: "Rule text" });
    fireEvent.change(content, { target: { value: "Local draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.getByRole("textbox", { name: "Rule text" })).toHaveValue("Use net revenue after approved returns.");
    expect(onDiscard).toHaveBeenCalledWith(expect.objectContaining({ content: "Use net revenue after approved returns." }));
  });

  it("keeps source and diff in editor tabs with safely wrapped code", async () => {
    render(<RuleWorkbench rules={[rule()]} source={{ path: "knowledge/rules/sales.md", content: "# Sales\nUse net revenue.\n", diff: "- old rule\n+ new rule\n" }} />);

    fireEvent.click(await screen.findByRole("tab", { name: "Source" }));
    expect(screen.getByRole("tabpanel")).toHaveTextContent("# Sales");
    expect(screen.getByRole("tabpanel").querySelector("pre")).toHaveClass("kw-code");
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(screen.getByRole("tabpanel")).toHaveTextContent("+ new rule");
  });

  it("supports arrow-key navigation across editor tabs", async () => {
    render(<RuleWorkbench rules={[rule()]} source={{ path: "knowledge/rules/sales.md", content: "source", diff: "diff" }} />);
    const contentTab = await screen.findByRole("tab", { name: "Content" });
    fireEvent.keyDown(contentTab, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Source" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Source" }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-selected", "true");
  });

  it("renders a useful loading skeleton and an actionable error state", async () => {
    const retry = vi.fn();
    const { rerender } = render(<RuleWorkbench rules={[]} loading />);
    expect(screen.getByRole("status", { name: "Loading rules" })).toBeInTheDocument();
    rerender(<RuleWorkbench rules={[]} error="API unavailable" onRetry={retry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("API unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("does not expose editing controls in read-only mode", async () => {
    render(<RuleWorkbench rules={[rule()]} readOnly onSave={vi.fn()} onToggleRule={vi.fn()} />);
    expect(await screen.findByText("Read only")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Rule text" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
    expect(screen.getAllByRole("switch", { name: "Disable rule: sales guidance" })[0]).toBeDisabled();
  });

  it("rolls back an optimistic toggle and reports the API error", async () => {
    const onToggleRule = vi.fn().mockRejectedValue(new Error("Revision conflict"));
    render(<RuleWorkbench rules={[rule()]} onToggleRule={onToggleRule} />);
    const toggle = (await screen.findAllByRole("switch", { name: "Disable rule: sales guidance" }))[0]!;
    fireEvent.click(toggle);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Revision conflict"));
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });

  it("localizes visible labels without changing the rule payload", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<RuleWorkbench rules={[rule()]} locale="zh-CN" onSave={onSave} />);
    expect(await screen.findByRole("heading", { name: "业务规则" })).toBeInTheDocument();
    const content = screen.getByRole("textbox", { name: "规则文本" });
    fireEvent.change(content, { target: { value: "仅使用已批准的净收入。" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ id: "sales", content: "仅使用已批准的净收入。" }), "draft"));
  });
});
