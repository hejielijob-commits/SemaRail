import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { AgentTraceDetail, TraceComparisonResponse } from "../types";
import comparisonFixture from "./fixtures/trace-comparison-unmatched.json";
import persisted from "./fixtures/persisted-trace.json";
import TraceComparisonWorkbench from "./TraceComparisonWorkbench";

const comparison = comparisonFixture as TraceComparisonResponse;
const fixture = persisted as AgentTraceDetail;

describe("TraceComparisonWorkbench", () => {
  afterEach(() => vi.restoreAllMocks());

  it("compares two manually entered IDs in the current project and keeps unmatched nodes visible", async () => {
    const compareTraces = vi.spyOn(api, "compareTraces").mockResolvedValue(comparison);
    vi.spyOn(api, "getTrace").mockImplementation(async (id) => ({
      ...fixture,
      id,
      coreDiagnostics: fixture.coreDiagnostics,
    }));
    render(<TraceComparisonWorkbench locale="en-US" adminToken="admin" projectName="Revenue intelligence" />);

    expect(screen.getByText("Revenue intelligence")).toBeInTheDocument();
    expect(compareTraces).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/Left trace ID/), { target: { value: "atr_fixture_left" } });
    fireEvent.change(screen.getByLabelText(/Right trace ID/), { target: { value: "atr_fixture_right" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare traces" }));

    expect(await screen.findByText("Matched activity")).toBeInTheDocument();
    expect(await screen.findByText("Only in left trace")).toBeInTheDocument();
    expect(await screen.findByText("Only in right trace")).toBeInTheDocument();
    expect(await screen.findByText("call-retry")).toBeInTheDocument();
    expect((await screen.findAllByText("agent-repair")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("semantic-13").length).toBeGreaterThan(0);
    expect(screen.getByText("Core phase durations")).toBeInTheDocument();
    expect(screen.getAllByText("Updated", { selector: ".badge" }).length).toBeGreaterThan(0);
    expect(within(screen.getByRole("table")).getByText("State changed")).toBeInTheDocument();
    await waitFor(() => expect(compareTraces).toHaveBeenCalledWith("atr_fixture_left", "atr_fixture_right"));
    expect(api.getTrace).toHaveBeenCalledWith("atr_fixture_left");
    expect(api.getTrace).toHaveBeenCalledWith("atr_fixture_right");
  });

  it("requires two different trace IDs before sending a comparison request", async () => {
    const compareTraces = vi.spyOn(api, "compareTraces");
    render(<TraceComparisonWorkbench locale="en-US" adminToken="admin" projectName="Revenue intelligence" />);
    fireEvent.change(screen.getByLabelText(/Left trace ID/), { target: { value: "same-id" } });
    fireEvent.change(screen.getByLabelText(/Right trace ID/), { target: { value: "same-id" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare traces" }));

    expect(await screen.findByText("Enter two different trace IDs.")).toBeInTheDocument();
    expect(compareTraces).not.toHaveBeenCalled();
  });

  it("shows uncollected version changes instead of declaring equality", async () => {
    vi.spyOn(api, "compareTraces").mockResolvedValue({
      ...comparison,
      left: { ...comparison.left, model: null, semanticVersion: null, semanticVersions: [], policyVersions: [] },
      right: { ...comparison.right, semanticVersion: null, semanticVersions: ["revision-a", "revision-b"] },
      versionChanges: { model: null, semanticVersion: null, policyVersions: null },
    });
    vi.spyOn(api, "getTrace").mockResolvedValue(fixture);
    const { container } = render(<TraceComparisonWorkbench locale="en-US" adminToken="admin" projectName="Fixture" />);
    fireEvent.change(screen.getByLabelText(/Left trace ID/), { target: { value: "left" } });
    fireEvent.change(screen.getByLabelText(/Right trace ID/), { target: { value: "right" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare traces" }));
    await screen.findByText("Version changes");
    const versions = container.querySelector(".trace-compare-versions")!;
    expect(within(versions as HTMLElement).getAllByText("Not collected", { selector: ".badge" })).toHaveLength(3);
    expect(within(versions as HTMLElement).queryByText("Unchanged")).not.toBeInTheDocument();
    expect(screen.getAllByText("revision-a, revision-b").length).toBeGreaterThan(0);
  });
});
