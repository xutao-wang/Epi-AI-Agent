# Agent Activity Timeline Design

## Goal

Show users a small, truthful timeline of the agent's visible workflow while a
request is running. The timeline shows generic model activity, successful tool
calls, repeated calls, and human-review pauses without exposing private model
reasoning, raw tool data, or recoverable tool failures.

The first version targets the current single-EC2, single-process deployment. It
uses the existing one-second polling flow and SQLite storage. It does not add
SSE, token streaming, a separate worker, or multi-instance coordination.

## Current Behavior

The API accepts a message, starts LangGraph execution on a background Python
thread, and returns the current thread state. The browser polls that state every
second while the run is active.

The UI currently shows one generic activity card containing `Working on your
request` and a completed graph-step count. The backend does not expose which
tool is active or which tools have completed. Human interruptions already
project into dedicated clarification, dataset-plan review, dataset review,
analysis review, and model-output-limit interfaces.

## User-Facing Contract

Each user request owns one activity timeline. A timeline remains associated
with that request across human-review interruptions and resumptions.

While work is active, the timeline is expanded automatically. It contains
ordered entries such as:

```text
✓ Understanding your request
✓ Searching the data catalog
✓ Inspecting a database table
● Creating the dataset
```

When the workflow requires human input, the timeline remains expanded and its
current entry changes to a waiting state:

```text
✓ Checking dataset quality
⚠ Waiting for dataset approval
```

The existing review interface appears immediately after the waiting timeline.
After the user responds, the same timeline resumes. Approval may add a concise
entry such as `Dataset plan approved`; revision may add `Updating the dataset
plan`.

When the run completes, the timeline collapses automatically to a control such
as `View agent activity · 12 activities`. Reopening a completed conversation
keeps its completed timelines collapsed. Expanding a tool activity reveals the
fixed technical tool name and call occurrence, but not its arguments or result.

The final answer continues to appear as one completed response. Token-by-token
output is not part of this feature.

## Public Activity Content

Public activity is a curated operational trace, not model chain-of-thought.
The application may show:

- a generic model stage such as `Understanding your request` or `Choosing the
  next step`;
- a friendly label for an active or completed tool call;
- repeated successful calls as separate ordered activities;
- a human-review waiting state and the visible review outcome;
- a generic resume state after process recovery; and
- final run completion or the existing run-level error.

The public activity contract never contains:

- model prompts or private reasoning;
- tool arguments;
- raw tool output;
- SQL or provider errors;
- dataset rows or attachment contents;
- credentials or other secrets; or
- a recoverable tool-failure marker.

Friendly tool labels come from one central allowlist keyed by registered tool
name. For example:

```text
dbrag-search_catalog       -> Searching the data catalog
dbrag-inspect_table        -> Inspecting a database table
dbrag-find_join_paths      -> Finding relationships between tables
dbrag-profile_relationship -> Checking table relationships
dbrag-save_dataset_plan    -> Saving the dataset plan
dbrag-validate_dataset_plan -> Validating the dataset plan
dbrag-validate_and_extract -> Creating the dataset
dbrag-inspect_dataset      -> Checking dataset quality
analysis-run_custom_python -> Running statistical analysis
publication-search_pubmed  -> Searching PubMed
```

A registered tool missing from the allowlist receives a deterministic,
sanitized label derived from its fixed registered name. Model-produced text is
never used as a label.

## Architecture

Add an activity-tracking layer around the existing run and tool execution
boundaries. This layer records public activity without changing how the model
chooses tools or how tools execute.

The major components are:

1. **Activity store.** A focused backend module owns the SQLite schema and the
   operations for starting a request timeline, appending or updating an
   activity, hiding a recoverable failed attempt, pausing for review, resuming,
   completing a run, and deleting a conversation's records. It uses the same
   checkpoint database path already configured for the application.
2. **Activity presenter.** A small central mapping converts registered tool
   names into friendly labels and provides a safe fallback for unknown
   registered names. Individual tools do not gain presentation fields or
   custom summary logic.
3. **Runtime instrumentation.** The existing central model and tool execution
   boundaries report generic model stages and tool lifecycle changes. Each
   tool invocation uses its existing unique tool-call ID. Human-interruption
   projection reports waiting and visible decision states.
4. **API projection.** Thread state includes activity runs grouped by the user
   message that initiated them. Existing polling retrieves new and changed
   activities with the rest of the state.
5. **Timeline UI.** A focused React component renders ordered statuses, manages
   automatic expansion or collapse, and reveals technical tool details only
   when the user expands them.

Instrumentation is best-effort. Failure to save or read activity information
must not fail the agent run. The backend logs the activity-storage problem and
the frontend falls back to the existing generic working card.

## Data Contract

The public API adds activity runs to `ApiThreadState`. Each activity run
contains:

- a stable run identifier;
- the thread identifier;
- the initiating user-message identifier;
- the overall activity-run state;
- ordered activity items; and
- start and update times.

Each public activity item contains only:

- a stable activity identifier;
- its sequence within the run;
- a friendly label;
- an optional registered technical tool name;
- the existing tool-call ID when the entry represents a tool;
- a public status of `running`, `completed`, or `waiting`; and
- creation and update times.

Repeated tool calls receive separate activity items because their tool-call
IDs differ. The frontend keys items by stable activity identifier and replaces
an item's status on later polls instead of appending a duplicate.

