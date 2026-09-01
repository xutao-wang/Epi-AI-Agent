#!/usr/bin/env python3
"""Verify catalog-backed schemas through real FastAPI and the compiled UI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
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
from utils.env_loader import load_app_environment


REQUEST = (
    "Create a RePORT India Cohort A index-case dataset with participant ID, "
    "age, sex, weight, and alcohol frequency. Use one row per participant and "
    "name the output columns exactly participant_id, baseline_age_years, "
    "baseline_sex, baseline_weight, and baseline_alcohol_frequency."
)
EXPECTED_DESCRIPTIONS = {
    "participant_id": "Participant ID",
    "baseline_age_years": "Age",
    "baseline_sex": "Sex",
    "baseline_weight": "Weight",
}
EXPECTED_SEX_VALUES = {"1": "Female", "2": "Male"}
MESSAGE_LABEL = "Ask a question about your dataset!"
LOCAL_API_HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}


class E2EProcessHarness:
    """Own the real backend process and retain its log for diagnostics."""

    def __init__(self, *, artifact_dir: Path, environment: dict[str, str]) -> None:
        self.artifact_dir = artifact_dir
        self.environment = environment
        self.log_path = artifact_dir / "api.log"
        self._log_handle: Any | None = None
        self.process: subprocess.Popen[str] | None = None

    @staticmethod
    def available_port(host: str, preferred: int) -> int:
        for port in range(preferred, preferred + 100):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
                try:
                    candidate.bind((host, port))
                    return port
                except OSError:
                    continue
        raise RuntimeError("Could not find an available local API port.")

    def start(self, *, host: str, port: int) -> None:
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
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
            env=self.environment,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_until_ready(self, api_url: str, *, deadline: float) -> None:
        if self.process is None:
            raise RuntimeError("Backend process was not started.")
        last_error = ""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"FastAPI exited early with code {self.process.returncode}."
                )
            try:
                response = requests.get(
                    f"{api_url}/api/health",
                    timeout=_request_timeout(deadline, maximum=2),
                )
                if response.status_code == 200:
                    return
                last_error = f"HTTP {response.status_code}"
            except requests.RequestException as error:
                last_error = f"{type(error).__name__}: {error}"
            time.sleep(0.25)
        raise TimeoutError(f"FastAPI did not become ready: {last_error}")

    def close(self, *, deadline: float) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=_remaining_seconds(deadline))
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=_remaining_seconds(deadline))
                except subprocess.TimeoutExpired:
                    pass
        if self._log_handle is not None:
            self._log_handle.close()


def _remaining_ms(deadline: float) -> int:
    return max(1, int((deadline - time.monotonic()) * 1000))


def _remaining_seconds(deadline: float) -> float:
    return max(0.01, deadline - time.monotonic())


def _request_timeout(deadline: float, *, maximum: float = 10) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Dataset schema smoke deadline reached.")
    return max(0.05, min(maximum, remaining))


def _launch_browser(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch()
    except Exception as error:
        if "Executable doesn't exist" not in str(error):
            raise
        return playwright.chromium.launch(channel="chrome")


def _thread_id(api_url: str, *, deadline: float) -> str:
    while time.monotonic() < deadline:
        response = requests.get(
            f"{api_url}/api/conversations",
            headers=LOCAL_API_HEADERS,
            timeout=_request_timeout(deadline),
        )
        response.raise_for_status()
        items = list(response.json().get("items") or [])
        if len(items) == 1:
            thread_id = str(dict(items[0]).get("thread_id") or "").strip()
            if thread_id:
                return thread_id
        elif len(items) > 1:
            raise AssertionError(
                f"Isolated smoke runtime exposed multiple threads: {items!r}"
            )
        time.sleep(0.25)
    raise TimeoutError("Could not resolve the active thread ID.")


def _state(
    api_url: str,
    thread_id: str,
    *,
    deadline: float,
) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state",
        headers=LOCAL_API_HEADERS,
        timeout=_request_timeout(deadline),
    )
    response.raise_for_status()
    return dict(response.json())


def _schema(
    api_url: str,
    thread_id: str,
    dataset_id: str,
    *,
    deadline: float,
) -> dict[str, object]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/datasets/{dataset_id}/schema",
        headers=LOCAL_API_HEADERS,
        timeout=_request_timeout(deadline),
    )
    response.raise_for_status()
    return dict(response.json().get("schema") or {})


def _click_when_enabled(page: Any, name: str, *, deadline: float) -> None:
    button = page.get_by_role("button", name=name, exact=True)
    while time.monotonic() < deadline:
        if button.count() and button.is_visible() and button.is_enabled():
            button.click(timeout=_remaining_ms(deadline))
            return
        time.sleep(0.25)
    raise AssertionError(f"Button {name!r} did not become enabled.")


def _handle_clarification(page: Any, *, deadline: float) -> None:
    page.get_by_role("heading", name="Clarification needed").wait_for(
        timeout=_remaining_ms(deadline)
    )
    page.get_by_role("radio", name="Let the agent decide").click(
        timeout=_remaining_ms(deadline)
    )
    _click_when_enabled(page, "Continue", deadline=deadline)


def _handle_plan_review(
    page: Any,
    interrupt: dict[str, Any],
    *,
    deadline: float,
) -> None:
    page.get_by_role("heading", name="Review dataset plan").wait_for(
        timeout=_remaining_ms(deadline)
    )
    groups = [
        group
        for group in list(dict(interrupt.get("view") or {}).get("concept_groups") or [])
        if isinstance(group, dict)
    ]
    if not groups:
        raise AssertionError("Dataset plan has no reviewable concept groups.")
    for index in range(len(groups)):
        button_name = (
            "Approve plan and extract"
            if index == len(groups) - 1
            else "Approve & continue"
        )
        _click_when_enabled(page, button_name, deadline=deadline)


def _assert_persisted_schema(schema: dict[str, object]) -> None:
    for alias, description in EXPECTED_DESCRIPTIONS.items():
        metadata = dict(schema.get(alias) or {})
        if metadata.get("description") != description:
            raise AssertionError(
                f"{alias} description mismatch: {metadata.get('description')!r}"
            )
        if not str(metadata.get("dataType") or "").strip():
            raise AssertionError(f"{alias} has no persisted physical dtype.")
    sex_values = dict(dict(schema["baseline_sex"]).get("values") or {})
    if sex_values != EXPECTED_SEX_VALUES:
        raise AssertionError(f"Unexpected baseline sex values: {sex_values!r}")


def _assert_rendered_schema(page: Any, *, deadline: float) -> None:
    table = page.get_by_role("table", name="Dataset schema").last
    table.wait_for(timeout=_remaining_ms(deadline))
    body = table.inner_text()
    for alias, description in EXPECTED_DESCRIPTIONS.items():
        if alias not in body or description not in body:
            raise AssertionError(
                f"Rendered schema omitted {alias!r} or {description!r}."
            )
    raw_summary = page.get_by_text("Raw schema", exact=True).last
    raw_summary.wait_for(timeout=_remaining_ms(deadline))
    if raw_summary.locator("xpath=..").get_attribute("open") is not None:
        raise AssertionError("Raw schema must be collapsed by default.")


def _assert_pending_review(
    page: Any,
    *,
    api_url: str,
    thread_id: str,
    interrupt: dict[str, Any],
    deadline: float,
) -> str:
    artifact = dict(interrupt.get("artifact") or {})
    dataset_id = str(artifact.get("id") or "").strip()
    if not dataset_id or artifact.get("expected_status") != "pending_review":
        raise AssertionError("Dataset review has no pending artifact identity.")
    _assert_persisted_schema(
        _schema(api_url, thread_id, dataset_id, deadline=deadline)
    )
    page.get_by_role("heading", name="Review extracted dataset").wait_for(
        timeout=_remaining_ms(deadline)
    )
    schema_summary = page.locator("summary").filter(
        has_text=re.compile(r"^Schema$")
    ).last
    schema_summary.click(timeout=_remaining_ms(deadline))
    _assert_rendered_schema(page, deadline=deadline)
    return dataset_id


def _assert_approved_attachment(
    page: Any,
    state: dict[str, Any],
    *,
    dataset_id: str,
    deadline: float,
) -> None:
    attachments = [
        attachment
        for message in list(state.get("conversation") or [])
        if isinstance(message, dict) and message.get("role") == "assistant"
        for attachment in list(message.get("attachments") or [])
        if isinstance(attachment, dict)
        and attachment.get("id") == dataset_id
        and attachment.get("relationship") == "output"
    ]
    if len(attachments) != 1:
        raise AssertionError("Approved dataset was not attached exactly once.")
    details_summary = page.locator("summary").filter(
        has_text=re.compile(r"^Dataset details$")
    ).last
    details_summary.wait_for(timeout=_remaining_ms(deadline))
    details_summary.click(timeout=_remaining_ms(deadline))
    _assert_rendered_schema(page, deadline=deadline)


def _write_diagnostics(
    artifact_dir: Path,
    *,
    api_url: str,
    page: Any | None,
    thread_id: str | None,
    deadline: float,
    error: BaseException | None = None,
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if page is not None:
        try:
            (artifact_dir / "page.txt").write_text(
                page.locator("body").inner_text(), encoding="utf-8"
            )
            page.screenshot(
                path=str(artifact_dir / "page.png"),
                full_page=True,
            )
        except Exception:
            pass
    if thread_id and time.monotonic() < deadline:
        try:
            (artifact_dir / "state.json").write_text(
                json.dumps(
                    _state(api_url, thread_id, deadline=deadline),
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
    if error is not None:
        (artifact_dir / "traceback.txt").write_text(
            "".join(traceback.format_exception(error)),
            encoding="utf-8",
        )


def _browser_flow(
    *,
    api_url: str,
    artifact_dir: Path,
    deadline: float,
) -> None:
    from playwright.sync_api import sync_playwright

    page: Any | None = None
    thread_id: str | None = None
    dataset_id: str | None = None
    handled_interrupts: set[str] = set()
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(
                api_url,
                wait_until="networkidle",
                timeout=_remaining_ms(deadline),
            )
            field = page.get_by_label(MESSAGE_LABEL)
            field.fill(REQUEST)
            page.get_by_role("button", name="Send", exact=True).click()
            thread_id = _thread_id(api_url, deadline=deadline)

            while time.monotonic() < deadline:
                state = _state(api_url, thread_id, deadline=deadline)
                interrupt = dict(state.get("active_interrupt") or {})
                interrupt_id = str(interrupt.get("id") or "")
                interrupt_type = str(interrupt.get("type") or "")
                if interrupt_id and interrupt_id not in handled_interrupts:
                    if interrupt_type == "agent_clarification":
                        _handle_clarification(page, deadline=deadline)
                    elif interrupt_type == "dataset_plan_review":
                        _handle_plan_review(page, interrupt, deadline=deadline)
                    elif interrupt_type == "dataset_review":
                        dataset_id = _assert_pending_review(
                            page,
                            api_url=api_url,
                            thread_id=thread_id,
                            interrupt=interrupt,
                            deadline=deadline,
                        )
                        _click_when_enabled(page, "Approve", deadline=deadline)
                    else:
                        raise AssertionError(
                            f"Unexpected interrupt type {interrupt_type!r}."
                        )
                    handled_interrupts.add(interrupt_id)
                elif (
                    dataset_id
                    and not interrupt
                    and dict(state.get("run") or {}).get("state") == "done"
                ):
                    _assert_persisted_schema(
                        _schema(
                            api_url,
                            thread_id,
                            dataset_id,
                            deadline=deadline,
                        )
                    )
                    _assert_approved_attachment(
                        page,
                        state,
                        dataset_id=dataset_id,
                        deadline=deadline,
                    )
                    _write_diagnostics(
                        artifact_dir,
                        api_url=api_url,
                        page=page,
                        thread_id=thread_id,
                        deadline=deadline,
                    )
                    return
                run = dict(state.get("run") or {})
                if run.get("state") in {"error", "timeout"}:
                    raise AssertionError(
                        f"Dataset workflow failed: {run.get('error') or run}"
                    )
                time.sleep(0.5)
            raise TimeoutError("Five-minute dataset schema smoke deadline reached.")
        except BaseException as error:
            _write_diagnostics(
                artifact_dir,
                api_url=api_url,
                page=page,
                thread_id=thread_id,
                deadline=deadline,
                error=error,
            )
            raise
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify catalog metadata in both real dataset schema views."
    )
    parser.add_argument("--api-port", type=int, default=8062)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--environment-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout_seconds <= 300:
        raise ValueError("The feature smoke is limited to five minutes.")
    deadline = time.monotonic() + min(args.timeout_seconds, 300)
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="dataset-schema-metadata-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)

    environment_root = args.environment_root.expanduser().resolve()
    load_app_environment(environment_root)
    static_dir = REPO_ROOT / "frontend" / "dist"
    if not (static_dir / "index.html").is_file():
        raise RuntimeError("Build frontend/dist before running this smoke.")
    runtime_root = artifact_dir / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(
                runtime_root / "agent_memory_fastapi.db"
            ),
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_STATIC_DIR": str(static_dir),
            "REPORT_AGENT_STUDY_ROOT": str(environment_root / "study_data"),
        }
    )
    host = "127.0.0.1"
    port = E2EProcessHarness.available_port(host, args.api_port)
    api_url = f"http://{host}:{port}"
    harness = E2EProcessHarness(
        artifact_dir=artifact_dir,
        environment=environment,
    )
    try:
        harness.start(host=host, port=port)
        harness.wait_until_ready(api_url, deadline=deadline)
        _browser_flow(
            api_url=api_url,
            artifact_dir=artifact_dir,
            deadline=deadline,
        )
    except BaseException as error:
        _write_diagnostics(
            artifact_dir,
            api_url=api_url,
            page=None,
            thread_id=None,
            deadline=deadline,
            error=error,
        )
        print(f"FAIL dataset schema metadata smoke; diagnostics: {artifact_dir}")
        raise
    finally:
        harness.close(deadline=deadline)
    print(f"PASS dataset schema metadata smoke; diagnostics: {artifact_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
