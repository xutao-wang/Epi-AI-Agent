from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from db_rag.embedding_startup import (
    EmbeddingStartupStatus,
    silent_embedding_startup_status,
)


RunState = Literal[
    "idle",
    "running",
    "interrupted",
    "done",
    "cancelled",
    "error",
    "timeout",
]


class RunStatus(BaseModel):
    state: RunState
    steps: int = 0
    error: str | None = None
    error_code: str | None = None
    user_message: str | None = None
    started_at: float | None = None
    updated_at: float | None = None


class RuntimeInfo(BaseModel):
    model_name: str = ""
    temperature: float | None = None
    top_p: float | None = None
    max_steps: int | None = None
    timeout_seconds: float | None = None
    db_rag_embedding_model: str = ""
    db_rag_reranker_model: str = ""


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str = ""
    temperature: float | None = None
    top_p: float | None = None
    max_steps: int | None = None
    timeout_seconds: float | None = None
    db_rag_embedding_model: str = ""
    db_rag_reranker_model: str = ""


class RuntimeCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "not_configured"]
    message: str


class RuntimeCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication_knowledge: RuntimeCapability
    db_rag_dataset: RuntimeCapability
    study_design: RuntimeCapability = Field(
        default_factory=lambda: RuntimeCapability(
            status="available",
            message="Study design knowledge is available.",
        )
    )


class ModelOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    provider: Literal["openai", "anthropic", "openai_compatible"] = "openai"
    provider_label: str = "OpenAI"
    supports_sampling_controls: bool
    summary: str
    initial_output_tokens: int = Field(gt=0)
    automatic_output_token_ceiling: int = Field(gt=0)
    user_output_token_increment: int = Field(gt=0)
    absolute_output_token_ceiling: int = Field(gt=0)
    request_timeout_seconds: int = Field(gt=0)
    workflow_timeout_seconds: int = Field(gt=0)
    automatic_output_cost: str | None = None
    incremental_output_cost: str | None = None


class RuntimeOptions(BaseModel):
    defaults: RuntimeSettings
    models: list[ModelOption] = Field(default_factory=list)
    capabilities: RuntimeCapabilities
    embedding_startup_status: EmbeddingStartupStatus = Field(
        default_factory=silent_embedding_startup_status
    )


class ConversationAttachment(BaseModel):
    id: str
    kind: str = ""
    label: str = ""
    filename: str = ""
    mime: str = ""
    byte_size: int | None = None
    relationship: Literal["input", "used", "output"]
    origin_message_id: str | None = None


class ClarificationExchange(BaseModel):
    interrupt_id: str
    question: str
    reason: str = ""
    answer: str


class ConversationMessage(BaseModel):
    id: str
    role: Literal["user", "assistant", "system"]
    text: str
    status: Literal["cancelled"] | None = None
    created_at: str | None = None
    attachments: list[ConversationAttachment] = Field(default_factory=list)
    clarifications: list[ClarificationExchange] = Field(default_factory=list)


class AttachmentManifestSummary(BaseModel):
    id: str
    filename: str
    kind: str
    mime: str
    byte_size: int
    status: Literal["staged", "available"]


class AttachmentUploadError(BaseModel):
    filename: str
    code: str
    message: str


class AttachmentUploadResult(BaseModel):
    attachments: list[AttachmentManifestSummary] = Field(default_factory=list)
    errors: list[AttachmentUploadError] = Field(default_factory=list)


class ReviewArtifactIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    version: int = Field(ge=1)
    expected_status: str = Field(min_length=1)


class DatasetPlanReviewView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_title: str
    goal: str
    concept_groups: list[dict[str, Any]]
    selected_fields: list[str]
    filters: list[dict[str, Any]]
    required_fields: list[dict[str, Any]] = Field(default_factory=list)
    joins: list[dict[str, Any]]
    unresolved_scientific_choices: list[str]


class ReviewQualityWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["low", "medium", "high"]
    message: str


class DatasetReviewView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    dimensions: dict[str, int | None]
    columns: list[dict[str, Any]]
    filters: list[dict[str, Any]]
    quality: dict[str, Any]
    warnings: list[ReviewQualityWarning]
    provenance: dict[str, dict[str, Any]]
    feedback_history: list[dict[str, Any]]


class AnalysisOutputIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    kind: Literal["figure", "table"]
    version: int = Field(ge=1)


class ProvenanceArtifactIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    version: int = Field(ge=1)


class DatasetProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    dataset_id: str = Field(min_length=1)
    dataset_version: int = Field(ge=1)
    sql: str = Field(min_length=1)
    sql_artifact: ProvenanceArtifactIdentity
    sql_sha256: str | None = None


class CompletedAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    analysis_run_id: str = Field(min_length=1)
    analysis_run_version: int = Field(ge=1)
    method: str = Field(min_length=1)
    python_code: str = ""
    output_text: str = ""
    dataset: ProvenanceArtifactIdentity
    dataset_source: Literal["current_upload", "prior_artifact"] = "prior_artifact"
    dataset_source_reason: str = ""
    tables: list[AnalysisOutputIdentity] = Field(default_factory=list)
    figures: list[AnalysisOutputIdentity] = Field(default_factory=list)


class AnalysisResultReviewView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    dataset: dict[str, Any]
    specification: dict[str, Any]
    output_text: str
    warnings: list[str]
    warnings_truncated: bool
    runtime: dict[str, Any]
    tables: list[AnalysisOutputIdentity] = Field(default_factory=list)
    figures: list[AnalysisOutputIdentity] = Field(default_factory=list)
    feedback_history: list[dict[str, Any]]


