from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import threading
import time

import pandas as pd
import pytest

from epi_agent.runtimes.python import LocalPythonRuntime, PythonExecutionRequest
from epi_agent.runtimes.python import local_process
from epi_agent.runtimes.python.local_process import (
    _child_environment,
    _parse_result,
    _resource_limit_specs,
)
from epi_agent.runtimes.python.models import PythonRuntimeFailure
from utils.run_cancellation import (
    CancellationToken,
    RunCancelled,
    bind_cancellation,
)


def _request(code: str = "print('unused')") -> PythonExecutionRequest:
    return PythonExecutionRequest(code=code, selected_dataset_id="selected")


def _datasets() -> dict[str, pd.DataFrame]:
    return {"selected": pd.DataFrame({"cases": [1]})}


def _sleeping_worker(tmp_path: Path) -> tuple[Path, Path]:
    pid_file = tmp_path / "descendant.pid"
    script = tmp_path / "sleeping_worker.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    return script, pid_file


def _assert_process_disappears(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"process {pid} survived worker process-group cleanup")


def _cancel_after_file_exists(token: CancellationToken, pid_file: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.01)
    token.cancel()


def test_runtime_executes_against_only_the_selected_dataset(tmp_path):
    runtime = LocalPythonRuntime(
        runtime_root=tmp_path,
        timeout_seconds=180,
        memory_limit_bytes=None,
    )

    result = runtime.execute(
        PythonExecutionRequest(
            code="print(int(dataset['cases'].sum()))",
            selected_dataset_id="selected",
        ),
        {
            "selected": pd.DataFrame({"cases": [2, 3]}),
            "other": pd.DataFrame({"cases": [100]}),
        },
    )

    assert result.output_text.strip() == "5"
    assert result.figure_png == b""
    assert result.runtime.language == "Python"
    assert result.runtime.execution_mode == "guarded_local"
    assert result.runtime.security_boundary == "none"
    assert list(tmp_path.iterdir()) == []


def test_child_environment_omits_credentials_proxies_and_python_overrides(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test")
    monkeypatch.setenv("PYTHONPATH", "/host/code")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = _child_environment(tmp_path)

    assert environment["PATH"] == "/usr/bin"
    assert environment["MPLBACKEND"] == "Agg"
    assert environment["MPLCONFIGDIR"] == str(tmp_path / "matplotlib")
    assert environment["OPENBLAS_NUM_THREADS"] == "1"
    assert "OPENAI_API_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "PYTHONPATH" not in environment


def test_resource_limits_cover_cpu_process_file_and_linux_memory():
    linux_names = {
        name
        for name, _soft, _hard in _resource_limit_specs(
            timeout_seconds=2.5,
            memory_limit_bytes=1024,
            platform_name="linux",
        )
    }
    darwin_names = {
        name
        for name, _soft, _hard in _resource_limit_specs(
            timeout_seconds=2.5,
            memory_limit_bytes=1024,
            platform_name="darwin",
        )
    }

    assert {"RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NPROC", "RLIMIT_AS"} <= linux_names
    assert darwin_names == {"RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NPROC"}


def test_parent_rejects_malformed_worker_result(tmp_path):
    (tmp_path / "result.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(PythonRuntimeFailure) as caught:
        _parse_result(tmp_path, duration_seconds=0.1)

    assert caught.value.code == "OUTPUT_MALFORMED"
    assert caught.value.recoverable is True


def test_generated_stdout_limit_returns_typed_failure(tmp_path):
    runtime = LocalPythonRuntime(
        runtime_root=tmp_path,
        timeout_seconds=180,
        memory_limit_bytes=None,
    )

    with pytest.raises(PythonRuntimeFailure) as caught:
        runtime.execute(_request("print('x' * 100_001)"), _datasets())

    assert caught.value.code == "STDOUT_TOO_LARGE"
    assert caught.value.category == "invalid_output"
    assert caught.value.recoverable is True


def test_native_stderr_is_bounded_before_process_exit(monkeypatch, tmp_path):
    script = tmp_path / "flood.py"
    script.write_text(
        "import sys, time\n"
        "sys.stderr.buffer.write(b'x' * 200_000)\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(local_process, "MAX_CHILD_STDERR_BYTES", 16_384)
    monkeypatch.setattr(
        local_process,
        "_worker_command",
        lambda: [sys.executable, str(script)],
    )
    runtime = LocalPythonRuntime(
        runtime_root=tmp_path,
        timeout_seconds=5,
        memory_limit_bytes=None,
    )

    with pytest.raises(PythonRuntimeFailure) as caught:
        runtime.execute(
            PythonExecutionRequest(
                code="print('unused')",
                selected_dataset_id="selected",
            ),
            {"selected": pd.DataFrame({"cases": [1]})},
        )

    assert caught.value.code == "PROCESS_OUTPUT_TOO_LARGE"
    assert caught.value.category == "invalid_output"


def test_timeout_terminates_worker_descendants(monkeypatch, tmp_path):
    script, pid_file = _sleeping_worker(tmp_path)
    monkeypatch.setattr(
        local_process,
        "_worker_command",
        lambda: [sys.executable, str(script)],
    )

    with pytest.raises(PythonRuntimeFailure) as caught:
        LocalPythonRuntime(
            runtime_root=tmp_path,
            timeout_seconds=0.5,
            memory_limit_bytes=None,
        ).execute(_request(), _datasets())

    assert caught.value.code == "EXECUTION_TIMEOUT"
    _assert_process_disappears(int(pid_file.read_text(encoding="utf-8")))


def test_cancellation_terminates_worker_descendants(monkeypatch, tmp_path):
    script, pid_file = _sleeping_worker(tmp_path)
    monkeypatch.setattr(
        local_process,
        "_worker_command",
        lambda: [sys.executable, str(script)],
    )
    token = CancellationToken()
    canceller = threading.Thread(
        target=_cancel_after_file_exists,
        args=(token, pid_file),
    )
    canceller.start()

    with bind_cancellation(token), pytest.raises(RunCancelled):
        LocalPythonRuntime(
            runtime_root=tmp_path,
            timeout_seconds=5,
            memory_limit_bytes=None,
        ).execute(_request(), _datasets())

    canceller.join(timeout=2)
    _assert_process_disappears(int(pid_file.read_text(encoding="utf-8")))


def test_process_start_failure_is_typed(monkeypatch, tmp_path):
    def fail_to_start(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(local_process.subprocess, "Popen", fail_to_start)

    with pytest.raises(PythonRuntimeFailure) as caught:
        LocalPythonRuntime(
            runtime_root=tmp_path,
            memory_limit_bytes=None,
        ).execute(_request(), _datasets())

    assert caught.value.code == "PROCESS_START_FAILED"
    assert caught.value.category == "infrastructure"


def test_pipe_error_terminates_started_worker(monkeypatch, tmp_path):
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(
        local_process,
        "_worker_command",
        lambda: [sys.executable, str(script)],
    )
    started_pids: list[int] = []

    def fail_while_collecting(process, *, deadline):
        started_pids.append(process.pid)
        raise OSError("pipe failed")

    monkeypatch.setattr(local_process, "_communicate_bounded", fail_while_collecting)
    try:
        with pytest.raises(PythonRuntimeFailure) as caught:
            LocalPythonRuntime(
                runtime_root=tmp_path,
                memory_limit_bytes=None,
            ).execute(_request(), _datasets())
        assert caught.value.code == "PROCESS_START_FAILED"
        _assert_process_disappears(started_pids[0])
    finally:
        if started_pids:
            try:
                os.killpg(started_pids[0], signal.SIGKILL)
            except ProcessLookupError:
                pass
