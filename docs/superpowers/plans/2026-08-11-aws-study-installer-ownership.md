# AWS Study Installer Ownership Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the root-owned AWS study installer checksum packages as root, repair retained study ownership without following symlinks, and run package validation/activation as `epi-agent-web` so the service can read its registry and use Chroma.

**Architecture:** Keep the existing root operator boundary for fixed-input validation, S3 download, and outer SHA-256 verification. After the checksum passes, hand the retained study tree and private staging directory to `epi-agent-web`, then invoke both Python operations through one fixed `runuser` plus `env -i` command array. A dedicated real-shell harness substitutes only privileged host commands and verifies success, checksum failure, and package-install failure ordering.

**Tech Stack:** Bash, GNU `chown`, `runuser`, Python 3.12, pytest, AWS immutable-release assets

## Global Constraints

- The installed study is exactly `report-india-synthetic@0.2.0` with SHA-256 `51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e`.
- Keep S3 download and outer SHA-256 verification in the root-owned installer and before package parsing.
- Use `chown -R -h epi-agent-web:epi-agent-web` only for `/srv/epi-agent/study_data` and the current private study staging directory.
- Execute package validation/activation and installed-manifest verification through `/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i`.
- Pass only fixed `PATH`, `LANG`, `LC_ALL`, `PYTHONUTF8`, and `REPORT_AGENT_STUDY_ROOT` values into the unprivileged Python process.
- Do not carry AWS credentials, provider API keys, caller-controlled `PATH`, or arbitrary shell text into the Python installer.
- Do not delete or replace the retained `0.2.0` package, registry, prior versions, conversations, checkpoints, user directories, artifacts, releases, or other EBS data.
- Do not change CloudFormation, EC2 user data, EBS configuration, systemd, SQLite schema, the application service identity, study manifests, or the uploaded study object.
- Keep EC2 instance `i-0f9ed9c133ea2358b` stopped throughout implementation and review.
- Do not retry failed SSM command ID `6c1f1445-8e9b-470e-9fbc-72d2cf60a80b`.

---

## File Structure

- `deploy/aws/bin/install-study.sh`: retains the root download/checksum boundary, repairs the fixed study paths, and runs both Python operations as `epi-agent-web` in a minimal environment.
- `tests/test_aws_host_assets.py`: locks the static privilege-drop, ownership scope, ordering, and fixed-environment contract.
- `tests/test_aws_study_installer.py`: executes the real shell installer with privileged operations substituted and verifies success and failure behavior.
- `scripts/smoke_aws_study_installer.py`: provides a focused executable smoke gate for the three real-shell installer regressions.

### Task 1: Repair the AWS study installer privilege and ownership boundary

**Files:**
- Create: `tests/test_aws_study_installer.py`
- Create: `scripts/smoke_aws_study_installer.py`
- Modify: `tests/test_aws_host_assets.py`
- Modify: `deploy/aws/bin/install-study.sh`

**Interfaces:**
- Consumes: `install-study.sh <bucket> <study-key> <sha256> <study-id> <version>` and the fixed host identities and paths from the approved design.
- Produces: `_study_installer_harness(tmp_path, *, checksum_fails=False, installer_fails=False)` returning `(completed, study_root, operation_log, environment_log, staging_parent)`, plus an installer whose two Python invocations share the fixed `study_python` command array.

- [ ] **Step 1: Add the failing static privilege-boundary assertions**

Extend `test_study_installer_verifies_archive_and_preserves_prior_versions` in `tests/test_aws_host_assets.py` with these assertions:

```python
    ownership_repair = (
        'chown -R -h epi-agent-web:epi-agent-web "$study_root" "$staging_dir"'
    )
    assert ownership_repair in source
    assert "readonly -a study_python=(" in source
    assert "/usr/sbin/runuser" in source
    assert "--user epi-agent-web" in source
    assert "/usr/bin/env" in source
    assert "-i" in source
    assert "PATH=/opt/epi-agent/current/.venv/bin:/usr/bin" in source
    assert "LANG=C.UTF-8" in source
    assert "LC_ALL=C.UTF-8" in source
    assert "PYTHONUTF8=1" in source
    assert "REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data" in source
    assert source.count('"${study_python[@]}"') == 2
    assert source.index("sha256sum -c") < source.index(ownership_repair)
    assert source.index(ownership_repair) < source.index('"${study_python[@]}"')
    assert "OPENAI_API_KEY" not in source
    assert "AWS_ACCESS_KEY_ID" not in source
    assert "eval " not in source
    assert "bash -c" not in source
```

