# Concurrent Conversation Title Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return a first-message HTTP response without waiting for automatic conversation-title generation, then refresh the sidebar until the generated title arrives.

**Architecture:** Schedule the existing title-generation method on a two-worker `ThreadPoolExecutor` only when the conversation history row is first created. In React, start one serialized one-second refresh loop after a successful message submission and cancel it when the title changes, the active conversation changes, or two minutes elapse. Keep the API schema, persistence schema, LangGraph execution, and model configuration unchanged.

**Tech Stack:** Python 3.12, `concurrent.futures`, FastAPI runtime, SQLite conversation history, pytest, React 19, TypeScript, Vitest fake timers, Playwright, compiled Vite frontend.

## Global Constraints

- Keep this deliberately small: one bounded backend executor and one bounded frontend refresh loop.
- Title generation remains a first-message-only operation.
- A title-model failure must not delay or fail the agent response.
- A late automatic title must not overwrite a manual rename.
- Refresh saved conversations once per second and stop after success, conversation replacement, or 120 seconds.
- Do not change the API response schema, database schema, LangGraph execution, prompts, tools, or model configuration.
- Do not introduce token streaming, SSE, `astream`, workflow-progress events, title fallbacks, or broader runtime concurrency changes.
- Add a dedicated executable real smoke under `scripts/`; it must use the native FastAPI launcher and compiled TypeScript UI, run at most once, and retain diagnostics on failure.
- Rebuild tracked `frontend/dist` and refresh `frontend/dist/build-manifest.json` after changing frontend source.
- Preserve unrelated user changes and mutable runtime data.

## File Map

- `api/runtime.py`: own the bounded title executor and schedule the existing failure-isolated title operation.
- `tests/test_api_runtime.py`: prove non-blocking submission, eventual persistence, failure isolation, first-message-only scheduling, and manual-rename protection.
- `frontend/src/App.tsx`: own the bounded, serialized saved-conversation refresh loop.
- `frontend/src/App.test.tsx`: prove eventual title replacement and refresh cancellation with fake timers.
- `scripts/e2e_working_demo_native_real.py`: expose a focused concurrent-title acceptance boundary through the existing production browser harness.
- `scripts/smoke_concurrent_title_generation_real.py`: provide the dedicated feature-named executable without duplicating launcher or cleanup code.
- `tests/test_e2e_working_demo_native_real.py`: cheaply verify the focused smoke wiring and production boundaries.
- `frontend/dist/**`: regenerated production UI and build manifest.

---

### Task 1: Non-blocking Runtime Title Generation

**Files:**
- Modify: `tests/test_api_runtime.py:637-659`
- Modify: `api/runtime.py:1-15,597-630,1037-1055`

**Interfaces:**
- Consumes: `ConversationHistoryStore.get/create/set_automatic_title`, `OpenAIConversationTitleGenerator.generate(first_message) -> str`.
- Produces: private `ReportAgentApiRuntime._title_executor: ThreadPoolExecutor`; `submit_message(...)` schedules `_generate_title(thread_id, text)` only when no history row existed before this submit.

- [ ] **Step 1: Replace the synchronous-title test with failing concurrency tests**

Replace `test_runtime_generates_first_conversation_title_before_submit_returns` and add the adjacent cases below. The recording store event makes the manual-rename race deterministic without exposing executor internals.

