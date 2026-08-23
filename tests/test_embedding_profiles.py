from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from db_rag.embedding_profiles import (
    EmbeddingProfileRegistry,
    load_embedding_profile_registry,
    resolve_embedding_profile,
)


def _profile(
    profile_id: str = "openai-large",
    *,
    compatibility: str = "OpenAI/text-embedding-3-large",
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "id": profile_id,
        "label": "OpenAI text-embedding-3-large",
        "provider": "openai",
        "transport": "openai_embeddings",
        "model": "text-embedding-3-large",
        "index_compatibility": compatibility,
        "dimensions": 3072,
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "timeout_seconds": 10,
        "enabled": enabled,
    }


def _registry(*profiles: dict[str, object], default: str = "openai-large") -> dict[str, object]:
    return {"default_profile": default, "profiles": list(profiles or (_profile(),))}


def _write_registry(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "embedding_models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_tracked_embedding_registry_has_valid_enabled_default() -> None:
    registry = load_embedding_profile_registry(
        Path(__file__).resolve().parents[1] / "config" / "embedding_models.json"
    )

    selected = registry.profile_by_id(registry.default_profile)
    assert selected is not None
    assert selected.enabled is True
    assert selected.index_compatibility == "OpenAI/text-embedding-3-large"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["profiles"][0].update(extra="forbidden"),
        lambda payload: payload["profiles"][0].update(base_url="file:///tmp/model"),
        lambda payload: payload["profiles"][0].update(api_key_env="openai-key"),
        lambda payload: payload["profiles"][0].update(dimensions=0),
        lambda payload: payload["profiles"][0].update(dimensions="3072"),
        lambda payload: payload["profiles"][0].update(timeout_seconds=0),
        lambda payload: payload["profiles"][0].update(timeout_seconds="10"),
        lambda payload: payload["profiles"][0].update(enabled="true"),
        lambda payload: payload.update(default_profile="missing"),
    ],
)
def test_registry_rejects_invalid_cards_and_defaults(mutation) -> None:
    payload = _registry(_profile())
    mutation(payload)

    with pytest.raises(ValidationError):
        EmbeddingProfileRegistry.model_validate(payload)


def test_registry_rejects_duplicate_ids_and_compatibility_identities() -> None:
    duplicate_id = _registry(
        _profile("same"),
        _profile("same", compatibility="OpenAI/other"),
        default="same",
    )
    duplicate_compatibility = _registry(
        _profile("first"),
        _profile("second"),
        default="first",
    )

    with pytest.raises(ValidationError, match="profile IDs"):
        EmbeddingProfileRegistry.model_validate(duplicate_id)
    with pytest.raises(ValidationError, match="index compatibility"):
        EmbeddingProfileRegistry.model_validate(duplicate_compatibility)


def test_explicit_profile_takes_precedence_over_legacy_model(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        _registry(
            _profile("default"),
            _profile("selected", compatibility="OpenAI/selected"),
            default="default",
        ),
    )

    resolution = resolve_embedding_profile(
        {
            "DB_RAG_EMBEDDING_PROFILE": "selected",
            "DB_RAG_EMBEDDING_MODEL": "OpenAI/text-embedding-3-large",
        },
        registry_path=path,
    )

    assert resolution.profile is not None
    assert resolution.profile.id == "selected"
    assert resolution.reason_code is None


def test_legacy_model_maps_by_unique_index_identity(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, _registry(_profile()))

    resolution = resolve_embedding_profile(
        {"DB_RAG_EMBEDDING_MODEL": "OpenAI/text-embedding-3-large"},
        registry_path=path,
    )

    assert resolution.profile is not None
    assert resolution.profile.id == "openai-large"


def test_registry_default_is_used_only_without_explicit_selection(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, _registry(_profile()))

    resolution = resolve_embedding_profile({}, registry_path=path)

    assert resolution.profile is not None
    assert resolution.profile.id == "openai-large"


@pytest.mark.parametrize(
    ("environment", "reason_code"),
    [
        ({"DB_RAG_EMBEDDING_PROFILE": "unknown"}, "EMBEDDING_PROFILE_UNKNOWN"),
        ({"DB_RAG_EMBEDDING_PROFILE": "disabled"}, "EMBEDDING_PROFILE_DISABLED"),
        ({"DB_RAG_EMBEDDING_MODEL": "OpenAI/unknown"}, "EMBEDDING_PROFILE_UNKNOWN"),
    ],
)
def test_invalid_explicit_selection_never_substitutes_default(
    tmp_path: Path,
    environment: dict[str, str],
    reason_code: str,
) -> None:
    path = _write_registry(
        tmp_path,
        _registry(
            _profile(),
            _profile(
                "disabled",
                compatibility="OpenAI/disabled",
                enabled=False,
            ),
        ),
    )

    resolution = resolve_embedding_profile(environment, registry_path=path)

    assert resolution.profile is None
    assert resolution.profile_label == (
        "OpenAI text-embedding-3-large"
        if reason_code == "EMBEDDING_PROFILE_DISABLED"
        else "Configured embedding profile"
    )
    assert resolution.reason_code == reason_code


def test_malformed_registry_returns_safe_generic_resolution(tmp_path: Path) -> None:
    path = tmp_path / "embedding_models.json"
    secret_marker = "secret-provider-payload"
    path.write_text("{" + secret_marker, encoding="utf-8")

    resolution = resolve_embedding_profile({}, registry_path=path)
    serialized = repr(resolution)

    assert resolution.profile is None
    assert resolution.profile_id == "configured"
    assert resolution.profile_label == "Configured embedding profile"
    assert resolution.reason_code == "EMBEDDING_PROFILE_INVALID"
    assert secret_marker not in serialized
