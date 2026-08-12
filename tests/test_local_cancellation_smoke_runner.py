from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

from api.auth import LOCAL_SESSION_ID
import scripts.e2e_active_run_cancellation_real as smoke


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run_active_run_cancellation_smoke_local.sh"


def test_local_runner_uses_selected_environment_and_worktree_smoke(
    tmp_path: Path,
) -> None:
    environment_root = tmp_path / "main-checkout"
    python_executable = environment_root / ".venv" / "bin" / "python"
    python_executable.parent.mkdir(parents=True)
    python_executable.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$SMOKE_CAPTURE_PATH"\n',
        encoding="utf-8",
    )
    python_executable.chmod(0o755)
    capture_path = tmp_path / "arguments.txt"

    environment = dict(os.environ)
    environment["SMOKE_CAPTURE_PATH"] = str(capture_path)
    result = subprocess.run(
        [str(RUNNER), str(environment_root)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        str(PROJECT_ROOT / "scripts" / "e2e_active_run_cancellation_real.py"),
        "--environment-root",
        str(environment_root),
        "--timeout-seconds",
        "240",
    ]


def test_browser_launch_falls_back_to_installed_chrome() -> None:
    expected_browser = object()

    class Chromium:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        def launch(self, **kwargs: str) -> object:
            self.calls.append(kwargs)
            if not kwargs:
                raise RuntimeError(
                    "BrowserType.launch: Executable doesn't exist at bundled-browser"
                )
            assert kwargs == {"channel": "chrome"}
            return expected_browser

    chromium = Chromium()
    playwright = type("Playwright", (), {"chromium": chromium})()

    assert smoke._launch_browser(playwright) is expected_browser
    assert chromium.calls == [{}, {"channel": "chrome"}]


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
            JsonResponse({"items": [{"thread_id": "thread-123"}]}),
            JsonResponse({"run": {"state": "cancelled"}}),
        ]
    )

    def fake_get(url: str, **kwargs: Any) -> JsonResponse:
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(smoke.requests, "get", fake_get)

    assert smoke._thread_state("http://127.0.0.1:8000") == {
        "run": {"state": "cancelled"}
    }
    expected_headers = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
    assert calls == [
        (
            "http://127.0.0.1:8000/api/conversations",
            {"headers": expected_headers, "timeout": 5},
        ),
        (
            "http://127.0.0.1:8000/api/threads/thread-123/state",
            {"headers": expected_headers, "timeout": 5},
        ),
    ]
