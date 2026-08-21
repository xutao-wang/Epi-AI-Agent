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
        "reason_code": "EMBEDDING_CREDENTIALS_MISSING",
        "message": (
            "Embedding model OpenAI/text-embedding-3-large is unavailable "
            "because OPENAI_API_KEY is not configured. Results use lexical "
            "string search only."
        ),
    }


def test_hybrid_status_contains_no_failure_reason() -> None:
    status = hybrid_status("OpenAI/text-embedding-3-large")

    assert status.mode == "hybrid_vector_lexical"
    assert status.as_dict() == {
        "available": True,
        "model": "OpenAI/text-embedding-3-large",
    }
    assert RetrievalOutcome(value=("hit",), status=status).value == ("hit",)
