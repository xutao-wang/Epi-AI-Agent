from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from epi_agent.analysis_artifacts import RuntimePackage


class PythonExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1, max_length=100_000)
    selected_dataset_id: str = Field(min_length=1, max_length=512)


class PythonRuntimeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    language: Literal["Python"] = "Python"
    version: str = Field(min_length=1)
    packages: list[RuntimePackage] = Field(default_factory=list)
    execution_mode: Literal["guarded_local"] = "guarded_local"
    security_boundary: Literal["none"] = "none"


class PythonExecutionResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
    )

    output_text: str = ""
    warnings: list[str] = Field(default_factory=list)
    figure_png: bytes = b""
    runtime: PythonRuntimeManifest
    duration_seconds: float = Field(ge=0)


class PythonRuntimeFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str,
        recoverable: bool,
        execution_started: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.category = category
        self.recoverable = recoverable
        self.execution_started = bool(execution_started)
        self.details = (
            json.loads(json.dumps(details, allow_nan=False))
            if details is not None
            else None
        )
        super().__init__(message)
