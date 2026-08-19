# Multi-Study Review and Tool-Failure Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make dataset-plan review resolve the plan's exact study and guarantee that a failed tool turn stays visible without corrupting later turns in the same conversation.

**Architecture:** Resolve `DatasetPlan.study_id` at the review boundary and pass a `StudyBundle` into join-path discovery. Centralize generic tool-error construction and orphaned-call repair in a small protocol module; the executor closes every emitted tool call before terminating, while the API replaces malformed message history atomically before appending a follow-up. Verify both paths through focused tests and one real FastAPI/compiled-TypeScript/OpenAI smoke with two installed studies.

**Tech Stack:** Python 3.12, LangChain messages, LangGraph `add_messages`/SQLite checkpoints, Pydantic, pytest, FastAPI, React/TypeScript production bundle, Playwright, OpenAI, DuckDB, RePORT India and NHANES study packages.

## Global Constraints

- Run Python commands with `.venv/bin/python`; never use the system `python3`.
- Follow red-green-refactor for every production behavior change.
- Do not expose Python exception types, tracebacks, paths, secrets, or raw exception text outside server logs.
- Preserve `GraphInterrupt` and `RunCancelled` control flow exactly.
- A terminal checkpoint containing an assistant tool-call message must contain one matching `ToolMessage` for every call ID.
- A failed turn remains visible; a later user turn clears only the current terminal status and continues in the same thread.
- Active human-review interrupts remain blocked by `ThreadAwaitingReviewError` and are never repaired as failures.
- The dedicated feature smoke must use real FastAPI, the compiled TypeScript frontend, OpenAI, LangGraph, DB-RAG, embeddings, and real study packages; it runs once with a hard five-minute maximum and preserves diagnostics on failure.
- Rebuild `frontend/dist` and refresh its build manifest only if a frontend source or build input changes.

---

## File Map

- Modify `epi_agent/db_rag/reviews.py`: resolve the plan study before join-path discovery.
- Modify `tests/test_db_rag_agent_reviews.py`: repair stale multi-study fixtures and cover exact-study review behavior.
- Create `epi_agent/tool_call_protocol.py`: own generic internal tool failures, error `ToolMessage` construction, batch closing, orphan detection, and atomic follow-up message patches.
- Modify `epi_agent/runtime.py`: convert unexpected registered-tool exceptions into terminal protocol-safe results and close unexecuted calls.
- Modify `tests/test_epi_agent_runtime.py`: cover unexpected failures, terminal batches, cancellation, and interrupt behavior.
- Modify `api/runtime.py`: run the defensive message repair while binding a later `HumanMessage`.
- Modify `tests/test_api_runtime.py`: cover repair ordering, idempotence, interrupt blocking, and durable follow-up continuation.
- Create `scripts/smoke_multi_study_review_failure_recovery_real.py`: exercise both accepted production paths through the browser and raw API/checkpoint state.
- Modify `README.md`: distinguish catalog-only smoke coverage and document the new feature smoke.

---

### Task 1: Restore exact-study dataset-plan review

**Files:**
- Modify: `epi_agent/db_rag/reviews.py:17-31,465-590`
- Modify: `tests/test_db_rag_agent_reviews.py:20-230`

**Interfaces:**
- Consumes: `require_context_study(context: ToolContext, study_id: str) -> StudyBundle` and `_verified_join_paths(plan: DatasetPlan, study: StudyBundle) -> list[dict[str, Any]]`.
- Produces: `_data_linkage_payload(plan: DatasetPlan, context: ToolContext) -> dict[str, Any]` that propagates structured study-resolution errors and never passes `ToolContext` as a study.

- [ ] **Step 1: Repair the stale review fixture and add failing exact-study tests**

Update the imports and shared builders in `tests/test_db_rag_agent_reviews.py` so every plan and context uses the current multi-study contracts. Leave the existing `catalog = SchemaCatalog` construction unchanged, then replace the obsolete singular-study return block with:

```python
from epi_agent.studies import StudyBundle, StudyRegistry

report = StudyBundle(
    study_id="report",
    label="RePORT",
    knowledge=object(),
    catalog=catalog,
    data_sources={
        "report_duckdb": DuckDbStudyDataSource(_REVIEW_DB_PATH),
        "source_a": DuckDbStudyDataSource(_REVIEW_DB_PATH),
        "source_b": DuckDbStudyDataSource(_REVIEW_DB_PATH),
    },
)
studies = [report]
if include_second_study:
    studies.append(
        StudyBundle(
            study_id="other-study",
            label="Other Study",
            knowledge=object(),
            catalog=SchemaCatalog({"tables": [], "columns": []}),
            data_sources={"other_source": object()},
        )
    )
return ToolContext(
    studies=StudyRegistry(studies),
    artifact_store=StateArtifactStore.from_state(_state()),
    thread_id="thread-1",
    policy=object(),
)
```

Also change the function signature to `def _context(*, include_second_study: bool = False) -> ToolContext:`.

Add `study_id="report"` to `_plan()` and every directly constructed `DatasetPlan` in this file. Then add these boundary tests:

```python
def test_plan_review_resolves_join_paths_from_plan_study_in_multi_study_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(include_second_study=True)
    observed: list[str] = []

    def verified_join_paths(plan: DatasetPlan, study: StudyBundle):
        observed.append(study.study_id)
        assert plan.study_id == "report"
        assert "report_duckdb" in study.data_sources
        assert "other_source" not in study.data_sources
        return []

    monkeypatch.setattr(
        "epi_agent.db_rag.tools._verified_join_paths",
        verified_join_paths,
    )

    view = _plan_review_view(
        _plan(),
        context=context,
        plan_id="plan-1",
        version=1,
    )

    assert observed == ["report"]
    assert view["data_linkage"] == {"relationships": []}


def test_plan_review_rejects_unavailable_plan_study() -> None:
    plan = _plan().model_copy(update={"study_id": "missing-study"})

    with pytest.raises(ToolExecutionError) as raised:
        _plan_review_view(
            plan,
            context=_context(include_second_study=True),
            plan_id="plan-1",
            version=1,
        )

    assert raised.value.code == "STUDY_NOT_AVAILABLE"
    assert raised.value.recoverable is True
```

