import { useEffect, useState } from "react";
import CodeBlock from "./CodeBlock";
import DatasetSchemaView from "./DatasetSchemaView";
import type {
  ActiveInterrupt,
  DatasetPreview,
  DatasetProvenance,
  DatasetSchemaResponse,
  ResumeInterruptPayload,
} from "./types";

type DatasetReviewInterrupt = Extract<
  ActiveInterrupt,
  { type: "dataset_review" }
>;

interface DatasetApiClient {
  getDatasetPreview(
    threadId: string,
    datasetId: string,
    limit?: number,
  ): Promise<DatasetPreview>;
  getDatasetSchema(
    threadId: string,
    datasetId: string,
  ): Promise<DatasetSchemaResponse>;
  getDatasetProvenance(
    threadId: string,
    datasetId: string,
  ): Promise<DatasetProvenance>;
}

interface Props {
  apiClient: DatasetApiClient;
  disabled?: boolean;
  interrupt: DatasetReviewInterrupt;
  onResume: (payload: ResumeInterruptPayload) => void;
  threadId: string | null;
}

function display(value: unknown): string {
  if (value === null || value === undefined) return "";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export default function DbRagDatasetReview({
  apiClient,
  disabled = false,
  interrupt,
  onResume,
  threadId,
}: Props) {
  const datasetId = interrupt.artifact.id;
  const view = interrupt.view;
  const [feedback, setFeedback] = useState("");
  const [feedbackError, setFeedbackError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [preview, setPreview] = useState<DatasetPreview | null>(null);
  const [provenance, setProvenance] = useState<DatasetProvenance | null>(null);
  const [schema, setSchema] = useState<DatasetSchemaResponse | null>(null);

  useEffect(() => {
    if (!threadId) return;
    let cancelled = false;
    setLoadError("");
    setPreview(null);
    setProvenance(null);
    setSchema(null);
    apiClient
      .getDatasetPreview(threadId, datasetId, 100)
      .then((value) => {
        if (!cancelled) setPreview(value);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(
            error instanceof Error
              ? error.message
              : "Unable to load dataset preview.",
          );
        }
      });
    apiClient
      .getDatasetSchema(threadId, datasetId)
      .then((value) => {
        if (!cancelled) setSchema(value);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(
            error instanceof Error
              ? error.message
              : "Unable to load dataset schema.",
          );
        }
      });
    apiClient
      .getDatasetProvenance(threadId, datasetId)
      .then((value) => {
        if (!cancelled) setProvenance(value);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [apiClient, datasetId, threadId]);

  function revise() {
    const trimmed = feedback.trim();
    if (!trimmed) {
      setFeedbackError("Please enter feedback before submitting.");
      return;
    }
    setFeedbackError("");
    onResume({ action: "revise", feedback: trimmed });
  }

  return (
    <section
      aria-labelledby="db-rag-dataset-review-heading"
      className="db-rag-dataset-review-panel"
    >
      <header className="db-rag-dataset-review-header">
        <h2 id="db-rag-dataset-review-heading">Review extracted dataset</h2>
        <p>{view.dimensions.rows ?? "Unknown"} rows</p>
        <p>{view.dimensions.columns ?? "Unknown"} columns</p>
      </header>

      {loadError ? <p role="alert">{loadError}</p> : null}

      <section className="db-rag-dataset-review-section">
        <h3>Data quality</h3>
        {view.warnings.length ? (
          <ul className="db-rag-quality-warnings">
            {view.warnings.map((warning) => (
              <li
                className={`db-rag-quality-warning db-rag-quality-warning-${warning.severity}`}
                key={warning.code}
              >
                {warning.message}
              </li>
            ))}
          </ul>
        ) : (
          <p>No quality warnings were detected.</p>
        )}
        <details>
          <summary>Raw quality report</summary>
          <CodeBlock code={JSON.stringify(view.quality, null, 2)} language="json" />
        </details>
      </section>

      <section className="db-rag-dataset-review-section">
        <h3>Extraction goal</h3>
        <p>{view.goal}</p>
      </section>

      {view.columns.length ? (
        <section className="db-rag-dataset-review-section">
          <h3>Included columns</h3>
          <ul>
            {view.columns.map((column, index) => (
              <li key={index}>{display(column.column ?? column)}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {view.filters.length ? (
        <section className="db-rag-dataset-review-section">
          <h3>Filters</h3>
          <ul>
            {view.filters.map((filter, index) => (
              <li key={index}>
                {filter.description || filter.predicate || JSON.stringify(filter)}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {preview ? (
        <section className="db-rag-dataset-review-section">
          <h3>Data preview</h3>
          <div className="dataset-table-wrap">
            <table className="dataset-table">
              <thead>
                <tr>
                  {preview.columns.map((column) => (
                    <th key={column}>{column}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {preview.columns.map((column) => (
                      <td key={column}>{display(row[column])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {schema ? (
        <details className="db-rag-dataset-review-section">
          <summary>Schema</summary>
          <DatasetSchemaView schema={schema.schema} />
        </details>
      ) : null}

      {provenance?.sql ? (
        <details className="db-rag-dataset-review-section">
          <summary>SQL used</summary>
          <CodeBlock code={provenance.sql} label="SQL used" language="sql" />
        </details>
      ) : null}

      <label className="db-rag-dataset-review-feedback">
        <span>Feedback for the next dataset attempt</span>
        <textarea
          aria-invalid={feedbackError ? "true" : "false"}
          disabled={disabled}
          onChange={(event) => setFeedback(event.target.value)}
          rows={4}
          value={feedback}
        />
      </label>
      {feedbackError ? <p role="alert">{feedbackError}</p> : null}

      <div className="db-rag-dataset-review-actions">
        <button
          className="review-action-primary"
          disabled={disabled}
          onClick={() => onResume({ action: "approve" })}
          type="button"
        >
          Approve
        </button>
        <button
          className="review-action-primary"
          disabled={disabled}
          onClick={revise}
          type="button"
        >
          Request revision
        </button>
        <button
          className="review-action-secondary"
          disabled={disabled}
          onClick={() => onResume({ action: "cancel" })}
          type="button"
        >
          Cancel
        </button>
      </div>
    </section>
  );
}
