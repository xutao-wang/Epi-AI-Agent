# Concurrent Conversation Title Generation

## Goal

Allow the first message request to return as soon as the agent run is accepted. Conversation-title generation must continue independently and must never delay or fail the agent response.

## Design

When the first message is submitted, the runtime will create the existing `Untitled conversation` history record, start the agent as it does today, and submit automatic title generation to a small bounded background executor. The message endpoint will then return the current `running` thread state without waiting for the title result.

Title generation remains a first-message-only operation. A successful result updates the existing history row. A title-model failure is contained to the background task and leaves the conversation untitled. The existing conditional automatic-title update continues to protect a manual rename from being overwritten by a late result.

The frontend will refresh saved conversations once per second while the active conversation is still untitled. It will stop when the generated title appears, when the user leaves or replaces that conversation, or when a two-minute safety ceiling is reached. Agent-state polling and title polling remain independent, so either the agent response or the title may appear first.

No API response schema, database schema, LangGraph execution, prompt, tool, or model configuration changes are required.

## Verification

Backend tests will use a controllably blocked title generator to prove that message submission returns while title generation is still running, then verify eventual title persistence. Tests will also cover title failure isolation, first-message-only scheduling, and manual-rename protection.

Frontend tests will use fake timers and successive conversation-list responses to verify one-second refreshes, eventual title replacement, and cancellation after success or conversation change.

A dedicated real smoke test will launch the production FastAPI entry point and compiled frontend, submit a first message through the browser, observe immediate agent activity, and verify that both the agent result and generated sidebar title eventually appear. The frontend production bundle and build manifest will be rebuilt and verified.

## Out of Scope

This change does not introduce token streaming, SSE, `astream`, workflow-progress events, title fallbacks, or broader runtime concurrency changes.
