# Embedding-Aware Lexical Retrieval Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make catalog, publication-evidence, and study-design searches continue with explicit lexical-only results whenever the configured embedding service is unavailable.

**Architecture:** Add one shared immutable retrieval-status contract, then let each provider expose an additive status-returning search method while preserving its existing list/tuple-returning method. Tools consume the richer method when available and persist the retrieval mode plus a sanitized embedding explanation. Existing hybrid ranking stays intact; local catalog data, verified publication chunks, and Markdown source sections supply deterministic fallback candidates.

**Tech Stack:** Python 3.12, dataclasses, Pydantic, ChromaDB, pytest, existing `ToolResult`/artifact contracts.

## Global Constraints

- `hybrid_vector_lexical` remains the mode when vector and lexical retrieval both run.
- `lexical_fallback` is a successful degraded result and never raises `ToolExecutionError` merely because embeddings are unavailable.
- Every fallback identifies the embedding model, a stable reason code, and a sanitized explicit message.
- Never include API keys, provider response bodies, or raw exception text in tool output.
- Invalid arguments, corrupt local evidence, malformed vector provenance, and artifact persistence failures remain errors.
- Existing tool names, artifact kinds, exact-inspection behavior, and hybrid result fields remain compatible.

---

### Task 1: Shared Retrieval Status Contract

**Files:**
- Create: `db_rag/retrieval_status.py`
- Create: `tests/test_retrieval_status.py`

**Interfaces:**
- Produces: `RetrievalStatus`, `RetrievalOutcome[T]`, `hybrid_status(model)`, and `lexical_fallback_status(model, reason_code)`.
- `RetrievalStatus.as_dict()` returns the exact additive tool-result payload.

- [ ] **Step 1: Write the failing status-contract tests**

```python
from db_rag.retrieval_status import (
    RetrievalOutcome,
    hybrid_status,
    lexical_fallback_status,
)


def test_missing_credentials_status_is_explicit_and_sanitized() -> None:
    status = lexical_fallback_status(
        "OpenAI/text-embedding-3-large",
        "EMBEDDING_CREDENTIALS_MISSING",
    )
    assert status.mode == "lexical_fallback"
    assert status.as_dict() == {
        "available": False,
        "model": "OpenAI/text-embedding-3-large",
        "reason_code": "EMBEDDING_CREDENTIALS_MISSING",
        "message": (
            "Embedding model OpenAI/text-embedding-3-large is unavailable "
            "because OPENAI_API_KEY is not configured. Results use lexical "
            "string search only."
        ),
    }


def test_hybrid_status_contains_no_failure_reason() -> None:
    status = hybrid_status("OpenAI/text-embedding-3-large")
    assert status.mode == "hybrid_vector_lexical"
    assert status.as_dict() == {
        "available": True,
        "model": "OpenAI/text-embedding-3-large",
    }
    assert RetrievalOutcome(value=("hit",), status=status).value == ("hit",)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/bin/pytest tests/test_retrieval_status.py -q`

Expected: collection error because `db_rag.retrieval_status` does not exist.

- [ ] **Step 3: Implement the immutable status contract**

```python
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

T = TypeVar("T")
RetrievalMode = Literal["hybrid_vector_lexical", "lexical_fallback"]
EmbeddingReasonCode = Literal[
    "EMBEDDING_CREDENTIALS_MISSING",
    "EMBEDDING_CONFIGURATION_UNAVAILABLE",
    "EMBEDDING_INDEX_UNAVAILABLE",
    "EMBEDDING_PROVIDER_UNAVAILABLE",
]

_REASONS = {
    "EMBEDDING_CREDENTIALS_MISSING": "OPENAI_API_KEY is not configured",
    "EMBEDDING_CONFIGURATION_UNAVAILABLE": "its configuration is unavailable or incompatible",
    "EMBEDDING_INDEX_UNAVAILABLE": "the semantic index is unavailable",
    "EMBEDDING_PROVIDER_UNAVAILABLE": "the embedding provider could not complete the query",
}


@dataclass(frozen=True)
class RetrievalStatus:
    mode: RetrievalMode
    model: str
    available: bool
    reason_code: EmbeddingReasonCode | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "available": self.available,
            "model": self.model,
        }
        if self.reason_code is not None:
            payload.update(
                reason_code=self.reason_code,
                message=(
                    f"Embedding model {self.model} is unavailable because "
                    f"{_REASONS[self.reason_code]}. Results use lexical string "
                    "search only."
                ),
            )
        return payload


@dataclass(frozen=True)
class RetrievalOutcome(Generic[T]):
    value: T
    status: RetrievalStatus
```

