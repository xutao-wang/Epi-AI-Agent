from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import pytest

from epi_agent.runtimes.python import LocalPythonRuntime, PythonExecutionRequest
from epi_agent.runtimes.python import local_process


class _SuccessfulWorker:
    returncode = 0

    def __init__(self, command: list[str], **kwargs: Any) -> None:
        self.command = command
        self.kwargs = kwargs
        output_dir = Path(command[command.index("--output-dir") + 1])
        (output_dir / "result.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "output_text": "ok",
                    "runtime": {"version": "test"},
                }
            ),
            encoding="utf-8",
        )

    def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
        del timeout
        return b"", b""


@pytest.mark.parametrize(
    ("worker_launcher", "expected_prefix"),
    [
        (
            (
                "/usr/bin/sudo",
                "-n",
                "/usr/local/libexec/epi-agent-python-worker",
            ),
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/local/libexec/epi-agent-python-worker",
            ],
        ),
        (
            None,
            [
                sys.executable,
                str(Path(local_process.__file__).with_name("worker.py")),
            ],
        ),
    ],
    ids=("hosted-launcher", "native-local-worker"),
)
def test_python_runtime_uses_configured_launcher_or_native_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_launcher: tuple[str, ...] | None,
    expected_prefix: list[str],
) -> None:
    launched: list[_SuccessfulWorker] = []

    def popen(command: list[str], **kwargs: Any) -> _SuccessfulWorker:
        process = _SuccessfulWorker(command, **kwargs)
        launched.append(process)
        return process

    for name in (
        "OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        monkeypatch.setenv(name, "must-not-reach-python-worker")
    monkeypatch.setattr(local_process.subprocess, "Popen", popen)
    runtime = LocalPythonRuntime(
        runtime_root=tmp_path,
        worker_launcher=worker_launcher,
        memory_limit_bytes=None,
    )

    result = runtime.execute(
        PythonExecutionRequest(
            code="print(len(dataset))",
            selected_dataset_id="dataset",
        ),
        {"dataset": pd.DataFrame({"value": [1]})},
    )

    assert result.output_text == "ok"
    assert len(launched) == 1
    command = launched[0].command
    assert command[: len(expected_prefix)] == expected_prefix
    assert command[len(expected_prefix) :] == [
        "--input-dir",
        command[len(expected_prefix) + 1],
        "--output-dir",
        command[len(expected_prefix) + 3],
    ]
    environment = launched[0].kwargs["env"]
    assert all(
        name not in environment
        for name in (
            "OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        )
    )


@pytest.mark.parametrize(
    "worker_launcher",
    [
        (),
        ("relative-launcher",),
        ("/usr/local/libexec/alternate-worker",),
        ("/usr/bin/sudo", "-n", "/usr/local/libexec/alternate-worker"),
        ("/usr/bin/sudo", "-n", "/usr/local/libexec/epi-agent-python-worker", "--x"),
    ],
)
def test_python_runtime_rejects_invalid_worker_launcher(
    worker_launcher: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        LocalPythonRuntime(worker_launcher=worker_launcher)
