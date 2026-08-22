"""Immutable runtime and presentation profiles for allowed application models."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


PROJECT_ROOT = Path(__file__).resolve().parents[1]

Provider = Literal["openai", "anthropic", "openai_compatible"]
ReasoningMode = Literal["adaptive"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]

PROVIDER_OPENAI: Provider = "openai"
PROVIDER_ANTHROPIC: Provider = "anthropic"
PROVIDER_OPENAI_COMPATIBLE: Provider = "openai_compatible"

PROVIDER_LABELS: dict[str, str] = {
    PROVIDER_OPENAI: "OpenAI",
    PROVIDER_ANTHROPIC: "Anthropic",
    PROVIDER_OPENAI_COMPATIBLE: "Custom endpoint",
}

PROVIDER_API_KEY_ENVS: dict[str, str] = {
    PROVIDER_OPENAI: "OPENAI_API_KEY",
    PROVIDER_ANTHROPIC: "ANTHROPIC_API_KEY",
}

CUSTOM_MODELS_PATH_ENV = "REPORT_AGENT_CUSTOM_MODELS_PATH"
DEFAULT_CUSTOM_MODELS_PATH = PROJECT_ROOT / "config" / "custom_models.json"


def _cost_display(tokens: int, usd_per_million: Decimal | None) -> str | None:
    if usd_per_million is None:
        return None
    cents = (
        Decimal(tokens)
        * usd_per_million
        / Decimal(1_000_000)
        * Decimal(100)
    ).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return f"${cents / Decimal(100):.2f}"


@dataclass(frozen=True)
class ReasoningConfig:
    """Provider-consumed reasoning settings for one model."""

    effort: ReasoningEffort
    mode: ReasoningMode | None = None

    def __post_init__(self) -> None:
        if self.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"Unsupported reasoning effort: {self.effort}")
        if self.mode not in {None, "adaptive"}:
            raise ValueError(f"Unsupported reasoning mode: {self.mode}")


@dataclass(frozen=True)
class ModelRuntimeProfile:
    model_id: str
    base_label: str
    reasoning: ReasoningConfig | None
    supports_sampling_controls: bool
    summary: str
    initial_output_tokens: int
    automatic_output_token_ceiling: int
    user_output_token_increment: int
    absolute_output_token_ceiling: int
    request_timeout_seconds: int
    workflow_timeout_seconds: int
    routing_context_char_ceiling: int
    output_usd_per_million: Decimal | None
    provider: Provider = PROVIDER_OPENAI
    api_key_env: str = "OPENAI_API_KEY"
    api_key_required: bool = True
    base_url: str | None = None
    remote_model_id: str | None = None
    supports_vision: bool = True
    supports_mid_conversation_system: bool = True

    def __post_init__(self) -> None:
        if self.provider == PROVIDER_OPENAI:
            if self.reasoning is not None and self.reasoning.mode is not None:
                raise ValueError(
                    f"{self.model_id} reasoning mode is not supported by OpenAI"
                )
            if (
                self.reasoning is not None
                and self.reasoning.effort not in {"low", "medium", "high"}
            ):
                raise ValueError(
                    f"{self.model_id} reasoning effort is not supported by OpenAI"
                )
        elif self.provider == PROVIDER_ANTHROPIC:
            if self.reasoning is not None and self.reasoning.mode != "adaptive":
                raise ValueError(
                    f"{self.model_id} reasoning mode must be adaptive for Anthropic"
                )
        elif self.provider == PROVIDER_OPENAI_COMPATIBLE:
            if self.reasoning is not None:
                raise ValueError(
                    "reasoning is not supported for openai_compatible models"
                )

    @property
    def reasoning_display(self) -> str:
        return "Standard" if self.reasoning is None else self.reasoning.effort.title()

    @property
    def label(self) -> str:
        return f"{self.base_label} ({self.reasoning_display})"

    @property
    def served_model_id(self) -> str:
        """Model name sent to the provider (custom endpoints may differ)."""
        return self.remote_model_id or self.model_id

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)

    def output_budget_kwargs(self, budget: int) -> dict[str, int]:
        """Provider-correct invoke kwarg carrying the output-token budget."""
        if self.provider == PROVIDER_OPENAI:
            return {"max_completion_tokens": budget}
        return {"max_tokens": budget}

    @property
    def automatic_output_cost_display(self) -> str | None:
        return _cost_display(
            self.automatic_output_token_ceiling,
            self.output_usd_per_million,
        )

    @property
    def incremental_output_cost_display(self) -> str | None:
        return _cost_display(
            self.user_output_token_increment,
            self.output_usd_per_million,
        )

    def descriptor(self) -> dict[str, object]:
        return {
            "id": self.model_id,
            "label": self.label,
            "provider": self.provider,
            "provider_label": self.provider_label,
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
        base_label="gpt-5.4",
        reasoning=None,
        supports_sampling_controls=True,
        summary="Reliable general-purpose default.",
        initial_output_tokens=8_192,
        automatic_output_token_ceiling=16_384,
        user_output_token_increment=8_192,
        absolute_output_token_ceiling=24_576,
        request_timeout_seconds=120,
        workflow_timeout_seconds=300,
        routing_context_char_ceiling=262_144,
        output_usd_per_million=Decimal("15"),
    ),
    "gpt-5.6-luna": ModelRuntimeProfile(
        model_id="gpt-5.6-luna",
        base_label="gpt-5.6-luna",
        reasoning=ReasoningConfig(effort="low"),
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
        routing_context_char_ceiling=262_144,
        output_usd_per_million=Decimal("1.20"),
    ),
    "gpt-5.6-terra": ModelRuntimeProfile(
        model_id="gpt-5.6-terra",
        base_label="gpt-5.6-terra",
        reasoning=ReasoningConfig(effort="medium"),
        supports_sampling_controls=False,
        summary="Balanced tier for moderately complex analysis.",
        initial_output_tokens=16_384,
        automatic_output_token_ceiling=32_768,
        user_output_token_increment=16_384,
        absolute_output_token_ceiling=49_152,
        request_timeout_seconds=180,
        workflow_timeout_seconds=420,
        routing_context_char_ceiling=262_144,
        output_usd_per_million=Decimal("12"),
    ),
    "gpt-5.6-sol": ModelRuntimeProfile(
        model_id="gpt-5.6-sol",
        base_label="gpt-5.6-sol",
        reasoning=ReasoningConfig(effort="medium"),
        supports_sampling_controls=False,
        summary="Frontier-capability tier with balanced reasoning.",
        initial_output_tokens=25_000,
        automatic_output_token_ceiling=50_000,
        user_output_token_increment=25_000,
        absolute_output_token_ceiling=75_000,
        request_timeout_seconds=240,
        workflow_timeout_seconds=600,
        routing_context_char_ceiling=262_144,
        output_usd_per_million=Decimal("30"),
    ),
    "claude-opus-5": ModelRuntimeProfile(
        model_id="claude-opus-5",
        base_label="Claude Opus 5",
        reasoning=ReasoningConfig(mode="adaptive", effort="medium"),
        supports_sampling_controls=False,
        summary="Most capable Claude tier for complex agentic analysis.",
        initial_output_tokens=16_384,
        automatic_output_token_ceiling=32_768,
        user_output_token_increment=16_384,
        absolute_output_token_ceiling=65_536,
        request_timeout_seconds=240,
        workflow_timeout_seconds=600,
        routing_context_char_ceiling=262_144,
        output_usd_per_million=Decimal("25"),
        provider=PROVIDER_ANTHROPIC,
        api_key_env="ANTHROPIC_API_KEY",
        supports_mid_conversation_system=False,
    ),
    "claude-sonnet-5": ModelRuntimeProfile(
        model_id="claude-sonnet-5",
        base_label="Claude Sonnet 5",
        reasoning=ReasoningConfig(mode="adaptive", effort="medium"),
        supports_sampling_controls=False,
        summary="Balanced Claude tier for most analysis workloads.",
        initial_output_tokens=16_384,
        automatic_output_token_ceiling=32_768,
        user_output_token_increment=16_384,
        absolute_output_token_ceiling=65_536,
        request_timeout_seconds=180,
        workflow_timeout_seconds=420,
        routing_context_char_ceiling=262_144,
        output_usd_per_million=Decimal("15"),
        provider=PROVIDER_ANTHROPIC,
        api_key_env="ANTHROPIC_API_KEY",
        supports_mid_conversation_system=False,
    ),
    "claude-haiku-4-5": ModelRuntimeProfile(
        model_id="claude-haiku-4-5",
        base_label="Claude Haiku 4.5",
        reasoning=None,
        supports_sampling_controls=False,
        summary="Fast, low-cost Claude tier for straightforward work.",
        initial_output_tokens=8_192,
        automatic_output_token_ceiling=16_384,
        user_output_token_increment=8_192,
        # Haiku 4.5 has a hard 64K output ceiling; keep the ladder below it.
        absolute_output_token_ceiling=49_152,
        request_timeout_seconds=120,
        workflow_timeout_seconds=300,
        routing_context_char_ceiling=262_144,
        output_usd_per_million=Decimal("5"),
        provider=PROVIDER_ANTHROPIC,
        api_key_env="ANTHROPIC_API_KEY",
        supports_mid_conversation_system=False,
    ),
}


INTERNAL_MODEL_RUNTIME_PROFILES = {
    "gpt5.6-Luna-Light": ModelRuntimeProfile(
        model_id="gpt5.6-Luna-Light",
        base_label="gpt5.6-Luna-Light",
        reasoning=ReasoningConfig(effort="low"),
        supports_sampling_controls=False,
        summary="Lightweight model for automatic titles and dataset names.",
        initial_output_tokens=8_192,
        automatic_output_token_ceiling=16_384,
        user_output_token_increment=8_192,
        absolute_output_token_ceiling=24_576,
        request_timeout_seconds=120,
        workflow_timeout_seconds=300,
        routing_context_char_ceiling=262_144,
        output_usd_per_million=Decimal("1.20"),
    ),
}


class CustomModelEntry(BaseModel):
    """One operator-registered OpenAI-compatible (e.g. vLLM) model."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1)
    label: str = ""
    model: str = ""
    api_key_env: str = ""
    summary: str = ""
    initial_output_tokens: int = Field(default=8_192, gt=0)
    automatic_output_token_ceiling: int = Field(default=16_384, gt=0)
    user_output_token_increment: int = Field(default=8_192, gt=0)
    absolute_output_token_ceiling: int = Field(default=24_576, gt=0)
    request_timeout_seconds: int = Field(default=120, gt=0)
    workflow_timeout_seconds: int = Field(default=300, gt=0)
    output_usd_per_million: str | None = None
    supports_vision: bool = False
    supports_mid_conversation_system: bool = False
    supports_sampling_controls: bool = True
    routing_context_char_ceiling: int = Field(default=262_144, gt=0)

    def to_profile(self) -> ModelRuntimeProfile:
        price: Decimal | None = None
        if self.output_usd_per_million is not None:
            try:
                price = Decimal(str(self.output_usd_per_million))
            except InvalidOperation as exc:
                raise ValueError(
                    f"Custom model {self.id!r} has an invalid "
                    "output_usd_per_million value."
                ) from exc
        return ModelRuntimeProfile(
            model_id=self.id,
            base_label=self.label or self.id,
            reasoning=None,
            supports_sampling_controls=self.supports_sampling_controls,
            summary=self.summary or "Operator-registered custom endpoint model.",
            initial_output_tokens=self.initial_output_tokens,
            automatic_output_token_ceiling=self.automatic_output_token_ceiling,
            user_output_token_increment=self.user_output_token_increment,
            absolute_output_token_ceiling=self.absolute_output_token_ceiling,
            request_timeout_seconds=self.request_timeout_seconds,
            workflow_timeout_seconds=self.workflow_timeout_seconds,
            routing_context_char_ceiling=self.routing_context_char_ceiling,
            output_usd_per_million=price,
            provider=PROVIDER_OPENAI_COMPATIBLE,
            api_key_env=self.api_key_env,
            api_key_required=bool(self.api_key_env.strip()),
            base_url=self.base_url,
            remote_model_id=self.model or self.id,
            supports_vision=self.supports_vision,
            supports_mid_conversation_system=(
                self.supports_mid_conversation_system
            ),
        )


