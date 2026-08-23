from __future__ import annotations

import base64
from copy import deepcopy
import csv
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import hashlib
import io
import inspect
import json
import logging
from pathlib import Path
import threading
import time
import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START
from langgraph.types import Command
from langgraph.graph.state import CompiledStateGraph
import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)
import pandas as pd
from pydantic import TypeAdapter, ValidationError

from api.auth import AuthenticatedUser, LOCAL_SESSION_ID, RequestIdentity
from api.schemas import (
    ActiveInterrupt,
    ActivityRun,
    ApiThreadState,
    AttachmentManifestSummary,
    AttachmentUploadError,
    AttachmentUploadResult,
    CompletedAnalysisResult,
    ClarificationExchange,
    ConversationAttachment,
    ConversationMessage,
    DatasetProvenance,
    DatasetPreview,
    DatasetSchemaResponse,
    DatasetSummary,
    FileArtifactSummary,
    ModelOption,
    RunStatus,
    RuntimeInfo,
    RuntimeOptions,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeSettings,
    TablePreview,
)
from api.activity_store import SqliteActivityStore
from api.conversation_history import ConversationHistoryStore, OpenAIConversationTitleGenerator
from epi_agent.analysis_artifacts import AnalysisRun
from epi_agent.tool_call_protocol import follow_up_message_patch
from graph.conversation_events import (
    append_conversation_event,
    build_attachment_event,
    build_user_event,
    ensure_conversation_state,
)
from graph.state import MetaKeys
from utils.artifact_publication import is_published_artifact
from utils.dataset_artifacts import (
    dataset_artifact_description,
    is_pending_review_dataset_artifact,
    is_selectable_dataset_artifact,
    load_dataset_artifact,
)
from utils.attachment_artifacts import (
    AttachmentError,
    LocalAttachmentStore,
    validate_message_attachment_limits,
)
from utils.attachment_readers import AttachmentReaderService
from utils.user_storage import ThreadStorageScope, UserStorageLayout
from utils.display_history import build_display_history
from utils.export_thread import build_thread_export
from db_rag.embedding_startup import (
    EmbeddingStartupStatus,
    silent_embedding_startup_status,
)
from utils.review_interrupts import (
    project_review_interrupt,
    validate_resume_decision,
)
from utils.model_runtime_profiles import (
    MODEL_RUNTIME_PROFILES,
    ModelRuntimeProfile,
)
from utils.provider_errors import classify_llm_error
from utils.run_cancellation import (
    CancellationToken,
    RunCancelled,
    bind_cancellation,
)


_LOGGER = logging.getLogger(__name__)
_ACTIVE_INTERRUPT_ADAPTER = TypeAdapter(ActiveInterrupt)
_PUBLIC_INTERRUPT_TYPES = {
    "dataset_plan_review",
    "dataset_review",
    "analysis_result_review",
    "model_output_limit",
    "db_rag_agent_clarification",
}


def new_thread_id() -> str:
    return uuid.uuid4().hex


def graph_config(
    thread_id: str,
    *,
    owner_user_id: str | None = None,
) -> dict[str, Any]:
    """Build a checkpointer config that cannot collide across owners."""
    checkpoint_thread_id = thread_id
    if owner_user_id is not None and owner_user_id != "local-user":
        digest = hashlib.sha256(
            f"{owner_user_id}\x00{thread_id}".encode("utf-8")
        ).hexdigest()
        checkpoint_thread_id = f"owner-{digest}"
    configurable: dict[str, Any] = {"thread_id": checkpoint_thread_id}
    if owner_user_id is not None and owner_user_id != "local-user":
        configurable["owner_user_id"] = owner_user_id
        configurable["conversation_thread_id"] = thread_id
    return {"configurable": configurable}


def _idle_status() -> dict[str, Any]:
    return {
        "state": "idle",
        "steps": 0,
        "error": None,
        "error_code": None,
        "user_message": None,
        "started_at": None,
        "updated_at": None,
    }


def _public_failure(code: str, message: str) -> tuple[str, str]:
    return code, f"{message} Error: {code}"


def _provider_error_markers(value: Any) -> set[str]:
    markers: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"code", "type"} and item is not None:
                markers.add(str(item).strip().lower())
            markers.update(_provider_error_markers(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            markers.update(_provider_error_markers(item))
    return markers


def _run_failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, (APITimeoutError, httpx.TimeoutException)):
        return _public_failure(
            "MODEL_REQUEST_TIMEOUT",
            "The selected model did not respond within its request timeout.",
        )
    if isinstance(exc, APIConnectionError):
        return _public_failure(
            "OPENAI_CONNECTION_FAILED",
            "The server could not reach OpenAI. Check the network connection, "
            "then retry.",
        )
    if isinstance(exc, AuthenticationError) or (
        isinstance(exc, APIStatusError) and exc.status_code == 401
    ):
        return _public_failure(
            "OPENAI_AUTHENTICATION_FAILED",
            "OpenAI rejected the session API key. Enter a valid key and retry.",
        )
    if isinstance(exc, PermissionDeniedError) or (
        isinstance(exc, APIStatusError) and exc.status_code == 403
    ):
        return _public_failure(
            "OPENAI_ACCESS_DENIED",
            "The configured OpenAI project is not allowed to use this resource. "
            "Check the project's permissions or use another API key.",
        )
    body = exc.body if isinstance(exc, APIStatusError) else {}
    markers = _provider_error_markers(body)
    if isinstance(exc, RateLimitError):
        if markers & {"insufficient_quota", "credit_balance_exhausted"}:
            return _public_failure(
                "OPENAI_CREDITS_EXHAUSTED",
                "The OpenAI account has no remaining API credits. Add credits or "
                "use a funded API key, then retry.",
            )
        return _public_failure(
            "OPENAI_RATE_LIMITED",
            "OpenAI's request limit was reached. Wait briefly, then retry.",
        )
    if "context_length_exceeded" in markers:
        return _public_failure(
            "OPENAI_CONTEXT_LIMIT_EXCEEDED",
            "This conversation exceeds the selected model's context limit. Start a "
            "new conversation or reduce the attached content.",
        )
    if "model_not_found" in markers or (
        isinstance(exc, APIStatusError) and exc.status_code == 404
    ):
        return _public_failure(
            "OPENAI_MODEL_UNAVAILABLE",
            "The selected OpenAI model is unavailable to this API project. Choose "
            "another model and retry.",
        )
    provider_failure = classify_llm_error(exc)
    if provider_failure[0] != "RUN_FAILED":
        return provider_failure
    return _public_failure(
        "RUN_FAILED",
        "The request failed unexpectedly. Check the server log for details.",
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "size": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (HumanMessage, AIMessage)):
        return {
            "type": getattr(value, "type", type(value).__name__),
            "content": _message_text(value),
            "additional_kwargs": _json_safe(
                dict(getattr(value, "additional_kwargs", {}) or {})
            ),
        }
    return str(value)


class ThreadAlreadyRunningError(RuntimeError):
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"Thread {thread_id} is already running")


class ThreadAwaitingReviewError(RuntimeError):
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"Thread {thread_id} is awaiting human review")


class ModelReplacementRequiredError(RuntimeError):
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__(
            f"Model {model_name} is unavailable. Choose an available model "
            "to continue."
        )


class StaleInterruptError(RuntimeError):
    def __init__(self, interrupt_id: str) -> None:
        self.interrupt_id = interrupt_id
        super().__init__(f"Interrupt {interrupt_id} is no longer active")


class InitialTurnCheckpointError(RuntimeError):
    pass


def _has_blocking_interrupt(snapshot: Any) -> bool:
    interrupts = list(getattr(snapshot, "interrupts", None) or [])
    return bool(interrupts)


def _checkpoint_contains_user_turn(
    snapshot: Any,
    *,
    message_id: str,
    turn_hash: str,
) -> bool:
    values = _projection_values(snapshot)
    if any(
        isinstance(message, HumanMessage) and str(message.id or "") == message_id
        for message in list(values.get("messages") or [])
    ):
        return True
    meta = dict(values.get("meta") or {})
    if str(meta.get(MetaKeys.LAST_USER_MESSAGE_HASH) or "") == turn_hash:
        return True
    events = list(dict(values.get("artifacts") or {}).get("conversation_events") or [])
    return any(
        isinstance(event, dict)
        and event.get("type") == "user"
        and str(event.get("user_turn_hash") or "") == turn_hash
        for event in events
    )


def _projection_values(snapshot: Any) -> dict[str, Any]:
    return dict(getattr(snapshot, "values", None) or {})


def _should_recover_snapshot(
    snapshot: Any,
    status: dict[str, Any],
) -> bool:
    values = _projection_values(snapshot)
    return (
        status.get("state") == "idle"
        and bool(list(getattr(snapshot, "next", None) or []))
        and not _has_blocking_interrupt(snapshot)
        and not values.get("terminal_error")
        and not values.get("terminal_control")
    )


@dataclass(frozen=True)
class FileArtifactBytes:
    content: bytes
    mime: str
    filename: str


@dataclass(frozen=True)
class ReadOnlyCheckpointSnapshot:
    values: dict[str, Any]
    next: tuple[str, ...]
    interrupts: tuple[Any, ...]


def _matches_pending_analysis_review(
    artifact_id: str,
    artifact: dict[str, Any],
    active_interrupt: ActiveInterrupt | None,
) -> bool:
    if (
        active_interrupt is None
        or active_interrupt.type != "analysis_result_review"
    ):
        return False
    identity = active_interrupt.artifact
    stored = dict(artifact.get("content") or {})
    return (
        artifact.get("status") == "pending_review"
        and stored.get("status") == "pending_review"
        and stored.get("kind") == "analysis_run"
        and artifact_id == identity.id
        and stored.get("version") == identity.version
        and identity.kind == "analysis_run"
        and identity.expected_status == "pending_review"
    )


def _matches_pending_analysis_linked_output(
    artifact_id: str,
    artifact: dict[str, Any],
    active_interrupt: ActiveInterrupt | None,
    files: dict[str, Any],
) -> bool:
    if (
        active_interrupt is None
        or active_interrupt.type != "analysis_result_review"
    ):
        return False
    run_id = active_interrupt.artifact.id
    run_record = files.get(run_id)
    if not isinstance(run_record, dict) or not _matches_pending_analysis_review(
        run_id,
        run_record,
        active_interrupt,
    ):
        return False
    stored_run = dict(run_record.get("content") or {})
    run_content = dict(stored_run.get("content") or {})
    linked = [
        *list(run_content.get("tables") or []),
        *list(run_content.get("figures") or []),
    ]
    stored_output = dict(artifact.get("content") or {})
    if (
        artifact.get("producer") != "epi_agent"
        or artifact.get("kind") not in {"figure", "table"}
        or artifact.get("status") != "pending_review"
        or stored_output.get("status") != "pending_review"
        or stored_output.get("kind") != artifact.get("kind")
        or stored_output.get("id") != artifact_id
    ):
        return False
    return any(
        isinstance(identity, dict)
        and identity.get("id") == artifact_id
        and identity.get("kind") == stored_output.get("kind")
        and identity.get("version") == stored_output.get("version")
        for identity in linked
    )


@dataclass(frozen=True)
class CancelledTurn:
    message_id: str
    text: str
    turn_hash: str
    attachment_ids: tuple[str, ...]


@dataclass
class GraphJob:
    status: dict[str, Any]
    token: CancellationToken
    durable_config: RunnableConfig | None
    restore: Callable[[RunnableConfig], None] | None = None
    transition_complete: threading.Event = field(default_factory=threading.Event)


class CancellationRestoreError(RuntimeError):
    pass


def _cancelled_turn_from_values(values: dict[str, Any]) -> CancelledTurn:
    events = list(
        dict(values.get("artifacts") or {}).get("conversation_events") or []
    )
    user_event = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, dict) and event.get("type") == "user"
        ),
        None,
    )
    if user_event is None:
        raise CancellationRestoreError(
            "The active run has no user turn to retain."
        )
    turn_hash = str(user_event.get("user_turn_hash") or "")
    user_event_id = str(user_event.get("event_id") or "")
    attachment_ids = tuple(
        dict.fromkeys(
            str(event.get("artifact_id") or "")
            for event in events
            if isinstance(event, dict)
            and event.get("type") == "attachment"
            and event.get("relationship") == "input"
            and (
                str(event.get("parent_event_id") or "") == user_event_id
                or str(event.get("user_turn_hash") or "") == turn_hash
            )
            and str(event.get("artifact_id") or "")
        )
    )
    message_id = user_event_id
    for message in reversed(list(values.get("messages") or [])):
        if isinstance(message, HumanMessage):
            message_id = str(message.id or user_event_id)
            break
    return CancelledTurn(
        message_id=message_id,
        text=str(user_event.get("text") or ""),
        turn_hash=turn_hash,
        attachment_ids=attachment_ids,
    )


