# Mandatory Semantic Publication Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove lexical-only catalog search and make publication evidence search require per-study vector retrieval while retaining deterministic lexical boosting and exact source opening.

**Architecture:** `SchemaCatalog` becomes exact-operation-only and fails closed for search. `LocalPublicationKnowledge` validates files, opens exact sources, and produces lexical candidates; session binding wraps it in either a semantic hybrid provider or an explicit unavailable provider. The publication tool translates semantic provider failure into a typed recoverable tool error.

**Tech Stack:** Python 3.12, Pydantic, ChromaDB, pytest.

## Global Constraints

- Do not change the study package format, builder, or installer.
- Do not change `dbrag-inspect_table`, dataset-plan behavior, or study routing.
- Vector retrieval is mandatory for catalog and publication search.
- Lexical ranking may boost successful vector retrieval but may never replace failed vector retrieval.
- Preserve exact `publication-open_study_source` behavior.

---

### Task 1: Remove Base Catalog Lexical Search

**Files:**
- Modify: `tests/test_db_rag_catalog.py`
- Modify: `db_rag/catalog.py`

**Interfaces:**
- Consumes: `SchemaCatalog.search_many(queries: list[str], *, limit: int)`.
- Produces: base search raises `SemanticCatalogUnavailableError` for non-empty, positive-limit searches; `inspect_table()` and `field_exists()` remain exact operations.

- [ ] **Step 1: Write the failing catalog regression test**

Add a test that constructs a base catalog containing a lexical match and asserts:

```python
catalog = SchemaCatalog(catalog_payload, default_source_id="study-1")
with pytest.raises(SemanticCatalogUnavailableError):
    catalog.search_many(["glycohemoglobin"], limit=5)
assert catalog.inspect_table("study-1", "GHB_J")
assert catalog.field_exists("GHB_J", "LBXGH")
```

- [ ] **Step 2: Run the test and verify RED**

Run: `pytest -q tests/test_db_rag_catalog.py -k base_catalog`

Expected: FAIL because `SchemaCatalog.search_many()` returns the lexical row.

- [ ] **Step 3: Remove the stale fallback**

Change `SchemaCatalog.search_many()` so it preserves empty-query and non-positive-limit neutral returns, then raises:

```python
raise SemanticCatalogUnavailableError(
    "Semantic catalog retrieval is unavailable for the selected study."
)
```

Remove the base class collection-dependent search branch and constructor state that no longer belongs to the exact catalog.

- [ ] **Step 4: Run focused catalog tests and verify GREEN**

Run: `pytest -q tests/test_db_rag_catalog.py tests/test_db_rag_agent_tools.py`

Expected: PASS.

---

### Task 2: Add Mandatory Hybrid Publication Retrieval

**Files:**
- Modify: `tests/test_local_publication_knowledge.py`
- Modify: `db_rag/local_knowledge.py`

**Interfaces:**
- Produces: `SemanticPublicationKnowledgeUnavailableError`.
- Produces: `LocalPublicationKnowledge.search_lexical(query: str, *, limit: int = 5) -> list[PublicationEvidenceHit]`.
- Produces: `SemanticPublicationKnowledge(local, collection, embedding_function)` with `search()` and delegated `open_source()`.
- Produces: `UnavailableSemanticPublicationKnowledge(local)` with failing `search()` and delegated `open_source()`.

- [ ] **Step 1: Write failing provider tests**

Add real-provider tests with small fake Chroma and embedding objects that assert:

```python
knowledge = SemanticPublicationKnowledge(local, collection, embedder)
hits = knowledge.search("cohort eligibility", limit=3)
assert embedder.calls == [["cohort eligibility"]]
assert collection.query_calls[0]["where"] == {"source_kind": "publication"}
assert hits[0].id == expected_fused_first_id
```

Add a failure test:

```python
with pytest.raises(SemanticPublicationKnowledgeUnavailableError):
    SemanticPublicationKnowledge(local, failing_collection, embedder).search(
        "cohort eligibility", limit=3
    )
```

Also prove `open_source()` works through both the semantic and unavailable wrappers.

- [ ] **Step 2: Run provider tests and verify RED**

Run: `pytest -q tests/test_local_publication_knowledge.py`

