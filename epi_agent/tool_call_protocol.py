from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from epi_agent.protocol import ToolExecutionError


INTERNAL_TOOL_ERROR_CODE = "INTERNAL_TOOL_ERROR"
INTERNAL_TOOL_ERROR_MESSAGE = (
    "A tool failed unexpectedly. This request was stopped, but you can "
    "continue the conversation."
)


def tool_error_content(error: ToolExecutionError) -> str:
    error_payload: dict[str, Any] = {
        "code": error.code,
        "message": str(error),
        "recoverable": error.recoverable,
    }
    if error.details is not None:
        error_payload["details"] = error.details
    return json.dumps({"error": error_payload}, sort_keys=True)


def internal_tool_error() -> ToolExecutionError:
    return ToolExecutionError(
        INTERNAL_TOOL_ERROR_CODE,
        INTERNAL_TOOL_ERROR_MESSAGE,
        recoverable=False,
    )


def error_tool_message(
    call: Mapping[str, Any],
    error: ToolExecutionError | None = None,
) -> ToolMessage:
    selected_error = error or internal_tool_error()
    return ToolMessage(
        content=tool_error_content(selected_error),
        tool_call_id=str(call["id"]),
        name=str(call.get("name") or ""),
        status="error",
    )


def aborted_tool_messages(
    calls: Sequence[Mapping[str, Any]],
) -> list[ToolMessage]:
    return [error_tool_message(call) for call in calls]


@dataclass(frozen=True)
class ToolCallRepair:
    messages: tuple[BaseMessage, ...]
    repaired_call_ids: tuple[str, ...]


def _repair_tool_message(call: Mapping[str, Any]) -> ToolMessage:
    call_id = str(call["id"])
    return error_tool_message(call).model_copy(
        update={
            "id": (
                "tool-repair-"
                + sha256(call_id.encode("utf-8")).hexdigest()[:24]
            )
        }
    )


def repair_orphaned_tool_calls(
    messages: Sequence[BaseMessage],
) -> ToolCallRepair:
    repaired: list[BaseMessage] = []
    repaired_call_ids: list[str] = []
    pending: dict[str, Mapping[str, Any]] = {}

    def flush_pending() -> None:
        for call_id, call in pending.items():
            repaired.append(_repair_tool_message(call))
            repaired_call_ids.append(call_id)
        pending.clear()

    for message in messages:
        if pending and not isinstance(message, ToolMessage):
            flush_pending()
        repaired.append(message)
        if isinstance(message, AIMessage):
            pending.update(
                (str(call["id"]), call)
                for call in list(message.tool_calls or [])
            )
        elif isinstance(message, ToolMessage):
            pending.pop(str(message.tool_call_id), None)
    flush_pending()
    return ToolCallRepair(
        messages=tuple(repaired),
        repaired_call_ids=tuple(repaired_call_ids),
    )


def follow_up_message_patch(
    messages: Sequence[BaseMessage],
    message: HumanMessage,
) -> list[BaseMessage]:
    repair = repair_orphaned_tool_calls(messages)
    if not repair.repaired_call_ids:
        return [message]
    return [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *repair.messages,
        message,
    ]
