from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import llm_vllm
from api.schemas import ModelOption
from run_fastapi import configure_and_verify_providers
from utils.model_runtime_profiles import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_BASE_URL,
    load_custom_model_profiles,
    reload_custom_model_profiles,
)
from utils.provider_startup import verify_openrouter_credentials


ROOT = Path(__file__).parents[1]


def _write_registry(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "id": "openrouter:test-model",
                    "provider": "openrouter",
                    "label": "Test model",
                    "model": "vendor/test-model",
                    "reasoning_effort": "medium",
                    "supports_sampling_controls": False,
                }
            ]
        ),
        encoding="utf-8",
    )


def test_openrouter_profile_defaults_and_api_schema(tmp_path: Path) -> None:
    registry = tmp_path / "custom_models.json"
    _write_registry(registry)

    profile = load_custom_model_profiles(registry)["openrouter:test-model"]

    assert profile.provider == "openrouter"
    assert profile.base_url == OPENROUTER_BASE_URL
    assert profile.api_key_env == OPENROUTER_API_KEY_ENV
    assert profile.api_key_required is True
    assert profile.served_model_id == "vendor/test-model"
    assert profile.reasoning is not None
    assert profile.reasoning.effort == "medium"
    assert ModelOption(**profile.descriptor()).provider == "openrouter"


def test_committed_example_registry_loads_openrouter_profile() -> None:
    profiles = load_custom_model_profiles(
        ROOT / "config" / "custom_models.example.json"
    )

    profile = profiles["openrouter:claude-sonnet-4.5"]
    assert profile.provider == "openrouter"
    assert profile.base_url == OPENROUTER_BASE_URL
    assert profile.api_key_env == OPENROUTER_API_KEY_ENV
    assert profile.served_model_id == "anthropic/claude-sonnet-4.5"


def test_openrouter_configuration_exposes_registered_model(tmp_path: Path) -> None:
    registry = tmp_path / "custom_models.json"
    _write_registry(registry)
    environ = {
        "REPORT_AGENT_CUSTOM_MODELS_PATH": str(registry),
        OPENROUTER_API_KEY_ENV: "test-key",
    }
    calls: list[tuple[str, str, str | None]] = []

    availability = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        verifier=lambda provider, key, *, base_url=None: calls.append(
            (provider, key, base_url)
        ),
        input_fn=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("provider menu should not open")
        ),
        getpass_fn=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("key prompt should not open")
        ),
        persist=lambda _root, _values: None,
        output_fn=lambda _message: None,
    )

    assert calls == [("openrouter", "test-key", OPENROUTER_BASE_URL)]
    assert availability.available_model_ids == ("openrouter:test-model",)
    assert availability.default_model_id == "openrouter:test-model"


def test_openrouter_llm_uses_chat_completions_without_attribution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = tmp_path / "custom_models.json"
    _write_registry(registry)
    monkeypatch.setenv("REPORT_AGENT_CUSTOM_MODELS_PATH", str(registry))
    reload_custom_model_profiles()
    captured: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(llm_vllm, "ChatOpenAI", FakeChatOpenAI)
    llm_vllm.build_chat_llm(
        model_name="openrouter:test-model",
        api_key="test-key",
        temperature=0.2,
        top_p=0.8,
    )

    assert captured["model"] == "vendor/test-model"
    assert captured["base_url"] == OPENROUTER_BASE_URL
    assert captured["max_retries"] == 0
    assert captured["extra_body"] == {"reasoning": {"effort": "medium"}}
    assert "temperature" not in captured
    assert "top_p" not in captured
    assert "default_headers" not in captured


def test_openrouter_credential_check_uses_authenticated_key_endpoint() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def request(url: str, **kwargs: object) -> SimpleNamespace:
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200)

    verify_openrouter_credentials(" test-key ", request_fn=request)

    assert calls == [
        (
            f"{OPENROUTER_BASE_URL}/key",
            {
                "headers": {"Authorization": "Bearer test-key"},
                "timeout": 10.0,
            },
        )
    ]
