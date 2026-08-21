from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from db_rag import vectorstore
from db_rag.catalog_relationships import CatalogRelationshipSpec
from db_rag.catalog import (
    SchemaCatalog,
    SemanticSchemaCatalog,
    build_full_schema_catalog,
)
from db_rag.knowledge import (
    StudyEvidenceChunk,
    parse_study_evidence,
)
from db_rag.study import DuckDbStudyDataSource


class _Collection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def query(self, *, query_embeddings, n_results, include):
        del include
        rows = self.rows[:n_results]
        count = len(query_embeddings)
        return {
            "ids": [[str(row["id"]) for row in rows] for _ in range(count)],
            "documents": [
                [str(row["document"]) for row in rows]
                for _ in range(count)
            ],
            "metadatas": [
                [dict(row["metadata"]) for row in rows]
                for _ in range(count)
            ],
        }


class _EmbeddingFunction:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_query(self, queries: list[str]) -> list[list[float]]:
        self.calls.append(list(queries))
        return [[float(index)] for index, _query in enumerate(queries)]


def _publication_chunk(
    *,
    text: str = "Mortality risk-factor design reference.",
) -> StudyEvidenceChunk:
    return StudyEvidenceChunk(
        id="publication.example",
        source_id="doi:10.1000/example",
        title="Example publication",
        section="Retrieval summary",
        text=text,
        path="doi_10.1000_example.json",
        knowledge_type="retrieval_summary",
        knowledge_role="historical_study_design_reference",
        indexed_path="retrieval_summary",
    )


def test_duckdb_study_source_caches_relationship_inventory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inventory = object()
    builds: list[tuple[Path, CatalogRelationshipSpec]] = []

    def fake_build(path: Path, *, relationship_spec):
        builds.append((path, relationship_spec))
        return inventory

    monkeypatch.setattr("db_rag.study.build_relationship_inventory", fake_build)
    database = tmp_path / "report.duckdb"
    database.touch()
    specification = CatalogRelationshipSpec(
        table_keys={"participants": {"participant_key": "PERSON_TOKEN"}},
        relationships=(),
    )
    source = DuckDbStudyDataSource(
        database,
        relationship_spec=specification,
    )

    assert source.relationship_inventory() is inventory
    assert source.relationship_inventory() is inventory
    assert builds == [(database, specification)]


def test_markdown_publications_are_not_parsed(tmp_path: Path) -> None:
    nested = tmp_path / "papers"
    nested.mkdir()
    (nested / "study.md").write_text(
        "# Main findings\n\nFirst result.\n\n## Mortality\n\nSecond result.\n",
        encoding="utf-8",
    )

    assert parse_study_evidence(tmp_path) == []


def test_schema_catalog_chroma_search_interleaves_table_and_column_hits() -> None:
    catalog = SemanticSchemaCatalog(
        {
            "tables": [{"table": "screening", "text": "Screening table"}],
            "columns": [
                {
                    "table": "screening",
                    "column": "AGE",
                    "text": "Age column",
                }
            ],
        },
        table_collection=_Collection(
            [
                {
                    "id": "screening.summary",
                    "document": "Screening table",
                    "metadata": {"table": "screening"},
                },
                {
                    "id": "visits.summary",
                    "document": "Visits table",
                    "metadata": {"table": "visits"},
                },
            ]
        ),
        column_collection=_Collection(
            [
                {
                    "id": "screening.AGE",
                    "document": "Age column",
                    "metadata": {"table": "screening", "column": "AGE"},
                },
                {
                    "id": "visits.DATE",
                    "document": "Visit date column",
                    "metadata": {"table": "visits", "column": "DATE"},
                },
            ]
        ),
        embedding_function=_EmbeddingFunction(),
    )

    hits = catalog.search("screening age", limit=2)

    assert len(hits) == 2
    assert [hit.column for hit in hits] == [None, "AGE"]
    assert all(hit.source_kind == "schema" for hit in hits)


