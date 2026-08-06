# Task 5 Report: Owner and Thread Storage Layout

## Summary

Implemented `UserStorageLayout` and `ThreadStorageScope` with the required
SHA-256 owner directory layout. Runtime attachment operations now construct an
authorized scope from `RequestIdentity`; hosted owners with the same thread or
artifact IDs use different paths. Tool contexts receive that scope and DB-RAG
persistence rejects attempts without it.

Local raw attachment thread IDs remain compatible: existing legacy files under
the pre-layout attachment directory are still discovered for `local-user`,
while all newly scoped runtime writes use the formal `users/<hash>/threads/...`
layout.

## Red / Green

### Red

Command:

```bash
.venv/bin/python -m pytest tests/test_user_storage.py tests/test_attachment_artifacts.py tests/test_dataset_artifacts.py tests/test_api_runtime.py -q
```

Observed expected failure before implementation:

```text
ModuleNotFoundError: No module named 'utils.user_storage'
```

### Green

Command:

```bash
.venv/bin/python -m pytest tests/test_user_storage.py tests/test_attachment_artifacts.py tests/test_dataset_artifacts.py tests/test_attachment_tools.py tests/test_db_rag_agent_tools.py tests/test_api_runtime.py -q
```

Output:

```text
212 passed in 1.92s
```

Dedicated internal feature smoke:

```bash
.venv/bin/python scripts/smoke_user_storage.py
```

Output: exit 0. The script writes an attachment through `LocalAttachmentStore`,
reads it as owner A, and proves the same ID cannot be read through owner B's
scope.

Whitespace validation:

```bash
git diff --check
```

Output: exit 0.

## Files

- Added `utils/user_storage.py`
- Added `tests/test_user_storage.py`
- Added `scripts/smoke_user_storage.py`
- Updated `utils/attachment_artifacts.py`
- Updated `utils/dataset_artifacts.py`
- Updated `utils/attachment_readers.py`
- Updated `epi_agent/protocol.py`
- Updated `epi_agent/agent.py`
- Updated `epi_agent/attachments/tools.py`
- Updated `epi_agent/db_rag/persistence.py`
- Updated `epi_agent/db_rag/tools.py`
- Updated `api/runtime.py`

## Concerns

The project has an ignored `tests/` pattern, so the newly added test and smoke
script must be force-added intentionally. Dataset compatibility retains the
existing explicit `runtime_root` plus `thread_id` test API while production
DB-RAG uses the authorized `scope.datasets` root; no process-global dataset
default remains.

## Review-fix evidence

- DB-RAG now passes the authorized `ThreadStorageScope` into
  `persist_sql_subset_artifact`; staging, promotion, journalling, and replay
  resolve its `datasets` directory without a global root.
- Dataset artifact resolution accepts `ThreadStorageScope`, and runtime
  preview, schema, CSV download, attachment-derived datasets, and custom
  Python resolve through that scope in production.
- Scoped attachment cleanup records the owner needed to reconstruct the exact
  scope; a new staged-TTL regression test proves deletion succeeds.
- `generated_dataset_artifact_paths` has no directory-name inference. Scoped
  callers use `dataset_root=`; legacy tests use explicit `runtime_root` plus
  `thread_id`.

Review verification:

```bash
.venv/bin/python -m pytest tests/test_user_storage.py tests/test_attachment_artifacts.py tests/test_dataset_artifacts.py tests/test_attachment_tools.py tests/test_db_rag_agent_tools.py tests/test_db_rag_agent_sql.py tests/test_api_runtime.py -q
```

Output:

```text
269 passed in 3.61s
```

`pytest -q --maxfail=20` was also run. Its remaining early failures are API
requests with `400 {"detail":"Invalid session ID"}` (reproduced directly
against the route without the required session header), plus sandbox-limited
socket-dependent tests. The scoped artifact and DB-RAG SQL regressions were
eliminated by the focused 269-test run above.

## Follow-up review evidence

- Added `tests/test_scoped_db_rag_persistence.py`, which invokes the real
  `persist_sql_subset_artifact` production function with a
  `ThreadStorageScope` and verifies the durable SQL subset is written to, and
  reloaded from, that scope's `datasets` directory.
- Updated the DB-RAG SQL test fixtures to use `context.thread_storage.datasets`
  rather than a process-global dataset root.
- The provenance API tests now send the stable local `X-Epi-Session-Id` header;
  they no longer contribute the former `400 Invalid session ID` failures.
- Public dataset persistence annotations name `ThreadStorageScope` rather than
  an opaque object type.

Follow-up verification:

```bash
.venv/bin/python -m pytest tests/test_user_storage.py tests/test_scoped_db_rag_persistence.py tests/test_attachment_artifacts.py tests/test_dataset_artifacts.py tests/test_attachment_tools.py tests/test_db_rag_agent_tools.py tests/test_db_rag_agent_sql.py tests/test_api_runtime.py tests/test_analysis_provenance_api.py -q
```

Output:

```text
276 passed in 4.80s
```

```bash
.venv/bin/python -m pytest -q --maxfail=1 --tb=short
```

Output: 7 passed, then `tests/test_api_auth.py` attempts to bind a local JWKS
server at `127.0.0.1:0` and the sandbox raises
`PermissionError: [Errno 1] Operation not permitted`. This is the first
remaining full-suite blocker after the session-header fix; it is an execution
sandbox limitation, not an application assertion failure.

## DB-RAG wrapper forwarding regression

`tests/test_scoped_db_rag_persistence.py` now also invokes the production
`epi_agent.db_rag.tools._persist_extraction_result` wrapper with a real
`ThreadStorageScope`. A narrow recording wrapper delegates to the real
`persist_sql_subset_artifact`, proving the exact authorized scope is passed as
`runtime_root` while real staging, promotion, and commit create the scoped
dataset artifact. Removing the production `runtime_root=runtime_root` argument
therefore fails this regression.

Verification:

```bash
.venv/bin/python -m pytest tests/test_user_storage.py tests/test_scoped_db_rag_persistence.py tests/test_attachment_artifacts.py tests/test_dataset_artifacts.py tests/test_attachment_tools.py tests/test_db_rag_agent_tools.py tests/test_db_rag_agent_sql.py tests/test_api_runtime.py tests/test_analysis_provenance_api.py -q
.venv/bin/python scripts/smoke_user_storage.py
git diff --check
```

Output: `277 passed in 5.00s`; smoke and whitespace validation exited 0.
