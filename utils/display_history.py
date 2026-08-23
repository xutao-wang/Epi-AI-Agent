from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from utils.artifact_publication import is_published_artifact
from utils.dataset_artifacts import dataset_artifact_description
from utils.message_window import compact_messages


def _conversation_events(state: dict) -> list[dict]:
    artifacts = dict(state.get("artifacts") or {})
    return [
        dict(event)
        for event in list(artifacts.get("conversation_events") or [])
        if isinstance(event, dict)
    ]


def _artifact_files(state: dict) -> dict[str, dict]:
    artifacts = dict(state.get("artifacts") or {})
    return {
        str(artifact_id): dict(record)
        for artifact_id, record in dict(artifacts.get("files") or {}).items()
        if isinstance(record, dict)
    }


def _attachment_manifests(state: dict) -> tuple[dict[str, dict], set[str], set[str]]:
    artifacts = dict(state.get("artifacts") or {})
    uploaded = {
        str(attachment_id): dict(record)
        for attachment_id, record in dict(artifacts.get("attachments") or {}).items()
        if isinstance(record, dict)
    }
    files = _artifact_files(state)
    datasets = {
        str(dataset_id): dict(record)
        for dataset_id, record in dict(artifacts.get("datasets") or {}).items()
        if isinstance(record, dict)
    }
    return (
        {**uploaded, **files, **datasets},
        set(files),
        set(datasets),
    )


def _attachment_is_visible(
    manifest: dict,
    relationship: str,
    *,
    is_file: bool,
    is_dataset: bool,
) -> bool:
    if relationship in {"input", "used"}:
        return manifest.get("status") == "available"
    if relationship != "output":
        return False
    if is_file:
        return is_published_artifact(manifest)
    if is_dataset:
        return manifest.get("status") == "active"
    return False


def _project_attachment(
    manifest: dict,
    attachment_id: str,
    relationship: str,
    *,
    is_dataset: bool,
    origin_message_id: str | None,
) -> dict:
    byte_size = manifest.get("byte_size")
    if isinstance(byte_size, bool) or not isinstance(byte_size, int):
        byte_size = None
    return {
        "id": str(
            manifest.get("id")
            or manifest.get("artifact_id")
            or attachment_id
        ),
        "kind": str(manifest.get("kind") or ""),
        "label": str(
            manifest.get("filename")
            or (dataset_artifact_description(manifest) if is_dataset else "")
            or manifest.get("summary")
            or manifest.get("label")
            or attachment_id
        ),
        "filename": str(manifest.get("filename") or ""),
        "mime": str(manifest.get("mime") or ""),
        "byte_size": byte_size,
        "relationship": relationship,
        "origin_message_id": origin_message_id,
    }


def _attachments_by_parent_event_id(
    state: dict,
    events: list[dict],
) -> dict[str, list[dict]]:
    manifests, file_ids, dataset_ids = _attachment_manifests(state)
    input_origins: dict[str, str] = {}
    for event in events:
        if event.get("type") != "attachment" or event.get("relationship") != "input":
            continue
        artifact_id = event.get("artifact_id")
        parent_event_id = event.get("parent_event_id")
        if isinstance(artifact_id, str) and isinstance(parent_event_id, str):
            input_origins.setdefault(artifact_id, parent_event_id)

    projected: dict[str, list[dict]] = {}
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        if event.get("type") != "attachment":
            continue
        parent_event_id = str(event.get("parent_event_id") or "")
        artifact_id = str(event.get("artifact_id") or "")
        relationship = str(event.get("relationship") or "")
        key = (parent_event_id, artifact_id, relationship)
        if not all(key) or key in seen:
            continue
        manifest = manifests.get(artifact_id)
        if manifest is None:
            continue
        if not _attachment_is_visible(
            manifest,
            relationship,
            is_file=artifact_id in file_ids,
            is_dataset=artifact_id in dataset_ids,
        ):
            continue
        if not isinstance(parent_event_id, str) or not parent_event_id.strip():
            continue
        seen.add(key)
        projected.setdefault(parent_event_id, []).append(
            _project_attachment(
                manifest,
                artifact_id,
                relationship,
                is_dataset=artifact_id in dataset_ids,
                origin_message_id=(
                    input_origins.get(artifact_id)
                    if relationship == "used"
                    else None
                ),
            )
        )
    return projected


