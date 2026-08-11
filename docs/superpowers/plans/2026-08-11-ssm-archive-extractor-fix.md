# SSM Archive Extractor Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the release tar file open while the SSM bootstrap extractor validates and extracts it, with a functional regression executing the real embedded code.

**Architecture:** Test the Python heredoc embedded in `EpiAgentDeployReleaseDocument` as executable code rather than checking indentation text. Make the single indentation correction in the CloudFormation template, publish it as a new SSM document version, and deploy a newly checksummed release once.

**Tech Stack:** Python 3.12, `tarfile`, pytest, AWS CloudFormation, AWS Systems Manager, S3, EC2.

## Global Constraints

- Work only in the isolated `aws-test` worktree.
- Preserve every existing tar-member safety check and `filter="data"` extraction.
- Do not retry failed SSM command `67a8ee05-4db7-431d-be98-443c54613253`.
- Do not change EC2, EBS, networking, IAM, Cognito, DNS, or S3 infrastructure.
- Execute a new application change set only when the extractor indentation is the sole static direct SSM content change and the existing AMI guard passes.
- Build and deploy only a clean, commit-identified, checksum-verified release.

---

### Task 1: Execute the real embedded archive extractor in regression coverage

**Files:**
- Modify: `tests/test_aws_infrastructure.py`
- Modify: `scripts/smoke_aws_phase2a_template_regressions.py`
- Modify: `infra/aws/phase2a/template.yaml`

**Interfaces:**
- Consumes: the Python heredoc following `/usr/bin/python3.12 - "$staging_dir/release.tar.gz" "$staging_dir/release" <<'PY'` in `mainSteps[0].inputs.runCommand`.
- Produces: validated extraction of a regular safe tar member while the `TarFile` object remains open.

- [ ] **Step 1: Add a reusable functional smoke check**

In `scripts/smoke_aws_phase2a_template_regressions.py`, add imports for
`io`, `subprocess`, `sys`, `tarfile`, and `tempfile`. Add a function that:

1. Joins the document's `runCommand` values.
2. Extracts the Python text between the exact heredoc start marker and the
   following `\nPY\n` terminator.
3. Creates a temporary `release.tar.gz` containing regular file
   `payload.txt` with bytes `b"extractor-ok\n"`.
4. Runs `[sys.executable, "-", archive_path, destination]` with the extracted
   Python text on stdin.
5. Raises `AssertionError(completed.stderr)` unless the process exits 0 and
   `destination/payload.txt` contains `extractor-ok\n`.

Call this function from `main()` using the already loaded Phase 2A template.

- [ ] **Step 2: Add the focused pytest regression**

Import the reusable smoke function into `tests/test_aws_infrastructure.py` and
call it from:

```python
def test_phase2a_ssm_release_archive_extractor_runs_before_tarfile_closes() -> None:
    assert_release_archive_extractor_works(phase2a_template())
```

- [ ] **Step 3: Verify RED**

Run the focused pytest and the executable smoke. Expected: both fail with
`OSError: TarFile is closed` from the exact embedded Python code.

- [ ] **Step 4: Apply the minimal indentation correction**

In `infra/aws/phase2a/template.yaml`, move only:

```python
source.extractall(destination, members=members, filter="data")
```

into the existing `with tarfile.open(archive, "r:gz") as source:` block,
after the member-validation loop. Do not change any validation condition.

- [ ] **Step 5: Verify GREEN and template validity**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_infrastructure.py tests/test_aws_host_assets.py tests/test_aws_phase2a_cli.py -q
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py validate
```

Expected: pytest passes, smoke prints its success message, and validation exits 0.

- [ ] **Step 6: Commit**

```bash
git add tests/test_aws_infrastructure.py scripts/smoke_aws_phase2a_template_regressions.py infra/aws/phase2a/template.yaml
git commit -m "fix: extract SSM release archive while open"
```

### Task 2: Publish and deploy the corrected extractor

**Files:**
- Use: `infra/aws/phase2a/template.yaml`
- Use: `scripts/build_aws_release.py`
- Use: `scripts/aws_phase2a.py`

- [ ] Create a fresh application change set with the existing production
  parameters and verify the extractor line's indentation is the sole static
  direct SSM content change.
- [ ] Recheck that the running EC2 AMI equals the public SSM AMI value, then
  execute the exact change-set ARN and require `UPDATE_COMPLETE`.
- [ ] Verify the live default SSM document contains `source.extractall` inside
  the `tarfile.open` block and retains six parameter-level `ENV_VAR` entries.
- [ ] Build a clean immutable release, verify its SHA-256 and exclusions,
  upload it under its exact commit-based key, and verify S3 encryption,
  metadata, and version ID.
- [ ] Deploy once through the corrected default SSM document. On failure,
  retrieve the invocation output and do not retry unchanged.
- [ ] Require SSM success, local health/readiness, public HTTPS health and
  readiness, a valid certificate for `epiagent.org`, unchanged EC2/EBS, and a
  non-ALARM deployment monitoring state.
