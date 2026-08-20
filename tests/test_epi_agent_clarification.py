from __future__ import annotations

from typing import Any

import pytest

from epi_agent.protocol import ToolExecutionError, ToolTerminalControl
from epi_agent.tool_packs.general.clarification import (
    AGENT_DECIDE_ANSWER,
)
from epi_agent.tool_packs.general.tools import build_general_tool_registry


OPTIONS = [
    {"id": "any", "label": "Any missed dose during follow-up"},
    {"id": "total", "label": "Total missed doses during follow-up"},
]


def _arguments(**overrides: Any) -> dict[str, Any]:
    return {
        "question": "Which follow-up summary should be used?",
        "reason": "Each participant has multiple visits.",
        "options": OPTIONS,
        **overrides,
    }


def test_shared_clarification_interrupts_with_itemized_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, Any]] = []

    def fake_interrupt(payload: dict[str, Any]) -> dict[str, str]:
        payloads.append(payload)
        return {
            "action": "answer",
            "answer": "Any missed dose during follow-up",
            "_clarification_interrupt_id": "interrupt-1",
        }

    monkeypatch.setattr(
        "epi_agent.tool_packs.general.clarification.interrupt",
        fake_interrupt,
    )

    result = build_general_tool_registry().invoke(
        "general-request_clarification",
        _arguments(),
        context=None,  # type: ignore[arg-type]
    )

    assert payloads == [
        {
            "type": "agent_clarification",
            "question": "Which follow-up summary should be used?",
            "reason": "Each participant has multiple visits.",
            "options": OPTIONS,
        }
    ]
    assert result.message == (
        "Human clarification answer: Any missed dose during follow-up"
    )
    assert result.clarification_exchange == {
        "interrupt_id": "interrupt-1",
        "question": "Which follow-up summary should be used?",
        "reason": "Each participant has multiple visits.",
        "answer": "Any missed dose during follow-up",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        _arguments(options=[OPTIONS[0]]),
        _arguments(options=[OPTIONS[0], {"id": "any", "label": "Total"}]),
        _arguments(
            options=[
                OPTIONS[0],
                {"id": "total", "label": OPTIONS[0]["label"]},
            ]
        ),
        _arguments(question=" "),
        _arguments(
            options=[
                OPTIONS[0],
                {
                    "id": "agent-choice",
                    "label": (
                        "You choose the most defensible option based on "
                        "available data"
                    ),
                },
            ]
        ),
        _arguments(unexpected="value"),
    ],
)
def test_shared_clarification_rejects_invalid_option_contract(
    arguments: dict[str, Any],
) -> None:
    with pytest.raises(ToolExecutionError, match="Invalid arguments"):
        build_general_tool_registry().invoke(
            "general-request_clarification",
            arguments,
            context=None,  # type: ignore[arg-type]
        )


def test_shared_clarification_delegate_instructs_model_to_choose_one_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "epi_agent.tool_packs.general.clarification.interrupt",
        lambda _payload: {
            "action": "answer",
            "answer": AGENT_DECIDE_ANSWER,
            "_clarification_interrupt_id": "interrupt-1",
        },
    )

    result = build_general_tool_registry().invoke(
        "general-request_clarification",
        _arguments(),
        context=None,  # type: ignore[arg-type]
    )

    assert "Choose exactly one offered option" in result.message
    assert OPTIONS[0]["label"] in result.message
    assert OPTIONS[1]["label"] in result.message
    assert result.clarification_exchange is not None
    assert result.clarification_exchange["answer"] == "Let the agent decide."


def test_shared_clarification_cancel_keeps_existing_terminal_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "epi_agent.tool_packs.general.clarification.interrupt",
        lambda _payload: {"action": "cancel"},
    )

    result = build_general_tool_registry().invoke(
        "general-request_clarification",
        _arguments(),
        context=None,  # type: ignore[arg-type]
    )

    assert result.terminal_control == ToolTerminalControl(
        status="cancelled",
        reason="Human cancelled the active clarification.",
    )


@pytest.mark.parametrize(
    "former_keyword",
    [
        "runtime",
        "catalog",
        "schema",
        "table",
        "column",
        "field match",
        "join key",
        "linkage field",
        "identifier",
        "foreign key",
    ],
)
def test_shared_clarification_does_not_classify_prose_from_keywords(
    monkeypatch: pytest.MonkeyPatch,
    former_keyword: str,
) -> None:
    payloads: list[dict[str, Any]] = []

    def fake_interrupt(payload: dict[str, Any]) -> dict[str, str]:
        payloads.append(payload)
        return {
            "action": "answer",
            "answer": OPTIONS[0]["label"],
            "_clarification_interrupt_id": "interrupt-1",
        }

    monkeypatch.setattr(
        "epi_agent.tool_packs.general.clarification.interrupt",
        fake_interrupt,
    )
    arguments = _arguments(
        reason=f"The scientific description uses {former_keyword} wording."
    )

    result = build_general_tool_registry().invoke(
        "general-request_clarification",
        arguments,
        context=None,  # type: ignore[arg-type]
    )

    assert payloads == [
        {
            "type": "agent_clarification",
            "question": arguments["question"],
            "reason": arguments["reason"],
            "options": OPTIONS,
        }
    ]
    assert result.message == f"Human clarification answer: {OPTIONS[0]['label']}"
