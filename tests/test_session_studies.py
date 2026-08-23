from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import db_rag.session_studies as session_studies
from db_rag.catalog import (
    SemanticCatalogUnavailableError,
    SemanticSchemaCatalog,
)
from db_rag.config import DbRagRuntimePaths, EMBEDDING_MODEL
from db_rag.embedding_routes import resolve_embedding_route
from db_rag.knowledge import StudyEvidenceChunk
from db_rag.local_knowledge import (
    LocalPublicationKnowledge,
    SemanticPublicationKnowledge,
)
from db_rag.readiness import DbRagReadiness
from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext
from epi_agent.studies import StudyBundle, StudyRegistry


def _bundle(
    tmp_path: Path,
    study_id: str,
    table: str,
    *,
    knowledge=None,
) -> StudyBundle:
    database = tmp_path / study_id / "database"
    chroma = database / "index"
    chroma.mkdir(parents=True)
    catalog_path = database / "schema_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "catalog_version": 2,
                "join_keys": {"record_key": "FIELD"},
                "relationships": [],
                "tables": [
                    {
                        "table": table,
                        "text": f"{table} records",
                        "has_record_key_join": True,
                    }
                ],
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
        knowledge=knowledge,
        catalog=None,
        data_sources={study_id: object()},
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


def test_bind_session_studies_binds_publications_only_for_owning_study(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _bundle(
        tmp_path,
        "report-india-synthetic",
        "REPORT_TABLE",
        knowledge=LocalPublicationKnowledge((), {}),
    )
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
    assert _FakeClient.requested_collections == [
        (report_paths.chroma_path, "table_summaries"),
        (report_paths.chroma_path, "column_chunks"),
        (report_paths.chroma_path, "study_knowledge"),
        (nhanes_paths.chroma_path, "table_summaries"),
        (nhanes_paths.chroma_path, "column_chunks"),
    ]
    assert isinstance(
        bound.studies.require("report-india-synthetic").knowledge,
        SemanticPublicationKnowledge,
    )
    assert bound.studies.require("nhanes-2017-2018").knowledge is None


def test_one_failed_binding_enables_scoped_lexical_fallback(
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
    outcome = failed_catalog.search_many_with_status(["FIELD"], limit=5)
    assert outcome.status.mode == "lexical_fallback"
    assert outcome.status.reason_code == "EMBEDDING_INDEX_UNAVAILABLE"
    assert outcome.value[0][0].column == "FIELD"


def test_missing_embedding_key_binds_lexical_fallback_without_opening_chroma(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _bundle(tmp_path, "report-india-synthetic", "REPORT_TABLE")
    monkeypatch.setattr(session_studies, "resolve_db_rag_readiness", _available)
    monkeypatch.setattr(
        session_studies.chromadb,
        "PersistentClient",
        _FakeClient,
    )

    bound = session_studies.bind_session_studies(
        StudyRegistry([report]),
        api_key="",
        expected_embedding_model=EMBEDDING_MODEL,
    )

    outcome = bound.studies.require(
        "report-india-synthetic"
    ).catalog.search_many_with_status(["REPORT_TABLE"], limit=5)
    assert _FakeClient.requested_paths == []
    assert bound.readiness["report-india-synthetic"].available is True
    assert "lexical fallback" in bound.readiness[
        "report-india-synthetic"
    ].message
    assert outcome.status.reason_code == "EMBEDDING_CREDENTIALS_MISSING"
    assert outcome.value[0][0].table == "REPORT_TABLE"


def test_incompatible_study_index_uses_scoped_lexical_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compatible = _bundle(tmp_path, "compatible", "MATCHING_TABLE")
    mismatch = _bundle(tmp_path, "mismatch", "MISMATCH_TABLE")
    mismatch = replace(
        mismatch,
        db_rag_paths=replace(
            mismatch.db_rag_paths,
            embedding_model="Other/incompatible-model",
        ),
    )
    monkeypatch.setattr(session_studies, "resolve_db_rag_readiness", _available)
    monkeypatch.setattr(session_studies.chromadb, "PersistentClient", _FakeClient)
    monkeypatch.setattr(
        session_studies,
        "OpenAIEmbeddingFunction",
        _FakeEmbeddingFunction,
    )
    route = resolve_embedding_route(
        {"OPENAI_API_KEY": "session-key"},
        EMBEDDING_MODEL,
    )
    route = replace(
        route,
        factory=lambda model, key: _FakeEmbeddingFunction(model, api_key=key),
    )

    bound = session_studies.bind_session_studies(
        StudyRegistry([compatible, mismatch]),
        embedding_route=route,
    )

    compatible_outcome = bound.studies.require(
        "compatible"
    ).catalog.search_many_with_status(["MATCHING_TABLE"], limit=5)
    mismatch_outcome = bound.studies.require(
        "mismatch"
    ).catalog.search_many_with_status(["MISMATCH_TABLE"], limit=5)
    assert compatible_outcome.status.mode == "hybrid_vector_lexical"
    assert mismatch_outcome.status.mode == "lexical_fallback"
    assert mismatch_outcome.status.reason_code == "EMBEDDING_INDEX_INCOMPATIBLE"
    assert mismatch.db_rag_paths.chroma_path not in _FakeClient.requested_paths


def test_unavailable_openrouter_route_binds_provider_aware_lexical_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _bundle(
        tmp_path,
        "report-india-synthetic",
        "REPORT_TABLE",
        knowledge=_publication_knowledge("report-india-synthetic"),
    )
    monkeypatch.setattr(session_studies, "resolve_db_rag_readiness", _available)
    monkeypatch.setattr(
        session_studies.chromadb,
        "PersistentClient",
        _FakeClient,
    )
    route = resolve_embedding_route(
        {"OPENROUTER_API_KEY": "router-key"},
        "OpenRouter/Qwen/qwen3-embedding-8b",
    )

    bound = session_studies.bind_session_studies(
        StudyRegistry([report]),
        embedding_route=route,
    )

    outcome = bound.studies.require(
        "report-india-synthetic"
    ).catalog.search_many_with_status(["REPORT_TABLE"], limit=5)
    assert _FakeClient.requested_paths == []
    assert bound.readiness["report-india-synthetic"].available is True
    assert "lexical fallback" in bound.readiness[
        "report-india-synthetic"
    ].message
    assert outcome.status.model == "OpenRouter/Qwen/qwen3-embedding-8b"
    assert outcome.status.provider == "openrouter"
    assert outcome.status.reason_code == "EMBEDDING_ROUTE_UNAVAILABLE"
    assert outcome.value[0][0].table == "REPORT_TABLE"
    publication_outcome = bound.studies.require(
        "report-india-synthetic"
    ).knowledge.search_with_status("eligibility", limit=3)
    assert publication_outcome.value
    assert publication_outcome.status.model == route.model
    assert publication_outcome.status.provider == "openrouter"
    assert publication_outcome.status.reason_code == "EMBEDDING_ROUTE_UNAVAILABLE"


class _IsolatedEmbeddingFunction:
    instances: list["_IsolatedEmbeddingFunction"] = []

    def __init__(self, model: str, *, api_key: str) -> None:
        del model, api_key
        self.calls: list[list[str]] = []
        self.instances.append(self)

    def embed_query(self, queries: list[str]) -> list[list[float]]:
        self.calls.append(list(queries))
        return [[1.0, 0.0] for _query in queries]


class _IsolatedCollection:
    def __init__(self, client: "_IsolatedClient", name: str) -> None:
        self.client = client
        self.name = name

    def query(self, **kwargs):
        self.client.query_count += 1
        count = len(kwargs["query_embeddings"])
        if self.name == "study_knowledge":
            self.client.knowledge_queries.append(dict(kwargs))
            chunk = _publication_chunk(self.client.study_id)
            return {
                "ids": [[chunk.id] for _ in range(count)],
                "metadatas": [[chunk.chroma_metadata()] for _ in range(count)],
            }
        if self.name == "table_summaries":
            documents = [f"{self.client.table} records"]
            metadatas = [
                {
                    "source": self.client.study_id,
                    "table": self.client.table,
                }
            ]
        else:
            documents = [f"{self.client.column} field"]
            metadatas = [
                {
                    "source": self.client.study_id,
                    "table": self.client.table,
                    "column": self.client.column,
                }
            ]
        return {
            "documents": [documents for _ in range(count)],
            "metadatas": [metadatas for _ in range(count)],
        }


class _IsolatedClient:
    clients: dict[Path, "_IsolatedClient"] = {}

    def __init__(self, *, path: str) -> None:
        resolved = Path(path)
        self.study_id = resolved.parent.parent.name
        if self.study_id == "report-india-synthetic":
            self.table = "Baseline Clinical and Demographic Information Cohort A"
            self.column = "CIGPAST"
        else:
            self.table = "GHB_J"
            self.column = "LBXGH"
        self.query_count = 0
        self.knowledge_queries: list[dict[str, object]] = []
        self.clients[resolved] = self

    def get_collection(self, name: str, *, embedding_function):
        del embedding_function
        return _IsolatedCollection(self, name)


def _publication_chunk(study_id: str) -> StudyEvidenceChunk:
    return StudyEvidenceChunk(
        id=f"publication.{study_id}",
        source_id=f"doi:10.1000/{study_id}",
        title=f"{study_id} publication",
        section="Eligibility",
        text=f"Eligibility evidence for {study_id}.",
        path=f"{study_id}.json",
        knowledge_type="eligibility",
        knowledge_role="historical_study_design_reference",
        indexed_path="design_reference.eligibility",
    )


def _publication_knowledge(study_id: str) -> LocalPublicationKnowledge:
    return LocalPublicationKnowledge(
        (_publication_chunk(study_id),),
        {"eligibility": 1},
    )


def test_selected_study_publications_cannot_cross_chroma_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id = "report-india-synthetic"
    second_id = "report-second-study"
    first = _bundle(
        tmp_path,
        first_id,
        "REPORT_TABLE",
        knowledge=_publication_knowledge(first_id),
    )
    second = _bundle(
        tmp_path,
        second_id,
        "SECOND_TABLE",
        knowledge=_publication_knowledge(second_id),
    )
    _IsolatedClient.clients = {}
    _IsolatedEmbeddingFunction.instances = []
    monkeypatch.setattr(session_studies, "resolve_db_rag_readiness", _available)
    monkeypatch.setattr(
        session_studies.chromadb,
        "PersistentClient",
        _IsolatedClient,
    )
    monkeypatch.setattr(
        session_studies,
        "OpenAIEmbeddingFunction",
        _IsolatedEmbeddingFunction,
    )

    bound = session_studies.bind_session_studies(
        StudyRegistry([first, second]),
        api_key="session-key",
        expected_embedding_model=EMBEDDING_MODEL,
    )

    first_hits = bound.studies.require(first_id).knowledge.search(
        "eligibility",
        limit=1,
    )
    second_hits = bound.studies.require(second_id).knowledge.search(
        "eligibility",
        limit=1,
    )

    assert [hit.source_id for hit in first_hits] == [f"doi:10.1000/{first_id}"]
    assert [hit.source_id for hit in second_hits] == [f"doi:10.1000/{second_id}"]
    first_client = _IsolatedClient.clients[first.db_rag_paths.chroma_path]
    second_client = _IsolatedClient.clients[second.db_rag_paths.chroma_path]
    assert len(first_client.knowledge_queries) == 1
    assert len(second_client.knowledge_queries) == 1


def test_selected_study_catalogs_cannot_cross_chroma_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _bundle(
        tmp_path,
        "report-india-synthetic",
        "Baseline Clinical and Demographic Information Cohort A",
    )
    nhanes = _bundle(tmp_path, "nhanes-2017-2018", "GHB_J")
    _IsolatedClient.clients = {}
    _IsolatedEmbeddingFunction.instances = []
    monkeypatch.setattr(session_studies, "resolve_db_rag_readiness", _available)
    monkeypatch.setattr(
        session_studies.chromadb,
        "PersistentClient",
        _IsolatedClient,
    )
    monkeypatch.setattr(
        session_studies,
        "OpenAIEmbeddingFunction",
        _IsolatedEmbeddingFunction,
    )

    bound = session_studies.bind_session_studies(
        StudyRegistry([report, nhanes]),
        api_key="session-key",
        expected_embedding_model=EMBEDDING_MODEL,
    )

    assert _IsolatedEmbeddingFunction.instances[0].calls == []
    registry = build_db_rag_tool_registry()
    observations = {}
    for study_id, query in (
        ("report-india-synthetic", "smoking intensity"),
        ("nhanes-2017-2018", "glycohemoglobin"),
    ):
        store = StateArtifactStore()
        result = registry.invoke(
            "dbrag-search_catalog",
            {"study_id": study_id, "queries": [query], "limit": 5},
            context=ToolContext(
                studies=bound.studies,
                artifact_store=store,
                thread_id=f"thread-{study_id}",
                policy=object(),
            ),
        )
        observations[study_id] = store.require(result.artifacts[0]).content

    hits_by_study = {
        study_id: [
            hit
            for probe in observation["probes"]
            for hit in probe["hits"]
        ]
        for study_id, observation in observations.items()
    }
    report_hits = {
        (hit["source"], hit["table"], hit["column"])
        for hit in hits_by_study["report-india-synthetic"]
        if hit.get("column") == "CIGPAST"
    }
    nhanes_hits = {
        (hit["source"], hit["table"], hit["column"])
        for hit in hits_by_study["nhanes-2017-2018"]
        if hit.get("column") == "LBXGH"
    }
    assert report_hits == {
        (
            "report-india-synthetic",
            "Baseline Clinical and Demographic Information Cohort A",
            "CIGPAST",
        )
    }
    assert nhanes_hits == {
        ("nhanes-2017-2018", "GHB_J", "LBXGH")
    }
    assert all(
        hit.get("source") == "report-india-synthetic"
        for hit in hits_by_study["report-india-synthetic"]
    )
    assert all(
        hit.get("source") == "nhanes-2017-2018"
        for hit in hits_by_study["nhanes-2017-2018"]
    )
    assert all(client.query_count == 2 for client in _IsolatedClient.clients.values())
    assert _IsolatedEmbeddingFunction.instances[0].calls == [
        ["smoking intensity"],
        ["glycohemoglobin"],
    ]
