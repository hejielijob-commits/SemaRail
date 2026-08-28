import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import ViewWorkbench, { type ViewWorkbenchProps } from "./ViewWorkbench";
import type { ViewDefinition, ViewSnapshot } from "../types";

vi.mock("@uiw/react-codemirror", () => ({
  default: ({ value, onChange, "aria-label": label }: { value: string; onChange: (value: string) => void; "aria-label"?: string }) => (
    <textarea aria-label={label ?? "SQL editor"} value={value} onChange={(event) => onChange(event.target.value)} />
  ),
}));

function view(overrides: Partial<ViewDefinition> = {}): ViewDefinition {
  return {
    name: "daily_orders",
    sourcePath: "views/daily_orders/metadata.yml",
    sqlPath: "views/daily_orders/sql.yml",
    statement: "SELECT id, ordered_at FROM orders",
    statementSource: "sql",
    storage: "sql",
    properties: { description: "Daily order dataset", tags: ["sales"] },
    draft: false,
    ...overrides,
  };
}

function snapshot(views = [view()]): ViewSnapshot {
  return { schemaVersion: 1, revision: "sha256:one", draftCount: 0, views, sourceFiles: [] };
}

function props(overrides: Partial<ViewWorkbenchProps> = {}): ViewWorkbenchProps {
  return {
    snapshot: snapshot(),
    modelNames: ["orders", "customers"],
    onSave: vi.fn(async () => undefined),
    onCreate: vi.fn(async () => undefined),
    onDelete: vi.fn(async () => undefined),
    onValidate: vi.fn(async () => ({ valid: true, errorCount: 0, warningCount: 0, errors: [], warnings: [] })),
    onPreview: vi.fn(async () => ({ status: "PREVIEW_UNAVAILABLE" as const, message: "runtime missing" })),
    onOpenSource: vi.fn(),
    onLoadDiff: vi.fn(),
    ...overrides,
  };
}

