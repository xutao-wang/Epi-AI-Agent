# Credential-Driven Model Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Derive the model selector from successfully verified OpenAI, Anthropic, and registered compatible providers while keeping every saved conversation readable when its original model is unavailable.

**Architecture:** Introduce one immutable model catalog that separates registered profiles from available model IDs and pass that catalog from native startup into application construction. Split persisted-thread normalization from executable-model validation so checkpoint reads never require an active provider, then expose replacement-required metadata for the frontend to confirm a model change before continuing an old thread. Keep compatible endpoint support connection-only: load the existing JSON registry, verify the external endpoint, and never install or launch vLLM or Ray.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite, pytest, React 19, TypeScript, Vitest, Testing Library, Vite.

## Global Constraints

- Do not persist `REPORT_AGENT_MODEL`, `REPORT_AGENT_ALLOWED_MODELS`, `OPENAI_MODEL`, or `REPORT_AGENT_TITLE_MODEL` as normal model-selection configuration.
- Verify configured provider credentials on every `python run_fastapi.py` startup.
- Do not make a billable generation request during startup; provider `/models` checks are sufficient.
- Do not install or launch vLLM, Ray, Docker, AWS, Cognito, or hosted secret storage.
- Preserve the README demo link retained from `master`.
- A compatible endpoint contributes models only when its external endpoint and optional configured key verify successfully.
- Opening, listing, or projecting review status for historical conversations must never require their stored model to be currently available.
- Never silently replace a historical model; continuing with a different model requires explicit user confirmation.
- Preserve historical messages, checkpoints, artifacts, and titles unchanged.
- Keep GPT-5.6 Terra as the default whenever OpenAI is available; otherwise prefer Claude Opus 5, then the first verified compatible model.
- Keys must not be echoed, logged, serialized, or persisted after failed validation.

---

### Task 1: Build the registered/available model catalog and remove persisted model policy

**Files:**
- Create: `utils/model_availability.py`
- Modify: `utils/model_runtime_profiles.py`
- Modify: `utils/env_loader.py`
- Test: `tests/test_model_availability.py`
- Test: `tests/test_env_loader.py`

**Interfaces:**
- Consumes: `MODEL_RUNTIME_PROFILES`, `load_custom_model_profiles()`, and `ModelRuntimeProfile` from `utils/model_runtime_profiles.py`.
- Produces: `ProviderEndpoint`, `ModelAvailability`, `registered_model_profiles(environ)`, `configured_provider_endpoints(environ)`, `build_model_availability(environ, verified_endpoints)`, and `remove_local_env_values(project_root, keys)`.

- [ ] **Step 1: Write failing catalog tests**

Create `tests/test_model_availability.py` with focused cases using no real network:

```python
from utils.model_availability import (
    ProviderEndpoint,
    build_model_availability,
    configured_provider_endpoints,
)


def endpoint(provider: str, key_env: str, base_url: str | None = None):
    return ProviderEndpoint(provider, key_env, base_url)


def test_openai_only_exposes_every_registered_gpt_model() -> None:
    environ = {"OPENAI_API_KEY": "verified"}
    verified = {endpoint("openai", "OPENAI_API_KEY")}

    catalog = build_model_availability(environ, verified)

    assert catalog.available_model_ids == (
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    )
    assert catalog.default_model_id == "gpt-5.6-terra"
    assert catalog.title_model_id == "gpt-5.6-luna"


def test_anthropic_only_exposes_every_registered_claude_model() -> None:
    environ = {"ANTHROPIC_API_KEY": "verified"}
    verified = {endpoint("anthropic", "ANTHROPIC_API_KEY")}

    catalog = build_model_availability(environ, verified)

    assert catalog.available_model_ids == (
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    )
    assert catalog.default_model_id == "claude-opus-5"
    assert catalog.title_model_id == "claude-haiku-4-5"


def test_both_verified_providers_expose_both_families() -> None:
    environ = {
        "OPENAI_API_KEY": "openai",
        "ANTHROPIC_API_KEY": "anthropic",
    }
    verified = {
        endpoint("openai", "OPENAI_API_KEY"),
        endpoint("anthropic", "ANTHROPIC_API_KEY"),
    }

    catalog = build_model_availability(environ, verified)

    assert set(catalog.available_model_ids) == {
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    }
    assert catalog.default_model_id == "gpt-5.6-terra"


def test_failed_compatible_endpoint_models_remain_registered_but_unavailable(
    tmp_path,
) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        '[{"id":"cluster-model","base_url":"https://llm.internal/v1",'
        '"api_key_env":"CLUSTER_LLM_KEY"}]',
        encoding="utf-8",
    )
    environ = {
        "REPORT_AGENT_CUSTOM_MODELS_PATH": str(registry),
        "CLUSTER_LLM_KEY": "configured",
    }

    catalog = build_model_availability(environ, set())

    assert "cluster-model" in catalog.registered_profiles
    assert "cluster-model" not in catalog.available_model_ids
```

- [ ] **Step 2: Run the catalog tests and confirm the module is missing**

