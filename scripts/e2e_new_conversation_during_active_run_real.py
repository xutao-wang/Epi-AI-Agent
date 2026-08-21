#!/usr/bin/env python3
"""Start a new conversation while a real background run remains active."""

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


def _conversations(api_url: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{api_url}/api/conversations",
        headers=LOCAL_API_HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return response.json().get("items") or []


def _thread_state(api_url: str, thread_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state",
        headers=LOCAL_API_HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return response.json()


def _write_failure_diagnostics(
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
        conversations = _conversations(api_url)
        (artifact_dir / "failure-conversations.json").write_text(
            json.dumps(conversations, indent=2),
            encoding="utf-8",
        )
        if conversations:
            state = _thread_state(api_url, conversations[0]["thread_id"])
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
        else Path(tempfile.mkdtemp(prefix="new-conversation-active-run-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)

    from utils.env_loader import load_app_environment

    environment_root = args.environment_root.expanduser().resolve()
    load_app_environment(environment_root)
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("A real OPENAI_API_KEY is required for this smoke.")
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
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                page = browser.new_page()
                page.goto(
                    api_url,
                    wait_until="networkidle",
                    timeout=_remaining_ms(deadline),
                )
                composer = page.get_by_label(MESSAGE_LABEL)
                composer.wait_for(timeout=_remaining_ms(deadline))
                composer.fill(
                    "Inspect the installed epidemiology data and perform a "
                    "careful multi-step analysis of an appropriate outcome."
                )
                page.get_by_role("button", name="Send", exact=True).click()
                page.get_by_role(
                    "button",
                    name="Cancel run",
                    exact=True,
                ).wait_for(timeout=_remaining_ms(deadline))

                new_button = page.get_by_role(
                    "button",
                    name="New conversation",
                    exact=True,
                )
                if not new_button.is_enabled():
                    raise AssertionError(
                        "New conversation was disabled during an active run."
                    )
                duplicate = page.get_by_role(
                    "button",
                    name="Start new conversation from saved conversations",
                )
                if duplicate.count():
                    raise AssertionError(
                        "The saved-conversations duplicate is still rendered."
                    )

                conversations = _conversations(api_url)
                if len(conversations) != 1:
                    raise AssertionError(
                        f"Expected one active conversation: {conversations!r}"
                    )
                thread_id = conversations[0]["thread_id"]
                running_state = _thread_state(api_url, thread_id)
                if running_state["run"]["state"] != "running":
                    raise AssertionError(
                        "The test did not observe an active run: "
                        f"{running_state['run']!r}"
                    )

                new_button.click()
                composer.wait_for(timeout=_remaining_ms(deadline))
                if not composer.is_enabled() or composer.input_value():
                    raise AssertionError(
                        "The blank conversation composer was not ready."
                    )
                detached_state = _thread_state(api_url, thread_id)
                (artifact_dir / "detached-state.json").write_text(
                    json.dumps(detached_state, indent=2),
                    encoding="utf-8",
                )
                if detached_state["run"]["state"] == "cancelled":
                    raise AssertionError(
                        "Starting a new conversation cancelled the previous run."
                    )
            finally:
                browser.close()
    except BaseException as error:
        _write_failure_diagnostics(
            artifact_dir,
            api_url,
            page,
            error,
        )
        print(f"FAIL new-conversation active-run smoke: {artifact_dir}")
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        api_log.close()

    print(f"PASS new-conversation active-run smoke: {artifact_dir}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start a new conversation during a real active run."
    )
    parser.add_argument("--api-port", type=int, default=8861)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument(
        "--environment-root",
        type=Path,
        default=REPO_ROOT,
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args()))
