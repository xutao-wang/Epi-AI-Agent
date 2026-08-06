from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from epi_agent.analysis_artifacts import (
    AnalysisRun,
    ArtifactIdentity,
    analysis_revision,
    save_analysis_figure,
    save_analysis_run,
)
from epi_agent.datasets import resolve_dataset
from epi_agent.protocol import (
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)
from epi_agent.registry import ToolRegistry
from epi_agent.runtimes.python import (
    PythonExecutionRequest,
    PythonExecutionResult,
    PythonRuntimeFailure,
)


Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
Description = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000),
]
SourceCode = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100_000),
]
_MAX_CUSTOM_MESSAGE_CHARS = 12_000


class PythonRuntime(Protocol):
    def execute(
        self,
        request: PythonExecutionRequest,
        datasets: Mapping[str, pd.DataFrame],
    ) -> PythonExecutionResult: ...


class CustomPythonArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    dataset_id: Identifier
    dataset_kind: Identifier
    dataset_version: int = Field(ge=1)
    analysis_goal: Description
    code: SourceCode = Field(
        description=(
            "Python analysis code. The exact approved input is available as "
            "the already-loaded pandas.DataFrame `dataset`; analyze it "
            "directly and do not load attachment files or discover paths."
        )
    )
    code_summary: Description
    code_assumptions: Description
    dataset_source: Literal["current_upload", "prior_artifact"]
    dataset_source_reason: Description


def _runtime_failure(error: PythonRuntimeFailure) -> ToolExecutionError:
    return ToolExecutionError(
        error.code,
        json.dumps(
            {
                "category": error.category,
                "message": str(error),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        recoverable=error.recoverable,
    )


def _result_message(run: AnalysisRun) -> str:
    payload = {
        "method": run.method,
        "analysis_run_status": "pending_review",
        "output_chars": len(run.output_text),
        "figure_count": len(run.figures),
        "warning_count": len(run.warnings),
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )[:_MAX_CUSTOM_MESSAGE_CHARS]


class RunCustomPythonTool:
    spec = ToolSpec(
        name="analysis-run_custom_python",
        description=(
            "Run bounded custom Python against one exact approved dataset, "
            "exposed to generated code as the already-loaded pandas.DataFrame "
            "`dataset`. Execution does not pause for code review; its exact "
            "result is staged for human review before publication."
        ),
        args_model=CustomPythonArguments,
        read_only=False,
        interrupting=False,
    )

    def __init__(
        self,
        runtime: PythonRuntime,
        *,
        runtime_root: str | Path | None,
    ) -> None:
        self._runtime = runtime
        self._runtime_root = runtime_root

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        stored, dataframe = resolve_dataset(
            arguments,
            context,
            runtime_root=context.thread_storage or self._runtime_root,
        )
        source_attachment_ids = {
            str(attachment_id)
            for attachment_id in list(
                dict(stored.content.get("provenance") or {}).get(
                    "source_attachment_ids"
                )
                or []
            )
            if str(attachment_id)
        }
        if arguments["dataset_source"] == "current_upload" and (
            not source_attachment_ids
            or source_attachment_ids.isdisjoint(context.current_attachment_ids)
        ):
            raise ToolExecutionError(
                "DATASET_SOURCE_MISMATCH",
                "The selected dataset was not derived from the current uploaded table.",
                recoverable=True,
            )
        request = PythonExecutionRequest(
            code=arguments["code"],
            selected_dataset_id=stored.id,
        )
        try:
            execution = self._runtime.execute(
                request,
                {stored.id: dataframe},
            )
        except PythonRuntimeFailure as error:
            raise _runtime_failure(error) from error

        if not execution.output_text.strip() and not execution.figure_png:
            raise ToolExecutionError(
                "PYTHON_EMPTY_OUTPUT",
                (
                    "Custom Python must print reviewable output or create "
                    "a Matplotlib figure."
                ),
                recoverable=True,
            )
        figure_references = ()
        if execution.figure_png:
            figure_references = (
                save_analysis_figure(
                    context.artifact_store,
                    execution.figure_png,
                    thread_id=context.thread_id,
                ),
            )
        run = AnalysisRun(
            method="custom_python",
            dataset=ArtifactIdentity(
                id=stored.id,
                kind=stored.kind,
                version=stored.version,
            ),
            specification={
                "analysis_goal": arguments["analysis_goal"],
                "code": arguments["code"],
                "code_summary": arguments["code_summary"],
                "code_assumptions": arguments["code_assumptions"],
                "dataset_source": arguments["dataset_source"],
                "dataset_source_reason": arguments["dataset_source_reason"],
            },
            output_text=execution.output_text,
            runtime=execution.runtime.model_dump(mode="json"),
            estimates=[],
            diagnostics={},
            warnings=execution.warnings,
            figures=[
                ArtifactIdentity(
                    id=reference.id,
                    kind=reference.kind,
                    version=reference.version,
                )
                for reference in figure_references
            ],
        )
        prior_analysis_run, reviewer_feedback = analysis_revision(context)
        run_reference = save_analysis_run(
            context.artifact_store,
            run,
            thread_id=context.thread_id,
            status="pending_review",
            prior_analysis_run=prior_analysis_run,
            reviewer_feedback=reviewer_feedback,
        )
        return ToolResult(
            message=_result_message(run),
            artifacts=(*figure_references, run_reference),
        )


def build_custom_python_tool_registry(
    runtime: PythonRuntime,
    *,
    runtime_root: str | Path | None,
) -> ToolRegistry:
    return ToolRegistry(
        [
            RunCustomPythonTool(
                runtime,
                runtime_root=runtime_root,
            )
        ]
    )


__all__ = [
    "CustomPythonArguments",
    "RunCustomPythonTool",
    "build_custom_python_tool_registry",
]
