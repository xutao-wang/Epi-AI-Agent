# Hide Tool Names in Activity Smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the real activity browser smoke with the approved UI contract that displays friendly activity labels without technical tool names.

**Architecture:** Keep technical tool names in the backend activity API for diagnostics and keep the smoke's existing API assertion. Isolate the browser criterion in a small helper that accepts rendered timeline text, rejects `dbrag-*` leakage, and continues rejecting public failure text.

**Tech Stack:** Python 3.12, pytest, Playwright smoke runner

## Global Constraints

- Do not change backend activity persistence or API schemas.
- Do not change `AgentActivityTimeline` rendering.
- Continue requiring the friendly catalog-search and dataset-review labels.
- Continue verifying technical tool names in raw API state.
- Do not make an OpenAI request during deterministic verification.

---

### Task 1: Correct the rendered activity contract

**Files:**
- Modify: `scripts/e2e_agent_activity_timeline_real.py`
- Test: `tests/test_agent_activity_timeline_smoke_runner.py`

**Interfaces:**
- Consumes: `timeline.inner_text()` from the Playwright activity timeline locator.
- Produces: `_assert_plain_language_timeline(rendered_text: str) -> None`.

- [ ] **Step 1: Write failing helper tests**

Add tests that require plain-language activity text to pass and rendered `dbrag-search_catalog` text to raise `AssertionError` with a technical-name leakage message.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```sh
../../.venv/bin/python -m pytest -q \
  tests/test_agent_activity_timeline_smoke_runner.py \
  -k plain_language_timeline
```

Expected: failures because `_assert_plain_language_timeline` does not exist.

- [ ] **Step 3: Implement the minimal browser assertion**

Add `_assert_plain_language_timeline(rendered_text: str) -> None`, reject case-insensitive `dbrag-` text and existing case-insensitive `fail` text, and replace the obsolete `locator("code")` tool-name expectation with one call using `timeline.inner_text()`.

- [ ] **Step 4: Run focused and aggregate verification**

Run:

```sh
../../.venv/bin/python -m pytest -q \
  tests/test_agent_activity_timeline_smoke_runner.py \
  tests/test_activity_labels.py \
  tests/test_activity_store.py \
  tests/test_activity_instrumentation.py \
  tests/test_api_activity_timeline.py
```

Then run all frontend tests from a temporary copy using the main checkout's installed `node_modules`, build that temporary frontend, and compare its generated assets with tracked `frontend/dist`.

Expected: all Python and frontend tests pass, generated frontend assets match tracked assets, and the release build succeeds from a clean worktree.

- [ ] **Step 5: Commit**

```sh
git add \
  docs/superpowers/plans/2026-08-12-hide-tool-names-activity-smoke.md \
  scripts/e2e_agent_activity_timeline_real.py \
  tests/test_agent_activity_timeline_smoke_runner.py
git commit -m "test: hide technical names in activity smoke"
```
