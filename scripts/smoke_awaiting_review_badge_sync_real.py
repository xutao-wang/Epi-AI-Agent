#!/usr/bin/env python3
"""Exercise saved-conversation review-badge synchronization end to end."""

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
from typing import Any, Callable

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.auth import LOCAL_SESSION_ID
from scripts.e2e_agent_activity_timeline_real import (
    DEFAULT_QUERY,
    MESSAGE_LABEL,
    _diagnostic_browser,
    _find_port,
    _launch_browser,
    _remaining_ms,
    _wait_for_dataset_plan_review,
    _wait_for_health,
    _write_page_artifacts,
)


LOCAL_API_HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}


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


def _wait_for(
    reader: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    deadline: float,
    description: str,
) -> Any:
    latest: Any = None
    while time.monotonic() < deadline:
        latest = reader()
        if predicate(latest):
            return latest
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {description}: {latest!r}")


def _write_failure(
    artifact_dir: Path,
    *,
    api_url: str,
    thread_id: str | None,
    page: Any | None,
    error: BaseException,
) -> None:
    (artifact_dir / "failure.txt").write_text(
        "".join(traceback.format_exception(error)),
        encoding="utf-8",
    )
    if page is not None:
        try:
            _write_page_artifacts(page, artifact_dir, "failure")
        except Exception:
            pass
    try:
        (artifact_dir / "failure-conversations.json").write_text(
            json.dumps(_conversations(api_url), indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    if thread_id is not None:
        try:
            (artifact_dir / "failure-state.json").write_text(
                json.dumps(_thread_state(api_url, thread_id), indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def _approve_plan(page: Any, *, deadline: float) -> None:
    for _ in range(20):
        final = page.get_by_role(
            "button",
            name="Approve plan and extract",
            exact=True,
        )
        if final.is_visible(timeout=100):
            final.click(timeout=_remaining_ms(deadline))
            return
        step = page.get_by_role(
            "button",
            name="Approve & continue",
            exact=True,
        )
        if not step.is_visible(timeout=100):
            raise AssertionError("Dataset-plan approval controls disappeared.")
        step.click(timeout=_remaining_ms(deadline))
    raise AssertionError("Dataset-plan review did not reach its final concept.")


def run(args: argparse.Namespace) -> int:
    if args.timeout_seconds > 300:
        raise ValueError("The feature smoke is limited to five minutes.")
    deadline = time.monotonic() + args.timeout_seconds
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="awaiting-review-badge-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    api_url = "http://127.0.0.1:0"
    process: subprocess.Popen[Any] | None = None
    api_log: Any | None = None
    page: Any | None = None
    thread_id: str | None = None

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

    try:
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
        _wait_for_health(api_url, deadline, process)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)

            def record_failure(error: BaseException) -> None:
                _write_failure(
                    artifact_dir,
                    api_url=api_url,
                    thread_id=thread_id,
                    page=page,
                    error=error,
                )

            with _diagnostic_browser(browser, record_failure):
                page = browser.new_page(viewport={"width": 1440, "height": 950})
                page.goto(
                    api_url,
                    wait_until="networkidle",
                    timeout=_remaining_ms(deadline),
                )
                field = page.get_by_label(MESSAGE_LABEL)
                field.wait_for(timeout=_remaining_ms(deadline))
                field.fill(DEFAULT_QUERY)
                page.get_by_role("button", name="Send", exact=True).click()
                _wait_for_dataset_plan_review(
                    page,
                    api_url=api_url,
                    deadline=deadline,
                )

                items = _wait_for(
                    lambda: _conversations(api_url),
                    lambda current: len(current) == 1,
                    deadline=deadline,
                    description="saved conversation",
                )
                thread_id = str(items[0]["thread_id"])
                if items[0].get("awaiting_review") is not True:
                    raise AssertionError(
                        f"Expected an awaiting-review summary: {items[0]!r}"
                    )
                page.get_by_text("Awaiting review", exact=True).wait_for(
                    timeout=_remaining_ms(deadline)
                )
                (artifact_dir / "waiting-state.json").write_text(
                    json.dumps(_thread_state(api_url, thread_id), indent=2),
                    encoding="utf-8",
                )

                _approve_plan(page, deadline=deadline)
                resumed = _wait_for(
                    lambda: _thread_state(api_url, thread_id),
                    lambda state: state.get("active_interrupt") is None
                    and (state.get("run") or {}).get("state") == "running",
                    deadline=deadline,
                    description="a running post-review state",
                )
                (artifact_dir / "resumed-state.json").write_text(
                    json.dumps(resumed, indent=2),
                    encoding="utf-8",
                )
                page.get_by_text("Awaiting review", exact=True).wait_for(
                    state="detached",
                    timeout=_remaining_ms(deadline),
                )
                resumed_summary = _wait_for(
                    lambda: _conversations(api_url),
                    lambda current: len(current) == 1
                    and current[0].get("awaiting_review") is False,
                    deadline=deadline,
                    description="the cleared review badge summary",
                )
                (artifact_dir / "resumed-conversations.json").write_text(
                    json.dumps(resumed_summary, indent=2),
                    encoding="utf-8",
                )

                cancel = page.get_by_role(
                    "button",
                    name="Cancel run",
                    exact=True,
                )
                cancel.wait_for(timeout=_remaining_ms(deadline))
                cancel.click()
                page.get_by_label("Message status: Cancelled", exact=True).wait_for(
                    timeout=_remaining_ms(deadline)
                )
                if not field.is_enabled():
                    raise AssertionError("Composer stayed disabled after cancellation.")
                cancelled = _wait_for(
                    lambda: _thread_state(api_url, thread_id),
                    lambda state: state.get("active_interrupt") is None
                    and (state.get("run") or {}).get("state") == "cancelled",
                    deadline=deadline,
                    description="the cancelled thread state",
                )
                cancelled_summary = _wait_for(
                    lambda: _conversations(api_url),
                    lambda current: len(current) == 1
                    and current[0].get("awaiting_review") is False,
                    deadline=deadline,
                    description="the cleared review badge after cancellation",
                )
                if page.get_by_text("Awaiting review", exact=True).count():
                    raise AssertionError("The sidebar review badge remained after cancellation.")
                (artifact_dir / "cancelled-state.json").write_text(
                    json.dumps(cancelled, indent=2),
                    encoding="utf-8",
                )
                (artifact_dir / "cancelled-conversations.json").write_text(
                    json.dumps(cancelled_summary, indent=2),
                    encoding="utf-8",
                )
                _write_page_artifacts(page, artifact_dir, "passing")
    except BaseException as error:
        if not (artifact_dir / "failure.txt").exists():
            _write_failure(
                artifact_dir,
                api_url=api_url,
                thread_id=thread_id,
                page=page,
                error=error,
            )
        print(f"FAIL awaiting-review badge smoke; diagnostics: {artifact_dir}")
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

    print(f"PASS awaiting-review badge smoke; diagnostics: {artifact_dir}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real awaiting-review badge synchronization smoke once."
    )
    parser.add_argument("--api-port", type=int, default=8890)
    parser.add_argument("--timeout-seconds", type=int, default=300)
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
