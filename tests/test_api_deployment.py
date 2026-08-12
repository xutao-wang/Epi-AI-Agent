import json
import os
from pathlib import Path

import pytest

from api.deployment import DeploymentState


VALID_RELEASE_ID = "a" * 40


def write_manifest(tmp_path: Path, payload: object) -> Path:
    manifest = tmp_path / "release.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def test_deployment_state_reads_release_and_maintenance(monkeypatch, tmp_path):
    sentinel = tmp_path / "maintenance"
    monkeypatch.setenv("REPORT_AGENT_MAINTENANCE_FILE", str(sentinel))
    monkeypatch.setenv("REPORT_AGENT_RELEASE_ID", "abc123")

    state = DeploymentState.from_environ(os.environ)

    assert state.release_id == "abc123"
    assert state.maintenance_enabled() is False
    sentinel.touch()
    assert state.maintenance_enabled() is True


def test_deployment_state_defaults_without_configuration(monkeypatch):
    monkeypatch.delenv("REPORT_AGENT_MAINTENANCE_FILE", raising=False)
    monkeypatch.delenv("REPORT_AGENT_RELEASE_ID", raising=False)

    state = DeploymentState.from_environ(os.environ)

    assert state.maintenance_file is None
    assert state.release_id == "development"


def test_deployment_state_uses_valid_manifest_without_environment(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("REPORT_AGENT_RELEASE_ID", raising=False)
    manifest = write_manifest(tmp_path, {"commit_sha": VALID_RELEASE_ID})

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == VALID_RELEASE_ID


@pytest.mark.parametrize("configured", ["abc123", "  abc123  "])
def test_deployment_state_environment_wins_over_manifest(
    monkeypatch, tmp_path, configured
):
    monkeypatch.setenv("REPORT_AGENT_RELEASE_ID", configured)
    manifest = write_manifest(tmp_path, {"commit_sha": VALID_RELEASE_ID})

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == "abc123"


def test_deployment_state_blank_environment_uses_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("REPORT_AGENT_RELEASE_ID", "   ")
    manifest = write_manifest(tmp_path, {"commit_sha": VALID_RELEASE_ID})

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == VALID_RELEASE_ID


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"commit_sha": None},
        {"commit_sha": 123},
        {"commit_sha": "A" * 40},
        {"commit_sha": "a" * 39},
        {"commit_sha": "g" * 40},
    ],
)
def test_deployment_state_rejects_invalid_manifest_values(
    monkeypatch, tmp_path, payload
):
    monkeypatch.delenv("REPORT_AGENT_RELEASE_ID", raising=False)
    manifest = write_manifest(tmp_path, payload)

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == "development"


def test_deployment_state_rejects_malformed_manifest(monkeypatch, tmp_path):
    monkeypatch.delenv("REPORT_AGENT_RELEASE_ID", raising=False)
    manifest = tmp_path / "release.json"
    manifest.write_text("{", encoding="utf-8")

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == "development"


@pytest.mark.parametrize("manifest_name", ["missing.json", "manifest-directory"])
def test_deployment_state_rejects_unreadable_manifest(
    monkeypatch, tmp_path, manifest_name
):
    monkeypatch.delenv("REPORT_AGENT_RELEASE_ID", raising=False)
    manifest = tmp_path / manifest_name
    if manifest_name == "manifest-directory":
        manifest.mkdir()

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == "development"
