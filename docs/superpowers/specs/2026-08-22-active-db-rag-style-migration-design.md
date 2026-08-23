# Active DB-RAG Style Migration Design

## Goal

Restore the intended layout styling for the active dataset-plan review markup
without removing obsolete DB-RAG selectors in the same change.

## Scope

- Give `.db-rag-linkage-section`, which `DbRagReview` currently renders, the
  spacing, divider, and heading treatment previously associated with
  `.db-rag-data-linkage`.
- Make the concept-card heading rule target the rendered `h3` rather than the
  retired `h2` markup.
- Add a focused stylesheet contract test covering both active selectors.
- Rebuild the tracked frontend production bundle and refresh its build
  manifest as required by the project instructions.

This change will not delete the old DB-RAG selectors. That cleanup remains a
separate follow-up after the migrated styles are verified.

## Approach

Use transitional selector aliases for linkage-section rules. The current and
retired class names will temporarily share declarations, avoiding duplicated
style blocks while preserving a simple later cleanup: remove the retired
selector from each alias.

Change the concept-card selector directly from `h2` to `h3`, because only the
active heading level needs the rule and the old heading markup is not rendered.

## Verification

1. Add a test that reads the real stylesheet and requires the active linkage
   and concept-heading selectors.
2. Observe that test fail before changing the stylesheet.
3. Apply the minimal CSS migration and observe the focused test pass.
4. Run the complete frontend test suite and TypeScript build.
5. Rebuild the production bundle and refresh the delivery build manifest.

## Non-goals

- Removing `RuntimeSettingsPanel` or other stale UI files.
- Deleting obsolete DB-RAG, SQL-review, debug, or legacy-shell CSS.
- Changing DB-RAG review behavior, payloads, markup, or backend schemas.
