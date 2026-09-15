# Dataset reuse and fresh retry: implementation verification

Branch: `fix/dataset-reuse-recovery`
Worktree: `.worktrees/dataset-reuse-recovery`
Baseline: `d2b3ef6f4ea1b950afbcbf4d4676b71036b728db`

The user-authorized live rerun reached dataset review for a fresh 450-row dataset without the original registry collision. The full smoke remains failed: its assertion required exactly one extraction-tool invocation, but the real model first made a rejected replacement call and then the original ordinary extraction call. Approval and active-reuse checks were not reached. The original failure is not reproduced by the corrected extraction path; this evidence is distinct from a full smoke pass. Evaluation and Demo code/state were not deployed or modified.

## Implemented behavior

- Reuse compares immutable saved content while excluding only top-level review status and `provenance.status_transitions`. Live `pending_review` and `active` records remain reusable. Active results retain their actual status and output reference.
- Existing-result validation and reconciliation are a bounded operation. Cancelled or incompatible old results select one fresh identity. Current source/storage failures and errors after a successful commit remain recoverable errors; they do not recursively trigger more SQL.
- Fresh identities use the exact deterministic dataset family and existing persistence journals. A scoped filesystem lock serializes reservation through commit. The begun journal is saved before SQL, so replay resumes the reserved identity.
- Verified committed evidence can restore missing registry state as pending review without reconstructing human approval. Graph/journal lag retains completed-save semantics.
- Unusable replacement control recovers as ordinary creation using a stable family derived from exact lineage. Once reserved, this recovery family remains current even if old evidence is later repaired. Old datasets, files, and decisions are preserved.
- No request-text keyword rules, lexical matching, fuzzy search, or prompt changes were introduced. Existing review checkpoints remain in force.

Production changes are limited to `epi_agent/dataset_reuse.py`, `epi_agent/artifacts.py`, `epi_agent/db_rag/tools.py`, and `utils/dataset_artifacts.py`. No new dataset status schema, migrations, artifact registry framework, UI changes, or broad label cleanup are included.

## Original failure and recorded-call verification

Historical thread: `a9a10994715544d4a624c5f5d18a2ccf`
Checkpoint immediately before the query: `1f1b04b5-2d40-6d0a-8032-0a8129104880`

Exact query recovered from history:

> skip any human checkpoint, Create a household-contact dataset with age, sex, household size, TST result, and IGRA result.

Original result: `DATASET_IDENTITY_COLLISION`, “Committed dataset registry does not match its persistence attempt.” The old live dataset was cancelled while its committed persistence snapshot recorded pending review.

A read-only SQLite backup and copied dataset/study files were relocated into an isolated fixture. The exact recorded extraction call was replayed through production `_execute_tools`, the real tool registry, and DuckDB. This check does not invoke an LLM and does not substitute for the live browser smoke.

Result:

- Original dataset: `subset-agent-3dd5da3f974dfd5ff040`, preserved with its files and cancelled status.
- Fresh dataset: `subset-agent-3dd5da3f974dfd5ff040-retry-1`, pending review.
- 450 rows, with participant ID, age, sex, household size, TST, and IGRA columns.
- Same approved plan ID/version; no terminal error.
- Replaying the same call reused the fresh ID without creating another dataset.

Local evidence is under `verification/dataset-reuse/final-tool-fixture/candidate-replay.json`. Fixture data and checkpoint databases are intentionally excluded from the staged deliverables.

## Regression verification

108 selected unit/component tests passed. Coverage includes the original cancelled replay, pending and active reuse, interrupted reservations, concurrent stale callers, malformed journals and registry records, immutable lineage conflicts, missing files/state, replacement recovery, lagging committed journals, restored old feedback, transient storage failures, and post-commit response failures. New failure cases were demonstrated before their fixes. Lock tests include process exclusion, cancellation, timeout, and storage-scope symlink rejection.

Command:

```sh
.venv/bin/python -m pytest tests/test_dataset_reuse_recovery.py tests/test_dataset_persistence_lock.py tests/test_scoped_db_rag_persistence.py tests/test_execution_artifact_lifecycle.py tests/test_tool_failure_recovery.py tests/test_multi_study_dataset_lineage.py tests/test_smoke_dataset_reuse_recovery_runner.py tests/test_db_rag_agent_tools.py tests/test_db_rag_sql_service.py tests/test_db_rag_generation.py tests/test_dataset_plan_study_provenance.py tests/test_multi_study_db_rag_tools.py -q --tb=short
```

Compilation and `git diff --check` passed. All 66 protected baseline file hashes match, including prompts, model configuration, dependencies, and compiled frontend assets. The source and Demo checkouts have clean tracked diffs. Final code review found no remaining blockers before the live smoke.

## Live smoke history and remaining verification

`scripts/smoke_dataset_reuse_recovery_real.py` launches real FastAPI and the compiled frontend against the isolated historical fixture. It uses the configured real model (`gpt-5.6-terra`, OpenAI, `api.openai.com`) and browser controls. Its single run is bounded to 300 seconds and covers the exact query, `skip all checkpoint, do it again.`, and active reuse after approval. It preserves diagnostics on failure and must not be rerun automatically.

The initial approval rejection was resolved by explicit user authorization. The first executed smoke used `--follow-up-case exact` and failed before application startup: `Attribute "app" not found in module "api.app"`. No browser opened and no query was submitted. The copied checkpoint still has its original 44 messages. Logs, traceback, raw checkpoint state, and result metadata are retained under `verification/dataset-reuse/real-smoke/`.

