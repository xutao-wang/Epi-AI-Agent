# Guarded AWS Study Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one account- and instance-guarded AWS command that installs, activates, and verifies an immutable checksummed study package through the existing production installer.

**Architecture:** Extend the Phase 2A operator CLI with `install-study`, backed by a stack-provided `epi-agent-install-study` SSM Command document. The SSM document delegates package validation and activation to the existing `install-study.sh`/`study_installer.py` boundary, then requires service health, readiness, active-registry identity, and installed archive checksum before reporting success.

**Tech Stack:** Python 3.12, pytest 9, AWS CLI, CloudFormation YAML, SSM Command documents, Bash, systemd, FastAPI health/readiness routes, S3 checksum metadata, and the existing versioned study-package installer.

## Global Constraints

- Do not duplicate study archive, manifest, DuckDB, catalog, Chroma, knowledge, Markdown, immutability, or registry validation outside the existing installer.
- Keep application releases and study packages as separate S3 artifacts and separate authorized operations.
- Use only stack outputs for bucket, document, and target instance; require the exact stack instance as operator confirmation.
- Never interpolate operator input into shell source; every SSM parameter uses `interpolationType: ENV_VAR` plus an AWS-compatible `allowedPattern`.
- Preserve all installed study versions. Installing `0.3.0` must not remove `0.2.0`.
- Print and poll only the new SSM command ID; do not retry failed or timed-out commands automatically.
- Do not create or execute a CloudFormation change set, upload an artifact, send an SSM command, restart AWS services, or otherwise mutate AWS during implementation and local verification.
- Use `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python` for Python commands in the isolated worktree.
- Modify source only in the existing isolated `aws-test` worktree.

---

## File Structure

- Modify: `scripts/aws_phase2a.py` — validates operator input, resolves fixed stack targets, verifies S3 checksum metadata, sends the install document, and polls its command ID.
- Modify: `tests/test_aws_phase2a_cli.py` — specifies CLI parsing, validation, AWS call ordering, fixed targeting, checksum metadata, polling, and failure behavior.
- Modify: `infra/aws/phase2a/template.yaml` — defines the constrained install-study SSM document and stack output.
- Modify: `tests/test_aws_infrastructure.py` — specifies document parameters, fixed command order, service-user verification, health/readiness, and output wiring.
- Modify: `scripts/smoke_aws_phase2a_template_regressions.py` — protects AWS-compatible patterns and the fixed install command order outside CloudFormation lint.
- Create: `scripts/smoke_aws_guarded_study_install.py` — executable feature smoke for the real CLI/template/installer contract without live AWS mutation.
- Modify: `docs/aws/phase2a-runbook.md` — documents upload, install, command-ID capture, failure handling, and explicit rollback.

### Task 1: Add the account-guarded operator CLI command

**Files:**
- Modify: `tests/test_aws_phase2a_cli.py`
- Modify: `scripts/aws_phase2a.py`

**Interfaces:**
- Produces: `install_study(r: Runner, key: str, sha: str, study_id: str, version: str, confirm: str) -> None`.
- Consumes stack outputs: `ApplicationBucketName`, `ApplicationInstanceId`, and `InstallStudyDocumentName`.
- Sends SSM parameters: `Bucket`, `StudyKey`, `StudySha256`, `StudyId`, and `PackageVersion`.

Add `import json` beside the existing test-module imports before adding the
contracts below.

- [ ] **Step 1: Add failing input and parser contracts**

Append tests that call `install_study` with invalid values and assert no runner calls occur:

