# Agent-Driven Multi-Study Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent choose an installed study independently for each retrieval call, preserve that study identity through DB-RAG and study evidence artifacts, and prevent cross-study SQL without introducing a sticky active study or deterministic router.

**Architecture:** `ToolContext` exposes an arbitrary `StudyRegistry`. The model sees a small installed-study directory and may call `search_studies` to read bounded `overview.md` content before choosing a scalar `study_id` for a study-dependent search. Initial searches resolve the study explicitly; exact follow-up tools consume structured references containing `study_id`; saved plans and downstream artifacts inherit immutable study provenance. A question about another study can therefore select another `study_id` on its next tool call without changing thread state.

**Tech Stack:** Python 3.12, Pydantic v2, LangGraph, DuckDB, ChromaDB, pytest, study-package format v3.

## Working constraints

- App worktree: `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.worktrees/agent-driven-multi-study`
- App branch: `agent-driven-multi-study`, created from `local-multi-study` at `8244242`
- App Python: `.venv/bin/python` (Python 3.12)
- Baseline: `731 passed, 1 skipped`
- Keep `study_id` scalar. Multiple-study comparisons use separate tool calls and separate plans/datasets.
- Do not implement cross-study SQL, automatic word matching, routing JSON, a sticky thread study, or a parallel catalog executor.
- Do not silently fall back to a previous, default, sole, or lexical-only study/catalog.
- Do not change the frontend.
- The NHANES package change belongs in a separate Database repository worktree created from its `master`; do not edit Database `master` directly.

---

### Task 1: Replace implicit active-study context with an explicit registry resolver

**Files:**

- Create: `tests/test_multi_study_tool_context.py`
- Modify: `epi_agent/protocol.py`
- Modify: `epi_agent/studies.py`
- Modify: existing tracked tests that instantiate `ToolContext` under `tests/`

- [ ] **Step 1: Write failing resolver tests**

Cover arbitrary registry size, exact lookup, missing study, and no installed studies:

```python
def test_require_context_study_resolves_exact_study_id() -> None:
    context = _context(StudyRegistry([_study("study-a"), _study("study-b")]))
    assert require_context_study(context, "study-b").study_id == "study-b"


def test_require_context_study_never_falls_back_to_sole_study() -> None:
    context = _context(StudyRegistry([_study("study-a")]))
    with pytest.raises(ToolExecutionError) as raised:
        require_context_study(context, "unknown")
    assert raised.value.code == "STUDY_NOT_AVAILABLE"
```

Also assert the error details contain the requested ID and bounded available IDs, while an empty registry reports `NO_STUDY_PACKAGE_INSTALLED`.

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `.venv/bin/python -m pytest tests/test_multi_study_tool_context.py -q`

Expected: FAIL because `ToolContext` still accepts one `study` and `require_context_study` has no `study_id` parameter.

- [ ] **Step 3: Implement the registry-backed context**

Change the protocol contract to:

```python
@dataclass(frozen=True)
class ToolContext:
    studies: StudyRegistry
    artifact_store: ArtifactStore
    thread_id: str
    policy: Any
    # existing non-study fields remain unchanged


def require_context_study(context: ToolContext, study_id: str) -> StudyBundle:
    normalized = study_id.strip()
    if not context.studies.values:
        raise ToolExecutionError(
            "NO_STUDY_PACKAGE_INSTALLED",
            "No study package is installed.",
            recoverable=True,
        )
    study = context.studies.get(normalized)
    if study is None:
        raise ToolExecutionError(
            "STUDY_NOT_AVAILABLE",
            f"The requested study package is unavailable: {normalized}",
            recoverable=True,
            details={
                "requested_study_id": normalized,
                "available_study_ids": sorted(
                    available.study_id for available in context.studies.values
                ),
            },
        )
    return study
```

Remove `ToolContext.study`, `available_study_ids`, and the old `ACTIVE_STUDY_SELECTION_REQUIRED` path. Add a deterministic `StudyRegistry.ids` property if it keeps callers concise. Update existing test fixtures mechanically to construct `StudyRegistry` from their existing `StudyBundle` fixtures.

