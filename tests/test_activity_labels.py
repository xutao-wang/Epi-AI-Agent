from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.activity_labels import tool_activity_labels
from api.schemas import ActivityItem, ActivityRun, ApiThreadState, RunStatus


def test_known_tool_uses_curated_public_labels() -> None:
    labels = tool_activity_labels("dbrag-search_catalog")
    assert labels.started == "Searching the data catalog"
    assert labels.waiting is None
    assert labels.completed == "Searching the data catalog"


def test_review_tool_has_waiting_and_completed_labels() -> None:
    labels = tool_activity_labels("dbrag-request_dataset_plan_review")
    assert labels.started == "Preparing dataset plan review"
    assert labels.waiting == "Waiting for dataset plan review"
    assert labels.completed == "Dataset plan reviewed"


def test_unknown_registered_tool_uses_sanitized_name_only() -> None:
    labels = tool_activity_labels("custom-check_sample_balance")
    assert labels.started == "Check sample balance"
    assert labels.completed == "Check sample balance"
    assert "custom" not in labels.started.casefold()


def test_thread_state_defaults_to_no_activity_runs() -> None:
    projected = ApiThreadState(thread_id="thread-1", run=RunStatus(state="idle"))
    assert projected.activity_runs == []


def test_public_activity_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ActivityItem(
            id="item-1",
            sequence=1,
            label="Searching the data catalog",
            status="running",
            tool_name="dbrag-search_catalog",
            tool_call_id="call-1",
            created_at="2026-08-11T00:00:00+00:00",
            updated_at="2026-08-11T00:00:00+00:00",
            raw_tool_result={"secret": "must not be public"},
        )

    with pytest.raises(ValidationError):
        ActivityRun(
            id="run-1",
            thread_id="thread-1",
            user_message_id="user-1",
            state="running",
            created_at="2026-08-11T00:00:00+00:00",
            updated_at="2026-08-11T00:00:00+00:00",
            prompt="must not be public",
        )