- [ ] **Step 2: Create the real-shell study-installer harness and behavior tests**

Create `tests/test_aws_study_installer.py` with the following complete content:

```python
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def _asset(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _study_installer_harness(
    tmp_path: Path,
    *,
    checksum_fails: bool = False,
    installer_fails: bool = False,
) -> tuple[
    subprocess.CompletedProcess[str],
    Path,
    Path,
    Path,
    Path,
]:
    host_root = tmp_path / "host" / "srv" / "epi-agent"
    study_root = host_root / "study_data"
    package_root = (
        study_root
        / "studies"
        / "packages"
        / "report-india-synthetic"
        / "0.2.0"
    )
    chroma_root = package_root / "database" / "index"
    chroma_root.mkdir(parents=True)
    registry = study_root / "studies" / "registry.json"
    registry.write_text(
        json.dumps(
            {"format_version": 1, "active": {"report-india-synthetic": "0.2.0"}}
        )
        + "\n",
        encoding="utf-8",
    )
    registry.chmod(0o000)
    sentinel = chroma_root / "sentinel.bin"
    sentinel.write_bytes(b"preserve retained study data")
    sentinel.chmod(0o400)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    archive = tmp_path / "study.tar.gz"
    archive.write_bytes(b"verified-study-archive")
    operation_log = tmp_path / "operations.log"
    environment_log = tmp_path / "installer-environment.log"
    dummy_installer = tmp_path / "study_installer.py"
    dummy_installer.write_text("# replaced by the harness\n", encoding="utf-8")

    fake_python = f"""#!/usr/bin/env bash
set -eu
/usr/bin/env > {environment_log}
if [ "${{1:-}}" = "{dummy_installer}" ]; then
  if [ "{1 if installer_fails else 0}" = 1 ]; then
    exit 17
  fi
  test -r "{registry}"
  test -w "{registry}"
  test -r "{sentinel}"
  test -w "{sentinel}"
  printf '%s\n' '{{"format_version":1,"active":{{"report-india-synthetic":"0.2.0"}}}}' > "{registry}"
  mkdir -p "{package_root}"
  printf '%s\n' '{{"format_version":2,"study_id":"report-india-synthetic","label":"RePORT India Synthetic","package_version":"0.2.0","database":{{"source_id":"report-india-synthetic","duckdb":"database/study.duckdb","catalog":"database/schema_catalog.json","index":"database/index","embedding_model":"OpenAI/text-embedding-3-large"}}}}' > "{package_root / 'study-package.json'}"
  printf '%s\n' 'Installed: report-india-synthetic@0.2.0'
  exit 0
fi
exec "{Path(os.sys.executable)}" "$@"
"""

    for name, body in {
        "id": "printf '%s\\n' 0\n",
        "aws": 'printf \'aws %s\\n\' "$*" >> "$TEST_OPERATION_LOG"\ncp "$TEST_ARCHIVE" "$4"\n',
        "sha256sum": """printf 'sha256sum %s\n' "$*" >> "$TEST_OPERATION_LOG"
[ "$TEST_CHECKSUM_FAILS" = 0 ]
""",
        "mktemp": """[ "$1" = -d ] && [ "$#" = 2 ]
staging_dir="${2%XXXXXX}fixture"
mkdir "$staging_dir"
printf '%s\n' "$staging_dir"
""",
        "install": """printf 'install %s\n' "$*" >> "$TEST_OPERATION_LOG"
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-g)
      [ "$#" -ge 2 ] || exit 64
      shift 2
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done
/usr/bin/install "${args[@]}"
""",
        "chown": """printf 'chown %s\n' "$*" >> "$TEST_OPERATION_LOG"
[ "$1" = -R ] && [ "$2" = -h ] && [ "$3" = epi-agent-web:epi-agent-web ]
shift 3
for target in "$@"; do
  chmod -R u+rwX "$target"
done
""",
        "runuser": """printf 'runuser %s\n' "$*" >> "$TEST_OPERATION_LOG"
[ "$1" = --user ] && [ "$2" = epi-agent-web ] && [ "$3" = -- ]
shift 3
exec "$@"
""",
    }.items():
        _write_executable(fake_bin / name, "#!/usr/bin/env bash\nset -eu\n" + body)
    _write_executable(fake_bin / "study-python", fake_python)

    source = _asset("deploy/aws/bin/install-study.sh")
    staging_parent = host_root / "study-staging"
    source = source.replace("/srv/epi-agent/study_data", str(study_root))
    source = source.replace("/srv/epi-agent/study-staging", str(staging_parent))
    source = source.replace(
        "/opt/epi-agent/current/.venv/bin/python", str(fake_bin / "study-python")
    )
    source = source.replace(
        "/opt/epi-agent/current/study_installer.py", str(dummy_installer)
    )
    source = source.replace(
        "/opt/epi-agent/current/.venv/bin:/usr/bin", f"{fake_bin}:/usr/bin"
    )
    source = source.replace("/usr/sbin/runuser", str(fake_bin / "runuser"))
    source = source.replace(
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"PATH={fake_bin}:/usr/bin:/bin",
    )
    harness = tmp_path / "install-study.sh"
    _write_executable(harness, source)

    environment = os.environ | {
        "TEST_ARCHIVE": str(archive),
        "TEST_CHECKSUM_FAILS": "1" if checksum_fails else "0",
        "TEST_OPERATION_LOG": str(operation_log),
    }
    completed = subprocess.run(
        [
            "bash",
            str(harness),
            "example-bucket",
            "studies/report-india-synthetic-0.2.0.tar.gz",
            "0" * 64,
            "report-india-synthetic",
            "0.2.0",
        ],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    return completed, study_root, operation_log, environment_log, staging_parent


def test_study_installer_repairs_retained_data_and_drops_privileges(
    tmp_path: Path,
) -> None:
    completed, study_root, operation_log, environment_log, staging_parent = (
        _study_installer_harness(tmp_path)
    )

    assert completed.returncode == 0, completed.stderr
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    checksum_index = next(
        index for index, value in enumerate(operations) if value.startswith("sha256sum ")
    )
    chown_index = next(
        index for index, value in enumerate(operations) if value.startswith("chown ")
    )
    runuser_indexes = [
        index for index, value in enumerate(operations) if value.startswith("runuser ")
    ]
    staging_dir = staging_parent.parent / "study-staging.fixture"
    assert checksum_index < chown_index < runuser_indexes[0]
    assert len(runuser_indexes) == 2
    assert operations[chown_index] == (
        "chown -R -h epi-agent-web:epi-agent-web "
        f"{study_root} {staging_dir}"
    )
    assert all(
        "--user epi-agent-web -- /usr/bin/env -i" in operations[index]
        for index in runuser_indexes
    )
    registry = study_root / "studies" / "registry.json"
    sentinel = (
        study_root
        / "studies"
        / "packages"
        / "report-india-synthetic"
        / "0.2.0"
        / "database"
        / "index"
        / "sentinel.bin"
    )
    assert json.loads(registry.read_text(encoding="utf-8"))["active"] == {
        "report-india-synthetic": "0.2.0"
    }
    assert sentinel.read_bytes() == b"preserve retained study data"
    assert registry.stat().st_mode & 0o600 == 0o600
    assert sentinel.stat().st_mode & 0o600 == 0o600
    environment = dict(
        line.split("=", 1)
        for line in environment_log.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert environment["PATH"].endswith("/bin:/usr/bin")
    assert environment["LANG"] == "C.UTF-8"
    assert environment["LC_ALL"] == "C.UTF-8"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["REPORT_AGENT_STUDY_ROOT"] == str(study_root)
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert not list(staging_parent.parent.glob("study-staging.*"))


def test_study_installer_checksum_failure_precedes_ownership_and_privilege_drop(
    tmp_path: Path,
) -> None:
    completed, study_root, operation_log, environment_log, staging_parent = (
        _study_installer_harness(tmp_path, checksum_fails=True)
    )

    assert completed.returncode != 0
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    assert any(value.startswith("sha256sum ") for value in operations)
    assert not any(value.startswith("chown ") for value in operations)
    assert not any(value.startswith("runuser ") for value in operations)
    assert not environment_log.exists()
    registry = study_root / "studies" / "registry.json"
    assert registry.stat().st_mode & 0o777 == 0
    assert not list(staging_parent.parent.glob("study-staging.*"))


def test_study_installer_failure_preserves_retained_data_and_cleans_staging(
    tmp_path: Path,
) -> None:
    completed, study_root, operation_log, _, staging_parent = _study_installer_harness(
        tmp_path,
        installer_fails=True,
    )

    assert completed.returncode == 17
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    assert sum(value.startswith("runuser ") for value in operations) == 1
    registry = study_root / "studies" / "registry.json"
    assert json.loads(registry.read_text(encoding="utf-8"))["active"] == {
        "report-india-synthetic": "0.2.0"
    }
    sentinel = (
        study_root
        / "studies"
        / "packages"
        / "report-india-synthetic"
        / "0.2.0"
        / "database"
        / "index"
        / "sentinel.bin"
    )
    assert sentinel.read_bytes() == b"preserve retained study data"
    assert not list(staging_parent.parent.glob("study-staging.*"))
```

