from __future__ import annotations

import sqlite3
from types import SimpleNamespace
import time
from typing import Any

from langchain_core.messages import HumanMessage

from api.activity_store import SqliteActivityStore
from api.conversation_history import ConversationHistoryStore
from api.runtime import ReportAgentApiRuntime, ThreadRuntime, project_thread_state
from api.schemas import ActivityRun, RunStatus, RuntimeSettings
from graph.conversation_events import (
    append_conversation_event,
    build_user_event,
    ensure_conversation_state,
)
from graph.state import MetaKeys


SETTINGS = {
    "model_name": "gpt-5.4",
    "temperature": 0.1,
    "top_p": 0.9,
    "max_steps": 4,
    "timeout_seconds": 10,
    "db_rag_embedding_model": "",
    "db_rag_reranker_model": "",
}


def _values() -> tuple[dict[str, Any], str, str]:
    turn_hash = "turn-hash-1"
    values = ensure_conversation_state(
        {"artifacts": {}, "meta": {MetaKeys.THREAD_ID: "thread-1"}}
    )
    values = append_conversation_event(
        values,
        build_user_event(
            actor="human",
            user_turn_hash=turn_hash,
            text="Create a cohort dataset",
        ),
    )
    user_event_id = values["artifacts"]["conversation_events"][-1]["event_id"]
    values["messages"] = [
        HumanMessage(content="Create a cohort dataset", id="model-user-1")
    ]
    values["meta"][MetaKeys.LAST_USER_MESSAGE_HASH] = turn_hash
    return values, str(user_event_id), turn_hash


class FakeCheckpointer:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class FakeApp:
    def __init__(self, snapshot: Any) -> None:
        self.snapshot = snapshot
        self.checkpointer = FakeCheckpointer()

    def get_state(self, _config, *, subgraphs: bool = False):
        assert subgraphs is True
        return self.snapshot


class SubmittingGraph(FakeApp):
    def __init__(self) -> None:
        super().__init__(_snapshot({}))

    def update_state(self, _config, values, *, as_node: str) -> None:
        assert as_node
        self.snapshot.values = dict(values)

    def invoke(self, payload, _config, **_kwargs: Any) -> None:
        self.snapshot.values = {**self.snapshot.values, **dict(payload)}
        self.snapshot.next = ()


class FakeRunner:
    def __init__(self, status: str = "done") -> None:
        self.state = status
        self.resumed = False
        self.recovered = False

    def status(self, _thread_id: str) -> dict[str, Any]:
        return {"state": self.state, "steps": 1}

    def start_background(self, **_kwargs: Any) -> bool:
        self.recovered = True
        self.state = "running"
        return True

    def start_background_after_durable_resume(self, **_kwargs: Any) -> bool:
        self.resumed = True
        self.state = "running"
        return True

    def cancel(self, _thread_id: str) -> dict[str, Any]:
        self.state = "cancelled"
        return self.status(_thread_id)


def _snapshot(
    values: dict[str, Any],
    *,
    interrupts: list[Any] | None = None,
    next_nodes: tuple[str, ...] = (),
) -> Any:
    return SimpleNamespace(
        values=values,
        interrupts=list(interrupts or []),
        next=next_nodes,
        config={"configurable": {"thread_id": "thread-1"}},
    )


def _runtime(
    tmp_path,
    *,
    app: FakeApp,
    runner: FakeRunner,
    activity_store: Any,
    history_store: ConversationHistoryStore | None = None,
) -> ReportAgentApiRuntime:
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings: app,
        default_runtime_settings=SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=history_store,
        activity_store=activity_store,
    )
    runtime._threads["thread-1"] = ThreadRuntime(
        settings=RuntimeSettings(**SETTINGS),
        app=app,
        runner=runner,
        locked=True,
    )
    return runtime


def _wait_until_finished(
    runtime: ReportAgentApiRuntime,
    thread_id: str,
):
    deadline = time.time() + 2
    state = runtime.state(thread_id)
    while state.run.state == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = runtime.state(thread_id)
    return state


def test_submit_message_projects_activity_for_the_durable_user_event(tmp_path) -> None:
    app = SubmittingGraph()
    store = SqliteActivityStore(tmp_path / "activity.db")
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings: app,
        default_runtime_settings=SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        activity_store=store,
    )
    thread_id = runtime.create_thread()

    runtime.submit_message(thread_id, "Create a cohort dataset")
    state = _wait_until_finished(runtime, thread_id)

    user_message = next(item for item in state.conversation if item.role == "user")
    assert state.run.state == "done"
    assert state.activity_runs[0].user_message_id == user_message.id


def test_first_durable_user_turn_starts_activity_with_conversation_event_id(
    tmp_path,
) -> None:
    values, user_event_id, turn_hash = _values()
    app = FakeApp(_snapshot(values))
    store = SqliteActivityStore(tmp_path / "activity.db")
    runtime = _runtime(
        tmp_path,
        app=app,
        runner=FakeRunner(),
        activity_store=store,
    )
    thread = runtime._thread("thread-1")

    runtime._accept_initial_turn(
        "thread-1",
        thread,
        message_id="model-user-1",
        turn_hash=turn_hash,
        manifests=[],
    )

    [run] = store.list_runs("thread-1")
    assert run.user_message_id == user_event_id


