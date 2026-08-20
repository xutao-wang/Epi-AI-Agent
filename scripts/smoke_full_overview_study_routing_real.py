#!/usr/bin/env python3
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

from api.auth import LOCAL_SESSION_ID
from scripts.e2e_agent_activity_timeline_real import (
    MESSAGE_LABEL,
    _find_port,
    _launch_browser,
    _remaining_ms,
    _wait_for_health,
)
from study_package.installer import install_study_archives
from study_package.registry import discover_studies
from tests.study_package_fixtures import (
    create_package_archive,
    minimal_manifest,
)
from utils.env_loader import load_app_environment


HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
COMPILED_FRONTEND = REPO_ROOT / "frontend/dist"
UNSUPPORTED_QUERY = (
    "Extract participant-level velunoxide crystallography measurements from "
    "deep-ocean vent expeditions in my database."
)
_FICTIONAL_STUDIES = (
    {
        "study_id": "urban-canopy-luminase",
        "label": "Urban Canopy Luminase Cohort",
        "source_id": "urban-canopy-source",
        "overview": (
            "# Urban Canopy Luminase Cohort\n\n"
            "A prospective observational cohort of rooftop-garden workers "
            "studying fictional luminase exposure and seasonal leaf health "
            "in inland cities. It contains no marine expedition or "
            "crystallography measurements."
        ),
    },
    {
        "study_id": "agricultural-fermentation",
        "label": "Agricultural Fermentation Survey",
        "source_id": "fermentation-source",
        "overview": (
            "# Agricultural Fermentation Survey\n\n"
            "A cross-sectional survey of fictional grain fermentation "
            "practices among rural cooperatives. It contains no marine "
            "expedition or crystallography measurements."
        ),
    },
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real full-overview study-routing smoke once."
    )
    parser.add_argument("--api-port", type=int, default=8892)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--environment-root", type=Path, default=REPO_ROOT)
    return parser


def _create_fictional_archives(artifact_dir: Path) -> tuple[Path, ...]:
    archives: list[Path] = []
    for study in _FICTIONAL_STUDIES:
        source_root = artifact_dir / "package-sources" / study["study_id"]
        archive = create_package_archive(
            source_root,
            manifest=minimal_manifest(
                study_id=study["study_id"],
                package_version="1.0.0",
                label=study["label"],
                source_id=study["source_id"],
                format_version=3,
            ),
            study_design_documents={
                "overview.md": study["overview"],
            },
        )
        archives.append(archive)
    return tuple(archives)


