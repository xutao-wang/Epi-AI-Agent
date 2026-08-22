# Consistent Hybrid Evidence Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make catalog, reviewed-publication, and study-design search use one truthful union-based vector-plus-lexical retrieval contract with equal reciprocal-rank weights and auditable hit provenance.

**Architecture:** Preserve catalog as the reference implementation, change publication fusion to admit lexical-only candidates, and give Markdown study-design search a locally verified section inventory plus vector/lexical rank fusion. Provider-specific evidence types retain their own validation and rendering, while all three report `hybrid_vector_lexical` only after both branches run successfully.

**Tech Stack:** Python 3.12, Pydantic/dataclasses, ChromaDB, pytest, existing Epi-Agent tool registries and artifact store.

## Global Constraints

- Use Python 3.12 through `.venv/bin/python` and `.venv/bin/pytest`.
- Reciprocal-rank fusion uses `K=60` and equal vector and lexical weights of `1.0`.
- Publication and study-design branches each retrieve at most `2 * requested_limit` candidates before fusion.
- Catalog retains its current table, column, and lexical candidate bounds plus exact-identifier priority.
- Hybrid fusion admits vector-only, lexical-only, and dual-matched candidates.
- Every hybrid hit records `matched_by` in stable `vector`, `lexical` order.
- Embedding unavailability remains a successful `lexical_fallback` with sanitized status metadata.
- Unknown, stale, duplicated, malformed, or empty vector evidence partitions remain integrity errors.
- Existing tool names, artifact kinds, exact-opening behavior, and final result limits remain compatible.
- The dedicated real smoke must use the real embedding provider, installed study packages, Chroma indexes, providers, tool registries, and artifact store without stubs.

---

### Task 1: Publication Union-Based Rank Fusion

**Files:**
- Modify: `tests/test_semantic_publication_knowledge.py`
- Modify: `tests/test_epi_agent_publication_tools.py`
- Modify: `db_rag/local_knowledge.py:247-282`

**Interfaces:**
- Consumes: verified `PublicationEvidenceHit` lists from vector and local lexical retrieval.
- Produces: `_fuse_hits(vector_hits, lexical_hits, *, limit) -> list[PublicationEvidenceHit]` that unions by `hit.id`, uses equal RRF weights, and writes comma-separated `provenance["matched_by"]` in stable order.

- [ ] **Step 1: Replace the vector-gating regression with a failing union-fusion test**

Replace `test_semantic_publication_fusion_never_replaces_vector_candidates` with a focused test whose vector ranking is `[dual, vector_only]` and lexical ranking is `[lexical_only, dual]`:

```python
def test_publication_fusion_unions_vector_and_lexical_candidates() -> None:
    dual = local_knowledge._hit(_chunk(
        "publication.dual",
        title="Cohort eligibility",
        text="Cohort eligibility.",
    ))
    vector_only = local_knowledge._hit(_chunk(
        "publication.vector-only",
        title="Renal outcomes",
        text="Kidney outcomes.",
    ))
    lexical_only = local_knowledge._hit(_chunk(
        "publication.lexical-only",
        title="Cohort definition",
        text="Cohort enrollment.",
    ))

    hits = local_knowledge._fuse_hits(
        [dual, vector_only],
        [lexical_only, dual],
        limit=3,
    )

    assert [hit.id for hit in hits] == [
        "publication.dual",
        "publication.lexical-only",
        "publication.vector-only",
    ]
    assert [hit.provenance["matched_by"] for hit in hits] == [
        "vector,lexical",
        "lexical",
        "vector",
    ]
```

This uses the existing `local_knowledge` module alias so the test exercises the production evidence shape. In `test_semantic_publication_search_requires_vector_and_boosts_lexical`, add `assert collection.calls[0]["n_results"] == 4` because its final limit is two.

