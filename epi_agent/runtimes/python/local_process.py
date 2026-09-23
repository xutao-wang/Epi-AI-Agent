from __future__ import annotations

import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping

import pandas as pd
from pydantic import ValidationError

from epi_agent.runtimes.python.models import (
    PythonExecutionRequest,
    PythonExecutionResult,
    PythonRuntimeFailure,
)
from tools.execution_policy import validate_generated_code
from utils.run_cancellation import RunCancelled, cancellation_point


MAX_STDOUT_BYTES = 100_000
MAX_CHILD_STDOUT_BYTES = 100_000
MAX_CHILD_STDERR_BYTES = 100_000
MAX_RESULT_BYTES = 1_000_000
MAX_FIGURE_BYTES = 10_000_000
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
_ALLOWED_ENVIRONMENT = ("PATH", "LANG", "LC_ALL", "PYTHONUTF8")
_NPROC_HEADROOM = 128
_NPROC_FLOOR = 256
_NPROC_FALLBACK = 4096


class _NativeOutputLimit(RuntimeError):
    pass


def _child_environment(run_dir: Path) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in _ALLOWED_ENVIRONMENT
        if name in os.environ
    }
    environment.update(
        {
            "PYTHONUTF8": "1",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(run_dir / "matplotlib"),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
        }
    )
    return environment


def _worker_command() -> list[str]:
    return [sys.executable, str(Path(__file__).with_name("worker.py"))]


def _failure(
    code: str,
    message: str,
    *,
    category: str,
    recoverable: bool,
    execution_started: bool = False,
    details: dict[str, object] | None = None,
) -> PythonRuntimeFailure:
    return PythonRuntimeFailure(
        code,
        message,
        category=category,
        recoverable=recoverable,
        execution_started=execution_started,
        details=details,
    )


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"Non-JSON numeric constant: {value}")


def _read_bounded(path: Path, maximum: int, *, code: str) -> bytes:
    if not path.exists():
        return b""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise _failure(
            "OUTPUT_UNREADABLE",
            "Python runtime output could not be read.",
            category="infrastructure",
            recoverable=False,
        ) from exc
    if size > maximum:
        raise _failure(
            code,
            "Python runtime output exceeded its size limit.",
            category="invalid_output",
            recoverable=True,
        )
    return path.read_bytes()


def _nproc_limit() -> int:
    """RLIMIT_NPROC counts the invoking user's tasks machine-wide, so a fixed
    cap breaks on busy hosts. Budget headroom above the current task count to
    keep fork bombs bounded without starving BLAS/thread-pool startup."""
    try:
        uid = os.getuid()
        total = 0
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                with open(f"/proc/{entry.name}/status", "rb") as handle:
                    matched_uid = False
                    threads = 0
                    for raw in handle:
                        if raw.startswith(b"Uid:"):
                            matched_uid = int(raw.split()[1]) == uid
                        elif raw.startswith(b"Threads:"):
                            threads = int(raw.split()[1])
                if matched_uid:
                    total += threads
            except (OSError, ValueError, IndexError):
                continue
        if total <= 0:
            return _NPROC_FALLBACK
        return max(_NPROC_FLOOR, total + _NPROC_HEADROOM)
    except OSError:
        return _NPROC_FALLBACK


def _resource_limit_specs(
    *,
    timeout_seconds: float,
    memory_limit_bytes: int | None,
    platform_name: str,
) -> tuple[tuple[str, int, int], ...]:
    cpu_seconds = max(1, int(math.ceil(timeout_seconds)))
    nproc = _nproc_limit()
    limits = [
        ("RLIMIT_CPU", cpu_seconds, cpu_seconds + 1),
        ("RLIMIT_FSIZE", MAX_FIGURE_BYTES + MAX_RESULT_BYTES, -1),
        ("RLIMIT_NPROC", nproc, nproc),
    ]
    if memory_limit_bytes is not None and platform_name != "darwin":
        limits.append(
            ("RLIMIT_AS", memory_limit_bytes, memory_limit_bytes),
        )
    return tuple(limits)


def _preexec_limits(
    *,
    timeout_seconds: float,
    memory_limit_bytes: int | None,
):
    def apply_limits() -> None:
        import resource

        limits = _resource_limit_specs(
            timeout_seconds=timeout_seconds,
            memory_limit_bytes=memory_limit_bytes,
            platform_name=sys.platform,
        )
        for name, soft, hard in limits:
            resource_id = getattr(resource, name, None)
            if resource_id is None:
                continue
            current_soft, current_hard = resource.getrlimit(resource_id)
            desired_hard = hard if hard >= 0 else current_hard
            if current_hard != resource.RLIM_INFINITY:
                desired_hard = min(desired_hard, current_hard)
            desired_soft = min(soft, desired_hard)
            resource.setrlimit(resource_id, (desired_soft, desired_hard))

    return apply_limits


