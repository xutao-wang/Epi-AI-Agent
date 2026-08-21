# Credential-Driven Model Configuration Design

**Date:** 2026-08-21

## Purpose

Replace the provider-specific model allowlist written by the native launcher
with provider-driven model availability. Adding Anthropic or a registered
compatible endpoint must expand the models that can be used without making
existing conversations unreadable.

The design also makes first-run provider setup explicit, preserves local data
locations, and reports provider failures such as exhausted Anthropic credits
with actionable errors.

## Current problems

The merged launcher currently writes both `REPORT_AGENT_MODEL` and
`REPORT_AGENT_ALLOWED_MODELS` to `.env`. Selecting Anthropic therefore replaces
the OpenAI model list with a Claude-only list. Saved conversations retain their
original model name, so the runtime rejects an existing `gpt-5.6-terra`
conversation when only Claude models are currently allowlisted. The history
index remains present, but opening the conversation returns HTTP 500.

Model configuration currently mixes three separate concepts:

1. models known to the application;
2. models usable with the credentials available now; and
3. models recorded in historical conversations.

The provider error classifier also treats Anthropic's low-credit
`BadRequestError` as generic `RUN_FAILED`, even though OpenAI credit exhaustion
already has a specific public error.

## Goals

- Derive model availability from verified OpenAI and Anthropic credentials.
- Show every model from each successfully verified configured provider,
  including models registered for a compatible endpoint.
- Keep all registered model profiles available for historical deserialization.
- Let users read historical conversations whose original provider is no longer
  available.
- Let users explicitly continue such conversations with an available model.
- Keep GPT-5.6 Terra as the default when OpenAI is available.
- Make first-run and `--reconfigure` provider setup direct and recoverable.
- Remove model-selection variables from normal `.env` configuration.
- Report Anthropic exhausted-credit failures clearly.

## Non-goals

- Do not silently convert a historical conversation to another model.
- Do not migrate or rewrite historical messages or checkpoints.
- Do not add remote authentication, AWS, Docker, or hosted secret storage.
- Do not make a billable model-generation request during every startup.
- Do not install or launch vLLM, Ray, or another model server. Compatible
  endpoint support is limited to loading an existing registration and checking
  that its externally managed endpoint is reachable.
- Do not require a real multi-GPU compatible deployment in this change's test
  suite; use a mock endpoint for connection and catalog behavior.

## Model sets

The application will distinguish three model sets.

### Registered models

Registered models are every model profile known to the application:

- built-in OpenAI GPT profiles; and
- built-in Anthropic Claude profiles; and
- profiles explicitly registered in `config/custom_models.json` for compatible
  endpoints.

This complete registry is the authority for deciding whether a model can be
executed. It is not filtered by current credentials. A historical record with
an unknown model ID is still readable, but that unknown model cannot execute.

### Available models

Available models are registered models whose provider can currently be used:

- GPT models are available when `OPENAI_API_KEY` is present and verified.
- Claude models are available when `ANTHROPIC_API_KEY` is present and verified.
- Registered compatible models are available when their endpoint responds to
  verification and any `api_key_env` named by the registration is present. A
  blank `api_key_env` explicitly denotes a keyless endpoint.

Every available model appears in the new-conversation model selector. Models
from a missing or failed provider are omitted rather than left selectable.

### Historical model

A saved conversation retains the model used for its prior turns. The runtime
must load its history even when that model is not currently available or is no
longer registered. Provider and registration availability are checked only
when the user tries to run another turn.

## Default model selection

Default selection is deterministic and is not stored in `.env`:

1. If OpenAI is available, use `gpt-5.6-terra`.
2. Otherwise, if Anthropic is available, use `claude-opus-5`.
3. Otherwise, use the first available registered compatible model.
4. If no model is available, enter provider setup instead of starting the
   application.

The user can select any other available model before the first turn of a new
conversation.

## Native setup sequence

Startup runs in this order:

1. Validate Python 3.12.
2. Load shared application defaults, `.env`, and inherited shell variables.
3. Resolve the study-package root.
4. Resolve the runtime root.
5. Derive the checkpoint database path from the runtime root.
6. Discover and verify OpenAI and Anthropic credentials and any registered
   compatible endpoints.
7. Calculate available models and the default model.
8. Validate the built frontend and start FastAPI.

### Study root

`study_installer.py` owns study-root selection. Without `--study-root`, it
uses an existing `REPORT_AGENT_STUDY_ROOT` or prompts for a folder and persists
the selection. Passing `--study-root` applies only to that invocation.

`run_fastapi.py` uses the saved study root or the project-local `study_data`
default. No installed package is a supported degraded state: startup succeeds
but reports that study-backed capabilities are unavailable.

### Runtime root and checkpoints

`run_fastapi.py` owns runtime-root selection. It persists only
`REPORT_AGENT_RUNTIME_ROOT`. Before accepting a selected folder, setup reports
whether its checkpoint database contains saved conversations.