In `test_native_langgraph_resume_writes_only_after_interrupt`, replace the remaining singular context construction with:

```python
context = ToolContext(
    studies=base_context.studies,
    artifact_store=store,
    thread_id="thread-native",
    policy=base_context.policy,
)
```

- [ ] **Step 2: Run the two tests and observe the production boundary failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_db_rag_agent_reviews.py::test_plan_review_resolves_join_paths_from_plan_study_in_multi_study_context \
  tests/test_db_rag_agent_reviews.py::test_plan_review_rejects_unavailable_plan_study -v
```

Expected: the exact-study test fails because `_verified_join_paths` receives `ToolContext`; the unavailable-study test fails because the review path does not resolve `plan.study_id`.

- [ ] **Step 3: Resolve the plan study before calling `_verified_join_paths`**

Add `require_context_study` to the `epi_agent.protocol` imports and make these exact edits to `_data_linkage_payload`:

```python
from epi_agent.protocol import (
    ArtifactRef,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    ToolTerminalControl,
    require_context_study,
)


@@
 def _data_linkage_payload(
     plan: DatasetPlan,
     context: ToolContext,
 ) -> dict[str, Any]:
+    study = require_context_study(context, plan.study_id)
     relationships: list[dict[str, Any]] = []
@@
-        for edge in _verified_join_paths(plan, context):
+        for edge in _verified_join_paths(plan, study):
@@
-    except (ToolExecutionError, KeyError, ValueError):
+    except (KeyError, ValueError):
         pass
```

Keep the explicit-operation loop and edge-to-review-payload conversion byte-for-byte unchanged. The diff deliberately propagates `STUDY_NOT_AVAILABLE` instead of treating it as absent optional relationship evidence.

- [ ] **Step 4: Run the focused review suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_db_rag_agent_reviews.py -q
```

Expected: all review tests pass, including the two-study and unavailable-study cases.

- [ ] **Step 5: Commit the exact-study review fix**

```bash
git add epi_agent/db_rag/reviews.py tests/test_db_rag_agent_reviews.py
git commit -m "fix: scope dataset plan review to its study"
```

---

### Task 2: Make every terminal tool failure provider-valid

**Files:**
- Create: `epi_agent/tool_call_protocol.py`
- Modify: `epi_agent/runtime.py:1-35,122-160,827-1135`
- Modify: `tests/test_epi_agent_runtime.py:1-180,980-1160`

**Interfaces:**
- Produces: `internal_tool_error() -> ToolExecutionError`, `error_tool_message(call: Mapping[str, Any], error: ToolExecutionError | None = None) -> ToolMessage`, and `aborted_tool_messages(calls: Sequence[Mapping[str, Any]]) -> list[ToolMessage]`.
- Preserves: `_tool_error_content(error: ToolExecutionError) -> str` as an import alias from `epi_agent.runtime` so existing callers and tests do not break.

- [ ] **Step 1: Add failing executor tests for unexpected and batched terminal failures**

Add these tools and tests to `tests/test_epi_agent_runtime.py`. Mechanically replace all twelve obsolete singular-study context arguments in this file with `studies=studies`; where a lambda currently reads `state["active_study_id"]` only for that argument, rename the unused lambda parameter to `_state`.

Add these imports for the new control-flow tests:

```python
import pytest

from utils.run_cancellation import (
    CancellationToken,
    RunCancelled,
    bind_cancellation,
)
```

```python
@dataclass(frozen=True)
class UnexpectedFailureTool:
    spec = ToolSpec(
        name="unexpected_failure",
        description="Raise an unexpected exception.",
        args_model=EmptyArguments,
        read_only=True,
    )

    def invoke(self, _arguments, _context):
        raise AttributeError("private implementation detail")


@dataclass(frozen=True)
class TerminalFailureTool:
    spec = ToolSpec(
        name="terminal_failure",
        description="Raise a terminal structured failure.",
        args_model=EmptyArguments,
        read_only=True,
    )

    def invoke(self, _arguments, _context):
        raise ToolExecutionError(
            "EXPECTED_TERMINAL_FAILURE",
            "The request cannot continue.",
            recoverable=False,
        )


def _tool_state(*calls: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": [AIMessage(content="", tool_calls=list(calls))],
        "artifacts": {},
    }


def _call(name: str, call_id: str) -> dict[str, Any]:
    return {
        "name": name,
        "args": {},
        "id": call_id,
        "type": "tool_call",
    }


def test_unexpected_tool_exception_closes_call_and_stops_run(caplog) -> None:
    config = _runtime_config(ToolRegistry([UnexpectedFailureTool()]))

    patch = _execute_tools(
        _tool_state(_call("unexpected_failure", "call-1")),
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=config,
    )

    assert patch["terminal_error"] == {
        "code": "INTERNAL_TOOL_ERROR",
        "message": "A tool failed unexpectedly. This request was stopped, but you can continue the conversation.",
        "recoverable": False,
    }
    assert [message.tool_call_id for message in patch["messages"]] == ["call-1"]
    assert patch["messages"][0].status == "error"
    public_payload = json.loads(str(patch["messages"][0].content))
    assert public_payload["error"]["code"] == "INTERNAL_TOOL_ERROR"
    assert "AttributeError" not in json.dumps(public_payload)
    assert "private implementation detail" not in json.dumps(public_payload)
    assert "private implementation detail" in caplog.text


def test_unexpected_failure_closes_remaining_read_only_batch() -> None:
    registry = ToolRegistry(
        [FunctionTool(_read_only_spec("successful_tool")), UnexpectedFailureTool(), FunctionTool(_read_only_spec("never_run"))]
    )

    patch = _execute_tools(
        _tool_state(
            _call("successful_tool", "call-1"),
            _call("unexpected_failure", "call-2"),
            _call("never_run", "call-3"),
        ),
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=_runtime_config(registry),
    )

    assert [message.tool_call_id for message in patch["messages"]] == [
        "call-1", "call-2", "call-3"
    ]
    assert [message.status for message in patch["messages"]] == [
        "success", "error", "error"
    ]
    assert json.loads(str(patch["messages"][2].content))["error"]["code"] == "INTERNAL_TOOL_ERROR"


def test_nonrecoverable_tool_error_closes_remaining_read_only_batch() -> None:
    registry = ToolRegistry([TerminalFailureTool(), FunctionTool(_read_only_spec("never_run"))])

    patch = _execute_tools(
        _tool_state(
            _call("terminal_failure", "call-1"),
            _call("never_run", "call-2"),
        ),
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=_runtime_config(registry),
    )

    assert [message.tool_call_id for message in patch["messages"]] == ["call-1", "call-2"]
    assert json.loads(str(patch["messages"][0].content))["error"]["code"] == "EXPECTED_TERMINAL_FAILURE"
    assert json.loads(str(patch["messages"][1].content))["error"]["code"] == "INTERNAL_TOOL_ERROR"
```

