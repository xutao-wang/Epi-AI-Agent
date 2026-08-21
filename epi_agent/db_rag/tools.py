from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import duckdb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from db_rag.filter_references import (
    FilterReferenceResolutionError,
    resolve_filter_references,
)
from db_rag.catalog import SemanticCatalogUnavailableError
from db_rag.config import EMBEDDING_MODEL
from db_rag.retrieval_status import RetrievalOutcome, hybrid_status
from db_rag.service.dataset_naming import deterministic_dataset_name
from db_rag.service.models import (
    PreparedSqlCandidate,
    SqlExecutionResult,
    ValidatedExtractionSql,
)
from db_rag.service.sql_service import (
    execute_validated_extraction_sql,
    validate_extraction_sql,
)
from epi_agent.artifacts import (
    DatasetPlan,
    PlanField,
    dataset_plan_from_artifact,
)
from epi_agent.db_rag import persistence as db_rag_persistence
from epi_agent.db_rag.references import FieldRef, TableRef
from epi_agent.db_rag.sql_compiler import compile_dataset_plan_sql
from epi_agent.db_rag.quality import inspect_dataset as inspect_dataset_artifact
from epi_agent.db_rag.reviews import (
    RequestDatasetPlanReviewTool,
    RequestDatasetReviewTool,
)
from epi_agent.protocol import (
    AgentTool,
    ArtifactRef,
    ArtifactStore,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    require_context_study,
)
from epi_agent.registry import ToolRegistry
from epi_agent.studies import StudySourceUnavailableError
from utils.dataset_artifacts import (
    cleanup_dataset_staging,
    generated_dataset_artifact_paths,
    generated_dataset_staging_paths,
    load_verified_dataset_artifact,
    load_dataset_persistence_journal,
    promote_staged_dataset_artifact,
    verify_dataset_artifact_manifest,
    write_dataset_persistence_journal,
)


_MAX_SEARCH_HITS = 10
_MAX_CATALOG_QUERIES = 5
_MAX_TABLE_FIELDS = 25
_MAX_MODEL_CATALOG_TEXT_CHARS = 80
_MAX_EXCERPT_CHARS = 500
_MAX_OPEN_CHARS = 4_000
_DBRAG_TOOL_PREFIX = "dbrag-"
_DATASET_KINDS = {
    "analysis_dataset",
    "dataset",
    "db_rag_result",
    "subset",
    "uploaded",
}

_PLAN_REPAIR_HINTS = {
    "PLAN_FIELD_UNAVAILABLE": "Replace the field with an available table and column from the runtime catalog.",
    "PLAN_FILTER_VALUE_UNAVAILABLE": "Use an observed stored value compatible with the runtime field.",
    "PLAN_JOIN_UNAVAILABLE": "Use tables connected by an observed runtime key path.",
}


def _dbrag_tool_name(operation: str) -> str:
    return f"{_DBRAG_TOOL_PREFIX}{operation}"


class OpenArtifactArguments(BaseModel):
    artifact_id: str
    version: int = Field(ge=1)


class CatalogSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    study_id: str = Field(min_length=1, max_length=512)
    queries: list[str] = Field(
        min_length=1,
        max_length=_MAX_CATALOG_QUERIES,
        description=(
            "Independent semantic schema probes to execute together. Include all "
            "currently needed concepts in one call."
        ),
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=_MAX_SEARCH_HITS,
        description="Maximum catalog hits returned for each semantic probe.",
    )

    @field_validator("queries")
    @classmethod
    def normalize_queries(cls, value: list[str]) -> list[str]:
        normalized = [query.strip() for query in value]
        if any(not query for query in normalized):
            raise ValueError("Catalog semantic probes cannot be empty.")
        return normalized


class InspectTableArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    table_ref: TableRef
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=_MAX_TABLE_FIELDS, ge=1, le=_MAX_TABLE_FIELDS)


class FindJoinPathsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    required_fields: list[FieldRef] = Field(min_length=2)
    max_hops: int = Field(default=3, ge=1, le=5)
    max_paths: int = Field(default=10, ge=1, le=20)


class RelationshipKeyPairArguments(BaseModel):
    left_column: str
    right_column: str


class ProfileRelationshipArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    left_table_ref: TableRef
    right_table_ref: TableRef
    key_pairs: list[RelationshipKeyPairArguments] = Field(min_length=1)


class SaveDatasetPlanArguments(BaseModel):
    plan: DatasetPlan
    prior_id: str | None = None
    prior_version: int | None = Field(default=None, ge=1)


class ValidateDatasetPlanArguments(BaseModel):
    plan_id: str
    plan_version: int = Field(ge=1)


class ValidateAndExtractArguments(BaseModel):
    plan_id: str
    plan_version: int = Field(ge=1)
    sql: str | None = None
    predecessor_dataset_id: str | None = None
    predecessor_dataset_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_replacement_pair(self) -> "ValidateAndExtractArguments":
        if (self.predecessor_dataset_id is None) != (
            self.predecessor_dataset_version is None
        ):
            raise ValueError(
                "predecessor_dataset_id and predecessor_dataset_version "
                "must be supplied together"
            )
        if self.sql is not None and not self.sql.strip():
            raise ValueError("sql must not be blank when supplied")
        return self


class InspectDatasetArguments(BaseModel):
    dataset_id: str
    dataset_version: int = Field(ge=1)
    plan_id: str
    plan_version: int = Field(ge=1)


@dataclass(frozen=True)
class _FunctionTool:
    spec: ToolSpec
    handler: Callable[[dict[str, Any], ToolContext], ToolResult]

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        return self.handler(arguments, context)


def _store(context: ToolContext) -> ArtifactStore:
    store = context.artifact_store
    required_methods = (
        "require",
        "save_artifact",
        "save_dataset_plan",
        "transition_artifact_status",
        "snapshot",
    )
    if not all(callable(getattr(store, method, None)) for method in required_methods):
        raise ToolExecutionError(
            "ARTIFACT_STORE_UNAVAILABLE",
            "The DB-RAG artifact store is unavailable.",
            recoverable=False,
        )
    return store


def _dataset_root(context: ToolContext):
    if context.thread_storage is None:
        raise ToolExecutionError(
            "THREAD_STORAGE_UNAVAILABLE",
            "Internal configuration error: authorized thread storage is required for dataset persistence.",
            recoverable=False,
        )
    return context.thread_storage


def _save_observation(
    context: ToolContext,
    *,
    kind: str,
    content: dict[str, Any],
    producer: str,
    summary: str,
    study_id: str,
) -> ArtifactRef:
    return _store(context).save_artifact(
        kind=kind,
        content=content,
        provenance={
            "study_id": study_id,
            "thread_id": context.thread_id,
            "producer": producer,
        },
        summary=summary,
    )


def _bounded_model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        row = value.model_dump(mode="json")
    elif isinstance(value, dict):
        row = dict(value)
    else:
        raise TypeError(f"Unsupported provider result: {type(value).__name__}")
    if "text" in row:
        row["text"] = str(row["text"])[:_MAX_EXCERPT_CHARS]
    return row


