from __future__ import annotations

import pytest

from db_rag.catalog import (
    SchemaCatalog,
    SemanticCatalogUnavailableError,
    SemanticSchemaCatalog,
    UnavailableSemanticSchemaCatalog,
)


class _SemanticCollection:
    def __init__(
        self,
        documents: list[str],
        metadatas: list[dict[str, str]],
    ) -> None:
        self.documents = documents
        self.metadatas = metadatas
        self.calls: list[dict[str, object]] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        count = len(kwargs["query_embeddings"])
        return {
            "documents": [self.documents for _ in range(count)],
            "metadatas": [self.metadatas for _ in range(count)],
        }


class _FailingCollection:
    def query(self, **kwargs):
        del kwargs
        raise RuntimeError("collection unavailable")


class _EmbeddingBatch:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_query(self, queries: list[str]) -> list[list[float]]:
        self.calls.append(list(queries))
        return [
            [float(index), 0.5]
            for index, _query in enumerate(queries, start=1)
        ]


def _catalog_data() -> dict[str, object]:
    return {
        "tables": [
            {"table": "GHB_J", "text": "Glycohemoglobin laboratory"}
        ],
        "columns": [
            {
                "table": "GHB_J",
                "column": "LBXGH",
                "text": "Glycohemoglobin percent",
            }
        ],
    }


def test_base_catalog_preserves_exact_operations_and_falls_back_to_lexical() -> None:
    catalog = SchemaCatalog(
        _catalog_data(),
        default_source_id="nhanes-2017-2018",
    )

    outcome = catalog.search_many_with_status(["glycohemoglobin"], limit=5)

    assert outcome.status.mode == "lexical_fallback"
    assert outcome.status.reason_code == "EMBEDDING_CONFIGURATION_UNAVAILABLE"
    assert outcome.value[0][0].matched_by == ("lexical",)
    assert catalog.inspect_table("nhanes-2017-2018", "GHB_J")
    assert catalog.field_exists("GHB_J", "LBXGH")


def test_semantic_catalog_runs_vector_and_exact_lexical_retrieval() -> None:
    embedder = _EmbeddingBatch()
    tables = _SemanticCollection(
        ["Table: GHB_J"],
        [{"table": "GHB_J"}],
    )
    columns = _SemanticCollection(
        ["Glycohemoglobin percent"],
        [{"table": "GHB_J", "column": "LBXGH"}],
    )
    catalog = SemanticSchemaCatalog(
        _catalog_data(),
        table_collection=tables,
        column_collection=columns,
        embedding_function=embedder,
        default_source_id="nhanes-2017-2018",
    )

    hits = catalog.search("LBXGH", limit=5)

    assert embedder.calls == [["LBXGH"]]
    assert hits[0].column == "LBXGH"
    assert set(hits[0].matched_by) == {"vector", "lexical"}
    assert tables.calls[0]["query_embeddings"] is columns.calls[0][
        "query_embeddings"
    ]


def test_semantic_catalog_does_not_invent_a_lexical_match() -> None:
    catalog = SemanticSchemaCatalog(
        _catalog_data(),
        table_collection=_SemanticCollection([], []),
        column_collection=_SemanticCollection(
            ["Glycohemoglobin percent"],
            [{"table": "GHB_J", "column": "LBXGH"}],
        ),
        embedding_function=_EmbeddingBatch(),
        default_source_id="nhanes-2017-2018",
    )

    hits = catalog.search("completely unrelated terminology", limit=5)

    assert len(hits) == 1
    assert hits[0].matched_by == ("vector",)


def test_semantic_catalog_filters_rows_from_another_source() -> None:
    catalog = SemanticSchemaCatalog(
        _catalog_data(),
        table_collection=_SemanticCollection([], []),
        column_collection=_SemanticCollection(
            ["Foreign field", "Glycohemoglobin percent"],
            [
                {
                    "source": "report-india-synthetic",
                    "table": "FOREIGN",
                    "column": "OTHER",
                },
                {"table": "GHB_J", "column": "LBXGH"},
            ],
        ),
        embedding_function=_EmbeddingBatch(),
        default_source_id="nhanes-2017-2018",
    )

    hits = catalog.search("glycohemoglobin", limit=5)

    assert {(hit.source, hit.table, hit.column) for hit in hits} == {
        ("nhanes-2017-2018", "GHB_J", "LBXGH"),
        ("nhanes-2017-2018", "GHB_J", None),
    }


def test_semantic_catalog_falls_back_after_vector_failure() -> None:
    catalog = SemanticSchemaCatalog(
        _catalog_data(),
        table_collection=_FailingCollection(),
        column_collection=_FailingCollection(),
        embedding_function=_EmbeddingBatch(),
        default_source_id="nhanes-2017-2018",
    )

    outcome = catalog.search_many_with_status(["LBXGH"], limit=5)

    assert outcome.status.mode == "lexical_fallback"
    assert outcome.status.reason_code == "EMBEDDING_INDEX_UNAVAILABLE"
    assert outcome.value[0][0].column == "LBXGH"
    assert outcome.value[0][0].matched_by == ("lexical",)


def test_unavailable_semantic_catalog_allows_inspection_and_lexical_search() -> None:
    catalog = UnavailableSemanticSchemaCatalog(
        _catalog_data(),
        default_source_id="nhanes-2017-2018",
    )

    outcome = catalog.search_many_with_status(["LBXGH"], limit=5)

    assert catalog.inspect_table("nhanes-2017-2018", "GHB_J")
    assert outcome.status.mode == "lexical_fallback"
    assert outcome.status.reason_code == "EMBEDDING_CREDENTIALS_MISSING"
    assert outcome.value[0][0].column == "LBXGH"
