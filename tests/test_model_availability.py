from __future__ import annotations

import json

import pytest

from utils.model_availability import (
    ProviderEndpoint,
    build_model_availability,
    model_availability_from_configured_credentials,
)


def _endpoint(
    provider: str,
    api_key_env: str,
    base_url: str | None = None,
) -> ProviderEndpoint:
    return ProviderEndpoint(provider, api_key_env, base_url)


def test_openai_only_exposes_every_registered_gpt_model() -> None:
    catalog = build_model_availability(
        {"OPENAI_API_KEY": "verified"},
        {_endpoint("openai", "OPENAI_API_KEY")},
    )

    assert catalog.available_model_ids == (
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    )
    assert catalog.default_model_id == "gpt-5.6-terra"
    assert catalog.title_model_id == "gpt-5.6-luna"


def test_anthropic_only_exposes_every_registered_claude_model() -> None:
    catalog = build_model_availability(
        {"ANTHROPIC_API_KEY": "verified"},
        {_endpoint("anthropic", "ANTHROPIC_API_KEY")},
    )

    assert catalog.available_model_ids == (
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    )
    assert catalog.default_model_id == "claude-opus-5"
    assert catalog.title_model_id == "claude-haiku-4-5"


def test_both_verified_providers_expose_both_families() -> None:
    catalog = build_model_availability(
        {
            "OPENAI_API_KEY": "openai",
            "ANTHROPIC_API_KEY": "anthropic",
        },
        {
            _endpoint("openai", "OPENAI_API_KEY"),
            _endpoint("anthropic", "ANTHROPIC_API_KEY"),
        },
    )

    assert set(catalog.available_model_ids) == {
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    }
    assert catalog.default_model_id == "gpt-5.6-terra"


def test_compatible_model_remains_registered_when_endpoint_fails(
    tmp_path,
) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        json.dumps(
            [
                {
                    "id": "cluster-model",
                    "base_url": "https://llm.internal/v1",
                    "api_key_env": "CLUSTER_LLM_KEY",
                }
            ]
        ),
        encoding="utf-8",
    )
    environ = {
        "REPORT_AGENT_CUSTOM_MODELS_PATH": str(registry),
        "CLUSTER_LLM_KEY": "configured",
    }

    with pytest.raises(ValueError, match="No verified AI model provider"):
        build_model_availability(environ, set())

    openai = _endpoint("openai", "OPENAI_API_KEY")
    catalog = build_model_availability(
        environ | {"OPENAI_API_KEY": "verified"},
        {openai},
    )
    assert "cluster-model" in catalog.registered_profiles
    assert "cluster-model" not in catalog.available_model_ids


def test_verified_compatible_model_is_available_without_builtin_provider(
    tmp_path,
) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        json.dumps(
            [
                {
                    "id": "cluster-model",
                    "base_url": "https://llm.internal/v1",
                    "api_key_env": "CLUSTER_LLM_KEY",
                }
            ]
        ),
        encoding="utf-8",
    )
    environ = {
        "REPORT_AGENT_CUSTOM_MODELS_PATH": str(registry),
        "CLUSTER_LLM_KEY": "configured",
    }

    catalog = build_model_availability(
        environ,
        {
            _endpoint(
                "openai_compatible",
                "CLUSTER_LLM_KEY",
                "https://llm.internal/v1",
            )
        },
    )

    assert catalog.available_model_ids == ("cluster-model",)
    assert catalog.default_model_id == "cluster-model"
    assert catalog.title_model_id == "cluster-model"


def test_credential_fallback_ignores_deprecated_model_allowlist() -> None:
    catalog = model_availability_from_configured_credentials(
        {
            "OPENAI_API_KEY": "configured",
            "REPORT_AGENT_MODEL": "claude-opus-5",
            "REPORT_AGENT_ALLOWED_MODELS": "claude-opus-5",
        }
    )

    assert catalog.default_model_id == "gpt-5.6-terra"
    assert all(model.startswith("gpt-") for model in catalog.available_model_ids)
