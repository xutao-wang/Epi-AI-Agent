from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
from uuid import uuid4

from .conversation_schema import (
    ConversationArtifactInput,
    ConversationArtifactRecord,
    ConversationArtifacts,
    ConversationEventInput,
    ConversationMeta,
    JsonObject,
    JsonValue,
)
from .state import MetaKeys


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# These helpers normalize and return a full state dict directly so conversation
# event and file containers stay explicit and JSON-only at the boundary.
def _json_safe_value(value: object, path: str) -> JsonValue:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path} must be JSON-serializable; got non-finite float")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_json_safe_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} must use string keys; got {type(key).__name__}")
            normalized[key] = _json_safe_value(item, f"{path}.{key}")
        return normalized
    raise TypeError(f"{path} must be JSON-serializable; got {type(value).__name__}")


def _normalize_event_input(event: ConversationEventInput) -> JsonObject:
    normalized = _json_safe_value(dict(event), "event")
    if not isinstance(normalized, dict):  # pragma: no cover - defensive guard
        raise TypeError("event must normalize to a JSON object")
    return normalized


def _normalize_artifact(artifact: ConversationArtifactInput) -> JsonObject:
    normalized = _json_safe_value(dict(artifact), "artifact")
    if not isinstance(normalized, dict):  # pragma: no cover - defensive guard
        raise TypeError("artifact must normalize to a JSON object")
    return normalized  # type: ignore[return-value]


def _build_event(
    *,
    actor: str,
    actor_role: str,
    event_type: str,
    user_turn_hash: str | None,
    **fields: JsonValue,
) -> JsonObject:
    event: JsonObject = {
        "actor": actor,
        "actor_role": actor_role,
        "type": event_type,
        "user_turn_hash": user_turn_hash,
    }
    event.update(fields)
    return event


def build_user_event(
    *,
    actor: str,
    user_turn_hash: str | None,
    text: str,
    status: str | None = None,
    parent_event_id: str | None = None,
) -> JsonObject:
    event = _build_event(
        actor=actor,
        actor_role="user",
        event_type="user",
        user_turn_hash=user_turn_hash,
        text=text,
    )
    if status is not None:
        event["status"] = status
    if parent_event_id is not None:
        event["parent_event_id"] = parent_event_id
    return event


def build_assistant_event(
    *,
    actor: str,
    user_turn_hash: str | None,
    text: str,
    status: str | None = None,
    parent_event_id: str | None = None,
) -> JsonObject:
    event = _build_event(
        actor=actor,
        actor_role="assistant",
        event_type="assistant",
        user_turn_hash=user_turn_hash,
        text=text,
    )
    if status is not None:
        event["status"] = status
    if parent_event_id is not None:
        event["parent_event_id"] = parent_event_id
    return event


def build_clarification_exchange_event(
    *,
    actor: str,
    user_turn_hash: str | None,
    interrupt_id: str,
    question: str,
    reason: str,
    answer: str,
) -> JsonObject:
    return _build_event(
        actor=actor,
        actor_role="review",
        event_type="clarification_exchange",
        user_turn_hash=user_turn_hash,
        interrupt_id=interrupt_id,
        question=question,
        reason=reason,
        answer=answer,
    )


def build_attachment_event(
    *,
    actor: str,
    user_turn_hash: str | None,
    artifact_id: str,
    relationship: str,
    parent_event_id: str,
    status: str | None = None,
) -> JsonObject:
    if relationship not in {"input", "used", "output"}:
        raise ValueError("attachment relationship must be input, used, or output")
    if not isinstance(parent_event_id, str) or not parent_event_id.strip():
        raise ValueError("attachment parent_event_id is required")
    event = _build_event(
        actor=actor,
        actor_role="system",
        event_type="attachment",
        user_turn_hash=user_turn_hash,
        artifact_id=artifact_id,
        relationship=relationship,
        parent_event_id=parent_event_id,
    )
    if status is not None:
        event["status"] = status
    return event


def _existing_event_seq_max(events: list[JsonObject]) -> int:
    highest = 0
    for event in events:
        seq = event.get("seq")
        if isinstance(seq, bool):
            continue
        if isinstance(seq, int) and seq > highest:
            highest = seq
    return highest


