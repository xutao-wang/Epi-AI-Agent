# Pinned AMI `www` Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pin the current application AMI so the existing stack can add the `www` DNS alias without a conditional EC2 replacement warning.

**Architecture:** `epi-agent-phase2a` remains the only stack owning the apex and `www` records. A new `ApplicationAmiId` parameter replaces the mutable Amazon Linux SSM dynamic reference; every guarded planning command passes the same initially pinned AMI ID. Future OS changes become deliberate parameter changes in separately approved maintenance releases.

**Tech Stack:** AWS CloudFormation, Route 53, EC2, Python 3.12, pytest, PyYAML, Bash, cfn-lint 1.53.1.

## Global Constraints

- Work only in the isolated `www-apex-redirect` worktree and run Python through `.venv/bin/python`.
- Initial AMI value: `ami-07a5b367e8dc8bd92`, verified from running instance `i-0f9ed9c133ea2358b` on 2026-08-13.
- `ApplicationAmiId` changes are explicit maintenance releases; routine deployments must use the unchanged pinned AMI.
- Keep `epi-agent-phase2a` as owner of apex and `www`; do not create a DNS-only stack or move the apex record.
- Do not execute a CloudFormation change set, upload a release, deploy through SSM, or run the live smoke without a separately reviewed exact change set and explicit user approval.

---

### Task 1: Add the Pinned AMI Stack Contract

**Files:**
- Modify: `tests/test_aws_infrastructure.py:501-561`
- Modify: `infra/aws/phase2a/template.yaml:13-18,25-61,410-414`
- Modify: `infra/aws/phase2a/parameters.example.json:1-9`

**Interfaces:**
- Produces: `Parameters.ApplicationAmiId`, an `AWS::EC2::Image::Id` parameter whose default and initial example value are `ami-07a5b367e8dc8bd92` and whose `AllowedPattern` is `^ami-[a-f0-9]{8,17}$`.
- Produces: `Resources.ApplicationInstance.Properties.ImageId: !Ref ApplicationAmiId`.

- [ ] **Step 1: Add a failing template contract test**

Add this test after `test_phase2a_parameters_examples_and_pinned_linter_contract`. Also extend that existing test's expected parameter-key set and example JSON list with `ApplicationAmiId`, and update `test_phase2a_compute_uses_a_hardened_single_worker_with_retained_data` to expect `{"Ref": "ApplicationAmiId"}` rather than the Amazon Linux `latest` dynamic reference.

```python
def test_phase2a_pins_application_ami_for_routine_updates() -> None:
    template = phase2a_template()
    parameters = template["Parameters"]

    assert parameters["ApplicationAmiId"] == {
        "Type": "AWS::EC2::Image::Id",
        "Default": "ami-07a5b367e8dc8bd92",
        "Description": "Pinned Amazon Linux AMI ID; change only in an approved maintenance release.",
        "AllowedPattern": "^ami-[a-f0-9]{8,17}$",
    }
    assert template["Resources"]["ApplicationInstance"]["Properties"]["ImageId"] == {
        "Ref": "ApplicationAmiId"
    }
    assert "ami-amazon-linux-latest" not in PHASE2A_TEMPLATE.read_text(encoding="utf-8")

    examples = json.loads(PHASE2A_PARAMETERS.read_text(encoding="utf-8"))
    assert {entry["ParameterKey"]: entry["ParameterValue"] for entry in examples}["ApplicationAmiId"] == (
        "ami-07a5b367e8dc8bd92"
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_infrastructure.py::test_phase2a_pins_application_ami_for_routine_updates -q
```

Expected: FAIL with `KeyError: 'ApplicationAmiId'`.

- [ ] **Step 3: Add the parameter and use it in the instance**

Add `ApplicationAmiId` to the `Application host configuration` parameter group, immediately after `InstanceType`, then add it after the `InstanceType` parameter:

```yaml
  ApplicationAmiId:
    Type: AWS::EC2::Image::Id
    Default: ami-07a5b367e8dc8bd92
    Description: Pinned Amazon Linux AMI ID; change only in an approved maintenance release.
    AllowedPattern: '^ami-[a-f0-9]{8,17}$'
```

Replace the current `ImageId` line with:

```yaml
      ImageId: !Ref ApplicationAmiId
```

