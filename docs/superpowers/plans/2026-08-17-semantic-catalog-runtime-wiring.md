# Semantic Catalog Runtime Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect every installed study's Chroma schema index to query-time DB-RAG, combine mandatory vector retrieval with exact lexical boosting, and prohibit silent lexical-only fallback.

**Architecture:** Startup discovery remains provider-key-free. Each user graph binds the discovered studies to separate session-scoped Chroma clients and semantic catalogs using that session's OpenAI key; the selected `StudyBundle` remains the only catalog and DuckDB visible to tools. `dbrag-search_catalog` runs one embedding batch, reuses it for table and column searches, fuses vector and lexical rankings, and returns a typed recoverable error if vector retrieval is unavailable.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, Pydantic, ChromaDB, DuckDB, OpenAI `text-embedding-3-large`, pytest.

## Global Constraints

- Use Python 3.12 and run project tooling with `.venv/bin/python` or `.venv/bin/pytest` plus `PYTHONPATH=.`.
- Do not modify database package formats or rebuild the RePORT or NHANES indexes.
- Keep `dbrag-search_catalog` as the single agent-facing schema-search tool.
- Vector retrieval is mandatory in production; never return lexical-only evidence after a vector or embedding failure.
- Lexical matching is a complementary exactness signal for codes, table names, phrases, and bounded terminology overlap.
- Generate one embedding batch per tool call and reuse it for both Chroma collections.
- Construct one isolated Chroma client and semantic catalog per installed study; search only `ToolContext.study`.
- Keep the existing `active_study_id` selection boundary; agent-driven study routing and clarification are a separate follow-on feature.
- Never retain a user's provider key in global discovery state, logs, diagnostics, reprs, or another session's graph.
- Preserve the unrelated existing modification to `.superpowers/sdd/task-5-report.md`.
- The dedicated real smoke runs once, for at most five minutes, and its first failure is preserved rather than automatically rerun.

---

## File Structure

- Modify `db_rag/retrieval.py`: allow one explicit query-embedding batch to drive both table and column collection queries.
- Modify `db_rag/catalog.py`: add mandatory semantic/hybrid catalog behavior, deterministic lexical ranking, rank fusion, and typed semantic-unavailable errors.
- Create `db_rag/session_studies.py`: bind discovered study bundles to session-scoped Chroma collections and return per-study readiness.
- Modify `epi_agent/db_rag/tools.py`: translate semantic catalog failures into typed tool errors and expose bounded retrieval provenance.
- Modify `api/app.py`: bind all installed studies inside the session graph factory where the provider key is available.
- Modify `graph/builder.py`: enable DB-RAG when any installed study is ready rather than only when a sole default study is ready.
- Modify focused tests under `tests/`: prove embedding reuse, hybrid retrieval, no fallback, provider-key isolation, and multi-study path isolation.
- Create `scripts/smoke_multi_study_semantic_catalog.py`: exercise real RePORT and NHANES packages through the production binding and catalog boundaries.

---

### Task 1: Reuse One Explicit Query-Embedding Batch

**Files:**
- Modify: `db_rag/retrieval.py:56-86`
- Modify: `tests/test_db_rag_retrieval.py`

**Interfaces:**
- Consumes: existing `retrieve_queries(table_collection, column_collection, queries, ...)`.
- Produces: `retrieve_queries(..., query_embeddings: list[list[float]] | None = None)`; when embeddings are supplied, both collection calls receive the same batch through `query_embeddings` and no collection receives `query_texts`.

- [ ] **Step 1: Write the failing embedding-reuse test**

Add a collection double that records `query_embeddings` and a test:

```python
class _EmbeddingRecordingCollection:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def query(
        self,
        *,
        n_results: int,
        include: list[str],
        query_embeddings=None,
        query_texts=None,
    ):
        self.calls.append(
            {
                "n_results": n_results,
                "include": include,
                "query_embeddings": query_embeddings,
                "query_texts": query_texts,
            }
        )
        count = len(query_embeddings or query_texts or [])
        return {
            "documents": [[] for _ in range(count)],
            "metadatas": [[] for _ in range(count)],
        }


def test_retrieve_queries_reuses_one_explicit_embedding_batch() -> None:
    tables = _EmbeddingRecordingCollection()
    columns = _EmbeddingRecordingCollection()
    embeddings = [[0.1, 0.2], [0.3, 0.4]]

    retrieval.retrieve_queries(
        tables,
        columns,
        ["diabetes diagnosis", "glycohemoglobin"],
        query_embeddings=embeddings,
    )

    assert tables.calls[0]["query_embeddings"] is embeddings
    assert columns.calls[0]["query_embeddings"] is embeddings
    assert tables.calls[0]["query_texts"] is None
    assert columns.calls[0]["query_texts"] is None
```

- [ ] **Step 2: Run the test and confirm the missing parameter failure**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_db_rag_retrieval.py::test_retrieve_queries_reuses_one_explicit_embedding_batch
```

Expected: `TypeError` because `retrieve_queries` does not accept `query_embeddings`.

- [ ] **Step 3: Implement the optional explicit-embedding path**

Update the signature and query arguments:

```python
def retrieve_queries(
    table_collection: Any,
    column_collection: Any,
    queries: list[str],
    *,
    table_k: int = 4,
    column_k: int = 12,
    debug: bool = False,
    query_embeddings: list[list[float]] | None = None,
) -> list[tuple[list[dict[str, str]], list[dict[str, str]]]]:
    if not queries:
        return []
    if query_embeddings is not None and len(query_embeddings) != len(queries):
        raise ValueError("Query embedding count does not match query count.")
    query_arguments = (
        {"query_embeddings": query_embeddings}
        if query_embeddings is not None
        else {"query_texts": queries}
    )
    table_result = table_collection.query(
        **query_arguments,
        n_results=table_k,
        include=["documents", "metadatas"],
    )
    column_result = column_collection.query(
        **query_arguments,
        n_results=column_k,
        include=["documents", "metadatas"],
    )
    # Preserve the existing result normalization below these calls.
```

Keep the existing timing stages around each collection query.

- [ ] **Step 4: Run focused retrieval tests**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_db_rag_retrieval.py
```

Expected: all retrieval tests pass, including both legacy `query_texts` callers and the new explicit-embedding test.

- [ ] **Step 5: Commit**

```bash
git add db_rag/retrieval.py tests/test_db_rag_retrieval.py
git commit -m "refactor: reuse schema query embeddings"
```

---

### Task 2: Add Mandatory Hybrid Semantic Catalog Retrieval

**Files:**
- Modify: `db_rag/catalog.py`
- Modify: `tests/test_db_rag_catalog.py`

**Interfaces:**
- Consumes: Task 1 `retrieve_queries(..., query_embeddings=...)` and an embedding callable returning `list[list[float]]`.
- Produces: `SemanticCatalogUnavailableError`, `SemanticSchemaCatalog`,
  `UnavailableSemanticSchemaCatalog`, and `SchemaEvidenceHit.matched_by`.

- [ ] **Step 1: Write failing hybrid and fail-closed tests**

Use deterministic collection and embedder doubles:

```python
class _SemanticCollection:
    def __init__(self, documents, metadatas) -> None:
        self.documents = documents
        self.metadatas = metadatas
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        count = len(kwargs["query_embeddings"])
        return {
            "documents": [self.documents for _ in range(count)],
            "metadatas": [self.metadatas for _ in range(count)],
        }


class _EmbeddingBatch:
    def __init__(self) -> None:
        self.calls = []

    def embed_query(self, queries):
        self.calls.append(list(queries))
        return [[float(index), 0.5] for index, _ in enumerate(queries, start=1)]


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
        {
            "tables": [{"table": "GHB_J", "text": "Glycohemoglobin laboratory"}],
            "columns": [
                {"table": "GHB_J", "column": "LBXGH", "text": "Glycohemoglobin (%)"}
            ],
        },
        table_collection=tables,
        column_collection=columns,
        embedding_function=embedder,
        default_source_id="nhanes-2017-2018",
    )

    hits = catalog.search("LBXGH", limit=5)

    assert embedder.calls == [["LBXGH"]]
    assert hits[0].column == "LBXGH"
    assert set(hits[0].matched_by) == {"vector", "lexical"}
    assert tables.calls[0]["query_embeddings"] == columns.calls[0]["query_embeddings"]


def test_semantic_catalog_never_returns_lexical_only_after_vector_failure() -> None:
    catalog = SemanticSchemaCatalog(
        {"tables": [], "columns": [{"table": "GHB_J", "column": "LBXGH", "text": "HbA1c"}]},
        table_collection=_FailingCollection(),
        column_collection=_FailingCollection(),
        embedding_function=_EmbeddingBatch(),
        default_source_id="nhanes-2017-2018",
    )

    with pytest.raises(SemanticCatalogUnavailableError):
        catalog.search("LBXGH")
```