```python
@pytest.mark.parametrize(
    ("key", "sha", "study_id", "version"),
    [
        ("releases/study.tar.gz", "a" * 64, "study", "1.0.0"),
        ("studies/../study.tar.gz", "a" * 64, "study", "1.0.0"),
        ("studies/study.tar.gz", "A" * 64, "study", "1.0.0"),
        ("studies/study.tar.gz", "a" * 63, "study", "1.0.0"),
        ("studies/study.tar.gz", "a" * 64, "Study", "1.0.0"),
        ("studies/study.tar.gz", "a" * 64, "study", "version/one"),
    ],
)
def test_install_study_rejects_invalid_input_before_aws(
    key: str,
    sha: str,
    study_id: str,
    version: str,
) -> None:
    runner = R("{}")

    with pytest.raises(cli.OperatorError, match="invalid study installation input"):
        cli.install_study(runner, key, sha, study_id, version, "i-confirmed")

    assert runner.calls == []


def test_install_study_parser_requires_exact_instance_confirmation() -> None:
    source = _source()
    start = source.index('q=s.add_parser("install-study")')
    end = source.index('q=s.add_parser("plan-stack")')
    section = source[start:end]

    assert 'q.add_argument("key")' in section
    assert 'q.add_argument("sha")' in section
    assert 'q.add_argument("study_id")' in section
    assert 'q.add_argument("version")' in section
    assert 'q.add_argument("--confirm-instance",required=True)' in section
```

- [ ] **Step 2: Run the new input contracts and verify RED**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' -m pytest \
  tests/test_aws_phase2a_cli.py::test_install_study_rejects_invalid_input_before_aws \
  tests/test_aws_phase2a_cli.py::test_install_study_parser_requires_exact_instance_confirmation -q
```

Expected: collection succeeds and tests fail because `install_study` and the parser entry do not exist.

- [ ] **Step 3: Add failing fixed-target and checksum-metadata contracts**

Add a sequenced runner test that returns identity, S3 metadata, a new SSM command ID, and terminal success. Assert:

```python
def test_install_study_uses_stack_targets_and_verified_object(monkeypatch, capsys) -> None:
    import subprocess

    calls: list[list[str]] = []
    events: list[object] = []

    class SequenceRunner:
        def run(self, argv, *, capture_output=True):
            calls.append(list(argv))
            if "get-caller-identity" in argv:
                events.append("identity")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    '{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',
                    "",
                )
            if "head-object" in argv:
                events.append("head")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    '{"Metadata":{"sha256":"' + "a" * 64 + '"}}',
                    "",
                )
            if "send-command" in argv:
                events.append("send")
                return subprocess.CompletedProcess(
                    argv, 0, '{"Command":{"CommandId":"new-study-command"}}', ""
                )
            if "get-command-invocation" in argv:
                events.append("poll")
                return subprocess.CompletedProcess(argv, 0, '{"Status":"Success"}', "")
            raise AssertionError(argv)

    def fixed_outputs(_runner):
        events.append("outputs")
        return {
            "ApplicationBucketName": "stack-bucket",
            "ApplicationInstanceId": "i-stack",
            "InstallStudyDocumentName": "epi-agent-install-study",
        }

    monkeypatch.setattr(cli, "outputs", fixed_outputs)

    cli.install_study(
        SequenceRunner(),
        "studies/report-india-synthetic-0.3.0.tar.gz",
        "a" * 64,
        "report-india-synthetic",
        "0.3.0",
        "i-stack",
    )

    head = next(call for call in calls if "head-object" in call)
    assert head[head.index("--bucket") + 1] == "stack-bucket"
    assert head[head.index("--key") + 1] == "studies/report-india-synthetic-0.3.0.tar.gz"
    sent = next(call for call in calls if "send-command" in call)
    assert sent[sent.index("--document-name") + 1] == "epi-agent-install-study"
    assert sent[sent.index("--instance-ids") + 1] == "i-stack"
    parameters = json.loads(sent[sent.index("--parameters") + 1])
    assert parameters == {
        "Bucket": ["stack-bucket"],
        "StudyKey": ["studies/report-india-synthetic-0.3.0.tar.gz"],
        "StudySha256": ["a" * 64],
        "StudyId": ["report-india-synthetic"],
        "PackageVersion": ["0.3.0"],
    }
    polled = next(call for call in calls if "get-command-invocation" in call)
    assert polled[polled.index("--command-id") + 1] == "new-study-command"
    assert events == ["identity", "outputs", "head", "send", "poll"]
    assert capsys.readouterr().out == "study installation command ID: new-study-command\n"