Run:

```bash
.venv/bin/python -m pytest tests/test_model_availability.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'utils.model_availability'`.

- [ ] **Step 3: Implement the immutable catalog**

Create `utils/model_availability.py` with these public types and selection rules:

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from utils.model_runtime_profiles import (
    MODEL_RUNTIME_PROFILES,
    ModelRuntimeProfile,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    load_custom_model_profiles,
)


@dataclass(frozen=True, order=True)
class ProviderEndpoint:
    provider: str
    api_key_env: str
    base_url: str | None = None


@dataclass(frozen=True)
class ModelAvailability:
    registered_profiles: Mapping[str, ModelRuntimeProfile]
    available_model_ids: tuple[str, ...]
    default_model_id: str
    title_model_id: str


def registered_model_profiles(environ: Mapping[str, str]):
    return {
        **MODEL_RUNTIME_PROFILES,
        **load_custom_model_profiles(environ=environ),
    }


def profile_endpoint(profile: ModelRuntimeProfile) -> ProviderEndpoint:
    return ProviderEndpoint(
        provider=profile.provider,
        api_key_env=profile.api_key_env,
        base_url=profile.base_url,
    )


def configured_provider_endpoints(environ: Mapping[str, str]):
    profiles = registered_model_profiles(environ)
    endpoints = {
        profile_endpoint(profile)
        for profile in profiles.values()
        if (
            (profile.provider == PROVIDER_OPENAI and environ.get("OPENAI_API_KEY", "").strip())
            or (profile.provider == PROVIDER_ANTHROPIC and environ.get("ANTHROPIC_API_KEY", "").strip())
            or profile.base_url is not None
        )
    }
    return tuple(sorted(endpoints))


def build_model_availability(
    environ: Mapping[str, str],
    verified_endpoints: set[ProviderEndpoint],
) -> ModelAvailability:
    profiles = registered_model_profiles(environ)
    available = tuple(
        model_id
        for model_id, profile in profiles.items()
        if profile_endpoint(profile) in verified_endpoints
    )
    if not available:
        raise ValueError("No verified AI model provider is available.")
    default = (
        "gpt-5.6-terra"
        if "gpt-5.6-terra" in available
        else "claude-opus-5"
        if "claude-opus-5" in available
        else available[0]
    )
    default_provider = profiles[default].provider
    preferred_title = {
        PROVIDER_OPENAI: "gpt-5.6-luna",
        PROVIDER_ANTHROPIC: "claude-haiku-4-5",
    }.get(default_provider, default)
    title = preferred_title if preferred_title in available else default
    return ModelAvailability(profiles, available, default, title)
```

Update `CustomModelEntry.to_profile()` so `api_key_required` is `bool(self.api_key_env.strip())`; a blank key name remains explicitly keyless.

- [ ] **Step 4: Write and run failing environment-migration tests**

Add to `tests/test_env_loader.py`:

```python
def test_remove_local_env_values_preserves_unrelated_lines_and_mode(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# local\nOPENAI_API_KEY=secret\nREPORT_AGENT_MODEL=gpt-5.4\n"
        "REPORT_AGENT_ALLOWED_MODELS=gpt-5.4\nREPORT_AGENT_RUNTIME_ROOT=/data\n",
        encoding="utf-8",
    )

    remove_local_env_values(
        tmp_path,
        {"REPORT_AGENT_MODEL", "REPORT_AGENT_ALLOWED_MODELS"},
    )

    assert path.read_text(encoding="utf-8") == (
        "# local\nOPENAI_API_KEY=secret\nREPORT_AGENT_RUNTIME_ROOT=/data\n"
    )
    assert path.stat().st_mode & 0o777 == 0o600
```

Run:

```bash
.venv/bin/python -m pytest tests/test_env_loader.py -q
```

Expected: FAIL because `remove_local_env_values` is not defined.

- [ ] **Step 5: Implement atomic environment-key removal**

Add `remove_local_env_values(project_root, keys)` to `utils/env_loader.py`, using the same temporary-file, `chmod(0o600)`, and atomic `replace()` sequence as `persist_local_env_values()`. Remove matching assignment lines without touching comments or unrelated values.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_model_availability.py tests/test_env_loader.py -q
```

Expected: PASS.

Commit:

```bash
git add utils/model_availability.py utils/model_runtime_profiles.py utils/env_loader.py tests/test_model_availability.py tests/test_env_loader.py
git commit -m "feat: derive model catalog from configured providers"
```

---

### Task 2: Make native startup verify providers and return only usable models

**Files:**
- Modify: `run_fastapi.py`
- Modify: `utils/provider_startup.py`
- Modify: `utils/runtime_defaults.py`
- Modify: `config/app.env`
- Modify: `.env.example`
- Modify: `tests/test_run_fastapi.py`
- Modify: `tests/test_provider_startup.py`

**Interfaces:**
- Consumes: `ProviderEndpoint`, `ModelAvailability`, `configured_provider_endpoints()`, and `build_model_availability()` from Task 1.
- Produces: `configure_and_verify_providers(...) -> ModelAvailability`; `main()` passes that catalog into `build_application()`.

