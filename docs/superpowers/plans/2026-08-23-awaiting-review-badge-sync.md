# Awaiting-Review Badge Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the stale `Awaiting review` sidebar badge after a successful review response and keep it absent after run cancellation.

**Architecture:** Keep FastAPI and conversation-summary projection unchanged. The interrupt-resume and active-run-cancellation paths will trigger the existing guarded, non-blocking saved-conversation refresh after accepting returned thread state.

**Tech Stack:** React 19, TypeScript, Vitest, Testing Library, FastAPI, Playwright, Python 3.12

## Global Constraints

- Keep the change localized; do not refactor conversation state management.
- Preserve activity history and clarification trace.
- Do not add continuous polling or cross-tab synchronization.
- Use `.venv/bin/python` for Python commands.
- Build `frontend` before refreshing the build manifest.
- Run the dedicated real smoke once, for no more than five minutes.

---

### Task 1: Refresh the sidebar after review and cancellation transitions

**Files:**
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/App.tsx:883-885,919-921`

**Interfaces:**
- Consumes: `refreshSavedConversations()` and `loadConversationHistory`
- Produces: successful resume and cancellation transitions that refresh the authoritative conversation summaries

- [ ] **Step 1: Write the failing regression test**

Add `clears the saved-conversation review badge after resume and cancellation` to `frontend/src/App.test.tsx`. Its URL-based `fetchMock` must return an initial conversation summary with `awaiting_review: true`, an interrupted `agent_clarification` thread, a running resume response without `active_interrupt`, a cancelled response, and subsequent summaries with `awaiting_review: false`.

Assert the exact behavior:

```tsx
expect(await screen.findByText("Awaiting review")).toBeInTheDocument();
fireEvent.click(screen.getByRole("radio", { name: "Let the agent decide" }));
fireEvent.click(screen.getByRole("button", { name: "Continue" }));
await waitFor(() => {
  expect(screen.queryByText("Awaiting review")).not.toBeInTheDocument();
});
fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
await waitFor(() => {
  expect(screen.queryByText("Awaiting review")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Ask a question about your dataset!")).toBeEnabled();
});
```

Also assert `/api/conversations` is requested once after resume and once after cancellation.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
npm --prefix frontend test -- --run frontend/src/App.test.tsx -t "clears the saved-conversation review badge after resume and cancellation"
```

Expected: FAIL because no post-resume or post-cancel conversation refresh occurs.

- [ ] **Step 3: Add the minimal production change**

Extend each existing successful state-application branch with:

```tsx
if (loadConversationHistory) {
  void refreshSavedConversations();
}
```

Add it after `setError(null)` in `resumeActiveInterrupt` and `cancelActiveRun`. Do not change polling, refresh guards, backend schemas, or backend history projection.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command. Expected: PASS without warnings.

- [ ] **Step 5: Run the complete frontend test suite**

Run `npm --prefix frontend test`. Expected: all frontend tests pass.

- [ ] **Step 6: Commit the tested UI fix**

```bash
git add frontend/src/App.tsx frontend/src/App.test.tsx
git commit -m "fix: refresh review badge after thread transitions"
```

---

### Task 2: Add and run the required real browser smoke

**Files:**
- Create: `scripts/smoke_awaiting_review_badge_sync_real.py`
- Modify: `frontend/dist/index.html`
- Modify: `frontend/dist/assets/*`
- Modify: build manifest reported by `scripts/verify_working_demo_delivery.py`

**Interfaces:**
- Consumes: compiled frontend, real FastAPI app, configured real model/provider, installed study data, and Playwright
- Produces: an executable smoke proving rendered and raw review state agree after resume and cancellation

- [ ] **Step 1: Create the dedicated smoke**

Follow the process, timeout, browser fallback, diagnostics, and teardown patterns in `scripts/e2e_agent_activity_timeline_real.py`. Use its real `DEFAULT_QUERY` and helpers where practical. The smoke must:

1. reject timeouts above 300 seconds;
2. launch real FastAPI with an isolated runtime and compiled `frontend/dist`;
3. submit `DEFAULT_QUERY` through the browser and wait for `Review dataset plan`;
4. assert raw summary `awaiting_review is True` and rendered `Awaiting review`;
5. drive `Approve & continue` until `Approve plan and extract` is clicked;
6. assert the resume response has no active interrupt and the badge disappears;
7. click `Cancel run` immediately, then assert the composer is enabled, raw run state is `cancelled`, raw summary has `awaiting_review is False`, and the badge remains absent;
8. preserve API log, raw states, page text, traceback, and screenshot on failure; and
9. print one PASS line with the artifact directory on success.

Use these existing accessible selectors:

```python
page.get_by_text("Awaiting review", exact=True)
page.get_by_role("heading", name="Review dataset plan", exact=True)
page.get_by_role("button", name="Approve & continue", exact=True)
page.get_by_role("button", name="Approve plan and extract", exact=True)
page.get_by_role("button", name="Cancel run", exact=True)
page.get_by_label("Ask a question about your dataset!")
```

- [ ] **Step 2: Validate smoke syntax and executable mode**

Run `chmod +x scripts/smoke_awaiting_review_badge_sync_real.py` and `.venv/bin/python -m py_compile scripts/smoke_awaiting_review_badge_sync_real.py`. Expected: exit code 0.

- [ ] **Step 3: Build the tracked production UI**

Run `npm --prefix frontend run build`. Expected: TypeScript and Vite succeed.

- [ ] **Step 4: Refresh the build manifest**

Run `.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest`. Expected: exit code 0.

- [ ] **Step 5: Run the dedicated real smoke exactly once**

Run `.venv/bin/python scripts/smoke_awaiting_review_badge_sync_real.py --timeout-seconds 300`. Expected: one PASS line. On failure, do not rerun; report the preserved diagnostics.

- [ ] **Step 6: Review the final diff**

Run `git diff --check` and `git status --short`. Expected: no whitespace errors and only focused source, test, smoke, compiled bundle, and manifest changes.

- [ ] **Step 7: Commit smoke and delivery artifacts**

Stage the new smoke, `frontend/dist`, and the verifier-reported manifest path, then commit with `test: smoke review badge synchronization`.