Insert this JSON object after the `InstanceType` object in `parameters.example.json`:

```json
  {"ParameterKey":"ApplicationAmiId","ParameterValue":"ami-07a5b367e8dc8bd92"},
```

- [ ] **Step 4: Run the focused test and template regression smoke**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_infrastructure.py::test_phase2a_pins_application_ami_for_routine_updates -q
.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
```

Expected: `1 passed` and `Phase 2A deployment-template regression smoke passed`.

- [ ] **Step 5: Commit the stack contract**

```bash
git add infra/aws/phase2a/template.yaml infra/aws/phase2a/parameters.example.json tests/test_aws_infrastructure.py
git commit -m "feat: pin application AMI"
```

---

### Task 2: Pass and Validate the AMI Through the Guarded Operator CLI

**Files:**
- Modify: `tests/test_aws_phase2a_cli.py:67-76`
- Modify: `scripts/aws_phase2a.py:145-160`

**Interfaces:**
- Consumes: `--application-ami-id` from `plan-stack`; default `ami-07a5b367e8dc8bd92`.
- Produces: the exact CloudFormation parameter value `ParameterKey=ApplicationAmiId,ParameterValue=<AMI>` and rejects malformed IDs before creating a change set.

- [ ] **Step 1: Add failing CLI tests**

Add these tests after `test_plan_stack_parser_requires_application_parameters`:

```python
def test_plan_stack_passes_the_default_pinned_application_ami(monkeypatch) -> None:
    captured = {}

    def record_plan(*args) -> None:
        captured["parameters"] = args[-1]

    monkeypatch.setattr(cli, "plan", record_plan)
    assert cli.main([
        "plan-stack", "--domain-name", "epiagent.org", "--hosted-zone-id", "Z02132461LVJ2PFOYFXFU",
        "--certificate-email", "ops@example.org",
    ], runner=R("{}")) == 0
    assert "ParameterKey=ApplicationAmiId,ParameterValue=ami-07a5b367e8dc8bd92" in captured["parameters"]


def test_plan_stack_rejects_an_invalid_application_ami_id(monkeypatch) -> None:
    monkeypatch.setattr(cli, "plan", lambda *args: pytest.fail("plan must not run"))
    assert cli.main([
        "plan-stack", "--domain-name", "epiagent.org", "--hosted-zone-id", "Z02132461LVJ2PFOYFXFU",
        "--certificate-email", "ops@example.org", "--application-ami-id", "latest",
    ], runner=R("{}")) == 2
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_aws_phase2a_cli.py::test_plan_stack_parser_pins_the_application_ami_by_default \
  tests/test_aws_phase2a_cli.py::test_plan_stack_rejects_an_invalid_application_ami_id -q
```

Expected: FAIL because `--application-ami-id` does not exist and the default parameter is not sent.

- [ ] **Step 3: Add the CLI argument, validation, and parameter value**

Add this module constant after `STUDY_TOKEN_PATTERN`:

```python
APPLICATION_AMI_PATTERN = re.compile(r"^ami-[a-f0-9]{8,17}$")
DEFAULT_APPLICATION_AMI_ID = "ami-07a5b367e8dc8bd92"
```

In the `plan-stack` parser, add:

```python
q.add_argument("--application-ami-id", default=DEFAULT_APPLICATION_AMI_ID)
```

Extend the `plan-stack` validation condition with:

```python
or not APPLICATION_AMI_PATTERN.fullmatch(a.application_ami_id)
```

Add this entry to `vals`:

```python
"ApplicationAmiId": a.application_ami_id,
```

Keep all existing identity checks, stack parameter validation, and change-set-only behavior unchanged.

- [ ] **Step 4: Run the focused CLI and regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_phase2a_cli.py tests/test_aws_infrastructure.py -q
.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
```

Expected: all selected tests pass and the executable smoke reports success.

- [ ] **Step 5: Commit the guarded CLI contract**

```bash
git add scripts/aws_phase2a.py tests/test_aws_phase2a_cli.py
git commit -m "feat: pass pinned AMI to stack plans"
```

---

### Task 3: Document Explicit AMI Maintenance and Verify the Full Change

**Files:**
- Modify: `tests/test_aws_host_assets.py`
- Modify: `docs/aws/phase2a-runbook.md`