def _coerce_next_seq(meta_value: object, event_history: list[dict[str, JsonValue]]) -> int:
    history_next = _existing_event_seq_max(event_history) + 1
    if isinstance(meta_value, bool):
        meta_next = 0
    elif isinstance(meta_value, int):
        meta_next = meta_value
    elif isinstance(meta_value, str) and meta_value.isdigit():
        meta_next = int(meta_value)
    else:
        meta_next = 0
    return max(1, history_next, meta_next)


def _normalize_artifacts(raw_artifacts: object) -> ConversationArtifacts:
    artifacts = dict(raw_artifacts or {})
    artifacts["conversation_events"] = [
        _normalize_existing_event(event)
        for event in list(artifacts.get("conversation_events") or [])
    ]
    artifacts["conversation_events_version"] = int(artifacts.get("conversation_events_version", 1) or 1)
    artifacts["artifact_manifest_version"] = int(artifacts.get("artifact_manifest_version", 1) or 1)
    normalized_files: dict[str, ConversationArtifactRecord] = {}
    for artifact_id, record in dict(artifacts.get("files") or {}).items():
        if not isinstance(artifact_id, str):
            raise TypeError(
                f"artifacts.files must use string keys; got {type(artifact_id).__name__}"
            )
        normalized_files[artifact_id] = _normalize_existing_artifact(record)
    artifacts["files"] = normalized_files
    normalized_attachments: dict[str, JsonObject] = {}
    for attachment_id, manifest in dict(artifacts.get("attachments") or {}).items():
        if not isinstance(attachment_id, str):
            raise TypeError(
                "artifacts.attachments must use string keys; "
                f"got {type(attachment_id).__name__}"
            )
        normalized = _json_safe_value(
            manifest,
            f"artifacts.attachments.{attachment_id}",
        )
        if not isinstance(normalized, dict):
            raise TypeError("artifacts.attachments must contain JSON objects")
        if normalized.get("id") != attachment_id:
            raise ValueError(
                "attachment manifest id must match its artifacts.attachments key"
            )
        normalized_attachments[attachment_id] = normalized
    artifacts["attachments"] = normalized_attachments
    return artifacts


def _normalize_meta(raw_meta: object, event_history: list[JsonObject]) -> ConversationMeta:
    meta = dict(raw_meta or {})
    next_seq = _coerce_next_seq(meta.get(MetaKeys.NEXT_EVENT_SEQ), event_history)
    meta[MetaKeys.NEXT_EVENT_SEQ] = next_seq
    return meta


def _normalize_existing_event(event: object) -> JsonObject:
    normalized = _json_safe_value(event, "artifacts.conversation_events[]")
    if not isinstance(normalized, dict):
        raise TypeError("artifacts.conversation_events[] must contain JSON objects")
    validated = dict(normalized)
    _validate_event_shape(validated, persisted=True)
    return validated


def _normalize_existing_artifact(record: object) -> JsonObject:
    normalized = _json_safe_value(record, "artifacts.files[]")
    if not isinstance(normalized, dict):
        raise TypeError("artifacts.files[] must contain JSON objects")
    _validate_artifact_shape(normalized, persisted=True)
    return normalized  # type: ignore[return-value]


