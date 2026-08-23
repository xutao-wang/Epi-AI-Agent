from __future__ import annotations

from typing import Literal, NotRequired, TypeAlias, TypedDict

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class ConversationEventInputBase(TypedDict):
    actor: str
    actor_role: str
    type: str
    user_turn_hash: str | None
    status: NotRequired[str]
    parent_event_id: NotRequired[str | None]
    resolved_by_event_id: NotRequired[str | None]
    superseded_by_event_id: NotRequired[str | None]


class ConversationEventBase(ConversationEventInputBase):
    event_id: str
    seq: int
    created_at: str


class UserEventInput(ConversationEventInputBase):
    type: Literal["user"]
    text: str


class UserEvent(ConversationEventBase):
    type: Literal["user"]
    text: str


class AssistantEventInput(ConversationEventInputBase):
    type: Literal["assistant"]
    text: str


class AssistantEvent(ConversationEventBase):
    type: Literal["assistant"]
    text: str


class ClarificationEventInput(ConversationEventInputBase):
    type: Literal["clarification"]
    text: str


class ClarificationEvent(ConversationEventBase):
    type: Literal["clarification"]
    text: str


class ClarificationExchangeEventInput(ConversationEventInputBase):
    type: Literal["clarification_exchange"]
    interrupt_id: str
    question: str
    reason: str
    answer: str


class ClarificationExchangeEvent(ConversationEventBase):
    type: Literal["clarification_exchange"]
    interrupt_id: str
    question: str
    reason: str
    answer: str


class RoutingDecisionEventInput(ConversationEventInputBase):
    type: Literal["routing_decision"]
    decision: str


class RoutingDecisionEvent(ConversationEventBase):
    type: Literal["routing_decision"]
    decision: str


class ToolCallEventInput(ConversationEventInputBase):
    type: Literal["tool_call"]
    tool: str
    args: dict[str, JsonValue]


class ToolCallEvent(ConversationEventBase):
    type: Literal["tool_call"]
    tool: str
    args: dict[str, JsonValue]


class ToolResultEventInput(ConversationEventInputBase):
    type: Literal["tool_result"]
    tool: str
    text: str
    artifact_id: str | None


class ToolResultEvent(ConversationEventBase):
    type: Literal["tool_result"]
    tool: str
    text: str
    artifact_id: str | None


class CodeEventInput(ConversationEventInputBase):
    type: Literal["code"]
    artifact_id: str
    text: str


class CodeEvent(ConversationEventBase):
    type: Literal["code"]
    artifact_id: str
    text: str


class ExecutionStartedEventInput(ConversationEventInputBase):
    type: Literal["execution_started"]
    text: str


class ExecutionStartedEvent(ConversationEventBase):
    type: Literal["execution_started"]
    text: str


class ExecutionFinishedEventInput(ConversationEventInputBase):
    type: Literal["execution_finished"]
    text: str
    artifact_id: NotRequired[str | None]


class ExecutionFinishedEvent(ConversationEventBase):
    type: Literal["execution_finished"]
    text: str
    artifact_id: str | None


class FigureEventInput(ConversationEventInputBase):
    type: Literal["figure"]
    artifact_id: str
    text: str


class FigureEvent(ConversationEventBase):
    type: Literal["figure"]
    artifact_id: str
    text: str


class AttachmentEventInput(ConversationEventInputBase):
    type: Literal["attachment"]
    artifact_id: str
    relationship: Literal["input", "used", "output"]


class AttachmentEvent(ConversationEventBase):
    type: Literal["attachment"]
    artifact_id: str
    relationship: Literal["input", "used", "output"]


class SqlEventInput(ConversationEventInputBase):
    type: Literal["sql"]
    artifact_id: str
    text: str


class SqlEvent(ConversationEventBase):
    type: Literal["sql"]
    artifact_id: str
    text: str


class ReviewRequestEventInput(ConversationEventInputBase):
    type: Literal["review_request"]
    review_kind: str
    text: str
    artifact_id: NotRequired[str | None]


class ReviewRequestEvent(ConversationEventBase):
    type: Literal["review_request"]
    review_kind: str
    text: str
    artifact_id: str | None


class ReviewDecisionEventInput(ConversationEventInputBase):
    type: Literal["review_decision"]
    review_kind: str
    decision: str
    text: str


class ReviewDecisionEvent(ConversationEventBase):
    type: Literal["review_decision"]
    review_kind: str
    decision: str
    text: str


class ErrorEventInput(ConversationEventInputBase):
    type: Literal["error"]
    text: str
    error: JsonObject


class ErrorEvent(ConversationEventBase):
    type: Literal["error"]
    text: str
    error: JsonObject


ConversationEventInput: TypeAlias = (
    UserEventInput
    | AssistantEventInput
    | ClarificationEventInput
    | ClarificationExchangeEventInput
    | RoutingDecisionEventInput
    | ToolCallEventInput
    | ToolResultEventInput
    | CodeEventInput
    | ExecutionStartedEventInput
    | ExecutionFinishedEventInput
    | FigureEventInput
    | AttachmentEventInput
    | SqlEventInput
    | ReviewRequestEventInput
    | ReviewDecisionEventInput
    | ErrorEventInput
)

class ConversationArtifactInput(TypedDict):
    kind: str
    producer: str
    mime: str | None
    summary: str
    content: JsonValue
    status: NotRequired[str]


class ConversationArtifactRecord(ConversationArtifactInput):
    artifact_id: str
    created_at: str


class ConversationArtifacts(TypedDict, total=False):
    conversation_events: list[JsonObject]
    conversation_events_version: int
    artifact_manifest_version: int
    files: dict[str, JsonObject]
    attachments: dict[str, JsonObject]


class ConversationMeta(TypedDict, total=False):
    next_event_seq: int
