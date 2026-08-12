# AWS Release Identity Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AWS readiness and deployment-status endpoints report the exact immutable Git commit from the active release manifest, then publish and deploy the verified correction before creating the first Cognito user.

**Architecture:** Keep `REPORT_AGENT_RELEASE_ID` as the highest-priority explicit override. When it is absent or blank, `DeploymentState` reads the fixed root-level `release.json`, accepts only a 40-character lowercase hexadecimal `commit_sha`, and otherwise fails soft to `development`. The application code changes locally; existing immutable release tooling publishes one new commit-specific archive without CloudFormation changes.

**Tech Stack:** Python 3.12, JSON, pathlib, pytest, FastAPI, deterministic tar/gzip, AWS S3, AWS Systems Manager, Cognito, systemd, HTTPS.

## Global Constraints

- Work only in the existing isolated `aws-execution` worktree on branch `aws-test`.
- Preserve the live release while implementing and testing locally; EC2 may remain running.
- Production remains native Python/systemd; Docker is not required for this correction.
- Observe focused RED failures before changing `api/deployment.py`.
- A nonblank `REPORT_AGENT_RELEASE_ID` must continue winning over manifest discovery.
- The default manifest is the fixed repository/release root `release.json` containing `api/deployment.py`.
- Accept a manifest SHA only when it matches `^[0-9a-f]{40}$`; all file, JSON, shape, and value failures return `development` without raising.
- Do not change CloudFormation, systemd, the release installer, EBS, study data, endpoint schemas, or authentication behavior.
- Build twice from a clean commit, require byte-identical archives, upload one commit-specific object, and deploy once without automatic retry.
- Create or confirm Cognito user `xw488@njms.rutgers.edu` only after public readiness reports the exact new release commit.

## File Map

- Modify `api/deployment.py`: resolve release identity from environment or manifest.
- Modify `tests/test_api_deployment.py`: cover precedence, valid manifests, and every fail-soft case.
- Use unchanged `scripts/build_aws_release.py`: build deterministic release artifacts.
- Use unchanged `scripts/aws_phase2a.py`: upload and deploy the verified release.
- AWS operations: verify the active release and create the first Cognito invitation after all deployment gates pass.

---

### Task 1: Resolve release identity from the active manifest

**Files:**
- Modify: `tests/test_api_deployment.py`
- Modify: `api/deployment.py`

**Interfaces:**
- Consumes: `DeploymentState.from_environ(environ, release_manifest_path=None)` and root JSON `{"commit_sha": "<40 lowercase hex>"}`.
- Produces: unchanged `DeploymentState` fields with release precedence `nonblank environment > valid manifest > development`.

- [ ] **Step 1: Write valid-manifest and precedence tests**

Add imports `json`, `Path`, and `pytest`. Define:

```python
VALID_RELEASE_ID = "a" * 40


def write_manifest(tmp_path: Path, payload: object) -> Path:
    manifest = tmp_path / "release.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest
```

Add:

```python
def test_deployment_state_uses_valid_manifest_without_environment(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("REPORT_AGENT_RELEASE_ID", raising=False)
    manifest = write_manifest(tmp_path, {"commit_sha": VALID_RELEASE_ID})

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == VALID_RELEASE_ID


@pytest.mark.parametrize("configured", ["abc123", "  abc123  "])
def test_deployment_state_environment_wins_over_manifest(
    monkeypatch, tmp_path, configured
):
    monkeypatch.setenv("REPORT_AGENT_RELEASE_ID", configured)
    manifest = write_manifest(tmp_path, {"commit_sha": VALID_RELEASE_ID})

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == "abc123"


def test_deployment_state_blank_environment_uses_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("REPORT_AGENT_RELEASE_ID", "   ")
    manifest = write_manifest(tmp_path, {"commit_sha": VALID_RELEASE_ID})

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == VALID_RELEASE_ID
```

- [ ] **Step 2: Write fail-soft manifest tests**

Add:

