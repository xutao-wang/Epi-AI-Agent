# Embedding Profile Routing and Startup Status

## Goal

Replace the hard-coded single embedding route with a validated registry of
non-secret embedding profiles. Select one application-wide profile, probe its
real connection exactly once when the application starts, and latch the result
for that application process. All embedding failures degrade to lexical search
without preventing the application or agent from running. The frontend shows
one informative notice only when fallback applies; successful startup is
silent.

## Scope

This design owns embedding configuration, route construction, startup probing,
study-index compatibility, runtime/API status, and the fallback notice. It
consumes the existing hybrid and lexical retrieval modes but does not redefine
provider ranking or evidence fusion.

The first release supports the existing OpenAI embedding transport and one
profile. The registry shape supports future profiles such as Qwen, but a
profile is usable only after its code-owned transport adapter is registered and
compatible study indexes have been built. Profile selection is deployment
configuration, not a user-facing web setting.

## Profile Registry

Tracked non-secret configuration lives in `config/embedding_models.json`.
Credentials remain exclusively in environment variables and are never
serialized to API responses, checkpoints, logs, artifacts, diagnostics, or
frontend state.

The initial registry is equivalent to:

```json
{
  "default_profile": "openai-text-embedding-3-large",
  "profiles": [
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
  ]
}
```

The registry validates its default profile, unique profile IDs, labels,
provider identifiers, model names, HTTP(S) base URLs, credential environment
variable names, positive dimensions, bounded positive timeouts, enabled flags,
and unique index-compatibility identities. `transport` selects an adapter from
a code-owned allow-list; configuration cannot name, import, or execute an
arbitrary Python object.

Adding a future model requires three administrative actions: add its registry
card, register its approved transport adapter, and build study indexes that
declare the card's `index_compatibility` identity. Retrieval tools remain
provider-neutral.

## Profile Selection

The application resolves one profile at startup using this precedence:

1. If `DB_RAG_EMBEDDING_PROFILE` is nonempty, resolve that exact profile ID.
2. Otherwise, during migration, if `DB_RAG_EMBEDDING_MODEL` is nonempty,
   resolve the unique profile whose `index_compatibility` matches it.
3. Otherwise, resolve the registry's explicit `default_profile`.

An explicit unknown, disabled, or malformed selection never silently switches
to another profile. It produces a lexical-fallback route with a safe
configuration reason. When no valid card can supply a label, public status uses
the generic label "Configured embedding profile" rather than exposing raw
configuration. A valid selected card names the credential environment
variable; for the initial profile, the application reads `OPENAI_API_KEY`.

Embedding-profile selection is independent of chat-model selection. The web UI
does not expose an embedding-profile chooser because users cannot rebuild or
replace the packaged vector indexes that constrain compatibility.

## Study-Index Compatibility

Every installed study package retains its declared embedding-index identity.
A successful startup probe enables semantic retrieval only for studies whose
identity exactly matches the selected card's `index_compatibility`. An
incompatible study receives a lexical-only bound provider and remains fully
searchable.

The application status records compatible and incompatible installed study
IDs. If all studies are affected by a global profile failure, the public notice
describes global lexical fallback. If only some studies are incompatible, the
notice names those studies and does not claim that compatible studies are
lexical-only.

## One-Time Startup Probe

Application construction resolves the selected profile and, when its
configuration and credential are available, sends one real bounded embedding
request through the registered transport. The request uses a fixed,
non-sensitive input. A successful response contains exactly one finite numeric
vector with the card's declared dimensions.

The resulting route and `EmbeddingStartupStatus` are immutable for the
application process. Thread creation, thread loading, refresh, retrieval tools,
and review actions never run another health probe. Restarting the application
creates a fresh probe and replaces the prior process status.

Ordinary hybrid searches still embed their real queries. A later query request
failure returns lexical results for that request without issuing a health probe
or changing the startup latch. Tool artifacts retain request-specific fallback
metadata for auditability.

## Soft-Failure Contract

No embedding-related failure may prevent FastAPI startup, agent construction,
thread creation, or evidence search. The resolver always returns a usable
latched status and route. Catalog, publication, and study-design providers use
lexical search when any of these conditions applies:

- registry syntax, default, or profile selection is invalid;
- the selected profile is unknown or disabled;
- its credential is missing;
- its transport adapter is not registered;
- its provider rejects, times out, or cannot complete the probe;
- the probe response has the wrong count, type, finiteness, or dimensions;
- a study index is incompatible with the selected profile; or
- a later real-query embedding request fails.