- [ ] **Step 1: Write failing startup-selection tests**

Add tests to `tests/test_run_fastapi.py` that inject a verifier and non-echoing key reader:

```python
def test_existing_openai_key_verifies_and_exposes_only_gpt_models(tmp_path) -> None:
    calls = []
    environ = {"OPENAI_API_KEY": "openai-key"}

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        verifier=lambda provider, key, **kwargs: calls.append((provider, key, kwargs)),
        persist=lambda *_args: None,
        input_fn=lambda _prompt: pytest.fail("provider menu should not open"),
        getpass_fn=lambda _prompt: pytest.fail("key prompt should not open"),
    )

    assert [call[0] for call in calls] == ["openai"]
    assert all(model.startswith("gpt-") for model in catalog.available_model_ids)


def test_no_keys_can_configure_both_and_persists_only_verified_keys(tmp_path) -> None:
    answers = iter(["3"])
    secrets = iter(["openai-key", "anthropic-key"])
    saved = []

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ={},
        input_fn=lambda _prompt: next(answers),
        getpass_fn=lambda _prompt: next(secrets),
        verifier=lambda *_args, **_kwargs: None,
        persist=lambda _root, values: saved.append(values),
    )

    assert {next(iter(item)) for item in saved} == {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }
    assert "gpt-5.6-terra" in catalog.available_model_ids
    assert "claude-opus-5" in catalog.available_model_ids


def test_failed_compatible_endpoint_is_omitted_when_user_continues(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        '[{"id":"cluster-model","base_url":"https://llm.internal/v1",'
        '"api_key_env":"CLUSTER_LLM_KEY"}]',
        encoding="utf-8",
    )
    environ = {
        "OPENAI_API_KEY": "openai-key",
        "CLUSTER_LLM_KEY": "cluster-key",
        "REPORT_AGENT_CUSTOM_MODELS_PATH": str(registry),
    }
    answers = iter(["3"])

    def verifier(provider, _key, *, base_url=None):
        if base_url == "https://llm.internal/v1":
            raise ProviderCredentialError("network", "endpoint unavailable")

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: next(answers),
        getpass_fn=lambda _prompt: pytest.fail("no key prompt expected"),
        verifier=verifier,
        persist=lambda *_args: None,
    )

    assert "gpt-5.6-terra" in catalog.available_model_ids
    assert "cluster-model" not in catalog.available_model_ids


def test_failed_anthropic_key_does_not_replace_saved_working_key(tmp_path) -> None:
    environ = {"ANTHROPIC_API_KEY": "working-key"}
    saved = []
    answers = iter(["1", "3"])

    def verifier(_provider, key, **_kwargs):
        if key == "replacement-key":
            raise ProviderCredentialError("authentication", "key rejected")

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: next(answers),
        getpass_fn=lambda _prompt: "replacement-key",
        verifier=verifier,
        persist=lambda _root, values: saved.append(values),
        force=True,
    )

    assert environ["ANTHROPIC_API_KEY"] == "working-key"
    assert saved == []
    assert "claude-opus-5" in catalog.available_model_ids


def test_native_runtime_persists_root_but_derives_checkpoint_path(tmp_path) -> None:
    selected = configure_native_runtime(
        project_root=tmp_path,
        environ={},
        input_fn=lambda _prompt: "",
        choose_directory=lambda: None,
        persist=True,
    )

    saved = dotenv_values(tmp_path / ".env")
    assert saved["REPORT_AGENT_RUNTIME_ROOT"] == str(selected)
    assert "REPORT_AGENT_CHECKPOINT_DB_PATH" not in saved
    environ = {"REPORT_AGENT_RUNTIME_ROOT": str(selected)}
    prepare_environment(project_root=tmp_path, environ=environ)
    assert environ["REPORT_AGENT_CHECKPOINT_DB_PATH"] == str(
        selected / "agent_memory_fastapi.db"
    )
```

For this failure menu, action `3` is “Continue with working providers.” Keep that label and numeric choice synchronized with the test.

- [ ] **Step 2: Run the startup tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_run_fastapi.py -k 'providers or compatible' -q
```

Expected: FAIL because `configure_and_verify_providers` does not exist and the launcher still writes model variables.

- [ ] **Step 3: Implement the provider setup state machine**

Replace `configure_model_provider()` plus `ensure_provider_credentials()` with one orchestrator:

```python
def configure_and_verify_providers(
    *,
    project_root=PROJECT_ROOT,
    environ=os.environ,
    input_fn=input,
    getpass_fn=getpass.getpass,
    output_fn=print,
    verifier=verify_provider_credential,
    persist=persist_local_env_values,
    force=False,
) -> ModelAvailability:
    remove_local_env_values(project_root, DEPRECATED_MODEL_ENV_KEYS)
    for key in DEPRECATED_MODEL_ENV_KEYS:
        environ.pop(key, None)

    configured = list(configured_provider_endpoints(environ))
    if force or not any(item.provider in {"openai", "anthropic"} for item in configured):
        configured = _provider_setup_menu(
            configured,
            environ=environ,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
            output_fn=output_fn,
            verifier=verifier,
            persist=persist,
            project_root=project_root,
        )

    verified: set[ProviderEndpoint] = set()
    for endpoint in configured:
        action = _verify_configured_endpoint(
            endpoint,
            environ=environ,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
            output_fn=output_fn,
            verifier=verifier,
            persist=persist,
            project_root=project_root,
        )
        if action == "verified":
            verified.add(endpoint)
    return build_model_availability(environ, verified)
