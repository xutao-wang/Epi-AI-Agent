# Conversation Thread Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee that messages, activity, artifacts, errors, runtime settings, and every review type render and mutate only their owning conversation while paused reviews remain durable across thread switches.

**Architecture:** Add an `awaiting_review` projection to conversation summaries, then make the React application enforce one guarded thread-snapshot boundary. Every asynchronous state producer carries an immutable owning thread ID plus a generation; only a current, matching `ApiThreadState.thread_id` may be applied. Review actions capture `{threadId, interruptId}` from the rendered snapshot, and the UI clears old content while a newly selected conversation loads.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, LangGraph SQLite checkpoints, React 19, TypeScript 5.8, Vitest, Testing Library, Playwright.

## Global Constraints

- Preserve backend checkpoint ownership by `(owner_user_id, thread_id)`.
- Switching conversations must not approve, cancel, reject, discard, or resume workflow state.
- Reuse the existing review cancellation actions; do not add a second discard control.
- Apply one isolation contract to dataset-plan review, dataset review, analysis-result review, agent clarification, and model-output-limit review.
- Use `.venv/bin/python` for Python commands; the repository requires Python 3.12.
- Add a dedicated executable real-browser smoke under `scripts/`, with a five-minute maximum and one run only.
- The smoke must use the real FastAPI backend, compiled TypeScript frontend, LangGraph state, and browser controls; it must not stub production dependencies.
- Every frontend change requires `npm --prefix frontend run build` followed by `.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest`.

## File Structure

- `api/conversation_history.py`: extend the durable conversation summary value object with informational `awaiting_review` state; SQLite persistence remains unchanged.
- `api/runtime.py`: project review presence from the owning thread's current snapshot into conversation summaries.
- `api/schemas.py`: publish `awaiting_review` in the conversation-list API contract.
- `tests/test_api_runtime.py`, `tests/test_api_server.py`: verify summary projection and serialization without cross-thread leakage.
- `frontend/src/types.ts`: mirror the summary contract.
- `frontend/src/ConversationHistory.tsx`, `frontend/src/styles.css`: render the per-thread **Awaiting review** badge.
- `frontend/src/ConversationHistory.test.tsx`: verify badge ownership and accessibility.
- `frontend/src/App.tsx`: own selection generations, validate all state applications, clear stale views during loading, and bind reviews to immutable ownership.
- `frontend/src/App.test.tsx`: exercise out-of-order open, poll, submit, resume, cancel, and conflict responses plus every review type.
- `scripts/e2e_conversation_thread_isolation_real.py`: dedicated compiled-frontend/FastAPI browser regression smoke.
- `frontend/dist/**`, `frontend/build-manifest.json`: regenerated production bundle and delivery manifest.

---

### Task 1: Project awaiting-review status into conversation summaries

**Files:**
- Modify: `api/conversation_history.py:15-29`
- Modify: `api/runtime.py:2180-2200`
- Modify: `api/schemas.py:472-484`
- Modify: `tests/test_api_runtime.py`
- Modify: `tests/test_api_server.py`

**Interfaces:**
- Consumes: `_snapshot(identity, thread_id, thread)` and `_active_interrupt(snapshot, values)` from `api/runtime.py`.
- Produces: `ConversationSummary.awaiting_review: bool` in Python and JSON.

- [ ] **Step 1: Write failing runtime and server tests**

Add a runtime test that creates two owned history records, gives only Thread A a projected public interrupt, and asserts exact ownership:

```python
def test_list_conversations_marks_only_owning_thread_as_awaiting_review(
    tmp_path,
    monkeypatch,
) -> None:
    identity = _identity("owner-a")
    history = ConversationHistoryStore(tmp_path / "history.db")
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings: None,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        checkpoint_path=tmp_path / "checkpoints.db",
        history_store=history,
    )
    thread_a = runtime.create_thread(identity)
    thread_b = runtime.create_thread(identity)
    history.promote_pending(identity.owner_user_id, thread_a)
    history.promote_pending(identity.owner_user_id, thread_b)

    snapshots = {
        thread_a: _plan_review_snapshot(),
        thread_b: SimpleNamespace(values={}, next=(), interrupts=[]),
    }
    monkeypatch.setattr(
        runtime,
        "_snapshot",
        lambda _identity, thread_id, _thread: snapshots[thread_id],
    )

    summaries = {
        item.thread_id: item
        for item in runtime.list_conversations(identity)
    }
    assert summaries[thread_a].awaiting_review is True
    assert summaries[thread_b].awaiting_review is False
```