- [ ] **Step 4: Run focused protocol tests**

Run: `.venv/bin/python -m pytest tests/test_multi_study_tool_context.py tests/test_epi_agent_root_state.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add epi_agent/protocol.py epi_agent/studies.py tests/test_multi_study_tool_context.py tests
git commit -m "refactor: expose installed studies to agent tools"
```

---

### Task 2: Add bounded, non-ranking study discovery and prompt context

**Files:**

- Create: `epi_agent/tool_packs/studies/__init__.py`
- Create: `epi_agent/tool_packs/studies/tools.py`
- Create: `tests/test_study_discovery_tools.py`
- Modify: `epi_agent/agent.py`
- Modify: `epi_agent/tool_packs/general/clarification.py` only if its description needs to mention study ambiguity

- [ ] **Step 1: Write failing `search_studies` tests**

Test these contracts:

- results are ordered by exact `study_id`, not relevance;
- `limit` is at most 5 and each overview is at most 1,200 characters;
- response includes `offset`, `returned_count`, `total_count`, and `next_offset`;
- missing overview yields `overview_available: false` plus a bounded per-entry error;
- one broken overview does not hide other studies;
- the tool never selects or persists an active study.

Representative assertion:

```python
message = json.loads(registry.invoke(
    "search_studies", {"offset": 0, "limit": 2}, context=context
).message)
assert [item["study_id"] for item in message["studies"]] == ["a", "b"]
assert message["next_offset"] == 2
assert all(len(item.get("overview", "")) <= 1_200 for item in message["studies"])
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_study_discovery_tools.py -q`

Expected: FAIL because the tool pack does not exist.

- [ ] **Step 3: Implement the discovery tool**

Use strict Pydantic arguments:

```python
class SearchStudiesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=5)
```

Read `study.study_design.render_context()` directly. Do not embed the query, rank studies, inspect schemas, or infer a decision. Store a bounded `study_directory` observation if artifact persistence is available, with thread provenance only.

- [ ] **Step 4: Put the minimal installed-study directory in every turn prompt**

Replace the selected-study overview argument to `build_epi_agent_context_prompt` with a generated directory containing only exact ID and label:

```text
Installed studies:
- study_id: nhanes-2017-2018; label: NHANES 2017-2018
- study_id: report-india-synthetic; label: RePORT India Synthetic
```

Register `search_studies` regardless of whether all installed packages have overviews. Add system instructions that:

- use an explicit study ID directly when the user/context identifies it;
- call `search_studies` only when the choice needs background;
- proceed automatically when one study clearly fits;
- call the existing clarification tool when ambiguity remains;
- choose independently for each request and never assume the last study.

- [ ] **Step 5: Run focused discovery/prompt tests**

Run: `.venv/bin/python -m pytest tests/test_study_discovery_tools.py tests/test_epi_agent_root_state.py tests/test_no_study_startup.py -q`

Expected: PASS; prompt assertions contain all IDs and no `active study` instruction.

- [ ] **Step 6: Commit**

```bash
git add epi_agent/tool_packs/studies epi_agent/agent.py epi_agent/tool_packs/general/clarification.py tests
git commit -m "feat: let the agent inspect installed study overviews"
```

---

### Task 3: Scope catalog discovery with `study_id` and structured references

**Files:**

- Create: `epi_agent/db_rag/references.py`
- Modify: `epi_agent/db_rag/tools.py`
- Modify: `epi_agent/db_rag/prompt.py`
- Modify: `tests/test_db_rag_agent_tools.py`
- Create: `tests/test_multi_study_db_rag_tools.py`

- [ ] **Step 1: Write failing search and exact-follow-up tests**

Test that:

- `dbrag-search_catalog` requires scalar `study_id`;
- it searches only that study's semantic catalog;
- all hits and observation provenance contain the selected study ID;
- another call in the same `ToolContext` can use another study;
- an unavailable study returns `STUDY_NOT_AVAILABLE` and never touches another catalog;
- `dbrag-inspect_table` accepts the structured reference emitted by search;
- references from different studies are rejected before catalog/source access.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_db_rag_agent_tools.py tests/test_multi_study_db_rag_tools.py -q`

Expected: FAIL on the new argument/reference contracts.

- [ ] **Step 3: Add strict structured references**

```python
class TableRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    study_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    table: str = Field(min_length=1)