Add `hybrid_status` and `lexical_fallback_status` constructors that build these exact values and reject blank model names.

- [ ] **Step 4: Run the status tests and verify GREEN**

Run: `.venv/bin/pytest tests/test_retrieval_status.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add db_rag/retrieval_status.py tests/test_retrieval_status.py
git commit -m "feat: define retrieval fallback status"
```

---

### Task 2: Catalog Lexical Fallback and Tool Metadata

**Files:**
- Modify: `db_rag/catalog.py`
- Modify: `db_rag/session_studies.py`
- Modify: `epi_agent/db_rag/tools.py`
- Modify: `tests/test_db_rag_catalog.py`
- Modify: `tests/test_db_rag_agent_tools.py`
- Modify: `tests/test_session_studies.py`

**Interfaces:**
- Consumes: `RetrievalOutcome[list[list[SchemaEvidenceHit]]]` and `RetrievalStatus` from Task 1.
- Produces: `SchemaCatalog.search_many_with_status(queries, *, limit)`, while `search_many` remains compatible and returns only `.value`.

- [ ] **Step 1: Replace fail-closed catalog expectations with failing fallback tests**

Update the existing tests named `test_semantic_catalog_never_returns_lexical_only_after_vector_failure` and `test_unavailable_semantic_catalog_allows_inspection_but_fails_search` so they assert lexical hits, `matched_by == ("lexical",)`, `status.mode == "lexical_fallback"`, and stable `EMBEDDING_PROVIDER_UNAVAILABLE` / `EMBEDDING_CREDENTIALS_MISSING` reason codes. Add a tool-level test whose fake catalog exposes `search_many_with_status` and assert the persisted artifact contains:

```python
assert observation["retrieval_mode"] == "lexical_fallback"
assert observation["embedding"] == {
    "available": False,
    "model": "OpenAI/text-embedding-3-large",
    "reason_code": "EMBEDDING_CREDENTIALS_MISSING",
    "message": (
        "Embedding model OpenAI/text-embedding-3-large is unavailable because "
        "OPENAI_API_KEY is not configured. Results use lexical string search only."
    ),
}
assert observation["retrieval_summary"]["vector_hits"] == 0
assert observation["retrieval_summary"]["lexical_hits"] >= 1
```

- [ ] **Step 2: Run focused catalog tests and verify RED**

Run: `.venv/bin/pytest tests/test_db_rag_catalog.py tests/test_db_rag_agent_tools.py tests/test_session_studies.py -q`

Expected: failures because unavailable catalogs still raise and the tool hardcodes `hybrid_vector_lexical`.

- [ ] **Step 3: Restore lexical-only provider behavior**

In `SchemaCatalog`, add `embedding_model` and `unavailable_reason_code` constructor fields. Implement `search_many_with_status` by calling the existing `_lexical_ranked_rows` and `_fuse_ranked_rows` with an empty vector list. Make `search_many` return `search_many_with_status(...).value`.

In `SemanticSchemaCatalog.search_many_with_status`, preserve the current embedding, vector retrieval, and fusion logic. If query embedding fails, return lexical batches with `EMBEDDING_PROVIDER_UNAVAILABLE`; if Chroma retrieval itself is unavailable, return lexical batches with `EMBEDDING_INDEX_UNAVAILABLE`. Keep invalid embedding counts and malformed returned evidence as `SemanticCatalogUnavailableError` so corrupt results do not masquerade as a valid fallback.

In `bind_session_studies`, do not construct a semantic catalog when `api_key.strip()` is empty. Bind `UnavailableSemanticSchemaCatalog` with the packaged catalog, configured model, and `EMBEDDING_CREDENTIALS_MISSING`. Preserve configuration/index-specific reason codes for the other unavailable branches.

- [ ] **Step 4: Teach the catalog tool to consume status**

Use `search_many_with_status` when callable. For legacy/fake providers that only implement `search_many`, wrap the returned batches with `hybrid_status(getattr(study.catalog, "embedding_model", EMBEDDING_MODEL))`. Populate:

```python
content = {
    "study_id": study.study_id,
    "queries": queries,
    "source_ids": source_ids,
    "retrieval_mode": outcome.status.mode,
    "embedding": outcome.status.as_dict(),
    "retrieval_summary": retrieval_summary,
    "probes": probe_results,
}
```

Remove the conversion of ordinary embedding unavailability into `ToolExecutionError`; retain translation for malformed-provider contract failures.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `.venv/bin/pytest tests/test_db_rag_catalog.py tests/test_db_rag_agent_tools.py tests/test_session_studies.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add db_rag/catalog.py db_rag/session_studies.py epi_agent/db_rag/tools.py tests/test_db_rag_catalog.py tests/test_db_rag_agent_tools.py tests/test_session_studies.py
git commit -m "feat: restore catalog lexical fallback"
```