def _require_event_fields(event: JsonObject) -> None:
    required_fields = ("actor", "actor_role", "type", "user_turn_hash")
    missing = [field for field in required_fields if field not in event]
    if missing:
        raise ValueError(
            "event is missing required fields: " + ", ".join(missing)
        )
    event_type = event["type"]
    if event_type == "user" or event_type == "assistant":
        _require_fields(event, ("text",), label=f"{event_type} event")
        return
    if event_type == "clarification":
        _require_fields(event, ("text",), label=f"{event_type} event")
        return
    if event_type == "clarification_exchange":
        _require_fields(
            event,
            ("interrupt_id", "question", "reason", "answer"),
            label="clarification_exchange event",
        )
        return
    if event_type == "code":
        _require_fields(event, ("artifact_id", "text"), label="code event")
        return
    if event_type == "execution_started":
        _require_fields(event, ("text",), label="execution_started event")
        return
    if event_type == "execution_finished":
        _require_fields(event, ("text",), label="execution_finished event")
        return
    if event_type == "figure":
        _require_fields(event, ("artifact_id", "text"), label="figure event")
        return
    if event_type == "attachment":
        _require_fields(
            event,
            ("artifact_id", "relationship", "parent_event_id"),
            label="attachment event",
        )
        return
    if event_type == "sql":
        _require_fields(event, ("artifact_id", "text"), label="sql event")
        return
    if event_type == "review_request":
        _require_fields(event, ("review_kind", "text", "artifact_id"), label="review_request event")
        return
    if event_type == "review_decision":
        _require_fields(event, ("review_kind", "decision", "text"), label="review_decision event")
        return
    if event_type == "error":
        _require_fields(event, ("text", "error"), label="error event")
        return
    if event_type == "routing_decision":
        _require_fields(event, ("decision",), label="routing_decision event")
        return
    if event_type == "tool_call":
        _require_fields(event, ("tool", "args"), label="tool_call event")
        return
    if event_type == "tool_result":
        _require_fields(event, ("tool", "text", "artifact_id"), label="tool_result event")
        return
    raise ValueError(f"event has unsupported type: {event_type}")


def _require_fields(record: JsonObject, fields: tuple[str, ...], *, label: str) -> None:
    missing = [field for field in fields if field not in record]
    if missing:
        raise ValueError(f"{label} is missing required fields: " + ", ".join(missing))


def _require_artifact_fields(artifact: JsonObject) -> None:
    _require_fields(
        artifact,
        ("kind", "producer", "mime", "summary", "content"),
        label="artifact",
    )


def _expect_string(record: JsonObject, field: str, *, label: str) -> None:
    value = record[field]
    if not isinstance(value, str):
        raise TypeError(f"{label}.{field} must be a string")


def _expect_optional_string(record: JsonObject, field: str, *, label: str) -> None:
    value = record[field]
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{label}.{field} must be a string or null")


def _validate_event_shape(event: JsonObject, *, persisted: bool) -> None:
    _require_event_fields(event)
    label = "event"
    for field in ("actor", "actor_role", "type"):
        _expect_string(event, field, label=label)
    _expect_optional_string(event, "user_turn_hash", label=label)

    for optional_str in ("status",):
        if optional_str in event:
            _expect_string(event, optional_str, label=label)
    for optional_nullable_str in ("parent_event_id", "resolved_by_event_id", "superseded_by_event_id"):
        if optional_nullable_str in event:
            _expect_optional_string(event, optional_nullable_str, label=label)

    event_type = event["type"]
    if event_type in {"user", "assistant", "clarification"}:
        _expect_string(event, "text", label=label)
    elif event_type == "clarification_exchange":
        for field in ("interrupt_id", "question", "reason", "answer"):
            _expect_string(event, field, label=label)
    elif event_type == "code":
        _expect_string(event, "artifact_id", label=label)
        _expect_string(event, "text", label=label)
    elif event_type == "execution_started":
        _expect_string(event, "text", label=label)
    elif event_type == "execution_finished":
        _expect_string(event, "text", label=label)
        if "artifact_id" in event:
            _expect_optional_string(event, "artifact_id", label=label)
    elif event_type == "figure":
        _expect_string(event, "artifact_id", label=label)
        _expect_string(event, "text", label=label)
    elif event_type == "attachment":
        _expect_string(event, "artifact_id", label=label)
        _expect_string(event, "relationship", label=label)
        if event["relationship"] not in {"input", "used", "output"}:
            raise ValueError(
                "event.relationship must be input, used, or output"
            )
        _expect_string(event, "parent_event_id", label=label)
        if not str(event["parent_event_id"]).strip():
            raise ValueError("event.parent_event_id is required")
    elif event_type == "sql":
        _expect_string(event, "artifact_id", label=label)
        _expect_string(event, "text", label=label)
    elif event_type == "review_request":
        _expect_string(event, "review_kind", label=label)
        _expect_string(event, "text", label=label)
        _expect_optional_string(event, "artifact_id", label=label)
    elif event_type == "review_decision":
        _expect_string(event, "review_kind", label=label)
        _expect_string(event, "decision", label=label)
        _expect_string(event, "text", label=label)
    elif event_type == "error":
        _expect_string(event, "text", label=label)
        if not isinstance(event["error"], dict):
            raise TypeError("event.error must be a JSON object")
    elif event_type == "routing_decision":
        _expect_string(event, "decision", label=label)
    elif event_type == "tool_call":
        _expect_string(event, "tool", label=label)
        if not isinstance(event["args"], dict):
            raise TypeError("event.args must be a JSON object")
    elif event_type == "tool_result":
        _expect_string(event, "tool", label=label)
        _expect_string(event, "text", label=label)
        _expect_optional_string(event, "artifact_id", label=label)
    else:  # pragma: no cover - guarded in _require_event_fields
        raise ValueError(f"event has unsupported type: {event_type}")

    if persisted:
        _require_fields(event, ("event_id", "seq", "created_at"), label="persisted event")
        _expect_string(event, "event_id", label=label)
        seq = event["seq"]
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise TypeError("event.seq must be an integer")
        _expect_string(event, "created_at", label=label)