- [ ] **Step 3: Run the focused tests and confirm RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_host_assets.py::test_study_installer_verifies_archive_and_preserves_prior_versions \
  tests/test_aws_study_installer.py -q
```

Expected: the static and success behavior tests fail because the current installer has no ownership repair, fixed unprivileged command array, or `runuser` invocations. The checksum-failure test may already pass because the current script checks SHA-256 before Python execution; record all outcomes.

- [ ] **Step 4: Implement the minimal privilege-drop correction**

Add this fixed command array after `readonly study_root` in `deploy/aws/bin/install-study.sh`:

```bash
readonly -a study_python=(
  /usr/sbin/runuser
  --user epi-agent-web
  --
  /usr/bin/env
  -i
  PATH=/opt/epi-agent/current/.venv/bin:/usr/bin
  LANG=C.UTF-8
  LC_ALL=C.UTF-8
  PYTHONUTF8=1
  REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data
  /opt/epi-agent/current/.venv/bin/python
)
```

Replace the current root Python invocation after `sha256sum -c` with:

```bash
chown -R -h epi-agent-web:epi-agent-web "$study_root" "$staging_dir"
"${study_python[@]}" /opt/epi-agent/current/study_installer.py \
  "--study" "$archive_path"
```

Replace the final root Python manifest checker prefix with:

```bash
"${study_python[@]}" - "$study_root" "$study_id" "$package_version" <<'PY'
```

Do not add deletion, package replacement, a shell evaluator, extra environment variables, CloudFormation changes, or service restart logic to the installer.

- [ ] **Step 5: Run the focused tests and confirm GREEN**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_host_assets.py::test_study_installer_verifies_archive_and_preserves_prior_versions \
  tests/test_aws_study_installer.py -q
```

