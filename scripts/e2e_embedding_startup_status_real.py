#!/usr/bin/env python3
"""Verify embedding startup status through real FastAPI and compiled UI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from typing import Any
from urllib.parse import urlparse

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.auth import LOCAL_SESSION_ID
from scripts.e2e_process_harness import (
    ManagedProcess,
    _find_available_port,
    _start_process,
    _terminate_process,
    _wait_for_http,
)
from utils.env_loader import load_app_environment


LOCAL_API_HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
PROBE_LOG_MARKER = "Embedding startup probe completed"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--api-port", type=int, default=8765)
    parser.add_argument("--environment-root", type=Path)
    return parser


def _environment_root(value: Path | None) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    if (REPO_ROOT / "study_data").is_dir():
        return REPO_ROOT
    linked_checkout_root = REPO_ROOT.parents[1]
    if (linked_checkout_root / "study_data").is_dir():
        return linked_checkout_root
    return REPO_ROOT


def _remaining_ms(deadline: float) -> int:
    return max(1, min(30_000, int((deadline - time.monotonic()) * 1000)))


def _launch_browser(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch()
    except Exception as error:
        if "Executable doesn't exist" not in str(error):
            raise
        return playwright.chromium.launch(channel="chrome")


def _json_get(url: str) -> dict[str, Any]:
    response = requests.get(url, headers=LOCAL_API_HEADERS, timeout=10)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected JSON object from {url}.")
    return payload


def _thread_id_from_attachment_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) != 4 or parts[:2] != ["api", "threads"] or parts[3] != "attachments":
        raise ValueError("Expected a thread attachment response URL.")
    thread_id = parts[2].strip()
    if not thread_id:
        raise ValueError("Expected a thread ID in the attachment response URL.")
    return thread_id


def _start_application(
    *,
    name: str,
    port: int,
    runtime_root: Path,
    study_root: Path,
    static_dir: Path,
    openai_key: str,
    log_path: Path,
) -> ManagedProcess:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "OPENAI_API_KEY": openai_key,
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(
                runtime_root / "agent_memory_fastapi.db"
            ),
            "REPORT_AGENT_STUDY_ROOT": str(study_root),
            "REPORT_AGENT_STATIC_DIR": str(static_dir),
        }
    )
    return _start_process(
        name=name,
        args=[
            sys.executable,
            "-m",
            "uvicorn",
            "api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--log-level",
            "info",
        ],
        cwd=REPO_ROOT,
        env=environment,
        log_path=log_path,
    )


def _write_browser_artifacts(page: Any, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "page.txt").write_text(
        page.locator("body").inner_text(timeout=5000),
        encoding="utf-8",
    )
    (directory / "page.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(directory / "screenshot.png"), full_page=True)


def _exercise_instance(
    *,
    browser: Any,
    api_url: str,
    deadline: float,
    artifact_dir: Path,
    expected_available: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    options = _json_get(f"{api_url}/api/runtime/options")
    status = dict(options.get("embedding_startup_status") or {})
    expected_mode = (
        "hybrid_vector_lexical" if expected_available else "lexical_fallback"
    )
    if status.get("available") is not expected_available:
        raise AssertionError(f"Unexpected runtime embedding status: {status!r}")
    if status.get("retrieval_mode") != expected_mode:
        raise AssertionError(f"Unexpected runtime retrieval mode: {status!r}")

    page = browser.new_page(viewport={"width": 1440, "height": 950})
    try:
        page.goto(api_url, wait_until="networkidle", timeout=_remaining_ms(deadline))
        file_input = page.get_by_test_id("attachment-file-input")
        file_input.wait_for(timeout=_remaining_ms(deadline))
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/attachments")
                and "/api/threads/" in response.url
            ),
            timeout=_remaining_ms(deadline),
        ) as attachment_response_info:
            file_input.set_input_files(
                {
                    "name": "embedding-status-probe.txt",
                    "mimeType": "text/plain",
                    "buffer": b"non-sensitive startup status smoke",
                }
            )
        attachment_response = attachment_response_info.value
        if not attachment_response.ok:
            raise AssertionError(
                f"Attachment upload failed with HTTP {attachment_response.status}."
            )
        page.get_by_text("embedding-status-probe.txt", exact=True).wait_for(
            timeout=_remaining_ms(deadline)
        )
        thread_id = _thread_id_from_attachment_url(attachment_response.url)
        thread_state = _json_get(f"{api_url}/api/threads/{thread_id}/state")
        thread_status = dict(thread_state.get("embedding_startup_status") or {})
        if thread_status != status:
            raise AssertionError(
                "Runtime options and thread state expose different startup status."
            )

        notices = page.locator(".embedding-fallback-notice")
        expected_notice_count = 0 if expected_available else 1
        if notices.count() != expected_notice_count:
            raise AssertionError(
                f"Expected {expected_notice_count} fallback notice(s), received "
                f"{notices.count()}."
            )
        if expected_available:
            if status.get("message"):
                raise AssertionError(f"Successful startup was not silent: {status!r}")
        else:
            message = str(status.get("message") or "")
            if "OpenAI text-embedding-3-large is not configured" not in message:
                raise AssertionError(f"Fallback message is not profile-specific: {status!r}")
            if notices.inner_text().strip() != message:
                raise AssertionError("Rendered fallback notice differs from API status.")

        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "runtime-options.json").write_text(
            json.dumps(options, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (artifact_dir / "thread-state.json").write_text(
            json.dumps(thread_state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _write_browser_artifacts(page, artifact_dir)
        return options, thread_state
    finally:
        page.close()


def _assert_secret_absent(directory: Path, secret: str) -> None:
    if not secret:
        return
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix == ".png":
            continue
        if secret in path.read_text(encoding="utf-8", errors="replace"):
            raise AssertionError(f"Credential value leaked into {path.name}.")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout_seconds > 300:
        raise ValueError("The feature smoke is limited to five minutes.")
    deadline = time.monotonic() + args.timeout_seconds
    artifact_root = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="embedding-startup-status-smoke-"))
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    environment_root = _environment_root(args.environment_root)
    load_app_environment(environment_root)
    openai_key = str(os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if not openai_key:
        raise RuntimeError("A real OPENAI_API_KEY is required for this smoke.")
    static_dir = REPO_ROOT / "frontend" / "dist"
    if not (static_dir / "index.html").is_file():
        raise RuntimeError("Build frontend/dist before running this smoke.")
    study_root = environment_root / "study_data"

    from playwright.sync_api import sync_playwright

    running: ManagedProcess | None = None
    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                success_dir = artifact_root / "success"
                success_runtime = success_dir / "runtime"
                success_runtime.mkdir(parents=True, exist_ok=True)
                success_port = _find_available_port("127.0.0.1", args.api_port)
                running = _start_application(
                    name="embedding-success-api",
                    port=success_port,
                    runtime_root=success_runtime,
                    study_root=study_root,
                    static_dir=static_dir,
                    openai_key=openai_key,
                    log_path=success_dir / "api.log",
                )
                success_url = f"http://127.0.0.1:{success_port}"
                _wait_for_http(
                    f"{success_url}/api/health",
                    deadline=deadline,
                    name="embedding success API",
                    expected_status=200,
                    processes=[running],
                )
                _exercise_instance(
                    browser=browser,
                    api_url=success_url,
                    deadline=deadline,
                    artifact_dir=success_dir,
                    expected_available=True,
                )
                warning = _terminate_process(running, deadline=deadline)
                running = None
                if warning:
                    raise RuntimeError(warning)
                success_log = (success_dir / "api.log").read_text(
                    encoding="utf-8", errors="replace"
                )
                if success_log.count(PROBE_LOG_MARKER) != 1:
                    raise AssertionError(
                        "Success application did not record exactly one startup probe."
                    )

                fallback_dir = artifact_root / "fallback"
                fallback_runtime = fallback_dir / "runtime"
                fallback_runtime.mkdir(parents=True, exist_ok=True)
                fallback_port = _find_available_port(
                    "127.0.0.1", success_port + 1
                )
                running = _start_application(
                    name="embedding-fallback-api",
                    port=fallback_port,
                    runtime_root=fallback_runtime,
                    study_root=study_root,
                    static_dir=static_dir,
                    openai_key="",
                    log_path=fallback_dir / "api.log",
                )
                fallback_url = f"http://127.0.0.1:{fallback_port}"
                _wait_for_http(
                    f"{fallback_url}/api/health",
                    deadline=deadline,
                    name="embedding fallback API",
                    expected_status=200,
                    processes=[running],
                )
                _exercise_instance(
                    browser=browser,
                    api_url=fallback_url,
                    deadline=deadline,
                    artifact_dir=fallback_dir,
                    expected_available=False,
                )
                warning = _terminate_process(running, deadline=deadline)
                running = None
                if warning:
                    raise RuntimeError(warning)
                fallback_log = (fallback_dir / "api.log").read_text(
                    encoding="utf-8", errors="replace"
                )
                if PROBE_LOG_MARKER in fallback_log:
                    raise AssertionError(
                        "Missing-key application made an embedding provider probe."
                    )
            finally:
                browser.close()
        _assert_secret_absent(artifact_root, openai_key)
    except BaseException as error:
        if running is not None:
            _terminate_process(running, deadline=deadline)
        (artifact_root / "failure-traceback.txt").write_text(
            "".join(traceback.format_exception(error)),
            encoding="utf-8",
        )
        print(f"FAIL embedding startup status smoke; diagnostics: {artifact_root}")
        raise

    print(
        "PASS embedding startup status smoke: one real probe, silent hybrid UI, "
        f"and one lexical fallback notice; artifacts: {artifact_root}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
