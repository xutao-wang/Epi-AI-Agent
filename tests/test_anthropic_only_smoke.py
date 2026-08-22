from __future__ import annotations

import os

from langchain_core.messages import AIMessage

from scripts.smoke_anthropic_only import run_smoke


class _FakeAnthropicModel:
    def invoke(self, messages, **kwargs):
        assert messages
        assert kwargs["max_tokens"] == 32
        return AIMessage(content="anthropic-only-smoke-ok")


def test_smoke_removes_embedding_credentials_and_sanitizes_output(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    observed: dict[str, object] = {}

    def builder(**kwargs):
        observed.update(kwargs)
        assert "OPENAI_API_KEY" not in os.environ
        assert "OPENROUTER_API_KEY" not in os.environ
        return _FakeAnthropicModel()

    result = run_smoke(
        model_name="claude-haiku-4-5",
        anthropic_api_key="anthropic-secret",
        llm_builder=builder,
    )

    output = capsys.readouterr().out
    assert result == 0
    assert observed == {
        "model_name": "claude-haiku-4-5",
        "api_key": "anthropic-secret",
    }
    assert "anthropic-only smoke passed" in output
    assert "claude-haiku-4-5" in output
    assert "secret" not in output
    assert os.environ["OPENAI_API_KEY"] == "openai-secret"
    assert os.environ["OPENROUTER_API_KEY"] == "openrouter-secret"
