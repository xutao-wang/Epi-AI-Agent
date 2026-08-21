# Active Run Cancellation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an immediate Cancel control that restores the current conversation to its latest completed or approved checkpoint, retains the cancelled user message and input attachments, and prevents unfinished model or tool work from entering future context.

**Architecture:** Extend the existing in-process `ApiGraphRunner` with one cooperative cancellation token and one restore callback per active thread. Each run remembers the checkpoint that preceded the turn; review resumes first persist their short decision step as the newer durable boundary, and cancellation forks a terminal checkpoint from that boundary with post-boundary model/tool messages removed and a visible cancelled-turn event added. Model/tool/Python boundaries observe the same token, while the API and React client expose only a thread-level Cancel operation because a thread already permits at most one active run.

**Tech Stack:** Python 3.12, FastAPI, LangGraph 1.0.3 checkpoint APIs, LangChain messages, SQLite checkpointer, React 19, TypeScript, Vitest, pytest.

## Global Constraints

- Keep the current single-process and single-Uvicorn-worker deployment model; do not add Redis, SQS, a worker service, or a new job database.
- Keep original user uploads in the existing attachment store and keep approved artifacts from the latest durable boundary.
- Remove model/tool messages and state created after the durable checkpoint while retaining history that belongs to an approved boundary.
- Persist the cancelled state before reporting `run.state="cancelled"` or unlocking the composer.
- Treat remote provider cancellation as best effort: a late response may complete and incur cost, but it must never be checkpointed.
- Keep cancellation-scoped tools read-only or staged. A tool's unreachable temporary files may be cleaned up after restoration, but irreversible external side effects require a separate compensation design and are not added here.
- Do not change Cancel actions inside existing clarification, dataset-review, analysis-review, or model-output-limit panels.
- Do not add a new authentication system in this feature. The cancel endpoint must live beside the existing thread endpoints and use the same identity boundary.
- Preserve the current meaning of New conversation: it opens a separate thread and never aliases cancellation.

---

## File Map

- Create `utils/run_cancellation.py`: process-local token, binding context, and `RunCancelled` control-flow exception.
- Create `tests/test_active_run_cancellation.py`: runner/checkpoint restoration and API-level cancellation regression coverage.
- Modify `api/runtime.py`: cancellable job records, durable checkpoint capture, cancelled-turn restoration, restart projection, and `cancel_run()`.
- Modify `api/schemas.py`: terminal `cancelled` run state and optional cancelled message status.
- Modify `api/server.py`: thread-level cancel endpoint.
- Modify `utils/display_history.py`: carry a user event's cancelled status into API projection.
- Modify `epi_agent/runtime.py`: cancellation points around model and tool operations and cancelled-turn state typing.
- Modify `epi_agent/agent.py`: bounded inactive context for retrying a cancelled request and its attachments.
- Modify `epi_agent/runtimes/python/local_process.py`: poll the child process so cancellation terminates its process group promptly.
- Modify `tests/test_epi_agent_runtime.py`: model/tool cancellation checks.
- Modify `tests/test_epi_python_tools.py`: Python child termination check.
- Modify `tests/test_display_history.py`: cancelled message projection with attachment events.
- Modify `frontend/src/types.ts`: cancelled run/message types.
- Modify `frontend/src/apiClient.ts` and `frontend/src/apiClient.test.ts`: typed cancel request.
- Modify `frontend/src/App.tsx` and `frontend/src/App.test.tsx`: Cancel/Cancelling behavior and poll-race protection.
- Modify `frontend/src/ConversationMessage.tsx` and `frontend/src/ConversationMessage.test.tsx`: visible Cancelled badge.
- Modify `frontend/src/styles.css`: bounded cancel button and status badge styles.
- Modify `README.md`: brief user-facing distinction between Cancel and New conversation.

---

### Task 1: Cooperative Cancellation Primitive and Safe Execution Boundaries

**Files:**
- Create: `utils/run_cancellation.py`
- Create: `tests/test_run_cancellation.py`
- Modify: `epi_agent/runtime.py`
- Modify: `epi_agent/runtimes/python/local_process.py`
- Modify: `tests/test_epi_agent_runtime.py`
- Modify: `tests/test_epi_python_tools.py`

**Interfaces:**
- Produces: `CancellationToken.cancel() -> None`, `CancellationToken.raise_if_cancelled() -> None`, `bind_cancellation(token)`, `cancellation_point() -> None`, and `RunCancelled`.
- Consumes: no application state; the token is process-local and is never placed in LangGraph config or checkpoints.

- [ ] **Step 1: Write failing primitive tests**

Create `tests/test_run_cancellation.py` with the exact behavior contract:

```python
import pytest

from utils.run_cancellation import (
    CancellationToken,
    RunCancelled,
    bind_cancellation,
    cancellation_point,
)


def test_cancellation_point_is_inert_without_a_bound_token() -> None:
    cancellation_point()


def test_bound_token_raises_only_after_cancel() -> None:
    token = CancellationToken()
    with bind_cancellation(token):
        cancellation_point()
        token.cancel()
        with pytest.raises(RunCancelled):
            cancellation_point()


def test_binding_is_restored_after_context_exit() -> None:
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RunCancelled):
        with bind_cancellation(token):
            cancellation_point()
    cancellation_point()
```

- [ ] **Step 2: Run the primitive tests and verify the missing module failure**

Run:

```bash
.venv/bin/pytest tests/test_run_cancellation.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'utils.run_cancellation'`.

- [ ] **Step 3: Implement the process-local token**