Add a graph-level terminal-routing assertion so the executor cannot accidentally call the model again after writing the generic failure:

```python
def test_unexpected_tool_failure_does_not_request_model_continuation() -> None:
    model = _SingleToolCallModel("unexpected_failure")
    graph = build_epi_agent_graph(
        state_schema=GenericEpiAgentState,
        model=model,
        config=_runtime_config(ToolRegistry([UnexpectedFailureTool()])),
    )

    result = graph.invoke(
        _initial_runtime_state("Trigger the failing tool"),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert model.calls == 1
    assert result["terminal_error"]["code"] == "INTERNAL_TOOL_ERROR"
    assert result["messages"][-1].tool_call_id == "call-1"
```

Implement `_read_only_spec` and `_runtime_config` as small test helpers using the existing `EmptyArguments`, `_studies()`, and `EpiAgentRuntimeConfig`; their exact returned objects are:

```python
def _read_only_spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        args_model=EmptyArguments,
        read_only=True,
    )


def _runtime_config(registry: ToolRegistry) -> EpiAgentRuntimeConfig:
    studies = _studies()
    return EpiAgentRuntimeConfig(
        model_profile=model_runtime_profile("gpt-5.4"),
        agent_name="test_agent",
        system_prompt="Use tools.",
        registry=registry,
        studies=studies,
        context_factory=lambda _state, _config, artifact_store: ToolContext(
            studies=studies,
            artifact_store=artifact_store,
            thread_id="thread-1",
            policy=None,
        ),
    )
```

- [ ] **Step 2: Run the new executor tests and observe the uncaught exception/incomplete batch**

Run:

```bash
.venv/bin/python -m pytest tests/test_epi_agent_runtime.py \
  -k "unexpected_tool_exception or closes_remaining_read_only_batch" -v
```

Expected: unexpected `AttributeError` escapes `_execute_tools`, and the structured terminal batch lacks the unexecuted call's result.

- [ ] **Step 3: Create the generic tool-call protocol primitives**

Create `epi_agent/tool_call_protocol.py` with this implementation:

```python
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import ToolMessage

from epi_agent.protocol import ToolExecutionError


INTERNAL_TOOL_ERROR_CODE = "INTERNAL_TOOL_ERROR"
INTERNAL_TOOL_ERROR_MESSAGE = (
    "A tool failed unexpectedly. This request was stopped, but you can "
    "continue the conversation."
)


def tool_error_content(error: ToolExecutionError) -> str:
    payload: dict[str, Any] = {
        "code": error.code,
        "message": str(error),
        "recoverable": error.recoverable,
    }
    if error.details is not None:
        payload["details"] = error.details
    return json.dumps({"error": payload}, sort_keys=True)


def internal_tool_error() -> ToolExecutionError:
    return ToolExecutionError(
        INTERNAL_TOOL_ERROR_CODE,
        INTERNAL_TOOL_ERROR_MESSAGE,
        recoverable=False,
    )


def error_tool_message(
    call: Mapping[str, Any],
    error: ToolExecutionError | None = None,
) -> ToolMessage:
    selected_error = error or internal_tool_error()
    return ToolMessage(
        content=tool_error_content(selected_error),
        tool_call_id=str(call["id"]),
        name=str(call.get("name") or ""),
        status="error",
    )


def aborted_tool_messages(
    calls: Sequence[Mapping[str, Any]],
) -> list[ToolMessage]:
    return [error_tool_message(call) for call in calls]
```

- [ ] **Step 4: Catch unexpected exceptions without catching interrupts or cancellation**

In `epi_agent/runtime.py`, import `RunCancelled` and the new helpers, retaining the old private serialization name as an alias:

```python
from epi_agent.tool_call_protocol import (
    aborted_tool_messages,
    error_tool_message,
    internal_tool_error,
    tool_error_content as _tool_error_content,
)
from utils.run_cancellation import RunCancelled, cancellation_point
```

Delete the old local `_tool_error_content`. Enumerate calls so terminal paths can close the suffix, then apply these exact exception-path edits while leaving the invoke and success blocks unchanged:

