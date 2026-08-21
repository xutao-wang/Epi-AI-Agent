from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from epi_agent.agent import build_general_epi_agent_graph
from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry
from epi_agent.tool_packs.publication import build_publication_tool_registry
from graph.state import MetaKeys
from api.auth import LOCAL_REQUEST_IDENTITY
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import AttachmentReaderService
from utils.model_runtime_profiles import model_runtime_profile


def test_graph_factory_binds_all_studies_with_the_provider_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.app as app_module
    from api.runtime import GraphBuildContext
    from db_rag.config import EMBEDDING_MODEL
    from db_rag.readiness import DbRagReadiness
    from utils.user_storage import UserStorageLayout

    discovered_studies = StudyRegistry(
        [
            StudyBundle(
                study_id="report-india-synthetic",
                label="RePORT",
                knowledge=None,
                catalog=None,
                data_sources={},
            ),
            StudyBundle(
                study_id="nhanes-2017-2018",
                label="NHANES",
                knowledge=None,
                catalog=None,
                data_sources={},
            ),
        ]
    )
    bound_studies = StudyRegistry(discovered_studies.values)
    readiness_by_study = {
        study.study_id: DbRagReadiness(
            status="available",
            message="DB-RAG dataset is available.",
        )
        for study in discovered_studies.values
    }
    bind_calls: list[dict[str, object]] = []
    graph_kwargs: dict[str, object] = {}

    def bind_session_studies(studies, *, api_key, expected_embedding_model):
        bind_calls.append(
            {
                "studies": studies,
                "api_key": api_key,
                "expected_embedding_model": expected_embedding_model,
            }
        )
        return SimpleNamespace(
            studies=bound_studies,
            readiness=readiness_by_study,
        )

    monkeypatch.setattr(
        app_module,
        "discover_studies",
        lambda _root: discovered_studies,
    )
    monkeypatch.setattr(
        app_module,
        "bind_session_studies",
        bind_session_studies,
        raising=False,
    )
    monkeypatch.setattr(app_module, "build_openai_llm", lambda **_kwargs: "llm")
    monkeypatch.setattr(
        app_module,
        "build_graph",
        lambda _llm, **kwargs: graph_kwargs.update(kwargs) or "graph",
    )
    application = app_module.build_application(
        environ={
            "OPENAI_API_KEY": "startup-provider-key",
            "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
            "REPORT_AGENT_ALLOWED_MODELS": "gpt-5.4",
            "OPENAI_MODEL": "gpt-5.4",
            "REPORT_AGENT_TITLE_MODEL": "gpt-5.4",
        }
    )
    storage = UserStorageLayout(tmp_path / "runtime").thread("user-a", "thread-a")

    application.state.report_agent_runtime.graph_factory(
        SimpleNamespace(model_name="gpt-5.4", temperature=None, top_p=None),
        GraphBuildContext(
            owner_user_id="user-a",
            session_id="11111111-1111-4111-8111-111111111111",
            thread_id="thread-a",
            provider_api_key="session-key",
            storage=storage,
        ),
    )

    assert bind_calls == [
        {
            "studies": discovered_studies,
            "api_key": "startup-provider-key",
            "expected_embedding_model": EMBEDDING_MODEL,
        }
    ]
    assert graph_kwargs["studies"] is bound_studies
    assert graph_kwargs["db_rag_readiness_by_study"] == readiness_by_study
    assert "startup-provider-key" not in repr(graph_kwargs)


def test_startup_claims_legacy_history_for_local_user(tmp_path: Path) -> None:
    from api.app import _history_store

    with sqlite3.connect(tmp_path / "history.db") as connection:
        connection.execute(
            """
            CREATE TABLE conversation_history (
                thread_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                title_source TEXT NOT NULL,
                model_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_opened_at TEXT,
                archived_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO conversation_history
            VALUES ('legacy-thread', 'Legacy', 'automatic', 'gpt-5.4',
                    '2026-08-06T00:00:00+00:00',
                    '2026-08-06T00:00:00+00:00', NULL, NULL)
            """
        )

    store = _history_store(tmp_path / "history.db")

    assert [item.thread_id for item in store.list("local-user")] == ["legacy-thread"]