Create `utils/run_cancellation.py`:

```python
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
from collections.abc import Iterator


class RunCancelled(Exception):
    """Internal control flow: the active run must publish no more state."""


@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RunCancelled("The active run was cancelled.")


_ACTIVE_TOKEN: ContextVar[CancellationToken | None] = ContextVar(
    "report_agent_active_cancellation_token",
    default=None,
)


@contextmanager
def bind_cancellation(token: CancellationToken) -> Iterator[None]:
    reset = _ACTIVE_TOKEN.set(token)
    try:
        yield
    finally:
        _ACTIVE_TOKEN.reset(reset)


def cancellation_point() -> None:
    token = _ACTIVE_TOKEN.get()
    if token is not None:
        token.raise_if_cancelled()
```

- [ ] **Step 4: Run the primitive tests and verify they pass**

Run:

```bash
.venv/bin/pytest tests/test_run_cancellation.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Add failing model and tool boundary tests**

In `tests/test_epi_agent_runtime.py`, import `CancellationToken`, `RunCancelled`, and `bind_cancellation`. Add a blocking model whose `invoke()` cancels the token immediately before returning an `AIMessage`, then assert no patch is returned:

```python
class CancellingModel:
    def __init__(self, token: CancellationToken) -> None:
        self.token = token

    def bind_tools(self, _schemas):
        return self

    def invoke(self, _messages, **_kwargs):
        self.token.cancel()
        return AIMessage(content="late answer")


def test_model_result_returned_after_cancel_is_discarded() -> None:
    token = CancellationToken()
    graph = build_epi_agent_graph(
        state_schema=GenericEpiAgentState,
        config=_cancellation_runtime_config(ToolRegistry([])),
        model=CancellingModel(token),
    )
    with bind_cancellation(token), pytest.raises(RunCancelled):
        graph.invoke(
            _cancellation_initial_state("Start work"),
            {"configurable": {"thread_id": "cancel-model"}},
        )
```

Add a tool whose `invoke()` cancels the token before returning, call `_execute_tools`, and assert `RunCancelled` rather than a `ToolMessage` patch:

```python
def test_tool_result_returned_after_cancel_is_discarded() -> None:
    token = CancellationToken()
    registry = ToolRegistry([CancellingFunctionTool(token=token)])
    with bind_cancellation(token), pytest.raises(RunCancelled):
        _execute_tools(
            _cancellation_tool_state(),
            {"configurable": {"thread_id": "cancel-tool"}},
            agent_config=_cancellation_runtime_config(registry),
        )
```

Add these concrete helpers beside the existing `EmptyArguments` and `_studies()` fixtures:

```python
@dataclass(frozen=True)
class CancellingFunctionTool:
    token: CancellationToken
    spec: ToolSpec = field(
        default=ToolSpec(
            name="cancel_tool",
            description="Cancel before returning.",
            args_model=EmptyArguments,
        ),
        init=False,
    )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        self.token.cancel()
        return ToolResult(message="late tool result")


def _cancellation_runtime_config(registry: ToolRegistry) -> EpiAgentRuntimeConfig:
    studies = _studies()
    return EpiAgentRuntimeConfig(
        model_profile=model_runtime_profile("gpt-5.4"),
        agent_name="test_agent",
        system_prompt="Use tools.",
        registry=registry,
        studies=studies,
        context_factory=lambda _state, _config, artifact_store: ToolContext(
            study=studies.require("study-1"),
            artifact_store=artifact_store,
            thread_id="cancel-tool",
            policy=None,
        ),
    )


def _cancellation_tool_state() -> dict[str, Any]:
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "cancel_tool",
                    "args": {},
                    "id": "cancel-call-1",
                    "type": "tool_call",
                }],
            )
        ],
        "artifacts": {},
    }


def _cancellation_initial_state(text: str) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content=text)],
        "active_study_id": "study-1",
        "artifact_ids": [],
        "artifacts": {},
        "final_response": None,
        "iteration_count": 0,
        "failure_signatures": [],
        "current_turn_artifact_refs": [],
    }
```

- [ ] **Step 6: Run the boundary tests and verify late results currently pass through**

Run:

```bash
.venv/bin/pytest \
  tests/test_epi_agent_runtime.py::test_model_result_returned_after_cancel_is_discarded \
  tests/test_epi_agent_runtime.py::test_tool_result_returned_after_cancel_is_discarded -q
```

Expected: both fail because `_call_model` and `_execute_tools` do not call `cancellation_point()`.

- [ ] **Step 7: Guard model and tool boundaries**

In `epi_agent/runtime.py`, import `cancellation_point`. In `_call_model`, replace the current synchronous provider assignment with:

```python
    cancellation_point()
    try:
        answer = model.bind_tools(agent_config.registry.model_schemas()).invoke(
            messages,
            config=config,
            max_completion_tokens=budget,
        )
    finally:
        cancellation_point()
```

Also make `cancellation_point()` the first executable statement in both `_call_model` and `_acall_model`, before `_prepare_model_request`, so a cancelled run cannot publish an early validation or budget patch.

In `_acall_model`, replace the current asynchronous provider assignment with:

```python
    cancellation_point()
    try:
        answer = await model.bind_tools(
            agent_config.registry.model_schemas()
        ).ainvoke(
            messages,
            config=config,
            max_completion_tokens=budget,
        )
    finally:
        cancellation_point()
```

This `finally` placement also converts a provider error arriving after cancellation into `RunCancelled`. Inside `_execute_tools`, replace the registry invocation at the start of its existing `try` block with:

```python
cancellation_point()
try:
    result = agent_config.registry.invoke(name, arguments, context=context)
