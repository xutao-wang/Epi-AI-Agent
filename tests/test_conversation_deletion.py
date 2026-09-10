from types import SimpleNamespace

import pytest

from api.conversation_history import ConversationHistoryStore
from api.runtime import ReportAgentApiRuntime, ThreadAlreadyRunningError


def _runtime_with_conversation(tmp_path, thread_id: str):
    history_store = ConversationHistoryStore(tmp_path / "history.sqlite")
    history_store.create(
        "local-user",
        thread_id,
        model_name="gpt-5.4",
    )
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda *_args, **_kwargs: None,
        default_runtime_settings={"model_name": "gpt-5.4"},
        models=["gpt-5.4"],
        history_store=history_store,
    )
    return runtime, history_store


def test_delete_conversation_allows_awaiting_review(tmp_path, monkeypatch) -> None:
    thread_id = "thread-awaiting-review"
    runtime, history_store = _runtime_with_conversation(tmp_path, thread_id)
    monkeypatch.setattr(
        runtime,
        "state",
        lambda *_args, **_kwargs: SimpleNamespace(
            run=SimpleNamespace(state="interrupted"),
            active_interrupt=object(),
        ),
    )

    assert runtime.delete_conversation(thread_id) is True
    assert history_store.get("local-user", thread_id) is None
    assert ("local-user", thread_id) not in runtime._threads


def test_delete_conversation_still_rejects_running_thread(tmp_path, monkeypatch) -> None:
    thread_id = "thread-running"
    runtime, history_store = _runtime_with_conversation(tmp_path, thread_id)
    monkeypatch.setattr(
        runtime,
        "state",
        lambda *_args, **_kwargs: SimpleNamespace(
            run=SimpleNamespace(state="running"),
            active_interrupt=None,
        ),
    )

    with pytest.raises(ThreadAlreadyRunningError):
        runtime.delete_conversation(thread_id)

    assert history_store.get("local-user", thread_id) is not None
