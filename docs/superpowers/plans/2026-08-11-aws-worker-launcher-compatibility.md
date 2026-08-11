# AWS Python-Worker Launcher Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start Epi Agent with the existing AWS Python-worker configuration without weakening its fixed privilege boundary, then deploy one new immutable release and install synthetic study package `0.2.0`.

**Architecture:** Normalize either of the two exact approved configuration forms to one immutable sudo launcher tuple inside `api.deployment.python_worker_launcher()`. Exercise the real AWS form through both a focused unit test and the existing systemd-entry-point smoke, keep EC2 stopped during coding/review, and start it only for a guarded one-command deployment.

**Tech Stack:** Python 3.12, FastAPI, pytest, systemd, Bash, AWS S3, AWS Systems Manager, EC2.

## Global Constraints

- Work only in `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.worktrees/aws-execution` on branch `aws-test`.
- Run Python with `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python`; never use an unqualified `python3`.
- EC2 instance `i-0f9ed9c133ea2358b` must remain stopped throughout implementation and review.
- Accept only `/usr/local/libexec/epi-agent-python-worker` and `/usr/bin/sudo -n /usr/local/libexec/epi-agent-python-worker`; normalize both to the same fixed sudo tuple.
- Continue rejecting relative paths, alternate executables, additional flags, shell separators, embedded newlines, null bytes, and all other token sequences.
- Do not change CloudFormation, EC2 user data, `/etc/epi-agent/app.env`, systemd, Nginx, IAM, Cognito, Route 53, EBS, the worker wrapper, sudoers, database formats, or the release installer.
- Use strict TDD: record focused unit and real-entry-point smoke failures before changing production code.
- Do not delete failed release directories, prior S3 objects, prior study versions, or EBS data.
- Never retry SSM command IDs `67a8ee05-4db7-431d-be98-443c54613253`, `84aa0bdd-aeaf-4c51-99f9-d1e13da343a1`, `b707be9e-b17f-44f4-9b36-955a246dc8f3`, or `c3c8b05d-30de-448e-bdee-e7b5c8fd4ce3`.
- Build, upload, and deploy only a clean, newly committed, checksum-verified release; send at most one new application deployment command.
- If the new deployment fails, collect its evidence, do not retry it, and stop EC2 again after diagnostics.
- Do not upload or install the study until the application is active and healthy.
- Install only `/Users/xutaowang/Desktop/RA work/Epi-Agent/Database/report-india-synthetic/delivery/report-india-synthetic-0.2.0.tar.gz`, whose required SHA-256 is `51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e`.
- AWS mutations must use profile `xutao-dev`, account `641379499556`, and region `us-east-1` through the guarded project CLI where supported.

---

### Task 1: Normalize the two fixed Python-worker launcher forms

**Files:**
- Modify: `api/deployment.py`
- Modify: `tests/test_no_study_startup.py`
- Modify: `scripts/smoke_aws_service_entrypoint.py`

**Interfaces:**
- Consumes: `REPORT_AGENT_PYTHON_WORKER_LAUNCHER` as a string in an environment mapping.
- Produces: `tuple[str, ...] | None`, where either accepted non-empty input returns `("/usr/bin/sudo", "-n", "/usr/local/libexec/epi-agent-python-worker")` and every other non-empty command raises `ValueError`.

- [ ] **Step 1: Add the failing AWS-form unit regression**

In `test_python_worker_launcher_reads_the_hosted_launcher_setting()` in `tests/test_no_study_startup.py`, extend the accepted-value parameterization to:

```python
@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("", None),
        (
            "/usr/local/libexec/epi-agent-python-worker",
            (
                "/usr/bin/sudo",
                "-n",
                "/usr/local/libexec/epi-agent-python-worker",
            ),
        ),
        (
            "/usr/bin/sudo -n /usr/local/libexec/epi-agent-python-worker",
            (
                "/usr/bin/sudo",
                "-n",
                "/usr/local/libexec/epi-agent-python-worker",
            ),
        ),
    ],
)
```

Remove only `/usr/bin/sudo -n /usr/local/libexec/epi-agent-python-worker` from the unsafe-configuration parameterization. Keep all other rejected examples unchanged.

- [ ] **Step 2: Configure the real entry-point smoke like AWS**

In `scripts/smoke_aws_service_entrypoint.py`, replace:

```python
"REPORT_AGENT_PYTHON_WORKER_LAUNCHER": "",
```

with:

```python
"REPORT_AGENT_PYTHON_WORKER_LAUNCHER": (
    "/usr/bin/sudo -n /usr/local/libexec/epi-agent-python-worker"
),
```

- [ ] **Step 3: Verify both regressions are RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_no_study_startup.py::test_python_worker_launcher_reads_the_hosted_launcher_setting -q
```

Expected: two cases pass and the new sudo-prefixed AWS case fails with `ValueError: REPORT_AGENT_PYTHON_WORKER_LAUNCHER must be the fixed worker path`.

Run the dedicated smoke once:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
```

Expected: nonzero with the same `ValueError` while importing the real `api.app:app` target.

- [ ] **Step 4: Implement the minimal exact-tuple normalization**

In `api/deployment.py`, replace:

```python
    if launcher != (_PYTHON_WORKER_PATH,):
        raise ValueError(
            "REPORT_AGENT_PYTHON_WORKER_LAUNCHER must be the fixed worker path"
        )
```

with:

```python
    if launcher not in {(_PYTHON_WORKER_PATH,), _PYTHON_WORKER_LAUNCHER}:
        raise ValueError(
            "REPORT_AGENT_PYTHON_WORKER_LAUNCHER must be the fixed worker path"
        )
```

Keep the existing empty-value behavior, unsafe-character checks, `shlex.split()`, error messages, and canonical return value unchanged.

- [ ] **Step 5: Verify GREEN with the identical focused checks**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_no_study_startup.py::test_python_worker_launcher_reads_the_hosted_launcher_setting \
  tests/test_no_study_startup.py::test_python_worker_launcher_rejects_unsafe_hosted_configuration -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
```

Expected: all parameterized unit cases pass and the smoke prints `AWS service ASGI entry-point smoke passed: api.app:app`.

- [ ] **Step 6: Run the full local regression gate once**

```bash
OPENAI_API_KEY=test-only-placeholder \
  /Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_no_study_startup.py tests/test_epi_python_runtime.py \
  tests/test_aws_infrastructure.py tests/test_aws_host_assets.py \
  tests/test_aws_phase2a_cli.py -q
bash -n deploy/aws/bin/install-release.sh
bash -n deploy/aws/bin/install-study.sh
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_release_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py validate
git diff --check
```

Expected: all tests and smokes pass, both shell syntax checks exit 0, CloudFormation validation exits 0, and `git diff --check` exits 0.

- [ ] **Step 7: Commit**

```bash
git add api/deployment.py tests/test_no_study_startup.py
git add -f scripts/smoke_aws_service_entrypoint.py
git commit -m "fix: accept fixed AWS worker launcher"
```

---

### Task 2: Publish and deploy one corrected application release

**Files:**
- Use: `scripts/build_aws_release.py`
- Use: `scripts/aws_phase2a.py`
- Append evidence: `.superpowers/sdd/startup-task-2-report.md`

**Interfaces:**
- Consumes: the clean reviewed Task 1 commit, stopped instance `i-0f9ed9c133ea2358b`, bucket `epi-agent-phase2a-applicationbucket-who6tewa8rga`, and SSM document `epi-agent-deploy-release` version 6.
- Produces: one immutable active application release with successful local/public health and readiness.

- [ ] **Step 1: Build and verify a new immutable archive**

Require `git status --short` to produce no output. Record the 40-character `git rev-parse HEAD` as `release_id`; require it to differ from both `004186c18f9ed9ea4e2a0939cf63eecee4d4a411` and `153c64bb7894d32bcb5646fe37c8c99c55cf903b`.

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws
```

From `dist/aws`, verify the emitted `.sha256` sidecar. Require the JSON manifest `commit_sha` to equal `release_id`, require the service unit and corrected `api/deployment.py` in the archive, and reject `.env`, `.env.*`, `runtime/`, `study_data/`, and `report-india-*.tar.gz` members.

- [ ] **Step 2: Upload and verify exactly one immutable release object**

Run `scripts/aws_phase2a.py upload-release` once for `dist/aws/epi-agent-${release_id}.tar.gz` and key `releases/${release_id}.tar.gz`. Require S3 `head-object` to report `ServerSideEncryption=AES256`, metadata SHA equal to the local archive SHA, and a non-empty `VersionId`.

