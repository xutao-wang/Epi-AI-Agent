from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel
from utils.user_storage import ThreadStorageScope

if TYPE_CHECKING:
    from epi_agent.studies import StudyBundle


_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    read_only: bool = True
    interrupting: bool = False

    def __post_init__(self) -> None:
        if _TOOL_NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError(
                "Tool name must match ^[a-zA-Z0-9_-]{1,64}$"
            )

    def model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    kind: str
    version: int


@dataclass(frozen=True)
class ToolTerminalControl:
    status: Literal["cancelled", "completed"]
    reason: str


@dataclass(frozen=True)
class ToolResult:
    message: str
    artifacts: tuple[ArtifactRef, ...] = ()
    output_artifacts: tuple[ArtifactRef, ...] = ()
    terminal_control: ToolTerminalControl | None = None
    review_feedback_entry: dict[str, Any] | None = None
    clarification_exchange: dict[str, str] | None = None


_MAX_MODEL_TOOL_MESSAGE_CHARS = 12_000
_MAX_MODEL_ARTIFACT_REFS = 100
_MAX_MODEL_ARTIFACT_REF_FIELD_CHARS = 512
_MAX_MODEL_ARTIFACT_VERSION = (2**63) - 1


def _model_artifact_ref(reference: ArtifactRef) -> dict[str, Any] | None:
    if (
        not isinstance(reference.id, str)
        or not reference.id
        or len(reference.id) > _MAX_MODEL_ARTIFACT_REF_FIELD_CHARS
        or not isinstance(reference.kind, str)
        or not reference.kind
        or len(reference.kind) > _MAX_MODEL_ARTIFACT_REF_FIELD_CHARS
        or type(reference.version) is not int
        or reference.version < 1
        or reference.version > _MAX_MODEL_ARTIFACT_VERSION
    ):
        return None
    return {
        "id": reference.id,
        "kind": reference.kind,
        "version": reference.version,
    }


def _serialize_model_tool_payload(
    *,
    artifacts: list[dict[str, Any]],
    message: str,
) -> str:
    return json.dumps(
        {
            "artifacts": artifacts,
            "message": message,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def serialize_tool_result(result: ToolResult) -> str:
    """Render a bounded model observation without opening artifact contents."""

    artifacts: list[dict[str, Any]] = []
    for reference in result.artifacts[:_MAX_MODEL_ARTIFACT_REFS]:
        candidate = _model_artifact_ref(reference)
        if candidate is None:
            continue
        serialized = _serialize_model_tool_payload(
            artifacts=[*artifacts, candidate],
            message="...",
        )
        if len(serialized) <= _MAX_MODEL_TOOL_MESSAGE_CHARS:
            artifacts.append(candidate)

    message_limit = min(
        len(result.message),
        _MAX_MODEL_TOOL_MESSAGE_CHARS,
    )
    message = result.message[:message_limit]
    if message_limit < len(result.message):
        message += "..."
    serialized = _serialize_model_tool_payload(
        artifacts=artifacts,
        message=message,
    )
    if len(serialized) <= _MAX_MODEL_TOOL_MESSAGE_CHARS:
        return serialized

    low = 0
    high = message_limit
    bounded = _serialize_model_tool_payload(
        artifacts=artifacts,
        message="...",
    )
    while low <= high:
        midpoint = (low + high) // 2
        message = result.message[:midpoint]
        if midpoint < len(result.message):
            message += "..."
        candidate = _serialize_model_tool_payload(
            artifacts=artifacts,
            message=message,
        )
        if len(candidate) <= _MAX_MODEL_TOOL_MESSAGE_CHARS:
            bounded = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return bounded


class ToolExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.recoverable = recoverable
        self.details = (
            json.loads(json.dumps(details, allow_nan=False))
            if details is not None
            else None
        )
        super().__init__(message)


class ArtifactStore(Protocol):
    def require(self, reference: ArtifactRef | str) -> Any: ...

    def list_artifacts(self, *, kind: str | None = None) -> list[Any]: ...

    def save_artifact(
        self,
        *,
        kind: str,
        content: dict[str, Any],
        mime: str = "application/json",
        status: str = "active",
        version: int = 1,
        provenance: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> ArtifactRef: ...

    def save_dataset_plan(
        self,
        plan: Any,
        *,
        status: str = "draft",
        prior_id: str | None = None,
        prior_version: int | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    def save_dataset(
        self,
        artifact: dict[str, Any],
        *,
        make_active: bool = False,
    ) -> ArtifactRef: ...

    def save_replacement_dataset(
        self,
        artifact: dict[str, Any],
        *,
        predecessor_ref: ArtifactRef,
        plan_ref: ArtifactRef,
        feedback_ref: ArtifactRef,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    def get_dataset_persistence_attempt(
        self,
        dataset_id: str,
    ) -> dict[str, Any] | None: ...

    def begin_dataset_persistence_attempt(
        self,
        attempt: dict[str, Any],
    ) -> dict[str, Any]: ...

    def advance_dataset_persistence_attempt(
        self,
        dataset_id: str,
        *,
        lineage: dict[str, Any],
        expected_state: str,
        state: str,
        manifest: dict[str, Any] | None = None,
        dataset: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def commit_dataset_persistence_attempt(
        self,
        dataset_id: str,
        *,
        lineage: dict[str, Any],
        artifact: dict[str, Any],
        plan_ref: ArtifactRef,
        predecessor_ref: ArtifactRef | None = None,
        feedback_ref: ArtifactRef | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    def activate_dataset(
        self,
        reference: ArtifactRef,
        *,
        expected_status: str = "pending_review",
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    def transition_artifact_status(
        self,
        reference: ArtifactRef,
        *,
        expected_status: str,
        status: str,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    def transition_artifact_statuses(
        self,
        references: tuple[ArtifactRef, ...],
        *,
        expected_status: str,
        status: str,
        provenance: dict[str, Any] | None = None,
    ) -> tuple[ArtifactRef, ...]: ...

    def snapshot(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ToolContext:
    study: StudyBundle | None
    artifact_store: ArtifactStore
    thread_id: str
    policy: Any
    thread_storage: ThreadStorageScope | None = None
    attachment_store: Any | None = None
    authorized_attachment_ids: tuple[str, ...] = ()
    current_attachment_ids: tuple[str, ...] = ()
    analysis_review_feedback_history: tuple[dict[str, Any], ...] = ()
    available_study_ids: tuple[str, ...] = ()


def require_context_study(context: ToolContext) -> StudyBundle:
    if context.study is not None:
        return context.study
    if context.available_study_ids:
        raise ToolExecutionError(
            "ACTIVE_STUDY_SELECTION_REQUIRED",
            "Select an active study package before using this tool.",
            recoverable=True,
            details={"available_study_ids": list(context.available_study_ids)},
        )
    raise ToolExecutionError(
        "NO_STUDY_PACKAGE_INSTALLED",
        "No study package is installed.",
        recoverable=True,
    )


class AgentTool(Protocol):
    spec: ToolSpec

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult: ...