```

Add these explicit failure contracts:

```python
def test_install_study_rejects_wrong_instance_before_object_or_send(monkeypatch) -> None:
    import subprocess

    calls: list[list[str]] = []

    class IdentityRunner:
        def run(self, argv, *, capture_output=True):
            calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv,
                0,
                '{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',
                "",
            )

    monkeypatch.setattr(
        cli,
        "outputs",
        lambda _runner: {
            "ApplicationBucketName": "stack-bucket",
            "ApplicationInstanceId": "i-stack",
            "InstallStudyDocumentName": "epi-agent-install-study",
        },
    )
    with pytest.raises(cli.OperatorError, match="confirm the exact stack instance ID"):
        cli.install_study(
            IdentityRunner(),
            "studies/report-india-synthetic-0.3.0.tar.gz",
            "a" * 64,
            "report-india-synthetic",
            "0.3.0",
            "i-wrong",
        )
    assert not any("head-object" in call or "send-command" in call for call in calls)


@pytest.mark.parametrize("metadata", [{}, {"sha256": "b" * 64}])
def test_install_study_rejects_missing_or_mismatched_object_checksum(
    monkeypatch,
    metadata: dict[str, str],
) -> None:
    import subprocess

    calls: list[list[str]] = []

    class MetadataRunner:
        def run(self, argv, *, capture_output=True):
            calls.append(list(argv))
            if "get-caller-identity" in argv:
                payload = '{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}'
            elif "head-object" in argv:
                payload = json.dumps({"Metadata": metadata})
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, payload, "")

    monkeypatch.setattr(
        cli,
        "outputs",
        lambda _runner: {
            "ApplicationBucketName": "stack-bucket",
            "ApplicationInstanceId": "i-stack",
            "InstallStudyDocumentName": "epi-agent-install-study",
        },
    )
    with pytest.raises(
        cli.OperatorError,
        match="study object checksum metadata does not match",
    ):
        cli.install_study(
            MetadataRunner(),
            "studies/report-india-synthetic-0.3.0.tar.gz",
            "a" * 64,
            "report-india-synthetic",
            "0.3.0",
            "i-stack",
        )
    assert not any("send-command" in call for call in calls)
```

Add `"install_study("` to the helper names checked by
`test_mutating_helpers_guard_identity_first` so the new mutation must retain
the pinned identity guard.

Add a terminal-failure contract using `@pytest.mark.parametrize` over
`["Failed", "TimedOut", "Cancelled"]`. Its runner returns valid identity,
matching S3 metadata, one command ID, and the parameterized terminal status.
The test must assert `OperatorError` contains
`"study installation command did not succeed"`, exactly one `send-command`,
exactly one `get-command-invocation`, and the printed command ID. Add a timeout
contract whose monotonic clock advances past `DEPLOY_TIMEOUT_SECONDS`; assert
one send, one poll, one sleep, the printed command ID, and an `OperatorError`
containing `"study installation command <id> timed out"`. These tests use the
same complete stack-output fixture and matching S3 metadata shown above; they
must not alter `wait_for_ssm_command`.

- [ ] **Step 4: Run the new targeting contracts and verify RED**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' -m pytest tests/test_aws_phase2a_cli.py -k 'install_study' -q
```

Expected: failures identify the missing command implementation.

- [ ] **Step 5: Implement the minimal operator command**

Add safe regex constants and this function to `scripts/aws_phase2a.py`, preserving the file's existing compact style:

```python
STUDY_KEY_PATTERN = re.compile(
    r"^studies/[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz$"
)
STUDY_TOKEN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def install_study(
    r: Runner,
    key: str,
    sha: str,
    study_id: str,
    version: str,
    confirm: str,
) -> None:
    if (
        not STUDY_KEY_PATTERN.fullmatch(key)
        or ".." in key
        or not re.fullmatch(r"[0-9a-f]{64}", sha)
        or not STUDY_TOKEN_PATTERN.fullmatch(study_id)
        or not STUDY_TOKEN_PATTERN.fullmatch(version)
    ):
        raise OperatorError("invalid study installation input")
    require_expected_identity(r)
    stack_outputs = outputs(r)
    bucket = stack_outputs.get("ApplicationBucketName", "")
    instance = stack_outputs.get("ApplicationInstanceId", "")
    document = stack_outputs.get("InstallStudyDocumentName", "")
    if not bucket or not instance or not document:
        raise OperatorError("required study installation outputs missing")
    if confirm != instance:
        raise OperatorError("confirm the exact stack instance ID")
    remote = run_json(
        r,
        aws("s3api", "head-object", "--bucket", bucket, "--key", key),
    )
    if (remote.get("Metadata") or {}).get("sha256") != sha:
        raise OperatorError("study object checksum metadata does not match")
    parameters = json.dumps(
        {
            "Bucket": [bucket],
            "StudyKey": [key],
            "StudySha256": [sha],
            "StudyId": [study_id],
            "PackageVersion": [version],
        }
    )
    sent = run_json(
        r,
        aws(
            "ssm",
            "send-command",
            "--document-name",
            document,
            "--instance-ids",
            instance,
            "--parameters",
            parameters,
        ),
    )
    command = sent["Command"]["CommandId"]
    print(f"study installation command ID: {command}", flush=True)
    wait_for_ssm_command(r, command, instance, "study installation")
```

Add the parser and dispatch entries:

```python
q=s.add_parser("install-study")
q.add_argument("key")
q.add_argument("sha")
q.add_argument("study_id")
q.add_argument("version")
q.add_argument("--confirm-instance",required=True)
```

```python
if a.cmd=="install-study":
    install_study(r,a.key,a.sha,a.study_id,a.version,a.confirm_instance)
    return 0
```

- [ ] **Step 6: Verify GREEN and the complete operator suite**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' -m pytest tests/test_aws_phase2a_cli.py -q
```

Expected: every operator CLI test passes.

- [ ] **Step 7: Commit the operator boundary**

```bash
git diff --check -- scripts/aws_phase2a.py tests/test_aws_phase2a_cli.py
git add -f scripts/aws_phase2a.py tests/test_aws_phase2a_cli.py
git commit -m "feat: guard AWS study installation command"
```

### Task 2: Add the fixed SSM installation document

**Files:**
- Modify: `tests/test_aws_infrastructure.py`
- Modify: `infra/aws/phase2a/template.yaml`
- Modify: `scripts/smoke_aws_phase2a_template_regressions.py`

**Interfaces:**
- Produces stack output: `InstallStudyDocumentName`.
- Consumes SSM environment variables: `SSM_Bucket`, `SSM_StudyKey`, `SSM_StudySha256`, `SSM_StudyId`, and `SSM_PackageVersion`.
- Delegates to: `/usr/local/sbin/install-study.sh <bucket> <key> <sha> <study-id> <version>`.

- [ ] **Step 1: Add the failing infrastructure contract**

Add `test_phase2a_install_study_document_is_constrained_and_verified` to `tests/test_aws_infrastructure.py`. It must parse `EpiAgentInstallStudyDocument` and assert:

```python
assert document["Type"] == "AWS::SSM::Document"
assert properties["Name"] == "epi-agent-install-study"
assert properties["DocumentType"] == "Command"
assert properties["DocumentFormat"] == "YAML"
assert properties["UpdateMethod"] == "NewVersion"
assert set(content["parameters"]) == {
    "Bucket",
    "StudyKey",
    "StudySha256",
    "StudyId",
    "PackageVersion",
}
assert all(
    parameter["interpolationType"] == "ENV_VAR"
    for parameter in content["parameters"].values()
)
```

Join the single run command and assert ordered tokens:

```python
order = (
    '/usr/local/sbin/install-study.sh "$SSM_Bucket" "$SSM_StudyKey"',
    "systemctl restart epi-agent.service",
    "http://127.0.0.1:8000/api/health",
    "http://127.0.0.1:8000/api/readiness",
    "/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i",
    "load_installed_study",
    "installed.archive_sha256 != expected_sha256",
)
positions = [command.index(token) for token in order]
assert positions == sorted(positions)
```

Also assert the fixed study root, fixed current-release Python, `REPORT_AGENT_STUDY_ROOT`, registry active-version check, service-inactive failure, bounded deadline, absence of `eval`, absence of `bash -c`, absence of deletion commands, and:

```python
assert template["Outputs"]["InstallStudyDocumentName"]["Value"] == {
    "Ref": "EpiAgentInstallStudyDocument"
}
```

- [ ] **Step 2: Run the infrastructure contract and verify RED**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' -m pytest \
  tests/test_aws_infrastructure.py::test_phase2a_install_study_document_is_constrained_and_verified -q
```

