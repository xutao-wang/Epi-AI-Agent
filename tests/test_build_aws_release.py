"""Contract tests for immutable AWS application release archives."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = PROJECT_ROOT / "scripts" / "build_aws_release.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_aws_release", BUILDER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(project_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def release_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    project_root.mkdir()
    git(project_root, "init", "-q")
    git(project_root, "config", "user.email", "release-tests@example.test")
    git(project_root, "config", "user.name", "Release Tests")

    files = {
        "api/app.py": "print('tracked application')\n",
        "frontend/dist/build-manifest.json": '{"assets": ["app.js"]}\n',
        "frontend/dist/app.js": "console.log('tracked frontend');\n",
        ".env": "SECRET=never-release\n",
        "runtime/state.db": "mutable runtime data\n",
        "study_data/private.csv": "private study data\n",
        "report-india-demo.tar.gz": "raw study archive\n",
    }
    for relative_path, contents in files.items():
        path = project_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    git(project_root, "add", ".")
    git(project_root, "commit", "-qm", "initial release fixture")
    (project_root / "untracked.txt").write_text("not versioned\n")
    return project_root


def archive_names(archive_path: Path) -> list[str]:
    with tarfile.open(archive_path, "r:gz") as archive:
        return archive.getnames()


def test_build_archives_only_safe_tracked_application_files(release_project: Path, tmp_path: Path) -> None:
    builder = load_builder()

    archive_path, checksum_path, manifest_path = builder.build_release(
        release_project, tmp_path / "dist"
    )

    names = archive_names(archive_path)
    assert "api/app.py" in names
    assert "frontend/dist/build-manifest.json" in names
    assert "release.json" in names
    assert all(
        excluded not in names
        for excluded in (
            ".env",
            "runtime/state.db",
            "study_data/private.csv",
            "report-india-demo.tar.gz",
            "untracked.txt",
            ".git/config",
        )
    )
    assert checksum_path.read_text() == f"{hashlib.sha256(archive_path.read_bytes()).hexdigest()}  {archive_path.name}\n"

    release_manifest = json.loads(manifest_path.read_text())
    assert release_manifest["commit_sha"] == git(release_project, "rev-parse", "HEAD")
    assert release_manifest["archive_sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert release_manifest["python_version"] == "3.12"

    with tarfile.open(archive_path, "r:gz") as archive:
        embedded_manifest = json.load(archive.extractfile("release.json"))
    assert embedded_manifest == {
        "build_format_version": 1,
        "commit_sha": git(release_project, "rev-parse", "HEAD"),
        "frontend_manifest_sha256": hashlib.sha256(
            (release_project / "frontend/dist/build-manifest.json").read_bytes()
        ).hexdigest(),
        "python_version": "3.12",
    }


def test_dirty_tracked_files_are_rejected_with_exit_code_two(
    release_project: Path, tmp_path: Path
) -> None:
    builder = load_builder()
    (release_project / "api/app.py").write_text("changed but not committed\n")

    with pytest.raises(builder.ReleaseBuildError) as error:
        builder.build_release(release_project, tmp_path / "dist")

    assert error.value.exit_code == 2


def test_untracked_frontend_manifest_is_rejected(release_project: Path, tmp_path: Path) -> None:
    builder = load_builder()
    manifest_path = release_project / "frontend/dist/build-manifest.json"
    git(release_project, "rm", "-q", manifest_path.relative_to(release_project).as_posix())
    git(release_project, "commit", "-qm", "remove tracked frontend manifest")
    manifest_path.write_text('{"assets": ["untracked.js"]}\n')

    with pytest.raises(builder.ReleaseBuildError, match="tracked regular file"):
        builder.build_release(release_project, tmp_path / "dist")


def test_archive_members_are_root_relative_and_safe(release_project: Path, tmp_path: Path) -> None:
    builder = load_builder()
    archive_path, _, _ = builder.build_release(release_project, tmp_path / "dist")

    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            assert not path.is_absolute()
            assert ".." not in path.parts
            assert member.isfile()


def test_release_commit_identity_is_exactly_forty_lowercase_hex_characters(
    release_project: Path, tmp_path: Path
) -> None:
    builder = load_builder()
    _, _, manifest_path = builder.build_release(release_project, tmp_path / "dist")

    commit_sha = json.loads(manifest_path.read_text())["commit_sha"]
    assert len(commit_sha) == 40
    assert all(character in "0123456789abcdef" for character in commit_sha)


def test_same_commit_produces_byte_identical_archive_and_sidecars(
    release_project: Path, tmp_path: Path
) -> None:
    builder = load_builder()

    first = builder.build_release(release_project, tmp_path / "one")
    second = builder.build_release(release_project, tmp_path / "two")

    assert [path.read_bytes() for path in first] == [path.read_bytes() for path in second]
