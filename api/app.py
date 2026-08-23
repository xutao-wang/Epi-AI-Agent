from __future__ import annotations

import logging
from collections.abc import Mapping
import os
from pathlib import Path

from fastapi import FastAPI

from api.activity_store import SqliteActivityStore
from api.conversation_history import (
    ConversationTitleGenerator,
    ConversationHistoryStore,
)
from api.deployment import (
    checkpoint_db_path,
    runtime_root,
    static_dir,
    study_root,
)
from api.runtime import GraphBuildContext, ReportAgentApiRuntime, RuntimeSettings
from api.schemas import RuntimeCapabilities, RuntimeCapability
from api.server import create_app
from db_rag.config import resolve_db_rag_reranker_model
from db_rag.embedding_routes import EmbeddingRoute
from db_rag.embedding_startup import (
    assess_study_compatibility,
    initialize_embedding,
)
from db_rag.readiness import DbRagReadiness, resolve_db_rag_readiness
from db_rag.retrieval_status import lexical_fallback_status
from db_rag.session_studies import bind_session_studies
from epi_agent.activity import NULL_ACTIVITY_SINK
from epi_agent.studies import StudyRegistry
from epi_agent.runtimes.python import LocalPythonRuntime
from graph.builder import build_graph
from llm_vllm import build_chat_llm, build_openai_llm, resolve_provider_api_key
from study_package.registry import discover_studies
from utils.env_loader import load_app_environment
from utils.model_availability import (
    ModelAvailability,
    model_availability_from_configured_credentials,
)
from utils.model_runtime_profiles import ModelRuntimeProfile
from utils.runtime_defaults import (
    DEFAULT_MAX_AUTO_STEPS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    configured_epi_agent_max_iterations,
)


_NO_STUDY_MESSAGE = "No study package is installed."
_LOGGER = logging.getLogger(__name__)


def _history_store(
    db_path: str | os.PathLike[str],
) -> ConversationHistoryStore:
    """Open history and claim legacy rows for the fixed local principal."""
    store = ConversationHistoryStore(db_path)
    store.claim_unowned("local-user")
    return store


def _provider_key(
    profile: ModelRuntimeProfile,
    environ: Mapping[str, str],
) -> str:
    configured = (
        str(environ.get(profile.api_key_env, "") or "").strip()
        if profile.api_key_env
        else ""
    )
    if not configured and profile.api_key_required:
        env_name = profile.api_key_env or "the provider API key"
        raise ValueError(f"{env_name} is required.")
    return resolve_provider_api_key(profile, api_key=configured)


def _db_rag_readiness(
    studies: StudyRegistry,
    *,
    embedding_route: EmbeddingRoute,
) -> DbRagReadiness:
    if not studies.values:
        return DbRagReadiness(
            status="not_configured",
            message=_NO_STUDY_MESSAGE,
        )
    readiness = [
        resolve_db_rag_readiness(
            paths=paths,
            expected_embedding_model=(
                embedding_route.model if embedding_route.available else None
            ),
        )
        for study in studies.values
        if (paths := getattr(study, "db_rag_paths", None)) is not None
    ]
    available_count = sum(item.available for item in readiness)
    if available_count:
        if not embedding_route.available:
            fallback_status = lexical_fallback_status(
                embedding_route.model,
                embedding_route.unavailable_reason_code
                or "EMBEDDING_CONFIGURATION_UNAVAILABLE",
                provider=embedding_route.provider,
                credential_env=embedding_route.credential_env,
            )
            return DbRagReadiness(
                status="available",
                message=(
                    "DB-RAG dataset retrieval is available with lexical fallback; "
                    f"{fallback_status.as_dict()['message']}"
                ),
            )
        return DbRagReadiness(
            status="available",
            message=(
                "DB-RAG dataset retrieval is available for "
                f"{available_count} installed "
                f"stud{'y' if available_count == 1 else 'ies'}."
            ),
        )
    return DbRagReadiness(
        status="not_configured",
        message="No installed study package has an available DB-RAG dataset.",
    )


def _capability(
    studies: StudyRegistry,
    attribute: str,
    label: str,
) -> RuntimeCapability:
    if not studies.values:
        return RuntimeCapability(
            status="not_configured",
            message=_NO_STUDY_MESSAGE,
        )
    available_count = sum(
        getattr(study, attribute, None) is not None
        for study in studies.values
    )
    if not available_count:
        return RuntimeCapability(
            status="not_configured",
            message=f"No installed study package has {label}.",
        )
    return RuntimeCapability(
        status="available",
        message=(
            f"{label.capitalize()} is available for {available_count} installed "
            f"stud{'y' if available_count == 1 else 'ies'}."
        ),
    )


