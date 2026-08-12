# Agent Activity Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable, user-visible activity timeline that shows generic model stages, successful tool calls, repeated calls, and human-review pauses through the existing polling API.

**Architecture:** A SQLite-backed `SqliteActivityStore` records one activity run per user request and implements the graph-facing activity sink. Central model/tool boundaries emit lifecycle notifications; `ReportAgentApiRuntime` owns request, review, recovery, cancellation, and deletion reconciliation. `ApiThreadState` carries durable activity runs to a focused React timeline component, while the existing one-second poll remains the transport.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite, LangGraph/LangChain, React 19, TypeScript 5.8, Vitest, pytest, Playwright.

## Global Constraints

- Run Python tooling only through `.venv/bin/python`; never use the system `python3`.
- Target the current single-EC2 deployment with one application process and one Uvicorn worker.
- Keep the existing `POLL_INTERVAL_MS = 1000`; do not add SSE, WebSockets, or token streaming.
- Do not modify individual tool implementations or how the model selects tools.
- Friendly labels must come from a fixed central mapping or a deterministic fallback based on the registered tool name.
- Public activity must not contain prompts, chain-of-thought, arguments, raw tool results, SQL/provider errors, dataset rows, attachment content, credentials, or secrets.
- Recoverable tool failures must disappear from public activity; only the existing run-level error may describe an unrecoverable overall failure.
- Activity recording is best-effort and must never fail model execution, tool execution, review handling, cancellation, or thread-state projection.
- Every UI change must run `npm --prefix frontend run build`, then `.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest`.
- The dedicated real smoke must use the production FastAPI, compiled TypeScript inputs, LLM, DB-RAG, and browser path; it may run once for at most five minutes and must preserve diagnostics on failure.

## File Map

- Create `api/activity_labels.py`: fixed friendly labels plus deterministic fallback.
- Create `api/activity_store.py`: SQLite schema, activity lifecycle operations, durable API projection.
- Create `epi_agent/activity.py`: graph-facing sink protocol, no-op sink, and exception-safe notification helper.
- Create `frontend/src/AgentActivityTimeline.tsx`: accessible expanded/waiting/collapsed timeline.
- Create `frontend/src/AgentActivityTimeline.test.tsx`: isolated timeline rendering tests.
- Create `tests/test_activity_labels.py`: public-label safety contract.
- Create `tests/test_activity_store.py`: persistence, ordering, retry hiding, review, recovery, and deletion tests.
- Create `tests/test_api_activity_timeline.py`: API runtime lifecycle and projection tests.
- Create `scripts/e2e_agent_activity_timeline_real.py`: dedicated real browser smoke.
- Modify `api/schemas.py`: public activity models and `ApiThreadState.activity_runs`.
- Modify `api/runtime.py`: start, reconcile, resume, recover, cancel, delete, and project activity.
- Modify `api/app.py`: construct one store and pass it to the graph and API runtime.
- Modify `graph/builder.py`: accept and forward the activity sink.
- Modify `epi_agent/agent.py`: accept and install the sink in `EpiAgentRuntimeConfig`.
- Modify `epi_agent/runtime.py`: emit model and tool lifecycle events centrally.
- Modify `frontend/src/types.ts`: mirror the public activity contract.
- Modify `frontend/src/App.tsx`: place timelines after their initiating user messages and preserve the generic fallback.
- Modify `frontend/src/App.test.tsx`: polling, placement, review, fallback, and cancellation integration tests.
- Modify `frontend/src/apiClient.test.ts`: verify activity JSON survives state parsing unchanged.
- Modify `frontend/src/styles.css`: timeline layout, statuses, disclosure, and responsive styling.
- Rebuild tracked files under `frontend/dist/` and refresh `frontend/dist/build-manifest.json`.

---

### Task 1: Define the public activity contract and label policy

**Files:**
- Create: `api/activity_labels.py`
- Create: `tests/test_activity_labels.py`
- Modify: `api/schemas.py`
- Test: `tests/test_api_root_projection.py`

**Interfaces:**
- Produces: `ToolActivityLabels`, `tool_activity_labels(tool_name: str) -> ToolActivityLabels`.
- Produces: `ActivityItemStatus`, `ActivityRunState`, `ActivityItem`, and `ActivityRun` Pydantic API models.
- Produces: `ApiThreadState.activity_runs: list[ActivityRun]` with an empty-list default.

- [ ] **Step 1: Write failing label-policy tests**

Create `tests/test_activity_labels.py` with exact assertions for a known DB-RAG tool, a review tool, and a sanitized fallback:

```python
from api.activity_labels import tool_activity_labels


def test_known_tool_uses_curated_public_labels() -> None:
    labels = tool_activity_labels("dbrag-search_catalog")
    assert labels.started == "Searching the data catalog"
    assert labels.waiting is None
    assert labels.completed == "Searching the data catalog"


def test_review_tool_has_waiting_and_completed_labels() -> None:
    labels = tool_activity_labels("dbrag-request_dataset_plan_review")
    assert labels.started == "Preparing dataset plan review"
    assert labels.waiting == "Waiting for dataset plan review"
    assert labels.completed == "Dataset plan reviewed"


def test_unknown_registered_tool_uses_sanitized_name_only() -> None:
    labels = tool_activity_labels("custom-check_sample_balance")
    assert labels.started == "Check sample balance"
    assert labels.completed == "Check sample balance"
    assert "custom" not in labels.started.casefold()
```