```python
def _wait_for_history_title(
    history_store: ConversationHistoryStore,
    thread_id: str,
    expected: str,
) -> None:
    deadline = time.time() + 2
    while time.time() < deadline:
        record = history_store.get(thread_id)
        if record is not None and record.title == expected:
            return
        time.sleep(0.01)
    record = history_store.get(thread_id)
    assert record is not None
    assert record.title == expected


def test_runtime_generates_first_title_without_blocking_submit(tmp_path: Path) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    title_started = threading.Event()
    release_title = threading.Event()
    submit_finished = threading.Event()
    submit_errors: list[BaseException] = []

    class _BlockingTitleGenerator:
        def generate(self, _text: str) -> str:
            title_started.set()
            assert release_title.wait(timeout=2)
            return "Connection test"

    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        history_store=history_store,
        title_generator=_BlockingTitleGenerator(),
    )
    thread_id = runtime.create_thread()

    def submit() -> None:
        try:
            runtime.submit_message(thread_id, "Test the connection")
        except BaseException as exc:
            submit_errors.append(exc)
        finally:
            submit_finished.set()

    worker = threading.Thread(target=submit)
    worker.start()
    try:
        assert title_started.wait(timeout=1)
        assert submit_finished.wait(timeout=1), "submit_message waited for the title"
        record = history_store.get(thread_id)
        assert record is not None
        assert record.title == "Untitled conversation"
    finally:
        release_title.set()
        worker.join(timeout=2)

    assert submit_errors == []
    _wait_for_history_title(history_store, thread_id, "Connection test")


def test_runtime_title_failure_is_isolated_and_not_retried_on_followup(
    tmp_path: Path,
) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    title_attempted = threading.Event()
    calls: list[str] = []

    class _FailingTitleGenerator:
        def generate(self, text: str) -> str:
            calls.append(text)
            title_attempted.set()
            raise RuntimeError("title provider unavailable")

    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        history_store=history_store,
        title_generator=_FailingTitleGenerator(),
    )
    thread_id = runtime.create_thread()

    runtime.submit_message(thread_id, "First message")
    assert title_attempted.wait(timeout=1)
    deadline = time.time() + 2
    while runtime.state(thread_id).run.state == "running" and time.time() < deadline:
        time.sleep(0.01)
    runtime.submit_message(thread_id, "Follow-up message")
    time.sleep(0.05)

    record = history_store.get(thread_id)
    assert record is not None
    assert record.title == "Untitled conversation"
    assert calls == ["First message"]


def test_runtime_late_automatic_title_preserves_manual_rename(tmp_path: Path) -> None:
    automatic_title_attempted = threading.Event()

    class _RecordingHistoryStore(ConversationHistoryStore):
        def set_automatic_title(self, thread_id: str, title: str):
            try:
                return super().set_automatic_title(thread_id, title)
            finally:
                automatic_title_attempted.set()

    history_store = _RecordingHistoryStore(tmp_path / "history.db")
    title_started = threading.Event()
    release_title = threading.Event()

    class _BlockingTitleGenerator:
        def generate(self, _text: str) -> str:
            title_started.set()
            assert release_title.wait(timeout=2)
            return "Late automatic title"

    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        history_store=history_store,
        title_generator=_BlockingTitleGenerator(),
    )
    thread_id = runtime.create_thread()

    runtime.submit_message(thread_id, "Analyze cohort retention")
    assert title_started.wait(timeout=1)
    runtime.rename_conversation(thread_id, "My manual title")
    release_title.set()
    assert automatic_title_attempted.wait(timeout=1)

    record = history_store.get(thread_id)
    assert record is not None
    assert record.title == "My manual title"
    assert record.title_source == "manual"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py \
  -k 'title_without_blocking or title_failure_is_isolated or late_automatic_title' -q
```

Expected: the blocking test fails at `submit_finished.wait(timeout=1)` because synchronous title generation prevents `submit_message` from returning. The follow-up test records two title calls while the first title remains untitled.

- [ ] **Step 3: Add the minimal bounded executor and first-message guard**

Add the import and dataclass field in `api/runtime.py`:

```python
from concurrent.futures import ThreadPoolExecutor


@dataclass
class ReportAgentApiRuntime:
    _title_executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="conversation-title",
        ),
        init=False,
        repr=False,
    )
```

Replace only the history block in `submit_message`; retain `_generate_title` unchanged because it already contains failure isolation and the store already protects manual titles.

