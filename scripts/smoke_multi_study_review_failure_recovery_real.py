#!/usr/bin/env python3
"""Exercise multi-study plan review and legacy tool-call recovery once."""

from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.auth import AuthenticatedUser, LOCAL_SESSION_ID, RequestIdentity
from api.runtime import _initial_graph_state, graph_config
from scripts.e2e_agent_activity_timeline_real import (
    _find_port,
    _launch_browser,
    _remaining_ms,
    _wait_for_health,
)
from study_package.installer import install_study_archives
from utils.env_loader import load_app_environment


HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
MESSAGE_LABEL = "Ask a question about your dataset!"
REPORT_ID = "report-india-synthetic"
NHANES_ID = "nhanes-2017-2018"
COMPILED_FRONTEND = REPO_ROOT / "frontend/dist"
PLAN_QUERY = (
    "Using study_id report-india-synthetic, create one row per index case "
    "with smoking status, HbA1c, missed treatment doses, and final TB outcome. "
    "Use the baseline HbA1c measurement (VISIT = B/L), aligned with baseline "
    "smoking status. Define high HbA1c as HbA1c >= 6.5%. Define poor "
    "outcome as treatment "
    "incomplete, bacteriologic failure, death, clinical failure, or loss to "
    "follow-up. Use missed doses from the last treatment follow-up record. "
    "Compare poor outcome rates between current smokers with high HbA1c and "
    "everyone else. Present the dataset plan for review before extraction."
)
FOLLOW_UP = "Who are you? Reply in one short sentence."
LEGACY_TITLE = "Legacy failed tool turn"
LEGACY_USER_MESSAGE = (
    "Create a dataset; preserve this deliberately failed legacy turn."
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    return repr(value)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_json_safe(value), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _default_environment_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = REPO_ROOT / common
    return common.resolve().parent


def _conversation_items(api_url: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{api_url}/api/conversations",
        headers=HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return [
        dict(item)
        for item in list(response.json().get("items") or [])
        if isinstance(item, dict)
    ]


def _thread_state(api_url: str, thread_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state",
        headers=HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return dict(response.json())


def _checkpoint_values(
    checkpoint_path: Path,
    thread_id: str,
) -> dict[str, Any]:
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        saved = saver.get_tuple(graph_config(thread_id))
    if saved is None:
        raise AssertionError(f"Missing checkpoint for {thread_id}")
    channels = dict(saved.checkpoint.get("channel_values") or {})
    root = channels.get("__root__")
    return dict(root) if isinstance(root, dict) else channels


def _seed_legacy_orphan(environment: dict[str, str]) -> str:
    os.environ.update(environment)
    from api.activity_store import SqliteActivityStore
    from api.app import app as application
    from graph.conversation_events import (
        append_conversation_event,
        build_user_event,
    )
    from graph.state import MetaKeys

    runtime = application.state.report_agent_runtime
    identity = RequestIdentity(
        user=AuthenticatedUser(owner_user_id="local-user"),
        session_id=LOCAL_SESSION_ID,
    )
    thread_id = runtime.create_thread(identity)
    thread = runtime._require_owned_thread(identity, thread_id)
    runtime._ensure_graph(
        identity,
        thread,
        str(environment["OPENAI_API_KEY"]),
    )
    graph, _runner = runtime._bound_graph(thread)
    user = HumanMessage(
        content=LEGACY_USER_MESSAGE,
        id="legacy-user",
    )
    turn_hash = runtime._message_turn_hash(user)
    state = _initial_graph_state(thread_id, user)
    state["meta"][MetaKeys.LAST_USER_MESSAGE_HASH] = turn_hash
    state = append_conversation_event(
        state,
        build_user_event(
            actor="human",
            user_turn_hash=turn_hash,
            text=str(user.content),
        ),
    )
    user_event_id = str(
        state["artifacts"]["conversation_events"][-1]["event_id"]
    )
    state["messages"] = [
        user,
        AIMessage(
            content="",
            id="legacy-assistant",
            tool_calls=[
                {
                    "name": "dbrag-request_dataset_plan_review",
                    "args": {"plan_id": "legacy-plan", "version": 1},
                    "id": "legacy-orphan-call",
                    "type": "tool_call",
                }
            ],
        ),
    ]
    terminal_error = {
        "code": "RUN_FAILED",
        "message": "The prior request failed unexpectedly.",
        "recoverable": False,
    }
    state["terminal_error"] = terminal_error
    state["agent_status"] = {
        "status": "error",
        "run_status": "error",
        "terminal_error": terminal_error,
    }
    graph.update_state(
        runtime._checkpoint_config(identity, thread_id),
        state,
        as_node="tools",
    )
    if runtime.history_store is None:
        raise AssertionError("Conversation history is unavailable")
    runtime.history_store.promote_pending("local-user", thread_id)
    runtime.history_store.rename("local-user", thread_id, LEGACY_TITLE)
    activity = SqliteActivityStore(
        Path(environment["REPORT_AGENT_CHECKPOINT_DB_PATH"])
    )
    activity.start_run(thread_id, user_event_id)
    activity.model_completed(thread_id)
    activity.tool_started(
        thread_id,
        "legacy-orphan-call",
        "dbrag-request_dataset_plan_review",
    )
    activity.finish(thread_id, "error")
    runtime.release_session("local-user", LOCAL_SESSION_ID)
    gc.collect()
    return thread_id


def _wait_for_new_thread(
    api_url: str,
    existing_ids: set[str],
    deadline: float,
) -> str:
    while time.monotonic() < deadline:
        current_ids = {
            str(item["thread_id"])
            for item in _conversation_items(api_url)
        }
        created = current_ids - existing_ids
        if len(created) == 1:
            return created.pop()
        if len(created) > 1:
            raise AssertionError(f"Multiple new conversations appeared: {created}")
        time.sleep(0.25)
    raise TimeoutError("The browser did not create a new conversation")


def _wait_for_dataset_plan_review(
    page: Any,
    api_url: str,
    thread_id: str,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        state = _thread_state(api_url, thread_id)
        interrupt = dict(state.get("active_interrupt") or {})
        heading = page.get_by_role(
            "heading",
            name="Review dataset plan",
            exact=True,
        )
        if (
            interrupt.get("type") == "dataset_plan_review"
            and heading.is_visible(timeout=100)
        ):
            return state
        run = dict(state.get("run") or {})
        run_state = str(run.get("state") or "")
        if run_state in {"error", "timeout", "cancelled", "done"}:
            raise AssertionError(
                f"Run ended before dataset-plan review: {run}"
            )
        if interrupt.get("type") == "agent_clarification":
            raise AssertionError(
                "Unexpected clarification before plan review: "
                f"{interrupt.get('question')}"
            )
        time.sleep(0.25)
    raise TimeoutError("Dataset-plan review did not render before the deadline")


def _wait_for_follow_up(
    api_url: str,
    thread_id: str,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        state = _thread_state(api_url, thread_id)
        run = dict(state.get("run") or {})
        run_state = str(run.get("state") or "")
        conversation = list(state.get("conversation") or [])
        if (
            run_state == "done"
            and conversation
            and conversation[-1].get("role") == "assistant"
        ):
            return state
        if run_state in {"error", "timeout", "cancelled"}:
            raise AssertionError(f"Follow-up failed: {run}")
        time.sleep(0.25)
    raise TimeoutError("Follow-up did not complete before the deadline")


def _assert_report_only_plan(values: dict[str, Any]) -> None:
    files = dict(dict(values.get("artifacts") or {}).get("files") or {})
    plans = [
        dict(record["content"])
        for record in files.values()
        if isinstance(record, dict)
        and isinstance(record.get("content"), dict)
        and record["content"].get("kind") == "dataset_plan"
    ]
    if not plans:
        raise AssertionError("The review checkpoint contains no dataset plan")
    study_ids = {
        str(dict(plan.get("content") or {}).get("study_id") or "")
        for plan in plans
    }
    if study_ids != {REPORT_ID}:
        raise AssertionError(f"Unexpected plan study IDs: {study_ids}")
    serialized = json.dumps(_json_safe(plans), sort_keys=True)
    if NHANES_ID in serialized:
        raise AssertionError("The RePORT plan contains NHANES evidence")


def _assert_repaired_checkpoint(values: dict[str, Any]) -> None:
    messages = list(values.get("messages") or [])
    call_index = next(
        index
        for index, message in enumerate(messages)
        if isinstance(message, AIMessage) and message.id == "legacy-assistant"
    )
    repaired = messages[call_index + 1]
    follow_up = messages[call_index + 2]
    if not isinstance(repaired, ToolMessage):
        raise AssertionError("The orphaned call has no matching ToolMessage")
    if repaired.tool_call_id != "legacy-orphan-call":
        raise AssertionError("The repaired result targets the wrong call ID")
    if repaired.status != "error":
        raise AssertionError("The repaired result is not marked as an error")
    payload = json.loads(str(repaired.content))
    if payload.get("error", {}).get("code") != "INTERNAL_TOOL_ERROR":
        raise AssertionError(f"Unexpected repaired payload: {payload}")
    if not isinstance(follow_up, HumanMessage):
        raise AssertionError("The follow-up does not follow the repaired result")
    public_payload = json.dumps(payload)
    for private_marker in ("AttributeError", "Traceback", "data_sources"):
        if private_marker in public_payload:
            raise AssertionError(
                f"Repair exposed private marker {private_marker!r}"
            )


def _write_page_artifacts(page: Any, artifact_dir: Path, prefix: str) -> None:
    (artifact_dir / f"{prefix}-page.txt").write_text(
        page.locator("body").inner_text(),
        encoding="utf-8",
    )
    (artifact_dir / f"{prefix}-page.html").write_text(
        page.content(),
        encoding="utf-8",
    )
    page.screenshot(
        path=str(artifact_dir / f"{prefix}-screenshot.png"),
        full_page=True,
    )


def _write_failure_diagnostics(
    artifact_dir: Path,
    error: BaseException,
    *,
    page: Any | None,
    api_url: str,
    thread_ids: list[str],
    checkpoint_path: Path,
) -> None:
    (artifact_dir / "failure-traceback.txt").write_text(
        "".join(traceback.format_exception(error)),
        encoding="utf-8",
    )
    if page is not None:
        try:
            _write_page_artifacts(page, artifact_dir, "failure")
        except Exception:
            pass
    for thread_id in thread_ids:
        try:
            _write_json(
                artifact_dir / f"failure-api-state-{thread_id}.json",
                _thread_state(api_url, thread_id),
            )
        except Exception:
            pass
        try:
            _write_json(
                artifact_dir / f"failure-checkpoint-{thread_id}.json",
                _checkpoint_values(checkpoint_path, thread_id),
            )
        except Exception:
            pass


def run(args: argparse.Namespace) -> int:
    if args.timeout_seconds > 300:
        raise ValueError("The feature smoke is limited to five minutes.")
    deadline = time.monotonic() + args.timeout_seconds
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else Path(
            tempfile.mkdtemp(
                prefix="report-multi-study-review-failure-recovery-smoke-"
            )
        )
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    study_root = artifact_dir / "study-root"
    checkpoint_path = runtime_root / "agent_memory_fastapi.db"
    runtime_root.mkdir(parents=True, exist_ok=True)
    study_root.mkdir(parents=True, exist_ok=True)

    environment_root = args.environment_root.expanduser().resolve()
    load_app_environment(environment_root)
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "A real OPENAI_API_KEY is required via the environment or "
            f"{environment_root / '.env'}."
        )
    if not (COMPILED_FRONTEND / "index.html").is_file():
        raise RuntimeError("Build frontend/dist before running this smoke.")
    archives = [
        args.report_archive.expanduser().resolve(),
        args.nhanes_archive.expanduser().resolve(),
    ]
    for archive in archives:
        if not archive.is_file():
            raise FileNotFoundError(f"Study archive not found: {archive}")
    install_study_archives(archives, study_root / "studies")

    environment = dict(os.environ)
    environment.update(
        {
            "OPENAI_API_KEY": api_key,
            "PYTHONPATH": str(REPO_ROOT),
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(checkpoint_path),
            "REPORT_AGENT_STATIC_DIR": str(COMPILED_FRONTEND),
            "REPORT_AGENT_STUDY_ROOT": str(study_root),
            "REPORT_AGENT_API_WORKFLOW_TIMEOUT_SECONDS": str(
                args.timeout_seconds
            ),
        }
    )

    legacy_thread_id = _seed_legacy_orphan(environment)
    host = "127.0.0.1"
    port = _find_port(host, args.api_port)
    api_url = f"http://{host}:{port}"
    api_log_handle = (artifact_dir / "api.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "api.app:app",
            "--host",
            host,
            "--port",
            str(port),
            "--workers",
            "1",
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=api_log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )

    page: Any | None = None
    observed_thread_ids = [legacy_thread_id]
    try:
        _wait_for_health(api_url, deadline, process)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = (
                playwright.chromium.launch(headless=False)
                if args.headful
                else _launch_browser(playwright)
            )
            try:
                page = browser.new_page(
                    viewport={"width": 1600, "height": 1050}
                )
                page.goto(
                    api_url,
                    wait_until="networkidle",
                    timeout=_remaining_ms(deadline),
                )
                existing_ids = {
                    str(item["thread_id"])
                    for item in _conversation_items(api_url)
                }
                page.get_by_role(
                    "button",
                    name="New conversation",
                    exact=True,
                ).click()
                field = page.get_by_label(MESSAGE_LABEL)
                field.fill(PLAN_QUERY)
                page.get_by_role("button", name="Send", exact=True).click()
                review_thread_id = _wait_for_new_thread(
                    api_url,
                    existing_ids,
                    deadline,
                )
                observed_thread_ids.append(review_thread_id)
                review_state = _wait_for_dataset_plan_review(
                    page,
                    api_url,
                    review_thread_id,
                    deadline,
                )
                review_values = _checkpoint_values(
                    checkpoint_path,
                    review_thread_id,
                )
                _assert_report_only_plan(review_values)
                _write_json(
                    artifact_dir / "review-api-state.json",
                    review_state,
                )
                _write_json(
                    artifact_dir / "review-checkpoint.json",
                    review_values,
                )
                _write_page_artifacts(page, artifact_dir, "review")

                page.get_by_text(LEGACY_TITLE, exact=True).click()
                legacy_message = page.get_by_text(
                    LEGACY_USER_MESSAGE,
                    exact=True,
                )
                legacy_message.wait_for(timeout=_remaining_ms(deadline))
                failed_timeline = page.get_by_label(
                    "Agent activity timeline",
                    exact=True,
                ).last
                failed_timeline.wait_for(timeout=_remaining_ms(deadline))
                if "agent-activity--error" not in str(
                    failed_timeline.get_attribute("class")
                ):
                    raise AssertionError(
                        "The prior failed turn is not visibly marked as failed"
                    )
                field = page.get_by_label(MESSAGE_LABEL)
                field.fill(FOLLOW_UP)
                page.get_by_role("button", name="Send", exact=True).click()
                legacy_state = _wait_for_follow_up(
                    api_url,
                    legacy_thread_id,
                    deadline,
                )
                legacy_values = _checkpoint_values(
                    checkpoint_path,
                    legacy_thread_id,
                )
                _assert_repaired_checkpoint(legacy_values)
                first_timeline = page.get_by_label(
                    "Agent activity timeline",
                    exact=True,
                ).first
                if "agent-activity--error" not in str(
                    first_timeline.get_attribute("class")
                ):
                    raise AssertionError(
                        "The old failed marker disappeared after follow-up"
                    )
                page_text = page.locator("body").inner_text()
                for private_marker in (
                    "No tool output found",
                    "AttributeError",
                    "data_sources",
                ):
                    if private_marker in page_text:
                        raise AssertionError(
                            f"Browser exposed private marker {private_marker!r}"
                        )
                _write_json(
                    artifact_dir / "legacy-api-state.json",
                    legacy_state,
                )
                _write_json(
                    artifact_dir / "legacy-checkpoint.json",
                    legacy_values,
                )
                _write_page_artifacts(page, artifact_dir, "follow-up")
            finally:
                browser.close()
    except BaseException as error:
        _write_failure_diagnostics(
            artifact_dir,
            error,
            page=page,
            api_url=api_url,
            thread_ids=observed_thread_ids,
            checkpoint_path=checkpoint_path,
        )
        print(
            "FAIL multi-study review/failure-recovery smoke; "
            f"diagnostics: {artifact_dir}",
            flush=True,
        )
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        api_log_handle.close()

    print(
        "PASS multi-study review/failure-recovery smoke; "
        f"diagnostics: {artifact_dir}",
        flush=True,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the real multi-study plan-review and tool-failure recovery "
            "browser smoke once."
        )
    )
    parser.add_argument("--report-archive", type=Path, required=True)
    parser.add_argument("--nhanes-archive", type=Path, required=True)
    parser.add_argument("--api-port", type=int, default=8890)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument(
        "--environment-root",
        type=Path,
        default=_default_environment_root(),
        help="Project root whose .env supplies the real OpenAI credentials.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args()))
