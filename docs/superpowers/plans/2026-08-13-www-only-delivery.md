# `www`-Only Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver `www.epiagent.org` as a redirect to canonical `https://epiagent.org` without changing the currently deployed EC2 image contract.

**Architecture:** Preserve the pinned-AMI implementation on a separate local maintenance branch. Restore the active `www-apex-redirect` branch to the existing Amazon Linux SSM image reference and its existing guarded CLI interface, so CloudFormation sees only the addition of `ApplicationWwwDnsRecord`. An AMI parameter change is intentionally deferred to a separately approved maintenance release.

**Tech Stack:** AWS CloudFormation, Route 53, EC2, Python 3.12, pytest, PyYAML, Bash, cfn-lint 1.53.1.

## Global Constraints

- Work only in the isolated `www-apex-redirect` worktree and run Python through `.venv/bin/python`.
- The immediate application-stack change set must contain exactly one action: Add `ApplicationWwwDnsRecord` of type `AWS::Route53::RecordSet`.
- Preserve AMI pinning at commit `f5c9a75950707fbc5290a63b2f8da129bee487f7` on local branch `planned-ami-maintenance`; do not merge it into this release.
- Do not execute a CloudFormation change set, upload a release, deploy through SSM, or run the live smoke without a separately reviewed exact change set and explicit user approval.

---

### Task 1: Preserve the Planned Maintenance Release and Restore the DNS-Only Stack Contract

**Files:**
- Modify: `tests/test_aws_infrastructure.py:515-580`
- Modify: `infra/aws/phase2a/template.yaml:12-47,410-414`
- Modify: `infra/aws/phase2a/parameters.example.json:1-7`

**Interfaces:**
- Produces: the existing dynamic `ImageId` expression, unchanged from the deployed stack.
- Produces: a template containing the already-implemented `ApplicationWwwDnsRecord` and no `ApplicationAmiId` parameter.

- [ ] **Step 1: Preserve the maintenance implementation separately**

Run:

```bash
git branch planned-ami-maintenance f5c9a75950707fbc5290a63b2f8da129bee487f7
git show --quiet --oneline planned-ami-maintenance
```

Expected: the branch points at `f5c9a75 docs: define explicit AMI maintenance releases`.

- [ ] **Step 2: Write a failing contract test for the unchanged image expression**

In `test_phase2a_compute_uses_a_hardened_single_worker_with_retained_data`, replace the AMI assertion with:

```python
assert properties["ImageId"] == "{{resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64}}"
```

Remove `test_phase2a_pins_application_ami_for_routine_updates`, and remove `ApplicationAmiId` from the expected parameter keys and JSON example list.

- [ ] **Step 3: Run the test to verify it fails against the pinned implementation**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_infrastructure.py::test_phase2a_compute_uses_a_hardened_single_worker_with_retained_data -q
```

Expected: FAIL because `ImageId` currently references `ApplicationAmiId`.

- [ ] **Step 4: Restore the deployed AMI contract**

Remove the `ApplicationAmiId` parameter, its parameter-group entry, and its example JSON object. Restore:

```yaml
      ImageId: '{{resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64}}'
```

- [ ] **Step 5: Verify and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_infrastructure.py -q
.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
git add -f tests/test_aws_infrastructure.py
git add infra/aws/phase2a/template.yaml infra/aws/phase2a/parameters.example.json
git commit -m "fix: keep www rollout independent of AMI maintenance"
```

Expected: tests pass and the regression smoke reports success.

---

### Task 2: Restore the Existing Guarded CLI and Documentation Scope

**Files:**
- Modify: `tests/test_aws_phase2a_cli.py:67-82`
- Modify: `scripts/aws_phase2a.py:12-16,145-156`
- Modify: `tests/test_aws_host_assets.py:249-270`
- Modify: `docs/aws/phase2a-runbook.md:81-140`

