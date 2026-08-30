import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccessControl from "./AccessControl";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: {
    getServiceAccounts: vi.fn(),
    getAccessPolicies: vi.fn(),
    getAccessAudit: vi.fn(),
    issueServiceAccountKey: vi.fn(),
  },
}));

const adminToken = "admin-token-that-is-long-enough-123456";
const account = {
  id: "subject-a",
  organizationId: "org-default",
  type: "service_account" as const,
  name: "Sales agent A",
  attributes: { regionCodes: ["CN-JIA"] },
  status: "active" as const,
  credentials: [],
  policyIds: ["policy-sales"],
};
const policy = {
  id: "policy-sales",
  organizationId: "org-default",
  name: "Sales region policy",
  version: 1,
  document: { schemaVersion: 1 },
};

describe("AccessControl", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getServiceAccounts).mockResolvedValue({ items: [account] });
    vi.mocked(api.getAccessPolicies).mockResolvedValue({ items: [policy] });
    vi.mocked(api.getAccessAudit).mockResolvedValue({ items: [] });
    vi.mocked(api.issueServiceAccountKey).mockResolvedValue({
      apiKey: "sr_live_plaintext_shown_once",
      credential: { id: "credential-1", subjectId: account.id, label: "console", createdAt: "2026-08-30T00:00:00Z" },
    });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
  });

  it("does not call protected APIs until the administrator token is submitted", async () => {
    render(<AccessControl locale="en-US" />);

    expect(api.getServiceAccounts).not.toHaveBeenCalled();
    const input = screen.getByLabelText("Administrator token");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");

    fireEvent.change(input, { target: { value: adminToken } });
    fireEvent.click(screen.getByRole("button", { name: "Open access control" }));

    expect((await screen.findAllByText("Sales agent A")).length).toBeGreaterThan(0);
    expect(api.getServiceAccounts).toHaveBeenCalledWith(adminToken);
    expect(api.getAccessPolicies).toHaveBeenCalledWith(adminToken);
    expect(api.getAccessAudit).toHaveBeenCalledWith(adminToken);
    expect(screen.queryByDisplayValue(adminToken)).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(adminToken);
  });

  it("shows an issued API key once and never mixes it into the account record", async () => {
    render(<AccessControl locale="en-US" />);
    fireEvent.change(screen.getByLabelText("Administrator token"), { target: { value: adminToken } });
    fireEvent.click(screen.getByRole("button", { name: "Open access control" }));
    await screen.findAllByText("Sales agent A");

    fireEvent.click(screen.getByRole("button", { name: "Issue key" }));
    expect(await screen.findByText("sr_live_plaintext_shown_once")).toBeInTheDocument();
    expect(api.issueServiceAccountKey).toHaveBeenCalledWith(adminToken, account.id, "console");

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("sr_live_plaintext_shown_once"));
    fireEvent.click(screen.getByRole("button", { name: "I saved the key" }));
    expect(screen.queryByText("sr_live_plaintext_shown_once")).not.toBeInTheDocument();
  });
});
