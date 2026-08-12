# AWS Release Frontend Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject stale compiled frontend assets during AWS release construction, refresh the tracked production frontend, and produce a locally verified immutable release from a new clean commit.

**Architecture:** Extend the existing deterministic release builder with one focused frontend manifest contract: its source-path set, SHA-256 values, and Vite version must match current tracked inputs before any archive is written. Use that same builder module to regenerate the manifest immediately after the existing Vite build, commit source validation and generated assets together, then build twice and audit the final ignored archive without touching AWS.

**Tech Stack:** Python 3.12, pytest, Git, SHA-256, JSON, tar/gzip, Node/npm, TypeScript, Vite 7.3.5, Vitest.

## Global Constraints

- Work only in `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.worktrees/aws-execution` on branch `aws-test`.
- Begin from committed design `f01cec6c39d57c335a33ce71f16b739b53fd2acd`; preserve unrelated user changes.
- Use RED-GREEN TDD for release-builder behavior. Do not write production validation before the failing tests exist and fail for the expected reason.
- The authoritative frontend input set is every tracked regular file under `frontend/src/` plus exactly `frontend/index.html`, `frontend/package-lock.json`, `frontend/package.json`, `frontend/tsconfig.json`, `frontend/tsconfig.node.json`, and `frontend/vite.config.ts`.
- `frontend/dist/build-manifest.json` must have exactly that `source_sha256` key set, matching current SHA-256 values, and `vite_version` equal to `packages["node_modules/vite"].version` in `frontend/package-lock.json`.
- `built_at` remains informational; it is regenerated but does not affect freshness validation.
- Continue excluding `.env`, `.env.*`, `runtime/`, `study_data/`, raw `report-india-*.tar.gz` files, untracked files, unsafe paths, and non-regular members from the release.
- Production remains native Python/EC2; do not add a Docker dependency.
- Do not upload to S3, start EC2, invoke recovery, deploy a release, install a study, or mutate AWS.
- The final archive release ID is the full 40-character correction commit created at the end of Task 1, not `6df62bcd6746b8971d94cf235762f5a60288cb48` or the design-only commit.
- Stop after reporting the local archive path, checksum, manifest, release ID, and proposed S3 key.

---

### Task 1: Enforce frontend freshness and commit regenerated production assets

**Files:**
- Modify: `scripts/build_aws_release.py`
- Modify: `tests/test_build_aws_release.py`
- Regenerate: `frontend/dist/index.html`
- Regenerate: `frontend/dist/assets/*`
- Regenerate: `frontend/dist/build-manifest.json`
- Evidence only: `.superpowers/sdd/aws-release-frontend-freshness-task-1-report.md`

**Interfaces:**
- Consumes: Git tracked paths, `frontend/package-lock.json`, tracked frontend source/build inputs, and `frontend/dist/build-manifest.json`.
- Produces: `frontend_source_paths(release_files) -> list[PurePosixPath]`, `vite_version(project_root) -> str`, `expected_frontend_source_sha256(project_root, release_files) -> dict[str, str]`, `validate_frontend_build_manifest(project_root, release_files) -> str`, `write_frontend_build_manifest(project_root) -> Path`, a fresh Vite production bundle, and one immutable correction commit.
- Preserves: `build_release(project_root, output_dir) -> tuple[Path, Path, Path]` and its deterministic archive/sidecar contract.

- [ ] **Step 1: Make the release fixture contain a valid generated frontend manifest**

In `tests/test_build_aws_release.py`, replace the minimal frontend fixture entries with tracked build inputs and one tracked source:

```python
    files = {
        "api/app.py": "print('tracked application')\n",
        "frontend/index.html": "<div id=\"root\"></div>\n",
        "frontend/package.json": '{"scripts":{"build":"vite build"}}\n',
        "frontend/package-lock.json": json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/vite": {"version": "7.3.5"}},
            }
        )
        + "\n",
        "frontend/tsconfig.json": "{}\n",
        "frontend/tsconfig.node.json": "{}\n",
        "frontend/vite.config.ts": "export default {};\n",
        "frontend/src/main.tsx": "console.log('tracked source');\n",
        "frontend/dist/app.js": "console.log('tracked frontend');\n",
        ".env": "SECRET=never-release\n",
        "runtime/state.db": "mutable runtime data\n",
        "study_data/private.csv": "private study data\n",
        "report-india-demo.tar.gz": "raw study archive\n",
    }
```

