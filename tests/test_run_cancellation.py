import pytest

from utils.run_cancellation import (
    CancellationToken,
    RunCancelled,
    bind_cancellation,
    cancellation_point,
)


def test_cancellation_point_is_inert_without_a_bound_token() -> None:
    cancellation_point()


def test_bound_token_raises_only_after_cancel() -> None:
    token = CancellationToken()
    with bind_cancellation(token):
        cancellation_point()
        token.cancel()
        with pytest.raises(RunCancelled):
            cancellation_point()


def test_binding_is_restored_after_context_exit() -> None:
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RunCancelled):
        with bind_cancellation(token):
            cancellation_point()
    cancellation_point()
