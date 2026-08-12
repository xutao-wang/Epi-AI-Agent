# Activity Smoke Domain Decoupling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the real agent-activity-timeline smoke verify tool activity and plan-review wiring without requiring diabetes interpretation.

**Architecture:** Keep the production application unchanged. Replace the smoke's broad scientific request with a minimal request backed by one known installed form, reject any clarification as outside this smoke's contract, and treat every terminal API run state—including `done`—as an immediate failure before review.

**Tech Stack:** Python 3.12, pytest 9, Playwright, FastAPI, React/Vitest, the compiled Vite frontend, and the installed `report-india-synthetic@0.3.0` study package.

## Global Constraints

- Modify only `scripts/e2e_agent_activity_timeline_real.py` and `tests/test_agent_activity_timeline_smoke_runner.py` for the behavior correction.
- Do not change agent, DB-RAG, model, API, frontend, authentication, study-package, or AWS behavior.
- The smoke must use real FastAPI, LangGraph, DB-RAG, OpenAI, persisted activity state, and the compiled frontend.
- The smoke request must not contain `diabetes` or choose a scientific proxy.
- Protected state reads must retain the canonical `X-Epi-Session-ID`; `/api/health` must remain headerless.
- The real smoke has a maximum of 300 seconds, runs once after deterministic gates pass, preserves diagnostics on failure, and is not rerun automatically.
- Use `.venv/bin/python` for Python commands.
- Preserve the pre-existing unstaged `frontend/dist/build-manifest.json` change and keep it out of the harness implementation commit.

---

## File Structure

- Modify: `scripts/e2e_agent_activity_timeline_real.py` — owns the fixed smoke stimulus and wait/fail contract before dataset-plan review.
- Modify: `tests/test_agent_activity_timeline_smoke_runner.py` — specifies the domain-neutral stimulus, terminal-state behavior, clarification rejection, session header, and existing helper behavior.
- Verify only: `frontend/src/AgentActivityTimeline.tsx`, `frontend/src/App.tsx`, and their existing tests — no source edits.

### Task 1: Define and implement the domain-neutral smoke contract

**Files:**
- Modify: `tests/test_agent_activity_timeline_smoke_runner.py`
- Modify: `scripts/e2e_agent_activity_timeline_real.py`

**Interfaces:**
- Consumes: `DEFAULT_QUERY` and `_wait_for_dataset_plan_review(page, *, api_url, deadline, state_reader=_thread_state)` from the smoke runner.
- Produces: a fixed catalog-backed `DEFAULT_QUERY`; immediate `RuntimeError` for `done`, `cancelled`, `error`, `timeout`, or `agent_clarification` before visible plan review; unchanged success return when review controls are visible.

- [ ] **Step 1: Replace the diabetes clarification test with failing contract tests**

In `tests/test_agent_activity_timeline_smoke_runner.py`, add the exact query contract after `StepwiseReviewPage`:

```python
def test_default_query_uses_only_supported_baseline_fields() -> None:
    assert smoke.DEFAULT_QUERY == (
        "Create a baseline index-case dataset from Form 2A - INDEX CASE: "
        "Clinical/Demographic Form with participant ID, age, sex, and marital "
        "status. Present the dataset plan for review."
    )
    assert "diabetes" not in smoke.DEFAULT_QUERY.casefold()
```

Add this test after the existing terminal error test:

```python
def test_review_wait_fails_when_run_completes_without_review() -> None:
    state = {"run": {"state": "done"}}

    with pytest.raises(
        RuntimeError,
        match="Agent run ended before dataset-plan review:.*state done",
    ):
        smoke._wait_for_dataset_plan_review(
            NoReviewPage(),
            api_url="http://unused.test",
            deadline=time.monotonic() + 0.1,
            state_reader=lambda _url: state,
        )
```

Replace `test_review_wait_answers_expected_diabetes_clarification` and its interactive page classes with:

```python
def test_review_wait_rejects_unexpected_scientific_clarification() -> None:
    state = {
        "run": {"state": "interrupted"},
        "active_interrupt": {
            "id": "clarification-1",
            "type": "agent_clarification",
            "question": "Which scientific proxy should be used?",
            "options": [
                {"id": "proxy", "label": "Use a proxy"},
                {"id": "omit", "label": "Omit the variable"},
            ],
        },
    }

    with pytest.raises(
        RuntimeError,
        match="unexpected clarification.*Which scientific proxy",
    ):
        smoke._wait_for_dataset_plan_review(
            NoReviewPage(),
            api_url="http://unused.test",
            deadline=time.monotonic() + 0.1,
            state_reader=lambda _url: state,
        )
```