- [ ] **Step 2: Run the publication test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_semantic_publication_knowledge.py::test_publication_fusion_unions_vector_and_lexical_candidates -q
```

Expected: FAIL because lexical-only candidates are currently skipped.

- [ ] **Step 3: Implement equal-weight union fusion**

Change `_fuse_hits` so both ranked inputs can introduce candidates:

```python
def _fuse_hits(
    vector_hits: list[PublicationEvidenceHit],
    lexical_hits: list[PublicationEvidenceHit],
    *,
    limit: int,
) -> list[PublicationEvidenceHit]:
    scores: dict[str, float] = {}
    hits_by_id: dict[str, PublicationEvidenceHit] = {}
    matched_by: dict[str, list[str]] = {}
    for mode, hits in (("vector", vector_hits), ("lexical", lexical_hits)):
        seen: set[str] = set()
        for rank, hit in enumerate(hits, start=1):
            if hit.id in seen:
                continue
            seen.add(hit.id)
            hits_by_id.setdefault(hit.id, hit)
            scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (_RRF_K + rank)
            matched_by.setdefault(hit.id, []).append(mode)
    ordered_ids = sorted(
        scores,
        key=lambda chunk_id: (-scores[chunk_id], chunk_id),
    )[:limit]
    return [
        hits_by_id[chunk_id].model_copy(
            update={
                "provenance": {
                    **hits_by_id[chunk_id].provenance,
                    "matched_by": ",".join(matched_by[chunk_id]),
                }
            }
        )
        for chunk_id in ordered_ids
    ]
```

- [ ] **Step 4: Verify publication provider and tool contracts GREEN**

Run:

```bash
.venv/bin/pytest tests/test_semantic_publication_knowledge.py tests/test_epi_agent_publication_tools.py -q
```

Expected: all selected tests pass; hybrid tool artifacts retain `matched_by` and fallback tests remain unchanged.

- [ ] **Step 5: Commit the publication deliverable**

```bash
git add db_rag/local_knowledge.py tests/test_semantic_publication_knowledge.py tests/test_epi_agent_publication_tools.py
git commit -m "feat: union publication hybrid candidates"
```

---

### Task 2: Authoritative Study-Design Section Inventory

**Files:**
- Modify: `tests/test_study_design_documents.py`
- Modify: `db_rag/study_design_documents.py:29-263`

**Interfaces:**
- Produces: `StudyDesignHit.id: str` as the section evidence ID and `StudyDesignHit.matched_by: tuple[Literal["vector", "lexical"], ...]`.
- Produces: `MarkdownStudyDesign._local_sections() -> tuple[StudyDesignHit, ...]` with file and section identities matching packaged Chroma rows.
- Produces: `_study_design_source_id(relative_path) -> str` and `_study_design_hit_id(source_id, section, chunk_ordinal, text) -> str` using the installed package identity formulas.

- [ ] **Step 1: Write failing local-identity and vector-validation tests**

Update the recording collection fixture to return its Chroma `ids` and metadata generated from the real Markdown fixture. Add this exact identity test:

```python
sections = provider._local_sections()
visits = next(hit for hit in sections if hit.section == "Visits")

assert visits.source_id == (
    "study-design-source."
    + hashlib.sha256(b"reference/visits.md").hexdigest()[:24]
)
assert visits.id.startswith("study-design.")
assert visits.source_sha256 == hashlib.sha256(
    (provider.design_root / "reference/visits.md").read_bytes()
).hexdigest()
assert visits.matched_by == ()
```

Add five separately named tests—`test_markdown_study_design_rejects_unknown_vector_id`, `test_markdown_study_design_rejects_stale_vector_hash`, `test_markdown_study_design_rejects_inconsistent_vector_text`, `test_markdown_study_design_rejects_duplicate_vector_id`, and `test_markdown_study_design_rejects_empty_vector_partition`. Each invokes `search_with_status` on a recording collection containing only the named defect and asserts `pytest.raises(StudyDesignKnowledgeUnavailableError)`.

- [ ] **Step 2: Run the identity tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_study_design_documents.py -q
```

Expected: failures because `StudyDesignHit` has no section evidence ID or match modes and vector metadata is not validated against local Markdown.

- [ ] **Step 3: Add canonical identity helpers and a dedicated integrity error**

Add:

```python
_RRF_K = 60


class StudyDesignKnowledgeUnavailableError(RuntimeError):
    """Semantic study-design evidence failed integrity validation."""


def _study_design_source_id(relative_path: str) -> str:
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]
    return f"study-design-source.{digest}"


def _study_design_hit_id(
    source_id: str,
    section: str,
    chunk_ordinal: int,
    text: str,
) -> str:
    value = f"{source_id}:{section}:{chunk_ordinal}:{text}"
    return f"study-design.{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"
```

