# Low-Risk Orphan Code Cleanup Design

## Goal

Remove the reviewed, production-unreachable orphan code from the current
FastAPI/TypeScript application without changing any current behavior, API,
stored-data interpretation, DB-RAG capability, or user interface.

This cleanup continues on the existing `high-confidence-codebase-cleanup`
branch. It supersedes the earlier two-file implementation boundary for this
branch but preserves the already completed removal of
`db_rag/service/errors.py` and the ignored retired Streamlit smoke.

## Safety Rule

A candidate is removable only when repository-wide reference analysis,
production-entry-point tracing, dynamic-registration review, and Git-history
review agree that the current application cannot reach it. Tests that exist
only to exercise a removed orphan may be removed or narrowed with that orphan.

If evidence is incomplete, conflicting, or dependent on an external/manual
workflow, retain the candidate. No speculative cleanup is allowed.

## Explicitly Protected Retrieval And Reranking Boundary

Retain all of `db_rag/retrieval.py`, including:

- `RerankedColumns` and `RetrievedColumns`;
- `retrieve_single_query`, `retrieve_queries`, and
  `retrieve_context_records_for_probes`;
- `rerank_columns` and every supporting lookup, injection, row-grain,
  subquery, and probe helper;
- the `OpenAIReranker` integration and reranking fallback warning.

The current embedding route does not activate reranking, but this code may
support a future rerank operation exposed through the DB-RAG capability. The
cleanup regression guard must prove these symbols remain present.

## Approved Backend Removals

### Retired DB-RAG service remnants

Remove:

- tracked `db_rag/service/errors.py` and its unused
  `DbRagUnanswerableError`;
- `DbRagTableHit`, `DbRagColumnHit`, `DbRagContext`, and
  `ColumnSelectionCandidate` from `db_rag/service/models.py`;
- `_mask_single_quoted_literals_and_comments`,
  `_contains_sql_identifier`, and `_validate_observation_sql` from
  `db_rag/service/sql_service.py`;
- `_lookup_schema_column` from `db_rag/service/schema.py`;
- `_bounded_interleave` from `db_rag/catalog.py`;
- `shared_env_path_for_project` and `embedding_credentials_ready` from
  `db_rag/config.py`.

Retain the active `PreparedSqlCandidate`, `ValidatedExtractionSql`, and
`SqlExecutionResult` service models and the complete active extraction SQL
validation chain.

### Retired tool and runtime adapters

Remove from `epi_agent/registry.py`:

- `ToolContextResolver`;
- `_RuntimeStructuredTool`;
- `ToolRegistry.as_langchain_tools`;
- `ToolRegistry._langchain_function`;
- imports used only by this retired LangChain `StructuredTool` adapter.

The active runtime continues to bind `ToolRegistry.model_schemas()` to the
model and execute calls with `ToolRegistry.invoke()`.

Remove:

- `AttachmentReaderService.for_conversation` from
  `utils/attachment_readers.py`;
- `RenderingPolicyError`, `prepare_plotting`, and `capture_figure_png` from
  `tools/execution_policy.py`, plus imports used only by those helpers;
- the `query_weather` convenience wrapper from `tools/mcp_pool.py` while
  retaining the active generic MCP call chain;
- `prepare_provider_credentials` from `run_fastapi.py` while retaining the
  active `configure_and_verify_providers` startup path.

### Retired projection, timing, and default helpers

Remove:

- `serialize_display_history` from `utils/display_history.py`;
- `dataset_artifact_display_label` from `utils/dataset_artifacts.py`;
- `append_workflow_timings` and `combined_timing_stages` from
  `utils/performance.py`, plus constants used only by those functions;
- the unused `ConversationEvent` output union from
  `graph/conversation_schema.py`, while retaining all individual event input
  and stored-event schemas;
- these unused runtime-default constants from `utils/runtime_defaults.py`:
  `AVAILABLE_OPENAI_MODELS`, `TEMPERATURE_RANGE`, `TEMPERATURE_STEP`,
  `TOP_P_RANGE`, `TOP_P_STEP`, `MAX_AUTO_STEPS_RANGE`,
  `DEFAULT_EXECUTION_TIMEOUT_SEC`, `EXECUTION_TIMEOUT_RANGE`, and
  `EXECUTION_TIMEOUT_STEP`;
- `_availability`, `configured_default_model`, `configured_models`,
  `configured_openai_models`, and `configured_title_model` from
  `utils/runtime_defaults.py`, plus imports used only by those helpers.

Retain active runtime defaults, including `DEFAULT_OPENAI_MODEL` because the
current real routing evaluation consumes it, as well as temperature, top-p,
auto-step, and Epi-agent iteration defaults used by application startup.

## Tests Removed Or Narrowed With Orphans

Remove or narrow only the assertions that directly exercise approved deleted
symbols:

- the `_lookup_schema_column` test in
  `tests/test_db_rag_service_schema.py`;
- plotting/capture tests in `tests/test_execution_policy.py`;
- the `serialize_display_history` test and import in
  `tests/test_display_history.py`;
- workflow-timing append/combination tests and imports in
  `tests/test_performance_timing.py`, retaining `collect_timings` and
  `timing_stage` coverage;
- the `prepare_provider_credentials`, `configured_openai_models`, and
  `configured_title_model` tests and imports in `tests/test_run_fastapi.py`.

Add a source-level cleanup contract before implementation. The contract must
fail while approved symbols remain, pass after removal, and separately assert
that the protected retrieval/reranking symbols remain.

## Explicitly Retained Medium-Risk Candidates

Do not remove or change:

- `build_full_schema_catalog`, `write_full_schema_catalog`, `build_chroma`,
  `replace_study_knowledge`, or `write_manifest`;
- `extract_sql`, `build_sql_policy_text`, or the study-neutral runtime smoke;
- `collect_timings` or `timing_stage`;
- `resolve_db_rag_embedding_model` or its current real smoke;
- generated Vite configuration files;
- ignored historical plans, specifications, smokes, or SDD artifacts;
- dependencies from Python or frontend manifests.

## Backward Compatibility

Retain all parsing and projection support for existing conversations,
LangGraph checkpoints, and exported threads. In particular, retain:

- `routing_decision` event schemas and validation;
- legacy event actor and producer values;
- API filtering of retired `orchestrator`, `agents`, and `next_action`
  diagnostic fields;
- conversation-history database migration;
- legacy attachment and dataset path readers;
- old-state recovery fixtures and compatibility tests.

The cleanup must not rewrite or migrate runtime data.

## Verification

Verification must run with Python bytecode and pytest cache generation disabled
so it does not recreate previously removed caches.

Required checks:

1. Run the new removal/protection contract red before deletion and green after
   deletion.
2. Run focused DB-RAG service, schema, registry, runtime, execution-policy,
   display-history, performance, startup, conversation-event, export, and API
   compatibility tests.
3. Run the complete Python test suite using `.venv/bin/python`.
4. Run the complete frontend Vitest suite even though no frontend source is
   changed.
5. Search active source to prove every approved symbol is absent.
6. Search active source to prove protected reranking and historical
   compatibility symbols remain.
7. Run `git diff --check` and review every changed path against this design.

No frontend production build or manifest refresh is required because no
frontend build input changes. No real model call is required because the
cleanup changes no active model, tool, retrieval, API, or browser path.

## Delivery

Keep all work on `high-confidence-codebase-cleanup`. Do not merge or cherry-pick
the branch during implementation. Commit the cleanup only after focused and
complete verification passes, then present integration options separately.
