# First-Release Startup Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the first AWS release remain selected while FastAPI starts, fail safely on a real startup failure, and automatically start the application after later EC2 reboots.

**Architecture:** Add a bounded condition-based health wait to the release installer after atomic activation and systemd restart. Extend the real shell harness to reproduce a delayed first startup and an early service exit, expose those regressions through an executable smoke script, then publish a new immutable release without changing CloudFormation infrastructure.

**Tech Stack:** Bash, systemd, curl, Python 3.12, pytest, AWS S3, AWS Systems Manager, EC2.

## Global Constraints

- Work only in the isolated `aws-test` worktree.
- Poll health every two seconds for at most 120 seconds; do not use a fixed startup sleep.
- Keep the maintenance marker and `current` symlink in place throughout startup polling.
- Fail early when systemd reports `epi-agent.service` is no longer active.
- Enable the service before restarting it; on failed first activation, disable and stop it again.
- Preserve existing archive validation, checksum verification, maintenance, update rollback, readiness, certificate, and Nginx behavior.
- Do not delete the inactive failed release directory or any EBS runtime data.
- Do not retry SSM commands `67a8ee05-4db7-431d-be98-443c54613253` or `84aa0bdd-aeaf-4c51-99f9-d1e13da343a1`.
- Do not change CloudFormation, EC2, EBS, VPC, IAM, Cognito, Route 53, database, or user-data configuration.
- Build, upload, and deploy only a clean, newly committed, checksum-verified release; send at most one new deployment command.

---

### Task 1: Make first-release startup condition-based and reboot-persistent

**Files:**
- Modify: `deploy/aws/bin/install-release.sh`
- Modify: `tests/test_aws_host_assets.py`
- Create: `scripts/smoke_aws_release_installer.py`

**Interfaces:**
- Consumes: `epi-agent.service`, `/opt/epi-agent/current`, `/run/epi-agent/maintenance`, and `GET http://127.0.0.1:8000/api/health`.
- Produces: `wait_for_service_health()` with a 120-second deadline; an enabled healthy service on success; the prior release or disabled pre-release state on failure.

- [ ] **Step 1: Extend the shell harness for delayed first startup**

Add keyword arguments to `_release_harness` in `tests/test_aws_host_assets.py`:

```python
def _release_harness(
    tmp_path: Path,
    *,
    invalid_archive: bool,
    readiness_fails: bool,
    staging_fails: bool = False,
    health_failures: int = 0,
    service_inactive: bool = False,
    previous_release_exists: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, Path]:
```

Replace the fake `systemctl`, `curl`, and add a fake `sleep` entry with:

```python
"systemctl": """printf "%s\\n" "$*" >> "$TEST_SYSTEMCTL_LOG"
if [ "${1:-}" = is-active ] && [ "$TEST_SERVICE_INACTIVE" = 1 ]; then
  exit 3
fi
""",
"curl": """case "$*" in
  *deployment-status*) printf '%s\\n' '{"maintenance": true, "active_runs": 0}' ;;
  */api/health*)
    count=0
    if [ -f "$TEST_HEALTH_COUNT_FILE" ]; then
      read -r count < "$TEST_HEALTH_COUNT_FILE"
    fi
    count=$((count + 1))
    printf '%s\\n' "$count" > "$TEST_HEALTH_COUNT_FILE"
    [ "$count" -gt "$TEST_HEALTH_FAILURES" ] || exit 7
    ;;
  *readiness*) [ "$TEST_READINESS_FAILS" = 0 ] || exit 22 ;;
esac
""",
"sleep": ":\n",
```

Create the previous symlink conditionally:

```python
current_link = root / "opt" / "epi-agent" / "current"
current_link.parent.mkdir(parents=True)
if previous_release_exists:
    current_link.symlink_to("/previous/release")
```

Add these environment values:

```python
"TEST_HEALTH_FAILURES": str(health_failures),
"TEST_HEALTH_COUNT_FILE": str(tmp_path / "health-count"),
"TEST_SERVICE_INACTIVE": "1" if service_inactive else "0",
```

- [ ] **Step 2: Add the delayed-start and failed-first-start regressions**

Add these tests to `tests/test_aws_host_assets.py`:

```python
def test_release_installer_waits_for_delayed_first_service_startup(
    tmp_path: Path,
) -> None:
    completed, current_link, maintenance_file, staging_parent, systemctl_log = (
        _release_harness(
            tmp_path,
            invalid_archive=False,
            readiness_fails=False,
            health_failures=1,
            previous_release_exists=False,
        )
    )

    assert completed.returncode == 0, completed.stderr
    assert current_link.readlink() == staging_parent / "releases" / ("a" * 40)
    assert not maintenance_file.exists()
    assert int((tmp_path / "health-count").read_text(encoding="utf-8")) >= 2
    systemctl_calls = systemctl_log.read_text(encoding="utf-8").splitlines()
    assert "enable epi-agent.service" in systemctl_calls
    assert systemctl_calls.count("restart epi-agent.service") == 1


def test_release_installer_disables_service_after_failed_first_startup(
    tmp_path: Path,
) -> None:
    completed, current_link, maintenance_file, _, systemctl_log = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
        health_failures=1,
        service_inactive=True,
        previous_release_exists=False,
    )

    assert completed.returncode != 0
    assert not current_link.exists()
    assert not current_link.is_symlink()
    assert not maintenance_file.exists()
    systemctl_calls = systemctl_log.read_text(encoding="utf-8").splitlines()
    assert "is-active --quiet epi-agent.service" in systemctl_calls
    assert "disable --now epi-agent.service" in systemctl_calls
```

