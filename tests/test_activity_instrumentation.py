from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from epi_agent.activity import notify_activity
from epi_agent.protocol import ToolContext, ToolExecutionError, ToolResult, ToolSpec
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import (
    EpiAgentRuntimeConfig,
    _acall_model,
    _call_model,
    _execute_tools,
)
from epi_agent.studies import StudyRegistry
from graph.state import MetaKeys
from utils.model_runtime_profiles import model_runtime_profile


class EmptyArguments(BaseModel):
    pass


class RecordingSink:
    def __init__(self, events: list[tuple[Any, ...]] | None = None) -> None:
        self.events = events if events is not None else []

    def model_started(self, thread_id: str) -> None:
        self.events.append(("model_started", thread_id))

    def model_completed(self, thread_id: str) -> None:
        self.events.append(("model_completed", thread_id))

    def tool_started(
        self,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        self.events.append(
            ("tool_started", thread_id, tool_call_id, tool_name)
        )

    def tool_completed(
        self,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        self.events.append(
            ("tool_completed", thread_id, tool_call_id, tool_name)
        )

    def tool_recoverable_failure(
        self,
        thread_id: str,
        tool_call_id: str,
    ) -> None:
        self.events.append(
            ("tool_recoverable_failure", thread_id, tool_call_id)
        )


@dataclass(frozen=True)
class SuccessfulTool:
    spec = ToolSpec(
        name="successful_tool_1",
        description="Succeed.",
        args_model=EmptyArguments,
    )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        return ToolResult(message="success")


@dataclass(frozen=True)
class FailingTool:
    spec = ToolSpec(
        name="failing_tool",
        description="Fail recoverably.",
        args_model=EmptyArguments,
    )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        raise ToolExecutionError(
            "RETRY_ME",
            "private provider failure",
            recoverable=True,
        )


def _runtime_config(
    registry: ToolRegistry,
    sink: RecordingSink,
) -> EpiAgentRuntimeConfig:
    studies = StudyRegistry()
    return EpiAgentRuntimeConfig(
        model_profile=model_runtime_profile("gpt-5.4"),
        agent_name="test_agent",
        system_prompt="Use tools.",
        registry=registry,
        studies=studies,
        context_factory=lambda _state, config, artifact_store: ToolContext(
            studies=studies,
            artifact_store=artifact_store,
            thread_id=str(config["configurable"]["thread_id"]),
            policy=None,
        ),
        activity_sink=sink,
    )


def _model_state() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="Answer this question.")],
        "artifacts": {},
        "meta": {MetaKeys.LAST_USER_MESSAGE_HASH: "turn-1"},
        "iteration_count": 0,
        "failure_signatures": [],
        "current_turn_artifact_refs": [],
        "current_turn_output_artifact_refs": [],
        "analysis_review_feedback_history": [],
        "model_output_state": {},
    }


def _tool_state(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "messages": [AIMessage(content="", tool_calls=calls)],
        "artifacts": {},
        "failure_signatures": [],
    }


def test_execute_tools_emits_success_and_recoverable_failure_activity() -> None:
    sink = RecordingSink()
    config = _runtime_config(
        ToolRegistry([SuccessfulTool(), FailingTool()]),
        sink,
    )

    _execute_tools(
        _tool_state(
            [
                {
                    "name": "successful_tool_1",
                    "args": {},
                    "id": "success-1",
                    "type": "tool_call",
                },
                {
                    "name": "failing_tool",
                    "args": {},
                    "id": "failure-1",
                    "type": "tool_call",
                },
            ]
        ),
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=config,
    )

    assert sink.events == [
        ("tool_started", "thread-1", "success-1", "successful_tool_1"),
        ("tool_completed", "thread-1", "success-1", "successful_tool_1"),
        ("tool_started", "thread-1", "failure-1", "failing_tool"),
        ("tool_recoverable_failure", "thread-1", "failure-1"),
    ]


def test_execute_tools_uses_public_conversation_thread_id() -> None:
    sink = RecordingSink()

    _execute_tools(
        _tool_state(
            [
                {
                    "name": "successful_tool_1",
                    "args": {},
                    "id": "success-1",
                    "type": "tool_call",
                }
            ]
        ),
        {
            "configurable": {
                "thread_id": "owner-hashed-checkpoint-id",
                "conversation_thread_id": "public-thread-id",
            }
        },
        agent_config=_runtime_config(
            ToolRegistry([SuccessfulTool()]),
            sink,
        ),
    )

    assert sink.events == [
        ("tool_started", "public-thread-id", "success-1", "successful_tool_1"),
        ("tool_completed", "public-thread-id", "success-1", "successful_tool_1"),
    ]


def test_unknown_tool_is_not_exposed_as_public_activity() -> None:
    sink = RecordingSink()
    result = _execute_tools(
        _tool_state(
            [
                {
                    "name": "model_invented_tool",
                    "args": {"private": "value"},
                    "id": "unknown-1",
                    "type": "tool_call",
                }
            ]
        ),
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=_runtime_config(ToolRegistry(), sink),
    )

    assert sink.events == []
    assert result["messages"][0].status == "error"


class ScriptedModel:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events

    def bind_tools(self, _schemas: list[dict[str, Any]]) -> ScriptedModel:
        return self

    def invoke(self, _messages: list[Any], **_kwargs: Any) -> AIMessage:
        self.events.append(("provider_invoked", "sync"))
        return AIMessage(content="Done.")

    async def ainvoke(self, _messages: list[Any], **_kwargs: Any) -> AIMessage:
        self.events.append(("provider_invoked", "async"))
        return AIMessage(content="Done.")


def test_sync_and_async_model_boundaries_emit_ordered_activity() -> None:
    sync_events: list[tuple[Any, ...]] = []
    _call_model(
        _model_state(),
        {"configurable": {"thread_id": "thread-sync"}},
        agent_config=_runtime_config(ToolRegistry(), RecordingSink(sync_events)),
        model=ScriptedModel(sync_events),
    )
    assert sync_events == [
        ("model_started", "thread-sync"),
        ("provider_invoked", "sync"),
        ("model_completed", "thread-sync"),
    ]

    async_events: list[tuple[Any, ...]] = []
    asyncio.run(
        _acall_model(
            _model_state(),
            {"configurable": {"thread_id": "thread-async"}},
            agent_config=_runtime_config(
                ToolRegistry(),
                RecordingSink(async_events),
            ),
            model=ScriptedModel(async_events),
        )
    )
    assert async_events == [
        ("model_started", "thread-async"),
        ("provider_invoked", "async"),
        ("model_completed", "thread-async"),
    ]


def test_model_activity_uses_public_conversation_thread_id() -> None:
    events: list[tuple[Any, ...]] = []

    _call_model(
        _model_state(),
        {
            "configurable": {
                "thread_id": "owner-hashed-checkpoint-id",
                "conversation_thread_id": "public-thread-id",
            }
        },
        agent_config=_runtime_config(ToolRegistry(), RecordingSink(events)),
        model=ScriptedModel(events),
    )

    assert events == [
        ("model_started", "public-thread-id"),
        ("provider_invoked", "sync"),
        ("model_completed", "public-thread-id"),
    ]


class ExplodingSink:
    def model_started(self, _thread_id: str) -> None:
        raise RuntimeError("activity store unavailable")


def test_activity_notification_failure_never_escapes() -> None:
    notify_activity(ExplodingSink(), "model_started", "thread-1")
