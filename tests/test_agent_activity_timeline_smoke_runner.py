from __future__ import annotations

import time
from typing import Any

import pytest

from api.auth import LOCAL_SESSION_ID
import scripts.e2e_agent_activity_timeline_real as smoke


def test_plain_language_timeline_accepts_friendly_activity_labels() -> None:
    smoke._assert_plain_language_timeline(
        "Searching the data catalog\nWaiting for dataset plan review"
    )


def test_plain_language_timeline_rejects_technical_tool_name_leakage() -> None:
    with pytest.raises(AssertionError, match="technical tool-name leakage"):
        smoke._assert_plain_language_timeline(
            "Searching the data catalog\ndbrag-search_catalog"
        )


class HiddenLocator:
    def is_visible(self, *, timeout: int) -> bool:
        assert timeout == 100
        return False


class NoReviewPage:
    def get_by_role(self, role: str, *, name: str, exact: bool):
        assert role in {"heading", "button"}
        assert name in {
            "Review dataset plan",
            "Approve & continue",
            "Approve plan and extract",
        }
        assert exact is True
        return HiddenLocator()


class VisibleLocator:
    def is_visible(self, *, timeout: int) -> bool:
        assert timeout == 100
        return True


class StepwiseReviewPage:
    def get_by_role(self, role: str, *, name: str, exact: bool):
        assert exact is True
        if role == "heading" and name == "Review dataset plan":
            return VisibleLocator()
        if role == "button" and name == "Approve & continue":
            return VisibleLocator()
        return HiddenLocator()


def test_default_query_uses_only_supported_baseline_fields() -> None:
    assert smoke.DEFAULT_QUERY == (
        "Create a baseline index-case dataset from Form 2A - INDEX CASE: "
        "Clinical/Demographic Form with participant ID, age, sex, and marital "
        "status. Present the dataset plan for review."
    )
    assert "diabetes" not in smoke.DEFAULT_QUERY.casefold()


def test_review_wait_stops_when_api_run_is_already_terminal() -> None:
    wait_for_review = getattr(smoke, "_wait_for_dataset_plan_review", None)
    assert wait_for_review is not None

    state = {
        "run": {
            "state": "error",
            "error_code": "OPENAI_CREDITS_EXHAUSTED",
            "user_message": "The OpenAI account has no remaining API credits.",
        }
    }

    with pytest.raises(RuntimeError, match="OPENAI_CREDITS_EXHAUSTED"):
        wait_for_review(
            NoReviewPage(),
            api_url="http://unused.test",
            deadline=time.monotonic() + 60,
            state_reader=lambda _url: state,
        )


def test_review_wait_fails_when_run_completes_without_review() -> None:
    state = {"run": {"state": "done"}}

    with pytest.raises(
        RuntimeError,
        match="Agent run ended before dataset-plan review:.*state done",
    ):
        smoke._wait_for_dataset_plan_review(
            NoReviewPage(),
            api_url="http://unused.test",
            deadline=time.monotonic() + 0.1,
            state_reader=lambda _url: state,
        )


def test_review_wait_treats_empty_conversation_history_as_transient() -> None:
    attempts = 0

    def state_reader(_url: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AssertionError("Expected exactly one conversation, received [].")
        return {
            "run": {
                "state": "error",
                "error_code": "OPENAI_CREDITS_EXHAUSTED",
                "user_message": "No API credits remain.",
            }
        }

    with pytest.raises(RuntimeError, match="OPENAI_CREDITS_EXHAUSTED"):
        smoke._wait_for_dataset_plan_review(
            NoReviewPage(),
            api_url="http://unused.test",
            deadline=time.monotonic() + 60,
            state_reader=state_reader,
        )

    assert attempts == 2


def test_review_wait_accepts_the_initial_stepwise_review_controls() -> None:
    def unexpected_state_read(_url: str):
        raise AssertionError("Review was already visible; API state was unnecessary.")

    smoke._wait_for_dataset_plan_review(
        StepwiseReviewPage(),
        api_url="http://unused.test",
        deadline=time.monotonic() + 60,
        state_reader=unexpected_state_read,
    )


def test_review_wait_rejects_unexpected_scientific_clarification() -> None:
    state = {
        "run": {"state": "interrupted"},
        "active_interrupt": {
            "id": "clarification-1",
            "type": "agent_clarification",
            "question": "Which scientific proxy should be used?",
            "options": [
                {"id": "proxy", "label": "Use a proxy"},
                {"id": "omit", "label": "Omit the variable"},
            ],
        },
    }

    with pytest.raises(
        RuntimeError,
        match="unexpected clarification.*Which scientific proxy",
    ):
        smoke._wait_for_dataset_plan_review(
            NoReviewPage(),
            api_url="http://unused.test",
            deadline=time.monotonic() + 0.1,
            state_reader=lambda _url: state,
        )


def test_timeline_label_wait_accepts_repeated_matching_rows() -> None:
    wait_for_label = getattr(smoke, "_wait_for_timeline_label", None)
    assert wait_for_label is not None
    observed: list[tuple[str, int]] = []

    class FirstMatch:
        def wait_for(self, *, timeout: int) -> None:
            observed.append(("first", timeout))

    class MultipleMatches:
        @property
        def first(self):
            return FirstMatch()

    class Timeline:
        def get_by_text(self, label: str, *, exact: bool):
            assert label == "Searching the data catalog"
            assert exact is True
            return MultipleMatches()

    wait_for_label(
        Timeline(),
        "Searching the data catalog",
        deadline=time.monotonic() + 1,
    )

    assert len(observed) == 1
    assert observed[0][0] == "first"
    assert observed[0][1] > 0


def test_browser_diagnostics_are_captured_before_browser_closes() -> None:
    diagnostic_browser = getattr(smoke, "_diagnostic_browser", None)
    assert diagnostic_browser is not None
    events: list[str] = []

    class Browser:
        def close(self) -> None:
            events.append("close")

    def record(_error: BaseException) -> None:
        events.append("diagnostics")

    with pytest.raises(RuntimeError, match="browser flow failed"):
        with diagnostic_browser(Browser(), record):
            events.append("flow")
            raise RuntimeError("browser flow failed")

    assert events == ["flow", "diagnostics", "close"]


class JsonResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def test_thread_state_sends_the_canonical_local_session_header(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    responses = iter(
        [
            JsonResponse({"items": [{"thread_id": "thread-456"}]}),
            JsonResponse({"run": {"state": "running"}}),
        ]
    )

    def fake_get(url: str, **kwargs: Any) -> JsonResponse:
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(smoke.requests, "get", fake_get)

    assert smoke._thread_state("http://127.0.0.1:8000") == {
        "run": {"state": "running"}
    }
    expected_headers = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
    assert calls == [
        (
            "http://127.0.0.1:8000/api/conversations",
            {"headers": expected_headers, "timeout": 5},
        ),
        (
            "http://127.0.0.1:8000/api/threads/thread-456/state",
            {"headers": expected_headers, "timeout": 5},
        ),
    ]
