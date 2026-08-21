from __future__ import annotations

from db_rag.config import EMBEDDING_MODEL
from db_rag.readiness import DbRagReadiness
from epi_agent.studies import StudyBundle, StudyRegistry


def test_missing_embedding_key_keeps_lexical_db_rag_available(monkeypatch) -> None:
    import api.app as app_module

    monkeypatch.setattr(
        app_module,
        "resolve_db_rag_readiness",
        lambda **_kwargs: DbRagReadiness(
            status="available",
            message="DB-RAG dataset is available.",
        ),
    )
    studies = StudyRegistry(
        [
            StudyBundle(
                study_id="study-1",
                label="Study One",
                knowledge=None,
                catalog=None,
                data_sources={},
                db_rag_paths=object(),
            )
        ]
    )

    readiness = app_module._db_rag_readiness(
        studies,
        embedding_model=EMBEDDING_MODEL,
        embedding_api_key="",
    )

    assert readiness.available is True
    assert "lexical fallback" in readiness.message
    assert "OPENAI_API_KEY is not configured" in readiness.message