- [ ] **Step 2: Run the label tests and confirm the missing-module failure**

Run: `.venv/bin/python -m pytest tests/test_activity_labels.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'api.activity_labels'`.

- [ ] **Step 3: Add the fixed mapping and fallback implementation**

Create `api/activity_labels.py` with an immutable record and one central mapping. Include every currently registered tool family; ordinary completed labels intentionally equal their started labels.

```python
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ToolActivityLabels:
    started: str
    waiting: str | None = None
    completed: str | None = None


_LABELS = {
    "attachments-inspect": ToolActivityLabels("Inspecting an attachment"),
    "attachments-inspect_image": ToolActivityLabels("Inspecting an image"),
    "attachments-load_table": ToolActivityLabels("Loading a data table"),
    "attachments-parse_structured": ToolActivityLabels("Reading structured data"),
    "attachments-read_document": ToolActivityLabels("Reading a document"),
    "analysis-run_custom_python": ToolActivityLabels("Running statistical analysis"),
    "analysis-request_result_review": ToolActivityLabels(
        "Preparing analysis review", "Waiting for analysis review", "Analysis reviewed"
    ),
    "dbrag-open_artifact": ToolActivityLabels("Opening database evidence"),
    "dbrag-search_catalog": ToolActivityLabels("Searching the data catalog"),
    "dbrag-inspect_table": ToolActivityLabels("Inspecting a database table"),
    "dbrag-find_join_paths": ToolActivityLabels("Finding relationships between tables"),
    "dbrag-profile_relationship": ToolActivityLabels("Checking table relationships"),
    "dbrag-save_dataset_plan": ToolActivityLabels("Saving the dataset plan"),
    "dbrag-validate_dataset_plan": ToolActivityLabels("Validating the dataset plan"),
    "dbrag-validate_and_extract": ToolActivityLabels("Creating the dataset"),
    "dbrag-inspect_dataset": ToolActivityLabels("Checking dataset quality"),
    "dbrag-request_dataset_plan_review": ToolActivityLabels(
        "Preparing dataset plan review",
        "Waiting for dataset plan review",
        "Dataset plan reviewed",
    ),
    "dbrag-request_dataset_review": ToolActivityLabels(
        "Preparing dataset review", "Waiting for dataset approval", "Dataset reviewed"
    ),
    "general-calculate": ToolActivityLabels("Calculating a result"),
    "general-get_weather_tips": ToolActivityLabels("Preparing weather guidance"),
    "general-query_weather": ToolActivityLabels("Checking the weather"),
    "general-request_clarification": ToolActivityLabels(
        "Preparing a clarification", "Waiting for your answer", "Clarification answered"
    ),
    "general-search_web": ToolActivityLabels("Searching the web"),
    "publication-open_pubmed_article": ToolActivityLabels("Opening a PubMed article"),
    "publication-open_study_source": ToolActivityLabels("Opening study evidence"),
    "publication-search_pubmed": ToolActivityLabels("Searching PubMed"),
    "publication-search_study_evidence": ToolActivityLabels("Searching study evidence"),
    "study-design-search": ToolActivityLabels("Searching the study design"),
}

_PREFIXES = {"analysis", "attachments", "custom", "dbrag", "general", "publication"}


def tool_activity_labels(tool_name: str) -> ToolActivityLabels:
    normalized = str(tool_name or "").strip()
    if normalized in _LABELS:
        labels = _LABELS[normalized]
        return ToolActivityLabels(
            started=labels.started,
            waiting=labels.waiting,
            completed=labels.completed or labels.started,
        )
    words = [word for word in re.split(r"[-_]+", normalized) if word]
    if words and words[0].casefold() in _PREFIXES:
        words = words[1:]
    safe = " ".join(words)[:160].strip() or "Using an agent tool"
    label = safe[0].upper() + safe[1:] if safe else "Using an agent tool"
    return ToolActivityLabels(started=label, completed=label)
```

- [ ] **Step 4: Add strict public models to `api/schemas.py`**

Insert these models before `ApiThreadState`, then add `activity_runs` to the thread state:

```python
ActivityItemStatus = Literal["running", "completed", "waiting"]
ActivityRunState = Literal["running", "waiting", "completed", "cancelled", "error"]


class ActivityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=200)
    status: ActivityItemStatus
    tool_name: str | None = Field(default=None, max_length=200)
    tool_call_id: str | None = Field(default=None, max_length=200)
    created_at: str
    updated_at: str


class ActivityRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    user_message_id: str = Field(min_length=1)
    state: ActivityRunState
    activities: list[ActivityItem] = Field(default_factory=list)
    created_at: str
    updated_at: str


class ApiThreadState(BaseModel):
    thread_id: str
    run: RunStatus
    conversation: list[ConversationMessage] = Field(default_factory=list)
    activity_runs: list[ActivityRun] = Field(default_factory=list)
```

