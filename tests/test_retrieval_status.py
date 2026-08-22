from __future__ import annotations

from db_rag.retrieval_status import (
    RetrievalOutcome,
    hybrid_status,
    lexical_fallback_status,
)


def test_missing_credentials_status_is_explicit_and_sanitized() -> None:
    status = lexical_fallback_status(
        "OpenAI/text-embedding-3-large",
        "EMBEDDING_CREDENTIALS_MISSING",
    )

    assert status.mode == "lexical_fallback"
    assert status.as_dict() == {
        "available": False,
        "model": "OpenAI/text-embedding-3-large",
        "provider": "openai",
        "reason_code": "EMBEDDING_CREDENTIALS_MISSING",
        "message": (
            "Embedding model OpenAI/text-embedding-3-large via openai is "
            "unavailable because OPENAI_API_KEY is not configured. Results "
            "use lexical string search only."
        ),
    }


def test_hybrid_status_contains_no_failure_reason() -> None:
    status = hybrid_status("OpenAI/text-embedding-3-large")

    assert status.mode == "hybrid_vector_lexical"
    assert status.as_dict() == {
        "available": True,
        "model": "OpenAI/text-embedding-3-large",
        "provider": "openai",
    }
    assert RetrievalOutcome(value=("hit",), status=status).value == ("hit",)


def test_unavailable_future_route_message_is_provider_aware() -> None:
    status = lexical_fallback_status(
        "OpenRouter/Qwen/qwen3-embedding-8b",
        "EMBEDDING_ROUTE_UNAVAILABLE",
        provider="openrouter",
        credential_env="OPENROUTER_API_KEY",
    )

    assert status.as_dict() == {
        "available": False,
        "model": "OpenRouter/Qwen/qwen3-embedding-8b",
        "provider": "openrouter",
        "reason_code": "EMBEDDING_ROUTE_UNAVAILABLE",
        "message": (
            "Embedding model OpenRouter/Qwen/qwen3-embedding-8b via openrouter "
            "is unavailable because no embedding adapter is available for "
            "this route. Results use lexical string search only."
        ),
    }
