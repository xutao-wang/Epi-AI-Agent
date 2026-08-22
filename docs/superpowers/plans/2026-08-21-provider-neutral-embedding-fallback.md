# Provider-Neutral Embedding Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make embedding availability provider-neutral, prove Anthropic-only retrieval remains lexical and non-terminal, and run one real Anthropic-only smoke request.

**Architecture:** Introduce an immutable embedding-route descriptor and resolve it independently from the chat model. OpenAI remains the sole concrete embedding adapter, while absent or unsupported provider adapters bind the existing lexical providers with provider-aware status metadata. Application startup must never fail merely because a configured embedding route is unavailable.

**Tech Stack:** Python 3.12, dataclasses, existing OpenAI embedding adapter, FastAPI/LangGraph runtime, pytest.

## Global Constraints

- Chat-provider selection and embedding-provider selection remain independent.
- OpenAI is the only concrete embedding transport added by this change.
- Missing credentials, missing adapters, adapter construction failures, and provider query failures degrade to lexical search.
- Package/vector-index model mismatches remain unavailable for semantic lookup and do not query the mismatched index.
- Status output includes provider, model, stable reason code, and a sanitized explanation without credentials or raw provider errors.
- The real smoke test removes OpenAI credentials from its subprocess and never prints provider credentials.

---

### Task 1: Provider-Neutral Route Contract

**Files:**
- Create: `db_rag/embedding_routes.py`
- Modify: `db_rag/retrieval_status.py`
- Test: `tests/test_embedding_routes.py`
- Test: `tests/test_retrieval_status.py`

**Interfaces:**
- Produces: `EmbeddingRoute(model, provider, credential_env, api_key, factory, unavailable_reason_code)`.
- Produces: `resolve_embedding_route(environ, model)` that returns an available OpenAI route or an unavailable route without raising for unknown future providers.
- Extends: `hybrid_status(model, provider)` and `lexical_fallback_status(model, reason_code, provider, credential_env)`.

- [ ] **Step 1: Write failing route and provider-aware status tests**

Cover an available OpenAI route, missing OpenAI credentials, and `OpenRouter/Qwen/...` without an installed adapter. Assert the latter returns `EMBEDDING_ROUTE_UNAVAILABLE`, provider `openrouter`, and no exception.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/test_embedding_routes.py tests/test_retrieval_status.py -q`

Expected: failure because the route module and provider-aware status fields do not exist.

- [ ] **Step 3: Implement the minimal route/status contract**

Keep environment resolution pure by accepting a mapping. Map the existing `OpenAI/text-embedding-3-large` model to `OPENAI_API_KEY` and `OpenAIEmbeddingFunction`; parse unknown `Provider/model` identifiers into unavailable descriptors. Generate bounded messages from route metadata.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `.venv/bin/pytest tests/test_embedding_routes.py tests/test_retrieval_status.py -q`

- [ ] **Step 5: Commit**

Commit message: `feat: add provider-neutral embedding routes`

---

### Task 2: Route Application Through Retrieval Binding

**Files:**
- Modify: `api/app.py`
- Modify: `db_rag/session_studies.py`
- Modify: `db_rag/study_design_documents.py`
- Modify: `db_rag/catalog.py`
- Modify: `db_rag/local_knowledge.py`
- Modify: `tests/test_session_studies.py`
- Modify: `tests/test_study_design_documents.py`
- Modify: `tests/test_embedding_fallback_readiness.py`

**Interfaces:**
- `bind_session_studies(..., embedding_route)` consumes the resolved route rather than a raw OpenAI key.
- `MarkdownStudyDesign` receives an optional route when session studies are bound; absent routes retain provider-neutral lexical fallback.

- [ ] **Step 1: Write failing binding tests**

Assert an unavailable OpenRouter/Qwen route never constructs Chroma or an embedding adapter, preserves lexical catalog/publication/design access, and reports `EMBEDDING_ROUTE_UNAVAILABLE` with provider `openrouter`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/pytest tests/test_session_studies.py tests/test_study_design_documents.py tests/test_embedding_fallback_readiness.py -q`

- [ ] **Step 3: Thread the route through startup and providers**

Resolve the route from the explicit `environ` mapping in `build_application`. Bind semantic providers only when the route is available. Replace direct environment reads in study-design search with the bound route. Preserve compatibility only where existing internal tests require it.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/pytest tests/test_session_studies.py tests/test_study_design_documents.py tests/test_embedding_fallback_readiness.py -q`

- [ ] **Step 5: Commit**

Commit message: `feat: route embedding availability independently`

---

### Task 3: Anthropic-Only Offline Integration Regression

**Files:**
- Modify: `tests/test_embedding_fallback_readiness.py`
- Modify: `tests/test_no_study_startup.py`

**Interfaces:**
- Proves `build_application` accepts an Anthropic-only model catalog with an installed study and no OpenAI/OpenRouter credential.
- Proves each of the three retrieval providers returns lexical fallback without a provider call or terminal tool error.

- [ ] **Step 1: Write the failing Anthropic-only application test**

Build model availability with only `ProviderEndpoint("anthropic", "ANTHROPIC_API_KEY")`, provide a study package fixture, omit every embedding-provider key, build the application, bind its graph studies, and assert catalog, publication, and study-design outcomes are lexical with explicit route status.

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/pytest tests/test_embedding_fallback_readiness.py -q`

- [ ] **Step 3: Make the smallest integration corrections required**

Keep all provider calls mocked/offline. Do not weaken package validation or evidence provenance.

- [ ] **Step 4: Run the test and verify GREEN**

Run: `.venv/bin/pytest tests/test_embedding_fallback_readiness.py -q`

- [ ] **Step 5: Commit**

Commit message: `test: cover Anthropic-only lexical retrieval`

---

### Task 4: Documentation and Real Anthropic-Only Smoke

**Files:**
- Modify: `README.md`
- Create: `scripts/smoke_anthropic_only.py`
- Test: `tests/test_anthropic_only_smoke.py`

**Interfaces:**
- `scripts/smoke_anthropic_only.py` loads the project environment, explicitly removes OpenAI/OpenRouter credentials from the child environment, makes one minimal Anthropic chat request through the production chat builder, and prints only model plus response success metadata.

- [ ] **Step 1: Write a failing offline smoke-script test**

Inject a fake chat builder/client and assert OpenAI/OpenRouter credential names are absent at invocation and no secret value appears in captured output.

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/pytest tests/test_anthropic_only_smoke.py -q`

- [ ] **Step 3: Implement the smoke script and documentation**

Default to the configured Anthropic model, send a short deterministic prompt, enforce a bounded timeout, and fail nonzero on provider or response errors. Document that OpenRouter/Qwen transport is not yet implemented but will degrade lexically when unavailable.

- [ ] **Step 4: Run offline tests and verify GREEN**

Run: `.venv/bin/pytest tests/test_anthropic_only_smoke.py -q`

- [ ] **Step 5: Run full verification**

Run: `.venv/bin/pytest -q`

Run: `git diff --check`

- [ ] **Step 6: Run the real smoke test**

Run: `.venv/bin/python scripts/smoke_anthropic_only.py`

Expected: exit 0 and a sanitized confirmation containing the Anthropic model and non-empty response metadata, with no credential values.

- [ ] **Step 7: Commit**

Commit message: `test: add Anthropic-only provider smoke`
