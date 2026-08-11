# AWS Study Access Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fixed, parameter-free SSM recovery operation that restores the retained study to `epi-agent-web`, validates access as that user, and returns the current application to health before another release deployment.

**Architecture:** CloudFormation owns one narrowly scoped recovery document with no caller-controlled parameters. The document repairs only `/srv/epi-agent/study_data`, validates the active registry/package/index paths through a fixed `runuser` and `env -i` boundary, then restarts and health-checks the existing service. The pinned operator CLI invokes that stack-provided document only after exact instance confirmation; a real-shell harness tests the inline recovery program without AWS access.

**Tech Stack:** AWS CloudFormation, AWS Systems Manager Command documents, Bash, Python 3.12, pytest, PyYAML

## Global Constraints

- Keep EC2 instance `i-0f9ed9c133ea2358b` stopped throughout source implementation, testing, and review.
- Name the parameter-free SSM document `epi-agent-recover-study-access` and keep it within the existing `epi-agent-*` IAM scope.
- Change ownership only with `chown -R -h epi-agent-web:epi-agent-web /srv/epi-agent/study_data`.
- Validate retained study access through `/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i`.
- Pass only fixed `PATH`, `LANG`, `LC_ALL`, `PYTHONUTF8`, and `REPORT_AGENT_STUDY_ROOT` values to recovery Python.
- Restart `epi-agent.service` only after service-user validation succeeds, then require local health and readiness within 120 seconds.
- Do not delete, replace, reinstall, or modify the active registry, packages, Chroma contents, conversations, checkpoints, artifacts, releases, or other retained EBS data.
- Do not change IAM policies, EC2, EBS, networking, Route 53, Cognito, systemd units, application schemas, the uploaded study object, or the reviewed `install-study.sh` implementation.
- The operator CLI must verify account `641379499556`, principal `arn:aws:iam::641379499556:user/xutao-dev`, region `us-east-1`, profile `xutao-dev`, the stack-provided instance ID, and the stack-provided recovery-document name.
- Never retry failed SSM command ID `6c1f1445-8e9b-470e-9fbc-72d2cf60a80b` or any other failed historical command ID.
- Do not create or execute a CloudFormation change set, start EC2, send an SSM command, upload an artifact, deploy a release, or restart a live service during implementation or review.

---

## File Structure

- `infra/aws/phase2a/template.yaml`: declares the parameter-free recovery SSM document and exposes its name.
- `tests/test_aws_infrastructure.py`: locks the static CloudFormation recovery contract and command ordering.
- `tests/test_aws_study_access_recovery.py`: executes the real inline SSM shell command with only privileged host commands substituted.
- `scripts/smoke_aws_phase2a_template_regressions.py`: keeps the recovery document in the existing executable template smoke gate.
- `scripts/aws_phase2a.py`: provides the account- and instance-guarded `recover-study-access` operator command and shared SSM polling.
- `tests/test_aws_phase2a_cli.py`: verifies exact document/instance selection, no parameters, polling, and fail-closed behavior.
- `docs/aws/phase2a-runbook.md`: documents recovery before release deployment.

### Task 1: Add and dynamically verify the parameter-free recovery document

**Files:**
- Modify: `infra/aws/phase2a/template.yaml`
- Modify: `tests/test_aws_infrastructure.py`
- Create: `tests/test_aws_study_access_recovery.py`
- Modify: `scripts/smoke_aws_phase2a_template_regressions.py`

**Interfaces:**
- Consumes: retained root `/srv/epi-agent/study_data`, current interpreter `/opt/epi-agent/current/.venv/bin/python`, and existing `epi-agent.service`.
- Produces: CloudFormation resource `EpiAgentRecoverStudyAccessDocument` and output `RecoverStudyAccessDocumentName`.

- [ ] **Step 1: Add the failing static CloudFormation contract test**

Append this test to `tests/test_aws_infrastructure.py`:

```python
def test_phase2a_recovery_document_is_fixed_and_parameter_free() -> None:
    template = phase2a_template()
    document = template["Resources"]["EpiAgentRecoverStudyAccessDocument"]

    assert document["Type"] == "AWS::SSM::Document"
    properties = document["Properties"]
    assert properties["Name"] == "epi-agent-recover-study-access"
    assert properties["DocumentType"] == "Command"
    assert properties["DocumentFormat"] == "YAML"
    assert properties["UpdateMethod"] == "NewVersion"
    content = properties["Content"]
    assert "parameters" not in content
    assert content["schemaVersion"] == "2.2"
    step = content["mainSteps"][0]
    assert step["action"] == "aws:runShellScript"
    command = "\n".join(step["inputs"]["runCommand"])
    ownership = (
        "chown -R -h epi-agent-web:epi-agent-web "
        '"$study_root"'
    )
    privilege_drop = (
        "/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i"
    )
    assert "readonly study_root=/srv/epi-agent/study_data" in command
    assert ownership in command
    assert privilege_drop in command
    assert "PATH=/opt/epi-agent/current/.venv/bin:/usr/bin" in command
    assert "LANG=C.UTF-8" in command
    assert "LC_ALL=C.UTF-8" in command
    assert "PYTHONUTF8=1" in command
    assert "REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data" in command
    assert command.index(ownership) < command.index(privilege_drop)
    assert command.index(privilege_drop) < command.index(
        "systemctl restart epi-agent.service"
    )
    assert command.index("systemctl restart epi-agent.service") < command.index(
        "http://127.0.0.1:8000/api/health"
    )
    assert "OPENAI_API_KEY" not in command
    assert "AWS_ACCESS_KEY_ID" not in command
    assert "eval " not in command
    assert "bash -c" not in command
    assert "rm -rf" not in command
    assert template["Outputs"]["RecoverStudyAccessDocumentName"]["Value"] == {
        "Ref": "EpiAgentRecoverStudyAccessDocument"
    }
```

- [ ] **Step 2: Add the failing real-shell recovery tests**

Create `tests/test_aws_study_access_recovery.py` with:

```python
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "aws" / "phase2a" / "template.yaml"


class CloudFormationLoader(yaml.SafeLoader):
    """Parse scalar CloudFormation short-form intrinsic functions."""


def _intrinsic(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    value = loader.construct_scalar(node)
    return {"Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}": value}


CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def _recovery_command() -> str:
    template = yaml.load(
        TEMPLATE.read_text(encoding="utf-8"),
        Loader=CloudFormationLoader,
    )
    run_command = template["Resources"]["EpiAgentRecoverStudyAccessDocument"][
        "Properties"
    ]["Content"]["mainSteps"][0]["inputs"]["runCommand"]
    return "\n".join(run_command)


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _recovery_harness(
    tmp_path: Path,
    *,
    invalid_registry: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, Path]:
    study_root = tmp_path / "host" / "srv" / "epi-agent" / "study_data"
    package_root = (
        study_root
        / "studies"
        / "packages"
        / "report-india-synthetic"
        / "0.2.0"
    )
    index_root = package_root / "database" / "index"
    index_root.mkdir(parents=True)
    sentinel = index_root / "sentinel.bin"
    sentinel.write_bytes(b"preserve retained Chroma data")
    sentinel.chmod(0o400)
    index_root.chmod(0o500)
    registry = study_root / "studies" / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "format_version": 1,
                "active": {}
                if invalid_registry
                else {"report-india-synthetic": "0.2.0"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry.chmod(0o000)
    (package_root / "study-package.json").write_text(
        json.dumps(
            {
                "format_version": 2,
                "study_id": "report-india-synthetic",
                "package_version": "0.2.0",
                "database": {"index": "database/index"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    operation_log = tmp_path / "operations.log"
    environment_log = tmp_path / "environment.log"
    service_marker = tmp_path / "service-running"
    fake_python = fake_bin / "current-python"
    _write_executable(
        fake_python,
        "#!/bin/bash\n"
        "set -eu\n"
        f"/usr/bin/env > {environment_log}\n"
        f'exec "{sys.executable}" "$@"\n',
    )

    fake_commands = {
        "id": "printf '%s\\n' 0\n",
        "chown": f"""printf 'chown %s\\n' "$*" >> "{operation_log}"
[ "$1" = -R ] && [ "$2" = -h ] && [ "$3" = epi-agent-web:epi-agent-web ]
shift 3
for target in "$@"; do
  chmod -R u+rwX "$target"
done
""",
        "runuser": f"""printf 'runuser %s\\n' "$*" >> "{operation_log}"
[ "$1" = --user ] && [ "$2" = epi-agent-web ] && [ "$3" = -- ]
shift 3
exec "$@"
""",
        "systemctl": f"""printf 'systemctl %s\\n' "$*" >> "{operation_log}"
case "$1" in
  restart)
    [ "$2" = epi-agent.service ]
    : > "{service_marker}"
    ;;
  is-active)
    [ -f "{service_marker}" ]
    ;;
  status)
    exit 0
    ;;
  *)
    exit 64
    ;;
esac
""",
        "curl": f"""printf 'curl %s\\n' "$*" >> "{operation_log}"
[ -f "{service_marker}" ]
case "$*" in
  *'/api/readiness'*) printf '%s\\n' '{{"status":"ready"}}' ;;
  *) printf '%s\\n' '{{"status":"ok"}}' ;;
esac
""",
        "sleep": ":\n",
    }
    for name, body in fake_commands.items():
        _write_executable(
            fake_bin / name,
            "#!/bin/bash\nset -eu\n" + body,
        )

    source = _recovery_command()
    source = source.replace("/srv/epi-agent/study_data", str(study_root))
    source = source.replace(
        "PATH=/opt/epi-agent/current/.venv/bin:/usr/bin",
        f"PATH={fake_bin}:/usr/bin",
    )
    source = source.replace(
        "/opt/epi-agent/current/.venv/bin/python",
        str(fake_python),
    )
    source = source.replace("/usr/sbin/runuser", str(fake_bin / "runuser"))
    source = source.replace("/usr/bin/python3.12", sys.executable)
    completed = subprocess.run(
        ["bash", "-c", source],
        text=True,
        capture_output=True,
        check=False,
        env=os.environ | {"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )
    return completed, registry, sentinel, operation_log, environment_log


def test_recovery_repairs_access_before_restarting_current_service(
    tmp_path: Path,
) -> None:
    completed, registry, sentinel, operation_log, environment_log = (
        _recovery_harness(tmp_path)
    )

    assert completed.returncode == 0, completed.stderr
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    chown_index = next(
        index for index, value in enumerate(operations) if value.startswith("chown ")
    )
    runuser_index = next(
        index for index, value in enumerate(operations) if value.startswith("runuser ")
    )
    restart_index = operations.index("systemctl restart epi-agent.service")
    health_index = next(
        index
        for index, value in enumerate(operations)
        if value.startswith("curl ") and "/api/health" in value
    )
    assert chown_index < runuser_index < restart_index < health_index
    assert json.loads(registry.read_text(encoding="utf-8"))["active"] == {
        "report-india-synthetic": "0.2.0"
    }
    assert sentinel.read_bytes() == b"preserve retained Chroma data"
    assert registry.stat().st_mode & 0o600 == 0o600
    assert sentinel.stat().st_mode & 0o600 == 0o600
    environment = dict(
        line.split("=", 1)
        for line in environment_log.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert environment["LANG"] == "C.UTF-8"
    assert environment["LC_ALL"] == "C.UTF-8"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["REPORT_AGENT_STUDY_ROOT"] == str(
        registry.parents[1]
    )
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_recovery_validation_failure_prevents_service_restart(tmp_path: Path) -> None:
    completed, registry, sentinel, operation_log, _ = _recovery_harness(
        tmp_path,
        invalid_registry=True,
    )

    assert completed.returncode != 0
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    assert any(value.startswith("chown ") for value in operations)
    assert any(value.startswith("runuser ") for value in operations)
    assert not any(value.startswith("systemctl restart") for value in operations)
    assert json.loads(registry.read_text(encoding="utf-8"))["active"] == {}
    assert sentinel.read_bytes() == b"preserve retained Chroma data"
```

- [ ] **Step 3: Run the focused tests and confirm RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_infrastructure.py::test_phase2a_recovery_document_is_fixed_and_parameter_free \
  tests/test_aws_study_access_recovery.py -q
