from __future__ import annotations

import json

import pytest

from db_rag.catalog import (
    SchemaEvidenceHit,
    SemanticCatalogUnavailableError,
)
from db_rag.retrieval_status import RetrievalOutcome, lexical_fallback_status
from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import (
    ToolContext,
    ToolExecutionError,
    serialize_tool_result,
)
from epi_agent.studies import StudyBundle, StudyRegistry


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


class _FallbackCatalog(_HybridCatalog):
    embedding_model = "OpenAI/text-embedding-3-large"

    def search_many_with_status(self, queries: list[str], *, limit: int):
        return RetrievalOutcome(
            value=super().search_many(queries, limit=limit),
            status=lexical_fallback_status(
                self.embedding_model,
                "EMBEDDING_CREDENTIALS_MISSING",
            ),
        )


class _ManyHitCatalog:
    def search_many(self, queries: list[str], *, limit: int):
        return [
            [
                SchemaEvidenceHit(
                    source="nhanes-2017-2018",
                    table=f"TABLE_{probe_index}_{hit_index}",
                    column=f"FIELD_{probe_index}_{hit_index}",
                    text=f"Evidence {probe_index}-{hit_index}",
                    provenance={
                        "authority": "runtime_schema_catalog",
                        "source_id": "nhanes-2017-2018",
                        "table": f"TABLE_{probe_index}_{hit_index}",
                        "column": f"FIELD_{probe_index}_{hit_index}",
                    },
                    matched_by=("vector", "lexical"),
                )
                for hit_index in range(limit)
            ]
            for probe_index, _query in enumerate(queries)
        ]


class _CatalogWithEmptyProbe(_ManyHitCatalog):
    def search_many(self, queries: list[str], *, limit: int):
        batches = super().search_many(queries, limit=limit)
        batches[1] = []
        return batches


class _InspectableCatalog(_HybridCatalog):
    def inspect_table(
        self,
        source: str,
        table: str,
        *,
        offset: int = 0,
        limit: int = 25,
    ):
        fields = [
            SchemaEvidenceHit(
                source=source,
                table=table,
                column=f"FIELD_{index:02d}",
                text=f"Annotated field {index}",
                provenance={
                    "authority": "runtime_schema_catalog",
                    "source_id": source,
                    "table": table,
                    "column": f"FIELD_{index:02d}",
                },
            )
            for index in range(30)
        ]
        return fields[offset : offset + limit]


def _context(catalog) -> ToolContext:
    return ToolContext(
        studies=StudyRegistry(
            [
                StudyBundle(
                    study_id="nhanes-2017-2018",
                    label="NHANES 2017-2018",
                    knowledge=None,
                    catalog=catalog,
                    data_sources={"nhanes-2017-2018": object()},
                    source_id="nhanes-2017-2018",
                )
            ]
        ),
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=object(),
    )


def test_catalog_tool_persists_hybrid_retrieval_provenance() -> None:
    context = _context(_HybridCatalog())

    result = build_db_rag_tool_registry().invoke(
        "dbrag-search_catalog",
        {
            "study_id": "nhanes-2017-2018",
            "queries": ["glycemic control"],
            "limit": 5,
        },
        context=context,
    )

    observation = context.artifact_store.require(result.artifacts[0]).content
    assert observation["retrieval_mode"] == "hybrid_vector_lexical"
    assert observation["retrieval_summary"]["vector_hits"] == 2
    assert observation["retrieval_summary"]["lexical_hits"] == 1
    assert observation["probes"][0]["hits"][0]["matched_by"] == [
        "vector",
        "lexical",
    ]


def test_catalog_tool_persists_lexical_fallback_reason() -> None:
    context = _context(_FallbackCatalog())

    result = build_db_rag_tool_registry().invoke(
        "dbrag-search_catalog",
        {
            "study_id": "nhanes-2017-2018",
            "queries": ["glycemic control"],
            "limit": 5,
        },
        context=context,
    )

    observation = context.artifact_store.require(result.artifacts[0]).content
    assert observation["retrieval_mode"] == "lexical_fallback"
    assert observation["embedding"]["available"] is False
    assert observation["embedding"]["reason_code"] == (
        "EMBEDDING_CREDENTIALS_MISSING"
    )
    assert "OPENAI_API_KEY is not configured" in observation["embedding"][
        "message"
    ]