Expected: collection errors or missing imports because semantic publication providers do not exist.

- [ ] **Step 3: Implement the minimal hybrid providers**

Rename the existing local `search()` implementation to `search_lexical()`. Add a semantic provider that:

```python
embeddings = embedding_function.embed_query([query])
result = collection.query(
    query_embeddings=embeddings,
    n_results=candidate_limit,
    where={"source_kind": "publication"},
    include=["metadatas"],
)
```

Reconstruct bounded `PublicationEvidenceHit` values from validated Chroma metadata, rank-fuse vector and lexical lists by stable chunk ID, and raise the typed unavailable error for embedding, collection, or malformed response failures. Delegate exact opening to the local provider.

- [ ] **Step 4: Run provider tests and verify GREEN**

Run: `pytest -q tests/test_local_publication_knowledge.py`

Expected: PASS.

---

### Task 3: Bind Publication Vectors Per Study and Expose Typed Tool Failure

**Files:**
- Modify: `tests/test_session_studies.py`
- Create: `tests/test_epi_agent_publication_tools.py`
- Modify: `db_rag/session_studies.py`
- Modify: `epi_agent/tool_packs/publication/tools.py`

**Interfaces:**
- Consumes: the three publication providers from Task 2.
- Produces: session-local `study_knowledge` binding only for studies with local publication knowledge.
- Produces: recoverable tool error code `SEMANTIC_STUDY_KNOWLEDGE_UNAVAILABLE`.

- [ ] **Step 1: Write failing session and tool tests**

Extend the session fake client to record `study_knowledge` requests and return study-specific publication metadata. Assert a RePORT-like study with local publication knowledge requests its own collection while NHANES with `knowledge=None` does not. Search both bound studies and prove no publication collection crosses study roots.

Add a tool test with a provider whose `search()` raises `SemanticPublicationKnowledgeUnavailableError` and assert:

```python
assert raised.value.code == "SEMANTIC_STUDY_KNOWLEDGE_UNAVAILABLE"
assert raised.value.recoverable is True
```

- [ ] **Step 2: Run integration tests and verify RED**

Run: `pytest -q tests/test_session_studies.py tests/test_epi_agent_publication_tools.py`

Expected: FAIL because publication collections are not bound and the tool does not translate the typed error.

- [ ] **Step 3: Implement session binding and error translation**

In `bind_session_studies()`, after table and column collection binding succeeds, request `study_knowledge` only when `study.knowledge` is a `LocalPublicationKnowledge`. Replace it with `SemanticPublicationKnowledge`. If that optional collection binding fails, use `UnavailableSemanticPublicationKnowledge` while leaving the semantic schema catalog and overall DB-RAG readiness intact. When catalog readiness itself is unavailable, wrap local publication knowledge as unavailable so no lexical search path is exposed.

In the publication tool, catch `SemanticPublicationKnowledgeUnavailableError` around `search()` and raise:

```python
ToolExecutionError(
    "SEMANTIC_STUDY_KNOWLEDGE_UNAVAILABLE",
    str(error),
    recoverable=True,
)
```

- [ ] **Step 4: Run integration tests and verify GREEN**

Run: `pytest -q tests/test_session_studies.py tests/test_epi_agent_publication_tools.py`

Expected: PASS.

---

### Task 4: Regression Verification

**Files:**
- Test only.

**Interfaces:**
- Verifies the completed runtime behavior without modifying package construction.

- [ ] **Step 1: Run the focused retrieval suite**

Run:

```bash
pytest -q \
  tests/test_db_rag_catalog.py \
  tests/test_db_rag_agent_tools.py \
  tests/test_local_publication_knowledge.py \
  tests/test_session_studies.py \
  tests/test_epi_agent_publication_tools.py \
  tests/test_report_study_bundle.py \
  tests/test_study_package_installer.py
```

Expected: PASS.

- [ ] **Step 2: Run repository-wide tests**

Run: `pytest -q`

Expected: PASS with zero failures.

- [ ] **Step 3: Inspect the final diff**

Run: `git diff --check && git status --short && git diff --stat`

Expected: no whitespace errors; only the approved runtime, test, spec, and plan files are changed.