def _cancelled_turn_patch(
    durable_values: dict[str, Any],
    *,
    turn: CancelledTurn,
    manifests: list[dict[str, Any]],
) -> dict[str, Any]:
    event_state = ensure_conversation_state(
        {
            "artifacts": durable_values.get("artifacts"),
            "meta": durable_values.get("meta"),
        }
    )
    artifacts = dict(event_state.get("artifacts") or {})
    events = list(artifacts.get("conversation_events") or [])
    user_event_id = ""
    for event in events:
        if (
            isinstance(event, dict)
            and event.get("type") == "user"
            and str(event.get("user_turn_hash") or "") == turn.turn_hash
        ):
            event["status"] = "cancelled"
            user_event_id = str(event.get("event_id") or "")
            break
    if not user_event_id:
        event_state = append_conversation_event(
            event_state,
            build_user_event(
                actor="human",
                user_turn_hash=turn.turn_hash,
                text=turn.text,
                status="cancelled",
            ),
        )
        artifacts = dict(event_state.get("artifacts") or {})
        events = list(artifacts.get("conversation_events") or [])
        user_event_id = str(events[-1]["event_id"])
    else:
        artifacts["conversation_events"] = events
        event_state = {**event_state, "artifacts": artifacts}

    linked_input_ids = {
        str(event.get("artifact_id") or "")
        for event in list(
            dict(event_state.get("artifacts") or {}).get(
                "conversation_events"
            )
            or []
        )
        if isinstance(event, dict)
        and event.get("type") == "attachment"
        and event.get("relationship") == "input"
        and str(event.get("user_turn_hash") or "") == turn.turn_hash
    }
    for attachment_id in turn.attachment_ids:
        if attachment_id in linked_input_ids:
            continue
        event_state = append_conversation_event(
            event_state,
            build_attachment_event(
                actor="api",
                user_turn_hash=turn.turn_hash,
                artifact_id=attachment_id,
                relationship="input",
                parent_event_id=user_event_id,
            ),
        )

    artifacts = dict(event_state.get("artifacts") or {})
    attachments = dict(artifacts.get("attachments") or {})
    available_ids: set[str] = set()
    for manifest in manifests:
        attachment_id = str(manifest.get("id") or "")
        if not attachment_id or manifest.get("status") != "available":
            continue
        attachments[attachment_id] = dict(manifest)
        available_ids.add(attachment_id)
    artifacts["attachments"] = attachments
    meta = dict(event_state.get("meta") or {})
    meta[MetaKeys.LAST_USER_MESSAGE_HASH] = turn.turn_hash
    authorized_attachment_ids = sorted(
        {
            *[
                str(attachment_id)
                for attachment_id in list(
                    durable_values.get("authorized_attachment_ids") or []
                )
                if str(attachment_id)
            ],
            *available_ids,
        }
    )
    return {
        "artifacts": artifacts,
        "meta": meta,
        "authorized_attachment_ids": authorized_attachment_ids,
        "current_turn_artifact_refs": [],
        "current_turn_output_artifact_refs": [],
        "terminal_error": None,
        "final_response": None,
        "completion_blocked": False,
        "terminal_control": {
            "status": "cancelled",
            "reason": "User cancelled the active run.",
        },
        "cancelled_turn": {
            "message_id": turn.message_id,
            "text": turn.text,
            "turn_hash": turn.turn_hash,
            "attachment_ids": list(turn.attachment_ids),
        },
    }


