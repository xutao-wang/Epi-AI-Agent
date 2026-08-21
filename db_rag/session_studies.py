from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace

import chromadb

from db_rag.catalog import (
    SemanticSchemaCatalog,
    UnavailableSemanticSchemaCatalog,
    load_full_schema_catalog,
)
from db_rag.config import DbRagRuntimePaths
from db_rag.retrieval_status import EmbeddingReasonCode
from db_rag.local_knowledge import (
    LocalPublicationKnowledge,
    SemanticPublicationKnowledge,
    UnavailableSemanticPublicationKnowledge,
)
from db_rag.readiness import DbRagReadiness, resolve_db_rag_readiness
from db_rag.vectorstore import OpenAIEmbeddingFunction
from epi_agent.studies import StudyBundle, StudyRegistry


@dataclass(frozen=True)
class BoundStudyRegistry:
    studies: StudyRegistry
    readiness: Mapping[str, DbRagReadiness]


def _unavailable(message: str) -> DbRagReadiness:
    return DbRagReadiness(status="not_configured", message=message)


def _catalog_data(paths: DbRagRuntimePaths) -> dict[str, object]:
    try:
        return load_full_schema_catalog(paths.catalog_path)
    except (OSError, json.JSONDecodeError):
        return {"tables": [], "columns": []}


def _unavailable_study(
    study: StudyBundle,
    catalog_data: dict[str, object],
    *,
    embedding_model: str,
    reason_code: EmbeddingReasonCode,
) -> StudyBundle:
    return replace(
        study,
        catalog=UnavailableSemanticSchemaCatalog(
            catalog_data,
            default_source_id=study.source_id,
            embedding_model=embedding_model,
            unavailable_reason_code=reason_code,
        ),
        knowledge=(
            UnavailableSemanticPublicationKnowledge(
                study.knowledge,
                embedding_model=embedding_model,
                reason_code=reason_code,
            )
            if isinstance(study.knowledge, LocalPublicationKnowledge)
            else study.knowledge
        ),
    )


def bind_session_studies(
    studies: StudyRegistry,
    *,
    api_key: str,
    expected_embedding_model: str,
) -> BoundStudyRegistry:
    """Attach isolated semantic catalogs using only this session's key."""

    bound_studies: list[StudyBundle] = []
    readiness_by_study: dict[str, DbRagReadiness] = {}
    embedders: dict[str, OpenAIEmbeddingFunction] = {}

    for study in studies.values:
        paths = study.db_rag_paths
        if not isinstance(paths, DbRagRuntimePaths):
            readiness = _unavailable(
                "Semantic catalog assets are unavailable for this study."
            )
            bound_studies.append(
                _unavailable_study(
                    study,
                    {"tables": [], "columns": []},
                    embedding_model=expected_embedding_model,
                    reason_code="EMBEDDING_CONFIGURATION_UNAVAILABLE",
                )
            )
            readiness_by_study[study.study_id] = readiness
            continue

        catalog_data = _catalog_data(paths)
        if not api_key.strip():
            readiness = DbRagReadiness(
                status="available",
                message=(
                    "DB-RAG dataset retrieval is available with lexical fallback; "
                    "OPENAI_API_KEY is not configured."
                ),
            )
            bound_studies.append(
                _unavailable_study(
                    study,
                    catalog_data,
                    embedding_model=paths.embedding_model,
                    reason_code="EMBEDDING_CREDENTIALS_MISSING",
                )
            )
            readiness_by_study[study.study_id] = readiness
            continue
        readiness = resolve_db_rag_readiness(
            paths=paths,
            expected_embedding_model=expected_embedding_model,
        )
        if not readiness.available:
            bound_studies.append(
                _unavailable_study(
                    study,
                    catalog_data,
                    embedding_model=paths.embedding_model,
                    reason_code="EMBEDDING_CONFIGURATION_UNAVAILABLE",
                )
            )
            readiness_by_study[study.study_id] = readiness
            continue

        try:
            embedder = embedders.get(paths.embedding_model)
            if embedder is None:
                embedder = OpenAIEmbeddingFunction(
                    paths.embedding_model,
                    api_key=api_key,
                )
                embedders[paths.embedding_model] = embedder
            client = chromadb.PersistentClient(path=str(paths.chroma_path))
            table_collection = client.get_collection(
                "table_summaries",
                embedding_function=embedder,
            )
            column_collection = client.get_collection(
                "column_chunks",
                embedding_function=embedder,
            )
            bound_knowledge = study.knowledge
            if isinstance(bound_knowledge, LocalPublicationKnowledge):
                try:
                    knowledge_collection = client.get_collection(
                        "study_knowledge",
                        embedding_function=embedder,
                    )
                    bound_knowledge = SemanticPublicationKnowledge(
                        bound_knowledge,
                        collection=knowledge_collection,
                        embedding_function=embedder,
                        embedding_model=paths.embedding_model,
                    )
                except Exception:
                    bound_knowledge = UnavailableSemanticPublicationKnowledge(
                        bound_knowledge,
                        embedding_model=paths.embedding_model,
                        reason_code="EMBEDDING_INDEX_UNAVAILABLE",
                    )
            bound = replace(
                study,
                catalog=SemanticSchemaCatalog(
                    catalog_data,
                    table_collection=table_collection,
                    column_collection=column_collection,
                    embedding_function=embedder,
                    default_source_id=study.source_id,
                ),
                knowledge=bound_knowledge,
            )
        except Exception:
            readiness = _unavailable(
                "Semantic catalog binding is unavailable for this study."
            )
            bound = _unavailable_study(
                study,
                catalog_data,
                embedding_model=paths.embedding_model,
                reason_code="EMBEDDING_INDEX_UNAVAILABLE",
            )

        bound_studies.append(bound)
        readiness_by_study[study.study_id] = readiness

    return BoundStudyRegistry(
        studies=StudyRegistry(bound_studies),
        readiness=readiness_by_study,
    )


__all__ = ["BoundStudyRegistry", "bind_session_studies"]