def test_schema_catalog_batches_multiple_semantic_probes() -> None:
    class _BatchCollection:
        def __init__(self, *, column: bool) -> None:
            self.column = column
            self.calls: list[list[str]] = []

        def query(self, *, query_embeddings, n_results, include):
            del include
            embeddings = list(query_embeddings)
            self.calls.append(embeddings)
            return {
                "documents": [
                    [f"query {index} result"][:n_results]
                    for index, _embedding in enumerate(embeddings)
                ],
                "metadatas": [
                    [
                        {
                            "source": "clinical_db",
                            "table": f"table_{index}",
                            **(
                                {"column": f"FIELD_{index}"}
                                if self.column
                                else {}
                            ),
                        }
                    ][:n_results]
                        for index, _embedding in enumerate(embeddings)
                    ],
                }

    tables = _BatchCollection(column=False)
    columns = _BatchCollection(column=True)
    embedder = _EmbeddingFunction()
    catalog = SemanticSchemaCatalog(
        {"tables": [], "columns": []},
        table_collection=tables,
        column_collection=columns,
        embedding_function=embedder,
        default_source_id="clinical_db",
    )

    batches = catalog.search_many(
        ["outcome", "household age", "household sex"],
        limit=2,
    )

    assert len(batches) == 3
    assert all(len(batch) == 2 for batch in batches)
    assert embedder.calls == [["outcome", "household age", "household sex"]]
    assert len(tables.calls) == 1
    assert len(columns.calls) == 1


def test_schema_catalog_inspects_exact_table_without_semantic_query() -> None:
    catalog = SchemaCatalog(
        {
            "tables": [
                {"table": "screening", "text": "Screening table"},
                {"table": "outcomes", "text": "Outcome table"},
            ],
            "columns": [
                {
                    "table": "screening",
                    "column": "AGE",
                    "text": "Age column",
                },
                {
                    "table": "screening",
                    "column": "SEX",
                    "text": "Sex column",
                },
                {
                    "table": "outcomes",
                    "column": "STATUS",
                    "text": "Outcome status",
                },
            ],
        },
        default_source_id="clinical_db",
    )

    hits = catalog.inspect_table(
        "clinical_db",
        "screening",
        offset=0,
        limit=10,
    )

    assert [(hit.table, hit.column) for hit in hits] == [
        ("screening", "AGE"),
        ("screening", "SEX"),
    ]
    assert all(hit.source == "clinical_db" for hit in hits)


def test_schema_catalog_exact_inspection_is_source_aware_and_pageable() -> None:
    catalog = SchemaCatalog(
        {
            "tables": [],
            "columns": [
                {
                    "source": source,
                    "table": "shared_table",
                    "column": f"FIELD_{index:02d}",
                    "text": f"Field {index}",
                }
                for source in ("clinical_db", "outcomes_db")
                for index in range(30)
            ],
        }
    )

    first_page = catalog.inspect_table(
        "outcomes_db",
        "shared_table",
        offset=0,
        limit=25,
    )
    second_page = catalog.inspect_table(
        "outcomes_db",
        "shared_table",
        offset=25,
        limit=25,
    )

    assert len(first_page) == 25
    assert len(second_page) == 5
    assert first_page[0].column == "FIELD_00"
    assert second_page[0].column == "FIELD_25"
    assert all(hit.source == "outcomes_db" for hit in [*first_page, *second_page])


def test_full_catalog_retains_table_profile_metadata() -> None:
    catalog = build_full_schema_catalog(
        table_chunks=[
            {
                "id": "screening.summary",
                "text": "Table: screening",
                "metadata": {
                    "table": "screening",
                    "row_count": 12,
                    "has_person_token_join": True,
                },
            }
        ],
        column_chunks=[],
        source_fingerprint="schema-fingerprint",
        join_keys={"person_token": "PERSON_TOKEN"},
        relationships=[],
    )

    assert catalog["catalog_version"] == 2
    assert catalog["join_keys"] == {"person_token": "PERSON_TOKEN"}
    assert catalog["relationships"] == []
    assert catalog["tables"][0] == {
        "table": "screening",
        "text": "Table: screening",
        "row_count": 12,
        "has_person_token_join": True,
    }
    assert not {
        "seqn_col",
        "subjid_col",
        "fid_col",
        "has_seqn_join",
        "has_subjid_join",
        "has_fid_join",
    } & set(catalog["tables"][0])