Update `tests/test_api_root_projection.py` to assert `projected.activity_runs == []` for callers that omit activity data.

- [ ] **Step 5: Run focused contract tests**

Run: `.venv/bin/python -m pytest tests/test_activity_labels.py tests/test_api_root_projection.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the public contract**

```bash
git add api/activity_labels.py api/schemas.py tests/test_activity_labels.py tests/test_api_root_projection.py
git commit -m "feat: define public agent activity contract"
```

---

### Task 2: Persist activity runs in SQLite

**Files:**
- Create: `api/activity_store.py`
- Create: `tests/test_activity_store.py`

**Interfaces:**
- Consumes: `ActivityRun`, `ActivityItem`, and `tool_activity_labels` from Task 1.
- Produces: `SqliteActivityStore(db_path: str | Path)`.
- Produces lifecycle methods `start_run`, `model_started`, `model_completed`, `tool_started`, `tool_completed`, `tool_recoverable_failure`, `mark_waiting`, `resume`, `finish`, `recover`, `list_runs`, and `delete_thread`.

- [ ] **Step 1: Write failing persistence and ordering tests**

Create `tests/test_activity_store.py` with a `tmp_path / "activity.db"` store and assert this exact sequence:

```python
def test_store_persists_ordered_repeated_tool_calls(tmp_path) -> None:
    path = tmp_path / "activity.db"
    store = SqliteActivityStore(path)
    store.start_run("thread-1", "user-event-1")
    store.model_completed("thread-1")
    store.tool_started("thread-1", "call-1", "dbrag-search_catalog")
    store.tool_completed("thread-1", "call-1", "dbrag-search_catalog")
    store.model_started("thread-1")
    store.model_completed("thread-1")
    store.tool_started("thread-1", "call-2", "dbrag-search_catalog")
    store.tool_completed("thread-1", "call-2", "dbrag-search_catalog")
    store.finish("thread-1", "completed")

    [run] = SqliteActivityStore(path).list_runs("thread-1")
    assert run.user_message_id == "user-event-1"
    assert run.state == "completed"
    assert [item.tool_call_id for item in run.activities if item.tool_name] == [
        "call-1",
        "call-2",
    ]
    assert [item.sequence for item in run.activities] == list(
        range(1, len(run.activities) + 1)
    )
```

Add tests proving `tool_recoverable_failure` hides `call-1`, `mark_waiting` persists a waiting activity, `resume` completes that waiting item and reopens the same run, `recover` hides stale running work and adds `Resuming your request`, and `delete_thread` removes only the requested thread.

- [ ] **Step 2: Run the store tests and confirm the missing-module failure**

Run: `.venv/bin/python -m pytest tests/test_activity_store.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'api.activity_store'`.

- [ ] **Step 3: Create the SQLite schema and connection helpers**

Create `api/activity_store.py`. Use a new connection per operation with `timeout=30`, `PRAGMA foreign_keys = ON`, and the following schema:

```sql
CREATE TABLE IF NOT EXISTS agent_activity_runs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','waiting','completed','cancelled','error')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_activity_run_per_thread
ON agent_activity_runs(thread_id)
WHERE state IN ('running','waiting');
CREATE TABLE IF NOT EXISTS agent_activity_items (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_activity_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    kind TEXT NOT NULL CHECK(kind IN ('model','tool','review','recovery')),
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','waiting')),
    tool_name TEXT,
    tool_call_id TEXT,
    visible INTEGER NOT NULL DEFAULT 1 CHECK(visible IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, sequence),
    UNIQUE(run_id, tool_call_id)
);
```

Use `datetime.now(UTC).isoformat()` and `uuid.uuid4().hex`. `_active_run` must select the newest `running` or `waiting` run for one thread. `_next_sequence` must calculate `COALESCE(MAX(sequence), 0) + 1` inside the same write transaction.

- [ ] **Step 4: Implement run and model-stage operations**

Implement these exact behaviors:

```python
def start_run(self, thread_id: str, user_message_id: str) -> str:
    # Insert one running run and sequence 1, kind=model,
    # label="Understanding your request", status=running.

def model_started(self, thread_id: str) -> None:
    # If the active run already has a visible running model item, do nothing.
    # Otherwise append kind=model, label="Choosing the next step", status=running.

def model_completed(self, thread_id: str) -> None:
    # Update the newest visible running model item to completed.
```

All values must use parameterized SQL. `start_run` is the only method that may raise for an invalid empty `thread_id` or `user_message_id`; runtime callers will invoke it through the best-effort boundary added in Task 4.

- [ ] **Step 5: Implement tool, review, and terminal operations**

Use the existing tool-call ID as the stable identity:

```python
def tool_started(self, thread_id: str, tool_call_id: str, tool_name: str) -> None:
    # INSERT OR IGNORE one visible running tool item using labels.started.