Expected: FAIL because `EpiAgentInstallStudyDocument` does not exist.

- [ ] **Step 3: Add AWS-compatible pattern checks to the template smoke**

Extend `scripts/smoke_aws_phase2a_template_regressions.py` to load the new document and require:

```python
install = phase2a["Resources"]["EpiAgentInstallStudyDocument"]["Properties"]
if install["Name"] != "epi-agent-install-study":
    raise AssertionError("study installation document name changed")
if install.get("UpdateMethod") != "NewVersion":
    raise AssertionError("study installation updates must create a new version")
install_parameters = install["Content"]["parameters"]
for name, parameter in install_parameters.items():
    if parameter.get("interpolationType") != "ENV_VAR":
        raise AssertionError(f"{name} does not export its SSM environment variable")
    if any(token in parameter["allowedPattern"] for token in unsupported):
        raise AssertionError(f"{name} uses lookaround unsupported by AWS SSM")
study_key = re.compile(install_parameters["StudyKey"]["allowedPattern"])
if not study_key.fullmatch("studies/report-india-synthetic-0.3.0.tar.gz"):
    raise AssertionError("study-key pattern rejects the intended immutable package")
for unsafe in ("studies/../secret", "studies/package..tar.gz", "studies//package.tar.gz"):
    if study_key.fullmatch(unsafe):
        raise AssertionError(f"study-key pattern accepts unsafe key {unsafe!r}")
```

Assert the same install/restart/health/readiness/service-user/post-verification order as the pytest contract.

- [ ] **Step 4: Run the template smoke and verify RED**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' scripts/smoke_aws_phase2a_template_regressions.py
```

Expected: FAIL because the install document is absent.

- [ ] **Step 5: Implement the SSM document and output**

Add `EpiAgentInstallStudyDocument` beside the existing release/recovery documents. Define the five parameter schemas with these allowed patterns:

```yaml
Bucket: '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
StudyKey: '^studies/[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz$'
StudySha256: '^[0-9a-f]{64}$'
StudyId: '^[a-z0-9][a-z0-9._-]*$'
PackageVersion: '^[a-z0-9][a-z0-9._-]*$'
```

The run command must explicitly enter Bash and perform:

```bash
exec /usr/bin/bash -Eeuo pipefail <<'BASH'
readonly study_root=/srv/epi-agent/study_data
readonly current_python=/opt/epi-agent/current/.venv/bin/python
readonly install_deadline_seconds=120

/usr/local/sbin/install-study.sh \
  "$SSM_Bucket" \
  "$SSM_StudyKey" \
  "$SSM_StudySha256" \
  "$SSM_StudyId" \
  "$SSM_PackageVersion"

