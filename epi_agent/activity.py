from __future__ import annotations

import logging
from typing import Any, Protocol


logger = logging.getLogger(__name__)


class ActivitySink(Protocol):
    def model_started(self, thread_id: str) -> None: ...

    def model_completed(self, thread_id: str) -> None: ...

    def tool_started(
        self,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None: ...

    def tool_completed(
        self,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None: ...

    def tool_recoverable_failure(
        self,
        thread_id: str,
        tool_call_id: str,
    ) -> None: ...


class NullActivitySink:
    def model_started(self, _thread_id: str) -> None:
        return None

    def model_completed(self, _thread_id: str) -> None:
        return None

    def tool_started(
        self,
        _thread_id: str,
        _tool_call_id: str,
        _tool_name: str,
    ) -> None:
        return None

    def tool_completed(
        self,
        _thread_id: str,
        _tool_call_id: str,
        _tool_name: str,
    ) -> None:
        return None

    def tool_recoverable_failure(
        self,
        _thread_id: str,
        _tool_call_id: str,
    ) -> None:
        return None


NULL_ACTIVITY_SINK = NullActivitySink()


def notify_activity(sink: ActivitySink, operation: str, *args: Any) -> None:
    try:
        getattr(sink, operation)(*args)
    except Exception:
        logger.exception(
            "Agent activity notification failed",
            extra={"operation": operation},
        )