---

### Task 3: Publication Evidence Lexical Fallback

**Files:**
- Modify: `db_rag/local_knowledge.py`
- Modify: `db_rag/session_studies.py`
- Modify: `epi_agent/tool_packs/publication/tools.py`
- Modify: `tests/test_semantic_publication_knowledge.py`
- Modify: `tests/test_epi_agent_publication_tools.py`

**Interfaces:**
- Consumes: Task 1 retrieval status contract.
- Produces: `search_with_status(query, *, limit) -> RetrievalOutcome[list[PublicationEvidenceHit]]` on local, semantic, and unavailable publication providers.

- [ ] **Step 1: Write failing provider and tool fallback tests**

Change `test_semantic_publication_search_never_falls_back_after_vector_failure` to assert lexical evidence and `EMBEDDING_PROVIDER_UNAVAILABLE`. Change the unavailable-provider test to assert its lexical results and missing-credentials status while preserving exact-source opening. Replace `test_publication_tool_translates_semantic_unavailability` with a test asserting a normal `ToolResult`, `retrieval_mode == "lexical_fallback"`, explicit embedding metadata, and no raised `ToolExecutionError`.

Retain tests that reject unverified IDs, empty vector partitions, and stale vector metadata; those integrity failures must continue to raise `SemanticPublicationKnowledgeUnavailableError`.

- [ ] **Step 2: Run focused publication tests and verify RED**

Run: `.venv/bin/pytest tests/test_semantic_publication_knowledge.py tests/test_epi_agent_publication_tools.py -q`

Expected: fallback assertions fail because semantic and unavailable wrappers still raise.

- [ ] **Step 3: Implement provider fallback without weakening provenance checks**

Add `search_with_status(self, query, *, limit=5, embedding_model=EMBEDDING_MODEL, reason_code="EMBEDDING_CONFIGURATION_UNAVAILABLE")` to `LocalPublicationKnowledge`, returning `search_lexical` with `lexical_fallback_status(embedding_model, reason_code)`. In `SemanticPublicationKnowledge`, separate embedding/collection availability exceptions from subsequent vector-result validation. Availability failures return `_local.search_lexical(...)` with a sanitized fallback reason. Invalid counts, unknown IDs, stale metadata, and empty verified partitions continue raising the existing exception. The compatibility `search` method returns `.value`.

Give `UnavailableSemanticPublicationKnowledge` `embedding_model` and `reason_code` fields and delegate `search_with_status` to `_local.search_lexical`; leave `open_source` unchanged.

- [ ] **Step 4: Add publication tool result metadata**

Use `search_with_status` when present, otherwise preserve legacy providers by wrapping their `search` results in a hybrid status. Persist and return:

```python
content = {
    "study_id": study.study_id,
    "query": arguments["query"],
    "retrieval_mode": outcome.status.mode,
    "embedding": outcome.status.as_dict(),
    "hits": hits,
}
```

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `.venv/bin/pytest tests/test_semantic_publication_knowledge.py tests/test_epi_agent_publication_tools.py tests/test_session_studies.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add db_rag/local_knowledge.py db_rag/session_studies.py epi_agent/tool_packs/publication/tools.py tests/test_semantic_publication_knowledge.py tests/test_epi_agent_publication_tools.py tests/test_session_studies.py
git commit -m "feat: add publication lexical fallback"
```

---

### Task 4: Markdown Study-Design Lexical Search

**Files:**
- Modify: `db_rag/study_design_documents.py`
- Modify: `epi_agent/studies.py`
- Modify: `epi_agent/tool_packs/study_design/tools.py`
- Modify: `tests/test_study_design_documents.py`
- Modify: `tests/test_study_design_tools.py`

**Interfaces:**
- Consumes: Task 1 retrieval status contract.
- Produces: `MarkdownStudyDesign.search_with_status(query, limit=5)` and an extended `SearchableStudyDesignProvider` protocol.

- [ ] **Step 1: Write failing lexical Markdown and tool-result tests**

Create a package fixture with `overview.md` and `reference/visits.md`, remove `OPENAI_API_KEY`, and assert:

```python
outcome = provider.search_with_status("household visit schedule", limit=3)
assert outcome.status.mode == "lexical_fallback"
assert outcome.status.reason_code == "EMBEDDING_CREDENTIALS_MISSING"
assert outcome.value[0].source_path == "reference/visits.md"
assert outcome.value[0].section == "Visits"
assert outcome.value[0].source_sha256 == hashlib.sha256(
    (provider.design_root / "reference/visits.md").read_bytes()
).hexdigest()
```