```python
        if self.history_store is not None:
            existing_record = self.history_store.get(thread_id)
            record = self.history_store.create(
                thread_id,
                model_name=thread.settings.model_name,
            )
            if (
                existing_record is None
                and text.strip()
                and record.title == "Untitled conversation"
                and self.title_generator is not None
            ):
                self._title_executor.submit(self._generate_title, thread_id, text)
        thread.locked = True
```

- [ ] **Step 4: Run focused and runtime regression tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py \
  -k 'title or conversation or concurrent_first_submit' -q
```

Expected: all selected tests pass; the submit completion event is set while the title generator remains blocked.

- [ ] **Step 5: Commit the backend change**

The repository ignores `tests/` by policy. Keep the focused tests locally for verification and commit only the tracked production file:

```bash
git add api/runtime.py
git commit -m "perf: generate conversation titles asynchronously"
```

---

### Task 2: Bounded Sidebar Title Refresh

**Files:**
- Modify: `frontend/src/App.test.tsx:1020-1135`
- Modify: `frontend/src/App.tsx:35-36,143-160,198-205,255-266,479-491`

**Interfaces:**
- Consumes: existing `apiClient.listConversations() -> Promise<{items: ConversationSummary[]}>`, active `threadId`, and `loadConversationHistory`.
- Produces: `refreshSavedConversations() -> Promise<ConversationSummary[] | null>` and a private `titlePollingThreadId` state that owns one serialized refresh loop.

- [ ] **Step 1: Add failing fake-timer coverage for eventual replacement and cancellation**

Add these tests near the existing saved-conversation refresh tests in `frontend/src/App.test.tsx`:

```tsx
it("refreshes an untitled conversation until its generated title arrives", async () => {
  vi.useFakeTimers();
  let historyRequests = 0;
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "http://api.test/api/runtime/options") {
      return Promise.resolve(runtimeOptionsResponse());
    }
    if (url === "http://api.test/api/conversations") {
      historyRequests += 1;
      const title = historyRequests >= 4
        ? "Concurrent response check"
        : "Untitled conversation";
      return Promise.resolve(jsonResponse({
        items: historyRequests === 1 ? [] : [{
          thread_id: "thread-1",
          title,
          title_source: "automatic",
          model_name: "gpt-5.4",
          created_at: "2026-08-05T00:00:00+00:00",
          updated_at: "2026-08-05T00:00:00+00:00",
        }],
      }));
    }
    if (url === "http://api.test/api/threads") {
      return Promise.resolve(createThreadResponse());
    }
    if (url === "http://api.test/api/threads/thread-1/messages") {
      return Promise.resolve(jsonResponse(threadState({
        run: { state: "done", steps: 1, error: null, error_code: null,
          user_message: null, started_at: null, updated_at: null },
        conversation: [
          { id: "user-1", role: "user", text: "Check concurrency" },
          { id: "assistant-1", role: "assistant", text: "Agent response ready" },
        ],
      })));
    }
    return Promise.reject(new Error(`Unexpected request: ${url}`));
  });

  render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
  await waitFor(() => expect(historyRequests).toBe(1));
  fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
    target: { value: "Check concurrency" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  expect(await screen.findByText("Agent response ready")).toBeInTheDocument();
  expect(
    await screen.findByRole("button", { name: "Untitled conversation" }),
  ).toBeInTheDocument();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
    expect(historyRequests).toBe(3);
    expect(
      screen.getByRole("button", { name: "Untitled conversation" }),
    ).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(
      screen.getByRole("button", { name: "Concurrent response check" }),
    ).toBeInTheDocument();
  const stoppedAt = historyRequests;

  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(historyRequests).toBe(stoppedAt);
});