def test_catalog_tool_preserves_ten_hits_for_each_of_five_probes() -> None:
    context = _context(_ManyHitCatalog())
    queries = [f"probe-{index}" for index in range(5)]

    result = build_db_rag_tool_registry().invoke(
        "dbrag-search_catalog",
        {"study_id": "nhanes-2017-2018", "queries": queries, "limit": 10},
        context=context,
    )

    observation = context.artifact_store.require(result.artifacts[0]).content
    assert "hits" not in observation
    assert [probe["query"] for probe in observation["probes"]] == queries
    assert [probe["returned_count"] for probe in observation["probes"]] == [
        10
    ] * 5
    assert sum(len(probe["hits"]) for probe in observation["probes"]) == 50
    assert observation["probes"][-1]["hits"][-1]["column"] == "FIELD_4_9"

    model_message = json.loads(result.message)
    assert [probe["query"] for probe in model_message["probes"]] == queries
    assert sum(len(probe["hits"]) for probe in model_message["probes"]) == 50


def test_catalog_tool_preserves_zero_hit_probe_in_original_position() -> None:
    context = _context(_CatalogWithEmptyProbe())

    result = build_db_rag_tool_registry().invoke(
        "dbrag-search_catalog",
        {
            "study_id": "nhanes-2017-2018",
            "queries": ["first", "empty", "third"],
            "limit": 2,
        },
        context=context,
    )

    observation = context.artifact_store.require(result.artifacts[0]).content
    assert observation["probes"][1] == {
        "query": "empty",
        "returned_count": 0,
        "table_hits": 0,
        "column_hits": 0,
        "unique_table_count": 0,
        "unique_column_count": 0,
        "hits": [],
    }


def test_catalog_tool_translates_semantic_unavailability() -> None:
    context = _context(_UnavailableCatalog())

    with pytest.raises(ToolExecutionError) as raised:
        build_db_rag_tool_registry().invoke(
            "dbrag-search_catalog",
            {
                "study_id": "nhanes-2017-2018",
                "queries": ["glycemic control"],
                "limit": 5,
            },
            context=context,
        )

    assert raised.value.code == "SEMANTIC_CATALOG_UNAVAILABLE"
    assert raised.value.recoverable is True


def test_inspect_table_returns_explicit_next_page_metadata() -> None:
    context = _context(_InspectableCatalog())

    result = build_db_rag_tool_registry().invoke(
        "dbrag-inspect_table",
        {
            "table_ref": {
                "study_id": "nhanes-2017-2018",
                "source_id": "nhanes-2017-2018",
                "table": "DEMO_J",
            },
            "offset": 0,
            "limit": 25,
        },
        context=context,
    )

    message = json.loads(result.message)
    assert message["returned_count"] == 25
    assert message["has_more"] is True
    assert message["next_offset"] == 25
    assert len(message["fields"]) == 25
    assert set(message["fields"][0]) == {
        "column",
        "text",
        "source_kind",
        "field_ref",
    }


def test_inspect_table_final_page_includes_null_next_offset() -> None:
    context = _context(_InspectableCatalog())

    result = build_db_rag_tool_registry().invoke(
        "dbrag-inspect_table",
        {
            "table_ref": {
                "study_id": "nhanes-2017-2018",
                "source_id": "nhanes-2017-2018",
                "table": "DEMO_J",
            },
            "offset": 25,
            "limit": 25,
        },
        context=context,
    )

    message = json.loads(result.message)
    assert message["returned_count"] == 5
    assert message["has_more"] is False
    assert "next_offset" in message
    assert message["next_offset"] is None
    assert [field["column"] for field in message["fields"]] == [
        f"FIELD_{index:02d}" for index in range(25, 30)
    ]


def test_maximum_catalog_message_survives_protocol_serialization() -> None:
    context = _context(_ManyHitCatalog())
    result = build_db_rag_tool_registry().invoke(
        "dbrag-search_catalog",
        {
            "study_id": "nhanes-2017-2018",
            "queries": [f"probe-{index}" for index in range(5)],
            "limit": 10,
        },
        context=context,
    )

    outer = json.loads(serialize_tool_result(result))
    message = json.loads(outer["message"])

    assert "code" not in message
    assert len(message["probes"]) == 5
    assert sum(len(probe["hits"]) for probe in message["probes"]) == 50


def test_maximum_inspection_message_survives_protocol_serialization() -> None:
    context = _context(_InspectableCatalog())
    result = build_db_rag_tool_registry().invoke(
        "dbrag-inspect_table",
        {
            "table_ref": {
                "study_id": "nhanes-2017-2018",
                "source_id": "nhanes-2017-2018",
                "table": "DEMO_J",
            },
            "offset": 0,
            "limit": 25,
        },
        context=context,
    )

    outer = json.loads(serialize_tool_result(result))
    message = json.loads(outer["message"])

    assert "code" not in message
    assert message["returned_count"] == 25
    assert message["has_more"] is True
    assert message["next_offset"] == 25
    assert len(message["fields"]) == 25
