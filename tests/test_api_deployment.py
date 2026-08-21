from pathlib import Path

import api.deployment as deployment


def test_native_path_helpers_are_project_local(tmp_path: Path) -> None:
    root = tmp_path.resolve()

    assert deployment.native_static_dir(root) == root / "frontend" / "dist"
    assert deployment.native_runtime_root(root) == root / "runtime"
    assert deployment.native_study_root(root) == root / "study_data"
    assert deployment.native_checkpoint_db_path(root) == (
        root / "runtime" / "agent_memory_fastapi.db"
    )


def test_deployment_module_has_no_hosted_state_or_worker_launcher() -> None:
    assert not hasattr(deployment, "DeploymentState")
    assert not hasattr(deployment, "release_id_from_manifest")
    assert not hasattr(deployment, "python_worker_launcher")
    assert not hasattr(deployment, "required_secret_names")


def test_local_cors_default_allows_only_loopback_http() -> None:
    pattern = deployment.DEFAULT_CORS_ALLOW_ORIGIN_REGEX

    assert "127\\.0\\.0\\.1" in pattern
    assert "localhost" in pattern
    assert "https" not in pattern
