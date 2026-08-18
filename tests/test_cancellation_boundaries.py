from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
import pandas as pd
from pydantic import BaseModel
import pytest

from epi_agent.protocol import ToolContext, ToolResult, ToolSpec
from epi_agent.agent import build_epi_agent_context_prompt
from epi_agent.registry import ToolRegistry
import epi_agent.runtimes.python.local_process as local_process
import epi_agent.runtime as epi_runtime
from epi_agent.runtimes.python import PythonExecutionRequest
from epi_agent.runtime import (
    EpiAgentRuntimeConfig,
    GenericEpiAgentState,
    _execute_tools,
    build_epi_agent_graph,
)
from epi_agent.studies import StudyBundle, StudyRegistry
from utils.model_runtime_profiles import model_runtime_profile
from utils.run_cancellation import (
    CancellationToken,
    RunCancelled,
    bind_cancellation,
)


class EmptyArguments(BaseModel):
    pass


def _studies() -> StudyRegistry:
    return StudyRegistry(
        [
            StudyBundle(
                study_id="study-1",
                label="Study",
                knowledge=object(),
                catalog=object(),
                data_sources={},
            )
        ]
    )


@dataclass(frozen=True)
class CancellingFunctionTool:
    token: CancellationToken
    spec: ToolSpec = field(
        default=ToolSpec(
            name="cancel_tool",
            description="Cancel before returning.",
            args_model=EmptyArguments,
        ),
        init=False,
    )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        self.token.cancel()
        return ToolResult(message="late tool result")


class CancellingModel:
    def __init__(self, token: CancellationToken) -> None:
        self.token = token

    def bind_tools(self, _schemas: list[dict[str, Any]]) -> CancellingModel:
        return self

    def invoke(self, _messages: list[Any], **_kwargs: Any) -> AIMessage:
        self.token.cancel()
        return AIMessage(content="late answer")


def _runtime_config(registry: ToolRegistry) -> EpiAgentRuntimeConfig:
    studies = _studies()
    return EpiAgentRuntimeConfig(
        model_profile=model_runtime_profile("gpt-5.4"),
        agent_name="test_agent",
        system_prompt="Use tools.",
        registry=registry,
        studies=studies,
        context_factory=lambda _state, _config, artifact_store: ToolContext(
            studies=studies,
            artifact_store=artifact_store,
            thread_id="cancel-tool",
            policy=None,
        ),
    )


def _tool_state() -> dict[str, Any]:
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "cancel_tool",
                        "args": {},
                        "id": "cancel-call-1",
                        "type": "tool_call",
                    }
                ],
            )
        ],
        "artifacts": {},
    }


def _initial_state(text: str) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content=text)],
        "active_study_id": "study-1",
        "artifact_ids": [],
        "artifacts": {},
        "final_response": None,
        "iteration_count": 0,
        "failure_signatures": [],
        "current_turn_artifact_refs": [],
    }


def test_model_result_returned_after_cancel_is_discarded() -> None:
    token = CancellationToken()
    graph = build_epi_agent_graph(
        state_schema=GenericEpiAgentState,
        config=_runtime_config(ToolRegistry([])),
        model=CancellingModel(token),
    )

    with bind_cancellation(token), pytest.raises(RunCancelled):
        graph.invoke(
            _initial_state("Start work"),
            {"configurable": {"thread_id": "cancel-model"}},
        )


def test_tool_result_returned_after_cancel_is_discarded() -> None:
    token = CancellationToken()
    registry = ToolRegistry([CancellingFunctionTool(token=token)])

    with bind_cancellation(token), pytest.raises(RunCancelled):
        _execute_tools(
            _tool_state(),
            {"configurable": {"thread_id": "cancel-tool"}},
            agent_config=_runtime_config(registry),
        )


class CancellingProcess:
    pid = 12345

    def __init__(self, token: CancellationToken) -> None:
        self.token = token
        self.returncode: int | None = None
        self.terminated = False

    def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
        self.token.cancel()
        raise subprocess.TimeoutExpired("python", timeout)

    def poll(self) -> int | None:
        return self.returncode


def test_python_process_is_terminated_when_run_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = CancellationToken()
    fake_process = CancellingProcess(token)
    monkeypatch.setattr(
        local_process.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_process,
    )

    def terminate(process: CancellingProcess) -> None:
        process.terminated = True
        process.returncode = -15

    monkeypatch.setattr(local_process, "_terminate_process_group", terminate)
    runtime = local_process.LocalPythonRuntime(
        runtime_root=tmp_path,
        timeout_seconds=5,
    )
    request = PythonExecutionRequest(
        code="print(dataset.head())",
        selected_dataset_id="dataset-1",
    )
    dataframe = pd.DataFrame({"value": [1]})

    with bind_cancellation(token), pytest.raises(RunCancelled):
        runtime.execute(request, {"dataset-1": dataframe})

    assert fake_process.terminated is True
    assert list(tmp_path.iterdir()) == []


def test_cancelled_turn_is_bounded_inactive_context_for_a_later_turn() -> None:
    prompt = build_epi_agent_context_prompt(
        {
            "messages": [HumanMessage(content="Continue where we left off")],
            "artifacts": {},
            "authorized_attachment_ids": [],
            "current_turn_artifact_refs": [],
            "cancelled_turn": {
                "text": "Analyze the attached cohort",
                "attachment_ids": ["attachment-cohort"],
            },
        }
    )

    assert "Analyze the attached cohort" in prompt
    assert "attachment-cohort" in prompt
    assert "inactive" in prompt
    assert "explicitly asks to retry, continue, or refer" in prompt
    assert "restart tools" in prompt.lower()
    assert "reattach" in prompt.lower()


def test_context_omits_cancelled_turn_guidance_when_none_exists() -> None:
    prompt = build_epi_agent_context_prompt(
        {
            "messages": [HumanMessage(content="Start a new analysis")],
            "artifacts": {},
            "authorized_attachment_ids": [],
            "current_turn_artifact_refs": [],
        }
    )

    assert "Most recent cancelled user turn" not in prompt


def test_model_output_gate_does_not_resume_after_active_run_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = CancellationToken()
    token.cancel()
    interrupt_called = False

    def resume_decision(_payload: dict[str, Any]) -> dict[str, str]:
        nonlocal interrupt_called
        interrupt_called = True
        return {"action": "continue"}

    monkeypatch.setattr(epi_runtime, "interrupt", resume_decision)

    with bind_cancellation(token), pytest.raises(RunCancelled):
        epi_runtime._model_output_gate(
            {"model_output_state": {"phase": "awaiting_user"}},
            agent_config=_runtime_config(ToolRegistry([])),
        )

    assert interrupt_called is False