def _contains_normalized_text(haystack: str, needle: str) -> bool:
    normalized_haystack = " ".join(str(haystack or "").split())
    normalized_needle = " ".join(str(needle or "").split())
    return bool(normalized_needle and normalized_needle in normalized_haystack)


def _message_kwargs_for_event(event: dict) -> dict:
    additional_kwargs = {}
    created_at = event.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        additional_kwargs["created_at"] = created_at
    if event.get("status") == "cancelled":
        additional_kwargs["status"] = "cancelled"
    return additional_kwargs


def _clarifications_by_turn_hash(events: list[dict]) -> dict[str, list[dict]]:
    traces: dict[str, list[dict]] = {}
    for event in events:
        if event.get("type") != "clarification_exchange":
            continue
        user_turn_hash = event.get("user_turn_hash")
        if not isinstance(user_turn_hash, str) or not user_turn_hash.strip():
            continue
        fields = {
            field: event.get(field)
            for field in ("interrupt_id", "question", "reason", "answer")
        }
        if not all(isinstance(value, str) for value in fields.values()):
            continue
        traces.setdefault(user_turn_hash, []).append(fields)
    return traces


def _final_assistant_event_ids(events: list[dict]) -> dict[str, str]:
    result: dict[str, str] = {}
    for event in events:
        if event.get("type") != "assistant":
            continue
        user_turn_hash = event.get("user_turn_hash")
        event_id = event.get("event_id")
        if isinstance(user_turn_hash, str) and user_turn_hash and isinstance(event_id, str):
            result[user_turn_hash] = event_id
    return result


def build_display_history(state: dict) -> list:
    events = _conversation_events(state)
    if not events:
        return compact_messages(list(state.get("messages", [])))

    attachments = _attachments_by_parent_event_id(state, events)
    clarifications = _clarifications_by_turn_hash(events)
    final_assistant_event_ids = _final_assistant_event_ids(events)
    display_messages: list = []
    assistant_text_by_turn_hash: dict[str, str] = {}
    for event in events:
        event_type = event.get("type")
        text = str(event.get("text") or "").strip()
        if event_type == "user":
            event_id = str(event.get("event_id") or "")
            additional_kwargs = _message_kwargs_for_event(event)
            if event_id in attachments:
                additional_kwargs["attachments"] = attachments[event_id]
            if text or event_id in attachments:
                display_messages.append(
                    HumanMessage(
                        content=text,
                        additional_kwargs=additional_kwargs,
                        id=event_id or None,
                    )
                )
            continue
        if event_type == "review_decision":
            if event.get("decision") in {"approve", "cancel"}:
                continue
            if text:
                display_messages.append(
                    HumanMessage(
                        content=text,
                        additional_kwargs=_message_kwargs_for_event(event),
                        id=str(event.get("event_id") or "") or None,
                    )
                )
            continue
        if event_type not in {"assistant", "clarification"}:
            continue
        user_turn_hash = event.get("user_turn_hash")
        if (
            event_type == "clarification"
            and isinstance(user_turn_hash, str)
            and _contains_normalized_text(
                assistant_text_by_turn_hash.get(user_turn_hash, ""),
                text,
            )
        ):
            continue
        additional_kwargs = _message_kwargs_for_event(event)
        event_id = event.get("event_id")
        if (
            event_type == "assistant"
            and isinstance(user_turn_hash, str)
            and event_id == final_assistant_event_ids.get(user_turn_hash)
            and user_turn_hash in clarifications
        ):
            additional_kwargs["clarifications"] = clarifications[user_turn_hash]
        if isinstance(event_id, str) and event_id in attachments:
            additional_kwargs["attachments"] = attachments[event_id]
        if not text and not additional_kwargs.get("attachments"):
            continue
        display_messages.append(
            AIMessage(
                content=text,
                additional_kwargs=additional_kwargs,
                id=str(event_id or "") or None,
            )
        )
        if event_type == "assistant" and isinstance(user_turn_hash, str):
            assistant_text_by_turn_hash[user_turn_hash] = text
    return display_messages
