from __future__ import annotations

import logging
import os

from api.activity_store import SqliteActivityStore
from api.conversation_history import ConversationHistoryStore, OpenAIConversationTitleGenerator
from api.deployment import checkpoint_db_path, runtime_root, static_dir, study_root
from api.runtime import ReportAgentApiRuntime
from api.schemas import RuntimeCapabilities, RuntimeCapability
from api.server import create_app
from db_rag.config import (
    resolve_db_rag_embedding_model,
    resolve_db_rag_reranker_model,
)
from db_rag.readiness import DbRagReadiness, resolve_db_rag_readiness
from epi_agent.activity import NULL_ACTIVITY_SINK
from epi_agent.studies import StudyRegistry
from utils.env_loader import load_app_environment
from graph.builder import build_graph
from llm_vllm import build_openai_llm
from study_package.registry import discover_studies
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
_STUDY_SELECTION_REQUIRED_MESSAGE = (
    "Multiple study packages are installed. Select an active study."
)
_LOGGER = logging.getLogger(__name__)


def _unselected_study_message(studies: StudyRegistry) -> str:
    if studies.values:
        return _STUDY_SELECTION_REQUIRED_MESSAGE
    return _NO_STUDY_MESSAGE


def _db_rag_readiness(
    studies: StudyRegistry,
    default_study_id: str | None,
    *,
    embedding_model: str,
) -> DbRagReadiness:
    study = studies.get(default_study_id) if default_study_id else None
    paths = getattr(study, "db_rag_paths", None)
    if paths is None:
        return DbRagReadiness(
            status="not_configured",
            message=_unselected_study_message(studies),
        )
    return resolve_db_rag_readiness(
        paths=paths,
        expected_embedding_model=embedding_model,
    )


def _capability(
    study: object | None,
    attribute: str,
    label: str,
    *,
    unavailable_message: str = _NO_STUDY_MESSAGE,
) -> RuntimeCapability:
    if study is None:
        return RuntimeCapability(
            status="not_configured",
            message=unavailable_message,
        )
    if getattr(study, attribute, None) is None:
        return RuntimeCapability(
            status="not_configured",
            message=f"The selected study package has no {label}.",
        )
    return RuntimeCapability(status="available", message=f"{label.capitalize()} is available.")


load_app_environment()

model_name = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
allowed_models = configured_openai_models(os.environ)
title_model = configured_title_model(os.environ)
max_iterations = configured_epi_agent_max_iterations(os.environ)
api_key = os.getenv("OPENAI_API_KEY", "")
runtime_root_path = runtime_root()
db_path = checkpoint_db_path(runtime_root_path)
try:
    activity_store: SqliteActivityStore | None = SqliteActivityStore(db_path)
except Exception:
    _LOGGER.exception("Agent activity persistence is unavailable")
    activity_store = None
studies = discover_studies(study_root() / "studies")
default_study_id = studies.sole_study_id()
default_study = studies.get(default_study_id) if default_study_id else None
unselected_study_message = _unselected_study_message(studies)
db_rag_embedding_model = resolve_db_rag_embedding_model()
db_rag_readiness = _db_rag_readiness(
    studies,
    default_study_id,
    embedding_model=db_rag_embedding_model,
)

def graph_factory(settings):
    profile = model_runtime_profile(settings.model_name)
    llm = build_openai_llm(
        model_name=settings.model_name,
        api_key=api_key,
    )
    return build_graph(
        llm,
        model_profile=profile,
        db_path=db_path,
        runtime_root=runtime_root_path,
        studies=studies,
        default_study_id=default_study_id,
        db_rag_readiness=db_rag_readiness,
        db_rag_embedding_model=db_rag_embedding_model,
        max_iterations=max_iterations,
        activity_sink=activity_store or NULL_ACTIVITY_SINK,
    )


default_runtime_settings = {
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

runtime = ReportAgentApiRuntime(
    graph_factory=graph_factory,
    default_runtime_settings=default_runtime_settings,
    models=list(allowed_models),
    runtime_root=runtime_root_path,
    history_store=ConversationHistoryStore(db_path),
    title_generator=OpenAIConversationTitleGenerator(
        build_openai_llm(model_name=title_model, api_key=api_key)
    ),
    activity_store=activity_store,
    capabilities=RuntimeCapabilities(
        publication_knowledge=_capability(
            default_study,
            "knowledge",
            "publication knowledge",
            unavailable_message=unselected_study_message,
        ),
        study_design=_capability(
            default_study,
            "study_design",
            "study design",
            unavailable_message=unselected_study_message,
        ),
        db_rag_dataset=RuntimeCapability(
            status=db_rag_readiness.status,
            message=db_rag_readiness.message,
        ),
    ),
)
app = create_app(runtime=runtime, static_dir=static_dir())