Also assert that a lexical query with no positive term match contributes no arbitrary first-catalog result.

- [ ] **Step 2: Run the new tests and confirm missing semantic types**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_db_rag_catalog.py
```

Expected: collection fails because `SemanticSchemaCatalog` and `SemanticCatalogUnavailableError` do not exist.

- [ ] **Step 3: Implement deterministic lexical ranking and rank fusion**

Add:

```python
class SemanticCatalogUnavailableError(RuntimeError):
    """The selected study cannot perform mandatory semantic catalog search."""


class UnavailableSemanticSchemaCatalog(SchemaCatalog):
    """Retain deterministic inspection while failing catalog search closed."""

    def search_many(self, queries: list[str], *, limit: int = 5):
        if not queries:
            return []
        raise SemanticCatalogUnavailableError(
            "Semantic catalog retrieval is unavailable for the selected study."
        )


class SchemaEvidenceHit(BaseModel):
    source: str | None = None
    table: str
    column: str | None = None
    text: str
    source_kind: Literal["schema"] = "schema"
    provenance: dict[str, str]
    matched_by: tuple[Literal["vector", "lexical"], ...] = ()
```

Create private helpers with these exact responsibilities:

```python
_TOKEN = re.compile(r"[a-z0-9_]+")
_RRF_K = 60
_LEXICAL_STOPWORDS = frozenset(
    {"a", "an", "and", "for", "from", "in", "of", "on", "or", "the", "to"}
)


def _evidence_key(row: dict[str, Any]) -> tuple[str, str]:
    return (_as_text(row.get("table")), _as_text(row.get("column")))