def tool_completed(self, thread_id: str, tool_call_id: str, tool_name: str) -> None:
    # Update the matching visible item to labels.completed and completed.

def tool_recoverable_failure(self, thread_id: str, tool_call_id: str) -> None:
    # Set visible=0 for the matching item, then append one visible running
    # model item labeled "Choosing the next step" unless one already exists.
    # Never store exception text.

def mark_waiting(self, thread_id: str, interrupt_type: str) -> None:
    # Set run state=waiting. Prefer updating the latest visible running tool
    # item with its mapped waiting label; otherwise append a review item using
    # the fixed interrupt label below.

def resume(self, thread_id: str, interrupt_type: str) -> None:
    # Complete the newest waiting item with the fixed completed label and set
    # the same run state back to running.

def finish(self, thread_id: str, state: ActivityRunState) -> None:
    # For completed, complete a running model item. For error/cancelled, hide
    # every remaining running item. Then close the active run with state.
```

Use these non-tool interrupt labels:

```python
_INTERRUPT_LABELS = {
    "dataset_plan_review": ("Waiting for dataset plan review", "Dataset plan reviewed"),
    "dataset_review": ("Waiting for dataset approval", "Dataset reviewed"),
    "analysis_result_review": ("Waiting for analysis review", "Analysis reviewed"),
    "agent_clarification": ("Waiting for your answer", "Clarification answered"),
    "model_output_limit": ("Waiting for output approval", "Output decision received"),
}
```

`recover(thread_id)` must hide every visible `running` item in the open run, set that run to `running`, and append one visible recovery item labeled `Resuming your request`. `list_runs(thread_id)` must return only visible items ordered by run creation then item sequence. `delete_thread(thread_id)` must delete runs in one transaction and rely on the item foreign-key cascade.

- [ ] **Step 6: Run persistence tests**

Run: `.venv/bin/python -m pytest tests/test_activity_store.py -q`

Expected: all persistence, ordering, retry-hiding, waiting, recovery, recreation, and deletion tests pass.

- [ ] **Step 7: Commit SQLite persistence**

```bash
git add api/activity_store.py tests/test_activity_store.py
git commit -m "feat: persist agent activity timelines"
```

---

### Task 3: Instrument central model and tool execution

**Files:**
- Create: `epi_agent/activity.py`
- Modify: `epi_agent/runtime.py`
- Modify: `epi_agent/agent.py`
- Modify: `graph/builder.py`
- Test: `tests/test_epi_agent_runtime.py`
- Test: `tests/test_epi_agent_root_graph.py`

**Interfaces:**
- Consumes: the lifecycle method names implemented by `SqliteActivityStore`.
- Produces: `ActivitySink` protocol, `NullActivitySink`, `NULL_ACTIVITY_SINK`, and `notify_activity`.
- Extends: `EpiAgentRuntimeConfig.activity_sink: ActivitySink`.
- Extends: `build_general_epi_agent_graph(..., activity_sink=...)` and `build_graph(..., activity_sink=...)`.

- [ ] **Step 1: Write failing model/tool notification tests**

In `tests/test_epi_agent_runtime.py`, add a recording sink with methods matching the protocol. Assert that `_execute_tools` produces:

```python
[
    ("tool_started", "thread-1", "success-1", "successful_tool_1"),
    ("tool_completed", "thread-1", "success-1", "successful_tool_1"),
    ("tool_started", "thread-1", "failure-1", "failing_tool"),
    ("tool_recoverable_failure", "thread-1", "failure-1"),
]
```

Use a separate one-tool state for this assertion so the existing multi-tool reducer test remains unchanged. Add a scripted-model test asserting `model_started` occurs before provider invocation and `model_completed` occurs after a successful answer for both `_call_model` and `_acall_model` paths.

- [ ] **Step 2: Run focused graph tests and confirm missing sink support**

Run: `.venv/bin/python -m pytest tests/test_epi_agent_runtime.py tests/test_epi_agent_root_graph.py -q`

Expected: the new tests fail because `EpiAgentRuntimeConfig` has no `activity_sink` and no notifications are emitted.

- [ ] **Step 3: Add the graph-facing sink boundary**

Create `epi_agent/activity.py`:

```python
from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ActivitySink(Protocol):
    def model_started(self, thread_id: str) -> None: ...
    def model_completed(self, thread_id: str) -> None: ...
    def tool_started(self, thread_id: str, tool_call_id: str, tool_name: str) -> None: ...
    def tool_completed(self, thread_id: str, tool_call_id: str, tool_name: str) -> None: ...
    def tool_recoverable_failure(self, thread_id: str, tool_call_id: str) -> None: ...


class NullActivitySink:
    def model_started(self, _thread_id: str) -> None:
        return None

    def model_completed(self, _thread_id: str) -> None:
        return None

    def tool_started(
        self,
        _thread_id: str,
        _tool_call_id: str,
        _tool_name: str,
    ) -> None:
        return None

    def tool_completed(
        self,
        _thread_id: str,
        _tool_call_id: str,
        _tool_name: str,
    ) -> None:
        return None

    def tool_recoverable_failure(
        self,
        _thread_id: str,
        _tool_call_id: str,
    ) -> None:
        return None


