from __future__ import annotations

from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel

from epi_agent.artifacts import DatasetPlan, dataset_plan_from_artifact
from epi_agent.protocol import ArtifactRef, ArtifactStore
from utils.dataset_artifacts import load_dataset_artifact


class QualityWarning(BaseModel):
    code: str
    severity: Literal["low", "medium", "high"]
    message: str


class DatasetQualityReport(BaseModel):
    dataset_id: str
    dataset_version: int
    plan_id: str
    plan_version: int
    sql_id: str | None = None
    sql_version: int | None = None
    row_count: int
    column_count: int
    null_rates: dict[str, float]
    duplicate_grain_rows: int | None
    grain_uniqueness: dict[str, Any] | None
    requested_concept_coverage: dict[str, bool]
    unexpected_columns: list[str]
    join_expansion: dict[str, float]
    relationship_metrics: list[dict[str, Any]]
    warnings: list[QualityWarning]


def _field_output_name(field: dict[str, Any]) -> str:
    output_column = str(field.get("output_column") or "").strip()
    return output_column or str(field.get("column") or "").strip()


def _plan_field_output_name(field: Any) -> str:
    output_column = str(field.output_column or "").strip()
    return output_column or str(field.column).strip()


def _concept_fields(concept: Any) -> list[str]:
    content = concept.model_dump(mode="json")
    return [
        _field_output_name(field)
        for field in content.get("fields") or []
        if isinstance(field, dict) and _field_output_name(field)
    ]


def _concept_key(concept: Any, index: int) -> str:
    content = concept.model_dump(mode="json")
    for key in ("concept_id", "id", "label"):
        value = str(content.get(key) or "").strip()
        if value:
            return value
    return f"concept_{index + 1}"


def _planned_columns(plan: DatasetPlan) -> set[str]:
    return {
        *(
            _plan_field_output_name(field)
            for field in plan.required_fields
        ),
        *(
            column
            for concept in plan.concepts
            for column in _concept_fields(concept)
        ),
    }


def _declared_grain_columns(
    provenance: dict[str, Any],
    plan: DatasetPlan,
) -> list[str]:
    declared = [
        _plan_field_output_name(field)
        for field in plan.required_fields
    ] + [
        _plan_field_output_name(field)
        for concept in plan.concepts
        for field in concept.fields
        if "grain" in field.roles
        or field.purpose.strip().casefold() in {"grain", "timing"}
    ]
    if declared:
        return list(dict.fromkeys(column for column in declared if column))
    return [
        str(column).strip()
        for column in provenance.get("grain_columns") or []
        if str(column).strip()
    ]


def _duplicate_grain_rows(
    dataframe: pd.DataFrame,
    provenance: dict[str, Any],
    plan: DatasetPlan,
) -> tuple[int | None, list[str]]:
    declared_columns = _declared_grain_columns(provenance, plan)
    if not declared_columns:
        return None, []
    missing_columns = [
        column for column in declared_columns if column not in dataframe.columns
    ]
    if missing_columns:
        return None, missing_columns
    usable_rows = dataframe.dropna(subset=declared_columns)
    return (
        int(usable_rows.duplicated(subset=declared_columns, keep=False).sum()),
        [],
    )


def _grain_uniqueness(
    dataframe: pd.DataFrame,
    provenance: dict[str, Any],
    plan: DatasetPlan,
) -> dict[str, Any] | None:
    columns = _declared_grain_columns(provenance, plan)
    if not columns or any(column not in dataframe.columns for column in columns):
        return None
    usable_rows = dataframe.dropna(subset=columns)
    row_count = int(len(usable_rows))
    distinct_key_count = int(usable_rows.drop_duplicates(subset=columns).shape[0])
    duplicate_rows = int(
        usable_rows.duplicated(subset=columns, keep=False).sum()
    )
    return {
        "columns": columns,
        "row_count": row_count,
        "distinct_key_count": distinct_key_count,
        "duplicate_rows": duplicate_rows,
        "is_unique": duplicate_rows == 0,
    }


