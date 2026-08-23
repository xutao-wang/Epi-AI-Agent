from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import PROJECT_ROOT
from .retrieval_status import EmbeddingReasonCode


DEFAULT_EMBEDDING_REGISTRY_PATH = PROJECT_ROOT / "config" / "embedding_models.json"
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class EmbeddingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=64)
    transport: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    index_compatibility: str = Field(min_length=1, max_length=240)
    dimensions: int = Field(gt=0, le=65_536)
    base_url: str = Field(min_length=1, max_length=500)
    api_key_env: str = Field(min_length=1, max_length=128)
    timeout_seconds: float = Field(gt=0, le=120)
    enabled: bool

    @field_validator(
        "id",
        "label",
        "provider",
        "transport",
        "model",
        "index_compatibility",
        "base_url",
        "api_key_env",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("id", "provider", "transport", "model", "index_compatibility")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("must be a safe identifier")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("must not contain credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str) -> str:
        if not _ENVIRONMENT_NAME_PATTERN.fullmatch(value):
            raise ValueError("must be an uppercase environment variable name")
        return value


class EmbeddingProfileRegistry(BaseModel):
    # Keep container parsing JSON-friendly (JSON arrays become tuples), while each
    # profile card above remains type-strict.
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_profile: str = Field(min_length=1, max_length=128)
    profiles: tuple[EmbeddingProfile, ...] = Field(min_length=1)

    @field_validator("default_profile")
    @classmethod
    def strip_default_profile(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_registry(self) -> "EmbeddingProfileRegistry":
        profile_ids = [profile.id for profile in self.profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("embedding profile IDs must be unique")
        compatibilities = [profile.index_compatibility for profile in self.profiles]
        if len(compatibilities) != len(set(compatibilities)):
            raise ValueError("embedding index compatibility identities must be unique")
        default = self.profile_by_id(self.default_profile)
        if default is None:
            raise ValueError("default_profile must identify a registered profile")
        if not default.enabled:
            raise ValueError("default_profile must be enabled")
        return self

    def profile_by_id(self, profile_id: str) -> EmbeddingProfile | None:
        return next((profile for profile in self.profiles if profile.id == profile_id), None)

    def profile_by_compatibility(self, identity: str) -> EmbeddingProfile | None:
        return next(
            (
                profile
                for profile in self.profiles
                if profile.index_compatibility == identity
            ),
            None,
        )


@dataclass(frozen=True)
class EmbeddingProfileResolution:
    profile: EmbeddingProfile | None
    profile_id: str
    profile_label: str
    reason_code: EmbeddingReasonCode | None = None


def load_embedding_profile_registry(
    path: Path = DEFAULT_EMBEDDING_REGISTRY_PATH,
) -> EmbeddingProfileRegistry:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return EmbeddingProfileRegistry.model_validate(payload)


def _unavailable_resolution(
    reason_code: EmbeddingReasonCode,
    *,
    profile: EmbeddingProfile | None = None,
) -> EmbeddingProfileResolution:
    return EmbeddingProfileResolution(
        profile=None,
        profile_id=profile.id if profile is not None else "configured",
        profile_label=(
            profile.label if profile is not None else "Configured embedding profile"
        ),
        reason_code=reason_code,
    )


def resolve_embedding_profile(
    environ: Mapping[str, str],
    *,
    registry_path: Path = DEFAULT_EMBEDDING_REGISTRY_PATH,
) -> EmbeddingProfileResolution:
    try:
        registry = load_embedding_profile_registry(registry_path)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return _unavailable_resolution("EMBEDDING_PROFILE_INVALID")

    explicit_profile = str(
        environ.get("DB_RAG_EMBEDDING_PROFILE", "") or ""
    ).strip()
    legacy_model = str(environ.get("DB_RAG_EMBEDDING_MODEL", "") or "").strip()
    if explicit_profile:
        profile = registry.profile_by_id(explicit_profile)
    elif legacy_model:
        profile = registry.profile_by_compatibility(legacy_model)
    else:
        profile = registry.profile_by_id(registry.default_profile)

    if profile is None:
        return _unavailable_resolution("EMBEDDING_PROFILE_UNKNOWN")
    if not profile.enabled:
        return _unavailable_resolution(
            "EMBEDDING_PROFILE_DISABLED",
            profile=profile,
        )
    return EmbeddingProfileResolution(
        profile=profile,
        profile_id=profile.id,
        profile_label=profile.label,
    )


__all__ = [
    "DEFAULT_EMBEDDING_REGISTRY_PATH",
    "EmbeddingProfile",
    "EmbeddingProfileRegistry",
    "EmbeddingProfileResolution",
    "load_embedding_profile_registry",
    "resolve_embedding_profile",
]
