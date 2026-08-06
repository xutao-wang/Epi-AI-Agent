from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


IndexingPurpose = Literal[
    "study_design_reference",
    "cohort_description",
    "variable_reference",
    "operational_definition_reference",
    "analysis_pattern_reference",
    "longitudinal_design_reference",
    "biospecimen_reference",
    "omics_reference",
    "imaging_reference",
    "prediction_model_reference",
    "external_validation_reference",
    "methods_reference",
]
DataSourceType = Literal[
    "prospective_cohort",
    "retrospective_cohort",
    "clinical_trial",
    "registry",
    "electronic_health_record",
    "survey",
    "household_contact_study",
    "case_control_dataset",
    "nested_case_control_dataset",
    "biospecimen_repository",
    "omics_dataset",
    "imaging_dataset",
    "external_population_dataset",
    "multi_source_linked_dataset",
    "other",
]
ParentStudyRelationship = Literal[
    "full_parent_cohort",
    "subset_of_parent_cohort",
    "site_specific_subset",
    "nested_substudy",
    "biospecimen_linked_subset",
    "follow_up_subset",
    "case_control_sample_from_cohort",
    "external_validation_sample",
    "secondary_analysis",
    "unclear",
]
DesignType = Literal[
    "cross_sectional",
    "baseline_cross_sectional",
    "prospective_cohort",
    "retrospective_cohort",
    "longitudinal_repeated_measures",
    "nested_case_control",
    "case_control",
    "diagnostic_accuracy",
    "prognostic_modeling",
    "survival_analysis",
    "trajectory_analysis",
    "biomarker_discovery",
    "external_validation",
    "multi_omics_analysis",
    "imaging_analysis",
    "mixed_methods",
    "other",
]
UnitOfAnalysis = Literal[
    "participant",
    "visit",
    "participant_visit",
    "household",
    "household_contact",
    "specimen",
    "participant_specimen",
    "participant_timepoint",
    "site",
    "image",
    "lesion",
    "gene",
    "protein",
    "metabolite",
    "cell",
    "cell_cluster",
    "other",
]
TimeOrientation = Literal[
    "baseline_only",
    "cross_sectional",
    "longitudinal",
    "time_to_event",
    "pre_post",
    "trajectory",
    "nested_within_follow_up",
    "not_applicable",
    "unclear",
]
AvailabilityClaim = Literal[
    "reported_as_collected_in_parent_study",
    "reported_as_collected_in_this_cohort_component",
    "reported_as_collected_in_this_publication",
    "reported_as_analyzed_in_this_publication",
    "derived_for_this_publication",
    "mentioned_but_not_fully_described",
    "availability_in_current_database_unknown",
]
DefinitionRole = Literal[
    "paper_specific_operationalization",
    "protocol_definition",
    "validated_instrument_based",
    "derived_variable",
    "outcome_definition",
    "exposure_definition",
    "subgroup_definition",
    "eligibility_definition",
    "unclear",
]
AnalysisPatternName = Literal[
    "baseline_cohort_characterization",
    "group_comparison",
    "sex_stratified_analysis",
    "site_stratified_analysis",
    "risk_factor_analysis",
    "external_population_comparison",
    "population_attributable_fraction",
    "longitudinal_change_analysis",
    "repeated_measures_modeling",
    "treatment_response_classification",
    "time_to_event_analysis",
    "survival_analysis",
    "relapse_analysis",
    "mortality_analysis",
    "diagnostic_accuracy_analysis",
    "prognostic_modeling",
    "feature_selection",
    "biomarker_discovery",
    "differential_expression",
    "pathway_analysis",
    "clustering",
    "trajectory_analysis",
    "multi_omics_integration",
    "imaging_feature_analysis",
    "external_validation",
    "sensitivity_analysis",
    "missing_data_analysis",
    "other",
]
DatabaseRelevance = Literal[
    "directly_demonstrated",
    "reported_as_collected",
    "derivable_if_variables_exist",
    "requires_longitudinal_linkage",
    "requires_household_linkage",
    "requires_biospecimen_linkage",
    "requires_omics_linkage",
    "requires_imaging_linkage",
    "requires_external_data",
    "requires_manual_definition_mapping",
    "suggested_by_design_but_not_demonstrated",
    "not_supported_by_publication",
]
PublicationSupport = Literal[
    "directly_demonstrated",
    "methodologically_demonstrated",
    "scientifically_motivated",
    "suggested_by_design",
    "weakly_supported",
    "not_supported",
]
DatabaseSupport = Literal[
    "not_checked",
    "not_yet_verified",
    "candidate_variables_identified",
    "variable_mapping_completed",
    "cohort_reconstruction_completed",
    "analysis_feasibility_verified",
    "not_feasible",
]
ConstraintType = Literal[
    "database_availability",
    "variable_harmonization",
    "cohort_reconstruction",
    "eligibility_reconstruction",
    "timepoint_reconstruction",
    "missingness",
    "linkage",
    "external_data_dependency",
    "specimen_availability",
    "assay_availability",
    "generalizability",
    "selection_bias",
    "confounding",
    "measurement_difference",
    "outcome_definition",
    "analysis_specific",
    "publication_reporting_gap",
    "other",
]
EvidenceType = Literal[
    "parent_study_description",
    "study_design",
    "analytic_population",
    "eligibility",
    "setting",
    "timepoint",
    "variable_collection",
    "instrument",
    "operational_definition",
    "specimen",
    "assay",
    "analysis_method",
    "external_data_source",
    "limitation",
    "conclusion_context",
    "other",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedPdfPage(StrictModel):
    page_number: int = Field(ge=1)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def require_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("PDF page does not contain searchable text.")
        return normalized


class ExtractedPdf(StrictModel):
    source_filename: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)
    extracted_text_sha256: str = Field(min_length=64, max_length=64)
    extractor: Literal["pypdf"] = "pypdf"
    extractor_version: str = Field(min_length=1)
    pages: list[ExtractedPdfPage] = Field(min_length=1)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page_labeled_text(self) -> str:
        return "\n\n".join(
            f"--- PDF page {page.page_number} ---\n{page.text}"
            for page in self.pages
        )


