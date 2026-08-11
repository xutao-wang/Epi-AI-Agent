# AWS ASGI Entry-Point Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start the configured Epi Agent FastAPI application on AWS, verify the exact systemd ASGI target locally before release, and then install synthetic study package `0.2.0`.

**Architecture:** Keep `api.server` as the route factory and point the AWS systemd unit at the canonical configured application object, `api.app:app`. Protect the correction with an exact host-asset assertion and an executable smoke derived from the real unit file, then publish one immutable application release before separately installing the immutable study archive.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, pytest, systemd, Bash, AWS S3, AWS Systems Manager, EC2.

## Global Constraints

- Work only in `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.worktrees/aws-execution` on branch `aws-test`.
- Run Python with `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python`; never use an unqualified `python3`.
- Change only the systemd Uvicorn target from `api.server:app` to `api.app:app`; preserve every other service directive.
- Keep `api.server` as the route factory; do not add an `app` alias and do not use Uvicorn factory mode.
- The executable smoke must read the real systemd `ExecStart`, import that exact target through real startup code, and require a `FastAPI` object.
- Use TDD: record the expected failure before editing the production unit, then apply the smallest correction and rerun the same checks.
- Do not change CloudFormation, EC2, EBS, VPC, IAM, Cognito, Route 53, Nginx, certificate, database format, release installer, or user-data configuration.
- Do not delete failed release directories, older S3 objects, prior study versions, or EBS runtime data.
- Build, upload, and deploy only a clean, newly committed, checksum-verified application release; send at most one new deployment command.
- Never retry SSM command IDs `67a8ee05-4db7-431d-be98-443c54613253`, `84aa0bdd-aeaf-4c51-99f9-d1e13da343a1`, or `b707be9e-b17f-44f4-9b36-955a246dc8f3`.
- Do not upload or install the study until the new application release is active and healthy.
- Install only `/Users/xutaowang/Desktop/RA work/Epi-Agent/Database/report-india-synthetic/delivery/report-india-synthetic-0.2.0.tar.gz`, with SHA-256 `51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e`.
- AWS mutations must use profile `xutao-dev`, account `641379499556`, and region `us-east-1` through the guarded project CLI where supported.

---

### Task 1: Correct and prove the AWS ASGI service target

**Files:**
- Modify: `deploy/aws/systemd/epi-agent.service`
- Modify: `tests/test_aws_host_assets.py`
- Create: `scripts/smoke_aws_service_entrypoint.py`

**Interfaces:**
- Consumes: the real systemd `ExecStart=` directive and the configured FastAPI object exported by `api.app`.
- Produces: an exact `api.app:app` service contract and an executable smoke that fails if the unit names a missing or non-FastAPI object.

- [ ] **Step 1: Add the exact failing host-asset assertion**

In `test_web_service_runs_the_api_with_least_privilege()` in `tests/test_aws_host_assets.py`, add:

```python
    assert (
        "ExecStart=/opt/epi-agent/current/.venv/bin/python -m uvicorn "
        "api.app:app --host 127.0.0.1 --port 8000 --workers 1"
    ) in source
    assert "api.server:app" not in source
```

- [ ] **Step 2: Verify the host-asset test is RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_host_assets.py::test_web_service_runs_the_api_with_least_privilege -q
```

Expected: FAIL because the unit contains `api.server:app`, not `api.app:app`.

- [ ] **Step 3: Add the executable exact-entry-point smoke**

Create executable `scripts/smoke_aws_service_entrypoint.py`:

```python
#!/usr/bin/env python3
"""Import the exact ASGI target configured by the AWS systemd service."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


IMPORT_ASSERTION = """
import importlib
import sys
from fastapi import FastAPI

module_name, separator, attribute_path = sys.argv[1].partition(":")
if not separator or not module_name or not attribute_path:
    raise SystemExit(f"invalid ASGI target: {sys.argv[1]!r}")
application = importlib.import_module(module_name)
for attribute in attribute_path.split("."):
    application = getattr(application, attribute)
if not isinstance(application, FastAPI):
    raise SystemExit(f"{sys.argv[1]} is not a FastAPI application")
