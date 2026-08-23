from __future__ import annotations

from db_rag.config import EMBEDDING_MODEL, resolve_db_rag_embedding_model
from db_rag.embedding_routes import resolve_embedding_route
from db_rag.embedding_profiles import EmbeddingProfile, EmbeddingProfileResolution
from db_rag.embedding_routes import resolve_profile_route


def test_openai_route_is_available_with_its_configured_credential() -> None:
    route = resolve_embedding_route(
        {"OPENAI_API_KEY": "embedding-key"},
        EMBEDDING_MODEL,
    )

    assert route.model == EMBEDDING_MODEL
    assert route.provider == "openai"
    assert route.credential_env == "OPENAI_API_KEY"
    assert route.available is True
    assert route.unavailable_reason_code is None
    assert "embedding-key" not in repr(route)


def test_openai_route_reports_missing_route_credential() -> None:
    route = resolve_embedding_route({}, EMBEDDING_MODEL)

    assert route.available is False
    assert route.unavailable_reason_code == "EMBEDDING_CREDENTIALS_MISSING"


def test_future_openrouter_qwen_route_degrades_when_adapter_is_unavailable() -> None:
    route = resolve_embedding_route(
        {"OPENROUTER_API_KEY": "router-key"},
        "OpenRouter/Qwen/qwen3-embedding-8b",
    )

    assert route.model == "OpenRouter/Qwen/qwen3-embedding-8b"
    assert route.provider == "openrouter"
    assert route.credential_env == "OPENROUTER_API_KEY"
    assert route.available is False
    assert route.unavailable_reason_code == "EMBEDDING_ROUTE_UNAVAILABLE"
    assert "router-key" not in repr(route)


def test_future_embedding_model_configuration_reaches_route_resolution() -> None:
    model = "OpenRouter/Qwen/qwen3-embedding-8b"

    assert resolve_db_rag_embedding_model(
        {"DB_RAG_EMBEDDING_MODEL": model}
    ) == model


def test_profile_route_uses_only_registered_transport_adapter() -> None:
    profile = EmbeddingProfile(
        id="qwen-profile",
        label="Qwen embedding",
        provider="qwen",
        transport="qwen_embeddings",
        model="qwen3-embedding-8b",
        index_compatibility="Qwen/qwen3-embedding-8b",
        dimensions=4096,
        base_url="https://qwen.example/v1",
        api_key_env="QWEN_API_KEY",
        timeout_seconds=10,
        enabled=True,
    )
    resolution = EmbeddingProfileResolution(
        profile=profile,
        profile_id=profile.id,
        profile_label=profile.label,
    )

    route = resolve_profile_route(
        {"QWEN_API_KEY": "qwen-secret"},
        resolution,
        adapters={},
    )

    assert route.model == "Qwen/qwen3-embedding-8b"
    assert route.profile_id == "qwen-profile"
    assert route.profile_label == "Qwen embedding"
    assert route.available is False
    assert route.unavailable_reason_code == "EMBEDDING_TRANSPORT_UNAVAILABLE"
    assert "qwen-secret" not in repr(route)
