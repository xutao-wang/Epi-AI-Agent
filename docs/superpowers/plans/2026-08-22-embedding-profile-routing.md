# Embedding Profile Routing and Startup Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route evidence embeddings through a validated non-secret profile registry, perform one real probe per FastAPI application instance, keep every embedding failure non-blocking through lexical fallback, and show one safe user notice only while fallback applies.

**Architecture:** Add a registry/selection layer that resolves one deployment-owned profile, an allow-listed adapter layer that builds the provider client, and an immutable startup result containing the route plus public status. `build_application()` initializes that result exactly once, assesses installed-study index compatibility without further provider requests, and injects the same status into runtime options and every projected thread state. Catalog, publication, and study-design binding consumes the latched route; request-time embedding failures retain their existing request-local lexical fallback and never mutate or repeat the startup probe. The React app renders a single component from runtime/thread status and remains silent when all hybrid paths are ready.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, OpenAI embeddings SDK, ChromaDB, React/TypeScript, Vitest/Testing Library, Playwright, pytest.

## Global Constraints

- Use Python 3.12 through `.venv/bin/python` and `.venv/bin/pytest`.
- Keep secrets only in environment variables. Never serialize or log API-key values, raw provider responses, or provider exception text.
- `config/embedding_models.json` contains non-secret cards only; `transport` resolves through a code-owned allow-list and cannot import configured Python code.
- Resolve `DB_RAG_EMBEDDING_PROFILE` first, legacy `DB_RAG_EMBEDDING_MODEL` second, and registry `default_profile` last.
- Never substitute the default after an explicit unknown, disabled, malformed, or ambiguous selection.
- Run exactly one real, bounded provider probe during each `build_application()` call when a valid enabled profile and credential exist. Threads, refreshes, review actions, and searches never health-probe again.
- Validate one and only one finite numeric probe vector with the configured dimension count.
- Treat every embedding registry, selection, credential, adapter, probe, response, dimension, index-compatibility, and later query failure as a soft failure. FastAPI, agent construction, thread operations, and lexical catalog/publication/study-design search must remain usable.
- Preserve existing hard integrity boundaries for corrupt authoritative files, unsafe paths, and unverifiable evidence provenance.
- Keep the startup result immutable for the application instance. Query-time fallback metadata is request-specific and must not change the startup latch.
- Do not add an embedding selector to the web UI. Operators select a profile because the choice must match prebuilt study indexes.
- Render no notice when the probe succeeds and all installed studies are compatible. Otherwise render one non-dismissible, accessible, profile-specific notice from current runtime/thread state.
- The dedicated real smoke must use compiled frontend assets, real FastAPI, browser controls, the real configured OpenAI embedding provider for the success process, and a separate missing-key process that performs no provider request. It runs once with a maximum of five minutes and preserves sanitized logs, API state, HTML, and screenshots on failure.

---

### Task 1: Strict Non-Secret Embedding Profile Registry

**Files:**
- Create: `config/embedding_models.json`
- Create: `db_rag/embedding_profiles.py`
- Create: `tests/test_embedding_profiles.py`
- Modify: `db_rag/retrieval_status.py`

**Interfaces:**
- Produces: `EmbeddingProfile`, an immutable Pydantic model with `id`, `label`, `provider`, `transport`, `model`, `index_compatibility`, `dimensions`, `base_url`, `api_key_env`, `timeout_seconds`, and `enabled`.
- Produces: `EmbeddingProfileRegistry`, an immutable strict model with one enabled `default_profile`, unique profile IDs, and unique `index_compatibility` values.
- Produces: `EmbeddingProfileResolution`, an immutable value with `profile: EmbeddingProfile | None`, safe `profile_id`, safe `profile_label`, and optional `EmbeddingReasonCode`.
- Produces: `load_embedding_profile_registry(path: Path) -> EmbeddingProfileRegistry` for strict parsing and `resolve_embedding_profile(environ, *, registry_path) -> EmbeddingProfileResolution` for non-throwing production resolution.

- [ ] **Step 1: Write failing registry and selection tests**

Cover the tracked OpenAI card plus temporary registries for:

- strict rejection of unknown keys, malformed URLs, unsafe credential-variable names, zero/oversized dimensions, non-positive/out-of-range timeouts, duplicate IDs, duplicate compatibility identities, missing defaults, and disabled defaults;
- exact `DB_RAG_EMBEDDING_PROFILE` precedence over legacy configuration;
- unique legacy `DB_RAG_EMBEDDING_MODEL` migration by `index_compatibility`;
- registry default selection when neither environment variable is set;
- explicit unknown and disabled selections returning `profile=None` with stable reason codes instead of selecting the default; and
- malformed registry content returning a generic safe resolution without leaking invalid JSON or raw configured values.

Use a helper that writes only non-secret test JSON. Assert `model_dump_json()` and `repr()` for profiles/resolutions do not contain any test API-key value.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
.venv/bin/pytest tests/test_embedding_profiles.py -q
```

Expected: collection/import failure because the registry module does not exist.

- [ ] **Step 3: Add stable reason codes and strict models**

Extend `EmbeddingReasonCode` with:

```python
"EMBEDDING_PROFILE_INVALID"
"EMBEDDING_PROFILE_UNKNOWN"
"EMBEDDING_PROFILE_DISABLED"
"EMBEDDING_TRANSPORT_UNAVAILABLE"
"EMBEDDING_PROBE_TIMEOUT"
"EMBEDDING_RESPONSE_INVALID"
"EMBEDDING_DIMENSION_MISMATCH"
"EMBEDDING_INDEX_INCOMPATIBLE"
```

Use `ConfigDict(extra="forbid", frozen=True)` and validators that normalize text, restrict `base_url` to HTTP(S), restrict `api_key_env` to `^[A-Z][A-Z0-9_]*$`, and enforce a bounded timeout such as `0 < timeout_seconds <= 120`. Registry model validation owns uniqueness/default invariants; it raises only to callers explicitly invoking the strict loader.

- [ ] **Step 4: Implement non-throwing selection around the strict loader**

`resolve_embedding_profile()` catches file, JSON, and validation errors and returns `EMBEDDING_PROFILE_INVALID` with label `Configured embedding profile`. It must not include the invalid profile ID in its public fields. For a parsed registry, implement the approved precedence exactly and return `EMBEDDING_PROFILE_UNKNOWN` or `EMBEDDING_PROFILE_DISABLED` for explicit invalid choices.

- [ ] **Step 5: Add the initial tracked registry**

Write the approved `openai-text-embedding-3-large` card with provider model `text-embedding-3-large`, index identity `OpenAI/text-embedding-3-large`, dimensions `3072`, OpenAI base URL, `OPENAI_API_KEY`, ten-second timeout, and `enabled: true`.

- [ ] **Step 6: Verify and commit the registry deliverable**

```bash
.venv/bin/pytest tests/test_embedding_profiles.py tests/test_embedding_routes.py -q
git add config/embedding_models.json db_rag/embedding_profiles.py db_rag/retrieval_status.py tests/test_embedding_profiles.py
git commit -m "feat: add embedding profile registry"
```

Expected: strict registry and legacy route tests pass; no secret is present in tracked configuration.

---

### Task 2: Allow-Listed Route Construction and One-Time Probe

**Files:**
- Modify: `db_rag/embedding_routes.py`
- Modify: `db_rag/vectorstore.py`
- Create: `db_rag/embedding_startup.py`
- Modify: `tests/test_embedding_routes.py`
- Create: `tests/test_embedding_startup.py`
- Create: `tests/test_vectorstore.py`

**Interfaces:**
- Produces: `EmbeddingAdapterFactory = Callable[[EmbeddingProfile, str], Any]` and an allow-list initially containing only `openai_embeddings`.
- Extends: `EmbeddingRoute` with safe profile metadata, index compatibility, dimensions, and the approved factory; `model` remains the packaged-index identity consumed by retrieval status and readiness code.
- Produces: `EmbeddingStartupStatus`, an immutable Pydantic model containing `profile_id`, `profile_label`, `provider`, `index_compatibility`, `available`, `retrieval_mode`, optional `reason_code`, bounded `message`, and compatible/incompatible study ID tuples.
- Produces: `EmbeddingStartupResult(route, status)` and `initialize_embedding(environ, *, registry_path, adapters=None) -> EmbeddingStartupResult` that never raises for an embedding-related failure.

- [ ] **Step 1: Write failing adapter, probe, and secret-boundary tests**

Use injected recording adapters/embedders; do not call the network in unit tests. Cover:

- valid OpenAI route construction from the selected card;
- missing credentials and unregistered transport;
- one successful call with one fixed non-sensitive probe string;
- provider rejection, connection error, and timeout;
- empty/multiple vectors, strings/booleans/non-numeric values, NaN/infinity, and incorrect dimensions;
- a successful vector of exactly the configured dimension;
- one adapter call and one `embed_query` call per `initialize_embedding()` invocation;
- safe fixed messages (`is not configured`, `cannot be reached`, `returned an incompatible response`) with no exception text, raw response, URL query, or key; and
- `repr()`/`model_dump_json()` exclusion of the credential value.

Keep dimensions small in injected test cards so tests remain fast.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
.venv/bin/pytest tests/test_embedding_routes.py tests/test_embedding_startup.py tests/test_vectorstore.py -q
```

