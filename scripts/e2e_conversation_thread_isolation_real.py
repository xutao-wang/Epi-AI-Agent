#!/usr/bin/env python3
"""Exercise conversation isolation through the real API and compiled UI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
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

from scripts.e2e_agent_activity_timeline_real import (
    DEFAULT_QUERY,
    LOCAL_API_HEADERS,
    MESSAGE_LABEL,
    _diagnostic_browser,
    _find_port,
    _launch_browser,
    _remaining_ms,
    _wait_for_dataset_plan_review,
    _wait_for_health,
    _write_page_artifacts,
)


THREAD_B_MESSAGE = "Who are you? Answer in one sentence."


def _conversations(api_url: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{api_url}/api/conversations",
        headers=LOCAL_API_HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return list(response.json().get("items") or [])


def _thread_state(api_url: str, thread_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state",
        headers=LOCAL_API_HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    state = response.json()
    if state.get("thread_id") != thread_id:
        raise AssertionError(
            f"Requested {thread_id!r}, received {state.get('thread_id')!r}."
        )
    return state


def _wait_for_conversation_count(
    api_url: str,
    count: int,
    deadline: float,
) -> list[dict[str, Any]]:
    while time.monotonic() < deadline:
        items = _conversations(api_url)
        if len(items) == count:
            return items
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {count} saved conversations.")


def _wait_for_thread_idle(
    api_url: str,
    thread_id: str,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        state = _thread_state(api_url, thread_id)
        if (state.get("run") or {}).get("state") != "running":
            return state
        time.sleep(0.25)
    raise TimeoutError(f"Thread {thread_id} did not stop running.")


def _conversation_text(state: dict[str, Any]) -> set[str]:
    return {
        str(item.get("text") or "")
        for item in state.get("conversation") or []
    }


def _assert_selected_thread(
    page: Any,
    *,
    present: str,
    absent: str,
    deadline: float,
) -> None:
    page.get_by_text(present, exact=True).wait_for(
        timeout=_remaining_ms(deadline),
    )
    if page.get_by_text(absent, exact=True).count():
        raise AssertionError(
            f"Stale conversation content {absent!r} rendered beside {present!r}."
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
        items = _conversations(api_url)
        (artifact_dir / "failure-conversations.json").write_text(
            json.dumps(items, indent=2),
            encoding="utf-8",
        )
        for index, item in enumerate(items, start=1):
            state = _thread_state(api_url, str(item["thread_id"]))
            (artifact_dir / f"failure-thread-{index}.json").write_text(
                json.dumps(state, indent=2),
                encoding="utf-8",
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
        else Path(tempfile.mkdtemp(prefix="conversation-thread-isolation-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)

    from utils.env_loader import load_app_environment

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
            "REPORT_AGENT_API_WORKFLOW_TIMEOUT_SECONDS": str(args.timeout_seconds),
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
    page: Any | None = None
    diagnostics_written = False
    try:
        _wait_for_health(api_url, deadline, process)
        from playwright.sync_api import sync_playwright

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

                first_items = _wait_for_conversation_count(api_url, 1, deadline)
                thread_a = first_items[0]
                if thread_a.get("awaiting_review") is not True:
                    raise AssertionError(
                        f"Thread A lacks awaiting-review status: {thread_a!r}."
                    )
                page.get_by_text("Awaiting review", exact=True).wait_for(
                    timeout=_remaining_ms(deadline)
                )

                page.get_by_role(
                    "button",
                    name="New conversation",
                    exact=True,
                ).click()
                field.fill(THREAD_B_MESSAGE)
                page.get_by_role("button", name="Send", exact=True).click()
                items = _wait_for_conversation_count(api_url, 2, deadline)
                thread_b = next(
                    item for item in items
                    if item["thread_id"] != thread_a["thread_id"]
                )
                state_b = _wait_for_thread_idle(
                    api_url,
                    str(thread_b["thread_id"]),
                    deadline,
                )

                page.get_by_role(
                    "button",
                    name=str(thread_a["title"]),
                    exact=True,
                ).click()
                _assert_selected_thread(
                    page,
                    present=args.query,
                    absent=THREAD_B_MESSAGE,
                    deadline=deadline,
                )
                page.get_by_text(
                    "This conversation was previously paused and is awaiting your review.",
                    exact=True,
                ).wait_for(timeout=_remaining_ms(deadline))

                page.get_by_role(
                    "button",
                    name=str(thread_b["title"]),
                    exact=True,
                ).click()
                _assert_selected_thread(
                    page,
                    present=THREAD_B_MESSAGE,
                    absent=args.query,
                    deadline=deadline,
                )

                state_a = _thread_state(api_url, str(thread_a["thread_id"]))
                if (state_a.get("active_interrupt") or {}).get("type") != (
                    "dataset_plan_review"
                ):
                    raise AssertionError("Thread A did not retain its dataset-plan review.")
                if state_b.get("active_interrupt") is not None:
                    raise AssertionError("Thread B unexpectedly owns Thread A's review.")
                if args.query not in _conversation_text(state_a):
                    raise AssertionError("Thread A lost its submitted message.")
                if args.query in _conversation_text(state_b):
                    raise AssertionError("Thread A's message persisted in Thread B.")
                if THREAD_B_MESSAGE not in _conversation_text(state_b):
                    raise AssertionError("Thread B lost its submitted message.")

                (artifact_dir / "thread-a.json").write_text(
                    json.dumps(state_a, indent=2),
                    encoding="utf-8",
                )
                (artifact_dir / "thread-b.json").write_text(
                    json.dumps(state_b, indent=2),
                    encoding="utf-8",
                )
                _write_page_artifacts(page, artifact_dir, "passing")
    except BaseException as error:
        if not diagnostics_written:
            _write_failure_diagnostics(
                artifact_dir=artifact_dir,
                api_url=api_url,
                page=page,
                error=error,
            )
        print(f"FAIL conversation thread isolation smoke; diagnostics: {artifact_dir}")
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        api_log.close()

    print(f"PASS conversation thread isolation smoke; diagnostics: {artifact_dir}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real conversation thread isolation browser smoke once."
    )
    parser.add_argument("--api-port", type=int, default=8890)
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
