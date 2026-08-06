from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

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


def _design_hit(value: Any) -> dict[str, str]:
    row = {
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
    return {key: item for key, item in row.items() if item}


def _search(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    study = require_context_study(context)
    provider = study.study_design
    if not isinstance(provider, SearchableStudyDesignProvider):
        raise ToolExecutionError(
            "STUDY_DESIGN_SEARCH_UNAVAILABLE",
            "The active study does not provide study-design document search.",
            recoverable=True,
        )
    hits = [
        _design_hit(hit)
        for hit in provider.search(
            str(arguments["query"]),
            limit=int(arguments["limit"]),
        )
    ][:_MAX_HITS]
    save_artifact = getattr(context.artifact_store, "save_artifact", None)
    if not callable(save_artifact):
        raise ToolExecutionError(
            "ARTIFACT_STORE_UNAVAILABLE",
            "The study-design artifact store is unavailable.",
            recoverable=False,
        )
    reference = save_artifact(
        kind="study_design_evidence",
        content={"query": arguments["query"], "hits": hits},
        provenance={
            "thread_id": context.thread_id,
            "producer": "study-design-search",
            "study_id": study.study_id,
        },
        summary=f"{len(hits)} study-design hits",
    )
    return ToolResult(
        message=json.dumps({"hits": hits}, sort_keys=True),
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
                        "Search the active package's Markdown study-design "
                        "documents and store bounded provenance-rich hits."
                    ),
                    args_model=SearchStudyDesignArguments,
                ),
                handler=_search,
            )
        ]
    )


__all__ = ["SearchStudyDesignArguments", "build_study_design_tool_registry"]