def build_application(
    *,
    environ: Mapping[str, str] | None = None,
    model_availability: ModelAvailability | None = None,
) -> FastAPI:
    """Build one fully configured application from startup-time settings."""
    if environ is None:
        load_app_environment()
        environ = os.environ

    catalog = model_availability or model_availability_from_configured_credentials(
        environ
    )
    model_name = catalog.default_model_id
    allowed_models = catalog.available_model_ids
    title_model = catalog.title_model_id
    max_iterations = configured_epi_agent_max_iterations(environ)
    default_profile = catalog.registered_profiles[model_name]
    _provider_key(default_profile, environ)
    title_profile = catalog.registered_profiles[title_model]
    _provider_key(title_profile, environ)

    runtime_root_path = (
        Path(environ["REPORT_AGENT_RUNTIME_ROOT"])
        if environ.get("REPORT_AGENT_RUNTIME_ROOT", "").strip()
        else runtime_root()
    )
    selected_study_root = (
        Path(environ["REPORT_AGENT_STUDY_ROOT"])
        if environ.get("REPORT_AGENT_STUDY_ROOT", "").strip()
        else study_root()
    )
    db_path = (
        Path(environ["REPORT_AGENT_CHECKPOINT_DB_PATH"])
        if environ.get("REPORT_AGENT_CHECKPOINT_DB_PATH", "").strip()
        else checkpoint_db_path(runtime_root_path)
    )
    try:
        activity_store: SqliteActivityStore | None = SqliteActivityStore(db_path)
    except Exception:
        _LOGGER.exception("Agent activity persistence is unavailable")
        activity_store = None
    selected_static_dir = (
        Path(environ["REPORT_AGENT_STATIC_DIR"])
        if environ.get("REPORT_AGENT_STATIC_DIR", "").strip()
        else static_dir()
    )

    studies = discover_studies(selected_study_root / "studies")
    embedding_startup = initialize_embedding(environ)
    embedding_route = embedding_startup.route
    embedding_startup_status = assess_study_compatibility(
        embedding_startup.status,
        embedding_route,
        studies,
    )
    db_rag_embedding_model = embedding_route.model
    db_rag_readiness = _db_rag_readiness(
        studies,
        embedding_route=embedding_route,
    )

    def graph_factory(
        settings: RuntimeSettings,
        context: GraphBuildContext,
    ):
        bound_studies = bind_session_studies(
            studies,
            embedding_route=embedding_route,
        )
        profile = catalog.registered_profiles[settings.model_name]
        llm_builder = (
            build_openai_llm
            if profile.provider == "openai"
            else build_chat_llm
        )
        llm = llm_builder(
            model_name=settings.model_name,
            api_key=_provider_key(profile, environ),
            temperature=settings.temperature,
            top_p=settings.top_p,
        )
        return build_graph(
            llm,
            model_profile=profile,
            db_path=db_path,
            runtime_root=runtime_root_path,
            storage=context.storage,
            studies=bound_studies.studies,
            db_rag_readiness_by_study=bound_studies.readiness,
            db_rag_embedding_model=db_rag_embedding_model,
            max_iterations=max_iterations,
            python_runtime=LocalPythonRuntime(
                runtime_root=context.storage.execution,
            ),
            activity_sink=activity_store or NULL_ACTIVITY_SINK,
        )

    runtime_settings = {
        "model_name": model_name,
        "temperature": DEFAULT_TEMPERATURE,
        "top_p": DEFAULT_TOP_P,
        "max_steps": DEFAULT_MAX_AUTO_STEPS,
        "timeout_seconds": default_profile.workflow_timeout_seconds,
        "db_rag_embedding_model": db_rag_embedding_model,
        "db_rag_reranker_model": resolve_db_rag_reranker_model() or "disabled",
    }
    history_store = _history_store(db_path)
    report_runtime = ReportAgentApiRuntime(
        graph_factory=graph_factory,
        default_runtime_settings=runtime_settings,
        models=list(allowed_models),
        registered_models=catalog.registered_profiles,
        runtime_root=runtime_root_path,
        checkpoint_path=db_path,
        history_store=history_store,
        title_generator_factory=lambda _settings, _provider_api_key: (
            ConversationTitleGenerator(
                build_chat_llm(
                    model_name=title_model,
                    api_key=_provider_key(title_profile, environ),
                )
            )
        ),
        capabilities=RuntimeCapabilities(
            publication_knowledge=_capability(
                studies,
                "knowledge",
                "publication knowledge",
            ),
            study_design=_capability(
                studies,
                "study_design",
                "study design",
            ),
            db_rag_dataset=RuntimeCapability(
                status=db_rag_readiness.status,
                message=db_rag_readiness.message,
            ),
        ),
        embedding_startup_status=embedding_startup_status,
        activity_store=activity_store,
    )

    application = create_app(
        runtime=report_runtime,
        provider_api_key="local-environment",
        static_dir=selected_static_dir,
    )
    application.state.report_agent_runtime = report_runtime
    return application

app = build_application()
runtime = app.state.report_agent_runtime


__all__ = ["app", "build_application", "runtime"]
