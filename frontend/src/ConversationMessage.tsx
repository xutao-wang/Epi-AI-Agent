import { useState } from "react";
import ClarificationTrace from "./ClarificationTrace";
import CodeBlock from "./CodeBlock";
import MessageAttachment from "./MessageAttachment";
import { parseAssistantMessage } from "./messageRendering";
import type {
  ConversationMessage as ConversationMessageType,
  CompletedAnalysisResult,
  DatasetProvenance,
  DatasetPreview,
  DatasetSchemaResponse,
} from "./types";
import type { ReactNode } from "react";

interface Props {
  fetchAttachmentBlob: (attachmentId: string) => Promise<Blob>;
  getDatasetPreview?: (
    attachmentId: string,
    limit: number,
  ) => Promise<DatasetPreview>;
  getDatasetSchema?: (
    attachmentId: string,
  ) => Promise<DatasetSchemaResponse>;
  getDatasetProvenance?: (attachmentId: string) => Promise<DatasetProvenance>;
  getAnalysisResult?: (attachmentId: string) => Promise<CompletedAnalysisResult>;
  message: ConversationMessageType;
}

function formatMessageTimestamp(createdAt: string | null | undefined): string | null {
  if (!createdAt) {
    return null;
  }
  const timestamp = new Date(createdAt);
  if (Number.isNaN(timestamp.getTime())) {
    return null;
  }
  return new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    hour: "numeric",
    minute: "2-digit",
  }).format(timestamp);
}

type MarkdownBlock =
  | { type: "paragraph"; text: string }
  | { type: "heading"; level: 1 | 2 | 3 | 4 | 5 | 6; text: string }
  | {
      type: "list";
      items: string[];
      ordered: boolean;
      start?: number;
    }
  | { type: "table"; headers: string[]; rows: string[][] };

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line);
  return (
    cells.length > 0 &&
    cells.every((cell) => /^:?-{3,}:?$/.test(cell))
  );
}

function startsMarkdownTable(lines: string[], index: number): boolean {
  const currentLine = lines[index]?.trim() ?? "";
  const nextLine = lines[index + 1]?.trim() ?? "";
  return currentLine.includes("|") && isTableSeparator(nextLine);
}

function parseMarkdownBlocks(text: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const paragraphLines: string[] = [];
  let listItems: string[] = [];
  let listOrdered = false;
  let listStart: number | undefined;

  function flushParagraph() {
    if (paragraphLines.length) {
      blocks.push({
        type: "paragraph",
        text: paragraphLines.join(" ").trim(),
      });
      paragraphLines.length = 0;
    }
  }

  function flushList() {
    if (listItems.length) {
      blocks.push({
        type: "list",
        items: listItems,
        ordered: listOrdered,
        start: listStart,
      });
      listItems = [];
      listOrdered = false;
      listStart = undefined;
    }
  }

  const lines = text.split(/\r?\n/);
  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const rawLine = lines[lineIndex];
    const line = rawLine.trim();
    if (!line) {
      flushParagraph();
      flushList();
      continue;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      flushParagraph();
      flushList();
      blocks.push({
        type: "heading",
        level: headingMatch[1].length as 1 | 2 | 3 | 4 | 5 | 6,
        text: headingMatch[2].trim(),
      });
      continue;
    }

    if (startsMarkdownTable(lines, lineIndex)) {
      flushParagraph();
      flushList();
      const headers = splitTableRow(line);
      const rows: string[][] = [];
      lineIndex += 2;
      while (lineIndex < lines.length) {
        const rowLine = lines[lineIndex].trim();
        if (!rowLine || !rowLine.includes("|")) {
          lineIndex -= 1;
          break;
        }
        rows.push(splitTableRow(rowLine));
        lineIndex += 1;
      }
      blocks.push({ type: "table", headers, rows });
      continue;
    }

    const bulletMatch = line.match(/^[-*]\s+(.+)$/);
    if (bulletMatch) {
      flushParagraph();
      if (listItems.length && listOrdered) {
        flushList();
      }
      listOrdered = false;
      listItems.push(bulletMatch[1]);
      continue;
    }

    const numberedMatch = line.match(/^(\d+)\.\s+(.+)$/);
    if (numberedMatch) {
      flushParagraph();
      if (listItems.length && !listOrdered) {
        flushList();
      }
      if (!listItems.length) {
        listStart = Number(numberedMatch[1]);
      }
      listOrdered = true;
      listItems.push(numberedMatch[2]);
      continue;
    }

    flushList();
    paragraphLines.push(line);
  }

  flushParagraph();
  flushList();

  return blocks.length ? blocks : [{ type: "paragraph", text }];
}

