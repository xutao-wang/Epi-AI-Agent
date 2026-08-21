# Local-Only Branch Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove AWS, Docker, Cognito, browser-managed provider credentials, and hosted deployment hooks from `local-multi-study` without changing its native local epidemiology, multi-study, conversation, attachment, or Python-runtime behavior.

**Architecture:** The application becomes one local deployment mode: startup obtains `OPENAI_API_KEY` from the local environment, FastAPI injects a fixed `RequestIdentity`, and the runtime receives the startup credential directly. Existing identity-bearing service interfaces and persisted storage layouts remain intact, while infrastructure-only files and UI gates are removed.

**Tech Stack:** Python 3.12, FastAPI, pytest, React 19, TypeScript, Vitest, Vite, SQLite, LangGraph.

## Global Constraints

- Work only on `cleanup/local-only-before-master-merge-20260821`, based on `local-multi-study` commit `9838671` plus the cleanup design commits.
- Do not merge `master`, implement Anthropic/OpenAI-compatible routing, modify `aws-test`, push, or open a pull request during this cleanup.
- Preserve the fixed `local-user` and local-session identity at internal conversation, attachment, storage, and runtime boundaries.
- Preserve all persistent database schemas, runtime paths, installed study packages, schema catalogs, and generated datasets.
- Native `run_fastapi.py` startup remains the supported entry point and requires `OPENAI_API_KEY`.
- Verification must not require AWS credentials, Docker, or a container daemon.
- Remove `frontend/dist/build-manifest.json`; regenerate the remaining tracked frontend build so its hashed assets agree with `frontend/dist/index.html`.

---

### Task 1: Establish the local-only repository boundary

**Files:**
- Create: `tests/test_local_only_repository.py`
- Delete: `.dockerignore`
- Delete: `Dockerfile`
- Delete: `compose.yaml`
- Delete: `infra/aws/**`
- Delete: `deploy/aws/**`
- Delete: `docs/aws/**`
- Delete: `.superpowers/sdd/aws-phase2a-task-7-report.md`
- Delete: AWS-, Cognito-, and Docker-only historical specs/plans listed by `git ls-files docs/superpowers | rg '(aws|cognito|docker)'`
- Delete: AWS release/provisioning/install/recovery/auth/provider smoke scripts listed by `git ls-files scripts | rg '(aws|www_apex|browser_auth|multi_user_isolation|provider_credentials|session_bound_provider|api_auth|api_owner_authorization)'`
- Delete: AWS/Docker-only tests listed by `git ls-files tests | rg '(aws|build_aws_release|smoke_www_apex|smoke_multi_user)'`

**Interfaces:**
- Consumes: the removal boundary in `docs/superpowers/specs/2026-08-21-local-only-branch-cleanup-design.md`.
- Produces: `test_local_only_repository.py`, a permanent guard against reintroducing deployment artifacts to this branch.

- [ ] **Step 1: Write the failing repository-boundary tests**

```python
from pathlib import Path


ROOT = Path(__file__).parents[1]
FORBIDDEN_PATHS = (
    ".dockerignore",
    "Dockerfile",
    "compose.yaml",
    "infra/aws",
    "deploy/aws",
    "docs/aws",
    "frontend/dist/build-manifest.json",
)


def test_local_only_branch_has_no_deployment_artifacts() -> None:
    assert [path for path in FORBIDDEN_PATHS if (ROOT / path).exists()] == []


def test_active_sources_do_not_reference_removed_hosted_features() -> None:
    roots = (ROOT / "api", ROOT / "frontend" / "src", ROOT / "scripts")
    forbidden = (
        "CognitoTokenVerifier",
        "REPORT_AGENT_AUTH_MODE",
        "REPORT_AGENT_AWS_",
        "REPORT_AGENT_COGNITO_",
        "REPORT_AGENT_PYTHON_WORKER_LAUNCHER",
        "/api/session/provider-key",
    )
    matches: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".ts", ".tsx"}:
                text = path.read_text(encoding="utf-8")
                matches.extend(
                    f"{path.relative_to(ROOT)}: {token}"
                    for token in forbidden
                    if token in text
                )
    assert matches == []
```

