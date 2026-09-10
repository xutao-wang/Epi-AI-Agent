from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
import pytest

from api.app import build_application
from api.auth import LOCAL_SESSION_ID
from api.runtime import graph_config
from utils.model_availability import ModelAvailability
from utils.model_runtime_profiles import load_custom_model_profiles


REPO_ROOT = Path(__file__).parents[1]
MODEL_ID = "openrouter:minimax/minimax-m3"
FAILED_QUESTION = (
    "Create a household-contact dataset with age, sex, household size, TST "
    "result, and IGRA result. Report the prevalence of positive TB infection "
    "tests."
)
LOCAL_HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}


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
    environ.pop("OPENAI_API_KEY", None)
    environ.update(
        {
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(runtime_root / "agent.db"),
            "REPORT_AGENT_CUSTOM_MODELS_PATH": str(
                environment_root / "config" / "custom_models.json"
            ),
            "REPORT_AGENT_MAX_ITERATIONS": "20",
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_STATIC_DIR": str(REPO_ROOT / "frontend" / "dist"),
            "REPORT_AGENT_STUDY_ROOT": str(environment_root / "study_data"),
        }
    )
    return environ


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


@pytest.mark.skipif(
    os.getenv("RUN_MINIMAX_TOOL_REPAIR_LIVE") != "1",
    reason="set RUN_MINIMAX_TOOL_REPAIR_LIVE=1 to call the real MiniMax model",
)
def test_real_minimax_failed_question_has_one_bounded_repair(
    tmp_path: Path,
) -> None:
    environment_root = Path(
        os.getenv("MINIMAX_TOOL_REPAIR_ENV_ROOT", REPO_ROOT)
    ).resolve()
    environ = _live_environment(environment_root, tmp_path)
    if not str(environ.get("OPENROUTER_API_KEY") or "").strip():
        pytest.skip("OPENROUTER_API_KEY is not configured")

    profiles = load_custom_model_profiles(
        environment_root / "config" / "custom_models.json",
        environ=environ,
    )
    if MODEL_ID not in profiles:
        pytest.skip(f"{MODEL_ID} is not registered")
    profile = profiles[MODEL_ID]
    availability = ModelAvailability(
        registered_profiles={MODEL_ID: profile},
        available_model_ids=(MODEL_ID,),
        default_model_id=MODEL_ID,
        title_model_id=MODEL_ID,
    )
    app = build_application(environ=environ, model_availability=availability)
    app.state.report_agent_runtime.title_generator_factory = None

    with TestClient(app) as client:
        answered_clarifications: set[str] = set()
        created = client.post(
            "/api/threads",
            headers=LOCAL_HEADERS,
            json={"model_name": MODEL_ID},
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
            if (
                isinstance(active_interrupt, dict)
                and active_interrupt.get("type") == "agent_clarification"
                and str(active_interrupt.get("id") or "")
                not in answered_clarifications
            ):
                interrupt_id = str(active_interrupt["id"])
                resumed = client.post(
                    f"/api/threads/{thread_id}/interrupts/{interrupt_id}/resume",
                    headers=LOCAL_HEADERS,
                    json={"action": "answer", "answer": "let agent decide"},
                )
                resumed.raise_for_status()
                answered_clarifications.add(interrupt_id)
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
            if repair_indexes:
                assert len(repair_indexes) == 1
                repair_index = repair_indexes[0]
                repair_names = _repair_tool_names(messages[repair_index])
                later_messages = messages[repair_index + 1 :]
                correction = next(
                    (
                        message
                        for message in later_messages
                        if isinstance(message, AIMessage)
                    ),
                    None,
                )
                corrected_call_ids = {
                    str(call["id"])
                    for call in list(
                        getattr(correction, "tool_calls", None) or []
                    )
                    if str(call.get("name") or "") in repair_names
                }
                repaired = any(
                    isinstance(message, ToolMessage)
                    and message.status == "success"
                    and str(message.tool_call_id) in corrected_call_ids
                    for message in later_messages
                )
                error_code = str(
                    dict(public_state.get("run") or {}).get("error_code") or ""
                )
                assert error_code != "REPEATED_TOOL_FAILURE"
                if repaired or (
                    corrected_call_ids and public_state.get("active_interrupt")
                ):
                    print("MiniMax tool repair outcome: repaired")
                    return
                if error_code == "TOOL_ARGUMENT_REPAIR_EXHAUSTED":
                    print("MiniMax tool repair outcome: bounded exhaustion")
                    return
            run_state = str(
                dict(public_state.get("run") or {}).get("state") or ""
            )
            if run_state in {
                "error",
                "timeout",
                "cancelled",
                "done",
                "waiting",
            }:
                pytest.fail(
                    "MiniMax stopped before exercising the malformed-call repair: "
                    f"{public_state.get('run')}"
                )
            time.sleep(0.5)

    pytest.fail("MiniMax tool-argument repair smoke exceeded ten minutes")
