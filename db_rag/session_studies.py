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
from db_rag.config import EMBEDDING_MODEL
from db_rag.embedding_routes import EmbeddingRoute
from db_rag.retrieval_status import EmbeddingReasonCode, lexical_fallback_status
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
    embedding_route: EmbeddingRoute,
    reason_code: EmbeddingReasonCode,
) -> StudyBundle:
    return replace(
        study,
        catalog=UnavailableSemanticSchemaCatalog(
            catalog_data,
            default_source_id=study.source_id,
            embedding_model=embedding_route.model,
            embedding_provider=embedding_route.provider,
            embedding_credential_env=embedding_route.credential_env,
            unavailable_reason_code=reason_code,
        ),
        knowledge=(
            UnavailableSemanticPublicationKnowledge(
                study.knowledge,
                embedding_model=embedding_route.model,
                embedding_provider=embedding_route.provider,
                embedding_credential_env=embedding_route.credential_env,
                reason_code=reason_code,
            )
            if isinstance(study.knowledge, LocalPublicationKnowledge)
            else study.knowledge
        ),
        study_design=(
            study.study_design.with_embedding_route(embedding_route)
            if hasattr(study.study_design, "with_embedding_route")
            else study.study_design
        ),
    )


def bind_session_studies(
    studies: StudyRegistry,
    *,
    embedding_route: EmbeddingRoute | None = None,
    api_key: str = "",
    expected_embedding_model: str = EMBEDDING_MODEL,
) -> BoundStudyRegistry:
    """Attach isolated semantic catalogs using only this session's key."""

    if embedding_route is None:
        embedding_route = EmbeddingRoute(
            model=expected_embedding_model,
            provider="openai",
            credential_env="OPENAI_API_KEY",
            api_key=api_key.strip(),
            factory=(
                lambda model, key: OpenAIEmbeddingFunction(model, api_key=key)
            ),
            unavailable_reason_code=(
                None if api_key.strip() else "EMBEDDING_CREDENTIALS_MISSING"
            ),
        )
    expected_embedding_model = embedding_route.model
    bound_studies: list[StudyBundle] = []
    readiness_by_study: dict[str, DbRagReadiness] = {}
    embedders: dict[str, object] = {}

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
                    embedding_route=embedding_route,
                    reason_code="EMBEDDING_CONFIGURATION_UNAVAILABLE",
                )
            )
            readiness_by_study[study.study_id] = readiness
            continue

        catalog_data = _catalog_data(paths)
        if not embedding_route.available:
            fallback_status = lexical_fallback_status(
                embedding_route.model,
                embedding_route.unavailable_reason_code
                or "EMBEDDING_CONFIGURATION_UNAVAILABLE",
                provider=embedding_route.provider,
                credential_env=embedding_route.credential_env,
            )
            readiness = DbRagReadiness(
                status="available",
                message=(
                    "DB-RAG dataset retrieval is available with lexical fallback; "
                    f"{fallback_status.as_dict()['message']}"
                ),
            )
            bound_studies.append(
                _unavailable_study(
                    study,
                    catalog_data,
                    embedding_route=embedding_route,
                    reason_code=(
                        embedding_route.unavailable_reason_code
                        or "EMBEDDING_CONFIGURATION_UNAVAILABLE"
                    ),
                )
            )
            readiness_by_study[study.study_id] = readiness
            continue
        if paths.embedding_model != expected_embedding_model:
            incompatible_route = replace(
                embedding_route,
                unavailable_reason_code="EMBEDDING_INDEX_INCOMPATIBLE",
            )
            readiness = DbRagReadiness(
                status="available",
                message=(
                    "DB-RAG dataset retrieval is available with lexical fallback; "
                    "the selected embedding profile is incompatible with this "
                    "study's semantic index."
                ),
            )
            bound_studies.append(
                _unavailable_study(
                    study,
                    catalog_data,
                    embedding_route=incompatible_route,
                    reason_code="EMBEDDING_INDEX_INCOMPATIBLE",
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
                    embedding_route=embedding_route,
                    reason_code="EMBEDDING_CONFIGURATION_UNAVAILABLE",
                )
            )
            readiness_by_study[study.study_id] = readiness
            continue

        try:
            embedder = embedders.get(paths.embedding_model)
            if embedder is None:
                embedder = embedding_route.create_embedding_function()
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
                        embedding_provider=embedding_route.provider,
                        embedding_credential_env=embedding_route.credential_env,
                    )
                except Exception:
                    bound_knowledge = UnavailableSemanticPublicationKnowledge(
                        bound_knowledge,
                        embedding_model=paths.embedding_model,
                        embedding_provider=embedding_route.provider,
                        embedding_credential_env=embedding_route.credential_env,
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
                    embedding_model=paths.embedding_model,
                    embedding_provider=embedding_route.provider,
                    embedding_credential_env=embedding_route.credential_env,
                ),
                knowledge=bound_knowledge,
                study_design=(
                    study.study_design.with_embedding_route(embedding_route)
                    if hasattr(study.study_design, "with_embedding_route")
                    else study.study_design
                ),
            )
        except Exception:
            readiness = _unavailable(
                "Semantic catalog binding is unavailable for this study."
            )
            bound = _unavailable_study(
                study,
                catalog_data,
                embedding_route=embedding_route,
                reason_code="EMBEDDING_INDEX_UNAVAILABLE",
            )

        bound_studies.append(bound)
        readiness_by_study[study.study_id] = readiness

    return BoundStudyRegistry(
        studies=StudyRegistry(bound_studies),
        readiness=readiness_by_study,
    )


__all__ = ["BoundStudyRegistry", "bind_session_studies"]
