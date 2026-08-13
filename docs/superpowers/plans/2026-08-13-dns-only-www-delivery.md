# DNS-Only `www` Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `www.epiagent.org` as a DNS alias to the existing apex record through a dedicated CloudFormation stack that has no EC2 resources.

**Architecture:** `epi-agent-phase2a` retains ownership of the apex application endpoint and remains unchanged. New stack `epi-agent-www-dns` owns exactly one Route 53 record, `www.${DomainName}` aliasing the apex in the existing hosted zone. The guarded CLI exposes dedicated planning and execution commands for that stack; the application release provides the already-implemented Nginx redirect and two-name TLS certificate.

**Tech Stack:** AWS CloudFormation, Route 53, Python 3.12, pytest, PyYAML, Bash, cfn-lint 1.53.1.

## Global Constraints

- Work only in the isolated `www-apex-redirect` worktree and run Python through `.venv/bin/python`.
- `epi-agent-www-dns` must contain exactly one resource: `ApplicationWwwDnsRecord` of type `AWS::Route53::RecordSet`.
- The stack must not create a hosted zone, an EC2 resource, IAM resource, certificate, or an apex DNS record.
- The CLI must create review-only change sets first; execution requires the exact change-set ARN and account confirmation.
- Do not execute a change set, upload a release, deploy through SSM, or run the live smoke without separate explicit user approval.

---

### Task 1: Move `www` DNS Ownership to the DNS-Only Stack

**Files:**
- Modify: `tests/test_aws_infrastructure.py:14-35,485-512`
- Modify: `scripts/smoke_aws_phase2a_template_regressions.py:90-125`
- Modify: `infra/aws/phase2a/template.yaml:1060-1072`
- Create: `infra/aws/www-dns/template.yaml`

**Interfaces:**
- Produces: `www_dns_template() -> dict` used by the template contract tests.
- Produces: `ApplicationWwwDnsRecord` in `infra/aws/www-dns/template.yaml`, aliasing `epiagent.org` without owning the apex record.

- [ ] **Step 1: Write failing ownership tests**

Add `WWW_DNS_TEMPLATE` and `www_dns_template()` beside the existing Phase 2A helpers. Replace the Phase 2A DNS assertion with:

```python
assert types["AWS::Route53::RecordSet"] == 1
assert "ApplicationWwwDnsRecord" not in resources
```

Add:

```python
def test_www_dns_stack_owns_only_the_compatibility_alias() -> None:
    template = www_dns_template()

    assert set(template["Parameters"]) == {"DomainName", "HostedZoneId"}
    assert template["Resources"] == {
        "ApplicationWwwDnsRecord": {
            "Type": "AWS::Route53::RecordSet",
            "Properties": {
                "HostedZoneId": {"Ref": "HostedZoneId"},
                "Name": {"Fn::Sub": "www.${DomainName}"},
                "Type": "A",
                "AliasTarget": {
                    "DNSName": {"Ref": "DomainName"},
                    "HostedZoneId": {"Ref": "HostedZoneId"},
                    "EvaluateTargetHealth": False,
                },
            },
        }
    }
```

- [ ] **Step 2: Verify the test fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_infrastructure.py::test_www_dns_stack_owns_only_the_compatibility_alias -q
```

Expected: FAIL because the DNS-only template does not yet exist.

- [ ] **Step 3: Implement the minimum DNS-only template and remove duplicate ownership**

Create `infra/aws/www-dns/template.yaml`:

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: Compatibility www DNS alias for the Epi Agent apex application.

Parameters:
  DomainName:
    Type: String
    Description: Existing apex domain name for the Epi Agent application.
    AllowedPattern: '^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$'
  HostedZoneId:
    Type: String
    Description: Existing Route 53 public hosted-zone ID for DomainName.
    AllowedPattern: '^Z[A-Z0-9]+$'

Resources:
  ApplicationWwwDnsRecord:
    Type: AWS::Route53::RecordSet
    Properties:
      HostedZoneId: !Ref HostedZoneId
      Name: !Sub 'www.${DomainName}'
      Type: A
      AliasTarget:
        DNSName: !Ref DomainName
        HostedZoneId: !Ref HostedZoneId
        EvaluateTargetHealth: false
```

Remove `ApplicationWwwDnsRecord` from the Phase 2A template. Update the regression smoke to load this new template and enforce the same one-resource contract.

