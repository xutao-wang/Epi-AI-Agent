# High-Confidence UI Cleanup Design

## Goal

Remove UI source and stylesheet rules that are demonstrably unreachable from
the current React application while preserving every active UI path and
dynamically generated selector family.

## Scope

### Orphaned component

Delete `frontend/src/RuntimeSettingsPanel.tsx` and
`frontend/src/RuntimeSettingsPanel.test.tsx`. Production code does not import
the component, its visible controls were replaced by the composer model
picker, and its text is absent from the production JavaScript bundle.

Remove the component-only stylesheet rules for `.settings-list`,
`.runtime-settings-panel`, `.runtime-checkbox`, and `.runtime-settings-lock`.
Keep `.settings-panel` because the current conversation sidebar still renders
that wrapper.

### Confirmed dead stylesheet families

Remove only selector families that have no production markup or dynamic class
construction:

- `.concept-display-*`
- `.debug-*`
- `.app-shell`, `.app-header`, and `.app-content`
- `.run-status` and `.empty-conversation`
- the dead `.upload-section` and `.artifact-section` members from the shared
  active layout rule
- retired dataset-review fragments:
  `.db-rag-dataset-review-subsection`, `.db-rag-dataset-review-list`,
  `.db-rag-dataset-review-schema`, `.db-rag-dataset-review-error`, and
  `.db-rag-dataset-review-provenance`
- retired clarification/code-review fragments:
  `.code-review-subsection`, `.code-review-feedback`, `.code-review-error`, and
  `.code-review-figure`
- retired analysis-result fragments:
  `.analysis-result-output`, `.analysis-result-runtime`,
  `.analysis-result-figures`, and `.analysis-result-error`
- the complete retired `.db-rag-sql-*` review family
- retired dataset-plan fragments not rendered by `DbRagReview`:
  `.db-rag-review-progress`, `.db-rag-review-section`,
  `.db-rag-column-filters`, `.db-rag-filter-label`, `.db-rag-no-filter`,
  `.db-rag-all-concepts`, `.db-rag-data-linkage`,
  `.db-rag-linkage-provenance`, `.db-rag-linkage-fields`,
  `.db-rag-linkage-relationships`, `.db-rag-linkage-field`,
  `.db-rag-linkage-keys`, `.db-rag-linkage-source`,
  `.db-rag-linkage-requirement`, `.db-rag-linkage-warning`,
  `.db-rag-empty-note`, `.db-rag-skip-hint`, `.db-rag-feedback-history`,
  `.db-rag-revision-decision`, and `.db-rag-decision-actions`.

When a dead selector shares a declaration with an active selector, remove only
the dead selector rather than the whole rule.

## Protected UI

Keep every production component reachable from `main.tsx`, including all five
backend-supported interrupt panels. Preserve selectors generated from runtime
values, including:

- `.message-user`, `.message-assistant`, `.message-system`, and cancellation
  modifiers
- `.agent-activity--*`, `.agent-activity-item--*`, and
  `.agent-activity-summary-indicator--*`
- `.db-rag-quality-warning-*`
- syntax and language modifiers emitted by `CodeBlock`

Preserve the active `.db-rag-linkage-section` and
`.db-rag-concept-card h3` styles introduced by the preceding migration.

## Testing

Extend the stylesheet contract test to assert that protected active and
dynamic selectors remain and representative retired selectors are absent.
Follow test-driven deletion: add the absence assertions, observe them fail,
then remove the dead code and CSS until they pass.

Run the complete frontend test suite, TypeScript/Vite production build, build
manifest refresh, and a dedicated real compiled-UI smoke. The real smoke must
run at most once unless the user explicitly authorizes a rerun after an
environmental failure.

## Delivery

Commit the cleanup separately on the existing
`active-db-rag-style-migration` worktree branch. Do not merge, push, remove the
worktree, or clean up the branch without a subsequent user choice.

## Non-goals

- Removing unused API-client helpers or backend endpoints.
- Updating ignored legacy local smoke scripts unrelated to this cleanup.
- Renaming internal RePORT or Streamlit terminology.
- Redesigning active UI components or changing behavior.
- Removing selectors based solely on automated static-reference output.
