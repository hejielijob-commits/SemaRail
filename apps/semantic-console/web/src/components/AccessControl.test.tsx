import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccessControl from "./AccessControl";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: {
    setBearerToken: vi.fn(),
    getServiceAccounts: vi.fn(),
    getUsers: vi.fn(),
    getAccessPolicies: vi.fn(),
    getAccessAudit: vi.fn(),
    issueServiceAccountKey: vi.fn(),
    updateUser: vi.fn(),
    setUserStatus: vi.fn(),
    bindAccessPolicy: vi.fn(),
    unbindAccessPolicy: vi.fn(),
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
const employee = {
  id: "user-a",
  organizationId: "org-default",
  type: "user" as const,
  name: "Employee A",
  attributes: { regionCodes: ["CN-JIA"] },
  status: "active" as const,
  identities: [{ provider: "dingtalk", externalSubject: "union-a", profile: { employeeNumber: "A001" }, lastLoginAt: "2026-08-30T00:00:00Z" }],
  policyIds: [],
};

describe("AccessControl", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getServiceAccounts).mockResolvedValue({ items: [account] });
    vi.mocked(api.getUsers).mockResolvedValue({ items: [employee] });
    vi.mocked(api.getAccessPolicies).mockResolvedValue({ items: [policy] });
    vi.mocked(api.getAccessAudit).mockResolvedValue({ items: [] });
    vi.mocked(api.issueServiceAccountKey).mockResolvedValue({
      apiKey: "sr_live_plaintext_shown_once",
      credential: { id: "credential-1", subjectId: account.id, label: "console", createdAt: "2026-08-30T00:00:00Z" },
    });
    vi.mocked(api.updateUser).mockResolvedValue(employee);
    vi.mocked(api.setUserStatus).mockResolvedValue({ ...employee, status: "disabled" });
    vi.mocked(api.bindAccessPolicy).mockResolvedValue({ subjectId: employee.id, policyId: policy.id });
    vi.mocked(api.unbindAccessPolicy).mockResolvedValue({ subjectId: account.id, policyId: policy.id, status: "unbound" });
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
    expect(api.setBearerToken).toHaveBeenCalledWith(adminToken);
    expect(api.getUsers).toHaveBeenCalledWith(adminToken);
    expect(api.getAccessPolicies).toHaveBeenCalledWith(adminToken);
    expect(api.getAccessAudit).toHaveBeenCalledWith(adminToken);
    expect(screen.queryByDisplayValue(adminToken)).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(adminToken);
  });

  it("shows employees from external identity login and saves administrator-controlled regions", async () => {
    render(<AccessControl locale="en-US" />);
    fireEvent.change(screen.getByLabelText("Administrator token"), { target: { value: adminToken } });
    fireEvent.click(screen.getByRole("button", { name: "Open access control" }));
    await screen.findAllByText("Sales agent A");

    fireEvent.click(screen.getByRole("tab", { name: "Employees" }));
    expect((await screen.findAllByText("Employee A")).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/dingtalk/).length).toBeGreaterThan(0);
    const regions = screen.getByLabelText("Region codes");
    expect(regions).toHaveValue("CN-JIA");
    fireEvent.change(regions, { target: { value: "CN-YI,CN-BEI" } });
    fireEvent.click(screen.getByRole("button", { name: "Save access attributes" }));

    await waitFor(() => expect(api.updateUser).toHaveBeenCalledWith(adminToken, employee.id, {
      attributes: { regionCodes: ["CN-YI", "CN-BEI"] },
    }));
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

  it("disables an employee immediately and binds a data policy", async () => {
    render(<AccessControl locale="en-US" />);
    fireEvent.change(screen.getByLabelText("Administrator token"), { target: { value: adminToken } });
    fireEvent.click(screen.getByRole("button", { name: "Open access control" }));
    await screen.findAllByText("Sales agent A");
    fireEvent.click(screen.getByRole("tab", { name: "Employees" }));

    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(api.setUserStatus).toHaveBeenCalledWith(adminToken, employee.id, "disabled"));

    fireEvent.change(screen.getByRole("combobox"), { target: { value: policy.id } });
    fireEvent.click(screen.getByRole("button", { name: "Bind policy" }));
    await waitFor(() => expect(api.bindAccessPolicy).toHaveBeenCalledWith(adminToken, employee.id, policy.id));
  });

  it("removes a bound policy without disabling the whole service account", async () => {
    render(<AccessControl locale="en-US" />);
    fireEvent.change(screen.getByLabelText("Administrator token"), { target: { value: adminToken } });
    fireEvent.click(screen.getByRole("button", { name: "Open access control" }));
    await screen.findAllByText("Sales agent A");

    fireEvent.click(screen.getByRole("button", { name: "Unbind Sales region policy" }));

    await waitFor(() => expect(api.unbindAccessPolicy).toHaveBeenCalledWith(adminToken, account.id, policy.id));
  });

  it("binds a new policy template to the active server datasource", async () => {
    render(<AccessControl locale="en-US" adminToken={adminToken} activeDatasourceId="datasource-sales" />);
    await screen.findAllByText("Sales agent A");

    fireEvent.click(screen.getByRole("tab", { name: "Policies" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Create policy" })[0]);

    expect(JSON.parse((screen.getByLabelText("Policy document (JSON)") as HTMLTextAreaElement).value)).toMatchObject({
      schemaVersion: 1,
      datasourceId: "datasource-sales",
    });
  });
});
