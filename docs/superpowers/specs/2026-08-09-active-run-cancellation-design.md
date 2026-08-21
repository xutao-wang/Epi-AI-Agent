# Active Run Cancellation Design

## Goal

Let a user stop the work currently running in a conversation without starting a
new conversation or losing previously completed work. Cancellation returns the
conversation to its latest durable save point, keeps the cancelled request and
its user-supplied attachments visible, and discards unfinished agent work.

The design intentionally targets the current single-process, single-worker
application. It does not introduce a queue, Redis, a separate worker service, or
multi-instance cancellation coordination.

## Current Behavior

The API starts graph execution on a background Python thread and stores job
status in memory. LangGraph automatically saves node checkpoints to SQLite, but
the application does not identify which checkpoint is the latest user-approved
save point and has no way to cancel an active job.

The browser polls the thread state while the graph is running. **New
conversation** is disabled while work is active. When available, it clears the
browser's active conversation and causes a separate thread to be created when
the next message is submitted. It does not stop work in an existing thread.

Existing Cancel actions belong to paused clarification and review interactions.
They answer those interrupts; they do not stop an active model call, tool call,
or Python analysis.

## User-Facing Contract

Cancellation and New conversation have different meanings:

- **Cancel run** stays in the current conversation, keeps completed and approved
  work, discards unfinished work, and records the latest request as cancelled.
- **New conversation** leaves the existing conversation unchanged and opens a
  blank conversation.

While a run is active, the composer shows a Cancel control in place of, or next
to, the disabled Send control. Clicking it immediately disables repeated cancel
submissions and shows `Cancelling...`. Once the restored state is persisted, the
conversation shows the submitted user message with a `Cancelled` label and the
composer becomes available again.

The user does not need to understand graph checkpoints, tools, or rollback.

## Durable Save Points

A durable save point is conversation state that the user can safely return to.
The following are durable boundaries:

- the completed state before a new user request starts;
- a dataset, dataset plan, or analysis result accepted through a human-review
  decision; and
- another existing human-review outcome that the graph has completely applied.

The approval decision must be checkpointed before the graph begins the next
potentially long model or tool step. After that checkpoint is written, it
becomes the active run's latest durable save point.

Ordinary model reasoning, tool requests, unapproved artifacts, partial Python
results, and draft assistant responses are not durable boundaries.

Each active job keeps only the information needed to cancel safely:

- an internal run identifier;
- a cancellation signal;
- the checkpoint reference for its latest durable save point;
- and the submitted message ID, text, and attachment IDs needed to create the
  visible cancelled-turn record after restoration.

This information extends the existing in-memory job record. It does not require
a new job database for the single-worker deployment. The final cancelled state
itself is persisted in the existing LangGraph checkpoint store so that a server
restart cannot resume the cancelled work.

## Cancellation Flow

The API exposes one idempotent operation that cancels the active run for an
owned conversation. Because the application permits only one active run per
conversation, the browser does not need to manage run identifiers.

When cancellation is requested:

1. The runtime sets the active job's cancellation signal.
2. It restores the graph values from the latest durable save point.
3. It adds the submitted user request as a conversation event with
   `status="cancelled"`, including references to its original input attachments.
4. It persists this restored state as the conversation's newest checkpoint.
5. It changes the public run state to `cancelled` and unlocks the composer.
6. It cleans up unfinished output after preventing the old worker from
   publishing any later result.

The old worker checks the cancellation signal before starting model or tool
work and again after each blocking operation returns, before its result can be
merged into graph state. A response that arrives after cancellation is ignored.
This protects the restored checkpoint without introducing a provisional graph
or a second checkpoint namespace.

Cancellation is immediate at the application boundary. A provider may still
finish an already submitted request and charge for it because the application
cannot guarantee remote provider cancellation. That response is never added to
the conversation.

## User Messages and Attachments

The cancelled user message remains visible because it records the user's intent
and explains why no assistant answer followed. It is tagged as cancelled rather
than represented as a normal pending or completed user turn.

Original uploads are user-owned input, not disposable agent output. Therefore:

- the original attachment bytes remain in owner-scoped storage;
- attachment cards remain visible beneath the cancelled message;
- derived text, tables, figures, or datasets created while processing those
  attachments are discarded unless they reached a durable approved boundary;