@pytest.mark.parametrize("value", ["false", 1, None])
def test_full_catalog_rejects_non_boolean_relationship_flags(value: object) -> None:
    with pytest.raises(ValueError, match="has_person_token_join must be a boolean"):
        build_full_schema_catalog(
            table_chunks=[
                {
                    "id": "screening.summary",
                    "text": "Table: screening",
                    "metadata": {
                        "table": "screening",
                        "has_person_token_join": value,
                    },
                }
            ],
            column_chunks=[],
            source_fingerprint="schema-fingerprint",
            join_keys={"person_token": "PERSON_TOKEN"},
            relationships=[],
        )


def test_build_chroma_keeps_publication_chunks_in_separate_collection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _WriteCollection:
        def __init__(self) -> None:
            self.rows: dict[str, object] | None = None

        def add(self, **rows) -> None:
            self.rows = rows

    class _Client:
        def __init__(self, path: str) -> None:
            del path
            self.collections: dict[str, _WriteCollection] = {}

        def delete_collection(self, name: str) -> None:
            del name

        def create_collection(self, name: str, *, embedding_function):
            del embedding_function
            collection = _WriteCollection()
            self.collections[name] = collection
            return collection

    client = _Client(str(tmp_path))
    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda path: client),
    )
    monkeypatch.setattr(
        vectorstore,
        "OpenAIEmbeddingFunction",
        lambda model, **_kwargs: SimpleNamespace(model=model),
    )
    knowledge_chunks = [
        _publication_chunk(text="Publication design reference.")
    ]

    vectorstore.build_chroma(
        [
            {
                "id": "screening.summary",
                "text": "Table: screening",
                "metadata": {"table": "screening"},
            }
        ],
        [
            {
                "id": "screening.age",
                "text": "Column: age",
                "metadata": {"table": "screening", "column": "age"},
            }
        ],
        model="test-embedding",
        api_key="session-key",
        chroma_dir=tmp_path / "chroma",
        knowledge_chunks=knowledge_chunks,
    )

    assert set(client.collections) == {
        "table_summaries",
        "column_chunks",
        "study_knowledge",
    }
    assert client.collections["table_summaries"].rows["ids"] == [
        "screening.summary"
    ]
    assert client.collections["column_chunks"].rows["ids"] == ["screening.age"]
    assert client.collections["study_knowledge"].rows["ids"] == [
        knowledge_chunks[0].id
    ]
    assert "Title: Example publication" in client.collections["study_knowledge"].rows[
        "documents"
    ][0]
    assert "Section: Retrieval summary" in client.collections[
        "study_knowledge"
    ].rows["documents"][0]
    assert "Publication design reference." in client.collections["study_knowledge"].rows[
        "documents"
    ][0]
    assert client.collections["study_knowledge"].rows["metadatas"][0][
        "source_kind"
    ] == "publication"


def test_replace_study_knowledge_preserves_schema_collections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _WriteCollection:
        def __init__(self, name: str) -> None:
            self.name = name
            self.rows: dict[str, object] | None = None

        def add(self, **rows) -> None:
            self.rows = rows

    class _Client:
        def __init__(self) -> None:
            self.collections = {
                "table_summaries": _WriteCollection("table_summaries"),
                "column_chunks": _WriteCollection("column_chunks"),
                "study_knowledge": _WriteCollection("study_knowledge"),
            }
            self.deleted: list[str] = []

        def delete_collection(self, name: str) -> None:
            self.deleted.append(name)
            del self.collections[name]

        def create_collection(self, name: str, *, embedding_function):
            del embedding_function
            collection = _WriteCollection(name)
            self.collections[name] = collection
            return collection

    client = _Client()
    original_table = client.collections["table_summaries"]
    original_column = client.collections["column_chunks"]
    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda path: client),
    )
    monkeypatch.setattr(
        vectorstore,
        "OpenAIEmbeddingFunction",
        lambda model, **_kwargs: SimpleNamespace(model=model),
    )

    vectorstore.replace_study_knowledge(
        model="test-embedding",
        api_key="session-key",
        chroma_dir=tmp_path / "chroma",
        knowledge_chunks=[],
    )

    assert client.deleted == ["study_knowledge"]
    assert client.collections["table_summaries"] is original_table
    assert client.collections["column_chunks"] is original_column
    assert client.collections["study_knowledge"].rows is None
