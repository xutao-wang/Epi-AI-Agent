import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MessageAttachment from "./MessageAttachment";
import type { ConversationAttachment } from "./types";

const fetchAttachmentBlob = vi.fn().mockResolvedValue(
  new Blob(["attachment"], { type: "application/octet-stream" }),
);

beforeEach(() => {
  fetchAttachmentBlob.mockReset().mockResolvedValue(
    new Blob(["attachment"], { type: "application/octet-stream" }),
  );
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:attachment"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderAttachment(
  attachment: ConversationAttachment,
  overrides: Record<string, unknown> = {},
) {
  return render(
    <MessageAttachment
      attachment={attachment}
      fetchAttachmentBlob={fetchAttachmentBlob}
      getDatasetPreview={vi.fn()}
      getDatasetSchema={vi.fn()}
      {...overrides}
    />,
  );
}

describe("MessageAttachment", () => {
  it("renders a user input file as an authenticated download", async () => {
    renderAttachment({
      id: "attachment-csv",
      kind: "tabular",
      label: "cohort.csv",
      filename: "cohort.csv",
      mime: "text/csv",
      byte_size: 1200,
      relationship: "input",
      origin_message_id: "user-1",
    });

    expect(screen.getByText("cohort.csv")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Download cohort.csv" }),
    ).toBeInTheDocument();
    expect(fetchAttachmentBlob).toHaveBeenCalledWith("attachment-csv");
  });

  it("renders a reused file as a link to its originating message", () => {
    renderAttachment({
      id: "attachment-csv",
      kind: "tabular",
      label: "cohort.csv",
      filename: "cohort.csv",
      mime: "text/csv",
      byte_size: 1200,
      relationship: "used",
      origin_message_id: "user-1",
    });

    expect(screen.getByRole("link", { name: "cohort.csv" })).toHaveAttribute(
      "href",
      "#message-user-1",
    );
  });

  it("renders an approved image output inline with authenticated download", async () => {
    renderAttachment({
      id: "figure-1",
      kind: "figure",
      label: "Kaplan-Meier curve",
      filename: "",
      mime: "image/png",
      byte_size: null,
      relationship: "output",
      origin_message_id: null,
    });

    expect(
      await screen.findByRole("img", { name: "Kaplan-Meier curve" }),
    ).toHaveAttribute("src", "blob:attachment");
    expect(
      screen.getByRole("button", { name: "Download figure" }),
    ).toBeInTheDocument();
  });

  it("keeps dataset details collapsed until one disclosure is opened", async () => {
    const getDatasetPreview = vi.fn().mockResolvedValue({
      dataset_id: "subset-1",
      columns: ["subject_id"],
      rows: [{ subject_id: "SUB-1" }],
      row_count: 1,
    });
    const getDatasetSchema = vi.fn().mockResolvedValue({
      dataset_id: "subset-1",
      schema: {
        subject_id: {
          dataType: "string",
          description: "Participant identifier",
        },
      },
    });
    const getDatasetProvenance = vi.fn().mockResolvedValue({
      dataset_id: "subset-1",
      dataset_version: 1,
      sql: "SELECT * FROM cohort",
      sql_artifact: { id: "sql-1", kind: "validated_sql", version: 1 },
      sql_sha256: "hash",
    });
    renderAttachment(
      {
        id: "subset-1",
        kind: "subset",
        label: "Analysis subset",
        filename: "",
        mime: "text/csv",
        byte_size: null,
        relationship: "output",
        origin_message_id: null,
      },
      { getDatasetPreview, getDatasetSchema, getDatasetProvenance },
    );

    expect(screen.queryByRole("button", { name: "Preview" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Schema" })).not.toBeInTheDocument();
    expect(screen.getByText("Loading Download dataset…")).toBeInTheDocument();

    const detailsSummary = screen.getByText("Dataset details", {
      selector: "summary",
    });
    const details = detailsSummary.closest("details");

    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("SELECT * FROM cohort")).not.toBeInTheDocument();

    fireEvent.click(detailsSummary);

    expect(details).toHaveAttribute("open");
    expect((await screen.findByText("SUB-1")).closest("table"))
      .toHaveTextContent("SUB-1");
    expect(
      await within(details as HTMLDetailsElement).findByRole("table", {
        name: "Dataset schema",
      }),
    ).toHaveTextContent("Participant identifier");
    await waitFor(() => {
      expect(getDatasetPreview).toHaveBeenCalledWith("subset-1", 100);
      expect(getDatasetSchema).toHaveBeenCalledWith("subset-1");
    });
    expect(await screen.findByText("SELECT * FROM cohort")).toBeInTheDocument();
  });

  it("shows exact Python, copyable gray output, and SQL lineage for an analysis output", async () => {
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });

    const getAnalysisResult = vi.fn().mockResolvedValue({
      analysis_run_id: "analysis-1",
      analysis_run_version: 1,
      method: "custom_python",
      python_code: "print('exact analysis')",
      output_text: "exact output",
      dataset: { id: "subset-1", kind: "analysis_dataset", version: 1 },
      dataset_source: "prior_artifact",
      dataset_source_reason: "The uploaded table contains the requested fields.",
      tables: [],
      figures: [],
    });
    const getDatasetProvenance = vi.fn().mockResolvedValue({
      dataset_id: "subset-1",
      dataset_version: 1,
      sql: "SELECT * FROM cohort",
      sql_artifact: { id: "sql-1", kind: "validated_sql", version: 1 },
      sql_sha256: "hash",
    });

    renderAttachment(
      {
        id: "analysis-1",
        kind: "analysis_run",
        label: "Custom Python analysis",
        filename: "",
        mime: "application/json",
        byte_size: null,
        relationship: "output",
        origin_message_id: null,
      },
      { getAnalysisResult, getDatasetProvenance },
    );

    expect(await screen.findByText("print('exact analysis')")).toBeInTheDocument();
    expect(screen.getByText("exact output")).toBeInTheDocument();
    expect(screen.getByText("exact output").closest(".code-block")).toHaveClass(
      "code-block-text",
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy output" }));
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("exact output");
    });
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(screen.getByText("Earlier thread artifact").closest("p"))
      .toHaveTextContent("Dataset source: Earlier thread artifact");
    expect(screen.getByText(/Dataset used: subset-1/)).toBeInTheDocument();
    expect(await screen.findByText("SELECT * FROM cohort")).toBeInTheDocument();
    expect(screen.queryByText("Custom Python analysis")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Download Custom Python analysis/i }),
    ).not.toBeInTheDocument();
    expect(getAnalysisResult).toHaveBeenCalledWith("analysis-1");
    expect(getDatasetProvenance).toHaveBeenCalledWith("subset-1");

    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: originalClipboard,
    });
  });

  it("does not request unavailable SQL provenance for a current uploaded table", async () => {
    const getAnalysisResult = vi.fn().mockResolvedValue({
      analysis_run_id: "analysis-current-upload",
      analysis_run_version: 1,
      method: "custom_python",
      python_code: "print('exact analysis')",
      output_text: "exact output",
      dataset: { id: "uploaded-1", kind: "uploaded", version: 1 },
      dataset_source: "current_upload",
      dataset_source_reason: "The uploaded table contains the requested fields.",
      tables: [],
      figures: [],
    });
    const getDatasetProvenance = vi.fn().mockRejectedValue(
      new Error("API request failed with status 409"),
    );

    renderAttachment(
      {
        id: "analysis-current-upload",
        kind: "analysis_run",
        label: "Custom Python analysis",
        filename: "",
        mime: "application/json",
        byte_size: null,
        relationship: "output",
        origin_message_id: null,
      },
      { getAnalysisResult, getDatasetProvenance },
    );

    await screen.findByText("Current uploaded table");
    await new Promise((resolve) => window.setTimeout(resolve, 10));

    expect(getDatasetProvenance).not.toHaveBeenCalled();
    expect(screen.queryByText("Dataset and SQL")).not.toBeInTheDocument();
    expect(screen.queryByText("API request failed with status 409")).not.toBeInTheDocument();
  });

  it("uses plain recovery guidance when a completed analysis cannot load", async () => {
    renderAttachment(
      {
        id: "analysis-unavailable",
        kind: "analysis_run",
        label: "Custom Python analysis",
        filename: "",
        mime: "application/json",
        byte_size: null,
        relationship: "output",
        origin_message_id: null,
      },
      {
        getAnalysisResult: vi.fn().mockRejectedValue(
          new Error("API request failed with status 409"),
        ),
      },
    );

    expect(await screen.findByText(
      "Analysis details are unavailable. Refresh the conversation and try again.",
    )).toBeInTheDocument();
    expect(screen.queryByText("API request failed with status 409")).not.toBeInTheDocument();
  });
});
