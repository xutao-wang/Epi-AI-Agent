# SSM Environment-Variable Interpolation Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Phase 2A Systems Manager release document export all deployment parameters as `SSM_...` environment variables so the immutable release installer can run.

**Architecture:** Keep deployment values in typed SSM document parameters with anchored allow-list patterns. Declare `interpolationType: ENV_VAR` on each parameter, leave shell source free of direct `{{ parameter }}` interpolation, and update the live command document through a reviewed CloudFormation change set before building and deploying a new commit-based release.

**Tech Stack:** AWS CloudFormation, AWS Systems Manager Command documents schema 2.2, YAML, Python 3.12, pytest, cfn-lint, S3, EC2, Nginx, FastAPI.

## Global Constraints

- Work only in the existing isolated `aws-test` worktree.
- Use Python 3.12 through `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python`.
- Do not retry SSM command `81a30900-7fcc-4e7d-b6d8-83eaf369f6d9`.
- Do not interpolate operator values directly into shell source.
- The CloudFormation update must modify only `EpiAgentDeployReleaseDocument` (`AWS::SSM::Document`) with no replacement.
- Build and deploy only from a clean tracked tree and use the resulting exact commit SHA and archive SHA-256.
- Do not place a provider API key in Git, S3 command arguments, CloudFormation, or shell history.

---

### Task 1: Correct SSM parameter environment interpolation

**Files:**
- Modify: `tests/test_aws_infrastructure.py`
- Modify: `scripts/smoke_aws_phase2a_template_regressions.py`
- Modify: `infra/aws/phase2a/template.yaml`

**Interfaces:**
- Consumes: `EpiAgentDeployReleaseDocument.Properties.Content.parameters` and `mainSteps[0].inputs`.
- Produces: six SSM parameters whose environment-variable names are `SSM_Bucket`, `SSM_ReleaseKey`, `SSM_ReleaseSha256`, `SSM_ReleaseId`, `SSM_DomainName`, and `SSM_CertificateEmail`.

- [ ] **Step 1: Write the failing unit regression test**

Replace the obsolete step-level assertion in `test_phase2a_ssm_release_document_uses_strict_environment_interpolation` with parameter-level assertions:

```python
    parameters = content["parameters"]
    parameter_names = (
        "Bucket",
        "ReleaseKey",
        "ReleaseSha256",
        "ReleaseId",
        "DomainName",
        "CertificateEmail",
    )
    assert "interpolationType" not in inputs
    for name in parameter_names:
        assert parameters[name]["interpolationType"] == "ENV_VAR"
        assert parameters[name]["allowedPattern"].startswith("^")
```

Add the same contract to the executable smoke after loading `parameters`:

```python
    parameter_names = (
        "Bucket",
        "ReleaseKey",
        "ReleaseSha256",
        "ReleaseId",
        "DomainName",
        "CertificateEmail",
    )
    inputs = phase2a["Resources"]["EpiAgentDeployReleaseDocument"]["Properties"][
        "Content"
    ]["mainSteps"][0]["inputs"]
    if "interpolationType" in inputs:
        raise AssertionError("ENV_VAR interpolation must be declared on SSM parameters")
    for name in parameter_names:
        if parameters[name].get("interpolationType") != "ENV_VAR":
            raise AssertionError(f"{name} does not export its SSM environment variable")
```

- [ ] **Step 2: Run the focused test and smoke to verify RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_infrastructure.py::test_phase2a_ssm_release_document_uses_strict_environment_interpolation -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
```

Expected: both fail because the six parameters lack `interpolationType` and the step inputs contain it.

- [ ] **Step 3: Implement the minimal template correction**

For each of `Bucket`, `ReleaseKey`, `ReleaseSha256`, `ReleaseId`, `DomainName`, and `CertificateEmail`, add:

```yaml
            interpolationType: ENV_VAR
```

Remove this line from `mainSteps[0].inputs`:

```yaml
              interpolationType: ENV_VAR
```

Do not change the patterns or shell commands.

- [ ] **Step 4: Verify GREEN and CloudFormation validity**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_infrastructure.py tests/test_aws_host_assets.py tests/test_aws_phase2a_cli.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py validate
```

Expected: all pytest tests pass, the smoke prints `Phase 2A deployment-template regression smoke passed`, and validation exits 0.

- [ ] **Step 5: Commit the tested correction**

```bash
git add tests/test_aws_infrastructure.py scripts/smoke_aws_phase2a_template_regressions.py infra/aws/phase2a/template.yaml
git commit -m "fix: export SSM deployment parameters"
```

