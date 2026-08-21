from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import threading
import time
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command
from fastapi.testclient import TestClient

from api.conversation_history import ConversationHistoryStore
from api import runtime as api_runtime
from api.runtime import (
    ApiGraphRunner,
    CancellationRestoreError,
    ReportAgentApiRuntime,
    graph_config,
)
from api.schemas import ApiThreadState, RunStatus
from api.server import create_app
from graph.conversation_events import (
    append_conversation_event,
    build_attachment_event,
    build_user_event,
    ensure_conversation_state,
)
from graph.state import MetaKeys
from utils.attachment_artifacts import AttachmentLimits
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

    def invoke(self, _payload, _config, **kwargs):
        if kwargs.get("interrupt_before"):
            return
        self.started.set()
        assert self.release.wait(timeout=2)
        cancellation_point()


class FactoryBoundaryGraph:
    def __init__(self) -> None:
        self.snapshot = SimpleNamespace(
            config={
                "configurable": {
                    "thread_id": "thread-factory",
                    "checkpoint_id": "checkpoint-before-turn",
                }
            },
            interrupts=[],
            next=(),
            values={},
        )
        self.invoke_calls = 0

    def get_state(self, _config, *, subgraphs: bool = False):
        assert subgraphs is True
        return self.snapshot

    def invoke(self, _payload, _config, **_kwargs):
        self.invoke_calls += 1


class FailingInitialBoundaryGraph(FactoryBoundaryGraph):
    def __init__(self) -> None:
        super().__init__()
        self.invoke_started = threading.Event()
        self.release_invoke = threading.Event()

    def invoke(self, _payload, _config, **_kwargs):
        self.invoke_calls += 1
        self.invoke_started.set()
        assert self.release_invoke.wait(timeout=2)
        raise RuntimeError("initial checkpoint failed")


def test_cancel_waits_for_payload_factory_and_never_starts_cancelled_input() -> None:
    app = FactoryBoundaryGraph()
    runner = ApiGraphRunner(app)
    factory_started = threading.Event()
    release_factory = threading.Event()
    cancel_finished = threading.Event()
    restored: list[dict[str, Any]] = []

    def payload_factory() -> dict[str, Any]:
        factory_started.set()
        assert release_factory.wait(timeout=2)
        return {"messages": []}

    starter = threading.Thread(
        target=lambda: runner.start_background_from_factory(
            thread_id="thread-factory",
            payload_factory=payload_factory,
            restore=lambda config: restored.append(deepcopy(config)),
            max_steps=3,
            timeout_seconds=5,
        )
    )
    starter.start()
    assert factory_started.wait(timeout=1)
    canceller = threading.Thread(
        target=lambda: (
            runner.cancel("thread-factory"),
            cancel_finished.set(),
        )
    )
    canceller.start()
    assert not cancel_finished.wait(timeout=0.05)

    release_factory.set()
    starter.join(timeout=1)
    canceller.join(timeout=1)

    assert not starter.is_alive()
    assert not canceller.is_alive()
    assert app.invoke_calls == 0
    assert restored[0]["configurable"]["checkpoint_id"] == (
        "checkpoint-before-turn"
    )
    assert runner.status("thread-factory")["state"] == "cancelled"


def test_cancel_owns_restore_when_initial_checkpoint_fails_concurrently() -> None:
    app = FailingInitialBoundaryGraph()
    runner = ApiGraphRunner(app)
    cancel_finished = threading.Event()
    initial_errors: list[str] = []
    restored: list[dict[str, Any]] = []

    starter = threading.Thread(
        target=lambda: runner.start_background_from_factory(
            thread_id="thread-factory",
            payload_factory=lambda: {"messages": []},
            restore=lambda config: restored.append(deepcopy(config)),
            max_steps=3,
            timeout_seconds=5,
            on_initial_payload_error=lambda: initial_errors.append("rejected"),
        )
    )
    starter.start()
    assert app.invoke_started.wait(timeout=1)
    canceller = threading.Thread(
        target=lambda: (
            runner.cancel("thread-factory"),
            cancel_finished.set(),
        )
    )
    canceller.start()
    assert not cancel_finished.wait(timeout=0.05)

    app.release_invoke.set()
    starter.join(timeout=1)
    canceller.join(timeout=1)

    assert not starter.is_alive()
    assert not canceller.is_alive()
    assert initial_errors == []
    assert restored[0]["configurable"]["checkpoint_id"] == (
        "checkpoint-before-turn"
    )
    assert runner.status("thread-factory")["state"] == "cancelled"


