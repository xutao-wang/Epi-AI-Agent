"""Native Bedrock settings, bounded authenticated discovery, and Converse models.

Listing a model verifies catalog permissions, never account invocation access.
Unknown model families require an operator-registered capability override.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import os
import logging
import re
from typing import Any

from pydantic import SecretStr

from utils.model_runtime_profiles import (
    MODEL_RUNTIME_PROFILES, ModelRuntimeProfile, ReasoningConfig, ReasoningEffort, load_custom_model_profiles,
)


@dataclass(frozen=True)
class BedrockSettings:
    api_key: str = field(repr=False)
    region: str


def bedrock_configured(environ: Mapping[str, str]) -> bool:
    return any(str(environ.get(key, "") or "").strip() for key in ("BEDROCK_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"))


def resolve_bedrock_settings(environ: Mapping[str, str]) -> BedrockSettings:
    """Resolve only the supplied mapping, without consulting ambient credentials."""
    def first(*names: str) -> str:
        return next((value for name in names if (value := str(environ.get(name, "") or "").strip())), "")
    key = first("BEDROCK_API_KEY", "AWS_BEARER_TOKEN_BEDROCK")
    if not key:
        raise ValueError("BEDROCK_API_KEY (or AWS_BEARER_TOKEN_BEDROCK) is required.")
    region = first("BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
    if not region:
        raise ValueError("BEDROCK_REGION (or AWS_REGION / AWS_DEFAULT_REGION) is required.")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region):
        raise ValueError("Invalid Amazon Bedrock region.")
    return BedrockSettings(key, region)


def _aws_endpoint(service: str, region: str) -> str:
    # Explicit endpoints prevent AWS_ENDPOINT_URL* from redirecting bearer tokens.
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    return f"https://{service}.{region}.{suffix}"


def _client(settings: BedrockSettings):
    from botocore.config import Config
    from langchain_aws.utils import create_aws_client
    return create_aws_client(
        service_name="bedrock", region_name=settings.region,
        endpoint_url=_aws_endpoint("bedrock", settings.region),
        api_key=SecretStr(settings.api_key),
        config=Config(connect_timeout=10, read_timeout=10, retries={"total_max_attempts": 1}),
    )


@dataclass(frozen=True)
class _Capabilities:
    context: int
    output: int
    vision: bool = False
    sampling: bool = True
    reasoning_effort: ReasoningEffort | None = None
    requires_profile: bool = False


# Explicit supported Converse + streaming tool-use families. Keep this conservative;
# model listing supplies modality/streaming checks but does not advertise tool use.
# Sources and the override format are documented in README.md. Claude 3.7+
# access tables require inference profiles; ON_DEMAND is not direct-route proof.
_NOVA = {
    "amazon.nova-micro-v1:0": _Capabilities(128_000, 10_000),
    "amazon.nova-lite-v1:0": _Capabilities(300_000, 10_000, True),
    "amazon.nova-pro-v1:0": _Capabilities(300_000, 10_000, True),
}
_NAMED_MODELS = {
    "openai.gpt-5.6-luna": _Capabilities(1_050_000, 128_000, True, False, "low", True),
    "openai.gpt-5.6-terra": _Capabilities(1_050_000, 128_000, True, False, None, True),
    "openai.gpt-5.6-sol": _Capabilities(1_050_000, 128_000, True, False, "low", True),
    "anthropic.claude-sonnet-5": _Capabilities(1_000_000, 128_000, True, False, "medium", True),
    "deepseek.v3.2": _Capabilities(163_840, 8_192),
    "minimax.minimax-m2.5": _Capabilities(196_608, 8_192),
    "qwen.qwen3-coder-next": _Capabilities(262_144, 16_384),
}
_CLAUDE_PATTERNS = (
    (r"anthropic\.claude-3-(?:haiku|sonnet|opus)-\d{8}-v1:0", _Capabilities(200_000, 4_096, True)),
    (r"anthropic\.claude-3-5-(?:haiku|sonnet)-\d{8}-v[12]:0", _Capabilities(200_000, 8_192, True)),
    (r"anthropic\.claude-3-7-sonnet-\d{8}-v1:0", _Capabilities(200_000, 64_000, True, requires_profile=True)),
    (r"anthropic\.claude-(?:sonnet|opus)-4-\d{8}-v1:0", _Capabilities(200_000, 32_000, True, requires_profile=True)),
    (r"anthropic\.claude-opus-4-1-\d{8}-v1:0", _Capabilities(200_000, 32_000, True, requires_profile=True)),
    (r"anthropic\.claude-(?:haiku|sonnet|opus)-4-5-\d{8}-v1:0", _Capabilities(200_000, 64_000, True, requires_profile=True)),
    (r"anthropic\.claude-(?:sonnet|opus)-4-6(?:-v1(?::0)?)?", _Capabilities(200_000, 64_000, True, False, requires_profile=True)),
)


def _capabilities(model_id: str) -> _Capabilities | None:
    if model_id in _NOVA or model_id in _NAMED_MODELS:
        return {**_NOVA, **_NAMED_MODELS}[model_id]
    return next((cap for pattern, cap in _CLAUDE_PATTERNS if re.fullmatch(pattern, model_id)), None)


def _geo(region: str) -> tuple[str, ...]:
    """Preferred observed geographic scopes, from country to broader geography."""
    if region.startswith("us-gov-"):
        return ("us-gov",)
    country = {
        "ap-northeast-1": "jp", "ap-northeast-3": "jp",
        "ap-southeast-2": "au", "ap-southeast-4": "au",
        "ap-south-1": "in", "ap-south-2": "in",
    }.get(region)
    if country:
        return (country, "apac")
    prefix = region.split("-")[0]
    return ("apac" if prefix == "ap" else prefix,)


def _profile(base: str, name: str, target: str, scope: str, settings: BedrockSettings,
             cap: _Capabilities, *, explicit: bool = False) -> ModelRuntimeProfile:
    reasoning = (
        ReasoningConfig(
            effort=cap.reasoning_effort,
            mode="adaptive" if base.startswith("anthropic.") else None,
        )
        if cap.reasoning_effort else None
    )
    return ModelRuntimeProfile(
        model_id=f"bedrock:{target if explicit else base}",
        base_label=f"{name} · {base.split('.')[0].title()} · {scope}",
        provider="bedrock", api_key_env="BEDROCK_API_KEY", bedrock_region=settings.region,
        bedrock_base_model_id=base,
        automatic_selection_allowed=not explicit,
        remote_model_id=target, reasoning=reasoning,
        supports_sampling_controls=cap.sampling,
        supports_vision=cap.vision, supports_mid_conversation_system=False,
        context_window_tokens=cap.context,
        summary=f"Native Converse with tools and streaming; {scope} routing. Account invocation access is checked when used.",
        initial_output_tokens=min(4_096, cap.output), automatic_output_token_ceiling=min(8_192, cap.output),
        user_output_token_increment=min(4_096, cap.output), absolute_output_token_ceiling=cap.output,
        request_timeout_seconds=180, workflow_timeout_seconds=600,
        routing_context_char_ceiling=min(262_144, cap.context * 2), output_usd_per_million=None,
    )


def discover_bedrock_profiles(environ: Mapping[str, str], *, client: Any = None, mantle_discoverer=None) -> dict[str, ModelRuntimeProfile]:
    """Discover native, supported candidates. Does not invoke models.

    ListFoundationModels is unpaginated; ListInferenceProfiles supports nextToken.
    Cap profile pages at 20 so broken cursors cannot loop indefinitely.
    """
    from utils.provider_startup import ProviderCredentialError
    from utils.provider_errors import classify_llm_error
    settings = resolve_bedrock_settings(environ)
    try:
        client = client if client is not None else _client(settings)
        models = client.list_foundation_models(byOutputModality="TEXT").get("modelSummaries", [])
        inference_profiles = []
        next_token = None
        seen_tokens: set[str] = set()
        for _ in range(20):
            page = client.list_inference_profiles(**({"nextToken": next_token} if next_token else {}))
            inference_profiles.extend(page.get("inferenceProfileSummaries", []))
            next_token = page.get("nextToken")
            if not next_token:
                break
            if next_token in seen_tokens:
                raise ValueError("Amazon Bedrock returned a repeated catalog pagination token.")
            seen_tokens.add(next_token)
        else:
            raise ValueError("Amazon Bedrock catalog pagination exceeded the bounded page limit.")
    except ValueError:
        raise
    except Exception as exc:
        code, message = classify_llm_error(exc)
        raise ProviderCredentialError(code, "Amazon Bedrock catalog discovery failed. " + message) from None

    targets: dict[str, list[tuple[str, str]]] = {}
    observed_bases: dict[str, set[str]] = {}
    for entry in inference_profiles:
        if entry.get("status") != "ACTIVE":
            continue
        target = str(entry.get("inferenceProfileId") or "")
        if not target:
            continue
        for remote in entry.get("models", []):
            base = str(remote.get("modelArn", "")).split("foundation-model/")[-1]
            if entry.get("type") == "APPLICATION":
                scope = "Application"
            elif target.startswith("global."):
                scope = "Global"
            else:
                scope = target.split(".")[0].upper()
            targets.setdefault(base, []).append((target, scope))
            for identity in (target, entry.get("inferenceProfileArn")):
                if identity:
                    observed_bases.setdefault(str(identity), set()).add(base)

    profiles: dict[str, ModelRuntimeProfile] = {}
    for entry in models:
        base = str(entry.get("modelId", ""))
        cap = _capabilities(base)
        if cap is None or not entry.get("responseStreamingSupported"):
            continue
        if "TEXT" not in entry.get("inputModalities", []) or "TEXT" not in entry.get("outputModalities", []):
            continue
        cap = replace(cap, vision=cap.vision and "IMAGE" in entry.get("inputModalities", []))
        name = str(entry.get("modelName") or base)
        candidates = sorted(set(targets.get(base, [])))
        # Prefer an observed geographic target. ON_DEMAND alone does not prove
        # that a foundation model can be invoked directly without a profile.
        preferred = next(
            ((target, scope) for prefix in _geo(settings.region)
             for target, scope in candidates if target.startswith(f"{prefix}.")),
            None,
        )
        if preferred:
            target, scope = preferred
            profiles[f"bedrock:{base}"] = _profile(base, name, target, scope, settings, cap)
        elif "ON_DEMAND" in entry.get("inferenceTypesSupported", []) and not cap.requires_profile:
            profiles[f"bedrock:{base}"] = _profile(base, name, base, f"Regional ({settings.region})", settings, cap)
        for target, scope in candidates:
            # Global routing is always explicit; never silently choose it.
            if target.startswith("global.") or scope == "Application":
                profile = _profile(base, name, target, scope, settings, cap, explicit=True)
                profiles[profile.model_id] = profile
    from utils.bedrock_mantle import counterpart_profiles, discover_mantle_model_ids, matching_sources
    configured = load_custom_model_profiles(environ=environ)
    if matching_sources(configured):
        try:
            mantle_ids = (mantle_discoverer or discover_mantle_model_ids)(settings)
            profiles.update(counterpart_profiles(configured, mantle_ids, settings))
        except Exception:
            # Discovery errors may contain credential-bearing request objects.
            # Preserve usable Converse models and report only safe context.
            logging.getLogger(__name__).warning(
                "AWS Mantle discovery unavailable or Gemma source ambiguous; no Gemma counterpart added."
            )
    for model_id, override in configured.items():
        if override.provider == "bedrock" and override.bedrock_region == settings.region:
            bases = observed_bases.get(override.served_model_id, set())
            if len(bases) == 1:
                override = replace(override, bedrock_base_model_id=next(iter(bases)))
            profiles[model_id] = override
    from utils.bedrock_runtime_settings import align_bedrock_runtime_settings
    aligned = align_bedrock_runtime_settings(
        {**MODEL_RUNTIME_PROFILES, **configured, **profiles},
        explicit_ids={mid for mid, p in configured.items() if p.provider == "bedrock"},
    )
    return filter_bedrock_profiles({mid: aligned[mid] for mid in profiles}, environ)


def _canonical_base_identity(target: str) -> str:
    """Resolve a foundation ID or geographic target; opaque IDs need metadata."""
    target = target.rsplit("foundation-model/", 1)[-1]
    pieces = target.split(".")
    if pieces[0] in {"us", "eu", "apac", "global", "us-gov", "sa", "amer", "au", "jp", "in"}:
        pieces = pieces[1:]
    canonical = ".".join(pieces)
    if not re.fullmatch(r"[a-z][a-z0-9_-]*\.[a-z0-9][a-z0-9._:-]*", canonical):
        return ""
    return canonical


def bedrock_model_vendor(profile: ModelRuntimeProfile) -> str:
    """Resolve provider from exact base metadata or the observed AWS target."""
    canonical = _canonical_base_identity((profile.bedrock_base_model_id or profile.served_model_id).lower())
    return canonical.partition(".")[0].lower() if canonical else ""


def filter_bedrock_profiles(profiles: Mapping[str, ModelRuntimeProfile], environ: Mapping[str, str]) -> dict[str, ModelRuntimeProfile]:
    excluded = {part.strip().lower() for part in str(environ.get("BEDROCK_EXCLUDED_PROVIDERS", "") or "").split(",") if part.strip()}
    allowed = {part.strip() for part in str(environ.get("BEDROCK_ALLOWED_MODELS", "") or "").split(",") if part.strip()}
    filtered = {}
    for model_id, profile in profiles.items():
        if profile.provider == "bedrock" and allowed:
            base = _canonical_base_identity(profile.bedrock_base_model_id or profile.served_model_id)
            target_base = _canonical_base_identity(profile.served_model_id)
            # Both the public identity and resolved AWS identity must match. A
            # selected name cannot conceal another family or a global route.
            if (model_id not in allowed or model_id != f"bedrock:{base}"
                    or profile.model_id != model_id or not base
                    or target_base != base
                    or profile.served_model_id.rsplit("/", 1)[-1].startswith("global.")):
                continue
        if profile.provider == "bedrock" and excluded:
            vendor = bedrock_model_vendor(profile)
            if not vendor:
                logging.getLogger(__name__).warning(
                    "Bedrock target vendor could not be resolved; set bedrock_base_model_id "
                    "to the exact AWS foundation-model ID or use a discoverable target. "
                    "Model omitted while BEDROCK_EXCLUDED_PROVIDERS is active."
                )
                continue
            if vendor in excluded:
                continue
        filtered[model_id] = profile
    return filtered


def build_bedrock_chat(profile: ModelRuntimeProfile, api_key: str, *, temperature: float | None, top_p: float | None):
    from botocore.config import Config
    from langchain_aws import ChatBedrockConverse
    settings_env = dict(os.environ) | {"BEDROCK_API_KEY": api_key}
    if profile.bedrock_region:
        settings_env["BEDROCK_REGION"] = profile.bedrock_region
    settings = resolve_bedrock_settings(settings_env)
    from utils.bedrock_mantle import MANTLE_GEMMA, build_mantle_chat
    if profile.served_model_id == MANTLE_GEMMA:
        return build_mantle_chat(profile, settings, temperature=temperature, top_p=top_p)
    base_model = profile.bedrock_base_model_id or profile.served_model_id
    if not profile.bedrock_base_model_id and base_model.split(".")[0] in {
        "us", "eu", "apac", "global", "us-gov", "sa", "amer", "au", "jp", "in",
    }:
        base_model = base_model.partition(".")[2]
    kwargs: dict[str, Any] = dict(
        base_model=base_model,
        provider=base_model.split(".")[0],
        model=profile.served_model_id, api_key=SecretStr(api_key), region_name=settings.region,
        endpoint_url=_aws_endpoint("bedrock-runtime", settings.region),
        max_tokens=profile.initial_output_tokens,
        bedrock_client=_client(settings),
        disable_streaming=False,
        supports_tool_choice_values=("auto", "any", "tool"),
        config=Config(connect_timeout=10, read_timeout=profile.request_timeout_seconds,
                      retries={"total_max_attempts": 3, "mode": "standard"}),
    )
    if profile.supports_sampling_controls:
        if temperature is not None:
            kwargs["temperature"] = temperature
        # Claude 4.5+ rejects temperature + topP together. Prefer the user's
        # temperature when both controls are set; topP remains usable alone.
        if top_p is not None and not ("anthropic.claude" in base_model and temperature is not None):
            kwargs["top_p"] = top_p
    if profile.reasoning is not None:
        if "openai.gpt-" in base_model:
            kwargs["additional_model_request_fields"] = {"reasoning": {"effort": profile.reasoning.effort}}
        elif "anthropic.claude-sonnet-5" in base_model:
            kwargs["additional_model_request_fields"] = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": profile.reasoning.effort},
            }
            if base_model == "anthropic.claude-sonnet-5-5":
                # Agent reminders and review resumes can edit an earlier turn.
                # AWS documents these Converse fields for discarding thinking
                # whose signature no longer matches that conversation prefix.
                fields = kwargs["additional_model_request_fields"]
                fields["anthropic_beta"] = ["thinking-binding-controls-2026-08-01"]
                fields["thinking"]["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
            kwargs["supports_tool_choice_values"] = ("auto",)
        else:
            raise ValueError("Bedrock reasoning overrides are supported for OpenAI GPT and Claude Sonnet 5 models.")
    if base_model == "deepseek.v3.2":
        from utils.bedrock_converse import ChatBedrockDeepSeekV32
        return ChatBedrockDeepSeekV32(**kwargs)
    return ChatBedrockConverse(**kwargs)
