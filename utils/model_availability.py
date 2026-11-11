"""Registered and currently usable model catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from utils.model_runtime_profiles import (
    MODEL_RUNTIME_PROFILES,
    ModelRuntimeProfile,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    PROVIDER_OPENAI_COMPATIBLE,
    enabled_custom_endpoint_ids,
    load_custom_model_profiles,
)


@dataclass(frozen=True)
class ProviderEndpoint:
    """One provider or compatible HTTP endpoint to verify at startup."""

    provider: str
    api_key_env: str
    base_url: str | None = None
    region: str | None = None


@dataclass(frozen=True)
class ModelAvailability:
    """Complete registered profiles plus the subset usable in this process."""

    registered_profiles: Mapping[str, ModelRuntimeProfile]
    available_model_ids: tuple[str, ...]
    default_model_id: str
    title_model_id: str


def registered_model_profiles(
    environ: Mapping[str, str],
    discovered_profiles: Mapping[str, ModelRuntimeProfile] | None = None,
) -> Mapping[str, ModelRuntimeProfile]:
    profiles = {
        **MODEL_RUNTIME_PROFILES,
        **(discovered_profiles or {}),
    }
    configured = load_custom_model_profiles(environ=environ)
    for model_id, custom in configured.items():
        observed = (discovered_profiles or {}).get(model_id)
        if (observed is not None and custom.provider == observed.provider == "bedrock"
                and custom.served_model_id == observed.served_model_id
                and custom.bedrock_region == observed.bedrock_region
                and observed.bedrock_base_model_id):
            # Keep operator runtime settings while retaining the identity resolved
            # from this exact target by authenticated discovery.
            custom = replace(custom, bedrock_base_model_id=observed.bedrock_base_model_id)
        profiles[model_id] = custom
    from utils.bedrock import filter_bedrock_profiles
    from utils.bedrock_runtime_settings import align_bedrock_runtime_settings
    profiles = align_bedrock_runtime_settings(
        profiles, explicit_ids={mid for mid, p in configured.items() if p.provider == "bedrock"},
    )
    return MappingProxyType(filter_bedrock_profiles(profiles, environ))


def profile_endpoint(profile: ModelRuntimeProfile) -> ProviderEndpoint:
    return ProviderEndpoint(
        provider=profile.provider,
        api_key_env=profile.api_key_env,
        base_url=profile.base_url,
        region=profile.bedrock_region,
    )


def configured_provider_endpoints(
    environ: Mapping[str, str],
) -> tuple[ProviderEndpoint, ...]:
    endpoints: dict[ProviderEndpoint, None] = {}
    enabled_custom = set(enabled_custom_endpoint_ids(environ))
    for profile in registered_model_profiles(environ).values():
        if (
            profile.provider == PROVIDER_OPENAI_COMPATIBLE
            and profile.custom_endpoint_id not in enabled_custom
        ):
            continue
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
    discovered_profiles: Mapping[str, ModelRuntimeProfile] | None = None,
) -> ModelAvailability:
    profiles = registered_model_profiles(environ, discovered_profiles)
    available = tuple(
        model_id
        for model_id, profile in profiles.items()
        if profile_endpoint(profile) in verified_endpoints
    )
    if not available:
        raise ValueError("No verified AI model provider is available.")

    automatic = tuple(
        model_id for model_id in available
        if profiles[model_id].automatic_selection_allowed
    )
    if not automatic:
        raise ValueError(
            "Only explicit-selection Bedrock global or application choices are available. "
            "Register an explicit global or application target in config/custom_models.json "
            "to permit automatic chat and title selection, or configure a regional/geographic model."
        )

    if "gpt-5.6-terra" in automatic:
        default = "gpt-5.6-terra"
    elif "claude-opus-5" in automatic:
        default = "claude-opus-5"
    elif (
        "bedrock:openai.gpt-5.6-terra" in automatic
        and all(profiles[mid].provider == "bedrock" for mid in available)
    ):
        default = "bedrock:openai.gpt-5.6-terra"
    elif (
        "bedrock:amazon.nova-micro-v1:0" in automatic
        and all(profiles[mid].provider == "bedrock" for mid in available)
    ):
        default = "bedrock:amazon.nova-micro-v1:0"
    else:
        default = automatic[0]

    default_provider = profiles[default].provider
    preferred_title = {
        PROVIDER_OPENAI: "gpt-5.6-luna",
        PROVIDER_ANTHROPIC: "claude-haiku-4-5",
    }.get(default_provider, default)
    title = preferred_title if preferred_title in automatic else default
    return ModelAvailability(
        registered_profiles=profiles,
        available_model_ids=available,
        default_model_id=default,
        title_model_id=title,
    )


def model_availability_from_configured_credentials(
    environ: Mapping[str, str],
) -> ModelAvailability:
    """Build the configured catalog, including authenticated native Bedrock discovery."""
    from utils.bedrock import bedrock_configured, discover_bedrock_profiles
    from utils.provider_startup import ProviderCredentialError
    discovered = {}
    bedrock_failure = None
    if bedrock_configured(environ):
        try:
            discovered = discover_bedrock_profiles(environ)
        except (ProviderCredentialError, ValueError) as exc:
            bedrock_failure = exc
    profiles = registered_model_profiles(environ, discovered)
    configured: set[ProviderEndpoint] = {profile_endpoint(p) for p in discovered.values()}
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
    try:
        return build_model_availability(environ, configured, discovered)
    except ValueError:
        if bedrock_failure is not None:
            raise ValueError(str(bedrock_failure)) from None
        raise


__all__ = [
    "ModelAvailability",
    "ProviderEndpoint",
    "build_model_availability",
    "configured_provider_endpoints",
    "model_availability_from_configured_credentials",
    "profile_endpoint",
    "registered_model_profiles",
]