- [ ] **Step 3: Start only the existing EC2 instance**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py start \
  --confirm-instance i-0f9ed9c133ea2358b
```

Wait until EC2 state is `running` and SSM `PingStatus=Online`. Require the instance to remain `i-0f9ed9c133ea2358b`, type `t3.large`, AMI `ami-07a5b367e8dc8bd92`, root volume `vol-0cb3e1bb3af5e4245`, and data volume `vol-043235ec767877900`.

- [ ] **Step 4: Recheck the deployment boundary and send one command**

Require account `641379499556`, principal `arn:aws:iam::641379499556:user/xutao-dev`, stack `epi-agent-phase2a` status `UPDATE_COMPLETE`, and document `epi-agent-deploy-release` with `DefaultVersion=6` and `LatestVersion=6`. Snapshot existing command IDs, including all four prohibited IDs.

Run `scripts/aws_phase2a.py deploy-release` once with the exact release key, archive SHA, release ID, domain `epiagent.org`, and email `xw488@njms.rutgers.edu`. Identify and record the one new command ID.

If it fails, retrieve its invocation and application/service logs, do not retry it, stop EC2 with the guarded CLI, verify `stopped`, and end Task 2 as blocked.

- [ ] **Step 5: Verify successful deployment**

Require:

```text
new command: Success
/opt/epi-agent/current: /opt/epi-agent/releases/${release_id}
epi-agent.service: enabled and active
systemd ASGI target: api.app:app
local /api/health: HTTP 200
local /api/readiness: HTTP 200 with status=ready
https://epiagent.org/api/health: HTTP 200
https://epiagent.org/api/readiness: HTTP 200 with status=ready
TLS certificate: valid for epiagent.org and unexpired
EC2/EBS identities: unchanged
deployment/service alarms: not ALARM
```

Append build, S3, lifecycle, SSM, endpoint, TLS, alarm, and resource-identity evidence to `.superpowers/sdd/startup-task-2-report.md`.

---

### Task 3: Install and verify synthetic study package 0.2.0

**Files:**
- Use: `/Users/xutaowang/Desktop/RA work/Epi-Agent/Database/report-india-synthetic/delivery/report-india-synthetic-0.2.0.tar.gz`
- Use: `scripts/aws_phase2a.py`
- Use on EC2: `/usr/local/sbin/install-study.sh`
- Append evidence: `.superpowers/sdd/startup-task-2-report.md`

**Interfaces:**
- Consumes: the healthy Task 2 application and immutable study SHA-256 `51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e`.
- Produces: active EBS-backed study `report-india-synthetic@0.2.0`, visible after one service restart.

- [ ] **Step 1: Reverify and upload the study once**

Require the local archive SHA-256 to equal `51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e`. Read its root `study-package.json` and require `format_version=2`, `study_id=report-india-synthetic`, and `package_version=0.2.0`.

Run `scripts/aws_phase2a.py upload-study` once with key `studies/report-india-synthetic-0.2.0.tar.gz`. Require S3 `AES256`, matching SHA metadata, and non-empty `VersionId`.

- [ ] **Step 2: Install and restart once through SSM**

After rechecking identity, SSM Online state, and application health, send one `AWS-RunShellScript` command whose commands are exactly:

```bash
set -euo pipefail
/usr/local/sbin/install-study.sh epi-agent-phase2a-applicationbucket-who6tewa8rga studies/report-india-synthetic-0.2.0.tar.gz 51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e report-india-synthetic 0.2.0
systemctl restart epi-agent.service
deadline=$((SECONDS + 120))
until curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/api/health >/dev/null && curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/api/readiness >/dev/null; do
  systemctl is-active --quiet epi-agent.service
  [ "$SECONDS" -lt "$deadline" ]
  sleep 2
done
```

Record the command ID. On failure, retrieve only that invocation's output and logs, do not retry, and stop.

- [ ] **Step 3: Verify the active study**

Require the installed manifest at `/srv/epi-agent/study_data/studies/packages/report-india-synthetic/0.2.0/study-package.json`, registry active value `report-india-synthetic=0.2.0`, active service, local/public HTTP 200 health and readiness, study-backed runtime capabilities, unchanged EC2/EBS identities, and alarms not `ALARM`.

Append study S3 version, command ID, installed manifest/registry evidence, endpoints, capabilities, alarms, and resource identities to `.superpowers/sdd/startup-task-2-report.md`.
