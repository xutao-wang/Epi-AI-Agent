from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command

from api.conversation_history import ConversationHistoryStore
from api import runtime as api_runtime
from api.runtime import ApiGraphRunner, ReportAgentApiRuntime, graph_config
from graph.conversation_events import (
    append_conversation_event,
    build_attachment_event,
    build_user_event,
    ensure_conversation_state,
)
from graph.state import MetaKeys
from utils.run_cancellation import cancellation_point


SETTINGS = {
    "model_name": "gpt-5.4",
    "temperature": 0.1,
    "top_p": 0.9,
    "max_steps": 4,
    "timeout_seconds": 10,
    "db_rag_embedding_model": "",
    "db_rag_reranker_model": "",
}


class BlockingGraph:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.snapshot = SimpleNamespace(
            config={
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_id": "checkpoint-before-turn",
                }
            },
            interrupts=[],
            next=("tools",),
            values={},
        )

    def get_state(self, _config, *, subgraphs: bool = False):
        assert subgraphs is True
        return self.snapshot

    def invoke(self, _payload, _config, **_kwargs):
        self.started.set()
        assert self.release.wait(timeout=2)
        cancellation_point()


def test_runner_cancels_active_job_and_uses_captured_checkpoint() -> None:
    app = BlockingGraph()
    runner = ApiGraphRunner(app)
    restored: list[dict] = []
    initial_errors: list[str] = []

    assert runner.start_background_from_factory(
        thread_id="thread-1",
        payload_factory=lambda: {"messages": []},
        restore=lambda config: restored.append(deepcopy(config)),
        max_steps=3,
        timeout_seconds=5,
        on_initial_payload_error=lambda: initial_errors.append("rejected"),
    )
    assert app.started.wait(timeout=1)

    status = runner.cancel("thread-1")
    app.release.set()

    assert status["state"] == "cancelled"
    assert restored == [
        {
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_id": "checkpoint-before-turn",
            }
        }
    ]
    assert initial_errors == []
    assert runner.status("thread-1")["state"] == "cancelled"
    assert graph_config("thread-1") == {
        "configurable": {"thread_id": "thread-1"}
    }


class ReviewResumeGraph:
    def __init__(self) -> None:
        self.background_started = threading.Event()
        self.release = threading.Event()
        self.interrupt_after: list[str] | None = None
        self.snapshot = SimpleNamespace(
            config={
                "configurable": {
                    "thread_id": "thread-review",
                    "checkpoint_id": "checkpoint-before-review",
                }
            },
            interrupts=[],
            next=("review",),
            values={},
        )

    def get_state(self, _config, *, subgraphs: bool = False):
        assert subgraphs is True
        return self.snapshot

    def invoke(self, payload, _config, **kwargs):
        if isinstance(payload, Command):
            self.interrupt_after = list(kwargs.get("interrupt_after") or [])
            self.snapshot = SimpleNamespace(
                config={
                    "configurable": {
                        "thread_id": "thread-review",
                        "checkpoint_id": "checkpoint-approved",
                    }
                },
                interrupts=[],
                next=("tools",),
                values={"approved_value": "dataset-1"},
            )
            return
        self.background_started.set()
        assert self.release.wait(timeout=2)
        cancellation_point()


def test_review_resume_updates_the_durable_cancellation_boundary() -> None:
    app = ReviewResumeGraph()
    runner = ApiGraphRunner(app)
    restored: list[dict] = []

    assert runner.start_background_after_durable_resume(
        thread_id="thread-review",
        initial_payload=Command(resume={"review-1": {"action": "approve"}}),
        restore=lambda config: restored.append(deepcopy(config)),
        max_steps=3,
        timeout_seconds=5,
    )
    assert app.background_started.wait(timeout=1)

    status = runner.cancel("thread-review")
    app.release.set()

    assert status["state"] == "cancelled"
    assert restored[0]["configurable"]["checkpoint_id"] == "checkpoint-approved"
    assert app.interrupt_after == ["tools", "model_output_gate"]


def test_cancelled_turn_patch_keeps_boundary_and_retains_user_input() -> None:
    boundary = ensure_conversation_state(
        {
            "messages": [],
            "authorized_attachment_ids": ["attachment-prior"],
            "artifacts": {
                "datasets": {
                    "approved-dataset": {
                        "id": "approved-dataset",
                        "status": "active",
                    }
                }
            },
            "meta": {MetaKeys.THREAD_ID: "thread-1"},
        }
    )
    turn = api_runtime.CancelledTurn(
        message_id="user-1",
        text="Analyze the attached cohort",
        turn_hash="turn-hash-1",
        attachment_ids=("attachment-1",),
    )
    manifest = {
        "id": "attachment-1",
        "filename": "cohort.csv",
        "kind": "table",
        "mime": "text/csv",
        "byte_size": 12,
        "status": "available",
    }

    patch = api_runtime._cancelled_turn_patch(
        boundary,
        turn=turn,
        manifests=[manifest],
    )

    events = patch["artifacts"]["conversation_events"]
    user_event = next(event for event in events if event["type"] == "user")
    attachment_event = next(
        event for event in events if event["type"] == "attachment"
    )
    assert user_event["text"] == "Analyze the attached cohort"
    assert user_event["status"] == "cancelled"
    assert attachment_event["artifact_id"] == "attachment-1"
    assert attachment_event["parent_event_id"] == user_event["event_id"]
    assert patch["artifacts"]["datasets"]["approved-dataset"]["status"] == "active"
    assert patch["artifacts"]["attachments"]["attachment-1"] == manifest
    assert patch["authorized_attachment_ids"] == [
        "attachment-1",
        "attachment-prior",
    ]
    assert patch["terminal_control"]["status"] == "cancelled"
    assert patch["cancelled_turn"]["attachment_ids"] == ["attachment-1"]
    assert "messages" not in patch


