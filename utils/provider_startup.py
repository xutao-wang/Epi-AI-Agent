from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from utils.model_runtime_profiles import OPENROUTER_BASE_URL


class ProviderCredentialError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class DiscoveredModelMetadata:
    """Deployment metadata advertised for one compatible model."""

    model_id: str
    max_model_len: int | None = None


def _positive_int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _status_failure(provider_label: str, status_code: int) -> ProviderCredentialError:
    bare_label = (
        provider_label[4:]
        if provider_label.lower().startswith("the ")
        else provider_label
    )
    messages = {
        401: (
            "authentication",
            f"{provider_label} rejected the API key. Paste a valid key and try again.",
        ),
        403: (
            "authorization",
            f"The {bare_label} API key is not authorized for this project.",
        ),
        402: (
            "credits",
            f"The {bare_label} account has no remaining credits. Add credits and try again.",
        ),
        429: (
            "temporary",
            f"{provider_label} temporarily rejected the credential check. Try again shortly.",
        ),
    }
    kind, message = messages.get(
        status_code,
        (
            "provider",
            f"{provider_label} credential check failed with HTTP status {status_code}.",
        ),
    )
    return ProviderCredentialError(kind, message)


def verify_openai_credentials(
    api_key: str,
    *,
    client_factory: Callable[..., Any] | None = None,
    base_url: str | None = None,
    provider_label: str = "OpenAI",
    allow_empty_key: bool = False,
) -> None:
    from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

    normalized_key = str(api_key or "").strip()
    if not normalized_key:
        if not allow_empty_key:
            raise ProviderCredentialError(
                "missing",
                f"A {provider_label} API key is required.",
            )
        normalized_key = "not-needed"
    factory = client_factory or OpenAI
    kwargs: dict[str, Any] = {
        "api_key": normalized_key,
        "max_retries": 0,
        "timeout": 10.0,
    }
    if base_url:
        kwargs["base_url"] = base_url
    try:
        factory(**kwargs).models.list()
    except (APIConnectionError, APITimeoutError) as error:
        raise ProviderCredentialError(
            "network",
            f"{provider_label} could not be reached. Check your network and try again.",
        ) from error
    except APIStatusError as error:
        status_code = int(error.status_code)
        if status_code == 404 and base_url:
            # Some OpenAI-compatible servers do not implement /models;
            # a 404 still proves the endpoint is reachable and accepted the key.
            return
        raise _status_failure(provider_label, status_code) from error


def verify_openrouter_credentials(
    api_key: str,
    *,
    base_url: str | None = None,
    request_fn: Callable[..., Any] | None = None,
) -> None:
    import httpx

    normalized_key = str(api_key or "").strip()
    if not normalized_key:
        raise ProviderCredentialError(
            "missing",
            "An OpenRouter API key is required.",
        )
    # OpenRouter serves /models without authentication, so listing models
    # proves nothing about the key; /key rejects an invalid credential.
    endpoint = f"{(base_url or OPENROUTER_BASE_URL).rstrip('/')}/key"
    getter = request_fn or httpx.get
    try:
        response = getter(
            endpoint,
            headers={"Authorization": f"Bearer {normalized_key}"},
            timeout=10.0,
        )
    except httpx.RequestError as error:
        raise ProviderCredentialError(
            "network",
            "OpenRouter could not be reached. Check your network and try again.",
        ) from error
    status_code = int(response.status_code)
    if status_code >= 400:
        raise _status_failure("OpenRouter", status_code)


def discover_openai_compatible_models(
    api_key: str,
    *,
    base_url: str,
    client_factory: Callable[..., Any] | None = None,
) -> tuple[DiscoveredModelMetadata, ...]:
    """Return exact model IDs and deployment metadata from ``/models``."""
    from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

    normalized_key = str(api_key or "").strip() or "not-needed"
    factory = client_factory or OpenAI
    try:
        response = factory(
            api_key=normalized_key,
            base_url=base_url,
            max_retries=0,
            timeout=10.0,
        ).models.list()
    except (APIConnectionError, APITimeoutError) as error:
        raise ProviderCredentialError(
            "network",
            "The custom endpoint could not be reached. Check your network and "
            "try again.",
        ) from error
    except APIStatusError as error:
        raise _status_failure(
            "The custom endpoint",
            int(error.status_code),
        ) from error

    discovered = []
    for model in getattr(response, "data", ()):
        model_id = str(getattr(model, "id", "") or "").strip()
        max_model_len = getattr(model, "max_model_len", None)
        if max_model_len is None:
            model_extra = getattr(model, "model_extra", None)
            if isinstance(model_extra, dict):
                max_model_len = model_extra.get("max_model_len")
        discovered.append(
            DiscoveredModelMetadata(
                model_id=model_id,
                max_model_len=_positive_int_or_none(max_model_len),
            )
        )
    return tuple(discovered)


def verify_anthropic_credentials(
    api_key: str,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> None:
    import anthropic

    normalized_key = str(api_key or "").strip()
    if not normalized_key:
        raise ProviderCredentialError(
            "missing",
            "An Anthropic API key is required.",
        )
    factory = client_factory or anthropic.Anthropic
    try:
        factory(
            api_key=normalized_key,
            max_retries=0,
            timeout=10.0,
        ).models.list()
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as error:
        raise ProviderCredentialError(
            "network",
            "Anthropic could not be reached. Check your network and try again.",
        ) from error
    except anthropic.APIStatusError as error:
        raise _status_failure("Anthropic", int(error.status_code)) from error


def verify_provider_credential(
    provider: str,
    api_key: str,
    *,
    base_url: str | None = None,
    openai_checker: Callable[..., None] = verify_openai_credentials,
    anthropic_checker: Callable[..., None] = verify_anthropic_credentials,
    openrouter_checker: Callable[..., None] = verify_openrouter_credentials,
) -> None:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider == "openai":
        openai_checker(api_key)
        return
    if normalized_provider == "anthropic":
        anthropic_checker(api_key)
        return
    if normalized_provider == "openrouter":
        openrouter_checker(api_key, base_url=base_url)
        return
    if normalized_provider == "openai_compatible":
        openai_checker(
            api_key,
            base_url=base_url,
            provider_label="The custom endpoint",
            allow_empty_key=True,
        )
        return
    if not normalized_provider:
        raise ProviderCredentialError(
            "provider",
            "An active AI provider must be configured.",
        )
    raise ProviderCredentialError(
        "provider",
        "Startup verification is not configured for the active provider.",
    )


def verify_active_provider(
    provider: str,
    api_key: str,
    *,
    openai_checker: Callable[[str], None] = verify_openai_credentials,
) -> None:
    """Deprecated alias for verify_provider_credential."""
    verify_provider_credential(
        provider,
        api_key,
        openai_checker=openai_checker,
    )


__all__ = [
    "DiscoveredModelMetadata",
    "ProviderCredentialError",
    "discover_openai_compatible_models",
    "verify_active_provider",
    "verify_anthropic_credentials",
    "verify_openai_credentials",
    "verify_openrouter_credentials",
    "verify_provider_credential",
]