After writing the files but before the initial commit, stage the source inputs, call the wished-for manifest writer, stage the generated manifest, and commit:

```python
    git(project_root, "add", ".")
    builder = load_builder()
    manifest_path = builder.write_frontend_build_manifest(project_root)
    assert manifest_path == project_root / "frontend/dist/build-manifest.json"
    git(project_root, "add", "frontend/dist/build-manifest.json")
    git(project_root, "commit", "-qm", "initial release fixture")
```

Leave creation of `untracked.txt` after the commit.

- [ ] **Step 2: Add failing stale-manifest tests**

Append these behavior tests to `tests/test_build_aws_release.py`:

```python
def test_stale_frontend_source_is_rejected_before_archive_write(
    release_project: Path, tmp_path: Path
) -> None:
    builder = load_builder()
    (release_project / "frontend/src/main.tsx").write_text(
        "console.log('changed after build');\n"
    )
    git(release_project, "add", "frontend/src/main.tsx")
    git(release_project, "commit", "-qm", "change frontend without rebuilding")
    output_dir = tmp_path / "dist"

    with pytest.raises(builder.ReleaseBuildError, match="does not match current frontend inputs"):
        builder.build_release(release_project, output_dir)

    assert not output_dir.exists() or not list(output_dir.iterdir())


def test_new_tracked_frontend_source_missing_from_manifest_is_rejected(
    release_project: Path, tmp_path: Path
) -> None:
    builder = load_builder()
    new_source = release_project / "frontend/src/NewPanel.tsx"
    new_source.write_text("export const NewPanel = () => null;\n")
    git(release_project, "add", "frontend/src/NewPanel.tsx")
    git(release_project, "commit", "-qm", "add frontend source without rebuilding")

    with pytest.raises(builder.ReleaseBuildError, match="does not match current frontend inputs"):
        builder.build_release(release_project, tmp_path / "dist")


def test_wrong_manifest_vite_version_is_rejected(
    release_project: Path, tmp_path: Path
) -> None:
    builder = load_builder()
    manifest_path = release_project / "frontend/dist/build-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["vite_version"] = "0.0.0"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    git(release_project, "add", "frontend/dist/build-manifest.json")
    git(release_project, "commit", "-qm", "corrupt recorded vite version")

    with pytest.raises(builder.ReleaseBuildError, match="does not match current frontend inputs"):
        builder.build_release(release_project, tmp_path / "dist")


def test_manifest_writer_refreshes_hashes_for_current_tracked_inputs(
    release_project: Path,
) -> None:
    builder = load_builder()
    source_path = release_project / "frontend/src/main.tsx"
    source_path.write_text("console.log('new build input');\n")
    git(release_project, "add", "frontend/src/main.tsx")

    manifest_path = builder.write_frontend_build_manifest(release_project)
    manifest = json.loads(manifest_path.read_text())

    assert manifest["vite_version"] == "7.3.5"
    assert manifest["source_sha256"]["frontend/src/main.tsx"] == hashlib.sha256(
        source_path.read_bytes()
    ).hexdigest()
```

- [ ] **Step 3: Run the focused tests and confirm RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  -m pytest tests/test_build_aws_release.py -q
```

Expected: the fixture and new tests fail because `write_frontend_build_manifest` does not exist and `build_release` does not validate frontend freshness. Confirm the failures occur before implementing the new functions.

- [ ] **Step 4: Add the minimal manifest contract**

In `scripts/build_aws_release.py`, add these constants below `FRONTEND_MANIFEST_PATH`:

```python
FRONTEND_BUILD_INPUTS = frozenset(
    {
        PurePosixPath("frontend/index.html"),
        PurePosixPath("frontend/package-lock.json"),
        PurePosixPath("frontend/package.json"),
        PurePosixPath("frontend/tsconfig.json"),
        PurePosixPath("frontend/tsconfig.node.json"),
        PurePosixPath("frontend/vite.config.ts"),
    }
)
```

Add these functions before `frontend_manifest_sha256`:

```python
def tracked_release_files(project_root: Path) -> list[PurePosixPath]:
    tracked = git_output(project_root, "ls-files", "-z").split("\0")
    tracked_paths = sorted(
        (safe_archive_path(value) for value in tracked if value),
        key=lambda path: path.as_posix(),
    )
    return [path for path in tracked_paths if include_path(path)]


