from __future__ import annotations

import pytest

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


def test_publication_tool_translates_semantic_unavailability() -> None:
    context = ToolContext(
        study=StudyBundle(
            study_id="report-india-synthetic",
            label="RePORT India Synthetic",
            knowledge=_UnavailableKnowledge(),
            catalog=None,
            data_sources={},
        ),
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=object(),
    )

    with pytest.raises(ToolExecutionError) as raised:
        build_publication_tool_registry(include_pubmed=False).invoke(
            "publication-search_study_evidence",
            {"query": "cohort eligibility", "limit": 5},
            context=context,
        )

    assert raised.value.code == "SEMANTIC_STUDY_KNOWLEDGE_UNAVAILABLE"
    assert raised.value.recoverable is True