finally:
    cancellation_point()
```

Keep the existing `except GraphInterrupt` and `except ToolExecutionError` clauses attached to that outer `try`. Add `cancellation_point()` as the first line inside `for call in calls` as well, so branches that do not invoke the registry cannot publish after cancellation. Do not catch `RunCancelled`; it must leave the node before LangGraph checkpoints the node result.

- [ ] **Step 8: Add a failing Python child-process cancellation test**

In `tests/test_epi_python_tools.py`, monkeypatch `subprocess.Popen` with a fake process whose `communicate(timeout=...)` raises `subprocess.TimeoutExpired` until a supplied token is cancelled. Run `LocalPythonRuntime.execute()` inside `bind_cancellation(token)` and assert:

```python
with bind_cancellation(token), pytest.raises(RunCancelled):
    runtime.execute(request, {"dataset-1": dataframe})

assert fake_process.terminated is True
```

The fake process must expose `pid`, `returncode`, `poll()`, `communicate()`, and a termination flag observed through a monkeypatched `_terminate_process_group`.
Point the runtime's temporary execution root at `tmp_path`; after cancellation, also assert no per-execution child directory remains beneath it. This verifies that termination still exits through the existing temporary-directory cleanup.

- [ ] **Step 9: Run the Python test and verify it blocks until its ordinary timeout**

Run:

```bash
.venv/bin/pytest tests/test_epi_python_tools.py::test_python_process_is_terminated_when_run_is_cancelled -q
```

Expected: FAIL because `LocalPythonRuntime.execute()` calls `communicate()` once with the entire execution timeout.

- [ ] **Step 10: Poll Python execution and terminate on cancellation**

In `epi_agent/runtimes/python/local_process.py`, import `RunCancelled` and `cancellation_point`. Replace the single blocking `communicate(timeout=self._timeout_seconds)` call with a bounded polling loop:

```python
deadline = started + self._timeout_seconds
try:
    while True:
        cancellation_point()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, self._timeout_seconds)
        try:
            child_stdout, child_stderr = process.communicate(
                timeout=min(0.1, remaining)
            )
            cancellation_point()
            break
        except subprocess.TimeoutExpired:
            continue
except RunCancelled:
    _terminate_process_group(process)
    raise
except subprocess.TimeoutExpired as exc:
    _terminate_process_group(process)
    raise _failure(
        "EXECUTION_TIMEOUT",
        (
            "Python analysis exceeded the configured "
            f"{self._timeout_seconds:g} second timeout."
        ),
        category="timeout",
        recoverable=True,
    ) from exc
```

Keep the existing nonzero-return-code and bounded-output parsing logic unchanged.

- [ ] **Step 11: Run the focused cancellation and existing runtime tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_run_cancellation.py \
  tests/test_epi_agent_runtime.py \
  tests/test_epi_python_tools.py -q
```

Expected: all selected tests pass.

- [ ] **Step 12: Commit the cooperative cancellation layer**

```bash
git add \
  utils/run_cancellation.py \
  tests/test_run_cancellation.py \
  epi_agent/runtime.py \
  epi_agent/runtimes/python/local_process.py \
  tests/test_epi_agent_runtime.py \
  tests/test_epi_python_tools.py
git commit -m "feat: add cooperative run cancellation points"
```

---

### Task 2: Cancellable Runner and Durable Checkpoint Restoration

**Files:**
- Create: `tests/test_active_run_cancellation.py`
- Modify: `api/runtime.py`

**Interfaces:**
- Consumes: `CancellationToken`, `RunCancelled`, and `bind_cancellation` from Task 1.
- Produces: `ApiGraphRunner.cancel(thread_id: str) -> dict[str, Any]`, `ReportAgentApiRuntime.cancel_run(thread_id: str) -> ApiThreadState`, `_cancelled_turn_patch(...)`, and terminal runner state `cancelled`.
- Produces for Task 3: root state field `cancelled_turn` with `message_id`, `text`, `turn_hash`, and `attachment_ids`.

- [ ] **Step 1: Write a real-checkpointer rollback test**

Create `tests/test_active_run_cancellation.py`. Build a small LangGraph with `InMemorySaver`, an `approved` node that writes `{"approved_value": "dataset-1"}`, a blocking node named `tools` that waits on a test event, calls `cancellation_point()`, and then returns `{"draft_value": "must-disappear"}`, and a no-op terminal node named `finish`. Route `tools` to `finish` and `finish` to `END`; this matches the production terminal node used below. Use an `Annotated[list, add_messages]` messages channel and conversation-event artifacts. Assert this sequence:

```python
runtime.submit_message(thread_id, "Analyze the attached cohort", [attachment_id])
assert blocking_started.wait(timeout=1)

cancelled = runtime.cancel_run(thread_id)
release_blocking.set()

assert cancelled.run.state == "cancelled"
assert cancelled.conversation[-1].text == "Analyze the attached cohort"
assert cancelled.conversation[-1].status == "cancelled"
assert cancelled.conversation[-1].attachments[0].id == attachment_id

snapshot = graph.get_state(graph_config(thread_id))
assert snapshot.values.get("draft_value") is None
assert snapshot.values["terminal_control"]["status"] == "cancelled"
assert all(
    getattr(message, "id", None) != "late-tool-result"
    for message in snapshot.values.get("messages", [])
)
```