def frontend_source_paths(release_files: list[PurePosixPath]) -> list[PurePosixPath]:
    tracked = set(release_files)
    missing = sorted(FRONTEND_BUILD_INPUTS - tracked, key=lambda path: path.as_posix())
    if missing:
        names = ", ".join(path.as_posix() for path in missing)
        raise ReleaseBuildError(f"required frontend build inputs are not tracked: {names}")
    return sorted(
        (
            path
            for path in tracked
            if path in FRONTEND_BUILD_INPUTS
            or path.parts[:2] == ("frontend", "src")
        ),
        key=lambda path: path.as_posix(),
    )


def vite_version(project_root: Path) -> str:
    try:
        lock = json.loads(
            (project_root / "frontend/package-lock.json").read_text(encoding="utf-8")
        )
        version = lock["packages"]["node_modules/vite"]["version"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ReleaseBuildError("frontend package lock has no pinned Vite version") from error
    if not isinstance(version, str) or not version.strip():
        raise ReleaseBuildError("frontend package lock has no pinned Vite version")
    return version


def expected_frontend_source_sha256(
    project_root: Path,
    release_files: list[PurePosixPath],
) -> dict[str, str]:
    return {
        path.as_posix(): hashlib.sha256((project_root / path).read_bytes()).hexdigest()
        for path in frontend_source_paths(release_files)
    }


def validate_frontend_build_manifest(
    project_root: Path,
    release_files: list[PurePosixPath],
) -> str:
    manifest_path = project_root / FRONTEND_MANIFEST_PATH
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReleaseBuildError("frontend build manifest is invalid") from error
    expected_sources = expected_frontend_source_sha256(project_root, release_files)
    if (
        not isinstance(manifest, dict)
        or manifest.get("source_sha256") != expected_sources
        or manifest.get("vite_version") != vite_version(project_root)
    ):
        raise ReleaseBuildError(
            "frontend build manifest does not match current frontend inputs"
        )
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def write_frontend_build_manifest(project_root: Path) -> Path:
    project_root = project_root.resolve()
    release_files = tracked_release_files(project_root)
    manifest_path = project_root / FRONTEND_MANIFEST_PATH
    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": expected_frontend_source_sha256(
            project_root,
            release_files,
        ),
        "vite_version": vite_version(project_root),
    }
    write_atomically(
        manifest_path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
    )
    return manifest_path
```

Change `frontend_manifest_sha256` to delegate to the validator:

```python
def frontend_manifest_sha256(
    project_root: Path,
    release_files: list[PurePosixPath],
) -> str:
    if FRONTEND_MANIFEST_PATH not in release_files:
        raise ReleaseBuildError(
            "frontend/dist/build-manifest.json must be a tracked regular file"
        )
    manifest_path = project_root / FRONTEND_MANIFEST_PATH
    if not manifest_path.is_file():
        raise ReleaseBuildError(
            "frontend/dist/build-manifest.json must be a tracked regular file"
        )
    return validate_frontend_build_manifest(project_root, release_files)
```

In `build_release`, replace the inline `git ls-files`/filter block with:

```python
    release_files = tracked_release_files(project_root)
```

- [ ] **Step 5: Expose the manifest writer as a local CLI operation**

In `main`, add the flag and dispatch before `build_release`:

```python
    parser.add_argument(
        "--write-frontend-manifest",
        action="store_true",
        help="refresh frontend/dist/build-manifest.json and exit",
    )
```

```python
        if arguments.write_frontend_manifest:
            print(write_frontend_build_manifest(Path.cwd()))
            return 0
        archive_path, checksum_path, manifest_path = build_release(
            Path.cwd(), arguments.output_dir
        )