def test_application_factory_uses_fixed_local_identity(
    tmp_path: Path,
) -> None:
    from api.app import build_application

    application = build_application(
        environ={
            "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
            "REPORT_AGENT_ALLOWED_MODELS": "gpt-5.4",
            "OPENAI_MODEL": "gpt-5.4",
            "REPORT_AGENT_TITLE_MODEL": "gpt-5.4",
            "OPENAI_API_KEY": "local-environment-key",
        }
    )

    assert not hasattr(application.state, "token_verifier")
    assert not hasattr(application.state, "provider_credential_store")
    thread_id = application.state.report_agent_runtime.create_thread(
        LOCAL_REQUEST_IDENTITY
    )
    assert application.state.report_agent_runtime._threads[
        ("local-user", thread_id)
    ].app is None


def test_application_factory_requires_local_openai_key(tmp_path: Path) -> None:
    from api.app import build_application

    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        build_application(
            environ={
                "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
                "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
            }
        )


class _FinalModel:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def bind_tools(self, _schemas: list[dict[str, Any]]) -> "_FinalModel":
        return self

    def invoke(
        self,
        messages: list[Any],
        *,
        config: dict[str, Any],
        **_kwargs: Any,
    ) -> AIMessage:
        del config
        self.messages = list(messages)
        return AIMessage(content="A generic epidemiology answer.")


def _service(tmp_path: Path) -> AttachmentReaderService:
    return AttachmentReaderService(
        LocalAttachmentStore(tmp_path),
        runtime_root=tmp_path,
    )


def _state(*, active_study_id: str | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [HumanMessage(content="What is incidence?")],
        "authorized_attachment_ids": [],
        "artifact_ids": [],
        "artifacts": {},
        "meta": {
            MetaKeys.THREAD_ID: "thread-1",
            MetaKeys.LAST_USER_MESSAGE_HASH: "turn-1",
        },
        "output": {},
        "final_response": None,
        "iteration_count": 0,
        "failure_signatures": [],
        "current_turn_artifact_refs": [],
        "analysis_review_feedback_history": [],
    }
    if active_study_id is not None:
        state["active_study_id"] = active_study_id
    return state


def _bundle(study_id: str, marker: str) -> StudyBundle:
    class _Design:
        def render_context(self) -> str:
            return marker

    overview = _Design()
    return StudyBundle(
        study_id=study_id,
        label=study_id,
        knowledge=object(),
        catalog=None,
        data_sources={},
        study_design=overview,
        study_overview=overview,
    )