def test_latest_turn_is_reconstructed_for_review_resume_cancellation() -> None:
    state = ensure_conversation_state(
        {"artifacts": {}, "meta": {MetaKeys.THREAD_ID: "thread-review"}}
    )
    state = append_conversation_event(
        state,
        build_user_event(
            actor="human",
            user_turn_hash="turn-review",
            text="Create and review a cohort",
        ),
    )
    user_event_id = state["artifacts"]["conversation_events"][-1]["event_id"]
    state = append_conversation_event(
        state,
        build_attachment_event(
            actor="api",
            user_turn_hash="turn-review",
            artifact_id="attachment-review",
            relationship="input",
            parent_event_id=user_event_id,
        ),
    )

    turn = api_runtime._cancelled_turn_from_values(state)

    assert turn.text == "Create and review a cohort"
    assert turn.turn_hash == "turn-review"
    assert turn.attachment_ids == ("attachment-review",)


class CancellationGraphState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    output: dict[str, Any]
    artifacts: dict[str, Any]
    artifact_ids: list[str]
    authorized_attachment_ids: list[str]
    final_response: str | None
    iteration_count: int
    failure_signatures: list[str]
    current_turn_artifact_refs: list[dict[str, Any]]
    current_turn_output_artifact_refs: list[dict[str, Any]]
    analysis_review_feedback_history: list[dict[str, Any]]
    completion_blocked: bool
    model_output_state: dict[str, Any]
    meta: dict[str, Any]
    terminal_error: dict[str, Any] | None
    terminal_control: dict[str, Any] | None
    cancelled_turn: dict[str, Any]
    draft_value: str


def _blocking_app(
    started: threading.Event,
    release: threading.Event,
):
    def tools(_state: CancellationGraphState) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=2)
        cancellation_point()
        return {
            "draft_value": "must-disappear",
            "messages": [
                ToolMessage(
                    id="late-tool-result",
                    content="late",
                    tool_call_id="call-1",
                )
            ],
        }

    builder = StateGraph(CancellationGraphState)
    builder.add_node("tools", tools)
    builder.add_node("finish", lambda _state: {})
    builder.add_edge(START, "tools")
    builder.add_edge("tools", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=InMemorySaver())


def _runtime(tmp_path: Path, app) -> ReportAgentApiRuntime:
    return ReportAgentApiRuntime(
        graph_factory=lambda _settings: app,
        default_runtime_settings=SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=ConversationHistoryStore(tmp_path / "history.db"),
    )


def test_runtime_cancellation_restores_pre_turn_checkpoint_with_attachment(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    app = _blocking_app(started, release)
    runtime = _runtime(tmp_path, app)
    thread_id = runtime.create_thread()
    upload = runtime.stage_attachments(
        thread_id,
        [("cohort.csv", "text/csv", b"participant_id\n1\n")],
    )
    attachment_id = upload.attachments[0].id

    runtime.submit_message(
        thread_id,
        "Analyze the attached cohort",
        [attachment_id],
    )
    assert started.wait(timeout=1)

    cancelled = runtime.cancel_run(thread_id)
    release.set()
    deadline = time.time() + 1
    while time.time() < deadline and runtime.state(thread_id).run.state == "running":
        time.sleep(0.01)

    snapshot = app.get_state(graph_config(thread_id))
    events = snapshot.values["artifacts"]["conversation_events"]
    assert cancelled.run.state == "cancelled"
    assert snapshot.values.get("draft_value") is None
    assert snapshot.values["terminal_control"]["status"] == "cancelled"
    assert snapshot.values["authorized_attachment_ids"] == [attachment_id]
    assert runtime.attachment_store.require(thread_id, attachment_id)["status"] == (
        "available"
    )
    assert any(
        event.get("type") == "user" and event.get("status") == "cancelled"
        for event in events
    )
    assert all(
        getattr(message, "id", None) != "late-tool-result"
        for message in snapshot.values.get("messages", [])
    )
    assert [item.thread_id for item in runtime.list_conversations()] == [thread_id]
