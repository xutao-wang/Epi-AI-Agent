from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import time
from typing import Any

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel
import pytest

from api.app import build_application
from api.auth import LOCAL_SESSION_ID
from api.runtime import graph_config
from epi_agent.protocol import ToolExecutionError, ToolResult, ToolSpec
from epi_agent.registry import ToolRegistry
from epi_agent.tool_call_protocol import error_tool_message
from llm_vllm import build_chat_llm
from utils.model_availability import ModelAvailability
from utils.model_runtime_profiles import (
    ModelRuntimeProfile,
    PROVIDER_OPENAI_COMPATIBLE,
    load_custom_model_profiles,
    model_runtime_profile,
)


REPO_ROOT = Path(__file__).parents[1]
MINIMAX_MODEL_ID = "openrouter:minimax/minimax-m3"
GEMMA_MODEL_ID = "vllm:google/gemma-4-31B-it"
GEMMA_SERVED_MODEL_ID = "google/gemma-4-31B-it"
DEFAULT_GEMMA_BASE_URL = "http://127.0.0.1:8000/v1"
FAILED_QUESTION = (
    "Create a household-contact dataset with age, sex, household size, TST "
    "result, and IGRA result. Report the prevalence of positive TB infection "
    "tests."
)
LOCAL_HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}


class _NestedPlan(BaseModel):
    concepts: list[str]


class _NestedPlanArguments(BaseModel):
    plan: _NestedPlan


class _NestedPlanTool:
    spec = ToolSpec(
        name="save_nested_plan",
        description="Save a plan whose concepts must be a JSON array.",
        args_model=_NestedPlanArguments,
    )

    def invoke(self, arguments, _context) -> ToolResult:
        return ToolResult(message=json.dumps(arguments, sort_keys=True))


def _live_environment(environment_root: Path, runtime_root: Path) -> dict[str, str]:
    environ = dict(os.environ)
    for path in (
        environment_root / "config" / "app.env",
        environment_root / ".env",
    ):
        if not path.is_file():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None and key not in environ:
                environ[key] = value
    environ.update(
        {
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(runtime_root / "agent.db"),
            "REPORT_AGENT_CUSTOM_MODELS_PATH": str(
                environment_root / "config" / "custom_models.json"
            ),
            "REPORT_AGENT_MAX_ITERATIONS": "40",
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_STATIC_DIR": str(REPO_ROOT / "frontend" / "dist"),
            "REPORT_AGENT_STUDY_ROOT": str(environment_root / "study_data"),
        }
    )
    return environ