class PublicationCitation(StrictModel):
    doi: str | None = None
    pmid: str | None = None
    title: str = Field(min_length=1)
    authors: list[str] = Field(default_factory=list)
    journal: str | None = None
    year: int = Field(ge=1800, le=3000)

    @field_validator("doi")
    @classmethod
    def normalize_doi(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().casefold()
        return normalized or None

    @field_validator("pmid", "journal")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


class StudyContext(StrictModel):
    parent_study_id: str | None = None
    parent_study_name: str | None = None
    cohort_or_site: str | None = None
    data_source_type: DataSourceType | None = None
    relationship_to_parent_study: ParentStudyRelationship | None = None
    publication_analytic_subset: str | None = None


class ResearchScope(StrictModel):
    research_question: str | None = None
    objective_summary: list[str] = Field(default_factory=list, max_length=5)
    scientific_domains: list[str] = Field(default_factory=list)
    publication_roles: list[str] = Field(default_factory=list)


class PublicationSetting(StrictModel):
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    sites: list[str] = Field(default_factory=list)
    recruitment_sources: list[str] = Field(default_factory=list)


class EnrollmentPeriod(StrictModel):
    start: str | None = None
    end: str | None = None
    description: str | None = None


class Eligibility(StrictModel):
    inclusion: list[str] = Field(default_factory=list)
    exclusion: list[str] = Field(default_factory=list)


class Timepoint(StrictModel):
    timepoint_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    window: str | None = None
    purpose: str = Field(min_length=1)
    used_in_primary_analysis: bool


class TimeStructure(StrictModel):
    orientation: TimeOrientation | None = None
    timepoints: list[Timepoint] = Field(default_factory=list)
    follow_up_duration: str | None = None


class DesignReference(StrictModel):
    design_types: list[DesignType] = Field(default_factory=list)
    analytic_population: str | None = None
    comparison_population: str | None = None
    unit_of_analysis: UnitOfAnalysis | None = None
    setting: PublicationSetting
    enrollment_period: EnrollmentPeriod
    eligibility: Eligibility
    sampling_strategy: str | None = None
    time_structure: TimeStructure
    analytic_sample_size: int | None = Field(default=None, ge=0)


class SourceLocator(StrictModel):
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    section: str | None = None
    subsection: str | None = None
    table: str | None = None
    figure: str | None = None
    supplement: str | None = None

    @model_validator(mode="after")
    def validate_page_span(self) -> "SourceLocator":
        if self.page_end < self.page_start:
            raise ValueError("source page_end cannot precede page_start")
        return self

    def label(self) -> str:
        if self.page_start == self.page_end:
            return f"PDF page {self.page_start}"
        return f"PDF pages {self.page_start}-{self.page_end}"


class DataDomainRecord(StrictModel):
    domain: str = Field(min_length=1)
    collection_timepoints: list[str] = Field(default_factory=list)
    variables_or_concepts: list[str] = Field(default_factory=list)
    source: str | None = None
    availability_claim: AvailabilityClaim


class OperationalDefinition(StrictModel):
    concept: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    instrument: str | None = None
    definition_role: DefinitionRole
    timepoint: str | None = None
    source_locator: SourceLocator
    definition_warning: str = Field(min_length=1)


class DataReference(StrictModel):
    data_domains: list[DataDomainRecord] = Field(default_factory=list)
    data_elements_demonstrated: list[str] = Field(default_factory=list)
    instruments: list[str] = Field(default_factory=list)
    specimens: list[str] = Field(default_factory=list)
    assays: list[str] = Field(default_factory=list)
    operational_definitions: list[OperationalDefinition] = Field(
        default_factory=list
    )
    external_data_sources: list[str] = Field(default_factory=list)
    required_linkages: list[str] = Field(default_factory=list)


class AnalysisPattern(StrictModel):
    analysis_pattern: AnalysisPatternName
    description: str = Field(min_length=1)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    used_in_publication: bool
    database_relevance: DatabaseRelevance
    reproducibility_claim: Literal[
        "not_assessed",
        "partially_assessed",
        "reproduced",
        "not_reproduced",
    ] = "not_assessed"


class AnalysisReference(StrictModel):
    analysis_patterns: list[AnalysisPattern] = Field(default_factory=list)
    outcome_types: list[str] = Field(default_factory=list)
    exposure_types: list[str] = Field(default_factory=list)
    comparison_types: list[str] = Field(default_factory=list)
    subgroup_dimensions: list[str] = Field(default_factory=list)
    required_linkages: list[str] = Field(default_factory=list)
    external_dependencies: list[str] = Field(default_factory=list)


class DatabaseQuestionTemplate(StrictModel):
    question_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    question_family: str = Field(min_length=1)
    scientific_basis: str = Field(min_length=1)
    publication_support: PublicationSupport
    database_support: DatabaseSupport = "not_yet_verified"
    required_elements: list[str] = Field(default_factory=list)
    required_linkages: list[str] = Field(default_factory=list)
    external_dependencies: list[str] = Field(default_factory=list)
    important_cautions: list[str] = Field(default_factory=list)


class CapabilityAssessment(StrictModel):
    capability_id: str = Field(min_length=1)
    question_family: str = Field(min_length=1)
    publication_evidence: Literal[
        "publication_demonstrated",
        "directly_demonstrated",
        "methodologically_demonstrated",
        "scientifically_motivated",
        "suggested_by_design",
        "weakly_supported",
        "not_supported",
    ]
    database_evidence: Literal[
        "not_checked",
        "database_elements_identified",
        "analysis_feasibility_verified",
    ] = "not_checked"
    required_data_domains: list[str] = Field(default_factory=list)
    required_timepoints: list[str] = Field(default_factory=list)
    required_linkages: list[str] = Field(default_factory=list)
    external_dependencies: list[str] = Field(default_factory=list)
    notes: str | None = None


class ApplicabilityConstraint(StrictModel):
    constraint_id: str = Field(min_length=1)
    type: ConstraintType
    constraint: str = Field(min_length=1)
    severity: Literal["low", "moderate", "high", "unknown"]


class ReproducibilityStatus(StrictModel):
    variable_mapping_assessed: bool = False
    cohort_reconstruction_assessed: bool = False
    timepoint_mapping_assessed: bool = False
    linkage_assessed: bool = False
    missingness_assessed: bool = False
    numeric_reproduction_assessed: bool = False
    conclusion_replication_assessed: bool = False


class DesignNumbers(StrictModel):
    analytic_sample_size: int | None = Field(default=None, ge=0)
    enrollment_period: str | None = None
    number_of_timepoints: int | None = Field(default=None, ge=0)


class ResultContext(StrictModel):
    priority: Literal["secondary"] = "secondary"
    main_interpretive_message: str = Field(min_length=1)
    design_numbers: DesignNumbers
    numeric_results_indexed: Literal[False] = False
    reason: str = Field(min_length=1)


class PublicationEvidenceItem(StrictModel):
    evidence_id: str = Field(min_length=1)
    evidence_type: EvidenceType
    supports_paths: list[str] = Field(min_length=1)
    normalized_statement: str = Field(min_length=1)
    source_locator: SourceLocator
    extraction_status: Literal["machine_extracted", "manually_extracted"]
    verification_status: Literal[
        "not_manually_verified",
        "manually_verified",
        "conflicting_source_text",
        "uncertain",
    ]


class LongitudinalExtension(StrictModel):
    repeated_measure_domains: list[str] = Field(default_factory=list)
    scheduled_timepoints: list[str] = Field(default_factory=list)
    observed_timepoints: list[str] = Field(default_factory=list)
    visit_windows: list[str] = Field(default_factory=list)
    outcome_timepoints: list[str] = Field(default_factory=list)
    time_origin: str | None = None
    follow_up_end: str | None = None
    attrition_reported: bool | None = None


class SurvivalExtension(StrictModel):
    event: str | None = None
    time_origin: str | None = None
    censoring_definition: str | None = None
    competing_risks: list[str] = Field(default_factory=list)
    time_scale: str | None = None


class PredictionModelExtension(StrictModel):
    prediction_target: str | None = None
    prediction_horizon: str | None = None
    candidate_predictors: list[str] = Field(default_factory=list)
    model_types: list[str] = Field(default_factory=list)
    training_population: str | None = None
    validation_type: str | None = None
    validation_population: str | None = None
    performance_metrics: list[str] = Field(default_factory=list)
    calibration_assessed: bool | None = None
    external_validation: bool | None = None


class DiagnosticAccuracyExtension(StrictModel):
    index_test: str | None = None
    reference_standard: str | None = None
    target_condition: str | None = None
    thresholds: list[str] = Field(default_factory=list)
    accuracy_metrics: list[str] = Field(default_factory=list)
    blinding_reported: bool | None = None


class BiospecimenExtension(StrictModel):
    specimen_types: list[str] = Field(default_factory=list)
    collection_timepoints: list[str] = Field(default_factory=list)
    processing_methods: list[str] = Field(default_factory=list)
    storage_conditions: list[str] = Field(default_factory=list)
    linked_to_clinical_data: bool | None = None
    participant_specimen_linkage_required: bool = True


class OmicsExtension(StrictModel):
    omics_types: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    feature_levels: list[str] = Field(default_factory=list)
    preprocessing_methods: list[str] = Field(default_factory=list)
    normalization_methods: list[str] = Field(default_factory=list)
    batch_adjustment_methods: list[str] = Field(default_factory=list)
    feature_selection_methods: list[str] = Field(default_factory=list)
    data_repository_accessions: list[str] = Field(default_factory=list)


class ImagingExtension(StrictModel):
    modalities: list[str] = Field(default_factory=list)
    anatomic_regions: list[str] = Field(default_factory=list)
    image_timepoints: list[str] = Field(default_factory=list)
    image_labels: list[str] = Field(default_factory=list)
    feature_types: list[str] = Field(default_factory=list)
    reader_information: list[str] = Field(default_factory=list)
    participant_image_linkage_required: bool = True


class HouseholdContactExtension(StrictModel):
    index_case_definition: str | None = None
    contact_definition: str | None = None
    household_definition: str | None = None
    contact_baseline_status: str | None = None
    household_linkage_required: bool = True
    transmission_or_progression_outcomes: list[str] = Field(default_factory=list)


class ExternalComparatorExtension(StrictModel):
    sources: list[str] = Field(default_factory=list)
    population: str | None = None
    years: list[int] = Field(default_factory=list)
    variables_harmonized: list[str] = Field(default_factory=list)
    harmonization_required: bool = True
    stored_in_parent_database: Literal["yes", "no", "unknown"] = "unknown"


class DesignExtensions(StrictModel):
    longitudinal: LongitudinalExtension | None = None
    survival: SurvivalExtension | None = None
    prediction_model: PredictionModelExtension | None = None
    diagnostic_accuracy: DiagnosticAccuracyExtension | None = None
    biospecimen: BiospecimenExtension | None = None
    omics: OmicsExtension | None = None
    imaging: ImagingExtension | None = None
    household_contact: HouseholdContactExtension | None = None
    external_comparator: ExternalComparatorExtension | None = None


class PublicationReviewStatus(StrictModel):
    status: Literal[
        "machine_generated",
        "needs_manual_review",
        "partially_verified",
        "manually_verified",
        "rejected",
    ] = "needs_manual_review"
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    review_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_manual_review_metadata(self) -> "PublicationReviewStatus":
        if self.status == "manually_verified":
            if not str(self.reviewer or "").strip():
                raise ValueError("manually verified indexes require a reviewer")
            if self.reviewed_at is None:
                raise ValueError(
                    "manually verified indexes require a reviewed_at timestamp"
                )
        return self


class PublicationProvenance(StrictModel):
    source_pdf_sha256: str = Field(min_length=64, max_length=64)
    extracted_text_sha256: str = Field(min_length=64, max_length=64)
    extractor: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    model: str | None = None
    prompt_sha256: str = Field(min_length=64, max_length=64)
    page_count: int = Field(ge=1)
    generated_at: datetime | None = None
    schema_version: Literal["1.0.0"] = "1.0.0"


class PublicationDesignIndex(StrictModel):
    publication_id: str = Field(min_length=1)
    citation: PublicationCitation
    indexing_purposes: list[IndexingPurpose] = Field(min_length=1)
    study_context: StudyContext
    research_scope: ResearchScope
    design_reference: DesignReference
    data_reference: DataReference
    analysis_reference: AnalysisReference
    database_question_templates: list[DatabaseQuestionTemplate] = Field(
        min_length=3,
        max_length=10,
    )
    capability_assessment: list[CapabilityAssessment] = Field(
        default_factory=list
    )
    applicability_constraints: list[ApplicabilityConstraint] = Field(
        min_length=1
    )
    reproducibility_status: ReproducibilityStatus
    result_context: ResultContext
    retrieval_summary: str
    evidence_items: list[PublicationEvidenceItem] = Field(default_factory=list)
    design_extensions: DesignExtensions
    review_status: PublicationReviewStatus
    provenance: PublicationProvenance

    @field_validator("retrieval_summary")
    @classmethod
    def validate_retrieval_summary(cls, value: str) -> str:
        normalized = " ".join(value.split())
        word_count = len(normalized.split())
        if word_count < 100 or word_count > 200:
            raise ValueError("retrieval_summary must contain 100-200 words")
        return normalized

    @model_validator(mode="after")
    def validate_index(self) -> "PublicationDesignIndex":
        doi = self.citation.doi
        if doi and self.publication_id != f"doi:{doi}":
            raise ValueError("publication_id must use the normalized citation DOI")
        if not doi and self.citation.pmid:
            expected = f"pmid:{self.citation.pmid}"
            if self.publication_id != expected:
                raise ValueError("publication_id must use the citation PMID")
        if not doi and not self.citation.pmid:
            expected = f"sha256:{self.provenance.source_pdf_sha256}"
            if self.publication_id != expected:
                raise ValueError(
                    "publication_id must use the source PDF hash when no "
                    "formal identifier is available"
                )
        for field_name, values, identifier in (
            (
                "question_id",
                self.database_question_templates,
                lambda value: value.question_id,
            ),
            (
                "capability_id",
                self.capability_assessment,
                lambda value: value.capability_id,
            ),
            (
                "constraint_id",
                self.applicability_constraints,
                lambda value: value.constraint_id,
            ),
            (
                "evidence_id",
                self.evidence_items,
                lambda value: value.evidence_id,
            ),
        ):
            ids = [identifier(value) for value in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{field_name} values must be unique")
        for evidence in self.evidence_items:
            if evidence.source_locator.page_end > self.provenance.page_count:
                raise ValueError(
                    f"Evidence locator {evidence.source_locator.label()} is "
                    "outside the source PDF"
                )
        return self


class GeneratedPublicationDesignContent(StrictModel):
    citation: PublicationCitation
    indexing_purposes: list[IndexingPurpose] = Field(min_length=1)
    study_context: StudyContext
    research_scope: ResearchScope
    design_reference: DesignReference
    data_reference: DataReference
    analysis_reference: AnalysisReference
    database_question_templates: list[DatabaseQuestionTemplate] = Field(
        min_length=3,
        max_length=10,
    )
    capability_assessment: list[CapabilityAssessment] = Field(
        default_factory=list
    )
    applicability_constraints: list[ApplicabilityConstraint] = Field(
        min_length=1
    )
    reproducibility_status: ReproducibilityStatus
    result_context: ResultContext
    retrieval_summary: str
    evidence_items: list[PublicationEvidenceItem] = Field(default_factory=list)
    design_extensions: DesignExtensions

    @field_validator("retrieval_summary")
    @classmethod
    def validate_retrieval_summary(cls, value: str) -> str:
        normalized = " ".join(value.split())
        word_count = len(normalized.split())
        if word_count < 100 or word_count > 200:
            raise ValueError("retrieval_summary must contain 100-200 words")
        return normalized


class PublicationIndexManifestDocument(StrictModel):
    source_filename: str
    source_sha256: str = Field(min_length=64, max_length=64)
    extracted_text_sha256: str = Field(min_length=64, max_length=64)
    publication_id: str
    index_filename: str
    index_sha256: str = Field(min_length=64, max_length=64)
    review_status: Literal[
        "machine_generated",
        "needs_manual_review",
        "partially_verified",
        "manually_verified",
        "rejected",
    ] = "needs_manual_review"


class PublicationIndexIngestionManifest(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    model: str | None
    prompt_sha256: str = Field(min_length=64, max_length=64)
    documents: list[PublicationIndexManifestDocument] = Field(min_length=1)


PUBLICATION_INDEX_SYSTEM_PROMPT = """\
Create a structured publication design index from one searchable epidemiology
paper. Use only information explicitly supported by the supplied PDF pages.

Prioritize who was studied, the parent study, the paper-specific analytic
population, study design, unit of analysis, data domains, collection
timepoints, instruments, operational definitions, reusable analysis patterns,
database question templates, required linkages, and reuse constraints.

Keep the parent study distinct from the analytic population. Distinguish
variables reported as collected from variables analyzed or derived. Preserve
paper-specific thresholds, units, time windows, and logical operators without
treating them as canonical database definitions.

Do not index detailed numerical results such as event rates, percentages,
effect estimates, confidence intervals, p-values, or model coefficients.
Design-relevant counts, eligibility thresholds, treatment durations, visit
windows, and operational definitions may be retained.

Do not claim that the current database contains a reported variable, can
reconstruct the publication cohort, can reproduce numerical results, or will
support the same conclusions. Database support and every reproducibility field
must remain unverified. Set result_context.numeric_results_indexed to false.

Create three to ten reusable database question templates. Include page-level
evidence for every major design, population, variable, timepoint, analysis, and
limitation claim. Use null, empty lists, unclear values, or explicit constraints
when the paper does not report a detail. The retrieval summary must contain
100-200 words and emphasize design reuse rather than findings.
"""


def _publication_id(
    citation: PublicationCitation,
    *,
    source_sha256: str,
) -> str:
    if citation.doi:
        return f"doi:{citation.doi}"
    if citation.pmid:
        return f"pmid:{citation.pmid}"
    return f"sha256:{source_sha256}"


def generate_publication_index(
    extracted_pdf: ExtractedPdf,
    *,
    model: object,
    model_name: str,
) -> PublicationDesignIndex:
    resolved_model_name = str(model_name or "").strip()
    if not resolved_model_name:
        raise ValueError("Publication-index extraction model name is required.")
    structured_model = model.with_structured_output(
        GeneratedPublicationDesignContent,
        method="json_schema",
        strict=True,
    )
    generated = structured_model.invoke(
        [
            SystemMessage(content=PUBLICATION_INDEX_SYSTEM_PROMPT),
            HumanMessage(content=extracted_pdf.page_labeled_text()),
        ]
    )
    if not isinstance(generated, GeneratedPublicationDesignContent):
        generated = GeneratedPublicationDesignContent.model_validate(generated)
    return PublicationDesignIndex(
        publication_id=_publication_id(
            generated.citation,
            source_sha256=extracted_pdf.source_sha256,
        ),
        **generated.model_dump(mode="python"),
        review_status=PublicationReviewStatus(status="needs_manual_review"),
        provenance=PublicationProvenance(
            source_pdf_sha256=extracted_pdf.source_sha256,
            extracted_text_sha256=extracted_pdf.extracted_text_sha256,
            extractor=extracted_pdf.extractor,
            extractor_version=extracted_pdf.extractor_version,
            model=resolved_model_name,
            prompt_sha256=hashlib.sha256(
                PUBLICATION_INDEX_SYSTEM_PROMPT.encode("utf-8")
            ).hexdigest(),
            page_count=extracted_pdf.page_count,
            generated_at=datetime.now(timezone.utc),
        ),
    )


def _json_bytes(value: BaseModel) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_atomic(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def ingest_publication_indexes(
    *,
    input_root: Path,
    output_root: Path,
    model: object,
    model_name: str,
) -> PublicationIndexIngestionManifest:
    sources = sorted(Path(input_root).glob("*.pdf"))
    if not sources:
        raise ValueError(f"No searchable PDFs found in {input_root}.")
    destination = Path(output_root)
    destination.mkdir(parents=True, exist_ok=True)
    documents: list[PublicationIndexManifestDocument] = []
    publication_ids: set[str] = set()
    for source in sources:
        extracted = extract_searchable_pdf(source)
        index = generate_publication_index(
            extracted,
            model=model,
            model_name=model_name,
        )
        if index.publication_id in publication_ids:
            raise ValueError(
                f"Duplicate publication ID: {index.publication_id}"
            )
        publication_ids.add(index.publication_id)
        index_filename = publication_index_filename(index.publication_id)
        index_path = destination / index_filename
        if index_path.exists():
            existing = load_publication_index(index_path)
            if existing.review_status.status == "manually_verified":
                raise ValueError(
                    f"Refusing to overwrite manually verified index: "
                    f"{index.publication_id}"
                )
        index_payload = _json_bytes(index)
        _write_atomic(index_path, index_payload)
        documents.append(
            PublicationIndexManifestDocument(
                source_filename=source.name,
                source_sha256=extracted.source_sha256,
                extracted_text_sha256=extracted.extracted_text_sha256,
                publication_id=index.publication_id,
                index_filename=index_filename,
                index_sha256=hashlib.sha256(index_payload).hexdigest(),
            )
        )
    manifest = PublicationIndexIngestionManifest(
        model=str(model_name).strip(),
        prompt_sha256=hashlib.sha256(
            PUBLICATION_INDEX_SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        documents=documents,
    )
    _write_atomic(
        destination / "ingestion-manifest.json",
        _json_bytes(manifest),
    )
    return manifest


def publication_index_filename(publication_id: str) -> str:
    normalized = re.sub(
        r"[^a-z0-9.]+",
        "_",
        publication_id.casefold(),
    ).strip("_")
    if not normalized:
        raise ValueError("Publication ID cannot produce an empty file name.")
    return f"{normalized}.json"


def load_publication_index(path: Path) -> PublicationDesignIndex:
    return PublicationDesignIndex.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def extract_searchable_pdf(path: Path) -> ExtractedPdf:
    import pypdf

    source = Path(path)
    payload = source.read_bytes()
    reader = pypdf.PdfReader(source)
    if reader.is_encrypted:
        raise ValueError(f"Encrypted PDF is not supported: {source.name}")
    pages: list[ExtractedPdfPage] = []
    for index, page in enumerate(reader.pages, start=1):
        text = str(page.extract_text() or "").strip()
        if not text:
            raise ValueError(
                f"PDF page {index} does not contain searchable text: {source.name}"
            )
        pages.append(ExtractedPdfPage(page_number=index, text=text))
    labeled = "\n\n".join(
        f"--- PDF page {page.page_number} ---\n{page.text}"
        for page in pages
    )
    return ExtractedPdf(
        source_filename=source.name,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        extracted_text_sha256=hashlib.sha256(
            labeled.encode("utf-8")
        ).hexdigest(),
        extractor="pypdf",
        extractor_version=pypdf.__version__,
        pages=pages,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m db_rag.publication_index"
    )
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--model", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from db_rag.config import PROJECT_ROOT
    from db_rag.service.model_routing import build_db_rag_openai_llm
    from utils.env_loader import load_app_environment

    arguments = parse_args(argv)
    load_app_environment(PROJECT_ROOT)
    model = build_db_rag_openai_llm(
        arguments.model,
        api_key=str(os.getenv("OPENAI_API_KEY", "") or ""),
    )
    if model is None:
        raise ValueError("Publication-index extraction model is required.")
    manifest = ingest_publication_indexes(
        input_root=arguments.input_root,
        output_root=arguments.output_root,
        model=model,
        model_name=arguments.model,
    )
    print(
        f"Created {len(manifest.documents)} publication indexes in "
        f"{arguments.output_root}."
    )
    return 0


__all__ = [
    "ExtractedPdf",
    "ExtractedPdfPage",
    "GeneratedPublicationDesignContent",
    "PUBLICATION_INDEX_SYSTEM_PROMPT",
    "PublicationDesignIndex",
    "PublicationEvidenceItem",
    "PublicationIndexIngestionManifest",
    "PublicationIndexManifestDocument",
    "PublicationProvenance",
    "PublicationReviewStatus",
    "SourceLocator",
    "extract_searchable_pdf",
    "generate_publication_index",
    "ingest_publication_indexes",
    "load_publication_index",
    "main",
    "parse_args",
    "publication_index_filename",
]


if __name__ == "__main__":
    raise SystemExit(main())