Expected embedding failures may be recorded in protected backend logs, but raw
provider responses and exception text never cross the public boundary. Public
status uses stable reason codes and fixed sanitized explanations such as:

- `EMBEDDING_PROFILE_INVALID`;
- `EMBEDDING_PROFILE_UNKNOWN`;
- `EMBEDDING_PROFILE_DISABLED`;
- `EMBEDDING_CREDENTIALS_MISSING`;
- `EMBEDDING_TRANSPORT_UNAVAILABLE`;
- `EMBEDDING_PROVIDER_UNAVAILABLE`;
- `EMBEDDING_PROBE_TIMEOUT`;
- `EMBEDDING_RESPONSE_INVALID`;
- `EMBEDDING_DIMENSION_MISMATCH`; and
- `EMBEDDING_INDEX_INCOMPATIBLE`.

This soft-failure contract is limited to embedding availability and
compatibility. Corrupted authoritative study files, unsafe paths, unverifiable
evidence provenance, and unrelated application errors keep their existing
integrity boundaries; they are not mislabeled as embedding outages or silently
trusted. Failures isolated to one study do not disable other valid studies or
the rest of the agent.

## Runtime and Thread Status

Runtime options and `ApiThreadState` expose one safe typed
`EmbeddingStartupStatus` with the selected profile ID and label, provider,
index-compatibility identity, availability, retrieval mode, optional reason
code, bounded public message, and compatible/incompatible study IDs. They never
expose credentials or secret values.

`available` describes the application-wide transport probe. Its retrieval mode
is hybrid after a successful probe and lexical fallback otherwise. The study ID
lists describe per-study index compatibility without overloading that global
probe result; an incompatible study stays lexical even when `available` is
true.

The status is held by the application and projected into every new or
historical thread response. It is not stored in checkpoints, diagnostics, or
conversation messages. Historical threads therefore receive the current
process status without a checkpoint migration.

The frontend renders nothing when hybrid startup succeeds and every installed
study is compatible. When global fallback applies, it renders one accessible
notice near conversation and review content:

> Semantic embedding search is unavailable. (OpenAI text-embedding-3-large
> cannot be reached.) Catalog, publication, and study-design searches will use
> lexical matching only.

The profile label and safe cause are derived from the selected registry card;
they are not hard-coded to OpenAI. Missing credentials say the profile "is not
configured," provider failures say it "cannot be reached," invalid responses
describe an incompatible response, and index mismatches name the affected
study or studies.

For a study-specific mismatch, the notice is scoped accurately:

> Semantic embedding search is unavailable for RePORT India Synthetic.
> (OpenAI text-embedding-3-large is incompatible with this study's semantic
> index.) Searches for this study will use lexical matching only.

The notice is one component derived from current thread state. It appears
immediately, survives refresh and reopening in the same application process,
and cannot be appended, repeated, dismissed, or changed by tool calls.

## Verification

Automated tests cover:

- strict registry parsing, unique IDs and compatibility identities, and a valid
  enabled default;
- selection precedence, explicit-selection failure, and legacy-model migration;
- secret exclusion from serialization, public status, logs, and artifacts;
- transport allow-list enforcement;
- successful, missing-credential, timeout, rejection, malformed-vector,
  non-finite-vector, wrong-count, and dimension-mismatch probes;
- exactly one probe across multiple thread creations, refreshes, review actions,
  and retrieval calls;
- compatible, incompatible, and mixed multi-study installations;
- hybrid retrieval after a successful probe and request-specific lexical
  fallback after a later query failure;
- application and agent availability for every embedding failure mode;
- live status projection into new and historical threads without persistence;
- no frontend notice after success;
- one accessible profile-specific fallback notice after failure; and
- no duplicate notice after state updates or tool calls.

A dedicated real smoke runs once with a five-minute maximum and preserves
sanitized logs, state, and screenshots on failure. It launches the production
FastAPI backend and compiled TypeScript frontend, performs one real OpenAI
startup probe, creates a thread through browser controls, verifies hybrid
runtime/thread status, and verifies that no fallback notice is rendered. The
same smoke then launches a separate missing-credential application instance,
which performs no provider request, and verifies immediate lexical-fallback
status plus the single rendered notice. Each application instance follows the
one-probe startup contract.