Have the blocked `tools` node attempt to return a `ToolMessage(id="late-tool-result", ...)` with its draft value. Add a second graph case that pauses for an approval interrupt, applies `Command(resume=...)` through `ReportAgentApiRuntime.resume_interrupt()`, then blocks in the next model/tool step. Cancel there and assert the approved artifact and its durable messages remain while the later draft artifact and tool-result message are absent. This test must fail if the approval resume is moved back into the long background phase.

Add a third case using a model whose `invoke()` waits on an event and returns `AIMessage(id="late-model-answer", content="late")`. Cancel while it is waiting, release it, and assert the restored checkpoint is terminal `cancelled` and contains no message with ID `late-model-answer`. This combines the boundary unit test from Task 1 with real checkpoint restoration.

- [ ] **Step 2: Run the cancellation tests and verify the API is missing**

Run:

```bash
.venv/bin/pytest tests/test_active_run_cancellation.py -q
```

Expected: FAIL because `cancel_run`, message `status`, and runner cancellation do not exist.

- [ ] **Step 3: Replace loose job dictionaries with a focused job record**

In `api/runtime.py`, add these internal records near `ApiGraphRunner`:

```python
from collections.abc import Callable
from copy import deepcopy
from langchain_core.runnables import RunnableConfig
from utils.run_cancellation import (
    CancellationToken,
    RunCancelled,
    bind_cancellation,
)


@dataclass(frozen=True)
class CancelledTurn:
    message_id: str
    text: str
    turn_hash: str
    attachment_ids: tuple[str, ...]


@dataclass
class GraphJob:
    status: dict[str, Any]
    token: CancellationToken
    durable_config: RunnableConfig | None
    restore: Callable[[RunnableConfig], None] | None = None


class CancellationRestoreError(RuntimeError):
    pass
```

Change `_jobs` to `dict[str, GraphJob]`. `_reserve_run()` must snapshot the current graph before the payload factory mutates it:

```python
def _durable_config(self, thread_id: str) -> RunnableConfig | None:
    root = graph_config(thread_id)
    snapshot = self.app.get_state(root, subgraphs=True)
    saved = getattr(snapshot, "config", None)
    return deepcopy(saved) if isinstance(saved, dict) else None
```

Return `(started, job)` from `_reserve_run`, and make `status()` return `dict(job.status)`.

- [ ] **Step 4: Bind the token around every graph invocation**

In `_run_reserved`, receive the `GraphJob` rather than a separate status dictionary. Use `job.status` for all updates and wrap every `self.app.invoke(...)` call:

```python
with bind_cancellation(job.token):
    self.app.invoke(initial_payload, config)
```

and:

```python
with bind_cancellation(job.token):
    self.app.invoke({}, config)
```

Catch cancellation before the generic exception mapping:

```python
except RunCancelled:
    return self.status(thread_id)
```

Do not call `on_initial_payload_error` for `RunCancelled`; cancellation restoration owns attachment promotion and conversation-history promotion.

Update the nested `store()` helper to mutate `job.status` while holding `_lock`; it must never replace `self._jobs[thread_id]` with a plain dictionary after `_jobs` has been converted to `GraphJob` records.

- [ ] **Step 5: Add idempotent runner cancellation**

Add this public runner contract:

```python
def cancel(self, thread_id: str) -> dict[str, Any]:
    with self._lock:
        job = self._jobs.get(thread_id)
        if job is None or job.status.get("state") != "running":
            return dict(job.status) if job is not None else _idle_status()
        if job.restore is None or job.durable_config is None:
            raise CancellationRestoreError(
                "The active run has no durable cancellation boundary."
            )
        job.token.cancel()
        restore = job.restore
        durable_config = deepcopy(job.durable_config)

    try:
        restore(durable_config)
    except Exception as exc:
        raise CancellationRestoreError(
            "Unable to restore the last durable checkpoint."
        ) from exc

    with self._lock:
        current = self._jobs.get(thread_id)
        if current is not job:
            return dict(current.status) if current is not None else _idle_status()
        job.status.update(
            {
                "state": "cancelled",
                "error": None,
                "error_code": None,
                "user_message": None,
                "updated_at": time.time(),
            }
        )
        return dict(job.status)
```

The restore callback runs outside `_lock`, allowing polling and the worker's cancellation check to proceed. A failed restore leaves the job's public state as `running`, making a retry safe.

- [ ] **Step 6: Persist review decisions as new durable boundaries before continuing**

Add `start_background_after_durable_resume(..., restore: Callable[[RunnableConfig], None])` to `ApiGraphRunner`. It must reserve the job, assign `job.restore = restore` before doing synchronous work, invoke the short `Command(resume=...)` synchronously with static breakpoints after the two nodes that can consume public interrupts, update `job.durable_config` from the resulting snapshot, then start the ordinary background loop with `initial_payload=None`:

```python
self.app.invoke(
    initial_payload,
    graph_config(thread_id),
    interrupt_after=["tools", "model_output_gate"],
)
snapshot = self.app.get_state(graph_config(thread_id), subgraphs=True)
if isinstance(getattr(snapshot, "config", None), dict):
    job.durable_config = deepcopy(snapshot.config)
job.status["steps"] += 1
```

Wrap this invocation in `bind_cancellation(job.token)`, just like `_run_reserved`. If another client cancels during the short synchronous phase, let `RunCancelled` leave the invocation and do not overwrite the cancellation endpoint's restored state. If the resume reaches a terminal graph state, keep the same job and let the background loop immediately project `done` or `interrupted`. This short synchronous phase guarantees an accepted review decision is saved before the browser can offer Cancel for subsequent work.

