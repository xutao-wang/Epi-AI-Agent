from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from langchain_core.messages import ToolMessage

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