class FieldRef(TableRef):
    column: str = Field(min_length=1)
```

Provide one resolver that checks study, source membership, and exact reference scope before returning `(study, source)`; translate failures into `STUDY_REFERENCE_MISMATCH` or `SOURCE_UNAVAILABLE`.

- [ ] **Step 4: Update search and inspection contracts**

Use:

```python
class CatalogSearchArguments(BaseModel):
    study_id: str
    queries: list[str]
    limit: int = 5


class InspectTableArguments(BaseModel):
    table_ref: TableRef
    offset: int = 0
    limit: int = 25
```

Every table hit contains `table_ref`; every column hit contains `field_ref`. Preserve the existing five probes, ten hits per probe, no global flattening, explicit pagination, and mandatory hybrid semantic retrieval. `_save_observation` must receive an explicit study and write its ID rather than read ambient context.

- [ ] **Step 5: Update join/relationship contracts without adding `study_id` again**

Use `required_fields: list[FieldRef]` for join paths and `left_table_ref`, `right_table_ref`, plus key pairs for relationship profiling. Reject mixed-study refs with `CROSS_STUDY_OPERATION_UNAVAILABLE`; retain same-study, same-source relationship behavior and existing `max_hops`/`max_paths` bounds.

- [ ] **Step 6: Update DB-RAG model instructions**

Tell the agent to pass one scalar study ID to each search, then copy returned refs exactly into inspection/relationship calls. State that multiple studies require separate calls and never a combined plan.

- [ ] **Step 7: Run focused DB-RAG tests**

Run: `.venv/bin/python -m pytest tests/test_db_rag_agent_tools.py tests/test_multi_study_db_rag_tools.py -q`

Expected: PASS, including the existing 50-hit output-contract regression.

- [ ] **Step 8: Commit**

```bash
git add epi_agent/db_rag/references.py epi_agent/db_rag/tools.py epi_agent/db_rag/prompt.py tests
git commit -m "feat: scope schema retrieval to an explicit study"
```

---

### Task 4: Make dataset-plan study provenance immutable

**Files:**

- Modify: `epi_agent/artifacts.py`
- Modify: `epi_agent/db_rag/tools.py`
- Modify: `tests/test_scoped_db_rag_persistence.py`
- Create: `tests/test_dataset_plan_study_provenance.py`

- [ ] **Step 1: Write failing plan provenance tests**

Cover:

- new plans require `study_id`;
- plan content and artifact provenance must match;
- revising a plan cannot change its study;
- a legacy plan with exactly one provenance `study_id` normalizes successfully;
- legacy content with no unambiguous immutable study provenance raises `ARTIFACT_STUDY_PROVENANCE_MISSING`;
- mismatched plan/provenance raises `STUDY_REFERENCE_MISMATCH`.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dataset_plan_study_provenance.py tests/test_scoped_db_rag_persistence.py -q`

Expected: FAIL because `DatasetPlan` does not contain `study_id`.

- [ ] **Step 3: Add the immutable plan field and one normalization boundary**

```python
class DatasetPlan(BaseModel):
    study_id: str = Field(min_length=1)
    goal: str
    # existing fields unchanged


def dataset_plan_from_artifact(stored: StoredArtifact) -> DatasetPlan:
    content = dict(stored.content)
    content_id = str(content.get("study_id") or "").strip()
    provenance_id = str(stored.provenance.get("study_id") or "").strip()
    # inject provenance_id only for unambiguous legacy content;
    # reject missing or conflicting identities.
```

Use this helper everywhere a saved plan is parsed. In `StateArtifactStore.save_dataset_plan`, require matching provenance and preserve `study_id` across revisions.

- [ ] **Step 4: Validate plan fields against the declared study**

