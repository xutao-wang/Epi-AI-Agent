from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from db_rag.local_knowledge import (
    SemanticPublicationKnowledgeUnavailableError,
)
from db_rag.config import EMBEDDING_MODEL
from db_rag.retrieval_status import RetrievalOutcome, hybrid_status
from epi_agent.protocol import (
    ArtifactStore,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    require_context_study,
)
from epi_agent.registry import ToolRegistry
from epi_agent.tool_packs.publication.pubmed import (
    PubMedClient,
    PubMedConfigurationError,
    PubMedRequestError,
    is_pubmed_configured,
)


_MAX_SEARCH_HITS = 10
_MAX_EXCERPT_CHARS = 500


class SearchStudyEvidenceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    study_id: str = Field(min_length=1, max_length=512)
    query: str = Field(min_length=1, max_length=8_000)
    limit: int = Field(default=5, ge=1)


class StudySourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    study_id: str = Field(min_length=1, max_length=512)
    source_id: str = Field(min_length=1, max_length=512)


class OpenStudySourceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_ref: StudySourceRef


class SearchPubMedArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=5, ge=1, le=_MAX_SEARCH_HITS)
    published_after: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    published_before: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_date_range(self) -> "SearchPubMedArguments":
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after > self.published_before
        ):
            raise ValueError("published_after must not be later than published_before")
        return self


class OpenPubMedArticleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pmid: str = Field(pattern=r"^\d{1,12}$")


def _store(context: ToolContext) -> ArtifactStore:
    store = context.artifact_store
    if not callable(getattr(store, "save_artifact", None)):
        raise ToolExecutionError(
            "ARTIFACT_STORE_UNAVAILABLE",
            "The publication artifact store is unavailable.",
            recoverable=False,
        )
    return store


def _bounded(value: Any) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return str(value or "")[:_MAX_EXCERPT_CHARS]


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _evidence_hit(value: Any, *, study_id: str) -> dict[str, Any]:
    provenance = _field(value, "provenance")
    source_id = _bounded(_field(value, "source_id"))
    row = {
        "source_id": source_id,
        "title": _bounded(_field(value, "title")),
        "section": _bounded(_field(value, "section")),
        "excerpt": _bounded(_field(value, "text")),
        "knowledge_type": _bounded(_field(value, "knowledge_type")),
        "knowledge_role": _bounded(_field(value, "knowledge_role")),
        "source_locator": _bounded(_field(value, "source_locator")),
        "indexed_path": _bounded(_field(value, "indexed_path")),
        "evidence_ids": _bounded(_field(value, "evidence_ids")),
        "matched_by": _bounded(_field(provenance, "matched_by")),
    }
    result: dict[str, Any] = {key: item for key, item in row.items() if item}
    if source_id:
        result["source_ref"] = {
            "study_id": study_id,
            "source_id": source_id,
        }
    return result


def _save_observation(
    context: ToolContext,
    *,
    kind: str,
    content: dict[str, Any],
    producer: str,
    summary: str,
    study_id: str | None = None,
):
    provenance = {
        "thread_id": context.thread_id,
        "producer": producer,
    }
    if study_id:
        provenance["study_id"] = study_id
    return _store(context).save_artifact(
        kind=kind,
        content=content,
        provenance=provenance,
        summary=summary,
    )


