from __future__ import annotations

import json
import math
from typing import Any

from langgraph.types import interrupt
from pydantic import BaseModel, Field

from db_rag.review_contracts import (
    _build_grouped_review,
    _column_key,
)
from db_rag.relationships import reverse_relationship_warning_code
from db_rag.service.dataset_naming import deterministic_dataset_name
from epi_agent.artifacts import (
    DatasetPlan,
    StateArtifactStore,
    dataset_plan_from_artifact,
)
from epi_agent.db_rag.quality import DatasetQualityReport
from epi_agent.protocol import (
    ArtifactRef,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    ToolTerminalControl,
    require_context_study,
)


class RequestDatasetPlanReviewArguments(BaseModel):
    plan_id: str
    version: int = Field(ge=1)


class RequestDatasetReviewArguments(BaseModel):
    dataset_id: str
    dataset_version: int = Field(ge=1)
    quality_report_id: str
    quality_report_version: int = Field(ge=1)


def _field_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, dict) else {}


def _field_ref(value: Any) -> tuple[str, str, str]:
    field = _field_dict(value)
    return tuple(
        str(field.get(part) or "").strip()
        for part in ("source", "table", "column")
    )


def _concept_fields(concept: Any) -> list[dict[str, Any]]:
    content = _field_dict(concept)
    return [
        _field_dict(field)
        for field in list(content.get("fields") or [])
        if _field_dict(field)
    ]


def _safe_filter_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [
            safe
            for item in value[:20]
            for safe in [_safe_filter_value(item)]
            if safe is not None or item is None
        ]
    return None


def _review_filters(plan: DatasetPlan) -> list[dict[str, Any]]:
    presented: list[dict[str, Any]] = []
    source_ids = {
        str(field.source or "").strip()
        for field in [
            *plan.required_fields,
            *(field for concept in plan.concepts for field in concept.fields),
        ]
        if str(field.source or "").strip()
    }
    if len(source_ids) == 1:
        default_source = next(iter(source_ids))
    else:
        default_source = ""
    for value in plan.filters:
        item: dict[str, Any] = {}
        for key in ("description", "predicate"):
            raw_text = value.get(key)
            text = raw_text.strip() if isinstance(raw_text, str) else ""
            if text:
                item[key] = text[:500]
        references: list[dict[str, str]] = []
        for reference in value.get("referenced_columns") or []:
            source, table, column = _field_ref(reference)
            if not source:
                source = default_source
            references.append(
                {"source": source, "table": table, "column": column}
            )
        if references:
            item["referenced_columns"] = references
        constraints: list[dict[str, Any]] = []
        for constraint in value.get("value_constraints") or []:
            source, table, column = _field_ref(constraint)
            if not source:
                source = default_source
            presented_constraint: dict[str, Any] = {
                "source": source,
                "table": table,
                "column": column,
            }
            raw_operator = dict(constraint).get("operator")
            operator = (
                raw_operator.strip()
                if isinstance(raw_operator, str)
                else ""
            )
            if operator:
                presented_constraint["operator"] = operator[:50]
            for key in ("value", "values"):
                if key in constraint:
                    safe_value = _safe_filter_value(constraint[key])
                    if safe_value is not None or constraint[key] is None:
                        presented_constraint[key] = safe_value
            constraints.append(presented_constraint)
        if constraints:
            item["value_constraints"] = constraints
        presented.append(item)
    return presented