This test deliberately reuses the existing `_identity`, `_plan_review_snapshot`, `_DEFAULT_RUNTIME_SETTINGS`, and `SimpleNamespace` definitions in `tests/test_api_runtime.py`. Extend the list-conversations API assertion in `tests/test_api_server.py`:

```python
assert response.json()["items"][0]["awaiting_review"] is False
```

- [ ] **Step 2: Run the focused tests and confirm the contract is absent**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_api_runtime.py -k 'list_conversations and awaiting_review' \
  tests/test_api_server.py -k 'list_conversations' -q
```

Expected: FAIL because `ConversationSummary` and the API response do not expose `awaiting_review`.

- [ ] **Step 3: Extend the summary models without changing SQLite schema**

Add a transient default to the history value object:

```python
@dataclass(frozen=True)
class ConversationSummary:
    thread_id: str
    title: str
    title_source: str
    model_name: str
    created_at: str
    updated_at: str
    last_opened_at: str | None = None
    archived_at: str | None = None
    awaiting_review: bool = False
```

Add the same field to `api.schemas.ConversationSummary`:

```python
class ConversationSummary(BaseModel):
    thread_id: str
    title: str
    title_source: Literal["automatic", "manual"]
    model_name: str
    created_at: str
    updated_at: str
    last_opened_at: str | None = None
    archived_at: str | None = None
    awaiting_review: bool = False
```

Do not add a database column. This value describes the current checkpoint, not history metadata.

- [ ] **Step 4: Project each summary from its own snapshot**

Import `replace` from `dataclasses` in `api/runtime.py` and update `list_conversations`:

```python
def _conversation_awaiting_review(
    self,
    identity: RequestIdentity,
    thread_id: str,
) -> bool:
    thread = self._require_owned_thread(identity, thread_id)
    snapshot = self._snapshot(identity, thread_id, thread)
    if snapshot is None:
        return False
    values = _projection_values(snapshot)
    return _active_interrupt(snapshot, values) is not None

def list_conversations(
    self,
    identity: RequestIdentity | None = None,
):
    owner_user_id = identity.owner_user_id if identity is not None else "local-user"
    items = self.history_store.list(owner_user_id) if self.history_store else []
    if identity is None:
        return items
    return [
        replace(
            item,
            awaiting_review=self._conversation_awaiting_review(
                identity,
                item.thread_id,
            ),
        )
        for item in items
    ]
```

If a malformed checkpoint cannot be projected, log the exception with the owning `thread_id` and report `awaiting_review=False`; listing history must remain available. Do not load or create a provider-bound graph merely to calculate the badge.

- [ ] **Step 5: Run the focused backend tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_api_runtime.py -k 'list_conversations' \
  tests/test_api_server.py -k 'conversation' -q
```

Expected: PASS, including one waiting thread and one non-waiting thread with no status leakage.

- [ ] **Step 6: Commit the backend contract**

```bash
git add api/conversation_history.py api/runtime.py api/schemas.py \
  tests/test_api_runtime.py tests/test_api_server.py
git commit -m "feat: expose conversation review status"
```

---

### Task 2: Render thread-specific review status in conversation history

**Files:**
- Modify: `frontend/src/types.ts:88-98`
- Modify: `frontend/src/ConversationHistory.tsx:75-145`
- Modify: `frontend/src/ConversationHistory.test.tsx`
- Modify: `frontend/src/styles.css:211-405`

**Interfaces:**
- Consumes: `ConversationSummary.awaiting_review: boolean` from Task 1.
- Produces: an accessible `.conversation-history-review-status` label rendered only for its owning item.

- [ ] **Step 1: Write the failing component test**

Add `awaiting_review: false` to the base `item`, then add:

```tsx
it("labels only the conversation that is awaiting review", () => {
  render(
    <ConversationHistory
      activeThreadId={null}
      items={[
        { ...item, thread_id: "thread-a", title: "Thread A", awaiting_review: true },
        { ...item, thread_id: "thread-b", title: "Thread B", awaiting_review: false },
      ]}
      onOpen={vi.fn()}
      onRename={vi.fn()}
      onArchive={vi.fn()}
      onRestore={vi.fn()}
      onDelete={vi.fn()}
    />,
  );

  expect(screen.getByText("Awaiting review")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Thread A" }).parentElement,
  ).toHaveTextContent("Awaiting review");
  expect(
    screen.getByRole("button", { name: "Thread B" }).parentElement,
  ).not.toHaveTextContent("Awaiting review");
});
```