Expected: failures because card-driven adapters and startup status are absent.

- [ ] **Step 3: Generalize the OpenAI embedding client without weakening index identity**

Change `OpenAIEmbeddingFunction` to accept the card's provider model, base URL, and timeout while retaining `config_model` as the `index_compatibility` identity. Pass `base_url` and `timeout` into `OpenAI(...)`, keep retries disabled, and preserve existing query caching/batching. Existing callers may omit new arguments and retain current defaults during migration.

- [ ] **Step 4: Replace provider-name inference with the adapter allow-list**

Construct a route only from `EmbeddingProfileResolution`. The OpenAI adapter imports `OpenAIEmbeddingFunction` in code and passes the validated card fields. Unknown `transport` values produce `EMBEDDING_TRANSPORT_UNAVAILABLE`; configuration never calls `importlib`, `eval`, or a dotted object path.

- [ ] **Step 5: Implement the startup probe and sanitized status builder**

For a usable route, call:

```python
vectors = route.create_embedding_function().embed_query(
    ["Epi Agent embedding startup probe"]
)
```

Validate outer count, vector type, numeric-but-not-boolean elements, `math.isfinite`, and exact dimensions. Map timeouts separately from other provider failures. For every failure, return the same route marked unavailable plus a lexical `EmbeddingStartupStatus`; do not raise. On success return `available=True`, `retrieval_mode="hybrid_vector_lexical"`, no reason/message, and the original route. Log only stable profile ID/reason code and probe completion, never exception strings or secret-bearing objects.

- [ ] **Step 6: Verify request-time behavior remains independent**

Add a regression proving a successful startup result remains `available=True` after an injected embedder later raises during catalog/publication/study-design query execution; each query returns its existing `lexical_fallback` status and no startup-probe function is called again.

- [ ] **Step 7: Verify and commit the routing/probe deliverable**

```bash
.venv/bin/pytest tests/test_embedding_routes.py tests/test_embedding_startup.py tests/test_vectorstore.py tests/test_embedding_fallback_readiness.py -q
git add db_rag/embedding_routes.py db_rag/vectorstore.py db_rag/embedding_startup.py tests/test_embedding_routes.py tests/test_embedding_startup.py tests/test_vectorstore.py tests/test_embedding_fallback_readiness.py
git commit -m "feat: probe selected embedding profile once"
```

Expected: every tested embedding failure returns lexical status and no test observes an uncaught startup exception.

---

### Task 3: Per-Study Index Compatibility and Lexical Binding

**Files:**
- Modify: `db_rag/embedding_startup.py`
- Modify: `db_rag/session_studies.py`
- Modify: `tests/test_session_studies.py`
- Modify: `tests/test_embedding_fallback_readiness.py`

**Interfaces:**
- Produces: `assess_study_compatibility(status, route, studies) -> EmbeddingStartupStatus`, which inspects packaged index identities without calling an embedding provider.
- Preserves: `bind_session_studies(..., embedding_route=route) -> BoundStudyRegistry`; it binds hybrid providers only for exact identity matches and lexical providers for all global/profile/index failures.

- [ ] **Step 1: Write failing compatible, incompatible, and mixed-registry tests**

Build study registries with package paths declaring matching and mismatching `embedding_model` values. Assert:

- global probe failure still partitions studies strictly by index identity, while producing the global three-search lexical message because transport is unavailable;
- successful probe plus all matches returns empty message and all compatible IDs;
- successful probe plus one mismatch keeps `available=True`, lists both partitions, and creates a message that names only the incompatible study label;
- compatibility assessment does not call the adapter/embedder;
- incompatible catalog, publication, and study-design providers all return lexical results with `EMBEDDING_INDEX_INCOMPATIBLE`; and
- one incompatible study does not change a compatible study's hybrid binding.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
.venv/bin/pytest tests/test_session_studies.py tests/test_embedding_fallback_readiness.py -q
```

Expected: failures because current mismatches use the generic configuration reason and no application status partitions studies.

- [ ] **Step 3: Implement pure compatibility assessment**

Compare each `DbRagRuntimePaths.embedding_model` exactly with `route.model`. Sort study IDs for stable API output. Build the public message from validated card label and installed study labels, with no filesystem paths. Use:

- global failure: `Semantic embedding search is unavailable. (<label> <safe cause>.) Catalog, publication, and study-design searches will use lexical matching only.`
- mixed mismatch: `Semantic embedding search is unavailable for <labels>. (<label> is incompatible with the semantic index for this study/these studies.) Searches for this study/these studies will use lexical matching only.`

Leave the message empty only when startup is available and every study is compatible.

- [ ] **Step 4: Make session binding consume the latched route only**

Remove the legacy `api_key`/implicit OpenAI construction path from production callers. Retain a narrow compatibility helper only if tests still require it, but do not resolve configuration or probe inside `bind_session_studies()`. Use `EMBEDDING_INDEX_INCOMPATIBLE` for exact identity mismatches, reuse one route-created embedder per compatible identity, and preserve lexical providers for every unavailable route or binding exception.

- [ ] **Step 5: Verify and commit per-study fallback**

```bash
.venv/bin/pytest tests/test_session_studies.py tests/test_embedding_fallback_readiness.py tests/test_study_design_documents.py tests/test_semantic_publication_knowledge.py tests/test_db_rag_catalog.py -q
git add db_rag/embedding_startup.py db_rag/session_studies.py tests/test_session_studies.py tests/test_embedding_fallback_readiness.py
git commit -m "feat: scope embedding fallback by study index"
```

Expected: matching studies stay hybrid, mismatching studies remain searchable lexically, and no compatibility check sends a provider request.

---

### Task 4: Initialize Once and Project Live Runtime/Thread Status

**Files:**
- Modify: `api/app.py`
- Modify: `api/schemas.py`
- Modify: `api/runtime.py`
- Modify: `tests/test_embedding_fallback_readiness.py`
- Modify: `tests/test_api_runtime.py`
- Modify: `tests/test_api_server.py`

**Interfaces:**
- Extends: `RuntimeOptions.embedding_startup_status: EmbeddingStartupStatus`.
- Extends: `ApiThreadState.embedding_startup_status: EmbeddingStartupStatus`.
- Extends: `ReportAgentApiRuntime.embedding_startup_status`, held once per application runtime and passed into every state projection.
- Changes: `build_application()` calls `initialize_embedding()` exactly once, calls pure study compatibility once, and captures the immutable route/status in `graph_factory`.

- [ ] **Step 1: Write failing application-lifecycle and API contract tests**

Inject a recording startup initializer into `build_application()` and assert:

- constructing one application invokes it once;
- runtime-options requests, two thread creations, repeated state reads, a refresh, a review resume, and retrieval binding do not invoke it again;
- a separate `build_application()` invocation performs one new initialization;
- missing credential, invalid registry, unknown/disabled profile, adapter absence, timeout, provider rejection, malformed response, dimension mismatch, and incompatible index still yield a constructed FastAPI app and usable lexical-bound graph;
- runtime options and both new and historical thread states contain the same current-process status;
- historical checkpoint data does not contain the status and needs no migration; and
- API JSON contains no credential variable value, API key, raw exception, or provider response.

- [ ] **Step 2: Run focused backend tests and verify RED**

```bash
.venv/bin/pytest tests/test_embedding_fallback_readiness.py tests/test_api_runtime.py tests/test_api_server.py -q
```

Expected: failures because the runtime schemas and application do not expose a latched startup status.

- [ ] **Step 3: Wire initialization into application construction**

Replace `resolve_db_rag_embedding_model()` plus direct `resolve_embedding_route()` in `build_application()` with one `initialize_embedding(...)` call followed by `assess_study_compatibility(...)`. Continue exposing `route.model` as `db_rag_embedding_model` to graph/runtime settings for compatibility. Capture `route` in `graph_factory`; no closure path may call profile resolution or startup probing again.

- [ ] **Step 4: Add typed runtime and state projection**

Store the final status on `ReportAgentApiRuntime`. Include it in `runtime_options()` and pass it internally to every `project_thread_state()` call, including graphless, restored, running, interrupted, and activity-enriched paths. Give test-only runtime construction a silent compatible default status so existing unrelated fixtures remain concise; production `build_application()` must always provide the real status.

- [ ] **Step 5: Prove state is live, not persisted**

Create a historical checkpoint under one runtime, instantiate a second runtime around the same checkpoint with a different safe status, and assert the loaded `ApiThreadState` contains the second runtime's status. Inspect exported/checkpoint values and diagnostics to confirm the status is absent there.

- [ ] **Step 6: Verify and commit application/API wiring**

```bash
.venv/bin/pytest tests/test_embedding_fallback_readiness.py tests/test_api_runtime.py tests/test_api_server.py -q
git add api/app.py api/schemas.py api/runtime.py tests/test_embedding_fallback_readiness.py tests/test_api_runtime.py tests/test_api_server.py
git commit -m "feat: expose latched embedding startup status"
```

Expected: each application initializes once, every thread response receives the same live status, and all embedding failures keep the app operational.

---

### Task 5: Single Silent-on-Success Frontend Notice

**Files:**
- Modify: `frontend/src/types.ts`
- Create: `frontend/src/EmbeddingFallbackNotice.tsx`
- Create: `frontend/src/EmbeddingFallbackNotice.test.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Adds: TypeScript `EmbeddingStartupStatus` matching the backend schema.
- Adds: `EmbeddingFallbackNotice({ status })`, which returns `null` for fully compatible hybrid status and one `role="status"` notice for global or study-scoped fallback.
- App status source: `state?.embedding_startup_status ?? runtimeOptions?.embedding_startup_status`, so the same component appears as soon as runtime options load and remains stable when a thread is created/opened/refreshed.