describe("ViewWorkbench", () => {
  it("renders the real Wren View contract in English and Chinese", () => {
    const { rerender } = render(<ViewWorkbench {...props()} />);
    expect(screen.getByRole("heading", { name: "Views", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "daily_orders", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Definition/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByDisplayValue("views/daily_orders/metadata.yml")).toHaveProperty("readOnly", true);
    expect(screen.getByLabelText("View SQL")).toHaveValue("SELECT id, ordered_at FROM orders");

    rerender(<ViewWorkbench {...props({ locale: "zh-CN" })} />);
    expect(screen.getByRole("heading", { name: "视图", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /定义/ })).toBeInTheDocument();
    expect(screen.getByLabelText("视图 SQL")).toBeInTheDocument();
  });

  it("keeps a local SQL draft and sends it only when Save draft is chosen", async () => {
    const onSave = vi.fn(async (_view: ViewDefinition) => undefined);
    render(<ViewWorkbench {...props({ onSave })} />);

    fireEvent.change(screen.getByLabelText("View SQL"), { target: { value: "SELECT id FROM orders WHERE status = 'paid'" } });
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onSave.mock.calls[0][0]).toMatchObject({
      name: "daily_orders",
      statement: "SELECT id FROM orders WHERE status = 'paid'",
      storage: "sql",
      properties: { description: "Daily order dataset", tags: ["sales"] },
    });
  });

  it("shows server validation issues without writing the View", async () => {
    const onValidate = vi.fn(async () => ({
      valid: false,
      errorCount: 1,
      warningCount: 0,
      errors: [{ path: "statement", code: "INVALID_SQL", message: "view statement must be one read-only query", severity: "error" }],
      warnings: [],
    }));
    const onSave = vi.fn();
    render(<ViewWorkbench {...props({ onValidate, onSave })} />);

    fireEvent.click(screen.getByRole("button", { name: "Validate" }));

    await waitFor(() => expect(screen.getByText("View definition needs attention")).toBeInTheDocument());
    expect(screen.getByText("view statement must be one read-only query")).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("creates and deletes only after explicit modal actions", async () => {
    const onCreate = vi.fn(async () => undefined);
    const onDelete = vi.fn(async () => undefined);
    const initialProps = props({ onCreate, onDelete });
    const { rerender } = render(<ViewWorkbench {...initialProps} />);

    fireEvent.click(screen.getByRole("button", { name: "New view" }));
    const createDialog = screen.getByRole("dialog", { name: "Create a view" });
    fireEvent.change(within(createDialog).getByLabelText("Technical name"), { target: { value: "paid_orders" } });
    fireEvent.change(within(createDialog).getByLabelText("View SQL"), { target: { value: "SELECT * FROM orders WHERE status = 'paid'" } });
    fireEvent.click(within(createDialog).getByRole("button", { name: "Create draft" }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({ name: "paid_orders", storage: "sql", expectedRevision: "sha256:one" })));

    rerender(<ViewWorkbench {...initialProps} snapshot={snapshot([view(), view({ name: "paid_orders", sourcePath: "views/paid_orders/metadata.yml", sqlPath: "views/paid_orders/sql.yml", statement: "SELECT * FROM orders WHERE status = 'paid'", draft: true })])} />);
    fireEvent.click(screen.getByRole("option", { name: /daily_orders/ }));

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onDelete).not.toHaveBeenCalled();
    const deleteDialog = screen.getByRole("dialog", { name: "Delete this view?" });
    expect(deleteDialog).toHaveTextContent("views/daily_orders/metadata.yml");
    fireEvent.click(within(deleteDialog).getByRole("button", { name: "Delete view" }));
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith(expect.objectContaining({ name: "daily_orders" })));
  });

  it("renders a real bounded preview and never fabricates unavailable rows", async () => {
    const onPreview = vi.fn(async () => ({
      schemaVersion: 1,
      queryId: "preview-1",
      status: "success" as const,
      semanticSql: 'SELECT * FROM "daily_orders"',
      nativeSql: "SELECT 1",
      columns: [{ name: "order_count", type: "BIGINT", semanticRole: "measure" }],
      previewRows: [{ order_count: "12" }, { order_count: null }],
      stats: { returnedRows: 2, durationMs: 12.4, truncated: true },
    }));
    render(<ViewWorkbench {...props({ onPreview })} />);
    fireEvent.click(screen.getByRole("tab", { name: /Preview/ }));
    fireEvent.click(screen.getAllByRole("button", { name: "Run preview" })[0]);

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.getByText("order_count")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("NULL")).toBeInTheDocument();
    expect(screen.getByText("Bounded result")).toBeInTheDocument();

    const unavailable = props({ onPreview: vi.fn(async () => ({ status: "PREVIEW_UNAVAILABLE" as const, message: "safe runtime missing" })) });
    const { unmount } = render(<ViewWorkbench {...unavailable} />);
    fireEvent.click(screen.getAllByRole("tab", { name: /Preview/ }).at(-1)!);
    fireEvent.click(screen.getAllByRole("button", { name: "Run preview" }).at(-1)!);
    await waitFor(() => expect(screen.getByText("safe runtime missing")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Retry preview" })).toBeInTheDocument();
    expect(screen.queryByText("sample row")).not.toBeInTheDocument();
    unmount();
  });

  it("distinguishes a failed preview from an unavailable runtime and offers retry", async () => {
    render(<ViewWorkbench {...props({ onPreview: vi.fn(async () => ({ status: "error" as const, message: "query timed out" })) })} />);
    fireEvent.click(screen.getByRole("tab", { name: /Preview/ }));
    fireEvent.click(screen.getAllByRole("button", { name: "Run preview" })[0]);

    await waitFor(() => expect(screen.getByText("Preview failed")).toBeInTheDocument());
    expect(screen.getByText("query timed out")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry preview" })).toBeInTheDocument();
    expect(screen.queryByText("Runtime preview is unavailable")).not.toBeInTheDocument();
  });

  it("shows nested View references as unsupported and supports tab keyboard navigation", () => {
    render(<ViewWorkbench {...props({ snapshot: snapshot([
      view(),
      view({ name: "order_summary", sourcePath: "views/order_summary/metadata.yml", sqlPath: "views/order_summary/sql.yml", statement: "SELECT * FROM daily_orders" }),
    ]) })} />);
    fireEvent.click(screen.getByRole("option", { name: /order_summary/ }));
    const previewTab = screen.getByRole("tab", { name: /Preview/ });
    previewTab.focus();
    fireEvent.keyDown(previewTab, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: /Dependencies/ })).toHaveAttribute("aria-selected", "true");
    const dependencies = screen.getByRole("tabpanel", { name: /Dependencies/ });
    expect(within(dependencies).getByText("daily_orders")).toBeInTheDocument();
    expect(screen.getByText(/Nested View reference is not supported/)).toBeInTheDocument();
  });

  it("loads source and diff lazily for the selected file", () => {
    const onOpenSource = vi.fn();
    const onLoadDiff = vi.fn();
    render(<ViewWorkbench {...props({ onOpenSource, onLoadDiff, sourceContent: "statement: SELECT 1", diff: { path: "views/daily_orders/sql.yml", changed: true, diff: "+statement: SELECT 1", revision: "two" } })} />);

    fireEvent.click(screen.getByRole("tab", { name: /Source/ }));
    expect(onOpenSource).toHaveBeenCalledWith("views/daily_orders/metadata.yml");
    fireEvent.click(screen.getByRole("button", { name: /sql.yml/ }));
    expect(onOpenSource).toHaveBeenCalledWith("views/daily_orders/sql.yml");
    expect(screen.getByText("statement: SELECT 1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    expect(onLoadDiff).toHaveBeenCalledWith("views/daily_orders/sql.yml");
    expect(screen.getByText("+statement: SELECT 1")).toBeInTheDocument();
  });

  it("covers loading, empty, no-match, and error states", () => {
    const { rerender } = render(<ViewWorkbench {...props({ loading: true })} />);
    expect(screen.getByRole("status", { name: "Loading Views" })).toBeInTheDocument();

    rerender(<ViewWorkbench {...props({ loading: false, snapshot: snapshot([]) })} />);
    expect(screen.getByText("No views yet")).toBeInTheDocument();

    const onRetry = vi.fn();
    rerender(<ViewWorkbench {...props({ error: "API unavailable", onRetry })} />);
    expect(screen.getByRole("alert")).toHaveTextContent("API unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();

    rerender(<ViewWorkbench {...props()} />);
    fireEvent.change(screen.getByLabelText("Search views"), { target: { value: "missing" } });
    expect(screen.getByText("No matching views")).toBeInTheDocument();
  });
});