```python
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"commit_sha": None},
        {"commit_sha": 123},
        {"commit_sha": "A" * 40},
        {"commit_sha": "a" * 39},
        {"commit_sha": "g" * 40},
    ],
)
def test_deployment_state_rejects_invalid_manifest_values(
    monkeypatch, tmp_path, payload
):
    monkeypatch.delenv("REPORT_AGENT_RELEASE_ID", raising=False)
    manifest = write_manifest(tmp_path, payload)

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == "development"


def test_deployment_state_rejects_malformed_manifest(monkeypatch, tmp_path):
    monkeypatch.delenv("REPORT_AGENT_RELEASE_ID", raising=False)
    manifest = tmp_path / "release.json"
    manifest.write_text("{", encoding="utf-8")

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == "development"


@pytest.mark.parametrize("manifest_name", ["missing.json", "manifest-directory"])
def test_deployment_state_rejects_unreadable_manifest(
    monkeypatch, tmp_path, manifest_name
):
    monkeypatch.delenv("REPORT_AGENT_RELEASE_ID", raising=False)
    manifest = tmp_path / manifest_name
    if manifest_name == "manifest-directory":
        manifest.mkdir()

    state = DeploymentState.from_environ(
        os.environ,
        release_manifest_path=manifest,
    )

    assert state.release_id == "development"
```

- [ ] **Step 3: Verify RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_api_deployment.py -q
```

Expected: new tests fail because `from_environ` does not accept
`release_manifest_path` and does not parse a manifest.

- [ ] **Step 4: Implement the minimal resolver**

In `api/deployment.py`, add `json` and `re` imports, then:

```python
_RELEASE_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DEFAULT_RELEASE_MANIFEST = Path(__file__).resolve().parents[1] / "release.json"


def release_id_from_manifest(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "development"
    if not isinstance(payload, dict):
        return "development"
    release_id = payload.get("commit_sha")
    if not isinstance(release_id, str) or not _RELEASE_ID_PATTERN.fullmatch(
        release_id
    ):
        return "development"
    return release_id
```

Change the class method to:

```python
    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
        *,
        release_manifest_path: Path | None = None,
    ) -> "DeploymentState":
        configured = str(environ.get("REPORT_AGENT_MAINTENANCE_FILE", "")).strip()
        release_id = str(environ.get("REPORT_AGENT_RELEASE_ID", "")).strip()
        if not release_id:
            release_id = release_id_from_manifest(
                release_manifest_path or _DEFAULT_RELEASE_MANIFEST
            )
        return cls(
            maintenance_file=Path(configured) if configured else None,
            release_id=release_id,
        )
```

- [ ] **Step 5: Verify GREEN and broader API behavior**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_api_deployment.py tests/test_api_server.py -q
```

Expected: all selected tests pass; readiness and deployment-status schema tests
remain unchanged.

- [ ] **Step 6: Commit the application correction**

```bash
git diff --check
git diff -- api/deployment.py tests/test_api_deployment.py
git add api/deployment.py
git add -f tests/test_api_deployment.py
git commit -m "fix: derive AWS release identity from manifest"
```

Expected: one focused application/test commit; tracked tree clean.

---

### Task 2: Run the full local release gate

**Files:** Verify application, AWS, frontend, and release tooling unchanged outside Task 1.

**Interfaces:**
- Consumes: clean manifest-fallback commit.
- Produces: fresh evidence that API, AWS deployment, frontend, and immutable builder contracts remain green.

- [ ] **Step 1: Run backend and AWS tests**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_api_deployment.py tests/test_api_server.py tests/test_aws_phase2a_cli.py tests/test_aws_infrastructure.py tests/test_aws_study_access_recovery.py tests/test_aws_study_installer.py tests/test_aws_host_assets.py tests/test_build_aws_release.py tests/test_smoke_aws_phase2a_real.py tests/test_smoke_multi_user_isolation_real.py -q
```

Expected: all selected tests pass. Docker-backed recovery tests require Docker
Desktop only as a local Linux harness; no live smoke is invoked.

- [ ] **Step 2: Run executable AWS smokes and syntax**

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_release_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_study_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
bash -n deploy/aws/bin/install-release.sh deploy/aws/bin/install-study.sh deploy/aws/bin/epi-agent-python-worker
git diff --check
```

