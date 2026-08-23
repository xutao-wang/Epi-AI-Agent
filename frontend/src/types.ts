export type RunState =
  | "idle"
  | "running"
  | "interrupted"
  | "done"
  | "cancelled"
  | "error"
  | "timeout";

export type ActivityItemStatus = "running" | "completed" | "waiting";

export type ActivityRunState =
  | "running"
  | "waiting"
  | "completed"
  | "cancelled"
  | "error";

export interface ActivityItem {
  id: string;
  sequence: number;
  label: string;
  status: ActivityItemStatus;
  tool_name: string | null;
  tool_call_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ActivityRun {
  id: string;
  thread_id: string;
  user_message_id: string;
  state: ActivityRunState;
  activities: ActivityItem[];
  created_at: string;
  updated_at: string;
}

export interface RunStatus {
  state: RunState;
  steps: number;
  error: string | null;
  error_code?: string | null;
  user_message?: string | null;
  started_at?: number | null;
  updated_at?: number | null;
}

export interface RuntimeSettings {
  model_name: string;
  temperature: number | null;
  top_p: number | null;
  max_steps: number | null;
  timeout_seconds: number | null;
  db_rag_embedding_model: string;
  db_rag_reranker_model: string;
}

export type RuntimeInfo = RuntimeSettings;

export interface RuntimeCapability {
  status: "available" | "not_configured";
  message: string;
}

export interface RuntimeCapabilities {
  publication_knowledge: RuntimeCapability;
  db_rag_dataset: RuntimeCapability;
}

export type EmbeddingRetrievalMode =
  | "hybrid_vector_lexical"
  | "lexical_fallback";

export interface EmbeddingStartupStatus {
  profile_id: string;
  profile_label: string;
  provider: string;
  index_compatibility: string;
  available: boolean;
  retrieval_mode: EmbeddingRetrievalMode;
  reason_code: string | null;
  message: string;
  compatible_study_ids: string[];
  incompatible_study_ids: string[];
}

export type ModelProvider = "openai" | "anthropic" | "openai_compatible";

export interface ModelOption {
  id: string;
  label: string;
  provider: ModelProvider;
  provider_label: string;
  supports_sampling_controls: boolean;
  summary: string;
  initial_output_tokens: number;
  automatic_output_token_ceiling: number;
  user_output_token_increment: number;
  absolute_output_token_ceiling: number;
  request_timeout_seconds: number;
  workflow_timeout_seconds: number;
  automatic_output_cost: string | null;
  incremental_output_cost: string | null;
}

export interface RuntimeOptions {
  defaults: RuntimeSettings;
  models: ModelOption[];
  capabilities: RuntimeCapabilities;
  embedding_startup_status: EmbeddingStartupStatus;
}

export interface ConversationSummary {
  thread_id: string;
  title: string;
  title_source: "automatic" | "manual";
  model_name: string;
  created_at: string;
  updated_at: string;
  last_opened_at: string | null;
  archived_at: string | null;
  awaiting_review: boolean;
}

export interface ConversationAttachment {
  id: string;
  kind: string;
  label: string;
  filename: string;
  mime: string;
  byte_size: number | null;
  relationship: "input" | "used" | "output";
  origin_message_id: string | null;
}

export interface ClarificationExchange {
  interrupt_id: string;
  question: string;
  reason: string;
  answer: string;
}

export interface AttachmentManifestSummary {
  id: string;
  filename: string;
  kind: string;
  mime: string;
  byte_size: number;
  status: "staged" | "available";
}

export interface AttachmentUploadError {
  filename: string;
  code: string;
  message: string;
}

export interface AttachmentUploadResult {
  attachments: AttachmentManifestSummary[];
  errors: AttachmentUploadError[];
}

export interface ConversationMessage {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  status?: "cancelled" | null;
  created_at?: string | null;
  attachments?: ConversationAttachment[];
  clarifications?: ClarificationExchange[];
}

export interface DatasetPreview {
  dataset_id: string;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  row_count: number | null;
}

export interface DatasetSchemaResponse {
  dataset_id: string;
  schema: Record<string, unknown>;
}

export interface ProvenanceArtifactIdentity {
  id: string;
  kind: string;
  version: number;
}

export interface DatasetProvenance {
  dataset_id: string;
  dataset_version: number;
  sql: string;
  sql_artifact: ProvenanceArtifactIdentity;
  sql_sha256: string | null;
}

export interface CompletedAnalysisResult {
  analysis_run_id: string;
  analysis_run_version: number;
  method: string;
  python_code: string;
  output_text: string;
  dataset: ProvenanceArtifactIdentity;
  dataset_source: "current_upload" | "prior_artifact";
  dataset_source_reason: string;
  tables: AnalysisOutputIdentity[];
  figures: AnalysisOutputIdentity[];
}

export interface DatasetQualityWarning {
  code: string;
  severity: "low" | "medium" | "high";
  message: string;
}

export interface FileArtifactSummary {
  id: string;
  kind: string;
  label: string;
  mime: string;
  status?: string;
}

export interface ReviewColumn {
  key: string;
  source?: string;
  table: string;
  column: string;
  description?: string;
  roles: Array<
    "requested" | "identifier" | "grain" | "filter_support" | "linkage"
  >;
  required?: boolean;
  selected?: boolean;
  filters?: Array<Record<string, unknown>>;
}

export interface ReviewGroup {
  concept_id: string;
  concept_label: string;
  columns: ReviewColumn[];
  unresolved_reason?: string;
  kind?: "clinical" | "additional";
}

export interface ReviewRequiredField {
  key: string;
  source?: string;
  table: string;
  column: string;
  output_column?: string | null;
  purpose: string;
  roles: Array<
    "requested" | "identifier" | "grain" | "filter_support" | "linkage"
  >;
  label: string;
  required: boolean;
}

export interface ReviewFilterReference {
  source: string;
  table: string;
  column: string;
}

export interface ReviewFilterConstraint extends ReviewFilterReference {
  operator?: string;
  value?: string | number | boolean | null;
  values?: Array<string | number | boolean | null>;
}

export interface ReviewFilter {
  description?: string;
  predicate?: string;
  referenced_columns?: ReviewFilterReference[];
  selection_keys?: string[];
  value_constraints?: ReviewFilterConstraint[];
}

export interface ReviewRelationshipKeyPair {
  left_column: string;
  right_column: string;
}

export interface ReviewRelationshipWarning {
  code: string;
  label: string;
}

export interface ReviewRelationship {
  description: string;
  source: string;
  evidence_label: string;
  join_type: "inner" | "left";
  join_strategy_label: string;
  left_table: string;
  right_table: string;
  key_pairs: ReviewRelationshipKeyPair[];
  left_cardinality?: string;
  right_cardinality?: string;
  cardinality_label?: string;
  warnings?: ReviewRelationshipWarning[];
}

export interface ReviewDataLinkage {
  relationships: ReviewRelationship[];
}

export interface ReviewArtifactIdentity {
  id: string;
  kind: string;
  version: number;
  expected_status: string;
}

export interface DatasetPlanReviewView {
  dataset_title: string;
  goal: string;
  concept_groups: ReviewGroup[];
  selected_fields: string[];
  filters: ReviewFilter[];
  required_fields?: ReviewRequiredField[];
  joins: ReviewRelationship[];
  unresolved_scientific_choices: string[];
}

export interface DatasetReviewView {
  goal: string;
  dimensions: { rows: number | null; columns: number | null };
  columns: Array<Record<string, unknown>>;
  filters: ReviewFilter[];
  quality: Record<string, unknown>;
  warnings: DatasetQualityWarning[];
  provenance: {
    plan: { id: string; version: number };
    sql: { id: string; version: number };
    quality_report: { id: string; version: number };
  };
  feedback_history: Array<Record<string, unknown>>;
}

export interface AnalysisResultReviewView {
  method: string;
  dataset: { id: string; kind: string; version: number };
  specification: Record<string, unknown>;
  output_text: string;
  warnings: string[];
  warnings_truncated: boolean;
  runtime: Record<string, unknown>;
  tables: AnalysisOutputIdentity[];
  figures: AnalysisOutputIdentity[];
  feedback_history: Array<Record<string, unknown>>;
}

export interface AnalysisOutputIdentity {
  id: string;
  kind: "figure" | "table";
  version: number;
}

export interface ClarificationOption {
  id: string;
  label: string;
}

export const AGENT_DECIDE_ANSWER = "__agent_decide__";

export interface TablePreview {
  columns: string[];
  rows: Array<Record<string, string | null>>;
  row_count: number | null;
}

export interface ModelOutputLimitInterrupt {
  id: string;
  type: "model_output_limit";
  model_id: string;
  model_label: string;
  automatic_token_ceiling: number;
  continuation_tokens: number;
  additional_output_cost: string;
  message: string;
  actions: ["continue", "cancel"];
}

export type ActiveInterrupt =
  | {
      id: string;
      type: "dataset_plan_review";
      artifact: ReviewArtifactIdentity;
      view: DatasetPlanReviewView;
    }
  | {
      id: string;
      type: "dataset_review";
      artifact: ReviewArtifactIdentity;
      view: DatasetReviewView;
    }
  | {
      id: string;
      type: "analysis_result_review";
      artifact: ReviewArtifactIdentity;
      view: AnalysisResultReviewView;
    }
  | {
      id: string;
      type: "agent_clarification";
      question: string;
      reason: string;
      options: ClarificationOption[];
    }
  | ModelOutputLimitInterrupt;

export type ResumeInterruptPayload =
  | { action: "approve"; selected_column_keys?: string[] }
  | {
      action: "revise";
      feedback: string;
      selected_column_keys?: string[];
    }
  | { action: "cancel" }
  | { action: "continue" }
  | { action: "answer"; answer: string };

export interface ApiThreadState {
  thread_id: string;
  run: RunStatus;
  conversation: ConversationMessage[];
  activity_runs: ActivityRun[];
  active_interrupt: ActiveInterrupt | null;
  runtime_settings: RuntimeSettings | null;
  runtime_settings_locked: boolean;
  model_name?: string;
  model_label?: string;
  model_available?: boolean;
  model_replacement_required?: boolean;
  datasets: Array<{ id: string; label: string; row_count: number | null }>;
  file_artifacts: FileArtifactSummary[];
  output: Record<string, unknown>;
  diagnostics: Record<string, unknown>;
  embedding_startup_status: EmbeddingStartupStatus;
}