```python
@@
-    for call in calls:
+    for call_index, call in enumerate(calls):
@@
-        except GraphInterrupt:
+        except (GraphInterrupt, RunCancelled):
             raise
@@
-            messages.append(
-                ToolMessage(
-                    content=_tool_error_content(error),
-                    tool_call_id=call["id"],
-                    name=name,
-                    status="error",
-                )
-            )
+            messages.append(error_tool_message(call, error))
             failures.append(_failure_signature(error.code, name, arguments))
             if not error.recoverable:
+                messages.extend(
+                    aborted_tool_messages(calls[call_index + 1 :])
+                )
                 terminal_error = _terminal_error(error.code, str(error))
                 break
@@
             continue
+        except Exception:
+            _LOGGER.exception(
+                "Unexpected registered tool failure",
+                extra={
+                    "tool_name": name,
+                    "tool_call_id": str(call["id"]),
+                    "thread_id": thread_id,
+                },
+            )
+            error = internal_tool_error()
+            messages.append(error_tool_message(call, error))
+            messages.extend(
+                aborted_tool_messages(calls[call_index + 1 :])
+            )
+            failures.append(_failure_signature(error.code, name, arguments))
+            terminal_error = _terminal_error(error.code, str(error))
+            break
```

Retain the complete existing success path. Replace direct terminal `ToolMessage` construction in the SQL-repair-exhausted branch with `error_tool_message(call, error)` and close the suffix there as well.

- [ ] **Step 5: Add explicit control-flow regression tests**

Add two tests that bind cancellation and raise a real LangGraph interrupt from tool invocation:

```python
def test_tool_executor_propagates_run_cancellation() -> None:
    token = CancellationToken()
    token.cancel()
    with bind_cancellation(token), pytest.raises(RunCancelled):
        _execute_tools(
            _tool_state(_call("successful_tool", "call-1")),
            {"configurable": {"thread_id": "thread-1"}},
            agent_config=_runtime_config(ToolRegistry([FunctionTool(_read_only_spec("successful_tool"))])),
        )


def test_tool_executor_propagates_graph_interrupt() -> None:
    @dataclass(frozen=True)
    class InterruptingTool:
        spec = _read_only_spec("interrupting_tool")

        def invoke(self, _arguments, _context):
            from langgraph.types import interrupt
            return interrupt({"type": "test_review"})

    graph = build_epi_agent_graph(
        state_schema=GenericEpiAgentState,
        model=_SingleToolCallModel("interrupting_tool"),
        config=_runtime_config(ToolRegistry([InterruptingTool()])),
        checkpointer=InMemorySaver(),
    )
    result = graph.invoke(_initial_runtime_state("run interrupt"), {"configurable": {"thread_id": "thread-1"}})
    snapshot = graph.get_state({"configurable": {"thread_id": "thread-1"}})
    assert snapshot.interrupts
    assert result.get("terminal_error") is None
```

Define the two referenced test helpers exactly as follows:

```python
class _SingleToolCallModel:
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0

    def bind_tools(self, _schemas: list[dict[str, Any]]):
        return self

    def invoke(self, _messages: list[Any], *, config: dict[str, Any]):
        self.calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.tool_name,
                    "args": {},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        )


def _initial_runtime_state(text: str) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content=text)],
        "artifact_ids": [],
        "artifacts": {},
        "final_response": None,
        "iteration_count": 0,
        "failure_signatures": [],
        "current_turn_artifact_refs": [],
        "current_turn_output_artifact_refs": [],
        "analysis_review_feedback_history": [],
    }
```

- [ ] **Step 6: Run runtime tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_epi_agent_runtime.py tests/test_run_cancellation.py -q
```

Expected: all tests pass; the unexpected exception appears only in captured server logs, not tool-message content.

Commit:

```bash
git add epi_agent/tool_call_protocol.py epi_agent/runtime.py tests/test_epi_agent_runtime.py
git commit -m "fix: persist protocol-safe tool failures"
```

---

### Task 3: Repair legacy orphaned tool calls before follow-up

**Files:**
- Modify: `epi_agent/tool_call_protocol.py`
- Modify: `api/runtime.py:1-30,1601-1770`
- Modify: `tests/test_api_runtime.py:1-30,1430-1510`

**Interfaces:**
- Produces: immutable `ToolCallRepair(messages: tuple[BaseMessage, ...], repaired_call_ids: tuple[str, ...])`, `repair_orphaned_tool_calls(messages: Sequence[BaseMessage]) -> ToolCallRepair`, and `follow_up_message_patch(messages: Sequence[BaseMessage], message: HumanMessage) -> list[BaseMessage]`.
- Consumes: LangGraph's `RemoveMessage(id=REMOVE_ALL_MESSAGES)` replacement protocol.

- [ ] **Step 1: Add failing pure repair tests**

Add these tests near the later-submit tests in `tests/test_api_runtime.py`:

```python
def _assistant_call(*call_ids: str) -> AIMessage:
    return AIMessage(
        content="",
        id="assistant-tools",
        tool_calls=[
            {"name": f"tool_{index}", "args": {}, "id": call_id, "type": "tool_call"}
            for index, call_id in enumerate(call_ids, start=1)
        ],
    )


def test_repair_inserts_only_missing_tool_results_before_later_human() -> None:
    messages = [
        HumanMessage(content="first", id="user-1"),
        _assistant_call("call-1", "call-2"),
        ToolMessage(content="ok", tool_call_id="call-1", name="tool_1"),
        HumanMessage(content="already appended", id="user-2"),
    ]

    repair = repair_orphaned_tool_calls(messages)

    assert repair.repaired_call_ids == ("call-2",)
    assert [type(message) for message in repair.messages] == [
        HumanMessage, AIMessage, ToolMessage, ToolMessage, HumanMessage
    ]
    inserted = repair.messages[3]
    assert inserted.tool_call_id == "call-2"
    assert json.loads(str(inserted.content))["error"]["code"] == "INTERNAL_TOOL_ERROR"


