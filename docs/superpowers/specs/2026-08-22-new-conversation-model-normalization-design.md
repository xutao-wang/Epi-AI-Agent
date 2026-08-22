# New-Conversation Model Normalization

## Goal

Starting a new conversation must never submit a stale or unavailable model.
The model picker, frontend state, and thread-creation request must agree even
after opening historical conversations created under a different provider
configuration.

## Observed Failure

The current runtime is Anthropic-only and defaults to `claude-opus-5`, while
saved conversations retain their historical `gpt-5.6-terra` setting. Opening
one of those conversations copies Terra into `selectedRuntimeSettings`.
`newConversation()` clears the thread state but does not normalize the selected
runtime settings. Because Terra is absent from the `<select>` options, the
browser visually displays Claude while React retains Terra and later submits it
to `POST /api/threads`.

The existing historical-model replacement flow correctly handles continuing
the saved conversation. It does not cover leaving that conversation and
starting a new one.

## Considered Approaches

1. **Normalize against current runtime options at every new-thread boundary
   (selected).** Preserve a selected model when it is still available;
   otherwise replace the entire runtime-settings object with the current
   defaults. Apply this both when “New conversation” is clicked and immediately
   before thread creation.
2. **Always reset to defaults.** This prevents stale models but unnecessarily
   discards an explicitly selected model that remains available.
3. **Let the backend silently replace unsupported models.** This avoids the
   error but can execute a request with a model the user did not choose and
   hides frontend/backend state divergence.

Approach 1 preserves valid user intent, remains provider-neutral, and adds a
second guard at the irreversible request boundary.

## State Invariant

Whenever no thread is selected and runtime options are available:

```text
selectedRuntimeSettings.model_name ∈ runtimeOptions.models[].id
```

If the current model satisfies the invariant, keep the complete current
settings. If it does not, use the complete `runtimeOptions.defaults` object so
model-dependent values such as workflow timeout also match the replacement
model.

No model or provider identifier is hardcoded. OpenAI-only, Anthropic-only,
custom-endpoint-only, and mixed-provider configurations follow the same rule.

## Component Design

Add a small pure frontend helper that receives current `RuntimeSettings` and
`RuntimeOptions`. It returns current settings when the model ID is available,
or the runtime defaults when it is not. Keeping this decision pure makes the
invariant independently testable and avoids duplicating membership checks.

`newConversation()` applies the helper while clearing thread-specific state.
This makes the picker and internal state agree immediately after the
transition.

`ensureThread()` applies the helper again before calling `createThread()`. This
is the request-boundary defense against asynchronous React updates, future
transition paths, or stale state introduced elsewhere. It updates displayed
settings if normalization was required and sends only the normalized model.
The backend continues rejecting unsupported models rather than silently
substituting one.

## Error Handling

If runtime options have not loaded, existing composer disabling remains in
effect and no thread is created. Runtime options are required to contain a
default model included in their available model list; backend construction
already enforces a non-empty verified model catalog. The frontend does not
invent a fallback when that server contract is violated.

Historical conversations retain their recorded model and existing explicit
replacement confirmation flow. This change affects only the transition to a
new, unlocked conversation.

## Tests

Frontend regression coverage will prove:

1. Open an unavailable Terra history thread under Claude-only runtime options,
   click “New conversation,” send a message, and assert thread creation uses
   `claude-opus-5`.
2. The model picker displays the same Claude model that is submitted.
3. When the prior selection is still available, “New conversation” preserves
   it rather than resetting to the default.
4. Request-boundary normalization prevents an unsupported model from reaching
   `createThread`, independent of the transition-time state update.

Existing saved-conversation replacement tests remain unchanged and must pass.
The full frontend and Python suites must pass before integration.

## Scope

This change does not migrate saved conversation records, change model-provider
availability, silently alter an existing thread’s model, or modify embedding
routing. It restores consistency only for new-thread model selection.
