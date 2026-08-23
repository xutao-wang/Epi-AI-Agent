from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import re
from typing import Any

from .config import EMBEDDING_MODEL
from .embedding_profiles import EmbeddingProfile, EmbeddingProfileResolution
from .retrieval_status import EmbeddingReasonCode


EmbeddingFactory = Callable[[str, str], Any]
EmbeddingAdapterFactory = Callable[[EmbeddingProfile, str], Any]

_PROVIDER_CREDENTIALS = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _openai_factory(model: str, api_key: str) -> Any:
    from .vectorstore import OpenAIEmbeddingFunction

    return OpenAIEmbeddingFunction(model, api_key=api_key)


def _openai_adapter(profile: EmbeddingProfile, api_key: str) -> Any:
    from .vectorstore import OpenAIEmbeddingFunction

    return OpenAIEmbeddingFunction(
        profile.index_compatibility,
        provider_model=profile.model,
        api_key=api_key,
        base_url=profile.base_url,
        timeout_seconds=profile.timeout_seconds,
    )


EMBEDDING_ADAPTERS: Mapping[str, EmbeddingAdapterFactory] = {
    "openai_embeddings": _openai_adapter,
}


def _provider_from_model(model: str) -> str:
    prefix = model.split("/", 1)[0].strip().casefold()
    return prefix or "unknown"


def _credential_env(provider: str) -> str:
    configured = _PROVIDER_CREDENTIALS.get(provider)
    if configured:
        return configured
    normalized = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")
    return f"{normalized or 'EMBEDDING_PROVIDER'}_API_KEY"


@dataclass(frozen=True)
class EmbeddingRoute:
    model: str
    provider: str
    credential_env: str
    api_key: str = field(repr=False)
    factory: EmbeddingFactory | EmbeddingAdapterFactory | None = field(
        default=None,
        repr=False,
    )
    unavailable_reason_code: EmbeddingReasonCode | None = None
    profile_id: str = "legacy"
    profile_label: str = "Configured embedding profile"
    dimensions: int = 0
    profile: EmbeddingProfile | None = field(default=None, repr=False)

    @property
    def available(self) -> bool:
        return self.factory is not None and self.unavailable_reason_code is None

    def create_embedding_function(self) -> Any:
        if not self.available or self.factory is None:
            raise RuntimeError("Embedding route is unavailable.")
        if self.profile is not None:
            return self.factory(self.profile, self.api_key)
        return self.factory(self.model, self.api_key)


def resolve_profile_route(
    environ: Mapping[str, str],
    resolution: EmbeddingProfileResolution,
    *,
    adapters: Mapping[str, EmbeddingAdapterFactory] | None = None,
) -> EmbeddingRoute:
    profile = resolution.profile
    if profile is None:
        return EmbeddingRoute(
            model="configured",
            provider="unknown",
            credential_env="",
            api_key="",
            unavailable_reason_code=(
                resolution.reason_code or "EMBEDDING_CONFIGURATION_UNAVAILABLE"
            ),
            profile_id=resolution.profile_id,
            profile_label=resolution.profile_label,
        )

    api_key = str(environ.get(profile.api_key_env, "") or "").strip()
    selected_adapters = EMBEDDING_ADAPTERS if adapters is None else adapters
    factory = selected_adapters.get(profile.transport)
    reason_code: EmbeddingReasonCode | None = None
    if not api_key:
        reason_code = "EMBEDDING_CREDENTIALS_MISSING"
    elif factory is None:
        reason_code = "EMBEDDING_TRANSPORT_UNAVAILABLE"
    return EmbeddingRoute(
        model=profile.index_compatibility,
        provider=profile.provider,
        credential_env=profile.api_key_env,
        api_key=api_key,
        factory=factory,
        unavailable_reason_code=reason_code,
        profile_id=profile.id,
        profile_label=profile.label,
        dimensions=profile.dimensions,
        profile=profile,
    )


def resolve_embedding_route(
    environ: Mapping[str, str],
    model: str,
) -> EmbeddingRoute:
    resolved_model = str(model or "").strip()
    if not resolved_model:
        raise ValueError("Embedding model must not be blank.")
    provider = _provider_from_model(resolved_model)
    credential_env = _credential_env(provider)
    api_key = str(environ.get(credential_env, "") or "").strip()

    if resolved_model != EMBEDDING_MODEL:
        return EmbeddingRoute(
            model=resolved_model,
            provider=provider,
            credential_env=credential_env,
            api_key=api_key,
            unavailable_reason_code="EMBEDDING_ROUTE_UNAVAILABLE",
        )
    return EmbeddingRoute(
        model=resolved_model,
        provider=provider,
        credential_env=credential_env,
        api_key=api_key,
        factory=_openai_factory,
        unavailable_reason_code=(
            None if api_key else "EMBEDDING_CREDENTIALS_MISSING"
        ),
    )


__all__ = [
    "EMBEDDING_ADAPTERS",
    "EmbeddingAdapterFactory",
    "EmbeddingFactory",
    "EmbeddingRoute",
    "resolve_embedding_route",
    "resolve_profile_route",
]