```

Expected: three failures because `EpiAgentRecoverStudyAccessDocument` and its output do not yet exist.

- [ ] **Step 4: Add the parameter-free recovery SSM document**

Insert this resource after `EpiAgentDeployReleaseDocument` in `infra/aws/phase2a/template.yaml`:

```yaml
  EpiAgentRecoverStudyAccessDocument:
    Type: AWS::SSM::Document
    Properties:
      DocumentType: Command
      DocumentFormat: YAML
      Name: epi-agent-recover-study-access
      UpdateMethod: NewVersion
      Content:
        schemaVersion: '2.2'
        description: Restore retained Epi Agent study access before release deployment.
        mainSteps:
          - action: aws:runShellScript
            name: recoverStudyAccess
            inputs:
              runCommand:
                - |
                  set -Eeuo pipefail
                  readonly study_root=/srv/epi-agent/study_data
                  readonly current_python=/opt/epi-agent/current/.venv/bin/python
                  readonly recovery_deadline_seconds=120
                  [ "$(id -u)" -eq 0 ]
                  [ -d "$study_root" ]
                  [ -x "$current_python" ]
                  chown -R -h epi-agent-web:epi-agent-web "$study_root"
                  /usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i \
                    PATH=/opt/epi-agent/current/.venv/bin:/usr/bin \
                    LANG=C.UTF-8 \
                    LC_ALL=C.UTF-8 \
                    PYTHONUTF8=1 \
                    REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data \
                    "$current_python" - "$study_root" <<'PY'
                  import json
                  import os
                  from pathlib import Path, PurePosixPath
                  import re
                  import sys

                  study_root = Path(sys.argv[1])
                  registry_path = study_root / "studies" / "registry.json"
                  if not os.access(registry_path, os.R_OK | os.W_OK):
                      raise SystemExit("study registry is not readable and writable")
                  try:
                      registry = json.loads(registry_path.read_text(encoding="utf-8"))
                  except (OSError, ValueError) as error:
                      raise SystemExit(f"study registry is invalid: {error}") from error
                  active = registry.get("active")
                  if not isinstance(active, dict) or not active:
                      raise SystemExit("study registry has no active package")
                  safe_token = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
                  for study_id, package_version in active.items():
                      if not isinstance(study_id, str) or not safe_token.fullmatch(study_id):
                          raise SystemExit("active study ID is invalid")
                      if not isinstance(package_version, str) or not safe_token.fullmatch(
                          package_version
                      ):
                          raise SystemExit("active study version is invalid")
                      package_root = (
                          study_root
                          / "studies"
                          / "packages"
                          / study_id
                          / package_version
                      )
                      manifest_path = package_root / "study-package.json"
                      try:
                          manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                      except (OSError, ValueError) as error:
                          raise SystemExit(
                              f"active study manifest is invalid: {error}"
                          ) from error
                      if (
                          manifest.get("study_id") != study_id
                          or manifest.get("package_version") != package_version
                      ):
                          raise SystemExit("active study manifest does not match registry")
                      database = manifest.get("database")
                      index_value = database.get("index") if isinstance(database, dict) else None
                      if not isinstance(index_value, str):
                          raise SystemExit("active study index path is missing")
                      index_relative = PurePosixPath(index_value)
                      if (
                          index_relative.is_absolute()
                          or not index_relative.parts
                          or any(part in {"", ".", ".."} for part in index_relative.parts)
                      ):
                          raise SystemExit("active study index path is unsafe")
                      index_path = package_root.joinpath(*index_relative.parts)
                      if not index_path.is_dir() or not os.access(
                          index_path,
                          os.R_OK | os.W_OK | os.X_OK,
                      ):
                          raise SystemExit(
                              "active study index is not readable, writable, and searchable"
                          )
                  PY
                  systemctl restart epi-agent.service
                  recovery_deadline=$((SECONDS + recovery_deadline_seconds))
                  until curl --fail --silent --show-error --max-time 5 \
                    http://127.0.0.1:8000/api/health >/dev/null \
                    && curl --fail --silent --show-error --max-time 5 \
                    http://127.0.0.1:8000/api/readiness \
                    | /usr/bin/python3.12 -c 'import json, sys; raise SystemExit(0 if json.load(sys.stdin).get("status") == "ready" else 1)'; do
                    if ! systemctl is-active --quiet epi-agent.service; then
                      systemctl status epi-agent.service --no-pager >&2 || true
                      exit 1
                    fi
                    [ "$SECONDS" -lt "$recovery_deadline" ] || exit 1
                    sleep 2
                  done
