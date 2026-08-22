from __future__ import annotations

from langchain_core.messages import AIMessage

from scripts import smoke_model_reasoning_matrix as smoke_matrix
from scripts.smoke_model_reasoning_matrix import run_smoke
from utils.model_runtime_profiles import MODEL_RUNTIME_PROFILES


class _FakeModel:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def invoke(self, messages, **kwargs):
        assert messages and kwargs
        return AIMessage(
            content=f"reasoning-matrix-ok:{self.model_id}",
            response_metadata={"model": f"provider/{self.model_id}"},
            usage_metadata={
                "input_tokens": 4,
                "output_tokens": 2,
                "total_tokens": 6,
            },
        )


def test_checks_every_available_builtin_once_without_secrets(capsys) -> None:
    calls: list[str] = []

    def builder(*, model_name: str, api_key: str):
        assert api_key in {"openai-secret", "anthropic-secret"}
        calls.append(model_name)
        return _FakeModel(model_name)

    result = run_smoke(
        {
            "OPENAI_API_KEY": "openai-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
        },
        llm_builder=builder,
    )
    output = capsys.readouterr().out
    assert result == 0
    assert calls == list(MODEL_RUNTIME_PROFILES)
    assert len(calls) == len(set(calls)) == 7
    assert "Claude Opus 5 (Medium)" in output
    assert "Claude Haiku 4.5 (Standard)" in output
    assert "response_model=provider/claude-haiku-4-5" in output
    assert "secret" not in output


def test_records_failure_once_and_continues(capsys) -> None:
    calls: list[str] = []

    def builder(*, model_name: str, api_key: str):
        calls.append(model_name)
        if model_name == "claude-sonnet-5":
            raise RuntimeError("provider rejected model")
        return _FakeModel(model_name)

    result = run_smoke(
        {"ANTHROPIC_API_KEY": "anthropic-secret"},
        llm_builder=builder,
    )
    output = capsys.readouterr().out
    assert result == 1
    assert calls == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ]
    assert calls.count("claude-sonnet-5") == 1
    assert "RUN_FAILED" in output


def test_global_timeout_stops_before_invoking_another_model(capsys) -> None:
    calls: list[str] = []

    class _TimedOutModel:
        def invoke(self, messages, **kwargs):
            raise smoke_matrix._MatrixDeadlineExceeded(
                "matrix deadline reached"
            )

    def builder(*, model_name: str, api_key: str):
        calls.append(model_name)
        return _TimedOutModel()

    result = run_smoke(
        {"OPENAI_API_KEY": "openai-secret"},
        llm_builder=builder,
    )

    assert result == 1
    assert calls == ["gpt-5.4"]
    assert "MODEL_MATRIX_TIMEOUT" in capsys.readouterr().out


def test_model_local_timeout_records_failure_and_continues(capsys) -> None:
    calls: list[str] = []

    class _Model:
        def __init__(self, model_id: str) -> None:
            self.model_id = model_id

        def invoke(self, messages, **kwargs):
            if self.model_id == "gpt-5.4":
                raise TimeoutError("socket timed out")
            return _FakeModel(self.model_id).invoke(messages, **kwargs)

    def builder(*, model_name: str, api_key: str):
        calls.append(model_name)
        return _Model(model_name)

    result = run_smoke(
        {"OPENAI_API_KEY": "openai-secret"},
        llm_builder=builder,
    )

    assert result == 1
    assert calls == [
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]
    assert "RUN_FAILED" in capsys.readouterr().out