systemctl restart epi-agent.service
install_deadline=$((SECONDS + install_deadline_seconds))
while :; do
  remaining_seconds=$((install_deadline - SECONDS))
  [ "$remaining_seconds" -gt 0 ] || exit 1
  request_timeout=$((remaining_seconds < 5 ? remaining_seconds : 5))
  if curl --fail --silent --show-error --max-time "$request_timeout" \
    http://127.0.0.1:8000/api/health >/dev/null; then
    readiness=$(curl --fail --silent --show-error --max-time "$request_timeout" \
      http://127.0.0.1:8000/api/readiness)
    if printf '%s' "$readiness" | /usr/bin/python3.12 -c \
      'import json, sys; raise SystemExit(0 if json.load(sys.stdin).get("status") == "ready" else 1)'; then
      break
    fi
  fi
  systemctl is-active --quiet epi-agent.service
  remaining_seconds=$((install_deadline - SECONDS))
  [ "$remaining_seconds" -gt 0 ] || exit 1
  sleep_seconds=$((remaining_seconds < 2 ? remaining_seconds : 2))
  sleep "$sleep_seconds"
done

cd /opt/epi-agent/current
/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i \
  PATH=/opt/epi-agent/current/.venv/bin:/usr/bin \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONUTF8=1 \
  REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data \
  "$current_python" - "$study_root" "$SSM_StudyId" \
  "$SSM_PackageVersion" "$SSM_StudySha256" <<'PY'
from pathlib import Path
import sys

from study_package.installer import load_installed_study
from study_package.registry import load_registry, package_root

study_root = Path(sys.argv[1])
study_id = sys.argv[2]
package_version = sys.argv[3]
expected_sha256 = sys.argv[4]
studies_root = study_root / "studies"
registry = load_registry(studies_root)
if registry.active.get(study_id) != package_version:
    raise SystemExit("installed study version is not active")
installed = load_installed_study(
    package_root(studies_root, study_id, package_version)
)
if (installed.study_id, installed.package_version) != (study_id, package_version):
    raise SystemExit("installed study identity does not match request")
if installed.archive_sha256 != expected_sha256:
    raise SystemExit("installed study archive checksum does not match request")
PY
BASH
```

Add output:

```yaml
InstallStudyDocumentName:
  Description: Name of the SSM Command document used to install study packages.
  Value: !Ref EpiAgentInstallStudyDocument
```

- [ ] **Step 6: Verify GREEN, lint, and the complete infrastructure suite**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' -m pytest tests/test_aws_infrastructure.py -q
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' scripts/smoke_aws_phase2a_template_regressions.py
uvx --from cfn-lint==1.53.1 cfn-lint \
  infra/aws/bootstrap/template.yaml infra/aws/phase2a/template.yaml
```

Expected: all commands exit `0`; the smoke prints
`Phase 2A deployment-template regression smoke passed`.

- [ ] **Step 7: Commit the SSM boundary**

```bash
git diff --check -- infra/aws/phase2a/template.yaml tests/test_aws_infrastructure.py scripts/smoke_aws_phase2a_template_regressions.py
git add infra/aws/phase2a/template.yaml
git add -f tests/test_aws_infrastructure.py scripts/smoke_aws_phase2a_template_regressions.py
git commit -m "feat: add guarded AWS study install document"
```

### Task 3: Add the dedicated smoke and operator runbook

**Files:**
- Create: `scripts/smoke_aws_guarded_study_install.py`
- Modify: `docs/aws/phase2a-runbook.md`

**Interfaces:**
- Smoke runs deterministic production-boundary contracts only; it never accepts `--allow-live-aws` and never contacts AWS.
- Runbook command consumes the exact uploaded key, SHA-256, study ID, version, and stack instance confirmation.

- [ ] **Step 1: Create the executable feature smoke**

Add:

```python
#!/usr/bin/env python3.12
"""Exercise guarded AWS study-install contracts without mutating AWS."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tests = [
        "tests/test_aws_phase2a_cli.py",
        "tests/test_aws_infrastructure.py::test_phase2a_install_study_document_is_constrained_and_verified",
        "tests/test_aws_study_installer.py",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        return completed.returncode
    regression = subprocess.run(
        [sys.executable, "scripts/smoke_aws_phase2a_template_regressions.py"],
        cwd=root,
        check=False,
    )
    if regression.returncode:
        return regression.returncode
    print("Guarded AWS study installation smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Update the runbook with the exact operation**

Add this command after `upload-study` in the reviewed actions:

```sh
python scripts/aws_phase2a.py install-study \
  studies/report-india-synthetic-0.3.0.tar.gz \
  c1cd71222657502d214a745a236018f984e1092cab0ddbf7479b5d14b15493d4 \
  report-india-synthetic \
  0.3.0 \
  --confirm-instance i-0f9ed9c133ea2358b
```

Document that operators must:

- inspect and execute the reviewed change set that creates the named document;
- verify the S3 object's `sha256` metadata;
- record `study installation command ID: COMMAND_ID`;
- never resend a failed command ID;
- inspect its invocation output once and return to source diagnosis;
- confirm `0.3.0` is active and `0.2.0` remains installed; and
- use explicit `study_installer.py --activate report-india-synthetic@0.2.0`
  plus restart/health/readiness checks for authorized rollback.

- [ ] **Step 3: Run the dedicated smoke**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' scripts/smoke_aws_guarded_study_install.py
```

Expected: `Guarded AWS study installation smoke passed` after all selected
contracts and the template regression smoke pass.

- [ ] **Step 4: Commit smoke and operations guidance**

```bash
git diff --check -- scripts/smoke_aws_guarded_study_install.py docs/aws/phase2a-runbook.md
git add -f scripts/smoke_aws_guarded_study_install.py docs/aws/phase2a-runbook.md
git commit -m "docs: add guarded study install procedure"
```

### Task 4: Verify the complete local release boundary

**Files:**
- Verify: all Task 1-3 source and tests
- Generated outside Git: `/private/tmp/epi-agent-guarded-study-install-verification`

**Interfaces:**
- Produces fresh deterministic evidence and an immutable application release candidate.
- Performs no live AWS mutation.

- [ ] **Step 1: Run the complete AWS deployment compatibility suites**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' -m pytest \
  tests/test_aws_phase2a_cli.py \
  tests/test_aws_infrastructure.py \
  tests/test_aws_host_assets.py \
  tests/test_aws_study_installer.py \
  tests/test_aws_study_access_recovery.py \
  tests/test_build_aws_release.py -q
```

Expected: every selected test passes.

- [ ] **Step 2: Run both AWS study smokes**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' scripts/smoke_aws_study_installer.py
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' scripts/smoke_aws_guarded_study_install.py
```

Expected: both print their PASS messages.

- [ ] **Step 3: Install the real `0.3.0` archive into a clean temporary root**

Choose a new explicit path if the target already exists; do not delete an
existing verification directory. Run:

```bash
mkdir -p /private/tmp/epi-agent-guarded-study-install-verification/study-root
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' study_installer.py \
  --study report-india-synthetic-0.3.0.tar.gz \
  --study-root /private/tmp/epi-agent-guarded-study-install-verification/study-root
```

Expected: `Installed: report-india-synthetic@0.3.0`. Verify the registry,
manifest format version 3, Markdown overview, and retained archive checksum in
`.installed.json`.

- [ ] **Step 4: Build an immutable application release**

Run:

```bash
'/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python' scripts/build_aws_release.py \
  --output-dir /private/tmp/epi-agent-guarded-study-install-verification/release
```

Expected: archive, checksum sidecar, and JSON release manifest are created for
the exact `aws-test` `HEAD`; the application archive excludes study archives
and includes the new operator CLI, CloudFormation template, runbook, and SSM
installer shell boundary.

- [ ] **Step 5: Record final state and stop before AWS mutation**

Run:

```bash
git status --short --branch
git log -6 --oneline
git diff --check
```

Expected: clean `aws-test`, the design/plan and three implementation commits at
the tip, and no whitespace errors.

Report the exact application archive path/hash, study archive path/hash,
release commit, current live release, current active study, and proposed
CloudFormation changes. Stop and request separate approval before
`plan-stack`, `execute-change-set`, `upload-release`, `upload-study`,
`deploy-release`, or `install-study` is run against AWS.
