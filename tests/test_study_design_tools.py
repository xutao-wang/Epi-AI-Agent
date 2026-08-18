from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from db_rag.study_design_documents import StudyDesignHit
from epi_agent.agent import build_general_epi_agent_registry
from epi_agent.protocol import ArtifactRef, ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry
from epi_agent.tool_packs.study_design import (
    STUDY_DESIGN_SYSTEM_PROMPT,
    build_study_design_tool_registry,
)
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import AttachmentReaderService


@dataclass
class _SearchableDesign:
    text: str = "Retrieval-only schedule."

    def render_context(self) -> str:
        return "# Overview\n\nAuthoritative."

    def search(self, query: str, limit: int = 5):
        assert query == "When are visits?"
        assert limit == 3
        return (
            StudyDesignHit(
                source_kind="study_design",
                source_id="study-design-source.fixture",
                source_path="reference/visits.md",
                source_sha256="a" * 64,
                section="Visits",
                text=self.text,
                distance=0.125,
            ),
        )


class _LegacyDesign:
    def render_context(self) -> str:
        return "Legacy context."


class _ArtifactStore:
    def __init__(self) -> None:
        self.saved: list[dict[str, object]] = []

    def save_artifact(self, **kwargs) -> ArtifactRef:
        self.saved.append(kwargs)
        return ArtifactRef(id="design-result", kind=str(kwargs["kind"]), version=1)


class _PythonRuntime:
    def execute(self, _request, _datasets):
        raise AssertionError("registry inspection must not execute Python")


def _study(design) -> StudyBundle:
    return StudyBundle(
        study_id="study-1",
        label="Study One",
        package_version="3.0.0",
        knowledge=None,
        catalog=None,
        data_sources={},
        study_design=design,
    )


def test_design_search_tool_returns_bounded_provenance_artifact() -> None:
    registry = build_study_design_tool_registry()
    store = _ArtifactStore()
    context = ToolContext(
        studies=StudyRegistry([_study(_SearchableDesign(text="x" * 5_000))]),
        artifact_store=store,
        thread_id="thread-1",
        policy=None,
    )

    result = registry.invoke(
        "study-design-search",
        {"study_id": "study-1", "query": "When are visits?", "limit": 3},
        context=context,
    )

    payload = json.loads(result.message)
    assert payload["hits"][0] == {
        "study_id": "study-1",
        "source_kind": "study_design",
        "source_id": "study-design-source.fixture",
        "source_path": "reference/visits.md",
        "source_sha256": "a" * 64,
        "section": "Visits",
        "excerpt": "x" * 1_200,
    }
    assert store.saved[0]["kind"] == "study_design_evidence"
    assert store.saved[0]["content"]["hits"] == payload["hits"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"study_id": "study-1", "query": "", "limit": 3},
        {"study_id": "study-1", "query": "x" * 8_001, "limit": 3},
        {"study_id": "study-1", "query": "When are visits?", "limit": 11},
    ],
)
def test_design_search_tool_bounds_arguments(arguments: dict[str, object]) -> None:
    with pytest.raises(ToolExecutionError, match="Invalid arguments"):
        build_study_design_tool_registry().invoke(
            "study-design-search",
            arguments,
            context=ToolContext(
                studies=StudyRegistry([_study(_SearchableDesign())]),
                artifact_store=_ArtifactStore(),
                thread_id="thread-1",
                policy=None,
            ),
        )


def test_design_search_tool_reports_unavailable_for_legacy_provider() -> None:
    with pytest.raises(ToolExecutionError) as raised:
        build_study_design_tool_registry().invoke(
            "study-design-search",
            {"study_id": "study-1", "query": "When are visits?", "limit": 3},
            context=ToolContext(
                studies=StudyRegistry([_study(_LegacyDesign())]),
                artifact_store=_ArtifactStore(),
                thread_id="thread-1",
                policy=None,
            ),
        )

    assert raised.value.code == "STUDY_DESIGN_SEARCH_UNAVAILABLE"


@pytest.mark.parametrize("design", [None, _LegacyDesign()])
def test_general_registry_omits_design_tool_without_searchable_provider(
    tmp_path: Path,
    design,
) -> None:
    registry = build_general_epi_agent_registry(
        service=AttachmentReaderService(
            LocalAttachmentStore(tmp_path),
            runtime_root=tmp_path,
        ),
        python_runtime=_PythonRuntime(),
        runtime_root=tmp_path,
        studies=StudyRegistry([_study(design)]),
    )

    assert "study-design-search" not in {
        schema["function"]["name"] for schema in registry.model_schemas()
    }


def test_general_registry_includes_design_tool_for_searchable_provider(
    tmp_path: Path,
) -> None:
    registry = build_general_epi_agent_registry(
        service=AttachmentReaderService(
            LocalAttachmentStore(tmp_path),
            runtime_root=tmp_path,
        ),
        python_runtime=_PythonRuntime(),
        runtime_root=tmp_path,
        studies=StudyRegistry([_study(_SearchableDesign())]),
    )

    assert "study-design-search" in {
        schema["function"]["name"] for schema in registry.model_schemas()
    }


def test_study_design_prompt_defines_overview_authority() -> None:
    assert "overview" in STUDY_DESIGN_SYSTEM_PROMPT.casefold()
    assert "conflict" in STUDY_DESIGN_SYSTEM_PROMPT.casefold()
    assert "delegates" in STUDY_DESIGN_SYSTEM_PROMPT.casefold()
    assert "superseding amendment" in STUDY_DESIGN_SYSTEM_PROMPT.casefold()
