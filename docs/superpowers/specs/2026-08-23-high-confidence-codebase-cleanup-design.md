# High-Confidence Codebase Cleanup Design

## Goal

Remove only code and generated artifacts that are demonstrably stale in the
current `local-multi-study-provider` system, including explicitly approved
ignored local files, without disrupting active UI, API, DB-RAG, runtime,
study-package, or development workflows.

## Scope

The cleanup removes these source files:

- Tracked `db_rag/service/errors.py`. Its sole
  `DbRagUnanswerableError` class has no imports, references, string-based
  dispatch, documented entrypoint, or test consumer.
- Ignored `scripts/e2e_streamlit_db_rag_grouped_review_real.py`. It launches
  the removed `streamlit_app.py`, requires the retired Streamlit runtime, and
  has no current caller. The repository explicitly tests that Streamlit and
  its runtime dependencies are absent.

The cleanup also removes generated repository-local residue:

- `__pycache__` directories and `.pyc` files outside `.venv`,
  `frontend/node_modules`, `.git`, runtime data, and study data.
- `.pytest_cache` at the repository root.

The cleanup preserves all ambiguous or active surfaces, including:

- runtime settings in the API and active composer model controls;
- DB-RAG SQL generation, execution, and repair code;
- current FastAPI/TypeScript, DB-RAG, study-package, and provider smokes;
- ignored tests and scripts that still exercise current behavior;
- `.env`, `.venv`, `frontend/node_modules`, runtime data, study data, package
  archives, and model configuration;
- tracked production frontend assets.

No broader renaming, refactoring, dependency changes, UI changes, API changes,
or behavior changes are included.

## Safety and Recovery

Before deleting the ignored Streamlit smoke, create a timestamped archive under
`/private/tmp` containing that file. Record and verify the archive's SHA-256
checksum and list its contents before deletion. The archived file is not part
of the active repository and is only a temporary recovery copy; macOS may
eventually clear `/private/tmp`.

The tracked exception module remains recoverable through Git history. Generated
caches do not require an archive because Python and pytest can recreate them.

If verification reveals an unexpected dependency on the ignored Streamlit
smoke, restore it from the verified archive and stop for review. Do not expand
the cleanup to remove additional candidates discovered during implementation.

## Test-Driven Removal Contract

Extend the tracked cleanup regression coverage before deleting source. The
failing contract must assert that both source paths are absent:

- `db_rag/service/errors.py`
- `scripts/e2e_streamlit_db_rag_grouped_review_real.py`

Run the focused contract once before deletion and confirm it fails for the
expected existing paths. After deletion, rerun it and require a pass.

## Generated-Artifact Cleanup

Resolve cache targets explicitly before deletion. Never use a recursive target
root, unresolved environment variable, broad glob, `.venv`, `node_modules`,
`.git`, runtime storage, or study storage. Remove only the enumerated cache
directories and files inside the repository boundary.

Verification commands must set `PYTHONDONTWRITEBYTECODE=1` and disable pytest's
cache provider so the cleanup does not immediately recreate the removed
artifacts.

## Verification

Verification consists of:

1. The focused stale-source removal contract.
2. The complete Python test suite using Python 3.12, with bytecode and pytest
   cache generation disabled.
3. The complete frontend Vitest suite.
4. A fresh static scan confirming neither removed source path nor
   `DbRagUnanswerableError` remains in active code.
5. `git diff --check` and a final worktree-status review.

A production frontend rebuild is not required because no frontend build input
changes. A real model/browser smoke is not required because the cleanup removes
only unreachable code and a smoke for a runtime that no longer exists; it does
not introduce or modify user-visible behavior.

## Delivery

Commit the tracked regression-test change and tracked module deletion. The
ignored Streamlit deletion and generated-cache cleanup do not appear in Git, so
the final report must state:

- the archive path and checksum;
- the ignored stale script removed;
- the number and size of generated artifacts removed;
- focused, Python, and frontend verification results;
- any preserved candidates that were intentionally excluded as ambiguous.