it("stops title refresh when the active conversation is replaced", async () => {
  vi.useFakeTimers();
  let historyRequests = 0;
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "http://api.test/api/runtime/options") {
      return Promise.resolve(runtimeOptionsResponse());
    }
    if (url === "http://api.test/api/conversations") {
      historyRequests += 1;
      return Promise.resolve(jsonResponse({
        items: historyRequests === 1 ? [] : [{
          thread_id: "thread-1",
          title: "Untitled conversation",
          title_source: "automatic",
          model_name: "gpt-5.4",
          created_at: "2026-08-05T00:00:00+00:00",
          updated_at: "2026-08-05T00:00:00+00:00",
        }],
      }));
    }
    if (url === "http://api.test/api/threads") {
      return Promise.resolve(createThreadResponse());
    }
    if (url === "http://api.test/api/threads/thread-1/messages") {
      return Promise.resolve(jsonResponse(threadState({
        run: { state: "done", steps: 1, error: null, error_code: null,
          user_message: null, started_at: null, updated_at: null },
      })));
    }
    return Promise.reject(new Error(`Unexpected request: ${url}`));
  });

  render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
  await waitFor(() => expect(historyRequests).toBe(1));
  fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
    target: { value: "Check cancellation" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByRole("button", { name: "Untitled conversation" });
  fireEvent.click(screen.getByRole("button", {
    name: "Start new conversation from saved conversations",
  }));
  const stoppedAt = historyRequests;

  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(historyRequests).toBe(stoppedAt);
});
```

- [ ] **Step 2: Run the two component tests and verify RED**

Run:

```bash
npm --prefix frontend test -- App.test.tsx \
  -t 'generated title arrives|active conversation is replaced'
```

Expected: FAIL because the existing submit path performs only one delayed refresh and has no conversation-change cancellation.

- [ ] **Step 3: Implement one serialized, bounded refresh loop**

Add constants beside `POLL_INTERVAL_MS`:

```tsx
const POLL_INTERVAL_MS = 1000;
const TITLE_POLL_TIMEOUT_MS = 120_000;
const UNTITLED_CONVERSATION = "Untitled conversation";
```

Add state beside `savedConversations`:

```tsx
const [titlePollingThreadId, setTitlePollingThreadId] = useState<string | null>(null);
```

Make the existing refresh return only the response it actually applied:

```tsx
async function refreshSavedConversations(): Promise<ConversationSummary[] | null> {
  const requestId = savedConversationsRequestRef.current + 1;
  savedConversationsRequestRef.current = requestId;
  try {
    const response = await apiClient.listConversations();
    if (requestId !== savedConversationsRequestRef.current) {
      return null;
    }
    const items = response.items ?? [];
    setSavedConversations(items);
    return items;
  } catch {
    // A history refresh must not block the active analysis workflow.
    return null;
  }
}
```

Add one effect after the initial conversation-history effect:

```tsx
useEffect(() => {
  if (
    !loadConversationHistory ||
    !titlePollingThreadId ||
    titlePollingThreadId !== threadId
  ) {
    return;
  }

  let cancelled = false;
  let timeoutId: number | undefined;
  const deadline = Date.now() + TITLE_POLL_TIMEOUT_MS;

  async function pollOnce() {
    const items = await refreshSavedConversations();
    if (cancelled) {
      return;
    }
    const activeConversation = items?.find(
      (item) => item.thread_id === titlePollingThreadId,
    );
    if (
      activeConversation &&
      activeConversation.title !== UNTITLED_CONVERSATION
    ) {
      setTitlePollingThreadId((current) =>
        current === titlePollingThreadId ? null : current,
      );
      return;
    }
    if (Date.now() < deadline) {
      timeoutId = window.setTimeout(pollOnce, POLL_INTERVAL_MS);
    }
  }

  void pollOnce();
  return () => {
    cancelled = true;
    if (timeoutId !== undefined) {
      window.clearTimeout(timeoutId);
    }
  };
}, [apiClient, loadConversationHistory, threadId, titlePollingThreadId]);
```

Replace the two ad-hoc refresh calls after `submitMessage` with one trigger:

```tsx
if (loadConversationHistory) {
  setTitlePollingThreadId(activeThreadId);
}
```

- [ ] **Step 4: Run focused and complete frontend tests and verify GREEN**

Run:

```bash
npm --prefix frontend test -- App.test.tsx
npm --prefix frontend test
```

Expected: all `App` tests and the complete Vitest suite pass; fake timers show no refreshes after title success or conversation replacement.

- [ ] **Step 5: Commit the frontend source and tests**

The repository ignores `tests/` by policy. Keep the focused component tests locally for verification and commit only the tracked frontend source:

```bash
git add frontend/src/App.tsx
git commit -m "feat: refresh sidebar until title arrives"
```

---

### Task 3: Dedicated Real Concurrent-Title Smoke

**Files:**
- Modify: `tests/test_e2e_working_demo_native_real.py:180-260`
- Modify: `scripts/e2e_working_demo_native_real.py:85-100,604-730,907-1026`
- Create: `scripts/smoke_concurrent_title_generation_real.py`

**Interfaces:**
- Consumes: native `run_fastapi.py` launcher, compiled frontend, real OpenAI configuration, Playwright page, `/api/conversations`, and `/api/threads/{thread_id}/state`.
- Produces: `args.stop_after_concurrent_title: bool`, browser outcome `concurrent_title`, artifacts `concurrent-title-state.json` and `concurrent-title-screenshot.png`, and pass text `PASS concurrent title generation feature smoke`.

- [ ] **Step 1: Add failing cheap smoke-wiring tests**

Add to `tests/test_e2e_working_demo_native_real.py`:

```python
def test_concurrent_title_smoke_uses_native_compiled_browser_boundary() -> None:
    from scripts import smoke_concurrent_title_generation_real as focused_smoke

    args = focused_smoke.parse_args([])
    helper = getattr(native_smoke, "_assert_concurrent_title_generation", None)

    assert args.stop_after_concurrent_title is True
    assert args.timeout_seconds == 300
    assert focused_smoke.PASS_MESSAGE == (
        "PASS concurrent title generation feature smoke"
    )
    assert callable(helper)
    source = inspect.getsource(helper)
    assert "/api/conversations" in source
    assert '"Untitled conversation"' in source
    assert "concurrent-title-state.json" in source
    assert "concurrent-title-screenshot.png" in source