```

The menu text must be exactly provider-oriented:

```text
No AI provider is configured.

1. Configure OpenAI
2. Configure Anthropic
3. Configure both
4. Connect to a compatible endpoint
```

For failed keys/endpoints, implement `retry`, `different provider`, `continue with working providers` when at least one provider verified, and `exit`. Never persist the attempted key before verification. Compatible registrations stay in JSON and are merely omitted from the returned catalog when verification fails.

Before calling the compatible verifier, enforce the registration's key semantics:

```python
key = normalize_secret_input(environ.get(endpoint.api_key_env, ""))
if endpoint.api_key_env and not key:
    raise ProviderCredentialError(
        "missing",
        f"{endpoint.api_key_env} is required by the compatible endpoint registration.",
    )
```

Remove active/default assignments and explanatory text for these variables from `config/app.env` and `.env.example`: `REPORT_AGENT_MODEL`, `REPORT_AGENT_ALLOWED_MODELS`, `OPENAI_MODEL`, and `REPORT_AGENT_TITLE_MODEL`. Retain `REPORT_AGENT_CUSTOM_MODELS_PATH` as an optional compatible-endpoint registry path.

Change `utils/runtime_defaults.py` so default/model/title selection consumes `ModelAvailability`; no function may read the four deprecated model variables. Keep deprecated function names only as short compatibility wrappers around credential-derived availability while callers migrate in Task 3.

Also stop persisting `REPORT_AGENT_CHECKPOINT_DB_PATH` in `configure_native_runtime()`. Derive it from `<REPORT_AGENT_RUNTIME_ROOT>/agent_memory_fastapi.db` in `prepare_environment()`, while preserving an already inherited process-level override for tests. Include `REPORT_AGENT_CHECKPOINT_DB_PATH` in the one-time `.env` key removal together with the four deprecated model variables.

- [ ] **Step 4: Make every startup use the returned catalog**

Change `main()` to construct the app with the verified catalog:

```python
catalog = configure_and_verify_providers(force=args.reconfigure)
validate_startup(model_availability=catalog)

from api.app import build_application

application = build_application(
    environ=os.environ,
    model_availability=catalog,
)
uvicorn.run(application, host=args.host, port=args.port, log_level="info")
```

Delete the launcher path that writes `REPORT_AGENT_MODEL` and `REPORT_AGENT_ALLOWED_MODELS`. Keep `verify_provider_credential()` backed by `/models`, including its accepted compatible-endpoint 404 behavior.

- [ ] **Step 5: Expand verifier tests for Anthropic and keyless compatible endpoints**

In `tests/test_provider_startup.py`, add mocked-client tests proving:

```python
verify_provider_credential(
    "anthropic",
    "anthropic-key",
    anthropic_checker=lambda key: seen.append(("anthropic", key)),
)
verify_provider_credential(
    "openai_compatible",
    "",
    base_url="http://127.0.0.1:8001/v1",
    openai_checker=lambda key, **kwargs: seen.append((key, kwargs)),
)

assert seen == [
    ("anthropic", "anthropic-key"),
    ("", {
        "base_url": "http://127.0.0.1:8001/v1",
        "provider_label": "The custom endpoint",
        "allow_empty_key": True,
    }),
]
```

- [ ] **Step 6: Run startup tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_provider_startup.py tests/test_run_fastapi.py tests/test_model_availability.py -q
```

Expected: PASS.

Commit:

```bash
git add run_fastapi.py utils/provider_startup.py utils/runtime_defaults.py config/app.env .env.example tests/test_run_fastapi.py tests/test_provider_startup.py
git commit -m "feat: verify configured providers at native startup"
```

---

### Task 3: Construct the API runtime from the verified model catalog

**Files:**
- Modify: `api/app.py`
- Modify: `tests/test_no_study_startup.py`
- Modify: `tests/test_api_server.py`
- Modify: `tests/test_api_runtime.py`

**Interfaces:**
- Consumes: `ModelAvailability` from Task 1 and the instance returned by Task 2.
- Produces: `build_application(environ=None, model_availability=None)` with `ReportAgentApiRuntime.models` containing exactly `available_model_ids`.

- [ ] **Step 1: Write failing application-construction tests**

Add tests that pass explicit catalogs and avoid network:

```python
def test_build_application_uses_only_catalog_available_models(tmp_path) -> None:
    environ = {
        "ANTHROPIC_API_KEY": "anthropic",
        "REPORT_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
        "REPORT_AGENT_STUDY_ROOT": str(tmp_path / "studies"),
        "REPORT_AGENT_STATIC_DIR": str(tmp_path / "static"),
    }
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    catalog = build_model_availability(
        environ,
        {ProviderEndpoint("anthropic", "ANTHROPIC_API_KEY")},
    )

    app = build_application(
        environ=environ,
        model_availability=catalog,
    )

    runtime = app.state.report_agent_runtime
    assert runtime.models == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ]
    assert runtime.default_runtime_settings["model_name"] == "claude-opus-5"
```

Add a second case with both verified providers and assert all registered GPT and Claude IDs are present once each.

- [ ] **Step 2: Run the application tests and verify the signature failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_no_study_startup.py tests/test_api_server.py -k 'catalog or models' -q
```

Expected: FAIL because `build_application()` does not accept `model_availability`.

- [ ] **Step 3: Inject availability into application construction**

Change the signature and defaults:

```python
def build_application(
    *,
    environ: Mapping[str, str] | None = None,
    model_availability: ModelAvailability | None = None,
) -> FastAPI:
    if environ is None:
        load_app_environment()
        environ = os.environ
    catalog = model_availability or availability_from_configured_credentials(environ)
    model_name = catalog.default_model_id
    allowed_models = catalog.available_model_ids
    title_model = catalog.title_model_id
```

`availability_from_configured_credentials()` is the non-native fallback for direct `uvicorn api.app:app` imports: it treats non-empty provider keys and registered compatible endpoints as configured, but the documented native path remains responsible for live verification.

Use `catalog.registered_profiles[settings.model_name]` in `graph_factory` instead of requiring a currently available global profile lookup. Preserve OpenAI-dependent DB-RAG behavior: an Anthropic-only catalog starts successfully while DB-RAG reports `not_configured` without `OPENAI_API_KEY`.

- [ ] **Step 4: Run application/runtime option tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_no_study_startup.py tests/test_api_server.py tests/test_api_runtime.py -k 'runtime_options or catalog or no_study or capability' -q
```

Expected: PASS, and `/api/runtime/options` returns every and only available model descriptor.

Commit:

```bash
git add api/app.py tests/test_no_study_startup.py tests/test_api_server.py tests/test_api_runtime.py
git commit -m "feat: inject verified model catalog into API runtime"
```

---

### Task 4: Make historical conversation reads independent of current availability

**Files:**
- Modify: `api/runtime.py`
- Modify: `api/schemas.py`
- Modify: `api/conversation_history.py`
- Modify: `api/server.py`
- Modify: `tests/test_api_runtime.py`
- Modify: `tests/test_api_server.py`

**Interfaces:**
- Consumes: registered profile mapping and available model IDs supplied to `ReportAgentApiRuntime`.
- Produces: `_normalize_persisted_settings()`, `_normalize_executable_settings()`, `ModelReplacementRequiredError`, `model_available`, `model_replacement_required`, and `ConversationHistoryStore.set_model()`.

- [ ] **Step 1: Write the exact regression test for `gpt-5.6-terra` history**

Add to `tests/test_api_runtime.py`:

```python
def test_saved_gpt_thread_loads_when_only_claude_is_available(tmp_path) -> None:
    history = ConversationHistoryStore(tmp_path / "history.db")
    history.create("local-user", "saved-thread", model_name="gpt-5.6-terra")
    runtime = ReportAgentApiRuntime(
        graph_factory=_RecordingGraphFactory(),
        default_runtime_settings={
            **_DEFAULT_RUNTIME_SETTINGS,
            "model_name": "claude-opus-5",
        },
        models=["claude-opus-5"],
        history_store=history,
    )

    state = runtime.state(_LOCAL_IDENTITY, "saved-thread")
    listed = runtime.list_conversations(_LOCAL_IDENTITY)

    assert state.runtime_settings.model_name == "gpt-5.6-terra"
    assert state.model_label
    assert state.model_available is False
    assert state.model_replacement_required is True
    assert listed[0].thread_id == "saved-thread"
```

Add a second test with `model_name="removed-custom-model"` and assert the state loads with the ID intact and no call to the graph factory.

- [ ] **Step 2: Run the regression and confirm the current ValueError**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py -k 'saved_gpt_thread_loads or removed_custom_model' -q
```

Expected: FAIL with `ValueError: Unsupported model: gpt-5.6-terra` or the equivalent unknown-profile failure.

- [ ] **Step 3: Split persisted normalization from execution validation**

Implement separate methods; do not add an `allow_unavailable=True` flag:

```python
def _normalize_persisted_settings(self, settings=None) -> RuntimeSettings:
    normalized = self._normalize_shape_and_ranges(settings)
    profile = self.registered_models.get(normalized.model_name)
    if profile is None:
        return normalized
    return self._apply_profile_defaults(normalized, profile, settings)


def _normalize_executable_settings(self, settings=None) -> RuntimeSettings:
    normalized = self._normalize_persisted_settings(settings)
    if normalized.model_name not in self.models:
        raise ValueError(f"Unsupported model: {normalized.model_name}")
    return normalized