- [ ] **Step 1: Write failing component tests**

Assert the component:

- renders nothing for `available=true` with no incompatible studies and empty message;
- renders the exact OpenAI missing-credential text once;
- renders a future Qwen card label without any OpenAI hard-coding;
- renders the mixed-study message once while `available=true`;
- exposes the notice through an accessible role and does not provide dismiss controls; and
- never renders profile IDs, credential environment names, URLs, or reason codes beyond the bounded public message.

- [ ] **Step 2: Write failing App integration tests**

Add status to the runtime-options fixture and thread-state fixtures. Prove:

- fallback appears immediately after runtime options load, before any tool call;
- creating a thread and receiving the same status does not duplicate the notice;
- polling, opening saved history, and reaching a review state keep exactly one notice;
- a later state with the same latched status does not change its text; and
- successful status produces no notice anywhere.

- [ ] **Step 3: Run frontend tests and verify RED**

```bash
npm --prefix frontend test -- --run frontend/src/EmbeddingFallbackNotice.test.tsx frontend/src/App.test.tsx
```

Expected: failures because the type/component/status rendering does not exist.

- [ ] **Step 4: Implement the focused notice and styles**

The component renders only the backend-generated safe `status.message`; do not reconstruct provider causes in TypeScript. Place it after loading/error cards and before the conversation panel so it is visible beside conversation and review content. Add a warning-toned class distinct from fatal `.error-banner`/`.run-failure-card`, because lexical retrieval remains operational.

- [ ] **Step 5: Verify frontend behavior and compile production assets**

```bash
npm --prefix frontend test -- --run frontend/src/EmbeddingFallbackNotice.test.tsx frontend/src/App.test.tsx frontend/src/apiClient.test.ts
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
```

Expected: tests pass, TypeScript builds, and the working-demo build manifest is refreshed.

- [ ] **Step 6: Commit the UI deliverable**

```bash
git add frontend/src/types.ts frontend/src/EmbeddingFallbackNotice.tsx frontend/src/EmbeddingFallbackNotice.test.tsx frontend/src/App.tsx frontend/src/App.test.tsx frontend/src/styles.css frontend/dist working-demo
git commit -m "feat: show embedding lexical fallback notice"
```

If generated paths differ from this repository's tracked build outputs, stage only the files changed by the verified build and manifest command; do not add unrelated artifacts.

