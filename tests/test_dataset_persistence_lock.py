from __future__ import annotations

import multiprocessing
from pathlib import Path
import time

import pytest

from utils.dataset_artifacts import (
    DatasetPersistenceLockTimeout,
    dataset_attempt_family_ids,
    dataset_persistence_lock,
)
from utils.run_cancellation import RunCancelled


def _hold_lock(dataset_root: str, ready: multiprocessing.synchronize.Event) -> None:
    with dataset_persistence_lock(
        runtime_root=dataset_root,
        base_dataset_id="subset-agent-family",
    ):
        ready.set()
        time.sleep(1.0)


def test_persistence_lock_excludes_an_independent_process(tmp_path: Path) -> None:
    ready = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_lock, args=(str(tmp_path), ready))
    process.start()
    try:
        assert ready.wait(timeout=2)
        started = time.monotonic()
        with pytest.raises(DatasetPersistenceLockTimeout):
            with dataset_persistence_lock(
                runtime_root=tmp_path,
                base_dataset_id="subset-agent-family",
                timeout_seconds=0.15,
                poll_interval_seconds=0.01,
            ):
                pytest.fail("concurrent holder entered the protected scope")
        assert time.monotonic() - started >= 0.1
    finally:
        process.join(timeout=3)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
    assert process.exitcode == 0

    with dataset_persistence_lock(
        runtime_root=tmp_path,
        base_dataset_id="subset-agent-family",
        timeout_seconds=0.2,
    ):
        pass


def test_persistence_lock_stops_waiting_when_cancelled(tmp_path: Path) -> None:
    checks = 0

    def check_cancellation() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise RunCancelled("cancelled")

    with dataset_persistence_lock(
        runtime_root=tmp_path,
        base_dataset_id="subset-agent-family",
    ):
        with pytest.raises(RunCancelled):
            with dataset_persistence_lock(
                runtime_root=tmp_path,
                base_dataset_id="subset-agent-family",
                cancellation_point=check_cancellation,
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            ):
                pytest.fail("cancelled waiter entered the protected scope")


@pytest.mark.parametrize("invalid_timeout", [0, -1, 30.01])
def test_persistence_lock_rejects_timeout_outside_bound(invalid_timeout: float, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        with dataset_persistence_lock(
            runtime_root=tmp_path,
            base_dataset_id="subset-agent-family",
            timeout_seconds=invalid_timeout,
        ):
            pass


def test_persistence_lock_rejects_symlinked_journal_directory(tmp_path: Path) -> None:
    dataset_root = tmp_path / "datasets"
    external = tmp_path / "external"
    dataset_root.mkdir()
    external.mkdir()
    (dataset_root / ".persistence_attempts").symlink_to(
        external,
        target_is_directory=True,
    )

    with pytest.raises(ValueError, match="configured dataset root"):
        with dataset_persistence_lock(
            runtime_root=dataset_root,
            base_dataset_id="subset-agent-family",
        ):
            pass

    assert not (external / ".locks").exists()


def test_family_discovery_rejects_symlinked_journal_directory(tmp_path: Path) -> None:
    dataset_root = tmp_path / "datasets"
    external = tmp_path / "external"
    dataset_root.mkdir()
    external.mkdir()
    (dataset_root / ".persistence_attempts").symlink_to(
        external,
        target_is_directory=True,
    )
    (external / "subset-agent-family.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="configured dataset root"):
        dataset_attempt_family_ids(
            runtime_root=dataset_root,
            base_dataset_id="subset-agent-family",
        )