In `_save_dataset_plan`, resolve `plan.study_id`, then validate every source/table/column reference against only that study. `_validate_dataset_plan` must derive its study from the plan artifact, not tool context state. Return `PLAN_STUDY_UNAVAILABLE` if the installed package needed by a saved plan is absent.

- [ ] **Step 5: Run focused plan tests**

Run: `.venv/bin/python -m pytest tests/test_dataset_plan_study_provenance.py tests/test_scoped_db_rag_persistence.py tests/test_db_rag_agent_tools.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add epi_agent/artifacts.py epi_agent/db_rag/tools.py tests
git commit -m "feat: freeze study identity in dataset plans"
```

---

### Task 5: Propagate plan study through review, SQL, dataset, and quality lineage

**Files:**

- Modify: `epi_agent/db_rag/reviews.py`
- Modify: `epi_agent/db_rag/tools.py`
- Modify: `epi_agent/db_rag/persistence.py`
- Modify: `epi_agent/db_rag/quality.py`
- Modify: `epi_agent/artifacts.py`
- Modify: `tests/test_scoped_db_rag_persistence.py`
- Create: `tests/test_multi_study_dataset_lineage.py`

- [ ] **Step 1: Write failing lineage tests**

Assert that plan review, validated SQL, persistence attempt lineage, committed dataset provenance, and quality report all carry the same `study_id`. Add negative tests for:

- validating a plan whose study is no longer installed;
- dataset/plan study mismatch;
- SQL artifact/plan study mismatch;
- attempted replacement with another study;
- inspect-dataset deriving study from plan/dataset lineage without a caller selector.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_multi_study_dataset_lineage.py tests/test_scoped_db_rag_persistence.py -q`

Expected: FAIL because lineage currently derives from `context.study` or omits the study.

- [ ] **Step 3: Refactor review and extraction to resolve from the plan**

Use `dataset_plan_from_artifact` in review, approval, validation, extraction, and quality paths. Temporary review validation contexts retain the full registry. `_plan_runtime_path` receives/resolves `plan.study_id`; `dbrag-validate_and_extract` and `dbrag-inspect_dataset` keep their existing artifact arguments and gain no free-form study selector.

- [ ] **Step 4: Add study identity to persistence lineage**

Add `study_id` to:

- `_dataset_persistence_lineage` and its exact-key validation;
- validated SQL content/provenance;
- a required `study_id` keyword on `persist_sql_subset_artifact`, supplied from `plan.study_id`;
- persisted dataset provenance and canonical lineage verification;
- replacement/supersession validation;
- quality report provenance.

Every boundary compares identities before reading a database or committing an artifact. Use typed errors at tool boundaries; keep internal persistence functions raising `ValueError` for broken invariants.

- [ ] **Step 5: Run focused review/extraction/persistence tests**

Run: `.venv/bin/python -m pytest tests/test_multi_study_dataset_lineage.py tests/test_scoped_db_rag_persistence.py tests/test_db_rag_agent_tools.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add epi_agent/artifacts.py epi_agent/db_rag/reviews.py epi_agent/db_rag/tools.py epi_agent/db_rag/persistence.py epi_agent/db_rag/quality.py tests
git commit -m "feat: preserve study provenance through dataset extraction"
```

---

### Task 6: Scope publication and study-design retrieval per call

**Files:**

- Modify: `epi_agent/tool_packs/publication/tools.py`
- Modify: `epi_agent/tool_packs/publication/prompt.py`
- Modify: `epi_agent/tool_packs/study_design/tools.py`
- Modify: `epi_agent/tool_packs/study_design/prompt.py`
- Modify: `tests/test_epi_agent_publication_tools.py`
- Modify: `tests/test_study_design_tools.py`
- Create: `tests/test_multi_study_evidence_tools.py`

- [ ] **Step 1: Write failing evidence-scope tests**

Test explicit study selection, two different studies in consecutive calls, unavailable capability, missing study, and no fallback. Verify evidence hits and saved artifacts carry `study_id`.

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m pytest tests/test_epi_agent_publication_tools.py tests/test_study_design_tools.py tests/test_multi_study_evidence_tools.py -q`

Expected: FAIL on missing explicit study arguments.