NULL_ACTIVITY_SINK = NullActivitySink()


def notify_activity(sink: ActivitySink, operation: str, *args: Any) -> None:
    try:
        getattr(sink, operation)(*args)
    except Exception:
        logger.exception("Agent activity notification failed", extra={"operation": operation})
```

The protocol ellipses are Python protocol method bodies, not deferred implementation.

- [ ] **Step 4: Add exception-safe notifications to model boundaries**

Add `activity_sink: ActivitySink = NULL_ACTIVITY_SINK` to `EpiAgentRuntimeConfig`. In both `_call_model` and `_acall_model`, resolve the thread ID from `config["configurable"]["thread_id"]`, call `notify_activity(..., "model_started", thread_id)` immediately before the provider call, and call `model_completed` only after `_model_answer_patch` has been built successfully. Do not send messages, prompts, budgets, answers, durations, or exceptions to the sink.

- [ ] **Step 5: Add exception-safe notifications to the central tool loop**

In `_execute_tools`, first validate the name with `registry.spec(name)`. Emit `tool_started` immediately before `registry.invoke` only when that registered spec lookup succeeds; an unknown model-invented name must never become public activity. On `ToolExecutionError(recoverable=True)`, emit `tool_recoverable_failure`; on success, emit `tool_completed` before reducing the result. Do not emit a public failure for a nonrecoverable error; Task 4 will close the overall run through its existing run-level error state. Leave `GraphInterrupt` and `RunCancelled` behavior unchanged. Add a test proving an unknown tool call creates its normal internal tool error but sends no activity notification.

- [ ] **Step 6: Thread the sink through graph construction**

Add an optional `activity_sink: ActivitySink = NULL_ACTIVITY_SINK` parameter to `build_graph` and `build_general_epi_agent_graph`, and assign it in `EpiAgentRuntimeConfig`. Update `tests/test_epi_agent_root_graph.py` with a recording sink and assert one direct-answer model start/completion pair for `thread-1`.

- [ ] **Step 7: Run graph instrumentation tests**

Run: `.venv/bin/python -m pytest tests/test_epi_agent_runtime.py tests/test_epi_agent_root_graph.py -q`

Expected: all tests pass, including sync model, async model, successful tool, recoverable failure, and default no-op behavior.

- [ ] **Step 8: Commit central instrumentation**

```bash
git add epi_agent/activity.py epi_agent/runtime.py epi_agent/agent.py graph/builder.py tests/test_epi_agent_runtime.py tests/test_epi_agent_root_graph.py
git commit -m "feat: record central model and tool activity"
```

---

### Task 4: Wire activity into API request and conversation lifecycles

**Files:**
- Create: `tests/test_api_activity_timeline.py`
- Modify: `api/app.py`
- Modify: `api/runtime.py`
- Test: `tests/test_active_run_cancellation.py`
- Test: `tests/test_api_review_contract.py`

**Interfaces:**
- Consumes: `SqliteActivityStore` and `ActivityRun` from Tasks 1–2.
- Extends: `ReportAgentApiRuntime.activity_store: SqliteActivityStore | None`.
- Extends: `project_thread_state(..., activity_runs: list[ActivityRun] | None = None)`.
- Produces: durable start, review, resume, recovery, cancellation, terminal-state, and deletion reconciliation.

- [ ] **Step 1: Write failing API lifecycle tests**

Create `tests/test_api_activity_timeline.py` using the existing fake compiled-app style from `tests/test_active_run_cancellation.py`. Cover these exact contracts:

```python
def test_first_durable_user_turn_starts_activity_with_conversation_event_id(...):
    runtime.submit_message("thread-1", "Create a cohort dataset")
    state = wait_for_non_running_state(runtime, "thread-1")
    user_message = next(item for item in state.conversation if item.role == "user")
    assert state.activity_runs[0].user_message_id == user_message.id


def test_state_marks_projected_interrupt_waiting_and_resume_reuses_run(...):
    waiting = runtime.state("thread-1")
    assert waiting.activity_runs[0].state == "waiting"
    runtime.resume_interrupt("thread-1", waiting.active_interrupt.id, {"action": "approve"})
    resumed = runtime.state("thread-1")
    assert len(resumed.activity_runs) == 1


def test_cancel_hides_running_item_and_closes_activity_run(...):
    cancelled = runtime.cancel_run("thread-1")
    assert cancelled.activity_runs[0].state == "cancelled"
    assert all(item.status != "running" for item in cancelled.activity_runs[0].activities)


def test_delete_conversation_deletes_activity_but_archive_retains_it(...):
    runtime.archive_conversation("thread-1")
    assert activity_store.list_runs("thread-1")
    runtime.restore_conversation("thread-1")
    assert runtime.delete_conversation("thread-1") is True
    assert activity_store.list_runs("thread-1") == []