Change `ReportAgentApiRuntime.resume_interrupt()` to use this method rather than `start_background()`.

- [ ] **Step 7: Build the cancelled checkpoint patch**

In `api/runtime.py`, add `_cancelled_turn_patch`. It receives the durable snapshot values, the `CancelledTurn`, and current attachment manifests. It must:

1. start from `ensure_conversation_state`;
2. find the user event with `user_turn_hash == turn.turn_hash` and set its `status` to `cancelled`, or append one with `build_user_event(..., status="cancelled")`;
3. add missing input attachment events beneath that user event;
4. merge available input manifests into `artifacts.attachments`;
5. omit `messages` from the patch so the new checkpoint inherits exactly the model history present at the selected durable boundary; messages from later checkpoints are therefore unreachable, while messages that produced an approved artifact remain consistent with that artifact;
6. clear `current_turn_artifact_refs`, `current_turn_output_artifact_refs`, `terminal_error`, `final_response`, and `completion_blocked`;
7. set `authorized_attachment_ids` to the sorted union of the boundary's existing authorized IDs and the retained cancelled-turn attachment IDs whose committed manifests are available;
8. set `terminal_control={"status": "cancelled", "reason": "User cancelled the active run."}`; and
9. set this bounded root-state record:

```python
"cancelled_turn": {
    "message_id": turn.message_id,
    "text": turn.text,
    "turn_hash": turn.turn_hash,
    "attachment_ids": list(turn.attachment_ids),
}
```

Return only the update patch required by `app.update_state`; do not copy attachment bytes or provider data.

- [ ] **Step 8: Capture the turn descriptor and restore callback at submission**

First, refactor `_initial_graph_state` so its `message` argument accepts `HumanMessage | None` and initializes `messages` to `[message] if message is not None else []`. In `submit_message()`, immediately after reading `snapshot`, seed a terminal blank checkpoint only when the thread has no saved values:

```python
if not _projection_values(snapshot):
    app.update_state(
        graph_config(thread_id),
        _initial_graph_state(
            thread_id,
            None,
            active_study_id=active_study_id,
        ),
        as_node="finish",
    )
    snapshot = app.get_state(graph_config(thread_id), subgraphs=True)
```

This one-time checkpoint gives a brand-new conversation a concrete pre-turn checkpoint ID; never use a bare `graph_config(thread_id)` as a durable reference because it resolves to whichever checkpoint is newest when restoration runs.

Then capture the turn descriptor before binding the new message:

```python
turn = CancelledTurn(
    message_id=str(message.id),
    text=str(message.content or ""),
    turn_hash=self._message_turn_hash(message),
    attachment_ids=tuple(attachment_ids),
)
```

Pass a restore closure to `start_background_from_factory`. The closure must load the selected durable snapshot, require and commit the original attachment bindings, apply `_cancelled_turn_patch`, and terminate the restored branch:

```python
def restore_cancelled(durable_config: RunnableConfig) -> None:
    base = app.get_state(durable_config, subgraphs=True)
    manifests = [
        self.attachment_store.require(thread_id, attachment_id)
        for attachment_id in turn.attachment_ids
    ]
    self._commit_binding_manifests(thread_id, manifests)
    committed_manifests = [
        self.attachment_store.require(thread_id, attachment_id)
        for attachment_id in turn.attachment_ids
    ]
    patch = _cancelled_turn_patch(
        _projection_values(base),
        turn=turn,
        manifests=committed_manifests,
    )
    app.update_state(durable_config, patch, as_node="finish")
    if self.history_store is not None:
        self.history_store.promote_pending(thread_id)
```

Add `restore: Callable[[RunnableConfig], None]` to `start_background_from_factory`, pass `restore=restore_cancelled` from `submit_message`, and assign it to the reserved `GraphJob` before calling `payload_factory()`. For resume runs, reconstruct the latest turn from the snapshot's latest user event and its input attachment events, build the same restore closure, and pass it to `start_background_after_durable_resume`. The selected durable checkpoint itself is the only source of retained model messages.

- [ ] **Step 9: Expose runtime cancellation and persistent cancelled projection**

Add:

```python
def cancel_run(self, thread_id: str) -> ApiThreadState:
    with self._lock:
        known_in_memory = thread_id in self._threads
    known_in_history = (
        self.history_store is not None
        and self.history_store.get(thread_id) is not None
    )
    if not known_in_memory and not known_in_history:
        raise KeyError(thread_id)
    thread = self._thread(thread_id)
    _app, runner = self._ensure_graph(thread)
    runner.cancel(thread_id)
    if self.history_store is not None:
        self.history_store.touch(thread_id)
    return self.state(thread_id)
```

In `project_thread_state`, when the job registry is idle after restart but `terminal_control.status == "cancelled"`, project `RunStatus(state="cancelled", ...)`. This rule must appear before the ordinary interrupted/done correction and after terminal-error handling.

- [ ] **Step 10: Run rollback tests and current lifecycle regression tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_active_run_cancellation.py \
  tests/test_conversation_lifecycle_backport.py \
  tests/test_api_root_projection.py -q