```

Use executable normalization in `create_thread()`, `runtime_info()`, `runtime_options()`, and any requested replacement. Use persisted normalization in `_require_owned_thread()`. Add `registered_models: Mapping[str, ModelRuntimeProfile]` to the runtime; default it from built-ins plus loaded custom profiles for compatibility with existing unit constructors.

For an unknown model ID, preserve the stored ID and the existing provider-independent defaults. Do not call `model_runtime_profile()` for it while loading state.

- [ ] **Step 4: Expose availability and prevent unconfirmed execution**

Extend `ThreadRuntime` and `ApiThreadState`:

```python
@dataclass
class ThreadRuntime:
    settings: RuntimeSettings
    model_available: bool = True
    # existing fields remain unchanged


class ApiThreadState(BaseModel):
    # existing fields remain unchanged
    model_label: str = ""
    model_available: bool = True
    model_replacement_required: bool = False
```

Extend `project_thread_state()` with `model_label: str = ""` and `model_available: bool = True`, and pass both from every `state()` return path. Resolve the label from `self.registered_models` and fall back to the raw stored ID for unknown legacy models. Set `model_replacement_required=not model_available` in the projected `ApiThreadState`; this keeps list/state/review projection on one consistent availability calculation.

Add the domain error:

```python
class ModelReplacementRequiredError(RuntimeError):
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__(
            f"Model {model_name} is unavailable. Choose an available model to continue."
        )
```

Before `_ensure_graph()` in `submit_message()`:

```python
if not thread.model_available:
    if not model_name:
        raise ModelReplacementRequiredError(thread.settings.model_name)
    replacement = self._normalize_executable_settings({"model_name": model_name})
    self._replace_unavailable_model(identity, thread, replacement, provider_api_key)
elif model_name and thread.locked:
    raise ValueError("The model is locked for this conversation.")
```

`_replace_unavailable_model()` must build the replacement graph successfully before persisting the new model, then set `thread.settings`, `thread.model_available=True`, and retain `thread.locked=True`.

Implement rollback explicitly:

```python
def _replace_unavailable_model(
    self,
    identity: RequestIdentity,
    thread: ThreadRuntime,
    replacement: RuntimeSettings,
    provider_api_key: str | None,
) -> None:
    previous = thread.settings
    thread.settings = replacement
    try:
        self._ensure_graph(identity, thread, provider_api_key)
    except Exception:
        thread.settings = previous
        self._clear_graph(thread)
        raise
    if self.history_store is not None:
        self.history_store.set_model(
            identity.owner_user_id,
            thread.thread_id,
            replacement.model_name,
        )
    thread.model_available = True
    thread.locked = True
```

- [ ] **Step 5: Persist only an explicitly confirmed successful replacement**

Add to `ConversationHistoryStore`:

```python
def set_model(self, owner_user_id: str, thread_id: str, model_name: str) -> bool:
    with self._connect() as connection:
        result = connection.execute(
            "UPDATE conversation_history SET model_name = ?, updated_at = ? "
            "WHERE owner_user_id = ? AND thread_id = ?",
            (model_name, self._now(), owner_user_id, thread_id),
        )
    return result.rowcount == 1
```

If graph construction fails, restore the prior in-memory settings and do not call `set_model()`. Existing checkpoint data is not rewritten.

Catch `ModelReplacementRequiredError` in `api/server.py` and return HTTP 409. Continue mapping invalid new-thread model selections to HTTP 400.

- [ ] **Step 6: Add API-level recovery tests**

In `tests/test_api_server.py`, create a saved GPT thread under a Claude-only runtime and assert:

```python
state = client.get("/api/threads/saved-thread/state")
assert state.status_code == 200
assert state.json()["model_replacement_required"] is True

blocked = client.post(
    "/api/threads/saved-thread/messages",
    json={"text": "continue"},
)
assert blocked.status_code == 409

continued = client.post(
    "/api/threads/saved-thread/messages",
    json={"text": "continue", "model_name": "claude-opus-5"},
)
assert continued.status_code == 200
assert history.get("local-user", "saved-thread").model_name == "claude-opus-5"
```

Also assert `/api/conversations` does not log `Conversation review status projection failed` for the unavailable saved model.

- [ ] **Step 7: Run backend compatibility tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py tests/test_api_server.py -q
```

Expected: PASS.

Commit:

```bash
git add api/runtime.py api/schemas.py api/conversation_history.py api/server.py tests/test_api_runtime.py tests/test_api_server.py
git commit -m "fix: keep unavailable-model conversations readable"
```

---

### Task 5: Let the frontend explicitly continue an unavailable-model conversation

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/apiClient.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/apiClient.test.ts`

**Interfaces:**
- Consumes: `ApiThreadState.model_available`, `model_replacement_required`, available `RuntimeOptions.models`, and the existing optional `model_name` submit payload.
- Produces: a replacement notice, available-model selector, explicit confirmation, and submission of the selected replacement model.

- [ ] **Step 1: Write the failing browser-component recovery test**

Add an `App.test.tsx` case that returns a saved state with `gpt-5.6-terra`, `model_available: false`, and `model_replacement_required: true`, while runtime options contain only Claude models:

```tsx
expect(await screen.findByText(/used GPT-5.6 Terra/i)).toBeInTheDocument();
expect(screen.getByRole("combobox", { name: "Replacement model" })).toBeEnabled();