Expected: `4 passed` with no warnings or errors.

- [ ] **Step 6: Add the focused executable smoke gate**

Create `scripts/smoke_aws_study_installer.py` with:

```python
#!/usr/bin/env python3
"""Execute AWS study-installer regressions through the real shell harness."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tests = [
        "tests/test_aws_study_installer.py::test_study_installer_repairs_retained_data_and_drops_privileges",
        "tests/test_aws_study_installer.py::test_study_installer_checksum_failure_precedes_ownership_and_privilege_drop",
        "tests/test_aws_study_installer.py::test_study_installer_failure_preserves_retained_data_and_cleans_staging",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        return completed.returncode
    print("AWS study installer smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Run focused and complete local verification**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_study_installer.py tests/test_aws_host_assets.py tests/test_no_study_startup.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_study_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_host_assets.py tests/test_aws_study_installer.py \
  tests/test_aws_infrastructure.py tests/test_aws_phase2a_cli.py \
  tests/test_build_aws_release.py tests/test_smoke_aws_phase2a_real.py \
  tests/test_smoke_multi_user_isolation_real.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_release_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_study_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
bash -n deploy/aws/bin/install-release.sh deploy/aws/bin/install-study.sh deploy/aws/bin/epi-agent-python-worker
git diff --check
```

Expected: every pytest and smoke command exits `0`, shell syntax is clean, and `git diff --check` reports no errors. EC2 remains stopped and no AWS command is run.

- [ ] **Step 8: Review the scoped diff and commit**

Run:

```bash
git diff -- deploy/aws/bin/install-study.sh tests/test_aws_host_assets.py \
  tests/test_aws_study_installer.py scripts/smoke_aws_study_installer.py
git add deploy/aws/bin/install-study.sh tests/test_aws_host_assets.py \
  tests/test_aws_study_installer.py scripts/smoke_aws_study_installer.py
git commit -m "fix: run AWS study installation as service user"
```

Expected: the diff contains only the planned installer, static contract,
real-shell harness, and focused smoke changes; the commit succeeds.

## Post-Implementation Deployment Gate

Do not build or deploy from this plan alone. Final review established that the
current application cannot pass the release installer's drain/health gates
until retained study ownership is repaired. Complete and review the dedicated
recovery work specified by
`docs/superpowers/specs/2026-08-11-aws-study-access-recovery-design.md`, then
follow its corrected live recovery order. Do not upload, start EC2, deploy,
reinstall, or restart before the resulting explicit authorization gate.
