from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _compose(filename: str = "compose.yaml") -> dict[str, object]:
    return yaml.safe_load((ROOT / filename).read_text(encoding="utf-8"))


def _dockerignore_patterns() -> set[str]:
    return {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_runtime_image_is_python_312_non_root_and_starts_fastapi():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim AS app-base" in dockerfile
    assert "FROM app-base AS runtime" in dockerfile
    runtime = dockerfile.split("FROM app-base AS runtime", 1)[1]
    assert "USER app" in runtime
    assert (
        'CMD ["python", "run_fastapi.py", "--host", "0.0.0.0", '
        '"--port", "8080"]'
    ) in runtime
    assert (ROOT / "frontend" / "dist" / "index.html").is_file()
    assert "frontend/dist" not in _dockerignore_patterns()


def test_docker_context_excludes_secrets_runtime_studies_and_local_scratch():
    patterns = _dockerignore_patterns()

    assert {
        ".env",
        ".worktrees",
        "runtime",
        "study_data",
        "config/custom_models.json",
        ".superpowers",
        ".streamlit",
        "AGENTS.md",
        "publication",
        "slides",
        "tmp",
        "todo.md",
    } <= patterns
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG OPENAI_API_KEY" not in dockerfile
    assert "ENV OPENAI_API_KEY" not in dockerfile
    assert "ARG ANTHROPIC_API_KEY" not in dockerfile
    assert "ENV ANTHROPIC_API_KEY" not in dockerfile
    assert "ARG OPENROUTER_API_KEY" not in dockerfile
    assert "ENV OPENROUTER_API_KEY" not in dockerfile


def test_compose_publishes_only_loopback_and_hardens_the_service():
    service = _compose()["services"]["epi-agent"]

    assert service["build"]["target"] == "runtime"
    assert service["ports"] == ["127.0.0.1:8080:8080"]
    assert service["init"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["pids_limit"] == 256
    assert service["mem_limit"] == "4g"
    assert "/var/run/docker.sock" not in json.dumps(service)


def test_default_compose_mounts_only_named_runtime_storage():
    document = _compose()
    service = document["services"]["epi-agent"]

    assert service["volumes"] == ["epi-agent-runtime:/app/runtime"]
    assert "epi-agent-runtime" in document["volumes"]


def test_study_override_requires_one_read_only_narrow_mount():
    service = _compose("compose.study-data.yaml")["services"]["epi-agent"]

    assert service["volumes"] == [
        "${REPORT_AGENT_STUDY_DATA:?Set REPORT_AGENT_STUDY_DATA to one "
        "study-data directory}:/app/study_data:ro"
    ]


def test_compose_requires_acknowledgement_at_interpolation_time():
    environment = _compose()["services"]["epi-agent"]["environment"]
    acknowledgement = environment[
        "REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED"
    ]

    assert acknowledgement.startswith(
        "${REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED:?"
    )
    assert environment["REPORT_AGENT_CONTAINER"] == "1"
    assert environment["OPENAI_API_KEY"] == "${OPENAI_API_KEY:-}"
