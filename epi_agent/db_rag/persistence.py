from __future__ import annotations

from typing import Any
from uuid import uuid4

from db_rag.catalog import SchemaCatalog
from db_rag.service.dataset_naming import generate_dataset_name
from graph.state import MetaKeys
from utils.dataset_artifacts import (
    StagedDatasetArtifact,
    persist_dataset_artifact,
    register_dataset_artifact,
    stage_dataset_artifact,
)


_COLUMN_METADATA_FIELDS = (
    "semantic",
    "reason",
    "values",
    "depends_on",
    "condition",
    "section_context",
    "column_profile",
)


class DatasetSchemaMetadataError(ValueError):
    def __init__(self, output_column: str, reason: str) -> None:
        super().__init__(
            "Dataset schema metadata is unavailable for output column "
            f"{output_column!r}: {reason}."
        )
        self.output_column = output_column
        self.reason = reason


def _read_value(payload: Any, field: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(field, default)
    return getattr(payload, field, default)


def _string_list(values: Any) -> list[str]:
    result: list[str] = []
    for value in list(values or []):
        text = str(value or "").strip()
        if text:
            result.append(text)
    return result


def _normalize_population_scope(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, dict) else {}
    population = str(raw.get("population") or "").strip().lower() or None
    if population not in {
        "index_case",
        "household_contact",
        "unclear",
        "not_applicable",
    }:
        population = "not_applicable"
    try:
        confidence = float(raw.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "population": population,
        "warning": str(raw.get("warning") or "").strip(),
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(raw.get("reason") or "").strip(),
    }


def serialize_columns(columns: Any) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for column in list(columns or []):
        source = str(_read_value(column, "source", "") or "").strip()
        table = str(_read_value(column, "table", "") or "").strip()
        column_name = str(_read_value(column, "column", "") or "").strip()
        output_column = str(
            _read_value(column, "output_column", "") or ""
        ).strip()
        purpose = str(_read_value(column, "purpose", "") or "").strip()
        description = str(
            _read_value(column, "description", "") or ""
        ).strip()
        if not table or not column_name:
            continue
        entry: dict[str, Any] = {"table": table, "column": column_name}
        if source:
            entry["source"] = source
        if output_column:
            entry["output_column"] = output_column
        if purpose:
            entry["purpose"] = purpose
        if description:
            entry["description"] = description
        for field in _COLUMN_METADATA_FIELDS:
            value = _read_value(column, field, None)
            if value is not None and value != "":
                entry[field] = value
        serialized.append(entry)
    return serialized


def _sql_candidate_review_texts(
    candidate: Any,
    approved_selection: dict[str, Any],
) -> tuple[str, str]:
    candidate_source = str(
        _read_value(candidate, "source_question", "") or ""
    ).strip()
    candidate_goal = str(
        _read_value(candidate, "goal_text", "") or ""
    ).strip()
    candidate_question = str(
        _read_value(candidate, "question", "") or ""
    ).strip()
    selection_source = str(
        approved_selection.get("source_question") or ""
    ).strip()
    selection_goal = str(
        approved_selection.get("goal_text") or ""
    ).strip()
    source_question = (
        candidate_source or selection_source or candidate_question
    )
    goal_text = (
        candidate_goal
        or selection_goal
        or candidate_question
        or source_question
    )
    return source_question, goal_text


def _projection_bindings(
    sql: str,
    selected_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        import sqlglot
        from sqlglot import exp

        expression = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception:
        return []
    select = (
        expression
        if isinstance(expression, exp.Select)
        else expression.find(exp.Select)
    )
    if select is None:
        return []

    table_names: dict[str, str] = {}
    for table in expression.find_all(exp.Table):
        physical = str(table.name or "").strip()
        alias = str(table.alias_or_name or "").strip()
        if physical:
            table_names[physical.casefold()] = physical
        if alias and physical:
            table_names[alias.casefold()] = physical

    approved = [dict(column) for column in selected_columns]
    bindings: list[dict[str, Any]] = []
    for position, projection in enumerate(select.expressions or []):
        source_fields: list[dict[str, str]] = []
        for source_column in projection.find_all(exp.Column):
            source_name = str(source_column.name or "").strip()
            raw_table = str(source_column.table or "").strip()
            source_table = table_names.get(raw_table.casefold(), raw_table)
            matches = [
                column
                for column in approved
                if str(column.get("column") or "").strip().casefold()
                == source_name.casefold()
                and (
                    not source_table
                    or str(column.get("table") or "").strip().casefold()
                    == source_table.casefold()
                )
            ]
            if len(matches) == 1:
                matched = matches[0]
                source_fields.append(
                    {
                        key: str(matched.get(key) or "").strip()
                        for key in ("source", "table", "column")
                        if str(matched.get(key) or "").strip()
                    }
                )
        bindings.append(
            {
                "position": position,
                "sql_alias": str(projection.alias_or_name or "").strip(),
                "source_fields": source_fields,
            }
        )
    return bindings


def _reconcile_output_columns(
    dataframe: Any,
    selected_columns: list[dict[str, Any]],
    *,
    schema_catalog: SchemaCatalog,
    sql: str,
) -> dict[str, Any]:
    physical = [str(column) for column in dataframe.columns]
    bindings = _projection_bindings(sql, selected_columns)
    sources: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, str]] = []
    for position, physical_name in enumerate(physical):
        binding = (
            dict(bindings[position])
            if position < len(bindings)
            else {
                "position": position,
                "sql_alias": physical_name,
                "source_fields": [],
            }
        )
        sql_alias = str(binding.get("sql_alias") or "").strip()
        source_fields = [
            dict(field)
            for field in list(binding.get("source_fields") or [])
            if isinstance(field, dict)
        ]
        sources[physical_name] = {
            "position": position,
            "sql_alias": sql_alias or physical_name,
            "source_fields": source_fields,
        }
        if sql_alias and sql_alias != physical_name:
            warnings.append(
                {
                    "code": "OUTPUT_ALIAS_DISAMBIGUATED",
                    "severity": "medium",
                    "message": (
                        f"SQL alias {sql_alias!r} was stored as physical output "
                        f"column {physical_name!r} at position {position}."
                    ),
                }
            )
        if not source_fields:
            warnings.append(
                {
                    "code": "OUTPUT_METADATA_UNRESOLVED",
                    "severity": "medium",
                    "message": (
                        f"Source metadata could not be resolved for physical output "
                        f"column {physical_name!r} at position {position}."
                    ),
                }
            )
    return {
        "schema": _build_schema_from_sources(
            dataframe,
            selected_columns,
            sources,
            schema_catalog=schema_catalog,
        ),
        "output_column_sources": sources,
        "warnings": warnings,
    }