def _terminate_process_group(process: Any) -> None:
    try:
        process_group = os.getpgid(process.pid)
    except (OSError, ProcessLookupError):
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=2)


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
) -> tuple[bytes, bytes]:
    streams: dict[str, tuple[Any, bytearray, int]] = {
        "stdout": (process.stdout, bytearray(), MAX_CHILD_STDOUT_BYTES),
        "stderr": (process.stderr, bytearray(), MAX_CHILD_STDERR_BYTES),
    }
    selector = selectors.DefaultSelector()
    try:
        for name, (stream, _buffer, _limit) in streams.items():
            if stream is None:
                continue
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, data=name)

        while selector.get_map() or process.poll() is None:
            cancellation_point()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, 0)
            for key, _mask in selector.select(min(0.1, remaining)):
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                _stream, buffer, limit = streams[str(key.data)]
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise _NativeOutputLimit(str(key.data))

        process.wait(timeout=max(0.0, deadline - time.monotonic()))
        return bytes(streams["stdout"][1]), bytes(streams["stderr"][1])
    finally:
        selector.close()


def _parse_result(output_dir: Path, *, duration_seconds: float) -> PythonExecutionResult:
    result_bytes = _read_bounded(
        output_dir / "result.json",
        MAX_RESULT_BYTES,
        code="RESULT_TOO_LARGE",
    )
    if not result_bytes:
        raise _failure(
            "OUTPUT_MISSING",
            "Python runtime did not return result.json.",
            category="infrastructure",
            recoverable=False,
        )
    try:
        payload = json.loads(
            result_bytes.decode("utf-8"),
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _failure(
            "OUTPUT_MALFORMED",
            "Python runtime returned malformed JSON.",
            category="invalid_output",
            recoverable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise _failure(
            "OUTPUT_MALFORMED",
            "Python runtime result must be an object.",
            category="invalid_output",
            recoverable=True,
        )
    if payload.get("status") == "error":
        error = payload.get("error")
        if not isinstance(error, dict):
            raise _failure(
                "OUTPUT_MALFORMED",
                "Python runtime returned an invalid error envelope.",
                category="invalid_output",
                recoverable=True,
            )
        details = error.get("details")
        if details is not None and not isinstance(details, dict):
            raise _failure(
                "OUTPUT_MALFORMED",
                "Python runtime returned an invalid error envelope.",
                category="invalid_output",
                recoverable=True,
            )
        raise _failure(
            str(error.get("code") or "EXECUTION_FAILED"),
            str(error.get("message") or "Python execution failed.")[:2_000],
            category=str(error.get("category") or "infrastructure"),
            recoverable=bool(error.get("recoverable")),
            details=details,
        )
    if payload.get("status") != "ok":
        raise _failure(
            "OUTPUT_MALFORMED",
            "Python runtime returned an unknown status.",
            category="invalid_output",
            recoverable=True,
        )

    figure_png = _read_bounded(
        output_dir / "figure.png",
        MAX_FIGURE_BYTES,
        code="FIGURE_TOO_LARGE",
    )
    try:
        return PythonExecutionResult.model_validate(
            {
                "output_text": payload.get("output_text", ""),
                "warnings": payload.get("warnings", []),
                "figure_png": figure_png,
                "runtime": payload.get("runtime"),
                "duration_seconds": duration_seconds,
            }
        )
    except ValidationError as exc:
        raise _failure(
            "OUTPUT_MALFORMED",
            "Python runtime returned an invalid result envelope.",
            category="invalid_output",
            recoverable=True,
        ) from exc


class LocalPythonRuntime:
    def __init__(
        self,
        *,
        runtime_root: str | Path | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        memory_limit_bytes: int | None = _DEFAULT_MEMORY_LIMIT_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if memory_limit_bytes is not None and memory_limit_bytes <= 0:
            raise ValueError("memory_limit_bytes must be positive")
        self._runtime_root = (
            Path(runtime_root).expanduser().resolve()
            if runtime_root is not None
            else None
        )
        self._timeout_seconds = float(timeout_seconds)
        self._memory_limit_bytes = memory_limit_bytes

    def execute(
        self,
        request: PythonExecutionRequest,
        datasets: Mapping[str, pd.DataFrame],
    ) -> PythonExecutionResult:
        validation = validate_generated_code(request.code)
        if validation.failure_code is not None:
            raise _failure(
                validation.failure_code,
                validation.message,
                category=(
                    "policy_blocked"
                    if validation.failure_code == "POLICY_BLOCKED"
                    else "invalid_syntax"
                ),
                recoverable=True,
                details=validation.as_details(),
            )
        if request.selected_dataset_id not in datasets:
            raise _failure(
                "DATASET_NOT_AVAILABLE",
                "The selected dataset is not available for Python execution.",
                category="invalid_output",
                recoverable=True,
            )
        if self._runtime_root is not None:
            self._runtime_root.mkdir(parents=True, exist_ok=True)
        temporary_root = (
            str(self._runtime_root) if self._runtime_root is not None else None
        )
        with tempfile.TemporaryDirectory(
            prefix="report-agent-python-",
            dir=temporary_root,
        ) as temporary_directory:
            run_dir = Path(temporary_directory)
            input_dir = run_dir / "input"
            output_dir = run_dir / "output"
            work_dir = run_dir / "work"
            datasets_dir = input_dir / "datasets"
            datasets_dir.mkdir(parents=True)
            output_dir.mkdir()
            work_dir.mkdir()

            manifest: dict[str, str] = {}
            try:
                for index, (dataset_id, dataframe) in enumerate(datasets.items()):
                    filename = f"{index}.csv"
                    dataframe.to_csv(datasets_dir / filename, index=False)
                    manifest[str(dataset_id)] = filename
                (input_dir / "datasets.json").write_text(
                    json.dumps(manifest, ensure_ascii=True, sort_keys=True),
                    encoding="utf-8",
                )
                (input_dir / "selected_dataset_id.txt").write_text(
                    request.selected_dataset_id,
                    encoding="utf-8",
                )
                (input_dir / "code.py").write_text(
                    request.code,
                    encoding="utf-8",
                )
            except (OSError, TypeError, ValueError) as exc:
                raise _failure(
                    "INPUT_SERIALIZATION_FAILED",
                    "Unable to serialize Python analysis input.",
                    category="infrastructure",
                    recoverable=False,
                ) from exc

            environment = _child_environment(run_dir)
            command = _worker_command()
            command.extend(
                [
                    "--input-dir",
                    str(input_dir),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            started = time.monotonic()
            process: subprocess.Popen[bytes] | None = None
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(work_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    start_new_session=True,
                    preexec_fn=_preexec_limits(
                        timeout_seconds=self._timeout_seconds,
                        memory_limit_bytes=self._memory_limit_bytes,
                    ),
                )
                deadline = started + self._timeout_seconds
                _child_stdout, child_stderr = _communicate_bounded(
                    process,
                    deadline=deadline,
                )
            except RunCancelled:
                if process is not None:
                    _terminate_process_group(process)
                raise
            except _NativeOutputLimit as exc:
                if process is not None:
                    _terminate_process_group(process)
                raise _failure(
                    "PROCESS_OUTPUT_TOO_LARGE",
                    "Python worker stdout or stderr exceeded its size limit.",
                    category="invalid_output",
                    recoverable=True,
                    execution_started=True,
                ) from exc
            except subprocess.TimeoutExpired as exc:
                if process is not None:
                    _terminate_process_group(process)
                raise _failure(
                    "EXECUTION_TIMEOUT",
                    (
                        "Python analysis exceeded the configured "
                        f"{self._timeout_seconds:g} second timeout."
                    ),
                    category="timeout",
                    recoverable=True,
                    execution_started=True,
                ) from exc
            except (OSError, subprocess.SubprocessError) as exc:
                if process is not None:
                    _terminate_process_group(process)
                raise _failure(
                    "PROCESS_START_FAILED",
                    "Unable to start the local Python runtime.",
                    category="infrastructure",
                    recoverable=False,
                ) from exc

            duration_seconds = time.monotonic() - started
            assert process is not None
            if process.returncode != 0:
                detail = child_stderr.decode(
                    "utf-8",
                    errors="replace",
                ).strip()[:2_000]
                raise _failure(
                    "PROCESS_FAILED",
                    detail or "The local Python runtime process failed.",
                    category="infrastructure",
                    recoverable=False,
                    execution_started=True,
                )
            try:
                return _parse_result(
                    output_dir,
                    duration_seconds=duration_seconds,
                )
            except PythonRuntimeFailure as error:
                raise _failure(
                    error.code,
                    str(error),
                    category=error.category,
                    recoverable=error.recoverable,
                    execution_started=True,
                    details=error.details,
                ) from error
