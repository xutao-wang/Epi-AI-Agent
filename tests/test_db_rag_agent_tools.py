from __future__ import annotations

import pytest

from db_rag.catalog import (
    SchemaEvidenceHit,
    SemanticCatalogUnavailableError,
)
from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle


class _HybridCatalog:
    def search_many(self, queries: list[str], *, limit: int):
        del limit
        return [
            [
                SchemaEvidenceHit(
                    source="nhanes-2017-2018",
                    table="GHB_J",
                    column="LBXGH",
                    text="Glycohemoglobin percent",
                    provenance={
                        "authority": "runtime_schema_catalog",
                        "source_id": "nhanes-2017-2018",
                        "table": "GHB_J",
                        "column": "LBXGH",
                    },
                    matched_by=("vector", "lexical"),
                ),
                SchemaEvidenceHit(
                    source="nhanes-2017-2018",
                    table="GHB_J",
                    text="Glycohemoglobin laboratory",
                    provenance={
                        "authority": "runtime_schema_catalog",
                        "source_id": "nhanes-2017-2018",
                        "table": "GHB_J",
                    },
                    matched_by=("vector",),
                ),
            ]
            for _query in queries
        ]


class _UnavailableCatalog:
    def search_many(self, queries: list[str], *, limit: int):
        del queries, limit
        raise SemanticCatalogUnavailableError("semantic retrieval failed")


def _context(catalog) -> ToolContext:
    return ToolContext(
        study=StudyBundle(
            study_id="nhanes-2017-2018",
            label="NHANES 2017-2018",
            knowledge=None,
            catalog=catalog,
            data_sources={"nhanes-2017-2018": object()},
            source_id="nhanes-2017-2018",
        ),
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=object(),
    )


def test_catalog_tool_persists_hybrid_retrieval_provenance() -> None:
    context = _context(_HybridCatalog())

    result = build_db_rag_tool_registry().invoke(
        "dbrag-search_catalog",
        {"queries": ["glycemic control"], "limit": 5},
        context=context,
    )

    observation = context.artifact_store.require(result.artifacts[0]).content
    assert observation["retrieval_mode"] == "hybrid_vector_lexical"
    assert observation["retrieval_summary"]["vector_hits"] == 2
    assert observation["retrieval_summary"]["lexical_hits"] == 1
    assert observation["hits"][0]["matched_by"] == ["vector", "lexical"]


def test_catalog_tool_translates_semantic_unavailability() -> None:
    context = _context(_UnavailableCatalog())

    with pytest.raises(ToolExecutionError) as raised:
        build_db_rag_tool_registry().invoke(
            "dbrag-search_catalog",
            {"queries": ["glycemic control"], "limit": 5},
            context=context,
        )

    assert raised.value.code == "SEMANTIC_CATALOG_UNAVAILABLE"
    assert raised.value.recoverable is True