```

Expected: all pass, including cancellation of a first pending turn promoting it into visible history rather than deleting it.

- [ ] **Step 11: Commit the runner and checkpoint restoration**

```bash
git add api/runtime.py tests/test_active_run_cancellation.py
git commit -m "feat: restore durable checkpoint on run cancellation"
```

---

### Task 3: API Contract, Cancelled Message Projection, and Retry Context

**Files:**
- Modify: `api/schemas.py`
- Modify: `api/server.py`
- Modify: `utils/display_history.py`
- Modify: `epi_agent/runtime.py`
- Modify: `epi_agent/agent.py`
- Modify: `tests/test_active_run_cancellation.py`
- Modify: `tests/test_display_history.py`
- Modify: `tests/test_api_root_projection.py`
- Modify: `tests/test_epi_agent_runtime.py`

**Interfaces:**
- Consumes: `ReportAgentApiRuntime.cancel_run()` and root `cancelled_turn` from Task 2.
- Produces: `POST /api/threads/{thread_id}/cancel -> ApiThreadState`, `RunState` member `cancelled`, `ConversationMessage.status`, and inactive retry context for the next model request.

- [ ] **Step 1: Add failing schema, endpoint, and projection tests**

Add these assertions:

```python
def test_cancelled_is_a_terminal_public_run_state() -> None:
    assert RunStatus(state="cancelled").state == "cancelled"


def test_cancelled_user_event_projects_status_and_attachment() -> None:
    messages = _conversation(cancelled_state_with_attachment())
    assert messages[-1].status == "cancelled"
    assert messages[-1].attachments[0].id == "attachment-1"
```

In `tests/test_active_run_cancellation.py`, construct `TestClient(create_app(runtime=fake_runtime))`, post `/api/threads/thread-1/cancel`, and assert status 200, `run.state == "cancelled"`, and exactly one `fake_runtime.cancel_run("thread-1")` call. Add an unknown-thread fake that raises `KeyError` and assert 404.

Run:

```bash
.venv/bin/pytest \
  tests/test_active_run_cancellation.py \
  tests/test_display_history.py \
  tests/test_api_root_projection.py -q
```

Expected: FAIL on the missing schema members and route.

- [ ] **Step 2: Extend strict API types**

In `api/schemas.py`:

```python
RunState = Literal[
    "idle",
    "running",
    "interrupted",
    "done",
    "cancelled",
    "error",
    "timeout",
]
```

and add to `ConversationMessage`:

```python
status: Literal["cancelled"] | None = None
```

Do not broaden the field to arbitrary strings.

- [ ] **Step 3: Carry event status through display projection**

In `utils/display_history.py`, extend `_message_kwargs_for_event`:

```python
status = event.get("status")
if status == "cancelled":
    additional_kwargs["status"] = "cancelled"
```

In `api/runtime.py`, add `_message_status(message) -> str | None` that returns only `"cancelled"` from `additional_kwargs`, and pass it to `ConversationMessage(status=...)` in `_conversation()`.

- [ ] **Step 4: Add the cancel endpoint**

In `api/server.py`, place the route immediately after message submission and before interrupt resume:

```python
@app.post(
    "/api/threads/{thread_id}/cancel",
    response_model=ApiThreadState,
)
def cancel_run(thread_id: str) -> ApiThreadState:
    try:
        return runtime.cancel_run(thread_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Conversation not found",
        ) from exc
    except CancellationRestoreError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CANCELLATION_RESTORE_FAILED",
                "message": str(exc),
            },
        ) from exc