### Task 2: Update the live SSM command document

**Files:**
- Use: `infra/aws/phase2a/template.yaml`
- Use: `scripts/aws_phase2a.py`

**Interfaces:**
- Consumes: the committed corrected CloudFormation template and live stack `epi-agent-phase2a`.
- Produces: a reviewed `UPDATE` change set that modifies only `EpiAgentDeployReleaseDocument` without replacement.

- [ ] **Step 1: Create the application update plan**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py plan-stack --domain-name epiagent.org --hosted-zone-id Z02132461LVJ2PFOYFXFU --certificate-email xw488@njms.rutgers.edu --alert-email xw488@njms.rutgers.edu --data-volume-gib 50
```

Expected: change-set status `CREATE_COMPLETE`, execution status `AVAILABLE`, one `Modify` action for `EpiAgentDeployReleaseDocument`, and `Replacement: False`.

- [ ] **Step 2: Inspect the exact change set and stop on scope expansion**

Use `aws cloudformation describe-change-set --include-property-values` with the returned ARN. Confirm there are no `Add`, `Remove`, `Import`, or replacement actions. If any other logical resource appears, do not execute.

- [ ] **Step 3: Execute the exact reviewed change-set ARN**

Run `scripts/aws_phase2a.py execute-change-set` with the exact ARN returned in Step 1 and `--confirm-account 641379499556`.

Expected: `epi-agent-phase2a` reaches `UPDATE_COMPLETE` and `EpiAgentDeployReleaseDocument` reaches `UPDATE_COMPLETE`.

- [ ] **Step 4: Verify the live SSM document schema**

Read the default version with `aws ssm get-document --name epi-agent-deploy-release --document-format YAML`. Confirm all six parameters declare `interpolationType: ENV_VAR` and the `installRelease` inputs do not.

### Task 3: Build, upload, deploy, and verify the corrected release

**Files:**
- Use: `scripts/build_aws_release.py`
- Use: `scripts/aws_phase2a.py`
- Create ignored artifacts whose filenames are emitted by `scripts/build_aws_release.py` from the exact 40-character HEAD commit SHA: one `.tar.gz`, one `.sha256`, and one `.json` file under `dist/aws/`

**Interfaces:**
- Consumes: clean committed `aws-test` HEAD, private bucket `epi-agent-phase2a-applicationbucket-who6tewa8rga`, instance `i-0f9ed9c133ea2358b`.
- Produces: an activated release under `/opt/epi-agent/releases/` named by the exact manifest `commit_sha`, public HTTPS health, and an auditable SSM command result.

- [ ] **Step 1: Build and verify the immutable release**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws
```

Read the emitted JSON manifest for the exact `commit_sha` and `archive_sha256`. From `dist/aws`, run `shasum -a 256 -c` against the emitted sidecar. Confirm the tar listing contains no `.env`, `runtime/`, `study_data/`, or `report-india-*.tar.gz` entry.

- [ ] **Step 2: Upload the exact release object**

Run `scripts/aws_phase2a.py upload-release` with the emitted archive. Construct the key by placing the exact 40-character manifest `commit_sha` between `releases/` and `.tar.gz`. Verify `head-object` reports AES256 encryption and metadata `sha256` equal to the manifest.

- [ ] **Step 3: Deploy the exact release once**

Run `scripts/aws_phase2a.py deploy-release` with the exact release key, archive SHA-256, commit SHA, `epiagent.org`, and `xw488@njms.rutgers.edu`.

Expected: the SSM command reaches `Success`. On failure, retrieve its invocation output and do not retry unchanged.

- [ ] **Step 4: Verify service and public endpoint**

Confirm:

- EC2 instance `i-0f9ed9c133ea2358b` is `running` and SSM is `Online`.
- The latest `epi-agent-deploy-release` command is `Success`.
- `https://epiagent.org/api/health` returns HTTP 200.
- `https://epiagent.org/api/readiness` returns HTTP 200 with JSON status `ready`.
- The certificate presented for `epiagent.org` is valid for that hostname.
- CloudFormation remains `UPDATE_COMPLETE`.
- CloudWatch service-error alarm is not `ALARM` because of this deployment.

- [ ] **Step 5: Record deployment identity**

Report the deployed commit SHA, archive SHA-256, S3 object version, SSM command ID, instance ID, public URL, and any remaining operational step such as creating the first Cognito user or uploading the synthetic study package.
