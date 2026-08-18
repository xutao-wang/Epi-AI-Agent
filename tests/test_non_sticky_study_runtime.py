from __future__ import annotations

import inspect

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from api.runtime import ReportAgentApiRuntime, _initial_graph_state
from api.schemas import SubmitMessageRequest
from epi_agent.agent import build_general_epi_agent_graph
from epi_agent.runtime import GenericEpiAgentState
from graph.builder import build_graph


def test_submit_message_schema_rejects_removed_active_study_field() -> None:
    with pytest.raises(ValidationError):
        SubmitMessageRequest.model_validate(
            {"text": "Use study two", "active_study_id": "study-two"}
        )


def test_initial_graph_state_never_contains_active_study() -> None:
    state = _initial_graph_state(
        "thread-1",
        HumanMessage(content="Use study two"),
    )

    assert "active_study_id" not in state


def test_runtime_and_graph_signatures_have_no_sticky_study_selector() -> None:
    assert "active_study_id" not in GenericEpiAgentState.__annotations__
    assert "active_study_id" not in inspect.signature(
        ReportAgentApiRuntime.submit_message
    ).parameters
    assert "default_study_id" not in inspect.signature(build_graph).parameters
    assert "default_study_id" not in inspect.signature(
        build_general_epi_agent_graph
    ).parameters
