from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from epi_agent.agent import build_general_epi_agent_graph
from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry
from epi_agent.tool_packs.publication import build_publication_tool_registry
from graph.state import MetaKeys
from api.auth import (
    AuthenticatedUser,
    CognitoTokenVerifier,
    LOCAL_SESSION_ID,
    LocalTokenVerifier,
    RequestIdentity,
)
from api.deployment import python_worker_launcher
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import AttachmentReaderService
from utils.model_runtime_profiles import model_runtime_profile


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("", None),
        (
            "/usr/local/libexec/epi-agent-python-worker",
            (
                "/usr/bin/sudo",
                "-n",
                "/usr/local/libexec/epi-agent-python-worker",
            ),
        ),
        (
            "/usr/bin/sudo -n /usr/local/libexec/epi-agent-python-worker",
            (
                "/usr/bin/sudo",
                "-n",
                "/usr/local/libexec/epi-agent-python-worker",
            ),
        ),
    ],
)
def test_python_worker_launcher_reads_the_hosted_launcher_setting(
    configured: str,
    expected: tuple[str, ...] | None,
) -> None:
    assert python_worker_launcher(
        {"REPORT_AGENT_PYTHON_WORKER_LAUNCHER": configured}
    ) == expected


@pytest.mark.parametrize(
    "configured",
    [
        "relative-launcher",
        "/usr/local/worker\n",
        "/usr/local/worker\x00",
        "/usr/local/libexec/alternate-worker",
        "/usr/local/libexec/epi-agent-python-worker --fixed-option",
    ],
)
def test_python_worker_launcher_rejects_unsafe_hosted_configuration(
    configured: str,
) -> None:
    with pytest.raises(ValueError):
        python_worker_launcher(
            {"REPORT_AGENT_PYTHON_WORKER_LAUNCHER": configured}
        )