def _conversation_items(api_url: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{api_url}/api/conversations",
        headers=HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return [dict(item) for item in response.json().get("items") or []]


def _thread_state(api_url: str, thread_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state",
        headers=HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return dict(response.json())


def _wait_for_thread(api_url: str, deadline: float) -> str:
    while time.monotonic() < deadline:
        items = _conversation_items(api_url)
        if len(items) == 1:
            return str(items[0]["thread_id"])
        if len(items) > 1:
            raise AssertionError(
                f"Expected one new thread, received {items!r}"
            )
        time.sleep(0.25)
    raise TimeoutError("The browser did not create a conversation.")


def _wait_for_completion(
    api_url: str,
    thread_id: str,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        state = _thread_state(api_url, thread_id)
        interrupt = dict(state.get("active_interrupt") or {})
        if interrupt:
            raise AssertionError(
                f"Unexpected routing interrupt: {interrupt!r}"
            )
        run = dict(state.get("run") or {})
        run_state = str(run.get("state") or "")
        if run_state == "done":
            return state
        if run_state in {"error", "timeout", "cancelled"}:
            raise AssertionError(
                f"Routing run ended unsuccessfully: {run!r}"
            )
        time.sleep(0.25)
    raise TimeoutError("Routing response did not complete before the deadline.")


def _assert_safe_negative_route(
    state: dict[str, Any],
    expected_labels: tuple[str, ...],
) -> None:
    conversation_text = "\n".join(
        str(message.get("text") or "")
        for message in state.get("conversation") or []
        if message.get("role") == "assistant"
    )
    for label in expected_labels:
        if label not in conversation_text:
            raise AssertionError(
                f"Negative response omitted live installed label {label!r}."
            )
    tool_names = {
        str(activity.get("tool_name") or "")
        for run in state.get("activity_runs") or []
        for activity in run.get("activities") or []
    }
    forbidden_prefixes = (
        "dbrag-",
        "study-design-search",
        "analysis-run_custom_python",
    )
    if any(
        name.startswith(prefix)
        for name in tool_names
        for prefix in forbidden_prefixes
    ):
        raise AssertionError(
            f"Unsupported request used study tools: {tool_names!r}"
        )
    forbidden_tools = {
        "general-request_clarification",
        "publication-search_study_evidence",
        "publication-open_study_source",
    }
    unexpected = tool_names & forbidden_tools
    if unexpected:
        raise AssertionError(
            f"Unsupported request used routing or study tools: {unexpected!r}"
        )


def _write_page(page: Any, artifact_dir: Path) -> None:
    (artifact_dir / "page.txt").write_text(
        page.locator("body").inner_text(),
        encoding="utf-8",
    )
    (artifact_dir / "page.html").write_text(
        page.content(),
        encoding="utf-8",
    )
    page.screenshot(
        path=str(artifact_dir / "screenshot.png"),
        full_page=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout_seconds > 300:
        raise ValueError("The feature smoke is limited to five minutes.")

    deadline = time.monotonic() + args.timeout_seconds
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="full-overview-routing-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    study_root = artifact_dir / "study-root"
    runtime_root.mkdir(parents=True, exist_ok=True)

    process: subprocess.Popen[Any] | None = None
    api_log: Any | None = None
    page: Any | None = None
    state: dict[str, Any] | None = None
    try:
        environment_root = args.environment_root.expanduser().resolve()
        load_app_environment(environment_root)
        api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("A real OPENAI_API_KEY is required.")
        if not (COMPILED_FRONTEND / "index.html").is_file():
            raise RuntimeError("Build frontend/dist before running this smoke.")

        archives = _create_fictional_archives(artifact_dir)
        install_study_archives(archives, study_root / "studies")
        expected_labels = tuple(
            study.label
            for study in discover_studies(study_root / "studies").values
        )
        environment = dict(os.environ)
        environment.update(
            {
                "OPENAI_API_KEY": api_key,
                "PYTHONPATH": str(REPO_ROOT),
                "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
                "REPORT_AGENT_CHECKPOINT_DB_PATH": str(
                    runtime_root / "agent_memory_fastapi.db"
                ),
                "REPORT_AGENT_STATIC_DIR": str(COMPILED_FRONTEND),
                "REPORT_AGENT_STUDY_ROOT": str(study_root),
                "REPORT_AGENT_API_WORKFLOW_TIMEOUT_SECONDS": str(
                    args.timeout_seconds
                ),
            }
        )

        host = "127.0.0.1"
        port = _find_port(host, args.api_port)
        api_url = f"http://{host}:{port}"
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
            try:
                page = browser.new_page(
                    viewport={"width": 1500, "height": 1000}
                )
                page.goto(
                    api_url,
                    wait_until="networkidle",
                    timeout=_remaining_ms(deadline),
                )
                field = page.get_by_label(MESSAGE_LABEL)
                field.fill(UNSUPPORTED_QUERY)
                page.get_by_role(
                    "button",
                    name="Send",
                    exact=True,
                ).click()
                thread_id = _wait_for_thread(api_url, deadline)
                state = _wait_for_completion(api_url, thread_id, deadline)
                _assert_safe_negative_route(state, expected_labels)
                for label in expected_labels:
                    page.get_by_text(label, exact=False).first.wait_for(
                        timeout=_remaining_ms(deadline)
                    )
                (artifact_dir / "api-state.json").write_text(
                    json.dumps(state, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                _write_page(page, artifact_dir)
            finally:
                browser.close()
    except BaseException as error:
        (artifact_dir / "failure-traceback.txt").write_text(
            "".join(traceback.format_exception(error)),
            encoding="utf-8",
        )
        if state is not None:
            (artifact_dir / "failure-api-state.json").write_text(
                json.dumps(state, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        if page is not None:
            try:
                _write_page(page, artifact_dir)
            except Exception:
                pass
        print(
            "FAIL full-overview study routing smoke; "
            f"diagnostics: {artifact_dir}",
            flush=True,
        )
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

    print(
        "PASS full-overview study routing smoke; "
        f"diagnostics: {artifact_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