def test_state_marks_interrupt_waiting_and_resume_reuses_run(tmp_path) -> None:
    values, user_event_id, _turn_hash = _values()
    interrupt = SimpleNamespace(
        id="interrupt-1",
        value={
            "type": "agent_clarification",
            "question": "Which cohort should be used?",
            "reason": "Two cohorts match.",
            "options": [
                {"id": "cohort-a", "label": "Cohort A"},
                {"id": "cohort-b", "label": "Cohort B"},
            ],
        },
    )
    app = FakeApp(
        _snapshot(values, interrupts=[interrupt], next_nodes=("tools",))
    )
    runner = FakeRunner("interrupted")
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", user_event_id)
    store.model_completed("thread-1")
    runtime = _runtime(
        tmp_path,
        app=app,
        runner=runner,
        activity_store=store,
    )

    waiting = runtime.state("thread-1")
    waiting_again = runtime.state("thread-1")

    assert waiting.activity_runs[0].state == "waiting"
    assert len(waiting_again.activity_runs[0].activities) == len(
        waiting.activity_runs[0].activities
    )

    runtime.resume_interrupt(
        "thread-1",
        "interrupt-1",
        {"action": "answer", "answer": "Cohort A"},
    )

    [resumed] = store.list_runs("thread-1")
    assert runner.resumed is True
    assert resumed.state == "running"
    assert len(store.list_runs("thread-1")) == 1


def test_terminal_state_closes_and_projects_activity(tmp_path) -> None:
    values, user_event_id, _turn_hash = _values()
    app = FakeApp(_snapshot(values))
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", user_event_id)
    runtime = _runtime(
        tmp_path,
        app=app,
        runner=FakeRunner("done"),
        activity_store=store,
    )

    state = runtime.state("thread-1")

    assert state.run.state == "done"
    assert state.activity_runs[0].state == "completed"
    assert all(
        item.status != "running" for item in state.activity_runs[0].activities
    )


def test_state_records_recovery_before_restarting_durable_work(tmp_path) -> None:
    values, user_event_id, _turn_hash = _values()
    app = FakeApp(_snapshot(values, next_nodes=("model",)))
    runner = FakeRunner("idle")
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", user_event_id)
    runtime = _runtime(
        tmp_path,
        app=app,
        runner=runner,
        activity_store=store,
    )

    state = runtime.state("thread-1")

    assert runner.recovered is True
    assert state.run.state == "running"
    assert [item.label for item in state.activity_runs[0].activities] == [
        "Resuming your request"
    ]


def test_cancel_hides_running_item_and_closes_activity_run(tmp_path) -> None:
    values, user_event_id, _turn_hash = _values()
    app = FakeApp(_snapshot(values, next_nodes=("model",)))
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", user_event_id)
    runtime = _runtime(
        tmp_path,
        app=app,
        runner=FakeRunner("running"),
        activity_store=store,
    )

    cancelled = runtime.cancel_run("thread-1")

    assert cancelled.activity_runs[0].state == "cancelled"
    assert all(
        item.status != "running"
        for item in cancelled.activity_runs[0].activities
    )


def test_delete_conversation_deletes_activity_but_archive_retains_it(
    tmp_path,
) -> None:
    values, user_event_id, _turn_hash = _values()
    app = FakeApp(_snapshot(values))
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", user_event_id)
    history = ConversationHistoryStore(tmp_path / "history.db")
    history.create_pending("thread-1", model_name="gpt-5.4")
    history.promote_pending("thread-1")
    runtime = _runtime(
        tmp_path,
        app=app,
        runner=FakeRunner("done"),
        activity_store=store,
        history_store=history,
    )

    runtime.archive_conversation("thread-1")
    assert store.list_runs("thread-1")
    runtime.restore_conversation("thread-1")
    assert runtime.delete_conversation("thread-1") is True
    assert store.list_runs("thread-1") == []


class ExplodingActivityStore:
    def __getattr__(self, _name: str):
        def fail(*_args: Any, **_kwargs: Any) -> None:
            raise sqlite3.OperationalError("activity database unavailable")

        return fail


def test_activity_store_failure_does_not_break_checkpoint_or_projection(
    tmp_path,
) -> None:
    values, _user_event_id, turn_hash = _values()
    app = FakeApp(_snapshot(values))
    runtime = _runtime(
        tmp_path,
        app=app,
        runner=FakeRunner("done"),
        activity_store=ExplodingActivityStore(),
    )

    runtime._accept_initial_turn(
        "thread-1",
        runtime._thread("thread-1"),
        message_id="model-user-1",
        turn_hash=turn_hash,
        manifests=[],
    )
    state = runtime.state("thread-1")

    assert state.run == RunStatus(state="done", steps=1)
    assert state.activity_runs == []


def test_activity_store_failure_does_not_break_message_submission(tmp_path) -> None:
    app = SubmittingGraph()
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings: app,
        default_runtime_settings=SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        activity_store=ExplodingActivityStore(),
    )
    thread_id = runtime.create_thread()

    runtime.submit_message(thread_id, "Create a cohort dataset")
    state = _wait_until_finished(runtime, thread_id)

    assert state.run.state == "done"
    assert state.conversation[-1].text == "Create a cohort dataset"
    assert state.activity_runs == []


def test_pure_projection_accepts_activity_runs() -> None:
    values, user_event_id, _turn_hash = _values()
    run = ActivityRun(
        id="run-1",
        thread_id="thread-1",
        user_message_id=user_event_id,
        state="completed",
        created_at="2026-08-11T00:00:00+00:00",
        updated_at="2026-08-11T00:00:01+00:00",
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=_snapshot(values),
        run_status={"state": "done", "steps": 1},
        activity_runs=[run],
    )

    assert projected.activity_runs == [run]
