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
  it("shows a plain-language completed history without tool details", () => {
    render(<AgentActivityTimeline run={completedRun} />);

    const button = screen.getByRole("button", {
      name: "Show activity history · 2 steps",
    });
    expect(button).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(button);

    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByText("Searching the data catalog")).toHaveLength(2);
    expect(screen.queryByText("dbrag-search_catalog")).not.toBeInTheDocument();
    expect(screen.queryByText(/Call 1|Call 2/)).not.toBeInTheDocument();
  });

  it("keeps the newest running status visible while its history is collapsed", () => {
    const runningRun: ActivityRun = {
      ...completedRun,
      id: "run-3",
      state: "running",
      activities: [
        ...completedRun.activities,
        {
          ...completedRun.activities[1],
          id: "activity-running",
          sequence: 3,
          label: "Checking table relationships",
          status: "running",
        },
      ],
    };
    const { container } = render(<AgentActivityTimeline run={runningRun} />);

    expect(screen.getByText("Checking table relationships")).toBeInTheDocument();
    expect(
      container.querySelector(".agent-activity-summary-indicator--running"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("button"));

    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Checking table relationships")).toBeInTheDocument();
    expect(container.querySelector(".agent-activity-history")).toBeNull();
  });

  it("uses a waiting status above completed history without a spinner", () => {
    const { container } = render(
      <AgentActivityTimeline
        run={{
          ...waitingRun,
          activities: [...completedRun.activities, ...waitingRun.activities],
        }}
      />,
    );

    expect(
      screen.getByRole("button", { name: /Hide activity history · 3 steps/ }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(container.querySelector('[aria-live="polite"]')).toHaveTextContent(
      "Waiting for dataset approval",
    );
    expect(
      container.querySelector(".agent-activity-summary-indicator--running"),
    ).toBeNull();
    expect(container.querySelector(".agent-activity-history")).toBeInTheDocument();
    expect(screen.getAllByText("Waiting for dataset approval")).toHaveLength(1);
  });

  it("collapses terminal work and removes the running summary indicator", () => {
    const { container, rerender } = render(
      <AgentActivityTimeline run={{ ...completedRun, state: "running" }} />,
    );
    expect(
      container.querySelector(".agent-activity-summary-indicator--running"),
    ).toBeInTheDocument();

    rerender(<AgentActivityTimeline run={completedRun} />);

    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
    expect(
      container.querySelector(".agent-activity-summary-indicator--running"),
    ).toBeNull();
    expect(container.querySelector(".agent-activity-summary-terminal")).toHaveTextContent(
      "✓",
    );
  });
});
