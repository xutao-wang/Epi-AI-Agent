# Multi-Study Review and Tool-Failure Recovery Design

Date: 2026-08-18
Status: Approved

## Problem

The multi-study refactor changed `_verified_join_paths` to accept a resolved
`StudyBundle`, while dataset-plan review still passes the surrounding
`ToolContext`. A real RePORT dataset request therefore fails while preparing
the dataset-plan review with:

```text
AttributeError: 'ToolContext' object has no attribute 'data_sources'
```

This exception occurs after the model has emitted an OpenAI function call but
before the graph has persisted the matching `ToolMessage`. The runner marks the
run as failed, yet the malformed message sequence remains durable. A later user
message is appended after the orphaned function call, and OpenAI rejects every
subsequent request with `No tool output found for function call ...`.

Two guarantees are required:

1. Dataset-plan review must resolve and use the plan's exact study in a
   multi-study session.
2. A failed tool turn must remain visibly failed without preventing later
   messages in the same conversation.

## Scope

This change covers dataset-plan review study resolution, unexpected tool
execution failures, repair of previously orphaned tool calls at the follow-up
boundary, and focused plus real-workflow regression coverage.

It does not change model selection, review-panel layout, ordinary recoverable
tool errors, human-review decisions, cancellation semantics, study package
formats, or catalog ranking.

## Selected Approach

Use protocol-safe failure persistence during normal execution plus a defensive
repair pass before each new user turn.

Alternatives were rejected as follows:

- Rolling back the failed turn would conflict with the requirement that the
  failed turn remain visible.
- Repairing only when the next message arrives would leave newly failed
  checkpoints malformed until another user action and would make reopening or
  exporting them inconsistent.

The selected design prevents new malformed checkpoints and repairs existing
ones that predate the fix or result from an unusual interruption outside the
normal tool boundary.

## Multi-Study Dataset-Plan Review

`RequestDatasetPlanReviewTool` already loads the exact `DatasetPlan`, whose
`study_id` is frozen in the artifact. Review-view construction will resolve
that identifier through `ToolContext.studies` using the existing
`require_context_study(context, plan.study_id)` contract.

`_data_linkage_payload` will pass the resulting `StudyBundle` to
`_verified_join_paths`. The helper will never infer a default study from the
session and will never receive a `ToolContext` in place of a study.

An unavailable plan study remains a structured, recoverable
`STUDY_NOT_AVAILABLE` tool error. Relationship evidence and join paths must
come only from the resolved study. A RePORT plan in a RePORT-plus-NHANES
session therefore cannot read NHANES sources or relationship inventory.

## Protocol-Safe Unexpected Tool Failure

The tool executor will preserve the current behavior for these cases:

- success produces the normal matching `ToolMessage`;
- `ToolExecutionError` produces the existing structured recoverable or
  terminal tool error;
- `GraphInterrupt` propagates so human review remains paused;
- cancellation propagates so the durable cancellation boundary remains in
  control.

For any other `Exception` raised while invoking a registered tool, the executor
will:

1. log the original exception and traceback server-side with tool name, call
   ID, and thread ID;
2. append exactly one `ToolMessage` for the failed call ID with status `error`;
3. use the public code `INTERNAL_TOOL_ERROR` and a generic message that does not
   expose Python types, paths, secrets, or traceback content;
4. if the assistant message contains a permitted read-only batch, append a
   generic terminal error `ToolMessage` for every remaining unexecuted call ID
   so no call in the batch is orphaned;
5. set `terminal_error` so the graph ends the current run without making
   another model request; and
6. retain the current user message, assistant tool call, activity history, and
   generic failure state in the durable checkpoint.

The same batch-closing rule applies when an existing non-recoverable
`ToolExecutionError` stops a permitted read-only batch. Calls that already have
results are preserved, the failing call keeps its structured error, and only
unexecuted calls receive the generic aborted-call result. An assistant message
therefore never reaches a terminal checkpoint with fewer tool results than
tool calls.

The activity store will finish the running tool item and run as errored through
the existing terminal activity transition. The failed turn remains visible in
conversation history. The existing failed activity marker is the intended
presentation. If the focused UI test or real smoke shows that it disappears or
attaches to the wrong user turn after a successful follow-up, the smallest
frontend correction needed to retain that marker is in scope and must rebuild
the production bundle.

## Defensive Follow-Up Repair

Before binding a new `HumanMessage`, the API runtime will validate the durable
message sequence. While scanning in order, it will retain the pending call IDs
from each assistant tool-call message, consume matching tool results, and
insert results for any still-pending IDs immediately before the first later
human or assistant message. Pending calls at the end of history receive their
results at the end, before the new user message is appended.

For each orphaned call, the runtime will insert one generic error `ToolMessage`
with the original call ID and public code `INTERNAL_TOOL_ERROR`. Repair is
idempotent: a second pass adds nothing once every call has a result. Existing
tool results are never replaced, and legitimate active interrupts remain
protected by the existing `ThreadAwaitingReviewError` gate rather than being
repaired as failures.

