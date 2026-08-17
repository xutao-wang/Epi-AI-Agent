from __future__ import annotations

import pytest

from db_rag.knowledge import PublicationEvidenceHit
from db_rag.local_knowledge import SemanticPublicationKnowledgeUnavailableError
from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle
from epi_agent.tool_packs.publication import build_publication_tool_registry


class _UnavailableKnowledge:
    def search(self, query: str, *, limit: int = 5):
        del query, limit
        raise SemanticPublicationKnowledgeUnavailableError(
            "semantic publication retrieval failed"
        )


class _HybridKnowledge:
    def search(self, query: str, *, limit: int = 5):
        del query, limit
        return [
            PublicationEvidenceHit(
                id="publication.verified",
                source_id="doi:10.1000/example",
                title="Example publication",
                section="Eligibility",
                text="Verified eligibility evidence.",
                path="doi_10.1000_example.json",
                provenance={
                    "authority": "publication_knowledge",
                    "matched_by": "vector,lexical",
                },
            )
        ]


def _context(knowledge) -> ToolContext:
    return ToolContext(
        study=StudyBundle(
            study_id="report-india-synthetic",
            label="RePORT India Synthetic",
            knowledge=knowledge,
            catalog=None,
            data_sources={},
        ),
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=object(),
    )


def test_publication_tool_persists_hybrid_retrieval_provenance() -> None:
    context = _context(_HybridKnowledge())

    result = build_publication_tool_registry(include_pubmed=False).invoke(
        "publication-search_study_evidence",
        {"query": "cohort eligibility", "limit": 5},
        context=context,
    )

    observation = context.artifact_store.require(result.artifacts[0]).content
    assert observation["retrieval_mode"] == "hybrid_vector_lexical"
    assert observation["hits"][0]["matched_by"] == "vector,lexical"


def test_publication_tool_translates_semantic_unavailability() -> None:
    context = _context(_UnavailableKnowledge())

    with pytest.raises(ToolExecutionError) as raised:
        build_publication_tool_registry(include_pubmed=False).invoke(
            "publication-search_study_evidence",
            {"query": "cohort eligibility", "limit": 5},
            context=context,
        )

    assert raised.value.code == "SEMANTIC_STUDY_KNOWLEDGE_UNAVAILABLE"
    assert raised.value.recoverable is True