Expected: all smokes print pass messages; syntax and diff checks exit `0`.

- [ ] **Step 3: Run frontend tests and build**

From `frontend/`, run:

```bash
npm test -- --run
npm run build -- --emptyOutDir false
```

Expected: 190 frontend tests pass and Vite builds the unchanged frontend while
preserving the tracked build manifest.

- [ ] **Step 4: Verify exact source scope**

```bash
git status --short --branch
git show --stat --oneline HEAD
git diff --check
```

Expected: clean `aws-test`; implementation commit touches only
`api/deployment.py` and `tests/test_api_deployment.py`.

---

### Task 3: Build, audit, upload, and deploy the corrected release

**Files:**
- Use unchanged: `scripts/build_aws_release.py`
- Use unchanged: `scripts/aws_phase2a.py`
- Create ignored local artifacts under `dist/aws/`

**Interfaces:**
- Consumes: clean correction commit and verified existing AWS stack.
- Produces: one new local/archive SHA, one S3 version, one deployment SSM command, and exact public release identity.

- [ ] **Step 1: Build twice and prove reproducibility**

Build to `dist/aws/release-identity-repro-one` and
`dist/aws/release-identity-repro-two`, then require `cmp` exit `0` and equal
`shasum -a 256` output. Audit members for unsafe paths, runtime databases,
study archives, `.env` files, credentials, and secrets. Verify the archive
contains the new `api/deployment.py` and root `release.json` commit ID.

- [ ] **Step 2: Build and checksum the handoff artifact**

Run the builder to `dist/aws`, verify its `.sha256` sidecar from that directory,
and record commit ID, SHA-256, size, manifest, and archive path. Require tracked
Git status clean.

- [ ] **Step 3: Upload once and verify S3 identity**

Confirm `releases/COMMIT.tar.gz` is absent, upload through
`aws_phase2a.py upload-release`, then require the exact size, SHA-256 metadata,
AES-256 encryption, and a nonempty S3 version ID.

- [ ] **Step 4: Deploy exactly once**

Require EC2 running and SSM Online. Invoke `deploy-release` once with the exact
key, SHA-256, commit ID, `epiagent.org`, and `xw488@njms.rutgers.edu`. Record the
new command ID and require terminal `Success`; do not resend on failure.

- [ ] **Step 5: Verify host and public release identity**

Through read-only SSM, require:

- `/opt/epi-agent/current` resolves to `/opt/epi-agent/releases/COMMIT`;
- current `release.json.commit_sha` equals `COMMIT`;
- `epi-agent.service` is active/enabled and contains `RuntimeDirectory`;
- `/run/epi-agent` remains `epi-agent-web:epi-agent-web 750`;
- local readiness reports `release_id: COMMIT`.

Require public HTTPS `/api/health` is `ok`, `/api/readiness` is ready with exact
`COMMIT`, and `/` returns HTTP 200.

---

### Task 4: Create or confirm the first Cognito user

**Files:** No repository modifications.

**Interfaces:**
- Consumes: verified public release and pool `us-east-1_USgB5GMZU`.
- Produces: one invited Cognito account for `xw488@njms.rutgers.edu`, or a verified existing account without duplication.

- [ ] **Step 1: Check for the exact user**

Use `cognito-idp list-users` with a server-side email filter in the exact pool.
If more than one result exists or the result's email attribute differs, stop.

- [ ] **Step 2: Create only when absent**

If absent, run `admin-create-user` for username/email
`xw488@njms.rutgers.edu`, set the `email` attribute, and allow Cognito's default
invitation delivery. Do not put any temporary password on the command line.

- [ ] **Step 3: Verify invitation state**

Run `admin-get-user` and require the email attribute equals the requested
address, `Enabled` is true, and status is `FORCE_CHANGE_PASSWORD` for a new
invitation. If the user already existed, preserve and report its existing
status without modifying credentials.

- [ ] **Step 4: Report the first-login action**

Tell the user to open `https://epiagent.org`, sign in with the emailed temporary
credential, and choose a permanent password satisfying the 14-character policy.
Do not request or expose that password in Codex.
