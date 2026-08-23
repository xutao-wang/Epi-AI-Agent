# Awaiting-Review Badge Synchronization Design

## Problem

The active conversation view and the saved-conversation sidebar use separate
frontend state. After a user answers an interrupt, the returned thread state
updates the conversation view, but the saved-conversation list is not
refreshed. If that resumed run is then cancelled, cancellation also updates
only the thread state. The sidebar can therefore keep showing `Awaiting review`
after no actionable interrupt remains.

Completed activity and clarification traces are historical records and should
remain visible. Only the current-status badge is stale.

## Scope

Keep the existing backend contracts and state projections unchanged. In
`frontend/src/App.tsx`, request the existing saved-conversation refresh after:

1. a successful interrupt resume whose returned state was accepted for the
   currently selected conversation; and
2. a successful active-run cancellation whose returned state was accepted for
   the currently selected conversation.

The refresh remains fire-and-forget, matching the existing terminal-polling
path. Existing request-generation guards prevent an older list response from
overwriting a newer one, and refresh failure remains non-blocking.

## Alternatives Considered

- Update `awaiting_review` optimistically inside `applyThreadState`. This avoids
  a request but duplicates the backend's summary projection in the frontend.
- Poll conversation history continuously. This would cover cross-tab changes
  but adds unnecessary network traffic and is outside this regression.
- Refresh after the two successful transitions. This reuses the authoritative
  list endpoint and is the smallest change, so it is selected.

## Verification

Add one focused frontend regression test for the reported sequence: a saved
conversation starts with the badge, an answered clarification starts a running
continuation and clears the badge, then cancelling that run leaves the badge
absent while re-enabling the composer.

Add the repository-required real browser smoke under `scripts/`. It will use
the compiled frontend and real FastAPI backend, exercise the review-state
transition through browser controls, and assert both the rendered badge and
the relevant API state. Rebuild the tracked frontend bundle and refresh the
delivery manifest before handoff.

## Non-Goals

- Changing or hiding activity history or clarification trace
- Adding continuous sidebar polling or cross-tab synchronization
- Refactoring conversation state management
- Changing backend review or cancellation behavior
