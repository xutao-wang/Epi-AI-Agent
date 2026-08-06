from __future__ import annotations

import base64
import io
import json
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from graph.state_views import get_artifact_files, get_conversation_events
from utils.attachment_artifacts import AttachmentError, LocalAttachmentStore
from utils.artifact_publication import is_published_artifact
from utils.dataset_artifacts import load_dataset_artifact
from utils.display_history import build_display_history


def _json_safe(value):
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "size": len(value),
        }
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _message_role(message):
    message_type = getattr(message, "type", None)
    if message_type == "human":
        return "human"
    if message_type == "ai":
        return "ai"
    if isinstance(message, HumanMessage):
        return "human"
    if isinstance(message, AIMessage):
        return "ai"
    return type(message).__name__


def serialize_messages(messages):
    serialized = []
    for index, message in enumerate(messages, start=1):
        serialized.append(
            {
                "index": index,
                "role": _message_role(message),
                "content": message.content,
                "additional_kwargs": _json_safe(
                    dict(getattr(message, "additional_kwargs", {}) or {})
                ),
            }
        )
    return serialized


def _artifact_extension(kind: str, mime: str | None) -> str:
    if kind == "figure" or mime == "image/png":
        return ".png"
    if kind == "code":
        return ".py"
    if kind == "text":
        return ".txt"
    if kind == "sql":
        return ".sql"
    return ".json"


def _artifact_bytes(record: dict) -> bytes:
    content = record.get("content")
    if isinstance(content, dict):
        path_value = content.get("path")
        if isinstance(path_value, str) and path_value:
            return Path(path_value).read_bytes()
        data_base64 = content.get("data_base64")
        if isinstance(data_base64, str):
            return base64.b64decode(data_base64)
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, (dict, list)):
        return json.dumps(content, indent=2, ensure_ascii=False).encode("utf-8")
    if isinstance(content, bytes):
        return content
    raise ValueError("artifact is missing exportable content")


def _linked_output_ids(state: dict) -> set[str]:
    return {
        str(event.get("artifact_id"))
        for event in get_conversation_events(state)
        if event.get("type") == "attachment"
        and event.get("relationship") == "output"
        and str(event.get("artifact_id") or "").strip()
    }


def _artifact_manifest_and_files(
    state: dict,
) -> tuple[list[dict], list[tuple[str, bytes]]]:
    artifact_files = get_artifact_files(state)
    linked_output_ids = _linked_output_ids(state)
    manifest: list[dict] = []
    archive_files: list[tuple[str, bytes]] = []
    for artifact_id, record in artifact_files.items():
        if artifact_id not in linked_output_ids or not is_published_artifact(record):
            continue
        kind = str(record.get("kind") or "artifact")
        mime = record.get("mime")
        extension = _artifact_extension(kind, mime if isinstance(mime, str) or mime is None else None)
        filename = f"artifacts/{artifact_id}{extension}"
        manifest.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "producer": record.get("producer"),
                "mime": mime,
                "summary": record.get("summary"),
                "created_at": record.get("created_at"),
                "filename": filename,
                "content_source": "artifacts.files",
            }
        )
        archive_files.append((filename, _artifact_bytes(record)))
    return manifest, archive_files


def _public_attachment_manifest(
    manifest: dict[str, Any],
    *,
    archive_path: str | None,
) -> dict[str, Any]:
    public = {
        key: manifest.get(key)
        for key in (
            "id",
            "artifact_id",
            "origin",
            "kind",
            "format",
            "filename",
            "mime",
            "byte_size",
            "sha256",
            "status",
            "created_at",
            "summary",
            "producer",
        )
        if manifest.get(key) is not None
    }
    if archive_path is not None:
        public["archive_path"] = archive_path
    return public