The normal checkpoint path is always:

```text
<REPORT_AGENT_RUNTIME_ROOT>/agent_memory_fastapi.db
```

`REPORT_AGENT_CHECKPOINT_DB_PATH` remains an internal/test override but is not
prompted for or written by native setup. Removing this redundant persisted
value prevents a runtime-root change from continuing to reference an old
database unexpectedly.

### Provider setup with no credentials

If no provider is configured, startup displays:

```text
No AI provider is configured.

1. Configure OpenAI
2. Configure Anthropic
3. Configure both
4. Connect to a compatible endpoint
```

Keys are entered through a non-echoing prompt. A key is persisted only after
successful verification. A key inherited from the process environment is used
without copying it into `.env`.

If verification fails, setup identifies the failure without echoing the key
and offers three actions:

```text
Anthropic API key validation failed:
The key was rejected by Anthropic.

1. Try another key
2. Choose a different provider
3. Exit setup
```

Network failures use the same actions with a network-specific message. When
configuring both providers, one successful provider is retained if the other
fails, and the user may retry the failure or continue with the working
provider.

The compatible-endpoint option does not install a serving stack. It directs
the user to prepare `config/custom_models.json`, then verifies the registered
endpoint and required key, if any. If no registration exists, setup explains
how to copy and edit the example and returns to the provider menu. A failed
endpoint is not persisted as available and its models do not appear in the
selector.

### Reconfiguration

`python run_fastapi.py --reconfigure` displays provider status without exposing
keys and permits adding, replacing, or removing a provider. A replacement key
does not overwrite a working key until verification succeeds. Removing a key
removes that provider's models from new-conversation selection but does not
remove or rewrite historical conversations.

For inherited shell credentials, setup reports that the key is supplied by the
process environment and cannot be removed by editing `.env`.

Compatible endpoint registrations remain file-managed in this initial scope.
Reconfiguration rechecks them but does not provide an interactive endpoint
editor.

## Local environment contents

Normal `.env` configuration stores local paths, verified secrets, and unrelated
optional integrations. It must not store model-selection policy.

Allowed examples:

```env
REPORT_AGENT_STUDY_ROOT=/chosen/study_data
REPORT_AGENT_RUNTIME_ROOT=/chosen/runtime
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
```

Native setup stops reading or writing these model-selection variables:

```text
REPORT_AGENT_MODEL
REPORT_AGENT_ALLOWED_MODELS
OPENAI_MODEL
REPORT_AGENT_TITLE_MODEL
```

The migration removes those deprecated assignments and the normally redundant
`REPORT_AGENT_CHECKPOINT_DB_PATH` from `.env` while preserving comments, API
keys, roots, and unrelated settings. Atomic writes retain mode `0600`.

The active model defaults in `config/app.env` are removed as well. Defaults and
registered built-in models live in the model-profile code, providing one source
of truth.

## Runtime and API behavior

The runtime receives both the registered-model catalog and the available-model
catalog:

- registered models validate executable settings while unknown historical IDs
  remain readable as unavailable legacy metadata;
- available models validate new selections and execution; and
- the API exposes only available model descriptors for ordinary selection.

The API state for a historical conversation also reports whether its stored
model is currently available. This status is metadata, not an error.

The backend remains the authority for provider availability. The frontend does
not inspect environment variables or infer availability from model names.

### Persisted settings versus executable settings

The current failure occurs because `_require_owned_thread()` calls
`_normalize_settings()`, which rejects any model absent from the currently
available catalog. That check is valid for a new run but invalid for reading a
checkpoint.

The implementation must replace this shared path with two explicit operations:

- persisted-settings normalization validates the stored shape, numeric ranges,
  and other provider-independent values, but does not require the stored model
  to be currently available; and
- executable-settings normalization first performs persisted normalization and
  then requires the chosen model to be in the available catalog.

`_require_owned_thread()` and read-only state/review projections use persisted
normalization. New-conversation selection, model replacement, and run startup
use executable normalization. This distinction should be expressed with
separate named functions rather than a permissive boolean flag.

For a registered but unavailable model, persisted normalization may use its
registered profile for defaults. For an unknown historical model ID, it keeps
the ID as unavailable legacy metadata and uses provider-independent safe
defaults; it must not call `model_runtime_profile()` in a way that prevents the
conversation from loading.

Conversation state reports the stored model ID and an explicit availability
status. Opening state must not build a provider-bound graph. If the user tries
to submit while the stored model is unavailable, the API returns a structured
409 or 422 response requiring an available replacement, never HTTP 500.

## Continuing a historical conversation

When the historical model is available, the conversation remains locked to
that model and continues normally.

When it is unavailable:

1. The full conversation opens in read-only form.
2. The UI states which model was previously used and why it is unavailable.
3. The user chooses one of the currently available models.
4. The UI requests explicit confirmation, for example:

   ```text
   This conversation used GPT-5.6 Terra. Continue with Claude Opus 5?
   ```

