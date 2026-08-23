import threading
import time
from typing import Any
from types import SimpleNamespace
from pathlib import Path
import json
import io
import zipfile

import pandas as pd
import pytest
import sqlite3
from httpx import ReadTimeout, Request, Response
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from openai import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from api.runtime import (
    ApiGraphRunner,
    ModelReplacementRequiredError,
    ReportAgentApiRuntime,
    ThreadRuntime,
    ThreadAwaitingReviewError,
    ThreadAlreadyRunningError,
    _dataset_summary,
    project_thread_state,
)
from api.auth import AuthenticatedUser, RequestIdentity
from api.conversation_history import ConversationHistoryStore
from epi_agent import tool_call_protocol
from api.schemas import (
    ApiThreadState,
    ModelOption,
    ResumeInterruptRequest,
    RunStatus,
    RuntimeCapabilities,
    RuntimeCapability,
)
from db_rag.embedding_startup import EmbeddingStartupStatus
from graph.conversation_events import (
    append_conversation_event,
    build_attachment_event,
    build_assistant_event,
    build_clarification_exchange_event,
    build_user_event,
)
from utils.attachment_artifacts import AttachmentError, LocalAttachmentStore
from utils.dataset_artifacts import persist_dataset_artifact
from utils.model_runtime_profiles import model_runtime_profile


_DEFAULT_RUNTIME_SETTINGS = {
    "model_name": "gpt-5.4",
    "temperature": 0.1,
    "top_p": 0.9,
    "max_steps": 4,
    "timeout_seconds": 300,
    "db_rag_embedding_model": "OpenAI/text-embedding-3-large",
    "db_rag_reranker_model": "disabled",
}


def _identity(
    owner_user_id: str,
    session_id: str = "11111111-1111-4111-8111-111111111111",
) -> RequestIdentity:
    return RequestIdentity(
        user=AuthenticatedUser(owner_user_id=owner_user_id),
        session_id=session_id,
    )


_LOCAL_IDENTITY = _identity("local-user")


def _local_attachment_scope(thread_id: str) -> str:
    return thread_id


def test_runtime_capabilities_include_study_design() -> None:
    capabilities = RuntimeCapabilities(
        publication_knowledge=RuntimeCapability(
            status="available",
            message="Publication knowledge is available.",
        ),
        db_rag_dataset=RuntimeCapability(
            status="not_configured",
            message="DB-RAG dataset is not configured.",
        ),
    )

    assert capabilities.study_design.status == "available"


def test_model_profiles_declare_sampling_control_support() -> None:
    assert model_runtime_profile("gpt-5.4").supports_sampling_controls is True
    for model_id in (
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "gpt5.6-Luna-Light",
    ):
        assert (
            model_runtime_profile(model_id).supports_sampling_controls
            is False
        )

    assert (
        model_runtime_profile("gpt-5.6-sol").descriptor()[
            "supports_sampling_controls"
        ]
        is False
    )

    option = ModelOption(
        **model_runtime_profile("claude-sonnet-5").descriptor()
    )
    assert option.label == "Claude Sonnet 5 (Medium)"
    assert "reasoning_tier" not in option.model_dump()


def test_dataset_summary_prefers_provenance_title_over_legacy_description() -> None:
    summary = _dataset_summary(
        {
            "id": "subset-agent-deadbeef",
            "provenance": {"name": "Index Case Demographics"},
            "description": "Legacy dataset description",
        }
    )

    assert summary.id == "subset-agent-deadbeef"
    assert summary.label == "Index Case Demographics"


def test_dataset_summary_uses_legacy_top_level_description_when_provenance_empty() -> None:
    summary = _dataset_summary(
        {
            "id": "subset-agent-deadbeef",
            "description": "Legacy dataset description",
        }
    )

    assert summary.label == "Legacy dataset description"


def test_dataset_summary_uses_legacy_top_level_label_when_description_empty() -> None:
    summary = _dataset_summary(
        {
            "id": "subset-agent-deadbeef",
            "label": "Legacy dataset label",
        }
    )

    assert summary.label == "Legacy dataset label"


def test_dataset_summary_falls_back_to_id_when_artifact_has_no_title() -> None:
    summary = _dataset_summary({"id": "subset-agent-deadbeef"})

    assert summary.id == "subset-agent-deadbeef"
    assert summary.label == "subset-agent-deadbeef"


def _plan_review_snapshot(
    *,
    interrupt_id: str = "interrupt-1",
    artifact_id: str = "plan-1",
) -> SimpleNamespace:
    artifact = {
        "id": artifact_id,
        "kind": "dataset_plan",
        "version": 1,
        "status": "draft",
        "content": {},
        "provenance": {},
    }
    return SimpleNamespace(
        values={
            "artifacts": {
                "files": {
                    artifact_id: {
                        "artifact_id": artifact_id,
                        "kind": "dataset_plan",
                        "producer": "epi_agent",
                        "mime": "application/json",
                        "summary": "Dataset plan awaiting review.",
                        "status": "draft",
                        "content": artifact,
                        "created_at": "2026-07-28T00:00:00+00:00",
                    }
                }
            }
        },
        next=("epi_agent",),
        interrupts=[
            SimpleNamespace(
                id=interrupt_id,
                value={
                    "type": "dataset_plan_review",
                    "artifact": {
                        "id": artifact_id,
                        "kind": "dataset_plan",
                        "version": 1,
                        "expected_status": "draft",
                    },
                    "view": {
                        "dataset_title": "Diabetes cohort plan",
                        "goal": "Create a diabetes cohort.",
                        "concept_groups": [],
                        "selected_fields": [],
                        "filters": [],
                        "joins": [],
                        "unresolved_scientific_choices": [],
                    },
                },
            )
        ],
    )


def _model_output_limit_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        values={"model_output_state": {"phase": "awaiting_user"}},
        next=("model_output_gate",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-output",
                value={
                    "type": "model_output_limit",
                    "model_id": "gpt-5.6-sol",
                    "model_label": "gpt-5.6-sol (Medium)",
                    "automatic_token_ceiling": 50_000,
                    "continuation_tokens": 25_000,
                    "additional_output_cost": "$0.75",
                    "message": (
                        "gpt-5.6-sol (Medium) reached its output limit."
                    ),
                    "actions": ["continue", "cancel"],
                },
            )
        ],
    )


def _dataset_review_payload(
    artifact_id: str,
    *,
    kind: str = "subset",
    version: int = 1,
) -> dict:
    return {
        "type": "dataset_review",
        "artifact": {
            "id": artifact_id,
            "kind": kind,
            "version": version,
            "expected_status": "pending_review",
        },
        "view": {
            "goal": "Create an analysis-ready cohort.",
            "dimensions": {"rows": 1, "columns": 1},
            "columns": [],
            "filters": [],
            "quality": {},
            "warnings": [],
            "provenance": {},
            "feedback_history": [],
        },
    }


class _FakeGraph:
    def __init__(
        self,
        snapshots: list[SimpleNamespace],
        *,
        invoke_exception: Exception | None = None,
    ) -> None:
        self._snapshots = list(snapshots)
        self._invoke_exception = invoke_exception
        self.invoke_calls: list[tuple[dict, dict]] = []

    def invoke(self, payload: dict, config: dict) -> None:
        self.invoke_calls.append((payload, config))
        if self._invoke_exception is not None:
            raise self._invoke_exception

    def get_state(
        self,
        config: dict,
        *,
        subgraphs: bool = False,
    ) -> SimpleNamespace | None:
        if not self._snapshots:
            return None
        return self._snapshots.pop(0)


class _BlockingGraph:
    def __init__(self) -> None:
        self.invoke_started = threading.Event()
        self.release_invoke = threading.Event()
        self.invoke_calls: list[tuple[dict, dict]] = []
        self.values: dict[str, Any] = {}

    def invoke(self, payload: dict, config: dict) -> None:
        self.invoke_calls.append((payload, config))
        self.invoke_started.set()
        self.release_invoke.wait(timeout=5)
        self.values.update(payload)

    def get_state(
        self,
        config: dict,
        *,
        subgraphs: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(values=self.values, next=(), interrupts=[])


class _RecordingCheckpointer:
    def __init__(self) -> None:
        self.deleted_threads: list[str] = []

    def delete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)


class _RuntimeFakeGraph:
    def __init__(self, snapshot: SimpleNamespace | None) -> None:
        self.snapshot = snapshot
        self.get_state_calls: list[dict] = []
        self.get_state_subgraphs: list[bool] = []
        self.checkpointer = _RecordingCheckpointer()

    def get_state(
        self,
        config: dict,
        *,
        subgraphs: bool = False,
    ) -> SimpleNamespace | None:
        self.get_state_calls.append(config)
        self.get_state_subgraphs.append(subgraphs)
        return self.snapshot


class _RecordingGraphFactory:
    def __init__(self, snapshot: SimpleNamespace | None = None) -> None:
        self.calls: list[dict] = []
        self.snapshot = snapshot or SimpleNamespace(values={}, next=(), interrupts=[])

    def __call__(self, settings, _context):
        self.calls.append(settings.model_dump())
        return _RuntimeFakeGraph(self.snapshot)


class _ContextRecordingGraphFactory:
    def __init__(self, graphs: list[Any] | None = None) -> None:
        self.calls: list[tuple[dict[str, Any], Any]] = []
        self.graphs = list(graphs or [])

    def __call__(self, settings, context):
        self.calls.append((settings.model_dump(), context))
        if self.graphs:
            return self.graphs.pop(0)
        return _RuntimeFakeGraph(SimpleNamespace(values={}, next=(), interrupts=[]))


class _SlowGraphFactory:
    def __init__(self, graph: _BlockingGraph) -> None:
        self.graph = graph
        self.calls: list[dict] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, settings, _context):
        with self._lock:
            self.calls.append(settings.model_dump())
            self.entered.set()
        self.release.wait(timeout=5)
        return self.graph


