from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from db_rag.config import EMBEDDING_MODEL
from db_rag.retrieval_status import RetrievalOutcome, hybrid_status
from db_rag.study_design_documents import StudyDesignKnowledgeUnavailableError

from epi_agent.protocol import (
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    require_context_study,
)
from epi_agent.registry import ToolRegistry
from epi_agent.studies import SearchableStudyDesignProvider


_MAX_HITS = 10
_MAX_EXCERPT_CHARS = 1_200
_MAX_PROVENANCE_CHARS = 512


class SearchStudyDesignArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    study_id: str = Field(min_length=1, max_length=512)
    query: str = Field(min_length=1, max_length=8_000)
    limit: int = Field(default=5, ge=1, le=_MAX_HITS)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _bounded(value: Any, limit: int) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return str(value or "")[:limit]


def _design_hit(value: Any, *, study_id: str) -> dict[str, object]:
    row: dict[str, object] = {
        "study_id": study_id,
        "evidence_id": _bounded(_field(value, "id"), _MAX_PROVENANCE_CHARS),
        "source_kind": _bounded(_field(value, "source_kind"), _MAX_PROVENANCE_CHARS),
        "source_id": _bounded(_field(value, "source_id"), _MAX_PROVENANCE_CHARS),
        "source_path": _bounded(_field(value, "source_path"), _MAX_PROVENANCE_CHARS),
        "source_sha256": _bounded(
            _field(value, "source_sha256"),
            _MAX_PROVENANCE_CHARS,
        ),
        "section": _bounded(_field(value, "section"), _MAX_PROVENANCE_CHARS),
        "excerpt": _bounded(_field(value, "text"), _MAX_EXCERPT_CHARS),
    }
    distance = _field(value, "distance")
    if (
        isinstance(distance, (int, float))
        and not isinstance(distance, bool)
        and math.isfinite(float(distance))
    ):
        row["distance"] = float(distance)
    matched_by = _field(value, "matched_by")
    if isinstance(matched_by, (list, tuple)):
        modes: list[str] = []
        for mode in matched_by:
            if mode in {"vector", "lexical"} and mode not in modes:
                modes.append(mode)
        if modes:
            row["matched_by"] = modes
    return {key: item for key, item in row.items() if item or item == 0.0}


def _search(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    study = require_context_study(context, arguments["study_id"])
    provider = study.study_design
    if not isinstance(provider, SearchableStudyDesignProvider):
        raise ToolExecutionError(
            "STUDY_DESIGN_SEARCH_UNAVAILABLE",
            "The requested study does not provide study-design document search.",
            recoverable=True,
        )
    search_with_status = getattr(provider, "search_with_status", None)
    try:
        outcome = (
            search_with_status(
                str(arguments["query"]),
                limit=int(arguments["limit"]),
            )
            if callable(search_with_status)
            else RetrievalOutcome(
                value=provider.search(
                    str(arguments["query"]),
                    limit=int(arguments["limit"]),
                ),
                status=hybrid_status(
                    getattr(provider, "embedding_model", EMBEDDING_MODEL)
                ),
            )
        )
    except StudyDesignKnowledgeUnavailableError as error:
        raise ToolExecutionError(
            "STUDY_DESIGN_EVIDENCE_INVALID",
            str(error),
            recoverable=True,
        ) from error
    hits = [
        _design_hit(hit, study_id=study.study_id)
        for hit in outcome.value
    ][:_MAX_HITS]
    save_artifact = getattr(context.artifact_store, "save_artifact", None)
    if not callable(save_artifact):
        raise ToolExecutionError(
            "ARTIFACT_STORE_UNAVAILABLE",
            "The study-design artifact store is unavailable.",
            recoverable=False,
        )
    content = {
        "study_id": study.study_id,
        "query": arguments["query"],
        "retrieval_mode": outcome.status.mode,
        "embedding": outcome.status.as_dict(),
        "hits": hits,
    }
    reference = save_artifact(
        kind="study_design_evidence",
        content=content,
        provenance={
            "thread_id": context.thread_id,
            "producer": "study-design-search",
            "study_id": study.study_id,
        },
        summary=f"{len(hits)} study-design hits",
    )
    return ToolResult(
        message=json.dumps(content, sort_keys=True),
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


def build_study_design_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            _FunctionTool(
                spec=ToolSpec(
                    name="study-design-search",
                    description=(
                        "Search one exact installed study's Markdown design "
                        "documents and store bounded provenance-rich hits."
                    ),
                    args_model=SearchStudyDesignArguments,
                ),
                handler=_search,
            )
        ]
    )


__all__ = ["SearchStudyDesignArguments", "build_study_design_tool_registry"]
