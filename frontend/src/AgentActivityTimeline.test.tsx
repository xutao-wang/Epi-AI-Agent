import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AgentActivityTimeline from "./AgentActivityTimeline";
import type { ActivityRun } from "./types";


const completedRun: ActivityRun = {
  id: "run-1",
  thread_id: "thread-1",
  user_message_id: "user-1",
  state: "completed",
  created_at: "2026-08-11T00:00:00+00:00",
  updated_at: "2026-08-11T00:00:03+00:00",
  activities: [
    {
      id: "activity-1",
      sequence: 1,
      label: "Searching the data catalog",
      status: "completed",
      tool_name: "dbrag-search_catalog",
      tool_call_id: "call-1",
      created_at: "2026-08-11T00:00:00+00:00",
      updated_at: "2026-08-11T00:00:01+00:00",
    },
    {
      id: "activity-2",
      sequence: 2,
      label: "Searching the data catalog",
      status: "completed",
      tool_name: "dbrag-search_catalog",
      tool_call_id: "call-2",
      created_at: "2026-08-11T00:00:02+00:00",
      updated_at: "2026-08-11T00:00:03+00:00",
    },
  ],
};

const waitingRun: ActivityRun = {
  ...completedRun,
  id: "run-2",
  state: "waiting",
  activities: [
    {
      ...completedRun.activities[0],
      id: "activity-waiting",
      label: "Waiting for dataset approval",
      status: "waiting",
      tool_name: "dbrag-request_dataset_review",
      tool_call_id: "review-1",
    },
  ],
};

describe("AgentActivityTimeline", () => {
  it("collapses a completed run and reveals repeated technical calls", () => {
    render(<AgentActivityTimeline run={completedRun} />);

    const button = screen.getByRole("button", {
      name: "View agent activity · 2 activities",
    });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("dbrag-search_catalog")).not.toBeInTheDocument();

    fireEvent.click(button);

    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByText("Searching the data catalog")).toHaveLength(2);
    expect(screen.getAllByText("dbrag-search_catalog")).toHaveLength(2);
    expect(screen.getByText("Call 1")).toBeInTheDocument();
    expect(screen.getByText("Call 2")).toBeInTheDocument();
  });

  it("starts waiting work expanded with a polite status update", () => {
    const { container } = render(<AgentActivityTimeline run={waitingRun} />);

    expect(
      screen.getByRole("button", { name: "Hide agent activity · 1 activity" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Waiting for dataset approval")).toBeInTheDocument();
    expect(container.querySelector('[aria-live="polite"]')).toHaveTextContent(
      "Waiting for dataset approval",
    );
    expect(screen.queryByText(/failed/i)).not.toBeInTheDocument();
  });

  it("collapses when live work transitions to a terminal state", () => {
    const { rerender } = render(
      <AgentActivityTimeline run={{ ...completedRun, state: "running" }} />,
    );
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");

    rerender(<AgentActivityTimeline run={completedRun} />);

    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps cancelled work collapsed without a running indicator", () => {
    const { container } = render(
      <AgentActivityTimeline
        run={{
          ...completedRun,
          state: "cancelled",
          activities: [
            { ...completedRun.activities[0], status: "completed" },
          ],
        }}
      />,
    );

    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
    expect(container.querySelector(".agent-activity-item--running")).toBeNull();
  });
});
