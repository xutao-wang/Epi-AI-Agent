from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph

from db_rag.readiness import DbRagReadiness, resolve_db_rag_readiness
from epi_agent.activity import ActivitySink, NULL_ACTIVITY_SINK
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
    *,
    supports_vision: bool = True,
) -> AttachmentReaderService:
    root = Path(runtime_root).expanduser().resolve()
    return AttachmentReaderService(
        LocalAttachmentStore(root),
        runtime_root=root,
        vision_analyzer=LangChainVisionAnalyzer(llm) if supports_vision else None,
    )


def build_graph(
    llm: Any,
    *,
    model_profile: ModelRuntimeProfile,
    db_path: str | Path,
    runtime_root: str | Path,
    storage: ThreadStorageScope | None = None,
    studies: StudyRegistry,
    db_rag_readiness: DbRagReadiness | None = None,
    db_rag_readiness_by_study: Mapping[str, DbRagReadiness] | None = None,
    db_rag_embedding_model: str | None = None,
    max_iterations: int = DEFAULT_EPI_AGENT_MAX_ITERATIONS,
    python_runtime: LocalPythonRuntime | None = None,
    activity_sink: ActivitySink = NULL_ACTIVITY_SINK,
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
    attachment_reader_service = (
        _build_attachment_reader_service(llm, root)
        if model_profile.supports_vision
        else _build_attachment_reader_service(
            llm,
            root,
            supports_vision=False,
        )
    )
    if db_rag_readiness_by_study is not None:
        include_db_rag = any(
            readiness.available
            for readiness in db_rag_readiness_by_study.values()
        )
    else:
        if db_rag_readiness is not None:
            include_db_rag = db_rag_readiness.available
        else:
            readiness_values = [
                resolve_db_rag_readiness(
                    paths=paths,
                    expected_embedding_model=db_rag_embedding_model,
                )
                for study in studies.values
                if (paths := getattr(study, "db_rag_paths", None)) is not None
            ]
            include_db_rag = any(
                readiness.available for readiness in readiness_values
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
        python_runtime=python_runtime
        or LocalPythonRuntime(runtime_root=execution_root),
        runtime_root=root,
        include_db_rag=include_db_rag,
        checkpointer=SqliteSaver(connection),
        max_iterations=max_iterations,
        activity_sink=activity_sink,
    )


__all__ = ["build_graph"]
