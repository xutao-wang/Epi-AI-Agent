from __future__ import annotations

import time
from typing import Any

import pytest

from api.auth import LOCAL_SESSION_ID
import scripts.e2e_agent_activity_timeline_real as smoke


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


def test_review_wait_answers_expected_diabetes_clarification() -> None:
    events: list[str] = []

    class InteractiveLocator:
        def __init__(self, action: str):
            self.action = action

        def is_visible(self, *, timeout: int) -> bool:
            assert timeout == 100
            return self.action == "review" and "continue" in events

        def check(self) -> None:
            events.append(self.action)

        def click(self) -> None:
            events.append(self.action)

    class ClarificationPage:
        def get_by_role(self, role: str, *, name: str, exact: bool):
            assert exact is True
            if role == "heading" and name == "Review dataset plan":
                return InteractiveLocator("review")
            if role == "button" and name == "Approve & continue":
                return InteractiveLocator("review")
            if role == "button" and name == "Approve plan and extract":
                return HiddenLocator()
            if role == "button" and name == "Continue":
                return InteractiveLocator("continue")
            raise AssertionError((role, name))

        def get_by_label(self, name: str, *, exact: bool):
            assert exact is True
            assert name == "Use medication as the diabetes proxy"
            return InteractiveLocator("medication_proxy")

    state = {
        "run": {"state": "interrupted"},
        "active_interrupt": {
            "id": "clarification-1",
            "type": "agent_clarification",
            "options": [
                {
                    "id": "medication_proxy",
                    "label": "Use medication as the diabetes proxy",
                },
                {"id": "omit_diabetes", "label": "Omit diabetes"},
            ],
        },
    }

    smoke._wait_for_dataset_plan_review(
        ClarificationPage(),
        api_url="http://unused.test",
        deadline=time.monotonic() + 0.5,
        state_reader=lambda _url: state,
    )

    assert events == ["medication_proxy", "continue"]


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
