from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Any, Iterator

TimingRecord = dict[str, Any]

_ACTIVE_TIMING_RECORDS: ContextVar[list[TimingRecord] | None] = ContextVar(
    "active_timing_records",
    default=None,
)


@contextmanager
def collect_timings() -> Iterator[list[TimingRecord]]:
    records: list[TimingRecord] = []
    token = _ACTIVE_TIMING_RECORDS.set(records)
    try:
        yield records
    finally:
        _ACTIVE_TIMING_RECORDS.reset(token)


@contextmanager
def timing_stage(stage: str, **metadata: Any) -> Iterator[None]:
    records = _ACTIVE_TIMING_RECORDS.get()
    if records is None:
        yield
        return

    start = perf_counter()
    try:
        yield
    finally:
        elapsed_ms = round((perf_counter() - start) * 1000, 2)
        record: TimingRecord = {"stage": stage, "elapsed_ms": elapsed_ms}
        for key, value in metadata.items():
            if value is not None:
                record[key] = value
        records.append(record)
