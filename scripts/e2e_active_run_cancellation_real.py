#!/usr/bin/env python3
"""Exercise active-run cancellation through the real API and compiled UI."""

from __future__ import annotations

import argparse
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
    thread_id = items[0]["thread_id"]
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state",
        headers=LOCAL_API_HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return response.json()


def _write_failure_diagnostics(
    *,
    artifact_dir: Path,
    api_url: str,
    page: Any | None,
    error: BaseException,
) -> None:
    (artifact_dir / "failure.txt").write_text(
        "".join(traceback.format_exception(error)),
        encoding="utf-8",
    )
    if page is not None:
        try:
            (artifact_dir / "failure-page.txt").write_text(
                page.locator("body").inner_text(),
                encoding="utf-8",
            )
            page.screenshot(
                path=str(artifact_dir / "failure-screenshot.png"),
                full_page=True,
            )
        except Exception:
            pass
    try:
        state = _thread_state(api_url)
        (artifact_dir / "failure-state.json").write_text(
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
        else Path(tempfile.mkdtemp(prefix="active-run-cancellation-smoke-"))
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
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    from playwright.sync_api import sync_playwright

    page: Any | None = None
    try:
        _wait_for_health(api_url, deadline, process)
        attachment = artifact_dir / "cancel-smoke.csv"
        attachment.write_text(
            "participant_id,exposure,outcome\n1,yes,no\n2,no,yes\n",
            encoding="utf-8",
        )
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                page = browser.new_page()
                page.goto(
                    api_url,
                    wait_until="networkidle",
                    timeout=_remaining_ms(deadline),
                )
                page.get_by_label(MESSAGE_LABEL).wait_for(
                    timeout=_remaining_ms(deadline)
                )
                page.get_by_test_id("attachment-file-input").set_input_files(
                    str(attachment)
                )
                page.get_by_text("cancel-smoke.csv", exact=True).first.wait_for(
                    timeout=_remaining_ms(deadline)
                )
                page.get_by_label(MESSAGE_LABEL).fill(
                    "Inspect the attached cohort and perform a careful analysis of "
                    "the exposure and outcome relationship."
                )
                page.get_by_role("button", name="Send", exact=True).click()

                cancel_button = page.get_by_role(
                    "button",
                    name="Cancel run",
                    exact=True,
                )
                cancel_button.wait_for(timeout=_remaining_ms(deadline))
                cancel_button.click()
                cancelled_badge = page.get_by_label(
                    "Message status: Cancelled",
                    exact=True,
                )
                cancelled_badge.wait_for(timeout=_remaining_ms(deadline))
                if not page.get_by_label(MESSAGE_LABEL).is_enabled():
                    raise AssertionError("Composer stayed disabled after cancellation.")
                page.get_by_text("cancel-smoke.csv", exact=True).first.wait_for(
                    timeout=_remaining_ms(deadline)
                )

                state = _thread_state(api_url)
                (artifact_dir / "cancelled-state.json").write_text(
                    json.dumps(state, indent=2),
                    encoding="utf-8",
                )
                if state["run"]["state"] != "cancelled":
                    raise AssertionError(f"Unexpected run state: {state['run']!r}")
                cancelled_messages = [
                    message
                    for message in state.get("conversation") or []
                    if message.get("status") == "cancelled"
                ]
                if len(cancelled_messages) != 1:
                    raise AssertionError(
                        f"Expected one cancelled message: {state['conversation']!r}"
                    )
                input_attachments = [
                    item
                    for item in cancelled_messages[0].get("attachments") or []
                    if item.get("relationship") == "input"
                    and item.get("filename") == "cancel-smoke.csv"
                ]
                if len(input_attachments) != 1:
                    raise AssertionError(
                        "The cancelled message did not retain its input attachment."
                    )

                time.sleep(2)
                late_state = _thread_state(api_url)
                if late_state["run"]["state"] != "cancelled":
                    raise AssertionError(
                        f"Late work replaced cancellation: {late_state['run']!r}"
                    )
                if any(
                    message.get("role") == "assistant"
                    for message in late_state.get("conversation") or []
                ):
                    raise AssertionError("A partial assistant result survived cancellation.")
            finally:
                browser.close()
    except BaseException as error:
        _write_failure_diagnostics(
            artifact_dir=artifact_dir,
            api_url=api_url,
            page=page,
            error=error,
        )
        print(f"FAIL active-run cancellation smoke; diagnostics: {artifact_dir}")
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        api_log.close()

    print(
        "PASS active-run cancellation browser smoke; "
        f"diagnostics: {artifact_dir}"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real active-run cancellation browser smoke once."
    )
    parser.add_argument("--api-port", type=int, default=8860)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument(
        "--environment-root",
        type=Path,
        default=REPO_ROOT,
        help="Project root whose .env and installed study_data should be used.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args()))
