import { useCallback, useEffect, useRef, useState } from "react";
import AuthenticatedArtifact from "./AuthenticatedArtifact";
import CodeBlock from "./CodeBlock";
import type {
  ActiveInterrupt,
  ResumeInterruptPayload,
  TablePreview,
} from "./types";
import type { ReactNode } from "react";

type AnalysisReviewInterrupt = Extract<
  ActiveInterrupt,
  { type: "analysis_result_review" }
>;

interface Props {
  apiClient: {
    fetchArtifactBlob: (threadId: string, artifactId: string) => Promise<Blob>;
    getTablePreview: (
      threadId: string,
      artifactId: string,
      limit?: number,
    ) => Promise<TablePreview>;
  };
  disabled?: boolean;
  interrupt: AnalysisReviewInterrupt;
  onResume: (payload: ResumeInterruptPayload) => void;
  threadId: string;
}

type CopyTarget = "code" | "output";

function ResultSection({
  action,
  children,
  title,
}: {
  action?: ReactNode;
  children: ReactNode;
  title: string;
}) {
  return (
    <div
      className={`analysis-result-section${action ? " analysis-result-section-has-action" : ""}`}
    >
      <details open>
        <summary>{title}</summary>
        <div className="analysis-result-section-body">{children}</div>
      </details>
      {action ? <div className="analysis-result-section-action">{action}</div> : null}
    </div>
  );
}

function CopyButton({
  copied,
  label,
  onClick,
}: {
  copied: boolean;
  label: CopyTarget;
  onClick: () => void;
}) {
  const name = `${label[0].toUpperCase()}${label.slice(1)}`;
  const accessibleLabel = copied ? `${name} copied` : `Copy ${label}`;
  return (
    <button
      aria-label={accessibleLabel}
      className={`analysis-result-copy-button${copied ? " analysis-result-copy-button-copied" : ""}`}
      onClick={onClick}
      title={accessibleLabel}
      type="button"
    >
      {copied ? (
        <svg
          aria-hidden="true"
          className="message-copy-checkmark"
          fill="none"
          viewBox="0 0 24 24"
        >
          <path
            d="m5 12 4.5 4.5L19 7"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="2.5"
          />
        </svg>
      ) : (
        <span aria-hidden="true" className="message-copy-icon" />
      )}
    </button>
  );
}

