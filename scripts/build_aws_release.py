#!/usr/bin/env python3
"""Build a deterministic, tracked-source archive for an AWS release."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile


BUILD_FORMAT_VERSION = 1
PYTHON_VERSION = "3.12"
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
EXCLUDED_TOP_LEVEL = {".git", "runtime", "study_data"}
FRONTEND_MANIFEST_PATH = PurePosixPath("frontend/dist/build-manifest.json")


class ReleaseBuildError(RuntimeError):
    """A release cannot be built safely."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ReleaseManifest:
    commit_sha: str
    archive_sha256: str
    created_at: str
    python_version: str
    frontend_manifest_sha256: str


def git_output(project_root: Path, *args: str) -> str:
    """Return Git's exact stdout, except its conventional trailing newline."""
    completed = subprocess.run(
        ["git", *args],
        cwd=project_root,
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseBuildError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.decode("utf-8", errors="surrogateescape").rstrip("\n")


def require_clean_tracked_tree(project_root: Path) -> None:
    """Reject both staged and unstaged changes to files known to Git."""
    for args in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
        completed = subprocess.run(["git", *args], cwd=project_root, check=False)
        if completed.returncode == 1:
            raise ReleaseBuildError("tracked files must be clean before building a release")
        if completed.returncode:
            raise ReleaseBuildError(f"git {' '.join(args)} failed")


def safe_archive_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseBuildError(f"unsafe tracked path: {value!r}")
    return path


def include_path(path: PurePosixPath) -> bool:
    if path.parts[0] in EXCLUDED_TOP_LEVEL:
        return False
    if path.name == ".env" or path.name.startswith(".env."):
        return False
    return not (path.name.startswith("report-india-") and path.name.endswith(".tar.gz"))


def fixed_tar_info(name: str, size: int, executable: bool) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = 0o755 if executable else 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def frontend_manifest_sha256(project_root: Path, release_files: list[PurePosixPath]) -> str:
    if FRONTEND_MANIFEST_PATH not in release_files:
        raise ReleaseBuildError("frontend/dist/build-manifest.json must be a tracked regular file")
    manifest_path = project_root / FRONTEND_MANIFEST_PATH
    if not manifest_path.is_file():
        raise ReleaseBuildError("frontend/dist/build-manifest.json must be a tracked regular file")
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def commit_created_at(project_root: Path) -> str:
    raw_timestamp = git_output(project_root, "show", "-s", "--format=%cI", "HEAD")
    try:
        parsed = datetime.fromisoformat(raw_timestamp)
    except ValueError as error:
        raise ReleaseBuildError(f"invalid Git commit timestamp: {raw_timestamp!r}") from error
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_atomically(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def build_release(project_root: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    """Build the archive, checksum sidecar, and JSON manifest for ``HEAD``."""
    project_root = project_root.resolve()
    output_dir = output_dir.resolve()
    commit_sha = git_output(project_root, "rev-parse", "HEAD")
    if not COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise ReleaseBuildError(f"Git returned an invalid commit identity: {commit_sha!r}")
    require_clean_tracked_tree(project_root)

    tracked = git_output(project_root, "ls-files", "-z").split("\0")
    tracked_paths = sorted(
        (safe_archive_path(value) for value in tracked if value), key=lambda path: path.as_posix()
    )
    release_files = [path for path in tracked_paths if include_path(path)]
    manifest_hash = frontend_manifest_sha256(project_root, release_files)
    embedded_manifest = {
        "build_format_version": BUILD_FORMAT_VERSION,
        "commit_sha": commit_sha,
        "frontend_manifest_sha256": manifest_hash,
        "python_version": PYTHON_VERSION,
    }
    embedded_bytes = (json.dumps(embedded_manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"epi-agent-{commit_sha}.tar.gz"
    checksum_path = output_dir / f"epi-agent-{commit_sha}.sha256"
    manifest_path = output_dir / f"epi-agent-{commit_sha}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{archive_path.name}.", dir=output_dir)
    try:
        with os.fdopen(descriptor, "wb") as raw_archive:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_archive, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    for relative_path in release_files:
                        source_path = project_root / relative_path
                        try:
                            source_stat = source_path.lstat()
                        except FileNotFoundError as error:
                            raise ReleaseBuildError(f"tracked file disappeared: {relative_path}") from error
                        if not stat.S_ISREG(source_stat.st_mode):
                            raise ReleaseBuildError(f"tracked path is not a regular file: {relative_path}")
                        with source_path.open("rb") as source_file:
                            archive.addfile(
                                fixed_tar_info(
                                    relative_path.as_posix(),
                                    source_stat.st_size,
                                    bool(source_stat.st_mode & stat.S_IXUSR),
                                ),
                                source_file,
                            )
                    archive.addfile(
                        fixed_tar_info("release.json", len(embedded_bytes), False),
                        fileobj=io.BytesIO(embedded_bytes),
                    )
            raw_archive.flush()
            os.fsync(raw_archive.fileno())
        os.replace(temporary_name, archive_path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    release_manifest = ReleaseManifest(
        commit_sha=commit_sha,
        archive_sha256=archive_sha256,
        created_at=commit_created_at(project_root),
        python_version=PYTHON_VERSION,
        frontend_manifest_sha256=manifest_hash,
    )
    write_atomically(checksum_path, f"{archive_sha256}  {archive_path.name}\n".encode())
    write_atomically(
        manifest_path,
        (json.dumps(asdict(release_manifest), sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return archive_path, checksum_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist/aws"))
    arguments = parser.parse_args()
    try:
        archive_path, checksum_path, manifest_path = build_release(Path.cwd(), arguments.output_dir)
    except ReleaseBuildError as error:
        print(f"error: {error}")
        return error.exit_code
    print(archive_path)
    print(checksum_path)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
