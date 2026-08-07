"""Immutable runtime and presentation profiles for allowed application models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Literal


ReasoningTier = Literal["standard", "low", "medium", "high"]
ReasoningEffort = Literal["low", "medium", "high"]


def _cost_display(tokens: int, usd_per_million: Decimal) -> str:
    cents = (
        Decimal(tokens)
        * usd_per_million
        / Decimal(1_000_000)
        * Decimal(100)
    ).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return f"${cents / Decimal(100):.2f}"


@dataclass(frozen=True)
class ModelRuntimeProfile:
    model_id: str
    label: str
    reasoning_tier: ReasoningTier
    reasoning_effort: ReasoningEffort | None
    supports_sampling_controls: bool
    summary: str
    initial_output_tokens: int
    automatic_output_token_ceiling: int
    user_output_token_increment: int
    absolute_output_token_ceiling: int
    request_timeout_seconds: int
    workflow_timeout_seconds: int
    output_usd_per_million: Decimal

    @property
    def automatic_output_cost_display(self) -> str:
        return _cost_display(
            self.automatic_output_token_ceiling,
            self.output_usd_per_million,
        )

    @property
    def incremental_output_cost_display(self) -> str:
        return _cost_display(
            self.user_output_token_increment,
            self.output_usd_per_million,
        )

    def descriptor(self) -> dict[str, object]:
        return {
            "id": self.model_id,
            "label": self.label,
            "reasoning_tier": self.reasoning_tier,
            "supports_sampling_controls": self.supports_sampling_controls,
            "summary": self.summary,
            "initial_output_tokens": self.initial_output_tokens,
            "automatic_output_token_ceiling": (
                self.automatic_output_token_ceiling
            ),
            "user_output_token_increment": self.user_output_token_increment,
            "absolute_output_token_ceiling": (
                self.absolute_output_token_ceiling
            ),
            "request_timeout_seconds": self.request_timeout_seconds,
            "workflow_timeout_seconds": self.workflow_timeout_seconds,
            "automatic_output_cost": self.automatic_output_cost_display,
            "incremental_output_cost": self.incremental_output_cost_display,
        }


MODEL_RUNTIME_PROFILES = {
    "gpt-5.4": ModelRuntimeProfile(
        model_id="gpt-5.4",
        label="gpt-5.4 (Standard)",
        reasoning_tier="standard",
        reasoning_effort=None,
        supports_sampling_controls=True,
        summary="Reliable general-purpose default.",
        initial_output_tokens=8_192,
        automatic_output_token_ceiling=16_384,
        user_output_token_increment=8_192,
        absolute_output_token_ceiling=24_576,
        request_timeout_seconds=120,
        workflow_timeout_seconds=300,
        output_usd_per_million=Decimal("15"),
    ),
    "gpt-5.6-luna": ModelRuntimeProfile(
        model_id="gpt-5.6-luna",
        label="gpt-5.6-luna (Low)",
        reasoning_tier="low",
        reasoning_effort="low",
        supports_sampling_controls=False,
        summary=(
            "Fastest and lowest-cost tier for straightforward work."
        ),
        initial_output_tokens=8_192,
        automatic_output_token_ceiling=16_384,
        user_output_token_increment=8_192,
        absolute_output_token_ceiling=24_576,
        request_timeout_seconds=120,
        workflow_timeout_seconds=300,
        output_usd_per_million=Decimal("1.20"),
    ),
    "gpt-5.6-terra": ModelRuntimeProfile(
        model_id="gpt-5.6-terra",
        label="gpt-5.6-terra (Medium)",
        reasoning_tier="medium",
        reasoning_effort="medium",
        supports_sampling_controls=False,
        summary="Balanced tier for moderately complex analysis.",
        initial_output_tokens=16_384,
        automatic_output_token_ceiling=32_768,
        user_output_token_increment=16_384,
        absolute_output_token_ceiling=49_152,
        request_timeout_seconds=180,
        workflow_timeout_seconds=420,
        output_usd_per_million=Decimal("12"),
    ),
    "gpt-5.6-sol": ModelRuntimeProfile(
        model_id="gpt-5.6-sol",
        label="gpt-5.6-sol (Medium)",
        reasoning_tier="medium",
        reasoning_effort="medium",
        supports_sampling_controls=False,
        summary="Frontier-capability tier with balanced reasoning.",
        initial_output_tokens=25_000,
        automatic_output_token_ceiling=50_000,
        user_output_token_increment=25_000,
        absolute_output_token_ceiling=75_000,
        request_timeout_seconds=240,
        workflow_timeout_seconds=600,
        output_usd_per_million=Decimal("30"),
    ),
}


INTERNAL_MODEL_RUNTIME_PROFILES = {
    "gpt5.6-Luna-Light": ModelRuntimeProfile(
        model_id="gpt5.6-Luna-Light",
        label="gpt5.6-Luna-Light (Low)",
        reasoning_tier="low",
        reasoning_effort="low",
        supports_sampling_controls=False,
        summary="Lightweight model for automatic titles and dataset names.",
        initial_output_tokens=8_192,
        automatic_output_token_ceiling=16_384,
        user_output_token_increment=8_192,
        absolute_output_token_ceiling=24_576,
        request_timeout_seconds=120,
        workflow_timeout_seconds=300,
        output_usd_per_million=Decimal("1.20"),
    ),
}


def model_runtime_profile(model_id: str) -> ModelRuntimeProfile:
    normalized = str(model_id or "").strip()
    try:
        return MODEL_RUNTIME_PROFILES[normalized]
    except KeyError:
        try:
            return INTERNAL_MODEL_RUNTIME_PROFILES[normalized]
        except KeyError as exc:
            choices = ", ".join(MODEL_RUNTIME_PROFILES)
            raise ValueError(
                f"{normalized or 'Blank model'} is not an allowed application "
                f"model; choose one of: {choices}"
            ) from exc


def configured_model_profiles(
    environ: Mapping[str, str],
) -> tuple[ModelRuntimeProfile, ...]:
    configured = str(
        environ.get("REPORT_AGENT_ALLOWED_MODELS", "")
    ).strip()
    model_ids = tuple(
        dict.fromkeys(
            model_id.strip()
            for model_id in configured.split(",")
            if model_id.strip()
        )
    )
    if not model_ids:
        raise ValueError(
            "REPORT_AGENT_ALLOWED_MODELS must list at least one model"
        )
    return tuple(model_runtime_profile(model_id) for model_id in model_ids)


__all__ = [
    "MODEL_RUNTIME_PROFILES",
    "ModelRuntimeProfile",
    "configured_model_profiles",
    "model_runtime_profile",
]