def _visible_attachment_graph(
    thread_id: str,
    state: dict,
    attachment_store: LocalAttachmentStore | None,
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    artifacts = dict(state.get("artifacts") or {})
    uploaded = {
        str(attachment_id): dict(manifest)
        for attachment_id, manifest in dict(
            artifacts.get("attachments") or {}
        ).items()
        if isinstance(manifest, dict)
    }
    files = get_artifact_files(state)
    datasets = {
        str(dataset_id): dict(record)
        for dataset_id, record in dict(
            artifacts.get("datasets") or {}
        ).items()
        if isinstance(record, dict)
    }
    links: list[dict[str, Any]] = []
    visible_ids: set[str] = set()
    input_ids: set[str] = set()

    for event in get_conversation_events(state):
        if event.get("type") != "attachment":
            continue
        attachment_id = str(event.get("artifact_id") or "").strip()
        relationship = str(event.get("relationship") or "").strip()
        if not attachment_id or relationship not in {"input", "used", "output"}:
            continue
        manifest = uploaded.get(attachment_id)
        visible = False
        if relationship in {"input", "used"}:
            visible = bool(
                manifest is not None
                and manifest.get("status") == "available"
            )
        elif attachment_id in files:
            visible = is_published_artifact(files[attachment_id])
        elif attachment_id in datasets:
            visible = datasets[attachment_id].get("status") == "active"
        if not visible:
            continue
        links.append(
            {
                key: event.get(key)
                for key in (
                    "event_id",
                    "parent_event_id",
                    "artifact_id",
                    "relationship",
                    "created_at",
                )
                if event.get(key) is not None
            }
        )
        visible_ids.add(attachment_id)
        if relationship == "input":
            input_ids.add(attachment_id)

    manifest_entries: list[dict[str, Any]] = []
    archive_files: list[tuple[str, bytes]] = []
    for attachment_id in sorted(visible_ids):
        uploaded_manifest = uploaded.get(attachment_id)
        if uploaded_manifest is not None:
            archive_path = None
            if attachment_id in input_ids and attachment_store is not None:
                try:
                    stored = attachment_store.require(
                        thread_id,
                        attachment_id,
                    )
                    if stored.get("status") == "available":
                        filename = Path(
                            str(stored.get("filename") or attachment_id)
                        ).name
                        archive_path = (
                            f"attachments/{attachment_id}/{filename}"
                        )
                        archive_files.append(
                            (
                                archive_path,
                                attachment_store.read_bytes(
                                    thread_id,
                                    attachment_id,
                                ),
                            )
                        )
                except AttachmentError:
                    archive_path = None
            manifest_entries.append(
                _public_attachment_manifest(
                    uploaded_manifest,
                    archive_path=archive_path,
                )
            )
            continue
        if attachment_id in files:
            record = files[attachment_id]
            kind = str(record.get("kind") or "artifact")
            mime_value = record.get("mime")
            mime = mime_value if isinstance(mime_value, str) else None
            archive_path = (
                f"artifacts/{attachment_id}"
                f"{_artifact_extension(kind, mime)}"
            )
            manifest_entries.append(
                _public_attachment_manifest(
                    {"id": attachment_id, **record},
                    archive_path=archive_path,
                )
            )
            continue
        if attachment_id in datasets:
            record = datasets[attachment_id]
            manifest_entries.append(
                _public_attachment_manifest(
                    {"id": attachment_id, **record},
                    archive_path=f"datasets/{attachment_id}.csv",
                )
            )

    return {
        "attachments": manifest_entries,
        "links": links,
    }, archive_files


def _dataset_manifest_and_files(
    state: dict,
    *,
    runtime_root: str | Path | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, bytes]]]:
    artifacts = dict(state.get("artifacts") or {})
    datasets = dict(artifacts.get("datasets") or {})
    output_ids = _linked_output_ids(state)
    manifest: list[dict[str, Any]] = []
    archive_files: list[tuple[str, bytes]] = []
    for dataset_id, raw_record in datasets.items():
        if (
            dataset_id not in output_ids
            or not isinstance(raw_record, dict)
            or raw_record.get("status") != "active"
        ):
            continue
        try:
            dataframe, schema = load_dataset_artifact(
                raw_record,
                runtime_root=runtime_root,
            )
        except (FileNotFoundError, OSError, KeyError):
            continue
        csv_path = f"datasets/{dataset_id}.csv"
        schema_path = f"datasets/{dataset_id}.schema.json"
        manifest.append(
            _public_attachment_manifest(
                {"id": dataset_id, **raw_record},
                archive_path=csv_path,
            )
        )
        archive_files.extend(
            [
                (
                    csv_path,
                    dataframe.to_csv(index=False).encode("utf-8"),
                ),
                (
                    schema_path,
                    json.dumps(
                        schema,
                        indent=2,
                        ensure_ascii=False,
                    ).encode("utf-8"),
                ),
            ]
        )
    return manifest, archive_files


def build_thread_export(
    thread_id,
    provider,
    model_name,
    state,
    *,
    attachment_store: LocalAttachmentStore | None = None,
    attachment_thread_id: str | None = None,
):
    state = dict(state or {})
    display_messages = build_display_history(state)
    runtime_messages = serialize_messages(list(state.get("messages", [])))
    display_conversation = serialize_messages(display_messages)
    conversation_events = get_conversation_events(state)
    artifacts_manifest, artifact_files = _artifact_manifest_and_files(state)
    attachment_manifest, attachment_files = _visible_attachment_graph(
        attachment_thread_id or thread_id,
        state,
        attachment_store,
    )
    datasets_manifest, dataset_files = _dataset_manifest_and_files(
        state,
        runtime_root=(
            attachment_store.runtime_root
            if attachment_store is not None
            else None
        ),
    )
    output = dict(state.get("output") or {})
    export_payload = {
        "thread_id": thread_id,
        "provider": provider,
        "model_name": model_name,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "conversation": display_conversation,
        "conversation_events": conversation_events,
        "runtime_messages": runtime_messages,
        "artifacts": artifacts_manifest,
        "attachments": attachment_manifest,
        "datasets": datasets_manifest,
        "output": {
            "text": output.get("text", ""),
            "generated_code": output.get("generated_code", ""),
            "has_figure_png": bool(output.get("figure_artifact_id")),
        },
    }

    transcript_lines = []
    for entry in display_conversation:
        role = entry["role"].upper()
        transcript_lines.append(f"## {role}\n\n{entry['content']}\n")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("conversation.json", json.dumps(export_payload, indent=2, ensure_ascii=False))
        archive.writestr("conversation.md", "\n".join(transcript_lines).strip() + "\n")
        archive.writestr("artifacts.json", json.dumps(artifacts_manifest, indent=2, ensure_ascii=False))
        archive.writestr(
            "attachments/manifest.json",
            json.dumps(attachment_manifest, indent=2, ensure_ascii=False),
        )
        for filename, data in [
            *attachment_files,
            *artifact_files,
            *dataset_files,
        ]:
            archive.writestr(filename, data)

    return zip_buffer.getvalue()
