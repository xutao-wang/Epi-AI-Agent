import os

from api.deployment import DeploymentState


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