```

The writer intentionally works with staged or unstaged frontend changes so it can refresh generated metadata before the resulting files are committed. The default archive path still requires a clean tracked tree.

- [ ] **Step 6: Run focused GREEN and regression checks**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  -m pytest tests/test_build_aws_release.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  -m py_compile scripts/build_aws_release.py tests/test_build_aws_release.py
git diff --check
```

Expected: every release-builder test passes; compilation and diff checks exit `0` with no output.

- [ ] **Step 7: Verify the frontend toolchain and run tests**

Run:

```bash
test -x frontend/node_modules/.bin/vite
node --version
npm --version
npm --prefix frontend test
```

Expected: the local Vite executable exists and the complete Vitest suite passes. If `node_modules` is absent, stop and request separate authorization before any networked dependency installation.

- [ ] **Step 8: Regenerate the production bundle**

Run:

```bash
npm --prefix frontend run build
```

Expected: TypeScript and Vite exit `0`; `frontend/dist/index.html` and hashed assets are regenerated. Do not manually edit generated assets.

- [ ] **Step 9: Refresh and verify the tracked build manifest**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/build_aws_release.py --write-frontend-manifest
```

Then run this read-only verification:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -c '
import json
from pathlib import Path
import scripts.build_aws_release as builder
root = Path.cwd()
files = builder.tracked_release_files(root)
digest = builder.validate_frontend_build_manifest(root, files)
print(json.dumps({"frontend_manifest_sha256": digest}, sort_keys=True))
'
```

Expected: validation exits `0` and prints a 64-character manifest SHA-256.

- [ ] **Step 10: Review all correction files and run pre-commit verification**

Run:

```bash
git status --short
git diff --stat
git diff -- frontend/dist/index.html frontend/dist/build-manifest.json
npm --prefix frontend test
docker info
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  -m pytest tests/test_build_aws_release.py tests/test_aws_phase2a_cli.py \
  tests/test_aws_infrastructure.py tests/test_aws_study_access_recovery.py \
  tests/test_aws_study_installer.py tests/test_aws_host_assets.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_service_entrypoint.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_release_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_study_installer.py
git diff --check
```

Expected: the working tree contains changes only to the release builder, its tests, tracked hashed frontend assets, `index.html`, and the build manifest; Docker is available only as a test harness; all tests/smokes pass and diff check exits `0`. If the Docker daemon is unavailable, stop and ask the user to start Docker Desktop before claiming this gate passed. Production remains non-Docker.

- [ ] **Step 11: Commit the complete correction as one immutable release source**

Stage exactly the builder, tests, and generated frontend paths together:

```bash
git add -f scripts/build_aws_release.py tests/test_build_aws_release.py
git add -A frontend/dist
git diff --cached --stat
git commit -m "fix: require fresh frontend assets in AWS releases"
```

Require:

```bash
git status --short
git rev-parse HEAD
```

Expected: status produces no output; `HEAD` is a new 40-character SHA distinct from `6df62bcd6746b8971d94cf235762f5a60288cb48` and `f01cec6c39d57c335a33ce71f16b739b53fd2acd`.

Record RED/GREEN commands, frontend build output, verification output, changed files, and concerns in the ignored evidence report. Submit this immutable Task 1 commit for review before Task 2. This same commit will be the release ID; do not add another tracked commit during Task 2.

---

### Task 2: Build and audit the immutable local release

**Files:**
- Evidence only: `.superpowers/sdd/aws-release-frontend-freshness-task-2-report.md`
- Create ignored artifacts: `dist/aws/epi-agent-${release_id}.tar.gz`, `dist/aws/epi-agent-${release_id}.sha256`, and `dist/aws/epi-agent-${release_id}.json`

**Interfaces:**
- Consumes: the reviewed, clean Task 1 correction commit.
- Produces: deterministic archive and sidecars named with that full 40-character commit SHA, archive SHA-256, and proposed S3 key `releases/${release_id}.tar.gz`.

- [ ] **Step 1: Build twice and prove reproducibility**

From the clean, reviewed Task 1 correction commit, run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/build_aws_release.py --output-dir dist/aws/repro-one
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/build_aws_release.py --output-dir dist/aws/repro-two
```

Set a task-local shell variable without changing any system path variables:

```bash
release_id=$(git rev-parse HEAD)
cmp "dist/aws/repro-one/epi-agent-${release_id}.tar.gz" \
  "dist/aws/repro-two/epi-agent-${release_id}.tar.gz"