def test_repair_is_idempotent_and_preserves_existing_results() -> None:
    first = repair_orphaned_tool_calls([_assistant_call("call-1")])
    second = repair_orphaned_tool_calls(first.messages)

    assert first.repaired_call_ids == ("call-1",)
    assert second.repaired_call_ids == ()
    assert second.messages == first.messages
```

- [ ] **Step 2: Run pure repair tests and observe missing symbols**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py \
  -k "repair_inserts_only_missing or repair_is_idempotent" -v
```

Expected: collection fails because `repair_orphaned_tool_calls` does not exist.

- [ ] **Step 3: Implement ordered orphan repair and atomic replacement patches**

Append this implementation to `epi_agent/tool_call_protocol.py`:

```python
from dataclasses import dataclass
from hashlib import sha256

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES


@dataclass(frozen=True)
class ToolCallRepair:
    messages: tuple[BaseMessage, ...]
    repaired_call_ids: tuple[str, ...]


def _repair_message(call: Mapping[str, Any]) -> ToolMessage:
    call_id = str(call["id"])
    message = error_tool_message(call)
    message.id = "tool-repair-" + sha256(call_id.encode("utf-8")).hexdigest()[:24]
    return message


def repair_orphaned_tool_calls(
    messages: Sequence[BaseMessage],
) -> ToolCallRepair:
    repaired: list[BaseMessage] = []
    repaired_ids: list[str] = []
    pending: dict[str, Mapping[str, Any]] = {}

    def flush_pending() -> None:
        for call_id, call in pending.items():
            repaired.append(_repair_message(call))
            repaired_ids.append(call_id)
        pending.clear()

    for message in messages:
        if pending and not isinstance(message, ToolMessage):
            flush_pending()
        repaired.append(message)
        if isinstance(message, AIMessage):
            pending.update(
                (str(call["id"]), call)
                for call in list(message.tool_calls or [])
            )
        elif isinstance(message, ToolMessage):
            pending.pop(str(message.tool_call_id), None)
    flush_pending()
    return ToolCallRepair(tuple(repaired), tuple(repaired_ids))


def follow_up_message_patch(
    messages: Sequence[BaseMessage],
    message: HumanMessage,
) -> list[BaseMessage]:
    repair = repair_orphaned_tool_calls(messages)
    if not repair.repaired_call_ids:
        return [message]
    return [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *repair.messages,
        message,
    ]
```

If `ToolMessage.id` is immutable in the installed LangChain version, construct a new `ToolMessage` in `_repair_message` with the same content, name, call ID, status, and deterministic `id` instead of assigning after creation.

- [ ] **Step 4: Bind the atomic repair patch before each later human message**

Import `follow_up_message_patch` in `api/runtime.py` and change the existing-values payload branch:

```python
from epi_agent.tool_call_protocol import follow_up_message_patch

# Inside _bind_message_payload, after building event_state and attachments:
if values:
    payload = {
        "messages": follow_up_message_patch(
            list(values.get("messages") or []),
            message,
        ),
        "artifacts": event_state["artifacts"],
        "meta": event_state["meta"],
        "authorized_attachment_ids": authorized_attachment_ids,
        "final_response": None,
        "iteration_count": 0,
        "failure_signatures": [],
        "current_turn_artifact_refs": [],
        "current_turn_output_artifact_refs": [],
        "completion_blocked": False,
        "model_output_state": {},
        "terminal_error": None,
        "terminal_control": None,
    }
    return payload
```

Do not move the existing `_has_blocking_interrupt(snapshot)` check in `submit_message`; it must remain before payload binding.

- [ ] **Step 5: Add API binding and reducer-level continuation tests**

Extend `test_runtime_later_submit_sends_message_and_event_log_delta` with a matched sequence assertion, then add:

```python
def test_runtime_later_submit_atomically_repairs_orphan_before_follow_up() -> None:
    orphan = _assistant_call("orphan-call")
    graph = _RuntimeFakeGraph(
        SimpleNamespace(
            values={
                "messages": [HumanMessage(content="first"), orphan],
                "meta": {"last_user_message_hash": "prior-turn"},
                "terminal_error": {
                    "code": "RUN_FAILED",
                    "message": "The prior request failed.",
                },
            },
            next=(),
            interrupts=[],
        )
    )
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner)

    runtime.submit_message(
        _LOCAL_IDENTITY,
        "thread-1",
        "Who are you?",
        provider_api_key="test-key",
    )

    patch = runner.background_calls[0]["initial_payload"]["messages"]
    merged = add_messages(
        [HumanMessage(content="first"), orphan],
        patch,
    )
    assert [type(message) for message in merged] == [
        HumanMessage, AIMessage, ToolMessage, HumanMessage
    ]
    assert merged[2].tool_call_id == "orphan-call"
    assert merged[3].content == "Who are you?"


def test_active_interrupt_is_blocked_before_orphan_repair() -> None:
    graph = _RuntimeFakeGraph(
        SimpleNamespace(
            values={"messages": [_assistant_call("active-review-call")]},
            next=("tools",),
            interrupts=[SimpleNamespace(id="interrupt-1", value={"type": "dataset_plan_review"})],
        )
    )
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner)

    with pytest.raises(ThreadAwaitingReviewError):
        runtime.submit_message(
            _LOCAL_IDENTITY,
            "thread-1",
            "continue",
            provider_api_key="test-key",
        )

    assert runner.background_calls == []
```

