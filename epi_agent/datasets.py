from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel

from epi_agent.artifacts import StoredArtifact
from epi_agent.protocol import ArtifactRef, ToolContext, ToolExecutionError
from utils.dataset_artifacts import (
    is_selectable_dataset_artifact,
    load_dataset_artifact,
)
from utils.user_storage import ThreadStorageScope


def _argument_mapping(
    arguments: BaseModel | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(arguments, BaseModel):
        return arguments.model_dump(mode="python")
    return dict(arguments)


def resolve_dataset(
    arguments: BaseModel | dict[str, Any],
    context: ToolContext,
    *,
    runtime_root: str | Path | ThreadStorageScope | None,
) -> tuple[StoredArtifact, pd.DataFrame]:
    payload = _argument_mapping(arguments)
    reference = ArtifactRef(
        id=str(payload["dataset_id"]),
        kind=str(payload["dataset_kind"]),
        version=int(payload["dataset_version"]),
    )
    try:
        stored = context.artifact_store.require(reference)
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolExecutionError(
            "DATASET_NOT_AVAILABLE",
            "The exact requested dataset artifact is unavailable or stale.",
            recoverable=True,
        ) from exc

    selectable_content = {
        **dict(stored.content),
        "status": stored.status,
    }
    if not is_selectable_dataset_artifact(selectable_content):
        raise ToolExecutionError(
            "DATASET_NOT_SELECTABLE",
            (
                f"Dataset {stored.id} has status={stored.status} and cannot "
                "be analyzed."
            ),
            recoverable=True,
        )

    try:
        dataframe, _schema = load_dataset_artifact(
            stored.content,
            runtime_root=runtime_root,
        )
    except (OSError, KeyError, ValueError) as exc:
        raise ToolExecutionError(
            "DATASET_LOAD_FAILED",
            f"Dataset {stored.id} could not be loaded from artifact storage.",
            recoverable=False,
        ) from exc
    return stored, dataframe


__all__ = ["resolve_dataset"]
