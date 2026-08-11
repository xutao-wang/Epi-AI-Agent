import { useEffect, useMemo, useRef, useState } from "react";
import type { ActivityRun, ActivityRunState } from "./types";


function isActive(state: ActivityRunState): boolean {
  return state === "running" || state === "waiting";
}

export default function AgentActivityTimeline({ run }: { run: ActivityRun }) {
  const active = isActive(run.state);
  const previousState = useRef(run.state);
  const [expanded, setExpanded] = useState(active);
  const activities = useMemo(
    () => [...run.activities].sort((left, right) => left.sequence - right.sequence),
    [run.activities],
  );
  const toolTotals = useMemo(() => {
    const totals = new Map<string, number>();
    for (const activity of activities) {
      if (activity.tool_name) {
        totals.set(activity.tool_name, (totals.get(activity.tool_name) ?? 0) + 1);
      }
    }
    return totals;
  }, [activities]);
  const toolOccurrences = new Map<string, number>();
  const liveActivity = [...activities]
    .reverse()
    .find((activity) => activity.status === "running" || activity.status === "waiting");

  useEffect(() => {
    const wasActive = isActive(previousState.current);
    if (active) {
      setExpanded(true);
    } else if (wasActive) {
      setExpanded(false);
    }
    previousState.current = run.state;
  }, [active, run.state]);

  const countLabel = `${activities.length} ${
    activities.length === 1 ? "activity" : "activities"
  }`;
  const actionLabel = expanded ? "Hide agent activity" : "View agent activity";

  return (
    <li className={`agent-activity agent-activity--${run.state}`}>
      <button
        type="button"
        className="agent-activity__summary"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        {actionLabel} · {countLabel}
      </button>
      {expanded ? (
        <ol className="agent-activity__items">
          {activities.map((activity) => {
            let occurrence: number | null = null;
            if (activity.tool_name) {
              occurrence = (toolOccurrences.get(activity.tool_name) ?? 0) + 1;
              toolOccurrences.set(activity.tool_name, occurrence);
            }
            const repeated = Boolean(
              activity.tool_name && (toolTotals.get(activity.tool_name) ?? 0) > 1,
            );
            return (
              <li
                key={activity.id}
                className={`agent-activity__item agent-activity__item--${activity.status}`}
              >
                <span className="agent-activity__icon" aria-hidden="true">
                  {activity.status === "completed"
                    ? "✓"
                    : activity.status === "waiting"
                      ? "⚠"
                      : ""}
                </span>
                <span
                  className="agent-activity__label"
                  aria-live={liveActivity?.id === activity.id ? "polite" : undefined}
                >
                  {activity.label}
                </span>
                {activity.tool_name ? (
                  <span className="agent-activity__technical">
                    <code>{activity.tool_name}</code>
                    {repeated ? <span>Call {occurrence}</span> : null}
                  </span>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : null}
    </li>
  );
}
