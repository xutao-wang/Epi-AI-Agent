from __future__ import annotations

import base64
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from epi_agent.artifacts import ArtifactStatus
from epi_agent.protocol import ArtifactRef, ArtifactStore, ToolContext


class ArtifactIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    version: int = Field(ge=1)


class RuntimePackage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


def _require_json_value(value: Any, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            _require_json_value(item, path=f"{path}.{key}")
        return
    raise ValueError(f"{path} contains a non-JSON value")


class AnalysisRun(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
    )

    schema_version: Literal["1.0"] = "1.0"
    method: str = Field(min_length=1)
    dataset: ArtifactIdentity | None = None
    specification: dict[str, Any]
    output_text: str = Field(default="", max_length=100_000)
    runtime: dict[str, Any]
    estimates: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    tables: list[ArtifactIdentity] = Field(default_factory=list)
    figures: list[ArtifactIdentity] = Field(default_factory=list)

    @field_validator(
        "specification",
        "runtime",
        "estimates",
        "diagnostics",
        mode="before",
    )
    @classmethod
    def require_json_content(cls, value: Any, info: Any) -> Any:
        _require_json_value(value, path=info.field_name)
        return value


def analysis_revision(
    context: ToolContext,
) -> tuple[ArtifactRef | None, str | None]:
    if not context.analysis_review_feedback_history:
        return None, None
    entry = dict(context.analysis_review_feedback_history[-1])
    if (
        entry.get("action") != "revise"
        or entry.get("replacement_analysis_run") is not None
    ):
        return None, None
    try:
        reference = ArtifactRef(
            id=str(entry["analysis_run_id"]),
            kind="analysis_run",
            version=int(entry["analysis_run_version"]),
        )
    except (KeyError, TypeError, ValueError):
        return None, None
    feedback = str(entry.get("feedback") or "").strip()
    return (reference, feedback) if feedback else (None, None)


def save_analysis_run(
    store: ArtifactStore,
    run: AnalysisRun,
    *,
    thread_id: str,
    status: ArtifactStatus,
    prior_analysis_run: ArtifactRef | None = None,
    reviewer_feedback: str | None = None,
) -> ArtifactRef:
    if (prior_analysis_run is None) != (reviewer_feedback is None):
        raise ValueError(
            "prior_analysis_run and reviewer_feedback must be provided together"
        )
    provenance: dict[str, Any] = {
        "producer": "epi_agent",
        "thread_id": thread_id,
    }
    if run.dataset is not None:
        provenance["dataset"] = run.dataset.model_dump(mode="json")
    if prior_analysis_run is not None:
        provenance["revision_of"] = {
            "id": prior_analysis_run.id,
            "kind": prior_analysis_run.kind,
            "version": prior_analysis_run.version,
        }
        provenance["reviewer_feedback"] = reviewer_feedback
    return store.save_artifact(
        kind="analysis_run",
        status=status,
        content=run.model_dump(mode="json"),
        provenance=provenance,
        summary=(
            f"{run.method} analysis of {run.dataset.id}"
            if run.dataset is not None
            else f"{run.method} standalone analysis"
        ),
    )


def save_analysis_figure(
    store: ArtifactStore,
    figure_png: bytes,
    *,
    thread_id: str,
) -> ArtifactRef:
    if not figure_png:
        raise ValueError("figure_png must not be empty")
    return store.save_artifact(
        kind="figure",
        mime="image/png",
        status="pending_review",
        content={
            "data_base64": base64.b64encode(figure_png).decode("ascii"),
        },
        provenance={
            "producer": "epi_agent",
            "thread_id": thread_id,
        },
        summary="Python analysis figure pending review",
    )
