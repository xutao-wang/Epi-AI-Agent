#!/usr/bin/env python3
"""Reproduce dataset reuse recovery from an isolated historical checkpoint once.

This smoke deliberately starts from a prepared, read-only checkpoint fixture. It
copies that SQLite database into a writable isolated runtime, drives the compiled
frontend, and submits the exact query recorded in the fixture manifest. It does
not create, modify, or repair the fixture itself.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any, Iterable

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.auth import LOCAL_SESSION_ID
from api.runtime import graph_config
from scripts.e2e_agent_activity_timeline_real import (
    _find_port,
    _launch_browser,
    _remaining_ms,
    _wait_for_health,
)
from utils.env_loader import load_app_environment


COMPILED_FRONTEND = REPO_ROOT / "frontend" / "dist"
HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
MESSAGE_LABEL = "Ask a question about your dataset!"
EXACT_ORIGINAL_QUERY = (
    "skip any human checkpoint, Create a household-contact dataset with age, "
    "sex, household size, TST result, and IGRA result."
)
ALL_CHECKPOINTS_QUERY = "skip all checkpoint, do it again."
EXTRACTION_TOOL = "dbrag-validate_and_extract"
PLAN_REVIEW_TOOL = "dbrag-request_dataset_plan_review"
ALLOWED_RECOVERY_EXTRACTION_ERRORS = frozenset(
    {"PREDECESSOR_NOT_PENDING_REVIEW"}
)


def _server_command(host: str, port: int) -> list[str]:
    return [
        sys.executable, "-m", "uvicorn", "api.app:build_application", "--factory",
        "--host", host, "--port", str(port), "--workers", "1",
    ]


def _timeout(value: str) -> float:
    parsed = float(value)
    if parsed <= 0 or parsed > 300:
        raise argparse.ArgumentTypeError("timeout must be > 0 and <= 300 seconds")
    return parsed


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _read_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Fixture manifest must be a JSON object.")
    return value


def _copy_sqlite_backup(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing runtime checkpoint: {destination}"
        )
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as reader:
        with sqlite3.connect(destination) as writer:
            reader.backup(writer)


def _checkpoint_values(
    checkpoint_path: Path,
    thread_id: str,
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    if read_only:
        uri = f"file:{checkpoint_path.resolve()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as connection:
            saved = SqliteSaver(connection).get_tuple(graph_config(thread_id))
    else:
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            saved = saver.get_tuple(graph_config(thread_id))
    if saved is None:
        raise AssertionError(f"Fixture has no checkpoint for thread {thread_id!r}.")
    channels = dict(saved.checkpoint.get("channel_values") or {})
    root = channels.get("__root__")
    return dict(root) if isinstance(root, dict) else channels


def _artifact_record(values: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    artifacts = dict(values.get("artifacts") or {})
    candidates: list[dict[str, Any]] = []
    dataset = dict(artifacts.get("datasets") or {}).get(artifact_id)
    if isinstance(dataset, dict):
        candidates.append(dataset)
    file_record = dict(artifacts.get("files") or {}).get(artifact_id)
    if isinstance(file_record, dict):
        content = file_record.get("content")
        candidates.append(dict(content) if isinstance(content, dict) else file_record)
    if not candidates:
        raise AssertionError(f"Dataset {artifact_id!r} is absent from checkpoint state.")
    return deepcopy(candidates[-1])


_DIRECT_PATH_FIELDS = frozenset({"path", "schema_path", "metadata_path"})
_PATH_MAP_FIELDS = frozenset({"expected_final_paths", "expected_staging_paths"})


def _path_values(value: Any) -> Iterable[Path]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            key = str(child_key)
            if key in _DIRECT_PATH_FIELDS and isinstance(child, str):
                candidate = Path(child).expanduser()
                if candidate.is_absolute():
                    yield candidate
            elif key in _PATH_MAP_FIELDS and isinstance(child, dict):
                for path_key in (*_DIRECT_PATH_FIELDS, "root"):
                    path_value = child.get(path_key)
                    if isinstance(path_value, str):
                        candidate = Path(path_value).expanduser()
                        if candidate.is_absolute():
                            yield candidate
            else:
                yield from _path_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _path_values(child)


def _file_hashes(record: dict[str, Any]) -> dict[str, str]:
    paths = sorted({path.resolve() for path in _path_values(record) if path.is_file()})
    if not paths:
        raise AssertionError("Historical dataset record exposes no saved files.")
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _validate_fixture(
    *,
    fixture: Path,
    manifest: dict[str, Any],
    runtime_root: Path,
    study_root: Path,
    thread_id: str,
) -> tuple[str, int, str, dict[str, Any]]:
    if not fixture.is_file():
        raise FileNotFoundError(f"Checkpoint fixture not found: {fixture}")
    if not study_root.is_dir():
        raise FileNotFoundError(f"Prepared study root not found: {study_root}")
    if runtime_root == fixture or _is_within(fixture, runtime_root):
        raise ValueError("Writable runtime and read-only fixture must be separate.")
    expected_thread = str(manifest.get("thread_id") or "")
    if expected_thread and thread_id != expected_thread:
        raise ValueError("--thread-id does not match fixture.json.")
    query = str(manifest.get("query") or "")
    if query != EXACT_ORIGINAL_QUERY:
        raise ValueError("fixture.json does not contain the exact historical query.")
    old_dataset_id = str(manifest.get("dataset_id") or "")
    plan_id = str(manifest.get("plan_id") or "")
    plan_version = int(manifest.get("plan_version") or manifest.get("version") or 0)
    if not old_dataset_id or not plan_id or plan_version < 1:
        raise ValueError("fixture.json must identify dataset_id and plan_id/version.")
    values = _checkpoint_values(fixture, thread_id, read_only=True)
    old_record = _artifact_record(values, old_dataset_id)
    if old_record.get("status") != "cancelled":
        raise AssertionError("Historical dataset must be cancelled before replay.")
    for candidate in _path_values(values.get("artifacts") or {}):
        if not (_is_within(candidate, runtime_root) or _is_within(candidate, study_root)):
            raise ValueError(f"Fixture artifact path escapes isolated roots: {candidate}")
    return plan_id, plan_version, old_dataset_id, old_record


def _state(api_url: str, thread_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state", headers=HEADERS, timeout=10
    )
    response.raise_for_status()
    return dict(response.json())


def _conversation_title(api_url: str, thread_id: str) -> str:
    response = requests.get(f"{api_url}/api/conversations", headers=HEADERS, timeout=10)
    response.raise_for_status()
    matches = [
        str(item.get("title") or "")
        for item in list(response.json().get("items") or [])
        if isinstance(item, dict) and item.get("thread_id") == thread_id
    ]
    if len(matches) != 1 or not matches[0]:
        raise AssertionError(f"Could not uniquely locate historical thread {thread_id}.")
    return matches[0]


def _tool_calls(values: dict[str, Any], start: int) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in list(values.get("messages") or [])[start:]:
        if isinstance(message, AIMessage):
            calls.extend(dict(call) for call in message.tool_calls)
    return calls


def _tool_results_by_call_id(
    values: dict[str, Any],
    start: int,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for message in list(values.get("messages") or [])[start:]:
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(str(message.content))
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            results[str(message.tool_call_id)] = payload
    return results


def _result_artifact_ids(payload: dict[str, Any]) -> set[str]:
    references: list[Any] = list(payload.get("artifacts") or [])
    if isinstance(payload.get("artifact"), dict):
        references.append(payload["artifact"])
    return {
        str(reference["id"])
        for reference in references
        if isinstance(reference, dict) and str(reference.get("id") or "")
    }


def _current_output_artifact_ids(values: dict[str, Any]) -> set[str]:
    return {
        str(reference["id"])
        for reference in list(values.get("current_turn_output_artifact_refs") or [])
        if isinstance(reference, dict) and str(reference.get("id") or "")
    }


def _successful_extraction_results(
    values: dict[str, Any],
    start: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    results_by_call = _tool_results_by_call_id(values, start)
    successful: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for call in _tool_calls(values, start):
        if call.get("name") != EXTRACTION_TOOL:
            continue
        payload = results_by_call.get(str(call.get("id") or ""))
        if (
            isinstance(payload, dict)
            and "error" not in payload
            and _result_artifact_ids(payload)
        ):
            successful.append((call, payload))
    return successful


def _assert_recovery(
    values: dict[str, Any],
    *,
    message_start: int,
    old_dataset_id: str,
    old_record: dict[str, Any],
    plan_id: str,
    plan_version: int,
    expected_new_id: str,
    old_file_hashes: dict[str, str],
) -> None:
    calls = _tool_calls(values, message_start)
    if any(call.get("name") == PLAN_REVIEW_TOOL for call in calls):
        raise AssertionError("Replay created another plan review instead of using the approved plan.")
    results_by_call = _tool_results_by_call_id(values, message_start)
    collision_codes = {
        str(dict(payload.get("error") or {}).get("code") or "")
        for payload in results_by_call.values()
        if isinstance(payload.get("error"), dict)
    }
    if collision_codes & {"DATASET_IDENTITY_COLLISION", "DATASET_DURABLE_COLLISION"}:
        raise AssertionError("Recovery exposed a dataset identity or durable collision.")
    for call in calls:
        if call.get("name") != EXTRACTION_TOOL:
            continue
        payload = results_by_call.get(str(call.get("id") or ""))
        if (
            isinstance(payload, dict)
            and "error" not in payload
            and _result_artifact_ids(payload)
        ):
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict) or (
            error.get("recoverable") is not True
            or str(error.get("code") or "") not in ALLOWED_RECOVERY_EXTRACTION_ERRORS
        ):
            raise AssertionError(
                f"Recovery exposed an unexpected extraction failure: {error!r}"
            )
    successful = _successful_extraction_results(values, message_start)
    if len(successful) != 1:
        raise AssertionError(
            f"Expected exactly one successful extraction result, got {successful!r}"
        )
    successful_call, successful_result = successful[0]
    args = dict(successful_call.get("args") or {})
    if (
        args.get("plan_id") != plan_id
        or int(args.get("plan_version") or 0) != plan_version
    ):
        raise AssertionError("Extraction did not use the exact historically approved plan/version.")
    if expected_new_id not in _result_artifact_ids(successful_result):
        raise AssertionError("Tool results do not identify the fresh dataset under review.")
    if _artifact_record(values, old_dataset_id) != old_record:
        raise AssertionError("Fresh recovery changed the old cancelled dataset record.")
    if _file_hashes(old_record) != old_file_hashes:
        raise AssertionError("Fresh recovery changed bytes belonging to the old dataset.")
    new_record = _artifact_record(values, expected_new_id)
    if new_record.get("status") != "pending_review":
        raise AssertionError("Fresh dataset is not pending normal human review.")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=repr), encoding="utf-8")


def _write_diagnostics(
    artifact_dir: Path,
    *,
    page: Any | None,
    api_url: str,
    thread_id: str,
    checkpoint_path: Path,
    error: BaseException | None = None,
) -> None:
    if page is not None:
        try:
            (artifact_dir / "page.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
            page.screenshot(path=str(artifact_dir / "screenshot.png"), full_page=True)
        except Exception:
            pass
    try:
        _write_json(artifact_dir / "api-state.json", _state(api_url, thread_id))
        _write_json(artifact_dir / "checkpoint-state.json", _checkpoint_values(checkpoint_path, thread_id))
    except Exception:
        pass
    if error is not None:
        (artifact_dir / "traceback.txt").write_text(
            "".join(traceback.format_exception(error)), encoding="utf-8"
        )


def _wait_for_dataset_review(
    page: Any, api_url: str, thread_id: str, *, deadline: float
) -> tuple[dict[str, Any], str]:
    while time.monotonic() < deadline:
        state = _state(api_url, thread_id)
        interrupt = dict(state.get("active_interrupt") or {})
        run = dict(state.get("run") or {})
        if interrupt.get("type") == "dataset_review":
            artifact = dict(interrupt.get("artifact") or {})
            dataset_id = str(artifact.get("id") or "")
            if not dataset_id or artifact.get("expected_status") != "pending_review":
                raise AssertionError("Dataset review lacks an exact pending artifact identity.")
            page.get_by_role("heading", name="Review extracted dataset").wait_for(
                timeout=_remaining_ms(deadline)
            )
            return state, dataset_id
        if interrupt.get("type") == "dataset_plan_review":
            raise AssertionError("Historical replay incorrectly requested another plan review.")
        if interrupt.get("type") == "agent_clarification":
            raise AssertionError(f"Unexpected clarification: {interrupt.get('question')}")
        if run.get("state") in {"error", "timeout", "cancelled", "done"}:
            raise AssertionError(f"Run ended before fresh dataset review: {run}")
        time.sleep(0.25)
    raise TimeoutError("Fresh dataset did not reach review before the smoke deadline.")


def _wait_for_active_dataset(
    api_url: str,
    thread_id: str,
    dataset_id: str,
    *,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        state = _state(api_url, thread_id)
        run = dict(state.get("run") or {})
        interrupt = dict(state.get("active_interrupt") or {})
        if (
            not interrupt
            and run.get("state") == "done"
            and dict(state.get("diagnostics") or {}).get("active_dataset_id")
            == dataset_id
        ):
            return state
        if run.get("state") in {"error", "timeout", "cancelled"}:
            raise AssertionError(f"Dataset approval did not complete: {run}")
        time.sleep(0.25)
    raise TimeoutError("Approved dataset did not become active before the deadline.")


def _wait_for_active_reuse(
    api_url: str,
    thread_id: str,
    dataset_id: str,
    *,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        state = _state(api_url, thread_id)
        run = dict(state.get("run") or {})
        interrupt = dict(state.get("active_interrupt") or {})
        if interrupt:
            raise AssertionError(
                "Active reuse unexpectedly reached a human review or clarification: "
                f"{interrupt.get('type')!r}"
            )
        if (
            run.get("state") == "done"
            and dict(state.get("diagnostics") or {}).get("active_dataset_id")
            == dataset_id
        ):
            return state
        if run.get("state") in {"error", "timeout", "cancelled"}:
            raise AssertionError(f"Active reuse failed: {run}")
        time.sleep(0.25)
    raise TimeoutError("Active reuse did not complete before the deadline.")


def _assert_active_reuse(
    values: dict[str, Any],
    *,
    message_start: int,
    dataset_id: str,
    dataset_record: dict[str, Any],
    dataset_file_hashes: dict[str, str],
    plan_id: str,
    plan_version: int,
    expected_dataset_ids: set[str],
) -> None:
    calls = _tool_calls(values, message_start)
    if any(call.get("name") == PLAN_REVIEW_TOOL for call in calls):
        raise AssertionError("Active reuse incorrectly requested another plan review.")
    successful = _successful_extraction_results(values, message_start)
    if len(successful) != 1:
        raise AssertionError(
            f"Active reuse requires exactly one successful extraction: {successful!r}"
        )
    successful_call, successful_result = successful[0]
    arguments = dict(successful_call.get("args") or {})
    if (
        arguments.get("plan_id") != plan_id
        or int(arguments.get("plan_version") or 0) != plan_version
    ):
        raise AssertionError("Active reuse did not use the exact approved plan/version.")
    if dataset_id not in _result_artifact_ids(successful_result):
        raise AssertionError("Tool results do not report the exact active reused dataset.")
    if dataset_id not in _current_output_artifact_ids(values):
        raise AssertionError("Turn state does not report the active output artifact.")
    artifacts = dict(values.get("artifacts") or {})
    current_ids = set(dict(artifacts.get("datasets") or {}))
    current_ids.update(dict(artifacts.get("files") or {}))
    generated_ids = {value for value in current_ids if value.startswith("subset-agent-")}
    if generated_ids != expected_dataset_ids:
        raise AssertionError("Active reuse created an additional dataset identity.")
    if _artifact_record(values, dataset_id) != dataset_record:
        raise AssertionError("Active reuse changed the approved dataset record.")
    if _file_hashes(dataset_record) != dataset_file_hashes:
        raise AssertionError("Active reuse changed the approved dataset bytes.")


def run(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout_seconds
    runtime_root = args.runtime_root.expanduser().resolve()
    study_root = args.study_root.expanduser().resolve()
    fixture = args.checkpoint_fixture.expanduser().resolve()
    manifest_path = args.fixture_manifest.expanduser().resolve()
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="dataset-reuse-recovery-smoke-"))
    )
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = _read_manifest(manifest_path)
    fixture_hash = hashlib.sha256(fixture.read_bytes()).hexdigest()
    plan_id, plan_version, old_dataset_id, old_record = _validate_fixture(
        fixture=fixture,
        manifest=manifest,
        runtime_root=runtime_root,
        study_root=study_root,
        thread_id=args.thread_id,
    )
    checkpoint_path = runtime_root / "agent_memory_fastapi.db"
    _copy_sqlite_backup(fixture, checkpoint_path)
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != fixture_hash:
        raise AssertionError("Read-only checkpoint fixture changed while copying it.")
    initial_values = _checkpoint_values(checkpoint_path, args.thread_id)
    old_file_hashes = _file_hashes(old_record)
    initial_message_count = len(list(initial_values.get("messages") or []))
    expected_count = int(manifest.get("original_count") or initial_message_count)
    if initial_message_count != expected_count:
        raise AssertionError(
            f"Fixture message count changed: expected {expected_count}, got {initial_message_count}."
        )

    environment_root = args.environment_root.expanduser().resolve()
    load_app_environment(environment_root)
    if not str(os.environ.get("OPENAI_API_KEY") or "").strip():
        raise RuntimeError("A real OPENAI_API_KEY is required by the feature smoke.")
    if not (COMPILED_FRONTEND / "index.html").is_file():
        raise RuntimeError("Build frontend/dist before running this smoke.")
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(checkpoint_path),
            "REPORT_AGENT_STATIC_DIR": str(COMPILED_FRONTEND),
            "REPORT_AGENT_STUDY_ROOT": str(study_root),
            "REPORT_AGENT_API_WORKFLOW_TIMEOUT_SECONDS": str(args.timeout_seconds),
        }
    )
    host = "127.0.0.1"
    port = _find_port(host, args.api_port)
    api_url = f"http://{host}:{port}"
    log_handle = (artifact_dir / "backend.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        _server_command(host, port),
        cwd=REPO_ROOT,
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    page: Any | None = None
    try:
        _wait_for_health(api_url, deadline, process)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False) if args.headful else _launch_browser(playwright)
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 1050})
                page.goto(api_url, wait_until="networkidle", timeout=_remaining_ms(deadline))
                title = _conversation_title(api_url, args.thread_id)
                page.get_by_text(title, exact=True).click(timeout=_remaining_ms(deadline))
                field = page.get_by_label(MESSAGE_LABEL)
                field.wait_for(timeout=_remaining_ms(deadline))
                field.fill(EXACT_ORIGINAL_QUERY, timeout=_remaining_ms(deadline))
                page.get_by_role("button", name="Send", exact=True).click()
                state, new_dataset_id = _wait_for_dataset_review(
                    page, api_url, args.thread_id, deadline=deadline
                )
                if new_dataset_id == old_dataset_id:
                    raise AssertionError("Cancelled dataset was reused instead of creating a fresh identity.")
                values = _checkpoint_values(checkpoint_path, args.thread_id)
                _assert_recovery(
                    values,
                    message_start=initial_message_count,
                    old_dataset_id=old_dataset_id,
                    old_record=old_record,
                    plan_id=plan_id,
                    plan_version=plan_version,
                    expected_new_id=new_dataset_id,
                    old_file_hashes=old_file_hashes,
                )
                _write_json(artifact_dir / "exact-query-state.json", state)
                _write_json(
                    artifact_dir / "summary.json",
                    {
                        "thread_id": args.thread_id,
                        "old_dataset_id": old_dataset_id,
                        "fresh_dataset_id": new_dataset_id,
                        "plan_id": plan_id,
                        "plan_version": plan_version,
                        "fixture_sha256": fixture_hash,
                        "secondary_case": "not_requested",
                    },
                )

                active_candidate_id = new_dataset_id
                first_cancelled_record: dict[str, Any] | None = None
                first_cancelled_hashes: dict[str, str] | None = None
                if args.follow_up_case == "all":
                    page.get_by_role("button", name="Cancel", exact=True).click(
                        timeout=_remaining_ms(deadline)
                    )
                    cancelled_state: dict[str, Any] | None = None
                    while time.monotonic() < deadline:
                        cancelled_state = _state(api_url, args.thread_id)
                        if dict(cancelled_state.get("run") or {}).get("state") == "cancelled":
                            break
                        time.sleep(0.25)
                    if (
                        cancelled_state is None
                        or dict(cancelled_state.get("run") or {}).get("state")
                        != "cancelled"
                    ):
                        raise TimeoutError("First fresh dataset cancellation did not finish.")
                    after_cancel = _checkpoint_values(checkpoint_path, args.thread_id)
                    first_cancelled_record = _artifact_record(after_cancel, new_dataset_id)
                    if first_cancelled_record.get("status") != "cancelled":
                        raise AssertionError("First fresh dataset was not recorded as cancelled.")
                    first_cancelled_hashes = _file_hashes(first_cancelled_record)
                    second_start = len(list(after_cancel.get("messages") or []))
                    field.wait_for(state="visible", timeout=_remaining_ms(deadline))
                    field.fill(ALL_CHECKPOINTS_QUERY, timeout=_remaining_ms(deadline))
                    page.get_by_role("button", name="Send", exact=True).click()
                    _, second_dataset_id = _wait_for_dataset_review(
                        page, api_url, args.thread_id, deadline=deadline
                    )
                    if second_dataset_id in {old_dataset_id, new_dataset_id}:
                        raise AssertionError("Second cancelled replay did not reserve another fresh identity.")
                    second_values = _checkpoint_values(checkpoint_path, args.thread_id)
                    _assert_recovery(
                        second_values,
                        message_start=second_start,
                        old_dataset_id=new_dataset_id,
                        old_record=first_cancelled_record,
                        plan_id=plan_id,
                        plan_version=plan_version,
                        expected_new_id=second_dataset_id,
                        old_file_hashes=first_cancelled_hashes,
                    )
                    if _artifact_record(second_values, old_dataset_id) != old_record:
                        raise AssertionError("Second case changed the historical cancelled record.")
                    if _file_hashes(old_record) != old_file_hashes:
                        raise AssertionError("Second case changed historical dataset bytes.")
                    active_candidate_id = second_dataset_id
                    summary = json.loads((artifact_dir / "summary.json").read_text(encoding="utf-8"))
                    summary.update({"secondary_case": "passed", "second_fresh_dataset_id": second_dataset_id})
                    _write_json(artifact_dir / "summary.json", summary)

                page.get_by_role("button", name="Approve", exact=True).click(
                    timeout=_remaining_ms(deadline)
                )
                active_state = _wait_for_active_dataset(
                    api_url,
                    args.thread_id,
                    active_candidate_id,
                    deadline=deadline,
                )
                active_values = _checkpoint_values(checkpoint_path, args.thread_id)
                active_record = _artifact_record(active_values, active_candidate_id)
                if active_record.get("status") != "active":
                    raise AssertionError("Approved fresh dataset is not active.")
                active_hashes = _file_hashes(active_record)
                reuse_start = len(list(active_values.get("messages") or []))
                field.wait_for(state="visible", timeout=_remaining_ms(deadline))
                field.fill(EXACT_ORIGINAL_QUERY, timeout=_remaining_ms(deadline))
                page.get_by_role("button", name="Send", exact=True).click()
                reused_state = _wait_for_active_reuse(
                    api_url,
                    args.thread_id,
                    active_candidate_id,
                    deadline=deadline,
                )
                reused_values = _checkpoint_values(checkpoint_path, args.thread_id)
                expected_ids = {old_dataset_id, new_dataset_id}
                if args.follow_up_case == "all":
                    expected_ids.add(active_candidate_id)
                _assert_active_reuse(
                    reused_values,
                    message_start=reuse_start,
                    dataset_id=active_candidate_id,
                    dataset_record=active_record,
                    dataset_file_hashes=active_hashes,
                    plan_id=plan_id,
                    plan_version=plan_version,
                    expected_dataset_ids=expected_ids,
                )
                if _artifact_record(reused_values, old_dataset_id) != old_record:
                    raise AssertionError("Active reuse changed the historical cancelled record.")
                if _file_hashes(old_record) != old_file_hashes:
                    raise AssertionError("Active reuse changed historical dataset bytes.")
                if first_cancelled_record is not None:
                    if (
                        _artifact_record(reused_values, new_dataset_id)
                        != first_cancelled_record
                    ):
                        raise AssertionError("Active reuse changed the first cancelled retry.")
                    if first_cancelled_hashes is None or (
                        _file_hashes(first_cancelled_record)
                        != first_cancelled_hashes
                    ):
                        raise AssertionError("Active reuse changed first-retry bytes.")
                _write_json(artifact_dir / "active-reuse-state.json", reused_state)
                summary = json.loads(
                    (artifact_dir / "summary.json").read_text(encoding="utf-8")
                )
                summary.update(
                    {
                        "active_reuse": "passed",
                        "active_dataset_id": active_candidate_id,
                    }
                )
                _write_json(artifact_dir / "summary.json", summary)

                _write_diagnostics(
                    artifact_dir,
                    page=page,
                    api_url=api_url,
                    thread_id=args.thread_id,
                    checkpoint_path=checkpoint_path,
                )
            except BaseException as error:
                _write_diagnostics(
                    artifact_dir,
                    page=page,
                    api_url=api_url,
                    thread_id=args.thread_id,
                    checkpoint_path=checkpoint_path,
                    error=error,
                )
                page = None
                raise
            finally:
                browser.close()
    except BaseException as error:
        _write_diagnostics(
            artifact_dir,
            page=page,
            api_url=api_url,
            thread_id=args.thread_id,
            checkpoint_path=checkpoint_path,
            error=error,
        )
        print(f"FAIL dataset reuse recovery smoke; diagnostics: {artifact_dir}", flush=True)
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log_handle.close()

    if hashlib.sha256(fixture.read_bytes()).hexdigest() != fixture_hash:
        raise AssertionError("Read-only checkpoint fixture changed during the smoke.")

    print(f"PASS dataset reuse recovery smoke; diagnostics: {artifact_dir}", flush=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one real compiled-UI dataset reuse recovery smoke from an isolated fixture."
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--study-root", type=Path, required=True)
    parser.add_argument("--checkpoint-fixture", type=Path, required=True)
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--environment-root", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--api-port", type=int, default=8893)
    parser.add_argument("--timeout-seconds", type=_timeout, default=300.0)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument(
        "--follow-up-case",
        choices=("exact", "all"),
        default="exact",
        help="Default runs only the exact historical query; 'all' adds the second user wording in the same run.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args()))
