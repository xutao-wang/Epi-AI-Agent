from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from typing import Annotated, Any

from langgraph.types import interrupt
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

from epi_agent.analysis_artifacts import AnalysisRun
from epi_agent.protocol import (
    ArtifactRef,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    ToolTerminalControl,
)
from epi_agent.registry import ToolRegistry
from utils.dataset_artifacts import is_selectable_dataset_artifact
from utils.review_interrupts import validate_bounded_review_view


Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
_MAX_WARNINGS = 20
_MAX_FEEDBACK_HISTORY = 20
_MAX_MAPPING_CHARS = 100_000
_MAX_STRING_CHARS = 8_000
_MAX_WARNING_CHARS = 2_000
_MAX_CODE_CHARS = 100_000
_MAX_COLLECTION_ITEMS = 500
_PRIVATE_KEY_PARTS = {
    "password",
    "path",
    "secret",
    "stderr",
    "stdout",
    "token",
}
_SPECIFICATION_KEYS = {
    "adjustment_variables",
    "analysis_goal",
    "analysis_unit_id",
    "censoring_description",
    "code",
    "code_assumptions",
    "code_summary",
    "event",
    "exposures",
    "missing",
    "outcome",
    "packages",
    "review",
    "time_origin",
    "time_to_event",
    "timepoint",
}
_RUNTIME_KEYS = {
    "execution_mode",
    "image",
    "language",
    "packages",
    "security_boundary",
    "version",
}


class RequestAnalysisResultReviewArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    analysis_run_id: Identifier
    analysis_run_version: int = Field(ge=1)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_private_key(key: str) -> bool:
    normalized = key.casefold()
    return any(part in normalized for part in _PRIVATE_KEY_PARTS)


def _sanitize_json(value: Any, *, code: bool = False) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in list(value.items())[:_MAX_COLLECTION_ITEMS]:
            normalized_key = str(key)
            if _is_private_key(normalized_key):
                continue
            sanitized[normalized_key] = _sanitize_json(item)
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_json(item)
            for item in value[:_MAX_COLLECTION_ITEMS]
        ]
    if isinstance(value, str):
        limit = _MAX_CODE_CHARS if code else _MAX_STRING_CHARS
        return value[:limit]
    return deepcopy(value)


