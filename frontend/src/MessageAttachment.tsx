import { useCallback, useEffect, useState } from "react";
import AuthenticatedArtifact from "./AuthenticatedArtifact";
import CodeBlock from "./CodeBlock";
import DatasetSchemaView from "./DatasetSchemaView";
import type {
  ConversationAttachment,
  CompletedAnalysisResult,
  DatasetProvenance,
  DatasetPreview,
  DatasetSchemaResponse,
} from "./types";

interface Props {
  attachment: ConversationAttachment;
  fetchAttachmentBlob(attachmentId: string): Promise<Blob>;
  getDatasetPreview(
    attachmentId: string,
    limit: number,
  ): Promise<DatasetPreview>;
  getDatasetSchema(
    attachmentId: string,
  ): Promise<DatasetSchemaResponse>;
  getDatasetProvenance?: (attachmentId: string) => Promise<DatasetProvenance>;
  getAnalysisResult?: (attachmentId: string) => Promise<CompletedAnalysisResult>;
}

const DATASET_KINDS = new Set([
  "analysis_dataset",
  "dataset",
  "db_rag_result",
  "subset",
  "uploaded",
]);

const PROVENANCE_DATASET_KINDS = new Set([
  "analysis_dataset",
  "dataset",
  "db_rag_result",
  "subset",
]);

const ANALYSIS_KINDS = new Set(["analysis_run"]);