The runner now uses `api.app:build_application --factory`. An import-contract regression first reproduced the stale-target failure and now passes; the runner suite reports 4 passed, and compilation passes. This check does not launch the app or substitute for the live smoke.

The user explicitly authorized the rerun using the separate fixture at `verification/dataset-reuse/exact-fixture-retry/`. The command below records that executed rerun; its result is detailed in the audit below. The full smoke did not pass, and approval/active-reuse verification remains incomplete. Failed real smokes must not be rerun automatically. Provider-transfer approval remains valid; no deployment or merge is authorized.

```sh
.venv/bin/python scripts/smoke_dataset_reuse_recovery_real.py --runtime-root verification/dataset-reuse/exact-fixture-retry/runtime --study-root verification/dataset-reuse/exact-fixture-retry/study_data --checkpoint-fixture verification/dataset-reuse/exact-fixture-retry/checkpoint-fixture.db --fixture-manifest verification/dataset-reuse/exact-fixture-retry/fixture.json --thread-id a9a10994715544d4a624c5f5d18a2ccf --environment-root /Users/xutaowang/Desktop/Demo/Epi-AI-Agent --artifact-dir verification/dataset-reuse/real-smoke-retry --follow-up-case exact --timeout-seconds 300
```

## Exact-incident audit after the authorized rerun

The rerun used the user's exact historical query from the same thread and checkpoint. Direct comparison with the read-only Demo database found **zero starting-state differences** after reversing fixture storage relocation and restoring the original manifest hash values for comparison. All 44 historical messages, approved plan/version, SQL artifact, cancelled dataset registry record, and committed persistence state match.

Original call: `dbrag-validate_and_extract` with plan `a7e749d5-7930-4c29-82c0-0233597f153c`, version 2, `sql=null`, and both predecessor fields null. Original result: nonrecoverable `DATASET_IDENTITY_COLLISION` / “Committed dataset registry does not match its persistence attempt.”

Live rerun sequence:

1. The model first supplied the cancelled dataset as a replacement predecessor. The tool returned recoverable `PREDECESSOR_NOT_PENDING_REVIEW` before creating a replacement.
2. The model then supplied the **exact same arguments as the original failing call**. This created `subset-agent-3dd5da3f974dfd5ff040-retry-1`, pending review, with 450 rows and 6 columns.
3. The model inspected the new dataset and requested dataset review. The compiled UI rendered “Review extracted dataset”; the API reported `dataset_review` with no run error or error code.
4. The smoke assertion failed because it counted two extraction-tool calls. This failed smoke has not been reclassified as a pass or rerun automatically. Its later approval/reuse checks were not executed.

The old cancelled registry record is unchanged; all three old artifact file hashes match the initial fixture manifests. The original parquet also matches the Demo source bytes. The output columns are participant ID, age, sex, household size, TST, and IGRA.

Local evidence: `verification/dataset-reuse/real-smoke-retry/incident-comparison.json`, `api-state.json`, `checkpoint-state.json`, `screenshot.png`, `page.txt`, `backend.log`, and `traceback.txt`. The independent recorded-call replay remains the deterministic check of the exact failing tool arguments; the live test allows the real model to choose its own calls. No lexical query rules were added, and no smoke assertion changes were made after the user's clarification.

## Simplification-branch final smoke and reliability result

Branch `refactor/dataset-reuse-simplification` at `93eb94c` received a separately authorized real smoke on 2026-09-15. The smoke used a new isolated fixture prepared read-only from the same historical checkpoint, launched the real FastAPI backend and compiled frontend, and sent the exact historical query to `gpt-5.6-terra` through `api.openai.com`.

The smoke **failed before extraction** because the model saved and validated a new draft plan, then called `dbrag-request_dataset_plan_review`. It did not call `dbrag-validate_and_extract`, so this run did not exercise dataset reuse or persistence recovery. The runner correctly stopped at the unexpected plan review. The new draft and historical approved plan have identical study, row definition, selected source fields, output aliases, filters, and unresolved fields. They differ only in goal wording and the TST-purpose sentence because the historical plan includes prevalence analysis while the replay query asks only for dataset creation.

The historical cancelled dataset remained the only dataset in state. Its registry status, persistence attempt, and all three canonical file hashes match the starting fixture. Diagnostics are preserved under `verification/dataset-reuse/simplification-real-smoke/`, including backend log, API state, checkpoint state, page text, screenshot, and traceback. This failed real smoke must not be rerun automatically.

The failure is at the model routing boundary rather than the simplified persistence boundary. The current and previous real runs used the same model and both first responses reported 53,517 input tokens, but chose different valid paths: the previous run reused the approved plan and reached fresh dataset review; the final run created a narrower new plan. This is evidence that implicit reference resolution is nondeterministic for this wording.

A separate no-LLM replay then sent the exact recorded `dbrag-validate_and_extract` call through the final production tool runtime. It passed: `subset-agent-3dd5da3f974dfd5ff040-retry-1` was created pending review with 450 rows and the six requested columns, the old dataset record and files remained unchanged, no terminal error occurred, and replay reused the same fresh identity. Evidence is stored at `verification/dataset-reuse/simplification-recorded-replay-fixture/candidate-replay.json`.

The resulting reliability claim is bounded: focused lifecycle tests and deterministic production-boundary replay support the persistence implementation, while the one-shot browser test does not establish reliable natural-language selection of the prior approved plan. Improving that routing requires a separately designed reference-resolution behavior or a repeated agent evaluation; it should not be hidden inside persistence recovery or implemented with lexical query heuristics.
