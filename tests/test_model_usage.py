from __future__ import annotations

from langchain_core.messages import AIMessage

from epi_agent.model_responses import observe_model_response
from epi_agent.runtime import _record_model_observation


def test_observer_preserves_openrouter_tokens_and_actual_cost() -> None:
    message = AIMessage(
        content="answer",
        response_metadata={
            "id": "generation-1",
            "model_name": "deepseek/deepseek-v4-pro-0813",
            "token_usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "cost": 0.0042,
            },
        },
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        },
    )

    observed = observe_model_response(message)

    assert observed.input_tokens == 120
    assert observed.output_tokens == 30
    assert observed.actual_cost_usd == 0.0042


def test_observer_keeps_missing_usage_as_missing() -> None:
    observed = observe_model_response(AIMessage(content="answer"))

    assert observed.input_tokens is None
    assert observed.output_tokens is None
    assert observed.reasoning_tokens is None
    assert observed.actual_cost_usd is None


def _message(input_tokens: int, output_tokens: int, cost: float) -> AIMessage:
    return AIMessage(
        content="segment",
        response_metadata={"token_usage": {"cost": cost}},
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def test_runtime_aggregates_complete_openrouter_usage() -> None:
    state, _ = _record_model_observation(
        {},
        _message(100, 20, 0.002),
        duration_ms=10,
        provider="openrouter",
        served_model_id="vendor/model",
    )
    state, _ = _record_model_observation(
        state,
        _message(50, 10, 0.001),
        duration_ms=11,
        provider="openrouter",
        served_model_id="vendor/model",
    )

    assert state["aggregate_input_tokens"] == 150
    assert state["aggregate_output_tokens"] == 30
    assert state["aggregate_actual_openrouter_cost_usd"] == 0.003


def test_one_missing_segment_marks_aggregate_usage_incomplete() -> None:
    state, _ = _record_model_observation(
        {},
        _message(100, 20, 0.002),
        duration_ms=10,
        provider="openrouter",
        served_model_id="vendor/model",
    )
    state, _ = _record_model_observation(
        state,
        AIMessage(content="segment"),
        duration_ms=11,
        provider="openrouter",
        served_model_id="vendor/model",
    )

    assert state["aggregate_input_tokens"] is None
    assert state["aggregate_output_tokens"] is None
    assert state["aggregate_actual_openrouter_cost_usd"] is None
