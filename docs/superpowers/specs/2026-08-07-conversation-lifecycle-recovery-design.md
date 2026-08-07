# Conversation Lifecycle Recovery Design

## Goal

Prevent failed or empty thread starts from creating visible saved conversations,
keep automatic title generation concurrent with the main workflow without hiding
failures forever, make sidebar deletion resistant to stale refreshes, and provide
an explicit one-time cleanup for existing orphan records.

This design narrows and supersedes the failure behavior in the durable-history
and concurrent-title designs. A title failure no longer leaves a permanent
`Untitled conversation`, and a thread is not a saved conversation until its
first user turn is durably checkpointed.

## Confirmed failure modes

Three independent defects combine into the reported behavior:

1. Conversation metadata can be persisted before LangGraph durably stores the
   initial user turn. If first-turn startup fails, an untitled history row can
   survive without a checkpoint or message.
2. Automatic titles run concurrently, but their exceptions are swallowed. A
   failed or timed-out title request therefore leaves the placeholder title
   indefinitely.
3. Title polling can complete after a successful delete and replace the
   frontend's newer local state with a stale pre-delete conversation list. The
   API returns HTTP 204 and removes the database row, but the deleted item can
   reappear visually.

The existing orphan records predate the post-rebase server process. Git history
operations did not create them; the restarted server exposed metadata already
stored in the persistent runtime database.

## Conversation lifecycle

Conversation metadata has two lifecycle states: `pending` and `ready`.

- A first accepted submission creates a `pending` history row. Pending rows are
  never returned by the ordinary conversation-list API and therefore never
  appear in the sidebar.
- Merely creating a runtime thread, selecting a model, or staging an attachment
  does not create a visible saved conversation.
- The main graph run and automatic title request start concurrently. Title work
  never blocks, cancels, or changes graph execution.
- The initial user turn becomes durable only when the configured LangGraph
  checkpointer contains a checkpoint for the scoped thread whose state includes
  the submitted user-message ID or its stable turn hash.
- After the initial invocation returns, the runtime verifies that durable
  checkpoint evidence. It then promotes the history row from `pending` to
  `ready`. This promotion does not require the entire analysis to finish; a
  running, interrupted, or completed workflow is valid once its first turn is
  recoverable after restart.
- If initial invocation raises, returns without the matching checkpoint, or is
  rejected before durable acceptance, the runtime deletes the pending history
  row and rolls back staged inputs according to the existing attachment rules.
- A later message never recreates or re-promotes an already-ready conversation.

The lifecycle transition is conditional and idempotent so duplicate callbacks
cannot promote the wrong owner/thread pair or overwrite a concurrently deleted
record.

## Concurrent title behavior

The title executor remains bounded and concurrent with first-turn graph work.
The title task produces a normalized title value independently of persistence.
The runtime coordinates that result with lifecycle promotion so either order is
safe:

- If the title finishes first, its result is retained until the pending row is
  promoted.
- If the checkpoint is promoted first, the eventual title result updates the
  ready row.
- A manual rename continues to win over every late automatic result.

Title work has a bounded timeout. Provider errors, invalid responses, and
timeouts are recorded through the application's structured logging without
including message content or credentials. They never fail the main workflow.

When model title generation fails, the runtime derives a fallback from the
first user message by trimming whitespace, collapsing internal whitespace,
removing surrounding quotation marks, and applying the existing maximum title
length. If the first turn contains only attachments or normalization produces
no text, the fallback is `Attached data analysis`. A ready conversation must
therefore never retain the placeholder title solely because title generation
failed.

## Persistence and API behavior

The conversation-history schema gains an explicit lifecycle field with allowed
values `pending` and `ready`. Existing rows migrate to `ready`; the migration
does not reinterpret or delete historical data. Store operations preserve
owner scoping in hosted mode and the existing native local owner behavior.

The ordinary list operation returns only `ready` rows. Direct rename, archive,
restore, open, and delete operations continue to require ownership and an
existing record. Pending rows are internal lifecycle state and are not exposed
as user-manageable conversations.

Deletion remains successful when the API returns HTTP 204. The backend removes
the history row, scoped LangGraph checkpoints, in-memory thread state, and
scoped attachments as defined by the existing archive/delete contract.