- [ ] **Step 2: Run the component test and confirm it fails**

Run:

```bash
npm --prefix frontend test -- ConversationHistory.test.tsx
```

Expected: FAIL because the type and badge do not exist.

- [ ] **Step 3: Add the TypeScript field and badge markup**

Extend the type:

```ts
export interface ConversationSummary {
  thread_id: string;
  title: string;
  title_source: "automatic" | "manual";
  model_name: string;
  created_at: string;
  updated_at: string;
  last_opened_at: string | null;
  archived_at: string | null;
  awaiting_review: boolean;
}
```

Render beside the title button:

```tsx
{item.awaiting_review ? (
  <span
    aria-label={`${item.title} is awaiting review`}
    className="conversation-history-review-status"
  >
    Awaiting review
  </span>
) : null}
```

Add compact status styling that remains readable in the narrow sidebar:

```css
.conversation-history-review-status {
  display: inline-flex;
  margin-top: 0.25rem;
  border-radius: 999px;
  background: #fff4d6;
  color: #7a4b00;
  font-size: 0.75rem;
  font-weight: 700;
  line-height: 1;
  padding: 0.3rem 0.45rem;
}
```

- [ ] **Step 4: Run the component tests**

Run:

```bash
npm --prefix frontend test -- ConversationHistory.test.tsx
```

Expected: PASS.

- [ ] **Step 5: Commit the history presentation**

```bash
git add frontend/src/types.ts frontend/src/ConversationHistory.tsx \
  frontend/src/ConversationHistory.test.tsx frontend/src/styles.css
git commit -m "feat: label conversations awaiting review"
```

---

### Task 3: Enforce one guarded thread-snapshot boundary in the React app

**Files:**
- Modify: `frontend/src/App.tsx:145-920`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: `ApiThreadState.thread_id`, selected history `threadId`, and immutable `{threadId, interruptId}` review ownership.
- Produces: `applyOwnedThreadState(ownerThreadId, generation, nextState): boolean`, which is the only path that installs asynchronous thread snapshots.

- [ ] **Step 1: Write failing out-of-order thread-open tests**

Use the existing `deferred<T>()` helper. Add a test that opens A and B, resolves B first and A last, and asserts that A never overwrites B:

```tsx
it("ignores a late state response from a previously selected conversation", async () => {
  const stateA = deferred<Response>();
  const stateB = deferred<Response>();
  // Configure listConversations with Thread A and Thread B and route each
  // GET /api/threads/:id/state request to its deferred response.

  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Thread A" }));
  fireEvent.click(screen.getByRole("button", { name: "Thread B" }));

  await act(async () => {
    stateB.resolve(jsonResponse(threadState({
      thread_id: "thread-b",
      conversation: [{ id: "b-user", role: "user", text: "Message B" }],
    })));
  });
  expect(await screen.findByText("Message B")).toBeInTheDocument();

  await act(async () => {
    stateA.resolve(jsonResponse(threadState({
      thread_id: "thread-a",
      conversation: [{ id: "a-user", role: "user", text: "Who are you" }],
    })));
  });
  expect(screen.queryByText("Who are you")).not.toBeInTheDocument();
  expect(screen.getByText("Message B")).toBeInTheDocument();
});
```

Add assertions that the previous conversation content disappears immediately and a `role="status"` loading message appears after selecting B.

- [ ] **Step 2: Write failing guards for every asynchronous state producer**

Add focused deferred-response tests for:

```text
poll A -> select B -> late poll A ignored
submit A -> select B -> late submit A ignored
cancel A -> select B -> late cancel A ignored
HTTP 409 refresh A -> select B -> late refresh A ignored
resume review A -> select B -> late resume A ignored
```

Each test must assert that Thread B's unique message remains visible and Thread A's unique message/review does not return. For a deliberately malformed API response, return `thread_id: "thread-a"` from Thread B's endpoint and assert a recoverable error plus no A content.

- [ ] **Step 3: Run the new App tests and confirm stale state wins today**

