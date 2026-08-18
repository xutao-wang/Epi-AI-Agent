from __future__ import annotations

import pytest

from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext, ToolExecutionError, require_context_study
from epi_agent.studies import StudyBundle, StudyRegistry


def _study(study_id: str) -> StudyBundle:
    return StudyBundle(
        study_id=study_id,
        label=study_id.upper(),
        knowledge=None,
        catalog=None,
        data_sources={},
    )


def _context(studies: StudyRegistry) -> ToolContext:
    return ToolContext(
        studies=studies,
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=object(),
    )


def test_require_context_study_resolves_exact_study_id() -> None:
    context = _context(StudyRegistry([_study("study-a"), _study("study-b")]))

    assert require_context_study(context, "study-b").study_id == "study-b"


def test_require_context_study_never_falls_back_to_sole_study() -> None:
    context = _context(StudyRegistry([_study("study-a")]))

    with pytest.raises(ToolExecutionError) as raised:
        require_context_study(context, "unknown")

    assert raised.value.code == "STUDY_NOT_AVAILABLE"
    assert raised.value.recoverable is True
    assert raised.value.details == {
        "requested_study_id": "unknown",
        "available_study_ids": ["study-a"],
    }


def test_require_context_study_reports_no_installed_packages() -> None:
    context = _context(StudyRegistry())

    with pytest.raises(ToolExecutionError) as raised:
        require_context_study(context, "study-a")

    assert raised.value.code == "NO_STUDY_PACKAGE_INSTALLED"
    assert raised.value.recoverable is True


def test_study_registry_ids_are_deterministic() -> None:
    studies = StudyRegistry([_study("study-z"), _study("study-a")])

    assert studies.ids == ("study-a", "study-z")
