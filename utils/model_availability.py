"""Registered and currently usable model catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from utils.model_runtime_profiles import (
    MODEL_RUNTIME_PROFILES,
    ModelRuntimeProfile,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    load_custom_model_profiles,
)


@dataclass(frozen=True)
class ProviderEndpoint:
    """One provider or compatible HTTP endpoint to verify at startup."""

    provider: str
    api_key_env: str
    base_url: str | None = None


@dataclass(frozen=True)
class ModelAvailability:
    """Complete registered profiles plus the subset usable in this process."""

    registered_profiles: Mapping[str, ModelRuntimeProfile]
    available_model_ids: tuple[str, ...]
    default_model_id: str
    title_model_id: str


def registered_model_profiles(
    environ: Mapping[str, str],
) -> Mapping[str, ModelRuntimeProfile]:
    profiles = {
        **MODEL_RUNTIME_PROFILES,
        **load_custom_model_profiles(environ=environ),
    }
    return MappingProxyType(profiles)


def profile_endpoint(profile: ModelRuntimeProfile) -> ProviderEndpoint:
    return ProviderEndpoint(
        provider=profile.provider,
        api_key_env=profile.api_key_env,
        base_url=profile.base_url,
    )


def configured_provider_endpoints(
    environ: Mapping[str, str],
) -> tuple[ProviderEndpoint, ...]:
    endpoints: dict[ProviderEndpoint, None] = {}
    for profile in registered_model_profiles(environ).values():
        has_builtin_key = (
            profile.provider == PROVIDER_OPENAI
            and str(environ.get("OPENAI_API_KEY", "") or "").strip()
        ) or (
            profile.provider == PROVIDER_ANTHROPIC
            and str(environ.get("ANTHROPIC_API_KEY", "") or "").strip()
        )
        if has_builtin_key or profile.base_url is not None:
            endpoints.setdefault(profile_endpoint(profile), None)
    return tuple(endpoints)


def build_model_availability(
    environ: Mapping[str, str],
    verified_endpoints: set[ProviderEndpoint],
) -> ModelAvailability:
    profiles = registered_model_profiles(environ)
    available = tuple(
        model_id
        for model_id, profile in profiles.items()
        if profile_endpoint(profile) in verified_endpoints
    )
    if not available:
        raise ValueError("No verified AI model provider is available.")

    if "gpt-5.6-terra" in available:
        default = "gpt-5.6-terra"
    elif "claude-opus-5" in available:
        default = "claude-opus-5"
    else:
        default = available[0]

    default_provider = profiles[default].provider
    preferred_title = {
        PROVIDER_OPENAI: "gpt-5.6-luna",
        PROVIDER_ANTHROPIC: "claude-haiku-4-5",
    }.get(default_provider, default)
    title = preferred_title if preferred_title in available else default
    return ModelAvailability(
        registered_profiles=profiles,
        available_model_ids=available,
        default_model_id=default,
        title_model_id=title,
    )


def model_availability_from_configured_credentials(
    environ: Mapping[str, str],
) -> ModelAvailability:
    """Build the non-native fallback catalog without performing network I/O."""
    profiles = registered_model_profiles(environ)
    configured: set[ProviderEndpoint] = set()
    for endpoint in configured_provider_endpoints(environ):
        endpoint_profiles = [
            profile
            for profile in profiles.values()
            if profile_endpoint(profile) == endpoint
        ]
        requires_key = any(profile.api_key_required for profile in endpoint_profiles)
        key = (
            str(environ.get(endpoint.api_key_env, "") or "").strip()
            if endpoint.api_key_env
            else ""
        )
        if requires_key and not key:
            continue
        configured.add(endpoint)
    return build_model_availability(environ, configured)


__all__ = [
    "ModelAvailability",
    "ProviderEndpoint",
    "build_model_availability",
    "configured_provider_endpoints",
    "model_availability_from_configured_credentials",
    "profile_endpoint",
    "registered_model_profiles",
]
