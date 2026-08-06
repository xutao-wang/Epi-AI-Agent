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
