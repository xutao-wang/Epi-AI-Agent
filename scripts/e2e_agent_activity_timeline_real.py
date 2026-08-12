#!/usr/bin/env python3
"""Exercise the agent activity timeline through the real API and compiled UI."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.auth import LOCAL_SESSION_ID


LOCAL_API_HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
MESSAGE_LABEL = "Ask a question about your dataset!"
DEFAULT_QUERY = (
    "Create a baseline index-case dataset from Form 2A - INDEX CASE: "
    "Clinical/Demographic Form with participant ID, age, sex, and marital "
    "status. Present the dataset plan for review."
)


def _launch_browser(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch()
    except Exception as error:
        if "Executable doesn't exist" not in str(error):
            raise
        return playwright.chromium.launch(channel="chrome")


def _find_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("Could not find an available local port.")


def _remaining_ms(deadline: float) -> int:
    return max(1, int((deadline - time.monotonic()) * 1000))


def _wait_for_health(
    api_url: str,
    deadline: float,
    process: subprocess.Popen[Any],
) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"FastAPI exited early with code {process.returncode}."
            )
        try:
            response = requests.get(f"{api_url}/api/health", timeout=2)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise TimeoutError("FastAPI did not become ready.")


def _thread_state(api_url: str) -> dict[str, Any]:
    conversations = requests.get(
        f"{api_url}/api/conversations",
        headers=LOCAL_API_HEADERS,
        timeout=5,
    )
    conversations.raise_for_status()
    items = conversations.json().get("items") or []
    if len(items) != 1:
        raise AssertionError(
            f"Expected exactly one conversation, received {items!r}."
        )
    response = requests.get(
        f"{api_url}/api/threads/{items[0]['thread_id']}/state",
        headers=LOCAL_API_HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return response.json()


def _wait_for_dataset_plan_review(
    page: Any,
    *,
    api_url: str,
    deadline: float,
    state_reader: Any = _thread_state,
) -> None:
    while time.monotonic() < deadline:
        heading = page.get_by_role(
            "heading",
            name="Review dataset plan",
            exact=True,
        )
        approve_stepwise = page.get_by_role(
            "button",
            name="Approve & continue",
            exact=True,
        )
        approve_final = page.get_by_role(
            "button",
            name="Approve plan and extract",
            exact=True,
        )
        has_review_controls = approve_stepwise.is_visible(
            timeout=100
        ) or approve_final.is_visible(timeout=100)
        if heading.is_visible(timeout=100) and has_review_controls:
            return

        try:
            state = state_reader(api_url)
        except AssertionError as error:
            if "received []" not in str(error):
                raise
            time.sleep(0.25)
            continue
        run = state.get("run") or {}
        run_state = str(run.get("state") or "")
        if run_state in {"done", "cancelled", "error", "timeout"}:
            error_code = str(run.get("error_code") or "AGENT_RUN_TERMINATED")
            message = str(
                run.get("user_message")
                or run.get("error")
                or f"Agent run ended with state {run_state}."
            )
            raise RuntimeError(
                "Agent run ended before dataset-plan review: "
                f"{message} Error: {error_code}"
            )

        interrupt = state.get("active_interrupt") or {}
        if interrupt.get("type") == "agent_clarification":
            question = str(
                interrupt.get("question")
                or "The agent did not provide clarification text."
            )
            raise RuntimeError(
                "Agent requested unexpected clarification before "
                f"dataset-plan review: {question}"
            )
        time.sleep(0.25)
    raise TimeoutError("Timed out waiting for dataset-plan review.")


def _wait_for_timeline_label(
    timeline: Any,
    label: str,
    *,
    deadline: float,
) -> None:
    timeline.get_by_text(label, exact=True).first.wait_for(
        timeout=_remaining_ms(deadline)
    )


def _assert_plain_language_timeline(rendered_text: str) -> None:
    normalized_text = rendered_text.casefold()
    if "dbrag-" in normalized_text:
        raise AssertionError(
            "The public timeline exposed technical tool-name leakage."
        )
    if "fail" in normalized_text:
        raise AssertionError(
            "The public timeline exposed a tool-call failure."
        )


@contextmanager
def _diagnostic_browser(browser: Any, record_failure: Any):
    try:
        yield browser
    except BaseException as error:
        record_failure(error)
        raise
    finally:
        browser.close()


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
    *,
    artifact_dir: Path,
    api_url: str,
    page: Any | None,
    error: BaseException,
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
    try:
        state = _thread_state(api_url)
        (artifact_dir / "failure-api-state.json").write_text(
            json.dumps(state, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _latest_user_message_id(state: dict[str, Any]) -> str:
    user_messages = [
        item
        for item in state.get("conversation") or []
        if item.get("role") == "user"
    ]
    if not user_messages:
        raise AssertionError("The API state has no user conversation event.")
    return str(user_messages[-1]["id"])


def _assert_waiting_activity_state(state: dict[str, Any]) -> None:
    user_message_id = _latest_user_message_id(state)
    runs = [
        run
        for run in state.get("activity_runs") or []
        if run.get("user_message_id") == user_message_id
    ]
    if len(runs) != 1:
        raise AssertionError(
            "Expected one activity run linked to the submitted user message; "
            f"received {runs!r}."
        )
    run = runs[0]
    if run.get("state") != "waiting":
        raise AssertionError(f"Expected a waiting activity run, received {run!r}.")

    activities = run.get("activities") or []
    labels = [str(item.get("label") or "") for item in activities]
    tool_names = [
        str(item.get("tool_name") or "")
        for item in activities
        if item.get("tool_name")
    ]
    if "Searching the data catalog" not in labels:
        raise AssertionError(f"Catalog-search activity is missing: {activities!r}")
    if "Waiting for dataset plan review" not in labels:
        raise AssertionError(f"Dataset-plan wait activity is missing: {activities!r}")
    if not any(name.startswith("dbrag-") for name in tool_names):
        raise AssertionError(f"DB-RAG technical tool names are missing: {tool_names!r}")
    if any(str(item.get("status") or "").casefold() == "failed" for item in activities):
        raise AssertionError(f"A failed tool call leaked into public activity: {activities!r}")


def run(args: argparse.Namespace) -> int:
    if args.timeout_seconds > 300:
        raise ValueError("The feature smoke is limited to five minutes.")
    deadline = time.monotonic() + args.timeout_seconds
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="agent-activity-timeline-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)

    from utils.env_loader import load_app_environment

    api_url = "http://127.0.0.1:0"
    api_log: Any | None = None
    process: subprocess.Popen[Any] | None = None
    try:
        environment_root = args.environment_root.expanduser().resolve()
        load_app_environment(environment_root)
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            raise RuntimeError(
                "A real OPENAI_API_KEY is required via the environment or "
                f"{environment_root / '.env'}."
            )
        static_dir = REPO_ROOT / "frontend" / "dist"
        if not (static_dir / "index.html").is_file():
            raise RuntimeError("Build frontend/dist before running this smoke.")

        host = "127.0.0.1"
        port = _find_port(host, args.api_port)
        api_url = f"http://{host}:{port}"
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONPATH": str(REPO_ROOT),
                "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
                "REPORT_AGENT_CHECKPOINT_DB_PATH": str(
                    runtime_root / "agent_memory_fastapi.db"
                ),
                "REPORT_AGENT_STATIC_DIR": str(static_dir),
                "REPORT_AGENT_STUDY_ROOT": str(environment_root / "study_data"),
                "REPORT_AGENT_API_WORKFLOW_TIMEOUT_SECONDS": str(
                    args.timeout_seconds
                ),
            }
        )
        api_log = (artifact_dir / "api.log").open("w", encoding="utf-8")
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
            stdout=api_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except BaseException as error:
        (artifact_dir / "failure-traceback.txt").write_text(
            "".join(traceback.format_exception(error)),
            encoding="utf-8",
        )
        if api_log is not None:
            api_log.close()
        print(f"FAIL agent activity timeline smoke; diagnostics: {artifact_dir}")
        raise

    from playwright.sync_api import sync_playwright

    page: Any | None = None
    diagnostics_written = False
    try:
        _wait_for_health(api_url, deadline, process)
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)

            def record_browser_failure(error: BaseException) -> None:
                nonlocal diagnostics_written
                _write_failure_diagnostics(
                    artifact_dir=artifact_dir,
                    api_url=api_url,
                    page=page,
                    error=error,
                )
                diagnostics_written = True

            with _diagnostic_browser(browser, record_browser_failure):
                page = browser.new_page(viewport={"width": 1440, "height": 950})
                page.goto(
                    api_url,
                    wait_until="networkidle",
                    timeout=_remaining_ms(deadline),
                )
                field = page.get_by_label(MESSAGE_LABEL)
                field.wait_for(timeout=_remaining_ms(deadline))
                field.fill(args.query)
                page.get_by_role("button", name="Send", exact=True).click()

                _wait_for_dataset_plan_review(
                    page,
                    api_url=api_url,
                    deadline=deadline,
                )

                timeline = page.get_by_label(
                    "Agent activity timeline",
                    exact=True,
                ).last
                timeline.wait_for(timeout=_remaining_ms(deadline))
                _wait_for_timeline_label(
                    timeline,
                    "Searching the data catalog",
                    deadline=deadline,
                )
                _wait_for_timeline_label(
                    timeline,
                    "Waiting for dataset plan review",
                    deadline=deadline,
                )
                _assert_plain_language_timeline(timeline.inner_text())

                state = _thread_state(api_url)
                _assert_waiting_activity_state(state)
                (artifact_dir / "waiting-api-state.json").write_text(
                    json.dumps(state, indent=2),
                    encoding="utf-8",
                )
                _write_page_artifacts(page, artifact_dir, "waiting")
    except BaseException as error:
        if not diagnostics_written:
            _write_failure_diagnostics(
                artifact_dir=artifact_dir,
                api_url=api_url,
                page=page,
                error=error,
            )
        print(f"FAIL agent activity timeline smoke; diagnostics: {artifact_dir}")
        raise
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if api_log is not None:
            api_log.close()

    print(f"PASS agent activity timeline smoke; diagnostics: {artifact_dir}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real agent activity timeline browser smoke once."
    )
    parser.add_argument("--api-port", type=int, default=8870)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument(
        "--environment-root",
        type=Path,
        default=REPO_ROOT,
        help="Project root whose .env and installed study_data should be used.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args()))
