import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import "../i18n";
import { i18n } from "../i18n";
import { Modal, Pagination, usePagination } from "./ui";

describe("shared pagination", () => {
  it("uses 20 rows by default, changes page size, and clamps after deletion", () => {
    const values = Array.from({ length: 41 }, (_, index) => index + 1);
    const { result, rerender } = renderHook(({ items }) => usePagination(items), { initialProps: { items: values } });

    expect(result.current.pageItems).toEqual(values.slice(0, 20));
    act(() => result.current.paginationProps.onPageChange(3));
    expect(result.current.pageItems).toEqual([41]);

    rerender({ items: values.slice(0, 39) });
    expect(result.current.paginationProps.page).toBe(2);
    expect(result.current.pageItems).toEqual(values.slice(20, 39));

    act(() => result.current.paginationProps.onPageSizeChange(50));
    expect(result.current.paginationProps.page).toBe(1);
    expect(result.current.pageItems).toHaveLength(39);
  });

  it("exposes accessible controls and commits a keyboard page jump", () => {
    let page = 1;
    const { rerender } = render(<Pagination page={page} pageSize={20} total={65} onPageChange={(next) => { page = next; }} onPageSizeChange={() => undefined} />);
    const jump = screen.getByRole("spinbutton", { name: "Go to page" });
    fireEvent.change(jump, { target: { value: "3" } });
    fireEvent.keyDown(jump, { key: "Enter" });
    expect(page).toBe(3);

    rerender(<Pagination page={4} pageSize={20} total={65} onPageChange={() => undefined} onPageSizeChange={() => undefined} />);
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    expect(screen.getByText("61-65 of 65")).toBeInTheDocument();
  });

  it("keeps the Chinese controls explicit without changing pagination behavior", async () => {
    await act(async () => { await i18n.changeLanguage("zh-CN"); });
    render(<Pagination page={2} pageSize={20} total={65} onPageChange={() => undefined} onPageSizeChange={() => undefined} />);
    expect(screen.getByText("第 21-40 条，共 65 条")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页" })).toBeEnabled();
    expect(screen.getByRole("spinbutton", { name: "跳转到页码" })).toHaveValue(2);
    await act(async () => { await i18n.changeLanguage("en-US"); });
  });
});

describe("shared modal", () => {
  it("uses a unique accessible title for every mounted dialog", () => {
    render(<><Modal open title="First dialog" onClose={() => undefined}><p>First body</p></Modal><Modal open title="Second dialog" onClose={() => undefined}><p>Second body</p></Modal></>);
    const dialogs = screen.getAllByRole("dialog");
    const titleIds = dialogs.map((dialog) => dialog.getAttribute("aria-labelledby"));
    expect(new Set(titleIds).size).toBe(2);
    expect(document.getElementById(titleIds[0] ?? "")).toHaveTextContent("First dialog");
    expect(document.getElementById(titleIds[1] ?? "")).toHaveTextContent("Second dialog");
  });
});
