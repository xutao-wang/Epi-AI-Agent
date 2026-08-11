# AWS Runtime Directory Ownership Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every immutable release deployment safely reassert that `/srv/epi-agent/runtime` is writable by the unprivileged `epi-agent-web` service before the release is activated.

**Architecture:** Keep the correction in the existing root-owned release installer so the current retained EBS directory and future instances self-heal through the same reviewed deployment path. Exercise the real installer with a non-root shell harness whose fake `install` command records the privileged arguments, removes only owner/group flags, and delegates the filesystem operation to `/usr/bin/install` inside a temporary host tree.

**Tech Stack:** Bash, GNU `install`, Python 3.12, pytest, systemd host-asset smoke tests

## Global Constraints

- Use exactly `install -d -m 0750 -o epi-agent-web -g epi-agent-web /srv/epi-agent/runtime`.
- Run the ownership repair after root and operator-input validation and before switching `/opt/epi-agent/current` or enabling `epi-agent.service`.
- Keep the repair non-recursive; do not change ownership or mode of existing runtime children.
- Do not change CloudFormation, EC2 user data, systemd, EBS, SQLite schema, checkpoint path, service identities, ACL behavior, study storage, or the Python-worker launcher.
- Preserve the installer cleanup and rollback behavior; an ownership-repair failure must stop before release activation without deleting runtime data.
- Keep EC2 instance `i-0f9ed9c133ea2358b` stopped during implementation and review.

---

## File Structure

- `deploy/aws/bin/install-release.sh`: reassert the fixed runtime directory owner, group, and mode from the privileged immutable-release installer.
- `tests/test_aws_host_assets.py`: extend the real shell harness to emulate privileged `install` safely and add the regression for exact arguments and activation ordering.
- `scripts/smoke_aws_release_installer.py`: include the ownership regression in the executable installer smoke gate.

### Task 1: Repair and verify runtime directory ownership

**Files:**
- Modify: `tests/test_aws_host_assets.py`
- Modify: `deploy/aws/bin/install-release.sh`
- Modify: `scripts/smoke_aws_release_installer.py`

**Interfaces:**
- Consumes: the existing `_release_harness(...)` real-shell test fixture and `install-release.sh` activation sequence.
- Produces: a recorded `install` operation in `tmp_path / "operation.log"` and an installer that reasserts the fixed runtime ownership before service activation.

- [ ] **Step 1: Extend the test harness with a privileged-install substitute**

In `_release_harness`, add an `install` entry to the fake-command mapping. It must log the original arguments before removing only the `-o OWNER` and `-g GROUP` pairs and delegating everything else to the real binary:

```python
        "install": """printf 'install %s\\n' "$*" >> "$TEST_OPERATION_LOG"
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
```

Make the existing fake `systemctl` record cross-command ordering while retaining its dedicated log:

```python
        "systemctl": """printf 'systemctl %s\\n' "$*" >> "$TEST_OPERATION_LOG"
printf "%s\\n" "$*" >> "$TEST_SYSTEMCTL_LOG"
if [ "${1:-}" = is-active ] && [ "$TEST_SERVICE_INACTIVE" = 1 ]; then
  exit 3
fi
""",
```

Replace the production runtime path only in the generated test copy:

```python
    source = source.replace(
        "/srv/epi-agent/runtime", str(root / "srv" / "epi-agent" / "runtime")
    )
```

Delete the existing test-only source rewrite:

```python
    source = source.replace(" -o root -g epi-agent-web", "")
```

The new fake `install` now handles both existing root ownership flags and the new service ownership flags without weakening the production source. Add the shared log to the harness environment:

```python
        "TEST_OPERATION_LOG": str(tmp_path / "operation.log"),
```

- [ ] **Step 2: Write the failing ownership-and-ordering regression**

Add this test after the existing delayed-first-startup test:

```python
def test_release_installer_repairs_runtime_ownership_before_service_activation(
    tmp_path: Path,
) -> None:
    completed, _, _, _, _ = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
        previous_release_exists=False,
    )

    assert completed.returncode == 0, completed.stderr
    source = _asset("deploy/aws/bin/install-release.sh")
    repair = (
        "install -d -m 0750 -o epi-agent-web -g epi-agent-web "
        "/srv/epi-agent/runtime"
    )
    assert repair in source
    assert source.index(repair) < source.index("systemctl enable epi-agent.service")

    operations = (tmp_path / "operation.log").read_text(encoding="utf-8").splitlines()
    harness_repair = (
        "install -d -m 0750 -o epi-agent-web -g epi-agent-web "
        f"{tmp_path / 'host' / 'srv' / 'epi-agent' / 'runtime'}"
    )
    assert harness_repair in operations
    assert operations.index(harness_repair) < operations.index(
        "systemctl enable epi-agent.service"
    )
```

- [ ] **Step 3: Run the focused test and confirm RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_host_assets.py::test_release_installer_repairs_runtime_ownership_before_service_activation -q
```

Expected: `FAIL` because the exact runtime ownership command is absent from the current production installer.

- [ ] **Step 4: Add the minimal production correction**

In `deploy/aws/bin/install-release.sh`, add exactly this command beside the existing directory preparation, after argument validation and trap setup but before staging, symlink activation, and service enablement:

```bash
install -d -m 0750 -o epi-agent-web -g epi-agent-web /srv/epi-agent/runtime
```

Do not add recursion, `chown -R`, ACL changes, or conditional branching.

- [ ] **Step 5: Run the focused test and confirm GREEN**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_host_assets.py::test_release_installer_repairs_runtime_ownership_before_service_activation -q
```

Expected: `1 passed`.

- [ ] **Step 6: Add the regression to the executable installer smoke**

Add this exact node ID to the `tests` list in `scripts/smoke_aws_release_installer.py`:

```python
        "tests/test_aws_host_assets.py::test_release_installer_repairs_runtime_ownership_before_service_activation",
```

Update the final message to describe the complete release-installer smoke:

```python
    print("AWS release installer smoke passed")
```

- [ ] **Step 7: Run focused installer verification**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_host_assets.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_release_installer.py
bash -n deploy/aws/bin/install-release.sh
```

Expected: all host-asset tests pass, all three installer smoke tests pass, the smoke prints `AWS release installer smoke passed`, and `bash -n` exits `0` without output.

- [ ] **Step 8: Run the complete AWS regression gates**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_host_assets.py tests/test_aws_infrastructure.py tests/test_aws_phase2a_cli.py tests/test_build_aws_release.py tests/test_smoke_aws_phase2a_real.py tests/test_smoke_multi_user_isolation_real.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_release_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
bash -n deploy/aws/bin/install-release.sh deploy/aws/bin/install-study.sh deploy/aws/bin/epi-agent-python-worker
```

Expected: every pytest and smoke gate exits `0`; all shell files pass syntax validation.

- [ ] **Step 9: Review the scoped diff and commit**

Run:

```bash
git diff --check
git diff -- deploy/aws/bin/install-release.sh tests/test_aws_host_assets.py scripts/smoke_aws_release_installer.py
git add deploy/aws/bin/install-release.sh tests/test_aws_host_assets.py scripts/smoke_aws_release_installer.py
git commit -m "fix: repair AWS runtime directory ownership"
```

Expected: `git diff --check` exits `0`, the diff contains only the planned installer and regression changes, and the commit succeeds.