Import `ToolMessage`, `InMemorySaver`, `MessagesState`, `add_messages`, `repair_orphaned_tool_calls`, `follow_up_message_patch`, and `ThreadAwaitingReviewError` in the test file. Add this real reducer/checkpoint test:

```python
def test_repaired_follow_up_sequence_is_durable_and_provider_valid() -> None:
    legacy = [
        HumanMessage(content="first", id="user-1"),
        _assistant_call("orphan-call"),
    ]
    follow_up = HumanMessage(content="Who are you?", id="user-2")
    merged = add_messages(
        legacy,
        follow_up_message_patch(legacy, follow_up),
    )

    def model_node(state: MessagesState) -> dict[str, list[AIMessage]]:
        messages = list(state["messages"])
        assert [type(message) for message in messages] == [
            HumanMessage,
            AIMessage,
            ToolMessage,
            HumanMessage,
        ]
        assert messages[2].tool_call_id == "orphan-call"
        return {"messages": [AIMessage(content="Follow-up succeeded")]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", model_node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "repair-thread"}}

    graph.invoke({"messages": merged}, config)
    durable = list(graph.get_state(config).values["messages"])

    assert [type(message) for message in durable] == [
        HumanMessage,
        AIMessage,
        ToolMessage,
        HumanMessage,
        AIMessage,
    ]
    assert durable[-1].content == "Follow-up succeeded"
```

- [ ] **Step 6: Run API/runtime protocol tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py \
  -k "later_submit or orphan or active_interrupt" -q
