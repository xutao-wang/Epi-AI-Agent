# Activity Smoke Domain Decoupling Design

## Goal

Make the real agent-activity-timeline smoke verify the activity architecture
without making diabetes interpretation part of that feature's acceptance
contract.

## Scope

This is a test-harness-only correction. It changes:

- `scripts/e2e_agent_activity_timeline_real.py`; and
- `tests/test_agent_activity_timeline_smoke_runner.py`.

It does not change the agent, DB-RAG behavior, model configuration, API,
frontend, authentication, study package, AWS infrastructure, or deployed
scientific behavior.

## Root Cause

The smoke reused a broad loss-to-follow-up query containing `diabetes`. The
installed catalog does not expose the direct diabetes field assumed by that
query. The real agent searched and inspected the catalog, correctly declined
to invent a field, and completed without creating a dataset plan.

The smoke then waited until its five-minute deadline because it treated
`cancelled`, `error`, and `timeout` as terminal before review but omitted the
normal terminal state `done`. The activity instrumentation itself worked: the
preserved API state contained completed catalog-search and table-inspection
activities.

## Approaches Considered

### 1. Use a minimal catalog-backed request and retain real review acceptance

Use the installed synthetic study's known baseline clinical/demographic form
and request only participant ID, age, sex, and marital status. Explicitly ask
the agent to present the dataset plan for review. Keep all existing real
browser, DB-RAG, activity, review, and raw-state assertions.

This is the selected approach. It exercises the full production path while
removing diabetes semantics from the feature contract.

### 2. Accept completion without dataset-plan review

This would allow almost any successful tool-using response to pass, but it
would no longer prove the required transition from running tool activity to a
waiting human-review activity. It weakens the original acceptance contract and
is rejected.

### 3. Seed activity state or mock the agent response

This would make the scenario deterministic, but it would not prove the real
FastAPI, LangGraph, DB-RAG, persistence, polling, and compiled-frontend wiring.
It also conflicts with the repository rule requiring real dependencies for a
user-visible feature smoke. It is rejected.

## Test Stimulus

The smoke's default request becomes:

```text
Create a baseline index-case dataset from Form 2A - INDEX CASE:
Clinical/Demographic Form with participant ID, age, sex, and marital status.
Present the dataset plan for review.
```

These fields are present together in the installed
`report-india-synthetic@0.3.0` schema catalog. The query is only a stable driver
of real tool execution and plan review; the smoke does not assert a scientific
interpretation, association, effect estimate, or diabetes proxy.

## Runtime Behavior

The smoke continues to:

1. launch the real FastAPI application with the compiled frontend;
2. submit the request through the browser;
3. use the authenticated local API contract for protected state reads;
4. wait for the visible dataset-plan review;
5. verify `Searching the data catalog` and
   `Waiting for dataset plan review` in the timeline;
6. verify at least one technical DB-RAG tool name in the expanded timeline;
7. compare the rendered state with the raw thread state; and
8. preserve API logs, raw state, page text, HTML, traceback, and screenshot on
   failure.

The smoke no longer answers scientific clarification interruptions. If an
`agent_clarification` appears before plan review, the smoke fails immediately
and records diagnostics because the fixed request was chosen specifically not
to require scientific interpretation.

If the API run reaches `done`, `cancelled`, `error`, or `timeout` before plan
review, the smoke fails immediately. In particular, `done` reports that the
agent completed before the required review boundary instead of waiting for the
remaining deadline.

## Focused Regression Coverage

The smoke-runner tests will establish that:

- the default request contains the known form and four supported fields;
- the default request does not contain `diabetes`;
- visible plan-review controls return successfully;
- `done` before review raises an immediate, descriptive failure;
- `cancelled`, `error`, and `timeout` retain their immediate failures;
- an unexpected clarification raises an immediate, descriptive failure;
- protected raw state requests retain `X-Epi-Session-ID`; and
- the public health request remains headerless.

Implementation follows test-driven development: add or update the focused
tests first, observe the expected failures, make the minimal runner change,
then rerun the focused suite.

## Verification and Release Boundary

After the deterministic tests pass, run the relevant tracked Python tests and
the complete frontend test/build gates. Run the corrected real activity smoke
once, with its existing five-minute maximum, only after deterministic gates
are green. Preserve and report diagnostics without automatic rerun if the real
smoke fails.

The real smoke must pass before `aws-test` is considered ready for an immutable
release. This correction itself does not upload, deploy, or otherwise mutate
AWS.
