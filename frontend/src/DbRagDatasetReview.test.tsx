import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import DbRagDatasetReview from "./DbRagDatasetReview";
import type { ActiveInterrupt } from "./types";

type DatasetReviewInterrupt = Extract<
  ActiveInterrupt,
  { type: "dataset_review" }
>;

function interrupt(datasetId = "subset-1"): DatasetReviewInterrupt {
  return {
    id: `interrupt-${datasetId}`,
    type: "dataset_review",
    artifact: {
      id: datasetId,
      kind: "db_rag_result",
      version: 1,
      expected_status: "pending_review",
    },
    view: {
      goal: "Create an index-case analysis dataset.",
      dimensions: { rows: 2, columns: 2 },
      columns: [{ table: "subject", column: "age" }],
      filters: [{ description: "Use baseline rows." }],
      quality: { duplicate_rows: 0 },
      warnings: [],
      provenance: {
        plan: { id: "plan-1", version: 1 },
        sql: { id: "sql-1", version: 1 },
        quality_report: { id: "quality-1", version: 1 },
      },
      feedback_history: [],
    },
  };
}

function client() {
  return {
    getDatasetPreview: vi.fn().mockResolvedValue({
      dataset_id: "subset-1",
      columns: ["age", "recurrence"],
      rows: [{ age: 42, recurrence: 1 }],
      row_count: 2,
    }),
    getDatasetSchema: vi.fn().mockResolvedValue({
      dataset_id: "subset-1",
      schema: { age: { dataType: "number" } },
    }),
    getDatasetProvenance: vi.fn().mockResolvedValue({
      dataset_id: "subset-1",
      dataset_version: 1,
      sql: "SELECT age, recurrence FROM cohort",
      sql_artifact: { id: "sql-1", kind: "validated_sql", version: 1 },
      sql_sha256: "hash-1",
    }),
  };
}

describe("DbRagDatasetReview", () => {
  it("shows collapsed SQL immediately after Schema", async () => {
    const apiClient = client();
    render(
      <DbRagDatasetReview
        apiClient={apiClient}
        interrupt={interrupt()}
        onResume={vi.fn()}
        threadId="thread-1"
      />,
    );

    const schemaSummary = await screen.findByText("Schema", {
      selector: "summary",
    });
    const sqlSummary = await screen.findByText("SQL used", {
      selector: "summary",
    });
    const sqlDetails = sqlSummary.closest("details");

    expect(sqlDetails).not.toHaveAttribute("open");
    expect(Boolean(
      (schemaSummary.closest("details")?.compareDocumentPosition(sqlDetails as Node) ?? 0)
        & Node.DOCUMENT_POSITION_FOLLOWING,
    )).toBe(true);
    expect(apiClient.getDatasetProvenance).toHaveBeenCalledWith(
      "thread-1",
      "subset-1",
    );

    fireEvent.click(sqlSummary);
    expect(sqlDetails).toHaveAttribute("open");
    expect(screen.getByText("SELECT age, recurrence FROM cohort"))
      .toBeInTheDocument();
    expect(screen.getByText("SELECT age, recurrence FROM cohort").closest(".code-block"))
      .toHaveClass("code-block-sql");
  });

  it("replaces stale SQL when revision produces a new dataset", async () => {
    const apiClient = client();
    apiClient.getDatasetProvenance.mockImplementation(
      async (_threadId: string, datasetId: string) => ({
        dataset_id: datasetId,
        dataset_version: 1,
        sql: datasetId === "subset-1"
          ? "SELECT age FROM original_cohort"
          : "SELECT age FROM revised_cohort",
        sql_artifact: {
          id: datasetId === "subset-1" ? "sql-1" : "sql-2",
          kind: "validated_sql",
          version: 1,
        },
        sql_sha256: datasetId === "subset-1" ? "hash-1" : "hash-2",
      }),
    );
    const { rerender } = render(
      <DbRagDatasetReview
        apiClient={apiClient}
        interrupt={interrupt("subset-1")}
        onResume={vi.fn()}
        threadId="thread-1"
      />,
    );
    await screen.findByText("SELECT age FROM original_cohort");

    rerender(
      <DbRagDatasetReview
        apiClient={apiClient}
        interrupt={interrupt("subset-2")}
        onResume={vi.fn()}
        threadId="thread-1"
      />,
    );

    await waitFor(() => expect(apiClient.getDatasetProvenance)
      .toHaveBeenLastCalledWith("thread-1", "subset-2"));
    expect(await screen.findByText("SELECT age FROM revised_cohort"))
      .toBeInTheDocument();
    expect(screen.queryByText("SELECT age FROM original_cohort"))
      .not.toBeInTheDocument();
  });

  it("does not expose an optional provenance API failure", async () => {
    const apiClient = client();
    apiClient.getDatasetProvenance.mockRejectedValue(
      new Error("API request failed with status 404"),
    );
    render(
      <DbRagDatasetReview
        apiClient={apiClient}
        interrupt={interrupt()}
        onResume={vi.fn()}
        threadId="thread-1"
      />,
    );

    expect(await screen.findByText("42")).toBeInTheDocument();
    await waitFor(() => expect(apiClient.getDatasetProvenance).toHaveBeenCalled());

    expect(screen.queryByText("API request failed with status 404"))
      .not.toBeInTheDocument();
    expect(screen.queryByText("SQL used", { selector: "summary" }))
      .not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("renders the typed view and preview", async () => {
    render(
      <DbRagDatasetReview
        apiClient={client()}
        interrupt={interrupt()}
        onResume={vi.fn()}
        threadId="thread-1"
      />,
    );

    expect(screen.queryByText("Dataset ID: subset-1")).not.toBeInTheDocument();
    expect(screen.getByText("Create an index-case analysis dataset."))
      .toBeInTheDocument();
    expect(screen.getByText("Use baseline rows.")).toBeInTheDocument();
    expect(await screen.findByText("42")).toBeInTheDocument();
  });

  it("emits approve, trimmed revise, and cancel", async () => {
    const onResume = vi.fn();
    render(
      <DbRagDatasetReview
        apiClient={client()}
        interrupt={interrupt()}
        onResume={onResume}
        threadId="thread-1"
      />,
    );
    await screen.findByText("42");

    const approveButton = screen.getByRole("button", { name: "Approve" });
    const reviseButton = screen.getByRole("button", { name: "Request revision" });
    const cancelButton = screen.getByRole("button", { name: "Cancel" });
    expect(approveButton).toHaveClass("review-action-primary");
    expect(reviseButton).toHaveClass("review-action-primary");
    expect(cancelButton).toHaveClass("review-action-secondary");

    fireEvent.click(approveButton);
    expect(onResume).toHaveBeenLastCalledWith({ action: "approve" });
    fireEvent.change(
      screen.getByLabelText("Feedback for the next dataset attempt"),
      { target: { value: "  Keep baseline rows only.  " } },
    );
    fireEvent.click(reviseButton);
    expect(onResume).toHaveBeenLastCalledWith({
      action: "revise",
      feedback: "Keep baseline rows only.",
    });
    fireEvent.click(cancelButton);
    expect(onResume).toHaveBeenLastCalledWith({ action: "cancel" });
  });
});
