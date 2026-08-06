from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_rag.study_design import LocalStudyDesign


def _document() -> dict[str, object]:
    return {
        "$schema": "report-study-design-1.0",
        "schema_version": "1.0",
        "study_id": "report_india",
        "label": "RePORT India",
        "study_purpose": "Prospective observational tuberculosis cohort study.",
        "populations": {
            "index_case": {
                "canonical_label": "Index cases",
                "aliases": ["Cohort A", "active pulmonary TB", "index case"],
                "definition": "Index cases with active pulmonary tuberculosis.",
            },
            "household_contact": {
                "canonical_label": "Household contacts",
                "aliases": ["Cohort B", "household contact", "contact"],
                "definition": "Household contacts linked to Cohort A, including contacts who later progress.",
            },
        },
        "relationships": [
            {
                "type": "household_contact_of",
                "from": "household_contact",
                "to": "index_case",
                "description": "Each Cohort B participant is linked to an index case in Cohort A.",
            }
        ],
        "authoritative_for": [
            "population_interpretation",
            "cohort_selection",
            "schema_evidence_interpretation",
        ],
    }


def _write_design(root: Path, payload: dict[str, object] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "report.study-design.json"
    path.write_text(
        json.dumps(payload if payload is not None else _document()),
        encoding="utf-8",
    )
    return path


def test_local_study_design_loads_and_renders_canonical_cohort_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "study_design"
    _write_design(root)

    design = LocalStudyDesign.from_root(root)
    rendered = design.render_context()

    assert design.study_id == "report_india"
    assert "Cohort A" in rendered
    assert "index cases" in rendered.casefold()
    assert "Cohort B" in rendered
    assert "household contacts" in rendered.casefold()
    assert "including contacts who later progress" in rendered
    assert "authoritative" in rendered.casefold()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload.pop("populations"),
        lambda payload: payload["populations"]["index_case"].update(
            {"definition": ""}
        ),
        lambda payload: payload.update({"unexpected": True}),
    ],
)
def test_local_study_design_rejects_invalid_canonical_documents(
    tmp_path: Path,
    mutator,
) -> None:
    payload = _document()
    mutator(payload)
    _write_design(tmp_path / "study_design", payload)

    with pytest.raises(ValueError, match="Study design path"):
        LocalStudyDesign.from_root(tmp_path / "study_design")


def test_local_study_design_rejects_missing_root_and_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Study design path"):
        LocalStudyDesign.from_root(tmp_path / "missing")

    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(ValueError, match="Study design path"):
        LocalStudyDesign.from_root(root)


def test_local_study_design_preserves_optional_versioned_sections(
    tmp_path: Path,
) -> None:
    payload = _document()
    payload.update(
        {
            "follow_up_timepoints": ["baseline", "month_6"],
            "terminology": {"index_case": "Cohort A"},
            "outcomes": ["incident tuberculosis"],
        }
    )
    design = LocalStudyDesign.from_root(
        _write_design(tmp_path / "study_design", payload).parent
    )

    assert design.document.follow_up_timepoints == ("baseline", "month_6")
    assert design.document.terminology == {"index_case": "Cohort A"}
    assert design.document.outcomes == ("incident tuberculosis",)
    assert "Cohort A" in design.render_context()
    assert "Cohort B" in design.render_context()


def test_local_study_design_rejects_relationships_to_unknown_populations(
    tmp_path: Path,
) -> None:
    payload = _document()
    payload["relationships"][0]["from"] = "unknown_population"
    _write_design(tmp_path / "study_design", payload)

    with pytest.raises(ValueError, match="Study design path"):
        LocalStudyDesign.from_root(tmp_path / "study_design")
