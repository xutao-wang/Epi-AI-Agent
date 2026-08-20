from __future__ import annotations

import json

import pytest

from epi_agent.studies import StudyBundle, StudyRegistry
from epi_agent.tool_packs.studies.context import (
    StudyRoutingContextError,
    render_installed_study_context,
)


class _Overview:
    def __init__(self, text: str) -> None:
        self.text = text

    def render_context(self) -> str:
        return self.text


class _BrokenOverview:
    def render_context(self) -> str:
        raise ValueError(
            "alphacyte overview cannot be decoded " + "x" * 2_000
        )


def _study(
    study_id: str,
    label: str,
    overview: object | None,
) -> StudyBundle:
    return StudyBundle(
        study_id=study_id,
        label=label,
        knowledge=None,
        catalog=None,
        data_sources={},
        study_design=overview,
    )


def _payload(rendered: str) -> dict[str, object]:
    prefix = "<installed_study_routing_context>\n"
    suffix = "\n</installed_study_routing_context>"
    assert rendered.startswith(prefix)
    assert rendered.endswith(suffix)
    return json.loads(rendered[len(prefix) : -len(suffix)])


def test_context_contains_every_complete_overview_in_stable_non_relevance_order() -> None:
    late_marker = "z" * 6_000 + " alphacyte-late-routing-evidence"
    studies = StudyRegistry(
        [
            _study(
                f"study-{index}",
                f"Live label {index}",
                _Overview(f"scope {index}"),
            )
            for index in range(7, 0, -1)
        ]
        + [_study("study-z", "Live label Z", _Overview(late_marker))]
    )

    payload = _payload(render_installed_study_context(studies))

    entries = payload["studies"]
    assert isinstance(entries, list)
    assert payload["study_count"] == 8
    assert [entry["study_id"] for entry in entries] == sorted(
        entry["study_id"] for entry in entries
    )
    assert entries[-1]["overview"] == late_marker
    assert entries[-1]["overview_available"] is True


def test_context_reflects_live_registry_labels_without_fixed_choices() -> None:
    first = _payload(
        render_installed_study_context(
            StudyRegistry(
                [
                    _study(
                        "alpha",
                        "First live label",
                        _Overview("scope a"),
                    )
                ]
            )
        )
    )
    second = _payload(
        render_installed_study_context(
            StudyRegistry(
                [
                    _study(
                        "beta",
                        "Replacement label",
                        _Overview("scope b"),
                    )
                ]
            )
        )
    )

    first_studies = first["studies"]
    second_studies = second["studies"]
    assert isinstance(first_studies, list)
    assert isinstance(second_studies, list)
    assert first_studies[0]["label"] == "First live label"
    assert second_studies[0]["label"] == "Replacement label"
    assert "First live label" not in json.dumps(second)


def test_context_marks_missing_broken_and_empty_overviews_unavailable() -> None:
    payload = _payload(
        render_installed_study_context(
            StudyRegistry(
                [
                    _study("broken", "Broken", _BrokenOverview()),
                    _study("empty", "Empty", _Overview("  ")),
                    _study("missing", "Missing", None),
                ]
            )
        )
    )

    entries = payload["studies"]
    assert isinstance(entries, list)
    by_id = {entry["study_id"]: entry for entry in entries}
    assert all(
        entry["overview_available"] is False for entry in by_id.values()
    )
    assert all("overview" not in entry for entry in by_id.values())
    assert len(by_id["broken"]["error"]) <= 300


def test_context_has_an_explicit_empty_registry_state() -> None:
    assert _payload(render_installed_study_context(StudyRegistry())) == {
        "context_kind": "installed_study_routing_evidence",
        "study_count": 0,
        "studies": [],
    }


def test_context_rejects_an_overview_that_breaks_the_total_ceiling() -> None:
    with pytest.raises(StudyRoutingContextError, match="exceeds"):
        render_installed_study_context(
            StudyRegistry(
                [_study("large", "Large", _Overview("x" * 101))]
            ),
            max_chars=100,
        )
