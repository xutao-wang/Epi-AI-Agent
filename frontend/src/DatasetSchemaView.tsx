import CodeBlock from "./CodeBlock";
import type { DatasetColumnSchema, DatasetSchema } from "./types";

interface Props {
  schema: DatasetSchema;
}

function displayValue(value: unknown): string {
  if (typeof value === "string") {
    return value.trim();
  }
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function valueLabels(values: unknown): string[] {
  if (Array.isArray(values)) {
    return values.map(displayValue).filter(Boolean);
  }
  if (values && typeof values === "object") {
    return Object.entries(values).map(
      ([code, label]) => `${code} — ${displayValue(label)}`,
    );
  }
  const label = displayValue(values);
  return label ? [label] : [];
}

function contextLabels(field: DatasetColumnSchema): string[] {
  return [
    ["Section", field.section_context],
    ["Depends on", field.depends_on],
    ["Condition", field.condition],
  ]
    .map(([label, value]) => {
      const rendered = displayValue(value);
      return rendered ? `${label}: ${rendered}` : "";
    })
    .filter(Boolean);
}

function ListCell({ items }: { items: string[] }) {
  if (!items.length) {
    return <span className="dataset-schema-unavailable">—</span>;
  }
  return (
    <div className="dataset-schema-cell-list">
      {items.map((item, index) => (
        <div key={`${index}-${item}`}>{item}</div>
      ))}
    </div>
  );
}

export default function DatasetSchemaView({ schema }: Props) {
  const entries = Object.entries(schema);

  return (
    <div className="dataset-schema-view">
      <div className="dataset-schema-table-wrap">
        <table aria-label="Dataset schema" className="dataset-schema-table">
          <thead>
            <tr>
              <th>Column</th>
              <th>Description</th>
              <th>Storage type</th>
              <th>Allowed values</th>
              <th>Context and conditions</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([column, field]) => (
              <tr key={column}>
                <td className="dataset-schema-column">{column}</td>
                <td>
                  {field.description?.trim() || (
                    <span className="dataset-schema-unavailable">
                      Description unavailable
                    </span>
                  )}
                </td>
                <td>{field.dataType?.trim() || "Unknown"}</td>
                <td>
                  <ListCell items={valueLabels(field.values)} />
                </td>
                <td>
                  <ListCell items={contextLabels(field)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details className="dataset-schema-raw">
        <summary>Raw schema</summary>
        <CodeBlock code={JSON.stringify(schema, null, 2)} language="json" />
      </details>
    </div>
  );
}
