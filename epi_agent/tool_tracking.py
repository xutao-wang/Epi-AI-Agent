"""Best-effort tool provenance, kept outside graph state and model messages."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import logging
import time
from typing import Any
from uuid import uuid4


_LOGGER = logging.getLogger(__name__)
_READS: ContextVar[dict | None] = ContextVar("tool_artifact_reads", default=None)


def observe_artifact_read(artifact: Any) -> None:
    reads = _READS.get()
    if reads is not None:
        ref = {"id": artifact.id, "kind": artifact.kind, "version": artifact.version}
        reads[(artifact.id, artifact.kind, artifact.version)] = ref


class ToolTracking:
    def __init__(self, sink: Any, thread_id: str, call: dict, store: Any) -> None:
        self.sink = sink
        self.thread_id = thread_id
        self.store = store
        self.reads: dict = {}
        self.before: dict = {}
        self.started = time.monotonic()
        self.record = {
            "schema_version": "tool-execution-v1",
            "execution_id": uuid4().hex,
            "tool_call_id": str(call["id"]),
            "tool_name": str(call["name"]),
            "arguments_hash": None,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "duration_seconds": None,
            "error_code": None,
            "recoverable": None,
            "accessed_artifacts": [],
            "created_artifacts": [],
            "updated_artifacts": [],
            "returned_artifacts": [],
        }
        # Legacy sinks need not implement this optional capability.
        self.enabled = callable(getattr(sink, "tool_execution", None))
        if self.enabled:
            try:
                encoded = json.dumps(call["args"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                self.record["arguments_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
                self.before = store.tracking_manifest()
            except Exception:
                _LOGGER.exception("Tool provenance preparation failed")
            self._emit()

    def _emit(self) -> None:
        try:
            self.sink.tool_execution(self.thread_id, dict(self.record))
        except Exception:
            _LOGGER.exception("Tool provenance persistence failed")

    @contextmanager
    def capture_reads(self):
        token = _READS.set(self.reads if self.enabled else None)
        try:
            yield
        finally:
            _READS.reset(token)

    def finish(self, status: str, *, error: Any = None, result: Any = None) -> None:
        if not self.enabled:
            return
        try:
            after = self.store.tracking_manifest()
            self.record.update(
                status=status,
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_seconds=max(0.0, time.monotonic() - self.started),
                error_code=getattr(error, "code", None),
                recoverable=getattr(error, "recoverable", None),
                accessed_artifacts=list(self.reads.values()),
                created_artifacts=[ref for key, ref in after.items() if key not in self.before],
                updated_artifacts=[ref for key, ref in after.items() if key in self.before and ref != self.before[key]],
                returned_artifacts=[
                    {"id": ref.id, "kind": ref.kind, "version": ref.version}
                    for ref in (list(result.artifacts) + list(result.output_artifacts) if result else [])
                ],
            )
            self._emit()
        except Exception:
            _LOGGER.exception("Tool provenance completion failed")


def record_unexecuted(sink: Any, thread_id: str, calls: list[dict], store: Any, *, status: str, error: Any) -> None:
    for call in calls:
        ToolTracking(sink, thread_id, call, store).finish(status, error=error)