def _persisted_post_sql_warnings(
    provenance: dict[str, Any],
) -> list[QualityWarning]:
    warnings: list[QualityWarning] = []
    raw_warnings = provenance.get("post_sql_warnings")
    if not isinstance(raw_warnings, list):
        return warnings
    for item in raw_warnings:
        if not isinstance(item, dict):
            continue
        try:
            warnings.append(QualityWarning.model_validate(item))
        except Exception:
            continue
    return warnings


def _warnings(
    *,
    row_count: int,
    duplicate_grain_rows: int | None,
    missing_grain_columns: list[str],
    coverage: dict[str, bool],
    unexpected_columns: list[str],
    join_expansion: dict[str, float],
    relationship_metrics: list[dict[str, Any]],
    inherited_warnings: list[QualityWarning] | None = None,
) -> list[QualityWarning]:
    warnings: list[QualityWarning] = list(inherited_warnings or [])
    if row_count == 0:
        warnings.append(
            QualityWarning(
                code="ZERO_ROWS",
                severity="high",
                message="The extracted dataset contains zero rows.",
            )
        )
    if duplicate_grain_rows:
        warnings.append(
            QualityWarning(
                code="DUPLICATE_GRAIN_ROWS",
                severity="high",
                message=(
                    f"{duplicate_grain_rows} rows belong to duplicated target-grain keys."
                ),
            )
        )
    if missing_grain_columns:
        warnings.append(
            QualityWarning(
                code="MISSING_GRAIN_COLUMNS",
                severity="high",
                message=(
                    "Declared target-grain columns are missing from the dataset: "
                    f"{', '.join(missing_grain_columns)}."
                ),
            )
        )
    missing_concepts = [name for name, covered in coverage.items() if not covered]
    if missing_concepts:
        warnings.append(
            QualityWarning(
                code="REQUESTED_CONCEPTS_MISSING",
                severity="high",
                message=f"Requested concepts lack output fields: {', '.join(missing_concepts)}.",
            )
        )
    if unexpected_columns:
        warnings.append(
            QualityWarning(
                code="UNEXPECTED_COLUMNS",
                severity="medium",
                message=f"Unexpected output columns: {', '.join(unexpected_columns)}.",
            )
        )
    if join_expansion.get("target_grain", 0.0) > 1.0:
        warnings.append(
            QualityWarning(
                code="JOIN_EXPANSION",
                severity="high",
                message=(
                    "Observed row expansion exceeds one row per distinct "
                    "target grain key."
                ),
            )
        )
    multiplying_relationships = [
        (
            f"{metric.get('left_table')}->{metric.get('right_table')}"
        )
        for metric in relationship_metrics
        if isinstance(metric.get("joined_rows"), int)
        and isinstance(metric.get("matched_keys"), int)
        and metric["joined_rows"] > metric["matched_keys"]
    ]
    if multiplying_relationships:
        warnings.append(
            QualityWarning(
                code="PROFILED_RELATIONSHIP_RISK",
                severity="high",
                message=(
                    "Profiled relationship risk indicates possible row "
                    "multiplication for: "
                    f"{', '.join(multiplying_relationships)}."
                ),
            )
        )
    deduplicated: list[QualityWarning] = []
    seen: set[tuple[str, str]] = set()
    for warning in warnings:
        key = (warning.code, warning.message)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(warning)
    return deduplicated