await user.selectOptions(
  screen.getByRole("combobox", { name: "Replacement model" }),
  "claude-opus-5",
);
await user.type(screen.getByRole("textbox"), "Continue this analysis");
await user.click(screen.getByRole("button", { name: "Send" }));

expect(window.confirm).toHaveBeenCalledWith(
  "This conversation used GPT-5.6 Terra. Continue with Claude Opus 5?",
);
expect(fetchMock).toHaveBeenCalledWith(
  expect.stringContaining("/messages"),
  expect.objectContaining({
    body: JSON.stringify({
      text: "Continue this analysis",
      model_name: "claude-opus-5",
    }),
  }),
);
```

Add a cancellation assertion: when `window.confirm` returns false, no message request is sent and the draft remains.

- [ ] **Step 2: Run the frontend test and verify missing metadata/UI**

Run:

```bash
cd frontend && npm test -- App.test.tsx
```

Expected: FAIL because the state type and replacement UI do not exist.

- [ ] **Step 3: Add typed state fields and safe selection behavior**

Extend `ApiThreadState` in `frontend/src/types.ts`:

```ts
model_available: boolean;
model_replacement_required: boolean;
model_label: string;
```

When applying a replacement-required state, retain the historical model for display but initialize a separate `replacementModelName` to `""`. Do not place the unavailable historical ID into the ordinary available-model `<select>`.

Render:

```tsx
{state?.model_replacement_required ? (
  <div className="model-replacement-notice" role="alert">
    <p>{`This conversation used ${historicalModelLabel}, which is not currently available.`}</p>
    <label>
      <span>Replacement model</span>
      <select
        aria-label="Replacement model"
        value={replacementModelName}
        onChange={(event) => setReplacementModelName(event.target.value)}
      >
        <option value="">Choose a model</option>
        {modelProviderGroups.map((group) => (
          <optgroup key={group.label} label={group.label}>
            {group.models.map((model) => (
              <option key={model.id} value={model.id}>
                {model.label}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </label>
  </div>
) : null}
```

Reuse the existing grouping markup directly; extract a small local renderer only if that avoids duplicating the same `<optgroup>` block.

Derive `historicalModelLabel` from `state.model_label || state.runtime_settings?.model_name || "Unknown model"`. Add `state?.model_replacement_required && !replacementModelName` to `isSendDisabled`, so the composer cannot submit until the user chooses an available model.

- [ ] **Step 4: Confirm and send only when a replacement is required**

In `submitMessage()` compute:

```ts
const replacementModel = state?.model_replacement_required
  ? replacementModelName
  : undefined;
if (state?.model_replacement_required && !replacementModel) {
  setError("Choose an available model before continuing this conversation.");
  return;
}
if (
  replacementModel &&
  !window.confirm(
    `This conversation used ${historicalModelLabel}. Continue with ${replacementModelLabel}?`,
  )
) {
  return;
}
```

Pass `replacementModel` as the fifth argument to the existing `apiClient.submitMessage()`. After a successful response reports `model_replacement_required: false`, clear `replacementModelName`. Ordinary new and available-model historical conversations retain their current flow.

- [ ] **Step 5: Run frontend tests and build, then commit**

Run:

```bash
cd frontend && npm test -- App.test.tsx apiClient.test.ts RuntimeSettingsPanel.test.tsx
cd frontend && npm run build
```

Expected: all tests PASS and TypeScript/Vite build succeeds.

Commit the source and compiled static bundle expected by this repository:

```bash
git add frontend/src frontend/dist
git commit -m "feat: confirm model replacement for saved conversations"
```

---

### Task 6: Classify Anthropic exhausted-credit failures correctly

**Files:**
- Modify: `utils/provider_errors.py`
- Modify: `tests/test_api_runtime.py`

**Interfaces:**
- Consumes: `classify_llm_error(exc) -> tuple[str, str]`.
- Produces: Anthropic `BadRequestError` credit/billing responses mapped to `PROVIDER_CREDITS_EXHAUSTED`.

- [ ] **Step 1: Write the failing Anthropic low-credit test**

Add to the provider-error section of `tests/test_api_runtime.py`:

```python
def test_anthropic_low_credit_bad_request_is_actionable() -> None:
    import anthropic

    error = anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=Response(
            400,
            request=Request("POST", "https://api.anthropic.com/v1/messages"),
        ),
        body={"error": {"type": "invalid_request_error", "message": "credit balance is too low"}},
    )

    code, message = classify_llm_error(error)

    assert code == "PROVIDER_CREDITS_EXHAUSTED"
    assert "Add credits or use a funded API key" in message
    assert "credit balance is too low" not in message
```

- [ ] **Step 2: Run the test and confirm generic classification**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py -k 'anthropic_low_credit' -q
```

Expected: FAIL because the result is `RUN_FAILED`.

- [ ] **Step 3: Add narrow credit markers before context handling**

In `_classify_anthropic_error()` handle `BadRequestError` safely:

```python
if isinstance(exc, anthropic.BadRequestError):
    message = str(exc).lower()
    markers = _body_markers(getattr(exc, "body", {}))
    if (
        "credit balance" in message
        or "billing" in message
        or markers & {"credit_balance_exhausted", "insufficient_credits"}
    ):
        return _public_failure(
            "PROVIDER_CREDITS_EXHAUSTED",
            "The Anthropic account has no remaining API credits. Add "
            "credits or use a funded API key, then retry.",
        )
    if "prompt is too long" in message or "context" in message:
        return _public_failure(
            "PROVIDER_CONTEXT_LIMIT_EXCEEDED",
            "This conversation exceeds the selected model's context limit. "
            "Start a new conversation or reduce the attached content.",
        )
    return _generic_status_failure()
```

- [ ] **Step 4: Run provider-error coverage and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py -k 'provider_failure or anthropic' -q
```

Expected: PASS.

Commit:

```bash
git add utils/provider_errors.py tests/test_api_runtime.py
git commit -m "fix: report exhausted Anthropic credits"
```

---

### Task 7: Update setup documentation and perform full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/working-demo.md`
- Modify: `.env` locally without staging it, removing deprecated model assignments

**Interfaces:**
- Consumes: completed native setup, catalog, history recovery, and frontend behavior from Tasks 1–6.
- Produces: accurate local setup instructions and final verification evidence.

- [ ] **Step 1: Rewrite the user-facing model setup section**

Document this startup behavior in `README.md` and `docs/working-demo.md`:

```text
python run_fastapi.py

OpenAI key only: all registered GPT models are shown.
Anthropic key only: all registered Claude models are shown.
Both keys: both model families are shown.
Compatible endpoint: prepare config/custom_models.json; the application checks
the existing endpoint but does not install or start its serving software.

python run_fastapi.py --reconfigure
```

Remove instructions telling users to set the four deprecated model variables. Explain that a historical conversation remains readable if its old provider is unavailable and requires confirmed replacement only when sending the next message.

- [ ] **Step 2: Remove deprecated assignments from the developer's local `.env`**

Run the project migration function, without printing `.env` contents:

```bash
.venv/bin/python -c 'from utils.env_loader import remove_local_env_values; remove_local_env_values(".", {"REPORT_AGENT_MODEL", "REPORT_AGENT_ALLOWED_MODELS", "OPENAI_MODEL", "REPORT_AGENT_TITLE_MODEL"})'
```

Confirm names, not values:

```bash
rg -n '^(REPORT_AGENT_MODEL|REPORT_AGENT_ALLOWED_MODELS|OPENAI_MODEL|REPORT_AGENT_TITLE_MODEL)=' .env
```

Expected: exit status 1 with no output. Do not stage `.env`.

- [ ] **Step 3: Run the complete backend suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all non-environmental tests PASS. Compare any Docker-dependent failures only against the six recorded pre-merge baseline failures; this change must introduce no additional failures.

- [ ] **Step 4: Run the complete frontend suite and production build**

Run:

```bash
cd frontend && npm test
cd frontend && npm run build
```

Expected: all tests PASS and `frontend/dist/index.html` is rebuilt successfully.

- [ ] **Step 5: Run a real local history smoke without exposing credentials**

Use an isolated runtime database and a saved conversation whose recorded model is not in the available provider family. Start `python run_fastapi.py` normally, then verify through the browser:

1. The saved conversation appears in the sidebar.
2. Opening it returns its full messages with no HTTP 500.
3. The UI identifies the unavailable historical model.
4. Sending is blocked until an available replacement is selected.
5. Cancelling confirmation sends nothing.
6. Confirming sends with the replacement and preserves prior messages.
7. A forced provider-credit fixture produces `PROVIDER_CREDITS_EXHAUSTED`, not `RUN_FAILED`.

Record the commands, temporary runtime path, provider family, and pass/fail outcomes in the commit message or handoff; never record key values.

- [ ] **Step 6: Check forbidden configuration and repository state**

Run:

```bash
rg -n 'REPORT_AGENT_MODEL|REPORT_AGENT_ALLOWED_MODELS|OPENAI_MODEL|REPORT_AGENT_TITLE_MODEL' README.md .env.example config/app.env run_fastapi.py utils api frontend/src tests
git diff --check
git status --short
```

Expected: deprecated names appear only in migration/removal tests or compatibility comments that explicitly state they are removed; `git diff --check` has no output; `.env` is not staged.

- [ ] **Step 7: Commit documentation and verification adjustments**

```bash
git add README.md docs/working-demo.md frontend/dist
git commit -m "docs: explain credential-driven model setup"
```

If `frontend/dist` is unchanged, omit it from `git add` rather than creating a cosmetic edit.