function renderInlineMarkdown(text: string): ReactNode[] {
  function normalizeTextSegment(segment: string) {
    return segment
      .replace(/\$([^$\n]+)\$/g, "$1")
      .replace(/\\approx/g, "≈")
      .replace(/\\times/g, "×")
      .replace(/\\infty/g, "∞")
      .replace(/\\\$/g, "$");
  }

  return text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((segment, index) => {
    if (segment.startsWith("`") && segment.endsWith("`") && segment.length > 1) {
      return (
        <code className="inline-code" key={index}>
          {segment.slice(1, -1)}
        </code>
      );
    }
    if (
      segment.startsWith("**") &&
      segment.endsWith("**") &&
      segment.length > 4
    ) {
      return <strong key={index}>{normalizeTextSegment(segment.slice(2, -2))}</strong>;
    }
    return normalizeTextSegment(segment);
  });
}

function renderMarkdownText(text: string, keyPrefix: string) {
  return parseMarkdownBlocks(text).map((block, index) => {
    if (block.type === "heading") {
      const key = `${keyPrefix}-heading-${index}`;
      const children = renderInlineMarkdown(block.text);
      if (block.level === 1) {
        return (
          <h1 className="message-markdown-heading" key={key}>
            {children}
          </h1>
        );
      }
      if (block.level === 2) {
        return (
          <h2 className="message-markdown-heading" key={key}>
            {children}
          </h2>
        );
      }
      if (block.level === 3) {
        return (
          <h3 className="message-markdown-heading" key={key}>
            {children}
          </h3>
        );
      }
      if (block.level === 4) {
        return (
          <h4 className="message-markdown-heading" key={key}>
            {children}
          </h4>
        );
      }
      if (block.level === 5) {
        return (
          <h5 className="message-markdown-heading" key={key}>
            {children}
          </h5>
        );
      }
      return (
        <h6 className="message-markdown-heading" key={key}>
          {children}
        </h6>
      );
    }

    if (block.type === "list") {
      if (block.ordered) {
        return (
          <ol
            className="message-markdown-list"
            key={`${keyPrefix}-list-${index}`}
            start={block.start}
          >
            {block.items.map((item, itemIndex) => (
              <li key={`${keyPrefix}-list-${index}-${itemIndex}`}>
                {renderInlineMarkdown(item)}
              </li>
            ))}
          </ol>
        );
      }

      return (
        <ul className="message-markdown-list" key={`${keyPrefix}-list-${index}`}>
          {block.items.map((item, itemIndex) => (
            <li key={`${keyPrefix}-list-${index}-${itemIndex}`}>
              {renderInlineMarkdown(item)}
            </li>
          ))}
        </ul>
      );
    }

    if (block.type === "table") {
      return (
        <div
          className="message-markdown-table-wrap"
          key={`${keyPrefix}-table-${index}`}
        >
          <table className="message-markdown-table">
            <thead>
              <tr>
                {block.headers.map((header, headerIndex) => (
                  <th key={`${keyPrefix}-table-${index}-header-${headerIndex}`}>
                    {renderInlineMarkdown(header)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={`${keyPrefix}-table-${index}-row-${rowIndex}`}>
                  {row.map((cell, cellIndex) => (
                    <td key={`${keyPrefix}-table-${index}-${rowIndex}-${cellIndex}`}>
                      {renderInlineMarkdown(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }

    return (
      <p key={`${keyPrefix}-paragraph-${index}`}>
        {renderInlineMarkdown(block.text)}
      </p>
    );
  });
}

export default function ConversationMessage({
  fetchAttachmentBlob,
  getDatasetPreview,
  getDatasetSchema,
  getDatasetProvenance,
  getAnalysisResult,
  message,
}: Props) {
  const [copiedCode, setCopiedCode] = useState<string | null>(null);
  const [messageCopied, setMessageCopied] = useState(false);
  const parts =
    message.role === "assistant"
      ? parseAssistantMessage(message.text)
      : [{ type: "text" as const, text: message.text }];
  const messageTimestamp = formatMessageTimestamp(message.created_at);
  const attachments = message.attachments ?? [];
  const usedAttachments = attachments.filter(
    (attachment) => attachment.relationship === "used",
  );
  const cardAttachments = attachments.filter(
    (attachment) => attachment.relationship !== "used",
  );
  const unavailablePreview = async () => {
    throw new Error("Dataset preview is unavailable.");
  };

  async function copyCode(code: string) {
    await navigator.clipboard.writeText(code);
    setCopiedCode(code);
  }

  async function copyMessage() {
    await navigator.clipboard.writeText(message.text);
    setMessageCopied(true);
  }

  function codeLabel(language: string) {
    const normalized = language.toLowerCase();
    if (normalized === "sql") {
      return "SQL used";
    }
    if (normalized === "python" || normalized === "py") {
      return "Python";
    }
    if (normalized === "json") {
      return "JSON";
    }
    if (["bash", "sh", "shell", "zsh"].includes(normalized)) {
      return "Bash";
    }
    return normalized === "text" ? "Code" : normalized.toUpperCase();
  }

  function copyLabel(language: string) {
    const label = codeLabel(language);
    return label === "SQL used" ? "Copy SQL" : `Copy ${label}`;
  }

  return (
    <li
      className={`message message-bounded message-${message.role}${
        message.status === "cancelled" ? " message-cancelled" : ""
      }`}
      id={`message-${message.id}`}
    >
      <div className="message-bubble message-bubble-bounded">
        <span className="message-role">{message.role}</span>
        {message.status === "cancelled" ? (
          <span
            aria-label="Message status: Cancelled"
            className="message-status message-status-cancelled"
          >
            Cancelled
          </span>
        ) : null}
        <div className="message-body">
          {parts.map((part, index) => {
            if (part.type === "code") {
              return (
                <CodeBlock
                  actions={
                    <button type="button" onClick={() => copyCode(part.code)}>
                      {copiedCode === part.code ? "Copied" : copyLabel(part.language)}
                    </button>
                  }
                  code={part.code}
                  key={`${message.id}-code-${index}`}
                  label={codeLabel(part.language)}
                  language={part.language}
                />
              );
            }

            if (part.type === "dbRagDatasetResult") {
              return (
                <section
                  className="db-rag-result-summary"
                  key={`${message.id}-db-rag-result-${index}`}
                >
                  {renderMarkdownText(part.summary, `${message.id}-summary-${index}`)}
                  <details>
                    <summary>SQL and selection details</summary>
                    {part.details
                      ? renderMarkdownText(part.details, `${message.id}-details-${index}`)
                      : null}
                    {part.sql ? (
                      <CodeBlock
                        actions={
                          <button type="button" onClick={() => copyCode(part.sql)}>
                            {copiedCode === part.sql ? "Copied" : "Copy SQL"}
                          </button>
                        }
                        code={part.sql}
                        label="SQL used"
                        language="sql"
                      />
                    ) : null}
                  </details>
                </section>
              );
            }

            return renderMarkdownText(part.text, `${message.id}-text-${index}`);
          })}
        </div>
        {message.role === "assistant" ? (
          <ClarificationTrace exchanges={message.clarifications ?? []} />
        ) : null}
        {usedAttachments.length ? (
          <section className="message-used-files" aria-label="Used files">
            <h4>Used files</h4>
            {usedAttachments.map((attachment) => (
              <MessageAttachment
                attachment={attachment}
                fetchAttachmentBlob={fetchAttachmentBlob}
                getDatasetPreview={getDatasetPreview ?? unavailablePreview}
                getDatasetSchema={getDatasetSchema ?? unavailablePreview}
                getDatasetProvenance={getDatasetProvenance}
                getAnalysisResult={getAnalysisResult}
                key={`${attachment.id}-used`}
              />
            ))}
          </section>
        ) : null}
        {cardAttachments.length ? (
          <section
            className="message-attachment-list"
            aria-label="Message files"
          >
            {cardAttachments.map((attachment) => (
              <MessageAttachment
                attachment={attachment}
                fetchAttachmentBlob={fetchAttachmentBlob}
                getDatasetPreview={getDatasetPreview ?? unavailablePreview}
                getDatasetSchema={getDatasetSchema ?? unavailablePreview}
                getDatasetProvenance={getDatasetProvenance}
                getAnalysisResult={getAnalysisResult}
                key={`${attachment.id}-${attachment.relationship}`}
              />
            ))}
          </section>
        ) : null}
      </div>
      <div className="message-actions" aria-label="Message actions">
        {messageTimestamp ? (
          <time aria-label="Message timestamp" dateTime={message.created_at ?? undefined}>
            {messageTimestamp}
          </time>
        ) : null}
        <button
          aria-label={messageCopied ? "Message copied" : "Copy message"}
          className={`message-copy-button${messageCopied ? " message-copy-button-copied" : ""}`}
          title={messageCopied ? "Message copied" : "Copy message"}
          type="button"
          onClick={copyMessage}
        >
          {messageCopied ? (
            <svg
              aria-hidden="true"
              className="message-copy-checkmark"
              data-testid="message-copy-checkmark"
              fill="none"
              viewBox="0 0 24 24"
            >
              <path d="m5 12 4.5 4.5L19 7" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" />
            </svg>
          ) : (
            <span className="message-copy-icon" aria-hidden="true" />
          )}
        </button>
      </div>
    </li>
  );
}
