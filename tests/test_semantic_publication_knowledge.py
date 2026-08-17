from __future__ import annotations

import pytest

import db_rag.local_knowledge as local_knowledge
from db_rag.knowledge import StudyEvidenceChunk
from db_rag.local_knowledge import LocalPublicationKnowledge


def _chunk(
    chunk_id: str,
    *,
    title: str,
    text: str,
    source_id: str = "doi:10.1000/example",
) -> StudyEvidenceChunk:
    return StudyEvidenceChunk(
        id=chunk_id,
        source_id=source_id,
        title=title,
        section="Eligibility",
        text=text,
        path="doi_10.1000_example.json",
        knowledge_type="eligibility",
        knowledge_role="historical_study_design_reference",
        indexed_path=f"design_reference.{chunk_id}",
    )


def _local() -> LocalPublicationKnowledge:
    chunks = (
        _chunk(
            "publication.vector-first",
            title="Renal outcomes",
            text="Kidney outcomes in the analytic population.",
        ),
        _chunk(
            "publication.lexical-first",
            title="Cohort eligibility",
            text="Cohort eligibility criteria and enrollment.",
        ),
    )
    return LocalPublicationKnowledge(
        chunks,
        {
            "cohort": 1,
            "eligibility": 2,
        },
    )


class _EmbeddingFunction:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_query(self, queries: list[str]) -> list[list[float]]:
        self.calls.append(list(queries))
        return [[0.25, 0.75] for _query in queries]


class _Collection:
    def __init__(self, chunks: tuple[StudyEvidenceChunk, ...]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, object]] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "ids": [[chunk.id for chunk in self.chunks]],
            "metadatas": [[chunk.chroma_metadata() for chunk in self.chunks]],
        }


class _FailingCollection:
    def query(self, **kwargs):
        del kwargs
        raise RuntimeError("collection unavailable")


def test_semantic_publication_search_requires_vector_and_boosts_lexical() -> None:
    local = _local()
    embedder = _EmbeddingFunction()
    collection = _Collection(local._chunks)
    knowledge = local_knowledge.SemanticPublicationKnowledge(
        local,
        collection=collection,
        embedding_function=embedder,
    )

    hits = knowledge.search("cohort eligibility", limit=2)

    assert embedder.calls == [["cohort eligibility"]]
    assert collection.calls[0]["query_embeddings"] == [[0.25, 0.75]]
    assert collection.calls[0]["where"] == {"source_kind": "publication"}
    assert hits[0].id == "publication.lexical-first"
    assert {hit.id for hit in hits} == {
        "publication.vector-first",
        "publication.lexical-first",
    }


def test_semantic_publication_search_never_falls_back_after_vector_failure() -> None:
    knowledge = local_knowledge.SemanticPublicationKnowledge(
        _local(),
        collection=_FailingCollection(),
        embedding_function=_EmbeddingFunction(),
    )

    with pytest.raises(
        local_knowledge.SemanticPublicationKnowledgeUnavailableError
    ):
        knowledge.search("cohort eligibility", limit=2)


def test_unavailable_semantic_publication_preserves_exact_source_opening() -> None:
    knowledge = local_knowledge.UnavailableSemanticPublicationKnowledge(_local())

    with pytest.raises(
        local_knowledge.SemanticPublicationKnowledgeUnavailableError
    ):
        knowledge.search("cohort eligibility", limit=2)

    hits = knowledge.open_source("doi:10.1000/example", limit=5)
    assert {hit.id for hit in hits} == {
        "publication.vector-first",
        "publication.lexical-first",
    }