def _review_payload(
    plan: DatasetPlan,
    *,
    context: ToolContext,
    plan_id: str,
    version: int,
) -> dict[str, Any]:
    concepts = [concept.model_dump(mode="json") for concept in plan.concepts]
    columns: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    clinical_concepts: list[dict[str, Any]] = []
    seen_fields: set[tuple[str, str, str]] = set()
    for concept in concepts:
        concept_id = str(
            concept.get("concept_id") or concept.get("id") or ""
        ).strip()
        label = str(concept.get("label") or concept_id).strip()
        fields = _concept_fields(concept)
        presented_fields: list[dict[str, Any]] = []
        for field in fields:
            reference = _field_ref(field)
            if not all(reference) or reference in seen_fields:
                continue
            seen_fields.add(reference)
            presented = {
                **field,
                "roles": ["requested"],
                "description": str(
                    field.get("description") or label
                ).strip(),
                "required": False,
                "key": _column_key(field),
            }
            columns.append(presented)
            presented_fields.append(presented)
        if not presented_fields:
            continue
        clinical_concepts.append(
            {
                "concept_id": concept_id,
                "label": label,
                "retrieval_probe": str(
                    concept.get("retrieval_probe") or ""
                ).strip(),
            }
        )
        assignments.append(
            {
                "concept_id": concept_id,
                "columns": presented_fields,
                "unresolved_reason": "",
            }
        )

    required_columns = _required_fields_payload(plan)
    filters = _review_filters(plan)
    row_filters = {
        "population": [],
        "data_quality": [],
        "explicit_value": filters,
        "not_applied": [],
    }
    review = {
        "artifact_id": plan_id,
        "artifact_version": version,
        "plan_id": plan_id,
        "plan_version": version,
        "goal_text": plan.goal,
        "row_definition": plan.row_definition,
        "tables": list(
            dict.fromkeys(
                str(column.get("table") or "")
                for column in columns
                if str(column.get("table") or "")
            )
        ),
        "columns": columns,
        "row_filters": row_filters,
        "filters": filters,
        "intent_snapshot": {
            "goal_text": plan.goal,
            "clinical_concepts": clinical_concepts,
            "clinical_concept_assignments": assignments,
            "required_columns": required_columns,
        },
        "status": "awaiting_review",
        "data_linkage": _data_linkage_payload(plan, context),
    }
    review["grouped_review"] = _build_grouped_review(review)
    return json.loads(json.dumps(review))


def _plan_review_view(
    plan: DatasetPlan,
    *,
    context: ToolContext,
    plan_id: str,
    version: int,
) -> dict[str, Any]:
    display = _review_payload(
        plan,
        context=context,
        plan_id=plan_id,
        version=version,
    )
    groups = list(dict(display["grouped_review"]).get("groups") or [])
    linkage = dict(display["data_linkage"])
    selected_fields = sorted(
        {
            str(column.get("key") or "")
            for group in groups
            for column in [
                *list(dict(group).get("columns") or []),
            ]
            if str(column.get("key") or "")
            and column.get("selected", True) is not False
        }
    )
    unresolved = [
        str(group.get("unresolved_reason") or "").strip()
        for group in groups
        if str(group.get("unresolved_reason") or "").strip()
    ]
    return json.loads(
        json.dumps(
            {
                "dataset_title": deterministic_dataset_name(goal_text=plan.goal),
                "goal": plan.goal,
                "concept_groups": groups,
                "selected_fields": selected_fields,
                "filters": list(display.get("filters") or []),
                "required_fields": _required_fields_payload(plan),
                "joins": list(linkage.get("relationships") or []),
                "unresolved_scientific_choices": unresolved,
            }
        )
    )


def _table_ref(source: Any, table: Any) -> tuple[str, str]:
    return (str(source or "").strip(), str(table or "").strip())


def _operation_name(operation: dict[str, Any]) -> str:
    return str(operation.get("type") or operation.get("name") or "").strip().casefold()


