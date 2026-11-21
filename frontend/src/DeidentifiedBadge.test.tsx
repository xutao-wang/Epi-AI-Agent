import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AttachmentComposer from "./AttachmentComposer";
import MessageAttachment from "./MessageAttachment";
import type { AttachmentManifestSummary, ConversationAttachment } from "./types";

function staged(deidentified?: boolean): AttachmentManifestSummary {
  return {
    id: "attachment-" + "0".repeat(32),
    filename: "dataset_1.csv",
    kind: "tabular",
    mime: "text/csv",
    byte_size: 120,
    status: "staged",
    ...(deidentified === undefined ? {} : { deidentified }),
  };
}

function renderComposer(attachment: AttachmentManifestSummary) {
  return render(
    <AttachmentComposer
      action={null}
      disabled={false}
      errors={[]}
      isUploading={false}
      onDismissError={vi.fn()}
      onFilesSelected={vi.fn()}
      onRemove={vi.fn(async () => undefined)}
      staged={[attachment]}
    />,
  );
}

function sent(deidentified?: boolean): ConversationAttachment {
  return {
    id: "attachment-" + "1".repeat(32),
    kind: "tabular",
    label: "dataset_1.csv",
    filename: "dataset_1.csv",
    mime: "text/csv",
    byte_size: 120,
    relationship: "input",
    origin_message_id: null,
    ...(deidentified === undefined ? {} : { deidentified }),
  };
}

function renderCard(attachment: ConversationAttachment) {
  // The download button loads lazily; a load that never settles keeps the test quiet.
  return render(
    <MessageAttachment
      attachment={attachment}
      fetchAttachmentBlob={vi.fn(() => new Promise<Blob>(() => undefined))}
      getDatasetPreview={vi.fn()}
      getDatasetSchema={vi.fn()}
    />,
  );
}

describe("Passed upload checker badge", () => {
  it("shows on a composer chip for a file from the upload checker", () => {
    renderComposer(staged(true));
    const badge = screen.getByText("Passed upload checker");
    expect(badge.className).toContain("upload-checker-badge");
    expect(badge.getAttribute("title")).toContain("can miss some identifiers");
  });

  it("does not show on a composer chip for a plain upload", () => {
    renderComposer(staged(false));
    expect(screen.queryByText("Passed upload checker")).toBeNull();
    renderComposer(staged());
    expect(screen.queryByText("Passed upload checker")).toBeNull();
  });

  it("shows on the message attachment card for a file from the upload checker", () => {
    renderCard(sent(true));
    expect(screen.getByText("dataset_1.csv")).toBeTruthy();
    expect(screen.getByText("Passed upload checker").className).toContain("upload-checker-badge");
  });

  it("does not show on the message attachment card for a plain upload", () => {
    renderCard(sent(false));
    expect(screen.queryByText("Passed upload checker")).toBeNull();
    renderCard(sent());
    expect(screen.queryByText("Passed upload checker")).toBeNull();
  });
});