- [ ] **Step 2: Run the three new contracts and verify intended failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_agent_activity_timeline_smoke_runner.py::test_default_query_uses_only_supported_baseline_fields tests/test_agent_activity_timeline_smoke_runner.py::test_review_wait_fails_when_run_completes_without_review tests/test_agent_activity_timeline_smoke_runner.py::test_review_wait_rejects_unexpected_scientific_clarification -q
```

Expected: the query contract fails because the current query contains `diabetes`; the `done` contract receives `TimeoutError`; and the clarification contract reaches the old proxy-selection path instead of the required `RuntimeError`.

- [ ] **Step 3: Replace the smoke stimulus**

In `scripts/e2e_agent_activity_timeline_real.py`, replace `DEFAULT_QUERY` with:

```python
DEFAULT_QUERY = (
    "Create a baseline index-case dataset from Form 2A - INDEX CASE: "
    "Clinical/Demographic Form with participant ID, age, sex, and marital "
    "status. Present the dataset plan for review."
)
```

- [ ] **Step 4: Make every terminal state fail immediately before review**

Replace:

```python
if run_state in {"cancelled", "error", "timeout"}:
```

with:

```python
if run_state in {"done", "cancelled", "error", "timeout"}:
```

Retain the existing error-code and user-message formatting. A normal `done` state must report:

```text
Agent run ended before dataset-plan review: Agent run ended with state done. Error: AGENT_RUN_TERMINATED
```

- [ ] **Step 5: Reject clarification instead of choosing scientific meaning**

Delete `answered_interrupt_ids: set[str] = set()` and replace the clarification-selection block with:

```python
        interrupt = state.get("active_interrupt") or {}
        if interrupt.get("type") == "agent_clarification":
            question = str(
                interrupt.get("question")
                or "The agent did not provide clarification text."
            )
            raise RuntimeError(
                "Agent requested unexpected clarification before "
                f"dataset-plan review: {question}"
            )
```

Do not select an option, click Continue, or retain clarification IDs.

- [ ] **Step 6: Run the complete runner suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_agent_activity_timeline_smoke_runner.py -q
```

Expected: every test passes, including existing review, transient-history, repeated-label, diagnostic-order, and session-header coverage.

- [ ] **Step 7: Commit only the harness correction**

Run:

```bash
git diff --check -- scripts/e2e_agent_activity_timeline_real.py tests/test_agent_activity_timeline_smoke_runner.py
git diff -- scripts/e2e_agent_activity_timeline_real.py tests/test_agent_activity_timeline_smoke_runner.py
git add scripts/e2e_agent_activity_timeline_real.py tests/test_agent_activity_timeline_smoke_runner.py
git commit -m "test: decouple activity smoke from diabetes query"
```

Expected: exactly the two harness files are committed. `frontend/dist/build-manifest.json` remains unstaged.

### Task 2: Verify the corrected boundary and run the real smoke once

**Files:**
- Verify: `scripts/e2e_agent_activity_timeline_real.py`
- Verify: tracked activity backend and frontend tests
- Generated verification output: `frontend/dist/`

**Interfaces:**
- Consumes: the Task 1 commit, installed `report-india-synthetic@0.3.0`, configured real OpenAI credentials, real DB-RAG assets, and compiled frontend.
- Produces: deterministic test/build evidence and one PASS or preserved FAIL diagnostic directory for the real activity smoke.

- [ ] **Step 1: Run tracked activity and cancellation compatibility tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_active_run_cancellation.py tests/test_activity_instrumentation.py tests/test_activity_labels.py tests/test_activity_store.py tests/test_agent_activity_timeline_smoke_runner.py tests/test_api_activity_timeline.py tests/test_cancellation_boundaries.py tests/test_local_cancellation_smoke_runner.py tests/test_run_cancellation.py -q
```

Expected: all selected tracked tests pass without failures or errors.

- [ ] **Step 2: Run complete frontend tests**

Run:

```bash
npm --prefix frontend test
```

Expected: all Vitest files and tests pass.

- [ ] **Step 3: Rebuild the compiled frontend and refresh its manifest**

Run:

```bash
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
```

Expected: TypeScript and Vite exit successfully, and the manifest matches the compiled assets and current frontend sources. The delivery verifier may still report the separately scoped, pre-existing `.gitignore` policy violations, but it must not report a stale or invalid frontend manifest.

Do not stage or commit `frontend/dist/build-manifest.json` in the harness correction. Record its final diff for the broader release-cleanup decision.

- [ ] **Step 4: Confirm prerequisites before consuming the real-smoke run**

Run:

```bash
test -x .venv/bin/python
test -f frontend/dist/index.html
test -f study_data/studies/packages/report-india-synthetic/0.3.0/database/schema_catalog.json
.venv/bin/python -c 'from utils.env_loader import load_app_environment; from pathlib import Path; import os; load_app_environment(Path.cwd()); assert os.environ.get("OPENAI_API_KEY", "").strip(), "OPENAI_API_KEY is missing"'
test ! -e /private/tmp/report-agent-activity-domain-decoupling-20260812
```

Expected: every command exits `0`. If a prerequisite fails, repair the environment without running the smoke. Do not delete or overwrite an existing artifact directory; choose a new explicit path.

- [ ] **Step 5: Run the corrected real activity smoke exactly once**

Run:

```bash
.venv/bin/python scripts/e2e_agent_activity_timeline_real.py --timeout-seconds 300 --artifact-dir /private/tmp/report-agent-activity-domain-decoupling-20260812
```

Expected within five minutes:

```text
PASS agent activity timeline smoke; diagnostics: /private/tmp/report-agent-activity-domain-decoupling-20260812
```

If it fails or times out, do not rerun automatically. Preserve and report the API log, raw state when available, page text, HTML, screenshot, and traceback. Stop before AWS packaging or deployment.

- [ ] **Step 6: Record final repository state without claiming broader readiness**

Run:

```bash
git status --short --branch
git log -3 --oneline
git diff --check
```

Expected: the Task 1 commit is present, no harness source remains modified, the frontend manifest modification is reported separately, and no whitespace errors exist. No AWS upload, SSM command, deployment, or public-access change occurs.

Do not claim `aws-test` is fully release-ready from this focused plan alone. The separately discussed `.gitignore`, complete tracked Python suite, Docker recovery tests, immutable release build, and private hosted acceptance remain broader release gates.
