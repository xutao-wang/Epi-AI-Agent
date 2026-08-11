# SSM Update Rollback Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the least-privilege SSM document lifecycle policy, recover `epi-agent-phase2a` from `UPDATE_ROLLBACK_FAILED`, and restore the corrected release-deployment document through CloudFormation.

**Architecture:** Extend only the document-scoped bootstrap policy statement with the five missing CloudFormation provider actions. Apply that policy through the administrator bootstrap stack, continue rollback without skipping resources, then recreate the previously reviewed application update.

**Tech Stack:** AWS CloudFormation, IAM, Systems Manager, YAML, Python 3.12, pytest.

## Global Constraints

- Work only in the existing isolated `aws-test` worktree.
- Keep every SSM lifecycle action scoped to `arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:document/epi-agent-*`.
- Do not add wildcard SSM access, `s3:GetObject`, or `iam:PassRole` for the document because this command document has no attachments or role parameters.
- Do not skip `EpiAgentDeployReleaseDocument` during rollback.
- Do not manually change the live SSM document default version.
- Do not deploy an application release until the stack is recovered and the corrected document is the CloudFormation-managed default.

---

### Task 1: Complete the document-scoped CloudFormation permissions

**Files:**
- Modify: `tests/test_aws_infrastructure.py`
- Modify: `scripts/smoke_aws_phase2a_template_regressions.py`
- Modify: `infra/aws/bootstrap/template.yaml`

**Interfaces:**
- Consumes: `CloudFormationExecutionPolicy` statement `ManageEpiAgentSsmDocuments`.
- Produces: an exact document-lifecycle action list scoped to `document/epi-agent-*`.

- [ ] **Step 1: Write failing permission-contract tests**

Assert that `ManageEpiAgentSsmDocuments.Action` equals this list:

```python
[
    "ssm:CreateDocument",
    "ssm:UpdateDocument",
    "ssm:UpdateDocumentDefaultVersion",
    "ssm:DeleteDocument",
    "ssm:DescribeDocument",
    "ssm:GetDocument",
    "ssm:AddTagsToResource",
    "ssm:RemoveTagsFromResource",
    "ssm:ListTagsForResource",
]
```

In the executable smoke, require at least `ssm:GetDocument` and
`ssm:UpdateDocumentDefaultVersion` in the same statement and verify its
resource remains the `document/epi-agent-*` ARN substitution.

- [ ] **Step 2: Verify RED**

Run the focused pytest and executable smoke. Expected: both fail because the
five new actions are absent.

- [ ] **Step 3: Add the exact five missing actions**

Add the actions to `ManageEpiAgentSsmDocuments` in the order shown in Step 1.
Do not change its resource, another IAM statement, or the application template.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_infrastructure.py tests/test_aws_host_assets.py tests/test_aws_phase2a_cli.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py validate
```

Expected: tests pass, smoke passes, and validation exits 0.

- [ ] **Step 5: Commit**

```bash
git add tests/test_aws_infrastructure.py scripts/smoke_aws_phase2a_template_regressions.py infra/aws/bootstrap/template.yaml
git commit -m "fix: complete SSM document lifecycle permissions"
```

### Task 2: Apply bootstrap policy and recover rollback

**Files:**
- Use: `infra/aws/bootstrap/template.yaml`
- Use: `scripts/aws_phase2a.py`

- [ ] Create an administrator bootstrap change set and require exactly one
  non-replacing modification to `CloudFormationExecutionPolicy`.
- [ ] Execute the reviewed bootstrap change set as the MFA-protected
  administrator and require `epi-agent-bootstrap` status `UPDATE_COMPLETE`.
- [ ] Verify the live inline role policy contains all nine document-scoped
  actions.
- [ ] Run `continue-update-rollback` for `epi-agent-phase2a` without
  `--resources-to-skip` and require `UPDATE_ROLLBACK_COMPLETE`.
- [ ] Verify EC2 `i-0f9ed9c133ea2358b` remains running with both original EBS
  volumes and that SSM document version 1 remains default after rollback.

### Task 3: Reapply the corrected document

**Files:**
- Use: `infra/aws/phase2a/template.yaml`
- Use: `scripts/aws_phase2a.py`

- [ ] Create a fresh application update change set using the existing domain,
  hosted-zone, email, and storage parameters.
- [ ] Require the exact seven static SSM content paths previously reviewed, no
  static direct EC2/EBS/network/storage/identity changes, and an unchanged AMI
  resolution.
- [ ] Execute the exact reviewed ARN and require `UPDATE_COMPLETE`.
- [ ] Verify the live default SSM document version declares `ENV_VAR` on all
  six parameters and not on `mainSteps[0].inputs`.
- [ ] Resume Task 3 of
  `docs/superpowers/plans/2026-08-10-ssm-env-interpolation-fix.md` only after
  these checks pass.
