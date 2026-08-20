from __future__ import annotations

from copy import deepcopy

from .conversation_events import ensure_conversation_state
from .conversation_schema import JsonObject


def get_artifacts(state: dict) -> dict:
    validated_state = ensure_conversation_state(state)
    artifacts = dict(validated_state.get("artifacts") or {})
    output = dict(state.get("output") or {})
    artifacts.setdefault("generated_code", output.get("generated_code"))
    artifacts.setdefault("execution_output", output.get("text"))
    artifacts.setdefault("error", output.get("error"))
    return artifacts


def get_conversation_events(state: dict) -> list[JsonObject]:
    validated_state = ensure_conversation_state(state)
    artifacts = dict(validated_state.get("artifacts") or {})
    return deepcopy(list(artifacts.get("conversation_events") or []))


def get_artifact_files(state: dict) -> dict[str, JsonObject]:
    validated_state = ensure_conversation_state(state)
    artifacts = dict(validated_state.get("artifacts") or {})
    return deepcopy(dict(artifacts.get("files") or {}))