class TablePreview(BaseModel):
    columns: list[str]
    rows: list[dict[str, str | None]]
    row_count: int | None


class DatasetPlanReviewInterrupt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["dataset_plan_review"]
    artifact: ReviewArtifactIdentity
    view: DatasetPlanReviewView


class DatasetReviewInterrupt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["dataset_review"]
    artifact: ReviewArtifactIdentity
    view: DatasetReviewView


class AnalysisResultReviewInterrupt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["analysis_result_review"]
    artifact: ReviewArtifactIdentity
    view: AnalysisResultReviewView


class ClarificationOption(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=500)

    @field_validator("id", "label")
    @classmethod
    def strip_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class AgentClarificationInterrupt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    type: Literal["agent_clarification"]
    question: str = Field(max_length=2_000)
    reason: str = Field(default="", max_length=2_000)
    options: list[ClarificationOption] = Field(min_length=2, max_length=8)

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_distinct_options(self) -> "AgentClarificationInterrupt":
        option_ids = [option.id.casefold() for option in self.options]
        option_labels = [option.label.casefold() for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("clarification option ids must be unique")
        if len(option_labels) != len(set(option_labels)):
            raise ValueError("clarification option labels must be unique")
        return self


class ModelOutputLimitInterrupt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    type: Literal["model_output_limit"]
    model_id: str = Field(min_length=1)
    model_label: str = Field(min_length=1)
    automatic_token_ceiling: int = Field(gt=0)
    continuation_tokens: int = Field(gt=0)
    additional_output_cost: str = Field(pattern=r"^(\$\d+\.\d{2}|unknown)$")
    message: str = Field(min_length=1, max_length=2_000)
    actions: tuple[Literal["continue"], Literal["cancel"]]

    @field_validator("actions", mode="before")
    @classmethod
    def normalize_json_actions(cls, value: Any) -> Any:
        if isinstance(value, list):
            return tuple(value)
        return value


ActiveInterrupt = Annotated[
    DatasetPlanReviewInterrupt
    | DatasetReviewInterrupt
    | AnalysisResultReviewInterrupt
    | AgentClarificationInterrupt
    | ModelOutputLimitInterrupt,
    Field(discriminator="type"),
]


class DatasetSummary(BaseModel):
    id: str
    label: str = ""
    row_count: int | None = None


class FileArtifactSummary(BaseModel):
    id: str
    kind: str = ""
    label: str = ""
    mime: str = ""
    status: str = ""


class DatasetPreview(BaseModel):
    dataset_id: str
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int | None = None


class DatasetSchemaResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    dataset_id: str
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")


ActivityItemStatus = Literal["running", "completed", "waiting"]
ActivityRunState = Literal[
    "running",
    "waiting",
    "completed",
    "cancelled",
    "error",
]


class ActivityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=200)
    status: ActivityItemStatus
    tool_name: str | None = Field(default=None, max_length=200)
    tool_call_id: str | None = Field(default=None, max_length=200)
    created_at: str
    updated_at: str


class ActivityRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    user_message_id: str = Field(min_length=1)
    state: ActivityRunState
    activities: list[ActivityItem] = Field(default_factory=list)
    created_at: str
    updated_at: str


class ApiThreadState(BaseModel):
    thread_id: str
    run: RunStatus
    conversation: list[ConversationMessage] = Field(default_factory=list)
    activity_runs: list[ActivityRun] = Field(default_factory=list)
    active_interrupt: ActiveInterrupt | None = None
    datasets: list[DatasetSummary] = Field(default_factory=list)
    file_artifacts: list[FileArtifactSummary] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    runtime_settings: RuntimeSettings | None = None
    runtime_settings_locked: bool = False
    model_name: str = ""
    model_label: str = ""
    model_available: bool = True
    model_replacement_required: bool = False
    embedding_startup_status: EmbeddingStartupStatus = Field(
        default_factory=silent_embedding_startup_status
    )


class CreateThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str | None = None


class ConversationSummary(BaseModel):
    thread_id: str
    title: str
    title_source: Literal["automatic", "manual"]
    model_name: str
    created_at: str
    updated_at: str
    last_opened_at: str | None = None
    archived_at: str | None = None
    awaiting_review: bool = False


class ConversationHistoryResponse(BaseModel):
    items: list[ConversationSummary] = Field(default_factory=list)


class RenameConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)


class CreateThreadResponse(BaseModel):
    thread_id: str


class SubmitMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    model_name: str | None = None

    @model_validator(mode="after")
    def require_content(self) -> "SubmitMessageRequest":
        self.text = self.text.strip()
        if not self.text and not self.attachment_ids:
            raise ValueError("text or attachment_ids is required")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("attachment_ids must be unique")
        return self


class ResumeInterruptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "revise", "cancel", "answer", "continue"]
    selected_column_keys: list[str] | None = None
    feedback: str | None = None
    answer: str | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "ResumeInterruptRequest":
        if self.action == "revise":
            self.feedback = str(self.feedback or "").strip()
            if not self.feedback:
                raise ValueError("feedback is required for revise")
        elif self.feedback is not None:
            raise ValueError("feedback is accepted only for revise")
        if self.action == "answer":
            self.answer = str(self.answer or "").strip()
            if not self.answer:
                raise ValueError("answer is required for clarification")
        elif self.answer is not None:
            raise ValueError("answer is accepted only for clarification")
        return self


class ResetThreadResponse(BaseModel):
    thread_id: str
