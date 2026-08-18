from __future__ import annotations

import logging
from collections.abc import Mapping
import os
from pathlib import Path

from fastapi import FastAPI

from api.activity_store import SqliteActivityStore
from api.auth import (
    AuthenticatedUser,
    CognitoTokenVerifier,
    LOCAL_SESSION_ID,
    LocalTokenVerifier,
    RequestIdentity,
)
from api.conversation_history import (
    ConversationHistoryStore,
    OpenAIConversationTitleGenerator,
)
from api.deployment import (
    checkpoint_db_path,
    python_worker_launcher,
    runtime_root,
    static_dir,
    study_root,
)
from api.provider_credentials import ProviderCredentialStore
from api.public_config import application_auth_config, public_app_config
from api.runtime import GraphBuildContext, ReportAgentApiRuntime, RuntimeSettings
from api.schemas import RuntimeCapabilities, RuntimeCapability
from api.server import create_app
from db_rag.config import (
    resolve_db_rag_embedding_model,
    resolve_db_rag_reranker_model,
)
from db_rag.readiness import DbRagReadiness, resolve_db_rag_readiness
from db_rag.session_studies import bind_session_studies
from epi_agent.activity import NULL_ACTIVITY_SINK
from epi_agent.studies import StudyRegistry
from epi_agent.runtimes.python import LocalPythonRuntime
from graph.builder import build_graph
from llm_vllm import build_openai_llm
from study_package.registry import discover_studies
from utils.env_loader import load_app_environment
from utils.model_runtime_profiles import model_runtime_profile
from utils.runtime_defaults import (
    DEFAULT_MAX_AUTO_STEPS,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    configured_epi_agent_max_iterations,
    configured_openai_models,
    configured_title_model,
)


_NO_STUDY_MESSAGE = "No study package is installed."
_LOGGER = logging.getLogger(__name__)


def _history_store_for_auth_mode(
    db_path: str | os.PathLike[str],
    *,
    auth_mode: str,
) -> ConversationHistoryStore:
    """Open history and claim legacy rows only for the fixed local principal."""
    store = ConversationHistoryStore(db_path)
    if auth_mode == "local":
        store.claim_unowned("local-user")
    return store


def _db_rag_readiness(
    studies: StudyRegistry,
    *,
    embedding_model: str,
) -> DbRagReadiness:
    if not studies.values:
        return DbRagReadiness(
            status="not_configured",
            message=_NO_STUDY_MESSAGE,
        )
    readiness = [
        resolve_db_rag_readiness(
            paths=paths,
            expected_embedding_model=embedding_model,
        )
        for study in studies.values
        if (paths := getattr(study, "db_rag_paths", None)) is not None
    ]
    available_count = sum(item.available for item in readiness)
    if available_count:
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


def build_application(*, environ: Mapping[str, str] | None = None) -> FastAPI:
    """Build one fully configured application from startup-time settings."""
    if environ is None:
        load_app_environment()
        environ = os.environ

    auth_config = application_auth_config(environ)
    model_name = environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    allowed_models = configured_openai_models(environ)
    title_model = configured_title_model(environ)
    max_iterations = configured_epi_agent_max_iterations(environ)
    worker_launcher = python_worker_launcher(environ)

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
    db_rag_embedding_model = resolve_db_rag_embedding_model()
    db_rag_readiness = _db_rag_readiness(
        studies,
        embedding_model=db_rag_embedding_model,
    )

    def graph_factory(
        settings: RuntimeSettings,
        context: GraphBuildContext,
    ):
        bound_studies = bind_session_studies(
            studies,
            api_key=context.provider_api_key,
            expected_embedding_model=db_rag_embedding_model,
        )
        profile = model_runtime_profile(settings.model_name)
        llm = build_openai_llm(
            model_name=settings.model_name,
            api_key=context.provider_api_key,
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
                worker_launcher=worker_launcher,
            ),
            activity_sink=activity_store or NULL_ACTIVITY_SINK,
        )

    runtime_settings = {
        "model_name": model_name,
        "temperature": DEFAULT_TEMPERATURE,
        "top_p": DEFAULT_TOP_P,
        "max_steps": DEFAULT_MAX_AUTO_STEPS,
        "timeout_seconds": model_runtime_profile(
            model_name
        ).workflow_timeout_seconds,
        "db_rag_embedding_model": db_rag_embedding_model,
        "db_rag_reranker_model": resolve_db_rag_reranker_model() or "disabled",
    }
    history_store = _history_store_for_auth_mode(
        db_path,
        auth_mode=auth_config.mode,
    )
    report_runtime = ReportAgentApiRuntime(
        graph_factory=graph_factory,
        default_runtime_settings=runtime_settings,
        models=list(allowed_models),
        runtime_root=runtime_root_path,
        checkpoint_path=db_path,
        history_store=history_store,
        title_generator_factory=lambda _settings, provider_api_key: (
            OpenAIConversationTitleGenerator.from_credentials(
                model_name=title_model,
                api_key=provider_api_key,
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
        activity_store=activity_store,
    )

    credential_store = ProviderCredentialStore(
        on_expire=report_runtime.release_session,
    )
    if auth_config.mode == "local":
        token_verifier = LocalTokenVerifier()
        local_api_key = str(environ.get("OPENAI_API_KEY", "") or "").strip()
        if not local_api_key:
            raise ValueError("OPENAI_API_KEY is required.")
        credential_store.put(
            RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            ),
            local_api_key,
        )
    else:
        assert auth_config.cognito_issuer is not None
        assert auth_config.cognito_app_client_id is not None
        token_verifier = CognitoTokenVerifier(
            issuer=auth_config.cognito_issuer,
            app_client_id=auth_config.cognito_app_client_id,
        )

    application = create_app(
        runtime=report_runtime,
        static_dir=selected_static_dir,
        public_config=public_app_config(auth_config),
        token_verifier=token_verifier,
        credential_store=credential_store,
    )
    application.state.report_agent_runtime = report_runtime
    application.state.provider_credential_store = credential_store
    application.state.token_verifier = token_verifier
    return application

app = build_application()
runtime = app.state.report_agent_runtime


__all__ = ["app", "build_application", "runtime"]
