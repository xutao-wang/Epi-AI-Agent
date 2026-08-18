from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry
from epi_agent.tool_packs.publication import build_publication_tool_registry
from epi_agent.tool_packs.publication.prompt import PUBLICATION_SYSTEM_PROMPT
from epi_agent.tool_packs.study_design import (
    STUDY_DESIGN_SYSTEM_PROMPT,
    build_study_design_tool_registry,
)


class _Knowledge:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.search_queries: list[str] = []
        self.opened_sources: list[str] = []

    def search(self, query: str, limit: int = 5):
        self.search_queries.append(query)
        return (
            SimpleNamespace(
                source_id=f"source-{self.marker}",
                title=f"Title {self.marker}",
                section="Methods",
                text=f"Evidence {self.marker}",
                knowledge_type="publication",
                knowledge_role="primary",
                source_locator="fixture",
                indexed_path=f"{self.marker}.md",
                evidence_ids="evidence-1",
                provenance={"matched_by": "vector,lexical"},
            ),
        )[:limit]

    def open_source(self, source_id: str, limit: int = 5):
        self.opened_sources.append(source_id)
        return self.search(f"open:{source_id}", limit=limit)


class _Design:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.queries: list[str] = []

    def render_context(self) -> str:
        return f"Overview {self.marker}"

    def search(self, query: str, limit: int = 5):
        self.queries.append(query)
        return (
            SimpleNamespace(
                source_kind="study_design",
                source_id=f"design-{self.marker}",
                source_path="overview.md",
                source_sha256="a" * 64,
                section="Overview",
                text=f"Design {self.marker}",
            ),
        )[:limit]


def _study(
    study_id: str,
    *,
    knowledge: object | None = None,
    design: object | None = None,
) -> StudyBundle:
    return StudyBundle(
        study_id=study_id,
        label=study_id,
        knowledge=knowledge,
        catalog=None,
        data_sources={},
        study_design=design,
    )


def _context(studies: list[StudyBundle]) -> ToolContext:
    return ToolContext(
        studies=StudyRegistry(studies),
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=object(),
    )


def test_publication_search_uses_only_the_explicit_study_per_call() -> None:
    first = _Knowledge("first")
    second = _Knowledge("second")
    context = _context(
        [
            _study("study-first", knowledge=first),
            _study("study-second", knowledge=second),
        ]
    )
    registry = build_publication_tool_registry(include_pubmed=False)

    result = registry.invoke(
        "publication-search_study_evidence",
        {"study_id": "study-second", "query": "eligibility", "limit": 5},
        context=context,
    )

    payload = json.loads(result.message)
    assert first.search_queries == []
    assert second.search_queries == ["eligibility"]
    assert payload["study_id"] == "study-second"
    assert payload["hits"][0]["source_ref"] == {
        "study_id": "study-second",
        "source_id": "source-second",
    }
    artifact = context.artifact_store.require(result.artifacts[0])
    assert artifact.provenance["study_id"] == "study-second"


def test_publication_exact_open_consumes_a_study_scoped_source_ref() -> None:
    first = _Knowledge("first")
    second = _Knowledge("second")
    context = _context(
        [
            _study("study-first", knowledge=first),
            _study("study-second", knowledge=second),
        ]
    )

    result = build_publication_tool_registry(include_pubmed=False).invoke(
        "publication-open_study_source",
        {
            "source_ref": {
                "study_id": "study-first",
                "source_id": "source-first",
            }
        },
        context=context,
    )

    assert first.opened_sources == ["source-first"]
    assert second.opened_sources == []
    artifact = context.artifact_store.require(result.artifacts[0])
    assert artifact.content["source_ref"] == {
        "study_id": "study-first",
        "source_id": "source-first",
    }


def test_study_design_search_uses_only_the_explicit_study() -> None:
    first = _Design("first")
    second = _Design("second")
    context = _context(
        [
            _study("study-first", design=first),
            _study("study-second", design=second),
        ]
    )

    result = build_study_design_tool_registry().invoke(
        "study-design-search",
        {"study_id": "study-first", "query": "sampling", "limit": 3},
        context=context,
    )

    payload = json.loads(result.message)
    assert first.queries == ["sampling"]
    assert second.queries == []
    assert payload["study_id"] == "study-first"
    assert payload["hits"][0]["study_id"] == "study-first"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "publication-search_study_evidence",
            {"study_id": "missing", "query": "eligibility", "limit": 5},
        ),
        (
            "publication-open_study_source",
            {"source_ref": {"study_id": "missing", "source_id": "source-1"}},
        ),
    ],
)
def test_publication_tools_never_fall_back_for_an_unknown_study(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    knowledge = _Knowledge("only")
    context = _context([_study("only-study", knowledge=knowledge)])

    with pytest.raises(ToolExecutionError) as raised:
        build_publication_tool_registry(include_pubmed=False).invoke(
            tool_name,
            arguments,
            context=context,
        )

    assert raised.value.code == "STUDY_NOT_AVAILABLE"
    assert knowledge.search_queries == []
    assert knowledge.opened_sources == []


def test_evidence_prompts_require_explicit_study_scope_and_exact_refs() -> None:
    assert "one exact study_id" in PUBLICATION_SYSTEM_PROMPT
    assert "exact source_ref returned by search" in PUBLICATION_SYSTEM_PROMPT
    assert "one exact study_id" in STUDY_DESIGN_SYSTEM_PROMPT
    assert "active study" not in STUDY_DESIGN_SYSTEM_PROMPT.casefold()
    assert "earlier request" in STUDY_DESIGN_SYSTEM_PROMPT
