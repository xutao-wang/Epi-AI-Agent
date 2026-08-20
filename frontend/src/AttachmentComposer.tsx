import { useRef, type ReactNode } from "react";
import type {
  AttachmentManifestSummary,
  AttachmentUploadError,
} from "./types";

interface Props {
  action: ReactNode;
  disabled: boolean;
  staged: AttachmentManifestSummary[];
  errors: AttachmentUploadError[];
  isUploading: boolean;
  onFilesSelected(files: File[]): void;
  onDismissError(index: number): void;
  onRemove(attachmentId: string): Promise<void>;
}

const ACCEPTED_EXTENSIONS =
  ".csv,.tsv,.xls,.xlsx,.json,.xml,.pdf,.docx,.txt,.md,.png,.jpg,.jpeg";

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${Math.round(bytes / 1024)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function AttachmentComposer({
  action,
  disabled,
  staged,
  errors,
  isUploading,
  onFilesSelected,
  onDismissError,
  onRemove,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <section
      aria-label="Message attachments"
      className="attachment-composer"
    >
      <input
        accept={ACCEPTED_EXTENSIONS}
        aria-hidden="true"
        className="attachment-composer-input"
        data-testid="attachment-file-input"
        disabled={disabled}
        multiple
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          if (files.length) {
            onFilesSelected(files);
          }
          event.target.value = "";
        }}
        ref={inputRef}
        type="file"
      />
      {isUploading ? (
        <p className="attachment-uploading" aria-live="polite">
          Uploading attachments…
        </p>
      ) : null}
      {staged.length ? (
        <ul className="attachment-composer-list">
          {staged.map((attachment) => (
            <li className="attachment-composer-chip" key={attachment.id}>
              <span>{attachment.filename}</span>
              <span>
                {attachment.kind} · {formatBytes(attachment.byte_size)} ·{" "}
                {attachment.status}
              </span>
              <button
                aria-label={`Remove ${attachment.filename}`}
                disabled={disabled}
                onClick={() => void onRemove(attachment.id)}
                type="button"
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {errors.length ? (
        <ul className="attachment-composer-errors" role="alert">
          {errors.map((uploadError, index) => (
            <li
              className="attachment-status-error"
              key={`${uploadError.filename}-${uploadError.code}-${index}`}
            >
              <span>
                {uploadError.filename}: {uploadError.message}
              </span>
              <button
                aria-label={`Dismiss upload error for ${uploadError.filename}`}
                disabled={disabled}
                onClick={() => onDismissError(index)}
                type="button"
              >
                Dismiss
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="message-form-actions">
        <div className="message-form-primary-actions">
          <button
            aria-label="Attach files"
            className="attachment-composer-picker"
            disabled={disabled}
            onClick={() => inputRef.current?.click()}
            title="Attach files"
            type="button"
          >
            <svg
              aria-hidden="true"
              className="attachment-paperclip-icon"
              fill="none"
              viewBox="0 0 24 24"
            >
              <path
                d="m18.375 12.739-7.318 7.318a4.5 4.5 0 0 1-6.364-6.364l8.025-8.025a3 3 0 1 1 4.243 4.243l-8.025 8.025a1.5 1.5 0 0 1-2.121-2.121l7.318-7.318"
                stroke="currentColor"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="1.8"
              />
            </svg>
          </button>
          {action}
        </div>
      </div>
    </section>
  );
}