def _lexical_ranked_rows(
    catalog: dict[str, Any], query: str, *, limit: int
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    normalized_query = " ".join(_TOKEN.findall(query.casefold()))
    terms = {
        term
        for term in _TOKEN.findall(query.casefold())
        if term not in _LEXICAL_STOPWORDS and (len(term) > 2 or "_" in term)
    }
    ranked = []
    exact_keys = set()
    entries = [
        *(dict(row) for row in catalog.get("tables", []) if isinstance(row, dict)),
        *(dict(row) for row in catalog.get("columns", []) if isinstance(row, dict)),
    ]
    for ordinal, row in enumerate(entries):
        table = _as_text(row.get("table")).casefold()
        column = _as_text(row.get("column")).casefold()
        text = _as_text(row.get("text")).casefold()
        identifiers = {value for value in (table, column, f"{table}.{column}") if value}
        exact = normalized_query in identifiers
        phrase = bool(normalized_query and normalized_query in text)
        overlap = sum(term in text for term in terms)
        if not (exact or phrase or overlap):
            continue
        key = _evidence_key(row)
        if exact:
            exact_keys.add(key)
        ranked.append(((int(exact), int(phrase), overlap, -ordinal), row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [row for _score, row in ranked[:limit]], exact_keys
```

Add `_fuse_ranked_rows(vector_rows, lexical_rows, exact_keys, limit)` using
reciprocal-rank fusion `1 / (_RRF_K + rank)` per mode, exact identifiers as
the first deterministic sort key, and `(table, column)` as the final stable
sort keys. Drop any row carrying an explicit source/source ID different from
`default_source_id`; fill a missing source from `default_source_id`. Convert
fused rows with `_schema_evidence_hit(..., matched_by=(...))`.

The unavailable provider deliberately inherits `field_exists` and
`inspect_table` from `SchemaCatalog`, but overrides both `search_many` and
`search` (or relies on the inherited `search` delegating to `search_many`) so
there is no searchable lexical fallback. Add a focused assertion proving that
inspection still works while search raises the typed error.

- [ ] **Step 4: Implement `SemanticSchemaCatalog.search_many`**

Subclass `SchemaCatalog` and require both collections plus the embedder:

```python
class SemanticSchemaCatalog(SchemaCatalog):
    def __init__(self, catalog, *, table_collection, column_collection, embedding_function, default_source_id=None):
        super().__init__(catalog, default_source_id=default_source_id)
        self._table_collection = table_collection
        self._column_collection = column_collection
        self._embedding_function = embedding_function

    def search_many(self, queries: list[str], *, limit: int = 5):
        if not queries:
            return []
        try:
            with timing_stage("db_rag.catalog.query_embedding", query_count=len(queries)):
                embeddings = self._embedding_function.embed_query(queries)
            vector_batches = retrieve_queries(
                self._table_collection,
                self._column_collection,
                queries,
                table_k=limit,
                column_k=limit,
                query_embeddings=embeddings,
            )
        except Exception as error:
            raise SemanticCatalogUnavailableError(
                "Semantic catalog retrieval is unavailable for the selected study."
            ) from error
        results = []
        for query, (table_rows, column_rows) in zip(queries, vector_batches):
            with timing_stage("db_rag.catalog.lexical_rank"):
                lexical_rows, exact_keys = _lexical_ranked_rows(
                    self._catalog, query, limit=limit
                )
            with timing_stage("db_rag.catalog.rank_fusion"):
                results.append(
                    _fuse_ranked_rows(
                        [*table_rows, *column_rows],
                        lexical_rows,
                        exact_keys,
                        limit=limit,
                        default_source_id=self._default_source_id,
                    )
                )
        return results
```

Do not catch and continue after embedding or Chroma failure.

- [ ] **Step 5: Run catalog and retrieval tests**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_db_rag_catalog.py tests/test_db_rag_retrieval.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add db_rag/catalog.py tests/test_db_rag_catalog.py
git commit -m "feat: require hybrid semantic catalog retrieval"
```

---

### Task 3: Bind Every Installed Study to Isolated Session Collections

**Files:**
- Create: `db_rag/session_studies.py`
- Modify: `tests/test_installed_study_bundle.py`
- Create: `tests/test_session_studies.py`

**Interfaces:**
- Consumes: `StudyRegistry`, `StudyBundle.db_rag_paths`, `OpenAIEmbeddingFunction`, `SemanticSchemaCatalog`, and `resolve_db_rag_readiness`.
- Produces: `BoundStudyRegistry(studies: StudyRegistry, readiness: Mapping[str, DbRagReadiness])` and `bind_session_studies(...)`.

- [ ] **Step 1: Change the installed-bundle test to distinguish discovery from semantic binding**

Replace the assertion that calls lexical `bundle.catalog.search("participant")` with assertions that discovery remains key-free and retains the package paths:

```python
assert bundle.catalog is not None
assert bundle.catalog.inspect_table("nondefault-source", "participants")
assert bundle.db_rag_paths.embedding_model == "OpenAI/text-embedding-3-large"
```

This test must not construct an OpenAI client.

- [ ] **Step 2: Write failing two-study binding tests**

In `tests/test_session_studies.py`, construct two `StudyBundle` instances with separate `DbRagRuntimePaths`, catalog files, and fake Chroma directories. Monkeypatch `chromadb.PersistentClient` and `OpenAIEmbeddingFunction` so the test records paths, collection names, and keys without network access.

Assert:

```python
bound = bind_session_studies(
    StudyRegistry([report_bundle, nhanes_bundle]),
    api_key="session-key",
    expected_embedding_model="OpenAI/text-embedding-3-large",
)

assert set(bound.readiness) == {
    "report-india-synthetic",
    "nhanes-2017-2018",
}
assert all(value.available for value in bound.readiness.values())
assert requested_paths == [report_paths.chroma_path, nhanes_paths.chroma_path]
assert requested_collections == [
    (report_paths.chroma_path, "table_summaries"),
    (report_paths.chroma_path, "column_chunks"),
    (nhanes_paths.chroma_path, "table_summaries"),
    (nhanes_paths.chroma_path, "column_chunks"),
]
assert isinstance(bound.studies.require("report-india-synthetic").catalog, SemanticSchemaCatalog)
assert isinstance(bound.studies.require("nhanes-2017-2018").catalog, SemanticSchemaCatalog)
assert "session-key" not in repr(bound)
```

Add a second test where one client cannot open `column_chunks`; assert that study is `not_configured`, the other remains available, and the failing study's catalog raises `SemanticCatalogUnavailableError` rather than searching lexically.

- [ ] **Step 3: Run the binding tests and confirm the module is missing**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_session_studies.py tests/test_installed_study_bundle.py
```

Expected: import failure for `db_rag.session_studies`.

- [ ] **Step 4: Implement session-bound study construction**

Create:

```python
import json
from dataclasses import dataclass, replace
from typing import Mapping

import chromadb

from db_rag.catalog import (
    SemanticSchemaCatalog,
    UnavailableSemanticSchemaCatalog,
    load_full_schema_catalog,
)
from db_rag.config import DbRagRuntimePaths
from db_rag.readiness import DbRagReadiness, resolve_db_rag_readiness
from db_rag.vectorstore import OpenAIEmbeddingFunction
from epi_agent.studies import StudyRegistry


@dataclass(frozen=True)
class BoundStudyRegistry:
    studies: StudyRegistry
    readiness: Mapping[str, DbRagReadiness]


def bind_session_studies(
    studies: StudyRegistry,
    *,
    api_key: str,
    expected_embedding_model: str,
) -> BoundStudyRegistry:
    bound = []
    readiness_by_study = {}
    embedders = {}
    for study in studies.values:
        paths = study.db_rag_paths
        if not isinstance(paths, DbRagRuntimePaths):
            readiness = DbRagReadiness(
                status="not_configured",
                message="Semantic catalog assets are unavailable for this study.",
            )
            catalog_data = {"tables": [], "columns": []}
        else:
            readiness = resolve_db_rag_readiness(
                paths=paths,
                expected_embedding_model=expected_embedding_model,
            )
            try:
                catalog_data = load_full_schema_catalog(paths.catalog_path)
            except (OSError, json.JSONDecodeError):
                catalog_data = {"tables": [], "columns": []}
        if readiness.available:
            try:
                embedder = embedders.get(paths.embedding_model)
                if embedder is None:
                    embedder = OpenAIEmbeddingFunction(
                        paths.embedding_model,
                        api_key=api_key,
                    )
                    embedders[paths.embedding_model] = embedder
                client = chromadb.PersistentClient(path=str(paths.chroma_path))
                table_collection = client.get_collection(
                    "table_summaries", embedding_function=embedder
                )
                column_collection = client.get_collection(
                    "column_chunks", embedding_function=embedder
                )
                catalog = SemanticSchemaCatalog(
                    catalog_data,
                    table_collection=table_collection,
                    column_collection=column_collection,
                    embedding_function=embedder,
                    default_source_id=study.source_id,
                )
            except Exception:
                readiness = DbRagReadiness(
                    status="not_configured",
                    message="Semantic catalog binding is unavailable for this study.",
                )
                catalog = UnavailableSemanticSchemaCatalog(
                    catalog_data,
                    default_source_id=study.source_id,
                )
        else:
            catalog = UnavailableSemanticSchemaCatalog(
                catalog_data,
                default_source_id=study.source_id,
            )
        study = replace(study, catalog=catalog)
        bound.append(study)
        readiness_by_study[study.study_id] = readiness
    return BoundStudyRegistry(
        studies=StudyRegistry(bound),
        readiness=readiness_by_study,
    )
```

The code above is structural guidance, not a copy-paste exception policy.
Create one provider client per model per session. Handle a
missing/non-`DbRagRuntimePaths` value
without dereferencing it, and do not allow a catalog-loading failure to escape
as an untyped startup error. Sanitize failure messages and retain the original
exception only as a chained cause or server log without credentials. For an
unavailable study, bind
`UnavailableSemanticSchemaCatalog`; never bind the searchable lexical
`SchemaCatalog` as an error fallback.

- [ ] **Step 5: Run binding and package tests**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_session_studies.py tests/test_installed_study_bundle.py tests/test_study_package_registry.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add db_rag/session_studies.py tests/test_session_studies.py tests/test_installed_study_bundle.py
git commit -m "feat: bind semantic catalogs per study session"
```

---

### Task 4: Surface Hybrid Provenance and Typed Semantic Failure

**Files:**
- Modify: `epi_agent/db_rag/tools.py:270-316,973-1071`
- Modify: `tests/test_db_rag_agent_tools.py`

**Interfaces:**
- Consumes: `SchemaEvidenceHit.matched_by` and `SemanticCatalogUnavailableError` from Task 2.
- Produces: `SEMANTIC_CATALOG_UNAVAILABLE` and retrieval summaries with `retrieval_mode`, `vector_hits`, and `lexical_hits`.

- [ ] **Step 1: Write failing tool-contract tests**

Add a provider that raises `SemanticCatalogUnavailableError` and assert:

```python
with pytest.raises(ToolExecutionError) as raised:
    registry.invoke(
        "dbrag-search_catalog",
        {"queries": ["glycemic control"], "limit": 5},
        context_with_failing_catalog,
    )

assert raised.value.code == "SEMANTIC_CATALOG_UNAVAILABLE"
assert raised.value.recoverable is True
```

Update catalog-search fixtures to return hits with `matched_by=("vector",)` and `matched_by=("vector", "lexical")`. Assert the stored observation contains:

```python
assert observation["retrieval_mode"] == "hybrid_vector_lexical"
assert observation["retrieval_summary"]["vector_hits"] >= 1
assert observation["retrieval_summary"]["lexical_hits"] >= 1
assert observation["hits"][0]["matched_by"] == ["vector", "lexical"]
```

- [ ] **Step 2: Run focused tests and confirm missing fields/error translation**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_db_rag_agent_tools.py -k 'catalog or semantic'
```

Expected: failures because the tool drops `matched_by` and does not translate the semantic exception.

- [ ] **Step 3: Implement bounded provenance and typed failure**

Import `SemanticCatalogUnavailableError`. In `_schema_evidence_hit`, add:

```python
matched_by = [
    mode
    for mode in _safe_string_list(_provider_field(value, "matched_by"), limit=2)
    if mode in {"vector", "lexical"}
]
```

Include it only when nonempty. Wrap `search_many`:

```python
try:
    provider_batches = search_many(queries, limit=limit)
except SemanticCatalogUnavailableError as error:
    raise ToolExecutionError(
        "SEMANTIC_CATALOG_UNAVAILABLE",
        str(error),
        recoverable=True,
    ) from error
```

Compute vector and lexical hit counts from `matched_by`, add
`retrieval_mode: "hybrid_vector_lexical"`, and preserve the existing table,
column, and probe counts.

- [ ] **Step 4: Run tool tests**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_db_rag_agent_tools.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add epi_agent/db_rag/tools.py tests/test_db_rag_agent_tools.py
git commit -m "feat: expose semantic catalog retrieval provenance"
```

---

### Task 5: Enable Session-Bound DB-RAG for Multiple Installed Studies

**Files:**
- Modify: `api/app.py:77-205`
- Modify: `graph/builder.py:40-100`
- Modify: `tests/test_no_study_startup.py`
- Modify: `tests/test_graph_studies.py`

**Interfaces:**
- Consumes: `bind_session_studies(...) -> BoundStudyRegistry` from Task 3.
- Produces: graph construction using session-bound studies and a per-study readiness map; DB-RAG tools are included when any study is ready.

- [ ] **Step 1: Write failing graph availability tests**

Add a `test_build_graph_enables_db_rag_when_any_selected_study_is_ready` that passes two studies and:

```python
db_rag_readiness_by_study={
    "report-india-synthetic": DbRagReadiness(
        status="available", message="DB-RAG dataset is available."
    ),
    "nhanes-2017-2018": DbRagReadiness(
        status="available", message="DB-RAG dataset is available."
    ),
}
```

Assert `build_general_epi_agent_graph` receives `include_db_rag=True` even though `default_study_id is None`. Add the complementary all-unavailable assertion.

- [ ] **Step 2: Write a failing application factory binding test**

In `tests/test_no_study_startup.py`, monkeypatch `discover_studies`,
`bind_session_studies`, `build_openai_llm`, and `build_graph`. Invoke the graph
factory with `GraphBuildContext(provider_api_key="session-key", ...)` and assert:

```python
assert bind_calls == [
    {
        "studies": discovered_studies,
        "api_key": "session-key",
        "expected_embedding_model": "OpenAI/text-embedding-3-large",
    }
]
assert build_graph_kwargs["studies"] is bound_studies
assert build_graph_kwargs["db_rag_readiness_by_study"] == readiness_by_study
assert "session-key" not in repr(build_graph_kwargs)
```

- [ ] **Step 3: Run tests and confirm missing interfaces**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_graph_studies.py tests/test_no_study_startup.py
```

Expected: failures because the graph accepts only one readiness value and the app does not bind studies per session.

- [ ] **Step 4: Update graph readiness semantics**

Change `build_graph` to accept:

```python
db_rag_readiness_by_study: Mapping[str, DbRagReadiness] | None = None
```

Derive:

```python
readiness_by_study = dict(db_rag_readiness_by_study or {})
include_db_rag = any(item.available for item in readiness_by_study.values())
```

Retain the existing single-study derivation only as a compatibility path when
the mapping is omitted. Pass `include_db_rag` to
`build_general_epi_agent_graph`; do not require a default study to enable the
tool registry.

- [ ] **Step 5: Bind session studies in the application graph factory**

Inside `graph_factory`:

```python
bound = bind_session_studies(
    studies,
    api_key=context.provider_api_key,
    expected_embedding_model=db_rag_embedding_model,
)
return build_graph(
    llm,
    ...,
    studies=bound.studies,
    default_study_id=default_study_id,
    db_rag_readiness_by_study=bound.readiness,
    db_rag_embedding_model=db_rag_embedding_model,
)
```

Keep startup capability reporting provider-key-free. It may report that study
selection is required when multiple studies exist, but that status must no
longer compile DB-RAG tools out of the session graph.

- [ ] **Step 6: Run application and graph tests**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_graph_studies.py tests/test_no_study_startup.py tests/test_api_runtime.py tests/test_api_server.py
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add api/app.py graph/builder.py tests/test_graph_studies.py tests/test_no_study_startup.py
git commit -m "fix: enable semantic DB-RAG across installed studies"
```

---

### Task 6: Prove Selected-Study Isolation Without Provider Calls

**Files:**
- Modify: `tests/test_session_studies.py`
- Modify: `tests/test_db_rag_agent_tools.py`

**Interfaces:**
- Consumes: bound registry from Task 3 and tool provenance from Task 4.
- Produces: a regression contract that identical collection names in separate Chroma roots cannot cross-contaminate results.

- [ ] **Step 1: Add a two-study tool isolation test**

Configure the fake RePORT client to return only:

```python
{"table": "Baseline Clinical and Demographic Information Cohort A", "column": "CIGPAST"}
```

Configure the fake NHANES client to return only:

```python
{"table": "GHB_J", "column": "LBXGH"}
```

Invoke `dbrag-search_catalog` twice with separate `ToolContext` instances whose
`study` values come from the bound registry. Assert:

```python
assert report_hits == {
    ("report-india-synthetic", "Baseline Clinical and Demographic Information Cohort A", "CIGPAST")
}
assert nhanes_hits == {
    ("nhanes-2017-2018", "GHB_J", "LBXGH")
}
assert report_client.query_count == 2  # table and column
assert nhanes_client.query_count == 2
```

Also assert each embedder receives only the probes sent to its selected
context; merely binding two studies must not embed anything.

- [ ] **Step 2: Run the isolation tests**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_session_studies.py tests/test_db_rag_agent_tools.py -k 'isolation or catalog'
```

Expected: PASS. If it fails, fix source propagation or client selection without weakening the assertions.

- [ ] **Step 3: Commit**

```bash
git add tests/test_session_studies.py tests/test_db_rag_agent_tools.py
git commit -m "test: prove multi-study semantic catalog isolation"
```

---

### Task 7: Add and Run the Real RePORT-plus-NHANES Smoke

**Files:**
- Create: `scripts/smoke_multi_study_semantic_catalog.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: two format-v3 archives, `OPENAI_API_KEY`, `install_study_archives`, `discover_studies`, and `bind_session_studies`.
- Produces: an executable internal-backend smoke with bounded JSON diagnostics and no persistent study installation.

- [ ] **Step 1: Write the smoke script**

Implement a Python 3.12 CLI with required `--report-archive` and
`--nhanes-archive` paths. It must:

```python
with tempfile.TemporaryDirectory() as temporary:
    studies_root = Path(temporary) / "studies"
    install_study_archives([report_archive, nhanes_archive], studies_root)
    discovered = discover_studies(studies_root)
    bound = bind_session_studies(
        discovered,
        api_key=os.environ["OPENAI_API_KEY"],
        expected_embedding_model="OpenAI/text-embedding-3-large",
    )
```

Then search the selected NHANES catalog for
`"long-term blood sugar control glycohemoglobin"` and selected RePORT catalog
for `"manufactured cigarette smoking intensity per day"`. Assert NHANES
returns `GHB_J.LBXGH`, RePORT returns the expected smoking field such as
`CIGPAST`, every hit has only the selected `source_id`, and every returned hit
has `vector` in `matched_by`. Open each selected DuckDB read-only and assert it
contains at least one table. Print bounded JSON containing study IDs, matched
fields, retrieval modes, table counts, and elapsed stages; never print the API
key, embeddings, or raw participant rows.

- [ ] **Step 2: Add the documented command**

Document:

```bash
PYTHONPATH=. .venv/bin/python scripts/smoke_multi_study_semantic_catalog.py \
  --report-archive ../Database/report-india-synthetic/delivery/report-india-synthetic-0.3.0.tar.gz \
  --nhanes-archive ../Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.1.0.tar.gz
```

State that `OPENAI_API_KEY` is required and the smoke installs only into a
temporary directory.

- [ ] **Step 3: Run focused unit tests before the real smoke**

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_db_rag_catalog.py \
  tests/test_db_rag_retrieval.py \
  tests/test_session_studies.py \
  tests/test_installed_study_bundle.py \
  tests/test_db_rag_agent_tools.py \
  tests/test_graph_studies.py \
  tests/test_no_study_startup.py
```

Expected: all pass.

- [ ] **Step 4: Run the dedicated real smoke once**

Run the command from Step 2 once with a five-minute tool timeout.

Expected: exit code 0 and diagnostics showing correct semantic hits and no
cross-study sources. If it fails or times out, do not rerun automatically;
preserve the traceback and diagnostics and report the failure.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_multi_study_semantic_catalog.py README.md
git commit -m "test: smoke multi-study semantic catalog retrieval"
```

---

### Task 8: Full Regression Verification

**Files:**
- Verify only; modify files only for failures caused by this feature.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a clean, tested branch with no silent lexical fallback and no unrelated-file changes.

- [ ] **Step 1: Run the complete Python suite**

```bash
PYTHONPATH=. .venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run source and delivery checks**

```bash
PYTHONPATH=. .venv/bin/python scripts/verify_working_demo_delivery.py
git diff --check
git status --short
```

Expected: delivery verification passes; no whitespace errors; only the pre-existing user change to `.superpowers/sdd/task-5-report.md` may remain unstaged.

- [ ] **Step 3: Inspect the final diff for the safety contract**

Verify with:

```bash
rg -n "SEMANTIC_CATALOG_UNAVAILABLE|hybrid_vector_lexical|bind_session_studies" \
  db_rag epi_agent api graph tests scripts
rg -n "except.*SemanticCatalogUnavailable|lexical" db_rag/catalog.py epi_agent/db_rag/tools.py
```

Confirm there is no exception path that catches an embedding or Chroma failure
and returns lexical search results.

- [ ] **Step 4: Commit any verification-only correction**

If verification required an in-scope correction:

```bash
git add <only-the-corrected-feature-files>
git commit -m "fix: close semantic catalog verification gaps"
```

If no correction was needed, do not create an empty commit.
