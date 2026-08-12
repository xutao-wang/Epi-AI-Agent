# AWS Runtime Directory Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the native systemd service survive EC2 reboots, bridge the currently installed older unit through study-access recovery, prepare a verified immutable release, and create—but not execute—the corresponding CloudFormation change set.

**Architecture:** The shipped systemd unit owns `/run/epi-agent` through `RuntimeDirectory`, so systemd recreates the ephemeral path on every service start. The parameter-free recovery SSM document also creates the directory immediately before restarting the currently installed older unit. Regression tests verify the unit contract, CloudFormation command ordering, and real Linux ownership/mode behavior inside a disposable Docker test container.

**Tech Stack:** systemd, Amazon Linux 2023, CloudFormation, Systems Manager, Bash, Python 3.12, pytest, Docker, Git, deterministic tar/gzip, AWS CLI.

## Global Constraints

- Work only in the isolated `aws-execution` worktree on branch `aws-test`.
- Keep EC2 instance `i-0f9ed9c133ea2358b` stopped throughout this plan.
- Do not execute the change set, start EC2, invoke recovery, upload/deploy a release, or create a Cognito user.
- Preserve EBS data, conversations, checkpoints, artifacts, studies, and prior S3 objects.
- Production remains native Python/systemd; Docker is test-only.
- Observe focused RED failures before editing either production file.
- The review-only change set may modify only `EpiAgentRecoverStudyAccessDocument`.
- Prepare the new release locally from a clean correction commit; do not upload it.

## File Map

- `deploy/aws/systemd/epi-agent.service`: permanent runtime-directory ownership.
- `infra/aws/phase2a/template.yaml`: recovery bridge for the older installed unit.
- `tests/test_aws_host_assets.py`: systemd contract.
- `tests/test_aws_infrastructure.py`: recovery command and ordering contract.
- `tests/test_aws_study_access_recovery.py`: real Linux ownership/mode and failure behavior.
- `scripts/build_aws_release.py`: deterministic local release builder, used unchanged.
- `scripts/aws_phase2a.py`: account-guarded change-set creator, used unchanged.

---

### Task 1: Make systemd own the runtime directory

**Files:**
- Modify: `tests/test_aws_host_assets.py`
- Modify: `deploy/aws/systemd/epi-agent.service`

**Interfaces:**
- Consumes: service identity `epi-agent-web:epi-agent-web` and existing `/run/epi-agent` `ReadWritePaths` entry.
- Produces: a unit that creates `/run/epi-agent` with mode `0750` before namespace setup.

- [ ] **Step 1: Write the failing assertions**

Add to `test_web_service_runs_the_api_with_least_privilege`:

```python
    assert source.count("RuntimeDirectory=epi-agent") == 1
    assert source.count("RuntimeDirectoryMode=0750") == 1
    service_section = source.split("[Service]\n", 1)[1].split("\n[Install]", 1)[0]
    assert "RuntimeDirectory=epi-agent" in service_section
    assert "RuntimeDirectoryMode=0750" in service_section
```

- [ ] **Step 2: Verify RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_host_assets.py::test_web_service_runs_the_api_with_least_privilege -q
```

Expected: failure because both directives are absent.

- [ ] **Step 3: Add the minimal implementation**

After `Group=epi-agent-web`, add:

```ini
RuntimeDirectory=epi-agent
RuntimeDirectoryMode=0750
```

Keep the existing `ReadWritePaths` entry unchanged.

- [ ] **Step 4: Verify GREEN and commit**

Run the focused test again, then:

```bash
git diff --check
git diff -- deploy/aws/systemd/epi-agent.service tests/test_aws_host_assets.py
git add deploy/aws/systemd/epi-agent.service tests/test_aws_host_assets.py
git commit -m "fix: recreate AWS service runtime directory"
```

Expected: `1 passed`; the commit contains only the two directives and assertions.

---

### Task 2: Bridge the older unit through recovery

**Files:**
- Modify: `tests/test_aws_infrastructure.py`
- Modify: `tests/test_aws_study_access_recovery.py`
- Modify: `infra/aws/phase2a/template.yaml`

**Interfaces:**
- Consumes: the fixed parameter-free recovery command and real container user/group ID `1001`.
- Produces: `install -d -m 0750 -o epi-agent-web -g epi-agent-web /run/epi-agent` after study validation and before the one restart.

- [ ] **Step 1: Add the failing template contract**

In `test_phase2a_recovery_document_is_fixed_and_parameter_free`, add:

```python
    runtime_install = (
        "install -d -m 0750 -o epi-agent-web -g epi-agent-web "
        "/run/epi-agent"
    )
    assert runtime_install in command
    assert command.index("active study index is not readable") < command.index(runtime_install)
    assert command.index(runtime_install) < command.index("systemctl restart epi-agent.service")