**Interfaces:**
- Produces: an operator rule that routine `plan-stack` calls retain the pinned AMI and that changing it is a separately approved maintenance release with an expected EC2 replacement.

- [ ] **Step 1: Add a failing runbook contract test**

Add this test next to the existing `www` rollout runbook test:

```python
def test_phase2a_runbook_requires_explicit_ami_maintenance_releases() -> None:
    source = _asset("docs/aws/phase2a-runbook.md")

    assert "--application-ami-id" in source
    assert "ami-07a5b367e8dc8bd92" in source
    assert "explicit maintenance release" in source
    assert "EC2 replacement" in source
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_host_assets.py::test_phase2a_runbook_requires_explicit_ami_maintenance_releases -q
```

Expected: FAIL because the runbook does not yet document `--application-ami-id`.

- [ ] **Step 3: Add the AMI maintenance section to the runbook**

Add this section before `## Add the www compatibility redirect`:

```markdown
## AMI maintenance releases

Routine stack updates must pass the pinned application AMI:

```sh
APPLICATION_AMI_ID="ami-07a5b367e8dc8bd92"
.venv/bin/python scripts/aws_phase2a.py plan-stack \
  --domain-name epiagent.org \
  --hosted-zone-id "$HOSTED_ZONE_ID" \
  --certificate-email "$CERT_EMAIL" \
  --alert-email "$ALERT_EMAIL" \
  --data-volume-gib 50 \
  --application-ami-id "$APPLICATION_AMI_ID"
```

Changing `APPLICATION_AMI_ID` is an explicit maintenance release. It is expected
to require EC2 replacement: first create and inspect the change set, confirm
data-recovery readiness and the maintenance window, then obtain separate
approval before execution. Do not use the Amazon Linux "latest" SSM parameter
for routine changes.
```

- [ ] **Step 4: Run the complete local verification suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_aws_infrastructure.py \
  tests/test_aws_host_assets.py \
  tests/test_aws_phase2a_cli.py \
  tests/test_build_aws_release.py \
  tests/test_smoke_aws_phase2a_real.py \
  tests/test_smoke_www_apex_redirect_real.py \
  tests/test_smoke_multi_user_isolation_real.py -q
.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
.venv/bin/python scripts/aws_phase2a.py validate
```

Expected: all pytest tests pass; the executable smoke prints `Phase 2A deployment-template regression smoke passed`; cfn-lint and AWS template validation succeed without creating a change set.

- [ ] **Step 5: Commit documentation and runbook contract**

```bash
git add -f docs/aws/phase2a-runbook.md tests/test_aws_host_assets.py
git commit -m "docs: define explicit AMI maintenance releases"
```

---

### Task 4: Create a Review-Only `www` Change Set

**Files:**
- Verify only; local artifacts under ignored `dist/aws/` and no AWS execution.

**Interfaces:**
- Consumes: committed implementation, the stack's current parameters, and the pinned AMI ID.
- Produces: an application-stack change set whose only resource action is Add `ApplicationWwwDnsRecord`.

- [ ] **Step 1: Build and verify the immutable release**

Run:

```bash
.venv/bin/python scripts/build_aws_release.py
```

Expected: release archive, SHA-256 sidecar, and manifest under `dist/aws/` with the current `git rev-parse HEAD` commit ID.

- [ ] **Step 2: Create a review-only change set with the current pinned AMI**

Run:

```bash
.venv/bin/python scripts/aws_phase2a.py plan-stack \
  --domain-name epiagent.org \
  --hosted-zone-id Z02132461LVJ2PFOYFXFU \
  --certificate-email xw488@njms.rutgers.edu \
  --alert-email xw488@njms.rutgers.edu \
  --data-volume-gib 50 \
  --application-ami-id ami-07a5b367e8dc8bd92
```

Expected: `CREATE_COMPLETE` output with exactly one resource change: Add `ApplicationWwwDnsRecord` of type `AWS::Route53::RecordSet`. Stop if the change set includes `ApplicationInstance`, an EIP association, a volume attachment, alarms due to an instance reference, or any unrelated action.

- [ ] **Step 3: Obtain explicit approval before external mutations**

Present the exact change-set ARN and complete change summary. Ask for approval to execute that exact ARN, upload and deploy the immutable release once, and run the dedicated live smoke once. Do not execute a change set or deployment based on general approval.