def test_live_environment_preserves_openai_key_for_semantic_search(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment_root = tmp_path / "environment"
    environment_root.mkdir()
    (environment_root / ".env").write_text(
        "OPENAI_API_KEY=semantic-search-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    environ = _live_environment(environment_root, tmp_path / "runtime")

    assert environ["OPENAI_API_KEY"] == "semantic-search-key"


def _json_content(message: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(message.content))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_repair_requirement(message: Any) -> bool:
    payload = _json_content(message)
    if isinstance(message, SystemMessage):
        return payload.get("code") == "TOOL_ARGUMENT_REPAIR_REQUIRED"
    if not isinstance(message, ToolMessage):
        return False
    error = payload.get("error")
    if not isinstance(error, dict) or error.get("code") != "INVALID_ARGUMENTS":
        return False
    details = error.get("details")
    return isinstance(details, dict) and details.get("repair_required") is True


def _repair_tool_names(message: Any) -> set[str]:
    if isinstance(message, ToolMessage):
        return {str(message.name or "")}
    payload = _json_content(message)
    return {
        str(call.get("name") or "")
        for call in list(payload.get("invalid_calls") or [])
        if isinstance(call, dict) and str(call.get("name") or "")
    }


def _minimax_profile(
    environment_root: Path,
    environ: dict[str, str],
) -> ModelRuntimeProfile:
    profiles = load_custom_model_profiles(
        environment_root / "config" / "custom_models.json",
        environ=environ,
    )
    if MINIMAX_MODEL_ID not in profiles:
        pytest.skip(f"{MINIMAX_MODEL_ID} is not registered")
    return profiles[MINIMAX_MODEL_ID]


def _gemma_profile() -> ModelRuntimeProfile:
    return replace(
        model_runtime_profile("gpt-5.4"),
        model_id=GEMMA_MODEL_ID,
        base_label="Gemma 4 31B IT",
        provider=PROVIDER_OPENAI_COMPATIBLE,
        api_key_env="",
        api_key_required=False,
        base_url=os.getenv("GEMMA4_VLLM_BASE_URL", DEFAULT_GEMMA_BASE_URL),
        remote_model_id=GEMMA_SERVED_MODEL_ID,
        output_approval_required=False,
    )


@pytest.mark.skipif(
    os.getenv("RUN_MINIMAX_TOOL_REPAIR_LIVE") != "1",
    reason="set RUN_MINIMAX_TOOL_REPAIR_LIVE=1 to call the real MiniMax model",
)
def test_real_minimax_repairs_item_wrapped_array_after_validation_message(
    tmp_path: Path,
) -> None:
    environment_root = Path(
        os.getenv("MINIMAX_TOOL_REPAIR_ENV_ROOT", REPO_ROOT)
    ).resolve()
    environ = _live_environment(environment_root, tmp_path)
    api_key = str(environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY is not configured")
    profile = _minimax_profile(environment_root, environ)
    model = build_chat_llm(
        model_name=MINIMAX_MODEL_ID,
        profile=profile,
        api_key=api_key,
    )
    tool = _NestedPlanTool()
    registry = ToolRegistry([tool])
    rejected_call = {
        "name": tool.spec.name,
        "args": {"plan": {"concepts": {"item": ["age", "sex"]}}},
        "id": "rejected-call",
        "type": "tool_call",
    }
    with pytest.raises(ToolExecutionError) as caught:
        registry.invoke(
            tool.spec.name,
            rejected_call["args"],
            context=None,  # type: ignore[arg-type]
        )
    details = dict(caught.value.details or {})
    details.update(
        {
            "instruction": (
                "Call this tool once more with arguments matching its "
                "advertised schema. Do not repeat the rejected arguments."
            ),
            "repair_required": True,
            "repair_attempts_remaining": 1,
        }
    )
    validation_message = error_tool_message(
        rejected_call,
        ToolExecutionError(
            "INVALID_ARGUMENTS",
            str(caught.value),
            recoverable=True,
            details=details,
        ),
    )
    rejected_message = AIMessage(content="", tool_calls=[rejected_call])

    answer = model.bind_tools(
        [tool.spec.model_schema(inline_local_references=True)],
        tool_choice={
            "type": "function",
            "function": {"name": tool.spec.name},
        },
    ).invoke(
        [
            SystemMessage(content="Follow the tool schema exactly."),
            HumanMessage(content="Save the concepts age and sex."),
            rejected_message,
            validation_message,
        ]
    )

    assert len(answer.tool_calls) == 1
    repaired = answer.tool_calls[0]
    assert repaired["name"] == tool.spec.name
    validated = _NestedPlanArguments.model_validate(repaired["args"])
    assert validated.plan.concepts == ["age", "sex"]
    assert isinstance(repaired["args"]["plan"]["concepts"], list)


def _run_household_contact_prevalence_task(
    *,
    environ: dict[str, str],
    model_profile: ModelRuntimeProfile,
) -> None:
    model_id = model_profile.model_id
    availability = ModelAvailability(
        registered_profiles={model_id: model_profile},
        available_model_ids=(model_id,),
        default_model_id=model_id,
        title_model_id=model_id,
    )
    app = build_application(environ=environ, model_availability=availability)
    app.state.report_agent_runtime.title_generator_factory = None

    with TestClient(app) as client:
        resumed_interrupts: set[str] = set()
        created = client.post(
            "/api/threads",
            headers=LOCAL_HEADERS,
            json={"model_name": model_id},
        )
        created.raise_for_status()
        thread_id = str(created.json()["thread_id"])
        submitted = client.post(
            f"/api/threads/{thread_id}/messages",
            headers=LOCAL_HEADERS,
            json={"text": FAILED_QUESTION},
        )
        submitted.raise_for_status()

        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            state_response = client.get(
                f"/api/threads/{thread_id}/state",
                headers=LOCAL_HEADERS,
            )
            state_response.raise_for_status()
            public_state = state_response.json()
            active_interrupt = public_state.get("active_interrupt")
            if isinstance(active_interrupt, dict):
                interrupt_id = str(active_interrupt["id"])
                interrupt_type = str(active_interrupt.get("type") or "")
                if interrupt_id in resumed_interrupts:
                    time.sleep(0.25)
                    continue
                if interrupt_type == "agent_clarification":
                    decision = {
                        "action": "answer",
                        "answer": "let agent decide",
                    }
                elif interrupt_type in {
                    "dataset_plan_review",
                    "dataset_review",
                    "analysis_result_review",
                }:
                    decision = {"action": "approve"}
                elif interrupt_type == "model_output_limit":
                    decision = {"action": "continue"}
                else:
                    pytest.fail(
                        f"Unexpected {model_profile.base_label} interrupt: "
                        f"{active_interrupt}"
                    )
                resumed = client.post(
                    f"/api/threads/{thread_id}/interrupts/{interrupt_id}/resume",
                    headers=LOCAL_HEADERS,
                    json=decision,
                )
                resumed.raise_for_status()
                resumed_interrupts.add(interrupt_id)
                continue
            runtime = app.state.report_agent_runtime
            thread = runtime._thread(thread_id)
            if thread.app is None:
                time.sleep(0.25)
                continue
            snapshot = thread.app.get_state(
                graph_config(thread_id),
                subgraphs=True,
            )
            messages = list(snapshot.values.get("messages") or [])
            repair_indexes = [
                index
                for index, message in enumerate(messages)
                if _is_repair_requirement(message)
            ]
            error_code = str(
                dict(public_state.get("run") or {}).get("error_code") or ""
            )
            assert error_code not in {
                "REPEATED_TOOL_FAILURE",
                "TOOL_ARGUMENT_REPAIR_EXHAUSTED",
            }
            run_state = str(
                dict(public_state.get("run") or {}).get("state") or ""
            )
            if run_state == "done":
                for repair_index in repair_indexes:
                    repair_names = _repair_tool_names(messages[repair_index])
                    later_messages = messages[repair_index + 1 :]
                    corrected_ids = {
                        str(call["id"])
                        for message in later_messages
                        if isinstance(message, AIMessage)
                        for call in list(message.tool_calls or [])
                        if str(call.get("name") or "") in repair_names
                    }
                    assert any(
                        isinstance(message, ToolMessage)
                        and message.status == "success"
                        and str(message.tool_call_id) in corrected_ids
                        for message in later_messages
                    )
                final_response = str(snapshot.values.get("final_response") or "")
                assert "prevalence" in final_response.casefold()
                successful_tools = {
                    str(message.name or "")
                    for message in messages
                    if isinstance(message, ToolMessage)
                    and message.status == "success"
                }
                assert {
                    "dbrag-save_dataset_plan",
                    "dbrag-validate_and_extract",
                    "dbrag-request_dataset_review",
                } <= successful_tools
                print(
                    f"{model_profile.base_label} household-contact task "
                    "outcome: completed"
                )
                return
            if run_state in {"error", "timeout", "cancelled", "waiting"}:
                pytest.fail(
                    f"{model_profile.base_label} stopped before completing "
                    "the household-contact task: "
                    f"{public_state.get('run')}"
                )
            time.sleep(0.5)

    pytest.fail(
        f"{model_profile.base_label} household-contact task exceeded ten minutes"
    )


@pytest.mark.skipif(
    os.getenv("RUN_MINIMAX_TOOL_REPAIR_LIVE") != "1",
    reason="set RUN_MINIMAX_TOOL_REPAIR_LIVE=1 to call the real MiniMax model",
)
def test_real_minimax_completes_household_contact_prevalence_task(
    tmp_path: Path,
) -> None:
    environment_root = Path(
        os.getenv("MINIMAX_TOOL_REPAIR_ENV_ROOT", REPO_ROOT)
    ).resolve()
    environ = _live_environment(environment_root, tmp_path)
    if not str(environ.get("OPENROUTER_API_KEY") or "").strip():
        pytest.skip("OPENROUTER_API_KEY is not configured")
    _run_household_contact_prevalence_task(
        environ=environ,
        model_profile=_minimax_profile(environment_root, environ),
    )


@pytest.mark.skipif(
    os.getenv("RUN_GEMMA4_TOOL_REPAIR_LIVE") != "1",
    reason="set RUN_GEMMA4_TOOL_REPAIR_LIVE=1 to call the real Gemma model",
)
def test_real_gemma4_completes_household_contact_prevalence_task(
    tmp_path: Path,
) -> None:
    environment_root = Path(
        os.getenv("GEMMA4_TOOL_REPAIR_ENV_ROOT", REPO_ROOT)
    ).resolve()
    _run_household_contact_prevalence_task(
        environ=_live_environment(environment_root, tmp_path),
        model_profile=_gemma_profile(),
    )