- [ ] **Step 3: Implement per-call evidence scope**

Add scalar `study_id` to `publication-search_study_evidence` and `study-design-search`. Add a strict provenance-rich reference for exact publication opening:

```python
class StudySourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    study_id: str
    source_id: str
```

Search hits expose `source_ref`; `publication-open_study_source` consumes it. PubMed tools remain study-independent. Continue mandatory vector-plus-lexical retrieval for packaged publication evidence; do not add a lexical fallback.

- [ ] **Step 4: Update prompts and run tests**

Run: `.venv/bin/python -m pytest tests/test_epi_agent_publication_tools.py tests/test_study_design_tools.py tests/test_multi_study_evidence_tools.py -q`

Expected: PASS; prompts no longer mention “the active study.”

- [ ] **Step 5: Commit**

```bash
git add epi_agent/tool_packs/publication epi_agent/tool_packs/study_design tests
git commit -m "feat: scope study evidence retrieval per call"
```

---

### Task 7: Remove sticky study selection from graph and API runtime

**Files:**

- Modify: `epi_agent/runtime.py`
- Modify: `epi_agent/agent.py`
- Modify: `graph/builder.py`
- Modify: `api/schemas.py`
- Modify: `api/server.py`
- Modify: `api/runtime.py`
- Modify: `api/app.py`
- Modify: `tests/test_epi_agent_root_state.py`
- Modify: `tests/test_graph_studies.py`
- Modify: `tests/test_no_study_startup.py`
- Modify: `tests/test_api_runtime.py`
- Modify: `tests/test_api_server.py`

- [ ] **Step 1: Write failing graph/API tests for non-sticky selection**

Assert:

- `SubmitMessageRequest` rejects the removed `active_study_id` extra field;
- `_initial_graph_state` and later-turn payloads never contain `active_study_id`;
- graph builders no longer accept `default_study_id`;
- `ToolContext.studies` contains every session-bound package;
- the context prompt lists all studies;
- capability availability is aggregated across all installed studies;
- multiple installed studies no longer produce “Select an active study.”

- [ ] **Step 2: Run the focused graph/API tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_epi_agent_root_state.py tests/test_graph_studies.py tests/test_no_study_startup.py tests/test_api_runtime.py tests/test_api_server.py -q`

Expected: FAIL because active/default study plumbing still exists.

- [ ] **Step 3: Remove active/default study plumbing**

Remove `active_study_id` from `GenericEpiAgentState`, `SubmitMessageRequest`, server calls, runtime method signatures/payloads, and graph construction. Old checkpoint dictionaries may contain the extra key, but production code must ignore it and never use it for retrieval.

`build_general_epi_agent_graph` creates:

```python
ToolContext(
    studies=studies,
    artifact_store=artifact_store,
    thread_id=conversation_thread_id,
    policy=None,
    thread_storage=(
        UserStorageLayout(runtime_root).thread(
            owner_user_id,
            conversation_thread_id,
        )
        if runtime_root is not None
        and owner_user_id
        and conversation_thread_id
        else None
    ),
)
```

Its context prompt always renders the bounded ID/label directory, not one selected overview.

- [ ] **Step 4: Aggregate startup capabilities across arbitrary studies**

Aggregate over all registry values/readiness entries:

- DB-RAG available if any study's runtime DB-RAG is ready;
- publication knowledge available if any study has knowledge;
- study design available if any study has design;
- no studies still reports the existing no-package state.

Do not encode the two current study IDs.

- [ ] **Step 5: Run focused graph/API tests**

Run: `.venv/bin/python -m pytest tests/test_epi_agent_root_state.py tests/test_graph_studies.py tests/test_no_study_startup.py tests/test_api_runtime.py tests/test_api_server.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add epi_agent/runtime.py epi_agent/agent.py graph/builder.py api/schemas.py api/server.py api/runtime.py api/app.py tests
git commit -m "refactor: remove sticky active study state"
```

---

### Task 8: Add an NHANES overview and package it through the existing builder

**Repository:** `/Users/xutaowang/Desktop/RA work/Epi-Agent/Database`

**Files:**

- Create in a new Database worktree: `nhanes-2017-2018/study/study_design/overview.md`
- Modify: `nhanes-2017-2018/scripts/nhanes_database_index/build.py`
- Modify: `nhanes-2017-2018/tests/test_database_index.py`
- Create: `nhanes-2017-2018/tests/test_release_builder.py`
- Modify: `nhanes-2017-2018/release-config.json`

- [ ] **Step 1: Create a separate Database worktree from `master`**

Before editing, verify `git status --short` is clean and create:

```bash
git check-ignore -q .worktrees
git worktree add .worktrees/nhanes-study-overview -b nhanes-study-overview master
```

Expected: new worktree reports the Database `master` commit as its base. Do not change the existing Database checkout.

- [ ] **Step 2: Write failing database-index/package tests**

Test that the NHANES builder accepts a study-design root, creates `study_knowledge`, indexes `overview.md` with `source_kind=study_design` and source hash/path, and that the shared release builder accepts the exact indexed Markdown provenance.

- [ ] **Step 3: Run and verify failure**

Run from the Database worktree with the existing NHANES Python 3.12 environment:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Database/nhanes-2017-2018/.venv/bin/python -m pytest \
  nhanes-2017-2018/tests/test_database_index.py \
  nhanes-2017-2018/tests/test_release_builder.py -q
```