5. After confirmation, the chosen model becomes the model for subsequent turns.
6. Existing messages and artifacts remain unchanged.

No provider change occurs solely because a conversation was opened. Unknown or
deferred model IDs follow the same read-only behavior.

## Provider and capability behavior

An Anthropic-only configuration supports chat without an OpenAI key. DB-RAG
semantic search remains unavailable because its query embeddings use OpenAI;
the capability response and UI must state this clearly rather than blocking
startup.

Title generation uses the provider-specific lightweight registered model when
available: GPT-5.6 Luna for OpenAI and Claude Haiku 4.5 for Anthropic. A
title-generation failure remains non-fatal.

## Minimal compatible endpoint support

Compatible endpoints, including externally managed vLLM and Ray Serve LLM
deployments, remain a setup option and their successfully verified registered
models appear in the selector. The main application does not install or launch
vLLM or Ray.

Cluster operators install the serving stack, allocate GPUs, load the model, and
expose one HTTP ingress. Epi-AI-Agent only reads the registration, supplies the
configured key when required, verifies the endpoint on every startup, and adds
the endpoint's registered models to the available catalog after verification.
Failure produces a warning with retry, choose-another-provider, or continue
options; continuing omits those models for that process.

The existing `http://127.0.0.1:8001/v1` example means that the model ingress is
reachable on the same machine as Epi-AI-Agent, including through an explicit
local tunnel. It is not a valid address for a remote cluster unless such a
tunnel exists. A remote deployment instead needs an address reachable from the
application host, such as a private cluster DNS name or secured gateway URL.

## Error handling

Provider errors must remain specific and safe:

- invalid key: `PROVIDER_AUTHENTICATION_FAILED`;
- insufficient permission: `PROVIDER_ACCESS_DENIED`;
- exhausted credits: `PROVIDER_CREDITS_EXHAUSTED`;
- request throttling: `PROVIDER_RATE_LIMITED`;
- unavailable model: `PROVIDER_MODEL_UNAVAILABLE`;
- connection failure: `PROVIDER_CONNECTION_FAILED`;
- request timeout: `MODEL_REQUEST_TIMEOUT`; and
- context overflow: `PROVIDER_CONTEXT_LIMIT_EXCEEDED`.

Anthropic's HTTP 400 response containing “credit balance is too low” is an
exhausted-credit response, not generic `RUN_FAILED`. The public message tells
the user to add credits or use a funded key without exposing provider response
details or credentials.

Ordinary credential verification proves authentication and connectivity; it
does not issue a billable generation on every startup. Credit exhaustion may
therefore first appear during a run and must be classified correctly there.

## Security and failure guarantees

- Keys never appear in prompts after entry, logs, API responses, or frontend
  state.
- Failed keys are never persisted.
- Failed replacement keys never erase a working key.
- A partially successful multi-provider setup can continue with the verified
  provider.
- Configuration writes are atomic and preserve existing unrelated values.
- A provider failure cannot delete or rewrite conversation checkpoints.
- Historical-model incompatibility must never produce HTTP 500.

## Verification

Automated coverage must include:

- no credentials enters setup and cannot start without one usable model;
- OpenAI only exposes GPT models and defaults to GPT-5.6 Terra;
- Anthropic only exposes Claude models and defaults to Claude Opus 5;
- both keys expose both model families and default to GPT-5.6 Terra;
- every successfully verified configured provider contributes all of its
  registered models to ordinary model selection;
- compatible endpoint success and failure are tested with a mock server, while
  no vLLM or Ray installation is required;
- invalid, cancelled, and failed replacement credentials are not persisted;
- partial success while configuring both providers can continue;
- shell credentials are not copied into `.env`;
- deprecated model variables and the persisted normal checkpoint override are
  removed without damaging other `.env` values;
- historical GPT conversations load under Anthropic-only availability;
- historical Claude conversations load under OpenAI-only availability;
- historical conversations can explicitly continue with an available model;
- unknown historical custom-model IDs remain readable and require explicit
  replacement before execution;
- opening history never silently changes its model;
- conversation listing, review-status projection, and state retrieval do not
  apply the executable-model allowlist and do not emit projection failures;
- Anthropic low-credit HTTP 400 maps to `PROVIDER_CREDITS_EXHAUSTED`;
- provider keys are absent from serialized responses and diagnostic output;
- full backend and frontend suites pass; and
- a real compiled-browser smoke covers provider-derived selection and
  historical-conversation recovery.

## Acceptance criteria

The design is complete when a fresh user can configure OpenAI, Anthropic, both,
or an existing compatible endpoint without model variables in `.env`, sees all
and only models from successfully verified configured providers, can still open
every prior conversation after provider changes, can explicitly continue
history with an available model, and receives an actionable Anthropic credit
error instead of `RUN_FAILED`.
