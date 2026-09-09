from __future__ import annotations

from types import SimpleNamespace

from api.runtime import project_thread_state
from api.schemas import AgentUsage


def test_api_projects_sanitized_aggregate_agent_usage() -> None:
    snapshot = SimpleNamespace(
        values={
            "model_output_state": {
                "telemetry": [
                    {
                        "response_id": "generation-1",
                        "input_tokens": 125,
                        "output_tokens": 25,
                    }
                ],
                "aggregate_input_tokens": 125,
                "aggregate_output_tokens": 25,
            }
        },
        next=(),
        interrupts=[],
    )

    projected = project_thread_state(
        thread_id="thread-usage",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 1, "error": None},
        model_provider="openrouter",
        served_model_id="deepseek/deepseek-v4-pro-0813",
    )

    assert projected.agent_usage == AgentUsage(
        provider="openrouter",
        served_model_id="deepseek/deepseek-v4-pro-0813",
        input_tokens=125,
        output_tokens=25,
    )
    payload = projected.model_dump(mode="json")
    assert "telemetry" not in payload["agent_usage"]
    assert "response_id" not in payload["agent_usage"]


def test_api_omits_agent_usage_before_any_model_response() -> None:
    snapshot = SimpleNamespace(
        values={"model_output_state": {}},
        next=(),
        interrupts=[],
    )

    projected = project_thread_state(
        thread_id="thread-unused",
        snapshot=snapshot,
        run_status={"state": "idle", "steps": 0, "error": None},
        model_provider="openrouter",
        served_model_id="deepseek/deepseek-v4-pro-0813",
    )

    assert projected.agent_usage is None