Run:

```bash
npm --prefix frontend test -- App.test.tsx
```

Expected: the new tests FAIL because `applyThreadState` accepts responses without owner/generation validation and thread switching retains old content while loading.

- [ ] **Step 4: Add selection generation and atomic loading state**

Add refs/state near the current polling generation:

```ts
const selectionGenerationRef = useRef(0);
const selectedThreadIdRef = useRef<string | null>(null);
const [isLoadingConversation, setIsLoadingConversation] = useState(false);
```

Keep `selectedThreadIdRef` synchronized whenever selection changes. Add one validator:

```ts
function applyOwnedThreadState(
  ownerThreadId: string,
  generation: number,
  nextState: ApiThreadState,
): boolean {
  if (
    selectionGenerationRef.current !== generation ||
    selectedThreadIdRef.current !== ownerThreadId
  ) {
    return false;
  }
  if (nextState.thread_id !== ownerThreadId) {
    setState(null);
    setError("The selected conversation returned mismatched thread data. Please try again.");
    setIsLoadingConversation(false);
    return false;
  }
  applyThreadState(nextState);
  setIsLoadingConversation(false);
  return true;
}
```

`applyThreadState` remains the synchronous field installer, but asynchronous callers must use `applyOwnedThreadState`.

- [ ] **Step 5: Guard conversation opening synchronously**

Replace `openConversation`'s selection flow with:

```ts
async function openConversation(nextThreadId: string) {
  const generation = selectionGenerationRef.current + 1;
  selectionGenerationRef.current = generation;
  pollGenerationRef.current += 1;
  selectedThreadIdRef.current = nextThreadId;
  setThreadId(nextThreadId);
  setState(null);
  setPendingUserMessage(null);
  setSubmittedClarifications({});
  setError(null);
  setRunFailureMessage(null);
  setIsLoadingConversation(true);

  try {
    const nextState = await apiClient.getThreadState(nextThreadId);
    if (!applyOwnedThreadState(nextThreadId, generation, nextState)) return;
    const opened = await apiClient.markConversationOpened(nextThreadId);
    if (
      selectionGenerationRef.current !== generation ||
      selectedThreadIdRef.current !== nextThreadId
    ) return;
    setSavedConversations((current) =>
      current.map((item) => item.thread_id === nextThreadId ? opened : item),
    );
    void refreshSavedConversations();
  } catch (openError) {
    if (
      selectionGenerationRef.current === generation &&
      selectedThreadIdRef.current === nextThreadId
    ) {
      setState(null);
      setIsLoadingConversation(false);
      setError(errorMessage(openError));
    }
  }
}
```

Ensure `newConversation()` also increments `selectionGenerationRef`, clears `selectedThreadIdRef`, and clears loading.

When `ensureThread()` creates the first thread for a new conversation, establish ownership before returning it:

```ts
const nextThreadId = await createThreadPromiseRef.current;
if (!selectedThreadIdRef.current) {
  selectionGenerationRef.current += 1;
  selectedThreadIdRef.current = nextThreadId;
  setThreadId(nextThreadId);
}
return nextThreadId;
```

After `submitMessage` awaits `ensureThread()`, capture `const generation = selectionGenerationRef.current` and use that generation for the submit response. This ensures a newly created thread has the same protection as a reopened saved thread.

- [ ] **Step 6: Route poll, submit, cancel, resume, and conflict state through the guard**

At operation start, capture:

```ts
const ownerThreadId = activeThreadId;
const generation = selectionGenerationRef.current;
```

Replace each asynchronous `applyThreadState(nextState)` with:

```ts
applyOwnedThreadState(ownerThreadId, generation, nextState);
```

Errors also require the same current-owner check before calling `setError`. Change `handleRequestError` to accept `generation` and ignore both refreshed state and error text when ownership is stale:

```ts
async function handleRequestError(
  requestError: unknown,
  ownerThreadId: string,
  generation: number,
) {
  if (requestError instanceof ApiError && requestError.status === 409) {
    try {
      const refreshed = await apiClient.getThreadState(ownerThreadId);
      applyOwnedThreadState(ownerThreadId, generation, refreshed);
    } catch {
      // Preserve the original current-owner conflict below.
    }
  }
  if (
    selectionGenerationRef.current === generation &&
    selectedThreadIdRef.current === ownerThreadId
  ) {
    setError(errorMessage(requestError));
  }
}
```