Extend the hit type:

```python
@dataclass(frozen=True)
class StudyDesignHit:
    id: str
    source_kind: Literal["study_design"]
    source_id: str
    source_path: str
    source_sha256: str
    section: str
    text: str
    distance: float | None
    matched_by: tuple[Literal["vector", "lexical"], ...] = ()
```

- [ ] **Step 4: Extract authoritative Markdown parsing into `_local_sections`**

Move the existing safe Markdown enumeration and ATX-heading splitting out of `_lexical_outcome`. For each section, use `chunk_ordinal=0`, the file-level source ID, and the canonical section evidence ID. Return all sections before query ranking; retain symlink, containment, UTF-8, and file-hash behavior.

- [ ] **Step 5: Validate every vector row against the local inventory**

Request `ids` from the Chroma result alongside metadata/documents/distances. Build `local_by_id = {hit.id: hit for hit in local_sections}` and reject:

```python
if not ids or len(ids) != len(metadatas):
    raise StudyDesignKnowledgeUnavailableError(
        "Study-design vector result is empty or malformed."
    )
if len(set(ids)) != len(ids):
    raise StudyDesignKnowledgeUnavailableError(
        "Study-design vector result contains duplicate evidence IDs."
    )
```

For every vector row, require exact equality for source kind, source ID, source path, source SHA-256, section, `chunk_ordinal == 0`, and body text against `local_by_id[id]`. Construct the vector hit from authoritative local fields and only copy the optional numeric distance from Chroma.

- [ ] **Step 6: Verify inventory and integrity tests GREEN**

Run:

```bash
.venv/bin/pytest tests/test_study_design_documents.py -q
```

Expected: local identity and semantic-integrity tests pass; existing lexical fallback tests remain green.

- [ ] **Step 7: Commit the inventory deliverable**

```bash
git add db_rag/study_design_documents.py tests/test_study_design_documents.py
git commit -m "feat: verify study design section evidence"
```

---

### Task 3: Study-Design Hybrid Fusion and Tool Provenance

**Files:**
- Modify: `tests/test_study_design_documents.py`
- Modify: `tests/test_study_design_tools.py`
- Modify: `db_rag/study_design_documents.py`
- Modify: `epi_agent/tool_packs/study_design/tools.py:35-115`

**Interfaces:**
- Consumes: canonical and verified `StudyDesignHit` values from Task 2.
- Produces: `_rank_lexical_sections(query, sections, *, limit) -> list[StudyDesignHit]`.
- Produces: `_fuse_study_design_hits(vector_hits, lexical_hits, *, limit) -> tuple[StudyDesignHit, ...]` with equal RRF weights.
- Extends tool hit JSON with `evidence_id`, optional `distance`, and `matched_by`.
- Translates `StudyDesignKnowledgeUnavailableError` to recoverable `ToolExecutionError(code="STUDY_DESIGN_EVIDENCE_INVALID")`.

- [ ] **Step 1: Write failing provider-level fusion tests**

Create fixture sections so vector ranking is `[dual, vector_only]` and lexical ranking is `[lexical_only, dual]`. Assert the final order and modes:

```python
assert [hit.id for hit in outcome.value] == [
    dual.id,
    lexical_only.id,
    vector_only.id,
]
assert [hit.matched_by for hit in outcome.value] == [
    ("vector", "lexical"),
    ("lexical",),
    ("vector",),
]
assert collection.calls[0]["n_results"] == 6
assert outcome.status.mode == "hybrid_vector_lexical"
```

Use `limit=3`, so each branch considers at most six candidates.