Activity storage associates a resumed human-review execution with the original
request timeline. A review resume does not create a second timeline.

## Activity Flow

When a message is accepted:

1. Create an activity run linked to the durable user-message ID.
2. Add `Understanding your request` as the current activity.
3. Return the thread state and let the existing browser polling begin.

At a model boundary:

1. Keep the generic current activity while the model is running.
2. When the model selects a tool, complete the generic activity.
3. Create a running tool activity immediately before central tool execution.

At a tool boundary:

1. On success, update that exact tool-call activity to `completed`.
2. Add `Choosing the next step` while the model selects another action.
3. If another invocation uses the same tool, create a separate item with the
   new tool-call ID.

At a human interruption:

1. Replace the active interrupting-tool presentation with an appropriate
   `waiting` activity.
2. Keep the activity run open while the review interface is visible.
3. When the user responds, record the visible outcome, resume the same activity
   run, and continue normal model and tool instrumentation.

At final completion:

1. Complete the final generic stage.
2. Mark the activity run completed.
3. Return the final assistant response through the existing conversation
   projection.
4. Let the frontend collapse the now-completed timeline.

## Recoverable Tool Failures

Recoverable tool failures are internal agent control flow. They are logged for
developers but do not receive a public failure icon, error text, or technical
details.

If a visible running tool attempt fails recoverably:

1. Hide that attempt from the public timeline.
2. Restore `Choosing the next step` as the current public activity.
3. Let the model repair the request, retry the same tool, or select another
   tool.
4. Create a fresh running activity for the next actual tool call.

A user who happened to poll while the failed attempt was running may briefly
see its friendly running label; the next poll removes it without showing a
failure. A successful retry appears normally as a later activity.

If the overall run cannot recover, hide any unfinished tool activity and use
the existing run-level error contract. Previously completed activity may remain
visible, but the public response never includes the individual tool error.

## Restart and Conversation Lifecycle

Completed activity and review-waiting state survive process restart because
they are stored in SQLite.

When recovery finds a stale `running` item left by a stopped process, it hides
that item, adds `Resuming your request`, and continues the same activity run.
An unfinished tool is never relabeled as completed merely because the process
restarted.

Conversation lifecycle follows the existing ownership boundaries:

- archiving retains all activity history;
- restoring a conversation returns its completed timelines collapsed;
- deleting a conversation deletes its activity records;
- multiple browser tabs read the same persisted ordering; and
- reset behavior follows the resulting thread identity and does not attach an
  old thread's activities to a new thread.

The first version continues to require one application process and one Uvicorn
worker. Shared activity coordination for multiple processes or instances is a
future multi-instance scaling concern.

## UI Placement and Accessibility

The frontend associates each activity run with its initiating user-message ID
and places the timeline after that user message and before its assistant answer
or review card.

The active or waiting item uses an accessible live region so screen readers
receive concise status changes without rereading the entire timeline. Completed
items do not repeatedly announce on every poll. Status is communicated by text
and semantics in addition to icons or color.

Technical details use a user-controlled disclosure element. They show the
registered tool name and call occurrence only. The collapsed timeline control
reports the count of visible activities, not the existing internal graph-step
count.

## Failure Handling

Activity tracking is observational and cannot become a prerequisite for model
or tool execution.

- A write failure is logged and the agent continues.
- A read failure omits activity data from the state response and preserves the
  rest of the response.
- The frontend shows the existing generic working card when an active run has
  no usable timeline.
- Malformed or unrecognized activity records are rejected at the API schema
  boundary and logged rather than rendered.
- Raw exception messages remain in backend logs and existing protected
  diagnostics, never in public activity labels.

## Testing

Backend unit and contract tests must prove that:

- tool start and success create and update one activity;
- repeated calls use separate IDs and preserve order;
- a recoverable failure becomes hidden;
- an unrecoverable run failure does not expose tool details;
- human review changes the activity run to waiting;
- approval resumes the same activity run;
- activity survives store and runtime recreation;
- deletion removes only the owned conversation's activity;
- missing label mappings use a safe deterministic fallback; and
- activity-store failure does not fail graph execution or thread-state
  projection.

Frontend component and application tests must prove that:

- active and waiting timelines are expanded;
- completed timelines are collapsed;
- a user can expand a completed timeline;
- technical tool names appear only in disclosed details;
- polling updates existing items without duplication;
- repeated successful calls render separately;
- review cards appear after their waiting timeline;
- recoverable failures produce no public failure marker or raw error; and
- missing activity falls back to the existing generic working card.

The dedicated feature smoke test must launch the real FastAPI backend and
compiled frontend, submit a real database request, verify live activities in
the browser, inspect a technical tool name through the disclosure, reach a real
human-review interruption, and compare rendered activity with raw API state.
It must follow the repository's real-dependency and five-minute limits.

Every frontend change must rebuild the tracked production bundle and refresh
the build manifest using the repository-required commands.

## First-Version Limits

The first version intentionally does not include:

- SSE or WebSockets;
- token-by-token model output;
- raw tool inputs or outputs;
- model chain-of-thought;
- per-tool result-summary code;
- LLM-generated activity labels or summaries;
- public recoverable-failure events;
- changes to model tool-selection behavior;
- changes to individual tool execution behavior; or
- multi-process or multi-instance coordination.

The durable event and API contract are transport-independent, so SSE can be
added later without changing the meaning of an activity item or rewriting the
timeline component.