```

Import `CancellationRestoreError`, define this route on the thread router, and call the identity-aware `runtime.cancel_run(identity, thread_id)` exactly as peer thread routes do.

- [ ] **Step 5: Type the cancelled-turn state and add bounded model context**

In `GenericEpiAgentState`, add:

```python
cancelled_turn: NotRequired[dict[str, Any]]
```

In `build_epi_agent_context_prompt`, append this exact instruction only when the root record validates as nonblank text plus a list of string attachment IDs:

```python
cancelled_context = ""
cancelled_turn = dict(state.get("cancelled_turn") or {})
cancelled_text = _bounded_text(cancelled_turn.get("text"))
cancelled_attachment_ids = _bounded_strings(
    cancelled_turn.get("attachment_ids")
)
if cancelled_text or cancelled_attachment_ids:
    cancelled_context = (
        "Most recent cancelled user turn. This record is inactive: do not "
        "continue it unless the latest user message explicitly asks to retry, "
        "continue, or refer to the cancelled work. Restart tools from the "
        "latest approved state; never claim to resume a partial execution. "
        "If a referenced attachment is unavailable, ask the user to attach "
        "it again:\n"
        + json.dumps(
            {
                "text": cancelled_text,
                "attachment_ids": cancelled_attachment_ids,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
```

Return `artifact_context`, `cancelled_context`, and optional study-design context joined by blank lines. The original attachment IDs remain in `authorized_attachment_ids`, so the existing attachment tools can reopen them if the new user explicitly continues.

- [ ] **Step 6: Test continuation context without hard-coded phrase matching**

In `tests/test_epi_agent_runtime.py`, call `build_epi_agent_context_prompt` with a cancelled turn and one available authorized attachment. Assert the prompt contains:

```python
assert '"attachment_ids":["attachment-1"]' in prompt
assert '"text":"Analyze the cohort"' in prompt
assert "inactive: do not continue it unless" in prompt
assert "never claim to resume a partial execution" in prompt
assert "ask the user to attach it again" in prompt
```

Also call it without `cancelled_turn` and assert `Most recent cancelled user turn` is absent. Then add a latest unrelated `HumanMessage` with no attachment IDs and assert the retained attachment card has `"current_turn":false` while the cancelled record is still labeled inactive. Finally, execute a fresh ordinary `FunctionTool` call under a new, uncancelled token and assert `_execute_tools` returns one successful `ToolMessage`; the previous partial call is not resumed or treated as a completed observation. Together these checks prove ordinary conversations receive no active cancelled work and continuation remains semantic rather than a keyword parser.

- [ ] **Step 7: Run backend contract tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_active_run_cancellation.py \
  tests/test_display_history.py \
  tests/test_api_root_projection.py \
  tests/test_epi_agent_runtime.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit the durable API and retry context**

```bash
git add \
  api/schemas.py \
  api/server.py \
  api/runtime.py \
  utils/display_history.py \
  epi_agent/runtime.py \
  epi_agent/agent.py \
  tests/test_active_run_cancellation.py \
  tests/test_display_history.py \
  tests/test_api_root_projection.py \
  tests/test_epi_agent_runtime.py
git commit -m "feat: expose durable active run cancellation"
```

---

### Task 4: Frontend Cancel Control and Cancelled Message Badge

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/apiClient.ts`
- Modify: `frontend/src/apiClient.test.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/ConversationMessage.tsx`
- Modify: `frontend/src/ConversationMessage.test.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `POST /api/threads/{thread_id}/cancel` and `ApiThreadState` from Task 3.
- Produces: one active-run Cancel button, transient `Cancelling...` UI, cancelled message badge, and stale-poll invalidation.

- [ ] **Step 1: Write the failing API-client test**

In `frontend/src/apiClient.test.ts`, import `cancelRun` and add:

```typescript
it("cancels the active run for one thread", async () => {
  const cancelled = {
    ...threadState,
    run: { ...threadState.run, state: "cancelled" as const },
  };
  const fetchMock = vi.fn().mockResolvedValue(jsonResponse(cancelled));

  await expect(cancelRun(fetchMock, "", "thread-1")).resolves.toEqual(cancelled);
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/threads/thread-1/cancel",
    { method: "POST" },
  );
});
```

Use a copied state object rather than mutating the shared `threadState` fixture in the final test implementation.

- [ ] **Step 2: Run the client test and verify the missing export**

Run:

```bash
cd frontend && npm test -- --run src/apiClient.test.ts
```

Expected: FAIL because `cancelRun` is not exported.

- [ ] **Step 3: Add TypeScript types and client method**

In `frontend/src/types.ts`, add `"cancelled"` to `RunState` and add:

```typescript
status?: "cancelled" | null;
```

to `ConversationMessage`.

In `frontend/src/apiClient.ts`, add:

```typescript
export async function cancelRun(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
): Promise<ApiThreadState> {
  const response = await fetchImpl(
    apiUrl(apiBase, `/api/threads/${pathParam(threadId)}/cancel`),
    { method: "POST" },
  );
  return parseJsonResponse<ApiThreadState>(response);
}
```

Expose it from `createApiClient`:

```typescript
cancelRun(threadId: string) {
  return cancelRun(fetchImpl, apiBase, threadId);
},
```

- [ ] **Step 4: Write failing App behavior tests**

In `frontend/src/App.test.tsx`, add one test with the following response order: runtime options, create thread, submitted running state, cancel response. The cancel response must contain the original user message with `status: "cancelled"` and an input attachment. Assert:

```typescript
expect(await screen.findByRole("button", { name: "Cancel run" })).toBeEnabled();
expect(screen.getByRole("button", { name: "New conversation" })).toBeDisabled();

fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
expect(screen.getByRole("button", { name: "Cancelling..." })).toBeDisabled();

expect(await screen.findByText("Cancelled")).toBeInTheDocument();
expect(screen.getByLabelText("Ask a question about your dataset!")).toBeEnabled();
expect(fetchMock).toHaveBeenCalledWith(
  "http://api.test/api/threads/thread-1/cancel",
  { method: "POST" },
);
```

Add a fake-timer race test in which a previously scheduled state poll resolves with `run.state="running"` after the cancel response. Assert the late poll does not remove the Cancelled badge or relock the composer.

- [ ] **Step 5: Run the App tests and verify Cancel is absent**

Run:

```bash
cd frontend && npm test -- --run src/App.test.tsx
```

Expected: the new tests fail because the UI only renders Send during an active run.

- [ ] **Step 6: Implement cancellation state and poll invalidation**

In `App.tsx`, add:

```typescript
const [isCancelling, setIsCancelling] = useState(false);
```

and:

```typescript
async function cancelActiveRun() {
  if (!threadId || state?.run.state !== "running" || isCancelling) {
    return;
  }
  setIsCancelling(true);
  pollGenerationRef.current += 1;
  try {
    const nextState = await apiClient.cancelRun(threadId);
    applyThreadState(nextState);
    setError(null);
  } catch (cancelError) {
    await handleRequestError(cancelError, threadId);
  } finally {
    setIsCancelling(false);
  }
}
```

Include `isCancelling` in `isBusy`, clear it in `newConversation`, and replace the Send button only while a run is in flight or cancellation is pending:

```tsx
{isRunInFlight || isCancelling ? (
  <button
    aria-label={isCancelling ? "Cancelling..." : "Cancel run"}
    className="cancel-run-button"
    disabled={isCancelling}
    onClick={() => void cancelActiveRun()}
    type="button"
  >
    {isCancelling ? "Cancelling..." : "Cancel"}
  </button>
) : (
  <button disabled={isSendDisabled} type="submit">Send</button>
)}
```

The existing polling generation guard must remain the single authority for discarding stale responses; do not introduce a second AbortController state machine.

- [ ] **Step 7: Write and implement the cancelled message badge test**

In `ConversationMessage.test.tsx`, render a user message with `status: "cancelled"` and one input attachment. Assert the text, attachment card, and an accessible status label remain visible:

```typescript
expect(screen.getByText("Cancelled")).toHaveAttribute(
  "aria-label",
  "Message status: Cancelled",
);
expect(screen.getByText("Analyze patients.csv")).toBeInTheDocument();
expect(screen.getByText("patients.csv")).toBeInTheDocument();
```

In `ConversationMessage.tsx`, render immediately beside the role label:

```tsx
{message.status === "cancelled" ? (
  <span
    aria-label="Message status: Cancelled"
    className="message-status message-status-cancelled"
  >
    Cancelled
  </span>
) : null}
```

- [ ] **Step 8: Add bounded styling**

In `styles.css`, add:

```css
.message-form .cancel-run-button {
  border-color: #cf222e;
  background: #cf222e;
}

.message-form .cancel-run-button:hover:not(:disabled) {
  background: #a40e26;
}

.message-status-cancelled {
  display: inline-flex;
  align-items: center;
  width: fit-content;
  border-radius: 999px;
  background: #fff1f0;
  color: #a40e26;
  padding: 2px 8px;
  font-size: 12px;
  font-weight: 700;
}
```

Reuse existing disabled-button opacity and focus styles.

- [ ] **Step 9: Run all focused frontend tests**

Run:

```bash
cd frontend && npm test -- --run \
  src/apiClient.test.ts \
  src/App.test.tsx \
  src/ConversationMessage.test.tsx
```

Expected: all pass, including existing New conversation and polling tests.

- [ ] **Step 10: Commit the frontend cancellation flow**

```bash
git add \
  frontend/src/types.ts \
  frontend/src/apiClient.ts \
  frontend/src/apiClient.test.ts \
  frontend/src/App.tsx \
  frontend/src/App.test.tsx \
  frontend/src/ConversationMessage.tsx \
  frontend/src/ConversationMessage.test.tsx \
  frontend/src/styles.css
git commit -m "feat: add active run cancel control"
```

---

### Task 5: End-to-End Regression, Documentation, and Browser Build

**Files:**
- Modify: `README.md`
- Modify: `tests/test_active_run_cancellation.py`
- Modify: `frontend/dist/index.html`
- Modify: `frontend/dist/assets/*` through the existing frontend build command

**Interfaces:**
- Consumes: all backend and frontend contracts from Tasks 1-4.
- Produces: restart/idempotence regression evidence, documented user semantics, and the committed production browser bundle.

- [ ] **Step 1: Add restart and duplicate-cancel regression tests**

In `tests/test_active_run_cancellation.py`, use a real temporary SQLite `SqliteSaver` graph. Cancel a running turn, dispose the first runtime/graph connection, reconstruct the runtime over the same checkpoint file, and assert:

```python
restored = restarted_runtime.state(thread_id)
assert restored.run.state == "cancelled"
assert restored.conversation[-1].status == "cancelled"
thread = restarted_runtime._thread(thread_id)
_app, restarted_runner = restarted_runtime._ensure_graph(thread)
assert restarted_runner.status(thread_id)["state"] == "idle"
```

Call `cancel_run(thread_id)` twice before restart and assert the second response equals the first public state and creates no duplicate cancelled user event or attachment event.

- [ ] **Step 2: Run the restart tests and correct any recovery regression**

Run:

```bash
.venv/bin/pytest \
  tests/test_active_run_cancellation.py -q
```

Expected: all pass. If `_should_recover_snapshot` attempts to restart cancelled work, ensure its existing `not values.get("terminal_control")` guard remains and that `project_thread_state` derives `cancelled` from the durable terminal control.

- [ ] **Step 3: Document the user-facing behavior**

Add this concise paragraph to the hosted/local usage section of `README.md`:

```markdown
While the agent is working, **Cancel** stops the active request and returns the
conversation to its latest completed or approved save point. The cancelled
request and its original attachments remain visible and can be retried later;
unfinished model, tool, and Python output is discarded. **New conversation**
continues to open a separate blank conversation and does not cancel work.
```

- [ ] **Step 4: Run the complete backend suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: full suite passes with zero failures. Real-provider smoke tests that require explicit credentials may remain deselected according to their existing markers; do not report them as passes if they did not run.

- [ ] **Step 5: Run the complete frontend suite and production build**

Run:

```bash
cd frontend && npm test -- --run
cd frontend && npm run build
```

Expected: all Vitest tests pass; TypeScript compilation and Vite production build succeed; `frontend/dist/build-manifest.json` and hashed assets are refreshed by the existing build.

- [ ] **Step 6: Run static repository checks**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only intended cancellation, documentation, test, and generated frontend build files are modified. Preserve unrelated untracked study packages and user files.

- [ ] **Step 7: Commit documentation and the verified browser bundle**

```bash
git add README.md tests/test_active_run_cancellation.py frontend/dist
git commit -m "build: ship active run cancellation"
```

- [ ] **Step 8: Review the final diff against the design**

Verify each statement directly from tests and code:

```text
Cancel stays in the same conversation.
The latest completed or approved checkpoint survives.
The cancelled user message and original attachments remain visible.
Partial model/tool messages and unapproved artifacts are absent.
Late model/tool results cannot checkpoint.
Python children terminate.
Continue/retry receives inactive cancelled-turn context and attachment IDs.
Unrelated messages are not instructed to continue cancelled work.
New conversation remains separate.
Successful cancellation remains terminal after restart.
No Redis, queue, extra worker, or new job database was added.
```

Expected: every line maps to at least one passing automated test and no spec requirement is left implicit.