- [ ] **Step 3: Verify RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_host_assets.py::test_release_installer_waits_for_delayed_first_service_startup \
  tests/test_aws_host_assets.py::test_release_installer_disables_service_after_failed_first_startup -q
```

Expected: both tests fail against the current installer. The delayed-start test fails because the first health error immediately rolls back; the failed-start test fails because there is no `is-active` check and cleanup only stops rather than disables the first-release service.

- [ ] **Step 4: Implement the bounded startup gate**

In `deploy/aws/bin/install-release.sh`, add with the existing constants:

```bash
readonly startup_deadline_seconds=120
```

Add after `check_service()`:

```bash
wait_for_service_health() {
  local deadline
  deadline=$((SECONDS + startup_deadline_seconds))
  while ! curl --fail --silent --show-error --max-time 5 \
    http://127.0.0.1:8000/api/health >/dev/null; do
    if ! systemctl is-active --quiet epi-agent.service; then
      systemctl status epi-agent.service --no-pager >&2 || true
      fail 'application service exited before becoming healthy'
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      fail 'timed out waiting for application service health'
    fi
    sleep 2
  done
}
```

Change first-release rollback in `restore_previous_release()` from:

```bash
systemctl stop epi-agent.service
```

to:

```bash
systemctl disable --now epi-agent.service
```

Replace the immediate post-activation section with:

```bash
activated=true
systemctl enable epi-agent.service
systemctl restart epi-agent.service
wait_for_service_health
rm -f -- "$maintenance_file"
check_service
activated=false
```

- [ ] **Step 5: Verify GREEN and existing rollback behavior**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_host_assets.py -q
bash -n deploy/aws/bin/install-release.sh
```

Expected: every host-asset test passes and `bash -n` exits 0.

- [ ] **Step 6: Add the executable regression smoke**

Create `scripts/smoke_aws_release_installer.py`:

```python
#!/usr/bin/env python3
"""Execute the first-release startup regression through the real shell harness."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tests = [
        "tests/test_aws_host_assets.py::test_release_installer_waits_for_delayed_first_service_startup",
        "tests/test_aws_host_assets.py::test_release_installer_disables_service_after_failed_first_startup",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        return completed.returncode
    print("AWS first-release startup smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_release_installer.py
```

Expected: two tests pass followed by `AWS first-release startup smoke passed`.

- [ ] **Step 7: Run the full AWS regression gate**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_infrastructure.py tests/test_aws_host_assets.py tests/test_aws_phase2a_cli.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py validate
git diff --check
```

Expected: all tests and both smokes pass, CloudFormation validation exits 0, and `git diff --check` exits 0.

- [ ] **Step 8: Commit**

```bash
git add deploy/aws/bin/install-release.sh tests/test_aws_host_assets.py scripts/smoke_aws_release_installer.py
git commit -m "fix: wait for first release startup"
```

---

### Task 2: Publish and verify the new release once

**Files:**
- Use: `scripts/build_aws_release.py`
- Use: `scripts/aws_phase2a.py`

**Interfaces:**
- Consumes: the clean Task 1 commit, existing private application bucket, default SSM document v6, and EC2 instance `i-0f9ed9c133ea2358b`.
- Produces: one active healthy release at the new commit SHA and a verified public `https://epiagent.org` endpoint.

- [ ] **Step 1: Recheck immutable build guards**

Require `git status --short` to be empty. Record the exact 40-character `git rev-parse HEAD`. Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws
```

Read the emitted JSON manifest and require `commit_sha` to equal HEAD. Verify the archive against its `.sha256` sidecar and require the existing `.env`, `runtime/`, `study_data/`, and `report-india-*.tar.gz` exclusion check to pass.

- [ ] **Step 2: Upload and verify the exact immutable object**

Run `scripts/aws_phase2a.py upload-release` with the new archive. Construct the
key by placing the manifest's exact 40-character `commit_sha` between
`releases/` and `.tar.gz`. Require `head-object` to report:

```text
ServerSideEncryption: AES256
Metadata.sha256: exactly equal to the manifest archive_sha256 value
VersionId: non-empty
```

- [ ] **Step 3: Send one deployment command**

Immediately before deployment require:

```text
instance i-0f9ed9c133ea2358b: SSM Online
epi-agent-deploy-release: DefaultVersion=6 and LatestVersion=6
```

Run `scripts/aws_phase2a.py deploy-release` once with the exact release key, archive SHA-256, commit SHA, `epiagent.org`, and `xw488@njms.rutgers.edu`. On failure, retrieve that invocation's output and stop without retry.

- [ ] **Step 4: Verify the live application and unchanged infrastructure**

Require all of:

```text
latest new deployment command: Success
/opt/epi-agent/current: symlink target equals /opt/epi-agent/releases/ followed by the manifest commit_sha
epi-agent.service: enabled and active
local /api/health: HTTP 200
local /api/readiness: HTTP 200 with status=ready
https://epiagent.org/api/health: HTTP 200
https://epiagent.org/api/readiness: HTTP 200 with status=ready
TLS certificate: valid for epiagent.org and not expired
instance: i-0f9ed9c133ea2358b, t3.large, ami-07a5b367e8dc8bd92
root volume: vol-0cb3e1bb3af5e4245
data volume: vol-043235ec767877900
deployment/service alarms: not ALARM
```

Record all command IDs, release identifiers, object version, and verification evidence in `.superpowers/sdd/startup-task-2-report.md`.
