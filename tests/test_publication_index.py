from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from db_rag.knowledge import parse_study_evidence
from db_rag.publication_index import (
    GeneratedPublicationDesignContent,
    PublicationDesignIndex,
    PublicationIndexIngestionManifest,
    extract_searchable_pdf,
    generate_publication_index,
    ingest_publication_indexes,
    publication_index_filename,
)


_RETRIEVAL_SUMMARY = """\
This publication uses the RePORT India observational cohort to study a
paper-specific subset of adults receiving tuberculosis treatment in southern
India. It demonstrates a participant-level retrospective cohort design using
baseline demographic, socioeconomic, clinical, behavioral, laboratory, and
treatment-outcome data. The analysis pattern evaluates baseline factors in
relation to mortality recorded during treatment and illustrates risk-factor
analysis using linked baseline and outcome information. It can guide questions
about mortality definitions, baseline risk domains, treatment follow-up, and
participant-level linkage requirements. The publication does not establish
that the current database contains every required variable, that its analytic
population can be reconstructed exactly, or that its numerical findings and
conclusions can be reproduced."""


def minimal_publication_index() -> dict:
    questions = [
        {
            "question_id": f"q_{index:03d}",
            "question": question,
            "question_family": family,
            "scientific_basis": (
                "The publication demonstrates a participant-level mortality "
                "risk-factor analysis."
            ),
            "publication_support": "methodologically_demonstrated",
            "database_support": "not_yet_verified",
            "required_elements": [
                "baseline participant characteristics",
                "treatment mortality outcome",
            ],
            "required_linkages": ["participant_to_outcome"],
            "external_dependencies": [],
            "important_cautions": [
                "Current database feasibility has not been assessed."
            ],
        }
        for index, (question, family) in enumerate(
            [
                (
                    "Which baseline factors are associated with mortality "
                    "during tuberculosis treatment?",
                    "treatment_mortality_risk_factors",
                ),
                (
                    "How is mortality during treatment operationally defined?",
                    "treatment_mortality_definition",
                ),
                (
                    "Can baseline and final treatment outcomes be linked at "
                    "the participant level?",
                    "participant_outcome_linkage",
                ),
            ],
            start=1,
        )
    ]
    return {
        "publication_id": "doi:10.1000/example",
        "citation": {
            "doi": "10.1000/example",
            "pmid": None,
            "title": "Example tuberculosis mortality publication",
            "authors": [],
            "journal": "Example Journal",
            "year": 2020,
        },
        "indexing_purposes": [
            "study_design_reference",
            "variable_reference",
            "analysis_pattern_reference",
        ],
        "study_context": {
            "parent_study_id": "report_india",
            "parent_study_name": "RePORT India",
            "cohort_or_site": "Southern India pulmonary tuberculosis cohort",
            "data_source_type": "prospective_cohort",
            "relationship_to_parent_study": "secondary_analysis",
            "publication_analytic_subset": (
                "Paper-specific participants with treatment follow-up."
            ),
        },
        "research_scope": {
            "research_question": (
                "Which baseline factors were associated with mortality during "
                "tuberculosis treatment?"
            ),
            "objective_summary": [
                "Describe mortality during treatment.",
                "Evaluate baseline mortality risk factors.",
            ],
            "scientific_domains": [
                "mortality",
                "risk_factor_analysis",
                "treatment_outcome",
            ],
            "publication_roles": [
                "study_design_reference",
                "analysis_pattern_reference",
            ],
        },
        "design_reference": {
            "design_types": ["retrospective_cohort"],
            "analytic_population": (
                "Adults from the RePORT India pulmonary tuberculosis cohort."
            ),
            "comparison_population": None,
            "unit_of_analysis": "participant",
            "setting": {
                "countries": ["India"],
                "regions": ["Puducherry", "Tamil Nadu"],
                "sites": [],
                "recruitment_sources": [
                    "National Tuberculosis Program clinics"
                ],
            },
            "enrollment_period": {
                "start": None,
                "end": None,
                "description": None,
            },
            "eligibility": {
                "inclusion": ["Adults receiving tuberculosis treatment."],
                "exclusion": [],
            },
            "sampling_strategy": None,
            "time_structure": {
                "orientation": "longitudinal",
                "timepoints": [
                    {
                        "timepoint_id": "baseline",
                        "label": "Baseline",
                        "window": None,
                        "purpose": "Enrollment assessment",
                        "used_in_primary_analysis": True,
                    },
                    {
                        "timepoint_id": "end_of_treatment",
                        "label": "End of treatment",
                        "window": None,
                        "purpose": "Treatment-outcome ascertainment",
                        "used_in_primary_analysis": True,
                    },
                ],
                "follow_up_duration": "During tuberculosis treatment",
            },
            "analytic_sample_size": None,
        },
        "data_reference": {
            "data_domains": [
                {
                    "domain": "demographics",
                    "collection_timepoints": ["baseline"],
                    "variables_or_concepts": ["age", "sex"],
                    "source": "participant assessment",
                    "availability_claim": (
                        "reported_as_analyzed_in_this_publication"
                    ),
                },
                {
                    "domain": "mortality",
                    "collection_timepoints": ["during_treatment"],
                    "variables_or_concepts": ["all-cause mortality"],
                    "source": "treatment outcome follow-up",
                    "availability_claim": (
                        "reported_as_analyzed_in_this_publication"
                    ),
                },
            ],
            "data_elements_demonstrated": [],
            "instruments": [],
            "specimens": [],
            "assays": [],
            "operational_definitions": [
                {
                    "concept": "treatment mortality",
                    "definition": "Death from any cause during treatment.",
                    "instrument": None,
                    "definition_role": "outcome_definition",
                    "timepoint": "during_treatment",
                    "source_locator": {
                        "page_start": 4,
                        "page_end": 4,
                        "section": "Methods",
                        "subsection": "Outcome",
                        "table": None,
                        "figure": None,
                        "supplement": None,
                    },
                    "definition_warning": (
                        "This paper-specific definition is not automatically "
                        "the current database's canonical definition."
                    ),
                }
            ],
            "external_data_sources": [],
            "required_linkages": ["participant_to_treatment_outcome"],
        },
        "analysis_reference": {
            "analysis_patterns": [
                {
                    "analysis_pattern": "mortality_analysis",
                    "description": (
                        "Evaluate baseline factors in relation to mortality "
                        "during tuberculosis treatment."
                    ),
                    "inputs": [
                        "baseline participant characteristics",
                        "treatment mortality outcome",
                    ],
                    "outputs": ["association estimates"],
                    "used_in_publication": True,
                    "database_relevance": "derivable_if_variables_exist",
                    "reproducibility_claim": "not_assessed",
                }
            ],
            "outcome_types": ["binary treatment mortality"],
            "exposure_types": ["baseline participant characteristics"],
            "comparison_types": ["mortality versus no mortality"],
            "subgroup_dimensions": [],
            "required_linkages": ["participant_to_treatment_outcome"],
            "external_dependencies": [],
        },
        "database_question_templates": questions,
        "capability_assessment": [
            {
                "capability_id": "cap_001",
                "question_family": "treatment_mortality_risk_factors",
                "publication_evidence": "directly_demonstrated",
                "database_evidence": "not_checked",
                "required_data_domains": [
                    "demographics",
                    "mortality",
                ],
                "required_timepoints": [
                    "baseline",
                    "during_treatment",
                ],
                "required_linkages": ["participant_to_treatment_outcome"],
                "external_dependencies": [],
                "notes": (
                    "Publication evidence does not establish database "
                    "feasibility."
                ),
            }
        ],
        "applicability_constraints": [
            {
                "constraint_id": "constraint_001",
                "type": "database_availability",
                "constraint": (
                    "The publication does not establish that all required "
                    "variables are present in the current database."
                ),
                "severity": "high",
            },
            {
                "constraint_id": "constraint_002",
                "type": "cohort_reconstruction",
                "constraint": (
                    "The paper-specific analytic population has not been "
                    "reconstructed from the current database."
                ),
                "severity": "high",
            },
        ],
        "reproducibility_status": {
            "variable_mapping_assessed": False,
            "cohort_reconstruction_assessed": False,
            "timepoint_mapping_assessed": False,
            "linkage_assessed": False,
            "missingness_assessed": False,
            "numeric_reproduction_assessed": False,
            "conclusion_replication_assessed": False,
        },
        "result_context": {
            "priority": "secondary",
            "main_interpretive_message": (
                "The publication used a treatment cohort to investigate "
                "baseline mortality risk factors."
            ),
            "design_numbers": {
                "analytic_sample_size": None,
                "enrollment_period": None,
                "number_of_timepoints": 2,
            },
            "numeric_results_indexed": False,
            "reason": (
                "The publication is indexed primarily as a study-design and "
                "variable-reference document."
            ),
        },
        "retrieval_summary": _RETRIEVAL_SUMMARY,
        "evidence_items": [
            {
                "evidence_id": "doi:10.1000/example#methods-outcome-001",
                "evidence_type": "operational_definition",
                "supports_paths": [
                    "data_reference.operational_definitions[0]",
                ],
                "normalized_statement": (
                    "The publication defined the outcome as death from any "
                    "cause during tuberculosis treatment."
                ),
                "source_locator": {
                    "page_start": 4,
                    "page_end": 4,
                    "section": "Methods",
                    "subsection": "Outcome",
                    "table": None,
                    "figure": None,
                    "supplement": None,
                },
                "extraction_status": "machine_extracted",
                "verification_status": "not_manually_verified",
            }
        ],
        "design_extensions": {
            "longitudinal": None,
            "survival": None,
            "prediction_model": None,
            "diagnostic_accuracy": None,
            "biospecimen": None,
            "omics": None,
            "imaging": None,
            "household_contact": None,
            "external_comparator": None,
        },
        "review_status": {
            "status": "needs_manual_review",
            "reviewer": None,
            "reviewed_at": None,
            "review_notes": [],
        },
        "provenance": {
            "source_pdf_sha256": "a" * 64,
            "extracted_text_sha256": "b" * 64,
            "extractor": "pypdf",
            "extractor_version": "test",
            "model": None,
            "prompt_sha256": "c" * 64,
            "page_count": 8,
            "generated_at": None,
            "schema_version": "1.0.0",
        },
    }