- [ ] **Step 2: Run the boundary tests and confirm the current branch violates them**

Run: `.venv/bin/pytest tests/test_local_only_repository.py -q`

Expected: FAIL listing the current Docker/AWS paths and hosted feature references.

- [ ] **Step 3: Remove the physical infrastructure subsystem and its single-purpose coverage**

Use `git rm` with the exact tracked paths returned by the three `git ls-files ... | rg ...` inventory commands above. Review the list before executing it. Do not remove generic local tests merely because their fixtures use an AWS-shaped identifier to validate sanitization, and do not remove `db_rag/service/dataset_naming.py`.

- [ ] **Step 4: Run the path half of the boundary test**

Run: `.venv/bin/pytest tests/test_local_only_repository.py::test_local_only_branch_has_no_deployment_artifacts -q`

Expected: PASS. The source-reference test remains red until Tasks 2–4.

- [ ] **Step 5: Commit the infrastructure removal and guard**

```bash
git add tests/test_local_only_repository.py
git commit -m "chore: remove hosted deployment infrastructure"
```

### Task 2: Replace hosted authentication with fixed local identity

**Files:**
- Modify: `api/auth.py`
- Modify: `api/app.py`
- Modify: `api/server.py`
- Modify: `api/schemas.py`
- Delete: `api/public_config.py`
- Delete: `api/provider_credentials.py`
- Modify: `tests/test_api_auth.py`
- Modify: `tests/test_api_multi_user_isolation.py`
- Delete: `tests/test_provider_credentials.py`
- Delete: `tests/test_public_config.py`
- Modify: `tests/test_no_study_startup.py`

**Interfaces:**
- Consumes: `AuthenticatedUser`, `RequestIdentity`, `LOCAL_SESSION_ID`, and existing runtime authorization methods.
- Produces: `LOCAL_REQUEST_IDENTITY: RequestIdentity`, `local_request_identity() -> RequestIdentity`, and `create_app(..., provider_api_key: str, ...) -> FastAPI`.

- [ ] **Step 1: Rewrite auth tests around one fixed local principal**

```python
from api.auth import LOCAL_REQUEST_IDENTITY, LOCAL_SESSION_ID, local_request_identity


def test_local_request_identity_is_fixed_and_stable() -> None:
    first = local_request_identity()
    second = local_request_identity()
    assert first == second == LOCAL_REQUEST_IDENTITY
    assert first.owner_user_id == "local-user"
    assert first.session_id == LOCAL_SESSION_ID
```

Update API tests so protected local routes work without `Authorization`, a supplied user, or a per-browser provider key. Retain owner/thread authorization assertions by constructing alternate `RequestIdentity` values directly at the runtime/service boundary.

- [ ] **Step 2: Run the focused backend tests and confirm they fail**

Run: `.venv/bin/pytest tests/test_api_auth.py tests/test_api_multi_user_isolation.py tests/test_no_study_startup.py -q`

Expected: FAIL because the fixed dependency and direct startup key interface do not exist yet.

- [ ] **Step 3: Reduce `api/auth.py` to local identity types and dependency**

```python
LOCAL_SESSION_ID = "local-session"
LOCAL_REQUEST_IDENTITY = RequestIdentity(
    user=AuthenticatedUser(owner_user_id="local-user", email=None),
    session_id=LOCAL_SESSION_ID,
)


def local_request_identity() -> RequestIdentity:
    return LOCAL_REQUEST_IDENTITY
```

Keep the two dataclasses and remove JWT imports, JWKS networking, token protocols, `LocalTokenVerifier`, `CognitoTokenVerifier`, bearer-token parsing, and configurable verifier factories.

- [ ] **Step 4: Make the application factory always local**

In `api/app.py`, replace `_history_store_for_auth_mode(..., auth_mode=...)` with:

```python
def _history_store(db_path: str | os.PathLike[str]) -> ConversationHistoryStore:
    store = ConversationHistoryStore(db_path)
    store.claim_unowned("local-user")
    return store
```

Read and strip `OPENAI_API_KEY`, raise an actionable `ValueError` when absent, remove auth/public-config/credential-store/worker-launcher construction, construct `LocalPythonRuntime(runtime_root=context.storage.execution)`, and call:

```python
return create_app(
    report_runtime,
    static_dir=selected_static_dir,
    provider_api_key=provider_api_key,
)
```

- [ ] **Step 5: Make FastAPI inject the fixed identity and startup credential**

Change the server signature to:

```python
def create_app(
    runtime: ReportAgentApiRuntime,
    *,
    provider_api_key: str,
    static_dir: Path | None = None,
    cors_origin_regex: str | None = None,
) -> FastAPI:
```

Use `Depends(local_request_identity)`, remove public-config/provider-key/readiness/deployment-status routes and provider/deployment middleware, and replace credential lookup with authorization plus the captured key:

```python
def provider_key_for_work(identity: RequestIdentity, thread_id: str) -> str:
    try:
        runtime.authorize_thread(identity, thread_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    return provider_api_key
```

Retain health, conversations, threads, attachments, capabilities, studies, activity, cancel, submit, and resume routes unchanged apart from their local dependency.

- [ ] **Step 6: Remove obsolete schemas/modules and make focused tests pass**

Delete `PublicAppConfig`, `CognitoPublicConfig`, provider-key request/status, readiness, and deployment-status schemas only after `rg` proves no retained caller uses them. Delete the two hosted-only modules/tests and update remaining factory calls with `provider_api_key="test-key"`.

Run: `.venv/bin/pytest tests/test_api_auth.py tests/test_api_multi_user_isolation.py tests/test_no_study_startup.py tests/test_api_server.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the local identity/application boundary**

```bash
git add api tests
git commit -m "refactor: make api authentication local only"
```

### Task 3: Remove hosted deployment and worker-launcher logic from native startup

**Files:**
- Modify: `api/deployment.py`
- Modify: `run_fastapi.py`
- Modify: `tests/test_api_deployment.py`
- Modify: `tests/test_run_fastapi.py`
- Modify: `tests/test_centralized_epi_agent_architecture.py`
- Modify: `tests/test_epi_python_runtime.py`

**Interfaces:**
- Consumes: existing native path helpers and `ensure_active_provider_credential`.
- Produces: native-only `prepare_environment`, `prepare_provider_credentials`, and `validate_startup`; path helpers remain source-compatible.

- [ ] **Step 1: Replace hosted-mode tests with native-only contracts**

Add or retain assertions equivalent to:

```python
def test_prepare_provider_credentials_always_verifies_openai_key() -> None:
    environ: dict[str, str] = {}
    calls: list[dict[str, object]] = []
    prepare_provider_credentials(environ, verifier=lambda **kwargs: calls.append(kwargs))
    assert calls == [{"environ": environ}]


def test_prepare_environment_does_not_add_auth_or_worker_modes(tmp_path: Path) -> None:
    environ: dict[str, str] = {}
    prepare_environment(project_root=tmp_path, environ=environ)
    assert "REPORT_AGENT_AUTH_MODE" not in environ
    assert "REPORT_AGENT_PYTHON_WORKER_LAUNCHER" not in environ
```

Keep tests for Python 3.12, writable runtime selection, static bundle presence, `.env` persistence, and missing `OPENAI_API_KEY`.

- [ ] **Step 2: Run native startup tests and confirm hosted assumptions fail**

Run: `.venv/bin/pytest tests/test_api_deployment.py tests/test_run_fastapi.py tests/test_centralized_epi_agent_architecture.py tests/test_epi_python_runtime.py -q`

Expected: FAIL until hosted branches and their imports are removed.

- [ ] **Step 3: Simplify deployment helpers**

Retain only `native_runtime_root`, `native_static_dir`, `native_study_root`, `native_checkpoint_db_path`, their environment-aware wrappers still consumed by `api/app.py`, and `cors_allow_origin_regex`. Remove `DeploymentState`, release-manifest/maintenance helpers, hosted paths, `python_worker_launcher`, and auth-mode-dependent secret selection.

- [ ] **Step 4: Simplify native startup**

Implement direct provider verification:

```python
def prepare_provider_credentials(
    environ: MutableMapping[str, str],
    *,
    verifier: Callable[..., None] = ensure_active_provider_credential,
) -> None:
    verifier(environ=environ)