---

### Task 6: Configuration Documentation and Compatibility Cleanup

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `db_rag/config.py`
- Modify: `tests/test_embedding_profiles.py`
- Modify: `tests/test_embedding_fallback_readiness.py`

**Interfaces:**
- Documents: registry card ownership, `DB_RAG_EMBEDDING_PROFILE`, legacy `DB_RAG_EMBEDDING_MODEL` migration, index-compatibility requirement, one-probe lifecycle, and lexical soft-failure behavior.
- Retains: `EMBEDDING_MODEL` as the built-in index-compatibility constant where package/build tooling still needs it, while application selection comes from the registry.

- [ ] **Step 1: Add a failing configuration-boundary assertion**

Assert no production startup path calls `resolve_db_rag_embedding_model()` directly and no frontend type/component references `DB_RAG_EMBEDDING_PROFILE`, `api_key_env`, or an editable embedding choice. Keep a compatibility test proving legacy `DB_RAG_EMBEDDING_MODEL=OpenAI/text-embedding-3-large` selects the initial card.

- [ ] **Step 2: Update operator-facing configuration**

Add optional `DB_RAG_EMBEDDING_PROFILE=` documentation to `.env.example` without a secret/default value. Rewrite the README routing section to explain that the registry default is used unless an operator selects a profile, that the profile must match packaged indexes, and that any embedding failure is non-blocking lexical fallback with a one-time startup notice.

- [ ] **Step 3: Remove obsolete hard-coded startup helpers where safe**

Remove provider-specific readiness helpers and supported-model tuples only when no package-building code consumes them. Preserve the built-in index identity constant and request-time compatibility needed by existing study assets. Use `rg` before deletion rather than changing unrelated indexing flows.

- [ ] **Step 4: Verify and commit documentation/migration cleanup**

```bash
.venv/bin/pytest tests/test_embedding_profiles.py tests/test_embedding_routes.py tests/test_embedding_fallback_readiness.py -q
rg -n "DB_RAG_EMBEDDING_PROFILE|DB_RAG_EMBEDDING_MODEL|embedding_models.json" .env.example README.md db_rag api frontend/src
git add .env.example README.md db_rag/config.py tests/test_embedding_profiles.py tests/test_embedding_fallback_readiness.py
git commit -m "docs: document embedding profile routing"
```

Expected: deployment selection is documented, the browser exposes no selector, and the legacy migration path remains tested.

---

### Task 7: Dedicated Real FastAPI and Browser Smoke

**Files:**
- Create: `scripts/e2e_embedding_startup_status_real.py`
- Create: `tests/test_embedding_startup_status_smoke_runner.py`
- Reuse: `scripts/e2e_process_harness.py`
- Modify: `AGENTS.md` only if its smoke inventory explicitly lists feature scripts

**Interfaces:**
- Script arguments: `--timeout-seconds` capped at `300` and optional `--artifact-dir`.
- Success process: real configured `OPENAI_API_KEY`, production registry/adapter, installed packaged indexes, compiled `frontend/dist`, FastAPI, and Playwright.
- Failure process: explicit empty `OPENAI_API_KEY`, same production code and frontend, with no embedding-provider request.

- [ ] **Step 1: Add the smoke-runner contract test**

Assert the script is executable and source contains markers for `api.app:app`, `frontend/dist`, `sync_playwright`, `OPENAI_API_KEY`, `embedding_startup_status`, both success/fallback assertions, `e2e_process_harness`, and the `300`-second cap. Reject fake, stub, monkeypatch, and automatic-retry markers.

- [ ] **Step 2: Run the runner test and verify RED**

```bash
.venv/bin/pytest tests/test_embedding_startup_status_smoke_runner.py -q
```

Expected: failure because the executable script does not exist.

- [ ] **Step 3: Implement one bounded two-process smoke execution**

Within one script invocation and one shared deadline:

1. Load the real repository environment and require a nonempty `OPENAI_API_KEY` for the success process.
2. Launch a production FastAPI process serving `frontend/dist` with isolated runtime/checkpoint paths.
3. Verify `/api/runtime/options` reports available hybrid startup status and sanitized profile metadata.
4. Open the compiled UI in Playwright, create a thread using a browser control that does not require an unrelated chat-model call (for example, upload a small attachment through the paperclip), then verify `/api/threads/<id>/state` reports the same status and the fallback notice count is zero.
5. Stop that process and preserve its sanitized backend log, runtime-options JSON, thread-state JSON, page HTML/text, and screenshot.
6. Launch a separate application process with `OPENAI_API_KEY` explicitly set to an empty string so environment loading cannot refill it.
7. Verify its runtime/thread state is lexical fallback with `EMBEDDING_CREDENTIALS_MISSING`, create/open the thread in the browser, and assert exactly one notice with the configured profile label and `is not configured` cause.
8. Assert the failure-process log contains no provider-probe request marker and neither process artifacts contain the real API key.

Use the shared process harness for bounded startup/teardown. Record a sanitized startup-probe completion marker so the success log can assert one probe without exposing request/response content.

- [ ] **Step 4: Verify script structure and compiled frontend before the real run**

```bash
chmod +x scripts/e2e_embedding_startup_status_real.py
.venv/bin/pytest tests/test_embedding_startup_status_smoke_runner.py -q
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
```

Expected: runner contract and build verification pass.

- [ ] **Step 5: Run the dedicated real smoke exactly once**

```bash
.venv/bin/python scripts/e2e_embedding_startup_status_real.py --timeout-seconds 300
```

Expected: one real OpenAI startup probe in the success application, zero fallback notices there, no provider request in the missing-key application, and exactly one lexical-fallback notice there. Do not automatically rerun on failure; report and inspect the preserved artifact directory.

- [ ] **Step 6: Commit the smoke deliverable**

```bash
git add scripts/e2e_embedding_startup_status_real.py tests/test_embedding_startup_status_smoke_runner.py AGENTS.md frontend/dist working-demo
git commit -m "test: add real embedding startup status smoke"
```

Stage `AGENTS.md` or generated bundles only if they actually changed.

---

### Task 8: Full Verification, Review, and Branch Handoff

**Files:**
- Review: all files changed by Tasks 1-7
- Verify: repository worktree and commit history

- [ ] **Step 1: Run focused backend regression suites**

```bash
.venv/bin/pytest tests/test_embedding_profiles.py tests/test_embedding_routes.py tests/test_embedding_startup.py tests/test_embedding_fallback_readiness.py tests/test_session_studies.py tests/test_vectorstore.py tests/test_db_rag_catalog.py tests/test_semantic_publication_knowledge.py tests/test_study_design_documents.py tests/test_api_runtime.py tests/test_api_server.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run frontend tests and production build verification**

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
```

Expected: all frontend tests pass and compiled assets/manifests are current.

- [ ] **Step 3: Run the complete Python suite**

```bash
.venv/bin/pytest -q
```

Expected: full suite passes with only documented skips.

- [ ] **Step 4: Audit the non-blocking and secret contracts**

```bash
rg -n "raise .*Embedding|raise .*embedding" db_rag/embedding_profiles.py db_rag/embedding_routes.py db_rag/embedding_startup.py api/app.py db_rag/session_studies.py
rg -n "api_key|OPENAI_API_KEY|base_url" api/schemas.py frontend/src/EmbeddingFallbackNotice.tsx frontend/src/types.ts
rg -n "initialize_embedding|embed_query" api db_rag
git diff --check
```

Inspect every match. Strict parser/internal adapter errors are acceptable only when caught by the startup boundary; no API/frontend payload may contain secret fields; `initialize_embedding()` must have one production call site in `build_application()`; and no thread/tool path may invoke the startup probe.

- [ ] **Step 5: Request code review and address findings**

Use the `requesting-code-review` skill. Review specifically for accidental hard failures, probe duplication, unsafe exception exposure, silent default substitution after explicit selection, mixed-study message accuracy, checkpoint persistence, and duplicate UI notices. Apply accepted findings with focused regression tests.

- [ ] **Step 6: Re-run affected verification and record final evidence**

After review changes, rerun every affected focused suite plus `git diff --check`. Do not claim completion from an earlier run if code changed afterward.

- [ ] **Step 7: Commit final review fixes and inspect branch state**

```bash
git status --short --branch
git log --oneline --decorate -12
```

If review changes exist:

```bash
git add <reviewed-files>
git commit -m "fix: harden embedding startup fallback"
```

Expected: implementation commits are present, unrelated user changes are untouched, and the worktree is clean before integration decisions.