def inspect_dataset(
    *,
    artifact_store: ArtifactStore,
    dataset_ref: ArtifactRef,
    plan_ref: ArtifactRef,
) -> ArtifactRef:
    dataset = artifact_store.require(dataset_ref)
    plan_artifact = artifact_store.require(
        ArtifactRef(
            id=plan_ref.id,
            kind="dataset_plan",
            version=plan_ref.version,
        )
    )
    plan = dataset_plan_from_artifact(plan_artifact)
    provenance = dict(dataset.content.get("provenance") or {})
    if (
        str(provenance.get("plan_id") or "") != plan_artifact.id
        or provenance.get("plan_version") != plan_artifact.version
        or provenance.get("study_id") != plan.study_id
    ):
        raise ValueError(
            "STUDY_REFERENCE_MISMATCH: dataset lineage does not match the "
            "requested dataset plan version and study"
        )
    dataframe, _schema = load_dataset_artifact(dataset.content)
    row_count = int(len(dataframe))
    columns = [str(column) for column in dataframe.columns]
    null_rates = {
        column: (
            float(dataframe[column].isna().sum()) / row_count
            if row_count
            else 0.0
        )
        for column in columns
    }
    optional_warnings: list[QualityWarning] = []
    try:
        coverage = {
            _concept_key(concept, index): bool(_concept_fields(concept))
            and all(
                column in dataframe.columns
                for column in _concept_fields(concept)
            )
            for index, concept in enumerate(plan.concepts)
        }
        expected_columns = _planned_columns(plan)
        reconciled_columns = set(
            dict(provenance.get("output_column_sources") or {})
        )
        unexpected_columns = sorted(
            set(columns) - expected_columns - reconciled_columns
        )
        duplicate_grain_rows, missing_grain_columns = _duplicate_grain_rows(
            dataframe,
            provenance,
            plan,
        )
        grain_uniqueness = _grain_uniqueness(dataframe, provenance, plan)
        join_expansion: dict[str, float] = {}
        if grain_uniqueness and grain_uniqueness["distinct_key_count"] > 0:
            join_expansion["target_grain"] = (
                float(grain_uniqueness["row_count"])
                / float(grain_uniqueness["distinct_key_count"])
            )
        relationship_metrics = [
            dict(metric)
            for metric in provenance.get("relationship_metrics") or []
            if isinstance(metric, dict)
        ]
    except (AttributeError, TypeError, ValueError) as error:
        coverage = {}
        unexpected_columns = []
        duplicate_grain_rows = None
        missing_grain_columns = []
        grain_uniqueness = None
        join_expansion = {}
        relationship_metrics = []
        optional_warnings.append(
            QualityWarning(
                code="QUALITY_CHECK_INCOMPLETE",
                severity="medium",
                message=(
                    "Optional dataset quality checks could not be completed: "
                    f"{type(error).__name__}: {error}"
                ),
            )
        )
    inherited_warnings = [
        *_persisted_post_sql_warnings(provenance),
        *optional_warnings,
    ]
    report = DatasetQualityReport(
        dataset_id=dataset.id,
        dataset_version=dataset.version,
        plan_id=plan_artifact.id,
        plan_version=plan_artifact.version,
        sql_id=(
            str(provenance.get("sql_id"))
            if str(provenance.get("sql_id") or "").strip()
            else None
        ),
        sql_version=(
            int(provenance["sql_version"])
            if isinstance(provenance.get("sql_version"), int)
            else None
        ),
        row_count=row_count,
        column_count=len(columns),
        null_rates=null_rates,
        duplicate_grain_rows=duplicate_grain_rows,
        grain_uniqueness=grain_uniqueness,
        requested_concept_coverage=coverage,
        unexpected_columns=unexpected_columns,
        join_expansion=join_expansion,
        relationship_metrics=relationship_metrics,
        warnings=_warnings(
            row_count=row_count,
            duplicate_grain_rows=duplicate_grain_rows,
            missing_grain_columns=missing_grain_columns,
            coverage=coverage,
            unexpected_columns=unexpected_columns,
            join_expansion=join_expansion,
            relationship_metrics=relationship_metrics,
            inherited_warnings=inherited_warnings,
        ),
    )
    return artifact_store.save_artifact(
        kind="dataset_quality_report",
        content=report.model_dump(mode="json"),
        provenance={
            "producer": "dbrag-inspect_dataset",
            "study_id": plan.study_id,
            "dataset": {"id": dataset.id, "version": dataset.version},
            "plan": {"id": plan_artifact.id, "version": plan_artifact.version},
            **(
                {
                    "sql": {
                        "id": str(provenance["sql_id"]),
                        "version": int(provenance["sql_version"]),
                    }
                }
                if str(provenance.get("sql_id") or "").strip()
                and isinstance(provenance.get("sql_version"), int)
                else {}
            ),
        },
        summary=(
            f"Dataset quality report for {dataset.id} version {dataset.version}"
        ),
    )


__all__ = ["DatasetQualityReport", "QualityWarning", "inspect_dataset"]
