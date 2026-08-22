from __future__ import annotations

import os

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from utils.model_runtime_profiles import (
    ModelRuntimeProfile,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    PROVIDER_OPENAI_COMPATIBLE,
    model_runtime_profile,
)


_COMPAT_PLACEHOLDER_KEY = "not-needed"


def resolve_provider_api_key(
    profile: ModelRuntimeProfile,
    *,
    api_key: str | None = None,
) -> str:
    resolved = str(api_key or "").strip()
    if not resolved and profile.api_key_env:
        resolved = str(os.getenv(profile.api_key_env, "") or "").strip()
    if not resolved:
        if profile.api_key_required:
            env_name = profile.api_key_env or "the provider API key"
            raise ValueError(f"{env_name} is required.")
        resolved = _COMPAT_PLACEHOLDER_KEY
    return resolved


def _build_openai_chat_llm(
    profile: ModelRuntimeProfile,
    api_key: str,
    *,
    temperature: float | None,
    top_p: float | None,
):
    kwargs: dict[str, object] = {
        "model": profile.served_model_id,
        "api_key": SecretStr(api_key),
        "timeout": profile.request_timeout_seconds,
        "max_retries": 0,
        "max_completion_tokens": profile.initial_output_tokens,
        "use_responses_api": True,
        "use_previous_response_id": True,
        "include_response_headers": True,
    }
    if profile.reasoning is not None:
        kwargs["reasoning_effort"] = profile.reasoning.effort
    if profile.supports_sampling_controls:
        kwargs["temperature"] = 0.0 if temperature is None else temperature
        kwargs["top_p"] = 1.0 if top_p is None else top_p
    return ChatOpenAI(**kwargs)


def _build_anthropic_chat_llm(profile: ModelRuntimeProfile, api_key: str):
    from langchain_anthropic import ChatAnthropic

    kwargs: dict[str, object] = {
        "model": profile.served_model_id,
        "api_key": api_key,
        "timeout": profile.request_timeout_seconds,
        "max_retries": 0,
        "max_tokens": profile.initial_output_tokens,
    }
    if profile.reasoning is not None:
        kwargs["thinking"] = {"type": profile.reasoning.mode}
        kwargs["effort"] = profile.reasoning.effort
    return ChatAnthropic(**kwargs)


def _build_openai_compatible_chat_llm(
    profile: ModelRuntimeProfile,
    api_key: str,
    *,
    temperature: float | None,
    top_p: float | None,
):
    # Plain chat-completions mode: OpenAI-compatible servers (e.g. vLLM)
    # generally do not implement the Responses API, response chaining,
    # or reasoning_effort.
    kwargs: dict[str, object] = dict(
        model=profile.served_model_id,
        api_key=SecretStr(api_key),
        base_url=profile.base_url,
        timeout=profile.request_timeout_seconds,
        max_retries=0,
        max_tokens=profile.initial_output_tokens,
    )
    if profile.supports_sampling_controls:
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
    return ChatOpenAI(**kwargs)


def build_chat_llm(
    *,
    model_name: str,
    api_key: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
):
    """Build the provider-correct LangChain chat model for a profiled model."""
    profile = model_runtime_profile(model_name)
    resolved_key = resolve_provider_api_key(profile, api_key=api_key)
    if profile.provider == PROVIDER_ANTHROPIC:
        return _build_anthropic_chat_llm(profile, resolved_key)
    if profile.provider == PROVIDER_OPENAI_COMPATIBLE:
        return _build_openai_compatible_chat_llm(
            profile,
            resolved_key,
            temperature=temperature,
            top_p=top_p,
        )
    if profile.provider == PROVIDER_OPENAI:
        return _build_openai_chat_llm(
            profile,
            resolved_key,
            temperature=temperature,
            top_p=top_p,
        )
    raise ValueError(f"Unsupported model provider: {profile.provider}")


def build_openai_llm(
    *,
    model_name: str,
    api_key: str,
    temperature: float | None = None,
    top_p: float | None = None,
):
    """Build an OpenAI model through the legacy local-only API."""
    resolved_key = str(api_key or "").strip()
    if not resolved_key:
        raise ValueError("provider_api_key is required.")
    resolved_model = str(model_name or "").strip()
    if not resolved_model:
        raise ValueError("model_name is required.")
    try:
        profile = model_runtime_profile(resolved_model)
    except ValueError:
        profile = None
    kwargs: dict[str, object] = {
        "model": profile.served_model_id if profile is not None else resolved_model,
        "api_key": SecretStr(resolved_key),
    }
    if profile is not None:
        kwargs.update(
            {
                "timeout": profile.request_timeout_seconds,
                "max_retries": 0,
                "max_completion_tokens": profile.initial_output_tokens,
                "use_responses_api": True,
                "use_previous_response_id": True,
                "include_response_headers": True,
            }
        )
    if profile is not None and profile.reasoning is not None:
        kwargs["reasoning_effort"] = profile.reasoning.effort
    supports_sampling = profile is None or profile.supports_sampling_controls
    if supports_sampling and temperature is not None:
        kwargs["temperature"] = temperature
    elif supports_sampling and profile is not None and profile.model_id == "gpt-5.4":
        kwargs["temperature"] = 0.0
    if supports_sampling and top_p is not None:
        kwargs["top_p"] = top_p
    elif supports_sampling and profile is not None and profile.model_id == "gpt-5.4":
        kwargs["top_p"] = 1.0
    return ChatOpenAI(**kwargs)


__all__ = ["build_chat_llm", "build_openai_llm", "resolve_provider_api_key"]