def test_concurrent_title_smoke_executes_outside_repo(tmp_path: Path) -> None:
    script = native_smoke.REPO_ROOT / "scripts" / (
        "smoke_concurrent_title_generation_real.py"
    )

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--stop-after-concurrent-title" in completed.stdout


def test_concurrent_title_pass_requires_focused_browser_outcome() -> None:
    args = parse_args(["--stop-after-concurrent-title"])

    with pytest.raises(AssertionError, match="concurrent-title"):
        native_smoke._pass_message(args, "broad")
    assert native_smoke._pass_message(args, "concurrent_title") == (
        "PASS concurrent title generation feature smoke"
    )
```

- [ ] **Step 2: Run the smoke-wiring tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_e2e_working_demo_native_real.py \
  -k concurrent_title -q
```

Expected: FAIL because the flag, helper, focused executable, and pass boundary do not exist.

- [ ] **Step 3: Add the focused boundary to the shared native harness**

Add this constant with the existing focused pass constant:

```python
CONCURRENT_TITLE_PASS = "PASS concurrent title generation feature smoke"
```

Add a helper before `_run_browser_flow`. It submits through the compiled UI, requires the POST response to report accepted running work, waits independently for a non-placeholder title and terminal assistant content through the real APIs, then confirms both are visible in the browser.

