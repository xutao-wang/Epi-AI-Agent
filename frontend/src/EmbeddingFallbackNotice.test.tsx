import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EmbeddingFallbackNotice } from "./EmbeddingFallbackNotice";
import type { EmbeddingStartupStatus } from "./types";


const hybridStatus: EmbeddingStartupStatus = {
  profile_id: "openai-large",
  profile_label: "OpenAI text-embedding-3-large",
  provider: "openai",
  index_compatibility: "OpenAI/text-embedding-3-large",
  available: true,
  retrieval_mode: "hybrid_vector_lexical",
  reason_code: null,
  message: "",
  compatible_study_ids: ["study-a"],
  incompatible_study_ids: [],
};


describe("EmbeddingFallbackNotice", () => {
  it("renders nothing when hybrid search is fully ready", () => {
    const { container } = render(
      <EmbeddingFallbackNotice status={hybridStatus} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("renders one non-dismissible safe fallback message", () => {
    const message =
      "Semantic embedding search is unavailable. " +
      "(OpenAI text-embedding-3-large is not configured.) Catalog, publication, " +
      "and study-design searches will use lexical matching only.";
    render(
      <EmbeddingFallbackNotice
        status={{
          ...hybridStatus,
          available: false,
          retrieval_mode: "lexical_fallback",
          reason_code: "EMBEDDING_CREDENTIALS_MISSING",
          message,
        }}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(message);
    expect(screen.getAllByText(message)).toHaveLength(1);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders a future profile and mixed-study message without hard-coding OpenAI", () => {
    const message =
      "Semantic embedding search is unavailable for Study B. " +
      "(Qwen 3 Embedding is incompatible with the semantic index for this study.) " +
      "Searches for this study will use lexical matching only.";
    render(
      <EmbeddingFallbackNotice
        status={{
          ...hybridStatus,
          profile_id: "qwen-3",
          profile_label: "Qwen 3 Embedding",
          provider: "qwen",
          available: true,
          reason_code: "EMBEDDING_INDEX_INCOMPATIBLE",
          message,
          compatible_study_ids: ["study-a"],
          incompatible_study_ids: ["study-b"],
        }}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Qwen 3 Embedding");
    expect(screen.queryByText(/OpenAI/)).not.toBeInTheDocument();
  });
});