.venv/bin/python -m pytest tests/test_epi_agent_runtime.py tests/test_api_runtime.py -q
```

Expected: all selected tests pass; a matched history still uses the one-message delta, while malformed history uses remove-all plus a complete replacement.

Commit:

```bash
git add epi_agent/tool_call_protocol.py api/runtime.py tests/test_api_runtime.py
git commit -m "fix: repair orphaned tool calls on follow-up"
```

---

### Task 4: Add the missing real multi-study and recovery acceptance smoke

**Files:**
- Create: `scripts/smoke_multi_study_review_failure_recovery_real.py`
- Modify: `README.md:99-120`

**Interfaces:**
- Consumes: installer-ready RePORT and NHANES archives, `OPENAI_API_KEY`, production `build_application`, FastAPI routes, compiled `frontend/dist`, Playwright, and production SQLite checkpoint/activity stores.
- Produces: one executable smoke with `--report-archive`, `--nhanes-archive`, `--artifact-dir`, `--timeout-seconds`, and optional port arguments; exit 0 proves both acceptance paths.

- [ ] **Step 1: Create the executable smoke harness with one global deadline**

Create `scripts/smoke_multi_study_review_failure_recovery_real.py` with:

```python
#!/usr/bin/env python3
"""Exercise multi-study plan review and legacy tool-call recovery once."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.auth import AuthenticatedUser, LOCAL_SESSION_ID, RequestIdentity
from api.runtime import _initial_graph_state, graph_config
from study_package.installer import install_study_archives
from utils.env_loader import load_app_environment

HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
REPORT_ID = "report-india-synthetic"
NHANES_ID = "nhanes-2017-2018"
PLAN_QUERY = (
    "Using study_id report-india-synthetic, create one row per index case "
    "with smoking status, HbA1c, missed treatment doses, and final TB outcome. "
    "Present the dataset plan for review before extraction."
)
FOLLOW_UP = "Who are you? Reply in one short sentence."
```

Import `_find_port`, `_launch_browser`, `_remaining_ms`, and `_wait_for_health` from `scripts.e2e_agent_activity_timeline_real`. Use `subprocess.Popen` with stdout/stderr directed to `artifact_dir / "api.log"`, and terminate/kill that process in `finally`. The smoke must set:

```python
environment.update(
    {
        "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
        "REPORT_AGENT_STUDY_ROOT": str(study_root),
        "REPORT_AGENT_CHECKPOINT_DB_PATH": str(checkpoint_path),
        "REPORT_AGENT_STATIC_DIR": str(REPO_ROOT / "frontend/dist"),
        "REPORT_AGENT_API_WORKFLOW_TIMEOUT_SECONDS": str(args.timeout_seconds),
        "PYTHONPATH": str(REPO_ROOT),
    }
)
install_study_archives(
    [args.report_archive.resolve(), args.nhanes_archive.resolve()],
    study_root / "studies",
)
```

Create one `deadline = time.monotonic() + min(args.timeout_seconds, 300.0)` before seeding or launching anything. Every wait derives its timeout from this deadline. Never rerun either flow after failure.

- [ ] **Step 2: Seed a legacy orphan through the production checkpoint stack**

Add this function to the smoke:

```python
def _seed_legacy_orphan(environment: dict[str, str]) -> str:
    os.environ.update(environment)
    from api.activity_store import SqliteActivityStore
    from api.app import build_application
    from graph.conversation_events import (
        append_conversation_event,
        build_user_event,
    )
    from graph.state import MetaKeys

    application = build_application(environ=environment)
    runtime = application.state.report_agent_runtime
    identity = RequestIdentity(
        user=AuthenticatedUser(owner_user_id="local-user"),
        session_id=LOCAL_SESSION_ID,
    )
    api_key = str(environment["OPENAI_API_KEY"])
    thread_id = runtime.create_thread(identity)
    thread = runtime._require_owned_thread(identity, thread_id)
    runtime._ensure_graph(identity, thread, api_key)
    graph, _runner = runtime._bound_graph(thread)
    user = HumanMessage(
        content="Create a dataset; preserve this deliberately failed legacy turn.",
        id="legacy-user",
    )
    state = _initial_graph_state(thread_id, user)
    state["meta"][MetaKeys.LAST_USER_MESSAGE_HASH] = "legacy-turn"
    state = append_conversation_event(
        state,
        build_user_event(
            actor="human",
            user_turn_hash="legacy-turn",
            text=str(user.content),
            status="error",
        ),
    )
    state["messages"] = [
        user,
        AIMessage(
            content="",
            id="legacy-assistant",
            tool_calls=[
                {
                    "name": "dbrag-request_dataset_plan_review",
                    "args": {"plan_id": "legacy-plan", "version": 1},
                    "id": "legacy-orphan-call",
                    "type": "tool_call",
                }
            ],
        ),
    ]
    state["terminal_error"] = {
        "code": "RUN_FAILED",
        "message": "The prior request failed unexpectedly.",
        "recoverable": False,
    }
    graph.update_state(
        runtime._checkpoint_config(identity, thread_id),
        state,
        as_node="tools",
    )
    runtime.history_store.promote_pending("local-user", thread_id)
    runtime.history_store.rename(
        "local-user",
        thread_id,
        "Legacy failed tool turn",
    )
    activity = SqliteActivityStore(Path(environment["REPORT_AGENT_CHECKPOINT_DB_PATH"]))
    activity.start_run(thread_id, "legacy-user")
    activity.model_completed(thread_id)
    activity.tool_started(
        thread_id,
        "legacy-orphan-call",
        "dbrag-request_dataset_plan_review",
    )
    activity.finish(thread_id, "error")
    runtime.release_session("local-user", LOCAL_SESSION_ID)
    return thread_id
```

This is malformed input construction only: the application, graph compilation, SQLite saver, conversation history, and activity store are production implementations.

- [ ] **Step 3: Drive both browser paths and assert raw state**

Add this raw checkpoint reader so browser assertions do not depend on a debug API:

```python
def _checkpoint_values(checkpoint_path: Path, thread_id: str) -> dict[str, Any]:
    from langgraph.checkpoint.sqlite import SqliteSaver

    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        saved = saver.get_tuple(graph_config(thread_id))
    if saved is None:
        raise AssertionError(f"Missing checkpoint for {thread_id}")
    channels = dict(saved.checkpoint.get("channel_values") or {})
    root = channels.get("__root__")
    return dict(root) if isinstance(root, dict) else channels
```

After FastAPI health succeeds, launch Playwright once and perform these exact assertions:

```python
# Path 1: create a new conversation and submit PLAN_QUERY through the UI.
page.goto(api_url, wait_until="networkidle", timeout=remaining_ms)
page.get_by_label("Ask a question about your dataset!").fill(PLAN_QUERY)
page.get_by_role("button", name="Send", exact=True).click()
review_state = wait_until_dataset_plan_review(page, api_url, deadline)
assert page.get_by_role("heading", name="Review dataset plan", exact=True).is_visible()
assert review_state["active_interrupt"]["type"] == "dataset_plan_review"
review_values = _checkpoint_values(checkpoint_path, review_state["thread_id"])
plan_files = [
    value["content"]
    for value in review_values["artifacts"]["files"].values()
    if value.get("content", {}).get("kind") == "dataset_plan"
]
assert plan_files
assert {item["content"]["study_id"] for item in plan_files} == {REPORT_ID}
assert NHANES_ID not in json.dumps(plan_files)

# Path 2: open the seeded conversation, verify its failed activity marker,
# submit FOLLOW_UP through the UI, and wait for a normal assistant response.
page.get_by_text("Legacy failed tool turn", exact=True).click()
failed_timeline = page.get_by_label("Agent activity timeline").last
assert "agent-activity--error" in str(failed_timeline.get_attribute("class"))
page.get_by_label("Ask a question about your dataset!").fill(FOLLOW_UP)
page.get_by_role("button", name="Send", exact=True).click()
legacy_state = wait_until_follow_up_done(
    api_url,
    legacy_thread_id,
    deadline,
)
messages = list(_checkpoint_values(checkpoint_path, legacy_thread_id)["messages"])
call_index = next(index for index, item in enumerate(messages) if item.id == "legacy-assistant")
assert isinstance(messages[call_index + 1], ToolMessage)
assert messages[call_index + 1].tool_call_id == "legacy-orphan-call"
assert messages[call_index + 1].status == "error"
assert isinstance(messages[call_index + 2], HumanMessage)
assert legacy_state["run"]["state"] == "done"
first_timeline = page.get_by_label("Agent activity timeline").first
assert "agent-activity--error" in str(first_timeline.get_attribute("class"))
```

Use these bounded HTTP/browser helpers:

```python
def _thread_state(api_url: str, thread_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state",
        headers=HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return dict(response.json())


def current_thread_state(api_url: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/conversations",
        headers=HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    items = list(response.json().get("items") or [])
    if not items:
        raise AssertionError("No saved conversation exists")
    latest = max(
        items,
        key=lambda item: str(item.get("updated_at") or ""),
    )
    return _thread_state(api_url, str(latest["thread_id"]))


def wait_until_dataset_plan_review(
    page: Any,
    api_url: str,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        state = current_thread_state(api_url)
        interrupt = dict(state.get("active_interrupt") or {})
        heading = page.get_by_role(
            "heading",
            name="Review dataset plan",
            exact=True,
        )
        if (
            interrupt.get("type") == "dataset_plan_review"
            and heading.is_visible(timeout=100)
        ):
            return state
        run_state = str(dict(state.get("run") or {}).get("state") or "")
        if run_state in {"error", "timeout", "cancelled", "done"}:
            raise AssertionError(
                f"Run ended before dataset-plan review: {state['run']}"
            )
        time.sleep(0.25)
    raise TimeoutError("Dataset-plan review did not render before the deadline")


def wait_until_follow_up_done(
    api_url: str,
    thread_id: str,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        state = _thread_state(api_url, thread_id)
        run_state = str(dict(state.get("run") or {}).get("state") or "")
        conversation = list(state.get("conversation") or [])
        if (
            run_state == "done"
            and conversation
            and conversation[-1].get("role") == "assistant"
        ):
            return state
        if run_state in {"error", "timeout", "cancelled"}:
            raise AssertionError(f"Follow-up failed: {state['run']}")
        time.sleep(0.25)
    raise TimeoutError("Follow-up did not complete before the deadline")
```

On any exception, write these files under `--artifact-dir` before re-raising: `failure-traceback.txt`, `api.log`, `page-text.txt`, `failure.png`, `review-state.json` when available, and `legacy-state.json` when available. Always terminate FastAPI in `finally`.

- [ ] **Step 4: Document why the old smoke is narrower**

Rename the README heading to `Multi-study semantic catalog-binding smoke` and add this paragraph and command after it:

```markdown
This catalog smoke does not build or review a dataset plan. The feature smoke
below covers the multi-study plan-review boundary and recovery of a legacy
failed tool call through the real API and compiled browser UI.

```bash
PYTHONPATH=. .venv/bin/python scripts/smoke_multi_study_review_failure_recovery_real.py \
  --report-archive ../Database/report-india-synthetic/delivery/report-india-synthetic-0.3.1.tar.gz \
  --nhanes-archive ../Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.1.0.tar.gz
```
```

- [ ] **Step 5: Run static checks for the smoke without executing it**

Run:

```bash
chmod +x scripts/smoke_multi_study_review_failure_recovery_real.py
.venv/bin/python -m py_compile scripts/smoke_multi_study_review_failure_recovery_real.py
.venv/bin/python scripts/smoke_multi_study_review_failure_recovery_real.py --help
```

Expected: compilation succeeds and help lists both archive arguments, artifact directory, timeout, and ports. Do not run the real smoke yet.

- [ ] **Step 6: Commit the dedicated acceptance smoke**

```bash
git add scripts/smoke_multi_study_review_failure_recovery_real.py README.md
git commit -m "test: smoke multi-study review failure recovery"
```

---

### Task 5: Verify the complete regression once

**Files:**
- Verify: all files changed by Tasks 1-4
- Conditionally modify: `frontend/dist/**` only if a frontend source/build input changed to retain the failed activity marker

**Interfaces:**
- Consumes: the complete implementation and the two local installer-ready archives.
- Produces: focused test evidence plus one and only one dedicated real-smoke outcome.

- [ ] **Step 1: Run formatting/type-adjacent and focused regression checks**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_db_rag_agent_reviews.py \
  tests/test_epi_agent_runtime.py \
  tests/test_api_runtime.py \
  tests/test_run_cancellation.py -q
.venv/bin/python -m compileall -q epi_agent api scripts/smoke_multi_study_review_failure_recovery_real.py
```

Expected: all selected tests pass and Python compilation exits 0.

- [ ] **Step 2: Verify the working-demo production bundle**

If no file under `frontend/src/` and no frontend build input changed, run:

```bash
.venv/bin/python scripts/verify_working_demo_delivery.py
```

Expected: exit 0 with the existing compiled bundle matching its manifest.

If a focused browser/component test proved that the old failed activity marker disappears and a frontend change was required, run instead:

```bash
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
.venv/bin/python scripts/verify_working_demo_delivery.py
```

Expected: build succeeds, the manifest is refreshed after the build, and delivery verification exits 0.

- [ ] **Step 3: Run the dedicated real smoke exactly once with a 300-second outer timeout**

Run this command once; do not automatically retry it:

```bash
.venv/bin/python -c 'import subprocess; subprocess.run([".venv/bin/python", "scripts/smoke_multi_study_review_failure_recovery_real.py", "--report-archive", "../Database/report-india-synthetic/delivery/report-india-synthetic-0.3.1.tar.gz", "--nhanes-archive", "../Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.1.0.tar.gz", "--timeout-seconds", "300"], check=True, timeout=300)'
```

Expected: one PASS line covering both multi-study plan review and legacy follow-up recovery. On failure or timeout, stop execution, preserve the smoke artifact directory, and report its log/state/screenshot paths without rerunning.

- [ ] **Step 4: Inspect privacy and checkpoint invariants from the preserved smoke output**

Run only after a passing smoke:

```bash
rg -n "AttributeError|Traceback|data_sources|private implementation detail" \
  /tmp/report-multi-study-review-failure-recovery-smoke \
  -g 'page-text*.txt' -g '*state*.json'
```

Expected: no matches in public page/state artifacts. The server log may contain an original exception only in the focused test that deliberately raises one; the real smoke's seeded legacy state uses generic public text.

- [ ] **Step 5: Review the final diff and commit any verification-only UI bundle update**

Run:

```bash
git status --short
git diff --check
git diff --stat HEAD~4..HEAD
```

Expected: no whitespace errors or unrelated files. If Task 5 produced a required tracked frontend bundle update, commit only those files:

```bash
git add frontend/dist
git commit -m "build: refresh failure recovery frontend bundle"
```

If no frontend files changed, do not create an empty commit.

---

## Completion Evidence

Before claiming completion, record:

- the focused pytest command and pass count;
- the delivery-verification result;
- the single dedicated smoke result and artifact directory;
- the exact tool call/result/human ordering observed in the repaired checkpoint;
- the RePORT-only `study_id`/source evidence observed while NHANES was installed;
- confirmation that the failed prior turn remained visible after a successful follow-up;
- confirmation that no internal exception details appeared in public state or browser text.
