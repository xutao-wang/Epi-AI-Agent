from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar


T = TypeVar("T")
RetrievalMode = Literal["hybrid_vector_lexical", "lexical_fallback"]
EmbeddingReasonCode = Literal[
    "EMBEDDING_PROFILE_INVALID",
    "EMBEDDING_PROFILE_UNKNOWN",
    "EMBEDDING_PROFILE_DISABLED",
    "EMBEDDING_CREDENTIALS_MISSING",
    "EMBEDDING_ROUTE_UNAVAILABLE",
    "EMBEDDING_TRANSPORT_UNAVAILABLE",
    "EMBEDDING_CONFIGURATION_UNAVAILABLE",
    "EMBEDDING_INDEX_UNAVAILABLE",
    "EMBEDDING_INDEX_INCOMPATIBLE",
    "EMBEDDING_PROVIDER_UNAVAILABLE",
    "EMBEDDING_PROBE_TIMEOUT",
    "EMBEDDING_RESPONSE_INVALID",
    "EMBEDDING_DIMENSION_MISMATCH",
]

_REASONS: dict[EmbeddingReasonCode, str] = {
    "EMBEDDING_PROFILE_INVALID": "its profile registry is invalid",
    "EMBEDDING_PROFILE_UNKNOWN": "its selected profile is unknown",
    "EMBEDDING_PROFILE_DISABLED": "its selected profile is disabled",
    "EMBEDDING_ROUTE_UNAVAILABLE": (
        "no embedding adapter is available for this route"
    ),
    "EMBEDDING_TRANSPORT_UNAVAILABLE": (
        "no embedding adapter is available for this transport"
    ),
    "EMBEDDING_CONFIGURATION_UNAVAILABLE": (
        "its configuration is unavailable or incompatible"
    ),
    "EMBEDDING_INDEX_UNAVAILABLE": "the semantic index is unavailable",
    "EMBEDDING_INDEX_INCOMPATIBLE": (
        "the semantic index is incompatible with the selected profile"
    ),
    "EMBEDDING_PROVIDER_UNAVAILABLE": (
        "the embedding provider could not complete the query"
    ),
    "EMBEDDING_PROBE_TIMEOUT": "the embedding provider timed out",
    "EMBEDDING_RESPONSE_INVALID": (
        "the embedding provider returned an invalid response"
    ),
    "EMBEDDING_DIMENSION_MISMATCH": (
        "the embedding provider returned an incompatible vector dimension"
    ),
}


@dataclass(frozen=True)
class RetrievalStatus:
    mode: RetrievalMode
    model: str
    available: bool
    provider: str
    credential_env: str | None = None
    reason_code: EmbeddingReasonCode | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "available": self.available,
            "model": self.model,
            "provider": self.provider,
        }
        if self.reason_code is not None:
            payload.update(
                reason_code=self.reason_code,
                message=(
                    f"Embedding model {self.model} via {self.provider} is "
                    f"unavailable because {self._reason()}. Results use lexical "
                    "string search only."
                ),
            )
        return payload

    def _reason(self) -> str:
        if self.reason_code == "EMBEDDING_CREDENTIALS_MISSING":
            credential = self.credential_env or "the embedding provider credential"
            return f"{credential} is not configured"
        return _REASONS[self.reason_code]


@dataclass(frozen=True)
class RetrievalOutcome(Generic[T]):
    value: T
    status: RetrievalStatus


def _model(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("Embedding model must not be blank.")
    return normalized


def _provider(model: str, value: str | None) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized:
        return normalized
    inferred = model.split("/", 1)[0].strip().casefold()
    return inferred or "unknown"


def _default_credential_env(provider: str) -> str | None:
    return {
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }.get(provider)


def hybrid_status(model: str, *, provider: str | None = None) -> RetrievalStatus:
    resolved_model = _model(model)
    return RetrievalStatus(
        mode="hybrid_vector_lexical",
        model=resolved_model,
        available=True,
        provider=_provider(resolved_model, provider),
    )


def lexical_fallback_status(
    model: str,
    reason_code: EmbeddingReasonCode,
    *,
    provider: str | None = None,
    credential_env: str | None = None,
) -> RetrievalStatus:
    resolved_model = _model(model)
    resolved_provider = _provider(resolved_model, provider)
    return RetrievalStatus(
        mode="lexical_fallback",
        model=resolved_model,
        available=False,
        provider=resolved_provider,
        credential_env=(
            str(credential_env or "").strip()
            or _default_credential_env(resolved_provider)
        ),
        reason_code=reason_code,
    )


__all__ = [
    "EmbeddingReasonCode",
    "RetrievalMode",
    "RetrievalOutcome",
    "RetrievalStatus",
    "hybrid_status",
    "lexical_fallback_status",
]