def test_application_routes_hosted_python_through_the_fixed_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.app as app_module
    from api.runtime import GraphBuildContext
    from utils.user_storage import UserStorageLayout

    captured: dict[str, Any] = {}
    monkeypatch.setattr(app_module, "build_openai_llm", lambda **_kwargs: "llm")
    monkeypatch.setattr(
        app_module,
        "build_graph",
        lambda _llm, **kwargs: captured.update(kwargs) or "graph",
    )
    application = app_module.build_application(
        environ={
            "REPORT_AGENT_AUTH_MODE": "cognito",
            "REPORT_AGENT_AWS_REGION": "us-east-1",
            "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
            "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
            "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://auth.example.test/logout",
            "REPORT_AGENT_AUTH_REDIRECT_URI": "https://example.test/callback",
            "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://example.test/",
            "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
            "REPORT_AGENT_ALLOWED_MODELS": "gpt-5.4",
            "OPENAI_MODEL": "gpt-5.4",
            "REPORT_AGENT_TITLE_MODEL": "gpt-5.4",
            "REPORT_AGENT_PYTHON_WORKER_LAUNCHER": (
                "/usr/local/libexec/epi-agent-python-worker"
            ),
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

    python_runtime = captured["python_runtime"]
    assert python_runtime._runtime_root == storage.execution.resolve()
    assert python_runtime._worker_launcher == (
        "/usr/bin/sudo",
        "-n",
        "/usr/local/libexec/epi-agent-python-worker",
    )


def test_startup_claims_legacy_history_only_in_local_mode(tmp_path: Path) -> None:
    from api.app import _history_store_for_auth_mode

    def create_legacy_history(db_path: Path) -> None:
        with sqlite3.connect(db_path) as connection:
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

    local_path = tmp_path / "local.db"
    create_legacy_history(local_path)
    local_store = _history_store_for_auth_mode(local_path, auth_mode="local")
    assert [item.thread_id for item in local_store.list("local-user")] == ["legacy-thread"]

    cognito_path = tmp_path / "cognito.db"
    create_legacy_history(cognito_path)
    cognito_store = _history_store_for_auth_mode(cognito_path, auth_mode="cognito")
    assert cognito_store.list("local-user") == []
    assert cognito_store.claim_unowned("other-user") == 1


def test_application_factory_seeds_only_the_fixed_local_session(
    tmp_path: Path,
) -> None:
    from api.app import build_application

    application = build_application(
        environ={
            "REPORT_AGENT_AUTH_MODE": "local",
            "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
            "REPORT_AGENT_ALLOWED_MODELS": "gpt-5.4",
            "OPENAI_MODEL": "gpt-5.4",
            "REPORT_AGENT_TITLE_MODEL": "gpt-5.4",
            "OPENAI_API_KEY": "local-session-key",
        }
    )
    identity = RequestIdentity(
        user=AuthenticatedUser(owner_user_id="local-user"),
        session_id=LOCAL_SESSION_ID,
    )

    assert isinstance(application.state.token_verifier, LocalTokenVerifier)
    assert application.state.provider_credential_store.get(identity) == (
        "local-session-key"
    )
    thread_id = application.state.report_agent_runtime.create_thread(identity)
    assert application.state.report_agent_runtime._threads[
        ("local-user", thread_id)
    ].app is None


def test_application_factory_cognito_mode_starts_without_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from api.app import build_application

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-seed-cognito")
    application = build_application(
        environ={
            "REPORT_AGENT_AUTH_MODE": "cognito",
            "REPORT_AGENT_AWS_REGION": "us-east-1",
            "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
            "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
            "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://auth.example.test/logout",
            "REPORT_AGENT_AUTH_REDIRECT_URI": "https://example.test/callback",
            "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://example.test/",
            "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
            "REPORT_AGENT_ALLOWED_MODELS": "gpt-5.4",
            "OPENAI_MODEL": "gpt-5.4",
            "REPORT_AGENT_TITLE_MODEL": "gpt-5.4",
        }
    )
    identity = RequestIdentity(
        user=AuthenticatedUser(owner_user_id="user-a"),
        session_id="11111111-1111-4111-8111-111111111111",
    )

    assert isinstance(application.state.token_verifier, CognitoTokenVerifier)
    assert application.state.provider_credential_store.get(identity) is None
    assert application.state.report_agent_runtime.runtime_root == tmp_path / "runtime"


def test_application_factory_binds_credential_expiry_to_runtime_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.app as app_module
    from api.provider_credentials import ProviderCredentialStore

    configured: dict[str, Any] = {}

    def credential_store_factory(**kwargs: Any) -> ProviderCredentialStore:
        configured.update(kwargs)
        return ProviderCredentialStore(**kwargs)

    monkeypatch.setattr(
        app_module,
        "ProviderCredentialStore",
        credential_store_factory,
    )
    application = app_module.build_application(
        environ={
            "REPORT_AGENT_AUTH_MODE": "cognito",
            "REPORT_AGENT_AWS_REGION": "us-east-1",
            "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
            "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
            "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://auth.example.test/logout",
            "REPORT_AGENT_AUTH_REDIRECT_URI": "https://example.test/callback",
            "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://example.test/",
            "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
            "REPORT_AGENT_ALLOWED_MODELS": "gpt-5.4",
            "OPENAI_MODEL": "gpt-5.4",
            "REPORT_AGENT_TITLE_MODEL": "gpt-5.4",
        }
    )

    callback = configured["on_expire"]
    assert callback.__self__ is application.state.report_agent_runtime
    assert callback.__func__ is type(application.state.report_agent_runtime).release_session


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


class _StudyEvidenceModel:
    def __init__(self) -> None:
        self.step = 0
        self.messages: list[Any] = []

    def bind_tools(self, _schemas: list[dict[str, Any]]) -> "_StudyEvidenceModel":
        return self

    def invoke(
        self,
        messages: list[Any],
        *,
        config: dict[str, Any],
        **_kwargs: Any,
    ) -> AIMessage:
        del config
        self.step += 1
        self.messages = list(messages)
        if self.step == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "publication-search_study_evidence",
                        "args": {"query": "tuberculosis", "limit": 5},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="Select a study first.")


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

    return StudyBundle(
        study_id=study_id,
        label=study_id,
        knowledge=object(),
        catalog=None,
        data_sources={},
        study_design=_Design(),
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
        default_study_id=None,
        runtime_root=tmp_path,
        include_db_rag=False,
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result["final_response"] == "A generic epidemiology answer."


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "publication-search_study_evidence",
            {"query": "tuberculosis", "limit": 5},
        ),
        ("publication-open_study_source", {"source_id": "source-1"}),
    ],
)
def test_study_evidence_tools_report_recoverable_no_study_error(
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    context = ToolContext(
        study=None,
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


def test_sole_study_is_used_as_the_default(tmp_path: Path) -> None:
    model = _FinalModel()
    studies = StudyRegistry([_bundle("study-one", "sole-study-marker")])
    graph = build_general_epi_agent_graph(
        llm=model,
        model_profile=model_runtime_profile("gpt-5.4"),
        service=_service(tmp_path),
        studies=studies,
        default_study_id=studies.sole_study_id(),
        runtime_root=tmp_path,
        include_db_rag=False,
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result["final_response"] == "A generic epidemiology answer."
    assert any(
        "sole-study-marker" in str(getattr(message, "content", ""))
        for message in model.messages
    )


def test_explicit_active_study_selects_one_of_multiple_packages(
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
        default_study_id=studies.sole_study_id(),
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
    assert any("selected-study-marker" in message for message in rendered_messages)
    assert all("first-study-marker" not in message for message in rendered_messages)


def test_multiple_studies_without_selection_report_selection_required(
    tmp_path: Path,
) -> None:
    model = _StudyEvidenceModel()
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
        default_study_id=None,
        runtime_root=tmp_path,
        include_db_rag=False,
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result["final_response"] == "Select a study first."
    tool_message = next(
        message for message in model.messages if isinstance(message, ToolMessage)
    )
    error = json.loads(str(tool_message.content))["error"]
    assert error["code"] == "ACTIVE_STUDY_SELECTION_REQUIRED"
    assert error["recoverable"] is True
    assert error["details"] == {
        "available_study_ids": ["study-one", "study-two"]
    }
    assert "No study package is installed" not in error["message"]


def test_multiple_installed_studies_report_selection_required_capabilities(
    tmp_path,
    monkeypatch,
) -> None:
    studies = StudyRegistry(
        [
            _bundle("study-one", "first-study-marker"),
            _bundle("study-two", "second-study-marker"),
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
    assert {
        capability["message"]
        for capability in capabilities.values()
    } == {"Multiple study packages are installed. Select an active study."}


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