The repaired messages are checkpointed before the new user message reaches the
model. The next provider request therefore always observes this order:

```text
AssistantMessage(tool call)
ToolMessage(matching generic failure)
HumanMessage(follow-up)
```

The previous failed turn remains in the conversation, while the new turn clears
the prior terminal status through the existing new-turn state patch and runs
normally. This repair also makes already-corrupted local conversations usable
after the fixed service is restarted.

## Privacy and Error Reporting

The complete unexpected exception is server-log-only. Checkpoints, API
responses, model context, activity labels, and browser text receive only the
generic public failure code and message.

Provider failures, authentication failures, timeouts, and workflow limits
continue to use their existing public error classification. This design is
specific to exceptions raised inside registered tool execution and orphaned
tool-call protocol repair.

## Test Strategy

### Focused tests

- Repair the dataset-review test fixture to construct `ToolContext` with a
  `StudyRegistry` instead of the removed `study=` argument.
- Build a two-study registry and prove that a RePORT plan reaches
  `dataset_plan_review` using only RePORT relationship evidence.
- Prove that an unavailable `plan.study_id` returns the existing structured
  study error.
- Prove that an unexpected tool exception produces one matching generic error
  `ToolMessage`, sets `terminal_error`, and does not route back to the model.
- Prove that terminal failure in a permitted read-only batch produces results
  for the failed and every unexecuted call without replacing completed results.
- Prove that `ToolExecutionError`, `GraphInterrupt`, and cancellation retain
  their existing behavior.
- Prove that orphan repair inserts only missing results, preserves existing
  results, and is idempotent.
- Start from a LangGraph checkpoint containing an orphaned tool call, submit a
  follow-up through `ReportAgentApiRuntime`, and assert the durable repaired
  state and successful provider-valid continuation.

Every production change follows red-green-refactor: add the focused failing
test, observe the expected failure, implement the minimum change, and rerun the
focused and neighboring suites.

### Dedicated real smoke

Add an executable feature smoke under `scripts/` with a five-minute maximum.
It will launch the real FastAPI backend and compiled TypeScript frontend and
will not stub OpenAI, LangGraph, DB-RAG, embeddings, reranking, or study data.

The smoke will install the real RePORT and NHANES packages, use a real OpenAI
key, and verify two production paths:

1. a RePORT-scoped dataset request reaches a rendered dataset-plan review while
   both studies are installed, and raw API/LangGraph state identifies only the
   RePORT study;
2. a deliberately seeded legacy checkpoint containing an orphaned tool call is
   opened in the browser, a follow-up is submitted through the UI, the failed
   prior turn remains visible, and the follow-up receives a normal assistant
   response with a repaired raw checkpoint.

Seeding the legacy checkpoint is input-state construction, not a replacement
for a production dependency. The backend, browser, model, checkpoint saver, and
message-binding path remain real. The smoke runs once; on failure or timeout it
preserves backend logs, page text, raw state, traceback, screenshot, and other
diagnostic artifacts without an automatic rerun.

## Why Existing Smokes Missed the Defect

`smoke_multi_study_semantic_catalog.py` validates archive installation,
session binding, semantic catalog retrieval, exact inspection, and read-only
DuckDB access. It does not build a dataset plan, invoke
`dbrag-request_dataset_plan_review`, create a LangGraph review interrupt, or
submit a follow-up turn.

The RePORT study-scoping smoke invokes discovery, catalog, study-design, and
publication tools directly but likewise stops before plan save, validation,
and review. These scripts passed because the broken `ToolContext` versus
`StudyBundle` boundary is downstream of everything they exercise.

The review unit suite should have caught the interface change, but its shared
fixture still used the removed `ToolContext(study=...)` constructor. It fails in
fixture setup before reaching review-view construction. The multi-study
implementation therefore lacked an acceptance test spanning selection through
human review, and its relevant focused suite was stale.

The existing catalog smoke will remain, but its name and documentation will
describe it as catalog-binding coverage. It does not substitute for the new
end-to-end multi-study review and failure-recovery smoke.

## Acceptance Criteria

- A dataset plan resolves join-path evidence from its exact installed study and
  reaches human review when another study is also installed.
- Unexpected registered-tool exceptions always leave a provider-valid matching
  tool result in newly written checkpoints.
- The failed turn remains visibly failed and durable.
- A later message in the same conversation runs normally.
- A previously orphaned tool call is repaired exactly once before follow-up.
- Active human-review interrupts and cancellation retain their current
  semantics.
- No internal exception details reach the model, checkpoint public payload,
  API response, or browser.
- Focused tests pass, and the dedicated real smoke passes once within five
  minutes or preserves the required diagnostics from its single failed run.