**Interfaces:**
- Produces: `plan-stack` with its existing parameters and no AMI override.
- Produces: a runbook that requires precisely one DNS resource action for the immediate release and states that AMI pinning is deferred to `planned-ami-maintenance`.

- [ ] **Step 1: Write failing negative-scope tests**

Replace the pinned-AMI CLI tests with:

```python
def test_plan_stack_www_delivery_does_not_send_an_ami_parameter(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "plan", lambda *args: captured.setdefault("parameters", args[-1]))
    assert cli.main([
        "plan-stack", "--domain-name", "epiagent.org", "--hosted-zone-id", "Z02132461LVJ2PFOYFXFU",
        "--certificate-email", "ops@example.org",
    ], runner=R("{}")) == 0
    assert not any("ApplicationAmiId" in parameter for parameter in captured["parameters"])
```

Replace the AMI maintenance runbook test with:

```python
def test_phase2a_runbook_defers_ami_pinning_from_www_rollout() -> None:
    source = _asset("docs/aws/phase2a-runbook.md")
    www_section = source.split("## Add the www compatibility redirect", 1)[1]

    assert "planned-ami-maintenance" in source
    assert "--application-ami-id" not in www_section
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_aws_phase2a_cli.py::test_plan_stack_www_delivery_does_not_send_an_ami_parameter \
  tests/test_aws_host_assets.py::test_phase2a_runbook_defers_ami_pinning_from_www_rollout -q
```

Expected: FAIL because the current CLI sends `ApplicationAmiId` and the runbook places an AMI flag in the `www` procedure.

- [ ] **Step 3: Restore scope in the CLI and runbook**

Remove `APPLICATION_AMI_PATTERN`, `DEFAULT_APPLICATION_AMI_ID`, the `--application-ami-id` argument, its validation, and the `ApplicationAmiId` parameter value from `scripts/aws_phase2a.py`.

Remove the AMI-maintenance section from the runbook. In the `www` section, keep the plan command ending at `--data-volume-gib 50`, and add this sentence after the one-action requirement:

```markdown
The separately planned AMI-pinning maintenance implementation is preserved on the local `planned-ami-maintenance` branch and is not part of this rollout.
```

- [ ] **Step 4: Verify and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_phase2a_cli.py tests/test_aws_host_assets.py -q
git add -f scripts/aws_phase2a.py tests/test_aws_phase2a_cli.py docs/aws/phase2a-runbook.md tests/test_aws_host_assets.py
git commit -m "docs: defer AMI pinning from www rollout"
```

Expected: selected tests pass.

---

### Task 3: Create and Inspect the DNS-Only Change Set

**Files:**
- Verify only; no local source changes.

**Interfaces:**
- Consumes: the restored dynamic-AMI template and current stack parameters.
- Produces: an application-stack change set with only Add `ApplicationWwwDnsRecord`.

- [ ] **Step 1: Run complete local verification**

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

Expected: all tests pass, the smoke reports success, and lint/AWS validation return zero.

- [ ] **Step 2: Build the immutable release**

Run:

```bash
.venv/bin/python scripts/build_aws_release.py
```

Expected: an archive, SHA-256 sidecar, and manifest under `dist/aws/` named with the current commit ID.

- [ ] **Step 3: Create a review-only change set**

Run:

```bash
.venv/bin/python scripts/aws_phase2a.py plan-stack \
  --domain-name epiagent.org \
  --hosted-zone-id Z02132461LVJ2PFOYFXFU \
  --certificate-email xw488@njms.rutgers.edu \
  --alert-email xw488@njms.rutgers.edu \
  --data-volume-gib 50
```

Expected: `CREATE_COMPLETE` with exactly one resource action: Add `ApplicationWwwDnsRecord` (`AWS::Route53::RecordSet`). Stop and diagnose if any other action appears.

- [ ] **Step 4: Obtain exact execution approval**

Present the exact change-set ARN and sole resource action. Obtain separate approval before executing it, uploading the release, deploying it once, or running the live smoke once.
