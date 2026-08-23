from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import logging
import math
from numbers import Real
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .embedding_profiles import (
    DEFAULT_EMBEDDING_REGISTRY_PATH,
    resolve_embedding_profile,
)
from .embedding_routes import (
    EmbeddingAdapterFactory,
    EmbeddingRoute,
    resolve_profile_route,
)
from .retrieval_status import EmbeddingReasonCode, RetrievalMode


_LOGGER = logging.getLogger(__name__)
_PROBE_TEXT = "Epi Agent embedding startup probe"


class EmbeddingStartupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(min_length=1, max_length=128)
    profile_label: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=64)
    index_compatibility: str = Field(default="", max_length=240)
    available: bool
    retrieval_mode: RetrievalMode
    reason_code: EmbeddingReasonCode | None = None
    message: str = Field(default="", max_length=2_000)
    compatible_study_ids: tuple[str, ...] = ()
    incompatible_study_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmbeddingStartupResult:
    route: EmbeddingRoute
    status: EmbeddingStartupStatus


def _safe_cause(reason_code: EmbeddingReasonCode) -> str:
    if reason_code in {
        "EMBEDDING_PROFILE_INVALID",
        "EMBEDDING_PROFILE_UNKNOWN",
        "EMBEDDING_PROFILE_DISABLED",
        "EMBEDDING_CREDENTIALS_MISSING",
        "EMBEDDING_CONFIGURATION_UNAVAILABLE",
    }:
        return "is not configured"
    if reason_code == "EMBEDDING_TRANSPORT_UNAVAILABLE":
        return "does not have a supported transport"
    if reason_code in {
        "EMBEDDING_RESPONSE_INVALID",
        "EMBEDDING_DIMENSION_MISMATCH",
    }:
        return "returned an incompatible response"
    return "cannot be reached"


def _fallback_status(
    route: EmbeddingRoute,
    reason_code: EmbeddingReasonCode,
) -> EmbeddingStartupStatus:
    label = route.profile_label or "Configured embedding profile"
    message = (
        "Semantic embedding search is unavailable. "
        f"({label} {_safe_cause(reason_code)}.) Catalog, publication, and "
        "study-design searches will use lexical matching only."
    )
    return EmbeddingStartupStatus(
        profile_id=route.profile_id or "configured",
        profile_label=label,
        provider=route.provider or "unknown",
        index_compatibility=route.model if route.profile is not None else "",
        available=False,
        retrieval_mode="lexical_fallback",
        reason_code=reason_code,
        message=message,
    )


def _hybrid_status(route: EmbeddingRoute) -> EmbeddingStartupStatus:
    return EmbeddingStartupStatus(
        profile_id=route.profile_id,
        profile_label=route.profile_label,
        provider=route.provider,
        index_compatibility=route.model,
        available=True,
        retrieval_mode="hybrid_vector_lexical",
    )


def _probe_reason(vectors: Any, dimensions: int) -> EmbeddingReasonCode | None:
    if not isinstance(vectors, list) or len(vectors) != 1:
        return "EMBEDDING_RESPONSE_INVALID"
    vector = vectors[0]
    if not isinstance(vector, (list, tuple)):
        return "EMBEDDING_RESPONSE_INVALID"
    if len(vector) != dimensions:
        return "EMBEDDING_DIMENSION_MISMATCH"
    if any(
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        for value in vector
    ):
        return "EMBEDDING_RESPONSE_INVALID"
    return None


def _is_timeout(error: Exception) -> bool:
    return isinstance(error, TimeoutError) or "timeout" in type(error).__name__.casefold()


def initialize_embedding(
    environ: Mapping[str, str],
    *,
    registry_path: Path = DEFAULT_EMBEDDING_REGISTRY_PATH,
    adapters: Mapping[str, EmbeddingAdapterFactory] | None = None,
) -> EmbeddingStartupResult:
    resolution = resolve_embedding_profile(environ, registry_path=registry_path)
    route = resolve_profile_route(environ, resolution, adapters=adapters)
    if not route.available:
        reason = route.unavailable_reason_code or "EMBEDDING_CONFIGURATION_UNAVAILABLE"
        return EmbeddingStartupResult(route=route, status=_fallback_status(route, reason))

    try:
        vectors = route.create_embedding_function().embed_query([_PROBE_TEXT])
    except Exception as error:
        reason = (
            "EMBEDDING_PROBE_TIMEOUT"
            if _is_timeout(error)
            else "EMBEDDING_PROVIDER_UNAVAILABLE"
        )
    else:
        reason = _probe_reason(vectors, route.dimensions)

    if reason is not None:
        unavailable_route = replace(route, unavailable_reason_code=reason)
        _LOGGER.info(
            "Embedding startup probe unavailable profile_id=%s reason_code=%s",
            route.profile_id,
            reason,
        )
        return EmbeddingStartupResult(
            route=unavailable_route,
            status=_fallback_status(unavailable_route, reason),
        )
    _LOGGER.info(
        "Embedding startup probe completed profile_id=%s",
        route.profile_id,
    )
    return EmbeddingStartupResult(route=route, status=_hybrid_status(route))


__all__ = [
    "EmbeddingStartupResult",
    "EmbeddingStartupStatus",
    "initialize_embedding",
]
