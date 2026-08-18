from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from epi_agent.protocol import (
    AgentTool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)
from epi_agent.registry import ToolRegistry


_MAX_STUDIES_PER_PAGE = 5
_MAX_OVERVIEW_CHARS = 1_200
_MAX_ERROR_CHARS = 300


class SearchStudiesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=_MAX_STUDIES_PER_PAGE)


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


def _bounded(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _overview_entry(study: object) -> dict[str, Any]:
    study_id = _bounded(getattr(study, "study_id", ""), 512)
    entry: dict[str, Any] = {
        "study_id": study_id,
        "label": _bounded(getattr(study, "label", ""), 512),
    }
    provider = getattr(study, "study_design", None)
    render_context = getattr(provider, "render_context", None)
    if not callable(render_context):
        return {
            **entry,
            "overview_available": False,
            "error": "This installed study does not provide overview.md content.",
        }
    try:
        overview = _bounded(render_context(), _MAX_OVERVIEW_CHARS)
    except Exception as error:
        return {
            **entry,
            "overview_available": False,
            "error": _bounded(
                f"{type(error).__name__}: {error}",
                _MAX_ERROR_CHARS,
            ),
        }
    if not overview:
        return {
            **entry,
            "overview_available": False,
            "error": "The installed study overview is empty.",
        }
    return {
        **entry,
        "overview": overview,
        "overview_available": True,
    }


def _search_studies(
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    ordered = sorted(
        context.studies.values,
        key=lambda study: study.study_id,
    )
    offset = int(arguments["offset"])
    limit = int(arguments["limit"])
    page = ordered[offset : offset + limit]
    content = {
        "offset": offset,
        "returned_count": len(page),
        "total_count": len(ordered),
        "next_offset": offset + len(page) if offset + len(page) < len(ordered) else None,
        "studies": [_overview_entry(study) for study in page],
    }
    save_artifact = getattr(context.artifact_store, "save_artifact", None)
    if not callable(save_artifact):
        raise ToolExecutionError(
            "ARTIFACT_STORE_UNAVAILABLE",
            "The study-discovery artifact store is unavailable.",
            recoverable=False,
        )
    reference = save_artifact(
        kind="study_directory",
        content=content,
        provenance={
            "thread_id": context.thread_id,
            "producer": "search_studies",
        },
        summary=f"{len(page)} installed study overviews",
    )
    return ToolResult(
        message=json.dumps(content, separators=(",", ":"), sort_keys=True),
        artifacts=(reference,),
    )


def build_study_discovery_tool_registry() -> ToolRegistry:
    tool: AgentTool = _FunctionTool(
        spec=ToolSpec(
            name="search_studies",
            description=(
                "Read a stable bounded page of authoritative overview.md content "
                "for installed studies when the appropriate study is unclear. "
                "This tool does not rank or select a study."
            ),
            args_model=SearchStudiesArguments,
        ),
        handler=_search_studies,
    )
    return ToolRegistry([tool])


__all__ = [
    "SearchStudiesArguments",
    "build_study_discovery_tool_registry",
]