```

Also inject a store whose methods raise `sqlite3.OperationalError` and prove message submission, graph completion, and `runtime.state` still succeed with `activity_runs=[]`.

- [ ] **Step 2: Run the API tests and confirm absent lifecycle wiring**

Run: `.venv/bin/python -m pytest tests/test_api_activity_timeline.py -q`

Expected: tests fail because `ReportAgentApiRuntime` does not accept or project an activity store.

- [ ] **Step 3: Construct and share one activity store in `api/app.py`**

Create `activity_store = SqliteActivityStore(db_path)` after resolving `db_path`. Pass it to both:

```python
return build_graph(
    llm,
    model_profile=profile,
    db_path=db_path,
    runtime_root=runtime_root_path,
    studies=studies,
    default_study_id=default_study_id,
    db_rag_readiness=db_rag_readiness,
    db_rag_embedding_model=db_rag_embedding_model,
    max_iterations=max_iterations,
    activity_sink=activity_store,
)
```

and `ReportAgentApiRuntime(activity_store=activity_store, ...)`. If store construction fails, log the exception, set `activity_store=None`, and pass the no-op sink to the graph so application startup continues.

- [ ] **Step 4: Start activity only after the first user turn is durable**

Extend `_accept_initial_turn` after its checkpoint verification. Find the conversation `user` event whose `user_turn_hash` equals `turn_hash`, then call through a new `_activity_call(operation, *args)` helper:

```python
user_event_id = next(
    str(event["event_id"])
    for event in reversed(list(dict(values.get("artifacts") or {}).get("conversation_events") or []))
    if isinstance(event, dict)
    and event.get("type") == "user"
    and event.get("user_turn_hash") == turn_hash
)
self._activity_call("start_run", thread_id, user_event_id)
```

`_activity_call` catches and logs every exception. This positioning ensures rejected or non-checkpointed initial turns never leave orphan activity runs and ensures the timeline key equals the `ConversationMessage.id` rendered by the API.

- [ ] **Step 5: Reconcile state, interrupts, resumes, recovery, and terminal outcomes**

Add `_activity_runs(thread_id) -> list[ActivityRun]`, returning `[]` on any exception. In `state(thread_id)`:

1. Before `runner.start_background` recovery, call `recover(thread_id)`.
2. Project the existing graph/run state.
3. If `active_interrupt` exists, call `mark_waiting(thread_id, active_interrupt.type)`.
4. Map public run states `done -> completed`, `cancelled -> cancelled`, and `error|timeout -> error`, then call `finish`.
5. Re-read activity runs and assign them through `project_thread_state(activity_runs=...)` so the response contains the reconciled state.

In `resume_interrupt`, retain `active_interrupt.type`, start the durable resume exactly as today, and after a successful start call `resume(thread_id, interrupt_type)`. In `delete_conversation`, call `delete_thread` after checkpoint and attachment cleanup but before removing the history record. Archive and restore must not call the activity store.

- [ ] **Step 6: Extend pure thread-state projection**

Change the signature and return statement without altering existing diagnostics:

```python
def project_thread_state(
    *,
    thread_id: str,
    snapshot: Any,
    run_status: dict[str, Any],
    runtime_settings: RuntimeSettings | None = None,
    runtime_settings_locked: bool = False,
    activity_runs: list[ActivityRun] | None = None,
) -> ApiThreadState:
    ...
    return ApiThreadState(
        thread_id=thread_id,
        run=status,
        conversation=_conversation(values),
        activity_runs=list(activity_runs or []),
        ...
    )
```

- [ ] **Step 7: Run API, review, and cancellation regression tests**

Run: `.venv/bin/python -m pytest tests/test_api_activity_timeline.py tests/test_api_root_projection.py tests/test_api_review_contract.py tests/test_active_run_cancellation.py -q`

Expected: all tests pass; cancellation remains durable and late model/tool results remain ignored.

- [ ] **Step 8: Commit API lifecycle integration**

```bash
git add api/app.py api/runtime.py tests/test_api_activity_timeline.py tests/test_api_root_projection.py tests/test_api_review_contract.py tests/test_active_run_cancellation.py
git commit -m "feat: expose durable activity through thread state"
```

---

### Task 5: Build the accessible timeline component

**Files:**
- Create: `frontend/src/AgentActivityTimeline.tsx`
- Create: `frontend/src/AgentActivityTimeline.test.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/apiClient.test.ts`

**Interfaces:**
- Consumes: snake-case JSON fields from `ApiThreadState.activity_runs`.
- Produces: TypeScript `ActivityItem`, `ActivityRun`, `ActivityItemStatus`, and `ActivityRunState`.
- Produces: `AgentActivityTimeline({ run }: { run: ActivityRun })` rendered as one `<li>`.

- [ ] **Step 1: Add failing component tests**

Create a completed run fixture containing two `Searching the data catalog` calls and one waiting fixture. Assert:

```tsx
render(<AgentActivityTimeline run={completedRun} />);
expect(screen.getByRole("button", { name: "View agent activity · 2 activities" }))
  .toHaveAttribute("aria-expanded", "false");
