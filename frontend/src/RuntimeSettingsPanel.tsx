import type { RuntimeOptions, RuntimeSettings } from "./types";
import { useEffect, useState } from "react";

interface Props {
  locked: boolean;
  onChange(settings: RuntimeSettings): void;
  options: RuntimeOptions | null;
  settings: RuntimeSettings;
}

const numericFields = {
  temperature: "Temperature",
  top_p: "Top probability",
  max_steps: "Auto-run steps",
  timeout_seconds: "Workflow timeout",
} as const satisfies Record<
  "temperature" | "top_p" | "max_steps" | "timeout_seconds",
  string
>;

type NumericField = keyof typeof numericFields;

function capabilityLabel(status: "available" | "not_configured") {
  return status === "available" ? "Available" : "Not configured";
}

function numericValue(value: number | null) {
  return value ?? "";
}

export default function RuntimeSettingsPanel({
  locked,
  onChange,
  options,
  settings,
}: Props) {
  const modelOptions = options?.models ?? [];
  const [selectedModelName, setSelectedModelName] = useState(
    settings.model_name,
  );
  useEffect(() => {
    setSelectedModelName(settings.model_name);
  }, [settings.model_name]);
  const selectedModel = modelOptions.find(
    (model) => model.id === selectedModelName,
  );
  const modelValue = selectedModel ? selectedModelName : "";
  const supportsSamplingControls =
    selectedModel?.supports_sampling_controls ?? false;
  const providerGroups: Array<{ label: string; models: typeof modelOptions }> =
    [];
  for (const model of modelOptions) {
    const groupLabel = model.provider_label || "Models";
    const group = providerGroups.find((entry) => entry.label === groupLabel);
    if (group) {
      group.models.push(model);
    } else {
      providerGroups.push({ label: groupLabel, models: [model] });
    }
  }
  const controlsDisabled = locked;

  function updateSettings(update: Partial<RuntimeSettings>) {
    onChange({ ...settings, ...update });
  }

  function handleNumberChange(field: NumericField, value: string) {
    if (value === "") {
      updateSettings({ [field]: null });
      return;
    }

    const parsed = Number(value);
    if (!Number.isFinite(parsed)) {
      return;
    }
    if (field === "max_steps" && !Number.isInteger(parsed)) {
      return;
    }

    updateSettings({ [field]: parsed });
  }

  function handleModelChange(modelName: string) {
    const selected = modelOptions.find((model) => model.id === modelName);
    setSelectedModelName(modelName);
    updateSettings({
      model_name: modelName,
      ...(selected && !selected.supports_sampling_controls
        ? { temperature: null, top_p: null }
        : {}),
    });
  }

  return (
    <section className="runtime-settings-panel" aria-labelledby="runtime-settings-title">
      <h2 id="runtime-settings-title">Model Settings</h2>
      {locked ? (
        <p className="runtime-settings-lock">Settings are locked for this thread.</p>
      ) : null}

      <label>
        <span>Model</span>
        <select
          disabled={controlsDisabled || modelOptions.length === 0}
          onChange={(event) => handleModelChange(event.target.value)}
          value={modelValue}
        >
          <option disabled value="">
            Select model
          </option>
          {providerGroups.map((group) => (
            <optgroup key={group.label} label={group.label}>
              {group.models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>

      {Object.entries(numericFields)
        .filter(
          ([field]) =>
            supportsSamplingControls ||
            (field !== "temperature" && field !== "top_p"),
        )
        .map(([field, label]) => (
        <label key={field}>
          <span>{label}</span>
          <input
            disabled={controlsDisabled}
            onChange={(event) =>
              handleNumberChange(field as NumericField, event.target.value)
            }
            inputMode={field === "max_steps" ? "numeric" : "decimal"}
            type="text"
            value={numericValue(settings[field as NumericField])}
          />
        </label>
        ))}
      {!supportsSamplingControls ? (
        <p>Sampling controls are unavailable for this model.</p>
      ) : null}

      <dl className="settings-list">
        {options ? (
          <>
            <div>
              <dt>Publication knowledge</dt>
              <dd>
                {`Publication knowledge: ${capabilityLabel(
                  options.capabilities.publication_knowledge.status,
                )}`}
              </dd>
            </div>
            <div>
              <dt>DB-RAG dataset</dt>
              <dd>
                {`DB-RAG dataset: ${capabilityLabel(
                  options.capabilities.db_rag_dataset.status,
                )}`}
                {options.capabilities.db_rag_dataset.status ===
                  "not_configured" &&
                options.capabilities.db_rag_dataset.message ? (
                  <small>{options.capabilities.db_rag_dataset.message}</small>
                ) : null}
              </dd>
            </div>
          </>
        ) : null}
        <div>
          <dt>Embedding model</dt>
          <dd>{settings.db_rag_embedding_model || "Unavailable"}</dd>
        </div>
        <div>
          <dt>Reranker model</dt>
          <dd>{settings.db_rag_reranker_model || "Unavailable"}</dd>
        </div>
      </dl>
    </section>
  );
}
