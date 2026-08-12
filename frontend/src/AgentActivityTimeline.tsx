import { useEffect, useMemo, useRef, useState } from "react";
import type { ActivityItem as ActivityItemType, ActivityRun, ActivityRunState } from "./types";


function isActive(state: ActivityRunState): boolean {
  return state === "running" || state === "waiting";
}

function ActivityTimelineItem({ activity }: { activity: ActivityItemType }) {
  return (
    <li className={`agent-activity-item agent-activity-item--${activity.status}`}>
      <span className="agent-activity-status" aria-hidden="true">
        {activity.status === "completed"
          ? "✓"
          : activity.status === "waiting"
            ? "⚠"
            : ""}
      </span>
      <span className="agent-activity-label">{activity.label}</span>
    </li>
  );
}

export default function AgentActivityTimeline({ run }: { run: ActivityRun }) {
  const active = isActive(run.state);
  const previousState = useRef(run.state);
  const [expanded, setExpanded] = useState(active);
  const activities = useMemo(
    () => [...run.activities].sort((left, right) => left.sequence - right.sequence),
    [run.activities],
  );
  const liveActivity = [...activities]
    .reverse()
    .find((activity) => activity.status === "running" || activity.status === "waiting");
  const historyActivities = liveActivity
    ? activities.filter((activity) => activity.id !== liveActivity.id)
    : activities;
  const latestLabel = liveActivity?.label ?? "Working on your request";
  const indicatorStatus = liveActivity?.status ?? (run.state === "waiting" ? "waiting" : "running");

  useEffect(() => {
    const wasActive = isActive(previousState.current);
    if (active) {
      setExpanded(true);
    } else if (wasActive) {
      setExpanded(false);
    }
    previousState.current = run.state;
  }, [active, run.state]);

  const countLabel = `${activities.length} ${activities.length === 1 ? "step" : "steps"}`;
  const actionLabel = expanded ? "Hide activity history" : "Show activity history";

  return (
    <li
      aria-label="Agent activity timeline"
      className={`message message-assistant agent-activity-timeline agent-activity--${run.state}`}
    >
      <button
        type="button"
        className="agent-activity-summary"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <span className="agent-activity-summary-content">
          {active ? (
            <span
              aria-hidden="true"
              className={`agent-activity-summary-indicator agent-activity-summary-indicator--${indicatorStatus}`}
            >
              {indicatorStatus === "waiting" ? "⚠" : null}
            </span>
          ) : (
            <span aria-hidden="true" className="agent-activity-summary-terminal">
              {run.state === "completed" ? "✓" : run.state === "error" ? "!" : "–"}
            </span>
          )}
          <span className="agent-activity-summary-text">
            <span className="agent-activity-summary-action">
              {actionLabel} · {countLabel}
            </span>
            {active ? (
              <span aria-live="polite" className="agent-activity-summary-status">
                {latestLabel}
              </span>
            ) : null}
          </span>
        </span>
      </button>
      {expanded && historyActivities.length > 0 ? (
        <div className="agent-activity-history">
          <ol className="agent-activity-list agent-activity-list--completed">
            {historyActivities.map((activity) => (
              <ActivityTimelineItem activity={activity} key={activity.id} />
            ))}
          </ol>
        </div>
      ) : null}
    </li>
  );
}