cmp "dist/aws/repro-one/epi-agent-${release_id}.sha256" \
  "dist/aws/repro-two/epi-agent-${release_id}.sha256"
cmp "dist/aws/repro-one/epi-agent-${release_id}.json" \
  "dist/aws/repro-two/epi-agent-${release_id}.json"
```

Expected: all three `cmp` commands exit `0` with no output.

- [ ] **Step 2: Build the final local artifact and verify its checksum**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/build_aws_release.py --output-dir dist/aws
```

From `dist/aws`, run:

```bash
release_id=$(git -C ../.. rev-parse HEAD)
shasum -a 256 -c "epi-agent-${release_id}.sha256"
```

Expected: `epi-agent-${release_id}.tar.gz: OK`.

- [ ] **Step 3: Audit the archive content and identities**

From the repository root, run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python - <<'PY'
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

root = Path.cwd()
release_id = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
archive_path = root / "dist/aws" / f"epi-agent-{release_id}.tar.gz"
sidecar_path = root / "dist/aws" / f"epi-agent-{release_id}.sha256"
manifest_path = root / "dist/aws" / f"epi-agent-{release_id}.json"
archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
manifest = json.loads(manifest_path.read_text())

assert manifest["commit_sha"] == release_id
assert manifest["archive_sha256"] == archive_sha
assert sidecar_path.read_text() == f"{archive_sha}  {archive_path.name}\n"

with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    names = {member.name for member in members}
    required = {
        "release.json",
        "api/app.py",
        "api/deployment.py",
        "deploy/aws/bin/install-release.sh",
        "deploy/aws/bin/install-study.sh",
        "deploy/aws/systemd/epi-agent.service",
        "frontend/dist/build-manifest.json",
        "frontend/dist/index.html",
    }
    assert required <= names
    assert all(member.isfile() for member in members)
    for name in names:
        path = PurePosixPath(name)
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert path.parts[0] not in {"runtime", "study_data", ".git"}
        assert path.name != ".env" and not path.name.startswith(".env.")
        assert not (
            path.name.startswith("report-india-")
            and path.name.endswith(".tar.gz")
        )
    embedded = json.load(archive.extractfile("release.json"))
    service = archive.extractfile(
        "deploy/aws/systemd/epi-agent.service"
    ).read().decode()
    deployment = archive.extractfile("api/deployment.py").read().decode()
    frontend_manifest_bytes = archive.extractfile(
        "frontend/dist/build-manifest.json"
    ).read()

assert embedded["commit_sha"] == release_id
assert embedded["python_version"] == "3.12"
assert embedded["frontend_manifest_sha256"] == hashlib.sha256(
    frontend_manifest_bytes
).hexdigest()
assert "api.app:app" in service
assert "/usr/local/libexec/epi-agent-python-worker" in deployment

print(
    json.dumps(
        {
            "archive": str(archive_path),
            "archive_sha256": archive_sha,
            "manifest": str(manifest_path),
            "proposed_s3_key": f"releases/{release_id}.tar.gz",
            "release_id": release_id,
        },
        sort_keys=True,
    )
)
PY
```

Expected: every assertion passes and one JSON object reports the exact local archive, checksum, manifest, release ID, and proposed S3 key.

- [ ] **Step 4: Run the completion gate and stop before AWS**

Run fresh:

```bash
npm --prefix frontend test
docker info
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  -m pytest tests/test_build_aws_release.py tests/test_aws_phase2a_cli.py \
  tests/test_aws_infrastructure.py tests/test_aws_study_access_recovery.py \
  tests/test_aws_study_installer.py tests/test_aws_host_assets.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_service_entrypoint.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_release_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_study_installer.py
git status --short
```

Expected: all tests and smokes pass; tracked status is clean. The ignored `dist/aws` artifacts may exist but must not appear in status.

Report the exact values emitted by Step 8. Do not run `upload-release`, `aws s3`, `start`, `recover-study-access`, `deploy-release`, or any other AWS-mutating command. Request separate explicit authorization naming the exact archive SHA-256 and proposed S3 key.