def _safe_schema_provenance(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    allowed = (
        "authority",
        "source_id",
        "source_kind",
        "path",
        "table",
        "column",
        "form",
        "collection",
        "index_name",
        "chunk_id",
    )
    return {
        key: _bounded_text(value.get(key), limit=300)
        for key in allowed
        if value.get(key) is not None
    }


def _schema_evidence_hit(value: Any) -> dict[str, Any]:
    table = _bounded_text(_provider_field(value, "table"), limit=300)
    column = _bounded_text(_provider_field(value, "column"), limit=300)
    provenance = _safe_schema_provenance(
        _provider_field(value, "provenance")
    ) or {
        "authority": "runtime_schema_catalog",
        "table": table,
        **({"column": column} if column else {}),
    }
    source = (
        _bounded_text(_provider_field(value, "source"), limit=300)
        or _bounded_text(provenance.get("source_id"), limit=300)
    )
    raw_matched_by = _provider_field(value, "matched_by")
    matched_by = [
        mode
        for mode in (
            list(raw_matched_by)
            if isinstance(raw_matched_by, (list, tuple))
            else []
        )[:2]
        if mode in {"vector", "lexical"}
    ]
    return {
        **({"source": source} if source else {}),
        "table": table,
        **({"column": column} if column else {}),
        "text": _bounded_text(_provider_field(value, "text")),
        "source_kind": (
            _bounded_text(_provider_field(value, "source_kind"), limit=100)
            or "schema"
        ),
        "provenance": provenance,
        **({"matched_by": matched_by} if matched_by else {}),
    }


def _provider_field(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _bounded_text(value: Any, *, limit: int = _MAX_EXCERPT_CHARS) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return str(value or "")[:limit]


def _collection(content: dict[str, Any], key: str) -> list[Any]:
    value = content.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list")
    return value


def _safe_string_list(value: Any, *, limit: int = 50) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        _bounded_text(item, limit=200)
        for item in value[:limit]
        if isinstance(item, str)
    ]


def _safe_numeric_mapping(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {
        _bounded_text(key, limit=200): float(item)
        for key, item in value.items()
        if isinstance(key, str)
        and isinstance(item, (int, float))
        and not isinstance(item, bool)
    }


def _safe_boolean_mapping(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {
        _bounded_text(key, limit=200): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, bool)
    }


def _safe_field(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    field = {
        key: _bounded_text(value.get(key), limit=300)
        for key in (
            "source",
            "table",
            "column",
            "output_column",
            "purpose",
            "aggregation",
            "description",
            "usage",
            "semantic",
            "dataType",
            "notes",
            "text",
            "source_kind",
            "retrieval_probe",
        )
        if value.get(key) is not None
    }
    provenance = _safe_schema_provenance(value.get("provenance"))
    if provenance:
        field["provenance"] = provenance
    matched_by = _safe_string_list(value.get("matched_by"), limit=2)
    if matched_by:
        field["matched_by"] = [
            mode for mode in matched_by if mode in {"vector", "lexical"}
        ]
    return field


def _safe_key_pairs(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    pairs: list[list[str]] = []
    for pair in value[:20]:
        if isinstance(pair, dict):
            left = _bounded_text(pair.get("left_column"), limit=200)
            right = _bounded_text(pair.get("right_column"), limit=200)
        elif isinstance(pair, (list, tuple)) and len(pair) == 2:
            left = _bounded_text(pair[0], limit=200)
            right = _bounded_text(pair[1], limit=200)
        else:
            continue
        if left and right:
            pairs.append([left, right])
    return pairs


def _safe_relationship_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    required = {
        key: _bounded_text(value.get(key), limit=300)
        for key in (
            "left_column",
            "right_column",
            "left_join_key",
            "right_join_key",
        )
    }
    if not all(required.values()):
        return {}
    source = _bounded_text(value.get("source"), limit=100)
    if source not in {"shared_join_key", "declared_relationship"}:
        return {}
    result: dict[str, Any] = {**required, "source": source}
    for key in ("relationship_id", "note"):
        text = _bounded_text(value.get(key), limit=500)
        if text:
            result[key] = text
    expected = _bounded_text(value.get("expected_cardinality"), limit=100)
    if expected in {
        "one_to_one",
        "one_to_many",
        "many_to_one",
        "many_to_many",
    }:
        result["expected_cardinality"] = expected
    direction = _bounded_text(value.get("direction"), limit=100)
    if direction in {"forward", "reverse"}:
        result["direction"] = direction
    return result


def _safe_relationship_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    profile: dict[str, Any] = {}
    for key in ("left_table", "right_table", "left_cardinality", "right_cardinality"):
        if value.get(key) is not None:
            profile[key] = _bounded_text(value.get(key), limit=200)
    for key in (
        "left_distinct_keys",
        "right_distinct_keys",
        "matched_keys",
        "joined_rows",
    ):
        if isinstance(value.get(key), int):
            profile[key] = value[key]
    profile["key_pairs"] = _safe_key_pairs(_collection(value, "key_pairs"))
    profile["warnings"] = _safe_string_list(
        _collection(value, "warnings"),
        limit=20,
    )
    profile["relationship_evidence"] = [
        evidence
        for evidence in (
            _safe_relationship_evidence(item)
            for item in _collection(value, "relationship_evidence")[:20]
        )
        if evidence
    ]
    return profile


def _render_dataset_plan(content: dict[str, Any]) -> dict[str, Any]:
    concepts: list[dict[str, Any]] = []
    for concept in _collection(content, "concepts"):
        if not isinstance(concept, dict):
            continue
        safe_concept = {
            key: _bounded_text(concept.get(key), limit=500)
            for key in ("concept_id", "id", "label", "retrieval_probe")
            if concept.get(key) is not None
        }
        safe_concept["fields"] = [
            field
            for field in (
                _safe_field(item) for item in _collection(concept, "fields")
            )
            if field
        ][:50]
        concepts.append(safe_concept)

    operations: list[dict[str, Any]] = []
    for operation in _collection(content, "operations"):
        if not isinstance(operation, dict):
            continue
        safe_operation = {
            key: _bounded_text(operation.get(key), limit=500)
            for key in (
                "name",
                "type",
                "description",
                "source",
                "left_table",
                "right_table",
                "relationship_artifact_id",
            )
            if operation.get(key) is not None
        }
        if isinstance(operation.get("relationship_artifact_version"), int):
            safe_operation["relationship_artifact_version"] = operation[
                "relationship_artifact_version"
            ]
        safe_operation["key_pairs"] = _safe_key_pairs(
            _collection(operation, "key_pairs")
        )
        safe_operation["field_refs"] = [
            field
            for field in (
                _safe_field(item)
                for item in _collection(operation, "field_refs")
            )
            if field
        ][:50]
        operations.append(safe_operation)

    return {
        "goal": _bounded_text(content.get("goal"), limit=1_000),
        "row_definition": _bounded_text(
            content.get("row_definition"),
            limit=1_000,
        ),
        "concepts": concepts[:50],
        "required_fields": [
            field
            for field in (
                _safe_field(item)
                for item in _collection(content, "required_fields")
            )
            if field
        ][:100],
        "operations": operations[:50],
        "filter_count": len(_collection(content, "filters")),
        "unresolved_count": len(_collection(content, "unresolved")),
    }


def _render_evidence(content: dict[str, Any]) -> dict[str, Any]:
    collection_key = "sections" if "sections" in content else "hits"
    hits: list[dict[str, Any]] = []
    for hit in _collection(content, collection_key):
        if not isinstance(hit, dict):
            continue
        hits.append(
            {
                key: _bounded_text(hit.get(key))
                for key in (
                    "source_id",
                    "source_kind",
                    "title",
                    "section",
                    "text",
                    "excerpt",
                )
                if hit.get(key) is not None
            }
        )
    rendered = {collection_key: hits[:_MAX_SEARCH_HITS]}
    if content.get("query") is not None:
        rendered["query"] = _bounded_text(content.get("query"))
    if content.get("source_id") is not None:
        rendered["source_id"] = _bounded_text(content.get("source_id"))
    return rendered


def _compact_catalog_hit(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    hit = {
        "text": _bounded_text(
            value.get("text"),
            limit=_MAX_MODEL_CATALOG_TEXT_CHARS,
        )
    }
    matched_by = _safe_string_list(value.get("matched_by"), limit=2)
    if matched_by:
        hit["matched_by"] = [
            mode for mode in matched_by if mode in {"vector", "lexical"}
        ]
    for key, model in (("table_ref", TableRef), ("field_ref", FieldRef)):
        try:
            reference = model.model_validate(value.get(key))
        except ValidationError:
            continue
        hit[key] = reference.model_dump(mode="json")
    if "table_ref" not in hit and "field_ref" not in hit:
        hit.update(
            {
                key: _bounded_text(value.get(key), limit=300)
                for key in ("source", "table", "column")
                if value.get(key) is not None
            }
        )
    return hit


def _render_catalog_search(content: dict[str, Any]) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []
    for value in _collection(content, "probes")[:_MAX_CATALOG_QUERIES]:
        if not isinstance(value, dict):
            continue
        hits = [
            hit
            for hit in (
                _compact_catalog_hit(item)
                for item in _collection(value, "hits")[:_MAX_SEARCH_HITS]
            )
            if hit
        ]
        probe: dict[str, Any] = {
            "query": _bounded_text(value.get("query"), limit=500),
            "returned_count": len(hits),
            "hits": hits,
        }
        for key in (
            "table_hits",
            "column_hits",
            "unique_table_count",
            "unique_column_count",
        ):
            count = value.get(key)
            probe[key] = (
                max(0, count)
                if isinstance(count, int) and not isinstance(count, bool)
                else 0
            )
        probes.append(probe)

    summary = content.get("retrieval_summary")
    safe_summary: dict[str, Any] = {}
    if isinstance(summary, dict):
        for key in (
            "probe_count",
            "unique_table_count",
            "unique_column_count",
            "vector_hits",
            "lexical_hits",
        ):
            count = summary.get(key)
            safe_summary[key] = (
                max(0, count)
                if isinstance(count, int) and not isinstance(count, bool)
                else 0
            )

    embedding = content.get("embedding")
    safe_embedding = {
        key: (
            value
            if key == "available" and isinstance(value, bool)
            else _bounded_text(value, limit=500)
        )
        for key, value in dict(embedding or {}).items()
        if key in {"available", "model", "reason_code", "message"}
    }
    return {
        "study_id": _bounded_text(content.get("study_id"), limit=300),
        "retrieval_mode": _bounded_text(
            content.get("retrieval_mode"),
            limit=100,
        ),
        "embedding": safe_embedding,
        "source_ids": _safe_string_list(
            _collection(content, "source_ids"),
            limit=50,
        ),
        "retrieval_summary": safe_summary,
        "probes": probes,
    }


def _compact_inspection_field(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {
        key: _bounded_text(value.get(key), limit=300)
        for key in ("column", "text", "source_kind")
        if value.get(key) is not None
    }
    try:
        field_ref = FieldRef.model_validate(value.get("field_ref"))
    except ValidationError:
        return result
    result["field_ref"] = field_ref.model_dump(mode="json")
    return result


def _render_table_profile(content: dict[str, Any]) -> dict[str, Any]:
    fields = [
        field
        for field in (
            _compact_inspection_field(item)
            for item in _collection(content, "fields")
        )
        if field
    ][:_MAX_TABLE_FIELDS]
    offset = content.get("offset")
    next_offset = content.get("next_offset")
    rendered: dict[str, Any] = {
        "offset": (
            max(0, offset)
            if isinstance(offset, int) and not isinstance(offset, bool)
            else 0
        ),
        "returned_count": len(fields),
        "has_more": bool(content.get("has_more")),
        "next_offset": (
            next_offset
            if isinstance(next_offset, int) and not isinstance(next_offset, bool)
            else None
        ),
        "fields": fields,
    }
    try:
        table_ref = TableRef.model_validate(content.get("table_ref"))
    except ValidationError:
        rendered["source"] = _bounded_text(content.get("source"), limit=300)
        rendered["table"] = _bounded_text(content.get("table"), limit=300)
    else:
        rendered["table_ref"] = table_ref.model_dump(mode="json")
    return rendered


def _render_relationship(content: dict[str, Any]) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "study_id": _bounded_text(content.get("study_id")),
        "source": _bounded_text(content.get("source")),
    }
    for key in ("left_table_ref", "right_table_ref"):
        try:
            table_ref = TableRef.model_validate(content.get(key))
        except ValidationError:
            continue
        rendered[key] = table_ref.model_dump(mode="json")
    profile = _safe_relationship_profile(content.get("profile"))
    if profile:
        rendered["profile"] = profile
    required_fields: list[dict[str, Any]] = []
    for item in _collection(content, "required_fields"):
        try:
            field_ref = FieldRef.model_validate(item)
        except ValidationError:
            field = _safe_field(item)
            if field:
                required_fields.append(field)
        else:
            required_fields.append(field_ref.model_dump(mode="json"))
    if required_fields:
        rendered["required_fields"] = required_fields[:50]
    paths: list[dict[str, Any]] = []
    for path in _collection(content, "paths"):
        if not isinstance(path, dict):
            continue
        paths.append(
            {
                "tables": _safe_string_list(
                    _collection(path, "tables"),
                    limit=10,
                ),
                "profiles": [
                    safe_profile
                    for safe_profile in (
                        _safe_relationship_profile(item)
                        for item in _collection(path, "profiles")
                    )
                    if safe_profile
                ][:10],
            }
        )
    if paths:
        rendered["paths"] = paths[:20]
    return rendered


def _render_quality(content: dict[str, Any]) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "dataset_id": _bounded_text(content.get("dataset_id"), limit=200),
        "plan_id": _bounded_text(content.get("plan_id"), limit=200),
        "sql_id": _bounded_text(content.get("sql_id"), limit=200),
    }
    for key in (
        "dataset_version",
        "plan_version",
        "sql_version",
        "row_count",
        "column_count",
        "duplicate_grain_rows",
    ):
        value = content.get(key)
        rendered[key] = value if isinstance(value, int) else None
    rendered["null_rates"] = _safe_numeric_mapping(content.get("null_rates"))
    rendered["requested_concept_coverage"] = _safe_boolean_mapping(
        content.get("requested_concept_coverage")
    )
    rendered["join_expansion"] = _safe_numeric_mapping(
        content.get("join_expansion")
    )
    grain_uniqueness = content.get("grain_uniqueness")
    if isinstance(grain_uniqueness, dict):
        rendered["grain_uniqueness"] = {
            "columns": _safe_string_list(
                _collection(grain_uniqueness, "columns"),
                limit=20,
            ),
            "row_count": grain_uniqueness.get("row_count"),
            "distinct_key_count": grain_uniqueness.get("distinct_key_count"),
            "duplicate_rows": grain_uniqueness.get("duplicate_rows"),
            "is_unique": grain_uniqueness.get("is_unique"),
        }
    else:
        rendered["grain_uniqueness"] = None
    rendered["relationship_metrics"] = [
        {
            "evidence_label": _bounded_text(
                metric.get("evidence_label"),
                limit=100,
            ),
            "relationship_artifact_id": _bounded_text(
                metric.get("relationship_artifact_id"),
                limit=200,
            ),
            "relationship_artifact_version": metric.get(
                "relationship_artifact_version"
            ),
            **_safe_relationship_profile(metric),
        }
        for metric in _collection(content, "relationship_metrics")
        if isinstance(metric, dict)
    ][:20]
    rendered["unexpected_columns"] = _safe_string_list(
        _collection(content, "unexpected_columns"),
        limit=100,
    )
    rendered["warnings"] = [
        {
            key: _bounded_text(warning.get(key), limit=500)
            for key in ("code", "severity", "message")
            if warning.get(key) is not None
        }
        for warning in _collection(content, "warnings")
        if isinstance(warning, dict)
    ][:50]
    return rendered


def _render_selection(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "selection_id": _bounded_text(content.get("selection_id")),
        "goal_text": _bounded_text(content.get("goal_text"), limit=1_000),
        "tables": _safe_string_list(_collection(content, "tables"), limit=50),
        "columns": [
            field
            for field in (
                _safe_field(item) for item in _collection(content, "columns")
            )
            if field
        ][:100],
        "rationale": _bounded_text(content.get("rationale"), limit=1_000),
        "status": _bounded_text(content.get("status"), limit=100),
    }


def _render_sql(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "sql_candidate_id": _bounded_text(content.get("sql_candidate_id")),
        "plan_id": _bounded_text(content.get("plan_id")),
        "plan_version": (
            content.get("plan_version")
            if isinstance(content.get("plan_version"), int)
            else None
        ),
        "selection_artifact_id": _bounded_text(
            content.get("selection_artifact_id")
        ),
        "goal_text": _bounded_text(content.get("goal_text"), limit=1_000),
        "tables": _safe_string_list(_collection(content, "tables"), limit=50),
        "columns": [
            field
            for field in (
                _safe_field(item) for item in _collection(content, "columns")
            )
            if field
        ][:100],
        "sql": _bounded_text(content.get("sql"), limit=3_000),
        "status": _bounded_text(content.get("status"), limit=100),
    }


def _dataset_lineage(content: dict[str, Any]) -> dict[str, Any]:
    provenance = content.get("provenance")
    if not isinstance(provenance, dict):
        return {}
    lineage: dict[str, Any] = {}
    for prefix in ("plan", "selection", "sql"):
        artifact_id = provenance.get(f"{prefix}_id")
        version = provenance.get(f"{prefix}_version")
        if isinstance(artifact_id, str) and isinstance(version, int):
            lineage[f"{prefix}_id"] = artifact_id
            lineage[f"{prefix}_version"] = version
    return lineage


def _render_dataset(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _bounded_text(content.get("id"), limit=200),
        "status": _bounded_text(content.get("status"), limit=100),
        "row_count": content.get("row_count")
        if isinstance(content.get("row_count"), int)
        else None,
        "column_count": content.get("column_count")
        if isinstance(content.get("column_count"), int)
        else None,
        "columns": _safe_string_list(
            _collection(content, "columns"),
            limit=200,
        ),
        "lineage": _dataset_lineage(content),
    }


_ARTIFACT_RENDERERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "catalog_search": _render_catalog_search,
    "dataset_plan": _render_dataset_plan,
    "dataset_quality_report": _render_quality,
    "db_rag_column_selection": _render_selection,
    "db_rag_sql_candidate": _render_sql,
    "validated_sql": _render_sql,
    "relationship_profile": _render_relationship,
    "study_evidence": _render_evidence,
    "study_source": _render_evidence,
    "table_profile": _render_table_profile,
}


def _load_artifact(context: ToolContext, artifact_id: str) -> Any:
    store = _store(context)
    try:
        artifact = store.require(artifact_id)
    except KeyError as error:
        snapshot = store.snapshot()
        files = dict(snapshot.get("files") or {})
        datasets = dict(snapshot.get("datasets") or {})
        if artifact_id in files or artifact_id in datasets:
            raise ToolExecutionError(
                "ARTIFACT_STALE",
                f"Artifact does not use the current envelope: {artifact_id}",
                recoverable=True,
            ) from error
        raise ToolExecutionError(
            "ARTIFACT_NOT_FOUND",
            f"Artifact is unavailable: {artifact_id}",
            recoverable=True,
        ) from error
    except ValidationError as error:
        raise ToolExecutionError(
            "ARTIFACT_STALE",
            f"Artifact does not use the current envelope: {artifact_id}",
            recoverable=True,
        ) from error
    for attribute in ("id", "kind", "version", "status", "content", "provenance"):
        if not hasattr(artifact, attribute):
            raise ToolExecutionError(
                "ARTIFACT_STALE",
                f"Artifact does not use the current envelope: {artifact_id}",
                recoverable=True,
            )
    return artifact


def _require_artifact(
    context: ToolContext,
    *,
    artifact_id: str,
    version: int,
    kind: str | None = None,
) -> Any:
    artifact = _load_artifact(context, artifact_id)
    if artifact.version != version or (kind is not None and artifact.kind != kind):
        raise ToolExecutionError(
            "ARTIFACT_STALE",
            f"Artifact version or kind is stale: {artifact_id}",
            recoverable=True,
        )
    return artifact


def _require_source_for_study(study: Any, source_id: str) -> Any:
    try:
        return study.data_sources[source_id]
    except KeyError as error:
        raise ToolExecutionError(
            "SOURCE_UNAVAILABLE",
            (
                f"Runtime source {source_id} is unavailable in study "
                f"{study.study_id}."
            ),
            recoverable=True,
            details={
                "study_id": study.study_id,
                "source_id": source_id,
            },
        ) from error


def _resolve_table_ref(
    context: ToolContext,
    value: dict[str, Any] | TableRef,
) -> tuple[TableRef, Any, Any]:
    reference = TableRef.model_validate(value)
    study = require_context_study(context, reference.study_id)
    source = _require_source_for_study(study, reference.source_id)
    return reference, study, source


def _catalog_field_exists_for_study(
    study: Any,
    table: str,
    column: str,
) -> bool:
    field_exists = getattr(study.catalog, "field_exists", None)
    if not callable(field_exists):
        raise ToolExecutionError(
            "CATALOG_UNAVAILABLE",
            (
                f"Study {study.study_id} does not provide runtime field "
                "validation."
            ),
            recoverable=True,
        )
    return bool(field_exists(table, column))


def _relationship_inventory_for_study(study: Any, source_id: str) -> Any:
    source = _require_source_for_study(study, source_id)
    factory = getattr(source, "relationship_inventory", None)
    if not callable(factory):
        raise ToolExecutionError(
            "RELATIONSHIP_PROVIDER_UNAVAILABLE",
            f"Source does not provide relationship inspection: {source_id}",
            recoverable=True,
        )
    try:
        return factory()
    except StudySourceUnavailableError as error:
        raise ToolExecutionError(
            "RELATIONSHIP_PROVIDER_UNAVAILABLE",
            f"Relationship provider is unavailable for source {source_id}.",
            recoverable=True,
        ) from error
    except (KeyError, ValueError) as error:
        raise ToolExecutionError(
            "RELATIONSHIP_UNAVAILABLE",
            f"Relationship inventory is unavailable for source {source_id}.",
            recoverable=True,
        ) from error


def _open_artifact(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    artifact_id = arguments["artifact_id"]
    artifact = _require_artifact(
        context,
        artifact_id=artifact_id,
        version=arguments["version"],
    )
    if artifact.kind in _DATASET_KINDS:
        renderer = _render_dataset
    else:
        renderer = _ARTIFACT_RENDERERS.get(artifact.kind)
        if renderer is None:
            raise ToolExecutionError(
                "ARTIFACT_KIND_UNSAFE",
                f"Artifact kind cannot be opened safely: {artifact.kind}",
                recoverable=True,
            )
    try:
        safe_content = renderer(artifact.content)
    except (TypeError, ValueError) as error:
        raise ToolExecutionError(
            "ARTIFACT_STALE",
            f"Artifact content is stale: {artifact.kind} {artifact.id}",
            recoverable=True,
        ) from error
    rendered = json.dumps(safe_content, sort_keys=True)
    if len(rendered) > _MAX_OPEN_CHARS:
        rendered = rendered[:_MAX_OPEN_CHARS] + "..."
    reference = ArtifactRef(
        id=artifact.id,
        kind=artifact.kind,
        version=artifact.version,
    )
    return ToolResult(
        message=f"{artifact.kind} {artifact.id} version {artifact.version}: {rendered}",
        artifacts=(reference,),
    )


def _raise_missing_result(code: str, message: str) -> None:
    raise ToolExecutionError(
        code,
        message,
        recoverable=True,
    )


def _search_catalog(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    limit = min(int(arguments["limit"]), _MAX_SEARCH_HITS)
    queries = list(arguments["queries"])
    study = require_context_study(context, arguments["study_id"])
    search_many = getattr(study.catalog, "search_many", None)
    search_many_with_status = getattr(
        study.catalog,
        "search_many_with_status",
        None,
    )
    if not callable(search_many):
        raise ToolExecutionError(
            "CATALOG_UNAVAILABLE",
            "The requested study does not provide batched catalog search.",
            recoverable=True,
        )
    source_ids = sorted(str(source_id) for source_id in study.data_sources)
    all_hits: list[dict[str, Any]] = []
    all_tables: set[tuple[str, str]] = set()
    all_columns: set[tuple[str, str, str]] = set()
    probe_results: list[dict[str, Any]] = []
    try:
        if callable(search_many_with_status):
            outcome = search_many_with_status(queries, limit=limit)
        else:
            outcome = RetrievalOutcome(
                value=search_many(queries, limit=limit),
                status=hybrid_status(
                    getattr(study.catalog, "embedding_model", EMBEDDING_MODEL)
                ),
            )
    except SemanticCatalogUnavailableError as error:
        raise ToolExecutionError(
            "SEMANTIC_CATALOG_UNAVAILABLE",
            str(error),
            recoverable=True,
        ) from error
    provider_batches = outcome.value
    if len(provider_batches) != len(queries):
        raise ToolExecutionError(
            "CATALOG_RESPONSE_INVALID",
            "Catalog search did not return one result batch per semantic probe.",
            recoverable=True,
        )
    for query, provider_hits in zip(queries, provider_batches):
        normalized_hits: list[dict[str, Any]] = []
        for provider_hit in provider_hits[:limit]:
            hit = _schema_evidence_hit(provider_hit)
            source = str(hit.get("source") or "").strip()
            if not source and len(source_ids) == 1:
                source = source_ids[0]
            if source and source not in source_ids:
                raise ToolExecutionError(
                    "CATALOG_SOURCE_UNAVAILABLE",
                    "Catalog evidence references an unavailable runtime source.",
                    recoverable=True,
                )
            if source:
                hit["source"] = source
                hit["provenance"] = {
                    **dict(hit.get("provenance") or {}),
                    "source_id": source,
                }
                table_ref = {
                    "study_id": study.study_id,
                    "source_id": source,
                    "table": str(hit.get("table") or ""),
                }
                if hit.get("column"):
                    hit["field_ref"] = {
                        **table_ref,
                        "column": str(hit["column"]),
                    }
                else:
                    hit["table_ref"] = table_ref
            hit["retrieval_probe"] = query
            normalized_hits.append(hit)
            all_hits.append(hit)
        probe_tables = {
            (str(hit.get("source") or ""), str(hit.get("table") or ""))
            for hit in normalized_hits
            if str(hit.get("table") or "")
        }
        probe_columns = {
            (
                str(hit.get("source") or ""),
                str(hit.get("table") or ""),
                str(hit.get("column") or ""),
            )
            for hit in normalized_hits
            if str(hit.get("table") or "") and str(hit.get("column") or "")
        }
        all_tables.update(probe_tables)
        all_columns.update(probe_columns)
        probe_results.append(
            {
                "query": query,
                "returned_count": len(normalized_hits),
                "table_hits": sum(
                    1 for hit in normalized_hits if not hit.get("column")
                ),
                "column_hits": sum(
                    1 for hit in normalized_hits if hit.get("column")
                ),
                "unique_table_count": len(probe_tables),
                "unique_column_count": len(probe_columns),
                "hits": normalized_hits,
            }
        )
    content = {
        "study_id": study.study_id,
        "queries": queries,
        "source_ids": source_ids,
        "retrieval_mode": outcome.status.mode,
        "embedding": outcome.status.as_dict(),
        "retrieval_summary": {
            "probe_count": len(queries),
            "unique_table_count": len(all_tables),
            "unique_column_count": len(all_columns),
            "vector_hits": sum(
                "vector" in hit.get("matched_by", []) for hit in all_hits
            ),
            "lexical_hits": sum(
                "lexical" in hit.get("matched_by", []) for hit in all_hits
            ),
        },
        "probes": probe_results,
    }
    reference = _save_observation(
        context,
        kind="catalog_search",
        content=content,
        producer="dbrag-search_catalog",
        summary=(
            f"{sum(probe['returned_count'] for probe in probe_results)} "
            "schema catalog hits for "
            f"{len(queries)} probes"
        ),
        study_id=study.study_id,
    )
    return ToolResult(
        message=json.dumps(
            _render_catalog_search(content),
            separators=(",", ":"),
            sort_keys=True,
        ),
        artifacts=(reference,),
    )


def _inspect_table(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    table_ref, study, _source = _resolve_table_ref(
        context,
        arguments["table_ref"],
    )
    inspect_table = getattr(study.catalog, "inspect_table", None)
    if not callable(inspect_table):
        raise ToolExecutionError(
            "CATALOG_UNAVAILABLE",
            "The requested study does not provide exact table inspection.",
            recoverable=True,
        )
    offset = int(arguments["offset"])
    limit = int(arguments["limit"])
    provider_fields = inspect_table(
        table_ref.source_id,
        table_ref.table,
        offset=offset,
        limit=limit + 1,
    )
    fields = []
    for provider_hit in provider_fields[:limit]:
        hit = _schema_evidence_hit(provider_hit)
        if (
            hit.get("source") != table_ref.source_id
            or hit.get("table") != table_ref.table
        ):
            raise ToolExecutionError(
                "STUDY_REFERENCE_MISMATCH",
                "Exact table evidence does not match the requested table reference.",
                recoverable=True,
            )
        hit["field_ref"] = {
            **table_ref.model_dump(mode="json"),
            "column": str(hit.get("column") or ""),
        }
        fields.append(hit)
    if not fields:
        _raise_missing_result(
            "TABLE_NOT_FOUND",
            f"Runtime table is unavailable: {table_ref.table}",
        )
    has_more = len(provider_fields) > limit
    content = {
        "table_ref": table_ref.model_dump(mode="json"),
        "offset": offset,
        "returned_count": len(fields),
        "has_more": has_more,
        "next_offset": (
            offset + limit
            if has_more
            else None
        ),
        "fields": fields,
    }
    reference = _save_observation(
        context,
        kind="table_profile",
        content=content,
        producer="dbrag-inspect_table",
        summary=f"Bounded schema profile for {table_ref.table}",
        study_id=study.study_id,
    )
    return ToolResult(
        message=json.dumps(
            _render_table_profile(content),
            separators=(",", ":"),
            sort_keys=True,
        ),
        artifacts=(reference,),
    )


def _find_join_paths(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    required_fields = [
        FieldRef.model_validate(field)
        for field in arguments["required_fields"]
    ]
    study_ids = {field.study_id for field in required_fields}
    if len(study_ids) != 1:
        raise ToolExecutionError(
            "CROSS_STUDY_OPERATION_UNAVAILABLE",
            "Join-path discovery cannot combine fields from different studies.",
            recoverable=True,
        )
    source_ids = {field.source_id for field in required_fields}
    sources = source_ids
    if len(sources) != 1:
        raise ToolExecutionError(
            "CROSS_SOURCE_RELATIONSHIP_UNAVAILABLE",
            "Join-path discovery requires fields from one runtime source.",
            recoverable=True,
        )
    study = require_context_study(context, next(iter(study_ids)))
    source_name = next(iter(source_ids))
    _require_source_for_study(study, source_name)
    for field in required_fields:
        if not _catalog_field_exists_for_study(
            study,
            field.table,
            field.column,
        ):
            _raise_missing_result(
                "JOIN_PATH_UNAVAILABLE",
                (
                    "Join-path field is unavailable: "
                    f"{field.table}.{field.column}"
                ),
            )
    tables = list(dict.fromkeys(field.table for field in required_fields))
    if len(tables) < 2:
        raise ToolExecutionError(
            "JOIN_PATH_NOT_REQUIRED",
            "Join-path discovery requires at least two runtime tables.",
            recoverable=True,
        )

    inventory = _relationship_inventory_for_study(study, source_name)
    paths: list[dict[str, Any]] = []
    for left_index, left_table in enumerate(tables):
        for right_table in tables[left_index + 1 :]:
            remaining = arguments["max_paths"] - len(paths)
            if remaining <= 0:
                break
            try:
                found = inventory.find_join_paths(
                    left_table,
                    right_table,
                    max_hops=arguments["max_hops"],
                    max_paths=remaining,
                )
            except (KeyError, ValueError) as error:
                raise ToolExecutionError(
                    "JOIN_PATH_UNAVAILABLE",
                    (
                        "No runtime join path is available between "
                        f"{left_table} and {right_table}."
                    ),
                    recoverable=True,
                ) from error
            paths.extend(_bounded_model_dump(path) for path in found)
    if not paths:
        _raise_missing_result(
            "JOIN_PATH_UNAVAILABLE",
            "No observed runtime join path covers the requested fields.",
        )
    content = {
        "study_id": study.study_id,
        "source": source_name,
        "required_fields": [
            field.model_dump(mode="json") for field in required_fields
        ],
        "paths": paths,
    }
    rendered_content = _render_relationship(content)
    reference = _save_observation(
        context,
        kind="relationship_profile",
        content=rendered_content,
        producer="dbrag-find_join_paths",
        summary=f"{len(paths)} observed join paths",
        study_id=study.study_id,
    )
    return ToolResult(
        message=json.dumps(rendered_content, sort_keys=True),
        artifacts=(reference,),
    )


def _profile_relationship(
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    left_ref = TableRef.model_validate(arguments["left_table_ref"])
    right_ref = TableRef.model_validate(arguments["right_table_ref"])
    if left_ref.study_id != right_ref.study_id:
        raise ToolExecutionError(
            "CROSS_STUDY_OPERATION_UNAVAILABLE",
            "Relationship profiling cannot combine tables from different studies.",
            recoverable=True,
        )
    if left_ref.source_id != right_ref.source_id:
        raise ToolExecutionError(
            "CROSS_SOURCE_RELATIONSHIP_UNAVAILABLE",
            "Relationship profiling requires tables from one runtime source.",
            recoverable=True,
        )
    study = require_context_study(context, left_ref.study_id)
    inventory = _relationship_inventory_for_study(study, left_ref.source_id)
    key_pairs = [
        (pair["left_column"], pair["right_column"])
        for pair in arguments["key_pairs"]
    ]
    try:
        profile = inventory.profile_relationship(
            left_ref.table,
            right_ref.table,
            key_pairs,
        )
    except (KeyError, ValueError) as error:
        raise ToolExecutionError(
            "RELATIONSHIP_UNAVAILABLE",
            (
                "The requested runtime relationship could not be profiled "
                f"between {left_ref.table} and {right_ref.table}."
            ),
            recoverable=True,
        ) from error
    profile_content = _bounded_model_dump(profile)
    content = {
        "study_id": study.study_id,
        "source": left_ref.source_id,
        "left_table_ref": left_ref.model_dump(mode="json"),
        "right_table_ref": right_ref.model_dump(mode="json"),
        "profile": profile_content,
    }
    rendered_content = _render_relationship(content)
    reference = _save_observation(
        context,
        kind="relationship_profile",
        content=rendered_content,
        producer="dbrag-profile_relationship",
        summary=(
            f"Observed relationship between {left_ref.table} "
            f"and {right_ref.table}"
        ),
        study_id=study.study_id,
    )
    return ToolResult(
        message=json.dumps(rendered_content, sort_keys=True),
        artifacts=(reference,),
    )


def _require_plan_study(context: ToolContext, plan: DatasetPlan) -> Any:
    try:
        return require_context_study(context, plan.study_id)
    except ToolExecutionError as error:
        if error.code not in {
            "STUDY_NOT_AVAILABLE",
            "NO_STUDY_PACKAGE_INSTALLED",
        }:
            raise
        raise ToolExecutionError(
            "PLAN_STUDY_UNAVAILABLE",
            f"Dataset plan study is unavailable: {plan.study_id}",
            recoverable=True,
            details={"study_id": plan.study_id},
        ) from error


def _save_dataset_plan(
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    plan = DatasetPlan.model_validate(arguments["plan"])
    study = _require_plan_study(context, plan)
    source_ids = set(study.data_sources)
    for source, table, column in _plan_reference_keys(plan):
        if source not in source_ids:
            raise ToolExecutionError(
                "STUDY_REFERENCE_MISMATCH",
                (
                    f"Plan field {source}.{table}.{column} does not belong "
                    f"to study {plan.study_id}."
                ),
                recoverable=True,
                details={
                    "study_id": plan.study_id,
                    "source_id": source,
                    "table": table,
                    "column": column,
                },
            )
        if not _catalog_field_exists_for_study(study, table, column):
            raise ToolExecutionError(
                "PLAN_FIELD_UNAVAILABLE",
                f"Plan field is unavailable: {source}.{table}.{column}",
                recoverable=True,
                details={
                    "study_id": plan.study_id,
                    "source": source,
                    "table": table,
                    "column": column,
                },
            )
    prior_id = arguments.get("prior_id")
    prior_version = arguments.get("prior_version")
    if (prior_id is None) != (prior_version is None):
        raise ToolExecutionError(
            "INVALID_ARGUMENTS",
            "prior_id and prior_version must be provided together.",
            recoverable=True,
        )
    if prior_id is not None and prior_version is not None:
        prior = _require_artifact(
            context,
            artifact_id=prior_id,
            version=prior_version,
            kind="dataset_plan",
        )
        revision_authorized = any(
            feedback.status == "active"
            and feedback.content.get("action") == "revise"
            and feedback.content.get("plan_id") == prior_id
            and feedback.content.get("plan_version") == prior_version
            for feedback in _store(context).list_artifacts(
                kind="dataset_review_feedback"
            )
        )
        if prior.status == "approved" and not revision_authorized:
            raise ToolExecutionError(
                "PLAN_REVISION_NOT_AUTHORIZED",
                (
                    "The approved dataset plan remains the exact human-reviewed "
                    "contract. Repair and retry SQL against the same approved plan; "
                    "do not save or request review for another dataset plan."
                ),
                recoverable=True,
            )
    try:
        reference = _store(context).save_dataset_plan(
            plan,
            prior_id=prior_id,
            prior_version=prior_version,
            provenance={
                "study_id": study.study_id,
                "thread_id": context.thread_id,
                "producer": "dbrag-save_dataset_plan",
            },
        )
    except KeyError as error:
        raise ToolExecutionError(
            "ARTIFACT_STALE",
            "The prior dataset plan revision is stale.",
            recoverable=True,
        ) from error
    return ToolResult(
        message=(
            f"Saved dataset plan plan_id={reference.id} "
            f"version={reference.version} status=draft."
        ),
        artifacts=(reference,),
    )


def _plan_output_fields(plan: DatasetPlan) -> list[PlanField]:
    fields = [
        *plan.required_fields,
        *(field for concept in plan.concepts for field in concept.fields),
    ]
    seen: set[tuple[str, str, str]] = set()
    output: list[PlanField] = []
    for field in fields:
        key = (field.source, field.table, field.column)
        if key in seen:
            continue
        seen.add(key)
        output.append(field)
    return output


def _plan_issue(
    error: ToolExecutionError,
    *,
    path: str | None = None,
    source: str | None = None,
    table: str | None = None,
    column: str | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "code": error.code,
        "message": str(error),
    }
    if error.details:
        issue.update(error.details)
    if path and "path" not in issue:
        issue["path"] = path
    for name, value in (("source", source), ("table", table), ("column", column)):
        if value and name not in issue:
            issue[name] = value
    issue.setdefault(
        "repair",
        _PLAN_REPAIR_HINTS.get(
            error.code,
            "Revise the plan entry identified by path and validate the plan again.",
        ),
    )
    return issue


def _raise_collected_plan_errors(issues: list[dict[str, Any]]) -> None:
    if not issues:
        return
    if len(issues) == 1:
        code = str(issues[0]["code"])
        message = str(issues[0]["message"])
    else:
        code = "PLAN_VALIDATION_FAILED"
        locations = ", ".join(
            f"{issue['code']} at {issue.get('path', 'plan')}"
            for issue in issues
        )
        message = (
            f"Dataset plan validation found {len(issues)} independent issues. "
            f"Issues: {locations}. Address every issue in details.issues and "
            "validate the revised plan."
        )
    raise ToolExecutionError(
        code,
        message,
        recoverable=True,
        details={"issues": issues},
    )


def _plan_reference_keys(plan: DatasetPlan) -> set[tuple[str, str, str]]:
    keys = {
        (field.source, field.table, field.column)
        for field in _plan_output_fields(plan)
    }
    for operation in plan.operations:
        for reference in operation.field_refs:
            keys.add((reference.source, reference.table, reference.column))
        if operation.name.strip().casefold() != "join" or not operation.source:
            continue
        left_table = str(operation.left_table or "").strip()
        right_table = str(operation.right_table or "").strip()
        for pair in operation.key_pairs:
            keys.add((operation.source, left_table, pair.left_column))
            keys.add((operation.source, right_table, pair.right_column))
    for filter_entry in plan.filters:
        for reference in [
            *list(filter_entry.get("referenced_columns") or []),
            *list(filter_entry.get("value_constraints") or []),
        ]:
            source = str(reference.get("source") or "").strip()
            table = str(reference.get("table") or "").strip()
            column = str(reference.get("column") or "").strip()
            if source and table and column:
                keys.add((source, table, column))
    for reduction in plan.reductions:
        for reference in [*reduction.group_by, reduction.order_by, *reduction.tie_breakers]:
            if reference is not None:
                keys.add((reference.source, reference.table, reference.column))
        for aggregate in reduction.aggregates:
            keys.add((aggregate.field.source, aggregate.field.table, aggregate.field.column))
        for filter_entry in reduction.filters:
            for reference in [
                *list(filter_entry.get("referenced_columns") or []),
                *list(filter_entry.get("value_constraints") or []),
            ]:
                if isinstance(reference, dict):
                    source = str(reference.get("source") or reduction.source).strip()
                    table = str(reference.get("table") or reduction.table).strip()
                    column = str(reference.get("column") or "").strip()
                    if source and table and column:
                        keys.add((source, table, column))
    return keys


def _raise_plan_error(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    raise ToolExecutionError(
        code,
        message,
        recoverable=True,
        details=details,
    )


def _operation_key_pairs(operation: dict[str, Any]) -> list[tuple[str, str]]:
    raw_pairs = operation.get("key_pairs")
    if not isinstance(raw_pairs, list) or not raw_pairs:
        _raise_plan_error(
            "PLAN_RELATIONSHIP_UNPROVEN",
            "Join operation requires explicit relationship key pairs.",
        )
    pairs: list[tuple[str, str]] = []
    for pair in raw_pairs:
        if not isinstance(pair, dict):
            _raise_plan_error(
                "PLAN_RELATIONSHIP_UNPROVEN",
                "Join operation relationship key pairs are malformed.",
            )
        left = str(pair.get("left_column") or "").strip()
        right = str(pair.get("right_column") or "").strip()
        if not left or not right:
            _raise_plan_error(
                "PLAN_RELATIONSHIP_UNPROVEN",
                "Join operation relationship key pairs are incomplete.",
            )
        pairs.append((left, right))
    return pairs


def _relationship_profiles(content: dict[str, Any]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    profile = content.get("profile")
    if isinstance(profile, dict):
        profiles.append(profile)
    for path in content.get("paths") or []:
        if not isinstance(path, dict):
            continue
        profiles.extend(
            profile
            for profile in path.get("profiles") or []
            if isinstance(profile, dict)
        )
    return profiles


def _profile_key_pairs(profile: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for pair in profile.get("key_pairs") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            pairs.append((str(pair[0]), str(pair[1])))
    return pairs


def _profile_matches_operation(
    profile: dict[str, Any],
    *,
    left_table: str,
    right_table: str,
    key_pairs: list[tuple[str, str]],
) -> bool:
    profile_left = str(profile.get("left_table") or "")
    profile_right = str(profile.get("right_table") or "")
    profile_pairs = _profile_key_pairs(profile)
    if profile_left == left_table and profile_right == right_table:
        return profile_pairs == key_pairs
    if profile_left == right_table and profile_right == left_table:
        return profile_pairs == [(right, left) for left, right in key_pairs]
    return False


def _relationship_metric_for_operation(
    context: ToolContext,
    operation: dict[str, Any],
) -> dict[str, Any]:
    artifact_id = str(
        operation.get("relationship_artifact_id") or ""
    ).strip()
    artifact_version = operation.get("relationship_artifact_version")
    relationship = _require_artifact(
        context,
        artifact_id=artifact_id,
        version=artifact_version,
        kind="relationship_profile",
    )
    key_pairs = _operation_key_pairs(operation)
    left_table = str(operation.get("left_table") or "")
    right_table = str(operation.get("right_table") or "")
    profile = next(
        (
            value
            for value in _relationship_profiles(relationship.content)
            if _profile_matches_operation(
                value,
                left_table=left_table,
                right_table=right_table,
                key_pairs=key_pairs,
            )
        ),
        None,
    )
    if profile is None:
        raise ToolExecutionError(
            "PLAN_RELATIONSHIP_UNPROVEN",
            "Approved join relationship evidence is no longer available.",
            recoverable=True,
        )
    return {
        "evidence_label": "profiled relationship risk",
        "relationship_artifact_id": relationship.id,
        "relationship_artifact_version": relationship.version,
        **dict(profile),
    }


def _plan_relationship_metrics(
    context: ToolContext,
    plan: DatasetPlan,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    metrics: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for model in plan.operations:
        operation = model.model_dump(mode="json")
        if str(operation.get("name") or "").strip().casefold() != "join":
            continue
        try:
            metrics.append(_relationship_metric_for_operation(context, operation))
        except (
            ToolExecutionError,
            AttributeError,
            TypeError,
            ValueError,
        ) as error:
            diagnostic = (
                error.code
                if isinstance(error, ToolExecutionError)
                else f"{type(error).__name__}: {error}"
            )
            warnings.append(
                {
                    "code": "RELATIONSHIP_METRICS_UNAVAILABLE",
                    "severity": "medium",
                    "message": (
                        "Optional relationship metadata was unavailable after "
                        f"SQL succeeded: {diagnostic}"
                    ),
                }
            )
    return metrics, warnings


_FILTER_OPERATOR_SQL = {
    "=": "=",
    "==": "=",
    "!=": "!=",
    "<>": "<>",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
    "in": "=",
    "not in": "=",
    "ilike": "ILIKE",
}


def _quote_duckdb_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _plan_fact_references(
    plan: DatasetPlan,
    constraints: list[dict[str, Any]],
    source_ids: set[str],
) -> list[tuple[str, str, str, str]]:
    references: dict[tuple[str, str, str], str] = {}

    def add(source: Any, table: Any, column: Any, path: str) -> None:
        normalized_source = str(source or "").strip()
        if not normalized_source and len(source_ids) == 1:
            normalized_source = next(iter(source_ids))
        key = (
            normalized_source,
            str(table or "").strip(),
            str(column or "").strip(),
        )
        references.setdefault(key, path)

    for index, field in enumerate(plan.required_fields):
        add(field.source, field.table, field.column, f"required_fields[{index}]")
    for concept_index, concept in enumerate(plan.concepts):
        for field_index, field in enumerate(concept.fields):
            add(
                field.source,
                field.table,
                field.column,
                f"concepts[{concept_index}].fields[{field_index}]",
            )
    for constraint in constraints:
        add(
            constraint.get("source"),
            constraint.get("table"),
            constraint.get("column"),
            str(constraint.get("path") or "filters"),
        )
    return [
        (source, table, column, path)
        for (source, table, column), path in references.items()
    ]


def _normalized_value_constraints(
    plan: DatasetPlan,
    source_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    plan_sources = {
        str(field.source or "").strip()
        for field in [
            *plan.required_fields,
            *(field for concept in plan.concepts for field in concept.fields),
        ]
        if str(field.source or "").strip()
    }
    inferred_source = (
        next(iter(plan_sources))
        if len(plan_sources) == 1
        else next(iter(source_ids))
        if len(source_ids) == 1
        else ""
    )
    constraints: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for filter_index, filter_entry in enumerate(plan.filters):
        raw_constraints = filter_entry.get("value_constraints") or []
        if not isinstance(raw_constraints, list) or not raw_constraints:
            error = ToolExecutionError(
                "PLAN_FILTER_VALUE_UNAVAILABLE",
                "A requested filter has no structured value constraint to verify.",
                recoverable=True,
                details={"path": f"filters[{filter_index}]"},
            )
            issues.append(_plan_issue(error, path=f"filters[{filter_index}]"))
            continue
        for constraint_index, raw_constraint in enumerate(raw_constraints):
            path = f"filters[{filter_index}].value_constraints[{constraint_index}]"
            if not isinstance(raw_constraint, dict):
                error = ToolExecutionError(
                    "PLAN_FILTER_VALUE_UNAVAILABLE",
                    "Filter value constraint is not structured.",
                    recoverable=True,
                )
                issues.append(_plan_issue(error, path=path))
                continue
            constraint = dict(raw_constraint)
            constraint["path"] = path
            source = str(constraint.get("source") or "").strip()
            if not source and inferred_source:
                constraint["source"] = inferred_source
            table = str(constraint.get("table") or "").strip()
            column = str(constraint.get("column") or "").strip()
            operator = str(constraint.get("operator") or "").strip().casefold()
            has_value = "value" in constraint
            has_values = "values" in constraint
            values = constraint.get("values")
            valid_values = has_value != has_values and (
                has_value or isinstance(values, list) and bool(values)
            )
            if (
                not str(constraint.get("source") or "").strip()
                or not table
                or not column
                or operator not in _FILTER_OPERATOR_SQL
                or not valid_values
            ):
                error = ToolExecutionError(
                    "PLAN_FILTER_VALUE_UNAVAILABLE",
                    "Filter value constraint lacks a verifiable runtime field, operator, or value.",
                    recoverable=True,
                )
                issues.append(
                    _plan_issue(
                        error,
                        path=path,
                        source=str(constraint.get("source") or ""),
                        table=table,
                        column=column,
                    )
                )
                continue
            constraint["operator"] = operator
            constraints.append(constraint)
    return constraints, issues


def _filter_constraint_values(constraint: dict[str, Any]) -> list[Any]:
    if "values" in constraint:
        return list(constraint.get("values") or [])
    return [constraint.get("value")]


def _filter_value_exists(
    *,
    database_path: Path,
    table: str,
    column: str,
    operator: str,
    value: Any,
) -> bool:
    operator_sql = _FILTER_OPERATOR_SQL[operator]
    table_sql = _quote_duckdb_identifier(table)
    column_sql = _quote_duckdb_identifier(column)
    with duckdb.connect(str(database_path), read_only=True) as connection:
        connection.execute(
            f"SELECT 1 FROM {table_sql} "
            f"WHERE {column_sql} {operator_sql} ? LIMIT 0",
            [value],
        )
        return (
            connection.execute(
                f"SELECT 1 FROM {table_sql} "
                f"WHERE {column_sql} IS NOT DISTINCT FROM ? LIMIT 1",
                [value],
            ).fetchone()
            is not None
        )


def _source_database_path(study: Any, source_name: str) -> Path:
    source = _require_source_for_study(study, source_name)
    path = getattr(source, "path", None)
    if path is None:
        raise ToolExecutionError(
            "PLAN_FILTER_VALUE_UNAVAILABLE",
            f"Runtime source does not expose a read-only DuckDB path: {source_name}.",
            recoverable=True,
        )
    return Path(path)


def _required_plan_tables(
    plan: DatasetPlan,
    constraints: list[dict[str, Any]],
    source_ids: set[str],
) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}

    def add(source: Any, table: Any) -> None:
        source_name = str(source or "").strip()
        if not source_name and len(source_ids) == 1:
            source_name = next(iter(source_ids))
        table_name = str(table or "").strip()
        if source_name and table_name:
            tables.setdefault(source_name, set()).add(table_name)

    for field in [
        *plan.required_fields,
        *(field for concept in plan.concepts for field in concept.fields),
    ]:
        add(field.source, field.table)
    for constraint in constraints:
        add(constraint.get("source"), constraint.get("table"))
    return tables


def _join_profile_edges(
    plan: DatasetPlan,
    study: Any,
    source_name: str,
) -> list[dict[str, Any]]:
    inventory = _relationship_inventory_for_study(study, source_name)
    profiles: list[Any] = []
    for operation in plan.operations:
        if operation.name.strip().casefold() != "join":
            continue
        if str(operation.source or source_name).strip() != source_name:
            continue
        left_table = str(operation.left_table or "").strip()
        right_table = str(operation.right_table or "").strip()
        pairs = [
            (pair.left_column, pair.right_column)
            for pair in operation.key_pairs
            if pair.left_column and pair.right_column
        ]
        if not left_table or not right_table or not pairs:
            continue
        try:
            profile = inventory.profile_relationship(left_table, right_table, pairs)
        except (KeyError, ValueError, duckdb.Error):
            continue
        if profile.matched_keys > 0:
            profiles.append(profile)
    try:
        profiles.extend(inventory.candidate_relationships())
    except (KeyError, ValueError, duckdb.Error):
        pass
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for profile in profiles:
        if profile.matched_keys <= 0:
            continue
        key_pairs = tuple(
            (str(left), str(right))
            for left, right in profile.key_pairs
        )
        key = (profile.left_table, profile.right_table, key_pairs)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            {
                "source": source_name,
                "left_table": profile.left_table,
                "right_table": profile.right_table,
                "key_pairs": [
                    {
                        "left_column": left,
                        "right_column": right,
                    }
                    for left, right in key_pairs
                ],
                "profile": profile,
            }
        )
    return edges


def _verified_join_paths(
    plan: DatasetPlan,
    study: Any,
) -> list[dict[str, Any]]:
    source_ids = set(study.data_sources)
    constraints, _constraint_issues = _normalized_value_constraints(plan, source_ids)
    required_tables = _required_plan_tables(plan, constraints, source_ids)
    if len(required_tables) != 1:
        return []
    source_name, tables = next(iter(required_tables.items()))
    if len(tables) < 2:
        return []
    edges = _join_profile_edges(plan, study, source_name)
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {
        table: [] for table in tables
    }
    for edge in edges:
        left = edge["left_table"]
        right = edge["right_table"]
        adjacency.setdefault(left, []).append((right, edge))
        adjacency.setdefault(right, []).append(
            (
                left,
                {
                    **edge,
                    "left_table": right,
                    "right_table": left,
                    "key_pairs": [
                        {
                            "left_column": pair["right_column"],
                            "right_column": pair["left_column"],
                        }
                        for pair in edge["key_pairs"]
                    ],
                },
            )
        )
    root = sorted(tables)[0]
    paths: list[dict[str, Any]] = []
    for target in sorted(tables - {root}):
        queue = deque([(root, [])])
        visited = {root}
        found: list[dict[str, Any]] | None = None
        while queue:
            current, path_edges = queue.popleft()
            if current == target:
                found = path_edges
                break
            if len(path_edges) >= 3:
                continue
            for neighbor, edge in sorted(
                adjacency.get(current, []),
                key=lambda item: (item[0], item[1]["left_table"], item[1]["right_table"]),
            ):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append((neighbor, [*path_edges, edge]))
        if found is None:
            return []
        paths.extend(found)
    unique_paths: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for edge in paths:
        key = (
            edge["left_table"],
            edge["right_table"],
            tuple(
                (pair["left_column"], pair["right_column"])
                for pair in edge["key_pairs"]
            ),
        )
        if key not in seen_paths:
            seen_paths.add(key)
            unique_paths.append(edge)
    return unique_paths


def _validate_dataset_plan(
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    reference = ArtifactRef(
        id=arguments["plan_id"],
        kind="dataset_plan",
        version=arguments["plan_version"],
    )
    stored = _require_artifact(
        context,
        artifact_id=reference.id,
        version=reference.version,
        kind=reference.kind,
    )
    try:
        plan = dataset_plan_from_artifact(stored)
    except ValueError as error:
        code = str(error).split(":", 1)[0]
        raise ToolExecutionError(
            code,
            str(error),
            recoverable=True,
        ) from error
    study = _require_plan_study(context, plan)
    source_ids = set(study.data_sources)
    constraints, issues = _normalized_value_constraints(plan, source_ids)

    if not any(concept.fields for concept in plan.concepts):
        error = ToolExecutionError(
            "PLAN_FIELD_UNAVAILABLE",
            "Dataset plan contains no requested concept field to validate.",
            recoverable=True,
        )
        issues.append(_plan_issue(error, path="concepts"))

    for source, table, column, path in _plan_fact_references(
        plan,
        constraints,
        source_ids,
    ):
        details = {"source": source, "table": table, "column": column}
        if source not in source_ids:
            error = ToolExecutionError(
                "PLAN_FIELD_UNAVAILABLE",
                f"Runtime source is unavailable: {source}.",
                recoverable=True,
                details={**details, "reason": "source"},
            )
            issues.append(_plan_issue(error, path=path))
            continue
        if not table or not column or not _catalog_field_exists_for_study(
            study,
            table,
            column,
        ):
            error = ToolExecutionError(
                "PLAN_FIELD_UNAVAILABLE",
                f"Runtime field is unavailable: {table}.{column}.",
                recoverable=True,
                details={**details, "reason": "table_or_column"},
            )
            issues.append(_plan_issue(error, path=path))

    for constraint in constraints:
        source = str(constraint.get("source") or "").strip()
        table = str(constraint.get("table") or "").strip()
        column = str(constraint.get("column") or "").strip()
        operator = str(constraint.get("operator") or "").strip().casefold()
        path = str(constraint.get("path") or "filters")
        try:
            database_path = _source_database_path(study, source)
            for value_index, value in enumerate(_filter_constraint_values(constraint)):
                if not _filter_value_exists(
                    database_path=database_path,
                    table=table,
                    column=column,
                    operator=operator,
                    value=value,
                ):
                    error = ToolExecutionError(
                        "PLAN_FILTER_VALUE_UNAVAILABLE",
                        f"Stored filter value is unavailable for {table}.{column}.",
                        recoverable=True,
                        details={
                            "source": source,
                            "table": table,
                            "column": column,
                            "operator": operator,
                            "value_index": value_index,
                        },
                    )
                    issues.append(_plan_issue(error, path=path))
        except (ToolExecutionError, duckdb.Error, OSError) as error:
            if isinstance(error, ToolExecutionError):
                tool_error = error
            else:
                tool_error = ToolExecutionError(
                    "PLAN_FILTER_VALUE_UNAVAILABLE",
                    f"Filter value could not be checked for {table}.{column}.",
                    recoverable=True,
                    details={
                        "source": source,
                        "table": table,
                        "column": column,
                        "operator": operator,
                    },
                )
            issues.append(_plan_issue(tool_error, path=path))

    if not any(issue["code"] == "PLAN_FIELD_UNAVAILABLE" for issue in issues):
        required_tables = _required_plan_tables(plan, constraints, source_ids)
        if len(required_tables) > 1:
            error = ToolExecutionError(
                "PLAN_JOIN_UNAVAILABLE",
                "Required fields and filters span multiple runtime sources.",
                recoverable=True,
                details={"sources": sorted(required_tables)},
            )
            issues.append(_plan_issue(error, path="tables"))
        elif required_tables:
            source_name, tables = next(iter(required_tables.items()))
            if len(tables) > 1:
                try:
                    paths = _verified_join_paths(plan, study)
                except ToolExecutionError as error:
                    paths = []
                    issues.append(_plan_issue(error, path="operations"))
                if not paths:
                    error = ToolExecutionError(
                        "PLAN_JOIN_UNAVAILABLE",
                        "Required runtime tables have no observed non-null join path.",
                        recoverable=True,
                        details={
                            "source": source_name,
                            "tables": sorted(tables),
                        },
                    )
                    issues.append(_plan_issue(error, path="operations"))

    _raise_collected_plan_errors(issues)
    return ToolResult(
        message=(
            f"Dataset plan version {reference.version} passed runtime fact "
            "validation (fields, filter values, and join feasibility)."
        ),
        artifacts=(reference,),
    )


def _approved_plan(
    context: ToolContext,
    *,
    plan_id: str,
    plan_version: int,
) -> tuple[Any, DatasetPlan]:
    stored = _require_artifact(
        context,
        artifact_id=plan_id,
        version=plan_version,
        kind="dataset_plan",
    )
    if stored.status != "approved":
        raise ToolExecutionError(
            "PLAN_VERSION_NOT_APPROVED",
            (
                f"Dataset plan {plan_id} version {plan_version} has "
                f"status={stored.status}; exact approval is required."
            ),
            recoverable=True,
        )
    try:
        plan = dataset_plan_from_artifact(stored)
    except ValueError as error:
        raise ToolExecutionError(
            "PLAN_STUDY_LINEAGE_INVALID",
            str(error),
            recoverable=True,
        ) from error
    _require_plan_study(context, plan)
    return stored, plan


def _plan_columns(plan: DatasetPlan) -> list[dict[str, Any]]:
    return [
        field.model_dump(mode="json")
        for field in _plan_output_fields(plan)
    ]


def _plan_reference_columns(plan: DatasetPlan) -> list[dict[str, Any]]:
    return [
        {"source": source, "table": table, "column": column}
        for source, table, column in sorted(_plan_reference_keys(plan))
        if source and table and column
    ]


def _plan_row_filters(plan: DatasetPlan) -> dict[str, Any]:
    try:
        resolved = resolve_filter_references(
            plan.filters,
            available_fields=_plan_reference_keys(plan),
        )
    except FilterReferenceResolutionError as error:
        raise ToolExecutionError(
            error.code,
            str(error),
            recoverable=True,
        ) from error
    return {
        "population": [],
        "data_quality": [],
        "explicit_value": resolved,
    }


def _plan_protected_columns(plan: DatasetPlan) -> list[dict[str, Any]]:
    protected: list[dict[str, Any]] = []
    for concept in plan.concepts:
        content = concept.model_dump(mode="json")
        concept_role = str(
            content.get("role") or content.get("variable_role") or ""
        ).strip().casefold()
        for field in content.get("fields") or []:
            if not isinstance(field, dict):
                continue
            if concept_role == "outcome" or bool(field.get("protected")):
                protected.append(dict(field))
    return protected


def _plan_runtime_path(context: ToolContext, plan: DatasetPlan) -> Any:
    sources = {
        str(column.get("source") or "").strip()
        for column in _plan_columns(plan)
        if str(column.get("source") or "").strip()
    }
    if len(sources) != 1:
        raise ToolExecutionError(
            "SQL_SOURCE_UNAVAILABLE",
            "Agent SQL execution currently requires one approved runtime source.",
            recoverable=True,
        )
    study = _require_plan_study(context, plan)
    source = _require_source_for_study(study, next(iter(sources)))
    path = getattr(source, "path", None)
    if path is None:
        raise ToolExecutionError(
            "SQL_SOURCE_UNAVAILABLE",
            "The approved runtime source does not provide DuckDB execution.",
            recoverable=True,
        )
    return path


def _sql_repair_details(
    *,
    plan_id: str,
    plan_version: int,
    origin: str,
    stage: str,
    attempted_sql: str | None,
    error: Exception,
    tables: list[str],
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "repair_required",
        "plan": {"id": plan_id, "version": plan_version},
        "origin": origin,
        "stage": stage,
        "attempted_sql": str(attempted_sql or "")[:12_000],
        "diagnostic": f"{type(error).__name__}: {error}"[:2_000],
        "allowed_tables": list(tables[:50]),
        "allowed_columns": [dict(column) for column in columns[:200]],
    }


def _validated_sql_artifact(
    context: ToolContext,
    *,
    plan_id: str,
    plan_version: int,
    plan: DatasetPlan,
    validated: ValidatedExtractionSql,
    origin: str,
    tables: list[str],
    columns: list[dict[str, Any]],
) -> Any:
    existing = next(
        (
            artifact
            for artifact in _store(context).list_artifacts(
                kind="validated_sql"
            )
            if artifact.status == "approved"
            and artifact.content.get("status") == "validated"
            and artifact.content.get("plan_id") == plan_id
            and artifact.content.get("plan_version") == plan_version
            and artifact.content.get("sql_sha256") == validated.sha256
            and artifact.content.get("sql") == validated.sql
        ),
        None,
    )
    if existing is not None:
        if existing.provenance.get("study_id") != plan.study_id:
            raise ToolExecutionError(
                "STUDY_REFERENCE_MISMATCH",
                "Validated SQL and its dataset plan identify different studies.",
                recoverable=False,
            )
        return existing

    row_filters = _plan_row_filters(plan)
    protected_columns = _plan_protected_columns(plan)
    reference = _store(context).save_artifact(
        kind="validated_sql",
        status="approved",
        content={
            "status": "validated",
            "origin": origin,
            "sql": validated.sql,
            "sql_sha256": validated.sha256,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "question": plan.goal,
            "selection_id": plan_id,
            "tables": list(tables),
            "columns": [dict(column) for column in columns],
            "operations": [
                operation.model_dump(mode="json")
                for operation in plan.operations
            ],
            "row_filters": row_filters,
            "protected_columns": protected_columns,
            "applied_filters": [],
        },
        provenance={
            "study_id": plan.study_id,
            "thread_id": context.thread_id,
            "producer": "dbrag-validate_and_extract",
            "plan": {"id": plan_id, "version": plan_version},
            "origin": origin,
            "sql_sha256": validated.sha256,
        },
        summary=(
            f"Validated SQL for dataset plan {plan_id} "
            f"version {plan_version}"
        ),
    )
    return _require_artifact(
        context,
        artifact_id=reference.id,
        version=reference.version,
        kind="validated_sql",
    )


def _validate_and_extract(
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    plan_id = arguments["plan_id"]
    plan_version = int(arguments["plan_version"])
    stored_plan, plan = _approved_plan(
        context,
        plan_id=plan_id,
        plan_version=plan_version,
    )
    columns = _plan_columns(plan)
    reference_columns = _plan_reference_columns(plan)
    tables = list(
        dict.fromkeys(
            str(column.get("table") or "").strip()
            for column in reference_columns
            if str(column.get("table") or "").strip()
        )
    )
    runtime_path = _plan_runtime_path(context, plan)
    supplied_sql = str(arguments.get("sql") or "").strip()
    origin = "llm_repair" if supplied_sql else "deterministic_compiler"
    attempted_sql: str | None = supplied_sql or None
    try:
        if attempted_sql is None:
            attempted_sql = compile_dataset_plan_sql(plan)
        validated = validate_extraction_sql(
            sql=attempted_sql,
            approved_tables=tables,
            approved_columns=reference_columns,
            database_path=runtime_path,
        )
    except (KeyError, ValueError) as error:
        raise ToolExecutionError(
            "SQL_REPAIR_REQUIRED",
            (
                "The SQL candidate requires technical repair against the "
                "frozen approved plan."
            ),
            recoverable=True,
            details=_sql_repair_details(
                plan_id=plan_id,
                plan_version=plan_version,
                origin=origin,
                stage="validation",
                attempted_sql=attempted_sql,
                error=error,
                tables=tables,
                columns=reference_columns,
            ),
        ) from error
    sql_artifact = _validated_sql_artifact(
        context,
        plan_id=plan_id,
        plan_version=plan_version,
        plan=plan,
        validated=validated,
        origin=origin,
        tables=tables,
        columns=columns,
    )
    result = _persist_extraction_result(
        arguments,
        context,
        stored_plan=stored_plan,
        plan=plan,
        sql_artifact=sql_artifact,
        validated=validated,
        execution=None,
    )
    sql_reference = ArtifactRef(
        id=sql_artifact.id,
        kind=sql_artifact.kind,
        version=sql_artifact.version,
    )
    return ToolResult(
        message=result.message,
        artifacts=(*result.artifacts, sql_reference),
        terminal_control=result.terminal_control,
    )


def _replacement_predecessor(
    arguments: dict[str, Any],
    context: ToolContext,
    *,
    plan_id: str,
    plan_version: int,
) -> tuple[Any, ArtifactRef] | None:
    predecessor_id = arguments.get("predecessor_dataset_id")
    predecessor_version = arguments.get("predecessor_dataset_version")
    if (predecessor_id is None) != (predecessor_version is None):
        raise ToolExecutionError(
            "INVALID_ARGUMENTS",
            (
                "predecessor_dataset_id and predecessor_dataset_version "
                "must be provided together."
            ),
            recoverable=True,
        )
    if predecessor_id is None:
        return None
    predecessor = _require_artifact(
        context,
        artifact_id=str(predecessor_id),
        version=int(predecessor_version),
    )
    if predecessor.kind not in _DATASET_KINDS:
        raise ToolExecutionError(
            "PREDECESSOR_NOT_DATASET",
            "Replacement predecessor must be an extracted dataset.",
            recoverable=True,
        )
    if predecessor.status != "pending_review":
        raise ToolExecutionError(
            "PREDECESSOR_NOT_PENDING_REVIEW",
            "Replacement requires an exact pending-review predecessor dataset.",
            recoverable=True,
        )
    predecessor_provenance = dict(
        predecessor.content.get("provenance") or {}
    )
    if (
        predecessor_provenance.get("plan_id") != plan_id
        or predecessor_provenance.get("plan_version") != plan_version
    ):
        raise ToolExecutionError(
            "PREDECESSOR_PLAN_MISMATCH",
            "Predecessor dataset does not belong to the exact approved plan.",
            recoverable=True,
        )
    list_artifacts = getattr(context.artifact_store, "list_artifacts", None)
    if not callable(list_artifacts):
        raise ToolExecutionError(
            "ARTIFACT_STORE_UNAVAILABLE",
            "The artifact store cannot inspect dataset feedback lineage.",
            recoverable=False,
        )
    feedback = next(
        (
            artifact
            for artifact in reversed(
                list(list_artifacts(kind="dataset_review_feedback"))
            )
            if artifact.content.get("action") == "revise"
            and artifact.content.get("dataset_id") == predecessor.id
            and artifact.content.get("dataset_version") == predecessor.version
            and artifact.content.get("plan_id") == plan_id
            and artifact.content.get("plan_version") == plan_version
            and artifact.content.get("sql_id")
            == predecessor_provenance.get("sql_id")
            and artifact.content.get("sql_version")
            == predecessor_provenance.get("sql_version")
            and artifact.status == "active"
        ),
        None,
    )
    if feedback is None:
        raise ToolExecutionError(
            "PREDECESSOR_FEEDBACK_REQUIRED",
            "Replacement requires matching human feedback for the predecessor.",
            recoverable=True,
        )
    return predecessor, ArtifactRef(
        id=feedback.id,
        kind=feedback.kind,
        version=feedback.version,
    )


def _predecessor_identity(
    arguments: dict[str, Any],
) -> tuple[str, int] | None:
    predecessor_id = arguments.get("predecessor_dataset_id")
    predecessor_version = arguments.get("predecessor_dataset_version")
    if (predecessor_id is None) != (predecessor_version is None):
        raise ToolExecutionError(
            "INVALID_ARGUMENTS",
            (
                "predecessor_dataset_id and predecessor_dataset_version "
                "must be provided together."
            ),
            recoverable=True,
        )
    if predecessor_id is None:
        return None
    return str(predecessor_id), int(predecessor_version)


def _deterministic_agent_dataset_id(
    *,
    thread_id: str,
    plan_id: str,
    plan_version: int,
    sql_id: str,
    sql_version: int,
    predecessor: tuple[str, int] | None,
) -> str:
    identity = {
        "thread_id": thread_id,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "sql_id": sql_id,
        "sql_version": sql_version,
        "predecessor_dataset_id": predecessor[0] if predecessor else None,
        "predecessor_dataset_version": predecessor[1] if predecessor else None,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"subset-agent-{digest[:20]}"


def _canonical_content_sha256(content: dict[str, Any]) -> str:
    canonical = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_sql_output_aliases(sql: str) -> list[str]:
    try:
        import sqlglot
        from sqlglot import exp

        expression = sqlglot.parse_one(sql, dialect="duckdb")
        select = (
            expression
            if isinstance(expression, exp.Select)
            else expression.find(exp.Select)
        )
        aliases = [
            str(projection.alias_or_name or "").strip()
            for projection in list(select.expressions if select else [])
        ]
    except Exception as error:
        raise ToolExecutionError(
            "SQL_NOT_VALIDATED",
            "Validated SQL artifact has an unreadable projection contract.",
            recoverable=False,
        ) from error
    if not aliases or any(not alias for alias in aliases):
        raise ToolExecutionError(
            "SQL_NOT_VALIDATED",
            "Validated SQL artifact has an incomplete projection contract.",
            recoverable=False,
        )
    return aliases


def _projected_aliases_are_covered(
    expected: list[str],
    physical: list[str],
) -> bool:
    expected_names = {
        str(name).strip()
        for name in expected
        if str(name).strip()
    }
    physical_names = {
        str(name).strip()
        for name in physical
        if str(name).strip()
    }
    return bool(expected_names) and expected_names.issubset(physical_names)


def _dataset_persistence_lineage(
    context: ToolContext,
    *,
    study_id: str,
    plan_id: str,
    plan_version: int,
    plan_content: dict[str, Any],
    sql_id: str,
    sql_version: int,
    sql_content: dict[str, Any],
    expected_output_aliases: list[str],
    approved_selected_tables: list[str],
    approved_selected_columns: list[dict[str, Any]],
    predecessor: tuple[str, int] | None,
) -> dict[str, Any]:
    return {
        "study_id": str(study_id).strip(),
        "thread_id": context.thread_id,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "plan_content_sha256": _canonical_content_sha256(plan_content),
        "sql_id": sql_id,
        "sql_version": sql_version,
        "sql_content_sha256": _canonical_content_sha256(sql_content),
        "expected_output_aliases": list(expected_output_aliases),
        "approved_selected_tables": list(approved_selected_tables),
        "approved_selected_columns": [
            dict(column) for column in approved_selected_columns
        ],
        "predecessor_dataset_id": predecessor[0] if predecessor else None,
        "predecessor_dataset_version": predecessor[1] if predecessor else None,
    }


def _dataset_result(
    dataset: Any,
    *,
    reused: bool,
    replacement: dict[str, Any] | None,
) -> ToolResult:
    reference = ArtifactRef(
        id=dataset.id,
        kind=dataset.kind,
        version=dataset.version,
    )
    suffix = " reused=true." if reused else "."
    if replacement:
        suffix = (
            f" predecessor_dataset_id={replacement['predecessor_id']} "
            f"predecessor_status=superseded reused={str(reused).lower()}."
        )
    return ToolResult(
        message=(
            f"{'Reused' if reused else 'Executed'} validated SQL "
            f"dataset_id={dataset.id} version={dataset.version} "
            f"status=pending_review rows={dataset.content.get('row_count', 0)} "
            f"columns={dataset.content.get('column_count', 0)}{suffix}"
        ),
        artifacts=(reference,),
    )


def _persistence_store_methods(context: ToolContext) -> dict[str, Callable[..., Any]]:
    methods = {
        name: getattr(context.artifact_store, name, None)
        for name in (
            "get_dataset_persistence_attempt",
            "begin_dataset_persistence_attempt",
            "advance_dataset_persistence_attempt",
            "commit_dataset_persistence_attempt",
        )
    }
    if not all(callable(method) for method in methods.values()):
        raise ToolExecutionError(
            "ARTIFACT_STORE_UNAVAILABLE",
            "The artifact store does not support durable dataset persistence attempts.",
            recoverable=False,
        )
    return methods  # type: ignore[return-value]


def _replacement_control(
    replacement: tuple[Any, ArtifactRef] | None,
) -> dict[str, Any] | None:
    if replacement is None:
        return None
    predecessor, feedback = replacement
    return {
        "predecessor_id": predecessor.id,
        "predecessor_kind": predecessor.kind,
        "predecessor_version": predecessor.version,
        "feedback_id": feedback.id,
        "feedback_kind": feedback.kind,
        "feedback_version": feedback.version,
    }


def _attempt_commit_references(
    attempt: dict[str, Any],
) -> tuple[ArtifactRef | None, ArtifactRef | None]:
    replacement = attempt.get("replacement")
    if not isinstance(replacement, dict):
        return None, None
    return (
        ArtifactRef(
            id=str(replacement["predecessor_id"]),
            kind=str(replacement["predecessor_kind"]),
            version=int(replacement["predecessor_version"]),
        ),
        ArtifactRef(
            id=str(replacement["feedback_id"]),
            kind=str(replacement["feedback_kind"]),
            version=int(replacement["feedback_version"]),
        ),
    )


def _verify_attempt_final_files(attempt: dict[str, Any]) -> None:
    try:
        verify_dataset_artifact_manifest(
            paths=dict(attempt.get("expected_final_paths") or {}),
            manifest=dict(attempt.get("manifest") or {}),
        )
    except ValueError as error:
        raise ToolExecutionError(
            "DATASET_DURABLE_COLLISION",
            str(error),
            recoverable=False,
        ) from error
    except OSError as error:
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_PENDING",
            f"Durable manifest could not be read: {type(error).__name__}.",
            recoverable=True,
        ) from error


def _durable_collision(message: str) -> ToolExecutionError:
    return ToolExecutionError(
        "DATASET_DURABLE_COLLISION",
        message,
        recoverable=False,
    )


def _validate_attempt_replacement_control(
    context: ToolContext,
    *,
    attempt: dict[str, Any],
    lineage: dict[str, Any],
) -> None:
    predecessor_id = lineage.get("predecessor_dataset_id")
    predecessor_version = lineage.get("predecessor_dataset_version")
    replacement = attempt.get("replacement")
    if predecessor_id is None and predecessor_version is None:
        if replacement is not None:
            raise _durable_collision(
                "Ordinary dataset persistence has replacement control."
            )
        return
    if predecessor_id is None or predecessor_version is None:
        raise _durable_collision(
            "Replacement dataset lineage has an incomplete predecessor."
        )
    if not isinstance(replacement, dict) or (
        replacement.get("predecessor_id") != predecessor_id
        or replacement.get("predecessor_version") != predecessor_version
    ):
        raise _durable_collision(
            "Replacement control does not match predecessor lineage."
        )
    try:
        predecessor = context.artifact_store.require(
            ArtifactRef(
                id=str(replacement["predecessor_id"]),
                kind=str(replacement["predecessor_kind"]),
                version=int(replacement["predecessor_version"]),
            )
        )
        feedback = context.artifact_store.require(
            ArtifactRef(
                id=str(replacement["feedback_id"]),
                kind=str(replacement["feedback_kind"]),
                version=int(replacement["feedback_version"]),
            )
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _durable_collision(
            "Replacement control references unavailable artifacts."
        ) from error
    predecessor_provenance = dict(
        predecessor.content.get("provenance") or {}
    )
    feedback_content = dict(feedback.content)
    if (
        predecessor.id != predecessor_id
        or predecessor.version != predecessor_version
        or predecessor.kind != replacement.get("predecessor_kind")
        or predecessor.status not in {"pending_review", "superseded"}
        or predecessor_provenance.get("plan_id") != lineage.get("plan_id")
        or predecessor_provenance.get("plan_version")
        != lineage.get("plan_version")
        or feedback.id != replacement.get("feedback_id")
        or feedback.version != replacement.get("feedback_version")
        or feedback.kind != "dataset_review_feedback"
        or feedback.kind != replacement.get("feedback_kind")
        or feedback.status != "active"
        or feedback_content.get("action") != "revise"
        or not str(feedback_content.get("feedback") or "").strip()
        or feedback_content.get("dataset_id") != predecessor.id
        or feedback_content.get("dataset_version") != predecessor.version
        or feedback_content.get("plan_id") != lineage.get("plan_id")
        or feedback_content.get("plan_version") != lineage.get("plan_version")
        or feedback_content.get("sql_id")
        != predecessor_provenance.get("sql_id")
        or feedback_content.get("sql_version")
        != predecessor_provenance.get("sql_version")
    ):
        raise _durable_collision(
            "Replacement predecessor or feedback lineage is inconsistent."
        )


def _validate_canonical_dataset_lineage(
    artifact: dict[str, Any],
    *,
    lineage: dict[str, Any],
    validated_sql: str,
) -> None:
    provenance = dict(artifact.get("provenance") or {})
    expected = {
        "study_id": lineage.get("study_id"),
        "thread_id": lineage.get("thread_id"),
        "plan_id": lineage.get("plan_id"),
        "plan_version": lineage.get("plan_version"),
        "sql_id": lineage.get("sql_id"),
        "sql_version": lineage.get("sql_version"),
    }
    if any(provenance.get(key) != value for key, value in expected.items()):
        raise _durable_collision(
            "Canonical dataset provenance does not match persistence lineage."
        )
    if (
        provenance.get("selection_artifact_id") != lineage.get("plan_id")
        or provenance.get("sql_candidate_artifact_id")
        != lineage.get("sql_id")
    ):
        raise _durable_collision(
            "Canonical dataset artifact references do not match lineage."
        )
    if provenance.get("sql") != validated_sql:
        raise _durable_collision(
            "Canonical dataset SQL does not match the validated SQL artifact."
        )
    expected_tables = list(lineage.get("approved_selected_tables") or [])
    expected_columns = list(
        lineage.get("approved_selected_columns") or []
    )
    if (
        provenance.get("source_tables") != expected_tables
        or provenance.get("selected_tables") != expected_tables
        or provenance.get("selected_columns") != expected_columns
    ):
        raise _durable_collision(
            "Canonical dataset selected fields do not match the approved plan."
        )
    physical_columns = [
        str(column).strip()
        for column in list(artifact.get("columns") or [])
    ]
    expected_aliases = [
        str(alias).strip()
        for alias in list(lineage.get("expected_output_aliases") or [])
    ]
    if not _projected_aliases_are_covered(expected_aliases, physical_columns):
        raise _durable_collision(
            "Canonical dataset is missing a projected SQL alias."
        )
    predecessor_id = lineage.get("predecessor_dataset_id")
    predecessor_version = lineage.get("predecessor_dataset_version")
    if predecessor_id is None and predecessor_version is None:
        if (
            "predecessor_dataset_id" in provenance
            or "predecessor_dataset_version" in provenance
        ):
            raise _durable_collision(
                "Ordinary dataset metadata contains predecessor lineage."
            )
    elif (
        provenance.get("predecessor_dataset_id") != predecessor_id
        or provenance.get("predecessor_dataset_version")
        != predecessor_version
    ):
        raise _durable_collision(
            "Canonical dataset predecessor does not match persistence lineage."
        )


def _verify_canonical_attempt_dataset(
    context: ToolContext,
    *,
    attempt: dict[str, Any],
    lineage: dict[str, Any],
    validated_sql: str,
    runtime_root: Any,
) -> dict[str, Any]:
    try:
        canonical = load_verified_dataset_artifact(
            runtime_root=runtime_root,
            thread_id=context.thread_id,
            dataset_id=str(attempt["dataset_id"]),
            paths=dict(attempt.get("expected_final_paths") or {}),
            manifest=dict(attempt.get("manifest") or {}),
            expected_kind="subset",
            expected_version=1,
            expected_status="pending_review",
        )
    except (OSError, ValueError) as error:
        raise _durable_collision(str(error)) from error
    if canonical != dict(attempt.get("dataset") or {}):
        raise _durable_collision(
            "Persistence journal dataset snapshot does not match canonical metadata."
        )
    _validate_canonical_dataset_lineage(
        canonical,
        lineage=lineage,
        validated_sql=validated_sql,
    )
    _validate_attempt_replacement_control(
        context,
        attempt=attempt,
        lineage=lineage,
    )
    return {**attempt, "dataset": canonical}


def _write_attempt_journal(
    context: ToolContext,
    *,
    runtime_root: Any,
    attempt: dict[str, Any],
) -> None:
    try:
        write_dataset_persistence_journal(
            runtime_root=runtime_root,
            thread_id=context.thread_id,
            dataset_id=str(attempt["dataset_id"]),
            attempt=attempt,
        )
    except ValueError as error:
        raise ToolExecutionError(
            "DATASET_DURABLE_COLLISION",
            str(error),
            recoverable=False,
        ) from error
    except OSError as error:
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_PENDING",
            f"Persistence journal could not be written: {type(error).__name__}.",
            recoverable=True,
        ) from error


def _load_attempt_journal(
    context: ToolContext,
    *,
    runtime_root: Any,
    dataset_id: str,
) -> dict[str, Any] | None:
    try:
        return load_dataset_persistence_journal(
            runtime_root=runtime_root,
            thread_id=context.thread_id,
            dataset_id=dataset_id,
        )
    except ValueError as error:
        raise ToolExecutionError(
            "DATASET_DURABLE_COLLISION",
            str(error),
            recoverable=False,
        ) from error
    except OSError as error:
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_PENDING",
            f"Persistence journal could not be read: {type(error).__name__}.",
            recoverable=True,
        ) from error


def _attempt_base(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: attempt[key]
        for key in (
            "dataset_id",
            "state",
            "lineage",
            "expected_final_paths",
            "expected_staging_paths",
            "replacement",
        )
        if key in attempt
    }


def _require_attempt_identity(
    attempt: dict[str, Any],
    *,
    lineage: dict[str, Any],
    expected_final_paths: dict[str, str],
    expected_staging_paths: dict[str, str],
) -> None:
    if (
        dict(attempt.get("lineage") or {}) != lineage
        or dict(attempt.get("expected_final_paths") or {}) != expected_final_paths
        or dict(attempt.get("expected_staging_paths") or {})
        != expected_staging_paths
    ):
        raise ToolExecutionError(
            "DATASET_IDENTITY_COLLISION",
            "Dataset persistence attempt lineage or storage paths do not match.",
            recoverable=False,
        )


def _sync_attempt_from_journal(
    context: ToolContext,
    *,
    methods: dict[str, Callable[..., Any]],
    attempt: dict[str, Any] | None,
    journal: dict[str, Any],
    runtime_root: Any,
) -> dict[str, Any]:
    dataset_id = str(journal["dataset_id"])
    journal_base = _attempt_base(journal)
    journal_base["state"] = "begun"
    if attempt is None:
        attempt = methods["begin_dataset_persistence_attempt"](journal_base)
    elif _attempt_base({**attempt, "state": "begun"}) != journal_base:
        raise ToolExecutionError(
            "DATASET_IDENTITY_COLLISION",
            "Graph persistence attempt does not match its durable journal.",
            recoverable=False,
        )

    rank = {"begun": 0, "staged": 1, "promoted": 2, "committed": 3}
    graph_state = str(attempt.get("state") or "")
    journal_state = str(journal.get("state") or "")
    if graph_state not in rank or journal_state not in rank:
        raise ToolExecutionError(
            "DATASET_IDENTITY_COLLISION",
            "Persistence attempt or journal has an unknown state.",
            recoverable=False,
        )

    if rank[graph_state] > rank[journal_state]:
        expected = {**journal, "state": graph_state}
        if graph_state in {"staged", "promoted", "committed"}:
            expected["manifest"] = attempt.get("manifest")
            expected["dataset"] = attempt.get("dataset")
        if attempt != expected:
            raise ToolExecutionError(
                "DATASET_IDENTITY_COLLISION",
                "Graph persistence attempt is incompatible with its durable journal.",
                recoverable=False,
            )
        _write_attempt_journal(
            context,
            runtime_root=runtime_root,
            attempt=attempt,
        )
        return attempt

    if rank[journal_state] >= rank["staged"] and graph_state == "begun":
        attempt = methods["advance_dataset_persistence_attempt"](
            dataset_id,
            lineage=dict(journal["lineage"]),
            expected_state="begun",
            state="staged",
            manifest=dict(journal["manifest"]),
            dataset=dict(journal["dataset"]),
        )
        graph_state = "staged"
    if rank[journal_state] >= rank["promoted"] and graph_state == "staged":
        attempt = methods["advance_dataset_persistence_attempt"](
            dataset_id,
            lineage=dict(journal["lineage"]),
            expected_state="staged",
            state="promoted",
        )
        graph_state = "promoted"

    if journal_state == "committed" and graph_state == "promoted":
        return attempt
    if attempt != journal:
        raise ToolExecutionError(
            "DATASET_IDENTITY_COLLISION",
            "Graph persistence attempt state does not match its durable journal.",
            recoverable=False,
        )
    return attempt


def _prepare_journal_for_hydration(
    context: ToolContext,
    *,
    journal: dict[str, Any],
    lineage: dict[str, Any],
    validated_sql: str,
    expected_final_paths: dict[str, str],
    expected_staging_paths: dict[str, str],
    runtime_root: Any,
) -> dict[str, Any]:
    _validate_attempt_replacement_control(
        context,
        attempt=journal,
        lineage=lineage,
    )
    state = str(journal.get("state") or "")
    if state == "begun":
        return journal
    if state == "staged":
        try:
            promote_staged_dataset_artifact(
                runtime_root=runtime_root,
                thread_id=context.thread_id,
                dataset_id=str(journal["dataset_id"]),
                expected_final_paths=expected_final_paths,
                expected_staging_paths=expected_staging_paths,
                manifest=dict(journal.get("manifest") or {}),
            )
        except ValueError as error:
            raise _durable_collision(str(error)) from error
        except OSError as error:
            raise ToolExecutionError(
                "DATASET_PERSISTENCE_PENDING",
                f"Staged promotion could not complete: {type(error).__name__}.",
                recoverable=True,
            ) from error
        promoted = _verify_canonical_attempt_dataset(
            context,
            attempt={**journal, "state": "promoted"},
            lineage=lineage,
            validated_sql=validated_sql,
            runtime_root=runtime_root,
        )
        _write_attempt_journal(
            context,
            runtime_root=runtime_root,
            attempt=promoted,
        )
        return promoted
    return _verify_canonical_attempt_dataset(
        context,
        attempt=journal,
        lineage=lineage,
        validated_sql=validated_sql,
        runtime_root=runtime_root,
    )


def _committed_attempt_result(
    context: ToolContext,
    *,
    attempt: dict[str, Any],
    reused: bool,
) -> ToolResult:
    if attempt.get("state") != "committed":
        raise ToolExecutionError(
            "DATASET_IDENTITY_COLLISION",
            "Dataset persistence attempt is not committed.",
            recoverable=False,
        )
    _verify_attempt_final_files(attempt)
    try:
        dataset = context.artifact_store.require(str(attempt["dataset_id"]))
    except KeyError as error:
        raise ToolExecutionError(
            "DATASET_IDENTITY_COLLISION",
            "Committed persistence attempt has no registered dataset.",
            recoverable=False,
        ) from error
    if (
        dataset.content != dict(attempt.get("dataset") or {})
        or dataset.status != "pending_review"
    ):
        raise ToolExecutionError(
            "DATASET_IDENTITY_COLLISION",
            "Committed dataset registry does not match its persistence attempt.",
            recoverable=False,
        )
    replacement = attempt.get("replacement")
    if isinstance(replacement, dict):
        predecessor = context.artifact_store.require(
            ArtifactRef(
                id=str(replacement["predecessor_id"]),
                kind=str(replacement["predecessor_kind"]),
                version=int(replacement["predecessor_version"]),
            )
        )
        if predecessor.status != "superseded":
            raise ToolExecutionError(
                "DATASET_IDENTITY_COLLISION",
                "Committed replacement did not supersede its predecessor.",
                recoverable=False,
            )
    return _dataset_result(
        dataset,
        reused=reused,
        replacement=replacement if isinstance(replacement, dict) else None,
    )


def _commit_persistence_attempt(
    context: ToolContext,
    *,
    methods: dict[str, Callable[..., Any]],
    attempt: dict[str, Any],
    plan_id: str,
    plan_version: int,
    reused: bool,
    runtime_root: Any,
) -> ToolResult:
    predecessor_ref, feedback_ref = _attempt_commit_references(attempt)
    try:
        methods["commit_dataset_persistence_attempt"](
            str(attempt["dataset_id"]),
            lineage=dict(attempt["lineage"]),
            artifact=dict(attempt["dataset"]),
            plan_ref=ArtifactRef(
                id=plan_id,
                kind="dataset_plan",
                version=plan_version,
            ),
            predecessor_ref=predecessor_ref,
            feedback_ref=feedback_ref,
            provenance={
                "actor": "dbrag-validate_and_extract",
                "thread_id": context.thread_id,
            },
        )
    except Exception as error:
        refreshed = methods["get_dataset_persistence_attempt"](
            str(attempt["dataset_id"])
        )
        if isinstance(refreshed, dict) and refreshed.get("state") == "committed":
            expected_committed = {**attempt, "state": "committed"}
            if refreshed != expected_committed:
                raise ToolExecutionError(
                    "DATASET_IDENTITY_COLLISION",
                    "Committed persistence response has mismatched attempt metadata.",
                    recoverable=False,
                ) from error
            _write_attempt_journal(
                context,
                runtime_root=runtime_root,
                attempt=refreshed,
            )
            return _committed_attempt_result(
                context,
                attempt=refreshed,
                reused=reused,
            )
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_PENDING",
            (
                f"Dataset persistence is tracked but not committed after "
                f"{type(error).__name__}; replay the exact tool call."
            ),
            recoverable=True,
        ) from error
    committed = methods["get_dataset_persistence_attempt"](
        str(attempt["dataset_id"])
    )
    if not isinstance(committed, dict) or committed.get("state") != "committed":
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_PENDING",
            "Dataset commit response did not include a committed persistence attempt.",
            recoverable=True,
        )
    if committed != {**attempt, "state": "committed"}:
        raise ToolExecutionError(
            "DATASET_IDENTITY_COLLISION",
            "Committed persistence attempt metadata changed unexpectedly.",
            recoverable=False,
        )
    _write_attempt_journal(
        context,
        runtime_root=runtime_root,
        attempt=committed,
    )
    return _committed_attempt_result(
        context,
        attempt=committed,
        reused=reused,
    )


def _reconcile_persistence_attempt(
    context: ToolContext,
    *,
    methods: dict[str, Callable[..., Any]],
    attempt: dict[str, Any],
    lineage: dict[str, Any],
    validated_sql: str,
    expected_final_paths: dict[str, str],
    expected_staging_paths: dict[str, str],
    runtime_root: Any,
    plan_id: str,
    plan_version: int,
) -> ToolResult | None:
    _require_attempt_identity(
        attempt,
        lineage=lineage,
        expected_final_paths=expected_final_paths,
        expected_staging_paths=expected_staging_paths,
    )
    _validate_attempt_replacement_control(
        context,
        attempt=attempt,
        lineage=lineage,
    )
    state = str(attempt.get("state") or "")
    if state == "committed":
        attempt = _verify_canonical_attempt_dataset(
            context,
            attempt=attempt,
            lineage=lineage,
            validated_sql=validated_sql,
            runtime_root=runtime_root,
        )
        return _committed_attempt_result(
            context,
            attempt=attempt,
            reused=True,
        )
    if state == "promoted":
        attempt = _verify_canonical_attempt_dataset(
            context,
            attempt=attempt,
            lineage=lineage,
            validated_sql=validated_sql,
            runtime_root=runtime_root,
        )
        return _commit_persistence_attempt(
            context,
            methods=methods,
            attempt=attempt,
            plan_id=plan_id,
            plan_version=plan_version,
            reused=True,
            runtime_root=runtime_root,
        )
    if state == "staged":
        try:
            promote_staged_dataset_artifact(
                runtime_root=runtime_root,
                thread_id=context.thread_id,
                dataset_id=str(attempt["dataset_id"]),
                expected_final_paths=expected_final_paths,
                expected_staging_paths=expected_staging_paths,
                manifest=dict(attempt.get("manifest") or {}),
            )
        except ValueError as error:
            raise ToolExecutionError(
                "DATASET_DURABLE_COLLISION",
                str(error),
                recoverable=False,
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "DATASET_PERSISTENCE_PENDING",
                f"Staged promotion could not complete: {type(error).__name__}.",
                recoverable=True,
            ) from error
        promoted_attempt = {**attempt, "state": "promoted"}
        promoted_attempt = _verify_canonical_attempt_dataset(
            context,
            attempt=promoted_attempt,
            lineage=lineage,
            validated_sql=validated_sql,
            runtime_root=runtime_root,
        )
        _write_attempt_journal(
            context,
            runtime_root=runtime_root,
            attempt=promoted_attempt,
        )
        try:
            attempt = methods["advance_dataset_persistence_attempt"](
                str(attempt["dataset_id"]),
                lineage=lineage,
                expected_state="staged",
                state="promoted",
            )
        except Exception as error:
            raise ToolExecutionError(
                "DATASET_PERSISTENCE_PENDING",
                (
                    f"Promoted dataset remains tracked after "
                    f"{type(error).__name__}; replay the exact tool call."
                ),
                recoverable=True,
            ) from error
        return _commit_persistence_attempt(
            context,
            methods=methods,
            attempt=attempt,
            plan_id=plan_id,
            plan_version=plan_version,
            reused=True,
            runtime_root=runtime_root,
        )
    if state == "begun":
        if any(Path(path).exists() for path in expected_final_paths.values()):
            raise ToolExecutionError(
                "DATASET_DURABLE_COLLISION",
                "Begun persistence attempt has unverified final files.",
                recoverable=False,
            )
        cleanup_dataset_staging(
            runtime_root=runtime_root,
            thread_id=context.thread_id,
            dataset_id=str(attempt["dataset_id"]),
            expected_staging_paths={
                "root": str(
                    generated_dataset_staging_paths(
                        runtime_root=runtime_root,
                        thread_id=context.thread_id,
                        dataset_id=str(attempt["dataset_id"]),
                    )["root"]
                ),
                **expected_staging_paths,
            },
        )
        return None
    raise ToolExecutionError(
        "DATASET_IDENTITY_COLLISION",
        f"Unknown dataset persistence attempt state: {state or '<missing>'}.",
        recoverable=False,
    )


def _persist_extraction_result(
    arguments: dict[str, Any],
    context: ToolContext,
    *,
    stored_plan: Any,
    plan: DatasetPlan,
    sql_artifact: Any,
    validated: ValidatedExtractionSql,
    execution: SqlExecutionResult | None,
) -> ToolResult:
    plan_id = arguments["plan_id"]
    plan_version = int(arguments["plan_version"])
    predecessor_identity = _predecessor_identity(arguments)
    sql_content = dict(sql_artifact.content)
    if sql_artifact.provenance.get("study_id") != plan.study_id:
        raise ToolExecutionError(
            "STUDY_REFERENCE_MISMATCH",
            "Validated SQL and its dataset plan identify different studies.",
            recoverable=False,
        )
    dataset_id = _deterministic_agent_dataset_id(
        thread_id=context.thread_id,
        plan_id=plan_id,
        plan_version=plan_version,
        sql_id=sql_artifact.id,
        sql_version=sql_artifact.version,
        predecessor=predecessor_identity,
    )
    from graph.state import MetaKeys

    columns = _plan_columns(plan)
    tables = [str(table) for table in list(sql_content.get("tables") or [])]
    validated_sql = validated.sql
    candidate = PreparedSqlCandidate(
        question=plan.goal,
        sql=validated.sql,
        tables=tables,
        columns=columns,
        selection_id=plan_id,
        row_filters=_plan_row_filters(plan),
        protected_columns=_plan_protected_columns(plan),
        applied_filters=[],
    )
    expected_output_aliases = _validated_sql_output_aliases(candidate.sql)
    approved_selected_columns = db_rag_persistence.serialize_columns(
        candidate.columns
    )
    methods = _persistence_store_methods(context)
    lineage = _dataset_persistence_lineage(
        context,
        study_id=plan.study_id,
        plan_id=plan_id,
        plan_version=plan_version,
        plan_content=dict(stored_plan.content),
        sql_id=sql_artifact.id,
        sql_version=sql_artifact.version,
        sql_content=sql_content,
        expected_output_aliases=expected_output_aliases,
        approved_selected_tables=candidate.tables,
        approved_selected_columns=approved_selected_columns,
        predecessor=predecessor_identity,
    )
    runtime_root = _dataset_root(context)
    final_paths = generated_dataset_artifact_paths(
        dataset_root=context.thread_storage.datasets,
        dataset_id=dataset_id,
    )
    staging_paths = generated_dataset_staging_paths(
        dataset_root=context.thread_storage.datasets,
        dataset_id=dataset_id,
    )
    expected_final_paths = {
        key: str(final_paths[key])
        for key in ("path", "schema_path", "metadata_path")
    }
    expected_staging_paths = {
        key: str(staging_paths[key])
        for key in ("path", "schema_path", "metadata_path")
    }
    attempt = methods["get_dataset_persistence_attempt"](dataset_id)
    journal = _load_attempt_journal(
        context,
        runtime_root=runtime_root,
        dataset_id=dataset_id,
    )
    if journal is not None:
        _require_attempt_identity(
            journal,
            lineage=lineage,
            expected_final_paths=expected_final_paths,
            expected_staging_paths=expected_staging_paths,
        )
        journal = _prepare_journal_for_hydration(
            context,
            journal=journal,
            lineage=lineage,
            validated_sql=validated_sql,
            expected_final_paths=expected_final_paths,
            expected_staging_paths=expected_staging_paths,
            runtime_root=runtime_root,
        )
        try:
            attempt = _sync_attempt_from_journal(
                context,
                methods=methods,
                attempt=attempt if isinstance(attempt, dict) else None,
                journal=journal,
                runtime_root=runtime_root,
            )
        except ToolExecutionError:
            raise
        except Exception as error:
            raise ToolExecutionError(
                "DATASET_IDENTITY_COLLISION",
                f"Persistence journal hydration failed: {type(error).__name__}.",
                recoverable=False,
            ) from error
    elif isinstance(attempt, dict):
        _write_attempt_journal(
            context,
            runtime_root=runtime_root,
            attempt=attempt,
        )
    replacement: tuple[Any, ArtifactRef] | None = None
    replacement_control: dict[str, Any] | None = None
    if isinstance(attempt, dict):
        reconciled = _reconcile_persistence_attempt(
            context,
            methods=methods,
            attempt=attempt,
            lineage=lineage,
            validated_sql=validated_sql,
            expected_final_paths=expected_final_paths,
            expected_staging_paths=expected_staging_paths,
            runtime_root=runtime_root,
            plan_id=plan_id,
            plan_version=plan_version,
        )
        if reconciled is not None:
            return reconciled
        replacement_control = (
            dict(attempt["replacement"])
            if isinstance(attempt.get("replacement"), dict)
            else None
        )
    else:
        try:
            context.artifact_store.require(dataset_id)
        except KeyError:
            pass
        else:
            raise ToolExecutionError(
                "DATASET_IDENTITY_COLLISION",
                "Deterministic dataset exists without a persistence attempt.",
                recoverable=False,
            )
        replacement = _replacement_predecessor(
            arguments,
            context,
            plan_id=plan_id,
            plan_version=plan_version,
        )
        replacement_control = _replacement_control(replacement)
    if execution is None:
        try:
            execution = execute_validated_extraction_sql(
                validated,
                source_tables=tables,
                database_path=_plan_runtime_path(context, plan),
            )
        except Exception as error:
            raise ToolExecutionError(
                "SQL_REPAIR_REQUIRED",
                (
                    "The validated SQL candidate failed during read-only "
                    "execution."
                ),
                recoverable=True,
                details=_sql_repair_details(
                    plan_id=plan_id,
                    plan_version=plan_version,
                    origin=str(sql_content.get("origin") or ""),
                    stage="execution",
                    attempted_sql=validated.sql,
                    error=error,
                    tables=tables,
                    columns=_plan_reference_columns(plan),
                ),
            ) from error
    relationship_metrics, post_sql_warnings = _plan_relationship_metrics(
        context,
        plan,
    )
    grain_columns = [
        str(field.output_column or field.column).strip()
        for field in [
            *plan.required_fields,
            *[
                field
                for concept in plan.concepts
                for field in concept.fields
                if "grain" in field.roles
                or field.purpose.strip().casefold() in {"grain", "timing"}
            ],
        ]
    ]
    approved_selection = {
        "selection_id": plan_id,
        "source_question": plan.goal,
        "goal_text": plan.goal,
        "tables": candidate.tables,
        "columns": approved_selected_columns,
        "row_filters": candidate.row_filters,
        "protected_columns": candidate.protected_columns,
    }
    if attempt is None:
        attempt_record = {
            "dataset_id": dataset_id,
            "state": "begun",
            "lineage": lineage,
            "expected_final_paths": expected_final_paths,
            "expected_staging_paths": expected_staging_paths,
        }
        if replacement_control is not None:
            attempt_record["replacement"] = replacement_control
        try:
            attempt = methods["begin_dataset_persistence_attempt"](
                attempt_record
            )
        except Exception as error:
            refreshed = methods["get_dataset_persistence_attempt"](dataset_id)
            if isinstance(refreshed, dict) and refreshed != attempt_record:
                raise ToolExecutionError(
                    "DATASET_IDENTITY_COLLISION",
                    "Persistence begin collided with different attempt metadata.",
                    recoverable=False,
                ) from error
            if refreshed != attempt_record:
                raise ToolExecutionError(
                    "DATASET_PERSISTENCE_PENDING",
                    (
                        f"Persistence begin response was ambiguous after "
                        f"{type(error).__name__}; replay the exact tool call."
                    ),
                    recoverable=True,
                ) from error
            attempt = refreshed
        _write_attempt_journal(
            context,
            runtime_root=runtime_root,
            attempt=attempt,
        )
    try:
        _state, artifact, staged = (
            db_rag_persistence.persist_sql_subset_artifact(
                {
                    "artifacts": context.artifact_store.snapshot(),
                    "meta": {MetaKeys.THREAD_ID: context.thread_id},
                },
                candidate,
                approved_selection,
                execution,
                study_id=plan.study_id,
                selection_artifact_id=plan_id,
                sql_candidate_artifact_id=sql_artifact.id,
                plan_id=plan_id,
                plan_version=plan_version,
                sql_version=sql_artifact.version,
                dataset_id=dataset_id,
                dataset_name=deterministic_dataset_name(goal_text=plan.goal),
                exact_sql=validated_sql,
                predecessor_dataset_id=(
                    predecessor_identity[0]
                    if predecessor_identity
                    else None
                ),
                predecessor_dataset_version=(
                    predecessor_identity[1]
                    if predecessor_identity
                    else None
                ),
                relationship_metrics=relationship_metrics,
                post_sql_warnings=post_sql_warnings,
                grain_columns=grain_columns,
                make_active=False,
                stage_only=True,
                artifact_version=1,
                artifact_status="pending_review",
                runtime_root=runtime_root,
            )
        )
    except FileExistsError as error:
        raise ToolExecutionError(
            "DATASET_DURABLE_COLLISION",
            str(error),
            recoverable=True,
        ) from error
    except Exception as error:
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_FAILED",
            f"{type(error).__name__}: {error}",
            recoverable=True,
        ) from error
    if staged is None:
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_FAILED",
            "Dataset staging did not return a durable manifest.",
            recoverable=False,
        )
    staged_attempt = {
        **attempt,
        "state": "staged",
        "manifest": staged.manifest,
        "dataset": artifact,
    }
    _write_attempt_journal(
        context,
        runtime_root=runtime_root,
        attempt=staged_attempt,
    )
    try:
        attempt = methods["advance_dataset_persistence_attempt"](
            dataset_id,
            lineage=lineage,
            expected_state="begun",
            state="staged",
            manifest=staged.manifest,
            dataset=artifact,
        )
    except Exception as error:
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_PENDING",
            (
                f"Staged dataset remains tracked after {type(error).__name__}; "
                "replay the exact tool call."
            ),
            recoverable=True,
        ) from error
    try:
        promote_staged_dataset_artifact(
            runtime_root=runtime_root,
            thread_id=context.thread_id,
            dataset_id=dataset_id,
            expected_final_paths=staged.expected_final_paths,
            expected_staging_paths=staged.expected_staging_paths,
            manifest=staged.manifest,
        )
    except ValueError as error:
        raise ToolExecutionError(
            "DATASET_DURABLE_COLLISION",
            str(error),
            recoverable=False,
        ) from error
    except OSError as error:
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_PENDING",
            f"Staged promotion could not complete: {type(error).__name__}.",
            recoverable=True,
        ) from error
    promoted_attempt = {**attempt, "state": "promoted"}
    promoted_attempt = _verify_canonical_attempt_dataset(
        context,
        attempt=promoted_attempt,
        lineage=lineage,
        validated_sql=validated_sql,
        runtime_root=runtime_root,
    )
    _write_attempt_journal(
        context,
        runtime_root=runtime_root,
        attempt=promoted_attempt,
    )
    try:
        attempt = methods["advance_dataset_persistence_attempt"](
            dataset_id,
            lineage=lineage,
            expected_state="staged",
            state="promoted",
        )
    except Exception as error:
        raise ToolExecutionError(
            "DATASET_PERSISTENCE_PENDING",
            (
                f"Promoted dataset remains tracked after {type(error).__name__}; "
                "replay the exact tool call."
            ),
            recoverable=True,
        ) from error
    return _commit_persistence_attempt(
        context,
        methods=methods,
        attempt=attempt,
        plan_id=plan_id,
        plan_version=plan_version,
        reused=False,
        runtime_root=runtime_root,
    )


def _inspect_dataset(
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    plan_id = arguments["plan_id"]
    plan_version = int(arguments["plan_version"])
    _approved_plan(
        context,
        plan_id=plan_id,
        plan_version=plan_version,
    )
    dataset = _require_artifact(
        context,
        artifact_id=arguments["dataset_id"],
        version=int(arguments["dataset_version"]),
    )
    if dataset.status != "pending_review":
        raise ToolExecutionError(
            "DATASET_NOT_PENDING_REVIEW",
            "Quality inspection requires an exact pending-review dataset.",
            recoverable=True,
        )
    dataset_ref = ArtifactRef(
        id=dataset.id,
        kind=dataset.kind,
        version=dataset.version,
    )
    plan_ref = ArtifactRef(
        id=plan_id,
        kind="dataset_plan",
        version=plan_version,
    )
    try:
        report_ref = inspect_dataset_artifact(
            artifact_store=context.artifact_store,
            dataset_ref=dataset_ref,
            plan_ref=plan_ref,
        )
    except (KeyError, ValueError) as error:
        raise ToolExecutionError(
            "DATASET_QUALITY_INSPECTION_FAILED",
            str(error),
            recoverable=True,
        ) from error
    report = context.artifact_store.require(report_ref)
    content = _render_quality(report.content)
    return ToolResult(
        message=(
            f"Dataset quality quality_report_id={report_ref.id} "
            f"version={report_ref.version} dataset_id={dataset.id} "
            f"dataset_version={dataset.version}: "
            f"{json.dumps(content, sort_keys=True)}"
        ),
        artifacts=(report_ref,),
    )


def build_db_rag_tool_registry() -> ToolRegistry:
    definitions: list[
        tuple[str, str, type[BaseModel], bool, bool, Callable[..., ToolResult]]
    ] = [
        (
            "open_artifact",
            "Open an exact versioned artifact using a bounded row-free representation.",
            OpenArtifactArguments,
            True,
            False,
            _open_artifact,
        ),
        (
            "search_catalog",
            (
                "Batch all currently needed semantic schema probes, search the "
                "runtime catalog once, and store bounded field evidence."
            ),
            CatalogSearchArguments,
            True,
            False,
            _search_catalog,
        ),
        (
            "inspect_table",
            "Inspect bounded runtime schema details for one table.",
            InspectTableArguments,
            True,
            False,
            _inspect_table,
        ),
        (
            "find_join_paths",
            "Find observed direct or multi-hop paths between required runtime fields.",
            FindJoinPathsArguments,
            True,
            False,
            _find_join_paths,
        ),
        (
            "profile_relationship",
            (
                "Profile an explicit source relationship and persist bounded "
                "pre-extraction relationship-risk evidence."
            ),
            ProfileRelationshipArguments,
            True,
            False,
            _profile_relationship,
        ),
        (
            "save_dataset_plan",
            (
                "Save a complete structured dataset plan or exact revision; "
                "every join must declare inner or left join_type."
            ),
            SaveDatasetPlanArguments,
            False,
            False,
            _save_dataset_plan,
        ),
        (
            "validate_dataset_plan",
            (
                "Validate only runtime plan fields, sources, operations, "
                "relationships, and unresolved items. If validation fails, "
                "details.issues contains all independent issues to repair "
                "together."
            ),
            ValidateDatasetPlanArguments,
            True,
            False,
            _validate_dataset_plan,
        ),
        (
            "validate_and_extract",
            (
                "Compile and immediately validate and extract an approved "
                "dataset plan. Omit sql for the deterministic first attempt. "
                "After SQL_REPAIR_REQUIRED, submit repaired read-only SQL "
                "against the same frozen plan, using at most four repairs."
            ),
            ValidateAndExtractArguments,
            False,
            False,
            _validate_and_extract,
        ),
        (
            "inspect_dataset",
            (
                "Create a deterministic row-private quality report for an exact "
                "pending-review dataset and approved plan."
            ),
            InspectDatasetArguments,
            False,
            False,
            _inspect_dataset,
        ),
    ]
    tools: list[AgentTool] = [
        _FunctionTool(
            spec=ToolSpec(
                name=_dbrag_tool_name(operation),
                description=description,
                args_model=args_model,
                read_only=read_only,
                interrupting=interrupting,
            ),
            handler=handler,
        )
        for operation, description, args_model, read_only, interrupting, handler in definitions
    ]
    tools.append(RequestDatasetPlanReviewTool())
    tools.append(RequestDatasetReviewTool())
    return ToolRegistry(tools)


__all__ = ["build_db_rag_tool_registry"]
