// @ts-expect-error Vitest resolves Node built-ins; the browser build excludes tests.
import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const stylesheet = readFileSync("src/styles.css", "utf8");

const retiredSelectors = [
  ".concept-display-panel",
  ".concept-display-toggle",
  ".debug-toggle",
  ".debug-panel",
  ".app-shell",
  ".app-header",
  ".app-content",
  ".run-status",
  ".empty-conversation",
  ".upload-section",
  ".artifact-section",
  ".db-rag-dataset-review-subsection",
  ".db-rag-dataset-review-list",
  ".db-rag-dataset-review-schema",
  ".db-rag-dataset-review-error",
  ".db-rag-dataset-review-provenance",
  ".code-review-subsection",
  ".code-review-feedback",
  ".code-review-error",
  ".code-review-figure",
  ".analysis-result-output",
  ".analysis-result-runtime",
  ".analysis-result-figures",
  ".analysis-result-error",
  ".db-rag-sql-",
  ".db-rag-review-progress",
  ".db-rag-review-section",
  ".db-rag-column-filters",
  ".db-rag-filter-label",
  ".db-rag-no-filter",
  ".db-rag-all-concepts",
  ".db-rag-data-linkage",
  ".db-rag-linkage-provenance",
  ".db-rag-linkage-fields",
  ".db-rag-linkage-relationships",
  ".db-rag-linkage-field",
  ".db-rag-linkage-keys",
  ".db-rag-linkage-source",
  ".db-rag-linkage-requirement",
  ".db-rag-linkage-warning",
  ".db-rag-empty-note",
  ".db-rag-skip-hint",
  ".db-rag-feedback-history",
  ".db-rag-revision-decision",
  ".db-rag-decision-actions",
];

const protectedSelectors = [
  ".settings-panel",
  ".db-rag-linkage-section",
  ".db-rag-concept-card h3",
  ".message-user",
  ".message-system",
  ".agent-activity--completed",
  ".agent-activity-item--running",
  ".agent-activity-summary-indicator--waiting",
  ".db-rag-quality-warning-high",
  ".syntax-keyword",
];

describe("active DB-RAG review styles", () => {
  it("styles the linkage section emitted by DbRagReview", () => {
    expect(stylesheet).toContain(".db-rag-linkage-section {");
    expect(stylesheet).toContain(".db-rag-linkage-section h3 {");
  });

  it("styles the concept-card heading level emitted by DbRagReview", () => {
    expect(stylesheet).toContain(".db-rag-concept-card h3 {");
    expect(stylesheet).not.toContain(".db-rag-concept-card h2 {");
  });
});

describe("orphaned UI removal", () => {
  it("does not ship the retired runtime-settings component", () => {
    expect(existsSync("src/RuntimeSettingsPanel.tsx")).toBe(false);
    expect(existsSync("src/RuntimeSettingsPanel.test.tsx")).toBe(false);
    expect(stylesheet).not.toContain(".runtime-settings-panel");
    expect(stylesheet).not.toContain(".runtime-settings-lock");
    expect(stylesheet).not.toContain(".runtime-checkbox");
    expect(stylesheet).not.toContain(".settings-list");
  });

  it("keeps the active conversation-sidebar wrapper", () => {
    expect(stylesheet).toContain(".settings-panel {");
  });
});

describe("retired stylesheet cleanup", () => {
  it.each(retiredSelectors)("removes %s", (selector) => {
    expect(stylesheet).not.toContain(selector);
  });

  it.each(protectedSelectors)("preserves %s", (selector) => {
    expect(stylesheet).toContain(selector);
  });
});
