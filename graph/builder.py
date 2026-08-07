from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph

from db_rag.readiness import DbRagReadiness, resolve_db_rag_readiness
from epi_agent.agent import build_general_epi_agent_graph
from epi_agent.runtimes.python import LocalPythonRuntime
from epi_agent.studies import StudyRegistry
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import (
    AttachmentReaderService,
    LangChainVisionAnalyzer,
)
from utils.runtime_defaults import DEFAULT_EPI_AGENT_MAX_ITERATIONS
from utils.model_runtime_profiles import ModelRuntimeProfile
from utils.user_storage import ThreadStorageScope, UserStorageLayout


def _build_attachment_reader_service(
    llm: Any,
    runtime_root: str | Path,
) -> AttachmentReaderService:
    root = Path(runtime_root).expanduser().resolve()
    return AttachmentReaderService(
        LocalAttachmentStore(root),
        runtime_root=root,
        vision_analyzer=LangChainVisionAnalyzer(llm),
    )


def build_graph(
    llm: Any,
    *,
    model_profile: ModelRuntimeProfile,
    db_path: str | Path,
    runtime_root: str | Path,
    storage: ThreadStorageScope | None = None,
    studies: StudyRegistry,
    default_study_id: str | None,
    db_rag_readiness: DbRagReadiness | None = None,
    db_rag_embedding_model: str | None = None,
    max_iterations: int = DEFAULT_EPI_AGENT_MAX_ITERATIONS,
    python_runtime: LocalPythonRuntime | None = None,
) -> CompiledStateGraph:
    """Compile the single checkpointed EpiAgent used by FastAPI."""

    root = Path(runtime_root).expanduser().resolve()
    if storage is not None:
        expected_storage = UserStorageLayout(root).thread(
            storage.owner_user_id,
            storage.thread_id,
        )
        if storage.root != expected_storage.root:
            raise ValueError("storage does not belong to runtime_root")
    attachment_reader_service = _build_attachment_reader_service(llm, root)
    selected_study = studies.get(default_study_id) if default_study_id else None
    paths = getattr(selected_study, "db_rag_paths", None)
    readiness = db_rag_readiness or (
        resolve_db_rag_readiness(
            paths=paths,
            expected_embedding_model=db_rag_embedding_model,
        )
        if paths is not None
        else DbRagReadiness(
            status="not_configured",
            message=(
                "Multiple study packages are installed. Select an active study."
                if studies.values
                else "No study package is installed."
            ),
        )
    )

    checkpoint_path = Path(db_path).expanduser().resolve()
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        checkpoint_path,
        check_same_thread=False,
    )
    execution_root = storage.execution if storage is not None else root
    return build_general_epi_agent_graph(
        llm=llm,
        model_profile=model_profile,
        service=attachment_reader_service,
        studies=studies,
        default_study_id=default_study_id,
        python_runtime=python_runtime
        or LocalPythonRuntime(runtime_root=execution_root),
        runtime_root=root,
        include_db_rag=readiness.available,
        checkpointer=SqliteSaver(connection),
        max_iterations=max_iterations,
    )


__all__ = ["build_graph"]