```

- [ ] **Step 2: Extend the real Linux harness**

Add this module constant:

```python
RUNTIME_INSTALL = (
    "install -d -m 0750 -o epi-agent-web -g epi-agent-web /run/epi-agent"
)
```

Add `fail_runtime_install: bool = False` to `_container_recovery`, then after
command construction add:

```python
    if fail_runtime_install:
        failed_command = command.replace(RUNTIME_INSTALL, "false")
        assert failed_command != command, "recovery runtime install command is missing"
        command = failed_command
```

In the driver, run `rm -rf /run/epi-agent` before recovery. Replace the fake
systemctl body with:

```bash
case "$1" in
  restart)
    test -d /run/epi-agent || exit 65
    stat -c '%u:%g %a' /run/epi-agent > /work/runtime-directory-stat
    printf 'systemctl %s\n' "$*" >> /work/operations
    : > /work/service-running
    ;;
  is-active)
    printf 'systemctl %s\n' "$*" >> /work/operations
    test -f /work/service-running
    ;;
  status)
    printf 'systemctl %s\n' "$*" >> /work/operations
    exit 0
    ;;
  *) exit 64 ;;
esac
```

Assert successful recovery writes `1001:1001 750` to
`runtime-directory-stat`. Add:

```python
def test_recovery_stops_before_restart_when_runtime_directory_creation_fails(
    tmp_path: Path,
) -> None:
    completed, work = _container_recovery(tmp_path, fail_runtime_install=True)

    assert completed.returncode != 0
    assert not (work / "operations").exists()
    assert not (work / "runtime-directory-stat").exists()
```

- [ ] **Step 3: Verify RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_infrastructure.py::test_phase2a_recovery_document_is_fixed_and_parameter_free tests/test_aws_study_access_recovery.py -q
```

Expected: failures identify the missing runtime-install command and missing real
Linux runtime directory.

- [ ] **Step 4: Add the minimal template implementation**

After the validation heredoc terminator `PY` and before the restart, add:

```bash
install -d -m 0750 -o epi-agent-web -g epi-agent-web /run/epi-agent
```

- [ ] **Step 5: Verify GREEN and commit**

Run the focused tests again, then:

```bash
git diff --check
git diff -- infra/aws/phase2a/template.yaml tests/test_aws_infrastructure.py tests/test_aws_study_access_recovery.py
git add infra/aws/phase2a/template.yaml tests/test_aws_infrastructure.py tests/test_aws_study_access_recovery.py
git commit -m "fix: bootstrap AWS recovery runtime directory"
```

Expected: all selected tests pass; the production template adds exactly one fixed command.

---

### Task 3: Run the complete correction gate

**Files:** Verify all planned source, tests, and existing smoke gates.

**Interfaces:**
- Consumes: both correction commits.
- Produces: fresh unit, template, Docker/Linux, release, frontend, and syntax evidence.

- [ ] **Step 1: Run all relevant Python tests**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_phase2a_cli.py tests/test_aws_infrastructure.py tests/test_aws_study_access_recovery.py tests/test_aws_study_installer.py tests/test_aws_host_assets.py tests/test_build_aws_release.py tests/test_smoke_aws_phase2a_real.py tests/test_smoke_multi_user_isolation_real.py -q
```

Expected: all tests pass; live AWS acceptance is not invoked.

- [ ] **Step 2: Run executable smokes and checks**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_release_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_study_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
bash -n deploy/aws/bin/install-release.sh deploy/aws/bin/install-study.sh deploy/aws/bin/epi-agent-python-worker
git diff --check
```

Expected: every command exits `0` and every smoke prints its pass result.

