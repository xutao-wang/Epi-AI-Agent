from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar


T = TypeVar("T")
RetrievalMode = Literal["hybrid_vector_lexical", "lexical_fallback"]
EmbeddingReasonCode = Literal[
    "EMBEDDING_CREDENTIALS_MISSING",
    "EMBEDDING_CONFIGURATION_UNAVAILABLE",
    "EMBEDDING_INDEX_UNAVAILABLE",
    "EMBEDDING_PROVIDER_UNAVAILABLE",
]

_REASONS: dict[EmbeddingReasonCode, str] = {
    "EMBEDDING_CREDENTIALS_MISSING": "OPENAI_API_KEY is not configured",
    "EMBEDDING_CONFIGURATION_UNAVAILABLE": (
        "its configuration is unavailable or incompatible"
    ),
    "EMBEDDING_INDEX_UNAVAILABLE": "the semantic index is unavailable",
    "EMBEDDING_PROVIDER_UNAVAILABLE": (
        "the embedding provider could not complete the query"
    ),
}


@dataclass(frozen=True)
class RetrievalStatus:
    mode: RetrievalMode
    model: str
    available: bool
    reason_code: EmbeddingReasonCode | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "available": self.available,
            "model": self.model,
        }
        if self.reason_code is not None:
            payload.update(
                reason_code=self.reason_code,
                message=(
                    f"Embedding model {self.model} is unavailable because "
                    f"{_REASONS[self.reason_code]}. Results use lexical string "
                    "search only."
                ),
            )
        return payload


@dataclass(frozen=True)
class RetrievalOutcome(Generic[T]):
    value: T
    status: RetrievalStatus


def _model(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("Embedding model must not be blank.")
    return normalized


def hybrid_status(model: str) -> RetrievalStatus:
    return RetrievalStatus(
        mode="hybrid_vector_lexical",
        model=_model(model),
        available=True,
    )


def lexical_fallback_status(
    model: str,
    reason_code: EmbeddingReasonCode,
) -> RetrievalStatus:
    return RetrievalStatus(
        mode="lexical_fallback",
        model=_model(model),
        available=False,
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