Expected: FAIL because the NHANES index currently creates only `table_summaries` and `column_chunks`.

- [ ] **Step 4: Reuse the shared study-design indexer**

Import `scripts/study_design/index.py` rather than copying Markdown parsing. Extend the NHANES build entry point with optional `study_design_root`, build its chunks, and add them to `study_knowledge` using the same embedding function as catalog chunks. When no design root is supplied, preserve the current two-collection build behavior.

- [ ] **Step 5: Write a concise authoritative overview**

Cover only stable routing/design facts needed to distinguish the package: NHANES 2017–2018, United States civilian noninstitutionalized population, survey/examination/laboratory/questionnaire structure, participant key `SEQN`, complex design variables/weights, and the 18-table v1 scope. Do not add a routing JSON file.

- [ ] **Step 6: Bump and configure the package**

Set `package_version` to `0.2.0` and add:

```json
"study_design_root": "study/study_design"
```

Rebuild the database/index with the exact overview root, then build `delivery/nhanes-2017-2018-0.2.0.tar.gz` through the existing shared `scripts/package_release/build.py`.

- [ ] **Step 7: Verify package tests and archive**

Run the focused tests, then inspect the archive manifest and checksum. Expected: format-v3 manifest declares `study_design`, Chroma has nonempty `study_knowledge`, and packaged/indexed overview hashes match.

- [ ] **Step 8: Commit in the Database worktree**

```bash
git add nhanes-2017-2018 scripts tests
git commit -m "feat: package NHANES study overview"
```

Record the Database commit and archive path in the app branch handoff; do not merge Database automatically unless the user asks.

---

### Task 9: Add one real end-to-end multi-study smoke

**Files:**

- Create: `scripts/smoke_agent_driven_multi_study_real.py`
- Modify: `README.md` or the existing smoke command index if one exists

- [ ] **Step 1: Implement a production-boundary smoke**

The smoke must use real package installation/discovery, session binding with the real OpenAI embedding credential, the production graph builder with a real configured OpenAI chat model, real tool registries, the real two Chroma indexes, DuckDB, and artifact lineage. It must accept explicit RePORT and NHANES archive paths.

Exercise two layers in one process:

1. Invoke the production graph with a clearly NHANES-specific schema question, then a clearly RePORT-specific schema question in the same thread. Inspect activity/artifact evidence to prove the model selected different explicit study IDs per call and did not persist an active study in graph state.
2. At the production tool-registry boundary, use one registry-backed `ToolContext` for deterministic isolation and lineage assertions:

   - `search_studies` returns both authoritative overviews;
   - `dbrag-search_catalog(study_id="nhanes-2017-2018", queries=["long-term blood sugar control glycohemoglobin"], limit=10)` returns vector-backed `GHB_J` evidence;
   - exact inspection consumes the returned NHANES `TableRef`;
   - `dbrag-search_catalog(study_id="report-india-synthetic", queries=["manufactured cigarette smoking intensity per day"], limit=10)` returns vector-backed RePORT evidence;
   - mixed-study relationship references fail with `CROSS_STUDY_OPERATION_UNAVAILABLE`;
   - a saved single-study plan preserves its study ID into validation lineage.

