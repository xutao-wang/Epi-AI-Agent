# Retired Orchestrator Cleanup Design

## Goal

Remove the approved, unreferenced remnants of the retired child-orchestrator
architecture without changing agent reasoning, tools, reviews, datasets,
conversation rendering, analysis execution, the public thread-state `output`
contract, or legacy `routing_decision` compatibility.

## Scope

The cleanup removes these isolated remnants as one verified batch:

- `invoke_epi_agent(...)` and its `__all__` entry from
  `epi_agent/runtime.py`;
- the write-only `agent_status` state annotation and its three terminal-state
  patches from `epi_agent/runtime.py`;
- `get_artifacts(...)` from `graph/state_views.py` and the test that exists
  only to exercise that unused compatibility view;
- the unimported `frontend/src/StructuredOutput.tsx` component, its dedicated
  test, and its unused `.structured-output-*` CSS rules;
- the unused `next_action` field in the frontend polling test fixture.

The cleanup explicitly retains:

- `routing_decision` event types and validation, preserving compatibility with
  old persisted or exported conversation state;
- the root, API, frontend type, and export `output` dictionary contract;
- `get_conversation_events(...)` and `get_artifact_files(...)`, which are used
  by active export code;
- `validate_generated_code(...)`, which is used by the active Python runtime;
- architecture regression assertions that intentionally name retired symbols.

## Runtime Behavior

The compiled graph continues to use `build_epi_agent_graph(...)` directly as
the root graph. Nothing invokes a child EpiAgent subgraph, so removing
`invoke_epi_agent(...)` does not alter graph edges, recursion limits, or
checkpoint execution.

Terminal behavior continues to be represented by `terminal_error`,
`terminal_control`, and `completion_blocked`. Current production code does not
read `agent_status`, so removing its writes only prevents redundant checkpoint
payload from being created.

Conversation events and artifact manifests remain canonical. Removing
`get_artifacts(...)` eliminates only the unused compatibility projection from
the retired `output.generated_code`, `output.text`, and `output.error` shape.

The frontend does not import or render `StructuredOutput`. Current responses
remain rendered from conversation messages and linked artifacts. Removing the
component and styles therefore does not change the visible application.

## Compatibility And Deployment

The application should be stopped or have no active runs while deploying the
cleanup so no in-flight graph invocation spans two state schemas. Existing
completed checkpoints may contain `agent_status`; the remaining runtime does
not consume it.

No API response or export schema changes are part of this cleanup. The
`output` dictionary remains available even though the current graph does not
populate legacy structured-answer fields.

## Verification

Verification will include:

1. a source-level architecture regression that fails while the approved stale
   symbols and frontend files remain;
2. focused backend architecture, conversation-event, export, root-state,
   runtime, and execution-policy tests;
3. focused frontend tests followed by the complete frontend test suite;
4. `npm --prefix frontend run build`;
5. `.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest`;
6. a final repository search proving the approved stale references are absent
   while retained compatibility surfaces remain.