function display(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function KeyValueGrid({ entries }: { entries: Record<string, unknown> }) {
  if (!Object.keys(entries).length) return <p>None reported.</p>;
  return (
    <dl className="analysis-result-grid">
      {Object.entries(entries).map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{display(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function AnalysisTable({
  apiClient,
  artifactId,
  threadId,
}: {
  apiClient: Props["apiClient"];
  artifactId: string;
  threadId: string;
}) {
  const [preview, setPreview] = useState<TablePreview | null>(null);
  const [previewUnavailable, setPreviewUnavailable] = useState(false);
  const loadArtifact = useCallback(
    () => apiClient.fetchArtifactBlob(threadId, artifactId),
    [apiClient, artifactId, threadId],
  );

  useEffect(() => {
    let cancelled = false;
    apiClient
      .getTablePreview(threadId, artifactId)
      .then((nextPreview) => {
        if (!cancelled) setPreview(nextPreview);
      })
      .catch(() => {
        if (!cancelled) setPreviewUnavailable(true);
      });
    return () => {
      cancelled = true;
    };
  }, [apiClient, artifactId, threadId]);

  return (
    <article className="analysis-result-table-artifact">
      {preview ? (
        <div className="analysis-result-table-wrap">
          <table className="analysis-result-table">
            <thead>
              <tr>{preview.columns.map((column) => <th key={column}>{column}</th>)}</tr>
            </thead>
            <tbody>
              {preview.rows.map((row, index) => (
                <tr key={index}>
                  {preview.columns.map((column) => <td key={column}>{row[column] ?? ""}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p>{previewUnavailable ? "Table preview is unavailable." : "Loading table preview…"}</p>
      )}
      <AuthenticatedArtifact
        alt="Download table"
        filename={`${artifactId}.csv`}
        load={loadArtifact}
        mode="download"
      />
    </article>
  );
}

function AnalysisFigure({
  apiClient,
  artifactId,
  threadId,
}: {
  apiClient: Props["apiClient"];
  artifactId: string;
  threadId: string;
}) {
  const loadArtifact = useCallback(
    () => apiClient.fetchArtifactBlob(threadId, artifactId),
    [apiClient, artifactId, threadId],
  );

  return (
    <figure className="analysis-result-figure">
      <AuthenticatedArtifact
        alt="Pending Python analysis figure"
        filename={`${artifactId}.png`}
        load={loadArtifact}
        mode="image"
      />
      <figcaption>
        <AuthenticatedArtifact
          alt="Download figure"
          filename={`${artifactId}.png`}
          load={loadArtifact}
          mode="download"
        />
      </figcaption>
    </figure>
  );
}

export default function AnalysisResultReview({
  apiClient,
  disabled = false,
  interrupt,
  onResume,
  threadId,
}: Props) {
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState<Record<CopyTarget, boolean>>({
    code: false,
    output: false,
  });
  const copyTimeouts = useRef<Partial<Record<CopyTarget, number>>>({});
  const view = interrupt.view;
  const rawCode = typeof view.specification.code === "string"
    ? view.specification.code
    : "";
  const code = rawCode.trim() ? rawCode : "";
  const specification = Object.fromEntries(
    Object.entries(view.specification).filter(([key]) => key !== "code"),
  );
  const runtimeLanguage =
    typeof view.runtime.language === "string" && view.runtime.language.trim()
      ? view.runtime.language
      : "Analysis";

  useEffect(() => () => {
    Object.values(copyTimeouts.current).forEach((timeoutId) => {
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    });
  }, []);

  async function copyReviewText(target: CopyTarget, value: string) {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      return;
    }
    setCopied((current) => ({ ...current, [target]: true }));
    const priorTimeout = copyTimeouts.current[target];
    if (priorTimeout !== undefined) window.clearTimeout(priorTimeout);
    copyTimeouts.current[target] = window.setTimeout(() => {
      setCopied((current) => ({ ...current, [target]: false }));
      delete copyTimeouts.current[target];
    }, 2_000);
  }

  function revise() {
    const trimmed = feedback.trim();
    if (!trimmed) {
      setError("Please enter revision feedback.");
      return;
    }
    setError("");
    onResume({ action: "revise", feedback: trimmed });
  }

  return (
    <section
      className="analysis-result-review-panel"
      aria-labelledby="analysis-result-review-heading"
    >
      <header className="analysis-result-review-header">
        <div>
          <h2 id="analysis-result-review-heading">Review analysis results</h2>
          <p>
            Approval permits publication of this exact result. It does not
            automatically interpret the result or complete your workflow.
          </p>
        </div>
        <span className="analysis-result-status">Pending review</span>
      </header>

      {code ? (
        <ResultSection
          action={
            <CopyButton
              copied={copied.code}
              label="code"
              onClick={() => void copyReviewText("code", code)}
            />
          }
          title="Code"
        >
          <CodeBlock code={code} label="Generated Python" language="python" />
        </ResultSection>
      ) : null}

      {view.output_text ? (
        <ResultSection
          action={
            <CopyButton
              copied={copied.output}
              label="output"
              onClick={() => void copyReviewText("output", view.output_text)}
            />
          }
          title="Output"
        >
          <CodeBlock
            code={view.output_text}
            label="Python output"
            language="text"
          />
        </ResultSection>
      ) : null}

      {view.warnings.length || view.warnings_truncated ? (
        <ResultSection title="Warnings">
          {view.warnings.length ? (
            <ul className="analysis-result-list">
              {view.warnings.map((warning, index) => <li key={index}>{warning}</li>)}
            </ul>
          ) : null}
          {view.warnings_truncated ? (
            <p>Additional warnings were omitted from this bounded preview.</p>
          ) : null}
        </ResultSection>
      ) : null}

      {view.tables.length ? (
        <ResultSection title="Tables">
          {view.tables.map((table) => (
            <AnalysisTable
              apiClient={apiClient}
              artifactId={table.id}
              key={`${table.id}:${table.version}`}
              threadId={threadId}
            />
          ))}
        </ResultSection>
      ) : null}

      {view.figures.length ? (
        <ResultSection title="Figures">
          {view.figures.map((figure) => (
            <AnalysisFigure
              apiClient={apiClient}
              artifactId={figure.id}
              key={`${figure.id}:${figure.version}`}
              threadId={threadId}
            />
          ))}
        </ResultSection>
      ) : null}

      <details className="analysis-result-section analysis-result-technical">
        <summary>Show technical details</summary>
        <div className="analysis-result-section-body">
          <section>
            <h3>Model specification</h3>
            <KeyValueGrid entries={specification} />
          </section>

          <section>
            <h3>{runtimeLanguage} runtime</h3>
            <KeyValueGrid entries={view.runtime} />
          </section>

          {view.feedback_history.length ? (
            <section>
              <h3>Prior feedback</h3>
              <ol className="analysis-result-list">
                {view.feedback_history.map((entry, index) => (
                  <li key={index}>{display(entry.feedback ?? entry)}</li>
                ))}
              </ol>
            </section>
          ) : null}
        </div>
      </details>

      <label className="analysis-result-feedback">
        <span>Revision feedback</span>
        <textarea
          disabled={disabled}
          onChange={(event) => setFeedback(event.target.value)}
          rows={4}
          value={feedback}
        />
      </label>
      {error ? <p role="alert">{error}</p> : null}
      <div className="analysis-result-actions">
        <button disabled={disabled} onClick={() => onResume({ action: "approve" })} type="button">
          Approve Results
        </button>
        <button disabled={disabled} onClick={revise} type="button">Revise Analysis</button>
        <button disabled={disabled} onClick={() => onResume({ action: "cancel" })} type="button">Cancel</button>
      </div>
    </section>
  );
}
