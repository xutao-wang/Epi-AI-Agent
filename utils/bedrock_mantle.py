"""Exact configured model counterparts and fixed-endpoint AWS Mantle transport."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from copy import deepcopy
from typing import TYPE_CHECKING

import requests
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from utils.model_runtime_profiles import ModelRuntimeProfile

if TYPE_CHECKING:
    from utils.bedrock import BedrockSettings

# Verified exact instruction-tuned counterpart; never infer versions by similarity.
OPENROUTER_GEMMA = "google/gemma-4-31b-it"
MANTLE_GEMMA = "google.gemma-4-31b"


def mantle_endpoint(settings: BedrockSettings) -> str:
    # Settings have already validated the AWS region. No configurable URL or
    # OpenAI/OpenRouter environment URL can receive the Bedrock bearer token.
    return f"https://bedrock-mantle.{settings.region}.api.aws"


def discover_mantle_model_ids(settings: BedrockSettings) -> set[str]:
    response = requests.get(
        f"{mantle_endpoint(settings)}/v1/models",
        headers={"Authorization": f"Bearer {settings.api_key}"},
        timeout=(5, 10), allow_redirects=False,
    )
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError("AWS Mantle discovery did not return a model catalog.")
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("AWS Mantle returned an invalid model catalog.")
    return {item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)}


def matching_sources(profiles: Mapping[str, ModelRuntimeProfile]) -> list[ModelRuntimeProfile]:
    return [profile for profile in profiles.values()
            if profile.provider == "openrouter" and profile.served_model_id == OPENROUTER_GEMMA]


def counterpart_profiles(
    profiles: Mapping[str, ModelRuntimeProfile], model_ids: set[str], settings: BedrockSettings,
) -> dict[str, ModelRuntimeProfile]:
    sources = matching_sources(profiles)
    if not sources or MANTLE_GEMMA not in model_ids:
        return {}
    if len(sources) > 1:
        raise ValueError("Gemma counterpart has multiple OpenRouter source profiles; configure one source.")
    model_id = f"bedrock:{MANTLE_GEMMA}"
    if model_id in profiles:
        raise ValueError("Gemma counterpart identity conflicts with a configured model.")
    return {model_id: replace(
        sources[0], model_id=model_id, provider="bedrock", api_key_env="BEDROCK_API_KEY",
        api_key_required=True, base_url=None, remote_model_id=MANTLE_GEMMA,
        bedrock_region=settings.region, bedrock_base_model_id=MANTLE_GEMMA,
        custom_endpoint_id=None, output_usd_per_million=None,
        base_label=f"Google Gemma 4 31B IT · AWS Mantle · Regional ({settings.region})",
        summary="Exact configured Gemma counterpart via AWS Mantle; tools, streaming and configured reasoning. AWS pricing is separate.",
    )}


def _gemma_tool_schema(schema: dict) -> dict:
    """Retain set guidance without Mantle's rejected uniqueItems keyword.

    Walk schema edges only: property names, defaults and enum values are data.
    The server's Pydantic validators and all other schema keywords stay intact.
    """
    result = deepcopy(schema)
    if result.get("type") == "array" and result.get("uniqueItems") is True:
        result.pop("uniqueItems")
        description = str(result.get("description") or "").rstrip()
        result["description"] = (description + " Each array item must be unique.").lstrip()
    for key in ("properties", "patternProperties", "$defs", "definitions"):
        if isinstance(result.get(key), dict):
            result[key] = {name: _gemma_tool_schema(value) if isinstance(value, dict) else value
                           for name, value in result[key].items()}
    for key in ("items", "additionalProperties", "contains", "not", "if", "then", "else"):
        if isinstance(result.get(key), dict):
            result[key] = _gemma_tool_schema(result[key])
    for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
        if isinstance(result.get(key), list):
            result[key] = [_gemma_tool_schema(value) if isinstance(value, dict) else value
                           for value in result[key]]
    return result


class _MantleChatOpenAI(ChatOpenAI):
    """Gemma supports serial tools; Mantle rejects the flag without tools."""

    def bind_tools(self, tools, *, parallel_tool_calls=None, **kwargs):
        bound = super().bind_tools(tools, parallel_tool_calls=False, **kwargs)
        # Public LangChain conversion accepts dicts, Pydantic models and tools.
        # Copy its formatted definitions before applying this Gemma-only rule.
        formatted = deepcopy(bound.kwargs["tools"])
        for tool in formatted:
            function = tool.get("function", {})
            if isinstance(function.get("parameters"), dict):
                function["parameters"] = _gemma_tool_schema(function["parameters"])
        return bound.bind(tools=formatted)


def build_mantle_chat(profile: ModelRuntimeProfile, settings: BedrockSettings, *, temperature, top_p):
    kwargs = dict(
        model=profile.served_model_id, api_key=SecretStr(settings.api_key),
        base_url=f"{mantle_endpoint(settings)}/openai/v1",
        timeout=profile.request_timeout_seconds, max_retries=3,
        max_tokens=profile.initial_output_tokens, use_responses_api=False,
        stream_usage=True,
    )
    if profile.reasoning is not None:
        kwargs["reasoning_effort"] = profile.reasoning.effort
    if profile.supports_sampling_controls:
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
    return _MantleChatOpenAI(**kwargs)
