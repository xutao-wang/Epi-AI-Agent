from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
RUN_DOCKER_SMOKE = os.getenv("RUN_DOCKER_SMOKE") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_DOCKER_SMOKE,
    reason="set RUN_DOCKER_SMOKE=1 to run the real Docker integration smoke test",
)


def _run_docker(
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def _require_docker_daemon() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker daemon is unavailable")
    result = _run_docker(
        "version",
        "--format",
        "{{.Server.Version}}",
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Docker daemon is unavailable")


def _start_container(name: str, image: str, volume: str) -> str:
    _run_docker(
        "run",
        "--detach",
        "--name",
        name,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--user",
        "app",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "4g",
        "--publish",
        "127.0.0.1::8080",
        "--volume",
        f"{volume}:/app/runtime",
        image,
    )
    port_output = _run_docker("port", name, "8080/tcp").stdout.strip()
    port = port_output.rsplit(":", 1)[-1]
    return f"http://127.0.0.1:{int(port)}"


def _wait_for_health(base_url: str) -> None:
    deadline = time.monotonic() + 60
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=2)
            if response.status_code == 200:
                assert response.json() == {"status": "ok"}
                return
        except (httpx.HTTPError, AssertionError) as error:
            last_error = error
        time.sleep(0.25)
    raise AssertionError("Docker smoke API did not become healthy") from last_error


def test_docker_fastapi_worker_review_and_persistence(tmp_path):
    _require_docker_daemon()
    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
    image = f"epi-agent-smoke:{suffix}"
    volume = f"epi-agent-smoke-runtime-{suffix}"
    first_container = f"epi-agent-smoke-first-{suffix}"
    second_container = f"epi-agent-smoke-second-{suffix}"
    sentinel = tmp_path / f"host-sentinel-{suffix}"
    sentinel.write_text("host-only", encoding="utf-8")

    try:
        _run_docker("build", "--target", "smoke", "--tag", image, ".")
        _run_docker("volume", "create", volume)

        first_url = _start_container(first_container, image, volume)
        _wait_for_health(first_url)
        response = httpx.post(
            f"{first_url}/analysis",
            json={"code": "print(int(dataset['cases'].sum()))"},
            timeout=90,
        )
        response.raise_for_status()
        review = response.json()
        assert review["output_text"].strip() == "10"
        assert review["review_status"] == "pending_review"
        assert review["runtime"]["execution_mode"] == "guarded_local"
        assert review["runtime"]["security_boundary"] == "none"

        inspection = json.loads(
            _run_docker("inspect", first_container).stdout
        )[0]
        assert inspection["Config"]["User"] == "app"
        assert [mount["Destination"] for mount in inspection["Mounts"]] == [
            "/app/runtime"
        ]
        assert inspection["Mounts"][0]["Type"] == "volume"
        assert inspection["Mounts"][0]["Name"] == volume
        _run_docker(
            "exec",
            first_container,
            "test",
            "!",
            "-e",
            str(sentinel),
        )

        _run_docker("rm", "--force", first_container)
        second_url = _start_container(second_container, image, volume)
        _wait_for_health(second_url)
        persisted = httpx.get(
            f"{second_url}/analysis/latest",
            timeout=10,
        )
        persisted.raise_for_status()
        assert persisted.json() == review
    finally:
        _run_docker("rm", "--force", first_container, check=False)
        _run_docker("rm", "--force", second_container, check=False)
        _run_docker("volume", "rm", "--force", volume, check=False)
        _run_docker("image", "rm", "--force", image, check=False)
