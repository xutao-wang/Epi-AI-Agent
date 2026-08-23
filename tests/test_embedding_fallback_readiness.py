from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from db_rag.config import EMBEDDING_MODEL
from db_rag.embedding_routes import resolve_embedding_route
from db_rag.embedding_startup import initialize_embedding as real_initialize_embedding
from db_rag.readiness import DbRagReadiness
from db_rag.study import build_study_bundle
from epi_agent.studies import StudyBundle, StudyRegistry
from study_package.installer import InstalledStudy
from study_package.manifest import parse_study_package_manifest
from tests.study_package_fixtures import create_package_root, minimal_manifest


def test_missing_embedding_key_keeps_lexical_db_rag_available(monkeypatch) -> None:
    import api.app as app_module

    monkeypatch.setattr(
        app_module,
        "resolve_db_rag_readiness",
        lambda **_kwargs: DbRagReadiness(
            status="available",
            message="DB-RAG dataset is available.",
        ),
    )
    studies = StudyRegistry(
        [
            StudyBundle(
                study_id="study-1",
                label="Study One",
                knowledge=None,
                catalog=None,
                data_sources={},
                db_rag_paths=object(),
            )
        ]
    )

    readiness = app_module._db_rag_readiness(
        studies,
        embedding_route=resolve_embedding_route({}, EMBEDDING_MODEL),
    )

    assert readiness.available is True
    assert "lexical fallback" in readiness.message
    assert "OPENAI_API_KEY is not configured" in readiness.message


def test_build_application_initializes_embedding_once_per_application(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import api.app as app_module
    from epi_agent.studies import StudyRegistry
    from utils.model_availability import (
        ProviderEndpoint,
        build_model_availability,
    )

    calls: list[dict[str, str]] = []

    def initialize(environ):
        calls.append(dict(environ))
        return real_initialize_embedding({})

    monkeypatch.setattr(app_module, "initialize_embedding", initialize, raising=False)
    monkeypatch.setattr(
        app_module,
        "discover_studies",
        lambda _root: StudyRegistry(),
    )
    environ = {
        "ANTHROPIC_API_KEY": "anthropic-key",
        "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
        "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
    }
    availability = build_model_availability(
        environ,
        {ProviderEndpoint("anthropic", "ANTHROPIC_API_KEY")},
    )

    application = app_module.build_application(
        environ=environ,
        model_availability=availability,
    )
    runtime = application.state.report_agent_runtime
    runtime.runtime_options()
    runtime.create_thread()
    runtime.create_thread()

    assert len(calls) == 1
    assert (
        runtime.embedding_startup_status.reason_code
        == "EMBEDDING_CREDENTIALS_MISSING"
    )


def test_unavailable_future_embedding_route_keeps_db_rag_available(monkeypatch) -> None:
    import api.app as app_module

    expected_models: list[str | None] = []

    def readiness(**kwargs):
        expected_models.append(kwargs["expected_embedding_model"])
        return DbRagReadiness(
            status="available",
            message="DB-RAG dataset is available.",
        )

    monkeypatch.setattr(
        app_module,
        "resolve_db_rag_readiness",
        readiness,
    )
    studies = StudyRegistry(
        [
            StudyBundle(
                study_id="study-1",
                label="Study One",
                knowledge=None,
                catalog=None,
                data_sources={},
                db_rag_paths=object(),
            )
        ]
    )

    readiness = app_module._db_rag_readiness(
        studies,
        embedding_route=resolve_embedding_route(
            {"OPENROUTER_API_KEY": "router-key"},
            "OpenRouter/Qwen/qwen3-embedding-8b",
        ),
    )

    assert readiness.available is True
    assert "lexical fallback" in readiness.message
    assert "openrouter" in readiness.message
    assert "adapter" in readiness.message
    assert expected_models == [None]


def test_anthropic_only_application_binds_all_retrieval_as_lexical(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import api.app as app_module
    from api.runtime import GraphBuildContext
    from utils.model_availability import (
        ProviderEndpoint,
        build_model_availability,
    )
    from utils.user_storage import UserStorageLayout

    manifest_data = minimal_manifest(
        format_version=3,
        study_design_format="markdown",
    )
    manifest_data["knowledge"] = {"root": "knowledge"}
    package_root = create_package_root(
        tmp_path / "package-source",
        manifest=manifest_data,
        study_design_documents={
            "overview.md": "# Overview\n\nAuthoritative cohort overview.",
            "reference/visits.md": "# Visits\n\nHousehold visit schedule.",
        },
    )
    manifest = parse_study_package_manifest(manifest_data)
    bundle = build_study_bundle(
        InstalledStudy(
            study_id=manifest.study_id,
            package_version=manifest.package_version,
            package_root=package_root,
            archive_sha256="a" * 64,
            manifest=manifest,
        )
    )
    discovered = StudyRegistry([bundle])
    captured: dict[str, object] = {}
    monkeypatch.setattr(app_module, "discover_studies", lambda _root: discovered)
    monkeypatch.setattr(app_module, "build_chat_llm", lambda **_kwargs: "claude")
    monkeypatch.setattr(
        app_module,
        "build_graph",
        lambda _llm, **kwargs: captured.update(kwargs) or "graph",
    )
    environ = {
        "ANTHROPIC_API_KEY": "anthropic-only-key",
        "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
        "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
    }
    availability = build_model_availability(
        environ,
        {ProviderEndpoint("anthropic", "ANTHROPIC_API_KEY")},
    )
    application = app_module.build_application(
        environ=environ,
        model_availability=availability,
    )
    storage = UserStorageLayout(tmp_path / "runtime").thread(
        "user-a",
        "thread-a",
    )

    application.state.report_agent_runtime.graph_factory(
        SimpleNamespace(
            model_name="claude-opus-5",
            temperature=None,
            top_p=None,
        ),
        GraphBuildContext(
            owner_user_id="user-a",
            session_id="11111111-1111-4111-8111-111111111111",
            thread_id="thread-a",
            provider_api_key="local-environment",
            storage=storage,
        ),
    )

    bound = captured["studies"].require("example-study")
    outcomes = (
        bound.catalog.search_many_with_status(["participant"], limit=3),
        bound.knowledge.search_with_status(
            "package-relative knowledge marker",
            limit=3,
        ),
        bound.study_design.search_with_status("visit schedule", limit=3),
    )
    assert all(outcome.value for outcome in outcomes)
    assert {outcome.status.mode for outcome in outcomes} == {"lexical_fallback"}
    assert {outcome.status.provider for outcome in outcomes} == {"openai"}
    assert {outcome.status.reason_code for outcome in outcomes} == {
        "EMBEDDING_CREDENTIALS_MISSING"
    }
    assert application.state.report_agent_runtime.models == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ]