"""


def service_asgi_target(service_path: Path) -> str:
    exec_start_lines = [
        line.removeprefix("ExecStart=")
        for line in service_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    ]
    if len(exec_start_lines) != 1:
        raise RuntimeError("AWS service must contain exactly one ExecStart directive")
    command = shlex.split(exec_start_lines[0])
    try:
        uvicorn_index = command.index("uvicorn")
        target = command[uvicorn_index + 1]
    except (ValueError, IndexError) as error:
        raise RuntimeError("AWS service ExecStart does not contain a Uvicorn target") from error
    return target


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = service_asgi_target(
        root / "deploy" / "aws" / "systemd" / "epi-agent.service"
    )
    with tempfile.TemporaryDirectory(prefix="epi-agent-asgi-smoke-") as temporary:
        isolated_root = Path(temporary)
        environment = os.environ.copy()
        environment.update(
            {
                "OPENAI_API_KEY": "",
                "REPORT_AGENT_AUTH_MODE": "cognito",
                "REPORT_AGENT_AWS_REGION": "us-east-1",
                "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
                "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "example",
                "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://example.auth.us-east-1.amazoncognito.com/logout",
                "REPORT_AGENT_AUTH_REDIRECT_URI": "https://example.test/auth/callback",
                "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://example.test/",
                "REPORT_AGENT_RUNTIME_ROOT": str(isolated_root / "runtime"),
                "REPORT_AGENT_CHECKPOINT_DB_PATH": str(isolated_root / "runtime" / "agent_memory_fastapi.db"),
                "REPORT_AGENT_STUDY_ROOT": str(isolated_root / "study_data"),
                "REPORT_AGENT_STATIC_DIR": str(root / "frontend" / "dist"),
                "REPORT_AGENT_PYTHON_WORKER_LAUNCHER": "",
                "DB_RAG_EMBEDDING_MODEL": "OpenAI/text-embedding-3-large",
                "DB_RAG_RERANKER_MODEL": "",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", IMPORT_ASSERTION, target],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    if completed.returncode:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        return completed.returncode
    print(f"AWS service ASGI entry-point smoke passed: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Set its executable bit with `chmod 755 scripts/smoke_aws_service_entrypoint.py`.

- [ ] **Step 4: Verify the executable smoke is RED**

Run once:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
```

Expected: nonzero with `AttributeError: module 'api.server' has no attribute 'app'`.

- [ ] **Step 5: Apply the minimal production correction**

Replace only the `ExecStart=` line in `deploy/aws/systemd/epi-agent.service` with:

```ini
ExecStart=/opt/epi-agent/current/.venv/bin/python -m uvicorn api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

- [ ] **Step 6: Verify GREEN with the same checks**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_host_assets.py::test_web_service_runs_the_api_with_least_privilege -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
```

Expected: one pytest passes and the smoke prints `AWS service ASGI entry-point smoke passed: api.app:app`.

- [ ] **Step 7: Run the complete local AWS regression gate**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_infrastructure.py tests/test_aws_host_assets.py tests/test_aws_phase2a_cli.py -q
bash -n deploy/aws/bin/install-release.sh
bash -n deploy/aws/bin/install-study.sh
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_release_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py validate
git diff --check
```

Expected: every check exits 0 with all tests and smokes passing.

- [ ] **Step 8: Commit**

```bash
git add deploy/aws/systemd/epi-agent.service tests/test_aws_host_assets.py scripts/smoke_aws_service_entrypoint.py
git commit -m "fix: launch configured AWS application"
```

---

### Task 2: Build, deploy, and verify one new application release

**Files:**
- Use: `scripts/build_aws_release.py`
- Use: `scripts/aws_phase2a.py`
- Append evidence: `.superpowers/sdd/startup-task-2-report.md`

**Interfaces:**
- Consumes: the clean, reviewed Task 1 commit, bucket `epi-agent-phase2a-applicationbucket-who6tewa8rga`, SSM document `epi-agent-deploy-release` version 6, and instance `i-0f9ed9c133ea2358b`.
- Produces: one immutable active application release whose identity equals the Task 1 commit and whose public health/readiness routes succeed over TLS.

- [ ] **Step 1: Build once from a clean reviewed commit**

Run `git status --short` and require no output. Record `git rev-parse HEAD` as `release_id`; require 40 lowercase hexadecimal characters and require it not to equal `004186c18f9ed9ea4e2a0939cf63eecee4d4a411`.

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws
```

Read `dist/aws/epi-agent-${release_id}.json`; require `commit_sha=${release_id}`. Run `shasum -a 256 -c dist/aws/epi-agent-${release_id}.sha256`. Inspect the archive member list; require `deploy/aws/systemd/epi-agent.service` and reject `.env`, `.env.*`, `runtime/`, `study_data/`, and `report-india-*.tar.gz` members.

- [ ] **Step 2: Upload and verify the immutable release object**

Run exactly once:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py upload-release \
  "dist/aws/epi-agent-${release_id}.tar.gz" "releases/${release_id}.tar.gz"
```

Use `aws s3api head-object` with profile `xutao-dev`, region `us-east-1`, bucket `epi-agent-phase2a-applicationbucket-who6tewa8rga`, and key `releases/${release_id}.tar.gz`. Require `ServerSideEncryption=AES256`, `Metadata.sha256` equal to the build manifest's `archive_sha256`, and non-empty `VersionId`.

- [ ] **Step 3: Verify the deployment boundary before mutation**

Use guarded identity and read-only AWS queries to require:

```text
account: 641379499556
principal: arn:aws:iam::641379499556:user/xutao-dev
instance i-0f9ed9c133ea2358b: SSM PingStatus=Online
document epi-agent-deploy-release: DefaultVersion=6 and LatestVersion=6
stack epi-agent-phase2a: UPDATE_COMPLETE
```

If any condition differs, stop without sending a command.

- [ ] **Step 4: Send one new application deployment command**

Run `scripts/aws_phase2a.py deploy-release` once with key `releases/${release_id}.tar.gz`, the manifest's exact 64-character `archive_sha256`, `release_id`, domain `epiagent.org`, and email `xw488@njms.rutgers.edu`. Record the new command ID. On `Failed`, `TimedOut`, or `Cancelled`, retrieve only that invocation's output and service/application logs, then stop without retry.

- [ ] **Step 5: Verify the live application and unchanged infrastructure**

Require all of:

```text
new deployment command: Success
/opt/epi-agent/current: /opt/epi-agent/releases/${release_id}
epi-agent.service: enabled and active
systemd ExecStart ASGI target: api.app:app
local /api/health: HTTP 200
local /api/readiness: HTTP 200 with status=ready
https://epiagent.org/api/health: HTTP 200
https://epiagent.org/api/readiness: HTTP 200 with status=ready
TLS certificate: valid for epiagent.org and unexpired
instance: i-0f9ed9c133ea2358b, t3.large, ami-07a5b367e8dc8bd92
root volume: vol-0cb3e1bb3af5e4245
data volume: vol-043235ec767877900
deployment/service alarms: not ALARM
```

Append release ID, SHA-256, S3 key/version, SSM command ID, terminal status, endpoint evidence, and unchanged resource identities to `.superpowers/sdd/startup-task-2-report.md`.

---

### Task 3: Install and verify synthetic study package 0.2.0

**Files:**
- Use: `/Users/xutaowang/Desktop/RA work/Epi-Agent/Database/report-india-synthetic/delivery/report-india-synthetic-0.2.0.tar.gz`
- Use: `scripts/aws_phase2a.py`
- Use on EC2: `/usr/local/sbin/install-study.sh`
- Append evidence: `.superpowers/sdd/startup-task-2-report.md`

**Interfaces:**
- Consumes: the healthy Task 2 application, immutable study SHA-256 `51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e`, and EBS-backed root `/srv/epi-agent/study_data`.
- Produces: active study `report-india-synthetic@0.2.0`, rediscovered after one service restart, while preserving prior packages and runtime data.

- [ ] **Step 1: Reverify the local study archive**

Run `shasum -a 256` on the exact archive and require `51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e`. Read `study-package.json` directly from the archive and require:

```text
format_version: 2
study_id: report-india-synthetic
package_version: 0.2.0
```

- [ ] **Step 2: Upload and verify the immutable study object**

Run exactly once:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py upload-study \
  "/Users/xutaowang/Desktop/RA work/Epi-Agent/Database/report-india-synthetic/delivery/report-india-synthetic-0.2.0.tar.gz" \
  "studies/report-india-synthetic-0.2.0.tar.gz"
```

Use `aws s3api head-object`; require `ServerSideEncryption=AES256`, `Metadata.sha256=51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e`, and non-empty `VersionId`.

- [ ] **Step 3: Send one guarded study-install command**

After rechecking identity, SSM Online state, and application health, send one new `AWS-RunShellScript` command to `i-0f9ed9c133ea2358b` whose commands are exactly:

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

Record the new command ID. On failure, retrieve only that invocation's output and logs, then stop without retry.

- [ ] **Step 4: Verify the active study and application**

Require all of:

```text
study install command: Success
/srv/epi-agent/study_data/studies/packages/report-india-synthetic/0.2.0/study-package.json: exists
installed manifest study_id: report-india-synthetic
installed manifest package_version: 0.2.0
study registry active version: report-india-synthetic=0.2.0
epi-agent.service: enabled and active
local health/readiness: HTTP 200
public health/readiness: HTTP 200
runtime capabilities: no longer report "No study package is installed."
EC2/EBS identities: unchanged from Task 2
deployment/service alarms: not ALARM
```

Append study SHA-256, S3 key/version, install command ID, installed paths, active registry evidence, endpoint evidence, and unchanged resource identities to `.superpowers/sdd/startup-task-2-report.md`.