def custom_models_path(
    environ: Mapping[str, str] = os.environ,
) -> Path:
    configured = str(environ.get(CUSTOM_MODELS_PATH_ENV, "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_CUSTOM_MODELS_PATH


def load_custom_model_profiles(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
) -> dict[str, ModelRuntimeProfile]:
    resolved = Path(path) if path is not None else custom_models_path(environ)
    if not resolved.is_file():
        return {}
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Custom model registry is unreadable: {resolved} ({exc})"
        ) from exc
    if not isinstance(raw, list):
        raise ValueError(
            f"Custom model registry must be a JSON array: {resolved}"
        )
    profiles: dict[str, ModelRuntimeProfile] = {}
    for item in raw:
        entry = CustomModelEntry.model_validate(item)
        if (
            entry.id in profiles
            or entry.id in MODEL_RUNTIME_PROFILES
            or entry.id in INTERNAL_MODEL_RUNTIME_PROFILES
        ):
            raise ValueError(
                f"Custom model id {entry.id!r} duplicates an existing model."
            )
        profiles[entry.id] = entry.to_profile()
    return profiles


_CUSTOM_PROFILES_LOCK = threading.Lock()
_CUSTOM_PROFILES: dict[str, ModelRuntimeProfile] | None = None


def _custom_profiles() -> dict[str, ModelRuntimeProfile]:
    global _CUSTOM_PROFILES
    with _CUSTOM_PROFILES_LOCK:
        if _CUSTOM_PROFILES is None:
            _CUSTOM_PROFILES = load_custom_model_profiles()
        return _CUSTOM_PROFILES


def reload_custom_model_profiles() -> None:
    """Drop the cached custom-model registry (used by tests)."""
    global _CUSTOM_PROFILES
    with _CUSTOM_PROFILES_LOCK:
        _CUSTOM_PROFILES = None


def model_runtime_profile(model_id: str) -> ModelRuntimeProfile:
    normalized = str(model_id or "").strip()
    profile = (
        MODEL_RUNTIME_PROFILES.get(normalized)
        or INTERNAL_MODEL_RUNTIME_PROFILES.get(normalized)
        or _custom_profiles().get(normalized)
    )
    if profile is not None:
        return profile
    choices = ", ".join([*MODEL_RUNTIME_PROFILES, *_custom_profiles()])
    raise ValueError(
        f"{normalized or 'Blank model'} is not an allowed application "
        f"model; choose one of: {choices}"
    )


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
    "CUSTOM_MODELS_PATH_ENV",
    "CustomModelEntry",
    "MODEL_RUNTIME_PROFILES",
    "ModelRuntimeProfile",
    "ReasoningConfig",
    "ReasoningEffort",
    "ReasoningMode",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_API_KEY_ENVS",
    "PROVIDER_LABELS",
    "PROVIDER_OPENAI",
    "PROVIDER_OPENAI_COMPATIBLE",
    "Provider",
    "configured_model_profiles",
    "custom_models_path",
    "load_custom_model_profiles",
    "model_runtime_profile",
    "reload_custom_model_profiles",
]