- [ ] **Step 7: Bind all review actions to immutable ownership**

Change the handler signature:

```ts
async function resumeActiveInterrupt(
  ownerThreadId: string,
  ownerInterruptId: string,
  payload: ResumeInterruptPayload,
) {
  const generation = selectionGenerationRef.current;
  if (
    selectedThreadIdRef.current !== ownerThreadId ||
    state?.thread_id !== ownerThreadId ||
    state.active_interrupt?.id !== ownerInterruptId
  ) return;
  // Submit using ownerThreadId and ownerInterruptId, then apply with the guard.
}
```

In `renderActiveInterrupt`, create one bound callback for every review component:

```ts
const onResume = (payload: ResumeInterruptPayload) =>
  resumeActiveInterrupt(threadId, interrupt.id, payload);
```

Pass `onResume`/`onDecision` to dataset-plan review, dataset review, analysis-result review, agent clarification, and model-output-limit review. Disable all of them while `isLoadingConversation` is true.

- [ ] **Step 8: Render loading and restored-review context**

Include loading in busy/action-disabled calculations and render:

```tsx
{isLoadingConversation ? (
  <section className="conversation-loading" role="status">
    Loading selected conversation…
  </section>
) : null}
```

Before the active review component, render only for a saved reopened conversation:

```tsx
{activeInterrupt && !isLoadingConversation ? (
  <p className="restored-review-notice">
    This conversation was previously paused and is awaiting your review.
  </p>
) : null}
```

Track whether the selected thread was opened from history so a newly produced review in the current live run does not receive misleading “previously paused” copy.

- [ ] **Step 9: Run App and full frontend tests**

Run:

```bash
npm --prefix frontend test -- App.test.tsx
npm --prefix frontend test
```

Expected: PASS. Deferred A responses never replace B, mismatched thread IDs fail closed, and existing review/cancellation tests remain green.

- [ ] **Step 10: Commit the isolation boundary**

```bash
git add frontend/src/App.tsx frontend/src/App.test.tsx frontend/src/styles.css
git commit -m "fix: isolate conversation thread snapshots"
```

---

### Task 4: Prove all review types remain attached to their owning thread

**Files:**
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: guarded `resumeActiveInterrupt(ownerThreadId, ownerInterruptId, payload)` from Task 3.
- Produces: parameterized regression coverage for every public `ActiveInterrupt` variant.

- [ ] **Step 1: Add a parameterized review-ownership test**

Create valid minimal fixtures for each existing interrupt type using the builders already present in `App.test.tsx`, then test this table:

```ts
it.each([
  ["dataset_plan_review", datasetPlanReviewState()],
  ["dataset_review", datasetReviewState()],
  ["analysis_result_review", analysisReviewState()],
  ["agent_clarification", clarificationReviewState()],
  ["model_output_limit", modelOutputLimitReviewState()],
])("keeps %s actions bound to the owning conversation", async (_type, ownerState) => {
  // Open Thread A with ownerState, begin its review request, switch to Thread B,
  // resolve A's request, and assert Thread B remains the selected/rendered state.
});
```

For review components whose primary action text differs, include the action button name in the table. Assert the captured request URL always contains Thread A and its interrupt ID, never Thread B.

- [ ] **Step 2: Run the parameterized review tests**

Run:

```bash
npm --prefix frontend test -- App.test.tsx -t 'actions bound to the owning conversation'
```

Expected: PASS for all five public review types.

- [ ] **Step 3: Commit review-type coverage**

```bash
git add frontend/src/App.test.tsx
git commit -m "test: cover review ownership across threads"
```

---

### Task 5: Add the real browser regression smoke and rebuild delivery artifacts

**Files:**
- Create: `scripts/e2e_conversation_thread_isolation_real.py`
- Modify: `frontend/dist/**`
- Modify: `frontend/build-manifest.json`

**Interfaces:**
- Consumes: compiled frontend, FastAPI conversation/state endpoints, browser history controls, and durable review checkpoints.
- Produces: a standalone executable smoke with preserved JSON, page text, screenshots, and logs on failure.

- [ ] **Step 1: Implement the dedicated smoke harness**

Copy the process, CLI parser, deadline, artifact-directory, server lifecycle, and diagnostic helpers from `scripts/e2e_conversation_history_native_real.py`, then replace its browser scenario with the following concrete assertions and browser operations:

```python
THREAD_A_MESSAGE = "Create a baseline cohort and pause for dataset plan review."
THREAD_B_MESSAGE = "Who are you? Answer in one sentence."

def _assert_selected_thread(
    page: Page,
    *,
    present: str,
    absent: str,
    deadline: float,
) -> None:
    page.get_by_text(present, exact=True).wait_for(
        timeout=_remaining_ms(deadline),
    )
    if page.get_by_text(absent, exact=True).count():
        raise AssertionError(
            f"Stale conversation content {absent!r} rendered beside {present!r}."
        )

def _thread_state(api_url: str, thread_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state",
        timeout=5,
    )
    response.raise_for_status()
    state = response.json()
    if state["thread_id"] != thread_id:
        raise AssertionError(
            f"Requested {thread_id!r}, received {state['thread_id']!r}."
        )
    return state
```

Inside `run`, submit `THREAD_A_MESSAGE`, wait until raw Thread A state contains a non-null `active_interrupt`, click **New conversation**, submit `THREAD_B_MESSAGE`, and wait until Thread B stops running. Locate history buttons by their distinct generated/manual titles, switch B to A to B, and call `_assert_selected_thread` after each switch. Assert exactly one `Awaiting review` label is rendered and its closest history item contains Thread A's title. Finally fetch both states with `_thread_state` and assert:

```python
assert thread_a_state["active_interrupt"] is not None
assert thread_b_state["active_interrupt"] is None
assert THREAD_A_MESSAGE in {
    item["text"] for item in thread_a_state["conversation"]
}
assert THREAD_A_MESSAGE not in {
    item["text"] for item in thread_b_state["conversation"]
}
assert THREAD_B_MESSAGE in {
    item["text"] for item in thread_b_state["conversation"]
}
```

On failure, preserve:

```text
failure-traceback.txt
failure-page.txt
failure-screenshot.png
failure-conversations.json
failure-thread-a.json
failure-thread-b.json
api.log
```

Use a five-minute default deadline and do not automatically rerun the smoke.

- [ ] **Step 2: Make the smoke executable and syntax-check it**

Run:

```bash
chmod +x scripts/e2e_conversation_thread_isolation_real.py
.venv/bin/python -m py_compile scripts/e2e_conversation_thread_isolation_real.py
```

Expected: exit 0.

- [ ] **Step 3: Run focused and broad automated verification before the real smoke**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_api_runtime.py \
  tests/test_api_server.py -q
npm --prefix frontend test
```

Expected: PASS.

- [ ] **Step 4: Build the tracked production frontend and refresh its manifest**

Run:

```bash
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
```

Expected: TypeScript and Vite build succeed; build-manifest verification exits 0.

- [ ] **Step 5: Run the dedicated real smoke exactly once**

Run:

```bash
.venv/bin/python scripts/e2e_conversation_thread_isolation_real.py \
  --timeout-seconds 300
```

Expected: exit 0 with distinct raw thread states, no cross-thread DOM content, and Thread A's review intact after returning. If it fails or times out, stop and report the preserved diagnostics without rerunning.

- [ ] **Step 6: Run final delivery verification**

Run:

```bash
.venv/bin/python scripts/verify_working_demo_delivery.py
git diff --check
git status --short
```

Expected: delivery verification and diff check exit 0; status contains only intentional source, test, smoke, bundle, and manifest changes.

- [ ] **Step 7: Commit the smoke and production bundle**

```bash
git add scripts/e2e_conversation_thread_isolation_real.py \
  frontend/dist frontend/build-manifest.json
git commit -m "test: smoke conversation thread isolation"
```

---

## Final Acceptance Checklist

- [ ] A persisted question exists and renders in only its owning thread.
- [ ] Rapid thread switching cannot install an older response.
- [ ] Loading and failure states never leave another thread's content visible.
- [ ] Poll, submit, resume, cancel, and conflict-recovery responses are guarded.
- [ ] All five public review types capture immutable thread and interrupt ownership.
- [ ] Switching away has no workflow side effect; returning restores the review.
- [ ] Conversation history labels only the owning waiting thread.
- [ ] Existing review cancellation remains the sole explicit discard mechanism.
- [ ] Focused backend tests, full frontend tests, production build, delivery verification, and the one-shot real smoke pass.