```python
def _assert_concurrent_title_generation(
    page: Any,
    *,
    api_url: str,
    artifact_dir: Path,
    deadline: float,
) -> dict[str, Any]:
    query = "Reply briefly that the concurrent response check is complete."
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/messages")
        ),
        timeout=_remaining_ms(deadline),
    ) as submitted:
        page.get_by_label(MESSAGE_LABEL).fill(
            query,
            timeout=_remaining_ms(deadline),
        )
        page.get_by_role("button", name="Send").click(
            timeout=_remaining_ms(deadline),
        )

    submit_response = submitted.value
    if not submit_response.ok:
        raise AssertionError(
            f"Message submission failed with HTTP {submit_response.status}."
        )
    submitted_state = submit_response.json()
    thread_id = submitted_state.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise AssertionError(f"Message response omitted thread_id: {submitted_state!r}")
    if (submitted_state.get("run") or {}).get("state") != "running":
        raise AssertionError(
            f"Message was not accepted as running work: {submitted_state!r}"
        )
    page.get_by_label("Agent activity").wait_for(
        timeout=min(10_000, _remaining_ms(deadline)),
    )

    generated_title: str | None = None
    terminal_state: dict[str, Any] | None = None
    assistant_text = ""
    while time.monotonic() < deadline:
        conversations_response = requests.get(
            f"{api_url}/api/conversations",
            timeout=max(1.0, min(10.0, _remaining_seconds(deadline))),
        )
        conversations_response.raise_for_status()
        for item in conversations_response.json().get("items", []):
            if (
                item.get("thread_id") == thread_id
                and item.get("title") != "Untitled conversation"
            ):
                generated_title = str(item["title"])

        state_response = requests.get(
            f"{api_url}/api/threads/{thread_id}/state",
            timeout=max(1.0, min(10.0, _remaining_seconds(deadline))),
        )
        state_response.raise_for_status()
        candidate_state = state_response.json()
        assistant_messages = [
            str(message.get("text") or "").strip()
            for message in candidate_state.get("conversation", [])
            if message.get("role") == "assistant"
            and str(message.get("text") or "").strip()
        ]
        if (
            (candidate_state.get("run") or {}).get("state") != "running"
            and assistant_messages
        ):
            terminal_state = candidate_state
            assistant_text = assistant_messages[-1]
        if generated_title and terminal_state is not None:
            break
        time.sleep(0.25)

    if generated_title is None:
        raise AssertionError("Automatic conversation title did not arrive.")
    if terminal_state is None:
        raise AssertionError("Agent response did not reach a visible terminal result.")

    page.get_by_role("button", name=generated_title, exact=True).wait_for(
        timeout=_remaining_ms(deadline),
    )
    page.get_by_text(assistant_text, exact=False).first.wait_for(
        timeout=_remaining_ms(deadline),
    )
    artifact = {
        "submitted": submitted_state,
        "terminal": terminal_state,
        "generated_title": generated_title,
    }
    (artifact_dir / "concurrent-title-state.json").write_text(
        json.dumps(artifact, indent=2),
        encoding="utf-8",
    )
    page.screenshot(
        path=str(artifact_dir / "concurrent-title-screenshot.png"),
        full_page=True,
    )
    return artifact
```

Immediately after the initial page readiness checks in `_run_browser_flow`, add:

```python
if args.stop_after_concurrent_title:
    _assert_concurrent_title_generation(
        page,
        api_url=api_url,
        artifact_dir=artifact_dir,
        deadline=deadline,
    )
    return "concurrent_title"
```

Extend `_pass_message` and `parse_args` without changing the broad workflow:

```python
def _pass_message(args: argparse.Namespace, browser_outcome: str) -> str:
    if args.stop_after_concurrent_title:
        if browser_outcome != "concurrent_title":
            raise AssertionError(
                "Focused concurrent-title smoke did not complete its acceptance boundary."
            )
        return CONCURRENT_TITLE_PASS
    if args.stop_after_review_code_copy:
        if browser_outcome != "analysis_review_code_copy":
            raise AssertionError(
                "Focused smoke did not complete the analysis-review "
                "Code/Output acceptance boundary."
            )
        return ANALYSIS_REVIEW_CODE_COPY_PASS
    return "PASS native working-demo browser smoke"
```

```python
parser.add_argument(
    "--stop-after-concurrent-title",
    action="store_true",
    help="Stop after the concurrent conversation-title acceptance boundary.",
)
```