```

Add this output beside the existing deployment-document output:

```yaml
  RecoverStudyAccessDocumentName:
    Description: Parameter-free SSM document for retained study access recovery
    Value: !Ref EpiAgentRecoverStudyAccessDocument
```

- [ ] **Step 5: Run the focused tests and confirm GREEN**

Run the command from Step 3.

Expected: `3 passed` with no warnings or errors.

- [ ] **Step 6: Extend the executable template smoke**

In `scripts/smoke_aws_phase2a_template_regressions.py`, immediately after
`phase2a = _load("infra/aws/phase2a/template.yaml")`, add:

```python
    recovery = phase2a["Resources"]["EpiAgentRecoverStudyAccessDocument"]["Properties"]
    if recovery["Name"] != "epi-agent-recover-study-access":
        raise AssertionError("study access recovery document name changed")
    if recovery.get("UpdateMethod") != "NewVersion":
        raise AssertionError("study access recovery updates must create a new version")
    recovery_content = recovery["Content"]
    if "parameters" in recovery_content:
        raise AssertionError("study access recovery must remain parameter-free")
    recovery_command = "\n".join(
        recovery_content["mainSteps"][0]["inputs"]["runCommand"]
    )
    recovery_order = (
        "chown -R -h epi-agent-web:epi-agent-web",
        "/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i",
        "systemctl restart epi-agent.service",
        "http://127.0.0.1:8000/api/health",
        "http://127.0.0.1:8000/api/readiness",
    )
    positions = [recovery_command.index(token) for token in recovery_order]
    if positions != sorted(positions):
        raise AssertionError("study access recovery command order is unsafe")
    if any(token in recovery_command for token in ("eval ", "bash -c", "rm -rf")):
        raise AssertionError("study access recovery contains an unsafe shell operation")
    if phase2a["Outputs"]["RecoverStudyAccessDocumentName"]["Value"] != {
        "Ref": "EpiAgentRecoverStudyAccessDocument"
    }:
        raise AssertionError("study access recovery output does not match its document")
```

- [ ] **Step 7: Run Task 1 verification**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_infrastructure.py tests/test_aws_study_access_recovery.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_phase2a_template_regressions.py
bash -n deploy/aws/bin/install-release.sh deploy/aws/bin/install-study.sh \
  deploy/aws/bin/epi-agent-python-worker
git diff --check
```

Expected: all tests and smoke checks exit `0`; shell syntax and diff checks produce no errors. No AWS command is run.

- [ ] **Step 8: Review and commit Task 1**

Run:

```bash
git diff -- infra/aws/phase2a/template.yaml tests/test_aws_infrastructure.py \
  tests/test_aws_study_access_recovery.py \
  scripts/smoke_aws_phase2a_template_regressions.py
git add -f infra/aws/phase2a/template.yaml tests/test_aws_infrastructure.py \
  tests/test_aws_study_access_recovery.py \
  scripts/smoke_aws_phase2a_template_regressions.py
git commit -m "feat: add AWS study access recovery document"
```

Expected: the commit contains only the recovery document, its output, static and real-shell tests, and the existing executable template smoke.

### Task 2: Add the guarded operator command and corrected runbook order

**Files:**
- Modify: `scripts/aws_phase2a.py`
- Modify: `tests/test_aws_phase2a_cli.py`
- Modify: `docs/aws/phase2a-runbook.md`

**Interfaces:**
- Consumes: stack outputs `ApplicationInstanceId` and `RecoverStudyAccessDocumentName` from Task 1.
- Produces: `wait_for_ssm_command(r, command, instance, operation) -> None`, `recover_study_access(r, confirm) -> None`, and CLI command `recover-study-access --confirm-instance INSTANCE_ID`.

- [ ] **Step 1: Add the failing recovery CLI tests**

Append these tests to `tests/test_aws_phase2a_cli.py`:

```python
def test_recover_study_access_uses_stack_document_without_parameters(monkeypatch):
 import subprocess
 calls=[]
 class SequenceRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   if "get-caller-identity" in argv:return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
   if "send-command" in argv:return subprocess.CompletedProcess(argv,0,'{"Command":{"CommandId":"new-recovery-command"}}',"")
   if "get-command-invocation" in argv:return subprocess.CompletedProcess(argv,0,'{"Status":"Success"}',"")
   raise AssertionError(argv)
 monkeypatch.setattr(cli,"outputs",lambda r:{"ApplicationInstanceId":"i-0f9ed9c133ea2358b","RecoverStudyAccessDocumentName":"epi-agent-recover-study-access"})
 cli.recover_study_access(SequenceRunner(),"i-0f9ed9c133ea2358b")
 sent=next(call for call in calls if "send-command" in call)
 assert sent[sent.index("--document-name")+1]=="epi-agent-recover-study-access"
 assert sent[sent.index("--instance-ids")+1]=="i-0f9ed9c133ea2358b"
 assert "--parameters" not in sent
 polled=next(call for call in calls if "get-command-invocation" in call)
 assert polled[polled.index("--command-id")+1]=="new-recovery-command"


def test_recover_study_access_rejects_wrong_instance_before_send(monkeypatch):
 import pytest, subprocess
 calls=[]
 class IdentityRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
 monkeypatch.setattr(cli,"outputs",lambda r:{"ApplicationInstanceId":"i-0f9ed9c133ea2358b","RecoverStudyAccessDocumentName":"epi-agent-recover-study-access"})
 with pytest.raises(cli.OperatorError,match="confirm the exact stack instance ID"):
  cli.recover_study_access(IdentityRunner(),"i-wrong")
 assert not any("send-command" in call for call in calls)


def test_recover_study_access_fails_closed_on_new_terminal_failure(monkeypatch):
 import pytest, subprocess
 calls=[]
 class FailedRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   if "get-caller-identity" in argv:return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
   if "send-command" in argv:return subprocess.CompletedProcess(argv,0,'{"Command":{"CommandId":"failed-new-command"}}',"")
   if "get-command-invocation" in argv:return subprocess.CompletedProcess(argv,0,'{"Status":"Failed"}',"")
   raise AssertionError(argv)
 monkeypatch.setattr(cli,"outputs",lambda r:{"ApplicationInstanceId":"i-0f9ed9c133ea2358b","RecoverStudyAccessDocumentName":"epi-agent-recover-study-access"})
 with pytest.raises(cli.OperatorError,match="study access recovery command did not succeed"):
  cli.recover_study_access(FailedRunner(),"i-0f9ed9c133ea2358b")
 assert sum("send-command" in call for call in calls)==1
 assert sum("get-command-invocation" in call for call in calls)==1
```

Extend `test_mutating_helpers_guard_identity_first` so its tuple includes
`"recover_study_access("`. Add this parser contract test:

```python
def test_recover_study_access_parser_requires_exact_instance_confirmation():
 s=_source(); start=s.index('s.add_parser("recover-study-access")'); end=s.index('q=s.add_parser("plan-stack")'); section=s[start:end]
 assert 'q.add_argument("--confirm-instance",required=True)' in section
```

- [ ] **Step 2: Run the focused CLI tests and confirm RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_phase2a_cli.py -q
```

Expected: the new tests fail because `recover_study_access` and its parser do not exist.

- [ ] **Step 3: Extract shared SSM polling and add the recovery command**

In `scripts/aws_phase2a.py`, add this helper before `deploy`:

```python
def wait_for_ssm_command(r:Runner,command:str,instance:str,operation:str)->None:
 deadline=time.monotonic()+DEPLOY_TIMEOUT_SECONDS; last_status="pending"
 while time.monotonic()<deadline:
  try: result=run_json(r,aws("ssm","get-command-invocation","--command-id",command,"--instance-id",instance))
  except OperatorError as error:
   if "InvocationDoesNotExist" in str(error): time.sleep(POLL_INTERVAL_SECONDS); continue
   raise
  status=result.get("Status"); last_status=status or last_status
  if status=="Success": return
  if status in {"Failed","TimedOut","Cancelled"}: raise OperatorError(f"{operation} command did not succeed")
  time.sleep(POLL_INTERVAL_SECONDS)
 raise OperatorError(f"{operation} command {command} timed out with last status {last_status}")
```

Replace the polling block at the end of `deploy` with:

```python
 wait_for_ssm_command(r,command,instance,"deployment")
