from __future__ import annotations

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from utils.model_runtime_profiles import model_runtime_profile


def build_openai_llm(
    *,
    model_name: str,
    api_key: str,
    temperature: float | None = None,
    top_p: float | None = None,
) -> ChatOpenAI:
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
        "model": profile.model_id if profile is not None else resolved_model,
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
    if profile is not None and profile.reasoning_effort is not None:
        kwargs["reasoning_effort"] = profile.reasoning_effort
    supports_sampling = (
        profile is None or profile.supports_sampling_controls
    )
    if supports_sampling and temperature is not None:
        kwargs["temperature"] = temperature
    elif supports_sampling and profile is not None and profile.model_id == "gpt-5.4":
        kwargs["temperature"] = 0.0
    if supports_sampling and top_p is not None:
        kwargs["top_p"] = top_p
    elif supports_sampling and profile is not None and profile.model_id == "gpt-5.4":
        kwargs["top_p"] = 1.0
    return ChatOpenAI(**kwargs)


__all__ = ["build_openai_llm"]
