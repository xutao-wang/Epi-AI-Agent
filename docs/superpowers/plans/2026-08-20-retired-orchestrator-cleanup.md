# Retired Orchestrator Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the approved unused orchestrator-era backend and frontend remnants without changing active agent behavior or public compatibility contracts.

**Architecture:** Strengthen the existing source-level architecture guard first, then delete only symbols and assets that have no current consumers. Preserve the active state accessors, `routing_decision` compatibility, the public `output` field, and Python execution policy, then rebuild the tracked frontend delivery bundle.

**Tech Stack:** Python 3.12, pytest, React 19, TypeScript, Vitest, Vite.

## Global Constraints

- Run Python tooling only through `.venv/bin/python`.
- Do not remove `routing_decision` event compatibility or the public thread-state `output` contract.
- Do not remove `get_conversation_events`, `get_artifact_files`, or `validate_generated_code`.
- Every frontend source change must run `npm --prefix frontend run build` and refresh `frontend/dist/build-manifest.json` with `.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest`.
- Do not add a feature smoke: this change deletes unreachable compatibility code and has no new user-visible or production entry point to exercise.

---

### Task 1: Lock the retired-remnant boundary with a failing architecture test

**Files:**
- Modify: `tests/test_centralized_epi_agent_architecture.py`

**Interfaces:**
- Consumes: repository source files and frontend source paths.
- Produces: `test_retired_orchestrator_and_fallback_helpers_are_absent()` as the permanent source-level regression guard for this cleanup.

- [ ] **Step 1: Extend the regression test before deleting production code**

Add the newly approved Python tokens to `forbidden`, add the retired frontend files to `retired`, and assert that the old CSS selector is absent:

```python
def test_retired_orchestrator_and_fallback_helpers_are_absent() -> None:
    forbidden = (
        "def merge_state_patch(",
        "def sole_study_id(",
        "def _sql_error_code(",
        "def invoke_epi_agent(",
        "def get_artifacts(",
        "agent_status",
    )
    offenders: list[str] = []
    for root_name in ("db_rag", "epi_agent", "graph"):
        for path in (REPO_ROOT / root_name).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in forbidden):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    retired_frontend = (
        REPO_ROOT / "frontend" / "src" / "StructuredOutput.tsx",
        REPO_ROOT / "frontend" / "src" / "StructuredOutput.test.tsx",
    )
    assert offenders == []
    assert [path.name for path in retired_frontend if path.exists()] == []
    styles = (REPO_ROOT / "frontend" / "src" / "styles.css").read_text(
        encoding="utf-8"
    )
    assert ".structured-output-" not in styles
```

- [ ] **Step 2: Run the test and verify the expected red result**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_centralized_epi_agent_architecture.py::test_retired_orchestrator_and_fallback_helpers_are_absent
```

Expected: FAIL listing `epi_agent/runtime.py`, `graph/state_views.py`, or the existing `StructuredOutput` files. This proves the guard detects the stale code before deletion.

---

### Task 2: Remove the approved backend remnants

**Files:**
- Modify: `epi_agent/runtime.py`
- Modify: `graph/state_views.py`
- Modify: `tests/test_conversation_events.py`

**Interfaces:**
- Consumes: the regression guard from Task 1.
- Produces: the same root graph, terminal error/control behavior, conversation-event accessors, and artifact-file accessors without the retired child invocation and compatibility projection.

- [ ] **Step 1: Remove only the retired runtime state and child invocation code**

In `epi_agent/runtime.py`:

- delete `agent_status: NotRequired[dict[str, Any]]` from `GenericEpiAgentState`;
- delete the `agent_status` entries from `_terminal_model_patch()` and the two terminal branches in `_execute_tools()` while retaining `terminal_error`, `terminal_control`, and `completion_blocked`;
- delete `invoke_epi_agent(...)`;
- delete only `"invoke_epi_agent"` from `__all__`.

The resulting terminal model patch must remain:

```python
def _terminal_model_patch(error: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": [AIMessage(content="")],
        "terminal_error": error,
        "completion_blocked": False,
    }
