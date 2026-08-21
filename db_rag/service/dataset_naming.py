from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from db_rag.config import resolve_db_rag_dataset_naming_model
from db_rag.generation import parse_json_object
from utils.llm_response import coerce_text_content

_LOGGER = logging.getLogger(__name__)

_DATASET_NAME_MAX_CHARS = 90
_DATASET_NAME_MAX_WORDS = 8


def _compact_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _headline(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", value)
    normalized_words: list[str] = []
    for word in words:
        for part in re.sub(r"[-–—]", " ", word).split():
            normalized_words.append(
                part if part.isupper() else part[:1].upper() + part[1:]
            )
    return " ".join(normalized_words)


def _summarize_request(value: Any) -> str:
    text = _compact_text(value).strip("\"'` .")
    if not text:
        return ""

    dataset_request = re.search(
        r"\b(?:create|build|generate|make|produce|prepare)\s+(?:(?:a|an|the)\s+)?(.+?)\s+(?:data\s*set|dataset)\b",
        text,
        flags=re.IGNORECASE,
    )
    if dataset_request:
        subject = _headline(dataset_request.group(1))
        if subject:
            return _sanitize_dataset_name(f"{subject} Dataset")

    # Remove common request boilerplate before using a short phrase as a
    # deterministic fallback when the lightweight naming model is unavailable.
    concise = re.sub(
        r"^(?:please\s+)?(?:query\s+(?:my\s+)?database,?\s*|help\s+me\s+(?:to\s+)?|show\s+me\s+|find\s+|retrieve\s+|select\s+|extract\s+|subset\s+|create\s+|build\s+|generate\s+|make\s+)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    concise = re.sub(r"^(?:a|an|the)\s+", "", concise, flags=re.IGNORECASE)
    if re.search(r"\b(?:data\s*set|dataset)$", concise, flags=re.IGNORECASE):
        return _sanitize_dataset_name(_headline(concise))
    words = concise.split()[: _DATASET_NAME_MAX_WORDS - 1]
    if not words:
        return ""
    return _sanitize_dataset_name(f"{_headline(' '.join(words))} Dataset")


def _fallback_name(*, goal_text: Any = "", source_question: Any = "", columns: list[Any] | None = None) -> str:
    for value in (goal_text, source_question):
        text = _summarize_request(value)
        if text:
            return text
    for column in list(columns or []):
        if not isinstance(column, dict):
            continue
        text = _sanitize_dataset_name(column.get("description") or column.get("column"))
        if text:
            return text
    return ""


def deterministic_dataset_name(
    *,
    goal_text: str,
    source_question: str = "",
    columns: list[dict[str, Any]] | None = None,
) -> str:
    return _fallback_name(
        goal_text=goal_text,
        source_question=source_question,
        columns=list(columns or []),
    )


def _sanitize_dataset_name(value: Any) -> str:
    name = _compact_text(value).strip("\"'` ")
    if not name:
        return ""
    if len(name) <= _DATASET_NAME_MAX_CHARS:
        return name
    cutoff = _DATASET_NAME_MAX_CHARS - 3
    candidate = name[:cutoff].rsplit(" ", 1)[0].strip()
    return f"{candidate or name[:cutoff].strip()}..."


def _concise_model_name(value: Any) -> str:
    name = _sanitize_dataset_name(value).rstrip(".").strip()
    if not name or len(name.split()) > _DATASET_NAME_MAX_WORDS:
        return ""
    return name


def _resolve_openai_client() -> Any | None:
    try:
        from openai import OpenAI as client_cls
    except ModuleNotFoundError:
        return None
    return client_cls


def generate_dataset_name(
    *,
    goal_text: str,
    source_question: str = "",
    columns: list[dict[str, Any]] | None = None,
    api_key: str | None = None,
    resolve_model=resolve_db_rag_dataset_naming_model,
) -> str:
    fallback = _fallback_name(
        goal_text=goal_text,
        source_question=source_question,
        columns=list(columns or []),
    )
    model = resolve_model()
    if not model:
        return fallback

    resolved_api_key = str(api_key or "").strip()
    if not resolved_api_key:
        return fallback

    openai_client = _resolve_openai_client()
    if openai_client is None:
        return fallback

    selected_columns = []
    for column in list(columns or [])[:12]:
        if not isinstance(column, dict):
            continue
        selected_columns.append(
            {
                "table": str(column.get("table") or "").strip(),
                "column": str(column.get("column") or "").strip(),
                "description": str(column.get("description") or "").strip(),
            }
        )

    client_kwargs: dict[str, Any] = {"api_key": resolved_api_key}
    base_url = str(os.getenv("DB_RAG_DATASET_NAMING_BASE_URL", "") or "").strip()
    if base_url:
        client_kwargs["base_url"] = base_url

    try:
        client = openai_client(**client_kwargs)
        response = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Generate a concise human-readable name for a saved dataset. "
                        "Return only JSON with key name. The name should be 2 to 8 words, "
                        "specific to the request, and not include the dataset id, SQL, quotes, "
                        "or generic filler such as 'saved dataset'."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "goal": goal_text,
                            "source_question": source_question,
                            "selected_columns": selected_columns,
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                },
            ],
        )
        content = coerce_text_content(getattr(response.choices[0].message, "content", ""))
        parsed = parse_json_object(content) or {}
    except Exception as error:
        _LOGGER.warning(
            "Dataset naming model call failed; using heuristic name: %s",
            error,
        )
        return fallback

    return _concise_model_name(parsed.get("name")) or fallback