def generated_publication_content() -> dict:
    payload = minimal_publication_index()
    for field in (
        "publication_id",
        "review_status",
        "provenance",
    ):
        payload.pop(field)
    return payload


def _write_pdf(path: Path, page_texts: list[str]) -> None:
    page_count = len(page_texts)
    font_id = 3 + (2 * page_count)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids ["
            + b" ".join(
                f"{3 + (2 * index)} 0 R".encode("ascii")
                for index in range(page_count)
            )
            + f"] /Count {page_count} >>".encode("ascii")
        ),
    ]
    for index, text in enumerate(page_texts):
        page_id = 3 + (2 * index)
        content_id = page_id + 1
        escaped = (
            text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        objects.extend(
            [
                (
                    b"<< /Type /Page /Parent 2 0 R "
                    b"/MediaBox [0 0 612 792] "
                    + f"/Contents {content_id} 0 R ".encode("ascii")
                    + b"/Resources << /Font << "
                    + f"/F1 {font_id} 0 R".encode("ascii")
                    + b" >> >> >>"
                ),
                (
                    f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
                    + stream
                    + b"\nendstream"
                ),
            ]
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_id, content in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{object_id} 0 obj\n".encode("ascii"))
        payload.extend(content)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(payload)


class _StructuredPublicationModel:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.schema = None
        self.calls: list[list] = []

    def with_structured_output(self, schema, **kwargs):
        assert kwargs == {"method": "json_schema", "strict": True}
        self.schema = schema
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        return self.schema.model_validate(self.response)


def test_publication_index_defaults_database_feasibility_to_unverified() -> None:
    index = PublicationDesignIndex.model_validate(minimal_publication_index())

    assert index.reproducibility_status.numeric_reproduction_assessed is False
    assert index.result_context.numeric_results_indexed is False
    assert index.review_status.status == "needs_manual_review"


def test_publication_index_rejects_invalid_controlled_vocabulary() -> None:
    payload = minimal_publication_index()
    payload["design_reference"]["design_types"] = ["made_up_design"]

    with pytest.raises(ValidationError):
        PublicationDesignIndex.model_validate(payload)


def test_retrieval_summary_requires_100_to_200_words() -> None:
    payload = minimal_publication_index()
    payload["retrieval_summary"] = "Too short."

    with pytest.raises(ValidationError, match="100-200 words"):
        PublicationDesignIndex.model_validate(payload)


def test_evidence_locator_cannot_exceed_provenance_page_count() -> None:
    payload = minimal_publication_index()
    payload["evidence_items"][0]["source_locator"]["page_start"] = 99
    payload["evidence_items"][0]["source_locator"]["page_end"] = 99

    with pytest.raises(ValidationError, match="outside"):
        PublicationDesignIndex.model_validate(payload)


def test_manually_verified_index_requires_reviewer_and_timestamp() -> None:
    payload = minimal_publication_index()
    payload["review_status"]["status"] = "manually_verified"

    with pytest.raises(ValidationError, match="reviewer"):
        PublicationDesignIndex.model_validate(payload)


def test_publication_index_filename_uses_stable_identifier() -> None:
    assert (
        publication_index_filename("doi:10.1371/journal.pone.0183195")
        == "doi_10.1371_journal.pone.0183195.json"
    )


def test_publication_index_rejects_duplicate_record_ids() -> None:
    payload = minimal_publication_index()
    payload["database_question_templates"].append(
        deepcopy(payload["database_question_templates"][0])
    )

    with pytest.raises(ValidationError, match="question_id"):
        PublicationDesignIndex.model_validate(payload)


def test_generation_uses_one_structured_call_and_records_selected_model(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "study.pdf"
    _write_pdf(
        pdf_path,
        [
            "RePORT India parent cohort and analytic population.",
            "Mortality methods and analysis.",
            "Eligibility and baseline variables.",
            "Outcome definition.",
            "Analysis limitations.",
            "Applicability.",
            "Discussion.",
            "References.",
        ],
    )
    extracted = extract_searchable_pdf(pdf_path)
    model = _StructuredPublicationModel(generated_publication_content())

    index = generate_publication_index(
        extracted,
        model=model,
        model_name="gpt-5.4",
    )

    assert len(model.calls) == 1
    assert index.publication_id == "doi:10.1000/example"
    assert index.provenance.model == "gpt-5.4"
    assert index.provenance.source_pdf_sha256 == extracted.source_sha256
    assert index.review_status.status == "needs_manual_review"
    assert index.result_context.numeric_results_indexed is False


def test_generation_prompt_prioritizes_design_over_results(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "study.pdf"
    _write_pdf(pdf_path, ["Searchable paper text."] * 8)
    model = _StructuredPublicationModel(generated_publication_content())

    generate_publication_index(
        extract_searchable_pdf(pdf_path),
        model=model,
        model_name="gpt-5.4",
    )

    system_prompt = model.calls[0][0].content
    assert "parent study" in system_prompt.casefold()
    assert "analytic population" in system_prompt.casefold()
    assert "do not index detailed numerical results" in system_prompt.casefold()
    assert "--- PDF page 1 ---" in model.calls[0][1].content


def test_ingestion_refuses_to_overwrite_manually_verified_index(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "pdfs"
    output_root = tmp_path / "indexes"
    input_root.mkdir()
    output_root.mkdir()
    _write_pdf(input_root / "study.pdf", ["Searchable paper text."] * 8)
    verified_payload = minimal_publication_index()
    verified_payload["review_status"] = {
        "status": "manually_verified",
        "reviewer": "reviewer@example.org",
        "reviewed_at": "2026-07-26T12:00:00Z",
        "review_notes": [],
    }
    output_path = output_root / publication_index_filename(
        verified_payload["publication_id"]
    )
    output_path.write_text(
        json.dumps(verified_payload),
        encoding="utf-8",
    )
    model = _StructuredPublicationModel(generated_publication_content())

    with pytest.raises(ValueError, match="manually verified"):
        ingest_publication_indexes(
            input_root=input_root,
            output_root=output_root,
            model=model,
            model_name="gpt-5.4",
        )


def test_ingestion_writes_draft_index_and_manifest(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "pdfs"
    output_root = tmp_path / "indexes"
    input_root.mkdir()
    _write_pdf(input_root / "study.pdf", ["Searchable paper text."] * 8)
    model = _StructuredPublicationModel(generated_publication_content())

    manifest = ingest_publication_indexes(
        input_root=input_root,
        output_root=output_root,
        model=model,
        model_name="gpt-5.4",
    )

    assert len(manifest.documents) == 1
    assert manifest.model == "gpt-5.4"
    assert manifest.documents[0].review_status == "needs_manual_review"
    assert (
        output_root / "doi_10.1000_example.json"
    ).is_file()
    assert (output_root / "ingestion-manifest.json").is_file()


def test_report_publication_indexes_are_valid_approved_design_references(
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    pdf_root = project_root / "publication" / "original_paper"
    index_root = project_root / "publication" / "publication_indexes"
    if not pdf_root.exists():
        pytest.skip("Local publication corpus is unavailable.")
    expected = {
        "doi_10.1371_journal.pone.0183195.json": "pone.0183195.pdf",
        "doi_10.1016_j.ijtb.2020.09.022.json": "risk_factor.pdf",
        "doi_10.1186_s12879-017-2629-9.json": "s12879-017-2629-9.pdf",
        "doi_10.4269_ajtmh.19-0415.json": "tpmd190415.pdf",
    }

    assert {
        path.name
        for path in index_root.glob("doi_*.json")
    } == set(expected)
    indexes = [
        PublicationDesignIndex.model_validate_json(
            (index_root / filename).read_text(encoding="utf-8")
        )
        for filename in expected
    ]

    assert all(
        index.review_status.status == "manually_verified"
        for index in indexes
    )
    assert all(index.review_status.reviewer == "xutaowang" for index in indexes)
    assert all(index.review_status.reviewed_at is not None for index in indexes)
    assert all(
        evidence.verification_status == "manually_verified"
        for index in indexes
        for evidence in index.evidence_items
    )
    assert all(index.provenance.model is None for index in indexes)
    assert all(
        index.result_context.numeric_results_indexed is False
        for index in indexes
    )
    assert all(
        3 <= len(index.database_question_templates) <= 10
        for index in indexes
    )
    assert all(
        100 <= len(index.retrieval_summary.split()) <= 200
        for index in indexes
    )
    for filename, pdf_filename in expected.items():
        index = PublicationDesignIndex.model_validate_json(
            (index_root / filename).read_text(encoding="utf-8")
        )
        assert index.provenance.source_pdf_sha256 == hashlib.sha256(
            (pdf_root / pdf_filename).read_bytes()
        ).hexdigest()

    manifest = PublicationIndexIngestionManifest.model_validate_json(
        (index_root / "ingestion-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.model is None
    assert {document.index_filename for document in manifest.documents} == set(
        expected
    )
    for document in manifest.documents:
        assert document.review_status == "manually_verified"
        assert document.index_sha256 == hashlib.sha256(
            (index_root / document.index_filename).read_bytes()
        ).hexdigest()


def test_only_manually_verified_publication_indexes_produce_chunks(
    tmp_path: Path,
) -> None:
    draft = minimal_publication_index()
    approved = minimal_publication_index()
    approved["publication_id"] = "doi:10.1000/approved"
    approved["citation"]["doi"] = "10.1000/approved"
    approved["review_status"] = {
        "status": "manually_verified",
        "reviewer": "reviewer@example.org",
        "reviewed_at": "2026-07-26T12:00:00Z",
        "review_notes": [],
    }
    (tmp_path / "doi_10.1000_draft.json").write_text(
        json.dumps(draft),
        encoding="utf-8",
    )
    (tmp_path / "doi_10.1000_approved.json").write_text(
        json.dumps(approved),
        encoding="utf-8",
    )

    chunks = parse_study_evidence(tmp_path)

    assert chunks
    assert {chunk.source_id for chunk in chunks} == {
        "doi:10.1000/approved"
    }
    assert "retrieval_summary" in {
        chunk.knowledge_type for chunk in chunks
    }
    assert "analysis_pattern" in {
        chunk.knowledge_type for chunk in chunks
    }
    assert "database_question_template" in {
        chunk.knowledge_type for chunk in chunks
    }
    assert all(
        chunk.knowledge_role == "historical_study_design_reference"
        for chunk in chunks
    )
    assert all(chunk.knowledge_type != "evidence_item" for chunk in chunks)


def test_markdown_and_legacy_cards_are_ignored(
    tmp_path: Path,
) -> None:
    (tmp_path / "legacy.md").write_text(
        "Published RR 2.27 (95% CI 1.24-4.15).",
        encoding="utf-8",
    )
    (tmp_path / "legacy.study-reference-card.json").write_text(
        '{"schema_version":"1.0","status":"approved"}',
        encoding="utf-8",
    )

    assert parse_study_evidence(tmp_path) == []
