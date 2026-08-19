from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt
from pydantic import BaseModel
import pytest

from epi_agent.protocol import (
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import EpiAgentRuntimeConfig, _execute_tools
from epi_agent.studies import StudyRegistry
from utils.model_runtime_profiles import model_runtime_profile
from utils.run_cancellation import (
    CancellationToken,
    RunCancelled,
    bind_cancellation,
)


class _NoArguments(BaseModel):
    pass


@dataclass
class _SuccessfulTool:
    name: str
    calls: list[str] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="Succeed.",
            args_model=_NoArguments,
            read_only=True,
        )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        self.calls.append(self.name)
        return ToolResult(message=f"{self.name} completed")


@dataclass(frozen=True)
class _UnexpectedFailureTool:
    spec: ToolSpec = ToolSpec(
        name="unexpected_failure",
        description="Raise an unexpected exception.",
        args_model=_NoArguments,
        read_only=True,
    )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        raise AttributeError("private implementation detail")


@dataclass(frozen=True)
class _TerminalFailureTool:
    spec: ToolSpec = ToolSpec(
        name="terminal_failure",
        description="Raise a structured terminal error.",
        args_model=_NoArguments,
        read_only=True,
    )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        raise ToolExecutionError(
            "EXPECTED_TERMINAL_FAILURE",
            "The request cannot continue.",
            recoverable=False,
        )


@dataclass(frozen=True)
class _InterruptingTool:
    spec: ToolSpec = ToolSpec(
        name="interrupting_tool",
        description="Interrupt the graph.",
        args_model=_NoArguments,
        read_only=True,
    )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        raise GraphInterrupt()


def _call(name: str, call_id: str) -> dict[str, Any]:
    return {
        "name": name,
        "args": {},
        "id": call_id,
        "type": "tool_call",
    }


def _state(*calls: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": [AIMessage(content="", tool_calls=list(calls))],
        "artifacts": {},
    }


def _config(registry: ToolRegistry) -> EpiAgentRuntimeConfig:
    studies = StudyRegistry()
    return EpiAgentRuntimeConfig(
        agent_name="test_agent",
        system_prompt="Use tools.",
        registry=registry,
        studies=studies,
        context_factory=lambda _state, _config, store: ToolContext(
            studies=studies,
            artifact_store=store,
            thread_id="thread-1",
            policy=None,
        ),
        model_profile=model_runtime_profile("gpt-5.4"),
    )


def _execute(registry: ToolRegistry, *calls: dict[str, Any]) -> dict[str, Any]:
    return _execute_tools(
        _state(*calls),
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=_config(registry),
    )


def test_unexpected_tool_exception_closes_call_and_stops_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="epi_agent.runtime")

    patch = _execute(
        ToolRegistry([_UnexpectedFailureTool()]),
        _call("unexpected_failure", "call-1"),
    )

    assert patch["terminal_error"] == {
        "code": "INTERNAL_TOOL_ERROR",
        "message": (
            "A tool failed unexpectedly. This request was stopped, but you "
            "can continue the conversation."
        ),
        "recoverable": False,
    }
    assert [message.tool_call_id for message in patch["messages"]] == ["call-1"]
    assert patch["messages"][0].status == "error"
    public_payload = json.loads(str(patch["messages"][0].content))
    assert public_payload["error"]["code"] == "INTERNAL_TOOL_ERROR"
    assert "AttributeError" not in json.dumps(public_payload)
    assert "private implementation detail" not in json.dumps(public_payload)
    assert "private implementation detail" in caplog.text


def test_unexpected_failure_closes_remaining_read_only_batch() -> None:
    completed = _SuccessfulTool("completed_tool")
    never_run = _SuccessfulTool("never_run")

    patch = _execute(
        ToolRegistry([completed, _UnexpectedFailureTool(), never_run]),
        _call("completed_tool", "call-1"),
        _call("unexpected_failure", "call-2"),
        _call("never_run", "call-3"),
    )

    assert completed.calls == ["completed_tool"]
    assert never_run.calls == []
    assert [message.tool_call_id for message in patch["messages"]] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    assert [message.status for message in patch["messages"]] == [
        "success",
        "error",
        "error",
    ]


def test_nonrecoverable_tool_error_closes_remaining_read_only_batch() -> None:
    never_run = _SuccessfulTool("never_run")

    patch = _execute(
        ToolRegistry([_TerminalFailureTool(), never_run]),
        _call("terminal_failure", "call-1"),
        _call("never_run", "call-2"),
    )

    assert never_run.calls == []
    assert [message.tool_call_id for message in patch["messages"]] == [
        "call-1",
        "call-2",
    ]
    assert json.loads(str(patch["messages"][0].content))["error"]["code"] == (
        "EXPECTED_TERMINAL_FAILURE"
    )
    assert json.loads(str(patch["messages"][1].content))["error"]["code"] == (
        "INTERNAL_TOOL_ERROR"
    )


def test_tool_executor_propagates_graph_interrupt() -> None:
    with pytest.raises(GraphInterrupt):
        _execute(
            ToolRegistry([_InterruptingTool()]),
            _call("interrupting_tool", "call-1"),
        )


def test_tool_executor_propagates_run_cancellation() -> None:
    token = CancellationToken()
    token.cancel()

    with bind_cancellation(token), pytest.raises(RunCancelled):
        _execute(
            ToolRegistry([_SuccessfulTool("successful_tool")]),
            _call("successful_tool", "call-1"),
        )