Extend `_SearchableDesign` in the tool tests with `search_with_status`, then assert the saved artifact and model message contain `retrieval_mode`, `embedding`, and bounded hits. Add a test showing fallback returns a normal result rather than a tool error.

- [ ] **Step 2: Run focused study-design tests and verify RED**

Run: `.venv/bin/pytest tests/test_study_design_documents.py tests/test_study_design_tools.py -q`

Expected: failures because there is no design root, Markdown scorer, or status-returning search.

- [ ] **Step 3: Implement deterministic Markdown section indexing**

Store `design_root` on `MarkdownStudyDesign`. Enumerate only regular, non-symlinked `*.md` files below that resolved root. Split each document at ATX headings (`#` through `######`), retaining `Document` for pre-heading text. Create `StudyDesignHit` values with a stable source ID derived from relative path plus section ordinal, relative POSIX source path, file SHA-256, heading, bounded section body, and `distance=None`.

Rank using normalized alphanumeric query tokens, excluding the same small English stopword set as catalog retrieval. Score exact normalized heading phrase, heading token overlap, body phrase, body token overlap, then source path and section ordinal for deterministic ties. Return no unrelated sections.

- [ ] **Step 4: Preserve semantic search and add availability fallback**

When `OPENAI_API_KEY` is missing, return Markdown lexical hits and `EMBEDDING_CREDENTIALS_MISSING` without opening Chroma. When configured, embed the query explicitly and query Chroma with `query_embeddings`; embedding failures return lexical hits with `EMBEDDING_PROVIDER_UNAVAILABLE`, while index-open/query availability failures return lexical hits with `EMBEDDING_INDEX_UNAVAILABLE`. Validate metadata and result lengths before mapping hits; malformed provenance remains an error.

Keep `search()` as a compatibility wrapper returning `search_with_status(...).value`.

- [ ] **Step 5: Add status metadata to the study-design tool**

Call `search_with_status` when available and otherwise wrap legacy search results as hybrid. Save and return:

```python
content = {
    "study_id": study.study_id,
    "query": arguments["query"],
    "retrieval_mode": outcome.status.mode,
    "embedding": outcome.status.as_dict(),
    "hits": hits,
}
```

- [ ] **Step 6: Run focused tests and verify GREEN**

Run: `.venv/bin/pytest tests/test_study_design_documents.py tests/test_study_design_tools.py -q`

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add db_rag/study_design_documents.py epi_agent/studies.py epi_agent/tool_packs/study_design/tools.py tests/test_study_design_documents.py tests/test_study_design_tools.py
git commit -m "feat: add study design lexical fallback"
```

---

### Task 5: Cross-Tool Regression and Documentation Verification

**Files:**
- Modify: `README.md`
- Modify: `tests/test_multi_study_evidence_tools.py`

**Interfaces:**
- Consumes: completed fallback behavior from Tasks 2–4.
- Produces: one end-to-end regression proving a Claude-only environment can perform all three searches without an OpenAI key.

- [ ] **Step 1: Write the failing cross-tool regression**

Build the installed-study registry with an empty embedding key, invoke all three search tools with queries known to match packaged local evidence, and assert each returns a `ToolResult` whose JSON message has `retrieval_mode == "lexical_fallback"`, `embedding.available is False`, and non-empty hits/probes. Assert no call raises `ToolExecutionError`.

- [ ] **Step 2: Run the cross-tool regression**

Run: `.venv/bin/pytest tests/test_multi_study_evidence_tools.py -q`

Expected: PASS because Tasks 2–4 provide the unit-level red/green cycles and complete the integration wiring.

- [ ] **Step 3: Document Claude-only degraded retrieval**

Update the model/credential section of `README.md` to state that `OPENAI_API_KEY` enables hybrid semantic-plus-lexical evidence retrieval. Without it, Claude and compatible chat providers remain usable and the three evidence-search tools return explicitly labeled lexical-only results. Keep study-package index creation documented as requiring embedding credentials.

- [ ] **Step 4: Run full verification**

Run:

```bash
.venv/bin/pytest -q
git diff --check
```

Expected: the complete suite passes with zero failures and `git diff --check` emits no output.

- [ ] **Step 5: Inspect the final diff against the design**

Run: `git diff --stat HEAD~4 && git status --short`

Confirm that only retrieval status, the three provider/tool paths, their tests, and the credential documentation changed.

- [ ] **Step 6: Commit**

```bash
git add README.md tests/test_multi_study_evidence_tools.py
git commit -m "test: verify embedding-free evidence retrieval"
```
