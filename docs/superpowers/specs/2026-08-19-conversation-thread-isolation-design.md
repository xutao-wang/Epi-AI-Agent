# Conversation Thread Isolation Design

## Problem

The frontend can display one conversation's state while another conversation is
selected. A user observed a question submitted in one conversation appearing in
other saved conversations, along with review interruptions that seemed to move
between threads.

The persisted checkpoint database shows that the repeated question belongs to
only one thread. Backend checkpoints and reviews are already durable and scoped
by owner and thread. The failure is therefore at the frontend state boundary:
asynchronous state requests can finish after the user has selected a different
conversation, and `applyThreadState` accepts them without confirming that the
response still belongs to the selected thread.

## Goals

- Display messages, activity, artifacts, settings, errors, and reviews only for
  the selected conversation.
- Keep a paused review durable in its original conversation when the user opens
  another conversation.
- Restore that same review when the user returns to its conversation.
- Apply the isolation rule consistently to every public review type.
- Make saved conversations awaiting review visibly distinguishable.
- Prevent actions from a stale screen from mutating the newly selected thread.

## Non-goals

- Do not cancel, approve, reject, or discard work merely because the user
  switches conversations.
- Do not replace durable checkpoint storage or change its ownership scheme.
- Do not add a second discard control; the existing review cancellation action
  remains the explicit way to abandon a pending review.
- Do not introduce a complete client-side cache of every conversation.

## Chosen Approach

Use a guarded, atomic thread-selection boundary in the frontend. Each operation
that can produce a thread snapshot is associated with both an owning thread ID
and a monotonically increasing selection/request generation. A result may be
rendered only when both still match the current selection.

This approach addresses the observed stale-response failure without adding the
cache invalidation complexity of storing full state for every thread. It also
preserves unsent-draft behavior more predictably than remounting the entire
application on every switch.

## State Ownership

The selected conversation identity and its rendered snapshot must remain
consistent:

```text
selectedThreadId = B
renderedState.thread_id = B
```

The UI must never intentionally render this combination:

```text
selectedThreadId = B
renderedState.thread_id = A
```

Thread selection will immediately:

1. Record the newly intended thread ID.
2. Increment the selection generation, invalidating earlier work.
3. Disable actions and show a conversation-loading state.
4. Fetch the selected thread snapshot.
5. Apply the snapshot only if its request generation is current and its
   `thread_id` equals the selected thread ID.

Messages, activity runs, active interruption, artifacts, runtime settings, and
run status are treated as one snapshot. They must not be applied independently
across thread boundaries.

## Asynchronous Result Guards

The same ownership check applies to every asynchronous path capable of applying
thread state:

- Opening a saved conversation
- Run polling
- Message submission
- Interrupt resume or review decision
- Active-run cancellation
- Conflict recovery after an HTTP 409 response

Polling for the previous conversation is invalidated synchronously when the
user selects another conversation. A late response is ignored even if it
finishes before React runs an effect cleanup.

Thread-state responses are also validated defensively. A response whose
`thread_id` differs from the operation's owning thread is not rendered. The UI
surfaces a recoverable loading error for the selected conversation rather than
showing mismatched data.

## Review Isolation

Review controls bind to the displayed review's immutable ownership pair:

```text
{ threadId, interruptId }
```

They do not consult a possibly newer selected thread at the time the network
request is sent. Before sending, the handler verifies that the ownership pair
still matches the selected snapshot. Review controls are disabled during a
thread transition.

This contract applies uniformly to:

- Dataset-plan review
- Dataset review
- Analysis-result review
- Agent clarification
- Model-output-limit review

The existing cancellation action for each applicable review remains the way to
abandon that review. Switching conversations has no workflow side effect.

## User Experience

While a conversation is loading, the main pane must not retain another
conversation's messages or review form. It shows an explicit loading state tied
to the selected history item.

When a saved conversation has a pending public review, its history entry shows
an **Awaiting review** status. Opening it renders its review immediately with a
short explanation:

> This conversation was previously paused and is awaiting your review.

The explanation distinguishes restoration from a newly triggered global pause.
The existing activity timeline remains visible and thread-specific.

Conversation summaries need a small status field derived by the backend from
the durable snapshot or activity state. The status is informational and does
not replace the authoritative thread-state endpoint.

## Error Handling

- Ignore stale responses from superseded selections without showing an error.
- Reject mismatched response thread IDs and retain the loading/error state for
  the intended conversation.
- Do not let an error from a superseded thread overwrite the selected thread's
  error state.
- If loading the selected thread fails, show the error without falling back to
  the previously rendered conversation.
- A stale review action must be blocked locally; backend stale-interrupt checks
  remain the final enforcement boundary.

## Testing

Focused frontend tests will use deferred promises to control response order and
prove that:

- A delayed Thread A response cannot render after Thread B is selected.
- Rapid A to B to C selection finishes with only Thread C visible.
- A question submitted in Thread A never appears in Thread B.
- Polling and conflict-recovery responses from Thread A are ignored after a
  switch to Thread B.
- Late submission, cancellation, and resume results cannot overwrite another
  selected thread.
- Each supported review type remains bound to its owning thread.
- Review controls are disabled during transitions.
- Returning to Thread A restores its unchanged pending review.
- Loading or failure never leaves the previous thread's content on screen.

Backend tests will verify that conversation summaries expose the correct
awaiting-review status without weakening existing owner/thread checkpoint
isolation.

A dedicated executable feature smoke under `scripts/` will launch the real
FastAPI backend and compiled TypeScript frontend. It will create multiple
conversations, leave one at a review, switch among them, and assert both the
rendered content and raw API thread IDs. It will also return to the paused
conversation and verify that its review remained intact. The smoke will follow
the repository's five-minute, single-run rule and preserve diagnostics on
failure.

Because this changes frontend inputs, delivery verification will rebuild the
tracked production bundle and refresh the build manifest after the source and
focused tests pass.

## Success Criteria

- At every stable render, the selected history thread and rendered snapshot
  have the same thread ID.
- No message, activity item, artifact, setting, error, or review appears under
  another conversation because of response ordering.
- Switching away from a pending review leaves it unchanged and returning to the
  conversation restores it.
- All current review types follow the same isolation contract.
- Users can identify conversations awaiting review before opening them.