```

Add this function immediately after `deploy`:

```python
def recover_study_access(r:Runner,confirm:str)->None:
 require_expected_identity(r); o=outputs(r); document=o.get("RecoverStudyAccessDocumentName",""); instance=o.get("ApplicationInstanceId","")
 if not document or not instance: raise OperatorError("required recovery outputs missing")
 if confirm!=instance: raise OperatorError("confirm the exact stack instance ID")
 sent=run_json(r,aws("ssm","send-command","--document-name",document,"--instance-ids",instance)); command=sent["Command"]["CommandId"]
 wait_for_ssm_command(r,command,instance,"study access recovery")
```

In `main`, add the parser directly after the lifecycle parsers:

```python
 q=s.add_parser("recover-study-access"); q.add_argument("--confirm-instance",required=True)
```

Add this dispatch directly after lifecycle dispatch:

```python
  if a.cmd=="recover-study-access": recover_study_access(r,a.confirm_instance); return 0
```

- [ ] **Step 4: Run the focused CLI tests and confirm GREEN**

Run the command from Step 2.

Expected: every CLI test passes with no warnings or errors.

- [ ] **Step 5: Document the corrected operator order**

In `docs/aws/phase2a-runbook.md`, replace the paragraph beginning “Upload immutable packages” with:

```markdown
Upload immutable packages with `upload-release RELEASE releases/ID.tar.gz` and
`upload-study STUDY studies/ID.tar.gz`. When retained study permissions prevent
the current application from starting, first create, inspect, approve, and
execute the change set that adds `epi-agent-recover-study-access`. Start the
exact stack instance, invoke `recover-study-access` with its exact instance
confirmation, and require SSM `Success` plus healthy local endpoints before
invoking `deploy-release`. Never deploy first and plan to repair the retained
study afterward: the release installer's current-service gate will fail closed.
In **CloudWatch → Alarms/Logs**, check status, CPU, credits, memory, disk, and
service errors.
```

In the “Exact reviewed actions” command block, insert this line immediately
before `deploy-release`:

```bash
python scripts/aws_phase2a.py recover-study-access --confirm-instance i-0f9ed9c133ea2358b
```

After that block, add:

```markdown
`recover-study-access` is a separately authorized, parameter-free recovery
operation. Run it only after its CloudFormation change set is complete and the
instance is SSM Online. Record its new command ID; on any non-success terminal
state, inspect that invocation once, do not retry the command ID, stop the
instance, and return to source diagnosis.
```

- [ ] **Step 6: Run Task 2 and combined verification**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest \
  tests/test_aws_phase2a_cli.py tests/test_aws_infrastructure.py \
  tests/test_aws_study_access_recovery.py tests/test_aws_study_installer.py \
  tests/test_aws_host_assets.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_study_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python \
  scripts/smoke_aws_release_installer.py
bash -n deploy/aws/bin/install-release.sh deploy/aws/bin/install-study.sh \
  deploy/aws/bin/epi-agent-python-worker
git diff --check
```

Expected: all pytest and smoke commands exit `0`; shell syntax and diff checks report no errors. No AWS command is run.

- [ ] **Step 7: Review and commit Task 2**

Run:

```bash
git diff -- scripts/aws_phase2a.py tests/test_aws_phase2a_cli.py \
  docs/aws/phase2a-runbook.md
git add -f scripts/aws_phase2a.py tests/test_aws_phase2a_cli.py \
  docs/aws/phase2a-runbook.md
git commit -m "feat: guard AWS study access recovery"
```

Expected: the commit contains only the guarded CLI command, shared SSM polling refactor, CLI regressions, and corrected runbook sequence.

## Post-Implementation Review and AWS Gate

After both tasks pass task review, whole-feature review, and fresh controller
verification, stop. Reconfirm EC2 is `stopped`, the root volume is
`vol-0cb3e1bb3af5e4245`, and the retained data volume is
`vol-043235ec767877900`.

The next AWS action is only to create a CloudFormation change set from the
reviewed template. Creating or executing that change set, building or uploading
the next release, starting EC2, invoking recovery, deploying, reinstalling the
study, and restarting the live service all remain outside this implementation
authorization. Present the exact proposed change set and request explicit user
authorization before execution.