- [ ] **Step 2: Run the provider fusion test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_study_design_documents.py -q
```

Expected: FAIL because semantic search currently skips lexical ranking and returns vector hits directly.

- [ ] **Step 3: Implement lexical ranking over the extracted inventory**

Move the current phrase, heading-overlap, body-overlap, relative-path, and ordinal ordering into `_rank_lexical_sections`. Return `hit` copies with `distance=None` and no preassigned match mode; fusion owns `matched_by`.

- [ ] **Step 4: Implement equal-weight study-design rank fusion**

Use the same two-loop union structure as publication fusion, keyed by `hit.id`. Prefer the vector copy for a dual-matched hit so its distance is preserved, calculate `1.0 / (60 + rank)` for both modes, order by descending score then stable evidence ID, and return at most `limit` hits with stable match-mode tuples.

- [ ] **Step 5: Run both branches and report hybrid truthfully**

In `search_with_status`, set `candidate_limit = limit * 2`, retrieve and validate vector hits, rank lexical hits from the same local inventory, fuse them, and only then return `hybrid_status`. Preserve existing lexical fallback for route, index-open, or provider-query availability failures.

- [ ] **Step 6: Write the failing tool-artifact assertion**

Update `_SearchableDesign` fixtures for the new `StudyDesignHit.id` field and assert bounded output contains:

```python
{
    "evidence_id": "study-design.fixture",
    "source_id": "study-design-source.fixture",
    "distance": 0.125,
    "matched_by": ["vector", "lexical"],
}
```

Keep the existing 1,200-character excerpt bound and safe string bounds.

- [ ] **Step 7: Extend `_design_hit`, translate integrity errors, and verify tool tests GREEN**

Change `_design_hit` to return `dict[str, object]`. Render `evidence_id` from `hit.id`, preserve finite numeric distance, and render `matched_by` as a bounded list containing only `vector` and `lexical` in stable order. Wrap the provider search call with:

```python
except StudyDesignKnowledgeUnavailableError as error:
    raise ToolExecutionError(
        "STUDY_DESIGN_EVIDENCE_INVALID",
        str(error),
        recoverable=True,
    ) from error
```

Add `test_design_search_tool_translates_invalid_semantic_evidence` using a provider whose `search_with_status` raises the deterministic integrity error, and assert the code and recoverability. Run:

```bash
.venv/bin/pytest tests/test_study_design_documents.py tests/test_study_design_tools.py -q
```

Expected: all selected tests pass, including lexical fallback and registry inclusion behavior.

- [ ] **Step 8: Commit the hybrid study-design deliverable**

```bash
git add db_rag/study_design_documents.py epi_agent/tool_packs/study_design/tools.py tests/test_study_design_documents.py tests/test_study_design_tools.py
git commit -m "feat: fuse study design hybrid evidence"
```

---

### Task 4: Cross-Provider Contract Regression

**Files:**
- Verify: `tests/test_db_rag_catalog.py`
- Verify: `tests/test_db_rag_agent_tools.py`
- Verify: `tests/test_semantic_publication_knowledge.py`
- Verify: `tests/test_epi_agent_publication_tools.py`
- Verify: `tests/test_study_design_documents.py`
- Verify: `tests/test_study_design_tools.py`

**Interfaces:**
- Verifies the shared public meaning of `hybrid_vector_lexical`, `lexical_fallback`, and `matched_by` without introducing a new cross-provider runtime abstraction.

- [ ] **Step 1: Confirm the existing catalog reference coverage remains unchanged**

Run:

```bash
.venv/bin/pytest \
  tests/test_db_rag_catalog.py::test_semantic_catalog_runs_vector_and_exact_lexical_retrieval \
  tests/test_db_rag_catalog.py::test_semantic_catalog_does_not_invent_a_lexical_match \
  tests/test_db_rag_catalog.py::test_semantic_catalog_filters_rows_from_another_source \
  tests/test_db_rag_agent_tools.py::test_catalog_tool_persists_hybrid_retrieval_provenance -q
```

Expected: four tests pass, proving dual, vector-only, lexical-only, cross-source filtering, and tool auditability in the unchanged reference provider.

- [ ] **Step 2: Run the complete focused retrieval suite**

Run:

```bash
.venv/bin/pytest \
  tests/test_db_rag_catalog.py \
  tests/test_db_rag_agent_tools.py \
  tests/test_semantic_publication_knowledge.py \
  tests/test_epi_agent_publication_tools.py \
  tests/test_study_design_documents.py \
  tests/test_study_design_tools.py \
  tests/test_session_studies.py \
  tests/test_embedding_fallback_readiness.py -q