def _operation_key_pairs(
    operation: dict[str, Any],
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for value in operation.get("key_pairs") or []:
        pair = _field_dict(value)
        left = str(pair.get("left_column") or "").strip()
        right = str(pair.get("right_column") or "").strip()
        if left and right:
            pairs.append((left, right))
    return pairs


def _required_fields_payload(
    plan: DatasetPlan,
) -> list[dict[str, Any]]:
    presented: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for field in plan.required_fields:
        content = field.model_dump(mode="json")
        reference = _field_ref(content)
        if not all(reference) or reference in seen:
            continue
        seen.add(reference)
        presented.append(
            {
                **content,
                "roles": ["identifier"],
                "key": _column_key(content),
                "label": "Required identifier",
                "required": True,
            }
        )
    return presented


def _relationship_profiles(content: dict[str, Any]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    if isinstance(content.get("profile"), dict):
        profiles.append(dict(content["profile"]))
    for path in content.get("paths") or []:
        if isinstance(path, dict):
            profiles.extend(
                dict(profile)
                for profile in path.get("profiles") or []
                if isinstance(profile, dict)
            )
    return profiles


def _profile_key_pairs(profile: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for value in profile.get("key_pairs") or []:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            pairs.append((str(value[0]), str(value[1])))
        elif isinstance(value, dict):
            left = str(value.get("left_column") or "")
            right = str(value.get("right_column") or "")
            if left and right:
                pairs.append((left, right))
    return pairs


def _matching_profile(
    operation: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any] | None:
    artifact_id = str(
        operation.get("relationship_artifact_id") or ""
    ).strip()
    artifact_version = operation.get("relationship_artifact_version")
    if not artifact_id or not isinstance(artifact_version, int):
        return None
    try:
        artifact = context.artifact_store.require(
            ArtifactRef(
                id=artifact_id,
                kind="relationship_profile",
                version=artifact_version,
            )
        )
    except (KeyError, ValueError):
        return None

    left_table = str(operation.get("left_table") or "").strip()
    right_table = str(operation.get("right_table") or "").strip()
    key_pairs = _operation_key_pairs(operation)
    for profile in _relationship_profiles(artifact.content):
        profile_left = str(profile.get("left_table") or "").strip()
        profile_right = str(profile.get("right_table") or "").strip()
        profile_pairs = _profile_key_pairs(profile)
        if (
            profile_left == left_table
            and profile_right == right_table
            and profile_pairs == key_pairs
        ):
            return profile
        if (
            profile_left == right_table
            and profile_right == left_table
            and profile_pairs
            == [(right, left) for left, right in key_pairs]
        ):
            return {
                **profile,
                "left_cardinality": profile.get("right_cardinality"),
                "right_cardinality": profile.get("left_cardinality"),
                "warnings": [
                    reverse_relationship_warning_code(str(warning))
                    for warning in profile.get("warnings") or []
                ],
            }
    return None


def _cardinality_label(
    left_table: str,
    right_table: str,
    left_cardinality: str,
    right_cardinality: str,
) -> str:
    if left_cardinality == "one" and right_cardinality == "one":
        return (
            f"One {left_table} record links to one "
            f"{right_table} record per key."
        )
    if left_cardinality == "one" and right_cardinality == "many":
        return (
            f"One {left_table} record can link to multiple "
            f"{right_table} records."
        )
    if left_cardinality == "many" and right_cardinality == "one":
        return (
            f"Multiple {left_table} records can link to one "
            f"{right_table} record."
        )
    if left_cardinality == "many" and right_cardinality == "many":
        return (
            f"Multiple {left_table} records can link to multiple "
            f"{right_table} records."
        )
    return ""


def _warning_label(code: str, left_table: str, right_table: str) -> str:
    label = {
        "row_multiplication": "This link can create more than one output row.",
        "unmatched_left_keys": f"Some {left_table} records may not have a match.",
        "unmatched_right_keys": f"Some {right_table} records may not have a match.",
        "null_left_keys": f"Some {left_table} records have a missing link key.",
        "null_right_keys": f"Some {right_table} records have a missing link key.",
    }.get(code, code.replace("_", " ").capitalize() + ".")
    return f"Profiled relationship risk: {label[:1].lower()}{label[1:]}"


def _join_strategy_label(
    join_type: str,
    left_table: str,
    right_table: str,
) -> str:
    if join_type == "inner":
        return (
            f"Keep only records with matches in both {left_table} "
            f"and {right_table}."
        )
    if join_type == "left":
        return (
            f"Keep every {left_table} record, even when no "
            f"{right_table} record matches."
        )
    return ""


def _data_linkage_payload(
    plan: DatasetPlan,
    context: ToolContext,
) -> dict[str, Any]:
    study = require_context_study(context, plan.study_id)
    relationships: list[dict[str, Any]] = []
    shown_edges: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for value in plan.operations:
        operation = value.model_dump(mode="json")
        if _operation_name(operation) != "join":
            continue
        left_table = str(operation.get("left_table") or "").strip()
        right_table = str(operation.get("right_table") or "").strip()
        join_type = str(operation.get("join_type") or "").strip().casefold()
        relationship = {
            "description": str(operation.get("description") or "").strip(),
            "source": str(operation.get("source") or "").strip(),
            "evidence_label": "Profiled relationship risk",
            "join_type": join_type,
            "join_strategy_label": _join_strategy_label(
                join_type,
                left_table,
                right_table,
            ),
            "left_table": left_table,
            "right_table": right_table,
            "key_pairs": [
                {
                    "left_column": left_column,
                    "right_column": right_column,
                }
                for left_column, right_column in _operation_key_pairs(operation)
            ],
        }
        shown_edges.add(
            (
                relationship["left_table"],
                relationship["right_table"],
                tuple(
                    (
                        pair["left_column"],
                        pair["right_column"],
                    )
                    for pair in relationship["key_pairs"]
                ),
            )
        )
        profile = _matching_profile(operation, context)
        if profile:
            left_cardinality = str(
                profile.get("left_cardinality") or ""
            ).strip()
            right_cardinality = str(
                profile.get("right_cardinality") or ""
            ).strip()
            relationship.update(
                {
                    "left_cardinality": left_cardinality,
                    "right_cardinality": right_cardinality,
                    "cardinality_label": _cardinality_label(
                        left_table,
                        right_table,
                        left_cardinality,
                        right_cardinality,
                    ),
                    "warnings": [
                        {
                            "code": str(code),
                            "label": _warning_label(
                                str(code),
                                left_table,
                                right_table,
                            ),
                        }
                        for code in profile.get("warnings") or []
                        if str(code).strip()
                    ],
                }
            )
        relationships.append(relationship)
    try:
        from epi_agent.db_rag.tools import _verified_join_paths

        for edge in _verified_join_paths(plan, study):
            profile = edge.get("profile")
            key_pairs = list(edge.get("key_pairs") or [])
            edge_key = (
                str(edge.get("left_table") or ""),
                str(edge.get("right_table") or ""),
                tuple(
                    (
                        str(pair.get("left_column") or ""),
                        str(pair.get("right_column") or ""),
                    )
                    for pair in key_pairs
                ),
            )
            if edge_key in shown_edges:
                continue
            shown_edges.add(edge_key)
            relationship = {
                "description": "",
                "source": str(edge.get("source") or "").strip(),
                "evidence_label": "Profiled relationship risk",
                "join_type": "",
                "join_strategy_label": "",
                "left_table": edge_key[0],
                "right_table": edge_key[1],
                "key_pairs": key_pairs,
            }
            if profile is not None:
                left_cardinality = str(
                    getattr(profile, "left_cardinality", "") or ""
                ).strip()
                right_cardinality = str(
                    getattr(profile, "right_cardinality", "") or ""
                ).strip()
                relationship.update(
                    {
                        "left_cardinality": left_cardinality,
                        "right_cardinality": right_cardinality,
                        "cardinality_label": _cardinality_label(
                            edge_key[0],
                            edge_key[1],
                            left_cardinality,
                            right_cardinality,
                        ),
                        "warnings": [
                            {
                                "code": str(code),
                                "label": _warning_label(
                                    str(code),
                                    edge_key[0],
                                    edge_key[1],
                                ),
                            }
                            for code in getattr(profile, "warnings", [])
                            if str(code).strip()
                        ],
                    }
                )
            relationships.append(relationship)
    except (KeyError, ValueError):
        pass
    return {"relationships": relationships}


def _selected_plan(
    plan: DatasetPlan,
    selected_column_keys: list[Any] | None,
) -> DatasetPlan:
    if selected_column_keys is None:
        return plan
    selected = {
        str(value or "").strip().casefold()
        for value in selected_column_keys
        if str(value or "").strip()
    }
    seen: set[tuple[str, str, str]] = set()
    concepts: list[dict[str, Any]] = []
    for concept in plan.concepts:
        content = concept.model_dump(mode="json")
        fields: list[dict[str, Any]] = []
        for field in _concept_fields(content):
            reference = _field_ref(field)
            if (
                _column_key(field).casefold() not in selected
                or not all(reference)
                or reference in seen
            ):
                continue
            seen.add(reference)
            fields.append(field)
        if fields:
            concepts.append({**content, "fields": fields})
    return DatasetPlan.model_validate(
        {
            **plan.model_dump(mode="json"),
            "concepts": concepts,
        }
    )


def _validate_plan(
    plan: DatasetPlan,
    context: ToolContext,
) -> None:
    from epi_agent.db_rag.tools import _validate_dataset_plan

    temporary_store = StateArtifactStore(context.artifact_store.snapshot())
    temporary_ref = temporary_store.save_dataset_plan(plan)
    temporary_context = ToolContext(
        studies=context.studies,
        artifact_store=temporary_store,
        thread_id=context.thread_id,
        policy=context.policy,
    )
    _validate_dataset_plan(
        {
            "plan_id": temporary_ref.id,
            "plan_version": temporary_ref.version,
        },
        temporary_context,
    )


class RequestDatasetPlanReviewTool:
    spec = ToolSpec(
        name="dbrag-request_dataset_plan_review",
        description=(
            "Request human review of an exact validated dataset-plan version. "
            "The review visibly includes each explicit join strategy. Call this "
            "tool alone when the plan is ready for approval."
        ),
        args_model=RequestDatasetPlanReviewArguments,
        read_only=False,
        interrupting=True,
    )

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        from epi_agent.db_rag.tools import (
            _require_artifact,
            _validate_dataset_plan,
        )

        plan_id = arguments["plan_id"]
        version = int(arguments["version"])
        stored = _require_artifact(
            context,
            artifact_id=plan_id,
            version=version,
            kind="dataset_plan",
        )
        if stored.status not in {"draft", "pending_review"}:
            raise ToolExecutionError(
                "PLAN_NOT_REVIEWABLE",
                (
                    f"Dataset plan {plan_id} version {version} has "
                    f"status={stored.status} and cannot be reviewed."
                ),
                recoverable=True,
            )
        _validate_dataset_plan(
            {"plan_id": plan_id, "plan_version": version},
            context,
        )
        plan = dataset_plan_from_artifact(stored)
        payload = {
            "type": "dataset_plan_review",
            "artifact": {
                "id": plan_id,
                "kind": "dataset_plan",
                "version": version,
                "expected_status": stored.status,
            },
            "view": _plan_review_view(
                plan,
                context=context,
                plan_id=plan_id,
                version=version,
            ),
        }

        decision = interrupt(payload)
        if not isinstance(decision, dict):
            raise ToolExecutionError(
                "REVIEW_DECISION_INVALID",
                "Dataset-plan review returned an invalid decision.",
                recoverable=True,
            )
        action = str(decision.get("action") or "").strip().casefold()
        original_ref = ArtifactRef(
            id=plan_id,
            kind="dataset_plan",
            version=version,
        )
        if action == "cancel":
            try:
                context.artifact_store.transition_artifact_status(
                    original_ref,
                    expected_status=stored.status,
                    status="cancelled",
                    provenance={
                        "actor": "dbrag-request_dataset_plan_review",
                        "decision": "cancel",
                        "thread_id": context.thread_id,
                    },
                )
            except (KeyError, ValueError) as error:
                raise ToolExecutionError(
                    "ARTIFACT_STALE",
                    str(error),
                    recoverable=True,
                ) from error
            return ToolResult(
                message=(
                    f"Dataset plan review action=cancel "
                    f"plan_id={plan_id} version={version} status=cancelled."
                ),
                artifacts=(original_ref,),
                terminal_control=ToolTerminalControl(
                    status="cancelled",
                    reason="Human cancelled the active dataset plan review.",
                ),
            )
        if action not in {"approve", "revise"}:
            raise ToolExecutionError(
                "REVIEW_DECISION_INVALID",
                "Dataset-plan review action must be approve, revise, or cancel.",
                recoverable=True,
            )

        selected = decision.get("selected_column_keys")
        selected_keys = list(selected) if isinstance(selected, list) else None
        candidate = _selected_plan(
            plan,
            selected_keys,
        )
        _validate_plan(candidate, context)

        if action == "approve":
            try:
                reference = context.artifact_store.save_dataset_plan(
                    candidate,
                    status="approved",
                    prior_id=plan_id,
                    prior_version=version,
                    provenance={
                        "study_id": candidate.study_id,
                        "thread_id": context.thread_id,
                        "producer": "dbrag-request_dataset_plan_review",
                        "review_action": "approve_selected_plan",
                    },
                )
            except (KeyError, ValueError) as error:
                raise ToolExecutionError(
                    "ARTIFACT_STALE",
                    str(error),
                    recoverable=True,
                ) from error
            return ToolResult(
                message=(
                    f"Dataset plan review action=approve "
                    f"plan_id={reference.id} version={reference.version} "
                    "status=approved."
                ),
                artifacts=(reference,),
            )

        feedback = str(decision.get("feedback") or "").strip()
        if not feedback:
            raise ToolExecutionError(
                "REVIEW_FEEDBACK_REQUIRED",
                "Semantic dataset-plan revision requires human feedback.",
                recoverable=True,
            )
        reference = context.artifact_store.save_dataset_plan(
            candidate,
            status="draft",
            prior_id=plan_id,
            prior_version=version,
            provenance={
                "study_id": candidate.study_id,
                "thread_id": context.thread_id,
                "producer": "dbrag-request_dataset_plan_review",
                "review_action": "revise",
                "review_feedback": feedback,
            },
        )
        return ToolResult(
            message=(
                "Human requested a semantic revision. "
                f'Feedback: "{feedback}" '
                f"prior_plan_id={plan_id} prior_version={version} "
                f"plan_id={reference.id} version={reference.version} "
                "status=draft."
            ),
            artifacts=(reference,),
        )


def _dataset_review_columns(plan: DatasetPlan) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = [
        {
            **field.model_dump(mode="json"),
            "role": "required_identifier",
        }
        for field in plan.required_fields
    ]
    for concept in plan.concepts:
        concept_content = concept.model_dump(mode="json")
        label = str(
            concept_content.get("label")
            or concept_content.get("concept_id")
            or ""
        ).strip()
        for field in _concept_fields(concept_content):
            columns.append(
                {
                    **field,
                    "role": "requested_variable",
                    "concept_id": str(
                        concept_content.get("concept_id") or ""
                    ).strip(),
                    "description": str(
                        field.get("description") or label
                    ).strip(),
                }
            )
    return columns


def _bounded_quality_summary(
    quality_content: dict[str, Any],
) -> dict[str, Any]:
    allowed = {
        "null_rates",
        "duplicate_grain_rows",
        "grain_uniqueness",
        "requested_concept_coverage",
        "unexpected_columns",
        "join_expansion",
        "relationship_metrics",
    }
    return json.loads(
        json.dumps(
            {
                key: quality_content[key]
                for key in allowed
                if key in quality_content
            }
        )
    )


class RequestDatasetReviewTool:
    spec = ToolSpec(
        name="dbrag-request_dataset_review",
        description=(
            "Request human review of an exact pending dataset and its matching "
            "deterministic quality report, with source profiles labeled as "
            "relationship risk rather than observed expansion. Call this tool alone."
        ),
        args_model=RequestDatasetReviewArguments,
        read_only=False,
        interrupting=True,
    )

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        from epi_agent.db_rag.tools import _require_artifact

        dataset_ref = ArtifactRef(
            id=arguments["dataset_id"],
            kind="subset",
            version=int(arguments["dataset_version"]),
        )
        dataset = _require_artifact(
            context,
            artifact_id=dataset_ref.id,
            version=dataset_ref.version,
        )
        if dataset.kind not in {
            "analysis_dataset",
            "dataset",
            "db_rag_result",
            "subset",
        }:
            raise ToolExecutionError(
                "DATASET_NOT_REVIEWABLE",
                f"Artifact {dataset.id} is not a reviewable dataset.",
                recoverable=True,
            )
        dataset_ref = ArtifactRef(
            id=dataset.id,
            kind=dataset.kind,
            version=dataset.version,
        )
        if dataset.status != "pending_review":
            raise ToolExecutionError(
                "DATASET_NOT_REVIEWABLE",
                (
                    f"Dataset {dataset.id} version {dataset.version} has "
                    f"status={dataset.status}."
                ),
                recoverable=True,
            )

        quality_ref = ArtifactRef(
            id=arguments["quality_report_id"],
            kind="dataset_quality_report",
            version=int(arguments["quality_report_version"]),
        )
        quality = _require_artifact(
            context,
            artifact_id=quality_ref.id,
            version=quality_ref.version,
            kind=quality_ref.kind,
        )
        try:
            quality_content = DatasetQualityReport.model_validate(
                quality.content
            ).model_dump(mode="json")
        except ValueError as error:
            raise ToolExecutionError(
                "QUALITY_REPORT_INVALID",
                "The quality report is malformed.",
                recoverable=True,
            ) from error
        if (
            str(quality_content.get("dataset_id") or "") != dataset.id
            or quality_content.get("dataset_version") != dataset.version
        ):
            raise ToolExecutionError(
                "QUALITY_REPORT_MISMATCH",
                "The quality report does not belong to the exact pending dataset.",
                recoverable=True,
            )

        provenance = dict(dataset.content.get("provenance") or {})
        plan_id = str(provenance.get("plan_id") or "").strip()
        plan_version = provenance.get("plan_version")
        if not plan_id or not isinstance(plan_version, int):
            raise ToolExecutionError(
                "DATASET_LINEAGE_INVALID",
                "The pending dataset has no exact approved-plan lineage.",
                recoverable=True,
            )
        if (
            quality_content.get("plan_id") != plan_id
            or quality_content.get("plan_version") != plan_version
            or quality_content.get("sql_id") != provenance.get("sql_id")
            or quality_content.get("sql_version") != provenance.get("sql_version")
        ):
            raise ToolExecutionError(
                "QUALITY_REPORT_MISMATCH",
                "The quality report does not match the dataset plan lineage.",
                recoverable=True,
            )
        plan_artifact = _require_artifact(
            context,
            artifact_id=plan_id,
            version=plan_version,
            kind="dataset_plan",
        )
        if plan_artifact.status != "approved":
            raise ToolExecutionError(
                "PLAN_VERSION_NOT_APPROVED",
                "Dataset review requires the exact approved dataset plan.",
                recoverable=True,
            )
        plan = dataset_plan_from_artifact(plan_artifact)
        if (
            provenance.get("study_id") != plan.study_id
            or quality.provenance.get("study_id") != plan.study_id
        ):
            raise ToolExecutionError(
                "STUDY_REFERENCE_MISMATCH",
                "Dataset, quality report, and approved plan must identify "
                "the same study.",
                recoverable=True,
            )
        payload = {
            "type": "dataset_review",
            "artifact": {
                "id": dataset.id,
                "kind": dataset.kind,
                "version": dataset.version,
                "expected_status": dataset.status,
            },
            "view": {
                "goal": plan.goal,
                "dimensions": {
                    "rows": quality_content.get("row_count"),
                    "columns": quality_content.get("column_count"),
                },
                "columns": _dataset_review_columns(plan),
                "filters": _review_filters(plan),
                "quality": _bounded_quality_summary(quality_content),
                "warnings": list(quality_content.get("warnings") or []),
                "provenance": {
                    "plan": {"id": plan_id, "version": plan_version},
                    "sql": {
                        "id": provenance.get("sql_id"),
                        "version": provenance.get("sql_version"),
                    },
                    "quality_report": {
                        "id": quality.id,
                        "version": quality.version,
                    },
                },
                "feedback_history": [],
            },
        }

        decision = interrupt(json.loads(json.dumps(payload)))
        if not isinstance(decision, dict):
            raise ToolExecutionError(
                "REVIEW_DECISION_INVALID",
                "Dataset review returned an invalid decision.",
                recoverable=True,
            )
        action = str(decision.get("action") or "").strip().casefold()
        if action == "approve":
            activate = getattr(context.artifact_store, "activate_dataset", None)
            if not callable(activate):
                raise ToolExecutionError(
                    "ARTIFACT_STORE_UNAVAILABLE",
                    "The artifact store cannot activate reviewed datasets.",
                    recoverable=False,
                )
            try:
                activate(
                    dataset_ref,
                    expected_status="pending_review",
                    provenance={
                        "actor": "dbrag-request_dataset_review",
                        "decision": "approve",
                        "thread_id": context.thread_id,
                        "quality_report_id": quality.id,
                        "quality_report_version": quality.version,
                    },
                )
            except (KeyError, ValueError) as error:
                raise ToolExecutionError(
                    "ARTIFACT_STALE",
                    str(error),
                    recoverable=True,
                ) from error
            return ToolResult(
                message=(
                    f"Dataset review action=approve dataset_id={dataset.id} "
                    f"version={dataset.version} status=active."
                ),
                artifacts=(dataset_ref, quality_ref),
            )
        if action == "revise":
            feedback = str(decision.get("feedback") or "").strip()
            if not feedback:
                raise ToolExecutionError(
                    "REVIEW_FEEDBACK_REQUIRED",
                    "Dataset revision feedback cannot be empty.",
                    recoverable=True,
                )
            feedback_ref = context.artifact_store.save_artifact(
                kind="dataset_review_feedback",
                status="active",
                content={
                    "action": "revise",
                    "feedback": feedback,
                    "dataset_id": dataset.id,
                    "dataset_version": dataset.version,
                    "quality_report_id": quality.id,
                    "quality_report_version": quality.version,
                    "plan_id": plan_id,
                    "plan_version": plan_version,
                    "sql_id": provenance.get("sql_id"),
                    "sql_version": provenance.get("sql_version"),
                },
                provenance={
                    "producer": "dbrag-request_dataset_review",
                    "study_id": plan.study_id,
                    "thread_id": context.thread_id,
                    "dataset": {
                        "id": dataset.id,
                        "version": dataset.version,
                    },
                    "quality_report": {
                        "id": quality.id,
                        "version": quality.version,
                    },
                },
                summary=(
                    f"Dataset review feedback for {dataset.id} "
                    f"version {dataset.version}"
                ),
            )
            return ToolResult(
                message=(
                    "Human requested another dataset attempt. "
                    f'Feedback: "{feedback}" dataset_id={dataset.id} '
                    f"version={dataset.version} status=pending_review "
                    f"feedback_event_id={feedback_ref.id} "
                    f"feedback_event_version={feedback_ref.version}."
                ),
                artifacts=(dataset_ref, quality_ref, feedback_ref),
            )
        if action == "cancel":
            try:
                context.artifact_store.transition_artifact_status(
                    dataset_ref,
                    expected_status="pending_review",
                    status="cancelled",
                    provenance={
                        "actor": "dbrag-request_dataset_review",
                        "decision": "cancel",
                        "thread_id": context.thread_id,
                        "quality_report_id": quality.id,
                        "quality_report_version": quality.version,
                    },
                )
            except (KeyError, ValueError) as error:
                raise ToolExecutionError(
                    "ARTIFACT_STALE",
                    str(error),
                    recoverable=True,
                ) from error
            return ToolResult(
                message=(
                    f"Dataset review action=cancel dataset_id={dataset.id} "
                    f"version={dataset.version} status=cancelled."
                ),
                artifacts=(dataset_ref, quality_ref),
                terminal_control=ToolTerminalControl(
                    status="cancelled",
                    reason="Human cancelled the active dataset review.",
                ),
            )
        raise ToolExecutionError(
            "REVIEW_DECISION_INVALID",
            "Dataset review action must be approve, revise, or cancel.",
            recoverable=True,
        )


__all__ = [
    "RequestDatasetReviewArguments",
    "RequestDatasetReviewTool",
    "RequestDatasetPlanReviewArguments",
    "RequestDatasetPlanReviewTool",
]