- [ ] **Step 4: Verify and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_infrastructure.py -q
.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
git add infra/aws/phase2a/template.yaml infra/aws/www-dns/template.yaml
git add -f tests/test_aws_infrastructure.py scripts/smoke_aws_phase2a_template_regressions.py
git commit -m "feat: isolate www DNS alias stack"
```

Expected: all infrastructure tests and the smoke pass.

---

### Task 2: Add Guarded DNS-Only Operator Commands and Runbook

**Files:**
- Modify: `tests/test_aws_phase2a_cli.py:67-82`
- Modify: `scripts/aws_phase2a.py:10-16,142-165`
- Modify: `tests/test_aws_host_assets.py:249-270`
- Modify: `docs/aws/phase2a-runbook.md:81-140`

**Interfaces:**
- Produces: `plan-www-dns --domain-name DOMAIN --hosted-zone-id ZONE`.
- Produces: `execute-www-dns-change-set ARN --confirm-account 641379499556`, guarded to stack `epi-agent-www-dns`.

- [ ] **Step 1: Write failing CLI and runbook tests**

Add a CLI test that monkeypatches `cli.plan`, calls:

```python
cli.main([
    "plan-www-dns", "--domain-name", "epiagent.org", "--hosted-zone-id", "Z02132461LVJ2PFOYFXFU",
], runner=R("{}"))
```

and asserts the stack name equals `cli.WWW_DNS_STACK`, the template path ends in `infra/aws/www-dns/template.yaml`, and parameters are exactly `DomainName` and `HostedZoneId`. Add a source-contract test for `execute(r, a.change_set_arn, a.confirm_account, WWW_DNS_STACK)`.

Update the runbook test to require `plan-www-dns`, `execute-www-dns-change-set`, and `epi-agent-www-dns`, while rejecting `ApplicationWwwDnsRecord` ownership in the application stack.

- [ ] **Step 2: Verify tests fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_phase2a_cli.py tests/test_aws_host_assets.py -q
```

Expected: FAIL because the dedicated CLI commands and runbook procedure do not exist.

- [ ] **Step 3: Implement the guarded commands and documentation**

Add `WWW_DNS_STACK="epi-agent-www-dns"`. Add `plan-www-dns` with `--domain-name` and `--hosted-zone-id`, validate them with the existing regexes, and call `plan` with the DNS template plus only those parameter values. Add `execute-www-dns-change-set` and call `execute` with `WWW_DNS_STACK`.

Update validation to lint the bootstrap, Phase 2A, and DNS-only templates. Update the `www` runbook to create/review/execute the DNS-only stack change set, require one Add `ApplicationWwwDnsRecord`, then deploy the immutable application release through the existing guarded commands.

- [ ] **Step 4: Verify and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_phase2a_cli.py tests/test_aws_host_assets.py -q
git add -f scripts/aws_phase2a.py tests/test_aws_phase2a_cli.py docs/aws/phase2a-runbook.md tests/test_aws_host_assets.py
git commit -m "feat: guard www DNS-only stack operations"
```

Expected: selected tests pass.

---

### Task 3: Verify and Create the DNS-Only Review Change Set

**Files:**
- Verify only; ignored artifacts under `dist/aws/` are local only.

**Interfaces:**
- Produces: `epi-agent-www-dns` change set with one Add `ApplicationWwwDnsRecord` and no application-stack changes.

- [ ] **Step 1: Run full verification and build the release**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_infrastructure.py tests/test_aws_host_assets.py tests/test_aws_phase2a_cli.py tests/test_build_aws_release.py tests/test_smoke_aws_phase2a_real.py tests/test_smoke_www_apex_redirect_real.py tests/test_smoke_multi_user_isolation_real.py -q
.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
.venv/bin/python scripts/aws_phase2a.py validate
.venv/bin/python scripts/build_aws_release.py
```

Expected: all tests pass, lint/AWS template validation return zero, and the archive has the current commit ID.

- [ ] **Step 2: Create the review-only DNS change set**

Run:

```bash
.venv/bin/python scripts/aws_phase2a.py plan-www-dns \
  --domain-name epiagent.org \
  --hosted-zone-id Z02132461LVJ2PFOYFXFU
```

Expected: `CREATE_COMPLETE`, stack `epi-agent-www-dns`, and exactly one action: Add `ApplicationWwwDnsRecord` (`AWS::Route53::RecordSet`).

- [ ] **Step 3: Obtain explicit execution approval**

Present the exact change-set ARN, one-resource action, and current immutable release path. Obtain separate approval before executing the DNS change set, uploading/deploying the release, or running the live smoke.
