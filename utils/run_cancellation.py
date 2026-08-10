from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading


class RunCancelled(Exception):
    """Internal control flow: the active run must publish no more state."""


@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RunCancelled("The active run was cancelled.")


_ACTIVE_TOKEN: ContextVar[CancellationToken | None] = ContextVar(
    "report_agent_active_cancellation_token",
    default=None,
)


@contextmanager
def bind_cancellation(token: CancellationToken) -> Iterator[None]:
    reset = _ACTIVE_TOKEN.set(token)
    try:
        yield
    finally:
        _ACTIVE_TOKEN.reset(reset)


def cancellation_point() -> None:
    token = _ACTIVE_TOKEN.get()
    if token is not None:
        token.raise_if_cancelled()
