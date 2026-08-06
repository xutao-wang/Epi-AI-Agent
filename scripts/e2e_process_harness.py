"""Shared process controls for real FastAPI and TypeScript browser smokes."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from typing import Any

import requests


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[str]
    log_handle: Any
    log_path: Path
    owns_process_group: bool = True


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _find_available_port(host: str, preferred_port: int) -> int:
    for port in range(preferred_port, preferred_port + 100):
        if _port_is_available(host, port):
            return port
    raise RuntimeError(
        f"No available port found from {preferred_port} to "
        f"{preferred_port + 99}."
    )


def _start_process(
    *,
    name: str,
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    start_new_session: bool = True,
) -> ManagedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=start_new_session,
    )
    return ManagedProcess(
        name=name,
        process=process,
        log_handle=log_handle,
        log_path=log_path,
        owns_process_group=start_new_session,
    )


def _tail_log(path: Path, *, max_chars: int = 4000) -> str:
    try:
        return path.read_text(
            encoding="utf-8",
            errors="replace",
        )[-max_chars:]
    except OSError as error:
        return (
            f"<failed to read {path}: "
            f"{type(error).__name__}: {error}>"
        )


def _assert_processes_alive(processes: list[ManagedProcess]) -> None:
    for managed in processes:
        exit_code = managed.process.poll()
        if exit_code is not None:
            raise AssertionError(
                f"{managed.name} process exited with code {exit_code}. "
                f"Log: {managed.log_path}\n{_tail_log(managed.log_path)}"
            )


def _wait_for_http(
    url: str,
    *,
    deadline: float,
    name: str,
    expected_status: int,
    processes: list[ManagedProcess],
) -> None:
    last_error = ""
    while time.monotonic() < deadline:
        _assert_processes_alive(processes)
        try:
            response = requests.get(url, timeout=2)
            if response.status_code == expected_status:
                _assert_processes_alive(processes)
                return
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as error:
            last_error = f"{type(error).__name__}: {error}"
        time.sleep(0.5)
    raise AssertionError(
        f"Timed out waiting for {name} at {url}. "
        f"Last error: {last_error}"
    )


def _terminate_process(
    managed: ManagedProcess | None,
    *,
    deadline: float | None = None,
) -> str | None:
    if managed is None:
        return None

    process = managed.process
    deadline = deadline if deadline is not None else time.monotonic() + 5.0
    warning: str | None = None

    def signal_managed_process(signum: int) -> None:
        if managed.owns_process_group:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass
            return
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass

    try:
        signal_managed_process(signal.SIGTERM)
        graceful_deadline = min(
            deadline,
            time.monotonic()
            + max(0.0, (deadline - time.monotonic()) / 2),
        )
        while (
            process.poll() is None
            and time.monotonic() < graceful_deadline
        ):
            time.sleep(0.02)
        signal_managed_process(signal.SIGKILL)
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if process.poll() is None:
            warning = (
                f"{managed.name} teardown deadline expired with PID "
                f"{process.pid} still present"
            )
    except Exception as error:
        warning = (
            f"{managed.name} teardown failed: "
            f"{type(error).__name__}: {error}"
        )
        try:
            signal_managed_process(signal.SIGKILL)
        except Exception:
            pass
    finally:
        try:
            managed.log_handle.close()
        except Exception as error:
            if warning is None:
                warning = (
                    f"{managed.name} log close failed: "
                    f"{type(error).__name__}: {error}"
                )
    return warning