expect(screen.queryByText("dbrag-search_catalog")).not.toBeInTheDocument();
fireEvent.click(screen.getByRole("button", { name: /View agent activity/ }));
expect(screen.getAllByText("Searching the data catalog")).toHaveLength(2);
expect(screen.getAllByText("dbrag-search_catalog")).toHaveLength(2);
```

For a waiting run, assert it starts expanded, contains an `aria-live="polite"` region with `Waiting for dataset approval`, and has no failure icon or `failed` text.

After expanding the completed fixture, also assert the two repeated technical entries show `Call 1` and `Call 2`; occurrence numbers are calculated within one run for activities sharing the same `tool_name`.

- [ ] **Step 2: Run the component test and confirm the missing-component failure**

Run: `npm --prefix frontend test -- AgentActivityTimeline.test.tsx`

Expected: the suite fails because `AgentActivityTimeline.tsx` does not exist.

- [ ] **Step 3: Mirror the API types in `frontend/src/types.ts`**

Add:

```typescript
export type ActivityItemStatus = "running" | "completed" | "waiting";
export type ActivityRunState =
  | "running"
  | "waiting"
  | "completed"
  | "cancelled"
  | "error";

export interface ActivityItem {
  id: string;
  sequence: number;
  label: string;
  status: ActivityItemStatus;
  tool_name: string | null;
  tool_call_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ActivityRun {
  id: string;
  thread_id: string;
  user_message_id: string;
  state: ActivityRunState;
  activities: ActivityItem[];
  created_at: string;
  updated_at: string;
}
```

Add `activity_runs: ActivityRun[]` to `ApiThreadState` and every shared test fixture. Extend `frontend/src/apiClient.test.ts` with one activity run in `threadState` and assert `getThreadState` returns it unchanged.

- [ ] **Step 4: Implement `AgentActivityTimeline`**

Use component state initialized from `run.state`, expand automatically when state becomes `running` or `waiting`, and collapse on a transition to a terminal state. Render:

- a summary button with `aria-expanded` and visible activity count;
- an ordered list sorted by `sequence`;
- `✓` plus completed text, a spinner plus running text, and `⚠` plus waiting text;
- a polite live region containing only the newest running/waiting label; and
- technical `<code>` details only while the run is expanded and `tool_name` is non-null.

The component must contain no branch for a failed item because `ActivityItemStatus` has no failure state.

- [ ] **Step 5: Run timeline and API-client tests**

Run: `npm --prefix frontend test -- AgentActivityTimeline.test.tsx apiClient.test.ts`

Expected: all tests pass.

- [ ] **Step 6: Commit the standalone timeline**

```bash
git add frontend/src/AgentActivityTimeline.tsx frontend/src/AgentActivityTimeline.test.tsx frontend/src/types.ts frontend/src/apiClient.test.ts
git commit -m "feat: add agent activity timeline component"
```

---

### Task 6: Integrate timelines with conversation polling and review UI

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `state.activity_runs` and `AgentActivityTimeline` from Task 5.
- Preserves: current message submission, one-second polling, review rendering, cancellation, and generic activity fallback.

- [ ] **Step 1: Add failing application placement and polling tests**

Extend the `threadState` helper to default `activity_runs: []`. Add tests for:

1. A running timeline appears immediately after its matching user message.
2. A later poll changes one item from `running` to `completed` without duplicating it.
3. Two repeated successful calls render as two rows.
4. A waiting timeline is the final item inside the message list and its review card follows the message list.
5. A completed historical timeline is collapsed when a conversation is reopened.
6. An active run with `activity_runs=[]` still displays the existing generic `Agent is working` card.
7. A cancelled activity run is collapsed and has no running spinner.

Use `within(messageList).getAllByText(...)` and DOM child order assertions rather than snapshot tests.

- [ ] **Step 2: Run focused App tests and confirm they fail**

Run: `npm --prefix frontend test -- App.test.tsx`

Expected: new assertions fail because App still renders only the generic activity card at the end of the conversation.

- [ ] **Step 3: Place timelines by user-message ID**

Import `Fragment` and `AgentActivityTimeline`. Build a memoized map:

```typescript
const activityRunByUserMessageId = useMemo(
  () => new Map((state?.activity_runs ?? []).map((run) => [run.user_message_id, run])),
  [state?.activity_runs],
);
```

Render each conversation message inside a keyed `Fragment`; directly after a user `ConversationMessage`, render its matching `AgentActivityTimeline`. Keep the existing generic `ActivityMessage` only when `run.state === "running"` and no visible activity run matches the active conversation turn. Do not change the polling effect or interrupt components.

- [ ] **Step 4: Add focused timeline styling**

Add `.agent-activity-timeline`, `.agent-activity-summary`, `.agent-activity-list`, `.agent-activity-item`, `.agent-activity-status`, `.agent-activity-tool`, and running/waiting/completed modifiers. Reuse the existing spinner animation. Ensure the component fits the assistant-message width, wraps long labels/tool names, uses text in addition to color, and remains readable at the existing mobile breakpoint.

- [ ] **Step 5: Run frontend component and application tests**

Run: `npm --prefix frontend test -- AgentActivityTimeline.test.tsx App.test.tsx apiClient.test.ts`

Expected: all focused tests pass, including existing cancellation and human-review UI tests.

- [ ] **Step 6: Commit frontend integration**

```bash
git add frontend/src/App.tsx frontend/src/App.test.tsx frontend/src/styles.css
git commit -m "feat: show activity timelines in conversations"
```

---

### Task 7: Add the real feature smoke and verify delivery

**Files:**
- Create: `scripts/e2e_agent_activity_timeline_real.py`
- Modify: tracked files under `frontend/dist/`
- Modify: `frontend/dist/build-manifest.json`

**Interfaces:**
- Consumes: the real `api.app:app`, production `frontend/dist` bundle, configured OpenAI model, installed study package, DB-RAG assets, and Playwright browser.
- Produces: one executable, maximum-five-minute smoke with preserved logs, page text, raw API state, traceback, and screenshot on failure.

- [ ] **Step 1: Create the dedicated real smoke script**

Follow the process-management and diagnostic pattern in `scripts/e2e_fastapi_typescript_db_rag_real.py`, but launch only FastAPI, set `REPORT_AGENT_STATIC_DIR` to `frontend/dist`, open the FastAPI origin in the browser, and stop at the first real dataset-plan review. Use the same default loss-to-follow-up query and assert all of the following before exiting:

```python
timeline = page.get_by_label("Agent activity timeline")
timeline.wait_for(state="visible", timeout=_remaining_ms(deadline))
page.get_by_text("Searching the data catalog", exact=True).wait_for(
    timeout=_remaining_ms(deadline)
)
page.get_by_text("Waiting for dataset plan review", exact=True).wait_for(
    timeout=_remaining_ms(deadline)
)
page.get_by_role("button", name="Approve final selection").wait_for(
    timeout=_remaining_ms(deadline)
)
```

Expand the activity disclosure and assert at least one visible technical name starts with `dbrag-`. Read the active thread ID from the browser's latest `/api/threads/{thread_id}/state` response using a Playwright `page.on("response", ...)` listener, GET that exact state from `api_url`, and assert:

```python
assert raw_state["activity_runs"]
assert raw_state["activity_runs"][-1]["state"] == "waiting"
assert any(
    item["tool_name"] and item["tool_name"].startswith("dbrag-")
    for item in raw_state["activity_runs"][-1]["activities"]
)
assert not any(item.get("status") == "failed" for item in raw_state["activity_runs"][-1]["activities"])
```

On failure, preserve `api.log`, `failure-page-text.txt`, `failure-page.html`, `failure-screenshot.png`, `failure-api-state.json` when available, and `failure-traceback.txt`. Make the file executable.

- [ ] **Step 2: Run all deterministic Python and frontend tests before the real smoke**

Run: `.venv/bin/python -m pytest tests/test_activity_labels.py tests/test_activity_store.py tests/test_api_activity_timeline.py tests/test_epi_agent_runtime.py tests/test_epi_agent_root_graph.py tests/test_api_root_projection.py tests/test_api_review_contract.py tests/test_active_run_cancellation.py -q`

Expected: all selected Python tests pass.

Run: `npm --prefix frontend test`

Expected: all Vitest suites pass.

- [ ] **Step 3: Build the production frontend and refresh its manifest**

Run: `npm --prefix frontend run build`

Expected: TypeScript compilation and Vite production build succeed.

Run: `.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest`

Expected: `frontend/dist/build-manifest.json` is refreshed for the newly built assets.

- [ ] **Step 4: Run the dedicated real smoke exactly once**

Run: `.venv/bin/python scripts/e2e_agent_activity_timeline_real.py --timeout-seconds 300 --artifact-dir /tmp/report-agent-activity-smoke`

Expected: `PASS agent activity timeline real browser smoke` within five minutes. If it fails or times out, do not rerun automatically; report the preserved diagnostic paths.

- [ ] **Step 5: Run final repository verification**

Run: `.venv/bin/python -m pytest -q`

Expected: the complete Python suite passes.

Run: `npm --prefix frontend test`

Expected: the complete frontend suite passes.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 6: Commit smoke and delivery artifacts**

```bash
git add scripts/e2e_agent_activity_timeline_real.py frontend/dist
git commit -m "test: verify real agent activity timeline"
```

## Completion Criteria

- A real database request shows a durable, ordered activity timeline through existing polling.
- Repeated successful tool calls remain separate and identifiable by technical name after expansion.
- Recoverable failed attempts never expose a public error state or raw details.
- Dataset-plan, dataset, analysis, clarification, and output-limit interruptions show a waiting activity and resume the original timeline.
- Completed, cancelled, and errored runs contain no visible running activity.
- Refresh, process recreation, archive, restore, cancellation, and conversation deletion follow the approved lifecycle contract.
- Missing or failing activity storage falls back to the existing generic working card without changing agent behavior.
- All focused tests, full suites, production build, manifest verification, and the one allowed real smoke have the required evidence.