```

Expected: all selected tests pass with no warnings or errors.

- [ ] **Step 3: Inspect the combined test evidence**

Confirm the output contains zero failures and that the named hybrid, fallback, provenance-integrity, and tool-artifact tests all ran. This verification task introduces no additional commit.

---

### Task 5: Dedicated Real Hybrid Retrieval Smoke

**Files:**
- Create: `scripts/smoke_hybrid_evidence_retrieval_real.py`
- Create: `tests/test_hybrid_evidence_retrieval_smoke_runner.py`

**Interfaces:**
- Exercises installed study discovery, the configured real embedding route, session study binding, all three production tool registries, Chroma, local lexical sources, and artifact persistence.
- Requires a configured `OPENAI_API_KEY` for the initial OpenAI profile; exits nonzero with a sanitized prerequisite message when unavailable.

- [ ] **Step 1: Write the failing smoke-runner structure test**

Create a test that requires the executable script and production markers:

```python
def test_hybrid_retrieval_smoke_uses_real_production_boundaries() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    source = SCRIPT.read_text(encoding="utf-8")
    required = {
        "discover_studies",
        "resolve_embedding_route",
        "bind_session_studies",
        "build_db_rag_tool_registry",
        "build_publication_tool_registry",
        "build_study_design_tool_registry",
        "StateArtifactStore",
        "hybrid_vector_lexical",
        "matched_by",
        "OPENAI_API_KEY",
        "300",
    }
    assert required <= {marker for marker in required if marker in source}
    assert "Fake" not in source
    assert "monkeypatch" not in source
    assert "stub" not in source.casefold()
```

- [ ] **Step 2: Run the structure test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_hybrid_evidence_retrieval_smoke_runner.py -q
```

Expected: FAIL because the dedicated executable does not exist.

- [ ] **Step 3: Implement the bounded real smoke**

The script must:

1. load the real application environment;
2. enforce `--timeout-seconds <= 300`;
3. discover installed studies beneath the production study root;
4. require one study providing catalog, reviewed publication knowledge, and searchable Markdown study design;
5. resolve and require the configured real embedding route without printing its key;
6. bind session studies through `bind_session_studies`;
7. invoke the three production search tools with bounded study-relevant queries;
8. load their saved artifacts from `StateArtifactStore`;
9. assert every artifact reports `hybrid_vector_lexical` and `embedding.available == true`;
10. assert each artifact has nonempty evidence with auditable `matched_by` values; and
11. print only study ID, model ID, result counts, elapsed seconds, and success.

Use `signal` or monotonic deadline checks so the process cannot exceed five minutes. Never print credentials, raw provider exceptions, full evidence text, or response bodies.

- [ ] **Step 4: Make the script executable and verify its offline contract test**

Run:

```bash
chmod +x scripts/smoke_hybrid_evidence_retrieval_real.py
.venv/bin/pytest tests/test_hybrid_evidence_retrieval_smoke_runner.py -q
```

Expected: PASS.

- [ ] **Step 5: Run focused tests before consuming the one real smoke attempt**

Run the Task 4 focused suite again. Do not run the real smoke until every offline test is green and `OPENAI_API_KEY` is configured.

- [ ] **Step 6: Run the dedicated real smoke exactly once**

Run:

```bash
.venv/bin/python scripts/smoke_hybrid_evidence_retrieval_real.py --timeout-seconds 300
```

Expected: exit 0 with sanitized hybrid counts for catalog, publication, and study design. On failure or timeout, preserve and report the single run's logs and diagnostics; do not rerun automatically.

- [ ] **Step 7: Commit the smoke deliverable**

```bash
git add scripts/smoke_hybrid_evidence_retrieval_real.py tests/test_hybrid_evidence_retrieval_smoke_runner.py
git commit -m "test: add real hybrid retrieval smoke"
```

---

### Task 6: Final Verification

**Files:**
- Verify only; no planned production changes.

**Interfaces:**
- Confirms the complete repository remains compatible after the focused feature deliverables.

- [ ] **Step 1: Run the complete Python suite**

```bash
.venv/bin/pytest -q
```

Expected: exit 0 with zero failures.

- [ ] **Step 2: Check repository formatting and generated-file scope**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors and only intended feature files changed. No frontend build is required because this first topic makes no frontend changes.

- [ ] **Step 3: Review the implementation against the approved specification**

Confirm each specification requirement maps to passing provider, tool, fallback, integrity, and real-smoke evidence. If any requirement lacks evidence, add a failing test before changing production code.
