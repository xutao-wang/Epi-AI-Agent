from __future__ import annotations

import pytest

from api.activity_store import SqliteActivityStore


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


def test_recoverable_tool_failure_is_hidden_and_model_work_resumes(tmp_path) -> None:
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", "user-event-1")
    store.model_completed("thread-1")
    store.tool_started("thread-1", "call-1", "dbrag-search_catalog")

    store.tool_recoverable_failure("thread-1", "call-1")

    [run] = store.list_runs("thread-1")
    assert all(item.tool_call_id != "call-1" for item in run.activities)
    assert run.activities[-1].label == "Choosing the next step"
    assert run.activities[-1].status == "running"


def test_waiting_and_resume_update_the_same_review_activity(tmp_path) -> None:
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", "user-event-1")
    store.model_completed("thread-1")
    store.tool_started(
        "thread-1",
        "review-call-1",
        "dbrag-request_dataset_plan_review",
    )

    store.mark_waiting("thread-1", "dataset_plan_review")

    [waiting_run] = store.list_runs("thread-1")
    assert waiting_run.state == "waiting"
    waiting_item = waiting_run.activities[-1]
    assert waiting_item.tool_call_id == "review-call-1"
    assert waiting_item.label == "Waiting for dataset plan review"
    assert waiting_item.status == "waiting"

    store.resume("thread-1", "dataset_plan_review")

    [resumed_run] = store.list_runs("thread-1")
    assert resumed_run.id == waiting_run.id
    assert resumed_run.state == "running"
    assert resumed_run.activities[-1].id == waiting_item.id
    assert resumed_run.activities[-1].label == "Dataset plan reviewed"
    assert resumed_run.activities[-1].status == "completed"


def test_waiting_without_a_running_tool_uses_interrupt_labels(tmp_path) -> None:
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", "user-event-1")
    store.model_completed("thread-1")

    store.mark_waiting("thread-1", "agent_clarification")

    [run] = store.list_runs("thread-1")
    assert run.state == "waiting"
    assert run.activities[-1].label == "Waiting for your answer"
    assert run.activities[-1].status == "waiting"


def test_repeated_waiting_reconciliation_does_not_duplicate_activity(tmp_path) -> None:
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", "user-event-1")
    store.model_completed("thread-1")

    store.mark_waiting("thread-1", "agent_clarification")
    store.mark_waiting("thread-1", "agent_clarification")

    [run] = store.list_runs("thread-1")
    waiting = [item for item in run.activities if item.status == "waiting"]
    assert len(waiting) == 1


def test_recover_hides_stale_running_items_and_appends_recovery(tmp_path) -> None:
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", "user-event-1")

    store.recover("thread-1")

    [run] = store.list_runs("thread-1")
    assert run.state == "running"
    assert [item.label for item in run.activities] == ["Resuming your request"]
    assert run.activities[0].status == "running"


def test_terminal_error_hides_running_work_and_allows_a_new_run(tmp_path) -> None:
    store = SqliteActivityStore(tmp_path / "activity.db")
    first_id = store.start_run("thread-1", "user-event-1")

    store.finish("thread-1", "error")
    second_id = store.start_run("thread-1", "user-event-2")

    runs = store.list_runs("thread-1")
    assert [run.id for run in runs] == [first_id, second_id]
    assert [run.state for run in runs] == ["error", "running"]
    assert runs[0].activities == []


def test_delete_thread_removes_only_that_threads_runs(tmp_path) -> None:
    store = SqliteActivityStore(tmp_path / "activity.db")
    store.start_run("thread-1", "user-event-1")
    store.start_run("thread-2", "user-event-2")

    store.delete_thread("thread-1")

    assert store.list_runs("thread-1") == []
    assert len(store.list_runs("thread-2")) == 1


@pytest.mark.parametrize("thread_id,user_message_id", [("", "user-1"), ("t", "")])
def test_start_run_rejects_empty_identifiers(
    tmp_path,
    thread_id: str,
    user_message_id: str,
) -> None:
    store = SqliteActivityStore(tmp_path / "activity.db")
    with pytest.raises(ValueError):
        store.start_run(thread_id, user_message_id)