def _validate_artifact_shape(artifact: JsonObject, *, persisted: bool) -> None:
    _require_artifact_fields(artifact)
    label = "artifact"
    for field in ("kind", "producer", "summary"):
        _expect_string(artifact, field, label=label)
    _expect_optional_string(artifact, "mime", label=label)
    if "status" in artifact:
        _expect_string(artifact, "status", label=label)
    if persisted:
        _require_fields(artifact, ("artifact_id", "created_at"), label="persisted artifact")
        _expect_string(artifact, "artifact_id", label=label)
        _expect_string(artifact, "created_at", label=label)


def ensure_conversation_state(state: dict) -> dict:
    artifacts = _normalize_artifacts(state.get("artifacts"))
    meta = _normalize_meta(state.get("meta"), list(artifacts.get("conversation_events") or []))
    return {**state, "artifacts": artifacts, "meta": meta}


def append_conversation_event(state: dict, event: ConversationEventInput) -> dict:
    updated = ensure_conversation_state(state)
    artifacts = _normalize_artifacts(updated.get("artifacts"))
    event_history = list(artifacts.get("conversation_events") or [])
    meta = _normalize_meta(updated.get("meta"), event_history)

    seq = int(meta[MetaKeys.NEXT_EVENT_SEQ])
    normalized_input = _normalize_event_input(event)
    _validate_event_shape(normalized_input, persisted=False)
    if normalized_input.get("type") == "attachment":
        identity = (
            normalized_input.get("parent_event_id"),
            normalized_input.get("artifact_id"),
            normalized_input.get("relationship"),
        )
        for existing in event_history:
            if existing.get("type") != "attachment":
                continue
            if (
                existing.get("parent_event_id"),
                existing.get("artifact_id"),
                existing.get("relationship"),
            ) == identity:
                return updated
    normalized = {
        **normalized_input,
        "event_id": str(uuid4()),
        "seq": seq,
        "created_at": _utc_now(),
    }

    event_history.append(normalized)
    artifacts["conversation_events"] = event_history
    meta[MetaKeys.NEXT_EVENT_SEQ] = seq + 1
    return {**updated, "artifacts": artifacts, "meta": meta}


def store_thread_artifact(state: dict, artifact: ConversationArtifactInput) -> dict:
    updated = ensure_conversation_state(state)
    artifacts = _normalize_artifacts(updated.get("artifacts"))

    artifact_id = str(uuid4())
    files = dict(artifacts.get("files") or {})
    normalized_input = _normalize_artifact(artifact)
    _validate_artifact_shape(normalized_input, persisted=False)
    normalized = {
        **normalized_input,
        "artifact_id": artifact_id,
        "created_at": _utc_now(),
    }
    files[artifact_id] = normalized
    artifacts["files"] = files
    return {**updated, "artifacts": artifacts}