def _search(
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    study = require_context_study(context, arguments["study_id"])
    search = getattr(study.knowledge, "search", None)
    if not callable(search):
        raise ToolExecutionError(
            "STUDY_KNOWLEDGE_UNAVAILABLE",
            "The active study does not provide publication evidence search.",
            recoverable=True,
        )
    limit = min(int(arguments["limit"]), _MAX_SEARCH_HITS)
    try:
        search_with_status = getattr(study.knowledge, "search_with_status", None)
        outcome = (
            search_with_status(arguments["query"], limit=limit)
            if callable(search_with_status)
            else RetrievalOutcome(
                value=search(arguments["query"], limit=limit),
                status=hybrid_status(
                    getattr(study.knowledge, "embedding_model", EMBEDDING_MODEL)
                ),
            )
        )
        hits = [
            _evidence_hit(hit, study_id=study.study_id)
            for hit in outcome.value
        ][:limit]
    except SemanticPublicationKnowledgeUnavailableError as error:
        raise ToolExecutionError(
            "SEMANTIC_STUDY_KNOWLEDGE_UNAVAILABLE",
            str(error),
            recoverable=True,
        ) from error
    content = {
        "study_id": study.study_id,
        "query": arguments["query"],
        "retrieval_mode": outcome.status.mode,
        "embedding": outcome.status.as_dict(),
        "hits": hits,
    }
    reference = _save_observation(
        context,
        kind="study_evidence",
        content=content,
        producer="publication-search_study_evidence",
        summary=f"{len(hits)} publication evidence hits",
        study_id=study.study_id,
    )
    return ToolResult(
        message=json.dumps(content, sort_keys=True),
        artifacts=(reference,),
    )


def _open_source(
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    source_ref = StudySourceRef.model_validate(arguments["source_ref"])
    study = require_context_study(context, source_ref.study_id)
    open_source = getattr(study.knowledge, "open_source", None)
    if not callable(open_source):
        raise ToolExecutionError(
            "STUDY_KNOWLEDGE_UNAVAILABLE",
            "The active study does not provide exact publication lookup.",
            recoverable=True,
        )
    source_id = source_ref.source_id
    hits = [
        _evidence_hit(hit, study_id=study.study_id)
        for hit in open_source(source_id, limit=_MAX_SEARCH_HITS)
    ][:_MAX_SEARCH_HITS]
    if not hits:
        raise ToolExecutionError(
            "STUDY_SOURCE_NOT_FOUND",
            f"Study source has no available sections: {source_id}",
            recoverable=True,
        )
    reference = _save_observation(
        context,
        kind="study_source",
        content={
            "source_ref": source_ref.model_dump(mode="json"),
            "sections": hits,
        },
        producer="publication-open_study_source",
        summary=f"{len(hits)} bounded sections from {source_id}",
        study_id=study.study_id,
    )
    return ToolResult(
        message=(
            f"Stored {len(hits)} bounded sections from study source "
            f"{source_id}."
        ),
        artifacts=(reference,),
    )


def _pubmed_error(error: Exception) -> ToolExecutionError:
    if isinstance(error, PubMedConfigurationError):
        return ToolExecutionError(
            "PUBMED_CONFIGURATION_REQUIRED",
            str(error),
            recoverable=False,
        )
    return ToolExecutionError("PUBMED_REQUEST_FAILED", str(error), recoverable=True)


def _search_pubmed(client: PubMedClient, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        hits = client.search(
            query=arguments["query"],
            limit=int(arguments["limit"]),
            published_after=arguments.get("published_after"),
            published_before=arguments.get("published_before"),
        )
    except (PubMedConfigurationError, PubMedRequestError) as error:
        raise _pubmed_error(error) from error
    content = {
        "query": arguments["query"],
        "published_after": arguments.get("published_after"),
        "published_before": arguments.get("published_before"),
        "hits": hits,
    }
    reference = _save_observation(
        context,
        kind="pubmed_search",
        content=content,
        producer="publication-search_pubmed",
        summary=f"{len(hits)} PubMed literature hits",
    )
    return ToolResult(
        message=json.dumps(content, sort_keys=True),
        artifacts=(reference,),
    )


def _open_pubmed_article(
    client: PubMedClient,
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    pmid = arguments["pmid"]
    try:
        article = client.open_article(pmid)
    except (PubMedConfigurationError, PubMedRequestError) as error:
        raise _pubmed_error(error) from error
    reference = _save_observation(
        context,
        kind="pubmed_article",
        content=article,
        producer="publication-open_pubmed_article",
        summary=f"PubMed article {pmid}",
    )
    return ToolResult(
        message=json.dumps(article, sort_keys=True),
        artifacts=(reference,),
    )


@dataclass(frozen=True)
class _FunctionTool:
    spec: ToolSpec
    handler: Callable[[dict[str, Any], ToolContext], ToolResult]

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        return self.handler(arguments, context)


def build_publication_tool_registry(
    *,
    pubmed_client: PubMedClient | None = None,
    include_pubmed: bool | None = None,
) -> ToolRegistry:
    if include_pubmed is None:
        include_pubmed = pubmed_client is not None or is_pubmed_configured()
    tools: list[_FunctionTool] = [
        _FunctionTool(
                spec=ToolSpec(
                    name="publication-search_study_evidence",
                    description=(
                        "Search manually verified publication-design evidence "
                        "within one exact installed study and store bounded cited hits."
                    ),
                    args_model=SearchStudyEvidenceArguments,
                ),
                handler=_search,
        ),
        _FunctionTool(
                spec=ToolSpec(
                    name="publication-open_study_source",
                    description=(
                        "Open bounded sections from one exact cited "
                        "study-scoped publication source reference."
                    ),
                    args_model=OpenStudySourceArguments,
                ),
                handler=_open_source,
        ),
    ]
    if include_pubmed:
        client = pubmed_client or PubMedClient()
        tools.extend(
            [
            _FunctionTool(
                spec=ToolSpec(
                    name="publication-search_pubmed",
                    description=(
                        "Search live PubMed biomedical literature and store bounded "
                        "citation metadata. Use publication-open_pubmed_article for "
                        "an exact article abstract."
                    ),
                    args_model=SearchPubMedArguments,
                ),
                handler=lambda arguments, context: _search_pubmed(
                    client, arguments, context
                ),
            ),
            _FunctionTool(
                spec=ToolSpec(
                    name="publication-open_pubmed_article",
                    description=(
                        "Open one exact PubMed article by PMID and store its bounded "
                        "abstract and citation metadata."
                    ),
                    args_model=OpenPubMedArticleArguments,
                ),
                handler=lambda arguments, context: _open_pubmed_article(
                    client, arguments, context
                ),
            ),
            ]
        )
    return ToolRegistry(tools)


__all__ = [
    "OpenStudySourceArguments",
    "OpenPubMedArticleArguments",
    "SearchPubMedArguments",
    "SearchStudyEvidenceArguments",
    "StudySourceRef",
    "build_publication_tool_registry",
]
