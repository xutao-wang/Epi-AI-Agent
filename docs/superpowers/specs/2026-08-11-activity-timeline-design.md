# Activity Timeline Design

## Purpose

Keep the user oriented during long, tool-heavy agent runs without exposing internal implementation details or allowing the growing trace to push the current status out of view.

## User experience

The activity timeline has a persistent summary strip and an optional history area.

- While a run is active, the summary strip always shows an animated spinner and the latest plain-language activity label. It is visible whether the history is expanded or collapsed.
- The summary control toggles the activity history and includes the total number of recorded steps: `Hide activity history · 23 steps` or `Show activity history · 23 steps`.
- When expanded, the latest active activity remains at the top of the history. Earlier completed activity is shown below it in a bounded, internally scrollable region. Incoming events therefore do not move the current state out of view or require users to scroll the page to follow progress.
- When a run completes, is cancelled, or errors, the timeline collapses automatically. The summary remains available, with a terminal icon instead of an active spinner.
- Waiting work shows a waiting state rather than an indeterminate running spinner.

## Information shown

Only existing human-readable activity labels are displayed. The interface does not render tool names, tool call IDs, or repeated-call counters. Users can trace the work through labels such as `Inspecting a database table` and `Checking table relationships`.

## Component behavior

`AgentActivityTimeline` derives the newest running or waiting activity from its sorted run activities. It uses that activity in the summary strip and, when the history is open, renders it separately before the scrollable completed history. The remaining activities retain sequence order in the history.

The toggle stays keyboard-accessible and maintains its `aria-expanded` state. The current live label retains polite live-region announcement behavior so assistive technology receives progress updates without repeatedly announcing the whole trace.

## Styling

The summary strip is visually distinct, but compact. The history has a maximum height with vertical overflow and a clear divider from the summary. The active item uses the existing spinner styling; completed and waiting states retain their existing semantic colors and icons. Focus styling remains visible.

## Error handling

No activity records is a valid state: the summary gives a generic working message while a run is active, and the history is omitted. Terminal runs continue to be readable even if no explicit completed activity is present.

## Testing

Frontend component tests will verify:

1. A collapsed live run shows the spinner and latest plain-language status.
2. Expanding the run keeps the active item visible above the completed history.
3. Internal tool names and call counters are absent in both states.
4. The toggle expands and collapses the history accessibly.
5. A terminal transition collapses the history and removes the active spinner.
