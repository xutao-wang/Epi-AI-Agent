from __future__ import annotations

import json
from pathlib import Path

import pytest

import db_rag.session_studies as session_studies
from db_rag.catalog import (
    SemanticCatalogUnavailableError,
    SemanticSchemaCatalog,
)
from db_rag.config import DbRagRuntimePaths, EMBEDDING_MODEL
from db_rag.readiness import DbRagReadiness
from epi_agent.studies import StudyBundle, StudyRegistry


def _bundle(tmp_path: Path, study_id: str, table: str) -> StudyBundle:
    database = tmp_path / study_id / "database"
    chroma = database / "index"
    chroma.mkdir(parents=True)
    catalog_path = database / "schema_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "catalog_version": 1,
                "tables": [{"table": table, "text": f"{table} records"}],
                "columns": [
                    {
                        "table": table,
                        "column": "FIELD",
                        "text": f"{table} field",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    duckdb_path = database / "study.duckdb"
    duckdb_path.touch()
    return StudyBundle(
        study_id=study_id,
        label=study_id,
        knowledge=None,
        catalog=None,
        data_sources={},
        source_id=study_id,
        db_rag_paths=DbRagRuntimePaths(
            duckdb_path=duckdb_path,
            catalog_path=catalog_path,
            chroma_path=chroma,
            embedding_model=EMBEDDING_MODEL,
        ),
    )


class _FakeEmbeddingFunction:
    created: list[tuple[str, str]] = []

    def __init__(self, model: str, *, api_key: str) -> None:
        self.created.append((model, api_key))

    def embed_query(self, queries: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _query in queries]


class _FakeCollection:
    def query(self, **kwargs):
        count = len(kwargs["query_embeddings"])
        return {
            "documents": [[] for _ in range(count)],
            "metadatas": [[] for _ in range(count)],
        }


class _FakeClient:
    requested_paths: list[Path] = []
    requested_collections: list[tuple[Path, str]] = []
    failing_path: Path | None = None

    def __init__(self, *, path: str) -> None:
        self.path = Path(path)
        self.requested_paths.append(self.path)

    def get_collection(self, name: str, *, embedding_function):
        del embedding_function
        self.requested_collections.append((self.path, name))
        if self.path == self.failing_path and name == "column_chunks":
            raise RuntimeError("missing column collection")
        return _FakeCollection()


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    _FakeEmbeddingFunction.created = []
    _FakeClient.requested_paths = []
    _FakeClient.requested_collections = []
    _FakeClient.failing_path = None


def _available(**kwargs) -> DbRagReadiness:
    del kwargs
    return DbRagReadiness(
        status="available",
        message="DB-RAG dataset is available.",
    )


def test_bind_session_studies_opens_isolated_collections_for_every_study(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _bundle(tmp_path, "report-india-synthetic", "REPORT_TABLE")
    nhanes = _bundle(tmp_path, "nhanes-2017-2018", "GHB_J")
    monkeypatch.setattr(session_studies, "resolve_db_rag_readiness", _available)
    monkeypatch.setattr(
        session_studies.chromadb,
        "PersistentClient",
        _FakeClient,
    )
    monkeypatch.setattr(
        session_studies,
        "OpenAIEmbeddingFunction",
        _FakeEmbeddingFunction,
    )

    bound = session_studies.bind_session_studies(
        StudyRegistry([report, nhanes]),
        api_key="session-key",
        expected_embedding_model=EMBEDDING_MODEL,
    )

    report_paths = report.db_rag_paths
    nhanes_paths = nhanes.db_rag_paths
    assert set(bound.readiness) == {
        "report-india-synthetic",
        "nhanes-2017-2018",
    }
    assert all(value.available for value in bound.readiness.values())
    assert _FakeClient.requested_paths == [
        report_paths.chroma_path,
        nhanes_paths.chroma_path,
    ]
    assert _FakeClient.requested_collections == [
        (report_paths.chroma_path, "table_summaries"),
        (report_paths.chroma_path, "column_chunks"),
        (nhanes_paths.chroma_path, "table_summaries"),
        (nhanes_paths.chroma_path, "column_chunks"),
    ]
    assert isinstance(
        bound.studies.require("report-india-synthetic").catalog,
        SemanticSchemaCatalog,
    )
    assert isinstance(
        bound.studies.require("nhanes-2017-2018").catalog,
        SemanticSchemaCatalog,
    )
    assert _FakeEmbeddingFunction.created == [
        (EMBEDDING_MODEL, "session-key")
    ]
    assert "session-key" not in repr(bound)


def test_one_failed_binding_does_not_enable_lexical_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _bundle(tmp_path, "report-india-synthetic", "REPORT_TABLE")
    nhanes = _bundle(tmp_path, "nhanes-2017-2018", "GHB_J")
    _FakeClient.failing_path = report.db_rag_paths.chroma_path
    monkeypatch.setattr(session_studies, "resolve_db_rag_readiness", _available)
    monkeypatch.setattr(
        session_studies.chromadb,
        "PersistentClient",
        _FakeClient,
    )
    monkeypatch.setattr(
        session_studies,
        "OpenAIEmbeddingFunction",
        _FakeEmbeddingFunction,
    )

    bound = session_studies.bind_session_studies(
        StudyRegistry([report, nhanes]),
        api_key="session-key",
        expected_embedding_model=EMBEDDING_MODEL,
    )

    assert not bound.readiness["report-india-synthetic"].available
    assert bound.readiness["nhanes-2017-2018"].available
    failed_catalog = bound.studies.require("report-india-synthetic").catalog
    assert failed_catalog.inspect_table(
        "report-india-synthetic",
        "REPORT_TABLE",
    )
    with pytest.raises(SemanticCatalogUnavailableError):
        failed_catalog.search("FIELD")
