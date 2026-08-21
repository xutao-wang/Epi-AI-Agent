# Single New Conversation Control

## Goal

Expose one unambiguous way to start a new conversation and allow it while the
currently selected conversation has an active background run.

## User interface

- Remove the duplicate **New conversation** button from the saved-conversations
  sidebar.
- Keep the upper-right **New conversation** button as the sole control.
- Keep that button enabled while the selected conversation has a background run.
  A short-lived submission, upload, resume, cancellation, or conversation-load
  transition may still disable it to prevent an in-flight request from reclaiming
  the new blank selection.

## Behavior

Selecting **New conversation** clears the current frontend selection and opens
the blank composer. It does not cancel the previous thread's backend run. The
existing thread ownership and generation checks continue to prevent responses
from the previous selection from changing the new conversation's UI.

## Scope

This change does not add background-run badges, confirmation dialogs, or new
backend APIs. Existing restrictions on submitting messages, uploading files,
and review controls remain unchanged.

## Verification

- Component coverage confirms the sidebar no longer renders its duplicate
  control.
- App coverage confirms the upper-right control remains enabled during a run
  and clears the selected conversation when clicked.
- Existing late-response isolation tests remain passing.
- The production frontend bundle and build manifest are regenerated according
  to the repository requirements.
