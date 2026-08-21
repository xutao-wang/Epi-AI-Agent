from __future__ import annotations

from typing import Any

from llm_vllm import build_openai_llm


def build_db_rag_openai_llm(
    model: str | None,
    *,
    api_key: str,
) -> Any | None:
    resolved = str(model or "").strip()
    if not resolved:
        return None
    return build_openai_llm(model_name=resolved, api_key=api_key)
