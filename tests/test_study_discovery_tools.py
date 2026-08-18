from __future__ import annotations

import importlib
import json

import pytest

import epi_agent.agent as agent_module
from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import AttachmentReaderService


class _Overview:
    def __init__(self, text: str) -> None:
        self._text = text

    def render_context(self) -> str:
        return self._text


class _BrokenOverview:
    def render_context(self) -> str:
        raise ValueError("overview could not be decoded " + "x" * 2_000)


def _study(
    study_id: str,
    *,
    overview: object | None,
) -> StudyBundle:
    return StudyBundle(
        study_id=study_id,
        label=f"Label {study_id}",
        knowledge=None,
        catalog=None,
        data_sources={},
        study_design=overview,
    )


def _registry():
    try:
        module = importlib.import_module("epi_agent.tool_packs.studies")
    except ModuleNotFoundError:
        pytest.fail("study discovery tool pack is not implemented")
    return module.build_study_discovery_tool_registry()


def _context(studies: list[StudyBundle]) -> ToolContext:
    return ToolContext(
        studies=StudyRegistry(studies),
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=object(),
    )


def test_search_studies_returns_stable_bounded_pages_without_ranking() -> None:
    context = _context(
        [
            _study("study-z", overview=_Overview("z" * 1_500)),
            _study("study-a", overview=_Overview("alpha overview")),
            _study("study-b", overview=_Overview("beta overview")),
        ]
    )

    result = _registry().invoke(
        "search_studies",
        {"offset": 0, "limit": 2},
        context=context,
    )

    message = json.loads(result.message)
    assert [item["study_id"] for item in message["studies"]] == [
        "study-a",
        "study-b",
    ]
    assert message == {
        "offset": 0,
        "returned_count": 2,
        "total_count": 3,
        "next_offset": 2,
        "studies": message["studies"],
    }
    assert all(item["overview_available"] is True for item in message["studies"])

    final_page = json.loads(
        _registry().invoke(
            "search_studies",
            {"offset": 2, "limit": 2},
            context=context,
        ).message
    )
    assert final_page["next_offset"] is None
    assert len(final_page["studies"][0]["overview"]) == 1_200


def test_search_studies_keeps_missing_and_broken_overviews_per_entry() -> None:
    context = _context(
        [
            _study("broken", overview=_BrokenOverview()),
            _study("missing", overview=None),
            _study("working", overview=_Overview("usable overview")),
        ]
    )

    result = _registry().invoke(
        "search_studies",
        {"offset": 0, "limit": 5},
        context=context,
    )

    message = json.loads(result.message)
    by_id = {item["study_id"]: item for item in message["studies"]}
    assert by_id["working"]["overview"] == "usable overview"
    assert by_id["working"]["overview_available"] is True
    assert by_id["missing"]["overview_available"] is False
    assert "overview" not in by_id["missing"]
    assert by_id["broken"]["overview_available"] is False
    assert len(by_id["broken"]["error"]) <= 300

    observation = context.artifact_store.require(result.artifacts[0])
    assert observation.kind == "study_directory"
    assert observation.provenance == {
        "thread_id": "thread-1",
        "producer": "search_studies",
    }
    assert "active_study_id" not in observation.content


@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": -1, "limit": 1},
        {"offset": 0, "limit": 0},
        {"offset": 0, "limit": 6},
        {"offset": 0, "limit": 1, "query": "NHANES"},
    ],
)
def test_search_studies_rejects_unbounded_or_ranking_arguments(
    arguments: dict[str, object],
) -> None:
    context = _context([_study("study-a", overview=_Overview("overview"))])

    with pytest.raises(ToolExecutionError) as raised:
        _registry().invoke("search_studies", arguments, context=context)

    assert raised.value.code == "INVALID_ARGUMENTS"


def test_agent_context_lists_only_installed_study_ids_and_labels() -> None:
    studies = StudyRegistry(
        [
            _study("study-z", overview=_Overview("private z overview")),
            _study("study-a", overview=_Overview("private a overview")),
        ]
    )
    renderer = getattr(agent_module, "render_installed_study_directory", None)
    assert callable(renderer), "installed-study directory renderer is missing"

    directory = renderer(studies)

    assert directory == (
        "Installed studies:\n"
        "- study_id: study-a; label: Label study-a\n"
        "- study_id: study-z; label: Label study-z"
    )
    assert "private" not in directory

    prompt = agent_module.build_epi_agent_context_prompt(
        {"artifacts": {}},
        installed_study_directory=directory,
    )
    assert directory in prompt


def test_system_prompt_keeps_study_choice_with_the_agent() -> None:
    prompt = agent_module.build_general_system_prompt(
        include_db_rag=True,
        include_study_design=True,
    )

    assert "Choose the study independently for each request" in prompt
    assert "use its exact study_id directly" in prompt
    assert "call search_studies" in prompt
    assert "general-request_clarification" in prompt
    assert "previous study" in prompt


def test_general_registry_always_exposes_study_discovery(tmp_path) -> None:
    registry = agent_module.build_general_epi_agent_registry(
        service=AttachmentReaderService(
            LocalAttachmentStore(tmp_path),
            runtime_root=tmp_path,
        ),
        python_runtime=object(),
        runtime_root=tmp_path,
        studies=StudyRegistry(),
        include_db_rag=False,
    )

    assert "search_studies" in {
        schema["function"]["name"] for schema in registry.model_schemas()
    }