## Frontend consistency after mutations

Conversation-list refreshes use a mutation generation as well as request
ordering. Every successful or starting archive, restore, or delete mutation
invalidates list requests that began against older state. A stale response may
complete, but it cannot replace state from a newer mutation.

Deleting a conversation also stops title polling for that thread. After the
DELETE returns HTTP 204, the item is removed optimistically and the frontend
performs one authoritative list refresh. This refresh is ordered after the
mutation and cannot be superseded by pre-delete polling. If the authoritative
refresh fails, the successful local removal remains in place and the normal
error surface reports only the refresh problem; the UI must not claim that the
backend deletion failed.

## One-time orphan cleanup command

A maintenance command inspects one explicitly resolved runtime database and is
dry-run by default. The application server must be stopped before the command
runs, so no in-memory workflow can race classification or deletion. The command
prints every candidate thread ID, owner scope, creation time, and the evidence
used to classify it. It changes nothing unless the user passes `--apply`.

A record is eligible only when all of the following are true:

- its title is exactly `Untitled conversation`;
- it has no checkpoint containing a user turn under either the legacy thread ID
  or current owner-scoped checkpoint ID;
- it has no stored attachment, dataset, figure, file, or other runtime artifact
  in either legacy or owner-scoped storage;
Ambiguous records are preserved and reported as skipped. Applied cleanup
deletes only the qualifying history rows; because qualifying records have no
checkpoints or artifacts, the command does not perform broad recursive storage
deletion. It runs in a transaction, prints the applied count, and is safe to run
again. `--apply` acquires the database write transaction before revalidating the
candidate set and fails without deletion if the database is busy. No recurring
startup cleanup or retention policy is introduced.

The command will be run once against the current local runtime database only
after its dry-run output confirms the expected 27 orphan candidates. This is a
destructive execution checkpoint and requires explicit user approval of that
printed candidate set before `--apply`.

## Error handling and observability

- Initial-turn failure removes provisional metadata but preserves the public
  workflow error already produced by the runner.
- Missing durable checkpoint evidence is treated as initial-turn failure even
  if invocation returned without raising.
- Title errors include a stable error category and thread ID in logs, but no
  prompt text, API key, or provider response body.
- Cleanup refuses an unresolved database path, malformed schema, locked
  transaction, or ambiguous artifact/checkpoint state. It exits nonzero without
  partial deletion.
- Frontend refresh errors cannot reverse a confirmed mutation.

## Verification

Focused backend tests will prove:

- pending rows are excluded from ordinary lists;
- an initial checkpoint containing the submitted turn promotes exactly one row;
- an exception or missing matching checkpoint removes the pending row;
- title generation overlaps graph execution;
- titles persist correctly whether title or checkpoint completes first;
- title timeout/error produces the deterministic fallback;
- a manual rename is never overwritten;
- owner-scoped and native-local histories remain isolated;
- dry-run cleanup reports without deleting;
- applied cleanup deletes only exact orphan candidates and preserves rows with
  any checkpoint, user turn, artifact, custom title, or ambiguous state.

Frontend tests will reproduce the stale-response race: begin title polling,
delete the item successfully, resolve the older list request afterward, and
assert that the deleted item never reappears. Tests will also verify title-poll
cancellation and the ordered post-delete refresh.

The dedicated feature smoke under `scripts/` will launch the real FastAPI
backend and compiled TypeScript frontend. It will submit a first turn, verify
that no pending row appears in the sidebar, observe concurrent agent/title
activity, verify promotion and a durable generated or fallback title, delete
the conversation during an outstanding list refresh, and confirm absence in
rendered state plus the raw API/database after reload. The smoke runs once with
the repository's five-minute limit and preserves diagnostics on failure.

Every frontend change requires rebuilding the tracked production bundle and
refreshing the delivery manifest before final verification.

## Non-goals

- No automatic recurring cleanup or retention policy.
- No bulk-delete UI.
- No change to model selection, temperature, top-p, reasoning effort, prompts,
  or the main graph's checkpoint frequency.
- No requirement that an analysis complete before becoming a saved
  conversation.
- No exposure of pending lifecycle rows through public API responses.
