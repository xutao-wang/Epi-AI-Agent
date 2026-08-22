from __future__ import annotations

from db_rag.config import EMBEDDING_MODEL
from db_rag.embedding_routes import resolve_embedding_route


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
