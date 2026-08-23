// @ts-expect-error Vitest resolves Node built-ins; the browser build excludes tests.
import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const stylesheet = readFileSync("src/styles.css", "utf8");

describe("active DB-RAG review styles", () => {
  it("styles the linkage section emitted by DbRagReview", () => {
    expect(stylesheet).toMatch(
      /\.db-rag-data-linkage,\s*\.db-rag-linkage-section\s*\{/,
    );
    expect(stylesheet).toMatch(
      /\.db-rag-data-linkage h3,\s*\.db-rag-data-linkage h4,\s*\.db-rag-linkage-section h3\s*\{/,
    );
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
