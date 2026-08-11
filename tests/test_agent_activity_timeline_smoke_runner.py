from __future__ import annotations

import time

import pytest

import scripts.e2e_agent_activity_timeline_real as smoke


class HiddenLocator:
    def is_visible(self, *, timeout: int) -> bool:
        assert timeout == 100
        return False


class NoReviewPage:
    def get_by_role(self, role: str, *, name: str, exact: bool):
        assert role in {"heading", "button"}
        assert name in {"Review dataset plan", "Approve plan and extract"}
        assert exact is True
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
