# Dataset Reuse Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify the approved dataset-reuse recovery implementation without changing its observable lifecycle, identity, durability, or review behavior.

**Architecture:** Keep exact retry-family selection, scoped serialization, canonical lineage checks, and pre-SQL reservation. Refactor reconciliation so old-evidence validation is distinct from current state mutation, canonical files are verified once per path, and one successful fresh extraction is the smoke-test invariant.

**Tech Stack:** Python 3.12, pytest, LangGraph checkpoint state, SQLite journals, Parquet artifacts, FastAPI, Playwright.

## Global Constraints

- Work only in `.worktrees/dataset-reuse-simplification` on branch `refactor/dataset-reuse-simplification`.
- Preserve all behavior tests from `fix/dataset-reuse-recovery`.
- Do not change prompts, UI source, artifact schemas, stored labels, or review requirements.
- Do not use request-text heuristics, lexical search, or fuzzy matching for recovery.
- Keep the Demo checkout and its runtime database read-only.
- Do not rerun a failed real smoke automatically.

---

### Task 1: Separate historical incompatibility from current persistence failure

**Files:**
- Modify: `epi_agent/db_rag/tools.py:3750-3883`
- Test: `tests/test_dataset_reuse_recovery.py`

**Interfaces:**
- Consumes: `_resume_existing_dataset(...)`, existing persistence store methods, and typed `ToolExecutionError` codes.
- Produces: old-record incompatibility as `DATASET_DURABLE_COLLISION`; current hydration/write failures as `DATASET_PERSISTENCE_PENDING`; unexpected programming errors unchanged.

- [x] **Step 1: Add a failing hydration-boundary regression**

Add a store wrapper that records `begin_dataset_persistence_attempt` identities and raises `ValueError` while hydrating the original committed journal. Assert that replay returns `DATASET_PERSISTENCE_PENDING`, never calls SQL, and never tries a `-retry-1` identity.

- [x] **Step 2: Add a failing wrapper-boundary regression**

Patch `_persist_extraction_result_locked` to raise `TypeError("implementation defect")`. Assert that `_persist_extraction_result` does not convert it into `DATASET_PERSISTENCE_PENDING`.

- [x] **Step 3: Run the two tests and verify RED**

Run:

```sh
.venv/bin/python -m pytest tests/test_dataset_reuse_recovery.py -k "hydration_failure or programming_error" -q
```

Expected: hydration attempts a fresh identity and the programming error is translated.

- [x] **Step 4: Narrow the exception boundaries**

Keep structural validation of already-read historical records in the collision boundary. Move `_sync_attempt_from_journal` and registry restoration outside it, translate their failures to `DATASET_PERSISTENCE_PENDING`, and make the outer lock wrapper catch only storage/lock `OSError`.

- [x] **Step 5: Run focused and lifecycle suites**

Run:

```sh
.venv/bin/python -m pytest tests/test_dataset_reuse_recovery.py tests/test_scoped_db_rag_persistence.py tests/test_tool_failure_recovery.py -q
```

Expected: all pass.

### Task 2: Give canonical verification and registry restoration one owner

**Files:**
- Modify: `epi_agent/db_rag/tools.py:3421-3835`
- Modify: `epi_agent/artifacts.py:681-763`
- Test: `tests/test_dataset_reuse_recovery.py`

**Interfaces:**
- Consumes: verified journal preparation and `_reconcile_persistence_attempt`.
- Produces: at most one `load_verified_dataset_artifact` call for a committed replay; one committed-registry restoration path.

- [x] **Step 1: Add a failing verification-count regression**

Wrap the real `load_verified_dataset_artifact`, replay a committed pending dataset, and assert it is called exactly once while SQL is not executed.

- [x] **Step 2: Run the test and verify RED**

Run:

```sh
.venv/bin/python -m pytest tests/test_dataset_reuse_recovery.py -k verifies_canonical_once -q
```

Expected: failure showing repeated canonical loads.

- [x] **Step 3: Carry an explicit verified-canonical result into reconciliation**

Have journal preparation report whether it verified canonical content. Pass that fact to reconciliation so committed/promoted paths do not rehash the same files. Graph-only attempts still verify before reuse or commit.

- [x] **Step 4: Centralize committed registry restoration**

Restore a missing committed registry record only after canonical verification in the committed reconciliation path. Keep `StateArtifactStore.commit_dataset_persistence_attempt` responsible for atomic normal commits and idempotent comparison, not for discovering recovery policy.

- [x] **Step 5: Run replacement, journal-lag, and missing-state tests**

Run:

```sh
.venv/bin/python -m pytest tests/test_dataset_reuse_recovery.py -q
```

Expected: all pass, including committed replacement restoration without repeated supersession.

### Task 3: Remove duplicate selection logic and assert outcomes in the smoke

**Files:**
- Modify: `epi_agent/db_rag/tools.py:3973-4058`
- Modify: `scripts/smoke_dataset_reuse_recovery_real.py:248-276`
- Test: `tests/test_dataset_reuse_recovery.py`
- Test: `tests/test_smoke_dataset_reuse_recovery_runner.py`

**Interfaces:**
- Consumes: the family lock and existing exact-ID family discovery.
- Produces: one selection path for ordinary recovery and smoke validation based on one successful artifact-producing extraction.

- [ ] **Step 1: Add a failing smoke assertion regression**

Construct messages containing a recoverable `PREDECESSOR_NOT_PENDING_REVIEW` tool result followed by one successful extraction result. Assert that `_assert_recovery` accepts the sequence and still rejects two successful dataset creations.

- [ ] **Step 2: Run the smoke assertion test and verify RED**

Run:

```sh
.venv/bin/python -m pytest tests/test_smoke_dataset_reuse_recovery_runner.py -k rejected_precondition -q
```

Expected: failure from the current exact tool-call-count assertion.

- [ ] **Step 3: Remove unreachable recovery-family rediscovery**

Delete the second family lookup and nested `_resume_existing_dataset` call. Under the held family lock, an existing recovery family was already selected before the original family is reconciled.

- [ ] **Step 4: Count successful extraction results**

Match extraction calls to their tool results by exact `tool_call_id`. Require exactly one result containing the expected fresh artifact ID, allow preceding recoverable validation failures, and reject identity-collision errors or multiple successful artifacts.

- [ ] **Step 5: Run all selected branch tests**

Run:

```sh
.venv/bin/python -m pytest tests/test_dataset_reuse_recovery.py tests/test_dataset_persistence_lock.py tests/test_scoped_db_rag_persistence.py tests/test_execution_artifact_lifecycle.py tests/test_tool_failure_recovery.py tests/test_multi_study_dataset_lineage.py tests/test_smoke_dataset_reuse_recovery_runner.py tests/test_db_rag_agent_tools.py tests/test_db_rag_sql_service.py tests/test_db_rag_generation.py tests/test_dataset_plan_study_provenance.py tests/test_multi_study_db_rag_tools.py -q --tb=short
```

Expected: all pass.

- [ ] **Step 6: Review the final diff**

Confirm production code is smaller than the base implementation, `git diff --check` passes, and no prompt, UI, schema, model, dependency, fixture, or Demo file changed. Do not run another real smoke without explicit authorization.
