# Embedding Profile Routing and Startup Status

## Goal

Replace the hard-coded single embedding route with a validated registry of
non-secret embedding profiles. Select one application-wide profile, probe its
real connection once at startup, latch the retrieval mode for the process
session, and show every new thread one informative, non-repeating status card.

## Scope

This design owns embedding configuration, route construction, startup probing,
runtime/API status, and the thread status card. It consumes the shared hybrid
and lexical modes but does not define provider ranking behavior.

The first release supports the existing OpenAI embedding transport and one
built-in profile. The registry and schema allow additional profiles later, but
do not claim support for a transport until its adapter is registered in code.

## Profile Registry

Tracked non-secret profiles live in `config/embedding_models.json`, with a
safe example in `config/embedding_models.example.json` if local overrides are
needed. Credentials remain exclusively in environment variables and are never
serialized to API responses, checkpoints, logs, artifacts, or diagnostics.

The initial profile is equivalent to:

```json
{
  "id": "openai-text-embedding-3-large",
  "label": "OpenAI text-embedding-3-large",
  "provider": "openai",
  "transport": "openai_embeddings",
  "model": "text-embedding-3-large",
  "index_compatibility": "OpenAI/text-embedding-3-large",
  "dimensions": 3072,
  "base_url": "https://api.openai.com/v1",
  "api_key_env": "OPENAI_API_KEY",
  "timeout_seconds": 10,
  "enabled": true
}
```

Profile IDs, labels, provider identifiers, model names, URLs, credential
variable names, positive dimensions, timeouts, and enabled flags are validated
strictly. `transport` selects a code-owned adapter factory; configuration cannot
name or import arbitrary Python objects.

## Selection and Compatibility

`DB_RAG_EMBEDDING_PROFILE` selects exactly one enabled application-wide
profile. During migration, the existing `DB_RAG_EMBEDDING_MODEL` may resolve to
the unique profile with the matching `index_compatibility` value. Ambiguous,
unknown, malformed, or disabled selections do not abort startup; they latch
lexical fallback with a sanitized configuration reason.

Chat-model selection remains independent from embedding-profile selection.
Every installed study package retains its declared embedding-model identity.
A successful provider connection enables semantic retrieval only for studies
whose packaged index identity matches the selected profile's
`index_compatibility`; incompatible studies remain lexically searchable.

## One-Time Startup Probe

Application construction resolves the selected profile and performs one real,
bounded embedding request through its registered transport. The probe uses a
fixed non-sensitive input and validates that the response contains exactly one
finite numeric vector with the configured dimension.

The probe result is immutable for the process session. Search tools do not
re-probe availability and do not independently switch the session back to
semantic retrieval. A missing credential, unavailable adapter, rejected
request, timeout, malformed response, or dimension mismatch latches lexical
fallback. Raw provider responses and exception text remain in protected logs;
public status uses stable reason codes and sanitized messages.

After a successful startup probe, ordinary search requests still call the
provider to embed their real queries. A later request failure returns lexical
results for that request without issuing a new health probe or changing the
latched startup status. The thread card continues to describe startup routing;
tool artifacts retain request-specific fallback metadata for auditability.

## Runtime and Thread Status

Runtime options expose one safe embedding status object containing profile ID,
label, provider, index-compatibility identity, availability, retrieval mode,
reason code when unavailable, and a bounded public message. They never expose
the API key or its value.

The immutable application-session status is projected into every thread state,
including immediately after thread creation. It is a dedicated typed field,
not diagnostics, a checkpoint value, or a conversation message. The frontend
renders one accessible card near the conversation and review content:

- hybrid: semantic and lexical evidence search are available through the named
  embedding profile;
- fallback: the named embedding profile is unavailable for the sanitized
  reason, so evidence search automatically uses lexical matching only.

The card appears immediately, survives refresh and reopening during the same
application session, and never repeats when search tools run. Historical
threads receive the current application's latched startup status at projection
time without checkpoint migration. Restarting the application performs a new
probe and replaces the session status rather than preserving a stale result in
thread history.

## Failure and Startup Behavior

Embedding failure never prevents the application from starting. Invalid local
profile syntax is logged and represented as lexical fallback when a safe
status can be constructed. Study data corruption, invalid vector provenance,
and unrelated application configuration errors retain their existing failure
boundaries.

## Verification

Tests will cover profile parsing, selection, migration from the model setting,
transport registration, secret exclusion, successful and failed real-probe
contracts, dimension validation, session latching, study-index compatibility,
thread-state projection, historical-thread projection, and one-card frontend
rendering without repetition.

A dedicated real smoke will launch the production FastAPI backend and compiled
TypeScript frontend, perform the real startup probe once, create a thread
through browser controls, and assert the rendered card plus raw runtime and
thread state. It will run once with a five-minute maximum and preserve logs,
state, and screenshots on failure.
