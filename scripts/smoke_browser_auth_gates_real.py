#!/usr/bin/env python3
"""Exercise the compiled browser auth gates against the real local FastAPI app."""

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
from playwright.sync_api import Page, sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e_process_harness import (
    _find_available_port,
    _start_process,
    _terminate_process,
    _wait_for_http,
)
from utils.env_loader import load_app_environment


LOCAL_SESSION_ID = "00000000-0000-4000-8000-000000000001"
TAB_SESSION_STORAGE_KEY = "report-agent.tab-session-id"


def _timeout_seconds(value: str) -> float:
    timeout = float(value)
    if timeout <= 0 or timeout > 300:
        raise argparse.ArgumentTypeError(
            "timeout must be greater than zero and no more than 300 seconds"
        )
    return timeout


def _remaining_seconds(deadline: float, *, cap: float = 30.0) -> float:
    return max(0.1, min(cap, deadline - time.monotonic()))


def _remaining_ms(deadline: float, *, cap: int = 30_000) -> int:
    return max(1, int(_remaining_seconds(deadline, cap=cap / 1000) * 1000))


def _write_failure_diagnostics(
    *,
    artifact_dir: Path,
    error: BaseException,
    page: Page | None,
    requests_seen: list[dict[str, Any]],
) -> None:
    (artifact_dir / "failure.txt").write_text(
        "".join(traceback.format_exception(error)),
        encoding="utf-8",
    )
    (artifact_dir / "failure-requests.json").write_text(
        json.dumps(requests_seen, indent=2),
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


def run(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout_seconds
    artifact_dir = (
        Path(args.artifact_dir)
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="report-browser-auth-gates-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)

    load_app_environment(REPO_ROOT)
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError(
            "This real local-mode smoke requires an existing OPENAI_API_KEY; "
            "it does not create or persist one."
        )

    subprocess.run(
        ["npm", "--prefix", "frontend", "run", "build"],
        cwd=REPO_ROOT,
        check=True,
        timeout=_remaining_seconds(deadline, cap=60),
    )

    host = "127.0.0.1"
    port = _find_available_port(host, args.api_port)
    app_url = f"http://{host}:{port}"
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "REPORT_AGENT_AUTH_MODE": "local",
            "REPORT_AGENT_STATIC_DIR": str(REPO_ROOT / "frontend" / "dist"),
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(
                runtime_root / "agent_memory_fastapi.db"
            ),
        }
    )

    process = _start_process(
        name="FastAPI",
        args=[
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
        log_path=artifact_dir / "api.log",
    )
    page: Page | None = None
    requests_seen: list[dict[str, Any]] = []
    try:
        _wait_for_http(
            f"{app_url}/api/health",
            deadline=min(deadline, time.monotonic() + 60),
            name="FastAPI health",
            expected_status=200,
            processes=[process],
        )

        raw_config = requests.get(
            f"{app_url}/api/public-config",
            timeout=_remaining_seconds(deadline),
        )
        raw_config.raise_for_status()
        if raw_config.json() != {
            "auth_mode": "local",
            "provider_key_required": False,
            "cognito": None,
        }:
            raise AssertionError(f"Unexpected public config: {raw_config.json()!r}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})

                def record_request(request: Any) -> None:
                    if "/api/" in request.url:
                        requests_seen.append(
                            {
                                "url": request.url,
                                "method": request.method,
                                "headers": dict(request.headers),
                            }
                        )

                page.on("request", record_request)
                page.goto(
                    app_url,
                    wait_until="networkidle",
                    timeout=_remaining_ms(deadline),
                )
                page.get_by_label("Model").wait_for(timeout=_remaining_ms(deadline))

                if page.get_by_role("button", name="Sign in").count():
                    raise AssertionError("Local mode rendered the Cognito sign-in gate.")
                if page.get_by_label("OpenAI API key").count():
                    raise AssertionError("Local mode rendered the provider-key gate.")

                page.get_by_role("button", name="Hide sidebar").click()
                page.get_by_role("button", name="Show sidebar").click()
                page.get_by_label("Model").wait_for(timeout=_remaining_ms(deadline))

                public_requests = [
                    item
                    for item in requests_seen
                    if item["url"].endswith("/api/public-config")
                ]
                if len(public_requests) != 1:
                    raise AssertionError(
                        f"Expected one raw public-config request: {requests_seen!r}"
                    )
                public_headers = public_requests[0]["headers"]
                if "authorization" in public_headers or "x-epi-session-id" in public_headers:
                    raise AssertionError(
                        "Public config was fetched with protected-request headers."
                    )

                protected_requests = [
                    item
                    for item in requests_seen
                    if item["url"].endswith("/api/runtime/options")
                    or item["url"].endswith("/api/conversations")
                ]
                if not protected_requests:
                    raise AssertionError("The mounted App made no protected API requests.")
                for item in protected_requests:
                    headers = item["headers"]
                    if headers.get("x-epi-session-id") != LOCAL_SESSION_ID:
                        raise AssertionError(
                            f"Missing fixed local session header: {item!r}"
                        )
                    if "authorization" in headers:
                        raise AssertionError(
                            f"Local request unexpectedly used a bearer token: {item!r}"
                        )

                session_values = page.evaluate(
                    "Object.fromEntries(Object.entries(window.sessionStorage))"
                )
                local_values = page.evaluate(
                    "Object.fromEntries(Object.entries(window.localStorage))"
                )
                if TAB_SESSION_STORAGE_KEY in session_values:
                    raise AssertionError("Local mode persisted a random tab session ID.")
                storage_text = json.dumps(
                    {"session": session_values, "local": local_values}
                ).lower()
                if "api_key" in storage_text or "openai api key" in storage_text:
                    raise AssertionError("Browser storage contains provider-key metadata.")

                raw_runtime = requests.get(
                    f"{app_url}/api/runtime/options",
                    headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
                    timeout=_remaining_seconds(deadline),
                )
                raw_runtime.raise_for_status()
                if "defaults" not in raw_runtime.json():
                    raise AssertionError("Raw runtime options response is malformed.")

                (artifact_dir / "final-page.txt").write_text(
                    page.locator("body").inner_text(),
                    encoding="utf-8",
                )
                (artifact_dir / "requests.json").write_text(
                    json.dumps(requests_seen, indent=2),
                    encoding="utf-8",
                )
                page.screenshot(
                    path=str(artifact_dir / "final-screenshot.png"),
                    full_page=True,
                )
            finally:
                browser.close()

        print(
            "PASS real browser auth-gates smoke; "
            f"artifacts: {artifact_dir}",
            flush=True,
        )
        return 0
    except BaseException as error:
        _write_failure_diagnostics(
            artifact_dir=artifact_dir,
            error=error,
            page=page,
            requests_seen=requests_seen,
        )
        print(
            f"FAIL: {type(error).__name__}: {error}; artifacts: {artifact_dir}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        warning = _terminate_process(process)
        if warning:
            print(f"WARN: {warning}", file=sys.stderr, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real compiled-browser authentication gate smoke."
    )
    parser.add_argument("--api-port", type=int, default=8070)
    parser.add_argument("--timeout-seconds", type=_timeout_seconds, default=180)
    parser.add_argument("--artifact-dir", default="")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
