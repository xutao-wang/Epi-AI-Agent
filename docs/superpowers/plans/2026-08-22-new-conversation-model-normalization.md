# New-Conversation Model Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee that a new conversation displays and submits an available model after any saved-conversation or provider transition.

**Architecture:** Add one pure provider-neutral normalization helper in `App.tsx`. Use it both while clearing a conversation and immediately before `createThread`, preserving valid model selections and replacing invalid settings with the complete current runtime defaults. Keep backend rejection of unsupported models unchanged.

**Tech Stack:** React 19, TypeScript 5.8, Vitest, Testing Library, Vite.

## Global Constraints

- Never hardcode a provider or model identifier in production normalization logic.
- Preserve complete current runtime settings when their model remains available.
- Replace the complete settings object with `runtimeOptions.defaults` when its model is unavailable.
- Normalize at both the “New conversation” transition and immediately before `POST /api/threads`.
- Do not migrate saved records or silently change an existing thread’s model.
- Keep explicit saved-conversation model replacement unchanged.

---

### Task 1: Enforce the New-Thread Model Invariant

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Regenerate: `frontend/dist/index.html`
- Regenerate: `frontend/dist/assets/index-*.js`

**Interfaces:**
- Produces: `normalizeNewConversationRuntimeSettings(current: RuntimeSettings, options: RuntimeOptions): RuntimeSettings`.
- Consumes: current frontend runtime settings and server-supplied runtime options.
- Guarantees: the model passed to `apiClient.createThread` belongs to `runtimeOptions.models`.

- [ ] **Step 1: Add the failing unavailable-history transition test**

Add a test named `normalizes an unavailable historical model before creating a new conversation` in `frontend/src/App.test.tsx`. Construct Claude-only runtime options whose defaults use `claude-opus-5`, open a saved state whose locked historical model is `gpt-5.6-terra`, click `New conversation`, enter `New Claude request`, and submit. Capture the thread-creation body and assert:

```typescript
expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue(
  "claude-opus-5",
);
expect(createThreadBody).toBe(JSON.stringify({
  model_name: "claude-opus-5",
}));
```

The mock returns the historical state, a successful open response, `thread-new` from `POST /api/threads`, and a completed state from `POST /api/threads/thread-new/messages`.

- [ ] **Step 2: Run the regression test and verify RED**

Run from `frontend/`:

```bash
npm test -- App.test.tsx -t "normalizes an unavailable historical model before creating a new conversation"
```

Expected: FAIL because the picker’s controlled value and the thread-creation request retain `gpt-5.6-terra`.

- [ ] **Step 3: Add focused pure-helper coverage**

Import the helper from `App.tsx`, then add assertions that describe both branches before implementing it:

```typescript
expect(
  normalizeNewConversationRuntimeSettings(validSettings, options),
).toBe(validSettings);
expect(
  normalizeNewConversationRuntimeSettings(staleSettings, options),
).toBe(options.defaults);
```

Use a custom-endpoint model ID for `validSettings` and a removed historical model ID for `staleSettings` to prove the helper is provider-neutral.

- [ ] **Step 4: Run focused tests and verify RED remains attributable to the missing helper**

Run from `frontend/`:

```bash
npm test -- App.test.tsx -t "normalizes"
```

Expected: FAIL because `normalizeNewConversationRuntimeSettings` is not exported yet.

- [ ] **Step 5: Implement the pure normalization helper**

Add this helper beside the existing frontend utility functions:

```typescript
export function normalizeNewConversationRuntimeSettings(
  current: RuntimeSettings,
  options: RuntimeOptions,
): RuntimeSettings {
  return options.models.some((model) => model.id === current.model_name)
    ? current
    : options.defaults;
}
```

- [ ] **Step 6: Normalize during both transition and request creation**

In `newConversation()`, normalize through a functional state update whenever runtime options exist:

```typescript
if (runtimeOptions) {
  setSelectedRuntimeSettings((current) =>
    normalizeNewConversationRuntimeSettings(current, runtimeOptions),
  );
}
```

In `ensureThread()`, normalize immediately before creating the promise, update displayed state when normalization changed the reference, and submit only the normalized model:

```typescript
const executableSettings = normalizeNewConversationRuntimeSettings(
  selectedRuntimeSettings,
  runtimeOptions,
);
if (executableSettings !== selectedRuntimeSettings) {
  setSelectedRuntimeSettings(executableSettings);
}
createThreadPromiseRef.current ??= apiClient
  .createThread(executableSettings.model_name)
  .then((response) => response.thread_id)
  .catch((createError: unknown) => {
    createThreadPromiseRef.current = null;
    throw createError;
  });
```

- [ ] **Step 7: Run focused tests and verify GREEN**

Run from `frontend/`:

```bash
npm test -- App.test.tsx -t "normalizes|starts a second independent conversation"
```

Expected: the unavailable-model regression and existing available-model preservation test pass.

- [ ] **Step 8: Run complete frontend verification and rebuild production assets**

Run from `frontend/`:

```bash
npm test
npm run build
```

Expected: all Vitest tests pass; TypeScript and Vite build successfully; generated `frontend/dist` assets reflect the source change.

- [ ] **Step 9: Run repository verification**

Run from the repository root:

```bash
.venv/bin/python -m pytest -q
git diff --check
```

Expected: all Python tests pass and the diff contains no whitespace errors.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/App.tsx frontend/src/App.test.tsx frontend/dist
git commit -m "fix: normalize models for new conversations"
```
