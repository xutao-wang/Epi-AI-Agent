import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AttachmentComposer from "./AttachmentComposer";

describe("AttachmentComposer", () => {
  it("opens its hidden file input from the paperclip button", () => {
    render(
      <AttachmentComposer
        action={<button type="submit">Send</button>}
        disabled={false}
        errors={[]}
        isUploading={false}
        onDismissError={vi.fn()}
        onFilesSelected={vi.fn()}
        onRemove={vi.fn()}
        staged={[]}
      />,
    );
    const input = screen.getByTestId("attachment-file-input");
    const click = vi.spyOn(input, "click");

    fireEvent.click(screen.getByRole("button", { name: "Attach files" }));

    expect(click).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
  });

  it("selects multiple supported files and removes a staged attachment", async () => {
    const onFilesSelected = vi.fn();
    const onRemove = vi.fn().mockResolvedValue(undefined);
    const csv = new File(["id\n1\n"], "cohort.csv", { type: "text/csv" });
    const xml = new File(["<variables/>"], "annotations.xml", {
      type: "application/xml",
    });
    const { rerender } = render(
      <AttachmentComposer
        action={<button type="submit">Send</button>}
        disabled={false}
        errors={[]}
        isUploading={false}
        onDismissError={vi.fn()}
        onFilesSelected={onFilesSelected}
        onRemove={onRemove}
        staged={[]}
      />,
    );

    fireEvent.change(screen.getByTestId("attachment-file-input"), {
      target: { files: [csv, xml] },
    });

    expect(onFilesSelected).toHaveBeenCalledWith([csv, xml]);

    rerender(
      <AttachmentComposer
        action={<button type="submit">Send</button>}
        disabled={false}
        errors={[]}
        isUploading={false}
        onDismissError={vi.fn()}
        onFilesSelected={onFilesSelected}
        onRemove={onRemove}
        staged={[
          {
            id: "attachment-csv",
            filename: "cohort.csv",
            kind: "tabular",
            mime: "text/csv",
            byte_size: 1024,
            status: "staged",
          },
        ]}
      />,
    );

    expect(screen.getByText("cohort.csv")).toBeInTheDocument();
    expect(screen.getByText("tabular · 1 KB · staged")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Remove cohort.csv" }),
    );
    expect(onRemove).toHaveBeenCalledWith("attachment-csv");
  });

  it("renders upload progress and per-file errors", () => {
    const onDismissError = vi.fn();
    render(
      <AttachmentComposer
        action={<button type="submit">Send</button>}
        disabled={false}
        errors={[
          {
            filename: "archive.zip",
            code: "UNSUPPORTED_EXTENSION",
            message: "Unsupported attachment type.",
          },
        ]}
        isUploading
        onDismissError={onDismissError}
        onFilesSelected={vi.fn()}
        onRemove={vi.fn()}
        staged={[]}
      />,
    );

    expect(screen.getByText("Uploading attachments…")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "archive.zip: Unsupported attachment type.",
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "Dismiss upload error for archive.zip",
      }),
    );
    expect(onDismissError).toHaveBeenCalledWith(0);
  });
});
