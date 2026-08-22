from __future__ import annotations

import json
from dataclasses import replace

import pytest

from utils.model_runtime_profiles import (
    MODEL_RUNTIME_PROFILES,
    ReasoningConfig,
    load_custom_model_profiles,
    model_runtime_profile,
)


@pytest.mark.parametrize(
    ("model_id", "mode", "effort", "label"),
    [
        ("gpt-5.4", None, None, "gpt-5.4 (Standard)"),
        ("gpt-5.6-luna", None, "low", "gpt-5.6-luna (Low)"),
        ("gpt-5.6-terra", None, "medium", "gpt-5.6-terra (Medium)"),
        ("gpt-5.6-sol", None, "medium", "gpt-5.6-sol (Medium)"),
        ("claude-opus-5", "adaptive", "medium", "Claude Opus 5 (Medium)"),
        ("claude-sonnet-5", "adaptive", "medium", "Claude Sonnet 5 (Medium)"),
        ("claude-haiku-4-5", None, None, "Claude Haiku 4.5 (Standard)"),
    ],
)
def test_every_builtin_declares_consumed_reasoning_and_label(
    model_id: str,
    mode: str | None,
    effort: str | None,
    label: str,
) -> None:
    assert len(MODEL_RUNTIME_PROFILES) == 7
    profile = model_runtime_profile(model_id)
    assert getattr(profile.reasoning, "mode", None) == mode
    assert getattr(profile.reasoning, "effort", None) == effort
    assert profile.label == label
    assert profile.descriptor()["label"] == label
    assert "reasoning_tier" not in profile.descriptor()


def test_openai_rejects_anthropic_reasoning_mode() -> None:
    profile = model_runtime_profile("gpt-5.6-terra")
    with pytest.raises(ValueError, match="gpt-5.6-terra.*mode"):
        replace(
            profile,
            reasoning=ReasoningConfig(mode="adaptive", effort="medium"),
        )


def test_reasoning_config_rejects_unknown_effort_at_runtime() -> None:
    with pytest.raises(ValueError, match="reasoning effort"):
        ReasoningConfig(effort="bogus")  # type: ignore[arg-type]


def test_compatible_provider_rejects_reasoning() -> None:
    profile = model_runtime_profile("gpt-5.6-terra")
    with pytest.raises(ValueError, match="reasoning.*openai_compatible"):
        replace(profile, provider="openai_compatible")


def test_custom_model_derives_standard_label(tmp_path) -> None:
    path = tmp_path / "custom_models.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "cluster-model",
                    "label": "Cluster Model",
                    "base_url": "https://llm.internal/v1",
                }
            ]
        ),
        encoding="utf-8",
    )
    profile = load_custom_model_profiles(path)["cluster-model"]
    assert profile.reasoning is None
    assert profile.label == "Cluster Model (Standard)"


def test_custom_model_rejects_removed_reasoning_tier(tmp_path) -> None:
    path = tmp_path / "custom_models.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "cluster-model",
                    "base_url": "https://llm.internal/v1",
                    "reasoning_tier": "standard",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reasoning_tier"):
        load_custom_model_profiles(path)
