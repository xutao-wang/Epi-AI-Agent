from __future__ import annotations

import sys
from types import SimpleNamespace

from db_rag.vectorstore import OpenAIEmbeddingFunction


def test_openai_embedding_function_uses_profile_connection_fields(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    embedding = OpenAIEmbeddingFunction(
        "OpenAI/custom-index-model",
        provider_model="provider-model",
        api_key="secret-key",
        base_url="https://embedding.example/v1",
        timeout_seconds=7,
    )

    assert embedding.config_model == "OpenAI/custom-index-model"
    assert embedding.model == "provider-model"
    assert captured == {
        "api_key": "secret-key",
        "base_url": "https://embedding.example/v1",
        "max_retries": 0,
        "timeout": 7,
    }