function formatBytes(byteSize: number | null): string {
  if (byteSize === null) {
    return "";
  }
  if (byteSize < 1024) {
    return `${byteSize} B`;
  }
  if (byteSize < 1024 * 1024) {
    return `${Math.round(byteSize / 1024)} KB`;
  }
  return `${(byteSize / (1024 * 1024)).toFixed(1)} MB`;
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export default function MessageAttachment({
  attachment,
  fetchAttachmentBlob,
  getDatasetPreview,
  getDatasetSchema,
  getDatasetProvenance,
  getAnalysisResult,
}: Props) {
  const [preview, setPreview] = useState<DatasetPreview | null>(null);
  const [schema, setSchema] = useState<DatasetSchemaResponse | null>(null);
  const [datasetProvenance, setDatasetProvenance] =
    useState<DatasetProvenance | null>(null);
  const [analysisResult, setAnalysisResult] =
    useState<CompletedAnalysisResult | null>(null);
  const [analysisResultError, setAnalysisResultError] = useState<string | null>(null);
  const [analysisDatasetProvenance, setAnalysisDatasetProvenance] =
    useState<DatasetProvenance | null>(null);
  const [isLoadingPreview, setIsLoadingPreview] = useState(false);
  const [isLoadingSchema, setIsLoadingSchema] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedCode, setCopiedCode] = useState<string | null>(null);
  const isDataset =
    attachment.relationship === "output" &&
    DATASET_KINDS.has(attachment.kind);
  const isProvenanceDataset =
    attachment.relationship === "output" &&
    PROVENANCE_DATASET_KINDS.has(attachment.kind);
  const isAnalysis =
    attachment.relationship === "output" && ANALYSIS_KINDS.has(attachment.kind);
  const name = attachment.filename || attachment.label || attachment.id;
  const downloadName = attachment.filename || "figure";
  const loadAttachment = useCallback(
    () => fetchAttachmentBlob(attachment.id),
    [attachment.id, fetchAttachmentBlob],
  );

  useEffect(() => {
    let cancelled = false;

    async function loadProvenance() {
      if (isProvenanceDataset && getDatasetProvenance) {
        try {
          const nextProvenance = await getDatasetProvenance(attachment.id);
          if (!cancelled) setDatasetProvenance(nextProvenance);
        } catch {
          // SQL provenance is optional for dataset attachments.
        }
        return;
      }

      if (!isAnalysis || !getAnalysisResult) return;
      try {
        const nextResult = await getAnalysisResult(attachment.id);
        if (cancelled) return;
        setAnalysisResult(nextResult);
        setAnalysisResultError(null);
        if (
          !getDatasetProvenance
          || nextResult.dataset_source !== "prior_artifact"
        ) return;
        try {
          const nextProvenance = await getDatasetProvenance(nextResult.dataset.id);
          if (!cancelled) setAnalysisDatasetProvenance(nextProvenance);
        } catch {
          // SQL provenance is optional for completed analysis output.
        }
      } catch {
        if (!cancelled) {
          setAnalysisResultError(
            "Analysis details are unavailable. Refresh the conversation and try again.",
          );
        }
      }
    }

    void loadProvenance();
    return () => {
      cancelled = true;
    };
  }, [
    attachment.id,
    getAnalysisResult,
    getDatasetProvenance,
    isAnalysis,
    isProvenanceDataset,
  ]);

  if (attachment.relationship === "used") {
    return (
      <a
        className="message-used-file"
        href={
          attachment.origin_message_id
            ? `#message-${attachment.origin_message_id}`
            : undefined
        }
      >
        {name}
      </a>
    );
  }

  if (attachment.mime.startsWith("image/")) {
    return (
      <figure className="message-attachment message-attachment-image">
        <AuthenticatedArtifact
          alt={attachment.label || name}
          filename={downloadName}
          load={loadAttachment}
          mode="image"
        />
        <figcaption>
          <span>{attachment.label || name}</span>
          <AuthenticatedArtifact
            alt={`Download ${downloadName}`}
            filename={downloadName}
            load={loadAttachment}
            mode="download"
          />
        </figcaption>
      </figure>
    );
  }

  async function previewDataset() {
    setIsLoadingPreview(true);
    setError(null);
    try {
      setPreview(await getDatasetPreview(attachment.id, 100));
    } catch (loadError) {
      setError(
        loadError instanceof Error
          ? loadError.message
          : "Unable to load dataset preview.",
      );
    } finally {
      setIsLoadingPreview(false);
    }
  }

  async function loadDatasetSchema() {
    setIsLoadingSchema(true);
    setError(null);
    try {
      setSchema(await getDatasetSchema(attachment.id));
    } catch (loadError) {
      setError(
        loadError instanceof Error
          ? loadError.message
          : "Unable to load dataset schema.",
      );
    } finally {
      setIsLoadingSchema(false);
    }
  }

  function loadDatasetDetails() {
    const requests: Promise<void>[] = [];
    if (!preview && !isLoadingPreview) {
      requests.push(previewDataset());
    }
    if (!schema && !isLoadingSchema) {
      requests.push(loadDatasetSchema());
    }
    void Promise.all(requests);
  }

  async function copyCode(code: string) {
    try {
      await navigator.clipboard.writeText(code);
    } catch {
      return;
    }
    setCopiedCode(code);
  }

  const analysisLineage = analysisResult ? (
    <section className="analysis-output-lineage" aria-label="Analysis provenance">
      <p><strong>Method:</strong> {analysisResult.method}</p>
      <p>
        <strong>Dataset source:</strong>{" "}
        {analysisResult.dataset_source === "current_upload"
          ? "Current uploaded table"
          : "Earlier thread artifact"}
      </p>
      {analysisResult.python_code ? (
        <details open>
          <summary>Python code</summary>
          <CodeBlock
            actions={
              <button type="button" onClick={() => void copyCode(analysisResult.python_code)}>
                {copiedCode === analysisResult.python_code ? "Copied" : "Copy Python"}
              </button>
            }
            code={analysisResult.python_code}
            label="Generated Python"
            language="python"
          />
        </details>
      ) : null}
      {analysisResult.output_text ? (
        <details open>
          <summary>Output</summary>
          <CodeBlock
            actions={
              <button
                type="button"
                onClick={() => void copyCode(analysisResult.output_text)}
              >
                {copiedCode === analysisResult.output_text ? "Copied" : "Copy output"}
              </button>
            }
            code={analysisResult.output_text}
            label="Python output"
            language="text"
          />
        </details>
      ) : null}
      {analysisResult.dataset_source === "prior_artifact" ? (
        <details open>
          <summary>Dataset and SQL</summary>
          <p>
            Dataset used: {analysisResult.dataset.id} ({analysisResult.dataset.kind} v{analysisResult.dataset.version})
          </p>
          {analysisDatasetProvenance ? (
            <CodeBlock
              actions={
                <button type="button" onClick={() => void copyCode(analysisDatasetProvenance.sql)}>
                  {copiedCode === analysisDatasetProvenance.sql ? "Copied" : "Copy SQL"}
                </button>
              }
              code={analysisDatasetProvenance.sql}
              label="SQL used"
              language="sql"
            />
          ) : null}
        </details>
      ) : null}
    </section>
  ) : null;

  if (isAnalysis) {
    return (
      <article className="analysis-run-attachment">
        {analysisResultError ? <p className="dataset-error">{analysisResultError}</p> : null}
        {analysisLineage}
      </article>
    );
  }

  return (
    <article className="message-attachment-card">
      <strong>{name}</strong>
      <span>
        {[attachment.kind, formatBytes(attachment.byte_size)]
          .filter(Boolean)
          .join(" · ")}
      </span>
      <div className="message-attachment-actions">
        <AuthenticatedArtifact
          alt={isDataset ? "Download dataset" : `Download ${name}`}
          filename={attachment.filename || name}
          load={loadAttachment}
          mode="download"
        />
      </div>
      {error ? <p className="dataset-error">{error}</p> : null}
      {isDataset ? (
        <details
          className="dataset-details"
          onToggle={(event) => {
            if (event.currentTarget.open) loadDatasetDetails();
          }}
        >
          <summary>Dataset details</summary>
          {isLoadingPreview || isLoadingSchema ? (
            <p className="dataset-details-loading">Loading dataset details…</p>
          ) : null}
          {preview ? (
            <section className="dataset-preview">
              <h4>Dataset preview</h4>
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
                      <td key={column}>{formatCell(row[column])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
            </section>
          ) : null}
          {schema ? (
            <section className="dataset-schema">
              <h4>Schema</h4>
              <DatasetSchemaView schema={schema.schema} />
            </section>
          ) : null}
          {datasetProvenance ? (
            <section className="dataset-sql">
              <h4>SQL used</h4>
          <CodeBlock
            actions={
              <button type="button" onClick={() => void copyCode(datasetProvenance.sql)}>
                {copiedCode === datasetProvenance.sql ? "Copied" : "Copy SQL"}
              </button>
            }
            code={datasetProvenance.sql}
            label="Validated SQL"
            language="sql"
          />
            </section>
          ) : null}
        </details>
      ) : null}
    </article>
  );
}