@dataclass
class ApiGraphRunner:
    app: Any
    _jobs: dict[str, GraphJob] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def status(self, thread_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(thread_id)
            if isinstance(job, GraphJob):
                return dict(job.status)
            if isinstance(job, dict):
                return dict(job)
            return _idle_status()

    def _invoke(
        self,
        payload: Any,
        config: dict[str, Any],
        **boundary_options: Any,
    ) -> Any:
        invoke = getattr(self.app, "invoke", None)
        if not callable(invoke):
            return None
        try:
            parameter_map = inspect.signature(invoke).parameters
        except (TypeError, ValueError):
            parameter_map = {}
        accepts_options = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameter_map.values()
        ) or all(option in parameter_map for option in boundary_options)
        if accepts_options:
            return invoke(payload, config, **boundary_options)
        return invoke(payload, config)

    def _supports_invoke_options(self, *options: str) -> bool:
        invoke = getattr(self.app, "invoke", None)
        if not callable(invoke):
            return False
        try:
            parameters = inspect.signature(invoke).parameters
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ) or all(option in parameters for option in options)

    def _durable_config(
        self,
        thread_id: str,
        checkpoint_config: dict[str, Any] | None = None,
    ) -> RunnableConfig | None:
        snapshot = self.app.get_state(
            checkpoint_config or graph_config(thread_id),
            subgraphs=True,
        )
        saved = getattr(snapshot, "config", None)
        return deepcopy(saved) if isinstance(saved, dict) else None

    def _reserve_run(
        self,
        thread_id: str,
        *,
        restore: Callable[[RunnableConfig], None] | None = None,
        checkpoint_config: dict[str, Any] | None = None,
    ) -> tuple[bool, GraphJob]:
        started_at = time.time()
        status = {
            "state": "running",
            "steps": 0,
            "error": None,
            "error_code": None,
            "user_message": None,
            "started_at": started_at,
            "updated_at": started_at,
        }
        with self._lock:
            current = self._jobs.get(thread_id)
            current_status = (
                current.status if isinstance(current, GraphJob) else current
            )
            if current and current_status.get("state") == "running":
                if isinstance(current, GraphJob):
                    return False, current
                compatibility_job = GraphJob(
                    status=current,
                    token=CancellationToken(),
                    durable_config=None,
                )
                return False, compatibility_job
            job = GraphJob(
                status=status,
                token=CancellationToken(),
                durable_config=(
                    self._durable_config(thread_id, checkpoint_config)
                    if restore is not None
                    else None
                ),
                restore=restore,
            )
            self._jobs[thread_id] = job
        return True, job

    def start_background(
        self,
        *,
        thread_id: str,
        initial_payload: Any | None,
        max_steps: int,
        timeout_seconds: float,
        checkpoint_config: dict[str, Any] | None = None,
    ) -> bool:
        started, job = self._reserve_run(
            thread_id,
            checkpoint_config=checkpoint_config,
        )
        if not started:
            return False
        job.transition_complete.set()
        thread = threading.Thread(
            target=self._run_reserved,
            kwargs={
                "thread_id": thread_id,
                "initial_payload": initial_payload,
                "max_steps": max_steps,
                "timeout_seconds": timeout_seconds,
                "job": job,
                "monotonic_started_at": time.monotonic(),
                "checkpoint_config": checkpoint_config,
            },
            daemon=True,
        )
        thread.start()
        return True

    def start_background_from_factory(
        self,
        *,
        thread_id: str,
        payload_factory: Any,
        max_steps: int,
        timeout_seconds: float,
        checkpoint_config: dict[str, Any] | None = None,
        restore: Callable[[RunnableConfig], None],
        on_initial_payload_error: Any | None = None,
        on_initial_payload_success: Any | None = None,
    ) -> bool:
        started, job = self._reserve_run(
            thread_id,
            restore=restore,
            checkpoint_config=checkpoint_config,
        )
        if not started:
            return False
        try:
            initial_payload = payload_factory()
        except Exception:
            with self._lock:
                cancelled = job.token.cancelled
                if self._jobs.get(thread_id) is job and not cancelled:
                    self._jobs.pop(thread_id, None)
                job.transition_complete.set()
            if cancelled:
                return True
            raise
        if job.token.cancelled:
            job.transition_complete.set()
            return True
        if not self._supports_invoke_options("interrupt_before"):
            thread = threading.Thread(
                target=self._run_reserved,
                kwargs={
                    "thread_id": thread_id,
                    "initial_payload": initial_payload,
                    "max_steps": max_steps,
                    "timeout_seconds": timeout_seconds,
                    "job": job,
                    "monotonic_started_at": time.monotonic(),
                    "on_initial_payload_error": on_initial_payload_error,
                    "on_initial_payload_success": on_initial_payload_success,
                    "checkpoint_config": checkpoint_config,
                },
                daemon=True,
            )
            with self._lock:
                if self._jobs.get(thread_id) is job and not job.token.cancelled:
                    thread.start()
                job.transition_complete.set()
            return True
        try:
            with bind_cancellation(job.token):
                self._invoke(
                    initial_payload,
                    checkpoint_config or graph_config(thread_id),
                    interrupt_before=["model", "tools", "model_output_gate"],
                )
            if on_initial_payload_success is not None:
                on_initial_payload_success()
        except RunCancelled:
            job.transition_complete.set()
            return True
        except Exception as exc:
            error_code, user_message = _run_failure(exc)
            cancelled = False
            try:
                with self._lock:
                    cancelled = job.token.cancelled
                    if self._jobs.get(thread_id) is job and not cancelled:
                        if on_initial_payload_error is not None:
                            try:
                                on_initial_payload_error()
                            except Exception:
                                pass
                        job.status.update(
                            {
                                "state": "error",
                                "error": f"{type(exc).__name__}: {exc}",
                                "error_code": error_code,
                                "user_message": user_message,
                                "updated_at": time.time(),
                            }
                        )
            finally:
                job.transition_complete.set()
            if cancelled:
                return True
            return True
        thread = threading.Thread(
            target=self._run_reserved,
            kwargs={
                "thread_id": thread_id,
                "initial_payload": None,
                "max_steps": max_steps,
                "timeout_seconds": timeout_seconds,
                "job": job,
                "monotonic_started_at": time.monotonic(),
                "checkpoint_config": checkpoint_config,
            },
            daemon=True,
        )
        with self._lock:
            if self._jobs.get(thread_id) is job and not job.token.cancelled:
                thread.start()
            job.transition_complete.set()
        return True

    def start_background_after_durable_resume(
        self,
        *,
        thread_id: str,
        initial_payload: Any,
        restore: Callable[[RunnableConfig], None],
        max_steps: int,
        timeout_seconds: float,
        checkpoint_config: dict[str, Any] | None = None,
    ) -> bool:
        config = checkpoint_config or graph_config(thread_id)
        started, job = self._reserve_run(
            thread_id,
            restore=restore,
            checkpoint_config=config,
        )
        if not started:
            return False
        try:
            with bind_cancellation(job.token):
                self._invoke(
                    initial_payload,
                    config,
                    interrupt_after=["tools", "model_output_gate"],
                )
            snapshot = self.app.get_state(
                config,
                subgraphs=True,
            )
        except RunCancelled:
            job.transition_complete.set()
            return True
        except Exception:
            with self._lock:
                cancelled = job.token.cancelled
                if self._jobs.get(thread_id) is job and not cancelled:
                    self._jobs.pop(thread_id, None)
                job.transition_complete.set()
            if cancelled:
                return True
            raise
        with self._lock:
            if isinstance(getattr(snapshot, "config", None), dict):
                job.durable_config = deepcopy(snapshot.config)
            job.status["steps"] += 1
            job.status["updated_at"] = time.time()
        thread = threading.Thread(
            target=self._run_reserved,
            kwargs={
                "thread_id": thread_id,
                "initial_payload": None,
                "max_steps": max_steps,
                "timeout_seconds": timeout_seconds,
                "job": job,
                "monotonic_started_at": time.monotonic(),
                "checkpoint_config": config,
            },
            daemon=True,
        )
        with self._lock:
            if self._jobs.get(thread_id) is job and not job.token.cancelled:
                thread.start()
            job.transition_complete.set()
        return True

    def run_until_blocked(
        self,
        *,
        thread_id: str,
        initial_payload: Any | None,
        max_steps: int,
        timeout_seconds: float,
        checkpoint_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        monotonic_started_at = time.monotonic()
        started, job = self._reserve_run(
            thread_id,
            checkpoint_config=checkpoint_config,
        )
        if not started:
            return dict(job.status)
        job.transition_complete.set()
        return self._run_reserved(
            thread_id=thread_id,
            initial_payload=initial_payload,
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
            job=job,
            monotonic_started_at=monotonic_started_at,
            checkpoint_config=checkpoint_config,
        )

    def _run_reserved(
        self,
        *,
        thread_id: str,
        initial_payload: Any | None,
        max_steps: int,
        timeout_seconds: float,
        job: GraphJob,
        monotonic_started_at: float,
        checkpoint_config: dict[str, Any] | None = None,
        on_initial_payload_error: Any | None = None,
        on_initial_payload_success: Any | None = None,
    ) -> dict[str, Any]:
        config = checkpoint_config or graph_config(thread_id)

        def timed_out() -> bool:
            return time.monotonic() - monotonic_started_at >= timeout_seconds

        def store() -> None:
            with self._lock:
                if self._jobs.get(thread_id) is job:
                    job.status["updated_at"] = time.time()

        def finish(
            state: str,
            error: str | None = None,
            *,
            error_code: str | None = None,
            user_message: str | None = None,
        ) -> dict[str, Any]:
            job.status["state"] = state
            job.status["error"] = error
            job.status["error_code"] = error_code
            job.status["user_message"] = user_message
            job.status["updated_at"] = time.time()
            store()
            return dict(job.status)

        def update_step() -> None:
            job.status["steps"] += 1
            job.status["updated_at"] = time.time()
            store()

        def finish_for_snapshot(snapshot: Any) -> dict[str, Any] | None:
            if snapshot is None:
                return finish("done")

            if _has_blocking_interrupt(snapshot):
                return finish("interrupted")

            if not list(getattr(snapshot, "next", None) or []):
                return finish("done")

            if job.status["steps"] >= max_steps:
                return finish(
                    "timeout",
                    f"Graph run reached max_steps={max_steps}",
                    error_code="WORKFLOW_MAX_STEPS_EXCEEDED",
                    user_message=(
                        "The workflow reached its step limit before producing a "
                        "result. Start a new conversation or increase the configured "
                        "step limit. Error: WORKFLOW_MAX_STEPS_EXCEEDED"
                    ),
                )

            return None

        try:
            if timed_out():
                return finish(
                    "timeout",
                    f"Graph run exceeded timeout_seconds={timeout_seconds:g}",
                    error_code="WORKFLOW_TIMEOUT",
                    user_message=(
                        "The workflow exceeded the selected model's deadline. "
                        "Error: WORKFLOW_TIMEOUT"
                    ),
                )

            if initial_payload is not None:
                try:
                    with bind_cancellation(job.token):
                        self._invoke(initial_payload, config)
                except RunCancelled:
                    raise
                except Exception:
                    if on_initial_payload_error is not None:
                        on_initial_payload_error()
                    raise
                if on_initial_payload_success is not None:
                    on_initial_payload_success()
                update_step()

            while True:
                if timed_out():
                    return finish(
                        "timeout",
                        f"Graph run exceeded timeout_seconds={timeout_seconds:g}",
                        error_code="WORKFLOW_TIMEOUT",
                        user_message=(
                            "The workflow exceeded the selected model's "
                            "deadline. Error: WORKFLOW_TIMEOUT"
                        ),
                    )

                snapshot = self.app.get_state(config, subgraphs=True)
                result = finish_for_snapshot(snapshot)
                if result is not None:
                    return result

                with bind_cancellation(job.token):
                    self._invoke({}, config)
                update_step()
        except RunCancelled:
            return self.status(thread_id)
        except Exception as exc:
            error_code, user_message = _run_failure(exc)
            return finish(
                "error",
                f"{type(exc).__name__}: {exc}",
                error_code=error_code,
                user_message=user_message,
            )

    def cancel(self, thread_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(thread_id)
            if job is None or job.status.get("state") != "running":
                return dict(job.status) if job is not None else _idle_status()
            if job.restore is None or job.durable_config is None:
                raise CancellationRestoreError(
                    "The active run has no durable cancellation boundary."
                )
            job.token.cancel()
            transition_complete = job.transition_complete

        transition_complete.wait()

        with self._lock:
            current = self._jobs.get(thread_id)
            if current is not job or job.status.get("state") != "running":
                return (
                    dict(current.status)
                    if current is not None
                    else _idle_status()
                )
            assert job.restore is not None
            assert job.durable_config is not None
            restore = job.restore
            durable_config = deepcopy(job.durable_config)

        try:
            restore(durable_config)
        except Exception as exc:
            raise CancellationRestoreError(
                "Unable to restore the last durable checkpoint."
            ) from exc

        with self._lock:
            current = self._jobs.get(thread_id)
            if current is not job:
                return (
                    dict(current.status)
                    if current is not None
                    else _idle_status()
                )
            job.status.update(
                {
                    "state": "cancelled",
                    "error": None,
                    "error_code": None,
                    "user_message": None,
                    "updated_at": time.time(),
                }
            )
            return dict(job.status)


def _initial_graph_state(
    thread_id: str,
    message: HumanMessage | None,
) -> dict[str, Any]:
    state = {
        "messages": [message] if message is not None else [],
        "output": {},
        "artifacts": {
            "datasets": {},
        },
        "artifact_ids": [],
        "authorized_attachment_ids": [],
        "final_response": None,
        "iteration_count": 0,
        "failure_signatures": [],
        "current_turn_artifact_refs": [],
        "current_turn_output_artifact_refs": [],
        "analysis_review_feedback_history": [],
        "completion_blocked": False,
        "model_output_state": {},
        "meta": {
            MetaKeys.THREAD_ID: thread_id,
        },
    }
    return ensure_conversation_state(state)


@dataclass
class ThreadRuntime:
    settings: RuntimeSettings
    model_available: bool = True
    thread_id: str = ""
    app: Any | None = None
    runner: ApiGraphRunner | None = None
    locked: bool = False
    credential_session_id: str | None = None
    release_when_idle: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class GraphBuildContext:
    owner_user_id: str
    session_id: str
    thread_id: str
    provider_api_key: str = field(repr=False)
    storage: ThreadStorageScope


GraphFactory = Callable[[RuntimeSettings, GraphBuildContext], CompiledStateGraph]
TitleGeneratorFactory = Callable[
    [RuntimeSettings, str],
    OpenAIConversationTitleGenerator,
]


@dataclass
class ReportAgentApiRuntime:
    graph_factory: GraphFactory
    default_runtime_settings: dict[str, Any]
    models: list[str]
    registered_models: Mapping[str, ModelRuntimeProfile] = field(
        default_factory=lambda: dict(MODEL_RUNTIME_PROFILES)
    )
    runtime_root: str | Path | None = None
    checkpoint_path: str | Path | None = None
    capabilities: RuntimeCapabilities = field(
        default_factory=lambda: RuntimeCapabilities(
            publication_knowledge=RuntimeCapability(
                status="available",
                message="Publication knowledge is available.",
            ),
            study_design=RuntimeCapability(
                status="available",
                message="Study design knowledge is available.",
            ),
            db_rag_dataset=RuntimeCapability(
                status="not_configured",
                message="DB-RAG dataset is not configured.",
            ),
        )
    )
    embedding_startup_status: EmbeddingStartupStatus = field(
        default_factory=silent_embedding_startup_status
    )
    history_store: ConversationHistoryStore | None = None
    title_generator: OpenAIConversationTitleGenerator | None = None
    title_generator_factory: TitleGeneratorFactory | None = None
    activity_store: SqliteActivityStore | None = None
    _threads: dict[tuple[str, str], ThreadRuntime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _attachment_store: LocalAttachmentStore | None = field(
        default=None,
        init=False,
    )
    _title_executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="conversation-title",
        ),
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.runtime_root is not None:
            self._attachment_store = LocalAttachmentStore(self.runtime_root)

    def _activity_call(self, operation: str, *args: Any) -> Any:
        if self.activity_store is None:
            return None
        try:
            return getattr(self.activity_store, operation)(*args)
        except Exception:
            _LOGGER.exception(
                "Agent activity lifecycle update failed",
                extra={"operation": operation},
            )
            return None

    def _activity_runs(self, thread_id: str) -> list[ActivityRun]:
        result = self._activity_call("list_runs", thread_id)
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, ActivityRun)]

    @property
    def attachment_store(self) -> LocalAttachmentStore:
        if self._attachment_store is None:
            raise RuntimeError(
                "runtime_root is required for attachment persistence"
            )
        return self._attachment_store

    @property
    def attachment_limits(self):
        return self.attachment_store.limits

    def create_thread(
        self,
        identity: RequestIdentity | dict[str, Any] | None = None,
        runtime_settings: dict[str, Any] | None = None,
    ) -> str:
        explicit_identity = isinstance(identity, RequestIdentity)
        if explicit_identity:
            assert isinstance(identity, RequestIdentity)
            owner_user_id = identity.owner_user_id
        else:
            owner_user_id = "local-user"
            runtime_settings = identity if isinstance(identity, dict) else runtime_settings
        thread_id = new_thread_id()
        thread = ThreadRuntime(
            settings=self._normalize_executable_settings(runtime_settings),
            thread_id=thread_id,
        )
        if self.history_store is not None and explicit_identity:
            self.history_store.create_pending(
                owner_user_id,
                thread_id,
                model_name=thread.settings.model_name,
            )
        with self._lock:
            self._threads[(owner_user_id, thread_id)] = thread
        return thread_id

    def release_session(self, owner_user_id: str, session_id: str) -> None:
        pending: list[tuple[tuple[str, str], ThreadRuntime]] = []
        with self._lock:
            matches = [
                (key, thread)
                for key, thread in self._threads.items()
                if key[0] == owner_user_id
                and thread.credential_session_id == session_id
            ]
            for key, thread in matches:
                with thread._lock:
                    if self._thread_is_running(key[1], thread):
                        thread.release_when_idle = True
                        pending.append((key, thread))
                    else:
                        self._clear_graph(thread)
                        self._threads.pop(key, None)
        for key, thread in pending:
            threading.Thread(
                target=self._evict_released_graph_when_idle,
                args=(key, thread, session_id),
                daemon=True,
                name="released-provider-session",
            ).start()

    @staticmethod
    def _thread_is_running(thread_id: str, thread: ThreadRuntime) -> bool:
        if thread.runner is None:
            return False
        try:
            return thread.runner.status(thread_id).get("state") == "running"
        except Exception:
            return False

    def active_run_count(self) -> int:
        with self._lock:
            threads = tuple(self._threads.items())
        return sum(
            1
            for (_owner, thread_id), thread in threads
            if self._thread_is_running(thread_id, thread)
        )

    @staticmethod
    def _clear_graph(thread: ThreadRuntime) -> None:
        thread.app = None
        thread.runner = None
        thread.credential_session_id = None
        thread.release_when_idle = False

    def _evict_released_graph_when_idle(
        self,
        key: tuple[str, str],
        thread: ThreadRuntime,
        session_id: str,
    ) -> None:
        while self._thread_is_running(key[1], thread):
            time.sleep(0.01)
        with self._lock:
            with thread._lock:
                if (
                    self._threads.get(key) is thread
                    and thread.credential_session_id == session_id
                    and thread.release_when_idle
                ):
                    self._clear_graph(thread)
                    self._threads.pop(key, None)

    def _normalize_persisted_settings(
        self,
        settings: dict[str, Any] | None = None,
    ) -> RuntimeSettings:
        unsupported = sorted(
            (set(self.default_runtime_settings) | set(settings or {}))
            - set(RuntimeSettings.model_fields)
        )
        if unsupported:
            raise ValueError(
                f"Unsupported runtime setting(s): {', '.join(unsupported)}"
            )
        merged = dict(self.default_runtime_settings)
        if settings:
            merged.update(
                {key: value for key, value in settings.items() if value is not None}
            )
        normalized = RuntimeSettings(**merged)
        profile = self.registered_models.get(normalized.model_name)
        if profile is not None:
            if not profile.supports_sampling_controls:
                normalized.temperature = None
                normalized.top_p = None
            if (
                settings
                and "model_name" in settings
                and "timeout_seconds" not in settings
            ):
                normalized.timeout_seconds = float(
                    profile.workflow_timeout_seconds
                )
        if normalized.temperature is not None and not 0 <= normalized.temperature <= 1:
            raise ValueError("temperature must be between 0 and 1")
        if normalized.top_p is not None and not 0 <= normalized.top_p <= 1:
            raise ValueError("top_p must be between 0 and 1")
        if normalized.max_steps is not None and normalized.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if normalized.timeout_seconds is not None and normalized.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        return normalized

    def _normalize_executable_settings(
        self,
        settings: dict[str, Any] | None = None,
    ) -> RuntimeSettings:
        normalized = self._normalize_persisted_settings(settings)
        if normalized.model_name not in self.models:
            raise ValueError(f"Unsupported model: {normalized.model_name}")
        return normalized

    def _require_owned_thread(
        self,
        identity: RequestIdentity,
        thread_id: str,
    ) -> ThreadRuntime:
        key = (identity.owner_user_id, thread_id)
        with self._lock:
            thread = self._threads.get(key)
            if thread is None and identity.owner_user_id == "local-user":
                thread = self._threads.pop(thread_id, None)  # type: ignore[arg-type]
                if thread is not None:
                    if not thread.thread_id:
                        thread.thread_id = thread_id
                    if thread.credential_session_id is None and thread.app is not None:
                        thread.credential_session_id = LOCAL_SESSION_ID
                    self._threads[key] = thread
            if thread is not None:
                return thread
        record = (
            self.history_store.get(identity.owner_user_id, thread_id)
            if self.history_store is not None
            else None
        )
        if self.history_store is not None and record is None:
            raise KeyError(thread_id)
        with self._lock:
            thread = self._threads.get(key)
            if thread is None:
                settings = {"model_name": record.model_name} if record else None
                normalized = self._normalize_persisted_settings(settings)
                thread = ThreadRuntime(
                    settings=normalized,
                    model_available=normalized.model_name in self.models,
                    thread_id=thread_id,
                    locked=record is not None,
                )
                self._threads[key] = thread
            return thread

    def authorize_thread(
        self,
        identity: RequestIdentity,
        thread_id: str,
    ) -> None:
        """Prove ownership without constructing a provider-bound graph."""
        self._require_owned_thread(identity, thread_id)

    def _thread(
        self,
        identity: RequestIdentity | str,
        thread_id: str | None = None,
    ) -> ThreadRuntime:
        if isinstance(identity, RequestIdentity):
            if thread_id is None:
                raise TypeError("thread_id is required")
            return self._require_owned_thread(identity, thread_id)
        local_identity = RequestIdentity(
            user=AuthenticatedUser(owner_user_id="local-user"),
            session_id=LOCAL_SESSION_ID,
        )
        return self._require_owned_thread(local_identity, identity)

    @staticmethod
    def _checkpoint_config(identity: RequestIdentity, thread_id: str) -> dict[str, Any]:
        return graph_config(thread_id, owner_user_id=identity.owner_user_id)

    @staticmethod
    def _config_for(
        identity: RequestIdentity | str,
        thread_id: str,
    ) -> dict[str, Any]:
        if isinstance(identity, RequestIdentity):
            return ReportAgentApiRuntime._checkpoint_config(identity, thread_id)
        return graph_config(thread_id)

    def _attachment_scope(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
    ) -> ThreadStorageScope | str:
        if self.runtime_root is None:
            return thread_id
        owner_user_id = (
            identity.owner_user_id
            if isinstance(identity, RequestIdentity)
            else "local-user"
        )
        return UserStorageLayout(self.runtime_root).thread(owner_user_id, thread_id)

    def _dataset_storage_scope(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
    ) -> ThreadStorageScope | str | Path | None:
        if self.runtime_root is None:
            return None
        return self._attachment_scope(identity, thread_id)

    def _graph_storage(
        self,
        identity: RequestIdentity,
        thread_id: str,
    ) -> ThreadStorageScope:
        return UserStorageLayout(self.runtime_root or Path.cwd()).thread(
            identity.owner_user_id,
            thread_id,
        )

    def _ensure_graph(
        self,
        identity: RequestIdentity | ThreadRuntime,
        thread: ThreadRuntime | None = None,
        provider_api_key: str | None = None,
    ) -> tuple[Any, ApiGraphRunner]:
        if isinstance(identity, ThreadRuntime):
            thread = identity
            identity = RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            )
        if thread is None:
            raise TypeError("thread runtime is required")
        resolved_key = str(provider_api_key or "").strip()
        with self._lock:
            with thread._lock:
                if thread.app is not None and (
                    thread.credential_session_id != identity.session_id
                    or thread.release_when_idle
                ):
                    if self._thread_is_running(
                        thread.thread_id,
                        thread,
                    ):
                        raise ThreadAlreadyRunningError(thread.thread_id)
                    self._clear_graph(thread)
                if thread.app is None:
                    thread_id = thread.thread_id
                    context = GraphBuildContext(
                        owner_user_id=identity.owner_user_id,
                        session_id=identity.session_id,
                        thread_id=thread_id,
                        provider_api_key=resolved_key,
                        storage=self._graph_storage(identity, thread_id),
                    )
                    try:
                        inspect.signature(self.graph_factory).bind(
                            thread.settings,
                            context,
                        )
                    except (TypeError, ValueError):
                        thread.app = self.graph_factory(thread.settings)
                    else:
                        if not resolved_key and identity.session_id != LOCAL_SESSION_ID:
                            raise ValueError("provider_api_key is required")
                        thread.app = self.graph_factory(thread.settings, context)
                    thread.runner = ApiGraphRunner(thread.app)
                    thread.credential_session_id = identity.session_id
                    thread.release_when_idle = False
                assert thread.runner is not None
                return thread.app, thread.runner

    @staticmethod
    def _bound_graph(thread: ThreadRuntime) -> tuple[Any, ApiGraphRunner]:
        if thread.app is None or thread.runner is None:
            raise KeyError(thread.thread_id)
        return thread.app, thread.runner

    def _read_only_checkpoint(
        self,
        identity: RequestIdentity,
        thread_id: str,
    ) -> ReadOnlyCheckpointSnapshot | None:
        if self.checkpoint_path is None:
            return None
        with SqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
            saved = saver.get_tuple(self._checkpoint_config(identity, thread_id))
        if saved is None:
            return None
        channel_values = dict(saved.checkpoint.get("channel_values") or {})
        root = channel_values.get("__root__")
        values = dict(root) if isinstance(root, dict) else channel_values
        next_nodes = tuple(
            key.removeprefix("branch:to:")
            for key in channel_values
            if key.startswith("branch:to:")
        )
        interrupts: list[Any] = []
        for _task_id, channel, value in list(saved.pending_writes or []):
            if channel != "__interrupt__":
                continue
            if isinstance(value, (list, tuple)):
                interrupts.extend(value)
            else:
                interrupts.append(value)
        return ReadOnlyCheckpointSnapshot(
            values=values,
            next=next_nodes,
            interrupts=tuple(interrupts),
        )

    def _snapshot(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        thread: ThreadRuntime,
    ) -> Any | None:
        if thread.app is not None:
            app, _runner = self._bound_graph(thread)
            return app.get_state(
                self._config_for(identity, thread_id),
                subgraphs=True,
            )
        if isinstance(identity, RequestIdentity):
            return self._read_only_checkpoint(identity, thread_id)
        return None

    def runtime_info(self) -> RuntimeInfo:
        return RuntimeInfo(**self._normalize_executable_settings().model_dump())

    def runtime_options(self) -> RuntimeOptions:
        defaults = self._normalize_executable_settings()
        return RuntimeOptions(
            defaults=defaults,
            capabilities=self.capabilities,
            embedding_startup_status=self.embedding_startup_status,
            models=[
                ModelOption(**self.registered_models[model].descriptor())
                for model in self.models
            ],
        )

    @staticmethod
    def _message_turn_hash(message: HumanMessage) -> str:
        content = str(message.content or "").strip()
        message_id = str(message.id or "").strip()
        return hashlib.sha256(
            f"{message_id}:{content}".encode("utf-8")
        ).hexdigest()[:16]

    def _bind_message_payload(
        self,
        *,
        thread_id: str,
        attachment_thread_id: ThreadStorageScope,
        snapshot: Any,
        message: HumanMessage,
        manifests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        values = dict(getattr(snapshot, "values", None) or {})
        available_manifests: list[dict[str, Any]] = []
        try:
            for manifest in manifests:
                binding_manifest = self.attachment_store.begin_binding(
                    attachment_thread_id,
                    str(manifest["id"]),
                )
                available_manifests.append(
                    {
                        **binding_manifest,
                        "status": "available",
                    }
                )
            if available_manifests:
                reader = AttachmentReaderService(
                    self.attachment_store,
                    self.attachment_store.runtime_root,
                )
                profiles = reader.inspect(
                    attachment_thread_id,
                    [str(manifest["id"]) for manifest in available_manifests],
                )
                inspected_by_id = {
                    str(profile["id"]): profile
                    for profile in profiles
                    if isinstance(profile, dict)
                    and isinstance(profile.get("id"), str)
                }
                available_manifests = [
                    {
                        **self.attachment_store.record_inspection(
                            attachment_thread_id,
                            str(manifest["id"]),
                            inspected_by_id[str(manifest["id"])],
                        ),
                        "status": "available",
                    }
                    for manifest in available_manifests
                ]
            if values:
                event_state = ensure_conversation_state(
                    {
                        "artifacts": values.get("artifacts"),
                        "meta": values.get("meta"),
                    }
                )
            else:
                event_state = _initial_graph_state(
                    thread_id,
                    message,
                )

            artifacts = dict(event_state.get("artifacts") or {})
            attachments = dict(artifacts.get("attachments") or {})
            for manifest in available_manifests:
                attachments[str(manifest["id"])] = manifest
            artifacts["attachments"] = attachments
            event_state = {**event_state, "artifacts": artifacts}

            user_turn_hash = self._message_turn_hash(message)
            event_state["meta"][MetaKeys.LAST_USER_MESSAGE_HASH] = (
                user_turn_hash
            )
            event_state = append_conversation_event(
                event_state,
                build_user_event(
                    actor="human",
                    user_turn_hash=user_turn_hash,
                    text=str(message.content or ""),
                ),
            )
            user_event_id = str(
                event_state["artifacts"]["conversation_events"][-1]["event_id"]
            )
            for manifest in available_manifests:
                event_state = append_conversation_event(
                    event_state,
                    build_attachment_event(
                        actor="api",
                        user_turn_hash=user_turn_hash,
                        artifact_id=str(manifest["id"]),
                        relationship="input",
                        parent_event_id=user_event_id,
                    ),
                )

            input_attachment_ids = {
                str(event.get("artifact_id") or "")
                for event in list(
                    event_state["artifacts"].get("conversation_events") or []
                )
                if isinstance(event, dict)
                and event.get("type") == "attachment"
                and event.get("relationship") == "input"
            }
            artifacts = dict(event_state.get("artifacts") or {})
            attachments = dict(artifacts.get("attachments") or {})
            for attachment_id in input_attachment_ids:
                current_manifest = attachments.get(attachment_id)
                if (
                    isinstance(current_manifest, dict)
                    and current_manifest.get("status") == "available"
                ):
                    continue
                try:
                    stored_manifest = self.attachment_store.require(
                        attachment_thread_id,
                        attachment_id,
                    )
                except AttachmentError:
                    continue
                if stored_manifest.get("status") == "available":
                    attachments[attachment_id] = stored_manifest
            artifacts["attachments"] = attachments
            event_state = {**event_state, "artifacts": artifacts}
            authorized_attachment_ids = sorted(
                attachment_id
                for attachment_id, manifest in attachments.items()
                if attachment_id in input_attachment_ids
                and isinstance(manifest, dict)
                and manifest.get("status") == "available"
            )
            if values:
                payload = {
                    "messages": follow_up_message_patch(
                        list(values.get("messages") or []),
                        message,
                    ),
                    "artifacts": event_state["artifacts"],
                    "meta": event_state["meta"],
                    "authorized_attachment_ids": authorized_attachment_ids,
                    "final_response": None,
                    "iteration_count": 0,
                    "failure_signatures": [],
                    "current_turn_artifact_refs": [],
                    "current_turn_output_artifact_refs": [],
                    "completion_blocked": False,
                    "model_output_state": {},
                    "terminal_error": None,
                    "terminal_control": None,
                }
                return payload
            return {
                **event_state,
                "authorized_attachment_ids": authorized_attachment_ids,
            }
        except Exception:
            self._rollback_available_manifests(
                attachment_thread_id,
                available_manifests,
            )
            raise

    def _rollback_available_manifests(
        self,
        thread_id: ThreadStorageScope,
        manifests: list[dict[str, Any]],
    ) -> None:
        for manifest in manifests:
            try:
                self.attachment_store.rollback_binding(
                    thread_id,
                    str(manifest["id"]),
                )
            except AttachmentError:
                continue

    def _commit_binding_manifests(
        self,
        thread_id: ThreadStorageScope,
        manifests: list[dict[str, Any]],
    ) -> None:
        for manifest in manifests:
            try:
                self.attachment_store.commit_binding(
                    thread_id,
                    str(manifest["id"]),
                )
            except AttachmentError:
                continue

    def _rollback_unbound_available_manifests(
        self,
        identity: RequestIdentity,
        thread_id: str,
        attachment_thread_id: ThreadStorageScope,
        thread: ThreadRuntime,
        manifests: list[dict[str, Any]],
    ) -> None:
        linked_input_ids: set[str] = set()
        try:
            app, _runner = self._bound_graph(thread)
            snapshot = app.get_state(
                self._checkpoint_config(identity, thread_id),
                subgraphs=True,
            )
            values = _projection_values(snapshot)
            linked_input_ids = {
                str(event.get("artifact_id"))
                for event in list(
                    dict(values.get("artifacts") or {}).get(
                        "conversation_events"
                    )
                    or []
                )
                if isinstance(event, dict)
                and event.get("type") == "attachment"
                and event.get("relationship") == "input"
            }
        except Exception:
            linked_input_ids = set()
        self._rollback_available_manifests(
            attachment_thread_id,
            [
                manifest
                for manifest in manifests
                if str(manifest.get("id") or "") not in linked_input_ids
            ],
        )
        self._commit_binding_manifests(
            attachment_thread_id,
            [
                manifest
                for manifest in manifests
                if str(manifest.get("id") or "") in linked_input_ids
            ],
        )

    def _reject_initial_turn(
        self,
        identity: RequestIdentity,
        thread_id: str,
        thread: ThreadRuntime,
        manifests: list[dict[str, Any]],
    ) -> None:
        self._rollback_unbound_available_manifests(
            identity,
            thread_id,
            self._attachment_scope(identity, thread_id),
            thread,
            manifests,
        )
        if self.history_store is not None:
            self.history_store.delete_pending(identity.owner_user_id, thread_id)

    def _accept_initial_turn(
        self,
        identity: RequestIdentity | str,
        thread_id: str | ThreadRuntime,
        thread: ThreadRuntime | None = None,
        *,
        message_id: str,
        turn_hash: str,
        manifests: list[dict[str, Any]],
    ) -> None:
        if not isinstance(identity, RequestIdentity):
            if not isinstance(thread_id, ThreadRuntime):
                raise TypeError("thread runtime is required")
            thread = thread_id
            thread_id = identity
            identity = RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            )
        if thread is None or not isinstance(thread_id, str):
            raise TypeError("thread runtime is required")
        app, _runner = self._bound_graph(thread)
        snapshot = app.get_state(
            self._checkpoint_config(identity, thread_id),
            subgraphs=True,
        )
        if not _checkpoint_contains_user_turn(
            snapshot,
            message_id=message_id,
            turn_hash=turn_hash,
        ):
            if callable(getattr(app, "invoke", None)):
                self._reject_initial_turn(identity, thread_id, thread, manifests)
                raise InitialTurnCheckpointError(
                    "Initial turn was not durably checkpointed"
                )
        values = _projection_values(snapshot)
        user_event_id = next(
            (
                str(event.get("event_id") or "").strip()
                for event in reversed(
                    list(
                        dict(values.get("artifacts") or {}).get(
                            "conversation_events"
                        )
                        or []
                    )
                )
                if isinstance(event, dict)
                and event.get("type") == "user"
                and event.get("user_turn_hash") == turn_hash
                and str(event.get("event_id") or "").strip()
            ),
            "",
        )
        if user_event_id:
            self._activity_call("start_run", thread_id, user_event_id)
        self._commit_binding_manifests(
            self._attachment_scope(identity, thread_id),
            manifests,
        )
        if self.history_store is not None:
            self.history_store.promote_pending(identity.owner_user_id, thread_id)

    def submit_message(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        text: str | None = None,
        attachment_ids: list[str] | None = None,
        model_name: str | None = None,
        *,
        provider_api_key: str | None = None,
    ) -> None:
        if not isinstance(identity, RequestIdentity):
            legacy_thread_id = identity
            legacy_text = thread_id
            if isinstance(text, list) and attachment_ids is None:
                attachment_ids = text
            identity = RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            )
            thread_id = legacy_thread_id
            text = legacy_text
        if text is None:
            raise TypeError("text is required")
        attachment_ids = list(attachment_ids or [])
        thread = self._require_owned_thread(identity, thread_id)
        if not thread.model_available:
            if not model_name:
                raise ModelReplacementRequiredError(thread.settings.model_name)
            replacement = self._normalize_executable_settings(
                {"model_name": model_name}
            )
            previous = thread.settings
            thread.settings = replacement
            try:
                self._ensure_graph(identity, thread, provider_api_key)
                if self.history_store is not None and not self.history_store.set_model(
                    identity.owner_user_id,
                    thread_id,
                    replacement.model_name,
                ):
                    raise KeyError(thread_id)
            except Exception:
                thread.settings = previous
                self._clear_graph(thread)
                raise
            thread.model_available = True
            thread.locked = True
        elif model_name:
            if thread.locked:
                raise ValueError("The model is locked for this conversation.")
            thread.settings = self._normalize_executable_settings(
                {"model_name": model_name}
            )
        self._ensure_graph(identity, thread, provider_api_key)
        app, runner = self._bound_graph(thread)
        snapshot = app.get_state(
            self._checkpoint_config(identity, thread_id),
            subgraphs=True,
        )
        if not _projection_values(snapshot) and callable(
            getattr(app, "update_state", None)
        ):
            app.update_state(
                self._checkpoint_config(identity, thread_id),
                _initial_graph_state(
                    thread_id,
                    None,
                ),
                as_node=START,
            )
            snapshot = app.get_state(
                self._checkpoint_config(identity, thread_id),
                subgraphs=True,
            )
        if _has_blocking_interrupt(snapshot):
            raise ThreadAwaitingReviewError(thread_id)
        manifests = [
            self.attachment_store.require(self._attachment_scope(identity, thread_id), attachment_id)
            for attachment_id in attachment_ids
        ]
        if any(manifest.get("status") != "staged" for manifest in manifests):
            raise AttachmentError(
                "INVALID_ATTACHMENT_STATE",
                "message attachments must be staged and not previously bound",
            )
        if manifests:
            validate_message_attachment_limits(
                manifests,
                self.attachment_store.limits,
            )
        message = HumanMessage(
            content=text,
            id=f"user-{uuid.uuid4().hex}",
            additional_kwargs={"attachment_ids": attachment_ids},
        )
        turn = CancelledTurn(
            message_id=str(message.id),
            text=str(message.content or ""),
            turn_hash=self._message_turn_hash(message),
            attachment_ids=tuple(attachment_ids),
        )

        def restore_cancelled(durable_config: RunnableConfig) -> None:
            base = app.get_state(durable_config, subgraphs=True)
            current_manifests = [
                self.attachment_store.require(
                    self._attachment_scope(identity, thread_id),
                    attachment_id,
                )
                for attachment_id in turn.attachment_ids
            ]
            self._commit_binding_manifests(
                self._attachment_scope(identity, thread_id),
                current_manifests,
            )
            committed_manifests = [
                self.attachment_store.require(
                    self._attachment_scope(identity, thread_id),
                    attachment_id,
                )
                for attachment_id in turn.attachment_ids
            ]
            patch = _cancelled_turn_patch(
                _projection_values(base),
                turn=turn,
                manifests=committed_manifests,
            )
            app.update_state(
                durable_config,
                patch,
                as_node="model_output_gate",
            )
            if self.history_store is not None:
                self.history_store.promote_pending(
                    identity.owner_user_id,
                    thread_id,
                )

        created_pending = False
        if (
            self.history_store is not None
            and self.history_store.get(identity.owner_user_id, thread_id) is None
        ):
            _record, created_pending = self.history_store.create_pending(
                identity.owner_user_id,
                thread_id,
                model_name=thread.settings.model_name,
            )
        runner_parameters = inspect.signature(
            runner.start_background_from_factory
        ).parameters
        payload_success = (
            lambda: self._accept_initial_turn(
                identity,
                thread_id,
                thread,
                message_id=str(message.id),
                turn_hash=self._message_turn_hash(message),
                manifests=manifests,
            )
            if "restore" in runner_parameters
            else lambda: self._commit_binding_manifests(
                self._attachment_scope(identity, thread_id),
                manifests,
            )
        )
        runner_kwargs: dict[str, Any] = {
            "thread_id": thread_id,
            "payload_factory": lambda: self._bind_message_payload(
                thread_id=thread_id,
                attachment_thread_id=self._attachment_scope(identity, thread_id),
                snapshot=snapshot,
                message=message,
                manifests=manifests,
            ),
            "max_steps": thread.settings.max_steps or 1,
            "timeout_seconds": thread.settings.timeout_seconds or 1,
            "checkpoint_config": self._checkpoint_config(identity, thread_id),
            "on_initial_payload_error": lambda: self._reject_initial_turn(
                identity,
                thread_id,
                thread,
                manifests,
            ),
            "on_initial_payload_success": payload_success,
        }
        if "restore" in runner_parameters:
            runner_kwargs["restore"] = restore_cancelled
        try:
            started = runner.start_background_from_factory(**runner_kwargs)
        except Exception:
            if created_pending and self.history_store is not None:
                self.history_store.delete_pending(identity.owner_user_id, thread_id)
            raise
        if not started:
            if created_pending and self.history_store is not None:
                self.history_store.delete_pending(identity.owner_user_id, thread_id)
            raise ThreadAlreadyRunningError(thread_id)
        if self.history_store is not None:
            record = self.history_store.get(identity.owner_user_id, thread_id)
            title_generator = (
                self.title_generator_factory(thread.settings, provider_api_key or "")
                if self.title_generator_factory is not None
                else self.title_generator
            )
            if (
                not thread.locked
                and record is not None
                and text.strip()
                and record.title == "Untitled conversation"
                and title_generator is not None
            ):
                self._title_executor.submit(
                    self._generate_title,
                    identity.owner_user_id,
                    thread_id,
                    text,
                    title_generator,
                )
            elif (
                not thread.locked
                and record is not None
                and not text.strip()
                and attachment_ids
            ):
                self.history_store.set_initial_automatic_title(
                    identity.owner_user_id,
                    thread_id,
                    ConversationHistoryStore.fallback_title(text),
                )
        thread.locked = True

    def _generate_title(
        self,
        owner_user_id: str,
        thread_id: str,
        text: str,
        title_generator: OpenAIConversationTitleGenerator,
    ) -> None:
        try:
            assert self.history_store is not None
            self.history_store.set_automatic_title(
                owner_user_id,
                thread_id,
                title_generator.generate(text),
            )
        except Exception:
            return

    def cancel_run(
        self,
        identity: RequestIdentity | str,
        thread_id: str | None = None,
    ) -> ApiThreadState:
        if not isinstance(identity, RequestIdentity):
            thread_id = identity
            identity = RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            )
        if thread_id is None:
            raise TypeError("thread_id is required")
        thread = self._require_owned_thread(identity, thread_id)
        if thread.runner is not None:
            thread.runner.cancel(thread_id)
        if self.history_store is not None:
            self.history_store.touch(identity.owner_user_id, thread_id)
        self._activity_call("finish", thread_id, "cancelled")
        return self.state(identity, thread_id)

    def _conversation_awaiting_review(
        self,
        identity: RequestIdentity,
        thread_id: str,
    ) -> bool:
        try:
            thread = self._require_owned_thread(identity, thread_id)
            snapshot = self._snapshot(identity, thread_id, thread)
            if snapshot is None:
                return False
            return _active_interrupt(snapshot, _projection_values(snapshot)) is not None
        except Exception:
            _LOGGER.exception(
                "Conversation review status projection failed",
                extra={"thread_id": thread_id},
            )
            return False

    def list_conversations(self, identity: RequestIdentity | None = None):
        owner_user_id = identity.owner_user_id if identity is not None else "local-user"
        items = self.history_store.list(owner_user_id) if self.history_store else []
        if identity is None:
            return items
        return [
            replace(
                item,
                awaiting_review=self._conversation_awaiting_review(
                    identity,
                    item.thread_id,
                ),
            )
            for item in items
        ]

    def rename_conversation(
        self,
        identity: RequestIdentity,
        thread_id: str,
        title: str,
    ):
        if self.history_store is None:
            return None
        return self.history_store.rename(identity.owner_user_id, thread_id, title)

    def open_conversation(self, identity: RequestIdentity, thread_id: str):
        if self.history_store is None:
            return None
        return self.history_store.mark_opened(identity.owner_user_id, thread_id)

    def _assert_conversation_mutable(
        self,
        identity: RequestIdentity,
        thread_id: str,
    ) -> bool:
        if (
            self.history_store is None
            or self.history_store.get(identity.owner_user_id, thread_id) is None
        ):
            return False
        self._thread(identity, thread_id)
        state = self.state(identity, thread_id)
        if state.run.state == "running":
            raise ThreadAlreadyRunningError(thread_id)
        if state.active_interrupt is not None:
            raise ThreadAwaitingReviewError(thread_id)
        return True

    def archive_conversation(
        self,
        identity: RequestIdentity | str,
        thread_id: str | None = None,
    ):
        if not isinstance(identity, RequestIdentity):
            thread_id = identity
            identity = RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            )
        assert thread_id is not None
        if not self._assert_conversation_mutable(identity, thread_id):
            return None
        assert self.history_store is not None
        return self.history_store.archive(identity.owner_user_id, thread_id)

    def restore_conversation(
        self,
        identity: RequestIdentity | str,
        thread_id: str | None = None,
    ):
        if not isinstance(identity, RequestIdentity):
            thread_id = identity
            identity = RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            )
        assert thread_id is not None
        if not self._assert_conversation_mutable(identity, thread_id):
            return None
        assert self.history_store is not None
        return self.history_store.restore(identity.owner_user_id, thread_id)

    def delete_conversation(
        self,
        identity: RequestIdentity | str,
        thread_id: str | None = None,
    ) -> bool:
        if not isinstance(identity, RequestIdentity):
            thread_id = identity
            identity = RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            )
        assert thread_id is not None
        if not self._assert_conversation_mutable(identity, thread_id):
            return False
        thread = self._thread(identity, thread_id)
        if thread.app is not None and hasattr(thread.app, "checkpointer"):
            app, _runner = self._bound_graph(thread)
            app.checkpointer.delete_thread(
                self._checkpoint_config(identity, thread_id)["configurable"]["thread_id"]
            )
        elif self.checkpoint_path is not None:
            with SqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
                saver.delete_thread(
                    self._checkpoint_config(identity, thread_id)["configurable"]["thread_id"]
                )
        if self._attachment_store is not None:
            self._attachment_store.delete_thread(self._attachment_scope(identity, thread_id))
        self._activity_call("delete_thread", thread_id)
        with self._lock:
            self._threads.pop((identity.owner_user_id, thread_id), None)
        assert self.history_store is not None
        return self.history_store.delete(identity.owner_user_id, thread_id)

    def stage_attachments(
        self,
        identity: RequestIdentity | str,
        thread_id: str | list[tuple[str, str, bytes]],
        uploads: list[tuple[str, str, bytes]] | None = None,
    ) -> AttachmentUploadResult:
        if isinstance(identity, RequestIdentity):
            assert isinstance(thread_id, str)
            assert uploads is not None
        else:
            uploads = thread_id
            thread_id = identity
        limits = self.attachment_store.limits
        if len(uploads) > limits.max_files_per_message:
            raise AttachmentError(
                "TOO_MANY_FILES",
                "upload exceeds the configured attachment count limit",
            )
        if sum(len(content) for _filename, _mime, content in uploads) > (
            limits.max_message_bytes
        ):
            raise AttachmentError(
                "MESSAGE_ATTACHMENTS_TOO_LARGE",
                "upload exceeds the configured total attachment size limit",
            )
        self._thread(identity, thread_id)
        attachments: list[AttachmentManifestSummary] = []
        errors: list[AttachmentUploadError] = []
        for filename, mime, content in uploads:
            try:
                manifest = self.attachment_store.stage(
                    self._attachment_scope(identity, thread_id),
                    filename,
                    mime,
                    content,
                )
            except AttachmentError as exc:
                errors.append(
                    AttachmentUploadError(
                        filename=filename,
                        code=exc.code,
                        message=str(exc),
                    )
                )
                continue
            attachments.append(AttachmentManifestSummary(**manifest))
        return AttachmentUploadResult(
            attachments=attachments,
            errors=errors,
        )

    def discard_staged_attachment(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        attachment_id: str | None = None,
    ) -> None:
        if not isinstance(identity, RequestIdentity):
            attachment_id = thread_id
            thread_id = identity
        assert attachment_id is not None
        self._thread(identity, thread_id)
        self.attachment_store.discard_staged(
            self._attachment_scope(identity, thread_id),
            attachment_id,
        )

    def conversation_attachment_bytes(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        attachment_id: str | None = None,
    ) -> FileArtifactBytes:
        if not isinstance(identity, RequestIdentity):
            attachment_id = thread_id
            thread_id = identity
        assert attachment_id is not None
        thread = self._thread(identity, thread_id)
        snapshot = self._snapshot(identity, thread_id, thread)
        if snapshot is None:
            raise KeyError(thread_id)
        values = _projection_values(snapshot)
        artifacts = dict(values.get("artifacts") or {})
        attachment_events = [
            event
            for event in list(artifacts.get("conversation_events") or [])
            if isinstance(event, dict)
            and event.get("type") == "attachment"
        ]
        input_ids = {
            str(event.get("artifact_id"))
            for event in attachment_events
            if event.get("relationship") == "input"
        }
        output_ids = {
            str(event.get("artifact_id"))
            for event in attachment_events
            if event.get("relationship") == "output"
        }

        if self._attachment_store is not None and attachment_id in input_ids:
            try:
                manifest = self._attachment_store.require(
                    self._attachment_scope(identity, thread_id),
                    attachment_id,
                )
            except AttachmentError:
                manifest = None
            if manifest is not None:
                if manifest.get("status") == "binding":
                    manifest = self._attachment_store.commit_binding(
                        self._attachment_scope(identity, thread_id),
                        attachment_id,
                    )
                if manifest.get("status") != "available":
                    raise KeyError(attachment_id)
                try:
                    content = self._attachment_store.read_bytes(
                        self._attachment_scope(identity, thread_id),
                        attachment_id,
                    )
                except AttachmentError as exc:
                    raise KeyError(attachment_id) from exc
                return FileArtifactBytes(
                    content=content,
                    mime=str(
                        manifest.get("mime") or "application/octet-stream"
                    ),
                    filename=str(
                        manifest.get("filename") or attachment_id
                    ),
                )

        if attachment_id not in output_ids:
            raise KeyError(attachment_id)

        file_artifact = dict(
            dict(artifacts.get("files") or {}).get(attachment_id) or {}
        )
        if file_artifact and is_published_artifact(file_artifact):
            return _file_artifact_bytes_from_record(
                attachment_id,
                file_artifact,
            )

        dataset = dict(
            dict(artifacts.get("datasets") or {}).get(attachment_id) or {}
        )
        if dataset.get("status") == "active":
            dataframe, _schema = load_dataset_artifact(
                dataset,
                runtime_root=self._dataset_storage_scope(identity, thread_id),
            )
            return FileArtifactBytes(
                content=dataframe.to_csv(index=False).encode("utf-8"),
                mime="text/csv",
                filename=f"{attachment_id}.csv",
            )
        raise KeyError(attachment_id)

    def resume_interrupt(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        interrupt_id: str | dict[str, Any],
        payload: dict[str, Any] | None = None,
        *,
        provider_api_key: str | None = None,
    ) -> None:
        if not isinstance(identity, RequestIdentity):
            if not isinstance(interrupt_id, dict):
                raise TypeError("payload is required")
            payload = interrupt_id
            interrupt_id = thread_id
            thread_id = identity
            identity = RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            )
        if payload is None or not isinstance(interrupt_id, str):
            raise TypeError("payload is required")
        thread = self._require_owned_thread(identity, thread_id)
        thread.locked = True
        self._ensure_graph(identity, thread, provider_api_key)
        app, runner = self._bound_graph(thread)
        snapshot = app.get_state(self._checkpoint_config(identity, thread_id), subgraphs=True)
        values = _projection_values(snapshot)
        active_interrupt = _active_interrupt(snapshot, values)
        if active_interrupt is None or active_interrupt.id != interrupt_id:
            raise StaleInterruptError(interrupt_id)
        interrupt_type = active_interrupt.type
        resume_payload = validate_resume_decision(
            active_interrupt.model_dump(mode="json"),
            payload,
        )
        if resume_payload.get("action") == "answer":
            resume_payload["_clarification_interrupt_id"] = interrupt_id
        try:
            turn = _cancelled_turn_from_values(values)
        except CancellationRestoreError:
            turn = None

        if turn is not None and hasattr(
            runner,
            "start_background_after_durable_resume",
        ):
            def restore_cancelled(durable_config: RunnableConfig) -> None:
                base = app.get_state(durable_config, subgraphs=True)
                current_manifests = [
                    self.attachment_store.require(
                        self._attachment_scope(identity, thread_id),
                        attachment_id,
                    )
                    for attachment_id in turn.attachment_ids
                ]
                self._commit_binding_manifests(
                    self._attachment_scope(identity, thread_id),
                    current_manifests,
                )
                committed_manifests = [
                    self.attachment_store.require(
                        self._attachment_scope(identity, thread_id),
                        attachment_id,
                    )
                    for attachment_id in turn.attachment_ids
                ]
                patch = _cancelled_turn_patch(
                    _projection_values(base),
                    turn=turn,
                    manifests=committed_manifests,
                )
                app.update_state(
                    durable_config,
                    patch,
                    as_node="model_output_gate",
                )
                if self.history_store is not None:
                    self.history_store.promote_pending(
                        identity.owner_user_id,
                        thread_id,
                    )

            started = runner.start_background_after_durable_resume(
                thread_id=thread_id,
                initial_payload=Command(resume={interrupt_id: resume_payload}),
                restore=restore_cancelled,
                max_steps=thread.settings.max_steps or 1,
                timeout_seconds=thread.settings.timeout_seconds or 1,
                checkpoint_config=self._checkpoint_config(identity, thread_id),
            )
        else:
            started = runner.start_background(
                thread_id=thread_id,
                initial_payload=Command(resume={interrupt_id: resume_payload}),
                max_steps=thread.settings.max_steps or 1,
                timeout_seconds=thread.settings.timeout_seconds or 1,
                checkpoint_config=self._checkpoint_config(identity, thread_id),
            )
        if not started:
            raise ThreadAlreadyRunningError(thread_id)
        self._activity_call("resume", thread_id, interrupt_type)
        if self.history_store is not None:
            self.history_store.touch(identity.owner_user_id, thread_id)

    def state(
        self,
        identity: RequestIdentity | str,
        thread_id: str | None = None,
        *,
        provider_api_key: str | None = None,
    ) -> ApiThreadState:
        legacy_local_call = not isinstance(identity, RequestIdentity)
        if isinstance(identity, RequestIdentity):
            if thread_id is None:
                raise TypeError("thread_id is required")
        else:
            thread_id = identity
        thread = self._thread(identity, thread_id)
        stored_profile = self.registered_models.get(thread.settings.model_name)
        model_label = (
            stored_profile.label if stored_profile is not None
            else thread.settings.model_name
        )
        if legacy_local_call and thread.app is None:
            local_identity = RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            )
            self._ensure_graph(local_identity, thread, None)
        if provider_api_key is not None and thread.model_available:
            if not isinstance(identity, RequestIdentity):
                raise TypeError("identity must be a RequestIdentity")
            self._ensure_graph(identity, thread, provider_api_key)
        if thread.app is None or thread.runner is None:
            snapshot = self._snapshot(identity, thread_id, thread)
            if snapshot is not None:
                return project_thread_state(
                    thread_id=thread_id,
                    snapshot=snapshot,
                    run_status=_idle_status(),
                    runtime_settings=thread.settings,
                    runtime_settings_locked=thread.locked,
                    model_label=model_label,
                    model_available=thread.model_available,
                    embedding_startup_status=self.embedding_startup_status,
                )
            return project_thread_state(
                thread_id=thread_id,
                snapshot=None,
                run_status=_idle_status(),
                runtime_settings=thread.settings,
                runtime_settings_locked=thread.locked,
                model_label=model_label,
                model_available=thread.model_available,
                embedding_startup_status=self.embedding_startup_status,
            )
        app, runner = self._bound_graph(thread)
        snapshot = app.get_state(
            self._config_for(identity, thread_id),
            subgraphs=True,
        )
        run_status = runner.status(thread_id)
        if (
            (provider_api_key is not None or legacy_local_call)
            and _should_recover_snapshot(snapshot, run_status)
        ):
            self._activity_call("recover", thread_id)
            runner.start_background(
                thread_id=thread_id,
                initial_payload=None,
                max_steps=thread.settings.max_steps or 1,
                timeout_seconds=thread.settings.timeout_seconds or 1,
                checkpoint_config=self._config_for(identity, thread_id),
            )
            run_status = runner.status(thread_id)
        projected = project_thread_state(
            thread_id=thread_id,
            snapshot=snapshot,
            run_status=run_status,
            runtime_settings=thread.settings,
            runtime_settings_locked=thread.locked,
            model_label=model_label,
            model_available=thread.model_available,
            embedding_startup_status=self.embedding_startup_status,
        )
        if projected.active_interrupt is not None:
            self._activity_call(
                "mark_waiting",
                thread_id,
                projected.active_interrupt.type,
            )
        terminal_activity_state = {
            "done": "completed",
            "cancelled": "cancelled",
            "error": "error",
            "timeout": "error",
        }.get(projected.run.state)
        if terminal_activity_state is not None:
            self._activity_call(
                "finish",
                thread_id,
                terminal_activity_state,
            )
        return project_thread_state(
            thread_id=thread_id,
            snapshot=snapshot,
            run_status=run_status,
            runtime_settings=thread.settings,
            runtime_settings_locked=thread.locked,
            model_label=model_label,
            model_available=thread.model_available,
            activity_runs=self._activity_runs(thread_id),
            embedding_startup_status=self.embedding_startup_status,
        )

    def _dataset_artifact(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        dataset_id: str,
        *,
        allow_current_pending_review: bool = False,
    ) -> dict[str, Any]:
        thread = self._thread(identity, thread_id)
        snapshot = self._snapshot(identity, thread_id, thread)
        if snapshot is None:
            raise KeyError(thread_id)
        values = _projection_values(snapshot)
        datasets = dict(dict(values.get("artifacts") or {}).get("datasets") or {})
        artifact = datasets.get(dataset_id)
        if not isinstance(artifact, dict):
            raise KeyError(dataset_id)
        active_interrupt = _active_interrupt(snapshot, values)
        interrupt_identity = (
            active_interrupt.artifact
            if active_interrupt is not None
            and active_interrupt.type == "dataset_review"
            else None
        )
        is_current_pending_review = (
            allow_current_pending_review
            and is_pending_review_dataset_artifact(artifact)
            and interrupt_identity is not None
            and interrupt_identity.id == dataset_id
            and interrupt_identity.version
            == int(artifact.get("version") or 1)
            and interrupt_identity.expected_status
            == str(artifact.get("status") or "").strip()
        )
        if not is_current_pending_review and not is_selectable_dataset_artifact(artifact):
            raise KeyError(dataset_id)
        return artifact

    def _file_artifact(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        thread = self._thread(identity, thread_id)
        snapshot = self._snapshot(identity, thread_id, thread)
        if snapshot is None:
            raise KeyError(thread_id)
        values = _projection_values(snapshot)
        files = dict(dict(values.get("artifacts") or {}).get("files") or {})
        artifact = files.get(artifact_id)
        if not isinstance(artifact, dict):
            raise KeyError(artifact_id)
        if is_published_artifact(artifact):
            return artifact
        active_interrupt = _active_interrupt(snapshot, values)
        if _matches_pending_analysis_review(
            artifact_id,
            artifact,
            active_interrupt,
        ):
            return artifact
        if _matches_pending_analysis_linked_output(
            artifact_id,
            artifact,
            active_interrupt,
            files,
        ):
            return artifact
        raise KeyError(artifact_id)

    def dataset_preview(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        dataset_id: str | None = None,
        *,
        limit: int = 100,
    ) -> DatasetPreview:
        if not isinstance(identity, RequestIdentity):
            dataset_id = thread_id
            thread_id = identity
        assert dataset_id is not None
        artifact = self._dataset_artifact(
            identity,
            thread_id,
            dataset_id,
            allow_current_pending_review=True,
        )
        df, _schema = load_dataset_artifact(
            artifact,
            runtime_root=self._dataset_storage_scope(identity, thread_id),
        )
        row_limit = min(max(limit, 0), 500)
        preview_df = df.head(row_limit).astype(object)
        preview_df = preview_df.where(preview_df.notna(), None)
        artifact_row_count = artifact.get("row_count")
        row_count = artifact_row_count if isinstance(artifact_row_count, int) else len(df)
        return DatasetPreview(
            dataset_id=dataset_id,
            columns=[str(column) for column in df.columns],
            rows=preview_df.to_dict(orient="records"),
            row_count=row_count,
        )

    def dataset_schema(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        dataset_id: str | None = None,
    ) -> DatasetSchemaResponse:
        if not isinstance(identity, RequestIdentity):
            dataset_id = thread_id
            thread_id = identity
        assert dataset_id is not None
        artifact = self._dataset_artifact(
            identity,
            thread_id,
            dataset_id,
            allow_current_pending_review=True,
        )
        _df, schema = load_dataset_artifact(
            artifact,
            runtime_root=self._dataset_storage_scope(identity, thread_id),
        )
        return DatasetSchemaResponse(dataset_id=dataset_id, schema_=dict(schema or {}))

    def dataset_provenance(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        dataset_id: str | None = None,
    ) -> DatasetProvenance:
        if not isinstance(identity, RequestIdentity):
            dataset_id = thread_id
            thread_id = identity
        assert dataset_id is not None
        artifact = self._dataset_artifact(
            identity,
            thread_id,
            dataset_id,
            allow_current_pending_review=True,
        )
        provenance = dict(artifact.get("provenance") or {})
        sql_id = str(provenance.get("sql_id") or "").strip()
        sql_version = provenance.get("sql_version")
        inline_sql = str(provenance.get("sql") or "").strip()
        if not sql_id or not isinstance(sql_version, int):
            if not inline_sql:
                raise ValueError("Dataset SQL provenance is unavailable")
            return DatasetProvenance(
                dataset_id=dataset_id,
                dataset_version=int(artifact.get("version") or 1),
                sql=inline_sql,
                sql_artifact={
                    "id": f"legacy-{dataset_id}",
                    "kind": "validated_sql",
                    "version": 1,
                },
                sql_sha256=hashlib.sha256(inline_sql.encode("utf-8")).hexdigest(),
            )

        sql_record = self._file_artifact(identity, thread_id, sql_id)
        stored_sql = dict(sql_record.get("content") or {})
        sql_content = dict(stored_sql.get("content") or {})
        if (
            sql_record.get("kind") != "validated_sql"
            or stored_sql.get("kind") != "validated_sql"
            or stored_sql.get("version") != sql_version
            or stored_sql.get("status") != "approved"
            or not isinstance(sql_content.get("sql"), str)
        ):
            raise ValueError("Dataset SQL provenance is invalid")
        exact_sql = sql_content["sql"]
        if inline_sql and inline_sql != exact_sql.strip():
            raise ValueError("Dataset SQL provenance does not match SQL artifact")
        expected_hash = str(sql_content.get("sql_sha256") or "").strip()
        actual_hash = hashlib.sha256(exact_sql.encode("utf-8")).hexdigest()
        if expected_hash and expected_hash != actual_hash:
            raise ValueError("SQL artifact content hash is invalid")
        return DatasetProvenance(
            dataset_id=dataset_id,
            dataset_version=int(artifact.get("version") or 1),
            sql=exact_sql,
            sql_artifact={"id": sql_id, "kind": "validated_sql", "version": sql_version},
            sql_sha256=expected_hash or actual_hash,
        )

    def analysis_result(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        analysis_id: str | None = None,
    ) -> CompletedAnalysisResult:
        if not isinstance(identity, RequestIdentity):
            analysis_id = thread_id
            thread_id = identity
        assert analysis_id is not None
        artifact = self._file_artifact(identity, thread_id, analysis_id)
        stored = dict(artifact.get("content") or {})
        run_content = dict(stored.get("content") or {})
        if (
            artifact.get("kind") != "analysis_run"
            or stored.get("kind") != "analysis_run"
            or stored.get("status") != "active"
        ):
            raise KeyError(analysis_id)
        run = AnalysisRun.model_validate(run_content)
        raw_code = run.specification.get("code")
        return CompletedAnalysisResult(
            analysis_run_id=analysis_id,
            analysis_run_version=int(stored.get("version") or 1),
            method=run.method,
            python_code=raw_code if isinstance(raw_code, str) else "",
            output_text=run.output_text,
            dataset=run.dataset.model_dump(mode="json"),
            dataset_source=(
                run.specification.get("dataset_source")
                if run.specification.get("dataset_source")
                in {"current_upload", "prior_artifact"}
                else "prior_artifact"
            ),
            dataset_source_reason=(
                run.specification.get("dataset_source_reason")
                if isinstance(run.specification.get("dataset_source_reason"), str)
                else ""
            ),
            tables=[identity.model_dump(mode="json") for identity in run.tables],
            figures=[identity.model_dump(mode="json") for identity in run.figures],
        )

    def dataset_csv_bytes(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        dataset_id: str | None = None,
    ) -> bytes:
        if not isinstance(identity, RequestIdentity):
            dataset_id = thread_id
            thread_id = identity
        assert dataset_id is not None
        artifact = self._dataset_artifact(identity, thread_id, dataset_id)
        df, _schema = load_dataset_artifact(
            artifact,
            runtime_root=self._dataset_storage_scope(identity, thread_id),
        )
        return df.to_csv(index=False).encode("utf-8")

    def file_artifact_bytes(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        artifact_id: str | None = None,
    ) -> FileArtifactBytes:
        if not isinstance(identity, RequestIdentity):
            artifact_id = thread_id
            thread_id = identity
        assert artifact_id is not None
        artifact = self._file_artifact(identity, thread_id, artifact_id)
        return _file_artifact_bytes_from_record(artifact_id, artifact)

    def table_preview(
        self,
        identity: RequestIdentity | str,
        thread_id: str,
        artifact_id: str | None = None,
        *,
        limit: int = 100,
    ) -> TablePreview:
        if not isinstance(identity, RequestIdentity):
            artifact_id = thread_id
            thread_id = identity
        assert artifact_id is not None
        artifact = self._file_artifact(identity, thread_id, artifact_id)
        if artifact.get("kind") != "table" or artifact.get("mime") != "text/csv":
            raise ValueError("Only CSV table artifacts can be previewed")
        content = artifact.get("content")
        if (
            isinstance(content, dict)
            and isinstance(content.get("content"), dict)
            and content.get("id") == artifact_id
            and content.get("kind") == "table"
        ):
            content = content["content"]
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            text = content["text"]
        elif isinstance(content, str):
            text = content
        else:
            raise ValueError("CSV table content is unavailable")

        reader = csv.DictReader(io.StringIO(text))
        columns = [str(column) for column in (reader.fieldnames or [])]
        rows: list[dict[str, str | None]] = []
        row_count = 0
        row_limit = min(max(limit, 0), 100)
        for row in reader:
            row_count += 1
            if len(rows) < row_limit:
                rows.append({column: row.get(column) for column in columns})
        return TablePreview(columns=columns, rows=rows, row_count=row_count)

    def reset(self, identity: RequestIdentity | str, thread_id: str | None = None) -> str:
        if isinstance(identity, RequestIdentity):
            assert thread_id is not None
            self._thread(identity, thread_id)
            return self.create_thread(identity)
        return self.create_thread()

    def export_thread(
        self,
        identity: RequestIdentity | str,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        state = self.state(identity, thread_id)
        payload = state.model_dump(mode="json")
        for message in payload.get("conversation", []):
            if isinstance(message, dict) and message.get("status") is None:
                message.pop("status", None)
        return payload

    def export_thread_archive(
        self,
        identity: RequestIdentity | str,
        thread_id: str | None = None,
    ) -> bytes:
        if isinstance(identity, RequestIdentity):
            assert thread_id is not None
        else:
            thread_id = identity
        thread = self._thread(identity, thread_id)
        snapshot = self._snapshot(identity, thread_id, thread)
        if snapshot is None:
            raise KeyError(thread_id)
        values = dict(getattr(snapshot, "values", None) or {})
        return build_thread_export(
            thread_id,
            "openai",
            thread.settings.model_name,
            values,
            attachment_store=self._attachment_store,
            attachment_thread_id=self._attachment_scope(identity, thread_id),
        )


def _message_role(message: Any) -> str:
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, AIMessage):
        return "assistant"
    return "system"


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def _message_created_at(message: Any) -> str | None:
    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    for key in ("created_at", "timestamp"):
        value = additional_kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _message_status(message: Any) -> str | None:
    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    if additional_kwargs.get("status") == "cancelled":
        return "cancelled"
    return None


def _message_attachments(message: Any) -> list[ConversationAttachment]:
    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    attachments = additional_kwargs.get("attachments")
    if not isinstance(attachments, list):
        return []
    result: list[ConversationAttachment] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        artifact_id = str(attachment.get("id") or "").strip()
        if not artifact_id:
            continue
        relationship = str(attachment.get("relationship") or "")
        if relationship not in {"input", "used", "output"}:
            continue
        result.append(
            ConversationAttachment(
                id=artifact_id,
                kind=str(attachment.get("kind") or ""),
                label=str(attachment.get("label") or artifact_id),
                filename=str(attachment.get("filename") or ""),
                mime=str(attachment.get("mime") or ""),
                byte_size=attachment.get("byte_size"),
                relationship=relationship,
                origin_message_id=(
                    str(attachment["origin_message_id"])
                    if attachment.get("origin_message_id")
                    else None
                ),
            )
        )
    return result


def _message_clarifications(message: Any) -> list[ClarificationExchange]:
    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    raw_clarifications = additional_kwargs.get("clarifications")
    if not isinstance(raw_clarifications, list):
        return []
    result: list[ClarificationExchange] = []
    for clarification in raw_clarifications:
        if not isinstance(clarification, dict):
            continue
        fields = {
            field: clarification.get(field)
            for field in ("interrupt_id", "question", "reason", "answer")
        }
        if not all(isinstance(value, str) for value in fields.values()):
            continue
        result.append(ClarificationExchange(**fields))
    return result


def _conversation(values: dict[str, Any]) -> list[ConversationMessage]:
    result: list[ConversationMessage] = []
    for index, message in enumerate(build_display_history(values)):
        text = _message_text(message).strip()
        attachments = _message_attachments(message)
        clarifications = _message_clarifications(message)
        if not text and not attachments and not clarifications:
            continue
        result.append(
            ConversationMessage(
                id=str(getattr(message, "id", "") or f"message-{index}"),
                role=_message_role(message),
                text=text,
                status=_message_status(message),
                created_at=_message_created_at(message),
                attachments=attachments,
                clarifications=clarifications,
            )
        )
    return result


def _datasets(values: dict[str, Any]) -> list[DatasetSummary]:
    datasets = dict(dict(values.get("artifacts") or {}).get("datasets") or {})
    result: list[DatasetSummary] = []
    for dataset_id, artifact in datasets.items():
        if not is_selectable_dataset_artifact(dict(artifact or {})):
            continue
        result.append(_dataset_summary(dict(artifact or {}), fallback_id=dataset_id))
    return result


def _dataset_summary(
    artifact: dict[str, Any],
    *,
    fallback_id: str = "",
) -> DatasetSummary:
    dataset_id = str(artifact.get("id") or fallback_id)
    row_count = artifact.get("row_count")
    label = (
        dataset_artifact_description(artifact)
        or str(artifact.get("description") or "").strip()
        or str(artifact.get("label") or "").strip()
        or dataset_id
    )
    return DatasetSummary(
        id=dataset_id,
        label=label,
        row_count=row_count if isinstance(row_count, int) else None,
    )


def _file_artifacts(values: dict[str, Any]) -> list[FileArtifactSummary]:
    files = dict(dict(values.get("artifacts") or {}).get("files") or {})
    result: list[FileArtifactSummary] = []
    for artifact_id, artifact in files.items():
        if not isinstance(artifact, dict):
            continue
        if not is_published_artifact(artifact):
            continue
        result.append(
            FileArtifactSummary(
                id=str(artifact.get("artifact_id") or artifact_id),
                kind=str(artifact.get("kind") or ""),
                label=str(artifact.get("summary") or artifact_id),
                mime=str(artifact.get("mime") or ""),
                status=str(artifact.get("status") or ""),
            )
        )
    return result


def _artifact_filename(artifact_id: str, mime: str) -> str:
    suffix_by_mime = {
        "image/png": ".png",
        "text/plain": ".txt",
        "text/sql": ".sql",
        "application/sql": ".sql",
        "application/json": ".json",
        "text/csv": ".csv",
    }
    suffix = suffix_by_mime.get(mime, "")
    return f"{artifact_id}{suffix}"


def _file_artifact_bytes_from_record(
    artifact_id: str,
    artifact: dict[str, Any],
) -> FileArtifactBytes:
    content = artifact.get("content")
    if (
        isinstance(content, dict)
        and isinstance(content.get("content"), dict)
        and artifact.get("kind") in {"figure", "table"}
        and content.get("id") == artifact_id
        and content.get("kind") == artifact.get("kind")
    ):
        content = content["content"]
    mime = str(artifact.get("mime") or "application/octet-stream")
    filename = _artifact_filename(artifact_id, mime)
    if isinstance(content, dict) and isinstance(content.get("path"), str):
        return FileArtifactBytes(
            content=Path(content["path"]).read_bytes(),
            mime=mime,
            filename=filename,
        )
    if isinstance(content, dict) and isinstance(
        content.get("data_base64"),
        str,
    ):
        return FileArtifactBytes(
            content=base64.b64decode(content["data_base64"]),
            mime=mime,
            filename=filename,
        )
    if isinstance(content, str):
        return FileArtifactBytes(
            content=content.encode("utf-8"),
            mime=mime,
            filename=filename,
        )
    if isinstance(content, (dict, list)):
        return FileArtifactBytes(
            content=json.dumps(content, indent=2).encode("utf-8"),
            mime=mime,
            filename=filename,
        )
    raise TypeError("Unsupported artifact content shape")


def _active_interrupt(snapshot: Any, values: dict[str, Any]) -> ActiveInterrupt | None:
    interrupts = list(getattr(snapshot, "interrupts", None) or [])
    if not interrupts:
        return None
    event = interrupts[0]
    projected = project_review_interrupt(event, values)
    if projected is None:
        return None
    try:
        return _ACTIVE_INTERRUPT_ADAPTER.validate_python(projected)
    except ValidationError:
        return None


def project_thread_state(
    *,
    thread_id: str,
    snapshot: Any,
    run_status: dict[str, Any],
    runtime_settings: RuntimeSettings | None = None,
    runtime_settings_locked: bool = False,
    model_label: str = "",
    model_available: bool = True,
    activity_runs: list[ActivityRun] | None = None,
    embedding_startup_status: EmbeddingStartupStatus | None = None,
) -> ApiThreadState:
    values = _projection_values(snapshot)
    snapshot_next = list(getattr(snapshot, "next", None) or [])
    raw_interrupts = list(getattr(snapshot, "interrupts", None) or [])
    artifacts = dict(values.get("artifacts") or {})
    status = RunStatus(
        state=run_status.get("state", "idle"),
        steps=int(run_status.get("steps", 0) or 0),
        error=run_status.get("error"),
        error_code=run_status.get("error_code"),
        user_message=run_status.get("user_message"),
        started_at=run_status.get("started_at"),
        updated_at=run_status.get("updated_at"),
    )
    interrupt = _active_interrupt(snapshot, values)
    has_unprojectable_interrupt = interrupt is None and any(
        isinstance(getattr(event, "value", None), dict)
        and str(event.value.get("type") or "") in _PUBLIC_INTERRUPT_TYPES
        for event in raw_interrupts
    )
    terminal_error = dict(values.get("terminal_error") or {})
    interrupt_artifact = {}
    if interrupt is not None and interrupt.type in {
        "dataset_plan_review",
        "dataset_review",
        "analysis_result_review",
    }:
        interrupt_artifact = interrupt.artifact.model_dump(mode="json")
    if has_unprojectable_interrupt and status.state != "running":
        status = RunStatus(
            state="error",
            steps=status.steps,
            error="A pending workflow interrupt could not be projected for display.",
            error_code="INTERRUPT_PROJECTION_FAILED",
            user_message=(
                "A pending review could not be displayed. This workflow is paused; "
                "do not submit another request. Refresh after the service is repaired."
            ),
            started_at=status.started_at,
            updated_at=status.updated_at,
        )
    elif terminal_error and interrupt is None:
        message = str(terminal_error.get("message") or "Workflow stopped.")
        status = RunStatus(
            state="error",
            steps=status.steps,
            error=message,
            error_code=str(
                terminal_error.get("code") or "WORKFLOW_TERMINAL_ERROR"
            ),
            user_message=message,
            started_at=status.started_at,
            updated_at=status.updated_at,
        )
    elif (
        dict(values.get("terminal_control") or {}).get("status")
        == "cancelled"
        and status.state != "running"
    ):
        status.state = "cancelled"
    elif interrupt is not None and status.state in {"idle", "done"}:
        status.state = "interrupted"
    elif (
        interrupt is None
        and status.state == "interrupted"
        and not snapshot_next
        and not _has_blocking_interrupt(snapshot)
    ):
        status.state = "done"
    return ApiThreadState(
        thread_id=thread_id,
        run=status,
        conversation=_conversation(values),
        activity_runs=list(activity_runs or []),
        active_interrupt=interrupt,
        datasets=_datasets(values),
        file_artifacts=_file_artifacts(values),
        output=dict(values.get("output") or {}),
        diagnostics={
            "semantic_graph": "epi_agent",
            "checkpoint_scope": "root",
            "thread_id": thread_id,
            "run_state": status.state,
            "run_steps": status.steps,
            "snapshot_next": snapshot_next,
            "interrupt_count": len(raw_interrupts),
            "active_dataset_id": artifacts.get("active_dataset_id"),
            "active_interrupt_artifact": interrupt_artifact or None,
            "dataset_ids": sorted(
                str(dataset_id)
                for dataset_id, artifact in dict(artifacts.get("datasets") or {}).items()
                if is_selectable_dataset_artifact(dict(artifact or {}))
            ),
            "file_artifact_ids": sorted(
                str(artifact_id)
                for artifact_id, artifact in dict(artifacts.get("files") or {}).items()
                if isinstance(artifact, dict) and is_published_artifact(artifact)
            ),
        },
        runtime_settings=runtime_settings,
        runtime_settings_locked=runtime_settings_locked,
        model_name=runtime_settings.model_name if runtime_settings else "",
        model_label=(
            model_label
            or (runtime_settings.model_name if runtime_settings else "")
        ),
        model_available=model_available,
        model_replacement_required=not model_available,
        embedding_startup_status=(
            embedding_startup_status or silent_embedding_startup_status()
        ),
    )
