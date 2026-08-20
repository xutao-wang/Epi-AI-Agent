from __future__ import annotations

import re
from typing import Any

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from epi_agent.protocol import (
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    ToolTerminalControl,
)


AGENT_DECIDE_ANSWER = "__agent_decide__"
_MAX_OPTION_COUNT = 8
_AGENT_DELEGATION_OPTION_PATTERNS = (
    re.compile(r"\blet (?:the )?agent decide\b", re.IGNORECASE),
    re.compile(
        r"\byou (?:choose|decide)\b.*\b(?:data|evidence|defensible)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:the )?(?:agent|model) (?:choose|decide)\b",
        re.IGNORECASE,
    ),
)
class ClarificationOptionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=64)
    label: str = Field(max_length=500)

    @field_validator("id", "label")
    @classmethod
    def require_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class RequestClarificationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=2_000)
    reason: str = Field(default="", max_length=2_000)
    options: list[ClarificationOptionArguments] = Field(
        min_length=2,
        max_length=_MAX_OPTION_COUNT,
    )

    @field_validator("question")
    @classmethod
    def require_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_distinct_options(self) -> "RequestClarificationArguments":
        option_ids = [option.id.casefold() for option in self.options]
        option_labels = [option.label.casefold() for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("option ids must be unique")
        if len(option_labels) != len(set(option_labels)):
            raise ValueError("option labels must be unique")
        if any(
            _is_agent_delegation_option(option.label)
            for option in self.options
        ):
            raise ValueError(
                "options must be concrete choices; the UI supplies the single "
                "'Let the agent decide' choice"
            )
        return self


def _is_agent_delegation_option(label: str) -> bool:
    return any(
        pattern.search(label) is not None
        for pattern in _AGENT_DELEGATION_OPTION_PATTERNS
    )


class RequestClarificationTool:
    spec = ToolSpec(
        name="general-request_clarification",
        description=(
            "Pause for one user choice between two or more fixed, "
            "evidence-supported concrete options and resume this same reasoning "
            "loop. Do not include an agent-decision option: the UI provides the "
            "single standard 'Let the agent decide' choice."
        ),
        args_model=RequestClarificationArguments,
        read_only=True,
        interrupting=True,
    )

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del context
        question = str(arguments["question"])
        reason = str(arguments.get("reason") or "")
        options = [dict(option) for option in list(arguments["options"])]
        response = interrupt(
            {
                "type": "agent_clarification",
                "question": question,
                "reason": reason,
                "options": options,
            }
        )
        if isinstance(response, dict) and response.get("action") == "cancel":
            return ToolResult(
                message="Human cancelled the active clarification.",
                terminal_control=ToolTerminalControl(
                    status="cancelled",
                    reason="Human cancelled the active clarification.",
                ),
            )
        if not isinstance(response, dict) or response.get("action") != "answer":
            raise ToolExecutionError(
                "CLARIFICATION_RESPONSE_INVALID",
                "Clarification requires action=answer and a non-empty answer.",
                recoverable=True,
            )
        answer = str(response.get("answer") or "").strip()
        if not answer:
            raise ToolExecutionError(
                "CLARIFICATION_ANSWER_REQUIRED",
                "Clarification answer cannot be empty.",
                recoverable=True,
            )
        interrupt_id = str(response.get("_clarification_interrupt_id") or "").strip()
        if not interrupt_id:
            raise ToolExecutionError(
                "CLARIFICATION_INTERRUPT_ID_REQUIRED",
                "Clarification answer is missing its interrupt identifier.",
                recoverable=False,
            )

        exchange_answer = answer
        if answer == AGENT_DECIDE_ANSWER:
            labels = "; ".join(
                f"{option['id']}: {option['label']}" for option in options
            )
            message = (
                "The user delegated this choice. Choose exactly one offered "
                "option using the available evidence, then continue without "
                f"asking another clarification. Offered options: {labels}"
            )
            exchange_answer = "Let the agent decide."
        else:
            message = f"Human clarification answer: {answer}"

        return ToolResult(
            message=message,
            clarification_exchange={
                "interrupt_id": interrupt_id,
                "question": question,
                "reason": reason,
                "answer": exchange_answer,
            },
        )


__all__ = [
    "AGENT_DECIDE_ANSWER",
    "ClarificationOptionArguments",
    "RequestClarificationArguments",
    "RequestClarificationTool",
]