```

Remove `REPORT_AGENT_AUTH_MODE` defaults/parsing and Cognito worker-count validation. Keep Python version, key presence, static build, runtime directory, environment loading, and uvicorn startup checks.

- [ ] **Step 5: Run the native startup/runtime tests**

Run: `.venv/bin/pytest tests/test_api_deployment.py tests/test_run_fastapi.py tests/test_centralized_epi_agent_architecture.py tests/test_epi_python_runtime.py -q`

Expected: PASS without Docker or AWS.

- [ ] **Step 6: Commit native-only startup**

```bash
git add api/deployment.py run_fastapi.py tests
git commit -m "refactor: keep native local startup only"
```

### Task 4: Boot the frontend directly into the local application

**Files:**
- Modify: `frontend/src/main.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/apiClient.ts`
- Modify: `frontend/src/apiClient.test.ts`
- Modify: `frontend/src/types.ts`
- Delete: `frontend/src/AuthGate.tsx`
- Delete: `frontend/src/AuthGate.test.tsx`
- Delete: `frontend/src/ProviderKeyGate.tsx`
- Delete: `frontend/src/ProviderKeyGate.test.tsx`
- Delete: `frontend/src/authClient.ts`
- Delete: `frontend/src/authClient.test.ts`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: existing `createApiClient()` and `App` application behavior.
- Produces: `App` with no authenticated-user/sign-out props and an API client with no token/provider-key lifecycle.

- [ ] **Step 1: Change frontend tests to assert immediate local startup**

Update the `App` render helpers to pass only `apiClient`. Remove hosted gate tests. Update API-client expectations so requests do not contain `Authorization` and the client type exposes no `providerKeyStatus`, `configureProviderKey`, `clearProviderKey`, or `requiresProviderKey` members.

- [ ] **Step 2: Run frontend tests and confirm they fail**

Run: `npm test -- --run`

Working directory: `frontend`

Expected: FAIL because the old component and client interfaces still require hosted auth state.

- [ ] **Step 3: Render the application directly**

```tsx
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App apiClient={createApiClient()} />
  </StrictMode>,
);
```

Remove `User`, `authenticatedUser`, and `onSignOut` from `App` and `AppForTesting`; delete the signed-in/sign-out panel while leaving conversation, study, activity, attachment, review, and run controls unchanged.

- [ ] **Step 4: Simplify the browser client and types**

Remove access-token acquisition, bearer headers, random session identity, public-config types, provider-key methods, and provider-key-required error translation. Keep the existing JSON/error/event-stream behavior for all retained routes. Delete the three hosted-only component/client pairs and remove their unused gate styles.

- [ ] **Step 5: Remove the OIDC dependency and run tests**

Run: `npm uninstall oidc-client-ts`

Working directory: `frontend`

Then run: `npm test -- --run`

Expected: all retained frontend tests PASS.

- [ ] **Step 6: Commit direct local frontend boot**

```bash
git add frontend
git commit -m "refactor: remove hosted frontend gates"
```

### Task 5: Prune active documentation, dependencies, and generated hosted metadata

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `config/app.env`
- Modify: `requirements.txt`
- Modify: `requirements.lock`
- Delete: `frontend/dist/build-manifest.json`
- Regenerate: `frontend/dist/index.html`
- Regenerate: `frontend/dist/assets/**`
- Modify: remaining tests/scripts found by the local-only guard

**Interfaces:**
- Consumes: the local-only backend and frontend from Tasks 2–4.
- Produces: one documented Python startup path and dependency manifests containing no hosted-only packages.

- [ ] **Step 1: Run reference and import inventories**

Run:

```bash
rg -n -i 'aws|cognito|docker|provider-key|provider_key|REPORT_AGENT_AUTH_MODE|REPORT_AGENT_PYTHON_WORKER_LAUNCHER' README.md .env.example config api frontend/src run_fastapi.py scripts tests requirements.txt requirements.lock
```

Classify each match before editing. Keep generic security tests that strip inherited `AWS_*` credentials from local subprocesses, because that is local sandbox hardening rather than AWS deployment support.

- [ ] **Step 2: Update configuration and user documentation**

Document only `python run_fastapi.py`, local `.env`/`OPENAI_API_KEY`, native runtime/study/static path overrides, and frontend development commands. Remove hosted auth, deployment, Docker, and browser-key instructions and their environment variables.

- [ ] **Step 3: Remove now-unused dependencies**

Use `rg` to prove no retained Python module imports `jwt`, `cryptography`, AWS SDKs, CloudFormation tooling, or hosted-only packages before removing the corresponding direct requirement lines. Do not remove transitive packages or runtime packages still imported by retained code.

- [ ] **Step 4: Build the frontend and remove hosted provenance metadata**

Run: `npm run build`

Working directory: `frontend`

Delete `frontend/dist/build-manifest.json` if the build does not remove it. Verify every `/assets/<hash>` referenced by `frontend/dist/index.html` exists and no unreferenced old hashed app bundle remains tracked.

- [ ] **Step 5: Make the repository boundary fully green**

Run: `.venv/bin/pytest tests/test_local_only_repository.py -q`

Expected: PASS with no forbidden artifact or active-source match.

- [ ] **Step 6: Commit documentation, dependency, and build cleanup**

```bash
git add README.md .env.example config requirements.txt requirements.lock frontend/dist tests scripts
git commit -m "chore: finish local-only application cleanup"
```

### Task 6: Verify retained system functionality and audit the branch

**Files:**
- Modify only if a verification failure exposes a cleanup regression; add the smallest regression test beside the affected subsystem before fixing it.

**Interfaces:**
- Consumes: all local-only cleanup commits.
- Produces: evidence that local startup and application behavior remain intact and a reviewed diff ready for integration into `local-multi-study`.

- [ ] **Step 1: Run the complete retained Python suite**

Run: `.venv/bin/pytest -q`

Expected: PASS. Any failure is investigated with the systematic-debugging workflow; the six previously recorded Docker failures no longer exist because Docker-only tests and runtime paths have been removed, not ignored.

- [ ] **Step 2: Run the complete frontend suite and production build**

Run: `npm test -- --run && npm run build`

Working directory: `frontend`

Expected: all retained tests PASS and the Vite production build succeeds.

- [ ] **Step 3: Run targeted local smoke coverage**

Run the retained smoke scripts/tests covering application construction, health, conversation creation/history, message execution/cancellation, attachments/artifacts, installed-study routing/DB-RAG, and `LocalPythonRuntime`. Use dummy/injected model clients where tests provide them; do not make a billable provider call merely to validate cleanup.

Expected: PASS with no AWS account, Cognito token, Docker daemon, or hosted worker.

- [ ] **Step 4: Audit forbidden references and generated assets**

Run:

```bash
git ls-files | rg -i '(^|/)(aws|docker|cognito)|Dockerfile|compose\.ya?ml|build-manifest'
rg -n -i 'CognitoTokenVerifier|REPORT_AGENT_AUTH_MODE|REPORT_AGENT_AWS_|REPORT_AGENT_COGNITO_|REPORT_AGENT_PYTHON_WORKER_LAUNCHER|/api/session/provider-key' api frontend/src run_fastapi.py scripts README.md .env.example config tests
```

Expected: no deployment artifact paths and no active hosted-feature references. Explicit local subprocess environment-denylist mentions of `AWS_*` may remain and must be called out in the final audit.

- [ ] **Step 5: Review repository scope and history**

Run:

```bash
git status --short
git diff --stat 9838671...HEAD
git diff --name-status 9838671...HEAD
git log --oneline --decorate 9838671..HEAD
```

Expected: only the cleanup design/plan and scoped cleanup changes; no study package, schema catalog, runtime database, generated dataset, or `aws-test` mutation.

- [ ] **Step 6: Record any verification-only fix and stop before integration**

If Step 1–5 required a fix, commit it with its regression test:

```bash
git add <exact verified files>
git commit -m "fix: preserve local behavior after cleanup"
```

Otherwise make no empty commit. Report the exact test/build results and remaining intentional references, then wait for explicit authorization before merging this branch into `local-multi-study` or starting the subsequent `master` integration.
