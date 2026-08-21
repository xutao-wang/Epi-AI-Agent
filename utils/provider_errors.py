"""Map provider SDK exceptions to public, provider-neutral error codes."""

from __future__ import annotations

from typing import Any

import httpx


def _public_failure(code: str, message: str) -> tuple[str, str]:
    return code, f"{message} Error: {code}"


def _body_markers(value: Any) -> set[str]:
    markers: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"code", "type"} and item is not None:
                markers.add(str(item).strip().lower())
            markers.update(_body_markers(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            markers.update(_body_markers(item))
    return markers


def _classify_openai_error(exc: Exception) -> tuple[str, str] | None:
    try:
        import openai
    except ImportError:  # pragma: no cover - openai is a core dependency
        return None

    if isinstance(exc, openai.APITimeoutError):
        return _public_failure(
            "MODEL_REQUEST_TIMEOUT",
            "The selected model did not respond within its request timeout.",
        )
    if isinstance(exc, openai.APIConnectionError):
        return _public_failure(
            "PROVIDER_CONNECTION_FAILED",
            "The server could not reach the model provider (OpenAI or a "
            "custom endpoint). Check the network connection, then retry.",
        )
    if isinstance(exc, openai.AuthenticationError) or (
        isinstance(exc, openai.APIStatusError) and exc.status_code == 401
    ):
        return _public_failure(
            "PROVIDER_AUTHENTICATION_FAILED",
            "The model provider rejected the configured API key. Update "
            "OPENAI_API_KEY (or the custom endpoint's key) and restart the server.",
        )
    if isinstance(exc, openai.PermissionDeniedError) or (
        isinstance(exc, openai.APIStatusError) and exc.status_code == 403
    ):
        return _public_failure(
            "PROVIDER_ACCESS_DENIED",
            "The configured API project is not allowed to use this resource. "
            "Check the project's permissions or use another API key.",
        )
    body = exc.body if isinstance(exc, openai.APIStatusError) else {}
    markers = _body_markers(body)
    if isinstance(exc, openai.RateLimitError):
        if markers & {"insufficient_quota", "credit_balance_exhausted"}:
            return _public_failure(
                "PROVIDER_CREDITS_EXHAUSTED",
                "The provider account has no remaining API credits. Add "
                "credits or use a funded API key, then retry.",
            )
        return _public_failure(
            "PROVIDER_RATE_LIMITED",
            "The model provider's request limit was reached. Wait briefly, "
            "then retry.",
        )
    if "context_length_exceeded" in markers:
        return _public_failure(
            "PROVIDER_CONTEXT_LIMIT_EXCEEDED",
            "This conversation exceeds the selected model's context limit. "
            "Start a new conversation or reduce the attached content.",
        )
    if "model_not_found" in markers or (
        isinstance(exc, openai.APIStatusError) and exc.status_code == 404
    ):
        return _public_failure(
            "PROVIDER_MODEL_UNAVAILABLE",
            "The selected model is unavailable at this provider or endpoint. "
            "Choose another model and retry.",
        )
    if isinstance(exc, openai.APIStatusError):
        return _generic_status_failure()
    return None


def _classify_anthropic_error(exc: Exception) -> tuple[str, str] | None:
    try:
        import anthropic
    except ImportError:
        return None

    if isinstance(exc, anthropic.APITimeoutError):
        return _public_failure(
            "MODEL_REQUEST_TIMEOUT",
            "The selected model did not respond within its request timeout.",
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return _public_failure(
            "PROVIDER_CONNECTION_FAILED",
            "The server could not reach Anthropic. Check the network "
            "connection, then retry.",
        )
    if isinstance(exc, anthropic.AuthenticationError):
        return _public_failure(
            "PROVIDER_AUTHENTICATION_FAILED",
            "Anthropic rejected the configured API key. Update "
            "ANTHROPIC_API_KEY and restart the server.",
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        message = str(exc).lower()
        if "credit" in message or "billing" in message:
            return _public_failure(
                "PROVIDER_CREDITS_EXHAUSTED",
                "The Anthropic account has no remaining API credits. Add "
                "credits or use a funded API key, then retry.",
            )
        return _public_failure(
            "PROVIDER_ACCESS_DENIED",
            "The configured Anthropic API key is not allowed to use this "
            "resource. Check the key's permissions or use another API key.",
        )
    if isinstance(exc, anthropic.RateLimitError):
        return _public_failure(
            "PROVIDER_RATE_LIMITED",
            "Anthropic's request limit was reached. Wait briefly, then retry.",
        )
    if isinstance(exc, anthropic.NotFoundError):
        return _public_failure(
            "PROVIDER_MODEL_UNAVAILABLE",
            "The selected Anthropic model is unavailable to this API key. "
            "Choose another model and retry.",
        )
    if isinstance(exc, anthropic.BadRequestError):
        message = str(exc).lower()
        if "credit" in message or "billing" in message:
            return _public_failure(
                "PROVIDER_CREDITS_EXHAUSTED",
                "The Anthropic account has no remaining API credits. Add "
                "credits or use a funded API key, then retry.",
            )
        if "prompt is too long" in message or "context" in message:
            return _public_failure(
                "PROVIDER_CONTEXT_LIMIT_EXCEEDED",
                "This conversation exceeds the selected model's context "
                "limit. Start a new conversation or reduce the attached content.",
            )
        return _generic_status_failure()
    if isinstance(exc, anthropic.APIStatusError):
        if int(getattr(exc, "status_code", 0) or 0) == 529:
            return _public_failure(
                "PROVIDER_RATE_LIMITED",
                "Anthropic is temporarily overloaded. Wait briefly, then retry.",
            )
        return _generic_status_failure()
    return None


def _generic_status_failure() -> tuple[str, str]:
    return _public_failure(
        "RUN_FAILED",
        "The request failed unexpectedly. Check the server log for details.",
    )


def classify_llm_error(exc: Exception) -> tuple[str, str]:
    """Return a (public_code, user_message) pair for a provider exception."""
    if isinstance(exc, httpx.TimeoutException):
        return _public_failure(
            "MODEL_REQUEST_TIMEOUT",
            "The selected model did not respond within its request timeout.",
        )
    for classifier in (_classify_anthropic_error, _classify_openai_error):
        result = classifier(exc)
        if result is not None:
            return result
    return _generic_status_failure()


__all__ = ["classify_llm_error"]
