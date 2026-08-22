from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import llm_vllm
import pytest
from db_rag import vectorstore
from db_rag.service import model_routing


def _capture_chat_openai(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(llm_vllm, "ChatOpenAI", FakeChatOpenAI)
    return captured


def _capture_chat_anthropic(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeChatAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "langchain_anthropic",
        SimpleNamespace(ChatAnthropic=FakeChatAnthropic),
    )
    return captured


def test_build_openai_llm_passes_key_without_mutating_environment(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured = _capture_chat_openai(monkeypatch)
    llm_vllm.build_openai_llm(model_name="gpt-test", api_key="session-key")

    assert str(captured["api_key"]) == "**********"
    assert captured["api_key"].get_secret_value() == "session-key"
    assert "OPENAI_API_KEY" not in os.environ


def test_gpt56_omits_unsupported_sampling_kwargs(monkeypatch) -> None:
    captured = _capture_chat_openai(monkeypatch)
    llm_vllm.build_openai_llm(
        model_name="gpt-5.6-sol",
        api_key="session-key",
        temperature=0.2,
        top_p=0.8,
    )
    assert "temperature" not in captured
    assert "top_p" not in captured


def test_gpt54_preserves_supported_sampling_kwargs(monkeypatch) -> None:
    captured = _capture_chat_openai(monkeypatch)
    llm_vllm.build_openai_llm(
        model_name="gpt-5.4",
        api_key="session-key",
        temperature=0.2,
        top_p=0.8,
    )
    assert captured["temperature"] == 0.2
    assert captured["top_p"] == 0.8


@pytest.mark.parametrize(
    ("model_id", "effort"),
    [
        ("gpt-5.4", None),
        ("gpt-5.6-luna", "low"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-sol", "medium"),
    ],
)
def test_every_openai_model_sends_configured_reasoning(
    monkeypatch,
    model_id: str,
    effort: str | None,
) -> None:
    captured = _capture_chat_openai(monkeypatch)
    llm_vllm.build_chat_llm(model_name=model_id, api_key="session-key")
    assert captured.get("reasoning_effort") == effort


@pytest.mark.parametrize(
    ("model_id", "thinking", "effort"),
    [
        ("claude-opus-5", {"type": "adaptive"}, "medium"),
        ("claude-sonnet-5", {"type": "adaptive"}, "medium"),
        ("claude-haiku-4-5", None, None),
    ],
)
def test_every_anthropic_model_sends_configured_reasoning(
    monkeypatch,
    model_id: str,
    thinking: dict[str, str] | None,
    effort: str | None,
) -> None:
    captured = _capture_chat_anthropic(monkeypatch)
    llm_vllm.build_chat_llm(model_name=model_id, api_key="session-key")
    assert captured.get("thinking") == thinking
    assert captured.get("effort") == effort


def test_db_rag_model_factory_requires_and_forwards_explicit_key(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        model_routing,
        "build_openai_llm",
        lambda **kwargs: captured.update(kwargs) or "model",
    )

    assert model_routing.build_db_rag_openai_llm(
        "gpt-test",
        api_key="session-key",
    ) == "model"
    assert captured == {"model_name": "gpt-test", "api_key": "session-key"}


def test_embedding_client_receives_explicit_key(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    vectorstore.OpenAIEmbeddingFunction(
        model="OpenAI/text-embedding-3-large",
        api_key="session-key",
    )

    assert captured["api_key"] == "session-key"