class _ReleaseObservedLock:
    """Expose when the release worker contends for the runtime lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.release_attempted = threading.Event()
        self.release_acquired = threading.Event()

    def __enter__(self):
        is_release = threading.current_thread().name == "test-session-release"
        if is_release:
            self.release_attempted.set()
        self._lock.acquire()
        if is_release:
            self.release_acquired.set()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self._lock.release()


class _RecordingRunner:
    def __init__(self, *, already_running: bool = False) -> None:
        self.calls: list[dict] = []
        self.background_calls: list[dict] = []
        self.status_calls: list[str] = []
        self.already_running = already_running

    def run_until_blocked(
        self,
        *,
        thread_id: str,
        initial_payload: dict | Command | None,
        max_steps: int,
        timeout_seconds: float,
    ) -> dict:
        self.calls.append(
            {
                "thread_id": thread_id,
                "initial_payload": initial_payload,
                "max_steps": max_steps,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"state": "done", "steps": 1, "error": None}

    def start_background(
        self,
        *,
        thread_id: str,
        initial_payload: dict | Command | None,
        max_steps: int,
        timeout_seconds: float,
        checkpoint_config: dict | None = None,
    ) -> bool:
        self.background_calls.append(
            {
                "thread_id": thread_id,
                "initial_payload": initial_payload,
                "max_steps": max_steps,
                "timeout_seconds": timeout_seconds,
            }
        )
        return not self.already_running

    def start_background_from_factory(
        self,
        *,
        thread_id: str,
        payload_factory,
        max_steps: int,
        timeout_seconds: float,
        on_initial_payload_error=None,
        on_initial_payload_success=None,
        checkpoint_config: dict | None = None,
    ) -> bool:
        if self.already_running:
            return False
        started = self.start_background(
            thread_id=thread_id,
            initial_payload=payload_factory(),
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
            checkpoint_config=checkpoint_config,
        )
        if started and on_initial_payload_success is not None:
            on_initial_payload_success()
        return started

    def status(self, thread_id: str) -> dict:
        self.status_calls.append(thread_id)
        return {"state": "done", "steps": 1, "error": None}


def _runtime(
    graph,
    *,
    runner: _RecordingRunner | None = None,
    max_steps: int = 4,
    timeout_seconds: float = 9,
    runtime_root: Path | None = None,
    runtime_settings: dict | None = None,
    capabilities: RuntimeCapabilities | None = None,
    models: list[str] | None = None,
    embedding_startup_status: EmbeddingStartupStatus | None = None,
) -> ReportAgentApiRuntime:
    settings = {
        **_DEFAULT_RUNTIME_SETTINGS,
        "max_steps": max_steps,
        "timeout_seconds": timeout_seconds,
    }
    if runtime_settings:
        settings.update(runtime_settings)
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings, _context: graph,
        default_runtime_settings=settings,
        models=models or ["gpt-5.4", "gpt-5.6-luna"],
        runtime_root=runtime_root,
        **(
            {"embedding_startup_status": embedding_startup_status}
            if embedding_startup_status is not None
            else {}
        ),
        **({"capabilities": capabilities} if capabilities is not None else {}),
    )
    thread = runtime._thread("thread-1")
    thread.app = graph
    thread.runner = runner or ApiGraphRunner(graph)
    thread.credential_session_id = _LOCAL_IDENTITY.session_id
    return runtime


def test_runtime_create_thread_stores_default_settings() -> None:
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4", "gpt-5.6-luna"],
    )

    thread_id = runtime.create_thread()
    state = runtime.state(thread_id)

    assert state.runtime_settings is not None
    assert state.runtime_settings.model_name == "gpt-5.4"
    assert state.runtime_settings.temperature == 0.1
    assert state.runtime_settings.top_p == 0.9
    assert state.runtime_settings.max_steps == 4
    assert state.runtime_settings.timeout_seconds == 300
    assert state.runtime_settings_locked is False


def test_active_run_count_ignores_idle_and_stale_threads() -> None:
    class _StatusRunner:
        def __init__(self, state: str | None = None, *, stale: bool = False) -> None:
            self.state = state
            self.stale = stale

        def status(self, _thread_id: str) -> dict[str, str]:
            if self.stale:
                raise RuntimeError("stale runner")
            return {"state": self.state or "idle"}

    runtime = _runtime(_RuntimeFakeGraph(None))
    settings = runtime._threads[("local-user", "thread-1")].settings
    runtime._threads = {
        ("owner-a", "running-thread"): ThreadRuntime(
            settings=settings,
            runner=_StatusRunner("running"),
        ),
        ("owner-b", "idle-thread"): ThreadRuntime(
            settings=settings,
            runner=_StatusRunner("idle"),
        ),
        ("owner-c", "stale-thread"): ThreadRuntime(
            settings=settings,
            runner=_StatusRunner(stale=True),
        ),
    }

    assert runtime.active_run_count() == 1


def test_runtime_reopens_saved_thread_with_its_persisted_model(tmp_path: Path) -> None:
    history_store = ConversationHistoryStore(tmp_path / "agent_memory.db")
    history_store.create("local-user", "saved-thread", model_name="gpt-5.6-luna")
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4", "gpt-5.6-luna"],
        history_store=history_store,
    )

    reopened = runtime._thread(_LOCAL_IDENTITY, "saved-thread")

    assert reopened.settings.model_name == "gpt-5.6-luna"
    assert reopened.locked is True


def test_saved_gpt_thread_loads_when_only_claude_is_available(
    tmp_path: Path,
) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create(
        "local-user",
        "saved-thread",
        model_name="gpt-5.6-terra",
    )
    factory = _RecordingGraphFactory()
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings={
            **_DEFAULT_RUNTIME_SETTINGS,
            "model_name": "claude-opus-5",
        },
        models=["claude-opus-5"],
        history_store=history_store,
    )

    state = runtime.state(
        _LOCAL_IDENTITY,
        "saved-thread",
        provider_api_key="local-environment",
    )
    listed = runtime.list_conversations(_LOCAL_IDENTITY)

    assert state.runtime_settings is not None
    assert state.runtime_settings.model_name == "gpt-5.6-terra"
    assert state.model_label == "gpt-5.6-terra (Medium)"
    assert state.model_available is False
    assert state.model_replacement_required is True
    assert listed[0].thread_id == "saved-thread"
    assert factory.calls == []


def test_unknown_historical_model_id_remains_readable(tmp_path: Path) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create(
        "local-user",
        "legacy-thread",
        model_name="removed-custom-model",
    )
    factory = _RecordingGraphFactory()
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        history_store=history_store,
    )

    state = runtime.state(_LOCAL_IDENTITY, "legacy-thread")

    assert state.runtime_settings is not None
    assert state.runtime_settings.model_name == "removed-custom-model"
    assert state.model_label == "removed-custom-model"
    assert state.model_replacement_required is True
    assert factory.calls == []


def test_unavailable_historical_model_requires_explicit_replacement(
    tmp_path: Path,
) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create(
        "local-user",
        "saved-thread",
        model_name="gpt-5.6-terra",
    )
    factory = _RecordingGraphFactory()
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings={
            **_DEFAULT_RUNTIME_SETTINGS,
            "model_name": "claude-opus-5",
        },
        models=["claude-opus-5"],
        history_store=history_store,
    )

    with pytest.raises(ModelReplacementRequiredError, match="gpt-5.6-terra"):
        runtime.submit_message(
            _LOCAL_IDENTITY,
            "saved-thread",
            "continue",
            provider_api_key="local-environment",
        )

    runtime.submit_message(
        _LOCAL_IDENTITY,
        "saved-thread",
        "continue",
        model_name="claude-opus-5",
        provider_api_key="local-environment",
    )

    saved = history_store.get("local-user", "saved-thread")
    assert saved is not None
    assert saved.model_name == "claude-opus-5"
    assert factory.calls[0]["model_name"] == "claude-opus-5"
    state = runtime.state(_LOCAL_IDENTITY, "saved-thread")
    assert state.model_available is True
    assert state.model_replacement_required is False
    assert state.runtime_settings_locked is True


def test_failed_replacement_graph_does_not_rewrite_historical_model(
    tmp_path: Path,
) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create(
        "local-user",
        "saved-thread",
        model_name="gpt-5.6-terra",
    )

    def failing_factory(_settings, _context):
        raise RuntimeError("provider unavailable")

    runtime = ReportAgentApiRuntime(
        graph_factory=failing_factory,
        default_runtime_settings={
            **_DEFAULT_RUNTIME_SETTINGS,
            "model_name": "claude-opus-5",
        },
        models=["claude-opus-5"],
        history_store=history_store,
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        runtime.submit_message(
            _LOCAL_IDENTITY,
            "saved-thread",
            "continue",
            model_name="claude-opus-5",
            provider_api_key="local-environment",
        )

    saved = history_store.get("local-user", "saved-thread")
    assert saved is not None
    assert saved.model_name == "gpt-5.6-terra"
    thread = runtime._thread(_LOCAL_IDENTITY, "saved-thread")
    assert thread.settings.model_name == "gpt-5.6-terra"
    assert thread.model_available is False
    assert thread.app is None


def test_runtime_history_operations_are_scoped_to_request_identity(tmp_path: Path) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create("user-a", "thread-a", model_name="gpt-5.4")
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        history_store=history_store,
    )
    user_a = _identity("user-a")
    user_b = _identity("user-b")

    assert [record.thread_id for record in runtime.list_conversations(user_a)] == [
        "thread-a"
    ]
    assert runtime.list_conversations(user_b) == []
    assert runtime.rename_conversation(user_b, "thread-a", "stolen") is None
    assert runtime.open_conversation(user_b, "thread-a") is None
    assert runtime.archive_conversation(user_b, "thread-a") is None
    assert runtime.restore_conversation(user_b, "thread-a") is None
    assert runtime.delete_conversation(user_b, "thread-a") is False
    assert runtime.rename_conversation(user_a, "thread-a", "Owned title") is not None
    assert history_store.get("user-a", "thread-a").title == "Owned title"


def test_list_conversations_marks_only_owning_thread_as_awaiting_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity("owner-a")
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create("owner-a", "thread-a", model_name="gpt-5.4")
    history_store.create("owner-a", "thread-b", model_name="gpt-5.4")
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        checkpoint_path=tmp_path / "checkpoints.db",
        history_store=history_store,
    )
    snapshots = {
        "thread-a": _plan_review_snapshot(),
        "thread-b": SimpleNamespace(values={}, next=(), interrupts=[]),
    }
    monkeypatch.setattr(
        runtime,
        "_snapshot",
        lambda _identity, thread_id, _thread: snapshots[thread_id],
    )

    summaries = {
        item.thread_id: item
        for item in runtime.list_conversations(identity)
    }

    assert summaries["thread-a"].awaiting_review is True
    assert summaries["thread-b"].awaiting_review is False


def test_runtime_keeps_same_thread_id_isolated_by_owner_before_graph_access(
    tmp_path: Path,
) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create("user-a", "shared-thread", model_name="gpt-5.4")
    history_store.create("user-b", "shared-thread", model_name="gpt-5.4")
    graphs = [
        _RuntimeFakeGraph(SimpleNamespace(values={"owner": "user-a"}, next=(), interrupts=[])),
        _RuntimeFakeGraph(SimpleNamespace(values={"owner": "user-b"}, next=(), interrupts=[])),
    ]

    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings, _context: graphs.pop(0),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        history_store=history_store,
    )
    user_a = _identity("user-a")
    user_b = _identity("user-b")

    runtime.state(user_a, "shared-thread", provider_api_key="key-a")
    runtime.state(user_b, "shared-thread", provider_api_key="key-b")

    assert set(runtime._threads) == {
        ("user-a", "shared-thread"),
        ("user-b", "shared-thread"),
    }
    graph_a = runtime._threads[("user-a", "shared-thread")].app
    graph_b = runtime._threads[("user-b", "shared-thread")].app
    assert graph_a is not graph_b
    assert graph_a.get_state_calls[0] != graph_b.get_state_calls[0]
    assert runtime.delete_conversation(user_b, "shared-thread") is True
    assert graph_a.checkpointer.deleted_threads == []
    assert graph_b.checkpointer.deleted_threads[0] != "shared-thread"
    with pytest.raises(KeyError):
        runtime.state(_identity("user-c"), "shared-thread")


def test_runtime_scopes_same_thread_attachments_by_owner(tmp_path: Path) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    for owner in ("user-a", "user-b"):
        history_store.create(owner, "shared-thread", model_name="gpt-5.4")
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path,
        history_store=history_store,
    )
    user_a, user_b = _identity("user-a"), _identity("user-b")

    attachment_a = runtime.stage_attachments(
        user_a, "shared-thread", [("a.csv", "text/csv", b"id\n1\n")]
    ).attachments[0]
    attachment_b = runtime.stage_attachments(
        user_b, "shared-thread", [("b.csv", "text/csv", b"id\n2\n")]
    ).attachments[0]

    assert runtime.attachment_store.require(
        runtime._attachment_scope(user_a, "shared-thread"), attachment_a.id
    )["filename"] == "a.csv"
    assert runtime.attachment_store.require(
        runtime._attachment_scope(user_b, "shared-thread"), attachment_b.id
    )["filename"] == "b.csv"
    with pytest.raises(KeyError):
        runtime.conversation_attachment_bytes(user_b, "shared-thread", attachment_a.id)
    assert runtime.delete_conversation(user_b, "shared-thread") is True
    assert runtime.attachment_store.require(
        runtime._attachment_scope(user_a, "shared-thread"), attachment_a.id
    )["filename"] == "a.csv"


def test_state_recovers_one_idle_checkpoint_with_pending_work() -> None:
    snapshot = SimpleNamespace(
        values={"messages": []},
        next=("model",),
        interrupts=[],
    )
    graph = _RuntimeFakeGraph(snapshot)

    class RecoveryRunner(_RecordingRunner):
        running = False

        def status(self, thread_id: str) -> dict:
            self.status_calls.append(thread_id)
            return {
                "state": "running" if self.running else "idle",
                "steps": 0,
                "error": None,
            }

        def start_background(self, **kwargs: Any) -> bool:
            started = super().start_background(**kwargs)
            if started:
                self.running = True
            return started

    runner = RecoveryRunner()
    runtime = _runtime(graph, runner=runner)

    first = runtime.state(
        _LOCAL_IDENTITY, "thread-1", provider_api_key="test-key"
    )
    second = runtime.state(
        _LOCAL_IDENTITY, "thread-1", provider_api_key="test-key"
    )

    assert first.run.state == "running"
    assert second.run.state == "running"
    assert len(runner.background_calls) == 1
    assert runner.background_calls[0]["initial_payload"] is None


def test_runtime_deletes_history_checkpoints_and_attachments(tmp_path: Path) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create("local-user", "thread-a", model_name="gpt-5.4")
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}, next=(), interrupts=[]))
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings, _context: graph,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path,
        history_store=history_store,
    )
    runtime.attachment_store.stage(
        runtime._attachment_scope(_LOCAL_IDENTITY, "thread-a"),
        "cohort.csv",
        "text/csv",
        b"id\n1\n",
    )
    runtime._thread(_LOCAL_IDENTITY, "thread-a")
    runtime.state(_LOCAL_IDENTITY, "thread-a", provider_api_key="test-key")

    assert runtime.delete_conversation(_LOCAL_IDENTITY, "thread-a") is True
    assert history_store.get("local-user", "thread-a") is None
    assert "thread-a" not in runtime._threads
    assert graph.checkpointer.deleted_threads == ["thread-a"]
    assert not any(runtime.attachment_store.root.iterdir())


def test_runtime_reads_and_deletes_legacy_local_attachment_scope(
    tmp_path: Path,
) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create("local-user", "thread-a", model_name="gpt-5.4")
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}, next=(), interrupts=[]))
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings, _context: graph,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path,
        history_store=history_store,
    )
    staged = runtime.attachment_store.stage(
        "thread-a",
        "legacy.csv",
        "text/csv",
        b"id\n1\n",
    )
    available = runtime.attachment_store.mark_available("thread-a", staged["id"])
    graph.snapshot = SimpleNamespace(
        values={
            "artifacts": {
                "attachments": {available["id"]: available},
                "conversation_events": [
                    build_attachment_event(
                        actor="api",
                        user_turn_hash="legacy-turn",
                        artifact_id=available["id"],
                        relationship="input",
                        parent_event_id="user-legacy",
                    )
                ],
            }
        },
        next=(),
        interrupts=[],
    )
    runtime.state(_LOCAL_IDENTITY, "thread-a", provider_api_key="test-key")

    assert runtime.conversation_attachment_bytes(
        _LOCAL_IDENTITY, "thread-a", available["id"]
    ).content == b"id\n1\n"
    assert runtime.delete_conversation(_LOCAL_IDENTITY, "thread-a") is True
    with pytest.raises(AttachmentError, match="attachment was not found"):
        runtime.attachment_store.require("thread-a", available["id"])


def test_runtime_rejects_archive_while_conversation_is_running(tmp_path: Path) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    history_store.create("local-user", "thread-a", model_name="gpt-5.4")
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}, next=(), interrupts=[]))
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings, _context: graph,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        history_store=history_store,
    )
    thread = runtime._thread(_LOCAL_IDENTITY, "thread-a")
    thread.app = graph
    thread.runner = SimpleNamespace(
        status=lambda _thread_id: {"state": "running", "steps": 1, "error": None}
    )

    with pytest.raises(ThreadAlreadyRunningError):
        runtime.archive_conversation(_LOCAL_IDENTITY, "thread-a")


def test_runtime_create_thread_validates_custom_openai_model() -> None:
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4", "gpt-5.6-luna"],
    )

    thread_id = runtime.create_thread(
        {
            "model_name": "gpt-5.6-luna",
            "temperature": 0.2,
            "top_p": 0.8,
            "max_steps": 6,
            "timeout_seconds": 120,
        }
    )
    state = runtime.state(thread_id)

    assert state.runtime_settings is not None
    assert state.runtime_settings.model_name == "gpt-5.6-luna"
    assert state.runtime_settings.temperature is None
    assert state.runtime_settings.top_p is None
    assert state.runtime_settings.max_steps == 6
    assert state.runtime_settings.timeout_seconds == 120


def test_runtime_preserves_supported_gpt54_sampling_settings() -> None:
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4", "gpt-5.6-luna"],
    )

    thread_id = runtime.create_thread(
        {
            "model_name": "gpt-5.4",
            "temperature": 0.2,
            "top_p": 0.8,
        }
    )
    state = runtime.state(thread_id)

    assert state.runtime_settings is not None
    assert state.runtime_settings.temperature == 0.2
    assert state.runtime_settings.top_p == 0.8


def test_runtime_rejects_unsupported_model() -> None:
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
    )

    with pytest.raises(ValueError, match="Unsupported model"):
        runtime.create_thread({"model_name": "not-real"})


def test_runtime_rejects_unknown_runtime_setting() -> None:
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
    )

    with pytest.raises(ValueError, match="Unsupported runtime setting\\(s\\): surprise"):
        runtime.create_thread({"surprise": "ignored-before"})


def test_runtime_returns_base64_file_artifact_bytes() -> None:
    state = {
        "artifacts": {
            "files": {
                "figure-1": {
                    "artifact_id": "figure-1",
                    "kind": "figure",
                    "producer": "executor",
                    "mime": "image/png",
                    "summary": "Figure generated by approved final output.",
                    "status": "approved",
                    "content": {"data_base64": "cGxvdC1ieXRlcw=="},
                    "created_at": "2026-07-01T00:00:00+00:00",
                }
            }
        }
    }
    graph = _RuntimeFakeGraph(SimpleNamespace(values=state, next=(), interrupts=[]))
    runtime = _runtime(graph)

    artifact = runtime.file_artifact_bytes("thread-1", "figure-1")

    assert artifact.content == b"plot-bytes"
    assert artifact.mime == "image/png"
    assert artifact.filename == "figure-1.png"


def test_runtime_locks_settings_after_submit_and_resume() -> None:
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
    )
    submitted_thread = runtime.create_thread()
    resumed_thread = runtime.create_thread()
    resumed = runtime._thread(resumed_thread)
    resumed.app = _RuntimeFakeGraph(_plan_review_snapshot())
    resumed.runner = _RecordingRunner()
    resumed.credential_session_id = _LOCAL_IDENTITY.session_id

    runtime.submit_message(
        _LOCAL_IDENTITY, submitted_thread, "Create a diabetes cohort",
        provider_api_key="test-key",
    )
    runtime.resume_interrupt(
        _LOCAL_IDENTITY, resumed_thread, "interrupt-1", {"action": "approve"},
        provider_api_key="test-key",
    )

    assert runtime.state(submitted_thread).runtime_settings_locked is True
    assert runtime.state(resumed_thread).runtime_settings_locked is True


def _wait_for_history_title(
    history_store: ConversationHistoryStore,
    thread_id: str,
    expected: str,
) -> None:
    deadline = time.time() + 2
    while time.time() < deadline:
        record = history_store.get("local-user", thread_id)
        if record is not None and record.title == expected:
            return
        time.sleep(0.01)
    record = history_store.get("local-user", thread_id)
    assert record is not None
    assert record.title == expected


def test_runtime_generates_first_title_without_blocking_submit(tmp_path: Path) -> None:
    history_store = ConversationHistoryStore(tmp_path / "history.db")
    title_started = threading.Event()
    release_title = threading.Event()
    submit_finished = threading.Event()
    submit_errors: list[BaseException] = []
    title_factory_keys: list[str] = []

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
        title_generator_factory=lambda _settings, api_key: (
            title_factory_keys.append(api_key) or _BlockingTitleGenerator()
        ),
    )
    thread_id = runtime.create_thread()

    def submit() -> None:
        try:
            runtime.submit_message(
                _LOCAL_IDENTITY, thread_id, "Test the connection",
                provider_api_key="test-key",
            )
        except BaseException as exc:
            submit_errors.append(exc)
        finally:
            submit_finished.set()

    worker = threading.Thread(target=submit)
    worker.start()
    try:
        assert title_started.wait(timeout=1)
        assert submit_finished.wait(timeout=1), "submit_message waited for the title"
        record = history_store.get("local-user", thread_id)
        assert record is not None
        assert record.title == "Untitled conversation"
    finally:
        release_title.set()
        worker.join(timeout=2)

    assert submit_errors == []
    assert title_factory_keys == ["test-key"]
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

    runtime.submit_message(
        _LOCAL_IDENTITY, thread_id, "First message", provider_api_key="test-key"
    )
    assert title_attempted.wait(timeout=1)
    deadline = time.time() + 2
    while runtime.state(_LOCAL_IDENTITY, thread_id).run.state == "running" and time.time() < deadline:
        time.sleep(0.01)
    runtime.submit_message(
        _LOCAL_IDENTITY, thread_id, "Follow-up message", provider_api_key="test-key"
    )
    time.sleep(0.05)

    record = history_store.get("local-user", thread_id)
    assert record is not None
    assert record.title == "Untitled conversation"
    assert calls == ["First message"]


def test_runtime_late_automatic_title_preserves_manual_rename(tmp_path: Path) -> None:
    automatic_title_attempted = threading.Event()

    class _RecordingHistoryStore(ConversationHistoryStore):
        def set_automatic_title(
            self,
            owner_user_id: str,
            thread_id: str,
            title: str,
        ):
            try:
                return super().set_automatic_title(owner_user_id, thread_id, title)
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

    runtime.submit_message(
        _LOCAL_IDENTITY, thread_id, "Analyze cohort retention",
        provider_api_key="test-key",
    )
    assert title_started.wait(timeout=1)
    runtime.rename_conversation(_LOCAL_IDENTITY, thread_id, "My manual title")
    release_title.set()
    assert automatic_title_attempted.wait(timeout=1)

    record = history_store.get("local-user", thread_id)
    assert record is not None
    assert record.title == "My manual title"
    assert record.title_source == "manual"


def test_runtime_concurrent_first_submit_builds_one_graph_and_rejects_duplicate() -> None:
    graph = _BlockingGraph()
    graph_factory = _SlowGraphFactory(graph)
    runtime = ReportAgentApiRuntime(
        graph_factory=graph_factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
    )
    thread_id = runtime.create_thread()
    start = threading.Barrier(3)
    results: list[str] = []
    results_lock = threading.Lock()

    def submit(text: str) -> None:
        start.wait(timeout=5)
        try:
            runtime.submit_message(
                _LOCAL_IDENTITY, thread_id, text, provider_api_key="test-key"
            )
        except ThreadAlreadyRunningError:
            result = "already-running"
        else:
            result = "started"
        with results_lock:
            results.append(result)

    workers = [
        threading.Thread(target=submit, args=("Create cohort A",)),
        threading.Thread(target=submit, args=("Create cohort B",)),
    ]
    for worker in workers:
        worker.start()

    start.wait(timeout=5)
    assert graph_factory.entered.wait(timeout=1)
    time.sleep(0.05)
    graph_factory.release.set()
    try:
        for worker in workers:
            worker.join(timeout=2)

        assert sorted(results) == ["already-running", "started"]
        assert len(graph_factory.calls) == 1
    finally:
        graph.release_invoke.set()


def test_runtime_graph_factory_receives_selected_settings() -> None:
    graph_factory = _RecordingGraphFactory()
    runtime = ReportAgentApiRuntime(
        graph_factory=graph_factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4", "gpt-5.6-luna"],
    )
    thread_id = runtime.create_thread({"model_name": "gpt-5.6-luna", "max_steps": 6})

    runtime.submit_message(
        _LOCAL_IDENTITY, thread_id, "Create a diabetes cohort",
        provider_api_key="test-key",
    )

    assert graph_factory.calls == [
        {
            **_DEFAULT_RUNTIME_SETTINGS,
            "model_name": "gpt-5.6-luna",
            "temperature": None,
            "top_p": None,
            "max_steps": 6,
        }
    ]


def test_runtime_builds_graph_with_owner_session_key_and_storage_context(
    tmp_path: Path,
) -> None:
    history = ConversationHistoryStore(tmp_path / "history.db")
    factory = _ContextRecordingGraphFactory()
    identity = _identity("user-a")
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=history,
    )
    thread_id = runtime.create_thread(identity)

    assert factory.calls == []
    runtime.submit_message(
        identity,
        thread_id,
        "Create a cohort",
        provider_api_key="session-key",
    )

    assert len(factory.calls) == 1
    _settings, context = factory.calls[0]
    assert context.owner_user_id == "user-a"
    assert context.session_id == identity.session_id
    assert context.thread_id == thread_id
    assert context.provider_api_key == "session-key"
    assert context.storage.owner_user_id == "user-a"
    assert context.storage.thread_id == thread_id
    cached = runtime._threads[("user-a", thread_id)]
    assert cached.credential_session_id == identity.session_id
    assert not hasattr(cached, "provider_api_key")
    assert "session-key" not in repr(cached)
    assert "session-key" not in repr(context)
    assert "session-key" not in repr(factory.calls)


def test_runtime_same_thread_id_cache_is_owner_scoped(tmp_path: Path) -> None:
    history = ConversationHistoryStore(tmp_path / "history.db")
    history.create("user-a", "shared-thread", model_name="gpt-5.4")
    history.create("user-b", "shared-thread", model_name="gpt-5.4")
    factory = _ContextRecordingGraphFactory()
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=history,
    )

    runtime.state(_identity("user-a"), "shared-thread", provider_api_key="key-a")
    runtime.state(_identity("user-b"), "shared-thread", provider_api_key="key-b")

    assert set(runtime._threads) == {
        ("user-a", "shared-thread"),
        ("user-b", "shared-thread"),
    }
    assert [call[1].owner_user_id for call in factory.calls] == ["user-a", "user-b"]
    with pytest.raises(KeyError):
        runtime.state(_identity("user-c"), "shared-thread", provider_api_key="key-c")


def test_runtime_switching_session_rebuilds_an_idle_graph(tmp_path: Path) -> None:
    history = ConversationHistoryStore(tmp_path / "history.db")
    history.create("user-a", "thread-a", model_name="gpt-5.4")
    factory = _ContextRecordingGraphFactory()
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=history,
    )
    first = _identity("user-a", "11111111-1111-4111-8111-111111111111")
    second = _identity("user-a", "22222222-2222-4222-8222-222222222222")

    runtime.state(first, "thread-a", provider_api_key="first-key")
    first_graph = runtime._threads[("user-a", "thread-a")].app
    runtime.state(second, "thread-a", provider_api_key="second-key")

    cached = runtime._threads[("user-a", "thread-a")]
    assert cached.app is not first_graph
    assert cached.credential_session_id == second.session_id
    assert [call[1].provider_api_key for call in factory.calls] == [
        "first-key",
        "second-key",
    ]


def test_release_session_evicts_only_its_idle_graphs(tmp_path: Path) -> None:
    history = ConversationHistoryStore(tmp_path / "history.db")
    history.create("user-a", "thread-a", model_name="gpt-5.4")
    history.create("user-a", "thread-b", model_name="gpt-5.4")
    factory = _ContextRecordingGraphFactory()
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=history,
    )
    first = _identity("user-a", "11111111-1111-4111-8111-111111111111")
    second = _identity("user-a", "22222222-2222-4222-8222-222222222222")
    runtime.state(first, "thread-a", provider_api_key="first-key")
    runtime.state(second, "thread-b", provider_api_key="second-key")

    runtime.release_session("user-a", first.session_id)

    assert ("user-a", "thread-a") not in runtime._threads
    assert ("user-a", "thread-b") in runtime._threads


def test_release_session_during_blocked_factory_leaves_no_stale_graph(
    tmp_path: Path,
) -> None:
    history = ConversationHistoryStore(tmp_path / "history.db")
    identity = _identity("user-a")
    graph = _BlockingGraph()
    factory = _SlowGraphFactory(graph)
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=history,
    )
    thread_id = runtime.create_thread(identity)
    thread = runtime._require_owned_thread(identity, thread_id)
    observed_lock = _ReleaseObservedLock()
    runtime._lock = observed_lock
    build_errors: list[Exception] = []

    def build_graph() -> None:
        try:
            runtime._ensure_graph(identity, thread, "session-key")
        except Exception as exc:  # pragma: no cover - asserted below
            build_errors.append(exc)

    builder = threading.Thread(target=build_graph, name="test-graph-build")
    releaser = threading.Thread(
        target=lambda: runtime.release_session("user-a", identity.session_id),
        name="test-session-release",
    )
    builder.start()
    assert factory.entered.wait(timeout=1)
    releaser.start()
    assert observed_lock.release_attempted.wait(timeout=1)
    assert not observed_lock.release_acquired.wait(timeout=0.1)
    factory.release.set()
    builder.join(timeout=2)
    releaser.join(timeout=2)

    assert not builder.is_alive()
    assert not releaser.is_alive()
    assert build_errors == []
    assert ("user-a", thread_id) not in runtime._threads


def test_released_same_session_rebuilds_with_replacement_key(
    tmp_path: Path,
) -> None:
    history = ConversationHistoryStore(tmp_path / "history.db")
    history.create("user-a", "thread-a", model_name="gpt-5.4")
    factory = _ContextRecordingGraphFactory()
    identity = _identity("user-a")
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=history,
    )

    runtime.state(identity, "thread-a", provider_api_key="first-key")
    first_graph = runtime._threads[("user-a", "thread-a")].app
    runtime.release_session(identity.owner_user_id, identity.session_id)
    runtime.state(identity, "thread-a", provider_api_key="replacement-key")

    assert runtime._threads[("user-a", "thread-a")].app is not first_graph
    assert [call[1].provider_api_key for call in factory.calls] == [
        "first-key",
        "replacement-key",
    ]


def test_release_session_allows_bound_run_to_finish_then_evicts_graph(
    tmp_path: Path,
) -> None:
    history = ConversationHistoryStore(tmp_path / "history.db")
    graph = _BlockingGraph()
    factory = _ContextRecordingGraphFactory([graph])
    identity = _identity("user-a")
    runtime = ReportAgentApiRuntime(
        graph_factory=factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=history,
    )
    thread_id = runtime.create_thread(identity)
    runtime.submit_message(
        identity,
        thread_id,
        "Create a cohort",
        provider_api_key="session-key",
    )
    assert graph.invoke_started.wait(timeout=1)

    runtime.release_session("user-a", identity.session_id)
    assert runtime._threads[("user-a", thread_id)].app is graph
    graph.release_invoke.set()

    deadline = time.time() + 2
    while ("user-a", thread_id) in runtime._threads and time.time() < deadline:
        time.sleep(0.01)
    assert ("user-a", thread_id) not in runtime._threads
    with pytest.raises(ValueError, match="provider_api_key"):
        runtime.submit_message(
            identity,
            thread_id,
            "Continue",
            provider_api_key="",
        )


def test_runtime_initial_submit_bootstraps_root_epi_agent_state() -> None:
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}))
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner, max_steps=7, timeout_seconds=11)

    runtime.submit_message(
        _LOCAL_IDENTITY, "thread-1", "Create a diabetes cohort",
        provider_api_key="test-key",
    )

    assert graph.get_state_calls == [{"configurable": {"thread_id": "thread-1"}}]
    assert runner.calls == []
    assert len(runner.background_calls) == 1
    call = runner.background_calls[0]
    assert call["thread_id"] == "thread-1"
    assert call["max_steps"] == 7
    assert call["timeout_seconds"] == 11
    payload = call["initial_payload"]
    assert isinstance(payload, dict)
    assert [message.content for message in payload["messages"]] == [
        "Create a diabetes cohort"
    ]
    assert payload["output"] == {}
    assert payload["artifacts"]["datasets"] == {}
    assert "active_dataset_id" not in payload["artifacts"]
    assert [
        event["type"]
        for event in payload["artifacts"]["conversation_events"]
    ] == ["user"]
    assert payload["artifact_ids"] == []
    assert payload["authorized_attachment_ids"] == []
    assert "attachment_handling" not in payload
    assert payload["iteration_count"] == 0
    assert payload["failure_signatures"] == []
    assert payload["current_turn_artifact_refs"] == []
    assert payload["current_turn_output_artifact_refs"] == []
    assert payload["analysis_review_feedback_history"] == []
    assert payload["completion_blocked"] is False
    assert payload["model_output_state"] == {}
    assert payload["meta"]["thread_id"] == "thread-1"
    assert payload["meta"]["last_user_message_hash"]
    assert "orchestrator" not in payload
    assert "agents" not in payload


def test_runtime_initial_submit_has_no_sticky_study_in_graph_state() -> None:
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}))
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner)

    runtime.submit_message(
        _LOCAL_IDENTITY,
        "thread-1",
        "Use the second installed study.",
        provider_api_key="test-key",
    )

    payload = runner.background_calls[0]["initial_payload"]
    assert "active_study_id" not in payload


def test_runtime_later_submit_sends_message_and_event_log_delta() -> None:
    graph = _RuntimeFakeGraph(
        SimpleNamespace(
            values={
                "messages": [AIMessage(content="ready")],
                "meta": {"last_user_message_hash": "prior-turn"},
            }
        )
    )
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner, max_steps=3, timeout_seconds=5)

    runtime.submit_message(
        _LOCAL_IDENTITY, "thread-1", "Add HbA1c", provider_api_key="test-key"
    )

    assert graph.get_state_calls == [{"configurable": {"thread_id": "thread-1"}}]
    assert runner.calls == []
    assert len(runner.background_calls) == 1
    payload = runner.background_calls[0]["initial_payload"]
    assert isinstance(payload, dict)
    assert set(payload) == {
        "messages",
        "artifacts",
        "meta",
        "authorized_attachment_ids",
        "final_response",
        "iteration_count",
        "failure_signatures",
        "current_turn_artifact_refs",
        "current_turn_output_artifact_refs",
        "completion_blocked",
        "model_output_state",
        "terminal_error",
        "terminal_control",
    }
    assert len(payload["messages"]) == 1
    assert isinstance(payload["messages"][0], HumanMessage)
    assert payload["messages"][0].content == "Add HbA1c"
    assert payload["model_output_state"] == {}
    assert "attachment_handling" not in payload
    assert [
        event["type"]
        for event in payload["artifacts"]["conversation_events"]
    ] == ["user"]


def _assistant_tool_calls(*call_ids: str) -> AIMessage:
    return AIMessage(
        content="",
        id="assistant-tools",
        tool_calls=[
            {
                "name": f"tool_{index}",
                "args": {},
                "id": call_id,
                "type": "tool_call",
            }
            for index, call_id in enumerate(call_ids, start=1)
        ],
    )


def test_orphan_repair_inserts_only_missing_result_before_later_human() -> None:
    existing_result = ToolMessage(
        content="ok",
        tool_call_id="call-1",
        name="tool_1",
    )
    messages = [
        HumanMessage(content="first", id="user-1"),
        _assistant_tool_calls("call-1", "call-2"),
        existing_result,
        HumanMessage(content="already appended", id="user-2"),
    ]

    repair = tool_call_protocol.repair_orphaned_tool_calls(messages)

    assert repair.repaired_call_ids == ("call-2",)
    assert [type(message) for message in repair.messages] == [
        HumanMessage,
        AIMessage,
        ToolMessage,
        ToolMessage,
        HumanMessage,
    ]
    assert repair.messages[2] is existing_result
    inserted = repair.messages[3]
    assert isinstance(inserted, ToolMessage)
    assert inserted.tool_call_id == "call-2"
    assert json.loads(str(inserted.content))["error"]["code"] == (
        "INTERNAL_TOOL_ERROR"
    )


def test_orphan_repair_is_idempotent() -> None:
    first = tool_call_protocol.repair_orphaned_tool_calls(
        [_assistant_tool_calls("call-1")]
    )
    second = tool_call_protocol.repair_orphaned_tool_calls(first.messages)

    assert first.repaired_call_ids == ("call-1",)
    assert second.repaired_call_ids == ()
    assert second.messages == first.messages


def test_runtime_later_submit_atomically_repairs_orphan_before_follow_up() -> None:
    legacy = [
        HumanMessage(content="first", id="user-1"),
        _assistant_tool_calls("orphan-call"),
    ]
    graph = _RuntimeFakeGraph(
        SimpleNamespace(
            values={
                "messages": legacy,
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

    patch = runner.background_calls[0]["initial_payload"]
    merged = add_messages(legacy, patch["messages"])
    assert [type(message) for message in merged] == [
        HumanMessage,
        AIMessage,
        ToolMessage,
        HumanMessage,
    ]
    assert merged[2].tool_call_id == "orphan-call"
    assert merged[3].content == "Who are you?"
    assert patch["terminal_error"] is None


def test_active_interrupt_is_blocked_before_orphan_repair() -> None:
    graph = _RuntimeFakeGraph(
        SimpleNamespace(
            values={"messages": [_assistant_tool_calls("active-review-call")]},
            next=("tools",),
            interrupts=[
                SimpleNamespace(
                    id="interrupt-1",
                    value={"type": "dataset_plan_review"},
                )
            ],
        )
    )
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner)

    with pytest.raises(ThreadAwaitingReviewError):
        runtime.submit_message(
            _LOCAL_IDENTITY,
            "thread-1",
            "Continue",
            provider_api_key="test-key",
        )

    assert runner.background_calls == []


def test_repaired_follow_up_sequence_is_durable_and_provider_valid() -> None:
    legacy = [
        HumanMessage(content="first", id="user-1"),
        _assistant_tool_calls("orphan-call"),
    ]
    follow_up = HumanMessage(content="Who are you?", id="user-2")
    merged = add_messages(
        legacy,
        tool_call_protocol.follow_up_message_patch(legacy, follow_up),
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


def test_runtime_resume_interrupt_sends_command_resume_payload() -> None:
    plan = {
        "id": "plan-1",
        "kind": "dataset_plan",
        "version": 1,
        "status": "draft",
        "content": {},
        "provenance": {},
    }
    graph = _RuntimeFakeGraph(
        SimpleNamespace(
            values={
                "artifacts": {
                    "files": {"plan-1": {"content": plan}},
                }
            },
            interrupts=[
                SimpleNamespace(
                    id="interrupt-1",
                    value={
                        "type": "dataset_plan_review",
                        "artifact": {
                            "id": "plan-1",
                            "kind": "dataset_plan",
                            "version": 1,
                            "expected_status": "draft",
                        },
                        "view": {
                            "dataset_title": "Recurrence cohort plan",
                            "goal": "Create a recurrence cohort.",
                            "concept_groups": [],
                            "selected_fields": [],
                            "filters": [],
                            "joins": [],
                            "unresolved_scientific_choices": [],
                        },
                    },
                )
            ],
        )
    )
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner, max_steps=4, timeout_seconds=9)
    payload = {"action": "approve", "selected_column_keys": ["age"]}

    runtime.resume_interrupt(
        _LOCAL_IDENTITY, "thread-1", "interrupt-1", payload,
        provider_api_key="test-key",
    )

    assert runner.calls == []
    assert len(runner.background_calls) == 1
    call = runner.background_calls[0]
    assert call["thread_id"] == "thread-1"
    assert call["max_steps"] == 4
    assert call["timeout_seconds"] == 9
    command = call["initial_payload"]
    assert isinstance(command, Command)
    assert command.resume == {"interrupt-1": payload}


def test_runtime_resume_model_output_limit_sends_exact_continue() -> None:
    graph = _RuntimeFakeGraph(_model_output_limit_snapshot())
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner)

    runtime.resume_interrupt(
        _LOCAL_IDENTITY,
        "thread-1",
        "interrupt-output",
        {"action": "continue"},
        provider_api_key="test-key",
    )

    assert len(runner.background_calls) == 1
    command = runner.background_calls[0]["initial_payload"]
    assert isinstance(command, Command)
    assert command.resume == {
        "interrupt-output": {"action": "continue"}
    }


def test_clarification_resume_schema_requires_nonempty_typed_answer() -> None:
    request = ResumeInterruptRequest(
        action="answer",
        answer="Use the 12-month follow-up visit.",
    )

    assert request.model_dump(exclude_defaults=True) == {
        "action": "answer",
        "answer": "Use the 12-month follow-up visit.",
    }

    with pytest.raises(ValueError, match="answer"):
        ResumeInterruptRequest(action="answer", answer="   ")


def test_runtime_duplicate_submit_raises_thread_already_running() -> None:
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}))
    runner = _RecordingRunner(already_running=True)
    runtime = _runtime(graph, runner=runner, max_steps=4, timeout_seconds=9)

    with pytest.raises(ThreadAlreadyRunningError) as exc:
        runtime.submit_message(
            _LOCAL_IDENTITY, "thread-1", "Create a diabetes cohort",
            provider_api_key="test-key",
        )

    assert exc.value.thread_id == "thread-1"
    assert runner.background_calls == []
    assert graph.get_state_calls == [
        {"configurable": {"thread_id": "thread-1"}}
    ]


def test_runtime_rejects_submit_for_unresolved_raw_interrupt(
    tmp_path: Path,
) -> None:
    snapshot = SimpleNamespace(
        values={},
        next=("tools",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-hidden",
                value={"type": "unprojectable_review"},
            )
        ],
    )
    graph = _RuntimeFakeGraph(snapshot)
    runner = _RecordingRunner()
    runtime = _runtime(
        graph,
        runner=runner,
        runtime_root=tmp_path,
    )
    staged = runtime.stage_attachments(
        "thread-1",
        [("notes.txt", "text/plain", b"study notes")],
    ).attachments[0]

    with pytest.raises(ThreadAwaitingReviewError) as caught:
        runtime.submit_message(
            _LOCAL_IDENTITY,
            "thread-1",
            "Where is my analysis?",
            [staged.id],
            provider_api_key="test-key",
        )

    assert caught.value.thread_id == "thread-1"
    assert runner.background_calls == []
    assert runtime.attachment_store.require(
        _local_attachment_scope("thread-1"),
        staged.id,
    )["status"] == "staged"


def test_runtime_duplicate_resume_raises_thread_already_running() -> None:
    graph = _RuntimeFakeGraph(_plan_review_snapshot())
    runner = _RecordingRunner(already_running=True)
    runtime = _runtime(graph, runner=runner, max_steps=4, timeout_seconds=9)

    with pytest.raises(ThreadAlreadyRunningError) as exc:
        runtime.resume_interrupt(
            _LOCAL_IDENTITY, "thread-1", "interrupt-1", {"action": "approve"},
            provider_api_key="test-key",
        )

    assert exc.value.thread_id == "thread-1"
    assert len(runner.background_calls) == 1


def test_runtime_state_projects_checkpoint_with_runner_status() -> None:
    snapshot = SimpleNamespace(
        values={"messages": [HumanMessage(content="hello")]},
        next=(),
        interrupts=[],
    )
    graph = _RuntimeFakeGraph(snapshot)
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner, max_steps=4, timeout_seconds=9)

    state = runtime.state("thread-1")

    assert graph.get_state_calls == [{"configurable": {"thread_id": "thread-1"}}]
    assert graph.get_state_subgraphs == [True]
    assert runner.status_calls == ["thread-1"]
    assert state.thread_id == "thread-1"
    assert state.run.state == "done"
    assert [(message.role, message.text) for message in state.conversation] == [
        ("user", "hello")
    ]


def test_runtime_stages_multiple_attachments_without_mutating_graph_state(
    tmp_path: Path,
) -> None:
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}))
    runtime = _runtime(
        graph,
        runner=_RecordingRunner(),
        max_steps=4,
        timeout_seconds=9,
        runtime_root=tmp_path,
    )
    result = runtime.stage_attachments(
        "thread-1",
        [
            ("cohort.csv", "text/csv", b"subject_id,age\nSUB-1,42\n"),
            ("annotations.xml", "application/xml", b"<variables/>"),
        ],
    )

    assert [item.filename for item in result.attachments] == [
        "cohort.csv",
        "annotations.xml",
    ]
    assert all(item.status == "staged" for item in result.attachments)
    assert graph.get_state_calls == []


def test_runtime_submit_binds_exact_attachments_to_the_user_event(
    tmp_path: Path,
) -> None:
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}))
    runner = _RecordingRunner()
    runtime = _runtime(
        graph,
        runner=runner,
        max_steps=4,
        timeout_seconds=9,
        runtime_root=tmp_path,
    )
    staged = runtime.stage_attachments(
        "thread-1",
        [
            ("cohort.csv", "text/csv", b"subject_id,age\nSUB-1,42\n"),
            (
                "annotations.json",
                "application/json",
                b'{"subject_id": {"description": "Subject identifier"}}',
            ),
        ],
    )
    attachment_ids = [item.id for item in staged.attachments]

    runtime.submit_message(
        _LOCAL_IDENTITY, "thread-1", "Analyze uploaded data", attachment_ids,
        provider_api_key="test-key",
    )

    payload = runner.background_calls[0]["initial_payload"]
    assert isinstance(payload, dict)
    assert "active_dataset_id" not in payload["artifacts"]
    assert set(payload["artifacts"]["attachments"]) == set(attachment_ids)
    assert {
        manifest["status"]
        for manifest in payload["artifacts"]["attachments"].values()
    } == {"available"}
    assert {
        manifest["inspection"]["kind"]
        for manifest in payload["artifacts"]["attachments"].values()
    } == {"tabular", "structured"}
    assert payload["authorized_attachment_ids"] == sorted(attachment_ids)
    assert "attachment_handling" not in payload
    user_message = payload["messages"][0]
    assert user_message.content == "Analyze uploaded data"
    assert user_message.id
    assert user_message.additional_kwargs["attachment_ids"] == attachment_ids
    events = payload["artifacts"]["conversation_events"]
    user_event = next(event for event in events if event["type"] == "user")
    input_events = [event for event in events if event["type"] == "attachment"]
    assert len(input_events) == 2
    assert {event["artifact_id"] for event in input_events} == set(attachment_ids)
    assert {event["parent_event_id"] for event in input_events} == {
        user_event["event_id"]
    }


def test_runtime_submit_inspects_newly_bound_attachment_before_graph_execution(
    tmp_path: Path,
) -> None:
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}))
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner, runtime_root=tmp_path)
    attachment = runtime.stage_attachments(
        "thread-1",
        [("cohort.csv", "text/csv", b"id,sex\nSUB-1,F\n")],
    ).attachments[0]

    runtime.submit_message(
        _LOCAL_IDENTITY, "thread-1", "Use this cohort.", [attachment.id],
        provider_api_key="test-key",
    )

    payload = runner.background_calls[0]["initial_payload"]
    assert payload["artifacts"]["attachments"][attachment.id]["inspection"] == {
        "id": attachment.id,
        "filename": "cohort.csv",
        "kind": "tabular",
        "format": "csv",
        "mime": "text/csv",
        "byte_size": 15,
        "status": "binding",
        "columns": ["id", "sex"],
        "row_count": 1,
        "sample_rows": [{"id": "SUB-1", "sex": "F"}],
    }


def test_runtime_text_followup_rehydrates_prior_upload_profile(
    tmp_path: Path,
) -> None:
    store = LocalAttachmentStore(tmp_path)
    attachment = store.stage(
        _local_attachment_scope("thread-1"),
        "cohort.csv",
        "text/csv",
        b"id,sex\nSUB-1,F\n",
    )
    store.mark_available(_local_attachment_scope("thread-1"), attachment["id"])
    store.record_inspection(
        _local_attachment_scope("thread-1"),
        attachment["id"],
        {"id": attachment["id"], "columns": ["id", "sex"], "row_count": 1},
    )
    prior_state = append_conversation_event(
        {"artifacts": {"attachments": {}}, "meta": {}},
        build_attachment_event(
            actor="api",
            user_turn_hash="prior-turn",
            artifact_id=attachment["id"],
            relationship="input",
            parent_event_id="user-prior",
        ),
    )
    graph = _RuntimeFakeGraph(
        SimpleNamespace(
            values={
                "messages": [AIMessage(content="ready")],
                "meta": {"last_user_message_hash": "prior-turn"},
                "artifacts": prior_state["artifacts"],
            }
        )
    )
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner, runtime_root=tmp_path)

    runtime.submit_message(
        _LOCAL_IDENTITY, "thread-1", "Analyze my earlier upload.",
        provider_api_key="test-key",
    )

    payload = runner.background_calls[0]["initial_payload"]
    assert payload["authorized_attachment_ids"] == [attachment["id"]]
    assert payload["artifacts"]["attachments"][attachment["id"]][
        "inspection"
    ]["row_count"] == 1


def test_busy_submit_leaves_attachment_staged(tmp_path: Path) -> None:
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}))
    runner = _RecordingRunner(already_running=True)
    runtime = _runtime(
        graph,
        runner=runner,
        runtime_root=tmp_path,
    )
    staged = runtime.stage_attachments(
        "thread-1",
        [("notes.txt", "text/plain", b"study notes")],
    ).attachments[0]

    with pytest.raises(ThreadAlreadyRunningError):
        runtime.submit_message(
            _LOCAL_IDENTITY, "thread-1", "Read this", [staged.id],
            provider_api_key="test-key",
        )

    assert runtime.attachment_store.require(
        _local_attachment_scope("thread-1"),
        staged.id,
    )["status"] == "staged"


def test_snapshot_failure_leaves_attachment_staged(tmp_path: Path) -> None:
    class _FailingSnapshotGraph(_RuntimeFakeGraph):
        def get_state(self, config, subgraphs: bool = False):
            raise RuntimeError("checkpoint unavailable")

    graph = _FailingSnapshotGraph(SimpleNamespace(values={}))
    runtime = _runtime(
        graph,
        runner=_RecordingRunner(),
        runtime_root=tmp_path,
    )
    staged = runtime.stage_attachments(
        "thread-1",
        [("notes.txt", "text/plain", b"study notes")],
    ).attachments[0]

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        runtime.submit_message(
            _LOCAL_IDENTITY, "thread-1", "Read this", [staged.id],
            provider_api_key="test-key",
        )

    assert runtime.attachment_store.require(
        _local_attachment_scope("thread-1"),
        staged.id,
    )["status"] == "staged"


def test_conversation_attachment_bytes_is_thread_scoped(tmp_path: Path) -> None:
    graph = _RuntimeFakeGraph(SimpleNamespace(values={}))
    runner = _RecordingRunner()
    runtime = _runtime(
        graph,
        runner=runner,
        runtime_root=tmp_path,
    )
    staged = runtime.stage_attachments(
        "thread-1",
        [("notes.txt", "text/plain", b"study notes")],
    ).attachments[0]
    runtime.submit_message(
        _LOCAL_IDENTITY, "thread-1", "", [staged.id], provider_api_key="test-key"
    )
    graph.snapshot = SimpleNamespace(
        values=runner.background_calls[0]["initial_payload"],
        next=(),
        interrupts=[],
    )

    artifact = runtime.conversation_attachment_bytes("thread-1", staged.id)

    assert artifact.content == b"study notes"
    assert artifact.filename == "notes.txt"
    with pytest.raises(KeyError):
        runtime.conversation_attachment_bytes("thread-2", staged.id)


def test_conversation_attachment_bytes_rejects_available_but_unlinked_upload(
    tmp_path: Path,
) -> None:
    graph = _RuntimeFakeGraph(
        SimpleNamespace(values={}, next=(), interrupts=[]),
    )
    runtime = _runtime(
        graph,
        runner=_RecordingRunner(),
        runtime_root=tmp_path,
    )
    staged = runtime.stage_attachments(
        "thread-1",
        [("notes.txt", "text/plain", b"study notes")],
    ).attachments[0]
    runtime.attachment_store.mark_available(
        _local_attachment_scope("thread-1"), staged.id
    )

    with pytest.raises(KeyError):
        runtime.conversation_attachment_bytes("thread-1", staged.id)


def test_failed_initial_invoke_rolls_attachment_back_to_staged(
    tmp_path: Path,
) -> None:
    graph = _FakeGraph(
        [SimpleNamespace(values={}, next=(), interrupts=[])],
        invoke_exception=RuntimeError("checkpoint write failed"),
    )
    runtime = _runtime(graph, runtime_root=tmp_path)
    staged = runtime.stage_attachments(
        "thread-1",
        [("notes.txt", "text/plain", b"study notes")],
    ).attachments[0]

    runtime.submit_message(
        _LOCAL_IDENTITY, "thread-1", "Read this", [staged.id],
        provider_api_key="test-key",
    )
    deadline = time.time() + 2
    while runtime._thread("thread-1").runner.status("thread-1")["state"] == "running":
        assert time.time() < deadline
        time.sleep(0.01)

    assert runtime.attachment_store.require(
        _local_attachment_scope("thread-1"),
        staged.id,
    )["status"] == "staged"


def test_failed_invoke_preserves_attachment_when_input_link_was_committed(
    tmp_path: Path,
) -> None:
    class _PartiallyCommittedGraph:
        def __init__(self):
            self.snapshot = SimpleNamespace(
                values={},
                next=(),
                interrupts=[],
            )

        def get_state(self, _config, *, subgraphs=False):
            return self.snapshot

        def invoke(self, payload, _config):
            self.snapshot = SimpleNamespace(
                values=payload,
                next=(),
                interrupts=[],
            )
            raise RuntimeError("downstream node failed")

        async def ainvoke(self, payload, config):
            self.invoke(payload, config)

    graph = _PartiallyCommittedGraph()
    runtime = _runtime(graph, runtime_root=tmp_path)
    staged = runtime.stage_attachments(
        "thread-1",
        [("notes.txt", "text/plain", b"study notes")],
    ).attachments[0]

    runtime.submit_message(
        _LOCAL_IDENTITY, "thread-1", "Read this", [staged.id],
        provider_api_key="test-key",
    )
    deadline = time.time() + 2
    while runtime._thread("thread-1").runner.status("thread-1")["state"] == "running":
        assert time.time() < deadline
        time.sleep(0.01)

    assert runtime.attachment_store.require(
        _local_attachment_scope("thread-1"),
        staged.id,
    )["status"] == "available"
    assert runtime.conversation_attachment_bytes(
        "thread-1",
        staged.id,
    ).content == b"study notes"


def test_conversation_attachment_bytes_serves_only_linked_approved_output() -> None:
    state = {
        "artifacts": {
            "conversation_events": [
                {
                    "event_id": "output-link",
                    "seq": 1,
                    "created_at": "2026-07-27T00:00:00Z",
                    "type": "attachment",
                    "actor": "human_review_before_output",
                    "actor_role": "system",
                    "user_turn_hash": "turn-1",
                    "artifact_id": "figure-approved",
                    "relationship": "output",
                    "parent_event_id": "assistant-1",
                }
            ],
            "files": {
                "figure-approved": {
                    "artifact_id": "figure-approved",
                    "kind": "figure",
                    "producer": "executor",
                    "mime": "image/png",
                    "summary": "Approved figure",
                    "status": "approved",
                    "content": {"data_base64": "cGxvdA=="},
                },
                "figure-unlinked": {
                    "artifact_id": "figure-unlinked",
                    "kind": "figure",
                    "producer": "executor",
                    "mime": "image/png",
                    "summary": "Approved but not published in chat",
                    "status": "approved",
                    "content": {"data_base64": "aGlkZGVu"},
                },
            },
        }
    }
    runtime = _runtime(
        _RuntimeFakeGraph(
            SimpleNamespace(values=state, next=(), interrupts=[]),
        ),
        runner=_RecordingRunner(),
    )

    artifact = runtime.conversation_attachment_bytes(
        "thread-1",
        "figure-approved",
    )

    assert artifact.content == b"plot"
    assert artifact.mime == "image/png"
    with pytest.raises(KeyError):
        runtime.conversation_attachment_bytes(
            "thread-1",
            "figure-unlinked",
        )


def test_runtime_export_thread_returns_projected_state_dict() -> None:
    snapshot = SimpleNamespace(
        values={
            "messages": [
                HumanMessage(content="How many rows?"),
                AIMessage(content="There are 17 rows."),
            ],
            "output": {"qa_response": "There are 17 rows."},
            "artifacts": {
                "datasets": {
                    "subset-1": {
                        "id": "subset-1",
                        "description": "QA subset",
                        "row_count": 17,
                    }
                },
                "files": {
                    "plot-1": {
                        "artifact_id": "plot-1",
                        "kind": "figure",
                        "summary": "A plot",
                        "mime": "image/png",
                    }
                },
            },
        },
        next=(),
        interrupts=[],
    )
    graph = _RuntimeFakeGraph(snapshot)
    runner = _RecordingRunner()
    runtime = _runtime(graph, runner=runner, max_steps=4, timeout_seconds=9)

    exported = runtime.export_thread("thread-1")

    assert exported["thread_id"] == "thread-1"
    assert exported["run"]["state"] == "done"
    assert exported["conversation"] == [
        {
            "id": "message-0",
                "role": "user",
                "text": "How many rows?",
                "created_at": None,
                "attachments": [],
                "clarifications": [],
            },
        {
            "id": "message-1",
                "role": "assistant",
                "text": "There are 17 rows.",
                "created_at": None,
                "attachments": [],
                "clarifications": [],
            },
    ]
    assert exported["output"] == {"qa_response": "There are 17 rows."}
    assert exported["datasets"] == [
        {"id": "subset-1", "label": "QA subset", "row_count": 17}
    ]
    assert exported["file_artifacts"] == [
        {
            "id": "plot-1",
            "kind": "figure",
            "label": "A plot",
            "mime": "image/png",
            "status": "",
        }
    ]
    assert exported["active_interrupt"] is None
    assert exported["diagnostics"]["interrupt_count"] == 0


def test_runtime_export_thread_archive_includes_visible_message_attachments(
    tmp_path: Path,
) -> None:
    attachment_store = LocalAttachmentStore(tmp_path)
    staged = attachment_store.stage(
        _local_attachment_scope("thread-1"),
        "cohort.csv",
        "text/csv",
        b"subject_id,age\nSUB-1,42\n",
    )
    uploaded = attachment_store.mark_available(
        _local_attachment_scope("thread-1"), staged["id"]
    )
    dataset = persist_dataset_artifact(
        runtime_root=tmp_path,
        thread_id="thread-1",
        dataset_id="subset-1",
        kind="db_rag_sql_extraction",
        dataframe=pd.DataFrame([{"subject_id": "SUB-1", "age": 42}]),
        schema={"subject_id": {"description": "Subject identifier"}},
        provenance={"source": "db_rag"},
    )
    dataset["status"] = "active"
    snapshot = SimpleNamespace(
        values={
            "messages": [
                HumanMessage(content="Create a subset"),
                AIMessage(content="Generated code is ready."),
            ],
            "output": {
                "generated_code": "print('hello')",
                "text": "hello\n",
            },
            "artifacts": {
                "attachments": {uploaded["id"]: uploaded},
                "datasets": {"subset-1": dataset},
                "files": {
                    "figure-1": {
                        "artifact_id": "figure-1",
                        "kind": "figure",
                        "producer": "executor",
                        "mime": "image/png",
                        "summary": "Approved figure",
                        "status": "approved",
                        "created_at": "2026-07-27T00:00:03Z",
                        "content": {"data_base64": "cGxvdA=="},
                    },
                },
                "conversation_events": [
                    {
                        "event_id": "user-1",
                        "seq": 1,
                        "created_at": "2026-07-27T00:00:00Z",
                        "type": "user",
                        "actor": "user",
                        "actor_role": "user",
                        "user_turn_hash": "turn-1",
                        "text": "Create a subset",
                    },
                    {
                        "event_id": "input-1",
                        "seq": 2,
                        "created_at": "2026-07-27T00:00:01Z",
                        "type": "attachment",
                        "actor": "api",
                        "actor_role": "system",
                        "user_turn_hash": "turn-1",
                        "artifact_id": uploaded["id"],
                        "relationship": "input",
                        "parent_event_id": "user-1",
                    },
                    {
                        "event_id": "assistant-1",
                        "seq": 3,
                        "created_at": "2026-07-27T00:00:02Z",
                        "type": "assistant",
                        "actor": "human_review_before_output",
                        "actor_role": "assistant",
                        "user_turn_hash": "turn-1",
                        "text": "Generated code is ready.",
                    },
                    {
                        "event_id": "dataset-output",
                        "seq": 4,
                        "created_at": "2026-07-27T00:00:03Z",
                        "type": "attachment",
                        "actor": "rag_db_qa",
                        "actor_role": "system",
                        "user_turn_hash": "turn-1",
                        "artifact_id": "subset-1",
                        "relationship": "output",
                        "parent_event_id": "assistant-1",
                    },
                    {
                        "event_id": "figure-output",
                        "seq": 5,
                        "created_at": "2026-07-27T00:00:04Z",
                        "type": "attachment",
                        "actor": "human_review_before_output",
                        "actor_role": "system",
                        "user_turn_hash": "turn-1",
                        "artifact_id": "figure-1",
                        "relationship": "output",
                        "parent_event_id": "assistant-1",
                    },
                ],
            },
        },
        next=(),
        interrupts=[],
    )
    graph = _RuntimeFakeGraph(snapshot)
    runtime = _runtime(graph, runtime_root=tmp_path)

    archive_bytes = runtime.export_thread_archive("thread-1")

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        conversation = json.loads(archive.read("conversation.json"))
        attachment_manifest = json.loads(
            archive.read("attachments/manifest.json")
        )

        assert {
            "conversation.json",
            "conversation.md",
            "attachments/manifest.json",
            f"attachments/{uploaded['id']}/cohort.csv",
            "datasets/subset-1.csv",
            "datasets/subset-1.schema.json",
            "artifacts/figure-1.png",
        } <= names
        assert conversation["thread_id"] == "thread-1"
        assert conversation["conversation"][0]["content"] == "Create a subset"
        assert b"SUB-1" in archive.read("datasets/subset-1.csv")
        assert archive.read("artifacts/figure-1.png") == b"plot"
        assert [link["relationship"] for link in attachment_manifest["links"]] == [
            "input",
            "output",
            "output",
        ]


def test_runtime_export_thread_archive_omits_unselectable_dataset_artifacts(
    tmp_path: Path,
) -> None:
    approved = persist_dataset_artifact(
        runtime_root=tmp_path,
        thread_id="thread-1",
        dataset_id="approved-1",
        kind="db_rag_sql_extraction",
        dataframe=pd.DataFrame([{"subject_id": "SUB-1"}]),
        schema={"subject_id": {"description": "Subject identifier"}},
        provenance={"source": "db_rag"},
    )
    approved["status"] = "active"
    pending = persist_dataset_artifact(
        runtime_root=tmp_path,
        thread_id="thread-1",
        dataset_id="pending-1",
        kind="db_rag_sql_extraction",
        dataframe=pd.DataFrame([{"subject_id": "SUB-PENDING"}]),
        schema={"subject_id": {"description": "Subject identifier"}},
        provenance={"source": "db_rag"},
    )
    pending["status"] = "pending_review"
    cancelled = persist_dataset_artifact(
        runtime_root=tmp_path,
        thread_id="thread-1",
        dataset_id="cancelled-1",
        kind="db_rag_sql_extraction",
        dataframe=pd.DataFrame([{"subject_id": "SUB-CANCELLED"}]),
        schema={"subject_id": {"description": "Subject identifier"}},
        provenance={"source": "db_rag"},
    )
    cancelled["status"] = "cancelled"
    snapshot = SimpleNamespace(
        values={
            "artifacts": {
                "datasets": {
                    "approved-1": approved,
                    "pending-1": pending,
                    "cancelled-1": cancelled,
                },
                "conversation_events": [
                    {
                        "event_id": f"link-{dataset_id}",
                        "seq": index,
                        "created_at": "2026-07-27T00:00:00Z",
                        "type": "attachment",
                        "actor": "rag_db_qa",
                        "actor_role": "system",
                        "user_turn_hash": "turn-1",
                        "artifact_id": dataset_id,
                        "relationship": "output",
                        "parent_event_id": "assistant-1",
                    }
                    for index, dataset_id in enumerate(
                        ("approved-1", "pending-1", "cancelled-1"),
                        start=1,
                    )
                ],
            },
        },
        next=(),
        interrupts=[],
    )
    runtime = _runtime(_RuntimeFakeGraph(snapshot), runtime_root=tmp_path)

    archive_bytes = runtime.export_thread_archive("thread-1")

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("attachments/manifest.json"))

        assert "datasets/approved-1.csv" in names
        assert "datasets/pending-1.csv" not in names
        assert "datasets/cancelled-1.csv" not in names
        assert [link["artifact_id"] for link in manifest["links"]] == [
            "approved-1"
        ]


def test_project_thread_state_projects_file_artifacts() -> None:
    snapshot = SimpleNamespace(
        values={
            "messages": [],
            "artifacts": {
                "files": {
                    "figure-1": {
                        "artifact_id": "figure-1",
                        "kind": "figure",
                        "producer": "executor",
                        "mime": "image/png",
                        "summary": "Figure generated by approved final output.",
                        "status": "approved",
                        "content": {"path": "/tmp/figure.png"},
                        "created_at": "2026-06-23T00:00:00+00:00",
                    }
                }
            },
        },
        next=(),
        interrupts=[],
    )

    state = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 1, "error": None},
    )

    assert [artifact.model_dump() for artifact in state.file_artifacts] == [
        {
            "id": "figure-1",
            "kind": "figure",
            "label": "Figure generated by approved final output.",
            "mime": "image/png",
            "status": "approved",
        }
    ]


def test_project_thread_state_hides_pending_executor_artifacts() -> None:
    snapshot = SimpleNamespace(
        values={
            "messages": [],
            "artifacts": {
                "files": {
                    "figure-1": {
                        "artifact_id": "figure-1",
                        "kind": "figure",
                        "producer": "executor",
                        "mime": "image/png",
                        "summary": "Figure awaiting final output approval.",
                        "status": "pending_review",
                        "content": {"data_base64": "cG5n"},
                    }
                }
            },
        },
        next=("human_review_before_output",),
        interrupts=[],
    )

    state = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "running", "steps": 1, "error": None},
    )

    assert state.file_artifacts == []


def test_project_thread_state_attaches_only_approved_output_to_message() -> None:
    values = append_conversation_event(
        {"messages": []},
        build_assistant_event(
            actor="human_review_before_output",
            user_turn_hash="turn-1",
            text="Approved output.",
        ),
    )
    assistant_event_id = values["artifacts"]["conversation_events"][-1]["event_id"]
    values = append_conversation_event(
        values,
        build_attachment_event(
            actor="human_review_before_output",
            user_turn_hash="turn-1",
            artifact_id="figure-1",
            relationship="output",
            parent_event_id=assistant_event_id,
        ),
    )
    values["artifacts"]["files"] = {
        "figure-1": {
            "artifact_id": "figure-1",
            "kind": "figure",
            "producer": "epi_agent",
            "mime": "image/png",
            "summary": "Figure generated by approved final output.",
            "status": "active",
            "content": {"data_base64": "cG5n"},
            "created_at": "2026-07-26T22:12:01Z",
        },
        "unrelated-figure": {
            "artifact_id": "unrelated-figure",
            "kind": "figure",
            "producer": "epi_agent",
            "mime": "image/png",
            "summary": "Unrelated active figure.",
            "status": "active",
            "content": {"data_base64": "cG5n"},
            "created_at": "2026-07-26T22:12:01Z",
        }
    }

    state = project_thread_state(
        thread_id="thread-1",
        snapshot=SimpleNamespace(values=values, next=(), interrupts=[]),
        run_status={"state": "done", "steps": 1, "error": None},
    )

    assert [attachment.id for attachment in state.conversation[0].attachments] == [
        "figure-1"
    ]
    assert state.conversation[0].attachments[0].relationship == "output"
    assert {artifact.id for artifact in state.file_artifacts} == {
        "figure-1",
        "unrelated-figure",
    }


def test_project_thread_state_projects_semantic_display_history() -> None:
    values = {
        "messages": [
            HumanMessage(content="raw user message"),
            AIMessage(content="raw assistant message"),
        ]
    }
    values = append_conversation_event(
        values,
        build_user_event(
            actor="api",
            user_turn_hash="turn-1",
            text="semantic user message",
        ),
    )
    values = append_conversation_event(
        values,
        build_assistant_event(
            actor="rag_db_qa",
            user_turn_hash="turn-1",
            text="semantic assistant message",
        ),
    )
    events = list(values["artifacts"]["conversation_events"])
    values["artifacts"]["files"] = {
        "figure-1": {"artifact_id": "figure-1", "mime": "image/png"}
    }
    snapshot = SimpleNamespace(values=values, next=(), interrupts=[])

    state = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 1, "error": None},
    )

    assert [(message.role, message.text, message.created_at) for message in state.conversation] == [
        ("user", "semantic user message", events[0]["created_at"]),
        ("assistant", "semantic assistant message", events[1]["created_at"]),
    ]


def test_project_thread_state_projects_clarifications_on_final_assistant_message() -> None:
    values = append_conversation_event(
        {"messages": []},
        build_user_event(
            actor="api",
            user_turn_hash="turn-1",
            text="Create an outcome dataset.",
        ),
    )
    values = append_conversation_event(
        values,
        build_clarification_exchange_event(
            actor="rag_db_qa",
            user_turn_hash="turn-1",
            interrupt_id="interrupt-1",
            question="Which visit should be used?",
            reason="",
            answer="Use 12 months.",
        ),
    )
    values = append_conversation_event(
        values,
        build_assistant_event(
            actor="rag_db_qa",
            user_turn_hash="turn-1",
            text="I created the requested dataset.",
        ),
    )

    state = project_thread_state(
        thread_id="thread-1",
        snapshot=SimpleNamespace(values=values, next=(), interrupts=[]),
        run_status={"state": "done", "steps": 1, "error": None},
    )

    assert state.conversation[-1].clarifications[0].answer == "Use 12 months."


def test_runtime_file_artifact_bytes_returns_inline_text_content() -> None:
    graph = _RuntimeFakeGraph(
        SimpleNamespace(
            values={
                "artifacts": {
                    "files": {
                        "text-1": {
                            "artifact_id": "text-1",
                            "kind": "text",
                            "producer": "executor",
                            "mime": "text/plain",
                            "summary": "Execution output from approved final output.",
                            "status": "approved",
                            "content": "hello from execution",
                            "created_at": "2026-06-23T00:00:00+00:00",
                        }
                    }
                }
            }
        )
    )
    runtime = _runtime(
        graph,
        runner=_RecordingRunner(),
        max_steps=4,
        timeout_seconds=9,
    )

    artifact = runtime.file_artifact_bytes("thread-1", "text-1")

    assert artifact.content == b"hello from execution"
    assert artifact.mime == "text/plain"
    assert artifact.filename == "text-1.txt"


def test_runtime_file_artifact_bytes_reads_path_content(tmp_path: Path) -> None:
    figure_path = tmp_path / "figure.png"
    figure_path.write_bytes(b"\x89PNG\r\n")
    graph = _RuntimeFakeGraph(
        SimpleNamespace(
            values={
                "artifacts": {
                    "files": {
                        "figure-1": {
                            "artifact_id": "figure-1",
                            "kind": "figure",
                            "producer": "executor",
                            "mime": "image/png",
                            "summary": "Figure",
                            "status": "approved",
                            "content": {"path": str(figure_path)},
                            "created_at": "2026-06-23T00:00:00+00:00",
                        }
                    }
                }
            }
        )
    )
    runtime = _runtime(
        graph,
        runner=_RecordingRunner(),
        max_steps=4,
        timeout_seconds=9,
    )

    artifact = runtime.file_artifact_bytes("thread-1", "figure-1")

    assert artifact.content == b"\x89PNG\r\n"
    assert artifact.mime == "image/png"
    assert artifact.filename == "figure-1.png"


def test_runtime_file_artifact_access_rejects_retired_final_review_preview() -> None:
    files = {
        "pending-active": {
            "artifact_id": "pending-active",
            "kind": "figure",
            "producer": "executor",
            "mime": "image/png",
            "summary": "Figure awaiting final output approval.",
            "status": "pending_review",
            "content": {"data_base64": "YWN0aXZl"},
        },
        "pending-stale": {
            "artifact_id": "pending-stale",
            "kind": "figure",
            "producer": "executor",
            "mime": "image/png",
            "summary": "Figure awaiting final output approval.",
            "status": "pending_review",
            "content": {"data_base64": "c3RhbGU="},
        },
        "discarded": {
            "artifact_id": "discarded",
            "kind": "figure",
            "producer": "executor",
            "mime": "image/png",
            "summary": "Figure awaiting final output approval.",
            "status": "discarded",
            "content": {"data_base64": "ZGlzY2FyZGVk"},
        },
    }
    snapshot = SimpleNamespace(
        values={
            "agents": {"human_review": {"final_decision": None}},
            "artifacts": {"files": files},
        },
        next=("human_review_before_output",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-final",
                value={
                    "type": "final_review",
                    "figure_artifact_id": "pending-active",
                },
            )
        ],
    )
    runtime = _runtime(_RuntimeFakeGraph(snapshot))

    with pytest.raises(KeyError):
        runtime.file_artifact_bytes("thread-1", "pending-active")
    with pytest.raises(KeyError):
        runtime.file_artifact_bytes("thread-1", "pending-stale")
    with pytest.raises(KeyError):
        runtime.file_artifact_bytes("thread-1", "discarded")


def test_runtime_export_thread_archive_omits_unapproved_executor_artifacts() -> None:
    files = {
        "approved": {
            "artifact_id": "approved",
            "kind": "figure",
            "producer": "executor",
            "mime": "image/png",
            "summary": "Figure generated by approved final output.",
            "status": "approved",
            "created_at": "2026-07-27T00:00:00Z",
            "content": {"data_base64": "YXBwcm92ZWQ="},
        },
        "pending": {
            "artifact_id": "pending",
            "kind": "figure",
            "producer": "executor",
            "mime": "image/png",
            "summary": "Figure awaiting final output approval.",
            "status": "pending_review",
            "created_at": "2026-07-27T00:00:00Z",
            "content": {"data_base64": "cGVuZGluZw=="},
        },
        "discarded": {
            "artifact_id": "discarded",
            "kind": "figure",
            "producer": "executor",
            "mime": "image/png",
            "summary": "Figure awaiting final output approval.",
            "status": "discarded",
            "created_at": "2026-07-27T00:00:00Z",
            "content": {"data_base64": "ZGlzY2FyZGVk"},
        },
    }
    snapshot = SimpleNamespace(
        values={
            "artifacts": {
                "files": files,
                "conversation_events": [
                    {
                        "event_id": f"link-{status}",
                        "seq": index,
                        "created_at": "2026-07-27T00:00:00Z",
                        "type": "attachment",
                        "actor": "human_review_before_output",
                        "actor_role": "system",
                        "user_turn_hash": "turn-1",
                        "artifact_id": status,
                        "relationship": "output",
                        "parent_event_id": "assistant-1",
                    }
                    for index, status in enumerate(
                        ("approved", "pending", "discarded"),
                        start=1,
                    )
                ],
            }
        },
        next=(),
        interrupts=[],
    )
    runtime = _runtime(_RuntimeFakeGraph(snapshot))

    archive_bytes = runtime.export_thread_archive("thread-1")

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("artifacts.json"))

    assert [entry["artifact_id"] for entry in manifest] == ["approved"]
    assert "artifacts/approved.png" in names
    assert "artifacts/pending.png" not in names
    assert "artifacts/discarded.png" not in names


def test_runtime_returns_dataset_preview_from_registered_artifact(tmp_path) -> None:
    artifact = persist_dataset_artifact(
        runtime_root=tmp_path,
        thread_id="thread-1",
        dataset_id="subset-1",
        kind="db_rag_subset",
        dataframe=pd.DataFrame(
            {
                "person_id": [1, 2],
                "condition": ["diabetes", None],
            }
        ),
        schema={"person_id": {"dataType": "integer"}},
        provenance={"description": "Diabetes subset"},
    )
    snapshot = SimpleNamespace(
        values={"artifacts": {"datasets": {"subset-1": artifact}}},
        next=(),
        interrupts=[],
    )
    runtime = _runtime(
        _RuntimeFakeGraph(snapshot),
        runner=_RecordingRunner(),
        max_steps=4,
        timeout_seconds=9,
    )

    preview = runtime.dataset_preview("thread-1", "subset-1", limit=1)

    assert preview.dataset_id == "subset-1"
    assert preview.columns == ["person_id", "condition"]
    assert preview.rows == [{"person_id": 1, "condition": "diabetes"}]
    assert preview.row_count == 2


def test_runtime_dataset_access_blocks_stale_unselectable_dataset_artifacts(tmp_path) -> None:
    pending_artifact = persist_dataset_artifact(
        runtime_root=tmp_path,
        thread_id="thread-1",
        dataset_id="pending-1",
        kind="subset",
        dataframe=pd.DataFrame({"person_id": [1]}),
        schema={"person_id": {"dataType": "integer"}},
        provenance={
            "description": "Pending subset",
            "sql": "SELECT person_id FROM baseline_subjects",
        },
    )
    pending_artifact["status"] = "pending_review"
    pending_artifact["version"] = 1
    cancelled_artifact = persist_dataset_artifact(
        runtime_root=tmp_path,
        thread_id="thread-1",
        dataset_id="cancelled-1",
        kind="subset",
        dataframe=pd.DataFrame({"person_id": [2]}),
        schema={"person_id": {"dataType": "integer"}},
        provenance={"description": "Cancelled subset"},
    )
    cancelled_artifact["status"] = "cancelled"
    cancelled_artifact["version"] = 1
    snapshot = SimpleNamespace(
        values={
            "artifacts": {
                "datasets": {
                    "pending-1": pending_artifact,
                    "cancelled-1": cancelled_artifact,
                }
            },
        },
        next=("rag_db_qa",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-dataset",
                value=_dataset_review_payload("pending-1"),
            )
        ],
    )
    runtime = _runtime(
        _RuntimeFakeGraph(snapshot),
        runner=_RecordingRunner(),
        max_steps=4,
        timeout_seconds=9,
    )

    preview = runtime.dataset_preview("thread-1", "pending-1", limit=1)
    schema = runtime.dataset_schema("thread-1", "pending-1")
    sql_provenance = runtime.dataset_provenance("thread-1", "pending-1")

    assert preview.dataset_id == "pending-1"
    assert schema.dataset_id == "pending-1"
    assert sql_provenance.sql == "SELECT person_id FROM baseline_subjects"
    with pytest.raises(KeyError):
        runtime.dataset_preview("thread-1", "cancelled-1", limit=1)
    with pytest.raises(KeyError):
        runtime.dataset_provenance("thread-1", "cancelled-1")
    with pytest.raises(KeyError):
        runtime.dataset_csv_bytes("thread-1", "pending-1")


def test_runtime_create_and_reset_return_new_thread_ids() -> None:
    runtime = _runtime(
        _RuntimeFakeGraph(SimpleNamespace(values={})),
        runner=_RecordingRunner(),
        max_steps=4,
        timeout_seconds=9,
    )

    created = runtime.create_thread()
    reset = runtime.reset("old-thread")

    assert created
    assert reset
    assert created != "old-thread"
    assert reset != "old-thread"
    assert created != reset


def test_runtime_create_thread_accepts_optional_runtime_settings_payload() -> None:
    runtime = _runtime(
        _RuntimeFakeGraph(SimpleNamespace(values={})),
        runner=_RecordingRunner(),
        max_steps=4,
        timeout_seconds=9,
    )

    default_created = runtime.create_thread(runtime_settings=None)
    custom_created = runtime.create_thread({"model_name": "gpt-5.6-luna"})

    assert default_created
    assert custom_created
    assert default_created != custom_created


def test_runtime_options_returns_existing_runtime_defaults() -> None:
    runtime = _runtime(
        _RuntimeFakeGraph(SimpleNamespace(values={})),
        runner=_RecordingRunner(),
        max_steps=4,
        timeout_seconds=9,
        runtime_settings={
            "model_name": "gpt-5.4",
            "temperature": 0.1,
            "top_p": 0.9,
            "db_rag_embedding_model": "OpenAI/text-embedding-3-large",
            "db_rag_reranker_model": "disabled",
        },
        capabilities=RuntimeCapabilities(
            publication_knowledge=RuntimeCapability(
                status="available",
                message="Publication knowledge is available.",
            ),
            db_rag_dataset=RuntimeCapability(
                status="not_configured",
                message="DB-RAG dataset is not configured.",
            ),
        ),
        models=["gpt-5.4"],
    )

    options = runtime.runtime_options()

    assert options.defaults.model_dump() == {
        "model_name": "gpt-5.4",
        "temperature": 0.1,
        "top_p": 0.9,
        "max_steps": 4,
        "timeout_seconds": 9.0,
        "db_rag_embedding_model": "OpenAI/text-embedding-3-large",
        "db_rag_reranker_model": "disabled",
    }
    assert [model.id for model in options.models] == ["gpt-5.4"]
    assert options.capabilities.model_dump() == {
        "publication_knowledge": {
            "status": "available",
            "message": "Publication knowledge is available.",
        },
        "db_rag_dataset": {
            "status": "not_configured",
            "message": "DB-RAG dataset is not configured.",
        },
        "study_design": {
            "status": "available",
            "message": "Study design knowledge is available.",
        },
    }


def test_runtime_options_and_thread_state_share_live_embedding_startup_status() -> None:
    status = EmbeddingStartupStatus(
        profile_id="test-profile",
        profile_label="Test embedding model",
        provider="test-provider",
        index_compatibility="Test/test-model",
        available=False,
        retrieval_mode="lexical_fallback",
        reason_code="EMBEDDING_CREDENTIALS_MISSING",
        message="Semantic embedding search is unavailable.",
        compatible_study_ids=("study-a",),
        incompatible_study_ids=(),
    )
    runtime = _runtime(
        _RuntimeFakeGraph(SimpleNamespace(values={})),
        runner=_RecordingRunner(),
        embedding_startup_status=status,
    )

    options = runtime.runtime_options()
    state = runtime.state("thread-1")

    assert options.embedding_startup_status is status
    assert state.embedding_startup_status is status
    assert "embedding_startup_status" not in state.diagnostics


def test_runtime_options_expose_ordered_model_descriptors() -> None:
    runtime = _runtime(
        _RuntimeFakeGraph(SimpleNamespace(values={})),
        runner=_RecordingRunner(),
        runtime_settings={"model_name": "gpt-5.4"},
        models=[
            "gpt-5.4",
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "gpt-5.6-sol",
        ],
    )

    options = runtime.runtime_options()

    assert [model.id for model in options.models] == [
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]
    assert options.models[-1].label == "gpt-5.6-sol (Medium)"
    assert options.models[-1].automatic_output_cost == "$1.50"
    assert options.models[-1].incremental_output_cost == "$0.75"
    gpt56 = next(model for model in options.models if model.id == "gpt-5.6-luna")
    assert gpt56.supports_sampling_controls is False


def test_selected_model_supplies_locked_workflow_deadline() -> None:
    runtime = _runtime(
        _RuntimeFakeGraph(SimpleNamespace(values={})),
        runner=_RecordingRunner(),
        runtime_settings={"model_name": "gpt-5.4"},
        models=["gpt-5.4", "gpt-5.6-terra"],
    )

    thread_id = runtime.create_thread({"model_name": "gpt-5.6-terra"})
    state = runtime.state(thread_id)

    assert state.runtime_settings is not None
    assert state.runtime_settings.model_name == "gpt-5.6-terra"
    assert state.runtime_settings.timeout_seconds == 420


def test_runner_start_background_returns_promptly_and_rejects_duplicate() -> None:
    graph = _BlockingGraph()
    runner = ApiGraphRunner(graph)

    started = runner.start_background(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=4,
        timeout_seconds=5,
    )
    duplicate = runner.start_background(
        thread_id="thread-1",
        initial_payload={"messages": [HumanMessage(content="duplicate")]},
        max_steps=4,
        timeout_seconds=5,
    )

    assert started is True
    assert graph.invoke_started.wait(timeout=1)
    assert duplicate is False
    assert runner.status("thread-1")["state"] == "running"

    graph.release_invoke.set()
    deadline = time.time() + 2
    while time.time() < deadline and runner.status("thread-1")["state"] == "running":
        time.sleep(0.01)
    assert runner.status("thread-1")["state"] == "done"


def test_background_runner_supports_compiled_graph_with_sqlite_saver() -> None:
    builder = StateGraph(dict)
    builder.add_node("finish", lambda state: {**state, "completed": True})
    builder.add_edge(START, "finish")

    with SqliteSaver.from_conn_string(":memory:") as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        runner = ApiGraphRunner(graph)

        assert runner.start_background(
            thread_id="thread-sqlite",
            initial_payload={"started": True},
            max_steps=4,
            timeout_seconds=5,
        )
        status = _wait_for_terminal_status(runner, "thread-sqlite")

        assert status["state"] == "done"
        assert status["steps"] == 1


def test_owner_can_recover_sqlite_interrupt_but_other_user_cannot_read_or_resume(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "owner-recovery.db"
    history_store = ConversationHistoryStore(database_path)
    user_a = _identity("user-a")
    user_b = _identity("user-b")

    builder = StateGraph(dict)

    def seed_artifact(state: dict[str, Any]) -> dict[str, Any]:
        artifacts = dict(state.get("artifacts") or {})
        files = dict(artifacts.get("files") or {})
        files["figure-recovered"] = {
            "artifact_id": "figure-recovered",
            "kind": "figure",
            "producer": "executor",
            "mime": "image/png",
            "summary": "Recovered figure",
            "status": "approved",
            "created_at": "2026-08-06T00:00:00+00:00",
            "content": {"data_base64": "cmVjb3ZlcmVk"},
        }
        artifacts["files"] = files
        return {**state, "artifacts": artifacts}

    def request_review(state: dict[str, Any]) -> dict[str, Any]:
        decision = interrupt(
            {
                "type": "model_output_limit",
                "model_id": "gpt-5.6-sol",
                "model_label": "gpt-5.6-sol (Medium)",
                "automatic_token_ceiling": 50_000,
                "continuation_tokens": 25_000,
                "additional_output_cost": "$0.75",
                "message": "Continue generating the analysis?",
                "actions": ["continue", "cancel"],
            }
        )
        return {**state, "review_decision": decision}

    builder.add_node("seed", seed_artifact)
    builder.add_node("review", request_review)
    builder.add_edge(START, "seed")
    builder.add_edge("seed", "review")
    builder.add_edge("review", END)

    def runtime_with_connection():
        connection = sqlite3.connect(database_path, check_same_thread=False)
        checkpointer = SqliteSaver(connection)
        runtime = ReportAgentApiRuntime(
            graph_factory=lambda _settings, _context: builder.compile(
                checkpointer=checkpointer
            ),
            default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
            models=["gpt-5.4"],
            history_store=history_store,
            checkpoint_path=database_path,
        )
        return runtime, connection

    first_runtime, first_connection = runtime_with_connection()
    thread_id = first_runtime.create_thread(user_a)
    first_runtime.submit_message(
        user_a,
        thread_id,
        "Create a cohort",
        provider_api_key="key-a",
    )
    deadline = time.time() + 2
    first_state = first_runtime.state(user_a, thread_id)
    while first_state.active_interrupt is None and time.time() < deadline:
        time.sleep(0.01)
        first_state = first_runtime.state(user_a, thread_id)
    assert first_state.active_interrupt is not None
    interrupt_id = first_state.active_interrupt.id
    first_runtime._title_executor.shutdown(wait=True)
    first_connection.close()

    recovered_runtime, recovered_connection = runtime_with_connection()
    try:
        recovered = recovered_runtime.state(user_a, thread_id)

        assert recovered.active_interrupt is not None
        assert recovered.active_interrupt.id == interrupt_id
        assert recovered_runtime.file_artifact_bytes(
            user_a,
            thread_id,
            "figure-recovered",
        ).content == b"recovered"
        assert recovered_runtime.export_thread_archive(user_a, thread_id)
        with pytest.raises(KeyError):
            recovered_runtime.state(
                user_b,
                thread_id,
                provider_api_key="key-b",
            )
        with pytest.raises(KeyError):
            recovered_runtime.resume_interrupt(
                user_b,
                thread_id,
                interrupt_id,
                {"action": "continue"},
                provider_api_key="key-b",
            )
    finally:
        recovered_runtime._title_executor.shutdown(wait=True)
        recovered_connection.close()


def test_runner_stops_when_interrupt_is_reached() -> None:
    graph = _FakeGraph(
        [
            SimpleNamespace(next=("orchestrator",), interrupts=[]),
            SimpleNamespace(
                next=("rag_db_qa",),
                interrupts=[object()],
            ),
        ]
    )
    runner = ApiGraphRunner(graph)

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=4,
        timeout_seconds=5,
    )

    assert status["state"] == "interrupted"
    assert status["steps"] == 2
    assert graph.invoke_calls == [
        ({"messages": []}, {"configurable": {"thread_id": "thread-1"}}),
        ({}, {"configurable": {"thread_id": "thread-1"}}),
    ]


def test_runner_stops_on_unprojectable_langgraph_interrupt() -> None:
    graph = _FakeGraph(
        [
            SimpleNamespace(next=("orchestrator",), interrupts=[], values={}),
            SimpleNamespace(
                next=(),
                interrupts=[
                    SimpleNamespace(
                        id="interrupt-sql",
                        value={
                            "type": "human_review_rag_db_sql_execution",
                            "artifact_id": "sql-art-1",
                        },
                    )
                ],
                values={
                    "last_action": "rag_db_qa",
                    "artifacts": {"files": {}, "datasets": {}},
                    "agents": {
                        "rag_db_qa": {
                            "run_status": "completed",
                        }
                    },
                },
            ),
        ]
    )
    runner = ApiGraphRunner(graph)

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=4,
        timeout_seconds=5,
    )

    assert status["state"] == "interrupted"
    assert status["steps"] == 2


def test_runner_records_timeout() -> None:
    runner = ApiGraphRunner(_FakeGraph([]))

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=4,
        timeout_seconds=0,
    )

    assert status["state"] == "timeout"
    assert status["error"] == "Graph run exceeded timeout_seconds=0"
    assert time.time() - status["started_at"] < 5
    assert time.time() - status["updated_at"] < 5


class _SyncFailureGraph:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.invoke_calls = 0

    def invoke(self, _payload: Any, _config: dict) -> None:
        self.invoke_calls += 1
        if self.error is not None:
            raise self.error

    def get_state(
        self,
        _config: dict,
        *,
        subgraphs: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(values={}, next=(), interrupts=[])


def _wait_for_terminal_status(
    runner: ApiGraphRunner,
    thread_id: str,
) -> dict:
    deadline = time.time() + 2
    status = runner.status(thread_id)
    while status["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
        status = runner.status(thread_id)
    return status


def test_background_runner_maps_provider_request_timeout() -> None:
    request = Request("POST", "https://api.openai.com/v1/responses")
    graph = _SyncFailureGraph(ReadTimeout("request timed out", request=request))
    runner = ApiGraphRunner(graph)

    assert runner.start_background(
        thread_id="thread-request-timeout",
        initial_payload={"messages": []},
        max_steps=4,
        timeout_seconds=5,
    )
    status = _wait_for_terminal_status(runner, "thread-request-timeout")

    assert graph.invoke_calls == 1
    assert status["state"] == "error"
    assert status["error_code"] == "MODEL_REQUEST_TIMEOUT"
    assert status["user_message"] == (
        "The selected model did not respond within its request timeout. "
        "Error: MODEL_REQUEST_TIMEOUT"
    )


def test_runner_records_max_steps_exhaustion() -> None:
    runner = ApiGraphRunner(
        _FakeGraph([SimpleNamespace(next=("orchestrator",), interrupts=[])])
    )

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=1,
        timeout_seconds=5,
    )

    assert status["state"] == "timeout"
    assert status["error"] == "Graph run reached max_steps=1"
    assert status["error_code"] == "WORKFLOW_MAX_STEPS_EXCEEDED"
    assert status["user_message"] == (
        "The workflow reached its step limit before producing a result. Start a "
        "new conversation or increase the configured step limit. "
        "Error: WORKFLOW_MAX_STEPS_EXCEEDED"
    )


def test_runner_final_allowed_step_can_reach_done() -> None:
    runner = ApiGraphRunner(
        _FakeGraph(
            [
                SimpleNamespace(next=("orchestrator",), interrupts=[]),
                SimpleNamespace(next=(), interrupts=[]),
            ]
        )
    )

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=2,
        timeout_seconds=5,
    )

    assert status["state"] == "done"
    assert status["steps"] == 2
    assert status["error"] is None


def test_runner_final_allowed_step_can_reach_interrupt() -> None:
    runner = ApiGraphRunner(
        _FakeGraph(
            [
                SimpleNamespace(next=("orchestrator",), interrupts=[]),
                SimpleNamespace(next=("human_review",), interrupts=[object()]),
            ]
        )
    )

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=2,
        timeout_seconds=5,
    )

    assert status["state"] == "interrupted"
    assert status["steps"] == 2
    assert status["error"] is None


def test_runner_returns_running_status_without_duplicate_invoke() -> None:
    graph = _FakeGraph([])
    runner = ApiGraphRunner(graph)
    runner._jobs["thread-1"] = {
        "state": "running",
        "steps": 1,
        "error": None,
        "started_at": 123.0,
        "updated_at": 124.0,
    }

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=2,
        timeout_seconds=5,
    )

    assert status == {
        "state": "running",
        "steps": 1,
        "error": None,
        "started_at": 123.0,
        "updated_at": 124.0,
    }
    assert graph.invoke_calls == []


def test_runner_records_typed_exception_error() -> None:
    runner = ApiGraphRunner(_FakeGraph([], invoke_exception=ValueError("bad payload")))

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=4,
        timeout_seconds=5,
    )

    assert status["state"] == "error"
    assert status["error"] == "ValueError: bad payload"


def test_runner_projects_structured_openai_quota_failure() -> None:
    quota_error = RateLimitError(
        "quota exhausted",
        response=Response(
            429,
            request=Request("POST", "https://api.openai.com/v1/responses"),
        ),
        body={
            "message": "You have no credits remaining.",
            "type": "insufficient_quota",
            "code": "credit_balance_exhausted",
        },
    )
    runner = ApiGraphRunner(_FakeGraph([], invoke_exception=quota_error))

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=4,
        timeout_seconds=5,
    )

    assert status["state"] == "error"
    assert status["error"] == "RateLimitError: quota exhausted"
    assert status["error_code"] == "OPENAI_CREDITS_EXHAUSTED"
    assert status["user_message"] == (
        "The OpenAI account has no remaining API credits. Add credits or use a "
        "funded API key, then retry. Error: OPENAI_CREDITS_EXHAUSTED"
    )


@pytest.mark.parametrize(
    ("error", "error_code", "message"),
    [
        (
            RateLimitError(
                "raw rate-limit detail",
                response=Response(
                    429,
                    request=Request("POST", "https://api.openai.com/v1/responses"),
                ),
                body={"code": "rate_limit_exceeded"},
            ),
            "OPENAI_RATE_LIMITED",
            "OpenAI's request limit was reached. Wait briefly, then retry. "
            "Error: OPENAI_RATE_LIMITED",
        ),
        (
            AuthenticationError(
                "raw authentication detail",
                response=Response(
                    401,
                    request=Request("POST", "https://api.openai.com/v1/responses"),
                ),
                body={},
            ),
            "OPENAI_AUTHENTICATION_FAILED",
            "OpenAI rejected the session API key. Enter a valid key and retry. "
            "Error: OPENAI_AUTHENTICATION_FAILED",
        ),
        (
            PermissionDeniedError(
                "raw permission detail",
                response=Response(
                    403,
                    request=Request("POST", "https://api.openai.com/v1/responses"),
                ),
                body={},
            ),
            "OPENAI_ACCESS_DENIED",
            "The configured OpenAI project is not allowed to use this resource. "
            "Check the project's permissions or use another API key. "
            "Error: OPENAI_ACCESS_DENIED",
        ),
        (
            NotFoundError(
                "raw model detail",
                response=Response(
                    404,
                    request=Request("POST", "https://api.openai.com/v1/responses"),
                ),
                body={"code": "model_not_found"},
            ),
            "OPENAI_MODEL_UNAVAILABLE",
            "The selected OpenAI model is unavailable to this API project. "
            "Choose another model and retry. Error: OPENAI_MODEL_UNAVAILABLE",
        ),
        (
            BadRequestError(
                "raw context detail",
                response=Response(
                    400,
                    request=Request("POST", "https://api.openai.com/v1/responses"),
                ),
                body={"error": {"code": "context_length_exceeded"}},
            ),
            "OPENAI_CONTEXT_LIMIT_EXCEEDED",
            "This conversation exceeds the selected model's context limit. "
            "Start a new conversation or reduce the attached content. "
            "Error: OPENAI_CONTEXT_LIMIT_EXCEEDED",
        ),
        (
            APIConnectionError(
                message="raw network detail",
                request=Request("POST", "https://api.openai.com/v1/responses"),
            ),
            "OPENAI_CONNECTION_FAILED",
            "The server could not reach OpenAI. Check the network connection, "
            "then retry. Error: OPENAI_CONNECTION_FAILED",
        ),
    ],
)
def test_runner_projects_actionable_provider_failure(
    error: Exception,
    error_code: str,
    message: str,
) -> None:
    runner = ApiGraphRunner(_FakeGraph([], invoke_exception=error))

    status = runner.run_until_blocked(
        thread_id="thread-provider-failure",
        initial_payload={"messages": []},
        max_steps=4,
        timeout_seconds=5,
    )

    assert status["state"] == "error"
    assert status["error_code"] == error_code
    assert status["user_message"] == message
    assert "raw " not in status["user_message"]


def test_runner_projects_safe_generic_failure_message() -> None:
    runner = ApiGraphRunner(_FakeGraph([], invoke_exception=ValueError("internal detail")))

    status = runner.run_until_blocked(
        thread_id="thread-1",
        initial_payload={"messages": []},
        max_steps=4,
        timeout_seconds=5,
    )

    assert status["state"] == "error"
    assert status["error"] == "ValueError: internal detail"
    assert status["error_code"] == "RUN_FAILED"
    assert status["user_message"] == (
        "The request failed unexpectedly. Check the server log for details. "
        "Error: RUN_FAILED"
    )
    assert "internal detail" not in status["user_message"]


def test_api_thread_state_serializes_minimal_payload() -> None:
    state = ApiThreadState(
        thread_id="thread-1",
        run=RunStatus(state="idle", steps=0, error=None),
        conversation=[],
        active_interrupt=None,
        datasets=[],
        output={},
        diagnostics={"snapshot_next": []},
    )

    assert state.model_dump()["thread_id"] == "thread-1"
    assert state.model_dump()["run"]["state"] == "idle"
    assert state.model_dump()["diagnostics"]["snapshot_next"] == []


def test_project_thread_state_exposes_root_interrupt_and_diagnostics() -> None:
    snapshot = SimpleNamespace(
        values={
            "messages": [
                HumanMessage(content="Create a subset"),
                AIMessage(content="Interactive DB-RAG dataset builder is ready."),
            ],
            "output": {"answer": "done"},
            "next_action": "rag_db_qa",
            "last_action": "orchestrator",
            "agents": {
                "rag_db_qa": {},
                "executor": {"run_status": "idle"},
            },
            "orchestrator": {
                "current_run": {
                    "run_id": "run:active",
                    "goal": "Create a subset and run logistic regression.",
                    "status": "running",
                    "work_packages": {
                        "wp-code": {
                            "work_package_id": "wp-code",
                            "owner": "codegen",
                            "goal": "Run logistic regression.",
                            "status": "pending",
                        }
                    },
                }
            },
            "artifacts": {
                "datasets": {"subset-1": {"id": "subset-1", "row_count": 3}},
                "files": {
                    "figure-1": {"artifact_id": "figure-1", "mime": "image/png"},
                    "artifact-1": {
                        "artifact_id": "artifact-1",
                        "kind": "dataset_plan",
                        "mime": "application/json",
                        "content": {
                            "id": "artifact-1",
                            "kind": "dataset_plan",
                            "version": 1,
                            "status": "draft",
                            "content": {},
                            "provenance": {},
                        },
                    },
                },
            },
            "meta": {
                "workflow_milestone": "review",
                "completion_status": "incomplete",
                "blocker_signature": "awaiting_review",
                "workflow_trace": ["planner", "orchestrator", "rag_db_qa"],
            },
        },
        next=("tools",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-1",
                value={
                    "type": "dataset_plan_review",
                    "artifact": {
                        "id": "artifact-1",
                        "kind": "dataset_plan",
                        "version": 1,
                        "expected_status": "draft",
                    },
                    "view": {
                        "dataset_title": "Subset and logistic regression plan",
                        "goal": "Create a subset and run logistic regression.",
                        "concept_groups": [],
                        "selected_fields": [],
                        "filters": [],
                        "joins": [],
                        "unresolved_scientific_choices": [],
                    },
                },
            )
        ],
        tasks=[],
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 2, "error": None},
    )

    assert projected.thread_id == "thread-1"
    assert projected.run.state == "interrupted"
    assert [(message.role, message.text) for message in projected.conversation] == [
        ("user", "Create a subset"),
        ("assistant", "Interactive DB-RAG dataset builder is ready."),
    ]
    assert projected.active_interrupt is not None
    assert projected.active_interrupt.id == "interrupt-1"
    assert projected.active_interrupt.type == "dataset_plan_review"
    assert projected.active_interrupt.artifact.id == "artifact-1"
    assert projected.active_interrupt.view.concept_groups == []
    assert [(dataset.id, dataset.row_count) for dataset in projected.datasets] == [
        ("subset-1", 3)
    ]
    assert projected.output == {"answer": "done"}
    assert projected.diagnostics["thread_id"] == "thread-1"
    assert projected.diagnostics["run_state"] == "interrupted"
    assert projected.diagnostics["run_steps"] == 2
    assert projected.diagnostics["semantic_graph"] == "epi_agent"
    assert projected.diagnostics["checkpoint_scope"] == "root"
    assert projected.diagnostics["snapshot_next"] == ["tools"]
    assert projected.diagnostics["active_interrupt_artifact"] == {
        "id": "artifact-1",
        "kind": "dataset_plan",
        "version": 1,
        "expected_status": "draft",
    }
    assert projected.diagnostics["dataset_ids"] == ["subset-1"]
    assert projected.diagnostics["file_artifact_ids"] == [
        "artifact-1",
        "figure-1",
    ]
    assert "attachment_handling" not in projected.diagnostics
    assert "current_run" not in projected.diagnostics
    assert "active_agent_checkpoint_ns" not in projected.diagnostics


def test_project_thread_state_reports_unprojectable_interrupt() -> None:
    snapshot = _plan_review_snapshot()
    snapshot.interrupts[0].value["artifact"]["id"] = "missing-plan"

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "idle", "steps": 2, "error": None},
    )

    assert projected.active_interrupt is None
    assert projected.run.state == "error"
    assert projected.run.error_code == "INTERRUPT_PROJECTION_FAILED"
    assert projected.run.user_message is not None
    assert "pending review could not be displayed" in projected.run.user_message
    assert projected.diagnostics["interrupt_count"] == 1


def test_project_thread_state_preserves_running_during_interrupt_retirement() -> None:
    snapshot = _plan_review_snapshot()
    snapshot.interrupts[0].value["artifact"]["id"] = "missing-plan"

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={
            "state": "running",
            "steps": 2,
            "error": None,
            "error_code": None,
            "user_message": None,
        },
    )

    assert projected.active_interrupt is None
    assert projected.run.state == "running"
    assert projected.run.error is None
    assert projected.run.error_code is None
    assert projected.run.user_message is None
    assert projected.diagnostics["interrupt_count"] == 1


def test_project_thread_state_surfaces_persisted_terminal_error() -> None:
    snapshot = SimpleNamespace(
        values={
            "terminal_error": {
                "code": "MAX_ITERATIONS",
                "message": "The agent reached its iteration limit.",
            }
        },
        next=(),
        interrupts=[],
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 4, "error": None},
    )

    assert projected.run.state == "error"
    assert projected.run.error_code == "MAX_ITERATIONS"
    assert projected.run.error == "The agent reached its iteration limit."
    assert projected.run.user_message == "The agent reached its iteration limit."


def test_project_thread_state_marks_persisted_interrupt_as_interrupted() -> None:
    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=_plan_review_snapshot(),
        run_status={"state": "idle", "steps": 0, "error": None},
    )

    assert projected.active_interrupt is not None
    assert projected.run.state == "interrupted"


def test_project_thread_state_restores_model_output_limit_interrupt() -> None:
    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=_model_output_limit_snapshot(),
        run_status={"state": "idle", "steps": 0, "error": None},
    )

    assert projected.run.state == "interrupted"
    assert projected.active_interrupt is not None
    assert projected.active_interrupt.type == "model_output_limit"
    assert projected.active_interrupt.actions == ("continue", "cancel")


def _analysis_file(
    artifact_id: str,
    *,
    version: int = 1,
    status: str = "pending_review",
) -> dict:
    return {
        "artifact_id": artifact_id,
        "kind": "analysis_run",
        "producer": "epi_agent",
        "mime": "application/json",
        "summary": "Logistic regression",
        "status": status,
        "content": {
            "id": artifact_id,
            "kind": "analysis_run",
            "version": version,
            "status": status,
            "content": {
                "schema_version": "1.0",
                "method": "logistic_regression",
                "dataset": {
                    "id": "dataset-1",
                    "kind": "analysis_dataset",
                    "version": 1,
                },
                "specification": {},
                "output_text": "Kaplan-Meier estimates\nLog-rank p-value=0.031\n",
                "runtime": {"language": "R"},
                "estimates": [],
                "diagnostics": {},
                "warnings": [],
                "tables": [],
                "figures": [],
            },
            "provenance": {"producer": "epi_agent"},
        },
    }


def _analysis_review_payload(
    artifact_id: str,
    *,
    version: int = 1,
) -> dict:
    return {
        "type": "analysis_result_review",
        "artifact": {
            "id": artifact_id,
            "kind": "analysis_run",
            "version": version,
            "expected_status": "pending_review",
        },
        "view": {
            "method": "logistic_regression",
            "dataset": {
                "id": "dataset-1",
                "kind": "analysis_dataset",
                "version": 1,
            },
            "specification": {},
            "output_text": "Kaplan-Meier estimates\nLog-rank p-value=0.031\n",
            "warnings": [],
            "warnings_truncated": False,
            "runtime": {"language": "R"},
            "tables": [],
            "figures": [],
            "feedback_history": [],
        },
    }


def test_project_thread_state_reads_active_epi_agent_review_from_root() -> None:
    snapshot = SimpleNamespace(
        values={
            "artifacts": {
                "files": {
                    "analysis-1": _analysis_file("analysis-1"),
                }
            }
        },
        next=("tools",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-analysis",
                value=_analysis_review_payload("analysis-1"),
            )
        ],
        tasks=[],
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 2, "error": None},
    )

    assert projected.run.state == "interrupted"
    assert projected.active_interrupt is not None
    assert projected.active_interrupt.type == "analysis_result_review"
    assert projected.active_interrupt.artifact.id == "analysis-1"
    assert projected.file_artifacts == []
    assert projected.diagnostics["checkpoint_scope"] == "root"


def test_project_thread_state_accepts_typed_analysis_figure_identities() -> None:
    payload = _analysis_review_payload("analysis-1")
    payload["view"]["figures"] = [
        {"id": "figure-1", "kind": "figure", "version": 1}
    ]
    snapshot = SimpleNamespace(
        values={
            "artifacts": {
                "files": {
                    "analysis-1": _analysis_file("analysis-1"),
                }
            }
        },
        next=("tools",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-analysis",
                value=payload,
            )
        ],
        tasks=[],
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 2, "error": None},
    )

    assert projected.active_interrupt is not None
    assert projected.active_interrupt.view.figures[0].id == "figure-1"


def test_runtime_allows_only_exact_pending_analysis_under_review() -> None:
    snapshot = SimpleNamespace(
        values={
            "artifacts": {
                "files": {
                    "analysis-1": _analysis_file("analysis-1"),
                    "analysis-other": _analysis_file("analysis-other"),
                }
            }
        },
        next=("tools",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-analysis",
                value=_analysis_review_payload("analysis-1"),
            )
        ],
        tasks=[],
    )
    runtime = _runtime(_RuntimeFakeGraph(snapshot))

    artifact = runtime.file_artifact_bytes("thread-1", "analysis-1")

    stored = json.loads(artifact.content)
    assert stored["id"] == "analysis-1"
    assert stored["status"] == "pending_review"
    with pytest.raises(KeyError):
        runtime.file_artifact_bytes("thread-1", "analysis-other")


def test_runtime_allows_only_exact_linked_pending_figure_under_review() -> None:
    analysis = _analysis_file("analysis-1")
    analysis["content"]["content"]["figures"] = [
        {"id": "figure-1", "kind": "figure", "version": 1}
    ]

    def pending_figure(artifact_id: str, data_base64: str) -> dict:
        return {
            "artifact_id": artifact_id,
            "kind": "figure",
            "producer": "epi_agent",
            "mime": "image/png",
            "summary": "Python analysis figure pending review",
            "status": "pending_review",
            "content": {
                "id": artifact_id,
                "kind": "figure",
                "version": 1,
                "status": "pending_review",
                "content": {"data_base64": data_base64},
                "provenance": {"producer": "epi_agent"},
            },
        }

    snapshot = SimpleNamespace(
        values={
            "artifacts": {
                "files": {
                    "analysis-1": analysis,
                    "figure-1": pending_figure("figure-1", "cG5n"),
                    "figure-other": pending_figure(
                        "figure-other",
                        "c3RhbGU=",
                    ),
                }
            }
        },
        next=("tools",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-analysis",
                value=_analysis_review_payload("analysis-1"),
            )
        ],
        tasks=[],
    )
    runtime = _runtime(_RuntimeFakeGraph(snapshot))

    figure = runtime.file_artifact_bytes("thread-1", "figure-1")

    assert figure.content == b"png"
    assert figure.mime == "image/png"
    assert figure.filename == "figure-1.png"
    with pytest.raises(KeyError):
        runtime.file_artifact_bytes("thread-1", "figure-other")


def test_runtime_previews_only_exact_linked_pending_table_under_review() -> None:
    analysis = _analysis_file("analysis-1")
    analysis["content"]["content"]["tables"] = [
        {"id": "table-1", "kind": "table", "version": 1}
    ]

    def pending_table(artifact_id: str, text: str) -> dict:
        return {
            "artifact_id": artifact_id,
            "kind": "table",
            "producer": "epi_agent",
            "mime": "text/csv",
            "summary": "Python analysis table pending review",
            "status": "pending_review",
            "content": {
                "id": artifact_id,
                "kind": "table",
                "version": 1,
                "status": "pending_review",
                "content": {"text": text},
                "provenance": {"producer": "epi_agent"},
            },
        }

    payload = _analysis_review_payload("analysis-1")
    payload["view"]["tables"] = [{"id": "table-1", "kind": "table", "version": 1}]
    snapshot = SimpleNamespace(
        values={
            "artifacts": {
                "files": {
                    "analysis-1": analysis,
                    "table-1": pending_table("table-1", "group,n\nGood,10\n"),
                    "table-other": pending_table("table-other", "group,n\nBad,2\n"),
                }
            }
        },
        next=("tools",),
        interrupts=[SimpleNamespace(id="interrupt-analysis", value=payload)],
        tasks=[],
    )
    runtime = _runtime(_RuntimeFakeGraph(snapshot))

    preview = runtime.table_preview("thread-1", "table-1", limit=100)

    assert preview.columns == ["group", "n"]
    assert preview.rows == [{"group": "Good", "n": "10"}]
    assert preview.row_count == 1
    with pytest.raises(KeyError):
        runtime.table_preview("thread-1", "table-other", limit=100)


def test_project_thread_state_omits_retired_before_run_code_interrupt() -> None:
    snapshot = SimpleNamespace(
        values={
            "messages": [],
            "output": {
                "generated_code": "print(df.head())",
                "code_summary": "Inspect the active dataset.",
            },
            "agents": {"human_review": {"before_run_decision": None}},
        },
        next=("human_review_before_run",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-code",
                value={
                    "type": "before_run_review",
                    "generated_code": "print(df.head())",
                    "code_summary": "Inspect the active dataset.",
                },
            )
        ],
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 2, "error": None},
    )

    assert projected.run.state == "done"
    assert projected.active_interrupt is None


def test_project_thread_state_omits_stale_db_rag_interrupt() -> None:
    snapshot = SimpleNamespace(
        values={
            "messages": [],
            "output": {},
            "agents": {"rag_db_qa": {}},
            "artifacts": {
                "files": {
                    "artifact-1": {
                        "content": {
                            "id": "artifact-1",
                            "kind": "dataset_plan",
                            "version": 2,
                            "status": "approved",
                            "content": {},
                            "provenance": {},
                        }
                    }
                }
            },
        },
        next=("rag_db_qa",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-1",
                value={
                    "type": "human_review_rag_db_column_selection",
                    "artifact_id": "artifact-1",
                    "artifact_version": 1,
                    "artifact_status": "draft",
                    "grouped_review": {"groups": []},
                },
            )
        ],
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 2, "error": None},
    )

    assert projected.active_interrupt is None


def test_project_thread_state_hides_pending_review_dataset_from_normal_dataset_surfaces() -> None:
    snapshot = SimpleNamespace(
        values={
            "messages": [],
            "output": {},
            "agents": {"rag_db_qa": {}},
            "artifacts": {
                "datasets": {
                    "approved-1": {"id": "approved-1", "row_count": 5},
                    "pending-1": {
                        "id": "pending-1",
                        "kind": "subset",
                        "version": 1,
                        "row_count": 12,
                        "status": "pending_review",
                    },
                }
            },
        },
        next=("rag_db_qa",),
        interrupts=[
            SimpleNamespace(
                id="interrupt-dataset",
                value=_dataset_review_payload("pending-1"),
            )
        ],
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "running", "steps": 2, "error": None},
    )

    assert [dataset.id for dataset in projected.datasets] == ["approved-1"]
    assert projected.diagnostics["dataset_ids"] == ["approved-1"]
    assert projected.diagnostics["active_interrupt_artifact"] == {
        "id": "pending-1",
        "kind": "subset",
        "version": 1,
        "expected_status": "pending_review",
    }
