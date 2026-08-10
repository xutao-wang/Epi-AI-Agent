from pathlib import Path
from types import SimpleNamespace
import time

from api.conversation_history import ConversationHistoryStore
from api.runtime import ReportAgentApiRuntime


SETTINGS = {
    "model_name": "gpt-5.4",
    "temperature": 0.1,
    "top_p": 0.9,
    "max_steps": 2,
    "timeout_seconds": 10,
    "db_rag_embedding_model": "",
    "db_rag_reranker_model": "",
}


class Graph:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.snapshot = SimpleNamespace(values={}, next=(), interrupts=[])

    def get_state(self, _config, *, subgraphs: bool = False):
        return self.snapshot

    def invoke(self, payload, _config, **_kwargs) -> None:
        if self.fail:
            raise RuntimeError("first invoke failed")
        self.snapshot = SimpleNamespace(values=payload, next=(), interrupts=[])


def _runtime(tmp_path: Path, graph: Graph) -> ReportAgentApiRuntime:
    return ReportAgentApiRuntime(
        graph_factory=lambda _settings: graph,
        default_runtime_settings=SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=ConversationHistoryStore(tmp_path / "history.db"),
    )


def _wait_for_completion(runtime: ReportAgentApiRuntime, thread_id: str) -> str:
    deadline = time.time() + 2
    while time.time() < deadline:
        state = runtime.state(thread_id).run.state
        if state != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("background run did not finish")


def test_first_turn_is_visible_only_after_it_is_checkpointed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, Graph())
    thread_id = runtime.create_thread()

    assert runtime.list_conversations() == []
    runtime.submit_message(thread_id, "Create a cohort")

    assert _wait_for_completion(runtime, thread_id) == "done"
    assert [item.thread_id for item in runtime.list_conversations()] == [thread_id]


def test_failed_first_turn_is_not_left_in_history(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, Graph(fail=True))
    thread_id = runtime.create_thread()

    runtime.submit_message(thread_id, "Create a cohort")

    assert _wait_for_completion(runtime, thread_id) == "error"
    assert runtime.list_conversations() == []