def test_empty_studies_root_starts_with_study_capabilities_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("REPORT_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("REPORT_AGENT_STUDY_ROOT", str(tmp_path / "study_data"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sys.modules.pop("api.app", None)

    module = importlib.import_module("api.app")

    assert module.app is not None
    assert module.runtime.capabilities.model_dump() == {
        "publication_knowledge": {
            "status": "not_configured",
            "message": "No study package is installed.",
        },
        "study_design": {
            "status": "not_configured",
            "message": "No study package is installed.",
        },
        "db_rag_dataset": {
            "status": "not_configured",
            "message": "No study package is installed.",
        },
    }


def test_generic_agent_completes_without_an_installed_study(tmp_path: Path) -> None:
    model = _FinalModel()
    graph = build_general_epi_agent_graph(
        llm=model,
        model_profile=model_runtime_profile("gpt-5.4"),
        service=_service(tmp_path),
        studies=StudyRegistry(),
        runtime_root=tmp_path,
        include_db_rag=False,
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result["final_response"] == "A generic epidemiology answer."
    rendered = "\n".join(
        str(getattr(message, "content", "")) for message in model.messages
    )
    assert '"study_count":0' in rendered


def test_graph_uses_selected_model_routing_context_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[int] = []

    def render_context(
        _studies: StudyRegistry,
        *,
        max_chars: int,
    ) -> str:
        captured.append(max_chars)
        return '{"study_count":0,"studies":[]}'

    monkeypatch.setattr(
        "epi_agent.agent.render_installed_study_context",
        render_context,
    )
    profile = model_runtime_profile("gpt-5.4")
    graph = build_general_epi_agent_graph(
        llm=_FinalModel(),
        model_profile=profile,
        service=_service(tmp_path),
        studies=StudyRegistry(),
        runtime_root=tmp_path,
        include_db_rag=False,
        checkpointer=InMemorySaver(),
    )

    graph.invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert captured == [profile.routing_context_char_ceiling]


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "publication-search_study_evidence",
            {"study_id": "missing", "query": "tuberculosis", "limit": 5},
        ),
        (
            "publication-open_study_source",
            {
                "source_ref": {
                    "study_id": "missing",
                    "source_id": "source-1",
                }
            },
        ),
    ],
)
def test_study_evidence_tools_report_recoverable_no_study_error(
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    context = ToolContext(
        studies=StudyRegistry(),
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=None,
    )

    with pytest.raises(ToolExecutionError) as error:
        build_publication_tool_registry(include_pubmed=False).invoke(
            tool_name,
            arguments,
            context=context,
        )

    assert error.value.code == "NO_STUDY_PACKAGE_INSTALLED"
    assert error.value.recoverable is True


def test_sole_study_injects_its_complete_overview_without_selection_state(
    tmp_path: Path,
) -> None:
    model = _FinalModel()
    studies = StudyRegistry([_bundle("study-one", "sole-study-marker")])
    graph = build_general_epi_agent_graph(
        llm=model,
        model_profile=model_runtime_profile("gpt-5.4"),
        service=_service(tmp_path),
        studies=studies,
        runtime_root=tmp_path,
        include_db_rag=False,
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result["final_response"] == "A generic epidemiology answer."
    rendered = "\n".join(
        str(getattr(message, "content", "")) for message in model.messages
    )
    assert '"study_id":"study-one"' in rendered
    assert '"label":"study-one"' in rendered
    assert "sole-study-marker" in rendered


def test_legacy_active_study_state_does_not_hide_other_packages(
    tmp_path: Path,
) -> None:
    model = _FinalModel()
    studies = StudyRegistry(
        [
            _bundle("study-one", "first-study-marker"),
            _bundle("study-two", "selected-study-marker"),
        ]
    )
    graph = build_general_epi_agent_graph(
        llm=model,
        model_profile=model_runtime_profile("gpt-5.4"),
        service=_service(tmp_path),
        studies=studies,
        runtime_root=tmp_path,
        include_db_rag=False,
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        _state(active_study_id="study-two"),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result["final_response"] == "A generic epidemiology answer."
    rendered_messages = [
        str(getattr(message, "content", ""))
        for message in model.messages
    ]
    rendered = "\n".join(rendered_messages)
    assert '"study_id":"study-one"' in rendered
    assert '"study_id":"study-two"' in rendered
    assert "selected-study-marker" in rendered
    assert "first-study-marker" in rendered


def test_multiple_studies_are_injected_without_prior_selection(
    tmp_path: Path,
) -> None:
    model = _FinalModel()
    studies = StudyRegistry(
        [
            _bundle("study-one", "first-study-marker"),
            _bundle("study-two", "second-study-marker"),
        ]
    )
    graph = build_general_epi_agent_graph(
        llm=model,
        model_profile=model_runtime_profile("gpt-5.4"),
        service=_service(tmp_path),
        studies=studies,
        runtime_root=tmp_path,
        include_db_rag=False,
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result["final_response"] == "A generic epidemiology answer."
    rendered = "\n".join(
        str(getattr(message, "content", "")) for message in model.messages
    )
    assert "first-study-marker" in rendered
    assert "second-study-marker" in rendered


def test_capabilities_are_aggregated_across_all_installed_studies(
    tmp_path,
    monkeypatch,
) -> None:
    studies = StudyRegistry(
        [
            _bundle("study-one", "first-study-marker"),
            _bundle("study-two", "second-study-marker"),
            _bundle("study-three", "third-study-marker"),
        ]
    )
    monkeypatch.setenv("REPORT_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("REPORT_AGENT_STUDY_ROOT", str(tmp_path / "study_data"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "study_package.registry.discover_studies",
        lambda _root: studies,
    )
    sys.modules.pop("api.app", None)

    module = importlib.import_module("api.app")

    capabilities = module.runtime.capabilities.model_dump()
    assert capabilities == {
        "publication_knowledge": {
            "status": "available",
            "message": (
                "Publication knowledge is available for 3 installed studies."
            ),
        },
        "study_design": {
            "status": "available",
            "message": "Study design is available for 3 installed studies.",
        },
        "db_rag_dataset": {
            "status": "not_configured",
            "message": (
                "No installed study package has an available DB-RAG dataset."
            ),
        },
    }


def test_optional_tool_context_has_no_unguarded_study_dereferences() -> None:
    unsafe = []
    for path in Path("epi_agent").rglob("*.py"):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if "context.study." in line:
                unsafe.append(f"{path}:{line_number}")

    assert unsafe == []