def test_initial_checkpoint_cleanup_error_still_finishes_transition() -> None:
    app = FailingInitialBoundaryGraph()
    app.release_invoke.set()
    runner = ApiGraphRunner(app)

    assert runner.start_background_from_factory(
        thread_id="thread-factory",
        payload_factory=lambda: {"messages": []},
        restore=lambda _config: None,
        max_steps=3,
        timeout_seconds=5,
        on_initial_payload_error=lambda: (_ for _ in ()).throw(
            RuntimeError("cleanup failed")
        ),
    )

    assert runner.status("thread-factory")["state"] == "error"
    assert runner._jobs["thread-factory"].transition_complete.is_set()


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


class ReviewPromotionRaceGraph:
    def __init__(self) -> None:
        self.resume_committed = threading.Event()
        self.promotion_started = threading.Event()
        self.release_promotion = threading.Event()
        self.background_started = threading.Event()
        self.get_state_calls = 0
        self.snapshot = SimpleNamespace(
            config={
                "configurable": {
                    "thread_id": "thread-review-race",
                    "checkpoint_id": "checkpoint-before-review",
                }
            },
            interrupts=[],
            next=("tools",),
            values={},
        )

    def get_state(self, _config, *, subgraphs: bool = False):
        assert subgraphs is True
        self.get_state_calls += 1
        if self.get_state_calls == 2:
            self.promotion_started.set()
            assert self.release_promotion.wait(timeout=2)
        return self.snapshot

    def invoke(self, payload, _config, **_kwargs):
        if isinstance(payload, Command):
            self.snapshot = SimpleNamespace(
                config={
                    "configurable": {
                        "thread_id": "thread-review-race",
                        "checkpoint_id": "checkpoint-approved",
                    }
                },
                interrupts=[],
                next=("model",),
                values={"approved_value": "dataset-1"},
            )
            self.resume_committed.set()
            return
        self.background_started.set()


def test_cancel_after_review_commit_uses_promoted_boundary_without_worker() -> None:
    app = ReviewPromotionRaceGraph()
    runner = ApiGraphRunner(app)
    restored: list[dict[str, Any]] = []
    cancel_finished = threading.Event()
    starter = threading.Thread(
        target=lambda: runner.start_background_after_durable_resume(
            thread_id="thread-review-race",
            initial_payload=Command(
                resume={"review-1": {"action": "approve"}}
            ),
            restore=lambda config: restored.append(deepcopy(config)),
            max_steps=3,
            timeout_seconds=5,
        )
    )
    starter.start()
    assert app.resume_committed.wait(timeout=1)
    assert app.promotion_started.wait(timeout=1)

    canceller = threading.Thread(
        target=lambda: (
            runner.cancel("thread-review-race"),
            cancel_finished.set(),
        )
    )
    canceller.start()
    assert not cancel_finished.wait(timeout=0.05)
    app.release_promotion.set()
    starter.join(timeout=1)
    canceller.join(timeout=1)

    assert not starter.is_alive()
    assert not canceller.is_alive()
    assert restored[0]["configurable"]["checkpoint_id"] == (
        "checkpoint-approved"
    )
    assert app.background_started.is_set() is False
    assert runner.status("thread-review-race")["state"] == "cancelled"


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


class InvocationTrackingGraph:
    def __init__(self, app: Any, finished: threading.Event) -> None:
        self._app = app
        self._finished = finished

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app, name)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._app.invoke(*args, **kwargs)
        finally:
            self._finished.set()


