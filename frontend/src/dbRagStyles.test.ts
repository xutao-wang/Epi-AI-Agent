// @ts-expect-error Vitest resolves Node built-ins; the browser build excludes tests.
import { readFileSync } from "node:fs";
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