Reject combining focused modes after parsing:

```python
focused_modes = sum(
    bool(value)
    for value in (
        args.stop_after_concurrent_title,
        args.stop_after_review_code_copy,
        args.cancel_first_interrupt,
    )
)
if focused_modes > 1:
    parser.error(
        "--stop-after-concurrent-title, --stop-after-review-code-copy, and "
        "--cancel-first-interrupt are mutually exclusive"
    )
```

- [ ] **Step 4: Add the thin dedicated executable**

Create `scripts/smoke_concurrent_title_generation_real.py`:

```python
#!/usr/bin/env python3
"""Run the dedicated real concurrent-title browser smoke."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e_working_demo_native_real import (
    CONCURRENT_TITLE_PASS as PASS_MESSAGE,
)
from scripts.e2e_working_demo_native_real import parse_args as parse_shared_args
from scripts.e2e_working_demo_native_real import run


def parse_args(argv: list[str] | None = None):
    values = list(sys.argv[1:] if argv is None else argv)
    return parse_shared_args(["--stop-after-concurrent-title", *values])


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
```

- [ ] **Step 5: Run the cheap harness tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_e2e_working_demo_native_real.py \
  -k 'concurrent_title or server_command_uses_native_launcher_only' -q
```

Expected: all selected tests pass, and `--help` works from outside the repository.

- [ ] **Step 6: Commit the dedicated smoke**

The repository ignores `scripts/` and `tests/` by policy. Keep the dedicated smoke and its wiring tests locally; do not force-add the existing ignored harness files or create a large tracking exception for this feature.

---

### Task 4: Production Build and Final Verification

**Files:**
- Regenerate: `frontend/dist/index.html`
- Regenerate: `frontend/dist/assets/*`
- Regenerate: `frontend/dist/build-manifest.json`

**Interfaces:**
- Consumes: completed backend, frontend, smoke implementation, and tracked frontend source.
- Produces: compiled UI served by `run_fastapi.py`, matching build manifest, and one real end-to-end evidence run.

- [ ] **Step 1: Run all focused automated verification**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py \
  tests/test_e2e_working_demo_native_real.py -q
npm --prefix frontend test
```

Expected: all selected pytest tests and the complete Vitest suite pass.

- [ ] **Step 2: Build the production frontend**

Run:

```bash
npm --prefix frontend run build
```

Expected: TypeScript compilation and Vite production build succeed, updating only generated files under `frontend/dist`.

- [ ] **Step 3: Refresh and verify the tracked build manifest**

Run:

```bash
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
.venv/bin/python scripts/verify_working_demo_delivery.py
```

Expected: the build manifest matches the current tracked frontend inputs and compiled assets. Report any unrelated missing local study bundle separately rather than changing runtime data.

- [ ] **Step 4: Run the dedicated real smoke exactly once**

Run:

```bash
.venv/bin/python scripts/smoke_concurrent_title_generation_real.py \
  --timeout-seconds 300
```

Expected: stdout contains `PASS concurrent title generation feature smoke`; artifacts include `concurrent-title-state.json`, `concurrent-title-screenshot.png`, and `api.log`. If it fails or times out, do not rerun automatically; preserve and report its diagnostic directory.

- [ ] **Step 5: Inspect generated changes and commit the production bundle**

Run:

```bash
git status --short
git diff --check
git diff --stat
```

Expected: only intended source/tests/smoke commits plus regenerated `frontend/dist` outputs are present; no runtime database, study data, secrets, or unrelated user files are staged.

Then commit the generated bundle:

```bash
git add -f frontend/dist
git commit -m "build: refresh concurrent title frontend bundle"
```

- [ ] **Step 6: Record final evidence**

Run:

```bash
git status --short
git log -4 --oneline
```

Expected: the working tree has no new task-related unstaged changes, and the log shows the backend, frontend, smoke, and production-bundle commits.