- [ ] **Step 3: Run frontend tests/build and verify scope**

From `frontend/`, run `npm test -- --run` and `npm run build`. Then from the
worktree root run:

```bash
git status --short --branch
git log --oneline -4
git show --stat --oneline HEAD~1
git show --stat --oneline HEAD
```

Expected: frontend passes; tracked status is clean; two implementation commits
touch only the five planned source/test files.

---

### Task 4: Prepare and audit the new local release

**Files:**
- Use unchanged: `scripts/build_aws_release.py`
- Create ignored artifacts: `dist/aws/runtime-directory-repro-one/`, `dist/aws/runtime-directory-repro-two/`, `dist/aws/`

**Interfaces:**
- Consumes: clean `aws-test` HEAD.
- Produces: a reproducible local `.tar.gz`, checksum, and manifest; no upload.

- [ ] **Step 1: Build twice**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws/runtime-directory-repro-one
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws/runtime-directory-repro-two
```

Expected: both builds use the same clean HEAD commit.

- [ ] **Step 2: Prove reproducibility and audit**

Set `RELEASE_ID` to `git rev-parse HEAD`, then run:

```bash
cmp "dist/aws/runtime-directory-repro-one/epi-agent-${RELEASE_ID}.tar.gz" "dist/aws/runtime-directory-repro-two/epi-agent-${RELEASE_ID}.tar.gz"
shasum -a 256 "dist/aws/runtime-directory-repro-one/epi-agent-${RELEASE_ID}.tar.gz" "dist/aws/runtime-directory-repro-two/epi-agent-${RELEASE_ID}.tar.gz"
tar -tzf "dist/aws/runtime-directory-repro-one/epi-agent-${RELEASE_ID}.tar.gz"
```

Expected: byte-identical archives, identical SHA-256, safe root-relative tracked
members plus `release.json`, and no database, study archive, `.env`, credential,
or secret file.

- [ ] **Step 3: Build the handoff copy**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws
shasum -a 256 -c "dist/aws/epi-agent-${RELEASE_ID}.sha256"
git status --short --branch
```

Expected: checksum `OK`, tracked status clean. Record ID, path, size, SHA-256,
and manifest; leave all artifacts local.

---

### Task 5: Create and inspect the review-only change set

**Files:** Use unchanged `scripts/aws_phase2a.py` and the committed Phase 2A template.

**Interfaces:**
- Consumes: account `641379499556`, profile `xutao-dev`, region `us-east-1`, domain `epiagent.org`, hosted zone `Z02132461LVJ2PFOYFXFU`, email `xw488@njms.rutgers.edu`, data volume `50` GiB.
- Produces: one unexecuted `CREATE_COMPLETE` change set modifying only the recovery SSM document.

- [ ] **Step 1: Validate identity and template**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py identity
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py validate
```

Expected: exact `xutao-dev` identity in account `641379499556`; lint and AWS validation pass.

- [ ] **Step 2: Create only the change set**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py plan-stack --domain-name epiagent.org --hosted-zone-id Z02132461LVJ2PFOYFXFU --certificate-email xw488@njms.rutgers.edu --alert-email xw488@njms.rutgers.edu --data-volume-gib 50 --data-snapshot-id ''
```

Expected: `CREATE_COMPLETE`. Do not call `execute-change-set`.

- [ ] **Step 3: Inspect scope and stopped state**

With the returned ARN:

```bash
aws cloudformation describe-change-set --change-set-name "$CHANGE_SET_ARN" --include-property-values --profile xutao-dev --region us-east-1 --output json
aws ec2 describe-instances --instance-ids i-0f9ed9c133ea2358b --profile xutao-dev --region us-east-1 --output json --query 'Reservations[0].Instances[0].State.Name'
```

Expected: one non-replacement `Modify` for
`EpiAgentRecoverStudyAccessDocument`; instance state `stopped`. If any other
resource appears, leave the change set unexecuted and report it.

- [ ] **Step 4: Report the exact handoff boundary**

Report the change-set ARN/action, local release ID/SHA-256, and stopped state.
State that the change set is unexecuted, the release is not uploaded, recovery
was not rerun, EC2 was not started, nothing was deployed, and no Cognito user
was created.