def _bounded_mapping(
    value: dict[str, Any],
    *,
    max_chars: int = _MAX_MAPPING_CHARS,
) -> dict[str, Any]:
    sanitized = _sanitize_json(value)
    serialized = json.dumps(
        sanitized,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(serialized) <= max_chars:
        return sanitized
    return {
        "truncated": True,
        "keys": sorted(str(key) for key in sanitized)[:100],
    }


def _bounded_specification(run: AnalysisRun) -> dict[str, Any]:
    specification = {
        key: deepcopy(value)
        for key, value in run.specification.items()
        if key in _SPECIFICATION_KEYS
    }
    code = specification.get("code")
    if isinstance(code, str):
        specification["code"] = _sanitize_json(code, code=True)
        specification["code_truncated"] = len(code) > _MAX_CODE_CHARS
    return _bounded_mapping(specification)


def _bounded_runtime(run: AnalysisRun) -> dict[str, Any]:
    return _bounded_mapping(
        {
            key: deepcopy(value)
            for key, value in run.runtime.items()
            if key in _RUNTIME_KEYS
        }
    )


def _review_payload(
    run_ref: ArtifactRef,
    run: AnalysisRun,
    *,
    feedback_history: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    warnings = [
        str(warning)[:_MAX_WARNING_CHARS]
        for warning in run.warnings[:_MAX_WARNINGS]
    ]
    return {
        "type": "analysis_result_review",
        "artifact": {
            "id": run_ref.id,
            "kind": run_ref.kind,
            "version": run_ref.version,
            "expected_status": "pending_review",
        },
        "view": {
            "method": run.method,
            "dataset": (
                run.dataset.model_dump(mode="json")
                if run.dataset is not None
                else None
            ),
            "specification": _bounded_specification(run),
            "output_text": run.output_text,
            "warnings": warnings,
            "warnings_truncated": (
                len(run.warnings) > _MAX_WARNINGS
                or any(
                    len(str(warning)) > _MAX_WARNING_CHARS
                    for warning in run.warnings[:_MAX_WARNINGS]
                )
            ),
            "runtime": _bounded_runtime(run),
            "tables": [
                table.model_dump(mode="json")
                for table in run.tables[:20]
            ],
            "figures": [
                figure.model_dump(mode="json")
                for figure in run.figures[:20]
            ],
            "feedback_history": [
                _bounded_mapping(dict(entry), max_chars=8_000)
                for entry in feedback_history[-_MAX_FEEDBACK_HISTORY:]
            ],
        },
    }


def _analysis_run(
    arguments: dict[str, Any],
    context: ToolContext,
) -> tuple[ArtifactRef, AnalysisRun, tuple[ArtifactRef, ...]]:
    reference = ArtifactRef(
        id=arguments["analysis_run_id"],
        kind="analysis_run",
        version=int(arguments["analysis_run_version"]),
    )
    try:
        stored = context.artifact_store.require(reference)
        run = AnalysisRun.model_validate(stored.content)
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise ToolExecutionError(
            "ANALYSIS_RUN_STALE",
            (
                f"Analysis run {reference.id} version {reference.version} "
                "is unavailable, stale, or malformed."
            ),
            recoverable=True,
        ) from error
    if stored.status != "pending_review":
        raise ToolExecutionError(
            "ANALYSIS_NOT_REVIEWABLE",
            (
                f"Analysis run {reference.id} version {reference.version} "
                f"has status={stored.status}."
            ),
            recoverable=True,
        )
    if run.dataset is not None:
        dataset_ref = ArtifactRef(
            id=run.dataset.id,
            kind=run.dataset.kind,
            version=run.dataset.version,
        )
        try:
            dataset = context.artifact_store.require(dataset_ref)
        except (KeyError, TypeError, ValueError) as error:
            raise ToolExecutionError(
                "ANALYSIS_DATASET_STALE",
                "The exact dataset used by this analysis is no longer available.",
                recoverable=True,
            ) from error
        if not is_selectable_dataset_artifact(
            {**dict(dataset.content), "status": dataset.status}
        ):
            raise ToolExecutionError(
                "ANALYSIS_DATASET_STALE",
                (
                    f"Dataset {dataset.id} version {dataset.version} "
                    f"has status={dataset.status}."
                ),
                recoverable=True,
            )
    linked = tuple(
        ArtifactRef(id=item.id, kind=item.kind, version=item.version)
        for item in [*run.tables, *run.figures]
    )
    for linked_reference in linked:
        if linked_reference.kind not in {"figure", "table"}:
            raise ToolExecutionError(
                "ANALYSIS_OUTPUT_STALE",
                "The analysis references an unsupported linked output.",
                recoverable=True,
            )
        try:
            linked_artifact = context.artifact_store.require(linked_reference)
        except (KeyError, TypeError, ValueError) as error:
            raise ToolExecutionError(
                "ANALYSIS_OUTPUT_STALE",
                "A linked analysis output is unavailable or stale.",
                recoverable=True,
            ) from error
        if linked_artifact.status != "pending_review":
            raise ToolExecutionError(
                "ANALYSIS_OUTPUT_STALE",
                (
                    f"Linked output {linked_reference.id} version "
                    f"{linked_reference.version} has "
                    f"status={linked_artifact.status}."
                ),
                recoverable=True,
            )
    return reference, run, linked


def _transition(
    context: ToolContext,
    references: tuple[ArtifactRef, ...],
    *,
    action: str,
    status: str,
    timestamp: str,
    feedback: str | None = None,
) -> tuple[ArtifactRef, ...]:
    provenance = {
        "actor": "analysis-request_result_review",
        "decision": action,
        "thread_id": context.thread_id,
        "timestamp": timestamp,
    }
    if feedback is not None:
        provenance["feedback"] = feedback
    try:
        return context.artifact_store.transition_artifact_statuses(
            references,
            expected_status="pending_review",
            status=status,
            provenance=provenance,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ToolExecutionError(
            "ANALYSIS_RUN_STALE",
            str(error),
            recoverable=True,
        ) from error


class AnalysisResultReviewTool:
    spec = ToolSpec(
        name="analysis-request_result_review",
        description=(
            "Request final human review of one exact pending "
            "EpiAgent analysis result. Call this tool alone."
        ),
        args_model=RequestAnalysisResultReviewArguments,
        read_only=False,
        interrupting=True,
    )

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        reference, run, linked = _analysis_run(arguments, context)
        payload = _review_payload(
            reference,
            run,
            feedback_history=context.analysis_review_feedback_history,
        )
        try:
            validate_bounded_review_view(payload["view"])
        except (TypeError, ValueError) as error:
            raise ToolExecutionError(
                "ANALYSIS_REVIEW_INVALID",
                (
                    "The analysis result could not be represented by the "
                    "review contract."
                ),
                recoverable=True,
            ) from error
        decision = interrupt(payload)
        if not isinstance(decision, dict):
            raise ToolExecutionError(
                "REVIEW_DECISION_INVALID",
                "Analysis review returned an invalid decision.",
                recoverable=True,
            )
        action = str(decision.get("action") or "").strip().casefold()
        if action not in {"approve", "revise", "cancel"}:
            raise ToolExecutionError(
                "REVIEW_DECISION_INVALID",
                (
                    "Analysis review action must be approve, revise, "
                    "or cancel."
                ),
                recoverable=True,
            )
        feedback = (
            str(decision.get("feedback") or "").strip()
            if action == "revise"
            else None
        )
        if action == "revise" and not feedback:
            raise ToolExecutionError(
                "REVIEW_FEEDBACK_REQUIRED",
                "Revision feedback is required before revising.",
                recoverable=True,
            )
        timestamp = _now()
        target_status = {
            "approve": "active",
            "revise": "rejected",
            "cancel": "cancelled",
        }[action]
        transitioned = _transition(
            context,
            (reference, *linked),
            action=action,
            status=target_status,
            timestamp=timestamp,
            feedback=feedback,
        )
        observation = {
            "action": action,
            "analysis_run_id": reference.id,
            "analysis_run_version": reference.version,
            "status": target_status,
        }
        if action == "approve":
            observation["output_text"] = run.output_text
            observation["warnings"] = list(run.warnings)
            approved_outputs = (
                reference,
                *tuple(
                    linked_reference
                    for linked_reference in linked
                    if linked_reference.kind in {"figure", "table"}
                ),
            )
            return ToolResult(
                message=json.dumps(
                    observation,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                artifacts=transitioned,
                output_artifacts=approved_outputs,
            )
        if action == "revise":
            feedback_entry = {
                "action": "revise",
                "analysis_run_id": reference.id,
                "analysis_run_version": reference.version,
                "feedback": feedback,
                "timestamp": timestamp,
            }
            return ToolResult(
                message=json.dumps(
                    feedback_entry,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                artifacts=transitioned,
                review_feedback_entry=feedback_entry,
            )
        return ToolResult(
            message=json.dumps(
                observation,
                separators=(",", ":"),
                sort_keys=True,
            ),
            artifacts=transitioned,
            terminal_control=ToolTerminalControl(
                status="cancelled",
                reason="Human cancelled the active analysis result review.",
            ),
        )


def build_analysis_review_tool_registry() -> ToolRegistry:
    return ToolRegistry([AnalysisResultReviewTool()])


__all__ = [
    "AnalysisResultReviewTool",
    "RequestAnalysisResultReviewArguments",
    "build_analysis_review_tool_registry",
]