def _build_schema_from_sources(
    dataframe: Any,
    selected_columns: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    *,
    schema_catalog: SchemaCatalog,
) -> dict[str, dict[str, Any]]:
    selected_by_identity = {
        (
            str(column.get("source") or "").strip(),
            str(column.get("table") or "").strip(),
            str(column.get("column") or "").strip(),
        ): dict(column)
        for column in selected_columns
    }
    schema: dict[str, dict[str, Any]] = {}
    dtypes = getattr(dataframe, "dtypes", None)
    for column_name in list(getattr(dataframe, "columns", [])):
        column_key = str(column_name)
        binding = dict(sources.get(column_key) or {})
        source_fields = [
            dict(field)
            for field in list(binding.get("source_fields") or [])
            if isinstance(field, dict)
        ]
        if len(source_fields) != 1:
            raise DatasetSchemaMetadataError(
                column_key,
                "expected exactly one source field",
            )
        identity = tuple(
            str(source_fields[0].get(key) or "").strip()
            for key in ("source", "table", "column")
        )
        if not all(identity) or identity not in selected_by_identity:
            raise DatasetSchemaMetadataError(
                column_key,
                f"exact approved source identity {identity!r} is missing",
            )
        reviewed_meta = schema_catalog.field_metadata(*identity)
        if reviewed_meta is None:
            raise DatasetSchemaMetadataError(
                column_key,
                f"exact catalog entry {identity!r} is missing",
            )
        meta: dict[str, Any] = {}
        description = str((reviewed_meta or {}).get("description") or "").strip()
        if not description:
            raise DatasetSchemaMetadataError(
                column_key,
                (
                    f"exact catalog entry {identity!r} has no non-empty "
                    "catalog description"
                ),
            )
        meta["description"] = description
        for field in (
            "values",
            "depends_on",
            "condition",
            "section_context",
        ):
            value = (reviewed_meta or {}).get(field)
            if value is not None and value != "":
                meta[field] = value
        if dtypes is not None:
            meta["dataType"] = str(dtypes[column_name])
        schema[column_key] = meta
    return schema