```

- [ ] **Step 2: Remove only the unused artifact compatibility view**

Delete `get_artifacts(...)` from `graph/state_views.py`. Retain its imports required by `get_conversation_events(...)` and `get_artifact_files(...)` and retain both functions unchanged.

- [ ] **Step 3: Remove the self-only test for the deleted view**

Change `test_state_view_accessors_validate_conversation_state()` so it imports and checks only `get_conversation_events(...)`:

```python
def test_state_view_accessors_validate_conversation_state() -> None:
    from graph.state_views import get_conversation_events

    with pytest.raises(TypeError, match="JSON-serializable"):
        get_conversation_events(
            {
                "artifacts": {
                    "conversation_events": [{"payload": object()}],
                    "files": {},
                }
            }
        )
```

- [ ] **Step 4: Run focused backend tests and verify green**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_centralized_epi_agent_architecture.py \
  tests/test_conversation_events.py \
  tests/test_export_thread.py \
  tests/test_epi_agent_root_state.py \
  tests/test_api_runtime.py \
  tests/test_execution_policy.py
```

Expected: all selected tests PASS.

---

### Task 3: Remove the orphan frontend component and fixture residue

**Files:**
- Delete: `frontend/src/StructuredOutput.tsx`
- Delete: `frontend/src/StructuredOutput.test.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: the architecture guard from Task 1.
- Produces: the same application UI, which renders answers through conversation messages and linked artifacts.

- [ ] **Step 1: Delete the orphan component and its self-only test**

Delete `frontend/src/StructuredOutput.tsx` and
`frontend/src/StructuredOutput.test.tsx`; no import replacement is required
because no application module imports the component.

- [ ] **Step 2: Remove the orphan CSS block**

Delete the complete block beginning with `.structured-output-panel` and ending
with `.structured-output-warning` from `frontend/src/styles.css`. Preserve the
following `@media (max-width: 900px)` block.

- [ ] **Step 3: Remove the unused retired routing fixture**

Delete this object member from the running-state response in
`frontend/src/App.test.tsx`:

```typescript
diagnostics: {
  next_action: "needs_code",
},
```

- [ ] **Step 4: Run focused frontend tests and architecture guard**

Run:

```bash
npm --prefix frontend test -- --run src/App.test.tsx
.venv/bin/python -m pytest -q tests/test_centralized_epi_agent_architecture.py
```

Expected: both commands PASS.

---

### Task 4: Rebuild delivery artifacts and verify the complete cleanup

**Files:**
- Modify generated files under: `frontend/dist/`
- Modify generated file: `frontend/dist/build-manifest.json`

**Interfaces:**
- Consumes: cleaned backend/frontend sources from Tasks 2 and 3.
- Produces: a tracked production frontend bundle whose manifest matches all current build inputs.

- [ ] **Step 1: Run the complete frontend test suite**

Run:

```bash
npm --prefix frontend test
```

Expected: all Vitest suites PASS.

- [ ] **Step 2: Rebuild the tracked production frontend bundle**

Run:

```bash
npm --prefix frontend run build
```

Expected: TypeScript compilation and Vite production build exit successfully.

- [ ] **Step 3: Refresh and verify the build manifest**

Run:

```bash
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
```

Expected: exit status 0 and `frontend/dist/build-manifest.json` updated for the rebuilt inputs.

- [ ] **Step 4: Run the full backend test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all pytest tests PASS.

- [ ] **Step 5: Verify removed and retained contracts directly**

Run:

```bash
rg -n 'invoke_epi_agent|get_artifacts|agent_status|StructuredOutput|structured-output|next_action: "needs_code"' \
  api db_rag epi_agent graph utils frontend/src \
  --glob '*.py' --glob '*.ts' --glob '*.tsx' --glob '*.css'
rg -n 'routing_decision|output: dict|validate_generated_code|get_conversation_events|get_artifact_files' \
  api epi_agent graph utils frontend/src \
  --glob '*.py' --glob '*.ts' --glob '*.tsx'
git diff --check
git status --short
```

Expected: the first search returns no matches; the second returns the explicitly retained compatibility and active runtime symbols; `git diff --check` succeeds; status lists only the planned cleanup and regenerated bundle changes.

- [ ] **Step 6: Review the final diff against the design**

Confirm every deletion is listed in the design, no `routing_decision` or public
`output` contract was removed, and no unrelated user files changed.