- an unrelated next request does not automatically activate the cancelled
  request or its attachments; and
- a later request such as `Continue where we left off` receives the most recent
  cancelled request and its attachment references as explicit context.

The most recent cancelled-turn record is supplied to the next run as structured
inactive context with an instruction to use it only when the new user message
explicitly asks to retry, continue, or otherwise refer to that cancelled work.
This is semantic conversation context, not a hard-coded match for one exact
phrase.

Continuing starts a new clean run from the latest durable save point. It reuses
the original intention and input attachments, but it does not resume partial
Python execution, partial model output, or an unfinished tool call.

If an original attachment is no longer available, continuation must ask the
user to attach it again rather than silently running without it.

## Treatment of Tool Work

Tool work follows one rule: keep approved results and discard unfinished or
unapproved results.

- A tool that has not started does not start.
- A read-only request already in flight may return, but its result is ignored.
- The local Python process is terminated using its existing process-group
  termination mechanism. Its temporary execution directory is removed.
- A completed but unapproved tool artifact is marked cancelled or removed from
  active graph state and cannot be selected by later agent work.
- An approved artifact at the latest durable boundary remains available.
- Partial tool-call and tool-result messages are removed from active model
  history so the next run cannot treat them as completed observations.

Tools in the cancellation scope must either be read-only or stage their output
until successful completion. Adding irreversible external side-effecting tools
would require a separate compensation design and is outside this feature.

## API and State Contract

Add an authenticated, owner-scoped endpoint for cancelling the one active run:

```text
POST /api/threads/{thread_id}/cancel
```

The operation is idempotent. Repeated requests return the same current thread
state. An unknown or unauthorized thread retains the existing not-found
behavior. Calling it for an idle, completed, interrupted-for-review, or already
cancelled conversation leaves that conversation unchanged.

The public run-state schema adds `cancelled` as a terminal state. The frontend
may use a local `Cancelling...` state while the request is pending; it does not
need a separately persisted `cancelling` state.

The internal run-control record contains only the submitted message fields and
identifiers needed for restoration. It never copies attachment bytes, model
output, or provider secrets. The durable conversation checkpoint stores the
cancelled message text and attachment references using the existing
owner-scoped conversation format.

## Failure and Recovery

Cancellation must never expose an older user's state, remove approved artifacts,
or allow a late worker result to replace the restored checkpoint.

If restoration fails, the composer remains locked and the API returns a typed
cancellation error. The existing run is not reported as cancelled until the
restored checkpoint has been persisted successfully. Retrying the endpoint is
safe.

If cleanup of temporary output fails after restoration, cancellation still
succeeds. The failure is logged for operational cleanup, while those files
remain unreachable from conversation state.

After a successful cancellation, restart recovery treats the cancelled
checkpoint as terminal and never automatically resumes the discarded work.

Before introducing multiple application instances or workers, active-run
ownership and cancellation signals must move to shared coordination. That later
scaling change is not part of this design.

## Testing

Backend tests must prove that:

- cancelling during a model call restores the durable state and ignores the
  late response;
- cancelling during Python execution terminates the child process and removes
  temporary output;
- an approved artifact survives while later unapproved artifacts disappear;
- the cancelled user message and original attachments remain visible and
  owner-scoped;
- partial tool messages do not enter subsequent model context;
- a continuation request receives the cancelled intention and attachments but
  starts tool work again;
- an unrelated next request does not reactivate cancelled work;
- repeated cancellation requests are safe;
- cancellation cannot cross user boundaries; and
- a restarted runtime projects the cancelled state without resuming the run.

Frontend tests must prove that the Cancel control appears only during active
work, prevents duplicate submission, shows the temporary cancelling state,
renders the cancelled message and attachments, and re-enables the composer after
the restored state arrives. New conversation must continue to create a separate
thread and must not become an alias for cancellation.

## Non-Goals

This feature does not add queued messages, arbitrary checkpoint browsing,
general time travel, restoration of partial model output, multi-instance job
coordination, or cancellation of remote provider billing. It also does not
change the meaning of Cancel inside existing human-review panels.