def _blocking_app(
    started: threading.Event,
    release: threading.Event,
    *,
    checkpointer: Any | None = None,
    finished: threading.Event | None = None,
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
    builder.add_node("model_output_gate", lambda _state: {})
    builder.add_edge(START, "tools")
    builder.add_edge("tools", "model_output_gate")
    builder.add_edge("model_output_gate", END)
    app = builder.compile(checkpointer=checkpointer or InMemorySaver())
    if finished is not None:
        return InvocationTrackingGraph(app, finished)
    return app


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


def test_cancel_is_idempotent_and_terminal_after_sqlite_restart(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "checkpoints.db"
    first_connection = sqlite3.connect(
        checkpoint_path,
        check_same_thread=False,
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    first_app = _blocking_app(
        started,
        release,
        checkpointer=SqliteSaver(first_connection),
        finished=finished,
    )
    runtime = _runtime(tmp_path, first_app)
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

    first_cancel = runtime.cancel_run(thread_id)
    second_cancel = runtime.cancel_run(thread_id)
    assert second_cancel == first_cancel

    snapshot = first_app.get_state(graph_config(thread_id))
    events = snapshot.values["artifacts"]["conversation_events"]
    assert sum(
        event.get("type") == "user" and event.get("status") == "cancelled"
        for event in events
    ) == 1
    assert sum(
        event.get("type") == "attachment"
        and event.get("relationship") == "input"
        and event.get("artifact_id") == attachment_id
        for event in events
    ) == 1

    release.set()
    assert finished.wait(timeout=1)
    first_connection.close()

    restarted_connection = sqlite3.connect(
        checkpoint_path,
        check_same_thread=False,
    )
    restarted_app = _blocking_app(
        threading.Event(),
        threading.Event(),
        checkpointer=SqliteSaver(restarted_connection),
    )
    restarted_runtime = _runtime(tmp_path, restarted_app)

    restored = restarted_runtime.state(thread_id)
    assert restored.run.state == "cancelled"
    assert restored.conversation[-1].status == "cancelled"
    assert restored.conversation[-1].attachments[0].id == attachment_id
    thread = restarted_runtime._thread(thread_id)
    _app, restarted_runner = restarted_runtime._ensure_graph(thread)
    assert restarted_runner.status(thread_id)["state"] == "idle"
    restarted_connection.close()


def test_cancelled_event_projects_message_status_and_attachment() -> None:
    turn = api_runtime.CancelledTurn(
        message_id="user-1",
        text="Analyze the attached cohort",
        turn_hash="turn-hash-1",
        attachment_ids=("attachment-1",),
    )
    values = api_runtime._cancelled_turn_patch(
        ensure_conversation_state(
            {"artifacts": {}, "meta": {MetaKeys.THREAD_ID: "thread-1"}}
        ),
        turn=turn,
        manifests=[
            {
                "id": "attachment-1",
                "filename": "cohort.csv",
                "kind": "table",
                "mime": "text/csv",
                "byte_size": 12,
                "status": "available",
            }
        ],
    )
    state = api_runtime.project_thread_state(
        thread_id="thread-1",
        snapshot=SimpleNamespace(values=values, next=(), interrupts=[]),
        run_status={"state": "cancelled", "steps": 1},
    )

    assert state.conversation[-1].status == "cancelled"
    assert state.conversation[-1].attachments[0].id == "attachment-1"


class CancelApiRuntime:
    attachment_limits = AttachmentLimits()

    def __init__(
        self,
        *,
        missing: bool = False,
        restore_failure: bool = False,
    ) -> None:
        self.missing = missing
        self.restore_failure = restore_failure
        self.cancelled_threads: list[str] = []

    def cancel_run(self, thread_id: str) -> ApiThreadState:
        self.cancelled_threads.append(thread_id)
        if self.missing:
            raise KeyError(thread_id)
        if self.restore_failure:
            raise CancellationRestoreError("restore failed")
        return ApiThreadState(
            thread_id=thread_id,
            run=RunStatus(state="cancelled"),
        )


def test_cancel_endpoint_returns_the_cancelled_thread_state() -> None:
    runtime = CancelApiRuntime()
    client = TestClient(
        create_app(runtime=runtime, provider_api_key="test-provider-key")
    )

    response = client.post("/api/threads/thread-1/cancel")

    assert response.status_code == 200
    assert response.json()["run"]["state"] == "cancelled"
    assert runtime.cancelled_threads == ["thread-1"]


def test_cancel_endpoint_returns_not_found_for_unknown_thread() -> None:
    client = TestClient(
        create_app(
            runtime=CancelApiRuntime(missing=True),
            provider_api_key="test-provider-key",
        )
    )

    response = client.post("/api/threads/missing/cancel")

    assert response.status_code == 404


def test_cancel_endpoint_returns_structured_restore_failure() -> None:
    client = TestClient(
        create_app(
            runtime=CancelApiRuntime(restore_failure=True),
            provider_api_key="test-provider-key",
        )
    )

    response = client.post("/api/threads/thread-1/cancel")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "CANCELLATION_RESTORE_FAILED",
        "message": "restore failed",
    }