The smoke must print bounded timing/identity diagnostics, retain its temporary diagnostics on failure where practical, run once, and finish within five minutes. It must not use a fake embedder, fake LLM decision, or stub catalog.

- [ ] **Step 2: Run focused unit/integration tests before the real smoke**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_multi_study_tool_context.py \
  tests/test_study_discovery_tools.py \
  tests/test_multi_study_db_rag_tools.py \
  tests/test_dataset_plan_study_provenance.py \
  tests/test_multi_study_dataset_lineage.py \
  tests/test_multi_study_evidence_tools.py -q
```

Expected: PASS.

- [ ] **Step 3: Run the real smoke exactly once**

```bash
.venv/bin/python scripts/smoke_agent_driven_multi_study_real.py \
  --report-archive /Users/xutaowang/Desktop/RA\ work/Epi-Agent/Database/report-india-synthetic/delivery/report-india-synthetic-0.3.0.tar.gz \
  --nhanes-archive /Users/xutaowang/Desktop/RA\ work/Epi-Agent/Database/.worktrees/nhanes-study-overview/nhanes-2017-2018/delivery/nhanes-2017-2018-0.2.0.tar.gz
```

Expected: PASS with both exact study IDs, vector-backed matches, isolated catalog sources, and same-study plan lineage. If it fails, preserve diagnostics, diagnose the failure, fix it, and do not automatically rerun; obtain a fresh deliberate run decision as required by `AGENTS.md`.

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke_agent_driven_multi_study_real.py README.md
git commit -m "test: smoke agent-driven multi-study retrieval"
```

---

### Task 10: Full regression, compatibility audit, and handoff

**Files:**

- Modify only files required by failures proven to be in changed code paths.

- [ ] **Step 1: Search for stale contracts**

Run:

```bash
rg -n "active_study_id|default_study_id|ACTIVE_STUDY_SELECTION_REQUIRED|context\.study|require_context_study\(context\)" api epi_agent graph scripts tests
```

Expected: no production use of sticky/implicit study selection. Any intentionally retained migration fixture is documented by its test.

- [ ] **Step 2: Run the full tracked app suite**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests pass (baseline was 731 passed, 1 skipped; final count will be higher).

- [ ] **Step 3: Run static import/compile checks**

Run:

```bash
.venv/bin/python -m compileall -q api epi_agent graph db_rag scripts
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 4: Audit behavior against the design spec**

Verify explicitly:

- no deterministic study router exists;
- `search_studies` only exposes bounded authoritative overviews;
- exact user-specified study IDs bypass discovery;
- consecutive turns/calls can use different studies;
- all installed studies are generalized through `StudyRegistry`;
- no tool silently selects the sole/default/previous study;
- no cross-study SQL or plan can be constructed;
- semantic catalog retrieval remains mandatory and hybrid lexical boosting remains internal;
- scalar calls are sequential; parallel execution remains a follow-up.

- [ ] **Step 5: Run the broader local suite after integration into `local-multi-study`**

Because the primary checkout contains additional ignored local regression tests not present in a fresh worktree, first finish and review this branch. After the user authorizes local merge, run that broader suite from the merged `local-multi-study` checkout to catch compatibility issues without copying ignored tests into this branch.

- [ ] **Step 6: Final commit if verification required fixes**

Review `git diff --name-only`, stage only files changed to repair verified regressions, and commit them as `fix: close multi-study regression gaps`. Skip this commit when verification required no fixes.

- [ ] **Step 7: Request code review and choose integration**

Use `superpowers:requesting-code-review`, address verified findings, then use `superpowers:finishing-a-development-branch`. Do not merge automatically; offer the user the local merge option into `local-multi-study`.