def persist_sql_subset_artifact(
    state: dict[str, Any],
    candidate: Any,
    approved_selection: dict[str, Any],
    execution_result: Any,
    *,
    schema_catalog: SchemaCatalog,
    study_id: str,
    selection_artifact_id: str | None,
    sql_candidate_artifact_id: str | None,
    plan_id: str | None = None,
    plan_version: int | None = None,
    sql_version: int | None = None,
    dataset_id: str | None = None,
    dataset_name: str | None = None,
    exact_sql: str | None = None,
    predecessor_dataset_id: str | None = None,
    predecessor_dataset_version: int | None = None,
    relationship_metrics: list[dict[str, Any]] | None = None,
    post_sql_warnings: list[dict[str, Any]] | None = None,
    join_expansion: dict[str, float] | None = None,
    grain_columns: list[str] | None = None,
    runtime_root: Any = None,
    make_active: bool = True,
    stage_only: bool = False,
    artifact_version: int | None = None,
    artifact_status: str | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    StagedDatasetArtifact | None,
]:
    normalized_study_id = str(study_id or "").strip()
    if not normalized_study_id:
        raise ValueError("DB-RAG SQL subset persistence requires a study_id.")
    thread_id = str(
        dict(state.get("meta") or {}).get(MetaKeys.THREAD_ID) or ""
    ).strip()
    if not thread_id:
        raise ValueError(
            "DB-RAG SQL subset persistence requires a thread_id in state meta."
        )
    selected_tables = _string_list(
        _read_value(approved_selection, "tables", [])
        or _read_value(candidate, "tables", [])
    )
    selected_columns = serialize_columns(
        _read_value(approved_selection, "columns", [])
        or _read_value(candidate, "columns", [])
    )
    source_question, goal_text = _sql_candidate_review_texts(
        candidate,
        approved_selection,
    )
    resolved_dataset_name = (
        str(dataset_name).strip()
        if dataset_name is not None
        else generate_dataset_name(
            goal_text=goal_text,
            source_question=source_question,
            columns=selected_columns,
        )
    )
    population_scope = (
        _read_value(approved_selection, "population_scope", None)
        or _read_value(candidate, "population_scope", None)
    )
    population_assumption = (
        _read_value(approved_selection, "population_assumption", None)
        or _read_value(candidate, "population_assumption", None)
    )
    persisted_sql = (
        exact_sql
        if exact_sql is not None
        else str(
            _read_value(
                execution_result,
                "sql",
                _read_value(candidate, "sql", ""),
            )
            or ""
        ).strip()
    )
    dataframe = _read_value(execution_result, "dataframe")
    reconciliation = _reconcile_output_columns(
        dataframe,
        selected_columns,
        schema_catalog=schema_catalog,
        sql=persisted_sql,
    )
    reconciliation_warnings = list(reconciliation["warnings"])
    for warning in list(post_sql_warnings or []):
        if isinstance(warning, dict):
            reconciliation_warnings.append(dict(warning))
    deduplicated_warnings: list[dict[str, Any]] = []
    seen_warning_keys: set[tuple[str, str]] = set()
    for warning in reconciliation_warnings:
        warning_key = (
            str(warning.get("code") or "").strip(),
            str(warning.get("message") or "").strip(),
        )
        if warning_key in seen_warning_keys:
            continue
        seen_warning_keys.add(warning_key)
        deduplicated_warnings.append(dict(warning))
    provenance: dict[str, Any] = {
        "source": "db_rag_sql",
        "study_id": normalized_study_id,
        "thread_id": thread_id,
        "source_question": source_question,
        "goal_text": goal_text,
        "name": resolved_dataset_name,
        "description": resolved_dataset_name,
        "sql": persisted_sql,
        "source_tables": _string_list(
            _read_value(execution_result, "source_tables", [])
            or _read_value(candidate, "tables", [])
        ),
        "selected_tables": selected_tables,
        "selected_columns": selected_columns,
        "selection_id": str(
            _read_value(
                approved_selection,
                "selection_id",
                _read_value(candidate, "selection_id", ""),
            )
            or ""
        ).strip(),
        "selection_artifact_id": str(
            selection_artifact_id or ""
        ).strip(),
        "sql_candidate_artifact_id": str(
            sql_candidate_artifact_id or ""
        ).strip(),
        "feedback_history": list(
            _read_value(approved_selection, "feedback_history", []) or []
        ),
        "output_column_sources": dict(
            reconciliation["output_column_sources"]
        ),
        "post_sql_warnings": deduplicated_warnings,
    }
    if plan_id and plan_version is not None:
        provenance.update(
            {"plan_id": str(plan_id), "plan_version": int(plan_version)}
        )
    if sql_candidate_artifact_id and sql_version is not None:
        provenance.update(
            {
                "sql_id": str(sql_candidate_artifact_id),
                "sql_version": int(sql_version),
            }
        )
    if predecessor_dataset_id and predecessor_dataset_version is not None:
        provenance.update(
            {
                "predecessor_dataset_id": str(predecessor_dataset_id),
                "predecessor_dataset_version": int(
                    predecessor_dataset_version
                ),
            }
        )
    if relationship_metrics:
        provenance["relationship_metrics"] = [
            dict(metric) for metric in relationship_metrics
        ]
    if join_expansion:
        provenance["join_expansion"] = {
            str(name): float(ratio)
            for name, ratio in join_expansion.items()
        }
    if grain_columns:
        provenance["grain_columns"] = [
            str(column)
            for column in grain_columns
            if str(column).strip()
        ]
    if population_scope:
        provenance["population_scope"] = _normalize_population_scope(
            population_scope
        )
    if (
        isinstance(population_assumption, dict)
        and population_assumption
    ):
        provenance["population_assumption"] = dict(
            population_assumption
        )

    persistence_arguments = {
        "runtime_root": runtime_root,
        "thread_id": thread_id,
        "dataset_id": str(dataset_id or "").strip()
        or f"subset-{uuid4().hex[:8]}",
        "kind": "subset",
        "dataframe": dataframe,
        "schema": reconciliation["schema"],
        "provenance": provenance,
        "artifact_version": artifact_version,
        "artifact_status": artifact_status,
    }
    if stage_only:
        staged = stage_dataset_artifact(**persistence_arguments)
        return state, staged.artifact, staged
    artifact = persist_dataset_artifact(**persistence_arguments)
    return (
        register_dataset_artifact(
            state,
            artifact,
            make_active=make_active,
        ),
        artifact,
        None,
    )


__all__ = [
    "DatasetSchemaMetadataError",
    "persist_sql_subset_artifact",
    "serialize_columns",
]
