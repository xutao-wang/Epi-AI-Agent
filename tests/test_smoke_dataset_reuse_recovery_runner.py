from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
import pytest

from scripts.smoke_dataset_reuse_recovery_real import (
    EXTRACTION_TOOL,
    PLAN_REVIEW_TOOL,
    _assert_active_reuse,
    _assert_recovery,
    _file_hashes,
    _path_values,
)


def _record(tmp_path: Path, dataset_id: str, status: str) -> dict[str, object]:
    paths = {
        "path": tmp_path / f"{dataset_id}.parquet",
        "schema_path": tmp_path / f"{dataset_id}.schema.json",
        "metadata_path": tmp_path / f"{dataset_id}.metadata.json",
    }
    for key, path in paths.items():
        path.write_text(f"{dataset_id}:{key}", encoding="utf-8")
    return {
        "id": dataset_id,
        "status": status,
        **{key: str(path) for key, path in paths.items()},
    }


def _call(plan_id: str = "plan-1", version: int = 2) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": EXTRACTION_TOOL,
                "args": {"plan_id": plan_id, "plan_version": version},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )


def _result(dataset_id: str, status: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps({"artifact": {"id": dataset_id}, "status": status}),
        tool_call_id="call-1",
    )


def test_path_validation_uses_only_declared_storage_fields(tmp_path: Path) -> None:
    direct = tmp_path / "data.parquet"
    staged = tmp_path / "staged.parquet"
    values = {
        "path": str(direct),
        "expected_staging_paths": {"path": str(staged)},
        "pathology_note": "/live/not-a-storage-path",
        "unrelated": {"search_path": "/live/also-not-a-storage-path"},
    }

    assert set(_path_values(values)) == {direct, staged}


def test_recovery_assertion_rejects_new_plan_review(tmp_path: Path) -> None:
    old = _record(tmp_path, "subset-agent-old", "cancelled")
    new = _record(tmp_path, "subset-agent-new", "pending_review")
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": PLAN_REVIEW_TOOL,
                        "args": {"plan_id": "plan-1", "version": 2},
                        "id": "review-1",
                        "type": "tool_call",
                    }
                ],
            ),
            _call(),
            _result("subset-agent-new", "pending_review"),
        ],
        "artifacts": {
            "files": {
                "subset-agent-old": {"content": old},
                "subset-agent-new": {"content": new},
            }
        },
    }

    with pytest.raises(AssertionError, match="another plan review"):
        _assert_recovery(
            state,
            message_start=0,
            old_dataset_id="subset-agent-old",
            old_record=old,
            old_file_hashes=_file_hashes(old),
            plan_id="plan-1",
            plan_version=2,
            expected_new_id="subset-agent-new",
        )


def test_active_reuse_rejects_new_dataset_identity(tmp_path: Path) -> None:
    old = _record(tmp_path, "subset-agent-old", "cancelled")
    active = _record(tmp_path, "subset-agent-active", "active")
    unexpected = _record(tmp_path, "subset-agent-unexpected", "pending_review")
    state = {
        "messages": [_call(), _result("subset-agent-active", "active")],
        "artifacts": {
            "files": {
                "subset-agent-old": {"content": old},
                "subset-agent-active": {"content": active},
                "subset-agent-unexpected": {"content": unexpected},
            }
        },
    }

    with pytest.raises(AssertionError, match="additional dataset identity"):
        _assert_active_reuse(
            state,
            message_start=0,
            dataset_id="subset-agent-active",
            dataset_record=active,
            dataset_file_hashes=_file_hashes(active),
            plan_id="plan-1",
            plan_version=2,
            expected_dataset_ids={"subset-agent-old", "subset-agent-active"},
        )


def test_server_command_resolves_the_real_application_factory() -> None:
    from uvicorn.importer import import_from_string
    from api.app import build_application
    from scripts.smoke_dataset_reuse_recovery_real import _server_command

    command = _server_command("127.0.0.1", 8893)
    target = command[command.index("uvicorn") + 1]
    assert import_from_string(target) is build_application
    assert "--factory" in command


def test_recovery_accepts_rejected_precondition_then_one_success(
    tmp_path: Path,
) -> None:
    old = _record(tmp_path, "subset-agent-old", "cancelled")
    fresh = _record(tmp_path, "subset-agent-fresh", "pending_review")
    rejected_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": EXTRACTION_TOOL,
                "args": {
                    "plan_id": "plan-1",
                    "plan_version": 2,
                    "predecessor_dataset_id": old["id"],
                    "predecessor_dataset_version": 1,
                },
                "id": "rejected-call",
                "type": "tool_call",
            }
        ],
    )
    rejected_result = ToolMessage(
        tool_call_id="rejected-call",
        content=json.dumps(
            {
                "error": {
                    "code": "PREDECESSOR_NOT_PENDING_REVIEW",
                    "recoverable": True,
                }
            }
        ),
    )
    values = {
        "messages": [
            rejected_call,
            rejected_result,
            _call(),
            _result(fresh["id"], "pending_review"),
        ],
        "artifacts": {
            "datasets": {
                old["id"]: old,
                fresh["id"]: fresh,
            }
        },
    }

    _assert_recovery(
        values,
        message_start=0,
        old_dataset_id=old["id"],
        old_record=old,
        old_file_hashes=_file_hashes(old),
        plan_id="plan-1",
        plan_version=2,
        expected_new_id=fresh["id"],
    )


def test_recovery_rejects_two_successful_dataset_creations(
    tmp_path: Path,
) -> None:
    old = _record(tmp_path, "subset-agent-old", "cancelled")
    fresh = _record(tmp_path, "subset-agent-fresh", "pending_review")
    second = _record(tmp_path, "subset-agent-second", "pending_review")
    second_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": EXTRACTION_TOOL,
                "args": {"plan_id": "plan-1", "plan_version": 2},
                "id": "call-2",
                "type": "tool_call",
            }
        ],
    )
    values = {
        "messages": [
            _call(),
            _result(fresh["id"], "pending_review"),
            second_call,
            ToolMessage(
                tool_call_id="call-2",
                content=json.dumps(
                    {
                        "artifacts": [
                            {"id": second["id"], "kind": "subset", "version": 1}
                        ]
                    }
                ),
            ),
        ],
        "artifacts": {
            "datasets": {
                old["id"]: old,
                fresh["id"]: fresh,
                second["id"]: second,
            }
        },
    }

    with pytest.raises(AssertionError, match="successful extraction"):
        _assert_recovery(
            values,
            message_start=0,
            old_dataset_id=old["id"],
            old_record=old,
            old_file_hashes=_file_hashes(old),
            plan_id="plan-1",
            plan_version=2,
            expected_new_id=fresh["id"],
        )


def test_active_reuse_accepts_production_artifact_result(
    tmp_path: Path,
) -> None:
    active = _record(tmp_path, "subset-agent-active", "active")
    values = {
        "messages": [
            _call(),
            ToolMessage(
                tool_call_id="call-1",
                content=json.dumps(
                    {
                        "artifacts": [
                            {"id": active["id"], "kind": "subset", "version": 1}
                        ],
                        "message": "Reused validated SQL status=active.",
                    }
                ),
            ),
        ],
        "artifacts": {"datasets": {active["id"]: active}},
    }

    _assert_active_reuse(
        values,
        message_start=0,
        dataset_id=active["id"],
        dataset_record=active,
        dataset_file_hashes=_file_hashes(active),
        plan_id="plan-1",
        plan_version=2,
        expected_dataset_ids={active["id"]},
    )
